"""Safety tests for controlled model-generated remediation candidate JSON."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import httpx2

from app.llm import LlmSettings, enhance_task_candidates
from app.llm.client import OpenAICompatibleClient
from app.rpa import build_task_candidates
from tests.fixture_factory import report_contract


class ControlledTaskLlmTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = report_contract()
        cls.bundle = build_task_candidates(cls.contract)

    def settings(self, **updates: object) -> LlmSettings:
        values = {
            "enabled": True,
            "base_url": "https://model.example.test/v1",
            "api_style": "responses",
            "model": "task-test-model",
            "api_key": "task-test-secret",
            "max_attempts": 2,
            "retry_delay_seconds": 0,
        }
        values.update(updates)
        return LlmSettings.model_validate(values)

    def draft(self, **updates: object) -> dict[str, object]:
        candidate = self.bundle.candidates[0]
        payload: dict[str, object] = {
            "candidate_id": candidate.candidate_id,
            "task_title": candidate.task_title,
            "suggestion": candidate.suggestion,
            "suggested_priority": candidate.suggested_priority,
            "suggested_department": candidate.suggested_department,
        }
        payload.update(updates)
        return {"candidates": [payload]}

    @staticmethod
    def response(payload: dict[str, object]) -> dict[str, object]:
        return {
            "id": "task_resp_001",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": json.dumps(payload, ensure_ascii=False)}
                    ],
                }
            ],
        }

    def client_for(self, settings: LlmSettings, payload: dict[str, object]) -> OpenAICompatibleClient:
        transport = httpx2.MockTransport(lambda _: httpx2.Response(200, json=self.response(payload)))
        http_client = httpx2.Client(transport=transport)
        self.addCleanup(http_client.close)
        return OpenAICompatibleClient(settings, client=http_client, sleeper=lambda _: None)

    def test_success_changes_only_controlled_fields_and_remains_pending_review(self) -> None:
        settings = self.settings()
        baseline = self.bundle.candidates[0]
        draft = self.draft(
            task_title="复核银黄口服液成本变动证据",
            suggestion=baseline.suggestion + "；建议核查原始凭证并归档复核结果。",
        )
        result = enhance_task_candidates(
            self.contract,
            self.bundle,
            settings,
            client=self.client_for(settings, draft),
        )

        self.assertEqual(result.generation.status, "generated")
        self.assertEqual(result.generation.mode, "llm")
        self.assertEqual(result.generation.request_id, "task_resp_001")
        candidate = result.candidates[0]
        self.assertEqual(candidate.state, "pending_review")
        self.assertEqual(candidate.finding, baseline.finding)
        self.assertEqual(candidate.source_refs, baseline.source_refs)
        self.assertEqual(candidate.task_id, baseline.task_id)
        self.assertEqual(candidate.report_contract_sha256, baseline.report_contract_sha256)
        self.assertNotEqual(candidate.candidate_id, baseline.candidate_id)
        self.assertNotEqual(candidate.idempotency_key, baseline.idempotency_key)
        candidate_json = candidate.model_dump(mode="json")
        for forbidden in ("assignee", "deadline", "reviewer", "notify_method", "submission"):
            self.assertNotIn(forbidden, candidate_json)

    def test_missing_key_returns_identical_deterministic_candidates(self) -> None:
        result = enhance_task_candidates(self.contract, self.bundle, self.settings(api_key=None))
        self.assertEqual(result.candidates, self.bundle.candidates)
        self.assertEqual(result.generation.status, "fallback")
        self.assertEqual(result.generation.mode, "deterministic")
        self.assertIn("COST_LLM_API_KEY", result.generation.warnings[0])

    def test_numeric_causality_department_priority_and_execution_overreach_fall_back(self) -> None:
        baseline = self.bundle.candidates[0]
        invalid_drafts = (
            self.draft(suggestion="建议核查99次并归档结果。"),
            self.draft(suggested_department="未经批准部门"),
            self.draft(suggested_priority="low"),
            self.draft(suggestion="建议核查凭证，任务已审批并提交。"),
            self.draft(suggestion="市场价格上涨导致成本增加，建议核查凭证。"),
        )
        for draft in invalid_drafts:
            with self.subTest(draft=draft):
                settings = self.settings()
                result = enhance_task_candidates(
                    self.contract,
                    self.bundle,
                    settings,
                    client=self.client_for(settings, draft),
                )
                self.assertEqual(result.candidates[0], baseline)
                self.assertEqual(result.generation.status, "fallback")
                self.assertTrue(result.generation.warnings)

    def test_schema_is_strict_and_prompt_protects_human_approval_fields(self) -> None:
        settings = self.settings()
        requests: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            requests.append(request)
            return httpx2.Response(200, json=self.response(self.draft()))

        http_client = httpx2.Client(transport=httpx2.MockTransport(handler))
        self.addCleanup(http_client.close)
        result = enhance_task_candidates(
            self.contract,
            self.bundle,
            settings,
            client=OpenAICompatibleClient(settings, client=http_client, sleeper=lambda _: None),
        )
        self.assertEqual(result.generation.status, "generated")
        payload = json.loads(requests[0].content)
        self.assertEqual(payload["text"]["format"]["name"], "cost_remediation_candidates")
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertFalse(payload["text"]["format"]["schema"]["additionalProperties"])
        instructions = payload["instructions"]
        for protected in ("责任人", "截止日期", "审批意见", "通知方式", "RPA执行结果"):
            self.assertIn(protected, instructions)


if __name__ == "__main__":
    unittest.main()

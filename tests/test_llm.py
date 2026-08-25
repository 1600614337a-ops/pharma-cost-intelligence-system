"""Tests for controlled model wording, structured output, and safe fallback."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import httpx2

from app.llm import LlmSettings, enhance_report_contract
from app.llm.client import OpenAICompatibleClient
from app.llm.models import NARRATIVE_FIELDS
from app.reporting import build_report_contract
from app.dashboard.period_reports import build_period_report_contract


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_ROOT = PROJECT_ROOT / "06_知识证据索引"


class ControlledLlmTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = build_report_contract(
            PROJECT_ROOT,
            INDEX_ROOT,
            "银黄口服液",
            "2026-05",
            generated_date="2026-08-03",
        )

    def settings(self, **updates: object) -> LlmSettings:
        values = {
            "enabled": True,
            "base_url": "https://model.example.test/v1",
            "api_style": "responses",
            "model": "test-model",
            "api_key": "super-secret-test-key",
            "max_attempts": 3,
            "retry_delay_seconds": 0,
        }
        values.update(updates)
        return LlmSettings.model_validate(values)

    def draft(self, **updates: str) -> dict[str, str]:
        payload = {name: self.contract.fields[name].value for name in NARRATIVE_FIELDS}
        payload.update(updates)
        return payload

    @staticmethod
    def draft_for(contract, **updates: str) -> dict[str, str]:
        payload = {name: contract.fields[name].value for name in NARRATIVE_FIELDS}
        payload.update(updates)
        return payload

    @staticmethod
    def responses_payload(draft: dict[str, str]) -> dict[str, object]:
        return {
            "id": "resp_test_001",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(draft, ensure_ascii=False)}],
                }
            ],
        }

    def client_for_handler(self, settings: LlmSettings, handler) -> OpenAICompatibleClient:
        transport = httpx2.MockTransport(handler)
        http_client = httpx2.Client(transport=transport)
        self.addCleanup(http_client.close)
        return OpenAICompatibleClient(settings, client=http_client, sleeper=lambda _: None)

    def test_disabled_mode_returns_unchanged_deterministic_contract(self) -> None:
        result = enhance_report_contract(self.contract, LlmSettings())
        self.assertEqual(result, self.contract)
        self.assertEqual(result.generation.status, "not_requested")

    def test_missing_key_falls_back_without_modifying_fields(self) -> None:
        settings = self.settings(api_key=None)
        result = enhance_report_contract(self.contract, settings)
        self.assertEqual(result.fields, self.contract.fields)
        self.assertEqual(result.generation.status, "fallback")
        self.assertIn("COST_LLM_API_KEY", result.generation.warnings[0])

    def test_responses_api_uses_strict_schema_and_only_rewrites_allowed_fields(self) -> None:
        settings = self.settings()
        requested: list[httpx2.Request] = []
        rewritten = self.draft(本月亮点=self.contract.fields["本月亮点"].value + "建议持续跟踪。")

        def handler(request: httpx2.Request) -> httpx2.Response:
            requested.append(request)
            return httpx2.Response(
                200,
                json=self.responses_payload(rewritten),
                headers={"x-request-id": "req_header_001"},
            )

        result = enhance_report_contract(
            self.contract,
            settings,
            client=self.client_for_handler(settings, handler),
        )
        self.assertEqual(result.generation.status, "generated")
        self.assertEqual(result.generation.mode, "llm")
        self.assertEqual(result.generation.request_id, "req_header_001")
        self.assertEqual(result.fields["本月亮点"].value, rewritten["本月亮点"])
        for name in set(self.contract.fields) - set(NARRATIVE_FIELDS):
            self.assertEqual(result.fields[name], self.contract.fields[name], name)
        request_payload = json.loads(requested[0].content)
        self.assertEqual(requested[0].url.path, "/v1/responses")
        self.assertIn("instructions", request_payload)
        self.assertIn("input", request_payload)
        self.assertIn("差异归因分析文本必须分为2至4个自然段", request_payload["instructions"])
        output_format = request_payload["text"]["format"]
        self.assertEqual(output_format["type"], "json_schema")
        self.assertTrue(output_format["strict"])
        self.assertFalse(output_format["schema"]["additionalProperties"])

    def test_model_can_rewrite_governed_recommendations_without_executing_them(self) -> None:
        settings = self.settings()
        requested: list[httpx2.Request] = []
        baseline_rows = self.contract.dynamic_tables["改进建议表格"].rows
        recommendations = [
            {
                "sequence": row[0],
                "action": row[1] + "并形成复核记录",
                "owner": row[2],
                "priority": row[3],
                "expected_effect": row[4],
                "due": "待业务审批",
            }
            for row in baseline_rows
        ]
        draft = self.draft()
        draft["改进建议列表"] = recommendations

        def handler(request: httpx2.Request) -> httpx2.Response:
            requested.append(request)
            return httpx2.Response(200, json=self.responses_payload(draft))

        result = enhance_report_contract(
            self.contract,
            settings,
            client=self.client_for_handler(settings, handler),
        )
        self.assertEqual(result.generation.status, "generated")
        generated_rows = result.dynamic_tables["改进建议表格"].rows
        self.assertEqual(len(generated_rows), len(baseline_rows))
        self.assertTrue(all(row[1].endswith("并形成复核记录") for row in generated_rows))
        self.assertTrue(all(row[5] == "待业务审批" for row in generated_rows))
        prompt = json.loads(requested[0].content)["input"]
        self.assertIn("对标差异结构", prompt)
        self.assertIn("确定性改进建议候选", prompt)

    def test_model_cannot_change_recommendation_count_order_or_approval_state(self) -> None:
        settings = self.settings()
        baseline_rows = self.contract.dynamic_tables["改进建议表格"].rows
        recommendation = {
            "sequence": baseline_rows[0][0],
            "action": baseline_rows[0][1],
            "owner": baseline_rows[0][2],
            "priority": baseline_rows[0][3],
            "expected_effect": baseline_rows[0][4],
            "due": "待业务审批",
        }
        invalid_lists = (
            [recommendation],
            [
                {**recommendation, "sequence": "2"},
                {
                    "sequence": "1",
                    "action": baseline_rows[1][1],
                    "owner": baseline_rows[1][2],
                    "priority": baseline_rows[1][3],
                    "expected_effect": baseline_rows[1][4],
                    "due": "待业务审批",
                },
            ],
        )
        for recommendations in invalid_lists:
            with self.subTest(recommendations=recommendations):
                draft = self.draft()
                draft["改进建议列表"] = recommendations
                client = self.client_for_handler(
                    settings,
                    lambda _: httpx2.Response(200, json=self.responses_payload(draft)),
                )
                result = enhance_report_contract(self.contract, settings, client=client)
                self.assertEqual(result.generation.status, "fallback")
                self.assertEqual(
                    result.dynamic_tables["改进建议表格"],
                    self.contract.dynamic_tables["改进建议表格"],
                )

    def test_chat_completions_compatibility_mode(self) -> None:
        settings = self.settings(api_style="chat_completions")
        requested: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            requested.append(request)
            return httpx2.Response(
                200,
                json={"id": "chat_test", "choices": [{"message": {"content": json.dumps(self.draft(), ensure_ascii=False)}}]},
            )

        result = enhance_report_contract(
            self.contract,
            settings,
            client=self.client_for_handler(settings, handler),
        )
        self.assertEqual(result.generation.status, "generated")
        payload = json.loads(requested[0].content)
        self.assertEqual(requested[0].url.path, "/v1/chat/completions")
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertEqual(len(payload["messages"]), 2)

        qwen_settings = self.settings(
            provider="dashscope",
            api_style="chat_completions",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model="qwen3.8-max",
        )
        qwen_requested: list[httpx2.Request] = []

        def qwen_handler(request: httpx2.Request) -> httpx2.Response:
            qwen_requested.append(request)
            return httpx2.Response(
                200,
                json={"id": "qwen_test", "choices": [{"message": {"content": json.dumps(self.draft(), ensure_ascii=False)}}]},
            )

        qwen_result = enhance_report_contract(
            self.contract,
            qwen_settings,
            client=self.client_for_handler(qwen_settings, qwen_handler),
        )
        self.assertEqual(qwen_result.generation.status, "generated")
        qwen_payload = json.loads(qwen_requested[0].content)
        self.assertEqual(qwen_requested[0].url.path, "/compatible-mode/v1/chat/completions")
        self.assertEqual(qwen_payload["model"], "qwen3.8-max")
        self.assertEqual(qwen_payload["response_format"], {"type": "json_object"})
        self.assertFalse(qwen_payload["enable_thinking"])
        self.assertFalse(qwen_payload["preserve_thinking"])
        self.assertNotIn("max_tokens", qwen_payload)
        self.assertIn("JSON Schema", qwen_payload["messages"][1]["content"])
        self.assertIn('"additionalProperties":false', qwen_payload["messages"][1]["content"])

    def test_quarterly_and_special_reports_share_the_controlled_llm_pipeline(self) -> None:
        settings = self.settings()
        scenarios = (
            build_period_report_contract(
                PROJECT_ROOT,
                INDEX_ROOT,
                analysis_type="季度成本分析",
                product="银黄口服液",
                quarter="2026-Q2",
                generated_date="2026-08-21",
            ),
            build_period_report_contract(
                PROJECT_ROOT,
                INDEX_ROOT,
                analysis_type="专题分析",
                product="银黄口服液",
                month="2026-05",
                topic="工厂成本差异专项",
                generated_date="2026-08-21",
            ),
        )
        prompts: list[str] = []
        for contract in scenarios:
            draft = self.draft_for(contract)

            def handler(request: httpx2.Request, *, response=draft) -> httpx2.Response:
                prompts.append(request.content.decode("utf-8"))
                return httpx2.Response(200, json=self.responses_payload(response))

            result = enhance_report_contract(
                contract,
                settings,
                client=self.client_for_handler(settings, handler),
            )
            self.assertEqual(result.generation.status, "generated")
            self.assertEqual(result.generation.mode, "llm")
            self.assertEqual(result.analysis_type, contract.analysis_type)
            self.assertEqual(result.period, contract.period)
        self.assertTrue(any("季度成本分析" in prompt and "2026-Q2" in prompt for prompt in prompts))
        self.assertTrue(any("专题分析" in prompt and "工厂成本差异专项" in prompt for prompt in prompts))

    def test_retryable_status_is_bounded_and_audited(self) -> None:
        settings = self.settings(max_attempts=2)
        calls = 0

        def handler(_: httpx2.Request) -> httpx2.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx2.Response(429, json={"error": "rate limited"})
            return httpx2.Response(200, json=self.responses_payload(self.draft()))

        result = enhance_report_contract(
            self.contract,
            settings,
            client=self.client_for_handler(settings, handler),
        )
        self.assertEqual(calls, 2)
        self.assertEqual(result.generation.attempt_count, 2)
        self.assertEqual(result.generation.status, "generated")

    def test_exhausted_retry_falls_back_with_attempt_count(self) -> None:
        settings = self.settings(max_attempts=2)
        calls = 0

        def handler(_: httpx2.Request) -> httpx2.Response:
            nonlocal calls
            calls += 1
            return httpx2.Response(503, json={"error": "unavailable"})

        result = enhance_report_contract(
            self.contract,
            settings,
            client=self.client_for_handler(settings, handler),
        )
        self.assertEqual(calls, 2)
        self.assertEqual(result.generation.status, "fallback")
        self.assertEqual(result.generation.attempt_count, 2)
        self.assertEqual(result.fields, self.contract.fields)

    def test_timeout_is_retried_then_safely_falls_back(self) -> None:
        settings = self.settings(max_attempts=2)
        calls = 0

        def handler(request: httpx2.Request) -> httpx2.Response:
            nonlocal calls
            calls += 1
            raise httpx2.ReadTimeout("simulated timeout", request=request)

        result = enhance_report_contract(
            self.contract,
            settings,
            client=self.client_for_handler(settings, handler),
        )
        self.assertEqual(calls, 2)
        self.assertEqual(result.generation.status, "fallback")
        self.assertEqual(result.generation.attempt_count, 2)
        self.assertIn("ReadTimeout", result.generation.warnings[0])
        self.assertEqual(result.fields, self.contract.fields)

    def test_invalid_schema_and_refusal_fall_back(self) -> None:
        settings = self.settings()
        invalid_payloads = [
            self.responses_payload({"本月亮点": "字段不完整"}),
            {"id": "refusal", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload.get("id")):
                client = self.client_for_handler(settings, lambda _: httpx2.Response(200, json=payload))
                result = enhance_report_contract(self.contract, settings, client=client)
                self.assertEqual(result.generation.status, "fallback")
                self.assertEqual(result.fields, self.contract.fields)

    def test_unapproved_number_false_causality_and_unsegmented_attribution_are_rejected(self) -> None:
        settings = self.settings()
        drafts = [
            self.draft(本月亮点="单位成本改善99.99%。"),
            self.draft(需关注问题="市场价格上涨导致成本费用增加。"),
            self.draft(差异归因分析文本="总体差异、原因研判和证据边界均在同一段。"),
        ]
        for draft in drafts:
            with self.subTest(text=draft["本月亮点"]):
                payload = self.responses_payload(draft)
                client = self.client_for_handler(settings, lambda _: httpx2.Response(200, json=payload))
                result = enhance_report_contract(self.contract, settings, client=client)
                self.assertEqual(result.generation.status, "fallback")
                self.assertEqual(result.fields, self.contract.fields)

    def test_material_attribution_cannot_drop_contribution_or_actions(self) -> None:
        settings = self.settings()
        drafts = [
            self.draft(材料成本归因分析文本="直接材料成本发生变化。建议核查相关凭证。"),
            self.draft(
                材料成本归因分析文本=(
                    f"直接材料对总成本变动贡献度为{self.contract.fields['材料贡献度'].value}。"
                )
            ),
        ]
        for draft in drafts:
            payload = self.responses_payload(draft)
            client = self.client_for_handler(
                settings, lambda _: httpx2.Response(200, json=payload)
            )
            result = enhance_report_contract(self.contract, settings, client=client)
            self.assertEqual(result.generation.status, "fallback")
            self.assertEqual(result.fields, self.contract.fields)

    def test_api_key_is_not_exposed_by_settings_or_failure_metadata(self) -> None:
        settings = self.settings()
        self.assertNotIn("super-secret-test-key", repr(settings))
        dumped = settings.model_dump(mode="json")
        self.assertNotEqual(dumped["api_key"], "super-secret-test-key")
        client = self.client_for_handler(settings, lambda _: httpx2.Response(401, json={"error": "bad key"}))
        result = enhance_report_contract(self.contract, settings, client=client)
        self.assertNotIn("super-secret-test-key", json.dumps(result.model_dump(mode="json"), ensure_ascii=False))

    def test_environment_configuration_rejects_plain_http_remote_endpoint(self) -> None:
        with self.assertRaises(ValueError):
            LlmSettings.from_env(
                {
                    "COST_LLM_ENABLED": "true",
                    "COST_LLM_BASE_URL": "http://remote.example.test/v1",
                    "COST_LLM_API_KEY": "key",
                }
            )
        qwen = LlmSettings.from_env(
            {
                "COST_LLM_ENABLED": "true",
                "COST_LLM_PROVIDER": "dashscope",
                "COST_LLM_API_STYLE": "chat_completions",
                "COST_LLM_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "COST_LLM_MODEL": "qwen3.8-max",
                "COST_LLM_API_KEY": "key",
            }
        )
        self.assertEqual(qwen.provider, "dashscope")
        self.assertEqual(qwen.api_style, "chat_completions")
        self.assertEqual(qwen.model, "qwen3.8-max")
        with self.assertRaises(ValueError):
            LlmSettings.from_env(
                {
                    "COST_LLM_ENABLED": "true",
                    "COST_LLM_PROVIDER": "dashscope",
                    "COST_LLM_API_STYLE": "responses",
                    "COST_LLM_API_KEY": "key",
                }
            )


if __name__ == "__main__":
    unittest.main()

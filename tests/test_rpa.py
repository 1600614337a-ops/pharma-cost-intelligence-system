"""Regression tests for the human-gated RPA task workflow."""

from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from pydantic import ValidationError

from app.reporting.models import ReportContract
from app.rpa import (
    JsonSubmissionLedger,
    RpaClient,
    RpaSubmissionError,
    RpaTaskCreateRequest,
    RpaWorkflowError,
    TransportResponse,
    approve_candidate,
    build_task_candidates,
    reject_candidate,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_JSON = PROJECT_ROOT / "07_报告输出" / "2026-05_银黄口服液_月度成本分析报告.json"


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict, float]] = []

    def post_json(self, url: str, payload: dict, timeout: float) -> TransportResponse:
        self.calls.append((url, payload, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def success_response(task_id: str = "TASK-202605-101") -> TransportResponse:
    return TransportResponse(
        status_code=200,
        payload={
            "code": 200,
            "message": "任务创建成功，已分发至责任人",
            "data": {
                "task_id": task_id,
                "status": "sent",
                "notify_status": {"email": "已发送"},
                "tracking_url": f"http://localhost:8090/api/rpa/tasks/{task_id}",
            },
        },
    )


class RpaFixtureMixin:
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = ReportContract.model_validate_json(REPORT_JSON.read_text(encoding="utf-8"))
        cls.bundle = build_task_candidates(cls.contract)
        cls.candidate = cls.bundle.candidates[0]

    def approve(self):
        return approve_candidate(
            self.candidate,
            reviewer="成本平台主管",
            decided_at=datetime.fromisoformat("2026-08-02T10:30:00+08:00"),
            assignee_name="测试责任人",
            department="成本管理部",
            role="成本分析员",
            deadline=date.fromisoformat("2026-08-31"),
            priority="medium",
            notify_method="email",
            comment="仅用于模拟接口验收",
        )


class RpaWorkflowTests(RpaFixtureMixin, unittest.TestCase):

    def test_candidate_is_deterministic_and_source_grounded(self) -> None:
        repeated = build_task_candidates(self.contract).candidates[0]
        self.assertEqual(self.candidate.idempotency_key, repeated.idempotency_key)
        self.assertEqual(self.candidate.candidate_id, repeated.candidate_id)
        self.assertEqual(self.candidate.task_id, "TASK-202605-101")
        self.assertEqual(self.candidate.state, "pending_review")
        self.assertEqual(self.candidate.suggested_department, None)
        self.assertEqual(len(self.candidate.source_refs), 6)
        self.assertTrue(all(Path(path).is_file() for path in self.candidate.source_refs))

    def test_candidate_uses_governed_finding_not_interface_example(self) -> None:
        finding = self.candidate.finding
        self.assertIn("金银花变动0.15元/盒", finding)
        self.assertIn("市场价环比6.15%", finding)
        self.assertIn("不能据此断言采购因果", finding)
        self.assertNotIn("采购价环比上涨12%", finding)
        self.assertNotIn("超出波动阈值10%", finding)

    def test_failed_report_contract_is_blocked(self) -> None:
        failed = self.contract.model_copy(
            update={"validation_status": "FAIL", "validation_issues": ["测试阻断"]}
        )
        with self.assertRaises(RpaWorkflowError):
            build_task_candidates(failed)

    def test_approval_requires_real_assignee_timezone_and_future_deadline(self) -> None:
        common = {
            "candidate": self.candidate,
            "reviewer": "成本平台主管",
            "decided_at": datetime.fromisoformat("2026-08-02T10:30:00+08:00"),
            "assignee_name": "测试责任人",
            "department": "成本管理部",
            "role": None,
            "deadline": date.fromisoformat("2026-08-31"),
            "priority": "medium",
            "notify_method": "email",
        }
        for key, value in (
            ("reviewer", "待指定"),
            ("assignee_name", "待指定"),
            ("department", "待业务确认"),
        ):
            with self.subTest(key=key), self.assertRaises(RpaWorkflowError):
                approve_candidate(**{**common, key: value})
        with self.assertRaises(RpaWorkflowError):
            approve_candidate(**{**common, "decided_at": datetime(2026, 8, 2, 10, 30)})
        with self.assertRaises(RpaWorkflowError):
            approve_candidate(**{**common, "deadline": date(2026, 8, 1)})

    def test_approved_payload_matches_documented_api_and_excludes_internal_fields(self) -> None:
        reviewed = self.approve()
        payload = reviewed.payload.model_dump(mode="json", exclude_none=True)
        self.assertEqual(
            set(payload),
            {
                "task_id", "task_title", "assignee", "source", "priority",
                "deadline", "suggestion", "notify_method", "created_at",
            },
        )
        self.assertEqual(
            set(payload["source"]),
            {"analysis_type", "analysis_month", "product", "finding"},
        )
        self.assertNotIn("idempotency_key", payload)
        self.assertEqual(payload["notify_method"], "email")
        self.assertEqual(payload["deadline"], "2026-08-31")

    def test_interface_model_rejects_missing_and_invalid_values(self) -> None:
        payload = self.approve().payload.model_dump(mode="json")
        payload.pop("deadline")
        with self.assertRaises(ValidationError):
            RpaTaskCreateRequest.model_validate(payload)
        payload = self.approve().payload.model_dump(mode="json")
        payload["priority"] = "urgent"
        with self.assertRaises(ValidationError):
            RpaTaskCreateRequest.model_validate(payload)

    def test_rejection_records_reason_and_cannot_be_submitted(self) -> None:
        rejected = reject_candidate(
            self.candidate,
            reviewer="成本平台主管",
            decided_at=datetime.fromisoformat("2026-08-02T10:30:00+08:00"),
            comment="缺少责任部门确认",
        )
        self.assertEqual(rejected.review.decision, "rejected")
        self.assertIsNone(rejected.payload)
        with self.assertRaises(RpaSubmissionError):
            RpaClient().submit(rejected)


class RpaClientTests(RpaFixtureMixin, unittest.TestCase):
    def test_dry_run_never_calls_transport_or_writes_ledger(self) -> None:
        transport = FakeTransport([success_response()])
        with tempfile.TemporaryDirectory() as temporary:
            ledger_path = Path(temporary) / "ledger.json"
            result = RpaClient(
                mode="dry_run",
                transport=transport,
                ledger=JsonSubmissionLedger(ledger_path),
            ).submit(self.approve())
            self.assertEqual(result.state, "dry_run")
            self.assertEqual(result.attempt_count, 0)
            self.assertEqual(transport.calls, [])
            self.assertFalse(ledger_path.exists())

    def test_success_and_local_idempotency(self) -> None:
        transport = FakeTransport([success_response()])
        with tempfile.TemporaryDirectory() as temporary:
            ledger = JsonSubmissionLedger(Path(temporary) / "ledger.json")
            client = RpaClient(mode="execute", transport=transport, ledger=ledger)
            first = client.submit(self.approve())
            second = client.submit(self.approve())
            self.assertEqual(first.state, "sent")
            self.assertEqual(second.state, "duplicate_local")
            self.assertEqual(len(transport.calls), 1)
            self.assertTrue(ledger.path.is_file())

    def test_remote_duplicate_is_classified_without_retry(self) -> None:
        response = TransportResponse(
            status_code=400,
            payload={"detail": "任务ID TASK-202605-101 已存在"},
        )
        transport = FakeTransport([response])
        result = RpaClient(
            mode="execute",
            transport=transport,
            max_attempts=3,
            retry_delay_seconds=0,
        ).submit(self.approve())
        self.assertEqual(result.state, "duplicate_remote")
        self.assertEqual(result.attempt_count, 1)
        self.assertEqual(len(transport.calls), 1)

    def test_transient_server_failure_retries_then_succeeds(self) -> None:
        transport = FakeTransport([
            TransportResponse(status_code=503, payload={"code": 503, "message": "暂时不可用"}),
            success_response(),
        ])
        result = RpaClient(
            mode="execute",
            transport=transport,
            max_attempts=3,
            retry_delay_seconds=0,
        ).submit(self.approve())
        self.assertEqual(result.state, "sent")
        self.assertEqual(result.attempt_count, 2)
        self.assertEqual(len(transport.calls), 2)

    def test_network_failure_retries_but_is_bounded(self) -> None:
        transport = FakeTransport([
            RpaSubmissionError("连接失败1"),
            RpaSubmissionError("连接失败2"),
            RpaSubmissionError("连接失败3"),
        ])
        result = RpaClient(
            mode="execute",
            transport=transport,
            max_attempts=3,
            retry_delay_seconds=0,
        ).submit(self.approve())
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.attempt_count, 3)
        self.assertEqual(len(transport.calls), 3)

    def test_nonretryable_validation_failure_stops_immediately(self) -> None:
        transport = FakeTransport([
            TransportResponse(status_code=400, payload={"code": 400, "message": "字段校验失败"})
        ])
        result = RpaClient(
            mode="execute",
            transport=transport,
            max_attempts=3,
            retry_delay_seconds=0,
        ).submit(self.approve())
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.attempt_count, 1)
        self.assertEqual(len(transport.calls), 1)

    def test_retry_count_remains_exact_when_second_response_is_nonretryable(self) -> None:
        transport = FakeTransport([
            TransportResponse(status_code=503, payload={"code": 503, "message": "暂时不可用"}),
            TransportResponse(status_code=400, payload={"code": 400, "message": "字段校验失败"}),
        ])
        result = RpaClient(
            mode="execute",
            transport=transport,
            max_attempts=3,
            retry_delay_seconds=0,
        ).submit(self.approve())
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.attempt_count, 2)
        self.assertEqual(len(transport.calls), 2)

    def test_cli_prepare_approve_and_default_dry_run(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidates = root / "candidates.json"
            approved = root / "approved.json"
            result_path = root / "result.json"
            commands = [
                [
                    sys.executable, "-m", "app.rpa", "prepare",
                    "--report-json", str(REPORT_JSON), "--output", str(candidates),
                ],
                [
                    sys.executable, "-m", "app.rpa", "approve",
                    "--candidates", str(candidates),
                    "--reviewer", "成本平台主管",
                    "--decided-at", "2026-08-02T10:30:00+08:00",
                    "--assignee", "测试责任人",
                    "--department", "成本管理部",
                    "--deadline", "2026-08-31",
                    "--priority", "medium",
                    "--notify-method", "email",
                    "--output", str(approved),
                ],
                [
                    sys.executable, "-m", "app.rpa", "submit",
                    "--reviewed-task", str(approved),
                    "--ledger", str(root / "ledger.json"),
                    "--output", str(result_path),
                ],
            ]
            for command in commands:
                completed = subprocess.run(
                    command,
                    cwd=PROJECT_ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(result_path.read_text(encoding="utf-8"))["state"], "dry_run")
            self.assertFalse((root / "ledger.json").exists())


@unittest.skipUnless(
    importlib.util.find_spec("fastapi") and importlib.util.find_spec("httpx2"),
    "需要requirements中的FastAPI和httpx2",
)
class RepositoryMockServerCompatibilityTests(RpaFixtureMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from fastapi.testclient import TestClient

        from app.mock_rpa import server

        cls.server = server
        cls.client = TestClient(cls.server.app)

    def setUp(self) -> None:
        self.server.tasks_db.clear()
        self.server.notifications_db.clear()

    def approved_payload(self) -> dict:
        return self.approve().payload.model_dump(mode="json", exclude_none=True)

    def test_repository_server_accepts_governed_payload_and_supports_query(self) -> None:
        created = self.client.post("/api/rpa/tasks", json=self.approved_payload())
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["data"]["status"], "sent")
        queried = self.client.get("/api/rpa/tasks/TASK-202605-101")
        self.assertEqual(queried.status_code, 200)
        self.assertEqual(queried.json()["data"]["source"]["finding"], self.candidate.finding)

    def test_repository_server_rejects_duplicate_task_id(self) -> None:
        self.assertEqual(self.client.post("/api/rpa/tasks", json=self.approved_payload()).status_code, 200)
        duplicate = self.client.post("/api/rpa/tasks", json=self.approved_payload())
        self.assertEqual(duplicate.status_code, 400)
        self.assertIn("已存在", duplicate.json()["detail"])

    def test_repository_server_rejects_missing_required_field(self) -> None:
        payload = self.approved_payload()
        payload.pop("task_title")
        response = self.client.post("/api/rpa/tasks", json=payload)
        self.assertEqual(response.status_code, 422)

    def test_repository_server_rejects_invalid_priority(self) -> None:
        payload = self.approved_payload()
        payload["priority"] = "urgent"
        response = self.client.post("/api/rpa/tasks", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertIn("priority", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()

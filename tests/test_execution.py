"""Governed test-environment execution, receipt, lease, and recovery tests."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.review import (
    CandidateStateError,
    ReviewStore,
    SubmissionRateLimitError,
    create_review_app,
)
from app.review.auth import AuthManager
from app.review.execution import GovernedTestExecutor, TestSubmissionSettings
from app.rpa import RpaSubmissionError, TaskCandidateBundle
from app.rpa.client import TransportResponse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_FILE = PROJECT_ROOT / "08_RPA任务输出" / "2026-05_银黄口服液_RPA任务候选.json"
FIXED_TIME = datetime.fromisoformat("2026-08-03T10:00:00+08:00")


class MemoryReceiptTransport:
    def __init__(self, *, post_error: bool = False, mismatch: bool = False):
        self.post_error = post_error
        self.mismatch = mismatch
        self.post_calls = 0
        self.get_calls = 0
        self.remote: dict | None = None

    def post_json(self, url: str, payload: dict, timeout: float) -> TransportResponse:
        self.post_calls += 1
        if self.post_error:
            raise RpaSubmissionError("受控网络中断")
        self.remote = {
            **payload,
            "task_title": "错误标题" if self.mismatch else payload["task_title"],
            "status": "sent",
            "status_history": [{"status": "sent", "time": FIXED_TIME.isoformat()}],
            "progress": "",
        }
        return TransportResponse(
            status_code=200,
            payload={
                "code": 200,
                "message": "任务创建成功",
                "data": {
                    "task_id": payload["task_id"],
                    "status": "sent",
                    "tracking_url": f"http://127.0.0.1:8090/api/rpa/tasks/{payload['task_id']}",
                },
            },
        )

    def get_json(self, url: str, timeout: float) -> TransportResponse:
        self.get_calls += 1
        if self.remote is None:
            return TransportResponse(status_code=404, payload={"detail": "不存在"})
        return TransportResponse(
            status_code=200,
            payload={"code": 200, "message": "查询成功", "data": self.remote},
        )


class GovernedExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "execution.sqlite3"
        self.store = ReviewStore(self.db_path)
        self.store.initialize()
        self.auth = AuthManager(self.store)
        self.auth.bootstrap_admin("execution-admin-token", created_at=FIXED_TIME)
        reviewer, _ = self.auth.create_user(
            user_id="reviewer-01", display_name="审核员甲", role="reviewer", created_at=FIXED_TIME
        )
        submitter, _ = self.auth.create_user(
            user_id="submitter-01", display_name="提交员乙", role="submitter", created_at=FIXED_TIME
        )
        self.reviewer = reviewer
        self.submitter = submitter
        self.bundle = TaskCandidateBundle.model_validate_json(CANDIDATE_FILE.read_text(encoding="utf-8"))
        self.candidate_id = self.bundle.candidates[0].candidate_id
        self.store.import_bundle(self.bundle, occurred_at=FIXED_TIME)
        self._approve_and_authorize(self.candidate_id)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _approve_and_authorize(self, candidate_id: str) -> None:
        self.store.approve(
            candidate_id,
            expected_version=0,
            reviewer=self.reviewer.display_name,
            reviewer_user_id=self.reviewer.user_id,
            assignee_name="测试责任人",
            department="成本管理部",
            role="成本分析员",
            deadline=date.fromisoformat("2026-08-31"),
            priority="medium",
            notify_method="email",
            comment="测试审批",
            decided_at=FIXED_TIME,
        )
        self.store.authorize_submission(
            candidate_id,
            expected_version=1,
            authorizer_user_id=self.submitter.user_id,
            authorizer_name=self.submitter.display_name,
            comment="第二人授权",
            authorized_at=FIXED_TIME,
        )

    def settings(self, **updates) -> TestSubmissionSettings:
        payload = {
            "enabled": True,
            "base_url": "http://127.0.0.1:8090",
            "retry_delay_seconds": 0,
        }
        payload.update(updates)
        return TestSubmissionSettings(**payload)

    def claim(self, *, candidate_id: str | None = None, expected_version: int = 2, **updates):
        settings = self.settings(**updates)
        return self.store.claim_test_execution(
            candidate_id or self.candidate_id,
            expected_version=expected_version,
            operator_user_id=self.submitter.user_id,
            operator_name=self.submitter.display_name,
            endpoint_origin=settings.safe_origin,
            rate_limit_per_minute=settings.rate_limit_per_minute,
            lease_seconds=settings.lease_seconds,
            occurred_at=FIXED_TIME,
        )

    def test_only_loopback_service_roots_are_allowed(self) -> None:
        with self.assertRaises(ValueError):
            TestSubmissionSettings(enabled=True, base_url="https://rpa.example.com:443")
        with self.assertRaises(ValueError):
            TestSubmissionSettings(enabled=True, base_url="http://127.0.0.1:8090/api/rpa")
        self.assertEqual(self.settings().safe_origin, "http://127.0.0.1:8090")

    def test_authorized_post_requires_verified_receipt_and_is_persisted(self) -> None:
        transport = MemoryReceiptTransport()
        executor = GovernedTestExecutor(self.settings(), transport=transport)
        claim = self.claim()
        outcome = executor.execute(claim)
        self.assertEqual(outcome.state, "succeeded")
        self.assertEqual(outcome.receipt.status, "verified")
        detail = self.store.complete_test_execution(
            claim, outcome, actor=self.submitter.display_name, occurred_at=FIXED_TIME
        )
        self.assertEqual(detail["submission_state"], "sent")
        self.assertEqual(detail["execution_state"], "succeeded")
        self.assertEqual(detail["version"], 4)
        self.assertEqual(detail["execution_job"]["state"], "succeeded")
        self.assertEqual(transport.post_calls, 1)
        self.assertEqual(transport.get_calls, 1)
        self.assertEqual(self.store.verify_audit_chain()["status"], "PASS")
        with self.assertRaises(CandidateStateError):
            self.claim(expected_version=4)

    def test_receipt_mismatch_requires_reconciliation_and_blocks_repost(self) -> None:
        transport = MemoryReceiptTransport(mismatch=True)
        executor = GovernedTestExecutor(self.settings(), transport=transport)
        claim = self.claim()
        outcome = executor.execute(claim)
        self.assertEqual(outcome.state, "reconcile_required")
        detail = self.store.complete_test_execution(
            claim, outcome, actor=self.submitter.display_name, occurred_at=FIXED_TIME
        )
        self.assertEqual(detail["execution_state"], "reconcile_required")
        reconciliation = self.claim(expected_version=4)
        self.assertEqual(reconciliation.mode, "reconcile")
        self.assertEqual(transport.post_calls, 1)

    def test_ambiguous_network_failure_reconciles_before_safe_retry(self) -> None:
        transport = MemoryReceiptTransport(post_error=True)
        executor = GovernedTestExecutor(self.settings(max_attempts=1), transport=transport)
        first = self.claim()
        first_outcome = executor.execute(first)
        self.assertEqual(first_outcome.state, "reconcile_required")
        self.store.complete_test_execution(
            first, first_outcome, actor=self.submitter.display_name, occurred_at=FIXED_TIME
        )
        reconciliation = self.claim(expected_version=4)
        self.assertEqual(reconciliation.mode, "reconcile")
        reconciled = executor.execute(reconciliation)
        self.assertEqual(reconciled.state, "failed")
        self.assertTrue(reconciled.safe_to_retry)
        self.store.complete_test_execution(
            reconciliation,
            reconciled,
            actor=self.submitter.display_name,
            occurred_at=FIXED_TIME + timedelta(seconds=1),
        )
        retry = self.claim(expected_version=6)
        self.assertEqual(retry.mode, "post")

    def test_persistent_rate_limit_blocks_second_candidate(self) -> None:
        original = self.bundle.candidates[0]
        second = original.model_copy(
            update={
                "candidate_id": "CAND-2222222222222222",
                "task_id": "TASK-202606-999",
                "idempotency_key": "2" * 64,
                "analysis_month": "2026-06",
            }
        )
        self.store.import_bundle(self.bundle.model_copy(update={"candidates": [second]}), occurred_at=FIXED_TIME)
        self._approve_and_authorize(second.candidate_id)
        self.claim(rate_limit_per_minute=1)
        with self.assertRaises(SubmissionRateLimitError):
            self.claim(candidate_id=second.candidate_id, rate_limit_per_minute=1)
        with closing(sqlite3.connect(self.db_path)) as connection:
            count = connection.execute("SELECT COUNT(*) FROM submission_rate_events").fetchone()[0]
        self.assertEqual(count, 1)


class GovernedExecutionWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.transport = MemoryReceiptTransport()
        settings = TestSubmissionSettings(
            enabled=True,
            base_url="http://127.0.0.1:8090",
            retry_delay_seconds=0,
        )
        self.app = create_review_app(
            database_path=Path(self.temporary.name) / "web.sqlite3",
            admin_token="execution-web-admin-token",
            candidate_files=[CANDIDATE_FILE],
            test_executor=GovernedTestExecutor(settings, transport=self.transport),
        )
        self.client = TestClient(self.app)
        self.admin_headers = {"X-Review-Token": "execution-web-admin-token"}
        self.candidate_id = TaskCandidateBundle.model_validate_json(
            CANDIDATE_FILE.read_text(encoding="utf-8")
        ).candidates[0].candidate_id

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

    def test_enabled_api_requires_exact_confirmation_and_authorizing_user(self) -> None:
        health = self.client.get("/health").json()
        self.assertTrue(health["test_submit_enabled"])
        self.assertFalse(health["external_submit_enabled"])
        submitter = self.client.post(
            "/api/users",
            headers=self.admin_headers,
            json={"user_id": "submitter-web", "display_name": "提交员乙", "role": "submitter"},
        ).json()
        submitter_headers = {"X-Review-Token": submitter["issued_token"]}
        approved = self.client.post(
            f"/api/candidates/{self.candidate_id}/approve",
            headers=self.admin_headers,
            json={
                "expected_version": 0,
                "assignee_name": "测试责任人",
                "department": "成本管理部",
                "role": "成本分析员",
                "deadline": "2099-12-31",
                "priority": "medium",
                "notify_method": "email",
                "comment": "测试审批",
            },
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        authorized = self.client.post(
            f"/api/candidates/{self.candidate_id}/authorize-submission",
            headers=submitter_headers,
            json={"expected_version": 1, "comment": "第二人授权"},
        )
        self.assertEqual(authorized.status_code, 200, authorized.text)
        wrong_confirmation = self.client.post(
            f"/api/candidates/{self.candidate_id}/execute-test",
            headers=submitter_headers,
            json={"expected_version": 2, "confirmation": "PROD"},
        )
        self.assertEqual(wrong_confirmation.status_code, 422)
        wrong_operator = self.client.post(
            f"/api/candidates/{self.candidate_id}/execute-test",
            headers=self.admin_headers,
            json={"expected_version": 2, "confirmation": "TEST"},
        )
        self.assertEqual(wrong_operator.status_code, 409)
        executed = self.client.post(
            f"/api/candidates/{self.candidate_id}/execute-test",
            headers=submitter_headers,
            json={"expected_version": 2, "confirmation": "TEST"},
        )
        self.assertEqual(executed.status_code, 200, executed.text)
        self.assertEqual(executed.json()["execution_state"], "succeeded")
        self.assertEqual(executed.json()["submission_state"], "sent")
        self.assertEqual(executed.json()["test_execution_outcome"]["receipt"]["status"], "verified")
        self.assertEqual(self.transport.post_calls, 1)


if __name__ == "__main__":
    unittest.main()

"""Persistence, authorization, audit, and API tests for the review workbench."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app.rpa.models import TaskCandidateBundle
from app.review import (
    CandidateConflictError,
    CandidateStateError,
    ReviewStore,
    create_review_app,
)
from app.review.auth import AuthManager, AuthenticationError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_FILE = PROJECT_ROOT / "08_RPA任务输出" / "2026-05_银黄口服液_RPA任务候选.json"
TOKEN = "review-test-token-2026"
FIXED_TIME = datetime.fromisoformat("2026-08-02T12:00:00+08:00")


def load_bundle() -> TaskCandidateBundle:
    return TaskCandidateBundle.model_validate_json(CANDIDATE_FILE.read_text(encoding="utf-8"))


class ReviewStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "review.sqlite3"
        self.store = ReviewStore(self.db_path)
        self.store.initialize()
        self.bundle = load_bundle()
        self.candidate_id = self.bundle.candidates[0].candidate_id
        self.store.import_bundle(self.bundle, occurred_at=FIXED_TIME)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def approve(self, expected_version: int = 0):
        return self.store.approve(
            self.candidate_id,
            expected_version=expected_version,
            reviewer="成本平台主管",
            assignee_name="测试责任人",
            department="成本管理部",
            role="成本分析员",
            deadline=date.fromisoformat("2026-08-31"),
            priority="medium",
            notify_method="email",
            comment="审核工作台自动化测试",
            decided_at=FIXED_TIME,
        )

    def test_database_schema_and_import_are_idempotent(self) -> None:
        result = self.store.import_bundle(self.bundle, occurred_at=FIXED_TIME)
        self.assertEqual(result, {"imported": 0, "unchanged": 1})
        self.assertEqual(len(self.store.list_candidates()), 1)
        with closing(sqlite3.connect(self.db_path)) as connection:
            version = connection.execute(
                "SELECT value FROM schema_meta WHERE key='version'"
            ).fetchone()[0]
        self.assertEqual(version, "3.0.0")

    def test_same_candidate_id_with_changed_content_is_blocked(self) -> None:
        changed_candidate = self.bundle.candidates[0].model_copy(
            update={"task_title": "被篡改的标题"}
        )
        changed_bundle = self.bundle.model_copy(update={"candidates": [changed_candidate]})
        with self.assertRaises(CandidateConflictError):
            self.store.import_bundle(changed_bundle, occurred_at=FIXED_TIME)

    def test_approval_is_persisted_with_complete_review_and_audit(self) -> None:
        detail = self.approve()
        self.assertEqual(detail["review_state"], "approved")
        self.assertEqual(detail["submission_state"], "not_submitted")
        self.assertEqual(detail["version"], 1)
        self.assertEqual(detail["review"]["reviewer"], "成本平台主管")
        self.assertEqual(detail["review"]["reviewed_task"]["payload"]["deadline"], "2026-08-31")
        self.assertEqual([item["event_type"] for item in detail["audit_events"]], ["candidate_imported", "candidate_approved"])
        self.assertEqual(self.store.verify_audit_chain(), {"status": "PASS", "checked_events": 2, "issues": []})
        reopened = ReviewStore(self.db_path).get_candidate(self.candidate_id)
        self.assertEqual(reopened["review_state"], "approved")

    def test_rejection_is_final_and_requires_a_reason(self) -> None:
        detail = self.store.reject(
            self.candidate_id,
            expected_version=0,
            reviewer="成本平台主管",
            comment="缺少责任部门确认",
            decided_at=FIXED_TIME,
        )
        self.assertEqual(detail["review_state"], "rejected")
        with self.assertRaises(CandidateStateError):
            self.approve(expected_version=1)

    def test_optimistic_version_prevents_double_review(self) -> None:
        self.approve()
        with self.assertRaises(CandidateConflictError):
            self.store.dry_run(
                self.candidate_id,
                expected_version=0,
                actor="成本平台主管",
                occurred_at=FIXED_TIME,
            )

    def test_failed_approval_rolls_back_completely(self) -> None:
        with self.assertRaises(Exception):
            self.store.approve(
                self.candidate_id,
                expected_version=0,
                reviewer="待指定",
                assignee_name="测试责任人",
                department="成本管理部",
                role=None,
                deadline=date.fromisoformat("2026-08-31"),
                priority="medium",
                notify_method="email",
                comment=None,
                decided_at=FIXED_TIME,
            )
        detail = self.store.get_candidate(self.candidate_id)
        self.assertEqual(detail["review_state"], "pending_review")
        self.assertEqual(detail["version"], 0)
        self.assertIsNone(detail["review"])
        self.assertEqual(len(detail["audit_events"]), 1)

    def test_dry_run_requires_approval_and_never_records_external_status(self) -> None:
        with self.assertRaises(CandidateStateError):
            self.store.dry_run(
                self.candidate_id,
                expected_version=0,
                actor="成本平台主管",
                occurred_at=FIXED_TIME,
            )
        self.approve()
        detail = self.store.dry_run(
            self.candidate_id,
            expected_version=1,
            actor="成本平台主管",
            occurred_at=FIXED_TIME,
        )
        self.assertEqual(detail["submission_state"], "dry_run")
        self.assertEqual(detail["version"], 2)
        self.assertEqual(detail["submissions"][0]["state"], "dry_run")
        self.assertEqual(detail["submissions"][0]["attempt_count"], 0)
        self.assertEqual(self.store.verify_audit_chain()["status"], "PASS")

    def test_audit_tampering_is_detected(self) -> None:
        self.approve()
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE audit_events SET actor='篡改者' WHERE candidate_id=? AND sequence_no=1",
                (self.candidate_id,),
            )
            connection.commit()
        result = self.store.verify_audit_chain(self.candidate_id)
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(result["issues"])

    def test_superseded_pending_candidate_is_retained_but_not_actionable(self) -> None:
        old_id = self.candidate_id
        old = self.bundle.candidates[0]
        new = old.model_copy(
            update={
                "candidate_id": "CAND-1111111111111111",
                "task_id": "TASK-202605-999",
                "idempotency_key": "1" * 64,
            }
        )
        self.store.import_bundle(self.bundle.model_copy(update={"candidates": [new]}))
        self.store.supersede_candidate(old_id, new.candidate_id, actor="规则迁移")
        self.assertEqual(len(self.store.list_candidates()), 1)
        self.assertEqual(len(self.store.list_candidates(include_inactive=True)), 2)
        old_detail = self.store.get_candidate(old_id)
        self.assertFalse(old_detail["is_active"])
        self.assertEqual(old_detail["superseded_by"], new.candidate_id)
        with self.assertRaises(CandidateStateError):
            self.store.reject(
                old_id,
                expected_version=1,
                reviewer="审核员",
                comment="不应允许",
            )
        self.assertEqual(self.store.verify_audit_chain()["status"], "PASS")


class AuthManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "auth.sqlite3"
        self.store = ReviewStore(self.db_path)
        self.auth = AuthManager(self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_tokens_are_one_way_hashed_and_new_token_is_returned_once(self) -> None:
        principal = self.auth.bootstrap_admin(TOKEN, created_at=FIXED_TIME)
        self.assertEqual(principal.role, "admin")
        created, issued_token = self.auth.create_user(
            user_id="reviewer-01",
            display_name="审核员甲",
            role="reviewer",
            created_at=FIXED_TIME,
        )
        self.assertEqual(created.role, "reviewer")
        self.assertEqual(self.auth.authenticate(issued_token).user_id, "reviewer-01")
        with closing(sqlite3.connect(self.db_path)) as connection:
            stored = connection.execute(
                "SELECT token_lookup,token_salt,token_hash FROM users WHERE user_id='reviewer-01'"
            ).fetchone()
        self.assertNotIn(issued_token, stored)
        self.assertNotEqual(stored[2], issued_token)
        self.assertNotIn("token_hash", self.auth.list_users()[0])
        with self.assertRaises(AuthenticationError):
            self.auth.authenticate("wrong-token-value")


class ReviewWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "review.sqlite3"
        self.app = create_review_app(
            database_path=self.db_path,
            admin_token=TOKEN,
            candidate_files=[CANDIDATE_FILE],
        )
        self.client = TestClient(self.app)
        self.headers = {"X-Review-Token": TOKEN}
        self.candidate_id = load_bundle().candidates[0].candidate_id

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

    def approval_payload(self, expected_version: int = 0) -> dict:
        return {
            "expected_version": expected_version,
            "assignee_name": "测试责任人",
            "department": "成本管理部",
            "role": "成本分析员",
            "deadline": "2026-08-31",
            "priority": "medium",
            "notify_method": "email",
            "comment": "API测试",
        }

    def test_root_is_public_shell_with_security_headers(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("成本任务审核工作台", response.text)
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])

    def test_api_requires_token_but_health_exposes_no_business_data(self) -> None:
        self.assertEqual(self.client.get("/api/candidates").status_code, 401)
        self.assertEqual(
            self.client.get("/api/candidates", headers={"X-Review-Token": "wrong-token"}).status_code,
            401,
        )
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertFalse(health.json()["external_submit_enabled"])
        self.assertNotIn("candidates", health.json())
        self.assertNotIn("database", health.json())

    def test_dashboard_and_candidate_detail_are_complete(self) -> None:
        dashboard = self.client.get("/api/dashboard", headers=self.headers).json()
        self.assertEqual(dashboard["total"], 1)
        self.assertEqual(dashboard["pending_review"], 1)
        self.assertEqual(dashboard["audit"]["status"], "PASS")
        detail = self.client.get(
            f"/api/candidates/{self.candidate_id}", headers=self.headers
        ).json()
        self.assertEqual(detail["candidate"]["product"], "银黄口服液")
        self.assertIn("6.15%", detail["candidate"]["finding"])
        self.assertEqual(len(detail["candidate"]["source_refs"]), 6)

    def test_role_permissions_and_two_person_submission_authorization(self) -> None:
        analyst = self.client.post(
            "/api/users",
            headers=self.headers,
            json={"user_id": "analyst-01", "display_name": "分析员甲", "role": "analyst"},
        ).json()
        submitter = self.client.post(
            "/api/users",
            headers=self.headers,
            json={"user_id": "submitter-01", "display_name": "提交员乙", "role": "submitter"},
        ).json()
        analyst_headers = {"X-Review-Token": analyst["issued_token"]}
        submitter_headers = {"X-Review-Token": submitter["issued_token"]}
        self.assertEqual(self.client.get("/api/candidates", headers=analyst_headers).status_code, 200)
        self.assertEqual(
            self.client.post(
                f"/api/candidates/{self.candidate_id}/approve",
                headers=analyst_headers,
                json=self.approval_payload(),
            ).status_code,
            403,
        )
        approved = self.client.post(
            f"/api/candidates/{self.candidate_id}/approve",
            headers=self.headers,
            json=self.approval_payload(),
        ).json()
        self.assertEqual(approved["review"]["reviewer_user_id"], "system-admin")
        same_person = self.client.post(
            f"/api/candidates/{self.candidate_id}/authorize-submission",
            headers=self.headers,
            json={"expected_version": 1, "comment": None},
        )
        self.assertEqual(same_person.status_code, 409)
        authorized = self.client.post(
            f"/api/candidates/{self.candidate_id}/authorize-submission",
            headers=submitter_headers,
            json={"expected_version": 1, "comment": "二人复核通过"},
        )
        self.assertEqual(authorized.status_code, 200, authorized.text)
        self.assertEqual(
            authorized.json()["submission_authorization"]["authorizer_user_id"],
            "submitter-01",
        )
        self.assertEqual(authorized.json()["submission_state"], "not_submitted")
        self.assertEqual(authorized.json()["audit_events"][-1]["event_type"], "submission_authorized")

    def test_approve_then_dry_run_updates_dashboard_and_audit(self) -> None:
        approved = self.client.post(
            f"/api/candidates/{self.candidate_id}/approve",
            headers=self.headers,
            json=self.approval_payload(),
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["review_state"], "approved")
        dry_run = self.client.post(
            f"/api/candidates/{self.candidate_id}/dry-run",
            headers=self.headers,
            json={"expected_version": 1},
        )
        self.assertEqual(dry_run.status_code, 200, dry_run.text)
        self.assertEqual(dry_run.json()["submission_state"], "dry_run")
        dashboard = self.client.get("/api/dashboard", headers=self.headers).json()
        self.assertEqual(dashboard["approved"], 1)
        self.assertEqual(dashboard["dry_run"], 1)
        self.assertEqual(dashboard["audit"]["checked_events"], 3)

    def test_reject_endpoint_and_state_conflict(self) -> None:
        rejected = self.client.post(
            f"/api/candidates/{self.candidate_id}/reject",
            headers=self.headers,
            json={"expected_version": 0, "comment": "缺少业务确认"},
        )
        self.assertEqual(rejected.status_code, 200)
        conflict = self.client.post(
            f"/api/candidates/{self.candidate_id}/approve",
            headers=self.headers,
            json=self.approval_payload(expected_version=1),
        )
        self.assertEqual(conflict.status_code, 409)

    def test_invalid_approval_is_rejected_without_state_change(self) -> None:
        payload = self.approval_payload()
        payload["assignee_name"] = "待指定"
        response = self.client.post(
            f"/api/candidates/{self.candidate_id}/approve",
            headers=self.headers,
            json=payload,
        )
        self.assertEqual(response.status_code, 422)
        detail = self.client.get(
            f"/api/candidates/{self.candidate_id}", headers=self.headers
        ).json()
        self.assertEqual(detail["review_state"], "pending_review")

    def test_version_conflict_returns_409(self) -> None:
        self.client.post(
            f"/api/candidates/{self.candidate_id}/approve",
            headers=self.headers,
            json=self.approval_payload(),
        )
        response = self.client.post(
            f"/api/candidates/{self.candidate_id}/dry-run",
            headers=self.headers,
            json={"expected_version": 0},
        )
        self.assertEqual(response.status_code, 409)

    def test_no_web_route_can_execute_external_submission(self) -> None:
        response = self.client.post(
            f"/api/candidates/{self.candidate_id}/execute",
            headers=self.headers,
            json={},
        )
        self.assertEqual(response.status_code, 404)

    def test_test_execution_route_is_disabled_without_explicit_configuration(self) -> None:
        response = self.client.post(
            f"/api/candidates/{self.candidate_id}/execute-test",
            headers=self.headers,
            json={"expected_version": 0, "confirmation": "TEST"},
        )
        self.assertEqual(response.status_code, 503)

    def test_frontend_uses_text_content_for_dynamic_business_data(self) -> None:
        script = (
            PROJECT_ROOT / "app" / "review" / "static" / "app.js"
        ).read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", script)
        self.assertIn("textContent", script)

    def test_short_admin_token_is_rejected_at_startup(self) -> None:
        with self.assertRaises(ValueError):
            create_review_app(database_path=self.db_path, admin_token="short")


if __name__ == "__main__":
    unittest.main()

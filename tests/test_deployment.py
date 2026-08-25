"""Enterprise-test deployment configuration, identity, readiness, and backup tests."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.review import (
    DeploymentSettings,
    SignedProxyIdentityProvider,
    TestSubmissionSettings,
    backup_sqlite_database,
    create_review_app,
    inspect_sqlite_database,
    proxy_signature,
    restore_sqlite_backup,
)
from app.review.database import ReviewStore
from app.rpa.models import TaskCandidateBundle


SIGNING_SECRET = "enterprise-test-signing-secret-32-bytes-minimum"
FIXED_EPOCH = 1_785_696_000
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_FILE = PROJECT_ROOT / "08_RPA任务输出" / "2026-05_银黄口服液_RPA任务候选.json"


def signed_headers(
    *,
    method: str,
    path: str,
    body: bytes = b"",
    request_id: str = "request-0000000001",
    user_id: str = "enterprise.admin",
    display_name: str = "企业管理员",
    role: str = "admin",
) -> dict[str, str]:
    display_name_b64 = base64.urlsafe_b64encode(display_name.encode("utf-8")).rstrip(b"=").decode("ascii")
    timestamp = str(FIXED_EPOCH)
    content_sha256 = hashlib.sha256(body).hexdigest()
    signature = proxy_signature(
        secret=SIGNING_SECRET,
        method=method,
        path_and_query=path,
        user_id=user_id,
        display_name_b64=display_name_b64,
        role=role,
        timestamp=timestamp,
        request_id=request_id,
        content_sha256=content_sha256,
    )
    return {
        "X-Review-User-Id": user_id,
        "X-Review-Display-Name-B64": display_name_b64,
        "X-Review-Role": role,
        "X-Review-Timestamp": timestamp,
        "X-Review-Request-Id": request_id,
        "X-Review-Content-SHA256": content_sha256,
        "X-Review-Signature": signature,
    }


class DeploymentSettingsTests(unittest.TestCase):
    def test_local_configuration_is_ready_and_secrets_are_not_serialized(self) -> None:
        settings = DeploymentSettings(admin_token="local-admin-token-1234")
        self.assertTrue(settings.readiness().ready)
        dumped = settings.model_dump(mode="json")
        self.assertNotIn("admin_token", dumped)
        self.assertNotIn("identity_signing_secret", dumped)

    def test_secret_file_is_supported_and_conflicting_sources_are_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            secret_file = Path(temporary) / "admin.secret"
            secret_file.write_text("file-admin-token-123456\n", encoding="utf-8")
            settings = DeploymentSettings.from_environment(
                {"REVIEW_ADMIN_TOKEN_FILE": str(secret_file)},
                database_path=Path(temporary) / "review.sqlite3",
            )
            self.assertEqual(settings.admin_token.get_secret_value(), "file-admin-token-123456")
            with self.assertRaises(ValueError):
                DeploymentSettings.from_environment(
                    {
                        "REVIEW_ADMIN_TOKEN": "direct-admin-token-1234",
                        "REVIEW_ADMIN_TOKEN_FILE": str(secret_file),
                    },
                    database_path=Path(temporary) / "review.sqlite3",
                )

    def test_enterprise_test_requires_signed_proxy_https_and_database_override(self) -> None:
        with self.assertRaises(ValueError):
            DeploymentSettings(
                environment="enterprise_test",
                auth_mode="local_token",
                admin_token="local-admin-token-1234",
                public_base_url="https://review.example.test",
            )
        settings = DeploymentSettings(
            environment="enterprise_test",
            auth_mode="signed_proxy",
            identity_signing_secret=SIGNING_SECRET,
            public_base_url="https://review.example.test",
        )
        self.assertFalse(settings.readiness().ready)
        allowed = settings.model_copy(update={"allow_sqlite_enterprise_test": True})
        self.assertTrue(allowed.readiness().ready)

    def test_enterprise_rpa_host_needs_exact_allowlist_and_https(self) -> None:
        allowed = TestSubmissionSettings(
            enabled=True,
            base_url="https://rpa-sandbox.example.test:443",
            allowed_hosts=("rpa-sandbox.example.test",),
        )
        self.assertEqual(allowed.safe_origin, "https://rpa-sandbox.example.test:443")
        with self.assertRaises(ValueError):
            TestSubmissionSettings(
                enabled=True,
                base_url="http://rpa-sandbox.example.test:8080",
                allowed_hosts=("rpa-sandbox.example.test",),
            )
        with self.assertRaises(ValueError):
            TestSubmissionSettings(
                enabled=True,
                base_url="https://other.example.test:443",
                allowed_hosts=("*.example.test",),
            )


class SignedProxyWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "review.sqlite3"
        self.settings = DeploymentSettings(
            environment="enterprise_test",
            auth_mode="signed_proxy",
            identity_signing_secret=SIGNING_SECRET,
            public_base_url="https://review.example.test",
            database_path=self.database,
            allow_sqlite_enterprise_test=True,
        )
        provider = SignedProxyIdentityProvider(
            signing_secret=SIGNING_SECRET,
            trusted_proxy_networks=("127.0.0.1/32",),
            clock=lambda: FIXED_EPOCH,
        )
        self.app = create_review_app(
            database_path=self.database,
            identity_provider=provider,
            deployment_settings=self.settings,
        )
        self.client = TestClient(self.app, client=("127.0.0.1", 50000))

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

    def test_signed_identity_is_accepted_once_and_replay_is_blocked(self) -> None:
        headers = signed_headers(method="GET", path="/api/me")
        first = self.client.get("/api/me", headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["display_name"], "企业管理员")
        replay = self.client.get("/api/me", headers=headers)
        self.assertEqual(replay.status_code, 401)
        self.assertIn("已使用", replay.json()["detail"])

    def test_tampered_body_and_signature_are_blocked(self) -> None:
        headers = signed_headers(
            method="POST",
            path="/api/users",
            body=b'{}',
            request_id="request-0000000002",
        )
        tampered = self.client.post(
            "/api/users",
            headers={**headers, "Content-Type": "application/json"},
            content=b'{"role":"admin"}',
        )
        self.assertEqual(tampered.status_code, 401)
        bad_signature = signed_headers(
            method="GET",
            path="/api/me",
            request_id="request-0000000003",
        )
        bad_signature["X-Review-Signature"] = "0" * 64
        rejected = self.client.get("/api/me", headers=bad_signature)
        self.assertEqual(rejected.status_code, 401)

    def test_enterprise_user_management_is_delegated_and_readiness_passes(self) -> None:
        headers = signed_headers(
            method="GET",
            path="/api/users",
            request_id="request-0000000004",
        )
        users = self.client.get("/api/users", headers=headers)
        self.assertEqual(users.status_code, 409)
        ready = self.client.get("/ready")
        self.assertEqual(ready.status_code, 200, ready.text)
        self.assertEqual(ready.json()["status"], "ready")
        self.assertIn("max-age=31536000", ready.headers["Strict-Transport-Security"])
        self.assertEqual(self.client.get("/live").json(), {"status": "ok"})


class DatabaseOperationsTests(unittest.TestCase):
    def test_read_only_inspection_and_consistent_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_directory = root / "runtime"
            backup_directory = root / "backups"
            restore_directory = root / "restore"
            source_directory.mkdir()
            backup_directory.mkdir()
            restore_directory.mkdir()
            source = source_directory / "review.sqlite3"
            ReviewStore(source).initialize()
            before = inspect_sqlite_database(source)
            self.assertEqual(before.status, "PASS")
            self.assertEqual(before.schema_version, "3.0.0")
            result = backup_sqlite_database(
                source,
                backup_directory,
                timestamp=datetime(2026, 8, 3, tzinfo=timezone.utc),
            )
            backup = inspect_sqlite_database(result.backup_path)
            after = inspect_sqlite_database(source)
            self.assertEqual(backup.status, "PASS")
            self.assertEqual(result.backup_sha256, backup.sha256)
            self.assertEqual(before.sha256, after.sha256)
            restored_path = restore_directory / "restored.sqlite3"
            restored = restore_sqlite_backup(result.backup_path, restored_path)
            restored_inspection = inspect_sqlite_database(restored_path)
            self.assertEqual(restored.integrity, "ok")
            self.assertEqual(restored.audit_status, "PASS")
            self.assertEqual(restored_inspection.schema_version, "3.0.0")
            self.assertEqual(restored_inspection.key_counts, before.key_counts)
            self.assertFalse(list(backup_directory.glob("*.partial*")))
            self.assertFalse(list(backup_directory.glob("*-wal")))
            self.assertFalse(list(backup_directory.glob("*-shm")))
            self.assertFalse(list(restore_directory.glob("*-wal")))
            self.assertFalse(list(restore_directory.glob("*-shm")))
            with self.assertRaises(FileExistsError):
                restore_sqlite_backup(result.backup_path, restored_path)
            with self.assertRaises(FileExistsError):
                backup_sqlite_database(
                    source,
                    backup_directory,
                    timestamp=datetime(2026, 8, 3, tzinfo=timezone.utc),
                )

    def test_backup_directory_must_be_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "review.sqlite3"
            ReviewStore(source).initialize()
            with self.assertRaises(ValueError):
                backup_sqlite_database(source, source.parent)

    def test_read_only_inspection_detects_audit_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "review.sqlite3"
            store = ReviewStore(source)
            store.initialize()
            bundle = TaskCandidateBundle.model_validate_json(CANDIDATE_FILE.read_text(encoding="utf-8"))
            store.import_bundle(bundle)
            before = inspect_sqlite_database(source)
            self.assertEqual(before.audit_status, "PASS")
            self.assertEqual(before.audit_events, 1)
            with closing(sqlite3.connect(source)) as connection:
                connection.execute("UPDATE audit_events SET actor='tampered' WHERE sequence_no=1")
                connection.commit()
            after = inspect_sqlite_database(source)
            self.assertEqual(after.status, "FAIL")
            self.assertEqual(after.audit_status, "FAIL")


if __name__ == "__main__":
    unittest.main()

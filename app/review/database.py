"""SQLite persistence and tamper-evident audit records for task review."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.rpa import (
    RpaClient,
    RpaWorkflowError,
    TaskCandidate,
    TaskCandidateBundle,
    approve_candidate,
    reject_candidate,
)
from app.rpa.models import ReviewedTask, SubmissionResult

from .execution import TestExecutionClaim, TestExecutionOutcome


REVIEW_DB_VERSION = "3.0.0"
SHANGHAI = ZoneInfo("Asia/Shanghai")


class ReviewStoreError(RuntimeError):
    pass


class CandidateNotFoundError(ReviewStoreError):
    pass


class CandidateConflictError(ReviewStoreError):
    pass


class CandidateStateError(ReviewStoreError):
    pass


class SubmissionRateLimitError(ReviewStoreError):
    pass


def _now() -> datetime:
    return datetime.now(SHANGHAI)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ReviewStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    report_number TEXT NOT NULL,
                    report_contract_sha256 TEXT NOT NULL,
                    analysis_month TEXT NOT NULL,
                    product TEXT NOT NULL,
                    candidate_json TEXT NOT NULL,
                    review_state TEXT NOT NULL CHECK(review_state IN ('pending_review','approved','rejected')),
                    submission_state TEXT NOT NULL CHECK(submission_state IN ('not_submitted','dry_run','sent','duplicate_local','duplicate_remote','failed')),
                    version INTEGER NOT NULL DEFAULT 0,
                    imported_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id TEXT NOT NULL UNIQUE REFERENCES candidates(candidate_id) ON DELETE CASCADE,
                    decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
                    reviewer TEXT NOT NULL,
                    decided_at TEXT NOT NULL,
                    comment TEXT,
                    reviewed_task_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS submissions (
                    submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
                    state TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    http_status INTEGER,
                    message TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
                    sequence_no INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    UNIQUE(candidate_id, sequence_no)
                );
                CREATE INDEX IF NOT EXISTS idx_candidates_state ON candidates(review_state, submission_state);
                CREATE INDEX IF NOT EXISTS idx_audit_candidate ON audit_events(candidate_id, sequence_no);
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin','analyst','reviewer','submitter','auditor')),
                    token_lookup TEXT NOT NULL UNIQUE,
                    token_salt TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS submission_authorizations (
                    authorization_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id TEXT NOT NULL UNIQUE REFERENCES candidates(candidate_id) ON DELETE CASCADE,
                    authorizer_user_id TEXT NOT NULL REFERENCES users(user_id),
                    authorizer_name TEXT NOT NULL,
                    authorized_at TEXT NOT NULL,
                    comment TEXT
                );
                CREATE TABLE IF NOT EXISTS submission_jobs (
                    candidate_id TEXT PRIMARY KEY REFERENCES candidates(candidate_id) ON DELETE CASCADE,
                    execution_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK(state IN ('running','succeeded','failed','reconcile_required')),
                    operation TEXT NOT NULL CHECK(operation IN ('post','reconcile')),
                    operator_user_id TEXT NOT NULL REFERENCES users(user_id),
                    operator_name TEXT NOT NULL,
                    endpoint_origin TEXT NOT NULL,
                    lease_expires_at TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_error TEXT,
                    receipt_json TEXT
                );
                CREATE TABLE IF NOT EXISTS submission_rate_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id) ON DELETE CASCADE,
                    execution_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_submission_rate_time ON submission_rate_events(occurred_at);
                """
            )
            candidate_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(candidates)")
            }
            if "is_active" not in candidate_columns:
                connection.execute(
                    "ALTER TABLE candidates ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1))"
                )
            if "superseded_by" not in candidate_columns:
                connection.execute(
                    "ALTER TABLE candidates ADD COLUMN superseded_by TEXT"
                )
            if "execution_state" not in candidate_columns:
                connection.execute(
                    "ALTER TABLE candidates ADD COLUMN execution_state TEXT NOT NULL DEFAULT 'idle' "
                    "CHECK(execution_state IN ('idle','running','succeeded','failed','reconcile_required'))"
                )
            review_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(reviews)")
            }
            if "reviewer_user_id" not in review_columns:
                connection.execute("ALTER TABLE reviews ADD COLUMN reviewer_user_id TEXT")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_candidates_task_id ON candidates(task_id)"
            )
            connection.execute(
                "INSERT INTO schema_meta(key,value) VALUES('version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (REVIEW_DB_VERSION,),
            )

    def _append_audit(
        self,
        connection: sqlite3.Connection,
        candidate_id: str,
        event_type: str,
        actor: str,
        occurred_at: datetime,
        detail: dict[str, Any],
    ) -> None:
        last = connection.execute(
            "SELECT sequence_no,event_hash FROM audit_events WHERE candidate_id=? ORDER BY sequence_no DESC LIMIT 1",
            (candidate_id,),
        ).fetchone()
        sequence = 1 if last is None else int(last["sequence_no"]) + 1
        previous_hash = "0" * 64 if last is None else str(last["event_hash"])
        detail_json = _json(detail)
        material = _json(
            {
                "candidate_id": candidate_id,
                "sequence_no": sequence,
                "event_type": event_type,
                "actor": actor,
                "occurred_at": occurred_at.isoformat(),
                "detail_json": detail_json,
                "previous_hash": previous_hash,
            }
        )
        event_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
        connection.execute(
            "INSERT INTO audit_events(candidate_id,sequence_no,event_type,actor,occurred_at,detail_json,previous_hash,event_hash) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                candidate_id,
                sequence,
                event_type,
                actor,
                occurred_at.isoformat(),
                detail_json,
                previous_hash,
                event_hash,
            ),
        )

    def import_bundle(
        self,
        bundle: TaskCandidateBundle,
        *,
        actor: str = "system-import",
        occurred_at: datetime | None = None,
    ) -> dict[str, int]:
        self.initialize()
        timestamp = occurred_at or _now()
        imported = 0
        unchanged = 0
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for candidate in bundle.candidates:
                    candidate_json = _json(candidate.model_dump(mode="json"))
                    existing = connection.execute(
                        "SELECT candidate_json FROM candidates WHERE candidate_id=?",
                        (candidate.candidate_id,),
                    ).fetchone()
                    if existing:
                        if existing["candidate_json"] != candidate_json:
                            raise CandidateConflictError(
                                f"候选{candidate.candidate_id}已存在但内容不同"
                            )
                        unchanged += 1
                        continue
                    connection.execute(
                        "INSERT INTO candidates(candidate_id,task_id,idempotency_key,report_number,report_contract_sha256,analysis_month,product,candidate_json,review_state,submission_state,version,imported_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            candidate.candidate_id,
                            candidate.task_id,
                            candidate.idempotency_key,
                            candidate.report_number,
                            candidate.report_contract_sha256,
                            candidate.analysis_month,
                            candidate.product,
                            candidate_json,
                            "pending_review",
                            "not_submitted",
                            0,
                            timestamp.isoformat(),
                            timestamp.isoformat(),
                        ),
                    )
                    self._append_audit(
                        connection,
                        candidate.candidate_id,
                        "candidate_imported",
                        actor,
                        timestamp,
                        {
                            "report_number": candidate.report_number,
                            "report_contract_sha256": candidate.report_contract_sha256,
                            "idempotency_key": candidate.idempotency_key,
                        },
                    )
                    imported += 1
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return {"imported": imported, "unchanged": unchanged}

    def import_candidates(
        self,
        candidates: list[TaskCandidate],
        *,
        actor: str = "system-batch-import",
        occurred_at: datetime | None = None,
    ) -> dict[str, int]:
        return self.import_bundle(
            TaskCandidateBundle(
                bundle_version="1.0.0",
                report_number="BATCH-AGGREGATE",
                report_contract_sha256="0" * 64,
                candidates=candidates,
            ),
            actor=actor,
            occurred_at=occurred_at,
        )

    def list_candidates(self, *, include_inactive: bool = False) -> list[dict[str, Any]]:
        self.initialize()
        with self._connection() as connection:
            query = (
                "SELECT candidate_json,review_state,submission_state,execution_state,version,imported_at,updated_at,is_active,superseded_by "
                "FROM candidates"
            )
            if not include_inactive:
                query += " WHERE is_active=1"
            query += " ORDER BY imported_at DESC,candidate_id"
            rows = connection.execute(query).fetchall()
        result = []
        for row in rows:
            candidate = json.loads(row["candidate_json"])
            result.append(
                {
                    "candidate": candidate,
                    "review_state": row["review_state"],
                    "submission_state": row["submission_state"],
                    "execution_state": row["execution_state"],
                    "version": row["version"],
                    "imported_at": row["imported_at"],
                    "updated_at": row["updated_at"],
                    "is_active": bool(row["is_active"]),
                    "superseded_by": row["superseded_by"],
                }
            )
        return result

    def _candidate_row(self, connection: sqlite3.Connection, candidate_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise CandidateNotFoundError(f"候选{candidate_id}不存在")
        return row

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        self.initialize()
        with self._connection() as connection:
            row = self._candidate_row(connection, candidate_id)
            review = connection.execute(
                "SELECT decision,reviewer,reviewer_user_id,decided_at,comment,reviewed_task_json FROM reviews WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            submissions = connection.execute(
                "SELECT submission_id,state,attempt_count,http_status,message,result_json,created_at "
                "FROM submissions WHERE candidate_id=? ORDER BY submission_id",
                (candidate_id,),
            ).fetchall()
            audits = connection.execute(
                "SELECT sequence_no,event_type,actor,occurred_at,detail_json,previous_hash,event_hash "
                "FROM audit_events WHERE candidate_id=? ORDER BY sequence_no",
                (candidate_id,),
            ).fetchall()
            authorization = connection.execute(
                "SELECT authorizer_user_id,authorizer_name,authorized_at,comment FROM submission_authorizations WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            execution_job = connection.execute(
                "SELECT execution_id,state,operation,operator_user_id,operator_name,endpoint_origin,"
                "lease_expires_at,started_at,updated_at,last_error,receipt_json "
                "FROM submission_jobs WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
        return {
            "candidate": json.loads(row["candidate_json"]),
            "review_state": row["review_state"],
            "submission_state": row["submission_state"],
            "execution_state": row["execution_state"],
            "version": row["version"],
            "imported_at": row["imported_at"],
            "updated_at": row["updated_at"],
            "is_active": bool(row["is_active"]),
            "superseded_by": row["superseded_by"],
            "review": None
            if review is None
            else {
                "decision": review["decision"],
                "reviewer": review["reviewer"],
                "reviewer_user_id": review["reviewer_user_id"],
                "decided_at": review["decided_at"],
                "comment": review["comment"],
                "reviewed_task": json.loads(review["reviewed_task_json"]),
            },
            "submissions": [
                {
                    "submission_id": item["submission_id"],
                    "state": item["state"],
                    "attempt_count": item["attempt_count"],
                    "http_status": item["http_status"],
                    "message": item["message"],
                    "result": json.loads(item["result_json"]),
                    "created_at": item["created_at"],
                }
                for item in submissions
            ],
            "submission_authorization": None
            if authorization is None
            else {
                "authorizer_user_id": authorization["authorizer_user_id"],
                "authorizer_name": authorization["authorizer_name"],
                "authorized_at": authorization["authorized_at"],
                "comment": authorization["comment"],
            },
            "execution_job": None
            if execution_job is None
            else {
                "execution_id": execution_job["execution_id"],
                "state": execution_job["state"],
                "operation": execution_job["operation"],
                "operator_user_id": execution_job["operator_user_id"],
                "operator_name": execution_job["operator_name"],
                "endpoint_origin": execution_job["endpoint_origin"],
                "lease_expires_at": execution_job["lease_expires_at"],
                "started_at": execution_job["started_at"],
                "updated_at": execution_job["updated_at"],
                "last_error": execution_job["last_error"],
                "receipt": None
                if execution_job["receipt_json"] is None
                else json.loads(execution_job["receipt_json"]),
            },
            "audit_events": [
                {
                    "sequence_no": item["sequence_no"],
                    "event_type": item["event_type"],
                    "actor": item["actor"],
                    "occurred_at": item["occurred_at"],
                    "detail": json.loads(item["detail_json"]),
                    "previous_hash": item["previous_hash"],
                    "event_hash": item["event_hash"],
                }
                for item in audits
            ],
        }

    def _check_mutable(
        self, row: sqlite3.Row, expected_version: int, required_state: str = "pending_review"
    ) -> None:
        if int(row["version"]) != expected_version:
            raise CandidateConflictError(
                f"版本冲突：当前为{row['version']}，提交为{expected_version}"
            )
        if not bool(row["is_active"]):
            raise CandidateStateError("已被替代的候选不能执行审核操作")
        if row["review_state"] != required_state:
            raise CandidateStateError(
                f"候选当前状态为{row['review_state']}，要求状态为{required_state}"
            )

    def approve(
        self,
        candidate_id: str,
        *,
        expected_version: int,
        reviewer: str,
        assignee_name: str,
        department: str,
        role: str | None,
        deadline: date,
        priority: str,
        notify_method: str,
        comment: str | None,
        reviewer_user_id: str | None = None,
        decided_at: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = decided_at or _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._candidate_row(connection, candidate_id)
                self._check_mutable(row, expected_version)
                candidate = TaskCandidate.model_validate_json(row["candidate_json"])
                reviewed = approve_candidate(
                    candidate,
                    reviewer=reviewer,
                    decided_at=timestamp,
                    assignee_name=assignee_name,
                    department=department,
                    role=role,
                    deadline=deadline,
                    priority=priority,
                    notify_method=notify_method,
                    comment=comment,
                )
                reviewed_json = _json(reviewed.model_dump(mode="json"))
                connection.execute(
                    "INSERT INTO reviews(candidate_id,decision,reviewer,reviewer_user_id,decided_at,comment,reviewed_task_json) VALUES(?,?,?,?,?,?,?)",
                    (candidate_id, "approved", reviewer, reviewer_user_id, timestamp.isoformat(), comment, reviewed_json),
                )
                connection.execute(
                    "UPDATE candidates SET review_state='approved',version=version+1,updated_at=? WHERE candidate_id=?",
                    (timestamp.isoformat(), candidate_id),
                )
                self._append_audit(
                    connection,
                    candidate_id,
                    "candidate_approved",
                    reviewer,
                    timestamp,
                    {
                        "assignee": assignee_name,
                        "department": department,
                        "deadline": deadline.isoformat(),
                        "priority": priority,
                        "notify_method": notify_method,
                    },
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_candidate(candidate_id)

    def reject(
        self,
        candidate_id: str,
        *,
        expected_version: int,
        reviewer: str,
        comment: str,
        reviewer_user_id: str | None = None,
        decided_at: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = decided_at or _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._candidate_row(connection, candidate_id)
                self._check_mutable(row, expected_version)
                candidate = TaskCandidate.model_validate_json(row["candidate_json"])
                reviewed = reject_candidate(
                    candidate,
                    reviewer=reviewer,
                    decided_at=timestamp,
                    comment=comment,
                )
                connection.execute(
                    "INSERT INTO reviews(candidate_id,decision,reviewer,reviewer_user_id,decided_at,comment,reviewed_task_json) VALUES(?,?,?,?,?,?,?)",
                    (
                        candidate_id,
                        "rejected",
                        reviewer,
                        reviewer_user_id,
                        timestamp.isoformat(),
                        comment,
                        _json(reviewed.model_dump(mode="json")),
                    ),
                )
                connection.execute(
                    "UPDATE candidates SET review_state='rejected',version=version+1,updated_at=? WHERE candidate_id=?",
                    (timestamp.isoformat(), candidate_id),
                )
                self._append_audit(
                    connection,
                    candidate_id,
                    "candidate_rejected",
                    reviewer,
                    timestamp,
                    {"comment": comment},
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_candidate(candidate_id)

    def dry_run(
        self,
        candidate_id: str,
        *,
        expected_version: int,
        actor: str,
        occurred_at: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = occurred_at or _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._candidate_row(connection, candidate_id)
                if int(row["version"]) != expected_version:
                    raise CandidateConflictError(
                        f"版本冲突：当前为{row['version']}，提交为{expected_version}"
                    )
                if not bool(row["is_active"]):
                    raise CandidateStateError("已被替代的候选不能执行dry-run")
                if row["execution_state"] == "running":
                    raise CandidateStateError("测试提交执行中，不能并行执行dry-run")
                if row["review_state"] != "approved":
                    raise CandidateStateError("只有approved候选可以执行dry-run")
                review_row = connection.execute(
                    "SELECT reviewed_task_json FROM reviews WHERE candidate_id=?",
                    (candidate_id,),
                ).fetchone()
                if review_row is None:
                    raise CandidateStateError("approved候选缺少审批记录")
                reviewed = ReviewedTask.model_validate_json(review_row["reviewed_task_json"])
                result = RpaClient(mode="dry_run").submit(reviewed)
                connection.execute(
                    "INSERT INTO submissions(candidate_id,state,attempt_count,http_status,message,result_json,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        candidate_id,
                        result.state,
                        result.attempt_count,
                        result.http_status,
                        result.message,
                        _json(result.model_dump(mode="json")),
                        timestamp.isoformat(),
                    ),
                )
                connection.execute(
                    "UPDATE candidates SET submission_state='dry_run',version=version+1,updated_at=? WHERE candidate_id=?",
                    (timestamp.isoformat(), candidate_id),
                )
                self._append_audit(
                    connection,
                    candidate_id,
                    "submission_dry_run",
                    actor,
                    timestamp,
                    {"attempt_count": result.attempt_count, "message": result.message},
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_candidate(candidate_id)

    def authorize_submission(
        self,
        candidate_id: str,
        *,
        expected_version: int,
        authorizer_user_id: str,
        authorizer_name: str,
        comment: str | None,
        authorized_at: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = authorized_at or _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._candidate_row(connection, candidate_id)
                if int(row["version"]) != expected_version:
                    raise CandidateConflictError(
                        f"版本冲突：当前为{row['version']}，提交为{expected_version}"
                    )
                if not bool(row["is_active"]):
                    raise CandidateStateError("已被替代的候选不能授权提交")
                if row["review_state"] != "approved":
                    raise CandidateStateError("只有approved候选可以授权提交")
                review = connection.execute(
                    "SELECT reviewer_user_id FROM reviews WHERE candidate_id=?",
                    (candidate_id,),
                ).fetchone()
                if review is None or not review["reviewer_user_id"]:
                    raise CandidateStateError("审批记录缺少可校验的审核人身份")
                if review["reviewer_user_id"] == authorizer_user_id:
                    raise CandidateStateError("提交授权人与审核人必须为不同用户")
                connection.execute(
                    "INSERT INTO submission_authorizations(candidate_id,authorizer_user_id,authorizer_name,authorized_at,comment) VALUES(?,?,?,?,?)",
                    (
                        candidate_id,
                        authorizer_user_id,
                        authorizer_name,
                        timestamp.isoformat(),
                        comment,
                    ),
                )
                connection.execute(
                    "UPDATE candidates SET version=version+1,updated_at=? WHERE candidate_id=?",
                    (timestamp.isoformat(), candidate_id),
                )
                self._append_audit(
                    connection,
                    candidate_id,
                    "submission_authorized",
                    authorizer_name,
                    timestamp,
                    {"authorizer_user_id": authorizer_user_id, "comment": comment},
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_candidate(candidate_id)

    def claim_test_execution(
        self,
        candidate_id: str,
        *,
        expected_version: int,
        operator_user_id: str,
        operator_name: str,
        endpoint_origin: str,
        rate_limit_per_minute: int,
        lease_seconds: int,
        occurred_at: datetime | None = None,
    ) -> TestExecutionClaim:
        self.initialize()
        timestamp = occurred_at or _now()
        execution_id = f"EXEC-{uuid.uuid4().hex[:16].upper()}"
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._candidate_row(connection, candidate_id)
                if int(row["version"]) != expected_version:
                    raise CandidateConflictError(
                        f"版本冲突：当前为{row['version']}，提交为{expected_version}"
                    )
                if not bool(row["is_active"]):
                    raise CandidateStateError("已被替代的候选不能执行测试提交")
                if row["review_state"] != "approved":
                    raise CandidateStateError("只有approved候选可以执行测试提交")
                if row["submission_state"] == "sent" or row["execution_state"] == "succeeded":
                    raise CandidateStateError("候选已完成测试提交，禁止重复执行")
                review = connection.execute(
                    "SELECT reviewed_task_json,reviewer_user_id FROM reviews WHERE candidate_id=?",
                    (candidate_id,),
                ).fetchone()
                authorization = connection.execute(
                    "SELECT authorizer_user_id,authorizer_name FROM submission_authorizations WHERE candidate_id=?",
                    (candidate_id,),
                ).fetchone()
                if review is None or not review["reviewer_user_id"]:
                    raise CandidateStateError("审批记录缺少可校验的审核人身份")
                if authorization is None:
                    raise CandidateStateError("候选尚未完成第二人提交授权")
                if authorization["authorizer_user_id"] != operator_user_id:
                    raise CandidateStateError("只有完成第二人授权的同一用户可以执行测试提交")
                if review["reviewer_user_id"] == operator_user_id:
                    raise CandidateStateError("审核人与测试提交执行人必须为不同用户")
                job = connection.execute(
                    "SELECT execution_id,state,lease_expires_at FROM submission_jobs WHERE candidate_id=?",
                    (candidate_id,),
                ).fetchone()
                mode = "post"
                if job is not None:
                    if job["state"] == "succeeded":
                        raise CandidateStateError("测试提交任务已经成功")
                    if job["state"] == "running":
                        lease_expires = datetime.fromisoformat(job["lease_expires_at"])
                        if lease_expires > timestamp:
                            raise CandidateConflictError("已有测试提交正在执行，租约尚未到期")
                        mode = "reconcile"
                    elif job["state"] == "reconcile_required":
                        mode = "reconcile"
                if mode == "post":
                    cutoff = (timestamp - timedelta(minutes=1)).isoformat()
                    recent = connection.execute(
                        "SELECT COUNT(*) FROM submission_rate_events WHERE occurred_at>=?",
                        (cutoff,),
                    ).fetchone()[0]
                    if recent >= rate_limit_per_minute:
                        raise SubmissionRateLimitError(
                            f"测试提交限流：最近1分钟已执行{recent}次POST"
                        )
                lease_expires_at = (timestamp + timedelta(seconds=lease_seconds)).isoformat()
                connection.execute(
                    "INSERT INTO submission_jobs(candidate_id,execution_id,state,operation,operator_user_id,operator_name,endpoint_origin,lease_expires_at,started_at,updated_at,last_error,receipt_json) "
                    "VALUES(?,?,'running',?,?,?,?,?,?,?,?,NULL) "
                    "ON CONFLICT(candidate_id) DO UPDATE SET execution_id=excluded.execution_id,state='running',operation=excluded.operation,"
                    "operator_user_id=excluded.operator_user_id,operator_name=excluded.operator_name,endpoint_origin=excluded.endpoint_origin,"
                    "lease_expires_at=excluded.lease_expires_at,started_at=excluded.started_at,updated_at=excluded.updated_at,last_error=NULL,receipt_json=NULL",
                    (
                        candidate_id,
                        execution_id,
                        mode,
                        operator_user_id,
                        operator_name,
                        endpoint_origin,
                        lease_expires_at,
                        timestamp.isoformat(),
                        timestamp.isoformat(),
                        None,
                    ),
                )
                if mode == "post":
                    connection.execute(
                        "INSERT INTO submission_rate_events(candidate_id,execution_id,occurred_at) VALUES(?,?,?)",
                        (candidate_id, execution_id, timestamp.isoformat()),
                    )
                connection.execute(
                    "UPDATE candidates SET execution_state='running',version=version+1,updated_at=? WHERE candidate_id=?",
                    (timestamp.isoformat(), candidate_id),
                )
                self._append_audit(
                    connection,
                    candidate_id,
                    "test_submission_started",
                    operator_name,
                    timestamp,
                    {
                        "execution_id": execution_id,
                        "mode": mode,
                        "operator_user_id": operator_user_id,
                        "endpoint_origin": endpoint_origin,
                    },
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return TestExecutionClaim(
            execution_id=execution_id,
            candidate_id=candidate_id,
            mode=mode,
            task=ReviewedTask.model_validate_json(review["reviewed_task_json"]),
            expected_completion_version=expected_version + 1,
        )

    def complete_test_execution(
        self,
        claim: TestExecutionClaim,
        outcome: TestExecutionOutcome,
        *,
        actor: str,
        occurred_at: datetime | None = None,
    ) -> dict[str, Any]:
        timestamp = occurred_at or _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._candidate_row(connection, claim.candidate_id)
                if int(row["version"]) != claim.expected_completion_version:
                    raise CandidateConflictError("测试提交完成时检测到候选版本冲突")
                job = connection.execute(
                    "SELECT execution_id,state FROM submission_jobs WHERE candidate_id=?",
                    (claim.candidate_id,),
                ).fetchone()
                if job is None or job["execution_id"] != claim.execution_id or job["state"] != "running":
                    raise CandidateConflictError("测试提交执行租约已失效")
                result = outcome.result
                attempt_count = 0 if result is None else result.attempt_count
                http_status = outcome.receipt.http_status
                if http_status is None and result is not None:
                    http_status = result.http_status
                connection.execute(
                    "INSERT INTO submissions(candidate_id,state,attempt_count,http_status,message,result_json,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        claim.candidate_id,
                        outcome.state,
                        attempt_count,
                        http_status,
                        outcome.message,
                        _json(outcome.model_dump(mode="json")),
                        timestamp.isoformat(),
                    ),
                )
                connection.execute(
                    "UPDATE submission_jobs SET state=?,updated_at=?,last_error=?,receipt_json=? WHERE candidate_id=? AND execution_id=?",
                    (
                        outcome.state,
                        timestamp.isoformat(),
                        None if outcome.state == "succeeded" else outcome.message,
                        _json(outcome.receipt.model_dump(mode="json")),
                        claim.candidate_id,
                        claim.execution_id,
                    ),
                )
                submission_state = row["submission_state"]
                if outcome.state == "succeeded":
                    submission_state = "sent"
                elif outcome.state == "failed":
                    submission_state = "failed"
                connection.execute(
                    "UPDATE candidates SET execution_state=?,submission_state=?,version=version+1,updated_at=? WHERE candidate_id=?",
                    (
                        outcome.state,
                        submission_state,
                        timestamp.isoformat(),
                        claim.candidate_id,
                    ),
                )
                event_type = {
                    "succeeded": "test_submission_receipt_verified",
                    "failed": "test_submission_failed",
                    "reconcile_required": "test_submission_reconcile_required",
                }[outcome.state]
                self._append_audit(
                    connection,
                    claim.candidate_id,
                    event_type,
                    actor,
                    timestamp,
                    {
                        "execution_id": claim.execution_id,
                        "mode": claim.mode,
                        "receipt_status": outcome.receipt.status,
                        "safe_to_retry": outcome.safe_to_retry,
                    },
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_candidate(claim.candidate_id)

    def supersede_candidate(
        self,
        old_candidate_id: str,
        new_candidate_id: str,
        *,
        actor: str,
        occurred_at: datetime | None = None,
    ) -> None:
        if old_candidate_id == new_candidate_id:
            raise CandidateConflictError("候选不能替代自身")
        timestamp = occurred_at or _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                old = self._candidate_row(connection, old_candidate_id)
                self._candidate_row(connection, new_candidate_id)
                if not bool(old["is_active"]):
                    if old["superseded_by"] == new_candidate_id:
                        connection.rollback()
                        return
                    raise CandidateStateError("旧候选已经被其他候选替代")
                if old["review_state"] != "pending_review":
                    raise CandidateStateError("只有未审核候选可以执行规则升级替代")
                review_count = connection.execute(
                    "SELECT COUNT(*) FROM reviews WHERE candidate_id=?", (old_candidate_id,)
                ).fetchone()[0]
                submission_count = connection.execute(
                    "SELECT COUNT(*) FROM submissions WHERE candidate_id=?", (old_candidate_id,)
                ).fetchone()[0]
                if review_count or submission_count:
                    raise CandidateStateError("存在审核或提交记录的候选不能被替代")
                connection.execute(
                    "UPDATE candidates SET is_active=0,superseded_by=?,version=version+1,updated_at=? WHERE candidate_id=?",
                    (new_candidate_id, timestamp.isoformat(), old_candidate_id),
                )
                self._append_audit(
                    connection,
                    old_candidate_id,
                    "candidate_superseded",
                    actor,
                    timestamp,
                    {"superseded_by": new_candidate_id, "reason": "全局任务编号规则升级"},
                )
                self._append_audit(
                    connection,
                    new_candidate_id,
                    "candidate_replaces_previous",
                    actor,
                    timestamp,
                    {"previous_candidate_id": old_candidate_id},
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def verify_audit_chain(self, candidate_id: str | None = None) -> dict[str, Any]:
        self.initialize()
        with self._connection() as connection:
            if candidate_id:
                self._candidate_row(connection, candidate_id)
                candidate_ids = [candidate_id]
            else:
                candidate_ids = [row[0] for row in connection.execute("SELECT candidate_id FROM candidates ORDER BY candidate_id")]
            issues: list[str] = []
            checked = 0
            for current_id in candidate_ids:
                rows = connection.execute(
                    "SELECT sequence_no,event_type,actor,occurred_at,detail_json,previous_hash,event_hash "
                    "FROM audit_events WHERE candidate_id=? ORDER BY sequence_no",
                    (current_id,),
                ).fetchall()
                expected_previous = "0" * 64
                for expected_sequence, row in enumerate(rows, start=1):
                    material = _json(
                        {
                            "candidate_id": current_id,
                            "sequence_no": expected_sequence,
                            "event_type": row["event_type"],
                            "actor": row["actor"],
                            "occurred_at": row["occurred_at"],
                            "detail_json": row["detail_json"],
                            "previous_hash": expected_previous,
                        }
                    )
                    expected_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
                    if row["sequence_no"] != expected_sequence:
                        issues.append(f"{current_id}序号不连续：{row['sequence_no']}")
                    if row["previous_hash"] != expected_previous:
                        issues.append(f"{current_id}第{expected_sequence}条前序哈希不匹配")
                    if row["event_hash"] != expected_hash:
                        issues.append(f"{current_id}第{expected_sequence}条事件哈希不匹配")
                    expected_previous = row["event_hash"]
                    checked += 1
        return {"status": "PASS" if not issues else "FAIL", "checked_events": checked, "issues": issues}

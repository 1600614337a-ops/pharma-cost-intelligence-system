"""Read-only database checks and recoverable SQLite backups."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class DatabaseInspection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    schema_version: str | None
    integrity: str
    foreign_key_issues: int
    audit_status: str
    audit_events: int
    key_counts: dict[str, int]
    sha256: str
    size_bytes: int


class BackupResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_sha256: str
    backup_sha256: str
    backup_path: str
    size_bytes: int
    integrity: str
    audit_status: str


class RestoreResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backup_sha256: str
    restored_sha256: str
    restored_path: str
    size_bytes: int
    integrity: str
    audit_status: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _cleanup_sidecars(database: Path) -> None:
    for suffix in ("-wal", "-shm"):
        Path(f"{database}{suffix}").unlink(missing_ok=True)


def _readonly_uri(database: Path) -> str:
    wal_path = Path(f"{database}-wal")
    immutable = not wal_path.exists() or wal_path.stat().st_size == 0
    suffix = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    return f"file:{database.as_posix()}{suffix}"


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return bool(
        connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()[0]
    )


def _audit_chain_status(connection: sqlite3.Connection) -> tuple[str, int]:
    if not _table_exists(connection, "candidates") or not _table_exists(connection, "audit_events"):
        return "NOT_AVAILABLE", 0
    connection.row_factory = sqlite3.Row
    checked = 0
    for candidate_row in connection.execute("SELECT candidate_id FROM candidates ORDER BY candidate_id"):
        candidate_id = str(candidate_row["candidate_id"])
        expected_previous = "0" * 64
        rows = connection.execute(
            "SELECT sequence_no,event_type,actor,occurred_at,detail_json,previous_hash,event_hash "
            "FROM audit_events WHERE candidate_id=? ORDER BY sequence_no",
            (candidate_id,),
        ).fetchall()
        for expected_sequence, row in enumerate(rows, start=1):
            material = json.dumps(
                {
                    "candidate_id": candidate_id,
                    "sequence_no": expected_sequence,
                    "event_type": row["event_type"],
                    "actor": row["actor"],
                    "occurred_at": row["occurred_at"],
                    "detail_json": row["detail_json"],
                    "previous_hash": expected_previous,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            expected_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
            if (
                row["sequence_no"] != expected_sequence
                or row["previous_hash"] != expected_previous
                or row["event_hash"] != expected_hash
            ):
                return "FAIL", checked
            expected_previous = str(row["event_hash"])
            checked += 1
    return "PASS", checked


def inspect_sqlite_database(path: str | Path) -> DatabaseInspection:
    database = Path(path).expanduser().resolve()
    if not database.is_file():
        raise FileNotFoundError(f"数据库不存在：{database}")
    connection = sqlite3.connect(_readonly_uri(database), uri=True)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_issues = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        schema_version = None
        if _table_exists(connection, "schema_meta"):
            row = connection.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
            schema_version = str(row[0]) if row else None
        audit_status, audit_events = _audit_chain_status(connection)
        key_counts = {}
        for table in (
            "candidates",
            "users",
            "reviews",
            "submission_authorizations",
            "submission_jobs",
            "submissions",
        ):
            if _table_exists(connection, table):
                key_counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()
    status = (
        "PASS"
        if integrity == "ok" and foreign_key_issues == 0 and audit_status in {"PASS", "NOT_AVAILABLE"}
        else "FAIL"
    )
    return DatabaseInspection(
        status=status,
        schema_version=schema_version,
        integrity=integrity,
        foreign_key_issues=foreign_key_issues,
        audit_status=audit_status,
        audit_events=audit_events,
        key_counts=key_counts,
        sha256=_sha256(database),
        size_bytes=database.stat().st_size,
    )


def backup_sqlite_database(
    source: str | Path,
    output_directory: str | Path,
    *,
    timestamp: datetime | None = None,
) -> BackupResult:
    source_path = Path(source).expanduser().resolve()
    output_path = Path(output_directory).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"源数据库不存在：{source_path}")
    if not output_path.is_dir():
        raise FileNotFoundError(f"备份目录不存在：{output_path}")
    if output_path == source_path.parent:
        raise ValueError("备份目录不得与运行数据库目录相同")
    moment = timestamp or datetime.now(timezone.utc)
    stamp = moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    source_hash = _sha256(source_path)
    destination = output_path / f"{source_path.stem}_{stamp}_{source_hash[:12]}.sqlite3"
    partial = destination.with_suffix(destination.suffix + ".partial")
    if destination.exists() or partial.exists():
        raise FileExistsError(f"备份目标已存在：{destination}")
    source_connection = sqlite3.connect(_readonly_uri(source_path), uri=True)
    destination_connection = sqlite3.connect(partial)
    try:
        source_connection.backup(destination_connection)
        destination_connection.execute("PRAGMA wal_checkpoint(FULL)")
        integrity = str(destination_connection.execute("PRAGMA integrity_check").fetchone()[0])
        destination_connection.commit()
    except Exception:
        destination_connection.close()
        source_connection.close()
        _cleanup_sidecars(partial)
        if partial.exists():
            partial.unlink()
        raise
    else:
        destination_connection.close()
        source_connection.close()
    _cleanup_sidecars(partial)
    if integrity != "ok":
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"备份完整性检查失败：{integrity}")
    backup_inspection = inspect_sqlite_database(partial)
    _cleanup_sidecars(partial)
    if backup_inspection.status != "PASS":
        partial.unlink(missing_ok=True)
        raise RuntimeError("备份数据库未通过完整性、外键或审计链检查")
    try:
        with partial.open("r+b") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        partial.replace(destination)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    backup_hash = _sha256(destination)
    return BackupResult(
        source_sha256=source_hash,
        backup_sha256=backup_hash,
        backup_path=str(destination),
        size_bytes=destination.stat().st_size,
        integrity=integrity,
        audit_status=backup_inspection.audit_status,
    )


def restore_sqlite_backup(backup: str | Path, target: str | Path) -> RestoreResult:
    backup_path = Path(backup).expanduser().resolve()
    target_path = Path(target).expanduser().resolve()
    if not backup_path.is_file():
        raise FileNotFoundError(f"备份数据库不存在：{backup_path}")
    backup_inspection = inspect_sqlite_database(backup_path)
    if backup_inspection.status != "PASS":
        raise RuntimeError("备份数据库未通过完整性、外键或审计链检查")
    if target_path.exists():
        raise FileExistsError(f"恢复目标已存在，禁止覆盖：{target_path}")
    if not target_path.parent.is_dir():
        raise FileNotFoundError(f"恢复目标目录不存在：{target_path.parent}")
    if target_path == backup_path:
        raise ValueError("恢复目标不得与备份文件相同")
    partial = target_path.with_suffix(target_path.suffix + ".partial")
    if partial.exists():
        raise FileExistsError(f"恢复临时目标已存在：{partial}")
    source_connection = sqlite3.connect(_readonly_uri(backup_path), uri=True)
    target_connection = sqlite3.connect(partial)
    try:
        source_connection.backup(target_connection)
        target_connection.commit()
    except Exception:
        target_connection.close()
        source_connection.close()
        _cleanup_sidecars(partial)
        partial.unlink(missing_ok=True)
        raise
    else:
        target_connection.close()
        source_connection.close()
    _cleanup_sidecars(partial)
    restored_inspection = inspect_sqlite_database(partial)
    _cleanup_sidecars(partial)
    if restored_inspection.status != "PASS":
        partial.unlink(missing_ok=True)
        raise RuntimeError("恢复副本未通过完整性、外键或审计链检查")
    try:
        with partial.open("r+b") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        partial.replace(target_path)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    final_inspection = inspect_sqlite_database(target_path)
    return RestoreResult(
        backup_sha256=backup_inspection.sha256,
        restored_sha256=final_inspection.sha256,
        restored_path=str(target_path),
        size_bytes=target_path.stat().st_size,
        integrity=final_inspection.integrity,
        audit_status=final_inspection.audit_status,
    )

"""Create governed task candidates and record explicit human review decisions."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime

from .models import (
    Assignee,
    ReviewRecord,
    ReviewedTask,
    RpaSource,
    RpaTaskCreateRequest,
    TaskCandidate,
    TaskCandidateBundle,
)
from app.reporting.models import ReportContract


CANDIDATE_VERSION = "1.1.0"
BUNDLE_VERSION = "1.1.0"
PLACEHOLDER_PREFIXES = ("待", "暂无", "未指定", "未知")


class RpaWorkflowError(RuntimeError):
    pass


def _canonical_hash(payload: object, *, uppercase: bool = False) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    value = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return value.upper() if uppercase else value


def _is_confirmed(value: str | None) -> bool:
    return bool(value and not value.startswith(PLACEHOLDER_PREFIXES))


def build_task_candidates(contract: ReportContract) -> TaskCandidateBundle:
    if contract.validation_status != "PASS":
        raise RpaWorkflowError("报告契约未通过验证，禁止生成RPA任务候选")
    task_table = contract.dynamic_tables.get("整改任务表格")
    suggestion_table = contract.dynamic_tables.get("改进建议表格")
    finding_field = contract.fields.get("需关注问题")
    if task_table is None or suggestion_table is None or finding_field is None:
        raise RpaWorkflowError("报告契约缺少整改任务、改进建议或需关注问题字段")
    if not suggestion_table.rows:
        raise RpaWorkflowError("改进建议表没有可用于任务候选的记录")
    if finding_field.status != "generated" or not finding_field.source_refs:
        raise RpaWorkflowError("任务finding必须来自带来源的已验证报告字段")

    contract_hash = _canonical_hash(contract.model_dump(mode="json"), uppercase=True)
    candidates: list[TaskCandidate] = []
    for index, row in enumerate(task_table.rows):
        if len(row) != 6:
            raise RpaWorkflowError(f"整改任务第{index + 1}行字段数不是6")
        task_id, title, _raw_assignee, priority, _source, _deadline = row
        suggestion_row = suggestion_table.rows[min(index, len(suggestion_table.rows) - 1)]
        suggestion = f"{suggestion_row[1]}；预期结果：{suggestion_row[4]}"
        suggested_department = suggestion_row[2] if _is_confirmed(suggestion_row[2]) else None
        core = {
            "report_number": contract.report_number,
            "task_id": task_id,
            "title": title,
            "finding": finding_field.value,
            "suggestion": suggestion,
        }
        idempotency_key = _canonical_hash(core)
        candidates.append(
            TaskCandidate(
                candidate_version=CANDIDATE_VERSION,
                candidate_id=f"CAND-{idempotency_key[:16].upper()}",
                idempotency_key=idempotency_key,
                task_id=task_id,
                report_number=contract.report_number,
                report_contract_sha256=contract_hash,
                analysis_month=contract.month,
                analysis_type=contract.analysis_type,
                analysis_period=contract.period or contract.month,
                product=contract.product,
                source_field="需关注问题",
                source_refs=finding_field.source_refs,
                task_title=title,
                finding=finding_field.value,
                suggestion=suggestion,
                suggested_priority=priority,
                suggested_department=suggested_department,
                created_at=datetime.fromisoformat(f"{contract.generated_date}T00:00:00+08:00"),
            )
        )
    return TaskCandidateBundle(
        bundle_version=BUNDLE_VERSION,
        report_number=contract.report_number,
        report_contract_sha256=contract_hash,
        candidates=candidates,
    )


def apply_controlled_task_wording(
    candidate: TaskCandidate,
    *,
    task_title: str,
    suggestion: str,
    suggested_priority: str,
    suggested_department: str | None,
) -> TaskCandidate:
    """Apply only model-controllable fields and refresh content-derived identifiers."""

    core = {
        "report_number": candidate.report_number,
        "task_id": candidate.task_id,
        "title": task_title,
        "finding": candidate.finding,
        "suggestion": suggestion,
    }
    idempotency_key = _canonical_hash(core)
    try:
        payload = candidate.model_dump(mode="python")
        payload.update(
            {
                "candidate_version": CANDIDATE_VERSION,
                "candidate_id": f"CAND-{idempotency_key[:16].upper()}",
                "idempotency_key": idempotency_key,
                "task_title": task_title,
                "suggestion": suggestion,
                "suggested_priority": suggested_priority,
                "suggested_department": suggested_department,
            }
        )
        return TaskCandidate.model_validate(payload)
    except ValueError as exc:
        raise RpaWorkflowError(f"受控任务文字不符合候选契约：{exc}") from exc


def approve_candidate(
    candidate: TaskCandidate,
    *,
    reviewer: str,
    decided_at: datetime,
    assignee_name: str,
    department: str,
    role: str | None,
    deadline: date,
    priority: str,
    notify_method: str,
    comment: str | None = None,
) -> ReviewedTask:
    for label, value in (
        ("审批人", reviewer),
        ("责任人", assignee_name),
        ("责任部门", department),
    ):
        if not _is_confirmed(value):
            raise RpaWorkflowError(f"{label}必须由人工明确填写，不能使用占位值")
    if decided_at.tzinfo is None or decided_at.utcoffset() is None:
        raise RpaWorkflowError("审批时间必须包含时区")
    if deadline < decided_at.date():
        raise RpaWorkflowError("截止日期不能早于审批日期")
    try:
        payload = RpaTaskCreateRequest(
            task_id=candidate.task_id,
            task_title=candidate.task_title,
            assignee=Assignee(name=assignee_name, department=department, role=role),
            source=RpaSource(
                analysis_type=candidate.analysis_type,
                analysis_month=candidate.analysis_month,
                product=candidate.product,
                finding=candidate.finding,
            ),
            priority=priority,
            deadline=deadline,
            suggestion=candidate.suggestion,
            notify_method=notify_method,
            created_at=decided_at,
        )
    except ValueError as exc:
        raise RpaWorkflowError(f"审批输入不符合RPA接口契约：{exc}") from exc
    return ReviewedTask(
        candidate=candidate,
        review=ReviewRecord(
            decision="approved",
            reviewer=reviewer,
            decided_at=decided_at,
            comment=comment,
        ),
        payload=payload,
    )


def reject_candidate(
    candidate: TaskCandidate,
    *,
    reviewer: str,
    decided_at: datetime,
    comment: str,
) -> ReviewedTask:
    if not _is_confirmed(reviewer):
        raise RpaWorkflowError("审批人必须由人工明确填写")
    if decided_at.tzinfo is None or decided_at.utcoffset() is None:
        raise RpaWorkflowError("审批时间必须包含时区")
    if not comment.strip():
        raise RpaWorkflowError("拒绝任务必须填写原因")
    return ReviewedTask(
        candidate=candidate,
        review=ReviewRecord(
            decision="rejected",
            reviewer=reviewer,
            decided_at=decided_at,
            comment=comment,
        ),
        payload=None,
    )

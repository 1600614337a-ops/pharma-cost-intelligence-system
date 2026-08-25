"""Controlled model assistance for non-executable remediation task candidates."""

from __future__ import annotations

import json
import re

from app.reporting.contract import FORBIDDEN_PATTERNS
from app.reporting.models import ReportContract
from app.rpa.models import TaskCandidateBundle, TaskGeneration
from app.rpa.workflow import apply_controlled_task_wording

from .client import LlmCallError, OpenAICompatibleClient
from .guardrails import CAUSAL_PATTERNS, LlmGuardrailError, _approved_text, _numbers
from .models import LlmSettings, TaskCandidateDraftBundle


ACTION_PATTERN = re.compile(
    r"(?:核查|复核|核验|比对|分析|确认|建立|跟踪|监控|完善|抽查|整改|优化|评估|补充|归档)"
)
EXECUTION_CLAIMS = (
    r"(?:已经|已)(?:审批|审核|确认|提交|发送|下发|执行|完成)",
    r"RPA.{0,6}(?:已|自动)(?:提交|发送|下发|执行)",
)
PRIORITY_RANK = {"low": 1, "medium": 2, "high": 3}


def _allowed_departments(contract: ReportContract, bundle: TaskCandidateBundle) -> list[str]:
    departments = {
        candidate.suggested_department
        for candidate in bundle.candidates
        if candidate.suggested_department
    }
    table = contract.dynamic_tables.get("改进建议表格")
    if table is not None:
        for row in table.rows:
            if len(row) >= 3:
                value = row[2].strip()
                if value and not value.startswith(("待", "暂无", "未指定", "未知")):
                    departments.add(value)
    return sorted(departments)


def _task_prompt(contract: ReportContract, bundle: TaskCandidateBundle) -> str:
    candidates = [
        {
            "candidate_id": item.candidate_id,
            "task_title": item.task_title,
            "finding": item.finding,
            "suggestion": item.suggestion,
            "suggested_priority": item.suggested_priority,
            "suggested_department": item.suggested_department,
            "source_refs": item.source_refs,
        }
        for item in bundle.candidates
    ]
    evidence = {
        "报告编号": contract.report_number,
        "分析类型": contract.analysis_type,
        "分析期间": contract.period or contract.month,
        "产品": contract.product,
        "允许责任部门": _allowed_departments(contract, bundle),
        "确定性候选": candidates,
        "受治理知识引用": contract.evidence.model_dump(mode="json"),
        "关键确定性字段": {
            name: field.value
            for name, field in contract.fields.items()
            if name in {
                "本月单位成本",
                "单位成本环比",
                "单位成本同比",
                "单位成本预算偏差",
                "材料贡献度",
                "人工贡献度",
                "制造费用贡献度",
                "需关注问题",
            }
        },
    }
    return (
        "请逐条返回同数量、同顺序的整改任务候选。candidate_id必须原样返回；"
        "标题和建议应清晰、可执行，但只能使用输入事实。责任部门只能从允许责任部门中选择，"
        "优先级只能保持或提高，不能降低。不得生成责任人、截止日期、审批意见、通知方式或执行结果，"
        "也不得声称任务已审批、已提交或已执行。没有必要改写时可原样返回。输入证据包如下：\n"
        + json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
    )


def _validate_task_drafts(
    contract: ReportContract,
    bundle: TaskCandidateBundle,
    drafts: TaskCandidateDraftBundle,
) -> None:
    if len(drafts.candidates) != len(bundle.candidates):
        raise LlmGuardrailError("模型改变了整改候选数量")
    allowed_departments = set(_allowed_departments(contract, bundle))
    approved_text = _approved_text(contract) + "\n" + "\n".join(
        f"{item.task_title}\n{item.finding}\n{item.suggestion}"
        for item in bundle.candidates
    )
    approved_numbers = _numbers(approved_text)
    for baseline, draft in zip(bundle.candidates, drafts.candidates, strict=True):
        if draft.candidate_id != baseline.candidate_id:
            raise LlmGuardrailError("模型改变了候选ID或候选顺序")
        if PRIORITY_RANK[draft.suggested_priority] < PRIORITY_RANK[baseline.suggested_priority]:
            raise LlmGuardrailError("模型降低了确定性候选优先级")
        if draft.suggested_department is not None and draft.suggested_department not in allowed_departments:
            raise LlmGuardrailError("模型输出了未批准的责任部门")
        generated_text = f"{draft.task_title}\n{draft.suggestion}"
        unapproved = _numbers(generated_text) - approved_numbers
        if unapproved:
            raise LlmGuardrailError(f"模型任务输出含未批准数字：{sorted(unapproved)}")
        for pattern in (*FORBIDDEN_PATTERNS, *CAUSAL_PATTERNS, *EXECUTION_CLAIMS):
            if re.search(pattern, generated_text):
                raise LlmGuardrailError(f"模型任务输出命中禁止表达：{pattern}")
        if not ACTION_PATTERN.search(draft.suggestion):
            raise LlmGuardrailError("模型整改建议缺少可执行动作")


def _fallback(
    bundle: TaskCandidateBundle,
    settings: LlmSettings,
    warning: str,
    *,
    attempt_count: int = 0,
) -> TaskCandidateBundle:
    return bundle.model_copy(
        deep=True,
        update={
            "generation": TaskGeneration(
                mode="deterministic",
                status="fallback",
                provider_protocol=settings.api_style,
                model=settings.model,
                attempt_count=attempt_count,
                warnings=[warning],
            )
        },
    )


def enhance_task_candidates(
    contract: ReportContract,
    bundle: TaskCandidateBundle,
    settings: LlmSettings | None = None,
    *,
    client: OpenAICompatibleClient | None = None,
) -> TaskCandidateBundle:
    """Return governed model-assisted candidates, or the deterministic bundle on failure."""

    settings = settings or LlmSettings.from_env()
    if not settings.enabled:
        return bundle.model_copy(deep=True)
    if settings.readiness_issue:
        return _fallback(bundle, settings, settings.readiness_issue)

    owns_client = client is None
    model_client = client or OpenAICompatibleClient(settings)
    try:
        response = model_client.generate_task_candidates(_task_prompt(contract, bundle))
        _validate_task_drafts(contract, bundle, response.draft)
        candidates = [
            apply_controlled_task_wording(
                baseline,
                task_title=draft.task_title,
                suggestion=draft.suggestion,
                suggested_priority=draft.suggested_priority,
                suggested_department=draft.suggested_department,
            )
            for baseline, draft in zip(bundle.candidates, response.draft.candidates, strict=True)
        ]
        return bundle.model_copy(
            deep=True,
            update={
                "bundle_version": "1.1.0",
                "candidates": candidates,
                "generation": TaskGeneration(
                    mode="llm",
                    status="generated",
                    provider_protocol=settings.api_style,
                    model=settings.model,
                    request_id=response.request_id,
                    attempt_count=response.attempt_count,
                ),
            },
        )
    except LlmCallError as exc:
        return _fallback(bundle, settings, str(exc), attempt_count=exc.attempt_count)
    except LlmGuardrailError as exc:
        return _fallback(bundle, settings, str(exc))
    except Exception as exc:
        return _fallback(bundle, settings, f"大模型任务生成失败：{type(exc).__name__}")
    finally:
        if owns_client:
            model_client.close()

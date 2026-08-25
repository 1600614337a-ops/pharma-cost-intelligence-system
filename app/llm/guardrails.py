"""Post-generation fact, boundary, and protected-field checks."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from app.reporting.contract import FORBIDDEN_PATTERNS
from app.reporting.models import ReportContract

from .models import NarrativeDraft


NUMBER_PATTERN = re.compile(r"(?<![A-Za-z0-9_])[-+]?\d[\d,]*(?:\.\d+)?%?")
CAUSAL_PATTERNS = (
    r"市场(?:行情|价格|价).{0,12}(?:导致|造成|引发).{0,16}(?:成本|费用)",
    r"采购(?:价格|价|成本).{0,12}(?:导致|造成|引发)",
    r"(?:由于|因为|因).{0,10}采购(?:价格|价|成本)",
)
EXECUTION_PATTERNS = (
    r"(?:已经|已完成|已下发|已执行|执行成功|整改完成)",
    r"(?:无需|跳过)人工(?:审批|审核)",
)


class LlmGuardrailError(RuntimeError):
    pass


def _canonical_number(token: str) -> str:
    normalized = token.rstrip("%").replace(",", "")
    try:
        value = Decimal(normalized)
    except InvalidOperation:
        return normalized
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _numbers(text: str) -> set[str]:
    return {_canonical_number(match.group()) for match in NUMBER_PATTERN.finditer(text)}


def _approved_text(contract: ReportContract) -> str:
    parts = [field.value for field in contract.fields.values()]
    parts.extend(value for value in contract.evidence.model_dump().values() if value)
    for table in contract.dynamic_tables.values():
        parts.extend(table.headers)
        parts.extend(cell for row in table.rows for cell in row)
    return "\n".join(parts)


def _signed_decimal(value: str) -> Decimal:
    try:
        return Decimal(value.rstrip("%").replace(",", ""))
    except InvalidOperation as exc:
        raise LlmGuardrailError(f"确定性指标不是有效数值：{value}") from exc


def _validate_metric_direction(text: str, label: str, value: str) -> None:
    signed = _signed_decimal(value)
    if signed > 0:
        opposite = r"(?:下降|降低|减少)"
    elif signed < 0:
        opposite = r"(?:上升|上涨|增加)"
    else:
        opposite = r"(?:上升|上涨|增加|下降|降低|减少)"
    if re.search(rf"{re.escape(label)}.{{0,12}}{opposite}", text):
        raise LlmGuardrailError(f"模型写反{label}方向")


def _validate_budget_appraisal(text: str, value: str) -> None:
    signed = _signed_decimal(value)
    if signed > 0:
        opposite = r"(?:优于|低于|节约).{0,8}预算|预算.{0,8}(?:有利|节约)"
    elif signed < 0:
        opposite = r"(?:高于|超出|超过).{0,8}预算|预算.{0,8}(?:不利|超支)"
    else:
        opposite = r"(?:高于|低于|超出|超过|优于).{0,8}预算|预算.{0,8}(?:有利|不利|节约|超支)"
    if re.search(opposite, text):
        raise LlmGuardrailError("模型写反单位成本预算偏差的经营评价")


def validate_draft(contract: ReportContract, draft: NarrativeDraft) -> None:
    recommendation_text = ""
    if draft.recommendations:
        recommendation_text = "\n".join(
            "；".join(
                (
                    item.sequence,
                    item.action,
                    item.owner,
                    item.priority,
                    item.expected_effect,
                    item.due,
                )
            )
            for item in draft.recommendations
        )
    generated_text = "\n".join(
        [*draft.by_report_field().values(), recommendation_text]
    )
    unapproved = _numbers(generated_text) - _numbers(_approved_text(contract))
    if unapproved:
        raise LlmGuardrailError(f"模型输出含未批准数字：{sorted(unapproved)}")
    for pattern in (*FORBIDDEN_PATTERNS, *CAUSAL_PATTERNS):
        if re.search(pattern, generated_text):
            raise LlmGuardrailError(f"模型输出命中禁止表达：{pattern}")
    for pattern in EXECUTION_PATTERNS:
        if recommendation_text and re.search(pattern, recommendation_text):
            raise LlmGuardrailError(f"模型建议命中禁止执行表达：{pattern}")
    if draft.recommendations:
        baseline = contract.dynamic_tables.get("改进建议表格")
        if baseline is None:
            raise LlmGuardrailError("报告缺少确定性改进建议基线")
        if len(draft.recommendations) != len(baseline.rows):
            raise LlmGuardrailError("模型改变改进建议数量")
        priority_rank = {"低": 1, "中": 2, "高": 3}
        for item, row in zip(draft.recommendations, baseline.rows, strict=True):
            if item.sequence != row[0]:
                raise LlmGuardrailError("模型改变改进建议顺序")
            if priority_rank[item.priority] < priority_rank.get(row[3], 1):
                raise LlmGuardrailError("模型降低确定性建议优先级")
    material_text = draft.material_attribution
    material_contribution = contract.fields["材料贡献度"].value
    if material_contribution not in material_text:
        raise LlmGuardrailError("材料归因文本遗漏总成本变动贡献度")
    if "建议" not in material_text:
        raise LlmGuardrailError("材料归因文本遗漏可执行核查建议")
    difference_paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", draft.difference_attribution)
        if paragraph.strip()
    ]
    if not 2 <= len(difference_paragraphs) <= 4:
        raise LlmGuardrailError("差异归因文本必须分为2至4个自然段")
    total_cost_text = "\n".join(
        (draft.anomaly_investigation, draft.monthly_highlights)
    )
    _validate_metric_direction(
        total_cost_text,
        "单位成本环比",
        contract.fields["单位成本环比"].value,
    )
    _validate_metric_direction(
        total_cost_text,
        "单位成本同比",
        contract.fields["单位成本同比"].value,
    )
    _validate_budget_appraisal(
        total_cost_text,
        contract.fields["单位成本预算偏差"].value,
    )
    if contract.product == "六味地黄胶囊" and contract.month == "2026-03":
        anomaly = draft.anomaly_investigation
        if "设备" in anomaly and not any(term in anomaly for term in ("不量化", "无法量化", "不能量化")):
            raise LlmGuardrailError("设备事件缺少不可量化边界")

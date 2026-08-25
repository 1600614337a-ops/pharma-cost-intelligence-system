"""Apply optional model wording without weakening the deterministic report contract."""

from __future__ import annotations

import json

from app.reporting.models import ReportContract, ReportGeneration

from .client import LlmCallError, OpenAICompatibleClient
from .guardrails import LlmGuardrailError, validate_draft
from .models import LlmSettings, NARRATIVE_FIELDS


def _prompt(contract: ReportContract) -> str:
    baseline = {name: contract.fields[name].value for name in NARRATIVE_FIELDS}
    evidence = contract.evidence.model_dump(mode="json")
    benchmark_table = contract.dynamic_tables.get("对标差异表格")
    recommendation_table = contract.dynamic_tables.get("改进建议表格")

    def table_payload(table) -> dict[str, object] | None:
        if table is None:
            return None
        return {"headers": table.headers, "rows": table.rows}

    def direction(value: str) -> str:
        if value.startswith("-"):
            return "负值：下降"
        if value.rstrip("%").replace(",", "") in {"0", "0.0", "0.00"}:
            return "零值：持平"
        return "正值：上升"

    budget_value = contract.fields["单位成本预算偏差"].value
    budget_appraisal = (
        "负值：成本低于预算，经营有利"
        if budget_value.startswith("-")
        else "零值：与预算持平"
        if budget_value.rstrip("%").replace(",", "") in {"0", "0.0", "0.00"}
        else "正值：成本高于预算，经营不利"
    )
    protected = {
        "产品": contract.product,
        "分析类型": contract.analysis_type,
        "分析期间": contract.period or contract.month,
        "专题名称": contract.topic,
        "报告编号": contract.report_number,
        "确定性文字基线": baseline,
        "受治理知识引用": evidence,
        "对标差异结构": table_payload(benchmark_table),
        "确定性改进建议候选": table_payload(recommendation_table),
        "关键确定性字段": {
            name: contract.fields[name].value
            for name in (
                "本月单位成本",
                "上月单位成本",
                "单位成本环比",
                "本月材料成本",
                "材料成本环比",
                "材料贡献度",
                "本月人工成本",
                "人工成本环比",
                "本月制造费用",
                "制造费用环比",
            )
        },
        "方向与经营评价约束": {
            "单位成本环比": f"{contract.fields['单位成本环比'].value}；{direction(contract.fields['单位成本环比'].value)}",
            "单位成本同比": f"{contract.fields['单位成本同比'].value}；{direction(contract.fields['单位成本同比'].value)}",
            "单位成本预算偏差": f"{budget_value}；{budget_appraisal}",
        },
    }
    return (
        "请在不改变任何事实、数值、方向、阈值、缺失状态和引用的前提下，"
        "让七个文字字段更清晰、专业、简洁，并使用与分析类型一致的月度、季度或专题表述。没有必要改写时可原样返回。"
        "材料成本归因分析文本不得删除总成本变动额、对总成本变动贡献度或可执行核查建议。"
        "差异归因分析文本须分为2至4个自然段，覆盖总体差异、原因研判和证据边界。"
        "改进建议列表必须逐条对应确定性建议候选，数量和顺序不变；应结合主导差异、具体产品、材料名称及证据缺口改写行动和预期效果。"
        "责任部门只能使用Schema允许值，优先级不得低于确定性基线，截止状态必须为待业务审批。"
        "单位成本环比、同比的上升下降方向以及预算偏差的经营评价必须服从证据包中的明确约束；"
        "如果不需要方向性修饰，可直接保留带符号数值，不得自行改写方向或优劣。"
        "只能输出Schema要求的七个文字字段和改进建议列表。输入证据包如下：\n"
        + json.dumps(protected, ensure_ascii=False, separators=(",", ":"))
    )


def _fallback(
    contract: ReportContract,
    settings: LlmSettings,
    warning: str,
    *,
    attempt_count: int = 0,
) -> ReportContract:
    return contract.model_copy(
        deep=True,
        update={
            "generation": ReportGeneration(
                mode="deterministic",
                status="fallback",
                provider_protocol=settings.api_style,
                model=settings.model,
                attempt_count=attempt_count,
                warnings=[warning],
            )
        },
    )


def enhance_report_contract(
    contract: ReportContract,
    settings: LlmSettings | None = None,
    *,
    client: OpenAICompatibleClient | None = None,
) -> ReportContract:
    """Return an enhanced copy, or the untouched deterministic copy on any failure."""

    settings = settings or LlmSettings.from_env()
    if not settings.enabled:
        return contract.model_copy(deep=True)
    if settings.readiness_issue:
        return _fallback(contract, settings, settings.readiness_issue)

    owns_client = client is None
    model_client = client or OpenAICompatibleClient(settings)
    try:
        response = model_client.generate(_prompt(contract))
        validate_draft(contract, response.draft)
        fields = {name: value.model_copy(deep=True) for name, value in contract.fields.items()}
        for name, text in response.draft.by_report_field().items():
            original = fields[name]
            fields[name] = original.model_copy(
                update={
                    "value": text,
                    "status": "generated",
                    "rule": "大模型受控改写；数值、方向与引用来自确定性报告契约",
                }
            )
        dynamic_tables = {
            name: table.model_copy(deep=True)
            for name, table in contract.dynamic_tables.items()
        }
        if response.draft.recommendations is not None:
            recommendation_table = dynamic_tables.get("改进建议表格")
            if recommendation_table is None:
                raise LlmGuardrailError("报告缺少确定性改进建议基线")
            recommendation_table.rows = [
                [
                    item.sequence,
                    item.action,
                    item.owner,
                    item.priority,
                    item.expected_effect,
                    item.due,
                ]
                for item in response.draft.recommendations
            ]
        return contract.model_copy(
            deep=True,
            update={
                "fields": fields,
                "dynamic_tables": dynamic_tables,
                "generation": ReportGeneration(
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
        return _fallback(contract, settings, str(exc), attempt_count=exc.attempt_count)
    except LlmGuardrailError as exc:
        return _fallback(contract, settings, str(exc))
    except Exception as exc:  # external compatible providers can fail in non-standard ways
        return _fallback(contract, settings, f"大模型生成失败：{type(exc).__name__}")
    finally:
        if owns_client:
            model_client.close()

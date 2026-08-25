"""Template-derived report artifacts for quarterly and special analysis."""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from app.llm import LlmSettings, enhance_report_contract
from app.reporting import (
    ReportContract,
    build_report_contract,
    render_contract_json,
    render_docx,
    render_markdown,
    render_pdf,
)
from app.reporting.models import DynamicTable, ReportFieldValue

from .service import build_selected_dashboard_analysis
from .report_view import build_report_web_content


class PeriodReportError(RuntimeError):
    pass


PRODUCT_TASK_SEQUENCE = {"银黄口服液": "101", "板蓝根颗粒": "201", "六味地黄胶囊": "301"}
NARRATIVE_FIELDS = (
    "波动告警描述",
    "材料成本归因分析文本",
    "成本异常排查分析",
    "差异结构拆解分析",
    "差异归因分析文本",
    "本月亮点",
    "需关注问题",
)


def _decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _q(value: object, pattern: str = "0.01") -> Decimal | None:
    number = _decimal(value)
    return None if number is None else number.quantize(Decimal(pattern), rounding=ROUND_HALF_UP)


def _money(value: object) -> str:
    number = _q(value)
    return "暂无数据" if number is None else f"{number:,.2f}"


def _qty(value: object) -> str:
    number = _decimal(value)
    return "暂无数据" if number is None else f"{number:,.0f}"


def _number(value: object) -> str:
    return _money(value)


def _compact_large_amount(value: str) -> str:
    """Avoid line-wrapping seven-digit cumulative amounts in the fixed template."""

    try:
        number = Decimal(value.replace(",", ""))
    except Exception:
        return value
    return f"{number:.2f}" if abs(number) >= Decimal("1000000") else value


def _pct(value: object, status: str = "available") -> str:
    number = _q(value)
    if number is None:
        return "不适用" if status == "not_applicable" else "暂无数据"
    return f"{number:.2f}%"


def _direction(delta: object) -> str:
    number = _decimal(delta)
    if number is None:
        return "暂无数据"
    return "上升" if number > 0 else "下降" if number < 0 else "持平"


def _status(item: dict[str, Any] | None) -> str:
    if not item:
        return "unavailable"
    value = item.get("value", item.get("current"))
    return str(item.get("status") or ("available" if value is not None else "unavailable"))


def _source_refs(result: dict[str, Any]) -> list[str]:
    return [str(item["path"]) for item in result.get("sources", []) if item.get("path")]


def _replace_field(
    fields: dict[str, ReportFieldValue],
    name: str,
    value: str,
    *,
    status: str = "available",
    refs: list[str] | None = None,
    rule: str = "分析期间结构化数据确定性映射",
) -> None:
    if name not in fields:
        raise PeriodReportError(f"统一报告契约缺少字段：{name}")
    original = fields[name]
    fields[name] = original.model_copy(
        update={
            "value": value,
            "status": status,
            "source_refs": original.source_refs if refs is None else refs,
            "rule": rule,
        }
    )


def _metric_fields(result: dict[str, Any], fields: dict[str, ReportFieldValue], refs: list[str]) -> None:
    mappings = {
        "quantity": ("本月产量", "上月产量", "产量环比", _qty),
        "unit_cost": ("本月单位成本", "上月单位成本", "单位成本环比", _money),
        "total_cost": ("本月总成本", "上月总成本", "总成本环比", _money),
        "direct_material": ("本月材料成本", "上月材料成本", "材料成本环比", _money),
        "direct_labor": ("本月人工成本", "上月人工成本", "人工成本环比", _money),
        "manufacturing_overhead": ("本月制造费用", "上月制造费用", "制造费用环比", _money),
    }
    for key, (current_name, previous_name, rate_name, formatter) in mappings.items():
        item = result["kpis"][key]
        status = _status(item)
        _replace_field(fields, current_name, formatter(item.get("current")), status="available", refs=refs)
        _replace_field(fields, previous_name, formatter(item.get("previous")), status=status, refs=refs)
        _replace_field(fields, rate_name, _pct(item.get("change_rate_pct"), status), status=status, refs=refs)

    aliases = {
        "材料金额": "本月材料成本", "材料环比": "材料成本环比", "人工金额": "本月人工成本",
        "人工单位成本": "本月人工成本", "上月人工单位成本": "上月人工成本", "人工环比": "人工成本环比",
        "制造费用金额": "本月制造费用", "制造费用合计": "本月制造费用", "上月制造费用合计": "上月制造费用",
        "制造费用合计环比": "制造费用环比", "单位成本": "本月单位成本", "总环比": "单位成本环比",
    }
    for target, source in aliases.items():
        fields[target] = fields[source].model_copy(update={"name": target})


def _comparison_fields(
    result: dict[str, Any],
    fields: dict[str, ReportFieldValue],
    supplemental: dict[str, ReportFieldValue],
    refs: list[str],
) -> None:
    quantity_names = {"去年同月产量", "预算产量"}
    money_names = {
        "去年单位成本", "去年材料成本", "去年人工成本", "去年制造费用", "去年总成本",
        "预算人工成本", "预算制造费用", "预算单位成本", "预算总成本", "预算材料成本",
    }
    supplemental_names = {"去年材料成本", "材料同比", "去年人工成本", "人工同比", "去年制造费用", "制造费用同比"}
    for name, item in result.get("comparisons", {}).items():
        if name not in fields and name not in supplemental_names:
            continue
        status = _status(item)
        value = item.get("value")
        if item.get("unit") == "%":
            display = _pct(value, status)
        elif name in quantity_names:
            display = _qty(value)
        elif name in money_names:
            display = _money(value)
        else:
            display = _number(value)
        target = supplemental if name in supplemental_names else fields
        original = target[name]
        target[name] = original.model_copy(
            update={"value": display, "status": status, "source_refs": refs, "rule": item.get("reason") or "分析期间结构化数据计算"}
        )


def _structure_fields(result: dict[str, Any], fields: dict[str, ReportFieldValue], refs: list[str]) -> None:
    structures = {item["name"]: item for item in result["cost_structure"]}
    contributions = {item["name"]: item for item in result["total_cost_contributions"]}
    for component, prefix in (("直接材料", "材料"), ("直接人工", "人工"), ("制造费用", "制造费用")):
        structure = structures[component]
        contribution = contributions[component]
        _replace_field(fields, f"{prefix}占比", _pct(structure.get("share_pct"), structure.get("status", "available")), status=structure.get("status", "available"), refs=refs)
        _replace_field(fields, f"{prefix}贡献度", _pct(contribution.get("contribution_pct"), contribution.get("status", "available")), status=contribution.get("status", "available"), refs=refs, rule="要素总成本变动额÷总成本变动额×100%")


def _compact_quarterly_core_amounts(fields: dict[str, ReportFieldValue]) -> None:
    for name in ("本月总成本", "上月总成本", "去年总成本", "预算总成本"):
        field = fields[name]
        fields[name] = field.model_copy(update={"value": _compact_large_amount(str(field.value))})


def _manufacturing_fields(result: dict[str, Any], fields: dict[str, ReportFieldValue], refs: list[str]) -> None:
    prefixes = {"折旧费": "折旧", "动力费(水电气)": "动力", "人工(间接)": "间接人工", "检验费": "检验", "其他制造费用": "其他"}
    items = {item["name"]: item for item in result.get("manufacturing_drivers", [])}
    for category, prefix in prefixes.items():
        item = items.get(category)
        status = _status(item)
        current = None if item is None else item.get("current")
        previous = None if item is None else item.get("previous")
        rate = None if item is None else item.get("change_rate_pct")
        _replace_field(fields, f"本月{prefix}", _money(current), status=status, refs=refs)
        _replace_field(fields, f"上月{prefix}", _money(previous), status=status, refs=refs)
        _replace_field(fields, f"{prefix}环比", _pct(rate, status), status=status, refs=refs)
        rate_number = _decimal(rate)
        explanation = "暂无数据，未进行推测。" if rate_number is None else f"单位费用较上一期间{_direction(rate_number)}{_pct(abs(rate_number))}；{'超过' if abs(rate_number) > 10 else '未超过'}±10%告警阈值。"
        _replace_field(fields, f"{prefix}变动说明", explanation, status="generated", refs=refs, rule="确定性期间比较")


def _dynamic_tables(result: dict[str, Any], base: ReportContract, period: str) -> dict[str, DynamicTable]:
    material_rows = []
    for index, item in enumerate(result.get("material_drivers", []), start=1):
        status = item.get("status", "available")
        material_rows.append([
            str(index), item["name"], _money(item.get("current")), _money(item.get("previous")),
            _pct(item.get("change_rate_pct"), status), item.get("reason") or "由结构化成本明细确定性计算",
        ])

    trend_rows = []
    previous_unit: Decimal | None = None
    for item in result.get("trend", []):
        unit = _decimal(item.get("unit_cost"))
        rate = None if previous_unit in {None, Decimal("0")} or unit is None else (unit - previous_unit) / previous_unit * Decimal("100")
        trend_rows.append([
            str(item.get("month")), _qty(item.get("quantity_boxes")), _money(item.get("direct_material")),
            _money(item.get("direct_labor")), _money(item.get("manufacturing_overhead")), _money(unit), _pct(rate),
        ])
        previous_unit = unit

    benchmark_rows = []
    for item in result.get("factory_benchmark", []):
        direction = item.get("direction")
        benchmark_rows.append([
            item["name"], _money(item.get("target_value")), _money(item.get("benchmark_value")),
            _money(item.get("difference")), _pct(item.get("difference_rate_pct"), item.get("status", "available")),
            "有利" if direction == "favorable" else "不利" if direction == "unfavorable" else "持平",
        ])

    recommendation_rows = [
        [str(item["sequence"]), item["action"], item["owner"], item["priority"], item["expected_effect"], item["due"]]
        for item in result.get("recommendations", [])
    ]
    top_name = result.get("material_drivers", [{}])[0].get("name", "成本差异") if result.get("material_drivers") else "成本差异"
    month = result["meta"]["month"]
    task_rows = [[
        f"TASK-{month.replace('-', '')}-{PRODUCT_TASK_SEQUENCE[result['meta']['product']]}",
        f"复核{top_name}成本变动证据", "审批时指定", "medium", f"{period}{result['meta']['product']}{result['meta']['analysis_type']}", "审批时确定",
    ]]
    market = base.dynamic_tables["原材料价格跟踪表格"].model_copy(deep=True)
    market.headers = ["原材料", "年初价", "期末价", "涨幅", "趋势", "证据边界"]
    return {
        "原材料成本明细表格": DynamicTable(name="原材料成本明细表格", headers=["序号", "原材料名称", "本期单位消耗成本", "上期单位消耗成本", "期间变动", "证据边界"], rows=material_rows or [["1", "暂无数据", "暂无数据", "暂无数据", "暂无数据", "不推测"]]),
        "近6个月成本趋势表格": DynamicTable(name="近6个月成本趋势表格", headers=["期间", "产量(盒)", "材料", "人工", "制造费用", "单位成本", "期间变动"], rows=trend_rows),
        "原材料价格跟踪表格": market,
        "对标差异表格": DynamicTable(name="对标差异表格", headers=["维度", "一厂", "二厂", "差异", "差异率", "方向"], rows=benchmark_rows),
        "改进建议表格": DynamicTable(name="改进建议表格", headers=["序号", "建议事项", "责任部门", "优先级", "预期效果", "建议完成时间"], rows=recommendation_rows),
        "整改任务表格": DynamicTable(name="整改任务表格", headers=["任务编号", "任务标题", "责任人", "优先级", "来源", "截止时间"], rows=task_rows),
    }


def build_period_report_contract(
    data_root: str | Path,
    index_root: str | Path,
    *,
    analysis_type: str,
    product: str,
    month: str | None = None,
    quarter: str | None = None,
    topic: str | None = None,
    generated_date: str | None = None,
) -> ReportContract:
    """Map period analysis into the same 107-field contract and official template as monthly reports."""

    result = build_selected_dashboard_analysis(
        data_root, index_root, analysis_type=analysis_type, product=product,
        month=month, quarter=quarter, topic=topic,
    )
    end_month = result["meta"]["month"]
    period = str(result["meta"].get("period") or end_month)
    base = build_report_contract(data_root, index_root, product, end_month, generated_date=generated_date or date.today().isoformat())
    fields = {name: value.model_copy(deep=True) for name, value in base.fields.items()}
    supplemental = {name: value.model_copy(deep=True) for name, value in base.supplemental_fields.items()}
    refs = _source_refs(result)

    title = f"{period} {product}季度成本分析报告" if analysis_type == "季度成本分析" else f"{period} {product}{topic}报告"
    for name, value, rule in (
        ("报告标题", title, "统一标题模板"),
        ("报告编号", result["meta"]["report_number"], "分析类型编号规则"),
        ("报告类型", analysis_type, "固定报告类型"),
        ("分析月份", period, "分析期间参数"),
    ):
        _replace_field(fields, name, str(value), status="generated" if name != "分析月份" else "available", refs=[] if name != "分析月份" else refs, rule=rule)

    _metric_fields(result, fields, refs)
    _comparison_fields(result, fields, supplemental, refs)
    _structure_fields(result, fields, refs)
    if analysis_type == "季度成本分析":
        _compact_quarterly_core_amounts(fields)
    _manufacturing_fields(result, fields, refs)
    for name in NARRATIVE_FIELDS:
        _replace_field(fields, name, str(result["narratives"][name]), status="generated", refs=refs, rule="确定性数据与受治理知识证据生成")

    material_text = str(fields["材料成本归因分析文本"].value)
    material_contribution = str(fields["材料贡献度"].value)
    if "贡献度" not in material_text:
        material_text += f" 直接材料对总成本变动的贡献度为{material_contribution}。"
    if "建议" not in material_text:
        material_text += " 建议复核实际采购量价、批次领退料、投料耗用与工艺收率证据；缺少数据时仅列为核验方向，不作因果推断。"
    _replace_field(
        fields,
        "材料成本归因分析文本",
        material_text,
        status="generated",
        refs=refs,
        rule="确定性数据、总成本变动贡献度与受治理知识证据生成",
    )

    tables = _dynamic_tables(result, base, period)
    for name, table in tables.items():
        markdown_rows = "\n".join("| " + " | ".join(row) + " |" for row in table.rows)
        _replace_field(fields, name, markdown_rows, status="generated", refs=refs, rule="动态表格")

    issues = []
    if len(fields) != 107:
        issues.append(f"报告字段数应为107，实际为{len(fields)}")
    if set(tables) != set(base.dynamic_tables):
        issues.append("动态表格集合与官方模板不一致")
    return base.model_copy(
        deep=True,
        update={
            "analysis_version": result["meta"]["analysis_version"],
            "formula_version": result["meta"]["formula_version"],
            "knowledge_index_version": result["meta"]["knowledge_index_version"],
            "analysis_type": analysis_type,
            "period": period,
            "topic": topic,
            "month": end_month,
            "report_number": result["meta"]["report_number"],
            "fields": fields,
            "supplemental_fields": supplemental,
            "dynamic_tables": tables,
            "validation_status": "PASS" if not issues else "FAIL",
            "validation_issues": issues,
        },
    )


def _adapt_markdown(path: Path, contract: ReportContract) -> None:
    if contract.analysis_type == "月度成本分析":
        return
    text = path.read_text(encoding="utf-8")
    for old, new in {
        "分析月份": "分析期间", "去年同月": "去年同期", "本月": "本期", "上月": "上期",
        "近6个月单位成本趋势": "期间单位成本趋势",
    }.items():
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8", newline="\n")


def generate_period_report_artifacts(
    *, data_root: str | Path, index_root: str | Path, output_root: str | Path,
    analysis_type: str, product: str, month: str | None, quarter: str | None,
    topic: str | None, artifact_files: dict[str, tuple[str, str]], use_llm: bool = False,
) -> dict[str, object]:
    output = Path(output_root).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report_id = uuid.uuid4().hex
    final = output / report_id
    staging = output / f".{report_id}.staging"
    staging.mkdir()
    try:
        contract = build_period_report_contract(
            data_root, index_root, analysis_type=analysis_type, product=product,
            month=month, quarter=quarter, topic=topic,
        )
        if use_llm:
            contract = enhance_report_contract(contract, LlmSettings.from_env(force_enabled=True))
        if contract.validation_status != "PASS":
            raise PeriodReportError(f"报告契约校验失败：{contract.validation_issues}")
        render_contract_json(contract, staging / "report.json")
        render_markdown(contract, staging / "report.md")
        _adapt_markdown(staging / "report.md", contract)
        render_docx(contract, staging / "report.docx")
        render_pdf(contract, staging / "report.pdf", source_docx=staging / "report.docx")
        web_content = build_report_web_content(contract)
        manifest = {
            "report_id": report_id, "report_number": contract.report_number,
            "product": contract.product, "month": contract.month, "period": contract.period,
            "analysis_type": contract.analysis_type, "topic": contract.topic,
            "workflow_supported": True, "generation": contract.generation.model_dump(mode="json"),
        }
        (staging / "artifact_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        staging.replace(final)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return {
        **manifest, "web_content": web_content,
        "preview_url": f"/api/reports/{report_id}/preview",
        "downloads": {kind: f"/api/reports/{report_id}/download/{kind}" for kind in artifact_files},
    }

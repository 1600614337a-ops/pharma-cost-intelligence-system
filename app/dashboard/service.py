"""Read-only application service joining analysis, trend, and governed evidence."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from app.analysis import AnalysisError, analyze_cost
from app.data_quality import load_validated_data
from app.reporting import build_report_contract
from app.reporting.benchmark_guidance import BenchmarkGap, build_benchmark_guidance
from app.reporting.trend_context import build_trend_context
from app.knowledge import llamaindex_available
from app.llm import LlmSettings


ANALYSIS_TYPES = ["月度成本分析", "季度成本分析", "专题分析"]
SPECIAL_TOPICS = ["原材料涨价专项", "工厂成本差异专项"]
COMPONENTS = (
    ("直接材料", "direct_material"),
    ("直接人工", "direct_labor"),
    ("制造费用", "manufacturing_overhead"),
)
HEATMAP_FACTORS = (
    ("直接材料", "direct_material"),
    ("直接人工", "direct_labor"),
    ("制造费用", "manufacturing_overhead"),
    ("单位成本", "unit_cost"),
)


def _number(value: Decimal | int | None) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    return float(value)


def dashboard_options(data_dir: str | Path) -> dict[str, Any]:
    """Return only validated products/months that the current engine can analyze."""

    report, bundle = load_validated_data(data_dir)
    if report.errors:
        raise AnalysisError("源数据存在阻断错误，分析入口已关闭")
    products = sorted({row.product for row in bundle.plant1_summary})
    months = sorted({row.month for row in bundle.plant1_summary})
    quarters = sorted({f"{month[:4]}-Q{(int(month[5:7]) - 1) // 3 + 1}" for month in months})
    llm = LlmSettings.from_env(force_enabled=True)
    return {
        "analysis_types": ANALYSIS_TYPES,
        "products": products,
        "months": months,
        "quarters": quarters,
        "topics": SPECIAL_TOPICS,
        "llm": {
            "ready": llm.readiness_issue is None,
            "model": llm.model,
            "issue": llm.readiness_issue,
        },
        "rag": {
            "default": "native",
            "llamaindex_adapter_available": llamaindex_available(),
            "ranking_policy": "governed-native",
        },
        "data_quality": {
            "status": report.status,
            "warning_count": len(report.warnings),
            "validated_files": len(report.files),
        },
    }


def _calendar_previous_month(month: str) -> str:
    year = int(month[:4])
    number = int(month[5:7])
    if number == 1:
        return f"{year - 1}-12"
    return f"{year}-{number - 1:02d}"


def build_heatmap_data(data_dir: str | Path) -> dict[str, Any]:
    """Return product-by-month factor facts without browser-side calculations."""

    report, bundle = load_validated_data(data_dir)
    if report.errors:
        raise AnalysisError("源数据存在阻断错误，热力图入口已关闭")
    products = sorted({row.product for row in bundle.plant1_summary})
    months = sorted({row.month for row in bundle.plant1_summary})
    plant1 = {(row.product, row.month): row for row in bundle.plant1_summary}
    plant2 = {(row.product, row.month): row for row in bundle.plant2_summary}
    cells: list[dict[str, Any]] = []
    for product in products:
        for month in months:
            current_row = plant1.get((product, month))
            if current_row is None:
                continue
            previous_row = plant1.get((product, _calendar_previous_month(month)))
            benchmark_row = plant2.get((product, month))
            for factor, attribute in HEATMAP_FACTORS:
                current = getattr(current_row, attribute)
                previous = getattr(previous_row, attribute) if previous_row else None
                benchmark = getattr(benchmark_row, attribute) if benchmark_row else None
                delta = current - previous if previous is not None else None
                change_rate = (
                    None
                    if previous in {None, Decimal("0")}
                    else delta / previous * Decimal("100")
                )
                difference = current - benchmark if benchmark is not None else None
                cells.append(
                    {
                        "product": product,
                        "month": month,
                        "factor": factor,
                        "unit": "元/盒",
                        "current": _number(current),
                        "previous": _number(previous),
                        "delta": _number(delta),
                        "change_rate_pct": _number(change_rate),
                        "benchmark": _number(benchmark),
                        "factory_difference": _number(difference),
                        "alert": bool(change_rate is not None and abs(change_rate) > Decimal("10")),
                    }
                )
    return {
        "products": products,
        "months": months,
        "factors": [name for name, _ in HEATMAP_FACTORS],
        "metrics": ["环比变动率", "要素单位成本", "一厂减二厂差异"],
        "cells": cells,
        "data_quality": {
            "status": report.status,
            "warning_count": len(report.warnings),
        },
        "source": "01_成本明细数据/中药一厂_成本汇总_2026年1-6月.csv；01_成本明细数据/中药二厂_成本汇总_2026年1-6月.csv",
    }


def _trend_rows(data_dir: Path, product: str, month: str) -> list[dict[str, Any]]:
    report, bundle = load_validated_data(data_dir)
    if report.errors:
        raise AnalysisError("源数据存在阻断错误，无法生成趋势")
    rows = [
        row
        for row in bundle.plant1_summary
        if row.product == product and row.month <= month
    ]
    rows.sort(key=lambda item: item.month)
    return [
        {
            "month": row.month,
            "quantity_boxes": row.quantity_boxes,
            "direct_material": _number(row.direct_material),
            "direct_labor": _number(row.direct_labor),
            "manufacturing_overhead": _number(row.manufacturing_overhead),
            "unit_cost": _number(row.unit_cost),
            "total_cost": _number(row.total_cost),
        }
        for row in rows[-6:]
    ]


def build_dashboard_analysis(
    data_dir: str | Path,
    index_dir: str | Path,
    product: str,
    month: str,
) -> dict[str, Any]:
    """Build a browser-ready view without changing the governed calculation layer."""

    root = Path(data_dir).resolve()
    analysis = analyze_cost(root, product, month)
    contract = build_report_contract(root, index_dir, product, month)
    if contract.validation_status != "PASS":
        raise AnalysisError("报告契约校验失败，分析结果禁止展示")

    summary = {item.name: item for item in analysis.summary}
    unit_cost = summary["单位成本"]
    total_cost = summary["总成本"]
    quantity = summary["产量"]
    unit_benchmark = next(
        item for item in analysis.factory_benchmark if item.name == "单位成本"
    )

    def metric(item: Any) -> dict[str, Any]:
        return {
            "name": item.name,
            "unit": item.unit,
            "current": _number(item.current),
            "previous": _number(item.previous),
            "delta": _number(item.delta),
            "change_rate_pct": _number(item.change_rate_pct),
            "status": item.status,
            "reason": item.reason,
        }

    report_metrics = {
        item.name: {
            "name": item.name,
            "unit": item.unit,
            "value": _number(item.value),
            "status": item.status,
            "reason": item.reason,
        }
        for item in analysis.report_metrics
    }
    _, validated_bundle = load_validated_data(root)
    prior_year_month = f"{int(month[:4]) - 1}-{month[5:7]}"
    prior_year_row = next(
        (
            row
            for row in validated_bundle.plant1_prior_summary
            if row.factory == analysis.request.factory
            and row.product == product
            and row.month == prior_year_month
        ),
        None,
    )
    component_comparison_names = (
        ("直接材料", "direct_material", "材料", "去年材料成本", "材料同比"),
        ("直接人工", "direct_labor", "人工", "去年人工成本", "人工同比"),
        ("制造费用", "manufacturing_overhead", "制造费用", "去年制造费用", "制造费用同比"),
    )
    for component_name, attribute, _, prior_name, yoy_name in component_comparison_names:
        current_value = summary[component_name].current
        prior_value = getattr(prior_year_row, attribute) if prior_year_row else None
        report_metrics[prior_name] = _report_value(
            prior_name,
            "元/盒",
            prior_value,
            "缺少去年同月成本汇总",
        )
        report_metrics[yoy_name] = _report_variance(
            yoy_name,
            current_value,
            prior_value,
            "缺少去年同月成本汇总",
        )
    component_kpis = {
        "direct_material": metric(summary["直接材料"]),
        "direct_labor": metric(summary["直接人工"]),
        "manufacturing_overhead": metric(summary["制造费用"]),
    }
    material_source_order: dict[str, int] = {}
    for row in validated_bundle.material_detail:
        if (
            row.factory == analysis.request.factory
            and row.product == product
            and row.month == month
            and row.material_name not in material_source_order
        ):
            material_source_order[row.material_name] = len(material_source_order)
    tree_material_drivers = sorted(
        analysis.material_drivers,
        key=lambda item: (material_source_order.get(item.name, len(material_source_order)), item.name),
    )
    recommendation_table = contract.dynamic_tables["改进建议表格"]
    recommendations = [
        {
            "sequence": row[0],
            "action": row[1],
            "owner": row[2],
            "priority": row[3],
            "expected_effect": row[4],
            "due": row[5],
        }
        for row in recommendation_table.rows
    ]
    benchmark_by_name = {item.name: item for item in analysis.factory_benchmark}
    component_nodes = []
    for component_name in ("直接材料", "直接人工", "制造费用"):
        item = benchmark_by_name[component_name]
        node = {
            "name": component_name,
            "kind": "component",
            "target_value": _number(item.target_value),
            "benchmark_value": _number(item.benchmark_value),
            "difference": _number(item.difference),
            "difference_rate_pct": _number(item.difference_rate_pct),
            "direction": item.direction,
            "status": item.status,
            "children": [],
        }
        if component_name == "直接材料":
            node["children"] = [
                {
                    "name": detail.name,
                    "kind": "plant1_material",
                    "target_value": _number(detail.current),
                    "benchmark_value": None,
                    "difference": None,
                    "difference_rate_pct": None,
                    "status": "unavailable",
                    "reason": "二厂未提供原材料明细；仅展示一厂单位消耗成本",
                }
                for detail in tree_material_drivers
            ]
        component_nodes.append(node)
    unit_node = benchmark_by_name["单位成本"]
    benchmark_tree = {
        "name": product,
        "kind": "product",
        "children": [
            {
                "name": "单位成本",
                "kind": "total",
                "target_value": _number(unit_node.target_value),
                "benchmark_value": _number(unit_node.benchmark_value),
                "difference": _number(unit_node.difference),
                "difference_rate_pct": _number(unit_node.difference_rate_pct),
                "direction": unit_node.direction,
                "status": unit_node.status,
                "children": component_nodes,
            }
        ],
    }
    factory_benchmark_rows = [
        {
            "name": item.name,
            "target_value": _number(item.target_value),
            "benchmark_value": _number(item.benchmark_value),
            "difference": _number(item.difference),
            "difference_rate_pct": _number(item.difference_rate_pct),
            "direction": item.direction,
            "status": item.status,
            "reason": item.reason,
        }
        for item in analysis.factory_benchmark
    ]
    trend = _trend_rows(root, product, month)
    return {
        "meta": {
            "analysis_type": "月度成本分析",
            "product": product,
            "month": month,
            "previous_month": analysis.request.previous_month,
            "factory": analysis.request.factory,
            "benchmark_factory": analysis.request.benchmark_factory,
            "report_number": contract.report_number,
            "analysis_version": analysis.analysis_version,
            "formula_version": analysis.formula_version,
            "knowledge_index_version": contract.knowledge_index_version,
            "data_quality_status": analysis.data_quality_status,
            "data_quality_warning_count": analysis.data_quality_warning_count,
        },
        "kpis": {
            "unit_cost": metric(unit_cost),
            "total_cost": metric(total_cost),
            "quantity": metric(quantity),
            **component_kpis,
            "factory_benchmark": {
                "target": _number(unit_benchmark.target_value),
                "benchmark": _number(unit_benchmark.benchmark_value),
                "difference": _number(unit_benchmark.difference),
                "difference_rate_pct": _number(unit_benchmark.difference_rate_pct),
                "direction": unit_benchmark.direction,
                "status": unit_benchmark.status,
            },
        },
        "comparisons": report_metrics,
        "trend": trend,
        "trend_context": build_trend_context(
            (item["month"] for item in trend), "月度成本分析"
        ),
        "cost_structure": [
            {
                "name": item.name,
                "unit_cost": _number(item.unit_cost),
                "share_pct": _number(item.share_pct),
                "status": item.status,
                "reason": item.reason,
            }
            for item in analysis.cost_structure
        ],
        "waterfall": [
            {
                "name": item.name,
                "delta_unit_cost": _number(item.delta_unit_cost),
                "contribution_pct": _number(item.contribution_pct),
                "status": item.status,
                "reason": item.reason,
            }
            for item in analysis.contributions
        ],
        "total_cost_contributions": [
            {
                "name": item.name,
                "current_total_cost": _number(item.current_total_cost),
                "previous_total_cost": _number(item.previous_total_cost),
                "delta_total_cost": _number(item.delta_total_cost),
                "contribution_pct": _number(item.contribution_pct),
                "status": item.status,
                "reason": item.reason,
            }
            for item in analysis.total_cost_contributions
        ],
        "total_cost_bridge": {
            "previous_total_cost": _number(total_cost.previous),
            "current_total_cost": _number(total_cost.current),
            "quantity_effect": _number(analysis.total_cost_bridge.quantity_effect),
            "unit_cost_effect": _number(analysis.total_cost_bridge.unit_cost_effect),
            "total_cost_delta": _number(analysis.total_cost_bridge.total_cost_delta),
            "reconciliation_difference": _number(
                analysis.total_cost_bridge.reconciliation_difference
            ),
            "status": analysis.total_cost_bridge.status,
            "reason": analysis.total_cost_bridge.reason,
        },
        "material_drivers": [
            {
                "name": item.name,
                "current": _number(item.current),
                "previous": _number(item.previous),
                "delta": _number(item.delta),
                "change_rate_pct": _number(item.change_rate_pct),
                "contribution_pct": _number(item.contribution_pct),
                "status": item.status,
                "reason": item.reason,
            }
            for item in analysis.material_drivers
        ],
        "manufacturing_drivers": [
            {
                "name": item.name,
                "current": _number(item.current),
                "previous": _number(item.previous),
                "delta": _number(item.delta),
                "change_rate_pct": _number(item.change_rate_pct),
                "status": item.status,
                "reason": item.reason,
            }
            for item in analysis.manufacturing_drivers
        ],
        "factory_benchmark": factory_benchmark_rows,
        "benchmark_tree": benchmark_tree,
        "recommendations": recommendations,
        "alerts": [item.model_dump(mode="json") for item in analysis.alerts],
        "narratives": {
            name: contract.fields[name].value
            for name in (
                "波动告警描述",
                "材料成本归因分析文本",
                "成本异常排查分析",
                "差异结构拆解分析",
                "差异归因分析文本",
                "本月亮点",
                "需关注问题",
            )
        },
        "evidence": contract.evidence.model_dump(mode="json"),
        "unavailable_metrics": [
            item.model_dump(mode="json") for item in analysis.unavailable_metrics
        ],
        "sources": [item.model_dump(mode="json") for item in analysis.sources],
    }


def _quarter_months(quarter: str) -> list[str]:
    year, raw_quarter = quarter.split("-Q")
    first = (int(raw_quarter) - 1) * 3 + 1
    return [f"{year}-{month:02d}" for month in range(first, first + 3)]


def _previous_quarter(quarter: str) -> str:
    year, raw_quarter = quarter.split("-Q")
    number = int(raw_quarter)
    return f"{int(year) - 1}-Q4" if number == 1 else f"{year}-Q{number - 1}"


def _aggregate_summary(rows: list[Any]) -> dict[str, Decimal | int] | None:
    if not rows:
        return None
    quantity = sum(row.quantity_boxes for row in rows)
    total_cost = sum((row.total_cost for row in rows), Decimal("0"))
    result: dict[str, Decimal | int] = {
        "quantity_boxes": quantity,
        "total_cost": total_cost,
        "unit_cost": total_cost / Decimal(quantity),
    }
    for _, attribute in COMPONENTS:
        result[attribute] = sum(
            (getattr(row, attribute) * row.quantity_boxes for row in rows),
            Decimal("0"),
        ) / Decimal(quantity)
    return result


def _aggregate_total_cost_contributions(
    current: dict[str, Decimal | int],
    previous: dict[str, Decimal | int] | None,
) -> list[dict[str, Any]]:
    current_quantity = Decimal(current["quantity_boxes"])
    if previous is None:
        return [
            {
                "name": name,
                "current_total_cost": _number(Decimal(current[attribute]) * current_quantity),
                "previous_total_cost": None,
                "delta_total_cost": None,
                "contribution_pct": None,
                "status": "unavailable",
                "reason": "上一季度暂无数据",
            }
            for name, attribute in COMPONENTS
        ]
    previous_quantity = Decimal(previous["quantity_boxes"])
    denominator = Decimal(current["total_cost"]) - Decimal(previous["total_cost"])
    result = []
    for name, attribute in COMPONENTS:
        current_total = Decimal(current[attribute]) * current_quantity
        previous_total = Decimal(previous[attribute]) * previous_quantity
        delta = current_total - previous_total
        result.append(
            {
                "name": name,
                "current_total_cost": _number(current_total),
                "previous_total_cost": _number(previous_total),
                "delta_total_cost": _number(delta),
                "contribution_pct": _number(delta / denominator * Decimal("100")) if denominator != 0 else None,
                "status": "available" if denominator != 0 else "not_applicable",
                "reason": None if denominator != 0 else "总成本变动额为零",
            }
        )
    return result


def _aggregate_total_cost_bridge(
    current: dict[str, Decimal | int],
    previous: dict[str, Decimal | int] | None,
) -> dict[str, Any]:
    """Bridge the aggregate total-cost change into quantity and unit-cost effects."""

    if previous is None:
        return {
            "previous_total_cost": None,
            "current_total_cost": _number(Decimal(current["total_cost"])),
            "quantity_effect": None,
            "unit_cost_effect": None,
            "total_cost_delta": None,
            "reconciliation_difference": None,
            "status": "unavailable",
            "reason": "上一季度暂无数据",
        }
    current_quantity = Decimal(current["quantity_boxes"])
    previous_quantity = Decimal(previous["quantity_boxes"])
    current_unit_cost = Decimal(current["unit_cost"])
    previous_unit_cost = Decimal(previous["unit_cost"])
    current_total_cost = Decimal(current["total_cost"])
    previous_total_cost = Decimal(previous["total_cost"])
    quantity_effect = (current_quantity - previous_quantity) * previous_unit_cost
    unit_cost_effect = current_quantity * (current_unit_cost - previous_unit_cost)
    total_cost_delta = current_total_cost - previous_total_cost
    return {
        "previous_total_cost": _number(previous_total_cost),
        "current_total_cost": _number(current_total_cost),
        "quantity_effect": _number(quantity_effect),
        "unit_cost_effect": _number(unit_cost_effect),
        "total_cost_delta": _number(total_cost_delta),
        "reconciliation_difference": _number(
            total_cost_delta - quantity_effect - unit_cost_effect
        ),
        "status": "available",
        "reason": None,
    }


def _aggregate_budget(rows: list[Any]) -> dict[str, Decimal | int] | None:
    if not rows:
        return None
    quantity = sum(row.budget_quantity_boxes for row in rows)
    total_cost = sum((row.budget_total_cost for row in rows), Decimal("0"))
    result: dict[str, Decimal | int] = {
        "quantity_boxes": quantity,
        "total_cost": total_cost,
        "unit_cost": total_cost / Decimal(quantity),
    }
    for _, attribute in COMPONENTS:
        budget_attribute = f"budget_{attribute}"
        result[attribute] = sum(
            (getattr(row, budget_attribute) * row.budget_quantity_boxes for row in rows),
            Decimal("0"),
        ) / Decimal(quantity)
    return result


def _aggregate_labor(rows: list[Any]) -> dict[str, Decimal] | None:
    if not rows:
        return None
    quantity = sum((Decimal(row.quantity_boxes) for row in rows), Decimal("0"))
    hours = sum((row.total_hours for row in rows), Decimal("0"))
    labor_total = sum((row.direct_labor_total for row in rows), Decimal("0"))
    person_days = sum(
        (Decimal(row.production_headcount * row.work_days) for row in rows),
        Decimal("0"),
    )
    return {
        "hours": hours / quantity * Decimal("10000"),
        "wage": labor_total / hours,
        "efficiency": quantity / person_days,
    }


def _report_value(
    name: str,
    unit: str,
    value: Decimal | int | None,
    reason: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "unit": unit,
        "value": _number(value),
        "status": "available" if value is not None else "unavailable",
        "reason": None if value is not None else reason,
    }


def _report_variance(
    name: str,
    current: Decimal | int,
    baseline: Decimal | int | None,
    reason: str,
) -> dict[str, Any]:
    if baseline is None:
        return _report_value(name, "%", None, reason)
    current_value = Decimal(current)
    baseline_value = Decimal(baseline)
    if baseline_value == 0:
        return {
            "name": name,
            "unit": "%",
            "value": None,
            "status": "not_applicable" if current_value == 0 else "unavailable",
            "reason": "基准值为零，无法计算百分比",
        }
    return _report_value(
        name,
        "%",
        (current_value - baseline_value) / baseline_value * Decimal("100"),
        reason,
    )


def _quarter_report_metrics(
    current: dict[str, Decimal | int],
    prior_year: dict[str, Decimal | int] | None,
    budget: dict[str, Decimal | int] | None,
    labor_current: dict[str, Decimal] | None,
    labor_previous: dict[str, Decimal] | None,
) -> dict[str, dict[str, Any]]:
    prior_reason = "缺少去年同期三个月成本汇总"
    budget_reason = "缺少本季度预算数据"
    labor_reason = "缺少本季度人工工时明细"
    previous_labor_reason = "缺少上一季度人工工时明细"
    values = [
        _report_value("去年单位成本", "元/盒", prior_year["unit_cost"] if prior_year else None, prior_reason),
        _report_value("去年总成本", "元", prior_year["total_cost"] if prior_year else None, prior_reason),
        _report_value("去年同月产量", "盒", prior_year["quantity_boxes"] if prior_year else None, prior_reason),
        _report_variance("单位成本同比", current["unit_cost"], prior_year["unit_cost"] if prior_year else None, prior_reason),
        _report_variance("总成本同比", current["total_cost"], prior_year["total_cost"] if prior_year else None, prior_reason),
        _report_variance("产量同比", current["quantity_boxes"], prior_year["quantity_boxes"] if prior_year else None, prior_reason),
        _report_value("预算单位成本", "元/盒", budget["unit_cost"] if budget else None, budget_reason),
        _report_value("预算总成本", "元", budget["total_cost"] if budget else None, budget_reason),
        _report_value("预算产量", "盒", budget["quantity_boxes"] if budget else None, budget_reason),
        _report_variance("单位成本预算偏差", current["unit_cost"], budget["unit_cost"] if budget else None, budget_reason),
        _report_variance("总成本预算偏差", current["total_cost"], budget["total_cost"] if budget else None, budget_reason),
        _report_variance("产量预算偏差", current["quantity_boxes"], budget["quantity_boxes"] if budget else None, budget_reason),
    ]
    for component_name, attribute, prefix in (
        ("直接材料", "direct_material", "材料"),
        ("直接人工", "direct_labor", "人工"),
        ("制造费用", "manufacturing_overhead", "制造费用"),
    ):
        prior_value = prior_year[attribute] if prior_year else None
        budget_value = budget[attribute] if budget else None
        values.extend([
            _report_value(f"去年{prefix}成本" if prefix != "制造费用" else "去年制造费用", "元/盒", prior_value, prior_reason),
            _report_variance(f"{prefix}同比", current[attribute], prior_value, prior_reason),
            _report_value(f"预算{prefix}成本" if prefix != "制造费用" else "预算制造费用", "元/盒", budget_value, budget_reason),
            _report_variance(f"{prefix}预算偏差", current[attribute], budget_value, budget_reason),
        ])
    for label, key, unit in (
        ("工时", "hours", "h/万盒"),
        ("时薪", "wage", "元/h"),
        ("效率", "efficiency", "盒/人·日"),
    ):
        current_value = labor_current[key] if labor_current else None
        previous_value = labor_previous[key] if labor_previous else None
        values.extend([
            _report_value(f"本月{label}", unit, current_value, labor_reason),
            _report_value(f"上月{label}", unit, previous_value, previous_labor_reason),
            _report_variance(
                f"{label}环比", current_value, previous_value, previous_labor_reason
            ) if current_value is not None else _report_value(
                f"{label}环比", "%", None, labor_reason
            ),
        ])
    return {item["name"]: item for item in values}


def _comparison(name: str, unit: str, current: Decimal | int, previous: Decimal | int | None) -> dict[str, Any]:
    current_decimal = Decimal(current)
    if previous is None:
        return {
            "name": name, "unit": unit, "current": _number(current), "previous": None,
            "delta": None, "change_rate_pct": None, "status": "unavailable",
            "reason": "上一季度暂无数据",
        }
    previous_decimal = Decimal(previous)
    delta = current_decimal - previous_decimal
    rate = None if previous_decimal == 0 else delta / previous_decimal * Decimal("100")
    return {
        "name": name, "unit": unit, "current": _number(current), "previous": _number(previous),
        "delta": _number(delta), "change_rate_pct": _number(rate), "status": "available",
        "reason": None,
    }


def _aggregate_detail(rows: list[Any], name_attribute: str, total_attribute: str) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for row in rows:
        name = getattr(row, name_attribute)
        totals[name] = totals.get(name, Decimal("0")) + getattr(row, total_attribute)
    return totals


def build_quarterly_dashboard_analysis(
    data_dir: str | Path,
    index_dir: str | Path,
    product: str,
    quarter: str,
) -> dict[str, Any]:
    """Build a quantity-weighted quarterly view from validated monthly rows."""

    root = Path(data_dir).resolve()
    report, bundle = load_validated_data(root)
    if report.errors:
        raise AnalysisError("源数据存在阻断错误，季度分析入口已关闭")
    months = _quarter_months(quarter)
    previous_quarter = _previous_quarter(quarter)
    previous_months = _quarter_months(previous_quarter)
    prior_year_quarter = f"{int(quarter[:4]) - 1}{quarter[4:]}"
    prior_year_months = _quarter_months(prior_year_quarter)

    def rows_for(rows: list[Any], selected: list[str], factory: str | None = None) -> list[Any]:
        return [
            row for row in rows
            if row.product == product and row.month in selected
            and (factory is None or row.factory == factory)
        ]

    current_rows = rows_for(bundle.plant1_summary, months)
    if len(current_rows) != 3:
        raise AnalysisError(f"{quarter}缺少完整三个月成本汇总数据，禁止季度分析")
    current = _aggregate_summary(current_rows)
    previous = _aggregate_summary(rows_for(bundle.plant1_summary, previous_months))
    prior_year = _aggregate_summary(rows_for(bundle.plant1_prior_summary, prior_year_months))
    budget = _aggregate_budget(rows_for(bundle.budgets, months))
    labor_current = _aggregate_labor(rows_for(bundle.labor_detail, months))
    labor_previous = _aggregate_labor(rows_for(bundle.labor_detail, previous_months))
    benchmark = _aggregate_summary(rows_for(bundle.plant2_summary, months))
    assert current is not None

    unit_metric = _comparison("单位成本", "元/盒", current["unit_cost"], previous["unit_cost"] if previous else None)
    total_metric = _comparison("总成本", "元", current["total_cost"], previous["total_cost"] if previous else None)
    quantity_metric = _comparison("产量", "盒", current["quantity_boxes"], previous["quantity_boxes"] if previous else None)
    total_delta = Decimal(str(unit_metric["delta"])) if unit_metric["delta"] is not None else None

    cost_structure = []
    waterfall = []
    for name, attribute in COMPONENTS:
        value = Decimal(current[attribute])
        share = None if Decimal(current["unit_cost"]) == 0 else value / Decimal(current["unit_cost"]) * Decimal("100")
        cost_structure.append({"name": name, "unit_cost": _number(value), "share_pct": _number(share), "status": "available", "reason": None})
        delta = value - Decimal(previous[attribute]) if previous else None
        contribution = None if delta is None or total_delta in {None, Decimal("0")} else delta / total_delta * Decimal("100")
        waterfall.append({
            "name": name, "delta_unit_cost": _number(delta), "contribution_pct": _number(contribution),
            "status": "available" if delta is not None else "unavailable",
            "reason": None if delta is not None else "上一季度暂无数据",
        })
    total_cost_contributions = _aggregate_total_cost_contributions(current, previous)
    total_cost_bridge = _aggregate_total_cost_bridge(current, previous)

    current_quantity = Decimal(current["quantity_boxes"])
    previous_quantity = Decimal(previous["quantity_boxes"]) if previous else None
    current_material = _aggregate_detail(rows_for(bundle.material_detail, months), "material_name", "material_total_cost")
    previous_material = _aggregate_detail(rows_for(bundle.material_detail, previous_months), "material_name", "material_total_cost")
    material_drivers = []
    for name, total in current_material.items():
        current_unit = total / current_quantity
        previous_unit = previous_material.get(name) / previous_quantity if previous_quantity and name in previous_material else None
        delta = current_unit - previous_unit if previous_unit is not None else None
        rate = None if previous_unit in {None, Decimal("0")} else delta / previous_unit * Decimal("100")
        contribution = None if delta is None or total_delta in {None, Decimal("0")} else delta / total_delta * Decimal("100")
        material_drivers.append({
            "name": name, "current": _number(current_unit), "previous": _number(previous_unit),
            "delta": _number(delta), "change_rate_pct": _number(rate), "contribution_pct": _number(contribution),
            "status": "available" if previous_unit is not None else "unavailable",
            "reason": None if previous_unit is not None else "上一季度暂无数据",
        })
    material_drivers.sort(key=lambda item: abs(item["delta"] or 0), reverse=True)
    material_source_order: dict[str, int] = {}
    for row in rows_for(bundle.material_detail, months, factory="中药一厂"):
        if row.material_name not in material_source_order:
            material_source_order[row.material_name] = len(material_source_order)
    tree_material_drivers = sorted(
        material_drivers,
        key=lambda item: (material_source_order.get(item["name"], len(material_source_order)), item["name"]),
    )

    current_manufacturing = _aggregate_detail(rows_for(bundle.manufacturing_detail, months), "expense_category", "expense_total")
    previous_manufacturing = _aggregate_detail(rows_for(bundle.manufacturing_detail, previous_months), "expense_category", "expense_total")
    manufacturing_drivers = []
    for name, total in current_manufacturing.items():
        current_unit = total / current_quantity
        previous_unit = previous_manufacturing.get(name) / previous_quantity if previous_quantity and name in previous_manufacturing else None
        delta = current_unit - previous_unit if previous_unit is not None else None
        rate = None if previous_unit in {None, Decimal("0")} else delta / previous_unit * Decimal("100")
        manufacturing_drivers.append({
            "name": name, "current": _number(current_unit), "previous": _number(previous_unit),
            "delta": _number(delta), "change_rate_pct": _number(rate), "status": "available" if previous_unit is not None else "unavailable",
            "reason": None if previous_unit is not None else "上一季度暂无数据",
        })
    manufacturing_drivers.sort(key=lambda item: abs(item["delta"] or 0), reverse=True)

    benchmark_unit = Decimal(benchmark["unit_cost"]) if benchmark else None
    benchmark_rows = []
    for name, attribute in (*COMPONENTS, ("单位成本", "unit_cost")):
        target = Decimal(current[attribute])
        comparison = Decimal(benchmark[attribute]) if benchmark else None
        difference = target - comparison if comparison is not None else None
        difference_rate = None if comparison in {None, Decimal("0")} else difference / comparison * Decimal("100")
        benchmark_rows.append({
            "name": name, "target_value": _number(target), "benchmark_value": _number(comparison),
            "difference": _number(difference), "difference_rate_pct": _number(difference_rate),
            "direction": "favorable" if difference is not None and difference < 0 else "unfavorable" if difference is not None and difference > 0 else "neutral",
            "status": "available" if comparison is not None else "unavailable", "reason": None if comparison is not None else "二厂季度数据缺失",
        })
    unit_benchmark = benchmark_rows[-1]
    comparisons = _quarter_report_metrics(
        current, prior_year, budget, labor_current, labor_previous
    )
    component_kpis = {
        attribute: _comparison(
            component_name,
            "元/盒",
            current[attribute],
            previous[attribute] if previous else None,
        )
        for component_name, attribute in COMPONENTS
    }
    benchmark_tree = {
        "name": product,
        "kind": "product",
        "children": [{
            "name": "单位成本",
            "kind": "total",
            **{key: unit_benchmark[key] for key in (
                "target_value", "benchmark_value", "difference",
                "difference_rate_pct", "direction", "status",
            )},
            "children": [
                {
                    "name": item["name"],
                    "kind": "component",
                    **{key: item[key] for key in (
                        "target_value", "benchmark_value", "difference",
                        "difference_rate_pct", "direction", "status",
                    )},
                    "children": [
                        {
                            "name": detail["name"],
                            "kind": "plant1_material",
                            "target_value": detail["current"],
                            "benchmark_value": None,
                            "difference": None,
                            "difference_rate_pct": None,
                            "status": "unavailable",
                            "reason": "二厂未提供原材料明细；仅展示一厂季度单位消耗成本",
                        }
                        for detail in tree_material_drivers
                    ] if item["name"] == "直接材料" else [],
                }
                for item in benchmark_rows[:-1]
            ],
        }],
    }

    trend = []
    available_quarters = sorted({f"{row.month[:4]}-Q{(int(row.month[5:7]) - 1) // 3 + 1}" for row in bundle.plant1_summary if row.product == product})
    for period in available_quarters:
        if period > quarter:
            continue
        aggregate = _aggregate_summary(rows_for(bundle.plant1_summary, _quarter_months(period)))
        if aggregate:
            trend.append({"month": period, **{key: _number(value) for key, value in aggregate.items()}})

    end_month = months[-1]
    evidence_contract = build_report_contract(root, index_dir, product, end_month)
    if evidence_contract.validation_status != "PASS":
        raise AnalysisError("季度知识证据契约校验失败，分析结果禁止展示")
    direction = "下降" if total_delta is not None and total_delta < 0 else "上升" if total_delta is not None and total_delta > 0 else "持平"
    delta_text = "暂无数据" if total_delta is None else f"{abs(total_delta):.2f}元/盒"
    material_text = "；".join(
        f"{item['name']}季度单位消耗成本变动{item['delta']:+.2f}元/盒"
        for item in material_drivers[:3] if item["delta"] is not None
    ) or "上一季度暂无数据，材料变动暂不可计算"
    quarter_difference_text = "；".join(
        f"{item['name']}一厂较二厂{'低' if (item['difference'] or 0) < 0 else '高' if (item['difference'] or 0) > 0 else '持平'}{abs(item['difference'] or 0):.2f}元/盒"
        for item in [unit_benchmark, *benchmark_rows[:-1]]
    )
    top_quarter_material = material_drivers[0]["name"] if material_drivers else None
    top_quarter_material_delta = (
        Decimal(str(material_drivers[0]["delta"]))
        if material_drivers and material_drivers[0]["delta"] is not None
        else None
    )
    special_event_note = None
    if product == "六味地黄胶囊" and quarter == "2026-Q1":
        special_event_note = (
            "季度内存在胶囊填充机故障及维修记录，但缺少费用归属与分摊依据，"
            "不将该事件直接写成季度成本差异原因。"
        )
    quarter_attribution, governed_recommendations = build_benchmark_guidance(
        product=product,
        period=quarter,
        factory="中药一厂",
        benchmark_factory="中药二厂",
        gaps=[
            BenchmarkGap(
                name=item["name"],
                difference=(
                    Decimal(str(item["difference"]))
                    if item["difference"] is not None
                    else None
                ),
                difference_rate_pct=(
                    Decimal(str(item["difference_rate_pct"]))
                    if item["difference_rate_pct"] is not None
                    else None
                ),
                direction=item["direction"],
            )
            for item in benchmark_rows
        ],
        top_material=top_quarter_material,
        top_material_delta=top_quarter_material_delta,
        special_event_note=special_event_note,
    )
    narratives = {
        "波动告警描述": f"{quarter}单位成本较{previous_quarter}{direction}{delta_text}；仅在上一季度数据完整时计算季度环比。",
        "材料成本归因分析文本": material_text + "。市场行情仅作外部相关性参考，不代表企业采购价。",
        "成本异常排查分析": "季度结果按各月总成本和产量汇总，未采用月度单位成本简单平均；同比、预算和实际人工指标均来自新增结构化数据。",
        "差异结构拆解分析": quarter_difference_text + "。",
        "差异归因分析文本": quarter_attribution,
        "本月亮点": f"已完成{quarter}三个月数据的产量加权汇总与勾稽。",
        "需关注问题": "企业实际采购量价、工艺收率、标准工时和设备费用分摊仍未提供，相关因果和差异不得量化。",
    }
    unavailable = [
        {
            "name": item["name"],
            "display": "暂无数据",
            "status": "unavailable",
            "reason": item["reason"],
        }
        for item in comparisons.values() if item["status"] == "unavailable"
    ]
    recommendations = [
        {
            "sequence": item.sequence,
            "action": item.action,
            "owner": item.owner,
            "priority": item.priority,
            "expected_effect": item.expected_effect,
            "due": item.due,
        }
        for item in governed_recommendations
    ]
    return {
        "meta": {
            "analysis_type": "季度成本分析", "product": product, "month": end_month,
            "period": quarter, "previous_period": previous_quarter, "factory": "中药一厂",
            "benchmark_factory": "中药二厂", "report_number": f"QA-{quarter.replace('-', '')}-{product[:2]}-001",
            "analysis_version": "quarterly-1.2.0", "formula_version": "V1.2",
            "knowledge_index_version": evidence_contract.knowledge_index_version,
            "data_quality_status": report.status, "data_quality_warning_count": len(report.warnings),
        },
        "kpis": {"unit_cost": unit_metric, "total_cost": total_metric, "quantity": quantity_metric, **component_kpis, "factory_benchmark": {
            "target": unit_benchmark["target_value"], "benchmark": _number(benchmark_unit),
            "difference": unit_benchmark["difference"], "difference_rate_pct": unit_benchmark["difference_rate_pct"],
            "direction": unit_benchmark["direction"], "status": unit_benchmark["status"],
        }},
        "comparisons": comparisons,
        "trend": trend,
        "trend_context": build_trend_context(
            (item["month"] for item in trend), "季度成本分析"
        ),
        "cost_structure": cost_structure, "waterfall": waterfall,
        "total_cost_contributions": total_cost_contributions,
        "total_cost_bridge": total_cost_bridge,
        "material_drivers": material_drivers, "manufacturing_drivers": manufacturing_drivers,
        "factory_benchmark": benchmark_rows, "benchmark_tree": benchmark_tree,
        "recommendations": recommendations,
        "alerts": [], "narratives": narratives,
        "evidence": evidence_contract.evidence.model_dump(mode="json"), "unavailable_metrics": unavailable,
        "sources": [
            {"kind": "季度成本汇总", "path": "01_成本明细数据/中药一厂_成本汇总_2026年1-6月.csv", "key": f"{product}|{quarter}"},
            {"kind": "季度对标", "path": "01_成本明细数据/中药二厂_成本汇总_2026年1-6月.csv", "key": f"{product}|{quarter}"},
            {"kind": "去年同期成本汇总", "path": "01_成本明细数据/中药一厂_成本汇总_2025年1-6月.csv", "key": f"{product}|{prior_year_quarter}"},
            {"kind": "季度预算", "path": "01_成本明细数据/中药一厂_预算数据_2026年.csv", "key": f"{product}|{quarter}"},
            {"kind": "季度人工工时", "path": "01_成本明细数据/中药一厂_人工工时明细_2026年1-6月.csv", "key": f"{product}|{quarter}"},
        ],
    }


def build_special_dashboard_analysis(
    data_dir: str | Path,
    index_dir: str | Path,
    product: str,
    month: str,
    topic: str,
) -> dict[str, Any]:
    """Build a bounded special-topic view from a validated monthly analysis."""

    result = build_dashboard_analysis(data_dir, index_dir, product, month)
    result["meta"]["analysis_type"] = "专题分析"
    result["meta"]["topic"] = topic
    result["meta"]["period"] = month
    result["meta"]["report_number"] = result["meta"]["report_number"].replace("CA-", "SA-")
    if topic == "原材料涨价专项":
        material = result.get("kpis", {}).get("direct_material", {})
        material_delta = material.get("delta")
        positive_detail = any(
            (item.get("delta") or 0) > 0
            for item in result.get("material_drivers", [])
        )
        positive_market = any(
            (item.get("price_change_rate_pct") or 0) > 0
            for item in result.get("market_evidence", [])
        )
        if material_delta is None or material_delta <= 0 or not (positive_detail or positive_market):
            raise AnalysisError(
                "当前月份直接材料成本未上涨，不符合“原材料涨价专项”准入条件；"
                "请选择材料成本上升月份，或改用“工厂成本差异专项”。"
            )
        result["narratives"]["本月亮点"] = "专题聚焦材料单位消耗成本与同名市场行情的方向关系。"
        result["narratives"]["需关注问题"] = "市场行情不等于企业实际采购价；缺少采购数量和实际采购价时，不计算价格差与用量差。"
    elif topic == "工厂成本差异专项":
        result["narratives"]["本月亮点"] = "专题聚焦一厂减二厂的单位成本及结构差异，成本越低评价越有利。"
        result["narratives"]["需关注问题"] = "工厂差异是数值对标；缺少工艺、批量和分摊明细时，不把差异直接归因于单一事件。"
    else:
        raise AnalysisError("不支持的专题分析类型")
    return result


def build_selected_dashboard_analysis(
    data_dir: str | Path,
    index_dir: str | Path,
    *,
    analysis_type: str,
    product: str,
    month: str | None = None,
    quarter: str | None = None,
    topic: str | None = None,
) -> dict[str, Any]:
    if analysis_type == "月度成本分析" and month:
        return build_dashboard_analysis(data_dir, index_dir, product, month)
    if analysis_type == "季度成本分析" and quarter:
        return build_quarterly_dashboard_analysis(data_dir, index_dir, product, quarter)
    if analysis_type == "专题分析" and month and topic:
        return build_special_dashboard_analysis(data_dir, index_dir, product, month, topic)
    raise AnalysisError("分析类型与期间参数不匹配")

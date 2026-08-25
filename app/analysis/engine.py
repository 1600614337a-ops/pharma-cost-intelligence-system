"""Deterministic cost calculations governed by calculation specification V1.2."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from app.data_quality import ValidatedDataBundle, load_validated_data
from app.data_quality.models import (
    BudgetRow,
    CostSummaryRow,
    LaborDetailRow,
    ManufacturingDetailRow,
    MaterialDetailRow,
)
from app.data_quality.normalization import (
    industry_unit_cost_per_box,
    normalize_market_prices,
    parse_benchmark_value,
)

from .models import (
    AnalysisRequest,
    AnalysisResult,
    ContributionItem,
    CostStructureItem,
    DetailDriver,
    FactoryBenchmarkItem,
    IndustryBenchmarkItem,
    MarketEvidence,
    MetricComparison,
    ReportMetric,
    SourceReference,
    ThresholdAlert,
    TotalCostBridge,
    TotalCostContributionItem,
    UnavailableMetric,
)


ANALYSIS_VERSION = "1.2.1"
FORMULA_VERSION = "V1.2"
ALERT_THRESHOLD_PCT = Decimal("10")
ZERO = Decimal("0")

SUMMARY_FIELDS = (
    ("产量", "盒", "quantity_boxes"),
    ("直接材料", "元/盒", "direct_material"),
    ("直接人工", "元/盒", "direct_labor"),
    ("制造费用", "元/盒", "manufacturing_overhead"),
    ("单位成本", "元/盒", "unit_cost"),
    ("总成本", "元", "total_cost"),
)

COST_COMPONENTS = (
    ("直接材料", "direct_material"),
    ("直接人工", "direct_labor"),
    ("制造费用", "manufacturing_overhead"),
)

PRODUCT_CATEGORY = {
    "银黄口服液": "口服液类",
    "板蓝根颗粒": "颗粒剂类",
    "六味地黄胶囊": "胶囊剂类",
}

PROHIBITED_CALCULATIONS = (
    ("实际采购价格差", "缺少企业实际采购价"),
    ("实际耗用量差", "缺少实际耗用数量"),
    ("工艺收率差", "缺少投入量、产出量和工艺收率数据"),
    ("人工工时差", "已有实际工时，但缺少标准工时"),
    ("工资率差", "已有实际小时工资，但缺少标准工资率"),
    ("效率差", "已有实际效率，但缺少标准工时或标准效率"),
    ("设备故障维修费单位成本影响", "缺少费用归属和产品或批次分摊依据"),
)


class AnalysisError(RuntimeError):
    """Base error raised when deterministic analysis cannot be completed."""


class DataQualityError(AnalysisError):
    """Raised when source data fails a blocking validation rule."""


class ScenarioNotFoundError(AnalysisError):
    """Raised when a requested unique analysis row does not exist."""


def _previous_month(month: str) -> str:
    try:
        year_text, month_text = month.split("-", maxsplit=1)
        year = int(year_text)
        number = int(month_text)
    except (ValueError, AttributeError) as exc:
        raise AnalysisError("月份必须符合YYYY-MM") from exc
    if len(year_text) != 4 or len(month_text) != 2 or not 1 <= number <= 12:
        raise AnalysisError("月份必须符合YYYY-MM且为有效公历月份")
    if number == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{number - 1:02d}"


def _previous_year_month(month: str) -> str:
    return f"{int(month[:4]) - 1:04d}-{month[5:]}"


def _decimal(value: int | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(value)


def _comparison(
    name: str,
    unit: str,
    current: int | Decimal,
    previous: int | Decimal | None,
) -> MetricComparison:
    current_value = _decimal(current)
    if previous is None:
        return MetricComparison(
            name=name,
            unit=unit,
            current=current_value,
            previous=None,
            delta=None,
            change_rate_pct=None,
            status="unavailable",
            reason="缺少上月数据",
        )
    previous_value = _decimal(previous)
    delta = current_value - previous_value
    if previous_value == ZERO:
        if current_value == ZERO:
            return MetricComparison(
                name=name,
                unit=unit,
                current=current_value,
                previous=previous_value,
                delta=delta,
                change_rate_pct=None,
                status="not_applicable",
                reason="本期与基期均为零",
            )
        return MetricComparison(
            name=name,
            unit=unit,
            current=current_value,
            previous=previous_value,
            delta=delta,
            change_rate_pct=None,
            status="unavailable",
            reason="基期为零，无法计算百分比",
        )
    return MetricComparison(
        name=name,
        unit=unit,
        current=current_value,
        previous=previous_value,
        delta=delta,
        change_rate_pct=delta / previous_value * Decimal("100"),
        status="available",
    )


def _select_summary(
    rows: list[CostSummaryRow], factory: str, product: str, month: str
) -> CostSummaryRow | None:
    matches = [
        row
        for row in rows
        if row.factory == factory and row.product == product and row.month == month
    ]
    if len(matches) > 1:
        raise DataQualityError(f"汇总数据主键不唯一：{factory} | {product} | {month}")
    return matches[0] if matches else None


def _scalar_metric(
    name: str,
    unit: str,
    value: Decimal | int | None,
    *,
    reason: str,
) -> ReportMetric:
    return ReportMetric(
        name=name,
        unit=unit,
        value=value,
        status="available" if value is not None else "unavailable",
        reason=None if value is not None else reason,
    )


def _variance_metric(
    name: str,
    current: Decimal | int | None,
    baseline: Decimal | int | None,
    *,
    reason: str,
) -> ReportMetric:
    if current is None:
        return _scalar_metric(name, "%", None, reason=reason)
    current_value = _decimal(current)
    if baseline is None:
        return _scalar_metric(name, "%", None, reason=reason)
    baseline_value = _decimal(baseline)
    if baseline_value == ZERO:
        return ReportMetric(
            name=name,
            unit="%",
            value=None,
            status="not_applicable" if current_value == ZERO else "unavailable",
            reason="基准值为零，无法计算百分比",
        )
    return _scalar_metric(
        name,
        "%",
        (current_value - baseline_value) / baseline_value * Decimal("100"),
        reason=reason,
    )


def _labor_values(row: LaborDetailRow | None) -> dict[str, Decimal | None]:
    if row is None:
        return {"hours": None, "wage": None, "efficiency": None}
    quantity = Decimal(row.quantity_boxes)
    return {
        "hours": row.total_hours / quantity * Decimal("10000"),
        "wage": row.direct_labor_total / row.total_hours,
        "efficiency": quantity
        / (Decimal(row.production_headcount) * Decimal(row.work_days)),
    }


def _make_report_metrics(
    current: CostSummaryRow,
    prior_year: CostSummaryRow | None,
    budget: BudgetRow | None,
    labor_current: LaborDetailRow | None,
    labor_previous: LaborDetailRow | None,
) -> list[ReportMetric]:
    current_labor = _labor_values(labor_current)
    previous_labor = _labor_values(labor_previous)
    prior_reason = "缺少去年同月成本汇总"
    budget_reason = "缺少本月预算数据"
    labor_reason = "缺少本月人工工时明细"
    previous_labor_reason = "缺少上月人工工时明细"

    metrics = [
        _scalar_metric("上月工时", "h/万盒", previous_labor["hours"], reason=previous_labor_reason),
        _scalar_metric("上月效率", "盒/人·日", previous_labor["efficiency"], reason=previous_labor_reason),
        _scalar_metric("上月时薪", "元/h", previous_labor["wage"], reason=previous_labor_reason),
        _variance_metric("产量同比", current.quantity_boxes, prior_year.quantity_boxes if prior_year else None, reason=prior_reason),
        _variance_metric("产量预算偏差", current.quantity_boxes, budget.budget_quantity_boxes if budget else None, reason=budget_reason),
        _variance_metric("工时环比", current_labor["hours"], previous_labor["hours"], reason=previous_labor_reason),
        _variance_metric("效率环比", current_labor["efficiency"], previous_labor["efficiency"], reason=previous_labor_reason),
        _variance_metric("时薪环比", current_labor["wage"], previous_labor["wage"], reason=previous_labor_reason),
        _variance_metric("单位成本同比", current.unit_cost, prior_year.unit_cost if prior_year else None, reason=prior_reason),
        _variance_metric("单位成本预算偏差", current.unit_cost, budget.budget_unit_cost if budget else None, reason=budget_reason),
        _scalar_metric("去年单位成本", "元/盒", prior_year.unit_cost if prior_year else None, reason=prior_reason),
        _scalar_metric("去年材料成本", "元/盒", prior_year.direct_material if prior_year else None, reason=prior_reason),
        _variance_metric("材料同比", current.direct_material, prior_year.direct_material if prior_year else None, reason=prior_reason),
        _scalar_metric("去年人工成本", "元/盒", prior_year.direct_labor if prior_year else None, reason=prior_reason),
        _variance_metric("人工同比", current.direct_labor, prior_year.direct_labor if prior_year else None, reason=prior_reason),
        _scalar_metric("去年制造费用", "元/盒", prior_year.manufacturing_overhead if prior_year else None, reason=prior_reason),
        _variance_metric("制造费用同比", current.manufacturing_overhead, prior_year.manufacturing_overhead if prior_year else None, reason=prior_reason),
        _scalar_metric("去年同月产量", "盒", prior_year.quantity_boxes if prior_year else None, reason=prior_reason),
        _scalar_metric("去年总成本", "元", prior_year.total_cost if prior_year else None, reason=prior_reason),
        _variance_metric("总成本同比", current.total_cost, prior_year.total_cost if prior_year else None, reason=prior_reason),
        _variance_metric("总成本预算偏差", current.total_cost, budget.budget_total_cost if budget else None, reason=budget_reason),
        _scalar_metric("本月工时", "h/万盒", current_labor["hours"], reason=labor_reason),
        _scalar_metric("本月效率", "盒/人·日", current_labor["efficiency"], reason=labor_reason),
        _scalar_metric("本月时薪", "元/h", current_labor["wage"], reason=labor_reason),
        _scalar_metric("预算产量", "盒", budget.budget_quantity_boxes if budget else None, reason=budget_reason),
        _scalar_metric("预算人工成本", "元/盒", budget.budget_direct_labor if budget else None, reason=budget_reason),
        _scalar_metric("预算制造费用", "元/盒", budget.budget_manufacturing_overhead if budget else None, reason=budget_reason),
        _scalar_metric("预算单位成本", "元/盒", budget.budget_unit_cost if budget else None, reason=budget_reason),
        _scalar_metric("预算总成本", "元", budget.budget_total_cost if budget else None, reason=budget_reason),
        _scalar_metric("预算材料成本", "元/盒", budget.budget_direct_material if budget else None, reason=budget_reason),
        _variance_metric("人工预算偏差", current.direct_labor, budget.budget_direct_labor if budget else None, reason=budget_reason),
        _variance_metric("制造费用预算偏差", current.manufacturing_overhead, budget.budget_manufacturing_overhead if budget else None, reason=budget_reason),
        _variance_metric("材料预算偏差", current.direct_material, budget.budget_direct_material if budget else None, reason=budget_reason),
    ]
    return metrics


def _make_structure(current: CostSummaryRow) -> list[CostStructureItem]:
    result: list[CostStructureItem] = []
    for name, field in COST_COMPONENTS:
        value = getattr(current, field)
        if current.unit_cost == ZERO:
            result.append(
                CostStructureItem(
                    name=name,
                    unit_cost=value,
                    share_pct=None,
                    status="not_applicable",
                    reason="单位成本为零",
                )
            )
        else:
            result.append(
                CostStructureItem(
                    name=name,
                    unit_cost=value,
                    share_pct=value / current.unit_cost * Decimal("100"),
                    status="available",
                )
            )
    return result


def _make_contributions(
    current: CostSummaryRow, previous: CostSummaryRow | None
) -> list[ContributionItem]:
    if previous is None:
        return [
            ContributionItem(
                name=name,
                delta_unit_cost=None,
                contribution_pct=None,
                status="unavailable",
                reason="缺少上月数据",
            )
            for name, _ in COST_COMPONENTS
        ]
    denominator = current.unit_cost - previous.unit_cost
    result: list[ContributionItem] = []
    for name, field in COST_COMPONENTS:
        delta = getattr(current, field) - getattr(previous, field)
        if denominator == ZERO:
            result.append(
                ContributionItem(
                    name=name,
                    delta_unit_cost=delta,
                    contribution_pct=None,
                    status="not_applicable",
                    reason="总单位成本变动额为零",
                )
            )
        else:
            result.append(
                ContributionItem(
                    name=name,
                    delta_unit_cost=delta,
                    contribution_pct=delta / denominator * Decimal("100"),
                    status="available",
                )
            )
    return result


def _make_total_cost_contributions(
    current: CostSummaryRow, previous: CostSummaryRow | None
) -> list[TotalCostContributionItem]:
    current_quantity = Decimal(current.quantity_boxes)
    if previous is None:
        return [
            TotalCostContributionItem(
                name=name,
                current_total_cost=getattr(current, field) * current_quantity,
                previous_total_cost=None,
                delta_total_cost=None,
                contribution_pct=None,
                status="unavailable",
                reason="缺少上月数据",
            )
            for name, field in COST_COMPONENTS
        ]
    previous_quantity = Decimal(previous.quantity_boxes)
    denominator = current.total_cost - previous.total_cost
    result: list[TotalCostContributionItem] = []
    for name, field in COST_COMPONENTS:
        current_total = getattr(current, field) * current_quantity
        previous_total = getattr(previous, field) * previous_quantity
        delta = current_total - previous_total
        if denominator == ZERO:
            result.append(
                TotalCostContributionItem(
                    name=name,
                    current_total_cost=current_total,
                    previous_total_cost=previous_total,
                    delta_total_cost=delta,
                    contribution_pct=None,
                    status="not_applicable",
                    reason="总成本变动额为零",
                )
            )
        else:
            result.append(
                TotalCostContributionItem(
                    name=name,
                    current_total_cost=current_total,
                    previous_total_cost=previous_total,
                    delta_total_cost=delta,
                    contribution_pct=delta / denominator * Decimal("100"),
                    status="available",
                )
            )
    return result


def _detail_drivers(
    current_rows: list[MaterialDetailRow] | list[ManufacturingDetailRow],
    previous_rows: list[MaterialDetailRow] | list[ManufacturingDetailRow],
    key_field: str,
    value_field: str,
    total_unit_cost_delta: Decimal | None,
) -> list[DetailDriver]:
    current_index = {getattr(row, key_field): row for row in current_rows}
    previous_index = {getattr(row, key_field): row for row in previous_rows}
    output: list[DetailDriver] = []
    for name in sorted(set(current_index) | set(previous_index)):
        current_row = current_index.get(name)
        previous_row = previous_index.get(name)
        if current_row is None or previous_row is None:
            output.append(
                DetailDriver(
                    name=name,
                    unit="元/盒",
                    current=(
                        getattr(current_row, value_field) if current_row is not None else None
                    ),
                    previous=(
                        getattr(previous_row, value_field)
                        if previous_row is not None
                        else None
                    ),
                    delta=None,
                    change_rate_pct=None,
                    contribution_pct=None,
                    status="unavailable",
                    reason="本月或上月缺少同名明细，禁止按零补齐",
                )
            )
            continue
        current_value = getattr(current_row, value_field)
        previous_value = getattr(previous_row, value_field)
        delta = current_value - previous_value
        if previous_value == ZERO:
            status = "not_applicable" if current_value == ZERO else "unavailable"
            reason = "本期与基期均为零" if current_value == ZERO else "基期为零，无法计算百分比"
            rate = None
        else:
            status = "available"
            reason = None
            rate = delta / previous_value * Decimal("100")
        contribution = (
            delta / total_unit_cost_delta * Decimal("100")
            if total_unit_cost_delta not in (None, ZERO)
            else None
        )
        output.append(
            DetailDriver(
                name=name,
                unit="元/盒",
                current=current_value,
                previous=previous_value,
                delta=delta,
                change_rate_pct=rate,
                contribution_pct=contribution,
                status=status,
                reason=reason,
            )
        )
    return sorted(
        output,
        key=lambda item: (
            item.delta is None,
            -(abs(item.delta) if item.delta is not None else ZERO),
            item.name,
        ),
    )


def _factory_benchmark(
    target: CostSummaryRow, benchmark: CostSummaryRow | None
) -> list[FactoryBenchmarkItem]:
    if benchmark is None:
        return [
            FactoryBenchmarkItem(
                name=name,
                unit="元/盒",
                target_value=getattr(target, field),
                benchmark_value=None,
                difference=None,
                difference_rate_pct=None,
                status="unavailable",
                direction="descriptive_only",
                reason="缺少同产品、同规格、同月份的对标工厂数据",
            )
            for name, field in COST_COMPONENTS + (("单位成本", "unit_cost"),)
        ]
    result: list[FactoryBenchmarkItem] = []
    for name, field in COST_COMPONENTS + (("单位成本", "unit_cost"),):
        target_value = getattr(target, field)
        benchmark_value = getattr(benchmark, field)
        difference = target_value - benchmark_value
        if benchmark_value == ZERO:
            rate = None
            status = "not_applicable" if target_value == ZERO else "unavailable"
            reason = "对标值为零，无法计算差异率"
        else:
            rate = difference / benchmark_value * Decimal("100")
            status = "available"
            reason = None
        direction = (
            "favorable"
            if difference < ZERO
            else "unfavorable"
            if difference > ZERO
            else "neutral"
        )
        result.append(
            FactoryBenchmarkItem(
                name=name,
                unit="元/盒",
                target_value=target_value,
                benchmark_value=benchmark_value,
                difference=difference,
                difference_rate_pct=rate,
                status=status,
                direction=direction,
                reason=reason,
            )
        )
    return result


def _industry_benchmark(
    bundle: ValidatedDataBundle, current: CostSummaryRow
) -> list[IndustryBenchmarkItem]:
    category = PRODUCT_CATEGORY[current.product]
    structure_values = (
        {
            "材料成本占比": current.direct_material
            / current.unit_cost
            * Decimal("100"),
            "人工成本占比": current.direct_labor
            / current.unit_cost
            * Decimal("100"),
            "制造费用占比": current.manufacturing_overhead
            / current.unit_cost
            * Decimal("100"),
        }
        if current.unit_cost != ZERO
        else {}
    )
    result: list[IndustryBenchmarkItem] = []
    for row in bundle.industry_benchmarks:
        if row.product_category != category:
            continue
        if "单位成本" in row.metric:
            current_value = current.unit_cost
            p25 = industry_unit_cost_per_box(category, row.metric, row.industry_p25)
            p50 = industry_unit_cost_per_box(category, row.metric, row.industry_p50)
            p75 = industry_unit_cost_per_box(category, row.metric, row.industry_p75)
            unit = "元/盒"
        elif row.metric in structure_values:
            current_value = structure_values[row.metric]
            p25_raw, _ = parse_benchmark_value(row.industry_p25)
            p50_raw, _ = parse_benchmark_value(row.industry_p50)
            p75_raw, _ = parse_benchmark_value(row.industry_p75)
            p25, p50, p75 = (
                p25_raw * Decimal("100"),
                p50_raw * Decimal("100"),
                p75_raw * Decimal("100"),
            )
            unit = "%"
        elif row.metric in {"材料成本占比", "人工成本占比", "制造费用占比"}:
            p25_raw, _ = parse_benchmark_value(row.industry_p25)
            p50_raw, _ = parse_benchmark_value(row.industry_p50)
            p75_raw, _ = parse_benchmark_value(row.industry_p75)
            result.append(
                IndustryBenchmarkItem(
                    name=row.metric,
                    unit="%",
                    current_value=None,
                    p25=p25_raw * Decimal("100"),
                    p50=p50_raw * Decimal("100"),
                    p75=p75_raw * Decimal("100"),
                    difference_from_p50=None,
                    status="not_applicable",
                    direction="descriptive_only",
                    reason="单位成本为零，成本结构占比不适用",
                    source_evaluation=row.evaluation,
                )
            )
            continue
        else:
            continue
        difference = current_value - p50
        direction = (
            "favorable"
            if difference < ZERO
            else "unfavorable"
            if difference > ZERO
            else "neutral"
        )
        result.append(
            IndustryBenchmarkItem(
                name=row.metric,
                unit=unit,
                current_value=current_value,
                p25=p25,
                p50=p50,
                p75=p75,
                difference_from_p50=difference,
                status="available",
                direction=direction,
                source_evaluation=row.evaluation,
            )
        )
    return result


def _market_evidence(
    bundle: ValidatedDataBundle,
    material_drivers: list[DetailDriver],
    current_month: str,
    previous_month: str,
) -> list[MarketEvidence]:
    points = normalize_market_prices(bundle.market_prices)
    by_key = {
        (point.material_name, point.grade, point.unit, point.month): point
        for point in points
    }
    rows_by_name: dict[str, list] = {}
    for row in bundle.market_prices:
        rows_by_name.setdefault(row.material_name, []).append(row)
    evidence: list[MarketEvidence] = []
    for driver in material_drivers:
        matches = rows_by_name.get(driver.name, [])
        if driver.delta is None or len(matches) != 1:
            continue
        row = matches[0]
        current = by_key.get((row.material_name, row.grade, row.unit, current_month))
        previous = by_key.get((row.material_name, row.grade, row.unit, previous_month))
        if current is None or previous is None:
            continue
        price_delta = current.price - previous.price
        rate = (
            price_delta / previous.price * Decimal("100")
            if previous.price != ZERO
            else None
        )
        if price_delta == ZERO or driver.delta == ZERO:
            relationship = "no_change"
        elif (price_delta > ZERO) == (driver.delta > ZERO):
            relationship = "same_direction"
        else:
            relationship = "opposite_direction"
        evidence.append(
            MarketEvidence(
                material_name=driver.name,
                grade=row.grade,
                unit=row.unit,
                previous_price=previous.price,
                current_price=current.price,
                price_delta=price_delta,
                price_change_rate_pct=rate,
                material_unit_cost_delta=driver.delta,
                relationship=relationship,
                source=row.source,
            )
        )
    return sorted(evidence, key=lambda item: item.material_name)


def _alerts(
    summary: list[MetricComparison],
    material_drivers: list[DetailDriver],
    manufacturing_drivers: list[DetailDriver],
    factory: str,
    product: str,
    month: str,
) -> list[ThresholdAlert]:
    candidates: list[tuple[str, str, str, Decimal, Decimal, Decimal, Decimal]] = []
    for item in summary:
        if item.name not in {"单位成本", "直接材料", "直接人工", "制造费用"}:
            continue
        if (
            item.status == "available"
            and item.previous is not None
            and item.delta is not None
            and item.change_rate_pct is not None
        ):
            candidates.append(
                (
                    "汇总成本",
                    item.name,
                    item.unit,
                    item.previous,
                    item.current,
                    item.delta,
                    item.change_rate_pct,
                )
            )
    for scope, rows in (
        ("原材料", material_drivers),
        ("制造费用类别", manufacturing_drivers),
    ):
        for item in rows:
            if (
                item.status == "available"
                and item.previous is not None
                and item.current is not None
                and item.delta is not None
                and item.change_rate_pct is not None
            ):
                candidates.append(
                    (
                        scope,
                        item.name,
                        item.unit,
                        item.previous,
                        item.current,
                        item.delta,
                        item.change_rate_pct,
                    )
                )
    result: list[ThresholdAlert] = []
    for scope, name, unit, previous, current, delta, rate in candidates:
        if abs(rate) <= ALERT_THRESHOLD_PCT:
            continue
        result.append(
            ThresholdAlert(
                scope=scope,
                name=name,
                unit=unit,
                previous=previous,
                current=current,
                delta=delta,
                change_rate_pct=rate,
                threshold_pct=ALERT_THRESHOLD_PCT,
                direction="increase" if rate > ZERO else "decrease",
                source_key=f"{factory} | {product} | {month} | {name}",
            )
        )
    return sorted(result, key=lambda item: (-abs(item.change_rate_pct), item.name))


def _bridge(
    current: CostSummaryRow, previous: CostSummaryRow | None
) -> TotalCostBridge:
    if previous is None:
        return TotalCostBridge(
            quantity_effect=None,
            unit_cost_effect=None,
            total_cost_delta=None,
            reconciliation_difference=None,
            status="unavailable",
            reason="缺少上月数据",
        )
    quantity_effect = Decimal(current.quantity_boxes - previous.quantity_boxes) * previous.unit_cost
    unit_cost_effect = Decimal(current.quantity_boxes) * (
        current.unit_cost - previous.unit_cost
    )
    total_delta = current.total_cost - previous.total_cost
    return TotalCostBridge(
        quantity_effect=quantity_effect,
        unit_cost_effect=unit_cost_effect,
        total_cost_delta=total_delta,
        reconciliation_difference=total_delta - quantity_effect - unit_cost_effect,
        status="available",
    )


def analyze_cost(
    data_dir: str | Path,
    product: str,
    month: str,
    factory: str = "中药一厂",
    benchmark_factory: str = "中药二厂",
) -> AnalysisResult:
    """Run a deterministic monthly analysis after the data-quality gate passes."""

    if factory != "中药一厂":
        raise AnalysisError("V1.1仅支持中药一厂作为分析工厂，因为明细数据仅覆盖中药一厂")
    if benchmark_factory != "中药二厂":
        raise AnalysisError("V1.1仅支持中药二厂作为对标工厂")
    previous_month = _previous_month(month)
    prior_year_month = _previous_year_month(month)
    report, bundle = load_validated_data(data_dir)
    if report.status == "FAIL":
        codes = sorted({issue.code for issue in report.errors})
        raise DataQualityError(
            f"数据质量门禁失败（{len(report.errors)}项阻断错误：{', '.join(codes)}）"
        )

    current = _select_summary(bundle.plant1_summary, factory, product, month)
    if current is None:
        raise ScenarioNotFoundError(f"找不到分析场景：{factory} | {product} | {month}")
    previous = _select_summary(
        bundle.plant1_summary, factory, product, previous_month
    )
    prior_year = _select_summary(
        bundle.plant1_prior_summary, factory, product, prior_year_month
    )
    benchmark = _select_summary(
        bundle.plant2_summary, benchmark_factory, product, month
    )
    if benchmark is not None and benchmark.specification != current.specification:
        benchmark = None
    budget_matches = [
        row
        for row in bundle.budgets
        if row.factory == factory and row.product == product and row.month == month
    ]
    labor_current_matches = [
        row
        for row in bundle.labor_detail
        if row.factory == factory and row.product == product and row.month == month
    ]
    labor_previous_matches = [
        row
        for row in bundle.labor_detail
        if row.factory == factory and row.product == product and row.month == previous_month
    ]
    if len(budget_matches) > 1 or len(labor_current_matches) > 1 or len(labor_previous_matches) > 1:
        raise DataQualityError("预算或人工工时数据主键不唯一")
    budget = budget_matches[0] if budget_matches else None
    labor_current = labor_current_matches[0] if labor_current_matches else None
    labor_previous = labor_previous_matches[0] if labor_previous_matches else None
    report_metrics = _make_report_metrics(
        current,
        prior_year,
        budget,
        labor_current,
        labor_previous,
    )

    summary = [
        _comparison(
            name,
            unit,
            getattr(current, field),
            getattr(previous, field) if previous is not None else None,
        )
        for name, unit, field in SUMMARY_FIELDS
    ]
    total_unit_delta = (
        current.unit_cost - previous.unit_cost if previous is not None else None
    )
    material_current = [
        row
        for row in bundle.material_detail
        if row.factory == factory and row.product == product and row.month == month
    ]
    material_previous = [
        row
        for row in bundle.material_detail
        if row.factory == factory
        and row.product == product
        and row.month == previous_month
    ]
    manufacturing_current = [
        row
        for row in bundle.manufacturing_detail
        if row.factory == factory and row.product == product and row.month == month
    ]
    manufacturing_previous = [
        row
        for row in bundle.manufacturing_detail
        if row.factory == factory
        and row.product == product
        and row.month == previous_month
    ]
    material_drivers = _detail_drivers(
        material_current,
        material_previous,
        "material_name",
        "unit_consumption_cost",
        total_unit_delta,
    )
    manufacturing_drivers = _detail_drivers(
        manufacturing_current,
        manufacturing_previous,
        "expense_category",
        "unit_expense",
        total_unit_delta,
    )

    source_keys = {
        "plant1_summary": f"{factory} | {product} | {month}",
        "plant2_summary": f"{benchmark_factory} | {product} | {month}",
        "plant1_prior_summary": f"{factory} | {product} | {prior_year_month}",
        "plant2_prior_summary": f"{benchmark_factory} | {product} | {prior_year_month}",
        "budgets": f"{factory} | {product} | {month}",
        "labor_detail": f"{factory} | {product} | {previous_month},{month}",
        "material_detail": f"{factory} | {product} | {previous_month},{month}",
        "manufacturing_detail": f"{factory} | {product} | {previous_month},{month}",
        "market_prices": ",".join(
            sorted(item.name for item in material_drivers)
        ),
        "industry_benchmarks": PRODUCT_CATEGORY[product],
    }
    sources = [
        SourceReference(
            kind=name,
            path=str(Path(report.source_root) / file_summary.path),
            key=source_keys[name],
        )
        for name, file_summary in report.files.items()
    ]

    try:
        return AnalysisResult(
            analysis_version=ANALYSIS_VERSION,
            formula_version=FORMULA_VERSION,
            request=AnalysisRequest(
                factory=factory,
                benchmark_factory=benchmark_factory,
                product=product,
                month=month,
                previous_month=previous_month,
            ),
            data_quality_status=report.status,
            data_quality_warning_count=len(report.warnings),
            summary=summary,
            cost_structure=_make_structure(current),
            contributions=_make_contributions(current, previous),
            total_cost_contributions=_make_total_cost_contributions(current, previous),
            material_drivers=material_drivers,
            manufacturing_drivers=manufacturing_drivers,
            total_cost_bridge=_bridge(current, previous),
            factory_benchmark=_factory_benchmark(current, benchmark),
            industry_benchmark=_industry_benchmark(bundle, current),
            market_evidence=_market_evidence(
                bundle, material_drivers, month, previous_month
            ),
            alerts=_alerts(
                summary,
                material_drivers,
                manufacturing_drivers,
                factory,
                product,
                month,
            ),
            report_metrics=report_metrics,
            unavailable_metrics=[
                UnavailableMetric(name=item.name, reason=item.reason or "数据不可用")
                for item in report_metrics
                if item.status == "unavailable"
            ],
            prohibited_calculations=[
                UnavailableMetric(name=name, reason=reason)
                for name, reason in PROHIBITED_CALCULATIONS
            ],
            sources=sources,
        )
    except ValidationError as exc:
        raise AnalysisError(f"分析结果模型校验失败：{exc}") from exc

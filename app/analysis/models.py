"""Typed outputs for the deterministic cost-analysis engine."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict


CalculationStatus = Literal["available", "unavailable", "not_applicable"]
Direction = Literal["favorable", "neutral", "unfavorable", "descriptive_only"]


class AnalysisModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnalysisRequest(AnalysisModel):
    factory: str
    benchmark_factory: str
    product: str
    month: str
    previous_month: str


class MetricComparison(AnalysisModel):
    name: str
    unit: str
    current: Decimal
    previous: Decimal | None
    delta: Decimal | None
    change_rate_pct: Decimal | None
    status: CalculationStatus
    reason: str | None = None


class CostStructureItem(AnalysisModel):
    name: str
    unit_cost: Decimal
    share_pct: Decimal | None
    status: CalculationStatus
    reason: str | None = None


class ContributionItem(AnalysisModel):
    name: str
    delta_unit_cost: Decimal | None
    contribution_pct: Decimal | None
    status: CalculationStatus
    reason: str | None = None


class TotalCostContributionItem(AnalysisModel):
    name: str
    current_total_cost: Decimal
    previous_total_cost: Decimal | None
    delta_total_cost: Decimal | None
    contribution_pct: Decimal | None
    status: CalculationStatus
    reason: str | None = None


class DetailDriver(AnalysisModel):
    name: str
    unit: str
    current: Decimal | None
    previous: Decimal | None
    delta: Decimal | None
    change_rate_pct: Decimal | None
    contribution_pct: Decimal | None
    status: CalculationStatus
    reason: str | None = None


class FactoryBenchmarkItem(AnalysisModel):
    name: str
    unit: str
    target_value: Decimal
    benchmark_value: Decimal | None
    difference: Decimal | None
    difference_rate_pct: Decimal | None
    status: CalculationStatus
    direction: Direction
    reason: str | None = None


class IndustryBenchmarkItem(AnalysisModel):
    name: str
    unit: str
    current_value: Decimal | None
    p25: Decimal
    p50: Decimal
    p75: Decimal
    difference_from_p50: Decimal | None
    status: CalculationStatus
    direction: Direction
    reason: str | None = None
    source_evaluation: str


class MarketEvidence(AnalysisModel):
    material_name: str
    grade: str
    unit: str
    previous_price: Decimal
    current_price: Decimal
    price_delta: Decimal
    price_change_rate_pct: Decimal | None
    material_unit_cost_delta: Decimal
    relationship: Literal["same_direction", "opposite_direction", "no_change"]
    causality: Literal["correlation_only"] = "correlation_only"
    source: str


class ThresholdAlert(AnalysisModel):
    scope: str
    name: str
    unit: str
    previous: Decimal
    current: Decimal
    delta: Decimal
    change_rate_pct: Decimal
    threshold_pct: Decimal
    direction: Literal["increase", "decrease"]
    source_key: str


class TotalCostBridge(AnalysisModel):
    quantity_effect: Decimal | None
    unit_cost_effect: Decimal | None
    total_cost_delta: Decimal | None
    reconciliation_difference: Decimal | None
    status: CalculationStatus
    reason: str | None = None


class ReportMetric(AnalysisModel):
    name: str
    unit: str
    value: Decimal | int | None
    status: CalculationStatus
    reason: str | None = None


class UnavailableMetric(AnalysisModel):
    name: str
    display: Literal["暂无数据"] = "暂无数据"
    status: Literal["unavailable"] = "unavailable"
    reason: str


class SourceReference(AnalysisModel):
    kind: str
    path: str
    key: str | None = None


class AnalysisResult(AnalysisModel):
    analysis_version: str
    formula_version: str
    request: AnalysisRequest
    data_quality_status: str
    data_quality_warning_count: int
    summary: list[MetricComparison]
    cost_structure: list[CostStructureItem]
    contributions: list[ContributionItem]
    total_cost_contributions: list[TotalCostContributionItem]
    material_drivers: list[DetailDriver]
    manufacturing_drivers: list[DetailDriver]
    total_cost_bridge: TotalCostBridge
    factory_benchmark: list[FactoryBenchmarkItem]
    industry_benchmark: list[IndustryBenchmarkItem]
    market_evidence: list[MarketEvidence]
    alerts: list[ThresholdAlert]
    report_metrics: list[ReportMetric]
    unavailable_metrics: list[UnavailableMetric]
    prohibited_calculations: list[UnavailableMetric]
    sources: list[SourceReference]

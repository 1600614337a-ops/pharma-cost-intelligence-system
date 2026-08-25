"""Typed report contract joining calculations, citations, and template fields."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReportFieldValue(ReportModel):
    name: str
    value: str
    status: Literal["available", "unavailable", "not_applicable", "generated"]
    source_refs: list[str]
    rule: str


class DynamicTable(ReportModel):
    name: str
    headers: list[str]
    rows: list[list[str]]


class ReportEvidence(ReportModel):
    recipe_citation: str
    process_citation: str
    gmp_citation: str
    industry_citation: str
    market_citation: str | None = None
    factory_benchmark_citation: str | None = None
    equipment_citation: str | None = None
    anomaly_history_citation: str | None = None


class ReportGeneration(ReportModel):
    """Auditable provenance for deterministic or model-assisted wording."""

    mode: Literal["deterministic", "llm"] = "deterministic"
    status: Literal["not_requested", "generated", "fallback"] = "not_requested"
    provider_protocol: Literal["responses", "chat_completions"] | None = None
    model: str | None = None
    request_id: str | None = None
    attempt_count: int = 0
    warnings: list[str] = Field(default_factory=list)


class ReportContract(ReportModel):
    contract_version: str
    analysis_version: str
    formula_version: str
    knowledge_index_version: str
    product: str
    month: str
    analysis_type: Literal["月度成本分析", "季度成本分析", "专题分析"] = "月度成本分析"
    period: str | None = None
    topic: str | None = None
    report_number: str
    generated_date: str
    markdown_template_path: str
    markdown_template_sha256: str
    word_template_path: str
    word_template_sha256: str
    fields: dict[str, ReportFieldValue]
    supplemental_fields: dict[str, ReportFieldValue] = Field(default_factory=dict)
    dynamic_tables: dict[str, DynamicTable]
    evidence: ReportEvidence
    generation: ReportGeneration = Field(default_factory=ReportGeneration)
    validation_status: Literal["PASS", "FAIL"]
    validation_issues: list[str]

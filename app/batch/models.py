"""Typed manifest for isolated multi-scenario report runs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.rpa.models import TaskCandidate


class BatchModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BatchScenario(BatchModel):
    product: str
    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")

    @property
    def key(self) -> str:
        return f"{self.month}|{self.product}"


class BatchItem(BatchModel):
    scenario: BatchScenario
    status: Literal["PENDING", "PASS", "FAIL"]
    report_number: str | None = None
    contract_path: str | None = None
    markdown_path: str | None = None
    word_path: str | None = None
    pdf_path: str | None = None
    candidate_path: str | None = None
    hashes: dict[str, str] = Field(default_factory=dict)
    candidate_count: int = 0
    error_type: str | None = None
    error_message: str | None = None


class BatchManifest(BatchModel):
    manifest_version: str
    run_id: str = Field(pattern=r"^RUN-[A-Z0-9-]+$")
    status: Literal["RUNNING", "PASS", "FAIL"]
    generated_date: str
    created_at: str
    completed_at: str | None = None
    data_root: str
    index_root: str
    output_root: str
    total_scenarios: int
    passed_scenarios: int
    failed_scenarios: int
    items: list[BatchItem]
    aggregate_candidates_path: str | None = None
    aggregate_candidates_sha256: str | None = None


class BatchCandidateBundle(BatchModel):
    bundle_version: str
    run_id: str = Field(pattern=r"^RUN-[A-Z0-9-]+$")
    candidate_count: int = Field(ge=0)
    candidates: list[TaskCandidate]

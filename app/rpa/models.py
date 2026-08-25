"""Typed states for governed RPA task preparation, review, and submission."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RpaModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TaskCandidate(RpaModel):
    candidate_version: str
    candidate_id: str = Field(pattern=r"^CAND-[A-F0-9]{16}$")
    idempotency_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    task_id: str = Field(pattern=r"^TASK-\d{6}-\d{3}$")
    report_number: str
    report_contract_sha256: str = Field(pattern=r"^[A-F0-9]{64}$")
    analysis_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    analysis_type: Literal["月度成本分析", "季度成本分析", "专题分析"] = "月度成本分析"
    analysis_period: str | None = None
    product: str
    source_field: str
    source_refs: list[str]
    task_title: str = Field(min_length=1, max_length=200)
    finding: str = Field(min_length=1, max_length=2000)
    suggestion: str = Field(min_length=1, max_length=2000)
    suggested_priority: Literal["high", "medium", "low"]
    suggested_department: str | None = None
    created_at: datetime
    state: Literal["pending_review"] = "pending_review"
    validation_status: Literal["PASS"] = "PASS"


class TaskGeneration(RpaModel):
    """Auditable provenance for controlled task-candidate wording."""

    mode: Literal["deterministic", "llm"] = "deterministic"
    status: Literal["not_requested", "generated", "fallback"] = "not_requested"
    provider_protocol: Literal["responses", "chat_completions"] | None = None
    model: str | None = None
    request_id: str | None = None
    attempt_count: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)
    controlled_fields: list[str] = Field(
        default_factory=lambda: [
            "task_title",
            "suggestion",
            "suggested_priority",
            "suggested_department",
        ]
    )
    protected_fields: list[str] = Field(
        default_factory=lambda: [
            "task_id",
            "report_number",
            "report_contract_sha256",
            "analysis_month",
            "analysis_type",
            "analysis_period",
            "product",
            "source_field",
            "source_refs",
            "finding",
            "created_at",
            "state",
            "validation_status",
        ]
    )


class TaskCandidateBundle(RpaModel):
    bundle_version: str
    report_number: str
    report_contract_sha256: str = Field(pattern=r"^[A-F0-9]{64}$")
    candidates: list[TaskCandidate]
    generation: TaskGeneration = Field(default_factory=TaskGeneration)


class Assignee(RpaModel):
    name: str = Field(min_length=1, max_length=100)
    department: str = Field(min_length=1, max_length=100)
    role: str | None = Field(default=None, max_length=100)


class RpaSource(RpaModel):
    analysis_type: Literal["月度成本分析", "季度成本分析", "专题分析"]
    analysis_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    product: str
    finding: str


class RpaTaskCreateRequest(RpaModel):
    task_id: str
    task_title: str
    assignee: Assignee
    source: RpaSource
    priority: Literal["high", "medium", "low"]
    deadline: date
    suggestion: str | None = None
    notify_method: Literal["wechat", "email", "sms"]
    created_at: datetime


class ReviewRecord(RpaModel):
    decision: Literal["approved", "rejected"]
    reviewer: str = Field(min_length=1, max_length=100)
    decided_at: datetime
    comment: str | None = Field(default=None, max_length=1000)


class ReviewedTask(RpaModel):
    candidate: TaskCandidate
    review: ReviewRecord
    payload: RpaTaskCreateRequest | None = None

    @model_validator(mode="after")
    def payload_matches_decision(self) -> "ReviewedTask":
        if self.review.decision == "approved" and self.payload is None:
            raise ValueError("approved任务必须包含RPA请求载荷")
        if self.review.decision == "rejected" and self.payload is not None:
            raise ValueError("rejected任务不得包含RPA请求载荷")
        return self


class SubmissionResult(RpaModel):
    task_id: str
    idempotency_key: str
    state: Literal[
        "dry_run",
        "sent",
        "duplicate_local",
        "duplicate_remote",
        "failed",
    ]
    attempt_count: int = Field(ge=0)
    http_status: int | None = None
    remote_status: str | None = None
    message: str
    tracking_url: str | None = None
    request_payload: dict[str, Any]
    response_payload: dict[str, Any] | None = None

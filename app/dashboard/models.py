"""Validated request models for the public dashboard API."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DashboardModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DashboardAnalysisRequest(DashboardModel):
    analysis_type: Literal["月度成本分析", "季度成本分析", "专题分析"] = "月度成本分析"
    product: str = Field(min_length=1, max_length=50)
    month: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    quarter: str | None = Field(default=None, pattern=r"^\d{4}-Q[1-4]$")
    topic: Literal["原材料涨价专项", "工厂成本差异专项"] | None = None

    @model_validator(mode="after")
    def validate_period(self) -> "DashboardAnalysisRequest":
        if self.analysis_type == "月度成本分析" and not self.month:
            raise ValueError("月度成本分析必须选择分析月份")
        if self.analysis_type == "季度成本分析" and not self.quarter:
            raise ValueError("季度成本分析必须选择分析季度")
        if self.analysis_type == "专题分析" and (not self.month or not self.topic):
            raise ValueError("专题分析必须选择分析月份和专题")
        return self


class DashboardReportRequest(DashboardAnalysisRequest):
    use_llm: bool = False


class DashboardWorkflowCreateRequest(DashboardModel):
    report_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    use_llm: bool | None = None


class DashboardWorkflowApproveRequest(DashboardModel):
    candidate_id: str = Field(pattern=r"^CAND-[A-F0-9]{16}$")
    reviewer: str = Field(min_length=1, max_length=100)
    assignee_name: str = Field(min_length=1, max_length=100)
    department: str = Field(min_length=1, max_length=100)
    role: str | None = Field(default=None, max_length=100)
    deadline: date
    priority: Literal["high", "medium", "low"]
    notify_method: Literal["wechat", "email", "sms"] = "wechat"
    comment: str | None = Field(default=None, max_length=1000)
    confirmation: str


class DashboardWorkflowSubmitRequest(DashboardModel):
    confirmation: str

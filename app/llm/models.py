"""Configuration and structured-output models for the controlled LLM layer."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


NARRATIVE_FIELDS = (
    "波动告警描述",
    "材料成本归因分析文本",
    "成本异常排查分析",
    "差异结构拆解分析",
    "差异归因分析文本",
    "本月亮点",
    "需关注问题",
)

ALLOWED_RECOMMENDATION_DEPARTMENTS = (
    "采购部",
    "生产部",
    "财务部",
    "设备部",
    "质量管理部",
    "采购部、生产部",
    "生产部、财务部",
    "设备部、财务部",
    "待业务确认",
)


class LlmModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LlmSettings(LlmModel):
    enabled: bool = False
    provider: Literal["openai", "dashscope"] = "openai"
    base_url: str = "https://api.openai.com/v1"
    api_style: Literal["responses", "chat_completions"] = "responses"
    model: str = "gpt-5.6-sol"
    api_key: SecretStr | None = Field(default=None, repr=False)
    timeout_seconds: float = Field(default=30.0, ge=1.0, le=120.0)
    max_attempts: int = Field(default=3, ge=1, le=5)
    retry_delay_seconds: float = Field(default=0.25, ge=0.0, le=5.0)
    max_output_tokens: int = Field(default=1800, ge=256, le=8000)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("大模型BASE_URL必须是有效HTTP(S)地址")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("远程大模型接口必须使用HTTPS")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_provider_protocol(self) -> "LlmSettings":
        if self.provider == "dashscope" and self.api_style != "chat_completions":
            raise ValueError("通义千问接入必须使用chat_completions协议")
        return self

    @property
    def readiness_issue(self) -> str | None:
        if not self.enabled:
            return "大模型功能未启用"
        if self.api_key is None or not self.api_key.get_secret_value().strip():
            return "未配置COST_LLM_API_KEY"
        return None

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        force_enabled: bool | None = None,
    ) -> "LlmSettings":
        env = environ or os.environ

        def flag(name: str, default: bool) -> bool:
            raw = env.get(name)
            if raw is None:
                return default
            normalized = raw.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
            raise ValueError(f"{name}必须是true/false或1/0")

        enabled = flag("COST_LLM_ENABLED", False) if force_enabled is None else force_enabled
        values: dict[str, object] = {
            "enabled": enabled,
            "provider": env.get("COST_LLM_PROVIDER", cls.model_fields["provider"].default),
            "base_url": env.get("COST_LLM_BASE_URL", cls.model_fields["base_url"].default),
            "api_style": env.get("COST_LLM_API_STYLE", cls.model_fields["api_style"].default),
            "model": env.get("COST_LLM_MODEL", cls.model_fields["model"].default),
            "api_key": env.get("COST_LLM_API_KEY"),
        }
        numeric = {
            "timeout_seconds": ("COST_LLM_TIMEOUT_SECONDS", float),
            "max_attempts": ("COST_LLM_MAX_ATTEMPTS", int),
            "retry_delay_seconds": ("COST_LLM_RETRY_DELAY_SECONDS", float),
            "max_output_tokens": ("COST_LLM_MAX_OUTPUT_TOKENS", int),
        }
        for field_name, (env_name, converter) in numeric.items():
            if env_name in env:
                try:
                    values[field_name] = converter(env[env_name])
                except ValueError as exc:
                    raise ValueError(f"{env_name}不是有效数值") from exc
        return cls.model_validate(values)


class RecommendationDraft(LlmModel):
    """Non-executable recommendation wording returned with a report narrative."""

    sequence: str = Field(pattern=r"^[1-9]\d*$")
    action: str = Field(min_length=1, max_length=300)
    owner: Literal[
        "采购部",
        "生产部",
        "财务部",
        "设备部",
        "质量管理部",
        "采购部、生产部",
        "生产部、财务部",
        "设备部、财务部",
        "待业务确认",
    ]
    priority: Literal["高", "中", "低"]
    expected_effect: str = Field(min_length=1, max_length=300)
    due: Literal["待业务审批"] = "待业务审批"


class NarrativeDraft(BaseModel):
    """The only report fields a model is allowed to rewrite."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    fluctuation_alert: str = Field(alias="波动告警描述", min_length=1, max_length=1200)
    material_attribution: str = Field(alias="材料成本归因分析文本", min_length=1, max_length=1200)
    anomaly_investigation: str = Field(alias="成本异常排查分析", min_length=1, max_length=1200)
    difference_breakdown: str = Field(alias="差异结构拆解分析", min_length=1, max_length=1200)
    difference_attribution: str = Field(alias="差异归因分析文本", min_length=1, max_length=1200)
    monthly_highlights: str = Field(alias="本月亮点", min_length=1, max_length=1200)
    attention_items: str = Field(alias="需关注问题", min_length=1, max_length=1200)
    recommendations: list[RecommendationDraft] | None = Field(
        default=None,
        alias="改进建议列表",
        min_length=1,
        max_length=4,
    )

    def by_report_field(self) -> dict[str, str]:
        dumped = self.model_dump(by_alias=True)
        return {name: str(dumped[name]) for name in NARRATIVE_FIELDS}


class TaskCandidateDraft(LlmModel):
    """The small, non-executable subset a model may propose for a task candidate."""

    candidate_id: str = Field(pattern=r"^CAND-[A-F0-9]{16}$")
    task_title: str = Field(min_length=1, max_length=200)
    suggestion: str = Field(min_length=1, max_length=2000)
    suggested_priority: Literal["high", "medium", "low"]
    suggested_department: str | None = Field(default=None, max_length=100)


class TaskCandidateDraftBundle(LlmModel):
    candidates: list[TaskCandidateDraft] = Field(min_length=1, max_length=20)


class LlmResponse(LlmModel):
    draft: NarrativeDraft
    request_id: str | None = None
    attempt_count: int


class TaskLlmResponse(LlmModel):
    draft: TaskCandidateDraftBundle
    request_id: str | None = None
    attempt_count: int

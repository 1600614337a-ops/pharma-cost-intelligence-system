"""Minimal OpenAI-compatible Responses/Chat Completions client."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx2
from pydantic import BaseModel, ValidationError

from .models import (
    LlmResponse,
    LlmSettings,
    NarrativeDraft,
    TaskCandidateDraftBundle,
    TaskLlmResponse,
)


class LlmCallError(RuntimeError):
    """A sanitized model-call failure safe to expose in generation metadata."""

    def __init__(self, message: str, *, attempt_count: int = 0) -> None:
        super().__init__(message)
        self.attempt_count = attempt_count


SYSTEM_INSTRUCTIONS = """你是制药企业成本分析报告的受控文字编辑器。
只能根据输入的确定性报告契约、差异结构、确定性建议候选和知识引用，改写指定的七个文字字段及改进建议列表。
不得重新计算、修改、补造或外推任何数字；不得把市场行情表述为企业采购因果；不得把背景事件写成没有数据支持的成本原因。
材料成本归因分析文本必须保留直接材料对总成本变动的贡献度和可执行核查建议。
差异归因分析文本必须分为2至4个自然段，保留总体差异、原因研判和证据边界；不得把待核查方向写成已确认因果。
改进建议必须保持确定性候选的数量、顺序和事实边界，并针对主导差异要素及证据缺口改写；不得降低优先级。
建议只能作为待人工审批的RPA候选，不得声称已经下发、执行或完成；截止状态必须保持“待业务审批”。
缺失数据必须保持为“暂无数据”，引用不得改写。只返回符合给定JSON Schema的对象。"""

TASK_SYSTEM_INSTRUCTIONS = """你是制药企业成本整改任务候选的受控编辑器。
只能补充或改写输入候选中的任务标题、整改建议、建议优先级和建议责任部门。
候选ID必须原样返回，候选数量和顺序不得改变。不得输出责任人、截止日期、审批意见、通知方式、提交状态或RPA执行结果。
不得改写已验证发现、来源引用、报告标识、分析对象和任何事实数值；不得新增输入证据中不存在的数字；不得把市场行情或背景事件写成已确认的采购或成本因果。
责任部门只能从输入的允许部门中选择；优先级不得低于确定性基线。所有输出仍是“待人工审批”的非执行候选。只返回符合给定JSON Schema的对象。"""


class OpenAICompatibleClient:
    def __init__(
        self,
        settings: LlmSettings,
        *,
        client: httpx2.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self._client = client or httpx2.Client(timeout=settings.timeout_seconds)
        self._owns_client = client is None
        self._sleeper = sleeper

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "OpenAICompatibleClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _structured_schema(model_type: type[BaseModel] = NarrativeDraft) -> dict[str, Any]:
        return model_type.model_json_schema(by_alias=True)

    def _request_structured(
        self,
        prompt: str,
        *,
        model_type: type[BaseModel],
        schema_name: str,
        system_instructions: str,
    ) -> tuple[str, dict[str, Any]]:
        schema = self._structured_schema(model_type)
        if self.settings.api_style == "responses":
            endpoint = f"{self.settings.base_url}/responses"
            payload = {
                "model": self.settings.model,
                "instructions": system_instructions,
                "input": prompt,
                "max_output_tokens": self.settings.max_output_tokens,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "strict": True,
                        "schema": schema,
                    }
                },
            }
        else:
            endpoint = f"{self.settings.base_url}/chat/completions"
            if self.settings.provider == "dashscope":
                schema_prompt = (
                    prompt
                    + "\n必须只输出一个JSON对象，并严格遵循以下JSON Schema；不得输出Markdown代码围栏或额外说明：\n"
                    + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
                )
                payload = {
                    "model": self.settings.model,
                    "messages": [
                        {"role": "system", "content": system_instructions},
                        {"role": "user", "content": schema_prompt},
                    ],
                    "response_format": {"type": "json_object"},
                    "enable_thinking": False,
                    "preserve_thinking": False,
                }
            else:
                payload = {
                    "model": self.settings.model,
                    "messages": [
                        {"role": "system", "content": system_instructions},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": self.settings.max_output_tokens,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema_name,
                            "strict": True,
                            "schema": schema,
                        },
                    },
                }
        return endpoint, payload

    def _request(self, prompt: str) -> tuple[str, dict[str, Any]]:
        return self._request_structured(
            prompt,
            model_type=NarrativeDraft,
            schema_name="cost_narrative",
            system_instructions=SYSTEM_INSTRUCTIONS,
        )

    @staticmethod
    def _extract_responses_text(payload: dict[str, Any]) -> str:
        direct = payload.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct
        for output in payload.get("output", []):
            if not isinstance(output, dict) or output.get("type") != "message":
                continue
            for content in output.get("content", []):
                if not isinstance(content, dict):
                    continue
                if content.get("type") == "refusal":
                    raise LlmCallError("模型拒绝生成")
                if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    return content["text"]
        raise LlmCallError("Responses响应缺少output_text")

    @staticmethod
    def _extract_chat_text(payload: dict[str, Any]) -> str:
        try:
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmCallError("Chat Completions响应结构无效") from exc
        if message.get("refusal"):
            raise LlmCallError("模型拒绝生成")
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            parts = [item.get("text", "") for item in content if isinstance(item, dict)]
            joined = "".join(parts)
            if joined.strip():
                return joined
        raise LlmCallError("Chat Completions响应缺少content")

    def _generate_structured(
        self,
        prompt: str,
        *,
        model_type: type[BaseModel],
        schema_name: str,
        system_instructions: str,
    ) -> tuple[BaseModel, str | None, int]:
        if self.settings.readiness_issue:
            raise LlmCallError(self.settings.readiness_issue)
        endpoint, request_payload = self._request_structured(
            prompt,
            model_type=model_type,
            schema_name=schema_name,
            system_instructions=system_instructions,
        )
        headers = {
            "Authorization": f"Bearer {self.settings.api_key.get_secret_value()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "cost-intelligence-system/1.0",
        }
        last_error = "大模型调用失败"
        for attempt in range(1, self.settings.max_attempts + 1):
            retryable = False
            try:
                response = self._client.post(endpoint, json=request_payload, headers=headers)
                if not 200 <= response.status_code < 300:
                    last_error = f"大模型接口返回HTTP {response.status_code}"
                    retryable = response.status_code in {408, 409, 429} or response.status_code >= 500
                    if not retryable:
                        raise LlmCallError(last_error)
                else:
                    try:
                        payload = response.json()
                    except (json.JSONDecodeError, ValueError) as exc:
                        raise LlmCallError("大模型响应不是有效JSON") from exc
                    if not isinstance(payload, dict):
                        raise LlmCallError("大模型响应根节点必须是对象")
                    text = (
                        self._extract_responses_text(payload)
                        if self.settings.api_style == "responses"
                        else self._extract_chat_text(payload)
                    )
                    try:
                        draft = model_type.model_validate_json(text)
                    except ValidationError as exc:
                        raise LlmCallError("大模型结构化输出未通过Schema校验") from exc
                    request_id = response.headers.get("x-request-id") or payload.get("id")
                    return draft, request_id, attempt
            except (httpx2.TimeoutException, httpx2.TransportError) as exc:
                last_error = f"大模型网络请求失败：{type(exc).__name__}"
                retryable = True
            except LlmCallError as exc:
                if exc.attempt_count == 0:
                    exc.attempt_count = attempt
                raise
            if not retryable or attempt >= self.settings.max_attempts:
                break
            delay = self.settings.retry_delay_seconds * attempt
            if delay:
                self._sleeper(delay)
        raise LlmCallError(last_error, attempt_count=self.settings.max_attempts)

    def generate(self, prompt: str) -> LlmResponse:
        draft, request_id, attempt_count = self._generate_structured(
            prompt,
            model_type=NarrativeDraft,
            schema_name="cost_narrative",
            system_instructions=SYSTEM_INSTRUCTIONS,
        )
        return LlmResponse(
            draft=draft,
            request_id=request_id,
            attempt_count=attempt_count,
        )

    def generate_task_candidates(self, prompt: str) -> TaskLlmResponse:
        draft, request_id, attempt_count = self._generate_structured(
            prompt,
            model_type=TaskCandidateDraftBundle,
            schema_name="cost_remediation_candidates",
            system_instructions=TASK_SYSTEM_INSTRUCTIONS,
        )
        return TaskLlmResponse(
            draft=draft,
            request_id=request_id,
            attempt_count=attempt_count,
        )

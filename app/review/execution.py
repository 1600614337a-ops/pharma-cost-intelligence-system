"""Test-only governed submission with endpoint restrictions and receipt verification."""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from app.rpa import RpaClient, RpaSubmissionError
from app.rpa.client import TransportResponse, UrllibTransport
from app.rpa.models import ReviewedTask, SubmissionResult


class ExecutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TestSubmissionSettings(ExecutionModel):
    enabled: bool = False
    base_url: str = "http://127.0.0.1:8090"
    api_token: SecretStr | None = None
    allowed_hosts: tuple[str, ...] = ()
    timeout_seconds: float = Field(default=10, gt=0, le=60)
    max_attempts: int = Field(default=3, ge=1, le=5)
    retry_delay_seconds: float = Field(default=0.2, ge=0, le=5)
    rate_limit_per_minute: int = Field(default=5, ge=1, le=60)
    lease_seconds: int = Field(default=120, ge=30, le=600)

    @model_validator(mode="after")
    def restrict_endpoint(self) -> "TestSubmissionSettings":
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("测试RPA地址只允许http或https")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("测试RPA地址不得包含用户信息、查询参数或片段")
        if parsed.path not in {"", "/"}:
            raise ValueError("测试RPA地址只能配置服务根地址")
        hostname = (parsed.hostname or "").lower()
        normalized_hosts: list[str] = []
        for allowed_host in self.allowed_hosts:
            normalized = allowed_host.strip().lower()
            if not re.fullmatch(r"[a-z0-9.-]+", normalized) or "*" in normalized:
                raise ValueError("测试RPA白名单只能包含精确DNS主机名，不允许通配符")
            normalized_hosts.append(normalized)
        self.allowed_hosts = tuple(dict.fromkeys(normalized_hosts))
        is_loopback = hostname == "localhost"
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                is_loopback = False
        if not is_loopback and hostname not in self.allowed_hosts:
            raise ValueError("测试RPA地址既不是回环地址，也不在显式主机白名单中")
        if not is_loopback and parsed.scheme != "https":
            raise ValueError("非回环测试RPA地址必须使用https")
        if parsed.port is None:
            raise ValueError("测试RPA地址必须显式指定端口")
        self.base_url = self.base_url.rstrip("/")
        return self

    @property
    def safe_origin(self) -> str:
        parsed = urlsplit(self.base_url)
        hostname = parsed.hostname or ""
        display_host = f"[{hostname}]" if ":" in hostname else hostname
        return f"{parsed.scheme}://{display_host}:{parsed.port}"


class ReceiptTransport(Protocol):
    def post_json(self, url: str, payload: dict[str, Any], timeout: float) -> TransportResponse: ...
    def get_json(self, url: str, timeout: float) -> TransportResponse: ...


class ReceiptCheck(ExecutionModel):
    status: Literal["verified", "not_found", "unavailable", "mismatch"]
    message: str
    http_status: int | None = None
    payload: dict[str, Any] | None = None


class TestExecutionOutcome(ExecutionModel):
    state: Literal["succeeded", "failed", "reconcile_required"]
    message: str
    result: SubmissionResult | None = None
    receipt: ReceiptCheck
    safe_to_retry: bool = False


class TestExecutionClaim(ExecutionModel):
    execution_id: str = Field(pattern=r"^EXEC-[A-F0-9]{16}$")
    candidate_id: str
    mode: Literal["post", "reconcile"]
    task: ReviewedTask
    expected_completion_version: int = Field(ge=1)


class GovernedTestExecutor:
    def __init__(
        self,
        settings: TestSubmissionSettings,
        *,
        transport: ReceiptTransport | None = None,
    ):
        self.settings = settings
        if transport is None:
            headers: dict[str, str] = {}
            if settings.api_token is not None:
                headers["Authorization"] = f"Bearer {settings.api_token.get_secret_value()}"
            transport = UrllibTransport(headers=headers)
        self.transport = transport

    def _verify_receipt(self, task: ReviewedTask) -> ReceiptCheck:
        assert task.payload is not None
        url = f"{self.settings.base_url}/api/rpa/tasks/{quote(task.payload.task_id, safe='')}"
        try:
            response = self.transport.get_json(url, self.settings.timeout_seconds)
        except RpaSubmissionError as exc:
            return ReceiptCheck(status="unavailable", message=str(exc))
        if response.status_code == 404:
            return ReceiptCheck(
                status="not_found",
                message="远端明确返回任务不存在",
                http_status=404,
                payload=response.payload,
            )
        if response.status_code != 200 or response.payload.get("code") != 200:
            return ReceiptCheck(
                status="unavailable",
                message=str(response.payload.get("message") or f"HTTP {response.status_code}"),
                http_status=response.status_code,
                payload=response.payload,
            )
        data = response.payload.get("data")
        request = task.payload.model_dump(mode="json", exclude_none=True)
        if not isinstance(data, dict):
            return ReceiptCheck(
                status="mismatch",
                message="远端回执缺少data对象",
                http_status=response.status_code,
                payload=response.payload,
            )
        assignee = data.get("assignee") if isinstance(data.get("assignee"), dict) else {}
        source = data.get("source") if isinstance(data.get("source"), dict) else {}
        comparisons = {
            "task_id": (data.get("task_id"), request["task_id"]),
            "task_title": (data.get("task_title"), request["task_title"]),
            "priority": (data.get("priority"), request["priority"]),
            "deadline": (data.get("deadline"), request["deadline"]),
            "assignee.name": (assignee.get("name"), request["assignee"]["name"]),
            "assignee.department": (assignee.get("department"), request["assignee"]["department"]),
            "source.analysis_month": (source.get("analysis_month"), request["source"]["analysis_month"]),
            "source.product": (source.get("product"), request["source"]["product"]),
            "source.finding": (source.get("finding"), request["source"]["finding"]),
        }
        mismatched = [name for name, pair in comparisons.items() if pair[0] != pair[1]]
        allowed_statuses = {"sent", "received", "confirmed", "in_progress", "completed", "overdue"}
        if data.get("status") not in allowed_statuses:
            mismatched.append("status")
        if mismatched:
            return ReceiptCheck(
                status="mismatch",
                message=f"远端回执字段不一致：{','.join(mismatched)}",
                http_status=response.status_code,
                payload=response.payload,
            )
        return ReceiptCheck(
            status="verified",
            message=f"远端回执已核验，状态为{data['status']}",
            http_status=response.status_code,
            payload=response.payload,
        )

    def execute(self, claim: TestExecutionClaim) -> TestExecutionOutcome:
        if not self.settings.enabled:
            raise RpaSubmissionError("测试提交功能未启用")
        if claim.mode == "reconcile":
            receipt = self._verify_receipt(claim.task)
            if receipt.status == "verified":
                return TestExecutionOutcome(
                    state="succeeded",
                    message="通过远端回执完成对账",
                    receipt=receipt,
                )
            if receipt.status == "not_found":
                return TestExecutionOutcome(
                    state="failed",
                    message="远端确认任务不存在，可由授权人重新发起测试提交",
                    receipt=receipt,
                    safe_to_retry=True,
                )
            return TestExecutionOutcome(
                state="reconcile_required",
                message="远端状态仍无法确定，需要继续对账，禁止重复POST",
                receipt=receipt,
            )

        client = RpaClient(
            base_url=self.settings.base_url,
            mode="execute",
            transport=self.transport,
            timeout_seconds=self.settings.timeout_seconds,
            max_attempts=self.settings.max_attempts,
            retry_delay_seconds=self.settings.retry_delay_seconds,
        )
        result = client.submit(claim.task)
        if result.state in {"sent", "duplicate_remote"}:
            receipt = self._verify_receipt(claim.task)
            if receipt.status == "verified":
                return TestExecutionOutcome(
                    state="succeeded",
                    message="测试任务已提交且远端回执一致",
                    result=result,
                    receipt=receipt,
                )
            return TestExecutionOutcome(
                state="reconcile_required",
                message="POST结果可能已成功，但回执未通过；禁止重复POST",
                result=result,
                receipt=receipt,
            )
        ambiguous = result.http_status is None or result.http_status == 429 or result.http_status >= 500
        if ambiguous:
            return TestExecutionOutcome(
                state="reconcile_required",
                message="提交结果不确定，需要先查询远端回执",
                result=result,
                receipt=ReceiptCheck(status="unavailable", message=result.message, http_status=result.http_status),
            )
        return TestExecutionOutcome(
            state="failed",
            message="远端明确拒绝测试任务，可修正后重试",
            result=result,
            receipt=ReceiptCheck(status="not_found", message=result.message, http_status=result.http_status),
            safe_to_retry=True,
        )

"""Dry-run-first RPA client with persistent idempotency and bounded retries."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict

from .models import ReviewedTask, SubmissionResult


class RpaSubmissionError(RuntimeError):
    pass


class TransportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status_code: int
    payload: dict[str, Any]


class RpaTransport(Protocol):
    def post_json(self, url: str, payload: dict[str, Any], timeout: float) -> TransportResponse: ...


class UrllibTransport:
    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = dict(headers or {})

    def _request(
        self,
        url: str,
        *,
        method: str,
        timeout: float,
        payload: dict[str, Any] | None = None,
    ) -> TransportResponse:
        headers = {"Accept": "application/json", **self.headers}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            url,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
                try:
                    payload_body = json.loads(body)
                except json.JSONDecodeError as exc:
                    raise RpaSubmissionError("RPA响应不是有效JSON") from exc
                if not isinstance(payload_body, dict):
                    raise RpaSubmissionError("RPA响应根节点必须是对象")
                return TransportResponse(status_code=response.status, payload=payload_body)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                payload_body = json.loads(body)
            except json.JSONDecodeError:
                payload_body = {"message": body or str(exc)}
            return TransportResponse(status_code=exc.code, payload=payload_body)
        except (URLError, TimeoutError, OSError) as exc:
            raise RpaSubmissionError(f"RPA网络请求失败：{exc}") from exc

    def post_json(self, url: str, payload: dict[str, Any], timeout: float) -> TransportResponse:
        return self._request(url, method="POST", timeout=timeout, payload=payload)

    def get_json(self, url: str, timeout: float) -> TransportResponse:
        return self._request(url, method="GET", timeout=timeout)


class JsonSubmissionLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RpaSubmissionError(f"幂等台账无法读取：{self.path}") from exc
        if not isinstance(payload, dict):
            raise RpaSubmissionError("幂等台账根节点必须是对象")
        return payload

    def get(self, key: str) -> dict[str, Any] | None:
        return self._read().get(key)

    def record(self, key: str, result: SubmissionResult) -> None:
        payload = self._read()
        payload[key] = result.model_dump(mode="json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(self.path)


class RpaClient:
    def __init__(
        self,
        *,
        base_url: str = "http://localhost:8090",
        mode: Literal["dry_run", "execute"] = "dry_run",
        transport: RpaTransport | None = None,
        ledger: JsonSubmissionLedger | None = None,
        timeout_seconds: float = 30,
        max_attempts: int = 3,
        retry_delay_seconds: float = 0.5,
    ):
        if max_attempts < 1 or max_attempts > 5:
            raise ValueError("max_attempts必须在1到5之间")
        self.base_url = base_url.rstrip("/")
        self.mode = mode
        self.transport = transport or UrllibTransport()
        self.ledger = ledger
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds

    def submit(self, task: ReviewedTask) -> SubmissionResult:
        if task.review.decision != "approved" or task.payload is None:
            raise RpaSubmissionError("只有人工审批通过且载荷完整的任务才能提交")
        request_payload = task.payload.model_dump(mode="json", exclude_none=True)
        key = task.candidate.idempotency_key
        if self.mode == "dry_run":
            return SubmissionResult(
                task_id=task.payload.task_id,
                idempotency_key=key,
                state="dry_run",
                attempt_count=0,
                message="dry-run：未调用外部RPA接口",
                request_payload=request_payload,
            )
        if self.ledger and self.ledger.get(key):
            return SubmissionResult(
                task_id=task.payload.task_id,
                idempotency_key=key,
                state="duplicate_local",
                attempt_count=0,
                message="本地幂等台账已存在成功记录，未重复提交",
                request_payload=request_payload,
            )

        last_response: TransportResponse | None = None
        last_error: str | None = None
        attempt_count = 0
        for attempt in range(1, self.max_attempts + 1):
            attempt_count = attempt
            try:
                response = self.transport.post_json(
                    f"{self.base_url}/api/rpa/tasks",
                    request_payload,
                    self.timeout_seconds,
                )
                last_response = response
                message = str(
                    response.payload.get("message")
                    or response.payload.get("detail")
                    or ""
                )
                if 200 <= response.status_code < 300 and response.payload.get("code") == 200:
                    data = response.payload.get("data") or {}
                    result = SubmissionResult(
                        task_id=task.payload.task_id,
                        idempotency_key=key,
                        state="sent",
                        attempt_count=attempt,
                        http_status=response.status_code,
                        remote_status=data.get("status"),
                        message=message or "任务已提交",
                        tracking_url=data.get("tracking_url"),
                        request_payload=request_payload,
                        response_payload=response.payload,
                    )
                    if self.ledger:
                        self.ledger.record(key, result)
                    return result
                if response.status_code == 400 and "已存在" in message:
                    result = SubmissionResult(
                        task_id=task.payload.task_id,
                        idempotency_key=key,
                        state="duplicate_remote",
                        attempt_count=attempt,
                        http_status=response.status_code,
                        message=message,
                        request_payload=request_payload,
                        response_payload=response.payload,
                    )
                    if self.ledger:
                        self.ledger.record(key, result)
                    return result
                last_error = message or f"HTTP {response.status_code}"
                retryable = response.status_code == 429 or response.status_code >= 500
                if not retryable:
                    break
            except RpaSubmissionError as exc:
                last_error = str(exc)
            if attempt < self.max_attempts and self.retry_delay_seconds > 0:
                time.sleep(self.retry_delay_seconds * attempt)

        return SubmissionResult(
            task_id=task.payload.task_id,
            idempotency_key=key,
            state="failed",
            attempt_count=attempt_count,
            http_status=last_response.status_code if last_response else None,
            message=last_error or "RPA提交失败",
            request_payload=request_payload,
            response_payload=last_response.payload if last_response else None,
        )

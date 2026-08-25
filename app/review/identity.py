"""Authentication adapters for local tokens and signed enterprise identity proxies."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Protocol

from fastapi import Request

from .auth import AuthManager, AuthenticationError, Principal, ROLE_PERMISSIONS


_USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9._@-]{1,100}$")
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{16,128}$")


class IdentityProvider(Protocol):
    async def authenticate(self, request: Request) -> Principal: ...


class LocalTokenIdentityProvider:
    def __init__(self, manager: AuthManager):
        self.manager = manager

    async def authenticate(self, request: Request) -> Principal:
        return self.manager.authenticate(request.headers.get("X-Review-Token", ""))


class ReplayGuard:
    """Bounded, process-local replay protection for a single enterprise-test instance."""

    def __init__(self, *, max_entries: int = 10_000):
        self.max_entries = max_entries
        self._entries: OrderedDict[str, int] = OrderedDict()
        self._lock = threading.Lock()

    def claim(self, request_id: str, *, expires_at: int, now: int) -> bool:
        with self._lock:
            while self._entries:
                first_key = next(iter(self._entries))
                if self._entries[first_key] >= now:
                    break
                self._entries.popitem(last=False)
            if request_id in self._entries:
                return False
            self._entries[request_id] = expires_at
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
            return True


def _decode_display_name(value: str) -> str:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise AuthenticationError("企业身份显示名编码无效") from exc
    if not decoded or len(decoded) > 100 or any(ord(char) < 32 for char in decoded):
        raise AuthenticationError("企业身份显示名无效")
    return decoded


def proxy_signature(
    *,
    secret: str,
    method: str,
    path_and_query: str,
    user_id: str,
    display_name_b64: str,
    role: str,
    timestamp: str,
    request_id: str,
    content_sha256: str,
) -> str:
    material = "\n".join(
        (
            method.upper(),
            path_and_query,
            user_id,
            display_name_b64,
            role,
            timestamp,
            request_id,
            content_sha256,
        )
    )
    return hmac.new(secret.encode("utf-8"), material.encode("utf-8"), hashlib.sha256).hexdigest()


class SignedProxyIdentityProvider:
    """Verify short-lived, body-bound identity assertions from an SSO gateway."""

    def __init__(
        self,
        *,
        signing_secret: str,
        trusted_proxy_networks: tuple[str, ...],
        max_age_seconds: int = 60,
        clock: Callable[[], float] = time.time,
        replay_guard: ReplayGuard | None = None,
    ):
        if len(signing_secret) < 32:
            raise ValueError("身份签名密钥至少需要32个字符")
        try:
            self.trusted_proxy_networks = tuple(
                ipaddress.ip_network(item, strict=False) for item in trusted_proxy_networks
            )
        except ValueError as exc:
            raise ValueError("可信代理网段配置无效") from exc
        if not self.trusted_proxy_networks:
            raise ValueError("至少需要一个可信代理网段")
        self.signing_secret = signing_secret
        self.max_age_seconds = max_age_seconds
        self.clock = clock
        self.replay_guard = replay_guard or ReplayGuard()

    def _trusted_client(self, request: Request) -> bool:
        if request.client is None:
            return False
        try:
            address = ipaddress.ip_address(request.client.host)
        except ValueError:
            return False
        return any(address in network for network in self.trusted_proxy_networks)

    async def authenticate(self, request: Request) -> Principal:
        if not self._trusted_client(request):
            raise AuthenticationError("请求未经过可信企业身份代理")
        headers = request.headers
        user_id = headers.get("X-Review-User-Id", "")
        display_name_b64 = headers.get("X-Review-Display-Name-B64", "")
        role = headers.get("X-Review-Role", "")
        timestamp = headers.get("X-Review-Timestamp", "")
        request_id = headers.get("X-Review-Request-Id", "")
        content_sha256 = headers.get("X-Review-Content-SHA256", "")
        supplied_signature = headers.get("X-Review-Signature", "")
        if not _USER_ID_PATTERN.fullmatch(user_id):
            raise AuthenticationError("企业身份用户ID无效")
        if role not in ROLE_PERMISSIONS:
            raise AuthenticationError("企业身份角色无效")
        if not _REQUEST_ID_PATTERN.fullmatch(request_id):
            raise AuthenticationError("企业身份请求ID无效")
        try:
            issued_at = int(timestamp)
        except ValueError as exc:
            raise AuthenticationError("企业身份时间戳无效") from exc
        now = int(self.clock())
        if issued_at > now + 5 or now - issued_at > self.max_age_seconds:
            raise AuthenticationError("企业身份断言已过期或尚未生效")
        body = await request.body()
        calculated_body_hash = hashlib.sha256(body).hexdigest()
        if not hmac.compare_digest(content_sha256, calculated_body_hash):
            raise AuthenticationError("企业身份请求体摘要不匹配")
        path_and_query = request.url.path
        if request.url.query:
            path_and_query += f"?{request.url.query}"
        expected_signature = proxy_signature(
            secret=self.signing_secret,
            method=request.method,
            path_and_query=path_and_query,
            user_id=user_id,
            display_name_b64=display_name_b64,
            role=role,
            timestamp=timestamp,
            request_id=request_id,
            content_sha256=content_sha256,
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise AuthenticationError("企业身份签名无效")
        if not self.replay_guard.claim(
            request_id,
            expires_at=issued_at + self.max_age_seconds,
            now=now,
        ):
            raise AuthenticationError("企业身份请求ID已使用")
        display_name = _decode_display_name(display_name_b64)
        return Principal(
            user_id=user_id,
            display_name=display_name,
            role=role,  # type: ignore[arg-type]
            permissions=ROLE_PERMISSIONS[role],
        )

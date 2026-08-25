"""Persistent role-based authentication using one-way token hashes."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .database import ReviewStore, ReviewStoreError, _now


Role = Literal["admin", "analyst", "reviewer", "submitter", "auditor"]
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "admin": frozenset({"view", "review", "dry_run", "authorize", "execute_test", "manage_users", "audit"}),
    "analyst": frozenset({"view"}),
    "reviewer": frozenset({"view", "review", "dry_run"}),
    "submitter": frozenset({"view", "authorize", "execute_test"}),
    "auditor": frozenset({"view", "audit"}),
}


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    display_name: str
    role: Role
    permissions: frozenset[str]


class AuthenticationError(ReviewStoreError):
    pass


def _lookup(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _token_hash(token: str, salt: bytes) -> str:
    return hashlib.scrypt(
        token.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
    ).hex()


class AuthManager:
    def __init__(self, store: ReviewStore):
        self.store = store
        self.store.initialize()

    def bootstrap_admin(self, token: str, *, created_at: datetime | None = None) -> Principal:
        if len(token) < 16:
            raise AuthenticationError("管理员令牌至少需要16个字符")
        timestamp = created_at or _now()
        with self.store._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()
            if row[0] == 0:
                salt = secrets.token_bytes(16)
                connection.execute(
                    "INSERT INTO users(user_id,display_name,role,token_lookup,token_salt,token_hash,active,created_at) VALUES(?,?,?,?,?,?,1,?)",
                    (
                        "system-admin",
                        "系统管理员",
                        "admin",
                        _lookup(token),
                        salt.hex(),
                        _token_hash(token, salt),
                        timestamp.isoformat(),
                    ),
                )
        principal = self.authenticate(token)
        if principal.user_id != "system-admin":
            raise AuthenticationError("启动令牌不是系统管理员令牌")
        return principal

    def authenticate(self, token: str) -> Principal:
        if not token:
            raise AuthenticationError("缺少审核令牌")
        with self.store._connection() as connection:
            row = connection.execute(
                "SELECT user_id,display_name,role,token_salt,token_hash FROM users WHERE token_lookup=? AND active=1",
                (_lookup(token),),
            ).fetchone()
        if row is None:
            raise AuthenticationError("审核令牌无效")
        calculated = _token_hash(token, bytes.fromhex(row["token_salt"]))
        if not hmac.compare_digest(calculated, row["token_hash"]):
            raise AuthenticationError("审核令牌无效")
        return Principal(
            user_id=row["user_id"],
            display_name=row["display_name"],
            role=row["role"],
            permissions=ROLE_PERMISSIONS[row["role"]],
        )

    def create_user(
        self,
        *,
        user_id: str,
        display_name: str,
        role: Role,
        created_at: datetime | None = None,
    ) -> tuple[Principal, str]:
        if not user_id or not display_name or role not in ROLE_PERMISSIONS:
            raise AuthenticationError("用户ID、显示名和角色必须有效")
        token = secrets.token_urlsafe(32)
        salt = secrets.token_bytes(16)
        timestamp = created_at or _now()
        try:
            with self.store._connection() as connection:
                connection.execute(
                    "INSERT INTO users(user_id,display_name,role,token_lookup,token_salt,token_hash,active,created_at) VALUES(?,?,?,?,?,?,1,?)",
                    (
                        user_id,
                        display_name,
                        role,
                        _lookup(token),
                        salt.hex(),
                        _token_hash(token, salt),
                        timestamp.isoformat(),
                    ),
                )
        except Exception as exc:
            raise AuthenticationError(f"用户创建失败：{exc}") from exc
        return self.authenticate(token), token

    def list_users(self) -> list[dict]:
        with self.store._connection() as connection:
            rows = connection.execute(
                "SELECT user_id,display_name,role,active,created_at FROM users ORDER BY user_id"
            ).fetchall()
        return [dict(row) for row in rows]

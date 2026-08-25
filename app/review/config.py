"""Fail-closed deployment configuration for the review workbench."""

from __future__ import annotations

import os
import ipaddress
from pathlib import Path
from typing import Literal, Mapping
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from .execution import TestSubmissionSettings


EnvironmentName = Literal["local", "enterprise_test"]
AuthMode = Literal["local_token", "signed_proxy"]
CheckStatus = Literal["pass", "warn", "block"]


class DeploymentConfigurationError(ValueError):
    pass


class DeploymentCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    status: CheckStatus
    message: str


class DeploymentReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ready: bool
    environment: EnvironmentName
    auth_mode: AuthMode
    checks: list[DeploymentCheck]


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise DeploymentConfigurationError(f"布尔配置值无效：{value}")


def _read_secret(
    environment: Mapping[str, str],
    name: str,
) -> SecretStr | None:
    direct = environment.get(name)
    file_name = environment.get(f"{name}_FILE")
    if direct and file_name:
        raise DeploymentConfigurationError(f"{name}与{name}_FILE不能同时设置")
    if file_name:
        secret_path = Path(file_name).expanduser().resolve()
        if not secret_path.is_file():
            raise DeploymentConfigurationError(f"{name}_FILE不是可读文件")
        try:
            value = secret_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise DeploymentConfigurationError(f"无法读取{name}_FILE") from exc
    else:
        value = direct or ""
    if not value:
        return None
    if "\x00" in value:
        raise DeploymentConfigurationError(f"{name}包含非法字符")
    return SecretStr(value)


def _csv_values(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(dict.fromkeys(item.strip().lower() for item in value.split(",") if item.strip()))


class DeploymentSettings(BaseModel):
    """Validated settings with no secret-bearing serialization surface."""

    model_config = ConfigDict(extra="forbid")

    environment: EnvironmentName = "local"
    auth_mode: AuthMode = "local_token"
    database_path: Path = Path("09_审核工作台/审核审计.sqlite3")
    public_base_url: str | None = None
    admin_token: SecretStr | None = Field(default=None, exclude=True)
    identity_signing_secret: SecretStr | None = Field(default=None, exclude=True)
    identity_max_age_seconds: int = Field(default=60, ge=10, le=300)
    trusted_proxy_networks: tuple[str, ...] = ("127.0.0.1/32", "::1/128")
    allow_sqlite_enterprise_test: bool = False
    test_submit_enabled: bool = False
    test_rpa_base_url: str = "http://127.0.0.1:8090"
    test_rpa_api_token: SecretStr | None = Field(default=None, exclude=True)
    test_rpa_allowed_hosts: tuple[str, ...] = ()
    test_rate_limit_per_minute: int = Field(default=5, ge=1, le=60)

    @model_validator(mode="after")
    def validate_security_boundary(self) -> "DeploymentSettings":
        if self.auth_mode == "local_token":
            if self.admin_token is None or len(self.admin_token.get_secret_value()) < 16:
                raise ValueError("本地令牌模式必须提供至少16位的管理员令牌")
        elif self.identity_signing_secret is None or len(self.identity_signing_secret.get_secret_value()) < 32:
            raise ValueError("签名代理模式必须提供至少32位的身份签名密钥")
        if self.auth_mode == "signed_proxy" and not self.trusted_proxy_networks:
            raise ValueError("签名代理模式必须配置至少一个可信代理网段")
        if self.auth_mode == "signed_proxy":
            try:
                tuple(ipaddress.ip_network(item, strict=False) for item in self.trusted_proxy_networks)
            except ValueError as exc:
                raise ValueError("可信代理网段配置无效") from exc
        if self.environment == "enterprise_test":
            if self.auth_mode != "signed_proxy":
                raise ValueError("企业测试环境必须使用signed_proxy身份模式")
            parsed = urlsplit(self.public_base_url or "")
            if parsed.scheme != "https" or not parsed.hostname:
                raise ValueError("企业测试环境必须配置HTTPS公开基础地址")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("公开基础地址不得包含用户信息、查询参数或片段")
            if parsed.path not in {"", "/"}:
                raise ValueError("公开基础地址只能配置服务根地址")
        return self

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        database_path: str | Path | None = None,
    ) -> "DeploymentSettings":
        values = os.environ if environment is None else environment
        selected_database = database_path or values.get("REVIEW_DATABASE_PATH") or "09_审核工作台/审核审计.sqlite3"
        return cls(
            environment=values.get("REVIEW_ENVIRONMENT", "local"),
            auth_mode=values.get("REVIEW_AUTH_MODE", "local_token"),
            database_path=Path(selected_database),
            public_base_url=values.get("REVIEW_PUBLIC_BASE_URL") or None,
            admin_token=_read_secret(values, "REVIEW_ADMIN_TOKEN"),
            identity_signing_secret=_read_secret(values, "REVIEW_IDENTITY_SIGNING_SECRET"),
            identity_max_age_seconds=int(values.get("REVIEW_IDENTITY_MAX_AGE_SECONDS", "60")),
            trusted_proxy_networks=_csv_values(values.get("REVIEW_TRUSTED_PROXY_NETWORKS"))
            or ("127.0.0.1/32", "::1/128"),
            allow_sqlite_enterprise_test=_parse_bool(values.get("REVIEW_ALLOW_SQLITE_ENTERPRISE_TEST")),
            test_submit_enabled=_parse_bool(values.get("REVIEW_TEST_SUBMIT_ENABLED")),
            test_rpa_base_url=values.get("REVIEW_TEST_RPA_BASE_URL", "http://127.0.0.1:8090"),
            test_rpa_api_token=_read_secret(values, "REVIEW_TEST_RPA_API_TOKEN"),
            test_rpa_allowed_hosts=_csv_values(values.get("REVIEW_TEST_RPA_ALLOWED_HOSTS")),
            test_rate_limit_per_minute=int(values.get("REVIEW_TEST_RATE_LIMIT", "5")),
        )

    def test_submission_settings(self) -> TestSubmissionSettings:
        return TestSubmissionSettings(
            enabled=self.test_submit_enabled,
            base_url=self.test_rpa_base_url,
            api_token=self.test_rpa_api_token,
            allowed_hosts=self.test_rpa_allowed_hosts,
            rate_limit_per_minute=self.test_rate_limit_per_minute,
        )

    def readiness(self) -> DeploymentReadiness:
        checks: list[DeploymentCheck] = []
        checks.append(
            DeploymentCheck(
                name="identity",
                status="pass",
                message="本地令牌认证已配置" if self.auth_mode == "local_token" else "签名式企业身份代理已配置",
            )
        )
        if self.environment == "enterprise_test":
            checks.append(DeploymentCheck(name="public_tls", status="pass", message="公开基础地址使用HTTPS"))
            if self.allow_sqlite_enterprise_test:
                checks.append(
                    DeploymentCheck(
                        name="database_backend",
                        status="warn",
                        message="已显式允许单实例SQLite试运行；不得横向扩容",
                    )
                )
            else:
                checks.append(
                    DeploymentCheck(
                        name="database_backend",
                        status="block",
                        message="尚未选择服务型数据库；如仅做单实例沙箱，需显式允许SQLite",
                    )
                )
        else:
            checks.append(DeploymentCheck(name="database_backend", status="pass", message="本地单实例使用SQLite"))
        database_parent = self.database_path.expanduser().resolve().parent
        if database_parent.exists() and database_parent.is_dir() and os.access(database_parent, os.W_OK):
            checks.append(DeploymentCheck(name="database_directory", status="pass", message="数据库目录存在"))
        else:
            checks.append(DeploymentCheck(name="database_directory", status="block", message="数据库目录不存在或不可写"))
        try:
            self.test_submission_settings()
        except ValueError as exc:
            checks.append(DeploymentCheck(name="rpa_sandbox", status="block", message=str(exc)))
        else:
            checks.append(
                DeploymentCheck(
                    name="rpa_sandbox",
                    status="pass" if self.test_submit_enabled else "warn",
                    message="测试RPA地址已通过安全校验" if self.test_submit_enabled else "测试RPA提交保持关闭",
                )
            )
        return DeploymentReadiness(
            ready=not any(item.status == "block" for item in checks),
            environment=self.environment,
            auth_mode=self.auth_mode,
            checks=checks,
        )

    def safe_summary(self) -> dict[str, object]:
        return {
            "environment": self.environment,
            "auth_mode": self.auth_mode,
            "database_backend": "sqlite",
            "public_base_url_configured": bool(self.public_base_url),
            "test_submit_enabled": self.test_submit_enabled,
            "test_rpa_origin": self.test_submission_settings().safe_origin,
        }

"""Authenticated local FastAPI workbench for governed task review."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from app.rpa.models import TaskCandidateBundle
from app.batch.models import BatchCandidateBundle
from app.rpa import RpaWorkflowError

from .database import (
    CandidateConflictError,
    CandidateNotFoundError,
    CandidateStateError,
    ReviewStore,
    ReviewStoreError,
    SubmissionRateLimitError,
)
from .auth import AuthManager, AuthenticationError, Principal, Role
from .config import DeploymentSettings
from .execution import GovernedTestExecutor
from .identity import IdentityProvider, LocalTokenIdentityProvider


class WebModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ApprovalRequest(WebModel):
    expected_version: int = Field(ge=0)
    assignee_name: str = Field(min_length=1, max_length=100)
    department: str = Field(min_length=1, max_length=100)
    role: str | None = Field(default=None, max_length=100)
    deadline: date
    priority: str
    notify_method: str
    comment: str | None = Field(default=None, max_length=1000)


class RejectionRequest(WebModel):
    expected_version: int = Field(ge=0)
    comment: str = Field(min_length=1, max_length=1000)


class DryRunRequest(WebModel):
    expected_version: int = Field(ge=0)


class UserCreateRequest(WebModel):
    user_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9._-]+$")
    display_name: str = Field(min_length=1, max_length=100)
    role: Role


class AuthorizationRequest(WebModel):
    expected_version: int = Field(ge=0)
    comment: str | None = Field(default=None, max_length=1000)


class ExecuteTestRequest(WebModel):
    expected_version: int = Field(ge=0)
    confirmation: str = Field(pattern=r"^TEST$")


def create_review_app(
    *,
    database_path: str | Path,
    admin_token: str | None = None,
    candidate_files: list[str | Path] | None = None,
    test_executor: GovernedTestExecutor | None = None,
    identity_provider: IdentityProvider | None = None,
    deployment_settings: DeploymentSettings | None = None,
) -> FastAPI:
    if identity_provider is None and (admin_token is None or len(admin_token) < 16):
        raise ValueError("本地令牌模式必须提供至少16位的审核工作台令牌")
    if identity_provider is not None and admin_token is not None:
        raise ValueError("企业身份模式不得同时启用本地管理员令牌")
    store = ReviewStore(database_path)
    store.initialize()
    for file_path in candidate_files or []:
        raw = Path(file_path).read_text(encoding="utf-8")
        try:
            bundle = TaskCandidateBundle.model_validate_json(raw)
            store.import_bundle(bundle)
        except ValueError:
            batch_bundle = BatchCandidateBundle.model_validate_json(raw)
            if batch_bundle.candidate_count != len(batch_bundle.candidates):
                raise ValueError("批量候选声明数量与实际数量不一致")
            store.import_candidates(batch_bundle.candidates)
    auth = AuthManager(store)
    local_user_management = identity_provider is None
    if identity_provider is None:
        auth.bootstrap_admin(admin_token or "")
        identity_provider = LocalTokenIdentityProvider(auth)

    app = FastAPI(
        title="成本分析任务审核工作台",
        version="4.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.review_store = store
    app.state.test_executor = test_executor
    app.state.deployment_settings = deployment_settings
    app.state.identity_provider = identity_provider
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        )
        if deployment_settings and deployment_settings.environment == "enterprise_test":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    async def authorized(request: Request) -> Principal:
        try:
            return await identity_provider.authenticate(request)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    def require(permission: str):
        def dependency(principal: Principal = Depends(authorized)) -> Principal:
            if permission not in principal.permissions:
                raise HTTPException(status_code=403, detail=f"角色{principal.role}没有{permission}权限")
            return principal
        return dependency

    def handle_store_error(exc: Exception) -> None:
        if isinstance(exc, CandidateNotFoundError):
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if isinstance(exc, (CandidateConflictError, CandidateStateError)):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if isinstance(exc, SubmissionRateLimitError):
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        if isinstance(exc, (ReviewStoreError, RpaWorkflowError, ValueError)):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise exc

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse((static_dir / "index.html").read_text(encoding="utf-8"))

    @app.get("/health")
    def health() -> dict:
        audit = store.verify_audit_chain()
        return {
            "status": "ok" if audit["status"] == "PASS" else "degraded",
            "audit": audit,
            "external_submit_enabled": False,
            "test_submit_enabled": bool(test_executor and test_executor.settings.enabled),
            "environment": deployment_settings.environment if deployment_settings else "local",
            "auth_mode": deployment_settings.auth_mode if deployment_settings else "local_token",
        }

    @app.get("/live")
    def live() -> dict:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> JSONResponse:
        deployment = deployment_settings.readiness() if deployment_settings else None
        with store._connection() as connection:
            database_integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            foreign_key_issues = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        audit = store.verify_audit_chain()
        is_ready = (
            database_integrity == "ok"
            and foreign_key_issues == 0
            and audit["status"] == "PASS"
            and (deployment is None or deployment.ready)
        )
        payload = {
            "status": "ready" if is_ready else "not_ready",
            "database_integrity": database_integrity,
            "foreign_key_issues": foreign_key_issues,
            "audit": audit,
            "deployment": deployment.model_dump(mode="json") if deployment else None,
        }
        return JSONResponse(payload, status_code=200 if is_ready else 503)

    @app.get("/api/me")
    def me(principal: Principal = Depends(require("view"))) -> dict:
        return principal.model_dump(mode="json")

    @app.get("/api/dashboard")
    def dashboard(principal: Principal = Depends(require("view"))) -> dict:
        candidates = store.list_candidates()
        return {
            "total": len(candidates),
            "pending_review": sum(item["review_state"] == "pending_review" for item in candidates),
            "approved": sum(item["review_state"] == "approved" for item in candidates),
            "rejected": sum(item["review_state"] == "rejected" for item in candidates),
            "dry_run": sum(item["submission_state"] == "dry_run" for item in candidates),
            "reconcile_required": sum(item["execution_state"] == "reconcile_required" for item in candidates),
            "external_submit_enabled": False,
            "test_submit_enabled": bool(test_executor and test_executor.settings.enabled),
            "audit": store.verify_audit_chain(),
        }

    @app.get("/api/candidates")
    def candidates(principal: Principal = Depends(require("view"))) -> list[dict]:
        return store.list_candidates()

    @app.get("/api/candidates/{candidate_id}")
    def candidate(candidate_id: str, principal: Principal = Depends(require("view"))) -> dict:
        try:
            return store.get_candidate(candidate_id)
        except Exception as exc:
            handle_store_error(exc)
            raise

    @app.post("/api/candidates/{candidate_id}/approve")
    def approve(
        candidate_id: str,
        request: ApprovalRequest,
        principal: Principal = Depends(require("review")),
    ) -> dict:
        try:
            return store.approve(
                candidate_id,
                expected_version=request.expected_version,
                reviewer=principal.display_name,
                reviewer_user_id=principal.user_id,
                assignee_name=request.assignee_name,
                department=request.department,
                role=request.role,
                deadline=request.deadline,
                priority=request.priority,
                notify_method=request.notify_method,
                comment=request.comment,
            )
        except Exception as exc:
            handle_store_error(exc)
            raise

    @app.post("/api/candidates/{candidate_id}/reject")
    def reject(
        candidate_id: str,
        request: RejectionRequest,
        principal: Principal = Depends(require("review")),
    ) -> dict:
        try:
            return store.reject(
                candidate_id,
                expected_version=request.expected_version,
                reviewer=principal.display_name,
                reviewer_user_id=principal.user_id,
                comment=request.comment,
            )
        except Exception as exc:
            handle_store_error(exc)
            raise

    @app.post("/api/candidates/{candidate_id}/dry-run")
    def dry_run(
        candidate_id: str,
        request: DryRunRequest,
        principal: Principal = Depends(require("dry_run")),
    ) -> dict:
        try:
            return store.dry_run(
                candidate_id,
                expected_version=request.expected_version,
                actor=principal.display_name,
            )
        except Exception as exc:
            handle_store_error(exc)
            raise

    @app.post("/api/candidates/{candidate_id}/authorize-submission")
    def authorize_submission(
        candidate_id: str,
        request: AuthorizationRequest,
        principal: Principal = Depends(require("authorize")),
    ) -> dict:
        try:
            return store.authorize_submission(
                candidate_id,
                expected_version=request.expected_version,
                authorizer_user_id=principal.user_id,
                authorizer_name=principal.display_name,
                comment=request.comment,
            )
        except Exception as exc:
            handle_store_error(exc)
            raise

    @app.post("/api/candidates/{candidate_id}/execute-test")
    def execute_test(
        candidate_id: str,
        request: ExecuteTestRequest,
        principal: Principal = Depends(require("execute_test")),
    ) -> dict:
        if test_executor is None or not test_executor.settings.enabled:
            raise HTTPException(status_code=503, detail="测试提交功能未启用")
        try:
            claim = store.claim_test_execution(
                candidate_id,
                expected_version=request.expected_version,
                operator_user_id=principal.user_id,
                operator_name=principal.display_name,
                endpoint_origin=test_executor.settings.safe_origin,
                rate_limit_per_minute=test_executor.settings.rate_limit_per_minute,
                lease_seconds=test_executor.settings.lease_seconds,
            )
            outcome = test_executor.execute(claim)
            detail = store.complete_test_execution(
                claim,
                outcome,
                actor=principal.display_name,
            )
            detail["test_execution_outcome"] = outcome.model_dump(mode="json")
            return detail
        except Exception as exc:
            handle_store_error(exc)
            raise

    @app.get("/api/users")
    def users(principal: Principal = Depends(require("manage_users"))) -> list[dict]:
        if not local_user_management:
            raise HTTPException(status_code=409, detail="企业身份模式由SSO网关管理用户")
        return auth.list_users()

    @app.post("/api/users")
    def create_user(
        request: UserCreateRequest,
        principal: Principal = Depends(require("manage_users")),
    ) -> dict:
        if not local_user_management:
            raise HTTPException(status_code=409, detail="企业身份模式由SSO网关管理用户")
        try:
            created, issued_token = auth.create_user(
                user_id=request.user_id,
                display_name=request.display_name,
                role=request.role,
            )
            return {"user": created.model_dump(mode="json"), "issued_token": issued_token}
        except AuthenticationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/audit/verify")
    def verify_audit(principal: Principal = Depends(require("audit"))) -> dict:
        return store.verify_audit_chain()

    return app

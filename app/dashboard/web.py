"""FastAPI entry point for the unified read-only cost-analysis dashboard."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.analysis import AnalysisError, DataQualityError, ScenarioNotFoundError

from app.rpa.client import RpaTransport

from .models import (
    DashboardAnalysisRequest,
    DashboardReportRequest,
    DashboardWorkflowApproveRequest,
    DashboardWorkflowCreateRequest,
    DashboardWorkflowSubmitRequest,
)
from .reports import (
    ArtifactKind,
    DashboardReportError,
    PeriodReportError,
    download_filename,
    generate_report_artifacts,
    resolve_report_artifact,
)
from .service import build_heatmap_data, build_selected_dashboard_analysis, dashboard_options
from .workflow import DashboardWorkflowError, DashboardWorkflowStore


def create_dashboard_app(
    *,
    data_dir: str | Path,
    index_dir: str | Path | None = None,
    report_output_dir: str | Path | None = None,
    rpa_base_url: str = "http://127.0.0.1:8090",
    rpa_transport: RpaTransport | None = None,
) -> FastAPI:
    data_root = Path(data_dir).resolve()
    knowledge_root = (
        Path(index_dir).resolve() if index_dir else data_root / "06_知识证据索引"
    )
    static_dir = Path(__file__).resolve().parent / "static"
    report_root = (
        Path(report_output_dir).resolve()
        if report_output_dir
        else data_root / "07_报告输出" / "web"
    )

    app = FastAPI(
        title="制药成本智能分析系统",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.data_dir = data_root
    app.state.index_dir = knowledge_root
    app.state.report_output_dir = report_root
    workflow_store = DashboardWorkflowStore(
        report_root,
        rpa_base_url=rpa_base_url,
        transport=rpa_transport,
    )
    app.state.workflow_store = workflow_store
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        is_pdf_preview = (
            request.url.path.startswith("/api/reports/")
            and request.url.path.endswith("/preview")
            and response.headers.get("content-type", "").lower().startswith("application/pdf")
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        # Chromium/Edge renders an inline PDF through its built-in extension viewer.
        # HTML-oriented frame/CSP restrictions can block that internal viewer even
        # though the authenticated PDF response itself is valid.  Keep the preview
        # non-cacheable and non-indexable, but do not apply document CSP/XFO to
        # this one inline PDF response. All HTML and API responses remain locked.
        if is_pdf_preview:
            if "X-Frame-Options" in response.headers:
                del response.headers["X-Frame-Options"]
            if "Content-Security-Policy" in response.headers:
                del response.headers["Content-Security-Policy"]
        else:
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; "
                "style-src 'self'; img-src 'self' data:; connect-src 'self'; "
                "font-src 'self'; object-src 'none'; frame-ancestors 'none'"
            )
        return response

    def fail(exc: Exception) -> None:
        if isinstance(exc, ScenarioNotFoundError):
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if isinstance(exc, (AnalysisError, DataQualityError, ValueError)):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if isinstance(exc, (DashboardReportError, PeriodReportError)):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if isinstance(exc, DashboardWorkflowError):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if isinstance(exc, FileNotFoundError):
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        raise exc

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse((static_dir / "index.html").read_text(encoding="utf-8"))

    @app.get("/health")
    def health() -> dict:
        try:
            options = dashboard_options(data_root)
        except Exception as exc:
            return {"status": "degraded", "detail": str(exc)}
        return {
            "status": "ok",
            "data_quality": options["data_quality"],
            "external_systems_required": False,
        }

    @app.get("/api/options")
    def options() -> dict:
        try:
            return dashboard_options(data_root)
        except Exception as exc:
            fail(exc)
            raise

    @app.get("/api/heatmap")
    def heatmap() -> dict:
        try:
            return build_heatmap_data(data_root)
        except Exception as exc:
            fail(exc)
            raise

    @app.post("/api/analyze")
    def analyze(request: DashboardAnalysisRequest) -> dict:
        try:
            return build_selected_dashboard_analysis(
                data_root,
                knowledge_root,
                analysis_type=request.analysis_type,
                product=request.product,
                month=request.month,
                quarter=request.quarter,
                topic=request.topic,
            )
        except Exception as exc:
            fail(exc)
            raise

    @app.post("/api/reports")
    def generate_report(request: DashboardReportRequest) -> dict:
        try:
            return generate_report_artifacts(
                data_root=data_root,
                index_root=knowledge_root,
                output_root=report_root,
                analysis_type=request.analysis_type,
                product=request.product,
                month=request.month,
                quarter=request.quarter,
                topic=request.topic,
                use_llm=request.use_llm,
            )
        except Exception as exc:
            fail(exc)
            raise

    @app.get("/api/reports/{report_id}/preview")
    def preview_report(report_id: str) -> FileResponse:
        try:
            path, manifest, media_type = resolve_report_artifact(report_root, report_id, "pdf")
            return FileResponse(
                path,
                media_type=media_type,
                filename=download_filename(manifest, "pdf"),
                content_disposition_type="inline",
            )
        except Exception as exc:
            fail(exc)
            raise

    @app.get("/api/reports/{report_id}/download/{artifact}")
    def download_report(report_id: str, artifact: ArtifactKind) -> FileResponse:
        try:
            path, manifest, media_type = resolve_report_artifact(report_root, report_id, artifact)
            return FileResponse(
                path,
                media_type=media_type,
                filename=download_filename(manifest, artifact),
            )
        except Exception as exc:
            fail(exc)
            raise

    @app.post("/api/workflows")
    def create_workflow(request: DashboardWorkflowCreateRequest) -> dict:
        try:
            return workflow_store.create_candidate(request.report_id, use_llm=request.use_llm)
        except Exception as exc:
            fail(exc)
            raise

    @app.get("/api/workflow-stats")
    def workflow_stats() -> dict:
        try:
            return workflow_store.tracking_stats()
        except Exception as exc:
            fail(exc)
            raise

    @app.get("/api/workflows/{report_id}")
    def workflow_state(report_id: str) -> dict:
        try:
            return workflow_store.get_state(report_id)
        except Exception as exc:
            fail(exc)
            raise

    @app.post("/api/workflows/{report_id}/approve")
    def approve_workflow(
        report_id: str,
        request: DashboardWorkflowApproveRequest,
    ) -> dict:
        try:
            return workflow_store.approve(report_id, **request.model_dump())
        except Exception as exc:
            fail(exc)
            raise

    @app.post("/api/workflows/{report_id}/submit")
    def submit_workflow(
        report_id: str,
        request: DashboardWorkflowSubmitRequest,
    ) -> dict:
        try:
            return workflow_store.submit(report_id, confirmation=request.confirmation)
        except Exception as exc:
            fail(exc)
            raise

    return app

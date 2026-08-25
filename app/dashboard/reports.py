"""Atomic Web report generation and traversal-safe artifact resolution."""

from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import date
from pathlib import Path
from typing import Literal

from app.llm import LlmSettings, enhance_report_contract
from app.reporting import (
    build_report_contract,
    render_contract_json,
    render_docx,
    render_markdown,
    render_pdf,
)

from .period_reports import PeriodReportError, generate_period_report_artifacts
from .report_view import build_report_web_content


REPORT_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
ARTIFACT_FILES = {
    "pdf": ("report.pdf", "application/pdf"),
    "docx": ("report.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "md": ("report.md", "text/markdown; charset=utf-8"),
    "json": ("report.json", "application/json"),
}
ArtifactKind = Literal["pdf", "docx", "md", "json"]


class DashboardReportError(RuntimeError):
    pass


def generate_report_artifacts(
    *,
    data_root: str | Path,
    index_root: str | Path,
    output_root: str | Path,
    product: str,
    month: str | None,
    analysis_type: str = "月度成本分析",
    quarter: str | None = None,
    topic: str | None = None,
    use_llm: bool = False,
) -> dict[str, object]:
    if analysis_type != "月度成本分析":
        return generate_period_report_artifacts(
            data_root=data_root,
            index_root=index_root,
            output_root=output_root,
            analysis_type=analysis_type,
            product=product,
            month=month,
            quarter=quarter,
            topic=topic,
            artifact_files=ARTIFACT_FILES,
            use_llm=use_llm,
        )
    if month is None:
        raise DashboardReportError("月度报告必须指定分析月份")
    output = Path(output_root).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report_id = uuid.uuid4().hex
    final = output / report_id
    staging = output / f".{report_id}.staging"
    staging.mkdir()
    try:
        contract = build_report_contract(
            data_root,
            index_root,
            product,
            month,
            generated_date=date.today().isoformat(),
        )
        if use_llm:
            contract = enhance_report_contract(
                contract,
                LlmSettings.from_env(force_enabled=True),
            )
        if contract.validation_status != "PASS":
            raise DashboardReportError(f"报告契约校验失败：{contract.validation_issues}")
        render_contract_json(contract, staging / "report.json")
        render_markdown(contract, staging / "report.md")
        render_docx(contract, staging / "report.docx")
        render_pdf(contract, staging / "report.pdf", source_docx=staging / "report.docx")
        web_content = build_report_web_content(contract)
        manifest = {
            "report_id": report_id,
            "report_number": contract.report_number,
            "product": contract.product,
            "month": contract.month,
            "period": contract.month,
            "analysis_type": "月度成本分析",
            "workflow_supported": True,
            "generation": contract.generation.model_dump(mode="json"),
        }
        (staging / "artifact_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
        staging.replace(final)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return {
        **manifest,
        "web_content": web_content,
        "preview_url": f"/api/reports/{report_id}/preview",
        "downloads": {
            kind: f"/api/reports/{report_id}/download/{kind}"
            for kind in ARTIFACT_FILES
        },
    }


def resolve_report_artifact(
    output_root: str | Path,
    report_id: str,
    artifact: ArtifactKind,
) -> tuple[Path, dict[str, object], str]:
    if not REPORT_ID_PATTERN.fullmatch(report_id):
        raise DashboardReportError("报告ID格式无效")
    if artifact not in ARTIFACT_FILES:
        raise DashboardReportError("不支持的报告格式")
    root = Path(output_root).resolve()
    report_dir = (root / report_id).resolve()
    if report_dir.parent != root:
        raise DashboardReportError("报告路径越界")
    manifest_path = report_dir / "artifact_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError("报告不存在")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DashboardReportError("报告清单损坏") from exc
    if manifest.get("report_id") != report_id:
        raise DashboardReportError("报告清单ID不一致")
    filename, media_type = ARTIFACT_FILES[artifact]
    path = report_dir / filename
    if not path.is_file():
        raise FileNotFoundError("报告文件不存在")
    return path, manifest, media_type


def download_filename(manifest: dict[str, object], artifact: ArtifactKind) -> str:
    product = re.sub(r"[^\w\u4e00-\u9fff-]", "_", str(manifest["product"]))
    period = re.sub(r"[^0-9Q-]", "", str(manifest.get("period", manifest["month"])))
    analysis_type = re.sub(r"[^\w\u4e00-\u9fff-]", "_", str(manifest.get("analysis_type", "月度成本分析")))
    return f"{period}_{product}_{analysis_type}报告.{artifact}"

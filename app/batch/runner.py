"""Atomic, resumable batch generation for reports and RPA candidates."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from app.data_quality import load_validated_data
from app.reporting import (
    build_report_contract,
    render_contract_json,
    render_docx,
    render_markdown,
    render_pdf,
)
from app.reporting.models import ReportContract
from app.rpa import TaskCandidateBundle, build_task_candidates

from .models import BatchCandidateBundle, BatchItem, BatchManifest, BatchScenario


MANIFEST_VERSION = "1.1.0"
SHANGHAI = ZoneInfo("Asia/Shanghai")


class BatchRunError(RuntimeError):
    pass


class BatchRunFailed(BatchRunError):
    def __init__(self, manifest: BatchManifest, staging_path: Path):
        super().__init__(f"批次{manifest.run_id}存在{manifest.failed_scenarios}个失败场景")
        self.manifest = manifest
        self.staging_path = staging_path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def discover_scenarios(data_root: str | Path) -> list[BatchScenario]:
    _, bundle = load_validated_data(data_root)
    pairs = sorted({(row.month, row.product) for row in bundle.plant1_summary})
    return [BatchScenario(product=product, month=month) for month, product in pairs]


def _item_files_valid(staging: Path, item: BatchItem) -> bool:
    paths = {
        "contract": item.contract_path,
        "markdown": item.markdown_path,
        "word": item.word_path,
        "pdf": item.pdf_path,
        "candidate": item.candidate_path,
    }
    if item.status != "PASS" or not all(paths.values()):
        return False
    return all(
        (staging / relative).is_file()
        and _sha(staging / relative) == item.hashes.get(label)
        for label, relative in paths.items()
    )


def run_batch(
    *,
    data_root: str | Path,
    index_root: str | Path,
    output_root: str | Path,
    run_id: str,
    generated_date: str,
    scenarios: list[BatchScenario] | None = None,
    resume: bool = False,
    contract_builder: Callable[..., ReportContract] = build_report_contract,
    now: datetime | None = None,
) -> tuple[BatchManifest, Path]:
    data_root = Path(data_root).resolve()
    index_root = Path(index_root).resolve()
    output_root = Path(output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    final_path = output_root / run_id
    staging = output_root / f".{run_id}.staging"
    manifest_path = staging / "manifest.json"
    timestamp = now or datetime.now(SHANGHAI)
    scenarios = scenarios or discover_scenarios(data_root)
    if not scenarios:
        raise BatchRunError("没有可运行的产品月份场景")
    if len({item.key for item in scenarios}) != len(scenarios):
        raise BatchRunError("批次场景存在重复")
    if final_path.exists():
        raise BatchRunError(f"正式批次目录已存在，禁止覆盖：{final_path}")

    if staging.exists():
        if not resume:
            raise BatchRunError(f"存在未完成暂存批次，必须显式resume：{staging}")
        if not manifest_path.is_file():
            raise BatchRunError("暂存批次缺少manifest.json")
        manifest = BatchManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        if manifest.run_id != run_id or manifest.generated_date != generated_date:
            raise BatchRunError("resume参数与暂存批次不一致")
    else:
        staging.mkdir(parents=True)
        manifest = BatchManifest(
            manifest_version=MANIFEST_VERSION,
            run_id=run_id,
            status="RUNNING",
            generated_date=generated_date,
            created_at=timestamp.isoformat(),
            data_root=str(data_root),
            index_root=str(index_root),
            output_root=str(final_path),
            total_scenarios=len(scenarios),
            passed_scenarios=0,
            failed_scenarios=0,
            items=[BatchItem(scenario=item, status="PENDING") for item in scenarios],
        )
        _write_json(manifest_path, manifest)

    existing = {item.scenario.key: item for item in manifest.items}
    if set(existing) != {item.key for item in scenarios}:
        raise BatchRunError("resume场景集合与暂存manifest不一致")
    completed_items: list[BatchItem] = []
    for scenario in scenarios:
        previous = existing[scenario.key]
        if _item_files_valid(staging, previous):
            completed_items.append(previous)
            continue
        safe_stem = f"{scenario.month}_{scenario.product}"
        report_dir = staging / "reports" / safe_stem
        try:
            contract = contract_builder(
                data_root,
                index_root,
                scenario.product,
                scenario.month,
                generated_date=generated_date,
            )
            if contract.validation_status != "PASS":
                raise BatchRunError(f"报告契约FAIL：{contract.validation_issues}")
            contract_path = report_dir / "report.json"
            markdown_path = report_dir / "report.md"
            word_path = report_dir / "report.docx"
            pdf_path = report_dir / "report.pdf"
            candidate_path = report_dir / "candidates.json"
            render_contract_json(contract, contract_path)
            render_markdown(contract, markdown_path)
            render_docx(contract, word_path)
            render_pdf(contract, pdf_path, source_docx=word_path)
            candidates = build_task_candidates(contract)
            _write_json(candidate_path, candidates)
            relative_paths = {
                "contract": contract_path.relative_to(staging).as_posix(),
                "markdown": markdown_path.relative_to(staging).as_posix(),
                "word": word_path.relative_to(staging).as_posix(),
                "pdf": pdf_path.relative_to(staging).as_posix(),
                "candidate": candidate_path.relative_to(staging).as_posix(),
            }
            item = BatchItem(
                scenario=scenario,
                status="PASS",
                report_number=contract.report_number,
                contract_path=relative_paths["contract"],
                markdown_path=relative_paths["markdown"],
                word_path=relative_paths["word"],
                pdf_path=relative_paths["pdf"],
                candidate_path=relative_paths["candidate"],
                hashes={label: _sha(staging / path) for label, path in relative_paths.items()},
                candidate_count=len(candidates.candidates),
            )
        except Exception as exc:
            item = BatchItem(
                scenario=scenario,
                status="FAIL",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        completed_items.append(item)
        manifest = manifest.model_copy(
            update={
                "items": completed_items + [
                    existing[item.key]
                    for item in scenarios[len(completed_items):]
                ],
                "passed_scenarios": sum(row.status == "PASS" for row in completed_items),
                "failed_scenarios": sum(row.status == "FAIL" for row in completed_items),
            }
        )
        _write_json(manifest_path, manifest)

    failed = [item for item in completed_items if item.status == "FAIL"]
    if failed:
        manifest = manifest.model_copy(
            update={
                "status": "FAIL",
                "completed_at": datetime.now(SHANGHAI).isoformat(),
                "items": completed_items,
                "passed_scenarios": len(completed_items) - len(failed),
                "failed_scenarios": len(failed),
            }
        )
        _write_json(manifest_path, manifest)
        raise BatchRunFailed(manifest, staging)

    all_candidates = []
    for item in completed_items:
        bundle = TaskCandidateBundle.model_validate_json(
            (staging / str(item.candidate_path)).read_text(encoding="utf-8")
        )
        all_candidates.extend(bundle.candidates)
    for field in ("candidate_id", "task_id", "idempotency_key"):
        values = [getattr(item, field) for item in all_candidates]
        if len(values) != len(set(values)):
            raise BatchRunError(f"聚合候选字段{field}不唯一")
    aggregate = BatchCandidateBundle(
        bundle_version="1.0.0",
        run_id=run_id,
        candidate_count=len(all_candidates),
        candidates=all_candidates,
    )
    aggregate_path = staging / "all_candidates.json"
    _write_json(aggregate_path, aggregate)
    manifest = manifest.model_copy(
        update={
            "status": "PASS",
            "completed_at": datetime.now(SHANGHAI).isoformat(),
            "items": completed_items,
            "passed_scenarios": len(completed_items),
            "failed_scenarios": 0,
            "aggregate_candidates_path": "all_candidates.json",
            "aggregate_candidates_sha256": _sha(aggregate_path),
        }
    )
    _write_json(manifest_path, manifest)
    staging.rename(final_path)
    return manifest, final_path

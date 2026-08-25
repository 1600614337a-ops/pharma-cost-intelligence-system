"""Persistent single-page competition workflow for governed mock-RPA delivery."""

from __future__ import annotations

import json
import hashlib
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from app.reporting.models import ReportContract
from app.llm import LlmSettings, enhance_task_candidates
from app.rpa import (
    JsonSubmissionLedger,
    RpaClient,
    RpaSubmissionError,
    SubmissionResult,
    TaskCandidateBundle,
    TaskCandidate,
    ReviewedTask,
    approve_candidate,
    build_task_candidates,
    RpaWorkflowError,
)
from app.rpa.client import RpaTransport

from .reports import resolve_report_artifact


CHINA_TIMEZONE = timezone(timedelta(hours=8))
CANDIDATE_FILE = "task_candidates.json"
REVIEWED_FILE = "reviewed_task.json"
SUBMISSION_FILE = "submission_result.json"


class DashboardWorkflowError(RuntimeError):
    pass


def _atomic_write_model(path: Path, model) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _validate_mock_rpa_base_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise DashboardWorkflowError("竞赛演示只允许连接本机HTTP模拟RPA根地址")
    return value.rstrip("/")


class DashboardWorkflowStore:
    """Keep report, human confirmation, and mock-RPA receipt in one traceable folder."""

    def __init__(
        self,
        output_root: str | Path,
        *,
        rpa_base_url: str = "http://127.0.0.1:8090",
        transport: RpaTransport | None = None,
    ):
        self.root = Path(output_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.rpa_base_url = _validate_mock_rpa_base_url(rpa_base_url)
        self.transport = transport
        self._lock = threading.RLock()

    def _report_info(self, report_id: str) -> tuple[Path, dict[str, object]]:
        contract_path, manifest, _media_type = resolve_report_artifact(
            self.root, report_id, "json"
        )
        return contract_path.parent, manifest

    def _report_dir(self, report_id: str) -> Path:
        return self._report_info(report_id)[0]

    @staticmethod
    def _period_candidates(result: dict, manifest: dict[str, object]) -> TaskCandidateBundle:
        meta = result.get("meta", {})
        narratives = result.get("narratives", {})
        analysis_type = meta.get("analysis_type")
        if analysis_type not in {"季度成本分析", "专题分析"}:
            raise DashboardWorkflowError("非月度报告的分析类型无效")
        finding = str(narratives.get("需关注问题") or "").strip()
        if not finding:
            raise DashboardWorkflowError("季度/专题报告缺少需关注问题，禁止形成任务")
        sources = result.get("sources") or []
        source_refs = [
            f"{item.get('path')}|{item.get('key')}"
            for item in sources
            if item.get("path")
        ]
        if not source_refs:
            raise DashboardWorkflowError("季度/专题报告缺少可追溯来源，禁止形成任务")
        product = str(meta["product"])
        month = str(meta["month"])
        period = str(meta.get("period", month))
        topic = str(meta.get("topic") or analysis_type)
        suggestion = (
            "核验主要材料的实际采购价、采购量和批次耗用，并在数据齐备后复核价格差与用量差。"
            if topic == "原材料涨价专项"
            else "复核成本结构、批量和费用分摊明细；对相反证据保留说明，不将数值差异直接归因于单一事件。"
        )
        result_hash = hashlib.sha256(
            json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest().upper()
        core = {
            "report_number": meta["report_number"], "analysis_type": analysis_type,
            "period": period, "product": product, "finding": finding,
            "suggestion": suggestion,
        }
        idempotency_key = hashlib.sha256(
            json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        rate = result.get("kpis", {}).get("unit_cost", {}).get("change_rate_pct")
        priority = "high" if rate is not None and abs(float(rate)) > 10 else "medium"
        candidate = TaskCandidate(
            candidate_version="1.1.0",
            candidate_id=f"CAND-{idempotency_key[:16].upper()}",
            idempotency_key=idempotency_key,
            task_id=f"TASK-{month.replace('-', '')}-001",
            report_number=str(meta["report_number"]),
            report_contract_sha256=result_hash,
            analysis_month=month,
            analysis_type=analysis_type,
            analysis_period=period,
            product=product,
            source_field="需关注问题",
            source_refs=source_refs,
            task_title=f"{product}{topic}复核",
            finding=finding,
            suggestion=suggestion,
            suggested_priority=priority,
            suggested_department="采购部" if topic == "原材料涨价专项" else "财务部",
            created_at=datetime.now(CHINA_TIMEZONE),
        )
        return TaskCandidateBundle(
            bundle_version="1.1.0",
            report_number=str(meta["report_number"]),
            report_contract_sha256=result_hash,
            candidates=[candidate],
        )

    @staticmethod
    def _load_model(path: Path, model_type):
        if not path.is_file():
            raise FileNotFoundError(f"流程文件不存在：{path.name}")
        try:
            return model_type.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DashboardWorkflowError(f"流程文件损坏：{path.name}") from exc

    def create_candidate(self, report_id: str, *, use_llm: bool | None = None) -> dict:
        with self._lock:
            report_dir, manifest = self._report_info(report_id)
            candidate_path = report_dir / CANDIDATE_FILE
            if not candidate_path.exists():
                try:
                    contract = self._load_model(report_dir / "report.json", ReportContract)
                except DashboardWorkflowError:
                    # Compatibility path for reports generated before the unified
                    # monthly/quarterly/special ReportContract was introduced.
                    if manifest.get("analysis_type", "月度成本分析") == "月度成本分析":
                        raise
                    try:
                        result = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError) as exc:
                        raise DashboardWorkflowError("季度/专题报告JSON损坏") from exc
                    bundle = self._period_candidates(result, manifest)
                else:
                    try:
                        bundle = build_task_candidates(contract)
                    except RpaWorkflowError as exc:
                        raise DashboardWorkflowError(str(exc)) from exc
                    should_use_llm = (
                        contract.generation.status != "not_requested"
                        if use_llm is None
                        else use_llm
                    )
                    if should_use_llm:
                        bundle = enhance_task_candidates(
                            contract,
                            bundle,
                            LlmSettings.from_env(force_enabled=True),
                        )
                if len(bundle.candidates) != 1:
                    raise DashboardWorkflowError("单页演示流程要求报告生成且仅生成一个整改候选")
                _atomic_write_model(candidate_path, bundle)
            return self.get_state(report_id)

    def get_state(self, report_id: str) -> dict:
        with self._lock:
            report_dir = self._report_dir(report_id)
            candidate_path = report_dir / CANDIDATE_FILE
            if not candidate_path.is_file():
                raise FileNotFoundError("尚未形成整改任务候选")
            bundle = self._load_model(candidate_path, TaskCandidateBundle)
            candidate = bundle.candidates[0]

            reviewed = None
            reviewed_path = report_dir / REVIEWED_FILE
            if reviewed_path.is_file():
                reviewed = self._load_model(reviewed_path, ReviewedTask)
                if reviewed.candidate.candidate_id != candidate.candidate_id:
                    raise DashboardWorkflowError("人工确认记录与任务候选不一致")

            submission = None
            submission_path = report_dir / SUBMISSION_FILE
            if submission_path.is_file():
                submission = self._load_model(submission_path, SubmissionResult)
                if submission.idempotency_key != candidate.idempotency_key:
                    raise DashboardWorkflowError("RPA回执与任务候选不一致")

            if submission is not None:
                state = submission.state
            elif reviewed is not None:
                state = reviewed.review.decision
            else:
                state = "pending_review"
            return {
                "report_id": report_id,
                "state": state,
                "candidate": candidate.model_dump(mode="json"),
                "generation": bundle.generation.model_dump(mode="json"),
                "review": reviewed.review.model_dump(mode="json") if reviewed else None,
                "payload": reviewed.payload.model_dump(mode="json") if reviewed and reviewed.payload else None,
                "submission": submission.model_dump(mode="json") if submission else None,
            }

    def tracking_stats(self) -> dict[str, object]:
        """Summarize locally persisted candidate, review, and delivery states."""

        with self._lock:
            generated = confirmed = delivered = failed = 0
            for report_dir in self.root.iterdir():
                if not report_dir.is_dir():
                    continue
                candidate_path = report_dir / CANDIDATE_FILE
                if not candidate_path.is_file():
                    continue
                bundle = self._load_model(candidate_path, TaskCandidateBundle)
                generated += len(bundle.candidates)
                reviewed_path = report_dir / REVIEWED_FILE
                if reviewed_path.is_file():
                    self._load_model(reviewed_path, ReviewedTask)
                    confirmed += 1
                submission_path = report_dir / SUBMISSION_FILE
                if submission_path.is_file():
                    result = self._load_model(submission_path, SubmissionResult)
                    if result.state in {"sent", "duplicate_local", "duplicate_remote"}:
                        delivered += 1
                    elif result.state == "failed":
                        failed += 1
            return {
                "generated_count": generated,
                "confirmed_count": confirmed,
                "delivered_count": delivered,
                "failed_count": failed,
                "source": "本地报告流程文件与模拟RPA回执",
            }

    def approve(
        self,
        report_id: str,
        *,
        candidate_id: str,
        reviewer: str,
        assignee_name: str,
        department: str,
        role: str | None,
        deadline: date,
        priority: str,
        notify_method: str,
        comment: str | None,
        confirmation: str,
    ) -> dict:
        if confirmation != "CONFIRM":
            raise DashboardWorkflowError("人工确认值必须为CONFIRM")
        with self._lock:
            report_dir = self._report_dir(report_id)
            if (report_dir / REVIEWED_FILE).exists():
                raise DashboardWorkflowError("该任务已经完成人工确认，不能重复审批")
            bundle = self._load_model(report_dir / CANDIDATE_FILE, TaskCandidateBundle)
            candidate = next(
                (item for item in bundle.candidates if item.candidate_id == candidate_id),
                None,
            )
            if candidate is None:
                raise DashboardWorkflowError("任务候选ID与报告不一致")
            try:
                reviewed = approve_candidate(
                    candidate,
                    reviewer=reviewer,
                    decided_at=datetime.now(CHINA_TIMEZONE),
                    assignee_name=assignee_name,
                    department=department,
                    role=role,
                    deadline=deadline,
                    priority=priority,
                    notify_method=notify_method,
                    comment=comment,
                )
            except RpaWorkflowError as exc:
                raise DashboardWorkflowError(str(exc)) from exc
            _atomic_write_model(report_dir / REVIEWED_FILE, reviewed)
            return self.get_state(report_id)

    def submit(self, report_id: str, *, confirmation: str) -> dict:
        if confirmation != "SUBMIT":
            raise DashboardWorkflowError("模拟提交确认值必须为SUBMIT")
        with self._lock:
            report_dir = self._report_dir(report_id)
            submission_path = report_dir / SUBMISSION_FILE
            if submission_path.exists():
                previous = self._load_model(submission_path, SubmissionResult)
                if previous.state != "failed":
                    return self.get_state(report_id)
            reviewed = self._load_model(report_dir / REVIEWED_FILE, ReviewedTask)
            client = RpaClient(
                base_url=self.rpa_base_url,
                mode="execute",
                transport=self.transport,
                ledger=JsonSubmissionLedger(self.root / "rpa_submission_ledger.json"),
                timeout_seconds=10,
                max_attempts=2,
                retry_delay_seconds=0.1,
            )
            try:
                result = client.submit(reviewed)
            except RpaSubmissionError as exc:
                raise DashboardWorkflowError(str(exc)) from exc
            _atomic_write_model(submission_path, result)
            return self.get_state(report_id)

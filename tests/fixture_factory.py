"""Build deterministic test fixtures without depending on runtime output folders."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from app.reporting import build_report_contract
from app.reporting.models import ReportContract
from app.rpa import build_task_candidates
from app.rpa.models import TaskCandidateBundle


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def report_contract() -> ReportContract:
    return build_report_contract(
        PROJECT_ROOT,
        PROJECT_ROOT / "06_知识证据索引",
        "银黄口服液",
        "2026-05",
        generated_date="2026-08-03",
    )


@lru_cache(maxsize=1)
def candidate_bundle() -> TaskCandidateBundle:
    return build_task_candidates(report_contract())


def write_report_contract(path: Path) -> Path:
    path.write_text(report_contract().model_dump_json(indent=2), encoding="utf-8")
    return path


def write_candidate_bundle(path: Path) -> Path:
    path.write_text(candidate_bundle().model_dump_json(indent=2), encoding="utf-8")
    return path

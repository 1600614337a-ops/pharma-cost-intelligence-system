"""CLI for preparing, reviewing, and optionally submitting governed RPA tasks."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

from app.reporting.models import ReportContract

from . import (
    JsonSubmissionLedger,
    ReviewedTask,
    RpaClient,
    TaskCandidateBundle,
    approve_candidate,
    build_task_candidates,
    reject_candidate,
)


def _write_model(model, path: str | Path) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    return output


def _load_candidate(path: str | Path, candidate_id: str | None):
    bundle = TaskCandidateBundle.model_validate_json(Path(path).read_text(encoding="utf-8"))
    candidates = bundle.candidates
    if candidate_id:
        candidates = [item for item in candidates if item.candidate_id == candidate_id]
    if len(candidates) != 1:
        raise ValueError("必须通过--candidate-id唯一选择一个任务候选")
    return candidates[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="受控RPA任务候选、审批和提交工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="从PASS报告契约生成待审批任务候选")
    prepare.add_argument("--report-json", required=True)
    prepare.add_argument("--output", required=True)

    approve = subparsers.add_parser("approve", help="记录人工审批并形成接口载荷")
    approve.add_argument("--candidates", required=True)
    approve.add_argument("--candidate-id")
    approve.add_argument("--reviewer", required=True)
    approve.add_argument("--decided-at", required=True)
    approve.add_argument("--assignee", required=True)
    approve.add_argument("--department", required=True)
    approve.add_argument("--role")
    approve.add_argument("--deadline", required=True)
    approve.add_argument("--priority", choices=("high", "medium", "low"), required=True)
    approve.add_argument("--notify-method", choices=("wechat", "email", "sms"), required=True)
    approve.add_argument("--comment")
    approve.add_argument("--output", required=True)

    reject = subparsers.add_parser("reject", help="记录人工拒绝")
    reject.add_argument("--candidates", required=True)
    reject.add_argument("--candidate-id")
    reject.add_argument("--reviewer", required=True)
    reject.add_argument("--decided-at", required=True)
    reject.add_argument("--comment", required=True)
    reject.add_argument("--output", required=True)

    submit = subparsers.add_parser("submit", help="默认仅dry-run；--execute才调用接口")
    submit.add_argument("--reviewed-task", required=True)
    submit.add_argument("--base-url", default="http://localhost:8090")
    submit.add_argument("--ledger", default="08_RPA任务输出/submission_ledger.json")
    submit.add_argument("--execute", action="store_true")
    submit.add_argument("--output", required=True)

    args = parser.parse_args()
    if args.command == "prepare":
        contract = ReportContract.model_validate_json(Path(args.report_json).read_text(encoding="utf-8"))
        output = _write_model(build_task_candidates(contract), args.output)
        print(f"任务候选：{output.resolve()}")
        print("状态：pending_review；未调用外部RPA接口")
        return 0
    if args.command == "approve":
        candidate = _load_candidate(args.candidates, args.candidate_id)
        reviewed = approve_candidate(
            candidate,
            reviewer=args.reviewer,
            decided_at=datetime.fromisoformat(args.decided_at),
            assignee_name=args.assignee,
            department=args.department,
            role=args.role,
            deadline=date.fromisoformat(args.deadline),
            priority=args.priority,
            notify_method=args.notify_method,
            comment=args.comment,
        )
        output = _write_model(reviewed, args.output)
        print(f"审批记录：{output.resolve()}")
        print("状态：approved；尚未提交")
        return 0
    if args.command == "reject":
        candidate = _load_candidate(args.candidates, args.candidate_id)
        reviewed = reject_candidate(
            candidate,
            reviewer=args.reviewer,
            decided_at=datetime.fromisoformat(args.decided_at),
            comment=args.comment,
        )
        output = _write_model(reviewed, args.output)
        print(f"审批记录：{output.resolve()}")
        print("状态：rejected；不可提交")
        return 0

    reviewed = ReviewedTask.model_validate_json(Path(args.reviewed_task).read_text(encoding="utf-8"))
    client = RpaClient(
        base_url=args.base_url,
        mode="execute" if args.execute else "dry_run",
        ledger=JsonSubmissionLedger(args.ledger),
    )
    result = client.submit(reviewed)
    output = _write_model(result, args.output)
    print(f"提交结果：{output.resolve()}")
    print(f"状态：{result.state}")
    return 0 if result.state in {"dry_run", "sent", "duplicate_local", "duplicate_remote"} else 2


if __name__ == "__main__":
    raise SystemExit(main())


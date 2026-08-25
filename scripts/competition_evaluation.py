"""Reproducible automated checks for the three required competition scenarios."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.reporting import build_report_contract
from app.rpa import JsonSubmissionLedger, RpaClient, approve_candidate, build_task_candidates


SCENARIOS = (
    {
        "name": "银黄口服液 2026-05",
        "product": "银黄口服液",
        "month": "2026-05",
        "expected": {
            "本月单位成本": "11.21",
            "上月单位成本": "10.90",
            "单位成本环比": "2.84%",
            "材料贡献度": "67.09%",
            "单位成本同比": "4.67%",
            "单位成本预算偏差": "5.75%",
            "本月工时": "231.72",
        },
        "benchmark_difference": "-0.39",
        "required_terms": ("金银花", "6.15%", "市场行情不等于企业实际采购价"),
    },
    {
        "name": "板蓝根颗粒 2026-05",
        "product": "板蓝根颗粒",
        "month": "2026-05",
        "expected": {
            "本月单位成本": "7.47",
            "上月单位成本": "7.24",
            "单位成本环比": "3.18%",
            "材料贡献度": "69.90%",
            "单位成本同比": "5.21%",
            "单位成本预算偏差": "6.71%",
            "本月工时": "164.71",
        },
        "benchmark_difference": "-0.50",
        "required_terms": ("板蓝根", "8.47%", "市场行情不等于企业实际采购价"),
    },
    {
        "name": "六味地黄胶囊 2026-03",
        "product": "六味地黄胶囊",
        "month": "2026-03",
        "expected": {
            "本月单位成本": "17.02",
            "上月单位成本": "17.60",
            "单位成本环比": "-3.30%",
            "产量环比": "25.00%",
            "单位成本同比": "0.59%",
            "单位成本预算偏差": "-0.47%",
            "本月工时": "603.43",
        },
        "benchmark_difference": "-1.16",
        "required_terms": ("单位成本实际下降", "胶囊填充机故障", "不量化其单位成本影响"),
    },
)

FORBIDDEN = (
    "采购价环比上涨12%",
    "板蓝根上涨10%",
    "设备故障导致单位成本上涨",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="执行三个赛题场景的可复现评测")
    parser.add_argument("--project-root", default=".")
    parser.add_argument(
        "--output",
        default="output/evaluation/三场景自动评测结果.json",
    )
    parser.add_argument("--execute-rpa", action="store_true")
    parser.add_argument("--rpa-base-url", default="http://127.0.0.1:8090")
    return parser


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def main() -> int:
    args = _parser().parse_args()
    root = Path(args.project_root).resolve()
    output = Path(args.output)
    if not output.is_absolute():
        output = root / output
    now = datetime.now(timezone(timedelta(hours=8)))
    results: list[dict] = []
    rpa_states: list[str] = []
    ledger = JsonSubmissionLedger(output.with_suffix(".rpa-ledger.json"))

    for scenario in SCENARIOS:
        contract = build_report_contract(
            root,
            root / "06_知识证据索引",
            scenario["product"],
            scenario["month"],
            generated_date=date.today().isoformat(),
        )
        checks: list[dict[str, object]] = []

        def record(name: str, passed: bool, actual: object, expected: object) -> None:
            checks.append(
                {
                    "name": name,
                    "status": "PASS" if passed else "FAIL",
                    "actual": actual,
                    "expected": expected,
                }
            )

        record("报告契约状态", contract.validation_status == "PASS", contract.validation_status, "PASS")
        record("报告字段数量", len(contract.fields) == 107, len(contract.fields), 107)
        record("动态表格数量", len(contract.dynamic_tables) == 6, len(contract.dynamic_tables), 6)
        for field, expected in scenario["expected"].items():
            actual = contract.fields[field].value
            record(field, actual == expected, actual, expected)

        benchmark_row = next(
            row
            for row in contract.dynamic_tables["对标差异表格"].rows
            if row[0] == "单位成本"
        )
        record(
            "一厂减二厂单位成本差异",
            benchmark_row[3] == scenario["benchmark_difference"],
            benchmark_row[3],
            scenario["benchmark_difference"],
        )

        narrative = "\n".join(
            contract.fields[name].value
            for name in (
                "材料成本归因分析文本",
                "成本异常排查分析",
                "需关注问题",
            )
        )
        for term in scenario["required_terms"]:
            record(f"必需归因边界：{term}", term in narrative, term in narrative, True)
        for phrase in FORBIDDEN:
            record(f"禁止结论：{phrase}", phrase not in narrative, phrase in narrative, False)
        structured_metrics_ok = all(
            contract.fields[name].status == "available"
            and contract.fields[name].value != "暂无数据"
            for name in (
                "单位成本同比", "单位成本预算偏差", "本月工时",
                "本月时薪", "本月效率", "材料预算偏差",
            )
        )
        record("V1.1新增结构化指标可用", structured_metrics_ok, structured_metrics_ok, True)
        citations = contract.evidence.model_dump()
        citation_count = sum(bool(value) for value in citations.values())
        record("八类受治理证据引用完整", citation_count == 8, citation_count, 8)

        bundle = build_task_candidates(contract)
        record("整改候选数量", len(bundle.candidates) == 1, len(bundle.candidates), 1)
        record(
            "候选来源可追溯",
            bool(bundle.candidates[0].source_refs),
            len(bundle.candidates[0].source_refs),
            "至少1条",
        )

        rpa_result = None
        if args.execute_rpa:
            candidate = bundle.candidates[0]
            reviewed = approve_candidate(
                candidate,
                reviewer="竞赛评测审批员",
                decided_at=now,
                assignee_name="竞赛评测责任人",
                department="采购部",
                role="评测专用账号",
                deadline=now.date() + timedelta(days=7),
                priority=candidate.suggested_priority,
                notify_method="wechat",
                comment="三场景端到端评测",
            )
            rpa_result = RpaClient(
                base_url=args.rpa_base_url,
                mode="execute",
                ledger=ledger,
                timeout_seconds=10,
                max_attempts=2,
                retry_delay_seconds=0.1,
            ).submit(reviewed)
            rpa_states.append(rpa_result.state)
            record(
                "模拟RPA触发",
                rpa_result.state in {"sent", "duplicate_local", "duplicate_remote"},
                rpa_result.state,
                "sent或幂等拦截",
            )

        status = "PASS" if all(item["status"] == "PASS" for item in checks) else "FAIL"
        results.append(
            {
                "scenario": scenario["name"],
                "report_number": contract.report_number,
                "status": status,
                "checks": checks,
                "rpa_result": rpa_result.model_dump(mode="json") if rpa_result else None,
            }
        )

    passed = sum(item["status"] == "PASS" for item in results)
    payload = {
        "evaluation_version": "1.3.0",
        "generated_at": now.isoformat(),
        "status": "PASS" if passed == len(results) else "FAIL",
        "summary": {
            "scenario_count": len(results),
            "passed_scenarios": passed,
            "automated_scenario_pass_rate_pct": f"{passed / len(results) * 100:.2f}",
            "rpa_executed": args.execute_rpa,
            "rpa_success_count": sum(
                state in {"sent", "duplicate_local", "duplicate_remote"}
                for state in rpa_states
            ),
            "rpa_trigger_success_rate_pct": (
                f"{sum(state in {'sent', 'duplicate_local', 'duplicate_remote'} for state in rpa_states) / len(rpa_states) * 100:.2f}"
                if rpa_states
                else "暂无数据"
            ),
        },
        "scenarios": results,
    }
    _write_json(output, payload)
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(f"评测结果：{payload['status']} · {output}")
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

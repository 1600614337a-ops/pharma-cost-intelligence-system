"""Run live-model acceptance checks for the three competition golden scenarios."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.evaluation import GOLDEN_LLM_SCENARIOS, evaluate_llm_contract
from app.llm import LlmSettings, enhance_report_contract
from app.reporting import build_report_contract


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="执行三个黄金场景的真实大模型自动验收")
    parser.add_argument("--project-root", default=".")
    parser.add_argument(
        "--output-json",
        default="output/evaluation/三场景大模型自动验收结果.json",
    )
    parser.add_argument(
        "--output-csv",
        default="output/evaluation/三场景大模型自动验收明细.csv",
    )
    parser.add_argument("--repeats", type=int, default=1, choices=range(1, 6))
    parser.add_argument("--expected-model", default="qwen3.8-max")
    parser.add_argument(
        "--live-llm",
        action="store_true",
        help="显式允许调用已配置的大模型接口；可能产生少量API费用",
    )
    return parser


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _atomic_csv(path: Path, runs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=(
                "场景", "运行序号", "场景状态", "检查项", "检查状态", "实际值", "期望值",
            ),
        )
        writer.writeheader()
        for run in runs:
            for check in run["checks"]:
                writer.writerow(
                    {
                        "场景": run["scenario"],
                        "运行序号": run["run_index"],
                        "场景状态": run["status"],
                        "检查项": check["name"],
                        "检查状态": check["status"],
                        "实际值": json.dumps(check["actual"], ensure_ascii=False),
                        "期望值": json.dumps(check["expected"], ensure_ascii=False),
                    }
                )
    temporary.replace(path)


def main() -> int:
    args = _parser().parse_args()
    if not args.live_llm:
        print("未执行：必须显式传入 --live-llm 才允许调用外部模型。", file=sys.stderr)
        return 2

    root = Path(args.project_root).resolve()
    output_json = _resolve(root, args.output_json)
    output_csv = _resolve(root, args.output_csv)
    settings = LlmSettings.from_env(force_enabled=True)
    if settings.readiness_issue:
        print(f"大模型配置不可用：{settings.readiness_issue}", file=sys.stderr)
        return 2

    started = datetime.now(timezone(timedelta(hours=8)))
    runs: list[dict] = []
    for scenario in GOLDEN_LLM_SCENARIOS:
        baseline = build_report_contract(
            root,
            root / "06_知识证据索引",
            scenario["product"],
            scenario["month"],
            generated_date=date.today().isoformat(),
        )
        for run_index in range(1, args.repeats + 1):
            enhanced = enhance_report_contract(baseline, settings)
            result = evaluate_llm_contract(
                baseline,
                enhanced,
                scenario,
                run_index=run_index,
                expected_model=args.expected_model,
            )
            runs.append(result)
            print(
                f"[{result['status']}] {result['scenario']} · 第{run_index}次 · "
                f"{result['generation']['status']}"
            )

    scenario_results: list[dict] = []
    for scenario in GOLDEN_LLM_SCENARIOS:
        scenario_runs = [item for item in runs if item["scenario"] == scenario["name"]]
        protected = {item["protected_fingerprint"] for item in scenario_runs}
        failed_checks = sum(
            check["status"] == "FAIL"
            for item in scenario_runs
            for check in item["checks"]
        )
        scenario_results.append(
            {
                "scenario": scenario["name"],
                "status": (
                    "PASS"
                    if all(item["status"] == "PASS" for item in scenario_runs)
                    and len(protected) == 1
                    else "FAIL"
                ),
                "run_count": len(scenario_runs),
                "failed_check_count": failed_checks,
                "protected_output_stable": len(protected) == 1,
                "narrative_variant_count": len(
                    {item["narrative_fingerprint"] for item in scenario_runs}
                ),
            }
        )

    passed_runs = sum(item["status"] == "PASS" for item in runs)
    passed_scenarios = sum(item["status"] == "PASS" for item in scenario_results)
    status = "PASS" if passed_runs == len(runs) and passed_scenarios == 3 else "FAIL"
    payload = {
        "evaluation_version": "1.0.0",
        "generated_at": started.isoformat(),
        "status": status,
        "model": {
            "provider": settings.provider,
            "protocol": settings.api_style,
            "base_url": settings.base_url,
            "model": settings.model,
        },
        "summary": {
            "scenario_count": len(scenario_results),
            "run_count": len(runs),
            "passed_scenarios": passed_scenarios,
            "passed_runs": passed_runs,
            "scenario_pass_rate_pct": f"{passed_scenarios / 3 * 100:.2f}",
            "run_pass_rate_pct": f"{passed_runs / len(runs) * 100:.2f}",
        },
        "scenario_results": scenario_results,
        "runs": runs,
    }
    _atomic_json(output_json, payload)
    _atomic_csv(output_csv, runs)
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(f"验收结果：{status} · {output_json}")
    print(f"验收明细：{output_csv}")
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

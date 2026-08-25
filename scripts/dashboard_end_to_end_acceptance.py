"""Exercise the live dashboard, report, approval, and mock-RPA chain over HTTP."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


SCENARIOS = (
    {
        "name": "月度",
        "request": {
            "analysis_type": "月度成本分析",
            "product": "银黄口服液",
            "month": "2026-05",
        },
        "workflow_llm": True,
    },
    {
        "name": "季度",
        "request": {
            "analysis_type": "季度成本分析",
            "product": "板蓝根颗粒",
            "quarter": "2026-Q2",
        },
        "workflow_llm": False,
    },
    {
        "name": "专题",
        "request": {
            "analysis_type": "专题分析",
            "product": "六味地黄胶囊",
            "month": "2026-03",
            "topic": "工厂成本差异专项",
        },
        "workflow_llm": False,
    },
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="执行真实Web端到端验收")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument(
        "--output",
        default="output/evaluation/端到端验收结果_20260824.json",
    )
    parser.add_argument("--require-live-llm", action="store_true")
    return parser.parse_args()


def request(base_url: str, method: str, path: str, payload: dict | None = None):
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    target = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    req = Request(target, data=body, method=method, headers=headers)
    try:
        with urlopen(req, timeout=300) as response:
            content = response.read()
            return response.status, dict(response.headers), content
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"{method} {path} -> {exc.reason}") from exc


def request_json(base_url: str, method: str, path: str, payload: dict | None = None) -> dict:
    status, _headers, content = request(base_url, method, path, payload)
    if status < 200 or status >= 300:
        raise RuntimeError(f"{method} {path} -> HTTP {status}")
    return json.loads(content.decode("utf-8"))


def add_check(checks: list[dict], name: str, passed: bool, actual, expected) -> None:
    checks.append(
        {
            "name": name,
            "status": "PASS" if passed else "FAIL",
            "actual": actual,
            "expected": expected,
        }
    )


def header_value(headers: dict[str, str], name: str) -> str:
    expected = name.casefold()
    return next(
        (str(value) for key, value in headers.items() if key.casefold() == expected),
        "",
    )


def main() -> int:
    args = parse_args()
    checks: list[dict] = []
    scenario_results: list[dict] = []

    health = request_json(args.base_url, "GET", "/health")
    add_check(checks, "Web健康状态", health.get("status") == "ok", health.get("status"), "ok")
    baseline = request_json(args.base_url, "GET", "/api/workflow-stats")
    expected_zero = {
        "generated_count": 0,
        "confirmed_count": 0,
        "delivered_count": 0,
        "failed_count": 0,
    }
    add_check(
        checks,
        "正式流程从零开始",
        all(baseline.get(key) == value for key, value in expected_zero.items()),
        {key: baseline.get(key) for key in expected_zero},
        expected_zero,
    )

    for index, scenario in enumerate(SCENARIOS, start=1):
        request_payload = dict(scenario["request"])
        analysis = request_json(args.base_url, "POST", "/api/analyze", request_payload)
        report = request_json(
            args.base_url,
            "POST",
            "/api/reports",
            {**request_payload, "use_llm": True},
        )
        report_id = report["report_id"]
        generation = report["generation"]

        _pdf_status, pdf_headers, pdf = request(
            args.base_url, "GET", report["preview_url"]
        )
        _word_status, word_headers, word = request(
            args.base_url, "GET", report["downloads"]["docx"]
        )
        workflow = request_json(
            args.base_url,
            "POST",
            "/api/workflows",
            {"report_id": report_id, "use_llm": scenario["workflow_llm"]},
        )
        candidate = workflow["candidate"]
        approval = request_json(
            args.base_url,
            "POST",
            f"/api/workflows/{report_id}/approve",
            {
                "candidate_id": candidate["candidate_id"],
                "reviewer": "端到端验收审批员",
                "assignee_name": "端到端验收责任人",
                "department": candidate["suggested_department"],
                "role": "验收专员",
                "deadline": (date.today() + timedelta(days=7)).isoformat(),
                "priority": candidate["suggested_priority"],
                "notify_method": "wechat",
                "comment": "2026-08-24端到端验收",
                "confirmation": "CONFIRM",
            },
        )
        submission = request_json(
            args.base_url,
            "POST",
            f"/api/workflows/{report_id}/submit",
            {"confirmation": "SUBMIT"},
        )

        scenario_checks: list[dict] = []
        add_check(
            scenario_checks,
            "分析类型",
            analysis["meta"]["analysis_type"] == request_payload["analysis_type"],
            analysis["meta"]["analysis_type"],
            request_payload["analysis_type"],
        )
        add_check(
            scenario_checks,
            "报告受控大模型状态",
            generation.get("status") == "generated" if args.require_live_llm else generation.get("status") in {"generated", "fallback"},
            generation,
            "generated" if args.require_live_llm else "generated或安全回退",
        )
        add_check(scenario_checks, "PDF有效", pdf.startswith(b"%PDF") and len(pdf) > 10_000, len(pdf), ">10000字节")
        add_check(scenario_checks, "Word有效", word.startswith(b"PK") and len(word) > 10_000, len(word), ">10000字节")
        add_check(
            scenario_checks,
            "报告下载类型",
            "application/pdf" in header_value(pdf_headers, "Content-Type")
            and "wordprocessingml" in header_value(word_headers, "Content-Type"),
            {"pdf": header_value(pdf_headers, "Content-Type"), "docx": header_value(word_headers, "Content-Type")},
            "PDF与DOCX",
        )
        add_check(scenario_checks, "候选校验", candidate["validation_status"] == "PASS", candidate["validation_status"], "PASS")
        add_check(scenario_checks, "人工确认", approval["state"] == "approved", approval["state"], "approved")
        delivery_state = submission["state"]
        add_check(
            scenario_checks,
            "模拟送达",
            delivery_state in {"sent", "duplicate_remote"},
            delivery_state,
            "sent 或 duplicate_remote（远端幂等送达）",
        )

        scenario_results.append(
            {
                "sequence": index,
                "name": scenario["name"],
                "request": request_payload,
                "report_id": report_id,
                "report_number": report["report_number"],
                "generation": generation,
                "workflow_generation": workflow["generation"],
                "checks": scenario_checks,
                "status": "PASS" if all(item["status"] == "PASS" for item in scenario_checks) else "FAIL",
            }
        )

    final_stats = request_json(args.base_url, "GET", "/api/workflow-stats")
    expected_final = {
        "generated_count": len(SCENARIOS),
        "confirmed_count": len(SCENARIOS),
        "delivered_count": len(SCENARIOS),
        "failed_count": 0,
    }
    add_check(
        checks,
        "整改统计按流程递增",
        all(final_stats.get(key) == value for key, value in expected_final.items()),
        {key: final_stats.get(key) for key in expected_final},
        expected_final,
    )

    passed = all(item["status"] == "PASS" for item in checks) and all(
        item["status"] == "PASS" for item in scenario_results
    )
    result = {
        "acceptance_version": "1.0.0",
        "status": "PASS" if passed else "FAIL",
        "base_url": args.base_url,
        "checks": checks,
        "scenarios": scenario_results,
        "final_stats": final_stats,
    }
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    temporary.replace(output)

    print(json.dumps({"status": result["status"], "scenarios": [item["status"] for item in scenario_results], "final_stats": final_stats}, ensure_ascii=False, indent=2))
    print(f"验收结果：{output}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

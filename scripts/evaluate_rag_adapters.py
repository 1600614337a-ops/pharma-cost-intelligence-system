"""Evaluate exact result parity between native and LlamaIndex retrieval."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.knowledge import compare_native_and_llamaindex, llamaindex_available


CASES = (
    {
        "query": "银黄口服液 提取收率 0.08元/盒",
        "product": "银黄口服液",
        "document_types": ["工艺"],
        "regulatory_claim": False,
    },
    {
        "query": "板蓝根颗粒 提取收率 0.03元/盒",
        "product": "板蓝根颗粒",
        "document_types": ["工艺"],
        "regulatory_claim": False,
    },
    {
        "query": "六味地黄胶囊 胶囊填充机 NJP-3200 维修费 8500 停工 24小时",
        "product": "六味地黄胶囊",
        "document_types": ["设备"],
        "regulatory_claim": False,
    },
    {
        "query": "生产全过程 记录 偏差 调查 批记录 追溯",
        "product": None,
        "document_types": None,
        "regulatory_claim": True,
    },
)


def main() -> int:
    parser = argparse.ArgumentParser(description="执行RAG框架适配A/B评测")
    parser.add_argument("--project-root", default=".")
    parser.add_argument(
        "--output",
        default="output/evaluation/RAG框架适配评测结果.json",
    )
    args = parser.parse_args()
    root = Path(args.project_root).resolve()
    output = Path(args.output)
    if not output.is_absolute():
        output = root / output
    now = datetime.now(timezone(timedelta(hours=8)))

    if not llamaindex_available():
        payload = {
            "evaluation_version": "1.0.0",
            "generated_at": now.isoformat(),
            "status": "NOT_INSTALLED",
            "framework": "LlamaIndex",
            "message": "安装requirements-llamaindex.txt后执行评测；默认原生RAG不受影响",
            "cases": [],
        }
    else:
        cases = [
            compare_native_and_llamaindex(
                root / "06_知识证据索引",
                top_k=3,
                **case,
            )
            for case in CASES
        ]
        payload = {
            "evaluation_version": "1.0.0",
            "generated_at": now.isoformat(),
            "status": "PASS" if all(case["identical"] for case in cases) else "FAIL",
            "framework": "LlamaIndex",
            "integration_mode": "governed-native-retrieval-adapter",
            "ranking_changed": False if all(case["identical"] for case in cases) else True,
            "case_count": len(cases),
            "identical_count": sum(case["identical"] for case in cases),
            "cases": cases,
        }

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(output)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

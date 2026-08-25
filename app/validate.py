"""Command-line entry point for source-data quality validation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .data_quality import validate_data_dir


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="校验成本智能分析系统的六类源CSV，不修改原始文件。"
    )
    parser.add_argument(
        "--data-dir",
        default=".",
        help="项目数据根目录，默认当前目录。",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以JSON格式输出完整验证报告。",
    )
    return parser


def _print_human_report(report) -> None:
    print(f"数据批次状态：{report.status}")
    print(f"数据目录：{report.source_root}")
    print()
    print("文件检查：")
    for name, summary in report.files.items():
        print(
            f"- {name}: 行数={summary.row_count}, "
            f"有效={summary.valid_row_count}, 无效={summary.invalid_row_count}, "
            f"UTF-8 BOM={'是' if summary.utf8_bom else '否'}"
        )
    print()
    print("标准化记录数：")
    for name, count in report.normalized_counts.items():
        print(f"- {name}: {count}")
    print()
    print(f"阻断错误：{len(report.errors)}")
    for issue in report.errors:
        location = f" [{issue.file or '-'}"
        if issue.row is not None:
            location += f":{issue.row}"
        location += "]"
        print(f"- {issue.code}{location} {issue.message}")
    print()
    print(f"警告：{len(report.warnings)}")
    for issue in report.warnings:
        key = f" ({issue.key})" if issue.key else ""
        print(f"- {issue.code}{key} {issue.message}")


def main() -> int:
    args = _build_parser().parse_args()
    report = validate_data_dir(Path(args.data_dir))
    if args.json:
        print(
            json.dumps(
                report.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        _print_human_report(report)
    return 1 if report.status == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())


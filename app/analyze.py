"""Command-line entry point for deterministic monthly cost analysis."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from .analysis import AnalysisError, analyze_cost


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="在数据质量门禁通过后执行确定性月度成本分析。"
    )
    parser.add_argument("--data-dir", default=".", help="项目数据根目录，默认当前目录。")
    parser.add_argument("--product", required=True, help="产品名称。")
    parser.add_argument("--month", required=True, help="分析月份，格式YYYY-MM。")
    parser.add_argument("--factory", default="中药一厂", help="分析工厂。")
    parser.add_argument(
        "--benchmark-factory", default="中药二厂", help="同期对标工厂。"
    )
    parser.add_argument("--json", action="store_true", help="输出完整JSON结果。")
    return parser


def _number(value: Decimal | None, places: str = "0.01") -> str:
    if value is None:
        return "暂无数据"
    rounded = value.quantize(Decimal(places), rounding=ROUND_HALF_UP)
    return f"{rounded:,.{abs(Decimal(places).as_tuple().exponent)}f}"


def _print_human(result) -> None:
    request = result.request
    print(f"分析场景：{request.factory} | {request.product} | {request.month}")
    print(
        f"数据门禁：{result.data_quality_status}（警告{result.data_quality_warning_count}项）"
    )
    print(f"分析版本：{result.analysis_version}；公式版本：{result.formula_version}")
    print()
    print("汇总环比：")
    for item in result.summary:
        if item.status == "available":
            places = "1" if item.unit == "盒" else "0.01"
            print(
                f"- {item.name}: {_number(item.previous, places)} → "
                f"{_number(item.current, places)}, "
                f"变动{_number(item.delta, places)} {item.unit}, "
                f"环比{_number(item.change_rate_pct)}%"
            )
        else:
            places = "1" if item.unit == "盒" else "0.01"
            print(
                f"- {item.name}: 本月{_number(item.current, places)} {item.unit}，"
                f"环比{'不适用' if item.status == 'not_applicable' else '暂无数据'}"
                f"（{item.reason}）"
            )
    print()
    print("单位成本贡献度：")
    for item in result.contributions:
        value = (
            f"{_number(item.contribution_pct)}%"
            if item.contribution_pct is not None
            else "不适用" if item.status == "not_applicable" else "暂无数据"
        )
        print(f"- {item.name}: {value}")
    print()
    unit_benchmark = next(
        (item for item in result.factory_benchmark if item.name == "单位成本"), None
    )
    if unit_benchmark is not None:
        difference = unit_benchmark.difference
        if difference is None:
            comparison_text = "暂无数据"
        elif difference < 0:
            comparison_text = f"低{_number(abs(difference))}元/盒"
        elif difference > 0:
            comparison_text = f"高{_number(difference)}元/盒"
        else:
            comparison_text = "相同"
        print(
            f"同期对标：{request.factory}比{request.benchmark_factory}单位成本"
            f"{comparison_text}，"
            f"差异率{_number(unit_benchmark.difference_rate_pct)}%。"
        )
    print(f"成本阈值告警：{len(result.alerts)}项")
    print(f"当前不可用报告字段：{len(result.unavailable_metrics)}项，统一显示“暂无数据”。")
    print(f"禁止定量计算：{len(result.prohibited_calculations)}项。")


def main() -> int:
    args = _build_parser().parse_args()
    try:
        result = analyze_cost(
            data_dir=Path(args.data_dir),
            product=args.product,
            month=args.month,
            factory=args.factory,
            benchmark_factory=args.benchmark_factory,
        )
    except AnalysisError as exc:
        print(f"分析失败：{exc}")
        return 2
    if args.json:
        print(
            json.dumps(
                result.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        _print_human(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

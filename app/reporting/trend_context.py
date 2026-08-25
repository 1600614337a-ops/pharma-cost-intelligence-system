"""Truthful display labels for bounded monthly and quarterly trend windows."""

from __future__ import annotations

from collections.abc import Iterable

from .models import ReportContract


def build_trend_context(periods: Iterable[object], analysis_type: str) -> dict[str, object]:
    ordered = [str(period) for period in periods if str(period).strip()]
    count = len(ordered)
    start = ordered[0] if ordered else None
    end = ordered[-1] if ordered else None
    period_range = "暂无数据" if start is None else start if start == end else f"{start}至{end}"

    if analysis_type == "季度成本分析":
        title = "暂无可得季度趋势" if count == 0 else f"可得季度单位成本趋势（共{count}个季度）"
        kicker = "季度序列" if count == 0 else f"数据范围：{period_range}"
        caption = "暂无可得季度单位成本趋势图" if count == 0 else f"{period_range}单位成本趋势图（共{count}个季度）"
        boundary = (
            "数据范围：暂无可用季度数据；未补造缺失期间。"
            if count == 0
            else f"数据范围：{period_range}，共{count}个季度；仅展示截至分析期的可得企业数据，未补造缺失期间。"
        )
        complete = False
    else:
        complete = count == 6
        if complete:
            title = "单位成本趋势"
            kicker = "近 6 个月"
            report_heading = "近6个月单位成本趋势"
            caption = "近6个月单位成本趋势图"
        elif count:
            title = f"截至分析期的可得月份趋势（共{count}个月）"
            kicker = f"数据范围：{period_range}"
            report_heading = title
            caption = f"{period_range}单位成本趋势图（共{count}个月）"
        else:
            title = "暂无可得月份趋势"
            kicker = "数据范围：暂无数据"
            report_heading = title
            caption = "暂无可得月份单位成本趋势图"
        boundary = (
            "数据范围：暂无可用月份数据；未补造缺失月份。"
            if count == 0
            else f"数据范围：{period_range}，共{count}个月；仅展示截至分析期的可得企业数据，未补造分析期前月份。"
        )
        if not complete:
            report_heading = title

    if analysis_type == "季度成本分析":
        report_heading = title
    return {
        "title": title,
        "kicker": kicker,
        "report_heading": report_heading,
        "caption": caption,
        "boundary_note": boundary,
        "period_range": period_range,
        "start_period": start,
        "end_period": end,
        "point_count": count,
        "is_complete_six_months": complete,
    }


def trend_context_from_contract(contract: ReportContract) -> dict[str, object]:
    table = contract.dynamic_tables["近6个月成本趋势表格"]
    return build_trend_context((row[0] for row in table.rows if row), contract.analysis_type)

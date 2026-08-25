"""Data-driven benchmark attribution and governed recommendation candidates."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable


@dataclass(frozen=True)
class BenchmarkGap:
    name: str
    difference: Decimal | None
    difference_rate_pct: Decimal | None
    direction: str


@dataclass(frozen=True)
class BenchmarkRecommendation:
    sequence: str
    action: str
    owner: str
    priority: str
    expected_effect: str
    due: str = "待业务审批"


def _money(value: Decimal | None) -> str:
    return "暂无数据" if value is None else f"{abs(value):.2f}"


def _signed_money(value: Decimal | None) -> str:
    if value is None:
        return "暂无数据"
    return f"{value:+.2f}"


def _signed_pct(value: Decimal | None) -> str:
    if value is None:
        return "暂无数据"
    return f"{value:+.2f}%"


def _relative_word(value: Decimal | None) -> str:
    if value is None:
        return "差异暂无数据"
    if value < 0:
        return f"低{_money(value)}元/盒"
    if value > 0:
        return f"高{_money(value)}元/盒"
    return "持平"


def _outcome(direction: str) -> str:
    if direction == "favorable":
        return "经营有利"
    if direction == "unfavorable":
        return "经营不利"
    return "持平"


def _priority(gap: BenchmarkGap) -> str:
    if gap.direction == "unfavorable":
        return "高"
    if gap.difference_rate_pct is not None and abs(gap.difference_rate_pct) > Decimal("10"):
        return "高"
    return "中"


def _recommendation_for(
    gap: BenchmarkGap,
    *,
    product: str,
    top_material: str | None,
) -> tuple[str, str, str]:
    if gap.name == "直接材料":
        material = top_material or "主要原材料"
        return (
            f"复核两厂{product}{material}采购量价、批次领退料、投料耗用及工艺收率记录，核验直接材料差异",
            "采购部、生产部",
            "形成可追溯的直接材料差异证据链",
        )
    if gap.name == "直接人工":
        return (
            f"补齐并复核两厂{product}实际工时、班组配置、工资率及人工分摊明细",
            "生产部、财务部",
            "形成可复核的直接人工差异记录",
        )
    return (
        f"补齐并复核两厂{product}设备利用、动力、检验、维修、折旧及制造费用分摊明细",
        "设备部、财务部",
        "形成可复核的制造费用差异记录",
    )


def build_benchmark_guidance(
    *,
    product: str,
    period: str,
    factory: str,
    benchmark_factory: str,
    gaps: Iterable[BenchmarkGap],
    top_material: str | None,
    top_material_delta: Decimal | None,
    special_event_note: str | None = None,
) -> tuple[str, list[BenchmarkRecommendation]]:
    """Build scenario-specific text while preserving missing-data boundaries."""

    ordered = list(gaps)
    components = [
        item
        for item in ordered
        if item.name in {"直接材料", "直接人工", "制造费用"}
    ]
    unit_cost = next((item for item in ordered if item.name == "单位成本"), None)
    total_parts = [
        f"{item.name}{factory}较{benchmark_factory}{_relative_word(item.difference)}"
        for item in [*components, *([unit_cost] if unit_cost else [])]
    ]
    overall = "；".join(total_parts) or "对标差异暂无数据"

    ranked_by_abs = sorted(
        (item for item in components if item.difference is not None),
        key=lambda item: (
            -abs(item.difference or Decimal("0")),
            ("直接材料", "直接人工", "制造费用").index(item.name),
        ),
    )
    recommendation_ranked = sorted(
        ranked_by_abs,
        key=lambda item: (
            item.direction != "unfavorable",
            -abs(item.difference or Decimal("0")),
            ("直接材料", "直接人工", "制造费用").index(item.name),
        ),
    )
    dominant = ranked_by_abs[0] if ranked_by_abs else None
    if dominant is None:
        reason = "三项成本要素缺少可比较差异，暂不能定位主要差异来源。"
    else:
        reason = (
            f"三项成本要素中，{dominant.name}差异绝对额最大，"
            f"{factory}较{benchmark_factory}{_relative_word(dominant.difference)}"
            f"（{_signed_pct(dominant.difference_rate_pct)}），评价为{_outcome(dominant.direction)}。"
        )
        if top_material:
            change = (
                "环比变动暂无数据"
                if top_material_delta is None
                else f"环比变动{_signed_money(top_material_delta)}元/盒"
            )
            reason += (
                f"本厂内部材料明细显示，{top_material}{change}；"
                "该内部环比线索仅用于确定核查顺序，不能替代两厂同期明细归因。"
            )
        route = {
            "直接材料": "优先核查采购量价、批次领退料、投料耗用与工艺收率证据",
            "直接人工": "优先核查实际工时、班组配置、工资率与人工分摊口径",
            "制造费用": "优先核查设备利用、动力、检验、维修、折旧与费用分摊口径",
        }[dominant.name]
        reason += f"结合产品配方、生产工艺、设备记录及GMP知识证据，{route}。"
    if special_event_note:
        reason += special_event_note

    narrative = (
        f"总体差异：{overall}。工厂差异按{factory}减{benchmark_factory}计算，成本越低评价越有利。\n\n"
        f"原因研判：{reason}\n\n"
        f"证据边界：以上排序基于{period}同期结构化成本差异和本厂内部明细。"
        "在对标厂原材料消耗、实际采购量价、工艺收率、人工工时或费用分摊明细未补齐前，"
        "核查方向不表述为已确认因果。"
    )

    recommendations: list[BenchmarkRecommendation] = []
    for index, gap in enumerate(recommendation_ranked[:2], start=1):
        action, owner, expected = _recommendation_for(
            gap,
            product=product,
            top_material=top_material,
        )
        recommendations.append(
            BenchmarkRecommendation(
                sequence=str(index),
                action=action,
                owner=owner,
                priority=_priority(gap),
                expected_effect=expected,
            )
        )
    if not recommendations:
        recommendations.append(
            BenchmarkRecommendation(
                sequence="1",
                action=f"补齐两厂{product}同期成本要素及分项明细后重新运行对标分析",
                owner="财务部",
                priority="高",
                expected_effect="恢复差异结构定位和归因分析条件",
            )
        )
    return narrative, recommendations

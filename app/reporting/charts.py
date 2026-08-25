"""Deterministic report charts derived only from the validated report contract."""

from __future__ import annotations

import re
import threading
from decimal import Decimal, InvalidOperation
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

from matplotlib import font_manager
from matplotlib import pyplot as plt

from .models import ReportContract


_GREEN = "#165c49"
_FAVORABLE = "#21835f"
_UNFAVORABLE = "#b94343"
_GOLD = "#d99832"
_SAGE = "#82a89b"
_GRID = "#e6ece9"
_TEXT = "#344b43"
_MUTED = "#687a73"
_CHART_LOCK = threading.Lock()
_MISSING = {"", "暂无数据", "不适用", "—", "-"}


def _font_family() -> str:
    for path in (
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/msyh.ttf"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/simsun.ttc"),
    ):
        if path.is_file():
            font_manager.fontManager.addfont(str(path))
            return font_manager.FontProperties(fname=str(path)).get_name()
    return "DejaVu Sans"


_FONT_FAMILY = _font_family()


def _decimal(value: object) -> Decimal | None:
    text = str(value).strip()
    if text in _MISSING:
        return None
    cleaned = re.sub(r"[,%元盒/]", "", text).replace("−", "-").strip()
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _money(value: Decimal, digits: int = 2) -> str:
    return f"{value:,.{digits}f}"


def _style_axis(axis) -> None:
    axis.spines[["top", "right", "left"]].set_visible(False)
    axis.spines["bottom"].set_color("#cfdad5")
    axis.tick_params(colors=_MUTED, labelsize=9, length=0)
    axis.grid(axis="y", color=_GRID, linewidth=0.8)
    axis.set_axisbelow(True)


def _save(figure, path: Path, *, tight: bool = True) -> Path:
    save_options = {"bbox_inches": "tight"} if tight else {}
    figure.savefig(path, dpi=220, facecolor="white", **save_options)
    plt.close(figure)
    return path


def _render_trend(contract: ReportContract, path: Path) -> Path:
    table = contract.dynamic_tables["近6个月成本趋势表格"]
    periods: list[str] = []
    materials: list[Decimal] = []
    unit_costs: list[Decimal] = []
    for row in table.rows:
        if len(row) < 6:
            continue
        material = _decimal(row[2])
        unit_cost = _decimal(row[5])
        if material is None or unit_cost is None:
            continue
        periods.append(str(row[0]))
        materials.append(material)
        unit_costs.append(unit_cost)

    figure, axis = plt.subplots(figsize=(8.8, 3.35))
    _style_axis(axis)
    if not periods:
        axis.set_axis_off()
        axis.text(0.5, 0.5, "暂无可绘制的成本趋势数据", ha="center", va="center", color=_MUTED, fontsize=13)
        return _save(figure, path)

    x = list(range(len(periods)))
    unit_values = [float(item) for item in unit_costs]
    material_values = [float(item) for item in materials]
    axis.plot(x, unit_values, color=_GREEN, linewidth=2.6, marker="o", markersize=5.5, label="单位成本")
    axis.plot(x, material_values, color=_GOLD, linewidth=1.8, marker="o", markersize=4.5, label="直接材料")
    lower = min(unit_values + material_values)
    axis.fill_between(x, unit_values, lower, color=_GREEN, alpha=0.08)
    axis.set_xticks(x, periods)
    axis.set_ylabel("元/盒", color=_MUTED, fontsize=9)
    axis.legend(loc="upper left", ncols=2, frameon=False, fontsize=9)
    span = max(unit_values + material_values) - lower
    axis.set_ylim(lower - max(span * 0.12, 0.08), max(unit_values + material_values) + max(span * 0.20, 0.12))
    for index in {0, len(periods) - 1}:
        axis.annotate(
            _money(unit_costs[index]),
            (x[index], unit_values[index]),
            xytext=(0, 8),
            textcoords="offset points",
            ha="center",
            color=_GREEN,
            fontsize=8.5,
            fontweight="bold",
        )
    figure.tight_layout(pad=0.8)
    return _save(figure, path)


def _render_waterfall(contract: ReportContract, path: Path) -> Path:
    pairs = (
        ("直接材料", "本月材料成本", "上月材料成本"),
        ("直接人工", "本月人工成本", "上月人工成本"),
        ("制造费用", "本月制造费用", "上月制造费用"),
    )
    labels: list[str] = []
    deltas: list[Decimal] = []
    for label, current_name, previous_name in pairs:
        current = _decimal(contract.fields[current_name].value)
        previous = _decimal(contract.fields[previous_name].value)
        if current is None or previous is None:
            continue
        labels.append(label)
        deltas.append(current - previous)

    figure, axis = plt.subplots(figsize=(8.8, 3.35))
    _style_axis(axis)
    if len(deltas) != 3:
        axis.set_axis_off()
        axis.text(0.5, 0.5, "暂无完整上期数据，单位成本变动瀑布图不适用", ha="center", va="center", color=_MUTED, fontsize=13)
        return _save(figure, path)

    starts: list[Decimal] = []
    cumulative = Decimal("0")
    for delta in deltas:
        starts.append(cumulative)
        cumulative += delta
    chart_labels = labels + ["总变动"]
    chart_starts = starts + [Decimal("0")]
    chart_values = deltas + [cumulative]
    for index, (start, delta) in enumerate(zip(chart_starts, chart_values, strict=True)):
        end = start + delta
        bottom = min(start, end)
        height = abs(delta)
        color = _GREEN if index == len(chart_values) - 1 else _UNFAVORABLE if delta > 0 else _FAVORABLE if delta < 0 else _SAGE
        axis.bar(index, float(max(height, Decimal("0.004"))), bottom=float(bottom), width=0.54, color=color, zorder=3)
        label_y = max(start, end) if delta >= 0 else min(start, end)
        axis.annotate(
            f"{'+' if delta > 0 else ''}{_money(delta)}",
            (index, float(label_y)),
            xytext=(0, 7 if delta >= 0 else -13),
            textcoords="offset points",
            ha="center",
            va="bottom" if delta >= 0 else "top",
            color=_TEXT,
            fontsize=9,
            fontweight="bold",
        )
        if index < len(deltas) - 1:
            connector = start + delta
            axis.plot([index + 0.27, index + 0.73], [float(connector), float(connector)], color="#9eb5ac", linestyle=(0, (4, 3)), linewidth=1)
    axis.axhline(0, color="#9eb5ac", linestyle=(0, (4, 3)), linewidth=1)
    axis.set_xticks(range(len(chart_labels)), chart_labels)
    axis.set_ylabel("元/盒", color=_MUTED, fontsize=9)
    values = [Decimal("0"), *starts, *(start + delta for start, delta in zip(chart_starts, chart_values, strict=True))]
    low, high = min(values), max(values)
    pad = max((high - low) * Decimal("0.28"), Decimal("0.08"))
    axis.set_ylim(float(low - pad), float(high + pad))
    figure.tight_layout(pad=0.8)
    return _save(figure, path)


def _render_structure(contract: ReportContract, path: Path) -> Path:
    pairs = (
        ("直接材料", "本月材料成本"),
        ("直接人工", "本月人工成本"),
        ("制造费用", "本月制造费用"),
    )
    labels: list[str] = []
    values: list[Decimal] = []
    for label, field_name in pairs:
        value = _decimal(contract.fields[field_name].value)
        if value is None:
            continue
        labels.append(label)
        values.append(value)

    # Keep the exported canvas horizontal like the trend and waterfall charts.
    # A tight bounding box around an equal-aspect pie produces a near-square
    # image, which becomes visually oversized when Word inserts every chart at
    # the same width.
    figure, axis = plt.subplots(figsize=(8.8, 3.50))
    if len(values) != 3 or sum(values) == 0:
        axis.set_axis_off()
        axis.text(0.5, 0.5, "暂无可绘制的成本结构数据", ha="center", va="center", color=_MUTED, fontsize=13)
        return _save(figure, path)

    total = sum(values)
    shares = [value / total * Decimal("100") for value in values]
    legend_labels = [f"{name}  {_money(value)} 元/盒  {share:.2f}%" for name, value, share in zip(labels, values, shares, strict=True)]
    wedges, _ = axis.pie(
        [float(item) for item in values],
        startangle=90,
        counterclock=False,
        colors=[_GREEN, "#d6b46b", _SAGE],
        radius=0.90,
        wedgeprops={"width": 0.30, "edgecolor": "white", "linewidth": 2},
    )
    period_name = "本季度" if contract.analysis_type == "季度成本分析" else "本期" if contract.analysis_type == "专题分析" else "本月"
    axis.text(0, 0.09, _money(total), ha="center", va="center", color="#14231f", fontsize=18, fontweight="bold")
    axis.text(0, -0.09, f"元/盒\n{period_name}单位成本", ha="center", va="center", color=_MUTED, fontsize=9, linespacing=1.28)
    axis.legend(
        wedges,
        legend_labels,
        loc="center left",
        bbox_to_anchor=(1.02, 0.5),
        frameon=False,
        fontsize=9,
        handlelength=1.2,
        labelspacing=1.05,
    )
    axis.set(aspect="equal")
    # Position the donut and legend as one centered visual group. The donut is
    # intentionally left of the page centre so the legend occupies the right
    # half without making the complete chart look off-centre.
    axis.set_position([0.18, 0.06, 0.42, 0.88])
    return _save(figure, path, tight=False)


def render_report_charts(contract: ReportContract, output_dir: str | Path) -> dict[str, Path]:
    """Render the three website-equivalent report charts as high-resolution PNGs."""

    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    settings = {
        "font.family": _FONT_FAMILY,
        "axes.unicode_minus": False,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "text.color": _TEXT,
        "axes.labelcolor": _MUTED,
    }
    with _CHART_LOCK, plt.rc_context(settings):
        return {
            "trend": _render_trend(contract, target / "cost-trend.png"),
            "waterfall": _render_waterfall(contract, target / "unit-cost-waterfall.png"),
            "structure": _render_structure(contract, target / "cost-structure.png"),
        }

"""In-memory normalization helpers used by validation and later analytics."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Iterable

from .models import MarketPricePoint, MarketPriceRow


MARKET_MONTH_FIELDS = (
    ("2026-01", "price_january"),
    ("2026-02", "price_february"),
    ("2026-03", "price_march"),
    ("2026-04", "price_april"),
    ("2026-05", "price_may"),
    ("2026-06", "price_june"),
)

INDUSTRY_UNIT_FACTORS = {
    "口服液类": Decimal("10"),
    "颗粒剂类": Decimal("20"),
    "胶囊剂类": Decimal("60"),
}


def normalize_market_prices(rows: Iterable[MarketPriceRow]) -> list[MarketPricePoint]:
    """Convert the six monthly price columns into one record per month."""

    points: list[MarketPricePoint] = []
    for row in rows:
        for month, field_name in MARKET_MONTH_FIELDS:
            points.append(
                MarketPricePoint(
                    material_name=row.material_name,
                    grade=row.grade,
                    unit=row.unit,
                    month=month,
                    price=getattr(row, field_name),
                    source=row.source,
                    trend_analysis=row.trend_analysis,
                )
            )
    return points


def parse_benchmark_value(value: str) -> tuple[Decimal, bool]:
    """Return a decimal value and whether the source was a percentage."""

    is_percentage = value.endswith("%")
    raw = value[:-1] if is_percentage else value
    try:
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"无法解析行业基准值：{value}") from exc
    if is_percentage:
        number /= Decimal("100")
    return number, is_percentage


def industry_unit_cost_per_box(
    product_category: str,
    metric: str,
    value: str,
) -> Decimal:
    """Normalize an industry unit-cost benchmark to yuan per box."""

    number, is_percentage = parse_benchmark_value(value)
    if is_percentage:
        raise ValueError("百分比指标不能换算为元/盒")
    if "单位成本" not in metric:
        raise ValueError(f"指标不是单位成本：{metric}")
    try:
        factor = INDUSTRY_UNIT_FACTORS[product_category]
    except KeyError as exc:
        raise ValueError(f"产品类别没有单位换算：{product_category}") from exc
    return number * factor


"""Typed row models for the governed source CSV files."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


PRODUCT_SPECS = {
    "银黄口服液": "10ml×10支/盒",
    "板蓝根颗粒": "10g×20袋/盒",
    "六味地黄胶囊": "0.3g×60粒/盒",
}

FACTORIES = {"中药一厂", "中药二厂"}

MANUFACTURING_CATEGORIES = {
    "折旧费",
    "动力费(水电气)",
    "人工(间接)",
    "检验费",
    "其他制造费用",
}

PRODUCT_MATERIALS = {
    "银黄口服液": {
        "金银花",
        "黄芩提取物",
        "蔗糖",
        "苯甲酸钠",
        "纯化水",
        "包装材料(盒+说明书)",
    },
    "板蓝根颗粒": {
        "板蓝根",
        "蔗糖",
        "糊精",
        "包装材料(复合膜袋+纸盒)",
    },
    "六味地黄胶囊": {
        "熟地黄",
        "山茱萸",
        "山药",
        "泽泻",
        "茯苓",
        "牡丹皮",
        "空心胶囊",
        "包装材料(PVC+铝箔+纸盒)",
    },
}

MARKET_UNITS = {"元/kg", "元/万粒"}

BENCHMARK_CATEGORIES = {"口服液类", "颗粒剂类", "胶囊剂类", "中成药行业整体"}


def _validate_month(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise ValueError("月份必须符合YYYY-MM")
    try:
        datetime.strptime(value, "%Y-%m")
    except ValueError as exc:
        raise ValueError("月份不是有效公历年月") from exc
    return value


def _validate_nonnegative(value: Decimal) -> Decimal:
    if value < 0:
        raise ValueError("数值不得为负")
    return value


def _validate_positive_int(value: int) -> int:
    if value <= 0:
        raise ValueError("整数值必须大于0")
    return value


def _validate_positive_decimal(value: Decimal) -> Decimal:
    if value <= 0:
        raise ValueError("数值必须大于0")
    return value


Month = Annotated[str, AfterValidator(_validate_month)]
NonNegativeDecimal = Annotated[Decimal, AfterValidator(_validate_nonnegative)]
PositiveQuantity = Annotated[int, AfterValidator(_validate_positive_int)]
PositiveDecimal = Annotated[Decimal, AfterValidator(_validate_positive_decimal)]


class SourceRow(BaseModel):
    """Base behavior shared by raw CSV row models."""

    model_config = ConfigDict(
        populate_by_name=True,
        extra="ignore",
        str_strip_whitespace=True,
    )


class ProductRow(SourceRow):
    factory: str = Field(alias="工厂")
    product: str = Field(alias="产品名称")
    specification: str = Field(alias="产品规格")
    month: Month = Field(alias="月份")
    quantity_boxes: PositiveQuantity = Field(alias="产量(盒)")

    @field_validator("factory")
    @classmethod
    def validate_factory(cls, value: str) -> str:
        if value not in FACTORIES:
            raise ValueError(f"未知工厂：{value}")
        return value

    @model_validator(mode="after")
    def validate_product_specification(self) -> "ProductRow":
        expected = PRODUCT_SPECS.get(self.product)
        if expected is None:
            raise ValueError(f"未知产品：{self.product}")
        if self.specification != expected:
            raise ValueError(
                f"产品规格不匹配：{self.product}应为{expected}，实际为{self.specification}"
            )
        return self


class CostSummaryRow(ProductRow):
    direct_material: NonNegativeDecimal = Field(alias="直接材料(元/盒)")
    direct_labor: NonNegativeDecimal = Field(alias="直接人工(元/盒)")
    manufacturing_overhead: NonNegativeDecimal = Field(alias="制造费用(元/盒)")
    unit_cost: NonNegativeDecimal = Field(alias="单位成本(元/盒)")
    total_cost: NonNegativeDecimal = Field(alias="总成本(元)")


class BudgetRow(SourceRow):
    factory: str = Field(alias="工厂")
    product: str = Field(alias="产品名称")
    specification: str = Field(alias="产品规格")
    month: Month = Field(alias="月份")
    budget_quantity_boxes: PositiveQuantity = Field(alias="预算产量(盒)")
    budget_direct_material: NonNegativeDecimal = Field(alias="预算直接材料(元/盒)")
    budget_direct_labor: NonNegativeDecimal = Field(alias="预算直接人工(元/盒)")
    budget_manufacturing_overhead: NonNegativeDecimal = Field(
        alias="预算制造费用(元/盒)"
    )
    budget_unit_cost: NonNegativeDecimal = Field(alias="预算单位成本(元/盒)")
    budget_total_cost: NonNegativeDecimal = Field(alias="预算总成本(元)")

    @field_validator("factory")
    @classmethod
    def validate_factory(cls, value: str) -> str:
        if value not in FACTORIES:
            raise ValueError(f"未知工厂：{value}")
        return value

    @model_validator(mode="after")
    def validate_product_specification(self) -> "BudgetRow":
        expected = PRODUCT_SPECS.get(self.product)
        if expected is None:
            raise ValueError(f"未知产品：{self.product}")
        if self.specification != expected:
            raise ValueError(
                f"产品规格不匹配：{self.product}应为{expected}，实际为{self.specification}"
            )
        return self


class LaborDetailRow(ProductRow):
    direct_labor_total: NonNegativeDecimal = Field(alias="直接人工总额(元)")
    total_hours: PositiveDecimal = Field(alias="总工时(小时)")
    production_headcount: PositiveQuantity = Field(alias="生产人数(人)")
    work_days: PositiveQuantity = Field(alias="工作天数(天)")


class MaterialDetailRow(ProductRow):
    material_name: str = Field(alias="原材料名称")
    unit_consumption_cost: NonNegativeDecimal = Field(alias="单位消耗成本(元/盒)")
    material_total_cost: NonNegativeDecimal = Field(alias="原材料总成本(元)")
    material_share: Decimal = Field(alias="占总材料成本比例")

    @field_validator("material_share", mode="before")
    @classmethod
    def parse_percentage(cls, value: object) -> Decimal:
        if not isinstance(value, str) or not value.endswith("%"):
            raise ValueError("占总材料成本比例必须是带%号的百分比")
        try:
            parsed = Decimal(value[:-1]) / Decimal("100")
        except InvalidOperation as exc:
            raise ValueError("占总材料成本比例无法解析") from exc
        if parsed < 0 or parsed > 1:
            raise ValueError("占总材料成本比例必须位于0%至100%")
        return parsed

    @model_validator(mode="after")
    def validate_material(self) -> "MaterialDetailRow":
        allowed = PRODUCT_MATERIALS.get(self.product, set())
        if self.material_name not in allowed:
            raise ValueError(
                f"产品{self.product}不允许原材料：{self.material_name}"
            )
        return self


class ManufacturingDetailRow(ProductRow):
    expense_category: str = Field(alias="费用类别")
    unit_expense: NonNegativeDecimal = Field(alias="单位费用(元/盒)")
    expense_total: NonNegativeDecimal = Field(alias="费用总额(元)")

    @field_validator("expense_category")
    @classmethod
    def validate_expense_category(cls, value: str) -> str:
        if value not in MANUFACTURING_CATEGORIES:
            raise ValueError(f"未知制造费用类别：{value}")
        return value


class MarketPriceRow(SourceRow):
    material_name: str = Field(alias="药材名称")
    grade: str = Field(alias="规格等级")
    unit: str = Field(alias="单位")
    price_january: NonNegativeDecimal = Field(alias="1月价格")
    price_february: NonNegativeDecimal = Field(alias="2月价格")
    price_march: NonNegativeDecimal = Field(alias="3月价格")
    price_april: NonNegativeDecimal = Field(alias="4月价格")
    price_may: NonNegativeDecimal = Field(alias="5月价格")
    price_june: NonNegativeDecimal = Field(alias="6月价格")
    source: str = Field(alias="价格来源")
    trend_analysis: str = Field(alias="趋势分析")

    @field_validator("unit")
    @classmethod
    def validate_unit(cls, value: str) -> str:
        if value not in MARKET_UNITS:
            raise ValueError(f"未知市场价格单位：{value}")
        return value


class IndustryBenchmarkRow(SourceRow):
    product_category: str = Field(alias="产品类别")
    metric: str = Field(alias="指标")
    industry_p25: str = Field(alias="行业P25")
    industry_p50: str = Field(alias="行业P50")
    industry_p75: str = Field(alias="行业P75")
    plant_level: str = Field(alias="本厂水平(中药一厂)")
    evaluation: str = Field(alias="对标评价")

    @field_validator("product_category")
    @classmethod
    def validate_category(cls, value: str) -> str:
        if value not in BENCHMARK_CATEGORIES:
            raise ValueError(f"未知行业产品类别：{value}")
        return value

    @field_validator("industry_p25", "industry_p50", "industry_p75", "plant_level")
    @classmethod
    def validate_benchmark_value(cls, value: str) -> str:
        raw = value[:-1] if value.endswith("%") else value
        try:
            Decimal(raw)
        except InvalidOperation as exc:
            raise ValueError(f"行业基准值无法解析：{value}") from exc
        return value


class MarketPricePoint(BaseModel):
    material_name: str
    grade: str
    unit: str
    month: Month
    price: NonNegativeDecimal
    source: str
    trend_analysis: str

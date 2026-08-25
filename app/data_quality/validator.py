"""Executable data-quality checks defined by the project specifications."""

from __future__ import annotations

import csv
import hashlib
import io
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .models import (
    BudgetRow,
    CostSummaryRow,
    IndustryBenchmarkRow,
    LaborDetailRow,
    ManufacturingDetailRow,
    MaterialDetailRow,
    MarketPriceRow,
    SourceRow,
)
from .normalization import industry_unit_cost_per_box, normalize_market_prices


MONEY_TOLERANCE = Decimal("0.01")
SHARE_TOLERANCE = Decimal("0.001")  # 0.1 percentage point


class ValidationIssue(BaseModel):
    severity: str
    code: str
    message: str
    file: str | None = None
    row: int | None = None
    key: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class FileValidationSummary(BaseModel):
    path: str
    sha256: str | None = None
    row_count: int = 0
    valid_row_count: int = 0
    invalid_row_count: int = 0
    utf8_bom: bool = False


class ValidationReport(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    status: str
    source_root: str
    files: dict[str, FileValidationSummary]
    issues: list[ValidationIssue]
    normalized_counts: dict[str, int]

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "ERROR"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "WARNING"]


@dataclass(frozen=True)
class ValidatedDataBundle:
    """Typed, read-only-by-convention rows that passed CSV row validation."""

    plant1_summary: list[CostSummaryRow]
    plant2_summary: list[CostSummaryRow]
    plant1_prior_summary: list[CostSummaryRow]
    plant2_prior_summary: list[CostSummaryRow]
    budgets: list[BudgetRow]
    labor_detail: list[LaborDetailRow]
    material_detail: list[MaterialDetailRow]
    manufacturing_detail: list[ManufacturingDetailRow]
    market_prices: list[MarketPriceRow]
    industry_benchmarks: list[IndustryBenchmarkRow]


@dataclass(frozen=True)
class CsvSpec:
    name: str
    relative_path: Path
    model: type[SourceRow]
    primary_key: tuple[str, ...]
    expected_factory: str | None = None

    @property
    def expected_headers(self) -> tuple[str, ...]:
        return tuple(
            str(field.alias or field_name)
            for field_name, field in self.model.model_fields.items()
        )


CSV_SPECS = (
    CsvSpec(
        name="plant1_summary",
        relative_path=Path("01_成本明细数据") / "中药一厂_成本汇总_2026年1-6月.csv",
        model=CostSummaryRow,
        primary_key=("factory", "product", "month"),
        expected_factory="中药一厂",
    ),
    CsvSpec(
        name="plant2_summary",
        relative_path=Path("01_成本明细数据") / "中药二厂_成本汇总_2026年1-6月.csv",
        model=CostSummaryRow,
        primary_key=("factory", "product", "month"),
        expected_factory="中药二厂",
    ),
    CsvSpec(
        name="plant1_prior_summary",
        relative_path=Path("01_成本明细数据") / "中药一厂_成本汇总_2025年1-6月.csv",
        model=CostSummaryRow,
        primary_key=("factory", "product", "month"),
        expected_factory="中药一厂",
    ),
    CsvSpec(
        name="plant2_prior_summary",
        relative_path=Path("01_成本明细数据") / "中药二厂_成本汇总_2025年1-6月.csv",
        model=CostSummaryRow,
        primary_key=("factory", "product", "month"),
        expected_factory="中药二厂",
    ),
    CsvSpec(
        name="budgets",
        relative_path=Path("01_成本明细数据") / "中药一厂_预算数据_2026年.csv",
        model=BudgetRow,
        primary_key=("factory", "product", "month"),
        expected_factory="中药一厂",
    ),
    CsvSpec(
        name="labor_detail",
        relative_path=Path("01_成本明细数据") / "中药一厂_人工工时明细_2026年1-6月.csv",
        model=LaborDetailRow,
        primary_key=("factory", "product", "month"),
        expected_factory="中药一厂",
    ),
    CsvSpec(
        name="material_detail",
        relative_path=Path("01_成本明细数据") / "中药一厂_原材料消耗明细_2026年1-6月.csv",
        model=MaterialDetailRow,
        primary_key=("factory", "product", "month", "material_name"),
        expected_factory="中药一厂",
    ),
    CsvSpec(
        name="manufacturing_detail",
        relative_path=Path("01_成本明细数据") / "中药一厂_制造费用明细_2026年1-6月.csv",
        model=ManufacturingDetailRow,
        primary_key=("factory", "product", "month", "expense_category"),
        expected_factory="中药一厂",
    ),
    CsvSpec(
        name="market_prices",
        relative_path=Path("02_行业参考数据") / "药材市场价格行情_2026年上半年.csv",
        model=MarketPriceRow,
        primary_key=("material_name", "grade", "unit"),
    ),
    CsvSpec(
        name="industry_benchmarks",
        relative_path=Path("02_行业参考数据") / "行业成本基准数据_2026.csv",
        model=IndustryBenchmarkRow,
        primary_key=("product_category", "metric"),
    ),
)


def _key_text(row: SourceRow, fields: tuple[str, ...]) -> str:
    return " | ".join(str(getattr(row, field)) for field in fields)


def _issue_code(error: dict[str, Any]) -> str:
    loc = tuple(str(item) for item in error.get("loc", ()))
    message = str(error.get("msg", ""))
    joined = " ".join(loc)
    if "month" in joined or "月份" in joined or "月份" in message:
        return "B07"
    if "specification" in joined or "产品规格" in message:
        return "B10"
    enum_locations = {
        "factory",
        "product",
        "expense_category",
        "product_category",
        "unit",
        "工厂",
        "产品名称",
        "费用类别",
        "产品类别",
        "单位",
    }
    if any(item in enum_locations for item in loc):
        return "B08"
    if "material" in joined or "原材料" in message:
        return "B08"
    if "不得为负" in message or "必须大于0" in message:
        return "B09"
    return "B06"


def _read_csv(
    root: Path,
    spec: CsvSpec,
    issues: list[ValidationIssue],
) -> tuple[list[SourceRow], FileValidationSummary]:
    path = root / spec.relative_path
    summary = FileValidationSummary(path=str(spec.relative_path))
    if not path.is_file():
        issues.append(
            ValidationIssue(
                severity="ERROR",
                code="B01",
                message="必需文件不存在或不是普通文件",
                file=str(spec.relative_path),
            )
        )
        return [], summary

    raw = path.read_bytes()
    summary.sha256 = hashlib.sha256(raw).hexdigest()
    summary.utf8_bom = raw.startswith(b"\xef\xbb\xbf")
    if not raw:
        issues.append(
            ValidationIssue(
                severity="ERROR",
                code="B01",
                message="文件为空",
                file=str(spec.relative_path),
            )
        )
        return [], summary
    if not summary.utf8_bom:
        issues.append(
            ValidationIssue(
                severity="ERROR",
                code="B02",
                message="CSV缺少UTF-8 BOM，文件头必须为EF BB BF",
                file=str(spec.relative_path),
                details={"first_bytes": raw[:3].hex().upper()},
            )
        )

    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        issues.append(
            ValidationIssue(
                severity="ERROR",
                code="B02",
                message=f"CSV不能严格按UTF-8解码：{exc}",
                file=str(spec.relative_path),
            )
        )
        return [], summary

    reader = csv.DictReader(io.StringIO(text, newline=""))
    headers = tuple(reader.fieldnames or ())
    expected = spec.expected_headers
    missing = [header for header in expected if header not in headers]
    unexpected = [header for header in headers if header not in expected]
    if missing:
        issues.append(
            ValidationIssue(
                severity="ERROR",
                code="B03",
                message="缺少必填字段",
                file=str(spec.relative_path),
                details={"missing_fields": missing},
            )
        )
        return [], summary
    if unexpected:
        issues.append(
            ValidationIssue(
                severity="WARNING",
                code="W10",
                message="存在数据字典未定义的附加字段，当前版本将忽略",
                file=str(spec.relative_path),
                details={"unexpected_fields": unexpected},
            )
        )

    rows: list[SourceRow] = []
    seen_keys: dict[tuple[Any, ...], int] = {}
    for row_number, raw_row in enumerate(reader, start=2):
        summary.row_count += 1
        empty_fields = [
            header
            for header in expected
            if raw_row.get(header) is None or str(raw_row.get(header)).strip() == ""
        ]
        if empty_fields:
            summary.invalid_row_count += 1
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="B05",
                    message="必填字段为空",
                    file=str(spec.relative_path),
                    row=row_number,
                    details={"empty_fields": empty_fields},
                )
            )
            continue

        try:
            model_row = spec.model.model_validate(raw_row)
        except ValidationError as exc:
            summary.invalid_row_count += 1
            for error in exc.errors():
                issues.append(
                    ValidationIssue(
                        severity="ERROR",
                        code=_issue_code(error),
                        message=str(error.get("msg", "数据行校验失败")),
                        file=str(spec.relative_path),
                        row=row_number,
                        details={"location": list(error.get("loc", ()))},
                    )
                )
            continue

        if spec.expected_factory and getattr(model_row, "factory") != spec.expected_factory:
            summary.invalid_row_count += 1
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="B08",
                    message=f"文件工厂必须为{spec.expected_factory}",
                    file=str(spec.relative_path),
                    row=row_number,
                    key=_key_text(model_row, spec.primary_key),
                )
            )
            continue

        key = tuple(getattr(model_row, field) for field in spec.primary_key)
        if key in seen_keys:
            summary.invalid_row_count += 1
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="B04",
                    message=f"主键重复，首次出现于第{seen_keys[key]}行",
                    file=str(spec.relative_path),
                    row=row_number,
                    key=_key_text(model_row, spec.primary_key),
                )
            )
            continue

        seen_keys[key] = row_number
        rows.append(model_row)
        summary.valid_row_count += 1

    if summary.row_count == 0:
        issues.append(
            ValidationIssue(
                severity="ERROR",
                code="B01",
                message="CSV只有表头，没有数据行",
                file=str(spec.relative_path),
            )
        )
    return rows, summary


def _add_reconciliation_issue(
    issues: list[ValidationIssue],
    code: str,
    message: str,
    file: str,
    key: str,
    difference: Decimal,
) -> None:
    issues.append(
        ValidationIssue(
            severity="ERROR",
            code=code,
            message=message,
            file=file,
            key=key,
            details={"difference": str(difference)},
        )
    )


def _validate_cross_file(
    loaded: dict[str, list[SourceRow]],
    issues: list[ValidationIssue],
) -> dict[str, int]:
    plant1 = {
        (row.product, row.month): row
        for row in loaded.get("plant1_summary", [])
        if isinstance(row, CostSummaryRow)
    }
    plant2 = {
        (row.product, row.month): row
        for row in loaded.get("plant2_summary", [])
        if isinstance(row, CostSummaryRow)
    }
    plant1_prior = {
        (row.product, row.month): row
        for row in loaded.get("plant1_prior_summary", [])
        if isinstance(row, CostSummaryRow)
    }
    plant2_prior = {
        (row.product, row.month): row
        for row in loaded.get("plant2_prior_summary", [])
        if isinstance(row, CostSummaryRow)
    }
    budgets = {
        (row.product, row.month): row
        for row in loaded.get("budgets", [])
        if isinstance(row, BudgetRow)
    }
    labor = {
        (row.product, row.month): row
        for row in loaded.get("labor_detail", [])
        if isinstance(row, LaborDetailRow)
    }

    for dataset_name, rows in (
        ("plant1_summary", plant1.values()),
        ("plant2_summary", plant2.values()),
        ("plant1_prior_summary", plant1_prior.values()),
        ("plant2_prior_summary", plant2_prior.values()),
    ):
        file = str(next(spec.relative_path for spec in CSV_SPECS if spec.name == dataset_name))
        for row in rows:
            key = f"{row.factory} | {row.product} | {row.month}"
            component_difference = row.unit_cost - (
                row.direct_material + row.direct_labor + row.manufacturing_overhead
            )
            if abs(component_difference) > MONEY_TOLERANCE:
                _add_reconciliation_issue(
                    issues,
                    "B11",
                    "单位成本与三项成本要素之和不一致",
                    file,
                    key,
                    component_difference,
                )
            total_difference = row.total_cost - Decimal(row.quantity_boxes) * row.unit_cost
            if abs(total_difference) > MONEY_TOLERANCE:
                _add_reconciliation_issue(
                    issues,
                    "B12",
                    "总成本与产量×单位成本不一致",
                    file,
                    key,
                    total_difference,
                )

    for (product, month), current in plant1.items():
        prior_month = f"{int(month[:4]) - 1}-{month[5:]}"
        prior = plant1_prior.get((product, prior_month))
        budget = budgets.get((product, month))
        labor_row = labor.get((product, month))
        key = f"中药一厂 | {product} | {month}"
        for source_name, source_row in (
            ("去年同期汇总", prior),
            ("预算数据", budget),
            ("人工工时明细", labor_row),
        ):
            if source_row is None:
                issues.append(
                    ValidationIssue(
                        severity="ERROR",
                        code="B18",
                        message=f"当前汇总缺少对应{source_name}",
                        key=key,
                    )
                )
        if budget is not None:
            budget_component_difference = budget.budget_unit_cost - (
                budget.budget_direct_material
                + budget.budget_direct_labor
                + budget.budget_manufacturing_overhead
            )
            if abs(budget_component_difference) > MONEY_TOLERANCE:
                _add_reconciliation_issue(
                    issues,
                    "B19",
                    "预算单位成本与三项预算成本要素之和不一致",
                    str(next(spec.relative_path for spec in CSV_SPECS if spec.name == "budgets")),
                    key,
                    budget_component_difference,
                )
            budget_total_difference = budget.budget_total_cost - (
                Decimal(budget.budget_quantity_boxes) * budget.budget_unit_cost
            )
            if abs(budget_total_difference) > MONEY_TOLERANCE:
                _add_reconciliation_issue(
                    issues,
                    "B19",
                    "预算总成本与预算产量×预算单位成本不一致",
                    str(next(spec.relative_path for spec in CSV_SPECS if spec.name == "budgets")),
                    key,
                    budget_total_difference,
                )
        if labor_row is not None:
            if labor_row.quantity_boxes != current.quantity_boxes:
                issues.append(
                    ValidationIssue(
                        severity="ERROR",
                        code="B20",
                        message="人工工时明细产量与成本汇总不一致",
                        key=key,
                        details={
                            "labor_quantity": labor_row.quantity_boxes,
                            "summary_quantity": current.quantity_boxes,
                        },
                    )
                )
            labor_total_difference = labor_row.direct_labor_total - (
                Decimal(current.quantity_boxes) * current.direct_labor
            )
            if abs(labor_total_difference) > MONEY_TOLERANCE:
                _add_reconciliation_issue(
                    issues,
                    "B20",
                    "人工总额与产量×单位直接人工成本不一致",
                    str(next(spec.relative_path for spec in CSV_SPECS if spec.name == "labor_detail")),
                    key,
                    labor_total_difference,
                )

    for (product, month), current in plant2.items():
        prior_month = f"{int(month[:4]) - 1}-{month[5:]}"
        if (product, prior_month) not in plant2_prior:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="B18",
                    message="二厂当前汇总缺少对应去年同期汇总",
                    key=f"中药二厂 | {product} | {month}",
                )
            )

    material_groups: dict[tuple[str, str], list[MaterialDetailRow]] = defaultdict(list)
    for row in loaded.get("material_detail", []):
        if not isinstance(row, MaterialDetailRow):
            continue
        key = f"{row.factory} | {row.product} | {row.month} | {row.material_name}"
        total_difference = (
            row.material_total_cost
            - Decimal(row.quantity_boxes) * row.unit_consumption_cost
        )
        if abs(total_difference) > MONEY_TOLERANCE:
            _add_reconciliation_issue(
                issues,
                "B13",
                "原材料总成本与产量×单位消耗成本不一致",
                str(next(spec.relative_path for spec in CSV_SPECS if spec.name == "material_detail")),
                key,
                total_difference,
            )
        material_groups[(row.product, row.month)].append(row)

    for group_key, rows in material_groups.items():
        summary = plant1.get(group_key)
        key = " | ".join(group_key)
        if summary is None:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="B17",
                    message="材料明细找不到对应的一厂汇总记录",
                    key=key,
                )
            )
            continue
        unit_total = sum((row.unit_consumption_cost for row in rows), Decimal("0"))
        difference = unit_total - summary.direct_material
        if abs(difference) > MONEY_TOLERANCE:
            _add_reconciliation_issue(
                issues,
                "B14",
                "材料明细合计与汇总直接材料不一致",
                str(next(spec.relative_path for spec in CSV_SPECS if spec.name == "material_detail")),
                key,
                difference,
            )
        for row in rows:
            if row.quantity_boxes != summary.quantity_boxes:
                issues.append(
                    ValidationIssue(
                        severity="ERROR",
                        code="B17",
                        message="材料明细产量与汇总产量不一致",
                        key=f"{key} | {row.material_name}",
                        details={
                            "detail_quantity": row.quantity_boxes,
                            "summary_quantity": summary.quantity_boxes,
                        },
                    )
                )
            if summary.direct_material:
                recalculated_share = row.unit_consumption_cost / summary.direct_material
                difference_share = row.material_share - recalculated_share
                if abs(difference_share) > SHARE_TOLERANCE:
                    issues.append(
                        ValidationIssue(
                            severity="WARNING",
                            code="W01",
                            message="源材料占比与重算值相差超过0.1个百分点",
                            key=f"{key} | {row.material_name}",
                            details={
                                "source_share": str(row.material_share),
                                "recalculated_share": str(recalculated_share),
                                "difference": str(difference_share),
                            },
                        )
                    )

    manufacturing_groups: dict[
        tuple[str, str], list[ManufacturingDetailRow]
    ] = defaultdict(list)
    for row in loaded.get("manufacturing_detail", []):
        if not isinstance(row, ManufacturingDetailRow):
            continue
        key = f"{row.factory} | {row.product} | {row.month} | {row.expense_category}"
        total_difference = (
            row.expense_total - Decimal(row.quantity_boxes) * row.unit_expense
        )
        if abs(total_difference) > MONEY_TOLERANCE:
            _add_reconciliation_issue(
                issues,
                "B15",
                "费用总额与产量×单位费用不一致",
                str(
                    next(
                        spec.relative_path
                        for spec in CSV_SPECS
                        if spec.name == "manufacturing_detail"
                    )
                ),
                key,
                total_difference,
            )
        manufacturing_groups[(row.product, row.month)].append(row)

    for group_key, rows in manufacturing_groups.items():
        summary = plant1.get(group_key)
        key = " | ".join(group_key)
        if summary is None:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="B17",
                    message="制造费用明细找不到对应的一厂汇总记录",
                    key=key,
                )
            )
            continue
        categories = {row.expense_category for row in rows}
        if len(categories) != 5:
            issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="B16",
                    message="制造费用类别不完整",
                    key=key,
                    details={"categories": sorted(categories)},
                )
            )
        unit_total = sum((row.unit_expense for row in rows), Decimal("0"))
        difference = unit_total - summary.manufacturing_overhead
        if abs(difference) > MONEY_TOLERANCE:
            _add_reconciliation_issue(
                issues,
                "B16",
                "制造费用明细合计与汇总制造费用不一致",
                str(
                    next(
                        spec.relative_path
                        for spec in CSV_SPECS
                        if spec.name == "manufacturing_detail"
                    )
                ),
                key,
                difference,
            )
        for row in rows:
            if row.quantity_boxes != summary.quantity_boxes:
                issues.append(
                    ValidationIssue(
                        severity="ERROR",
                        code="B17",
                        message="制造费用明细产量与汇总产量不一致",
                        key=f"{key} | {row.expense_category}",
                        details={
                            "detail_quantity": row.quantity_boxes,
                            "summary_quantity": summary.quantity_boxes,
                        },
                    )
                )

    market_rows = [
        row
        for row in loaded.get("market_prices", [])
        if isinstance(row, MarketPriceRow)
    ]
    market_names = {row.material_name for row in market_rows}
    material_names = {
        row.material_name
        for row in loaded.get("material_detail", [])
        if isinstance(row, MaterialDetailRow)
    }
    for material_name in sorted(material_names - market_names):
        issues.append(
            ValidationIssue(
                severity="WARNING",
                code="W02",
                message="原材料在市场行情中没有精确同名映射，禁止生成市场涨跌关联",
                key=material_name,
            )
        )

    benchmark_rows = [
        row
        for row in loaded.get("industry_benchmarks", [])
        if isinstance(row, IndustryBenchmarkRow)
    ]
    for row in benchmark_rows:
        if "单位成本" in row.metric:
            try:
                industry_unit_cost_per_box(
                    row.product_category,
                    row.metric,
                    row.industry_p50,
                )
            except ValueError as exc:
                issues.append(
                    ValidationIssue(
                        severity="ERROR",
                        code="B06",
                        message=str(exc),
                        key=f"{row.product_category} | {row.metric}",
                    )
                )

    # These are known availability warnings, not data corruption.
    issues.append(
        ValidationIssue(
            severity="WARNING",
            code="W09",
            message=(
                "仍缺少企业实际采购量价、实际耗用量、工艺收率、标准工时/工资率"
                "及设备费用分摊数据，相关量价差和事件影响禁止定量计算"
            ),
        )
    )

    return {
        "plant1_summary": len(plant1),
        "plant2_summary": len(plant2),
        "plant1_prior_summary": len(plant1_prior),
        "plant2_prior_summary": len(plant2_prior),
        "budgets": len(budgets),
        "labor_detail": len(labor),
        "material_detail": len(loaded.get("material_detail", [])),
        "manufacturing_detail": len(loaded.get("manufacturing_detail", [])),
        "market_price_points": len(normalize_market_prices(market_rows)),
        "industry_benchmarks": len(benchmark_rows),
    }


def load_validated_data(
    data_dir: str | Path,
) -> tuple[ValidationReport, ValidatedDataBundle]:
    """Validate all governed CSVs and return the report plus typed valid rows."""

    root = Path(data_dir).resolve()
    issues: list[ValidationIssue] = []
    loaded: dict[str, list[SourceRow]] = {}
    summaries: dict[str, FileValidationSummary] = {}

    for spec in CSV_SPECS:
        rows, summary = _read_csv(root, spec, issues)
        loaded[spec.name] = rows
        summaries[spec.name] = summary

    normalized_counts = _validate_cross_file(loaded, issues)

    has_error = any(issue.severity == "ERROR" for issue in issues)
    has_warning = any(issue.severity == "WARNING" for issue in issues)
    status = "FAIL" if has_error else "PASS_WITH_WARNING" if has_warning else "PASS"

    report = ValidationReport(
        status=status,
        source_root=str(root),
        files=summaries,
        issues=issues,
        normalized_counts=normalized_counts,
    )

    bundle = ValidatedDataBundle(
        plant1_summary=[
            row
            for row in loaded.get("plant1_summary", [])
            if isinstance(row, CostSummaryRow)
        ],
        plant2_summary=[
            row
            for row in loaded.get("plant2_summary", [])
            if isinstance(row, CostSummaryRow)
        ],
        plant1_prior_summary=[
            row
            for row in loaded.get("plant1_prior_summary", [])
            if isinstance(row, CostSummaryRow)
        ],
        plant2_prior_summary=[
            row
            for row in loaded.get("plant2_prior_summary", [])
            if isinstance(row, CostSummaryRow)
        ],
        budgets=[
            row for row in loaded.get("budgets", []) if isinstance(row, BudgetRow)
        ],
        labor_detail=[
            row
            for row in loaded.get("labor_detail", [])
            if isinstance(row, LaborDetailRow)
        ],
        material_detail=[
            row
            for row in loaded.get("material_detail", [])
            if isinstance(row, MaterialDetailRow)
        ],
        manufacturing_detail=[
            row
            for row in loaded.get("manufacturing_detail", [])
            if isinstance(row, ManufacturingDetailRow)
        ],
        market_prices=[
            row
            for row in loaded.get("market_prices", [])
            if isinstance(row, MarketPriceRow)
        ],
        industry_benchmarks=[
            row
            for row in loaded.get("industry_benchmarks", [])
            if isinstance(row, IndustryBenchmarkRow)
        ],
    )
    return report, bundle


def validate_data_dir(data_dir: str | Path) -> ValidationReport:
    """Validate all configured CSVs under data_dir without modifying them."""

    report, _ = load_validated_data(data_dir)
    return report

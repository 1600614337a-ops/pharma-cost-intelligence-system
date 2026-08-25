"""Regression tests for source-data validation and normalization."""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from app.data_quality.models import MarketPriceRow
from app.data_quality.normalization import (
    industry_unit_cost_per_box,
    normalize_market_prices,
    parse_benchmark_value,
)
from app.data_quality.validator import validate_data_dir


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_FOLDERS = ("01_成本明细数据", "02_行业参考数据")


def issue_codes(report) -> set[str]:
    return {issue.code for issue in report.issues}


def read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        return list(reader.fieldnames or []), list(reader)


def write_csv_rows(
    path: Path,
    headers: list[str],
    rows: list[dict[str, str]],
    *,
    encoding: str = "utf-8-sig",
) -> None:
    with path.open("w", encoding=encoding, newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


class TemporaryDataset:
    def __init__(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        for folder_name in SOURCE_FOLDERS:
            shutil.copytree(PROJECT_ROOT / folder_name, self.root / folder_name)

    def cleanup(self) -> None:
        self._temporary_directory.cleanup()


class CurrentDatasetTests(unittest.TestCase):
    def test_current_dataset_has_no_blocking_errors(self) -> None:
        report = validate_data_dir(PROJECT_ROOT)

        self.assertEqual(report.status, "PASS_WITH_WARNING")
        self.assertEqual(report.errors, [])
        self.assertEqual(report.files["plant1_summary"].row_count, 18)
        self.assertEqual(report.files["plant2_summary"].row_count, 18)
        self.assertEqual(report.files["plant1_prior_summary"].row_count, 18)
        self.assertEqual(report.files["plant2_prior_summary"].row_count, 18)
        self.assertEqual(report.files["budgets"].row_count, 18)
        self.assertEqual(report.files["labor_detail"].row_count, 18)
        self.assertEqual(report.files["material_detail"].row_count, 108)
        self.assertEqual(report.files["manufacturing_detail"].row_count, 90)
        self.assertEqual(report.files["market_prices"].row_count, 13)
        self.assertEqual(report.files["industry_benchmarks"].row_count, 15)
        self.assertEqual(report.normalized_counts["market_price_points"], 78)

    def test_current_dataset_warnings_are_expected(self) -> None:
        report = validate_data_dir(PROJECT_ROOT)
        warnings = {(issue.code, issue.key) for issue in report.warnings}

        self.assertNotIn(("W02", "黄芩提取物"), warnings)
        self.assertIn(("W02", "纯化水"), warnings)
        self.assertEqual(sum(issue.code == "W02" for issue in report.warnings), 4)
        self.assertEqual(sum(issue.code == "W09" for issue in report.warnings), 1)

    def test_report_is_json_serializable(self) -> None:
        report = validate_data_dir(PROJECT_ROOT)
        rendered = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
        self.assertIn("PASS_WITH_WARNING", rendered)
        self.assertIn("纯化水", rendered)
        self.assertIn('"budgets"', rendered)

    def test_cli_returns_success_for_current_dataset(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "app.validate",
                "--data-dir",
                str(PROJECT_ROOT),
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS_WITH_WARNING", result.stdout)
        self.assertIn("阻断错误：0", result.stdout)


class NormalizationTests(unittest.TestCase):
    def test_market_wide_row_normalizes_to_six_months(self) -> None:
        row = MarketPriceRow.model_validate(
            {
                "药材名称": "金银花",
                "规格等级": "统货,河南产",
                "单位": "元/kg",
                "1月价格": "125.0",
                "2月价格": "126.5",
                "3月价格": "124.0",
                "4月价格": "130.0",
                "5月价格": "138.0",
                "6月价格": "133.5",
                "价格来源": "亳州中药材市场",
                "趋势分析": "上涨",
            }
        )
        points = normalize_market_prices([row])

        self.assertEqual(len(points), 6)
        self.assertEqual(points[0].month, "2026-01")
        self.assertEqual(points[0].price, Decimal("125.0"))
        self.assertEqual(points[4].month, "2026-05")
        self.assertEqual(points[4].price, Decimal("138.0"))

    def test_industry_unit_cost_conversions(self) -> None:
        self.assertEqual(
            industry_unit_cost_per_box("口服液类", "单位成本(元/支)", "1.05"),
            Decimal("10.50"),
        )
        self.assertEqual(
            industry_unit_cost_per_box("颗粒剂类", "单位成本(元/袋)", "0.40"),
            Decimal("8.00"),
        )
        self.assertEqual(
            industry_unit_cost_per_box("胶囊剂类", "单位成本(元/粒)", "0.30"),
            Decimal("18.00"),
        )

    def test_percentage_is_not_unit_cost(self) -> None:
        value, is_percentage = parse_benchmark_value("62%")
        self.assertTrue(is_percentage)
        self.assertEqual(value, Decimal("0.62"))
        with self.assertRaises(ValueError):
            industry_unit_cost_per_box("口服液类", "材料成本占比", "62%")


class BlockingRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dataset = TemporaryDataset()

    def tearDown(self) -> None:
        self.dataset.cleanup()

    def test_missing_bom_is_blocked(self) -> None:
        path = (
            self.dataset.root
            / "01_成本明细数据"
            / "中药一厂_制造费用明细_2026年1-6月.csv"
        )
        text = path.read_text(encoding="utf-8-sig")
        path.write_text(text, encoding="utf-8", newline="")

        report = validate_data_dir(self.dataset.root)

        self.assertEqual(report.status, "FAIL")
        self.assertIn("B02", issue_codes(report))

    def test_missing_file_is_blocked(self) -> None:
        path = (
            self.dataset.root
            / "02_行业参考数据"
            / "行业成本基准数据_2026.csv"
        )
        path.unlink()

        report = validate_data_dir(self.dataset.root)

        self.assertEqual(report.status, "FAIL")
        self.assertIn("B01", issue_codes(report))

    def test_missing_required_column_is_blocked(self) -> None:
        path = (
            self.dataset.root
            / "01_成本明细数据"
            / "中药一厂_成本汇总_2026年1-6月.csv"
        )
        headers, rows = read_csv_rows(path)
        headers.remove("直接人工(元/盒)")
        for row in rows:
            row.pop("直接人工(元/盒)")
        write_csv_rows(path, headers, rows)

        report = validate_data_dir(self.dataset.root)

        self.assertEqual(report.status, "FAIL")
        self.assertIn("B03", issue_codes(report))

    def test_duplicate_primary_key_is_blocked(self) -> None:
        path = (
            self.dataset.root
            / "01_成本明细数据"
            / "中药二厂_成本汇总_2026年1-6月.csv"
        )
        headers, rows = read_csv_rows(path)
        rows.append(dict(rows[0]))
        write_csv_rows(path, headers, rows)

        report = validate_data_dir(self.dataset.root)

        self.assertEqual(report.status, "FAIL")
        self.assertIn("B04", issue_codes(report))

    def test_empty_required_value_is_blocked(self) -> None:
        path = (
            self.dataset.root
            / "01_成本明细数据"
            / "中药一厂_成本汇总_2026年1-6月.csv"
        )
        headers, rows = read_csv_rows(path)
        rows[0]["产品名称"] = ""
        write_csv_rows(path, headers, rows)

        report = validate_data_dir(self.dataset.root)

        self.assertEqual(report.status, "FAIL")
        self.assertIn("B05", issue_codes(report))

    def test_invalid_numeric_value_is_blocked(self) -> None:
        path = (
            self.dataset.root
            / "01_成本明细数据"
            / "中药二厂_成本汇总_2026年1-6月.csv"
        )
        headers, rows = read_csv_rows(path)
        rows[0]["单位成本(元/盒)"] = "not-a-number"
        write_csv_rows(path, headers, rows)

        report = validate_data_dir(self.dataset.root)

        self.assertEqual(report.status, "FAIL")
        self.assertIn("B06", issue_codes(report))

    def test_invalid_month_is_blocked(self) -> None:
        path = (
            self.dataset.root
            / "01_成本明细数据"
            / "中药一厂_制造费用明细_2026年1-6月.csv"
        )
        headers, rows = read_csv_rows(path)
        rows[0]["月份"] = "May-26"
        write_csv_rows(path, headers, rows)

        report = validate_data_dir(self.dataset.root)

        self.assertEqual(report.status, "FAIL")
        self.assertIn("B07", issue_codes(report))

    def test_invalid_expense_category_is_blocked(self) -> None:
        path = (
            self.dataset.root
            / "01_成本明细数据"
            / "中药一厂_制造费用明细_2026年1-6月.csv"
        )
        headers, rows = read_csv_rows(path)
        rows[0]["费用类别"] = "未知费用"
        write_csv_rows(path, headers, rows)

        report = validate_data_dir(self.dataset.root)

        self.assertEqual(report.status, "FAIL")
        self.assertIn("B08", issue_codes(report))

    def test_negative_cost_is_blocked(self) -> None:
        path = (
            self.dataset.root
            / "01_成本明细数据"
            / "中药一厂_成本汇总_2026年1-6月.csv"
        )
        headers, rows = read_csv_rows(path)
        rows[0]["直接人工(元/盒)"] = "-1.00"
        write_csv_rows(path, headers, rows)

        report = validate_data_dir(self.dataset.root)

        self.assertEqual(report.status, "FAIL")
        self.assertIn("B09", issue_codes(report))

    def test_product_specification_mismatch_is_blocked(self) -> None:
        path = (
            self.dataset.root
            / "01_成本明细数据"
            / "中药一厂_成本汇总_2026年1-6月.csv"
        )
        headers, rows = read_csv_rows(path)
        rows[0]["产品规格"] = "错误规格"
        write_csv_rows(path, headers, rows)

        report = validate_data_dir(self.dataset.root)

        self.assertEqual(report.status, "FAIL")
        self.assertIn("B10", issue_codes(report))

    def test_summary_component_mismatch_is_blocked(self) -> None:
        path = (
            self.dataset.root
            / "01_成本明细数据"
            / "中药一厂_成本汇总_2026年1-6月.csv"
        )
        headers, rows = read_csv_rows(path)
        rows[0]["单位成本(元/盒)"] = "11.70"
        write_csv_rows(path, headers, rows)

        report = validate_data_dir(self.dataset.root)

        self.assertEqual(report.status, "FAIL")
        self.assertIn("B11", issue_codes(report))
        self.assertIn("B12", issue_codes(report))

    def test_material_aggregate_mismatch_is_blocked(self) -> None:
        path = (
            self.dataset.root
            / "01_成本明细数据"
            / "中药一厂_原材料消耗明细_2026年1-6月.csv"
        )
        headers, rows = read_csv_rows(path)
        new_unit_cost = Decimal(rows[0]["单位消耗成本(元/盒)"]) + Decimal("0.10")
        rows[0]["单位消耗成本(元/盒)"] = str(new_unit_cost)
        rows[0]["原材料总成本(元)"] = str(
            new_unit_cost * Decimal(rows[0]["产量(盒)"])
        )
        write_csv_rows(path, headers, rows)

        report = validate_data_dir(self.dataset.root)

        self.assertEqual(report.status, "FAIL")
        self.assertIn("B14", issue_codes(report))

    def test_material_line_total_mismatch_is_blocked(self) -> None:
        path = (
            self.dataset.root
            / "01_成本明细数据"
            / "中药一厂_原材料消耗明细_2026年1-6月.csv"
        )
        headers, rows = read_csv_rows(path)
        rows[0]["原材料总成本(元)"] = "1.00"
        write_csv_rows(path, headers, rows)

        report = validate_data_dir(self.dataset.root)

        self.assertEqual(report.status, "FAIL")
        self.assertIn("B13", issue_codes(report))

    def test_manufacturing_line_total_mismatch_is_blocked(self) -> None:
        path = (
            self.dataset.root
            / "01_成本明细数据"
            / "中药一厂_制造费用明细_2026年1-6月.csv"
        )
        headers, rows = read_csv_rows(path)
        rows[0]["费用总额(元)"] = "1.00"
        write_csv_rows(path, headers, rows)

        report = validate_data_dir(self.dataset.root)

        self.assertEqual(report.status, "FAIL")
        self.assertIn("B15", issue_codes(report))

    def test_manufacturing_aggregate_mismatch_is_blocked(self) -> None:
        path = (
            self.dataset.root
            / "01_成本明细数据"
            / "中药一厂_制造费用明细_2026年1-6月.csv"
        )
        headers, rows = read_csv_rows(path)
        rows = rows[1:]
        write_csv_rows(path, headers, rows)

        report = validate_data_dir(self.dataset.root)

        self.assertEqual(report.status, "FAIL")
        self.assertIn("B16", issue_codes(report))

    def test_cross_file_quantity_mismatch_is_blocked(self) -> None:
        path = (
            self.dataset.root
            / "01_成本明细数据"
            / "中药一厂_制造费用明细_2026年1-6月.csv"
        )
        headers, rows = read_csv_rows(path)
        rows[0]["产量(盒)"] = "45001"
        rows[0]["费用总额(元)"] = str(
            Decimal(rows[0]["单位费用(元/盒)"]) * Decimal("45001")
        )
        write_csv_rows(path, headers, rows)

        report = validate_data_dir(self.dataset.root)

        self.assertEqual(report.status, "FAIL")
        self.assertIn("B17", issue_codes(report))

    def test_budget_reconciliation_mismatch_is_blocked(self) -> None:
        path = (
            self.dataset.root
            / "01_成本明细数据"
            / "中药一厂_预算数据_2026年.csv"
        )
        headers, rows = read_csv_rows(path)
        rows[0]["预算单位成本(元/盒)"] = "999.00"
        write_csv_rows(path, headers, rows)

        report = validate_data_dir(self.dataset.root)

        self.assertEqual(report.status, "FAIL")
        self.assertIn("B19", issue_codes(report))

    def test_labor_total_reconciliation_mismatch_is_blocked(self) -> None:
        path = (
            self.dataset.root
            / "01_成本明细数据"
            / "中药一厂_人工工时明细_2026年1-6月.csv"
        )
        headers, rows = read_csv_rows(path)
        rows[0]["直接人工总额(元)"] = "1.00"
        write_csv_rows(path, headers, rows)

        report = validate_data_dir(self.dataset.root)

        self.assertEqual(report.status, "FAIL")
        self.assertIn("B20", issue_codes(report))

    def test_unexpected_column_is_a_warning(self) -> None:
        path = (
            self.dataset.root
            / "02_行业参考数据"
            / "药材市场价格行情_2026年上半年.csv"
        )
        headers, rows = read_csv_rows(path)
        headers.append("备注")
        for row in rows:
            row["备注"] = "测试"
        write_csv_rows(path, headers, rows)

        report = validate_data_dir(self.dataset.root)

        self.assertNotEqual(report.status, "FAIL")
        self.assertIn("W10", issue_codes(report))


if __name__ == "__main__":
    unittest.main()

"""Golden-scenario and boundary tests for deterministic cost analysis."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from app.analysis import (
    AnalysisError,
    DataQualityError,
    ScenarioNotFoundError,
    analyze_cost,
)
from app.analysis.engine import _alerts, _market_evidence
from app.analysis.models import DetailDriver, MetricComparison
from app.data_quality import load_validated_data


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_FOLDERS = ("01_成本明细数据", "02_行业参考数据")
PERCENT_TOLERANCE = Decimal("0.01")


def by_name(rows):
    return {row.name: row for row in rows}


class GoldenScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.yinhuang = analyze_cost(PROJECT_ROOT, "银黄口服液", "2026-05")
        cls.banlangen = analyze_cost(PROJECT_ROOT, "板蓝根颗粒", "2026-05")
        cls.liuwei = analyze_cost(PROJECT_ROOT, "六味地黄胶囊", "2026-03")

    def assert_percent(self, actual: Decimal | None, expected: str) -> None:
        self.assertIsNotNone(actual)
        assert actual is not None
        self.assertLessEqual(abs(actual - Decimal(expected)), PERCENT_TOLERANCE)

    def test_yinhuang_summary_and_contribution(self) -> None:
        summary = by_name(self.yinhuang.summary)
        contribution = by_name(self.yinhuang.contributions)
        total_contribution = by_name(self.yinhuang.total_cost_contributions)

        self.assertEqual(summary["单位成本"].previous, Decimal("10.90"))
        self.assertEqual(summary["单位成本"].current, Decimal("11.21"))
        self.assertEqual(summary["单位成本"].delta, Decimal("0.31"))
        self.assert_percent(summary["单位成本"].change_rate_pct, "2.84")
        self.assertEqual(summary["总成本"].delta, Decimal("50680"))
        self.assert_percent(contribution["直接材料"].contribution_pct, "70.97")
        self.assertEqual(total_contribution["直接材料"].delta_total_cost, Decimal("34000.00"))
        self.assert_percent(total_contribution["直接材料"].contribution_pct, "67.09")
        self.assert_percent(
            sum(item.contribution_pct for item in self.yinhuang.total_cost_contributions if item.contribution_pct is not None),
            "100.00",
        )

    def test_yinhuang_material_market_and_factory_benchmark(self) -> None:
        materials = by_name(self.yinhuang.material_drivers)
        market = {item.material_name: item for item in self.yinhuang.market_evidence}
        benchmark = by_name(self.yinhuang.factory_benchmark)

        self.assertEqual(materials["金银花"].delta, Decimal("0.15"))
        self.assertEqual(market["金银花"].previous_price, Decimal("130"))
        self.assertEqual(market["金银花"].current_price, Decimal("138"))
        self.assert_percent(market["金银花"].price_change_rate_pct, "6.15")
        self.assertEqual(market["金银花"].causality, "correlation_only")
        self.assertEqual(benchmark["单位成本"].difference, Decimal("-0.39"))
        self.assertEqual(benchmark["单位成本"].direction, "favorable")
        self.assertEqual(self.yinhuang.alerts, [])

    def test_huangqin_extract_uses_only_exact_same_name_market_data(self) -> None:
        names = {item.material_name for item in self.yinhuang.market_evidence}
        self.assertIn("黄芩提取物", names)
        self.assertNotIn("黄芩", names)

    def test_banlangen_golden_values(self) -> None:
        summary = by_name(self.banlangen.summary)
        contribution = by_name(self.banlangen.contributions)
        materials = by_name(self.banlangen.material_drivers)
        market = {item.material_name: item for item in self.banlangen.market_evidence}
        benchmark = by_name(self.banlangen.factory_benchmark)

        self.assertEqual(summary["单位成本"].delta, Decimal("0.23"))
        self.assert_percent(summary["单位成本"].change_rate_pct, "3.18")
        self.assert_percent(contribution["直接材料"].contribution_pct, "78.26")
        self.assertEqual(materials["板蓝根"].delta, Decimal("0.15"))
        self.assert_percent(market["板蓝根"].price_change_rate_pct, "8.47")
        self.assertEqual(benchmark["单位成本"].difference, Decimal("-0.50"))
        self.assertEqual(self.banlangen.alerts, [])

    def test_liuwei_golden_values_and_signed_contribution(self) -> None:
        summary = by_name(self.liuwei.summary)
        contribution = by_name(self.liuwei.contributions)
        benchmark = by_name(self.liuwei.factory_benchmark)

        self.assert_percent(summary["产量"].change_rate_pct, "25.00")
        self.assertEqual(summary["单位成本"].delta, Decimal("-0.58"))
        self.assert_percent(summary["单位成本"].change_rate_pct, "-3.30")
        self.assertEqual(contribution["直接材料"].delta_unit_cost, Decimal("-0.36"))
        self.assert_percent(contribution["直接材料"].contribution_pct, "62.07")
        self.assertGreater(contribution["直接材料"].contribution_pct, Decimal("0"))
        self.assertEqual(benchmark["单位成本"].difference, Decimal("-1.16"))
        self.assertEqual(self.liuwei.alerts, [])

    def test_liuwei_total_cost_bridge_reconciles(self) -> None:
        bridge = self.liuwei.total_cost_bridge
        self.assertEqual(bridge.quantity_effect, Decimal("123200"))
        self.assertEqual(bridge.unit_cost_effect, Decimal("-20300"))
        self.assertEqual(bridge.total_cost_delta, Decimal("102900"))
        self.assertEqual(bridge.reconciliation_difference, Decimal("0"))

    def test_new_yoy_budget_and_labor_metrics_are_available(self) -> None:
        self.assertEqual(self.yinhuang.unavailable_metrics, [])
        metrics = by_name(self.yinhuang.report_metrics)
        self.assertEqual(len(metrics), 33)
        self.assertTrue(all(item.status == "available" for item in metrics.values()))
        self.assert_percent(metrics["单位成本同比"].value, "4.67")
        self.assert_percent(metrics["材料同比"].value, "6.73")
        self.assert_percent(metrics["人工同比"].value, "1.32")
        self.assert_percent(metrics["制造费用同比"].value, "0.85")
        self.assert_percent(metrics["单位成本预算偏差"].value, "5.75")
        self.assert_percent(metrics["材料预算偏差"].value, "7.35")
        self.assert_percent(metrics["本月工时"].value, "231.72")
        self.assertEqual(len(self.yinhuang.prohibited_calculations), 7)
        self.assertIn(
            "实际采购价格差",
            {item.name for item in self.yinhuang.prohibited_calculations},
        )

    def test_industry_unit_cost_is_normalized_to_yuan_per_box(self) -> None:
        industry = by_name(self.yinhuang.industry_benchmark)
        item = industry["单位成本(元/支)"]
        self.assertEqual(item.unit, "元/盒")
        self.assertEqual(item.p50, Decimal("10.50"))
        self.assertEqual(item.current_value, Decimal("11.21"))

    def test_sources_are_absolute_and_exist(self) -> None:
        self.assertEqual(self.yinhuang.data_quality_status, "PASS_WITH_WARNING")
        self.assertEqual(self.yinhuang.data_quality_warning_count, 5)
        self.assertEqual(len(self.yinhuang.sources), 10)
        for source in self.yinhuang.sources:
            path = Path(source.path)
            self.assertTrue(path.is_absolute())
            self.assertTrue(path.is_file())

    def test_result_is_json_serializable_without_float_conversion(self) -> None:
        rendered = json.dumps(
            self.yinhuang.model_dump(mode="json"), ensure_ascii=False
        )
        self.assertIn('"delta": "0.31"', rendered)
        self.assertIn('"causality": "correlation_only"', rendered)


class BoundaryTests(unittest.TestCase):
    def test_january_has_explicitly_unavailable_month_over_month(self) -> None:
        result = analyze_cost(PROJECT_ROOT, "银黄口服液", "2026-01")
        self.assertEqual(result.request.previous_month, "2025-12")
        self.assertTrue(all(item.status == "unavailable" for item in result.summary))
        self.assertTrue(
            all(item.status == "unavailable" for item in result.contributions)
        )
        self.assertEqual(len(result.unavailable_metrics), 6)
        self.assertEqual(
            {item.name for item in result.unavailable_metrics},
            {"上月工时", "上月效率", "上月时薪", "工时环比", "效率环比", "时薪环比"},
        )
        self.assertEqual(result.alerts, [])

    def test_missing_scenario_is_rejected(self) -> None:
        with self.assertRaises(ScenarioNotFoundError):
            analyze_cost(PROJECT_ROOT, "银黄口服液", "2026-12")

    def test_unsupported_target_factory_is_rejected(self) -> None:
        with self.assertRaises(AnalysisError):
            analyze_cost(
                PROJECT_ROOT,
                "银黄口服液",
                "2026-05",
                factory="中药二厂",
            )

    def test_threshold_is_strictly_greater_than_ten_percent(self) -> None:
        exact_ten = MetricComparison(
            name="单位成本",
            unit="元/盒",
            current=Decimal("11"),
            previous=Decimal("10"),
            delta=Decimal("1"),
            change_rate_pct=Decimal("10"),
            status="available",
        )
        above_ten = exact_ten.model_copy(
            update={
                "current": Decimal("11.00001"),
                "delta": Decimal("1.00001"),
                "change_rate_pct": Decimal("10.0001"),
            }
        )
        self.assertEqual(
            _alerts([exact_ten], [], [], "中药一厂", "测试产品", "2026-05"),
            [],
        )
        alerts = _alerts(
            [above_ten], [], [], "中药一厂", "测试产品", "2026-05"
        )
        self.assertEqual(len(alerts), 1)

    def test_ambiguous_market_grades_are_not_auto_selected(self) -> None:
        _, bundle = load_validated_data(PROJECT_ROOT)
        original = next(
            row for row in bundle.market_prices if row.material_name == "金银花"
        )
        ambiguous = original.model_copy(update={"grade": "另一规格"})
        altered_bundle = replace(
            bundle, market_prices=[*bundle.market_prices, ambiguous]
        )
        driver = DetailDriver(
            name="金银花",
            unit="元/盒",
            current=Decimal("3.50"),
            previous=Decimal("3.35"),
            delta=Decimal("0.15"),
            change_rate_pct=Decimal("4.4776"),
            contribution_pct=Decimal("48.3871"),
            status="available",
        )

        evidence = _market_evidence(
            altered_bundle, [driver], "2026-05", "2026-04"
        )

        self.assertNotIn("金银花", {item.material_name for item in evidence})

    def test_data_quality_failure_blocks_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for folder in SOURCE_FOLDERS:
                shutil.copytree(PROJECT_ROOT / folder, root / folder)
            path = root / "01_成本明细数据" / "中药一厂_成本汇总_2026年1-6月.csv"
            text = path.read_text(encoding="utf-8-sig")
            path.write_text(text, encoding="utf-8", newline="")
            with self.assertRaises(DataQualityError):
                analyze_cost(root, "银黄口服液", "2026-05")

    def test_validated_bundle_exposes_only_typed_valid_rows(self) -> None:
        report, bundle = load_validated_data(PROJECT_ROOT)
        self.assertEqual(report.status, "PASS_WITH_WARNING")
        self.assertEqual(len(bundle.plant1_summary), 18)
        self.assertEqual(len(bundle.plant2_summary), 18)
        self.assertEqual(len(bundle.material_detail), 108)
        self.assertEqual(len(bundle.manufacturing_detail), 90)

    def test_cli_json_output(self) -> None:
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "app.analyze",
                "--data-dir",
                str(PROJECT_ROOT),
                "--product",
                "银黄口服液",
                "--month",
                "2026-05",
                "--json",
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["request"]["product"], "银黄口服液")
        self.assertEqual(payload["summary"][4]["delta"], "0.31")


if __name__ == "__main__":
    unittest.main()

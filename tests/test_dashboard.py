"""Contract tests for the unified read-only cost-analysis dashboard."""

from __future__ import annotations

from io import BytesIO
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from docx import Document

from app.dashboard import create_dashboard_app
from app.reporting import ReportContract
from app.rpa import TransportResponse


ROOT = Path(__file__).resolve().parents[1]


class SuccessfulMockRpaTransport:
    def post_json(self, url: str, payload: dict, timeout: float) -> TransportResponse:
        return TransportResponse(
            status_code=200,
            payload={
                "code": 200,
                "message": "任务创建成功，已分发至责任人",
                "data": {
                    "task_id": payload["task_id"],
                    "status": "sent",
                    "notify_status": {
                        "wechat": "已发送至 " + payload["assignee"]["name"]
                        + "(" + payload["assignee"]["department"] + ")",
                    },
                    "tracking_url": "http://127.0.0.1:8090/api/rpa/tasks/"
                    + payload["task_id"],
                },
            },
        )


class DashboardWebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.client = TestClient(
            create_dashboard_app(
                data_dir=ROOT,
                report_output_dir=Path(cls.temporary.name) / "reports",
                rpa_transport=SuccessfulMockRpaTransport(),
            )
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        cls.temporary.cleanup()

    def test_dashboard_options_are_derived_from_validated_data(self) -> None:
        response = self.client.get("/api/options")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["analysis_types"], ["月度成本分析", "季度成本分析", "专题分析"])
        self.assertEqual(set(payload["products"]), {"银黄口服液", "板蓝根颗粒", "六味地黄胶囊"})
        self.assertEqual(payload["months"], [f"2026-{month:02d}" for month in range(1, 7)])
        self.assertEqual(payload["quarters"], ["2026-Q1", "2026-Q2"])
        self.assertEqual(payload["topics"], ["原材料涨价专项", "工厂成本差异专项"])
        self.assertEqual(payload["rag"]["default"], "native")
        self.assertEqual(payload["rag"]["ranking_policy"], "governed-native")
        self.assertIn(payload["data_quality"]["status"], {"PASS", "PASS_WITH_WARNING"})
        self.assertEqual(payload["data_quality"]["validated_files"], 10)

    def test_monthly_dashboard_matches_golden_scenario(self) -> None:
        response = self.client.post(
            "/api/analyze",
            json={
                "analysis_type": "月度成本分析",
                "product": "银黄口服液",
                "month": "2026-05",
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        unit = payload["kpis"]["unit_cost"]
        self.assertEqual(Decimal(str(unit["previous"])), Decimal("10.9"))
        self.assertEqual(Decimal(str(unit["current"])), Decimal("11.21"))
        self.assertEqual(Decimal(str(unit["delta"])), Decimal("0.31"))
        self.assertEqual(
            Decimal(str(unit["change_rate_pct"])).quantize(Decimal("0.01")),
            Decimal("2.84"),
        )

        changes = [Decimal(str(item["delta_unit_cost"])) for item in payload["waterfall"]]
        self.assertEqual(sum(changes), Decimal("0.31"))
        material = next(item for item in payload["waterfall"] if item["name"] == "直接材料")
        self.assertEqual(
            Decimal(str(material["contribution_pct"])).quantize(Decimal("0.01")),
            Decimal("70.97"),
        )
        total_contributions = payload["total_cost_contributions"]
        total_material = next(item for item in total_contributions if item["name"] == "直接材料")
        self.assertEqual(Decimal(str(total_material["delta_total_cost"])), Decimal("34000.0"))
        self.assertEqual(
            Decimal(str(total_material["contribution_pct"])).quantize(Decimal("0.01")),
            Decimal("67.09"),
        )
        self.assertLess(
            abs(sum(Decimal(str(item["contribution_pct"])) for item in total_contributions) - Decimal("100")),
            Decimal("0.000000001"),
        )
        bridge = payload["total_cost_bridge"]
        self.assertEqual(bridge["status"], "available")
        self.assertLessEqual(
            abs(Decimal(str(bridge["reconciliation_difference"]))), Decimal("0.01")
        )
        self.assertEqual(
            Decimal(str(bridge["quantity_effect"])) + Decimal(str(bridge["unit_cost_effect"])),
            Decimal(str(bridge["total_cost_delta"])),
        )
        self.assertEqual(
            Decimal(str(bridge["previous_total_cost"])) + Decimal(str(bridge["total_cost_delta"])),
            Decimal(str(bridge["current_total_cost"])),
        )

        benchmark = payload["kpis"]["factory_benchmark"]
        self.assertEqual(Decimal(str(benchmark["difference"])), Decimal("-0.39"))
        self.assertEqual(benchmark["direction"], "favorable")
        self.assertEqual(len(payload["trend"]), 5)
        self.assertEqual(payload["trend_context"]["point_count"], 5)
        self.assertEqual(
            payload["trend_context"]["title"],
            "截至分析期的可得月份趋势（共5个月）",
        )
        self.assertEqual(payload["trend_context"]["period_range"], "2026-01至2026-05")
        self.assertEqual(payload["meta"]["report_number"], "CA-202605-YH-001")
        self.assertTrue(payload["evidence"]["recipe_citation"])
        self.assertEqual(
            set(payload["evidence"]),
            {
                "recipe_citation",
                "process_citation",
                "gmp_citation",
                "industry_citation",
                "market_citation",
                "factory_benchmark_citation",
                "equipment_citation",
                "anomaly_history_citation",
            },
        )
        self.assertTrue(all(payload["evidence"].values()))
        self.assertIn("同集团工厂对标基线", payload["evidence"]["factory_benchmark_citation"])
        self.assertIn("历史成本异常处理记录", payload["evidence"]["anomaly_history_citation"])
        self.assertNotIn("核心摘要", payload["evidence"]["gmp_citation"])
        self.assertEqual(
            Decimal(str(payload["comparisons"]["单位成本同比"]["value"])).quantize(Decimal("0.01")),
            Decimal("4.67"),
        )
        for key in ("direct_material", "direct_labor", "manufacturing_overhead"):
            self.assertEqual(payload["kpis"][key]["status"], "available")
        for name in (
            "材料同比",
            "材料预算偏差",
            "人工同比",
            "人工预算偏差",
            "制造费用同比",
            "制造费用预算偏差",
        ):
            self.assertEqual(payload["comparisons"][name]["status"], "available")
        self.assertEqual(
            Decimal(str(payload["comparisons"]["材料预算偏差"]["value"])).quantize(Decimal("0.01")),
            Decimal("7.35"),
        )
        self.assertEqual(payload["unavailable_metrics"], [])
        self.assertEqual(payload["benchmark_tree"]["name"], "银黄口服液")

    def test_latest_month_returns_six_month_trend(self) -> None:
        response = self.client.post(
            "/api/analyze",
            json={
                "analysis_type": "月度成本分析",
                "product": "板蓝根颗粒",
                "month": "2026-06",
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        rows = payload["trend"]
        self.assertEqual(
            [row["month"] for row in rows],
            [f"2026-{month:02d}" for month in range(1, 7)],
        )
        self.assertEqual(payload["trend_context"]["kicker"], "近 6 个月")
        self.assertEqual(payload["trend_context"]["title"], "单位成本趋势")
        self.assertTrue(payload["trend_context"]["is_complete_six_months"])

    def test_monthly_trend_labels_follow_available_data_window(self) -> None:
        for month, count in (("2026-01", 1), ("2026-03", 3), ("2026-05", 5), ("2026-06", 6)):
            with self.subTest(month=month):
                response = self.client.post(
                    "/api/analyze",
                    json={
                        "analysis_type": "月度成本分析",
                        "product": "银黄口服液",
                        "month": month,
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                context = response.json()["trend_context"]
                self.assertEqual(context["point_count"], count)
                self.assertEqual(context["start_period"], "2026-01")
                self.assertEqual(context["end_period"], month)
                self.assertIn("未补造", context["boundary_note"])
                if count == 6:
                    self.assertEqual(context["title"], "单位成本趋势")
                    self.assertTrue(context["is_complete_six_months"])
                else:
                    self.assertEqual(
                        context["title"],
                        f"截至分析期的可得月份趋势（共{count}个月）",
                    )
                    self.assertFalse(context["is_complete_six_months"])

    def test_heatmap_api_returns_all_products_months_and_factors(self) -> None:
        response = self.client.get("/api/heatmap")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(len(payload["products"]), 3)
        self.assertEqual(len(payload["months"]), 6)
        self.assertEqual(payload["factors"], ["直接材料", "直接人工", "制造费用", "单位成本"])
        self.assertEqual(len(payload["cells"]), 72)
        january = next(
            item for item in payload["cells"]
            if item["product"] == "银黄口服液" and item["month"] == "2026-01" and item["factor"] == "单位成本"
        )
        self.assertIsNone(january["change_rate_pct"])
        may = next(
            item for item in payload["cells"]
            if item["product"] == "银黄口服液" and item["month"] == "2026-05" and item["factor"] == "单位成本"
        )
        self.assertEqual(Decimal(str(may["current"])), Decimal("11.21"))
        self.assertEqual(Decimal(str(may["factory_difference"])), Decimal("-0.39"))
        self.assertEqual(Decimal(str(may["change_rate_pct"])).quantize(Decimal("0.01")), Decimal("2.84"))

    def test_quarterly_dashboard_uses_total_cost_divided_by_total_quantity(self) -> None:
        response = self.client.post(
            "/api/analyze",
            json={
                "analysis_type": "季度成本分析",
                "product": "银黄口服液",
                "quarter": "2026-Q2",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["meta"]["period"], "2026-Q2")
        self.assertEqual(payload["meta"]["previous_period"], "2026-Q1")
        self.assertEqual(payload["kpis"]["quantity"]["current"], 163000)
        self.assertEqual(Decimal(str(payload["kpis"]["total_cost"]["current"])), Decimal("1796180.0"))
        expected = Decimal("1796180") / Decimal("163000")
        actual = Decimal(str(payload["kpis"]["unit_cost"]["current"]))
        self.assertLess(abs(actual - expected), Decimal("0.000000000001"))
        contribution = sum(
            Decimal(str(item["contribution_pct"])) for item in payload["waterfall"]
        )
        self.assertLess(abs(contribution - Decimal("100")), Decimal("0.000000001"))
        total_cost_contribution = sum(
            Decimal(str(item["contribution_pct"])) for item in payload["total_cost_contributions"]
        )
        self.assertLess(abs(total_cost_contribution - Decimal("100")), Decimal("0.000000001"))
        bridge = payload["total_cost_bridge"]
        self.assertEqual(bridge["status"], "available")
        self.assertLessEqual(
            abs(Decimal(str(bridge["reconciliation_difference"]))), Decimal("0.01")
        )
        self.assertLessEqual(
            abs(
                Decimal(str(bridge["quantity_effect"]))
                + Decimal(str(bridge["unit_cost_effect"]))
                - Decimal(str(bridge["total_cost_delta"]))
            ),
            Decimal("0.01"),
        )
        self.assertEqual([item["month"] for item in payload["trend"]], ["2026-Q1", "2026-Q2"])
        self.assertEqual(payload["trend_context"]["point_count"], 2)
        self.assertEqual(payload["trend_context"]["period_range"], "2026-Q1至2026-Q2")
        self.assertEqual(payload["unavailable_metrics"], [])
        self.assertEqual(
            Decimal(str(payload["comparisons"]["单位成本同比"]["value"])).quantize(Decimal("0.01")),
            Decimal("3.79"),
        )
        for key in ("direct_material", "direct_labor", "manufacturing_overhead"):
            self.assertEqual(payload["kpis"][key]["status"], "available")
        for name in (
            "材料同比",
            "材料预算偏差",
            "人工同比",
            "人工预算偏差",
            "制造费用同比",
            "制造费用预算偏差",
        ):
            self.assertEqual(payload["comparisons"][name]["status"], "available")
        self.assertEqual(payload["benchmark_tree"]["name"], "银黄口服液")

    def test_quarterly_analysis_rejects_incomplete_quarter(self) -> None:
        response = self.client.post(
            "/api/analyze",
            json={
                "analysis_type": "季度成本分析",
                "product": "银黄口服液",
                "quarter": "2026-Q3",
            },
        )
        self.assertEqual(response.status_code, 422)

    def test_special_analysis_preserves_monthly_numbers_and_adds_boundary(self) -> None:
        response = self.client.post(
            "/api/analyze",
            json={
                "analysis_type": "专题分析",
                "product": "六味地黄胶囊",
                "month": "2026-03",
                "topic": "工厂成本差异专项",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["meta"]["topic"], "工厂成本差异专项")
        self.assertEqual(payload["kpis"]["factory_benchmark"]["difference"], -1.16)
        self.assertIn("不把差异直接归因于单一事件", payload["narratives"]["需关注问题"])

    def test_unsupported_analysis_type_is_rejected(self) -> None:
        response = self.client.post(
            "/api/analyze",
            json={
                "analysis_type": "预测分析",
                "product": "银黄口服液",
                "month": "2026-05",
            },
        )
        self.assertEqual(response.status_code, 422)

    def test_dashboard_page_has_required_echarts_containers(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("echarts@6.0.0", response.text)
        self.assertIn('id="trendChart"', response.text)
        self.assertIn('id="contributionChart"', response.text)
        self.assertIn("总成本变动贡献度", response.text)
        self.assertIn('id="waterfallChart"', response.text)
        self.assertIn('id="totalCostBridgeChart"', response.text)
        self.assertIn("总成本变动桥接", response.text)
        self.assertIn('class="boundary bridge-boundary-note"', response.text)
        self.assertIn('id="structureChart"', response.text)
        self.assertIn('id="driverContributionChart"', response.text)
        self.assertEqual(response.text.count('id="structureChart"'), 1)
        self.assertLess(
            response.text.index('id="totalCostBridgeChart"'),
            response.text.index('id="structureChart"'),
        )
        self.assertLess(
            response.text.index('id="structureChart"'),
            response.text.index('id="heatmapSection"'),
        )
        self.assertNotIn('id="overview"', response.text)
        self.assertNotIn('class="kpi-number"', response.text)
        self.assertNotIn('class="kpi-unit"', response.text)
        self.assertIn('id="benchmarkTitle"', response.text)
        self.assertNotIn("竞赛原型环境", response.text)
        self.assertNotIn("模拟数据 · 受控闭环", response.text)
        self.assertIn('id="readingPosition"', response.text)
        self.assertIn('data-module="趋势图表"', response.text)
        self.assertIn('id="reportButton"', response.text)
        self.assertIn('id="reportPanel"', response.text)
        self.assertNotIn('id="markdownLink"', response.text)
        self.assertNotIn('id="jsonLink"', response.text)
        self.assertLess(
            response.text.index('id="reportPanel"'),
            response.text.index('id="comparisonSection"'),
        )
        self.assertIn('id="workflowPanel"', response.text)
        self.assertIn('id="candidateButton"', response.text)
        self.assertIn('id="approvalForm"', response.text)
        self.assertIn('id="submitButton"', response.text)
        self.assertIn('id="quarterSelect"', response.text)
        self.assertIn('id="topicSelect"', response.text)
        self.assertIn('id="useLlmToggle"', response.text)
        self.assertIn("受治理引用", response.text)
        self.assertIn('id="heatmapChart"', response.text)
        self.assertIn('id="heatmapMetric"', response.text)
        self.assertIn('id="yearBudgetCards"', response.text)
        self.assertIn('id="laborMetricCards"', response.text)
        self.assertIn('id="laborEfficiencySection"', response.text)
        self.assertIn("对标差异总览", response.text)
        self.assertIn('aria-label="对标差异总览表"', response.text)
        self.assertIn('id="benchmarkProduct"', response.text)
        self.assertIn('id="benchmarkTargetHeader"', response.text)
        self.assertIn("差异金额（元/盒）", response.text)
        self.assertIn("差异率", response.text)
        self.assertIn("评价", response.text)
        self.assertIn('id="businessPeriodLabel"', response.text)
        self.assertIn('<span class="section-kicker">分析对象</span><h2 id="scenarioTitle">—</h2>', response.text)
        self.assertIn('<span class="section-kicker">人工分析</span><h3 id="laborComparisonTitle">人工投入与生产效率分析</h3>', response.text)
        self.assertIn('id="laborInterpretation"', response.text)
        self.assertIn('id="laborManagementNote"', response.text)
        self.assertNotIn('id="laborBoundary"', response.text)
        self.assertIn('id="conclusionLabel">本月分析摘要</h3>', response.text)
        self.assertIn('id="analysisSummarySection"', response.text)
        self.assertNotIn('class="insight-icon"', response.text)
        self.assertLess(
            response.text.index('id="analysisSummarySection"'),
            response.text.index('id="comparisonSection"'),
        )
        styles = (ROOT / "app" / "dashboard" / "static" / "styles.css").read_text(
            encoding="utf-8"
        )
        self.assertRegex(
            styles,
            r"\.analysis-summary-panel\s*\{[^}]*margin:\s*24px 0 8px",
        )
        self.assertRegex(
            styles,
            r"\.comparison-panel\s*\{[^}]*margin:\s*8px 0 16px",
        )
        self.assertIn('id="comparisonBoundary">绿色表示经营有利，红色表示经营不利。</p>', response.text)
        self.assertNotIn("一眼看懂", response.text)
        self.assertNotIn("V1.1 新增数据", response.text)
        self.assertIn('id="trackingGenerated"', response.text)
        self.assertIn('id="trackingConfirmed"', response.text)
        self.assertIn('id="trackingDelivered"', response.text)
        self.assertIn("三维交叉分析热力图", response.text)

        script = (ROOT / "app" / "dashboard" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        for label in (
            "产品配方",
            "生产工艺",
            "GMP 规范（原文）",
            "行业基准",
            "药材市场行情",
            "同集团工厂对标",
            "设备记录",
            "历史异常处置",
        ):
            self.assertIn(label, script)
        self.assertIn('data-evidence-key="${escapeHtml(key)}"', script)

    def test_all_analysis_types_expose_the_complete_governed_evidence_card_set(self) -> None:
        expected = {
            "recipe_citation",
            "process_citation",
            "gmp_citation",
            "industry_citation",
            "market_citation",
            "factory_benchmark_citation",
            "equipment_citation",
            "anomaly_history_citation",
        }
        scenarios = (
            {"analysis_type": "月度成本分析", "product": "银黄口服液", "month": "2026-05"},
            {"analysis_type": "季度成本分析", "product": "板蓝根颗粒", "quarter": "2026-Q2"},
            {
                "analysis_type": "专题分析",
                "product": "六味地黄胶囊",
                "month": "2026-03",
                "topic": "工厂成本差异专项",
            },
        )
        for request in scenarios:
            with self.subTest(analysis_type=request["analysis_type"]):
                response = self.client.post("/api/analyze", json=request)
                self.assertEqual(response.status_code, 200, response.text)
                evidence = response.json()["evidence"]
                self.assertEqual(set(evidence), expected)
                self.assertTrue(all(evidence.values()))

    def test_sidebar_navigation_is_interactive_and_workflow_is_reachable(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn('href="#workflowPanel"', response.text)
        self.assertIn('id="workflowPanel" data-module="整改闭环">', response.text)
        self.assertNotIn('id="workflowPanel" hidden', response.text)
        self.assertEqual(response.text.count('aria-current="page"'), 1)

        script = (ROOT / "app" / "dashboard" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        styles = (ROOT / "app" / "dashboard" / "static" / "styles.css").read_text(
            encoding="utf-8"
        )
        self.assertIn("function setActiveNavigation(item)", script)
        self.assertIn("function syncActiveNavigation()", script)
        self.assertIn("const atPageBottom =", script)
        self.assertIn("setActiveNavigation(lastVisible);", script)
        self.assertIn('target.scrollIntoView({ behavior: "smooth"', script)
        self.assertIn('$("workflowPanel").hidden = false;', script)
        self.assertIn('$("candidateButton").disabled = !reportReady;', script)
        self.assertNotIn(".nav-item:not(.active)", styles)

    def test_charts_are_initialized_after_dashboard_becomes_visible(self) -> None:
        script = (ROOT / "app" / "dashboard" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        render_start = script.index("async function render(result)")
        render_end = script.index("async function analyze()")
        render_body = script[render_start:render_end]
        self.assertLess(
            render_body.index('$("dashboard").hidden = false;'),
            render_body.index("renderTrend(result.trend);"),
        )
        self.assertIn("requestAnimationFrame", render_body)
        self.assertIn("renderDriverContribution(result.material_drivers);", render_body)
        self.assertIn("ResizeObserver", script)

    def test_material_driver_table_and_contribution_chart_share_one_panel(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.text
        detail_start = html.index('id="detailSection"')
        detail_end = html.index('id="laborEfficiencySection"')
        detail_html = html[detail_start:detail_end]
        self.assertIn('class="driver-analysis-grid"', detail_html)
        self.assertIn('id="driverRows"', detail_html)
        self.assertIn('id="driverContributionChart"', detail_html)
        self.assertIn('class="boundary driver-evidence-boundary"', detail_html)
        self.assertIn('class="material-narrative-copy"', detail_html)
        script = (ROOT / "app" / "dashboard" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        styles = (ROOT / "app" / "dashboard" / "static" / "styles.css").read_text(
            encoding="utf-8"
        )
        self.assertIn("function renderDriverContribution(items)", script)
        self.assertIn("function renderMaterialNarrative(value)", script)
        self.assertIn('const marker = "建议：";', script)
        self.assertIn('name: "单位成本贡献度"', script)
        self.assertRegex(
            styles,
            r"\.driver-analysis-grid\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1\.62fr\)\s*minmax\(360px,\s*1fr\)",
        )
        self.assertRegex(
            styles,
            r"\.detail-panel > \.driver-evidence-boundary\s*\{[^}]*font-size:\s*14px",
        )
        self.assertRegex(
            styles,
            r"\.driver-table th:not\(:first-child\), \.driver-table td:not\(:first-child\)\s*\{[^}]*text-align:\s*center",
        )
        self.assertRegex(
            styles,
            r"\.driver-table td\s*\{[^}]*font-size:\s*15px",
        )

    def test_waterfall_uses_signed_cumulative_coordinates(self) -> None:
        script = (ROOT / "app" / "dashboard" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        waterfall_start = script.index("function waterfallData(items)")
        waterfall_end = script.index("function renderStructure(items)")
        waterfall_body = script[waterfall_start:waterfall_end]
        self.assertIn("value: [index, start, cumulative, delta, 0]", waterfall_body)
        self.assertIn('type: "custom"', waterfall_body)
        self.assertIn("api.coord", waterfall_body)
        self.assertNotIn('name: "辅助"', waterfall_body)

    def test_contribution_chart_uses_signed_horizontal_bars(self) -> None:
        script = (ROOT / "app" / "dashboard" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        contribution_start = script.index("function renderContribution(items)")
        contribution_end = script.index("function waterfallData(items)")
        contribution_body = script[contribution_start:contribution_end]
        self.assertIn('type: "bar"', contribution_body)
        self.assertIn("item.contribution_pct", contribution_body)
        self.assertIn("item.delta_total_cost", contribution_body)
        self.assertIn('delta < 0 ? "#21835f"', contribution_body)
        self.assertIn('data: [{ xAxis: 0 }]', contribution_body)
        self.assertIn("result.total_cost_contributions", script)

    def test_total_cost_bridge_uses_quantity_and_unit_cost_effects(self) -> None:
        script = (ROOT / "app" / "dashboard" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        bridge_start = script.index("function totalCostBridgeData(bridge)")
        bridge_end = script.index("function renderStructure(items)")
        bridge_body = script[bridge_start:bridge_end]
        self.assertIn('"上期总成本", "产量影响", "单位成本影响", "本期总成本"', bridge_body)
        self.assertIn("bridge.quantity_effect", bridge_body)
        self.assertIn("bridge.unit_cost_effect", bridge_body)
        self.assertIn('kind === 1 ? "#d99832"', bridge_body)
        self.assertIn('kind === 2 ? (delta >= 0 ? "#b94343" : "#21835f")', bridge_body)
        self.assertIn("result.total_cost_bridge", script)

    def test_total_cost_bridge_and_structure_use_shared_two_column_row(self) -> None:
        styles = (ROOT / "app" / "dashboard" / "static" / "styles.css").read_text(
            encoding="utf-8"
        )
        self.assertRegex(
            styles,
            r"\.bridge-structure-grid\s*\{[^}]*grid-template-columns:\s*minmax\(0, 3fr\) minmax\(320px, 2fr\)",
        )
        self.assertRegex(
            styles,
            r"@media \(max-width: 1180px\)[\s\S]*?\.bridge-structure-grid\s*\{[^}]*grid-template-columns:\s*1fr",
        )

    def test_structure_chart_labels_each_segment_and_centers_total(self) -> None:
        script = (ROOT / "app" / "dashboard" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        structure_start = script.index("function renderStructure(items)")
        structure_end = script.index("function renderBenchmarkTree(result)")
        structure_body = script[structure_start:structure_end]
        self.assertIn("const totalUnitCost = items.reduce", structure_body)
        self.assertIn('legend: { show: false }', structure_body)
        self.assertIn('show: true, position: "outside"', structure_body)
        self.assertIn("formatMoney(p.value)", structure_body)
        self.assertIn("Number(p.data.share).toFixed(2)", structure_body)
        self.assertIn('labelLine: { show: true', structure_body)
        self.assertIn('name: "单位成本合计", type: "pie", radius: [0, 0], center: ["50%", "50%"]', structure_body)
        self.assertIn('show: true, position: "center"', structure_body)
        self.assertIn('formatter: `{total|${formatMoney(totalUnitCost, 2)}}', structure_body)

    def test_explanatory_typography_uses_readable_sizes(self) -> None:
        styles = (ROOT / "app" / "dashboard" / "static" / "styles.css").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(".analysis-overview-panel", styles)
        self.assertNotIn(".kpi-grid", styles)
        self.assertRegex(styles, r"\.business-card-head h5\s*\{[^}]*font-size:\s*16px")
        self.assertRegex(styles, r"\.business-component-label\s*\{[^}]*font-size:\s*12px")
        self.assertRegex(styles, r"\.comparison-point-head span\s*\{[^}]*font-size:\s*14px")
        self.assertRegex(styles, r"\.comparison-point small\s*\{[^}]*font-size:\s*13px")
        self.assertRegex(styles, r"table\s*\{[^}]*font-size:\s*13px")
        self.assertRegex(styles, r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important")

    def test_benchmark_overview_renders_complete_difference_dimensions(self) -> None:
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("产品 × 成本要素 × 差异金额 × 差异率", page.text)
        self.assertEqual(page.text.count('class="benchmark-centered-heading"'), 5)
        script = (ROOT / "app" / "dashboard" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        styles = (ROOT / "app" / "dashboard" / "static" / "styles.css").read_text(
            encoding="utf-8"
        )
        table_start = script.index("function renderTables(result)")
        table_end = script.index("function escapeHtml(value)")
        table_body = script[table_start:table_end]
        for field in (
            "result.meta.product",
            "result.meta.factory",
            "result.meta.benchmark_factory",
            "item.difference",
            "item.difference_rate_pct",
            "item.direction",
        ):
            self.assertIn(field, table_body)
        self.assertIn('rowspan="${benchmarkRows.length}"', table_body)
        self.assertIn('item.direction === "favorable" ? "有利"', table_body)
        self.assertIn('item.direction === "unfavorable" ? "不利"', table_body)
        self.assertIn('class="benchmark-value-cell"', table_body)
        self.assertIn('class="benchmark-outcome-cell"', table_body)
        self.assertIn(".benchmark-overview-table", styles)
        self.assertIn(".benchmark-overview-table .benchmark-centered-heading", styles)
        self.assertIn(".benchmark-overview-table .benchmark-value-cell", styles)
        self.assertIn(".benchmark-overview-table .benchmark-outcome-cell", styles)
        self.assertRegex(
            styles,
            r"\.benchmark-overview-table \.benchmark-outcome-cell\s*\{[^}]*text-align:\s*center",
        )
        self.assertIn(".outcome-good", styles)
        self.assertIn(".outcome-bad", styles)

    def test_benchmark_tree_matches_overview_rows_and_preserves_data_boundary(self) -> None:
        response = self.client.post(
            "/api/analyze",
            json={
                "analysis_type": "月度成本分析",
                "product": "银黄口服液",
                "month": "2026-06",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        overview = {item["name"]: item for item in payload["factory_benchmark"]}
        unit_node = payload["benchmark_tree"]["children"][0]
        for field in (
            "target_value",
            "benchmark_value",
            "difference",
            "difference_rate_pct",
            "direction",
            "status",
        ):
            self.assertEqual(unit_node[field], overview["单位成本"][field])
        components = {item["name"]: item for item in unit_node["children"]}
        self.assertEqual(set(components), {"直接材料", "直接人工", "制造费用"})
        for name, node in components.items():
            for field in (
                "target_value",
                "benchmark_value",
                "difference",
                "difference_rate_pct",
                "direction",
                "status",
            ):
                self.assertEqual(node[field], overview[name][field])
        self.assertTrue(components["直接材料"]["children"])
        self.assertEqual(
            [leaf["name"] for leaf in components["直接材料"]["children"]],
            [
                "金银花",
                "黄芩提取物",
                "蔗糖",
                "苯甲酸钠",
                "纯化水",
                "包装材料(盒+说明书)",
            ],
        )
        self.assertTrue(all(
            leaf["status"] == "unavailable" and leaf["benchmark_value"] is None
            for leaf in components["直接材料"]["children"]
        ))

    def test_benchmark_tree_uses_readable_semantic_card_nodes(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("成本差异结构树", response.text)
        self.assertIn('id="benchmarkTreeHeadline"', response.text)
        self.assertIn('id="benchmarkTreeBasis"', response.text)
        self.assertIn("经营有利", response.text)
        self.assertIn("经营不利", response.text)
        self.assertIn("对标明细暂缺", response.text)
        script = (ROOT / "app" / "dashboard" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        styles = (ROOT / "app" / "dashboard" / "static" / "styles.css").read_text(
            encoding="utf-8"
        )
        tree_start = script.index("function renderBenchmarkTree(result)")
        tree_end = script.index("function heatmapMetricValue")
        tree_body = script[tree_start:tree_end]
        self.assertIn('metric_name: "单位成本总差异"', tree_body)
        self.assertIn('symbol: "roundRect"', tree_body)
        self.assertIn("result.meta.factory", tree_body)
        self.assertIn("result.meta.benchmark_factory", tree_body)
        self.assertIn("outcomeFor", tree_body)
        self.assertIn("initialTreeDepth: 3", tree_body)
        self.assertIn("treeDescription", tree_body)
        self.assertIn("未提供原材料明细，因此叶子节点不计算两厂差异", tree_body)
        self.assertIn("description: treeDescription", tree_body)
        self.assertRegex(styles, r"\.chart\.benchmark-tree-chart\s*\{[^}]*min-width:\s*1060px[^}]*height:\s*650px")
        self.assertIn(".tree-legend-good", styles)
        self.assertIn(".tree-legend-bad", styles)
        self.assertIn(".tree-legend-missing", styles)

    def test_report_generation_has_honest_progress_feedback(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="reportProgress"', response.text)
        self.assertIn('role="progressbar"', response.text)
        self.assertIn("正在生成报告", response.text)
        script = (ROOT / "app" / "dashboard" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('progress.hidden = false', script)
        self.assertIn('"渲染并校验PDF"', script)
        self.assertIn('setProgress(100, "报告生成完成")', script)

    def test_benchmark_attribution_and_recommendations_are_governed_api_outputs(self) -> None:
        for request in (
            {
                "analysis_type": "月度成本分析",
                "product": "银黄口服液",
                "month": "2026-06",
            },
            {
                "analysis_type": "季度成本分析",
                "product": "银黄口服液",
                "quarter": "2026-Q2",
            },
        ):
            with self.subTest(analysis_type=request["analysis_type"]):
                response = self.client.post("/api/analyze", json=request)
                self.assertEqual(response.status_code, 200, response.text)
                payload = response.json()
                narrative = payload["narratives"]["差异归因分析文本"]
                self.assertGreaterEqual(len(narrative.split("\n\n")), 3)
                self.assertIn("总体差异", narrative)
                self.assertIn("原因研判", narrative)
                self.assertIn("证据边界", narrative)
                self.assertEqual(len(payload["recommendations"]), 2)
                for item in payload["recommendations"]:
                    self.assertEqual(
                        set(item),
                        {
                            "sequence",
                            "action",
                            "owner",
                            "priority",
                            "expected_effect",
                            "due",
                        },
                    )
                    self.assertTrue(item["action"])
                    self.assertTrue(item["expected_effect"])
                self.assertNotIn("benchmark_attribution", payload)

    def test_benchmark_attribution_and_recommendations_change_with_scenario(self) -> None:
        scenarios = (
            ("银黄口服液", "2026-06"),
            ("板蓝根颗粒", "2026-05"),
            ("六味地黄胶囊", "2026-03"),
        )
        payloads = []
        for product, month in scenarios:
            response = self.client.post(
                "/api/analyze",
                json={
                    "analysis_type": "月度成本分析",
                    "product": product,
                    "month": month,
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            payloads.append(response.json())

        narratives = [
            item["narratives"]["差异归因分析文本"] for item in payloads
        ]
        recommendation_pairs = [
            tuple(row["action"] for row in item["recommendations"])
            for item in payloads
        ]
        self.assertEqual(len(set(narratives)), len(scenarios))
        self.assertEqual(len(set(recommendation_pairs)), len(scenarios))
        self.assertIn("直接人工差异绝对额最大", narratives[0])
        self.assertIn("板蓝根", recommendation_pairs[1][0])
        self.assertIn("制造费用", recommendation_pairs[1][1])
        self.assertIn("胶囊填充机故障", narratives[2])
        self.assertIn("不将该事件写成成本上涨原因", narratives[2])

    def test_benchmark_attribution_is_rendered_below_tree_in_same_panel(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        tree_start = response.text.index('id="benchmarkTreeSection"')
        chart_start = response.text.index('id="benchmarkTreeChart"')
        attribution_start = response.text.index('id="benchmarkAttributionSection"')
        evidence_start = response.text.index('id="evidence"')
        self.assertLess(tree_start, chart_start)
        self.assertLess(chart_start, attribution_start)
        self.assertLess(attribution_start, evidence_start)
        self.assertIn('id="benchmarkAttributionText"', response.text)
        self.assertIn('id="benchmarkRecommendationList"', response.text)
        self.assertIn("归因分析文本", response.text)
        self.assertIn("改进建议列表", response.text)
        self.assertIn("归因分析与改进建议", response.text)
        self.assertNotIn("对标结论总述", response.text)
        self.assertNotIn("分要素归因", response.text)
        script = (ROOT / "app" / "dashboard" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        styles = (ROOT / "app" / "dashboard" / "static" / "styles.css").read_text(
            encoding="utf-8"
        )
        self.assertIn("function renderBenchmarkAttribution(result)", script)
        self.assertIn("function applyReportGeneratedContent(report)", script)
        self.assertIn("applyReportGeneratedContent(report);", script)
        self.assertIn("webContent.narratives", script)
        self.assertIn("大模型受控生成", script)
        self.assertIn("renderBenchmarkAttribution(result);", script)
        self.assertIn('result.narratives?.["差异归因分析文本"]', script)
        self.assertIn("result.recommendations", script)
        self.assertIn("责任部门：", script)
        self.assertIn("优先级：", script)
        recommendation_start = script.index("function renderBenchmarkAttribution(result)")
        recommendation_end = script.index("function renderMaterialNarrative(value)")
        self.assertNotIn("预期结果：", script[recommendation_start:recommendation_end])
        self.assertNotIn("result.benchmark_attribution", script)
        self.assertNotIn("benchmarkFactorAttribution", script)
        self.assertRegex(
            styles,
            r"\.benchmark-output-grid\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1\.35fr\) minmax\(380px, \.65fr\)",
        )

    def test_three_benchmark_steps_share_one_panel_and_one_heading_pattern(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.text
        panel_class = 'class="panel benchmark-analysis-panel dashboard-module"'
        panel_start = html.index(panel_class)
        panel_end = html.index('id="evidence"')
        panel_html = html[panel_start:panel_end]
        self.assertEqual(html.count(panel_class), 1)
        self.assertEqual(panel_html.count('class="benchmark-stage-head"'), 3)
        self.assertEqual(panel_html.count('class="benchmark-stage-title"'), 3)
        self.assertNotIn('class="benchmark-stage-number"', panel_html)
        self.assertNotIn('class="benchmark-analysis-icon', panel_html)
        self.assertNotIn("单位成本口径 · 点击节点展开/收起", panel_html)
        self.assertNotIn('class="boundary benchmark-tree-boundary"', panel_html)
        for eyebrow, title in (
            ("对标分析", "对标差异总览"),
            ("结构拆解", "成本差异结构树"),
            ("归因建议", "归因分析与改进建议"),
        ):
            self.assertIn(eyebrow, panel_html)
            self.assertIn(f"<h3>{title}</h3>", panel_html)
        self.assertIn('href="#benchmarkSection"', html)
        styles = (ROOT / "app" / "dashboard" / "static" / "styles.css").read_text(
            encoding="utf-8"
        )
        self.assertRegex(styles, r"\.benchmark-analysis-panel\s*\{[^}]*display:\s*grid[^}]*background:\s*#f5f9f7")
        self.assertRegex(styles, r"\.benchmark-stage \+ \.benchmark-stage\s*\{[^}]*border-top:\s*1px solid")
        self.assertIn(".benchmark-stage-title h3", styles)
        self.assertNotIn(".benchmark-stage-number", styles)
        self.assertNotIn(".benchmark-analysis-icon", styles)
        self.assertNotIn(".benchmark-overview-head", styles)
        self.assertNotIn(".benchmark-tree-head", styles)
        self.assertNotIn(".benchmark-subsection-head", styles)

    def test_six_cost_comparison_cards_use_two_rows_and_labor_precedes_benchmark(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertLess(
            html.index('id="detailSection"'),
            html.index('id="laborEfficiencySection"'),
        )
        self.assertLess(
            html.index('id="laborEfficiencySection"'),
            html.index('id="benchmarkSection"'),
        )
        self.assertLess(
            html.index('href="#heatmapSection"'),
            html.index('href="#detailSection"'),
        )
        self.assertLess(
            html.index('href="#detailSection"'),
            html.index('href="#laborEfficiencySection"'),
        )
        self.assertLess(
            html.index('href="#laborEfficiencySection"'),
            html.index('href="#benchmarkSection"'),
        )
        script = (ROOT / "app" / "dashboard" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        styles = (ROOT / "app" / "dashboard" / "static" / "styles.css").read_text(
            encoding="utf-8"
        )
        comparison_start = script.index("function renderComparisons(result)")
        comparison_end = script.index("function commonChartOption()")
        comparison_body = script[comparison_start:comparison_end]
        for label in ("单位成本", "产量", "总成本", "直接材料", "直接人工", "制造费用"):
            self.assertIn(f'label: "{label}"', comparison_body)
        self.assertEqual(comparison_body.count("costComponent: true"), 3)
        self.assertIn('business-component-label">成本要素</span>', comparison_body)
        for label in ("环比变动", "同比变动", "预算偏差"):
            self.assertIn(f'comparisonPoint("{label}"', comparison_body)
        self.assertNotIn("eyebrow:", comparison_body)
        self.assertNotIn("row.eyebrow", comparison_body)
        self.assertNotIn("comparisonTakeaway", comparison_body)
        self.assertNotIn(".business-metric-card.total-cost", styles)
        self.assertNotIn(".business-metric-card.labor-cost", styles)
        self.assertNotIn(".comparison-panel .panel-head h3", styles)
        self.assertIn(".labor-panel {", styles)
        self.assertRegex(
            styles,
            r"\.business-card-grid\s*\{[^}]*grid-template-columns:\s*repeat\(3,\s*minmax\(0,\s*1fr\)\)",
        )
        self.assertNotIn("comparison-dashboard", styles)
        self.assertNotIn("business-comparison", styles)
        self.assertRegex(
            styles,
            r"\.labor-card-list\s*\{[^}]*grid-template-columns:\s*repeat\(3,\s*minmax\(0,\s*1fr\)\)",
        )
        self.assertIn(".labor-analysis-insight {", styles)
        self.assertIn(".business-component-label {", styles)
        self.assertIn("function buildAnalysisSummary(result)", script)
        self.assertIn("function renderLaborInterpretation(result)", script)
        self.assertIn('renderLaborInterpretation(result);', comparison_body)

    def test_dashboard_has_restrictive_security_headers(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("object-src 'none'", response.headers["content-security-policy"])

    def test_report_generation_preview_and_download_are_traversal_safe(self) -> None:
        baseline_stats_response = self.client.get("/api/workflow-stats")
        self.assertEqual(baseline_stats_response.status_code, 200)
        baseline_stats = baseline_stats_response.json()
        response = self.client.post(
            "/api/reports",
            json={
                "analysis_type": "月度成本分析",
                "product": "银黄口服液",
                "month": "2026-05",
                "use_llm": False,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertRegex(payload["report_id"], r"^[a-f0-9]{32}$")
        self.assertEqual(payload["generation"]["status"], "not_requested")
        self.assertIn("差异归因分析文本", payload["web_content"]["narratives"])
        self.assertGreaterEqual(len(payload["web_content"]["recommendations"]), 1)
        self.assertEqual(
            set(payload["web_content"]["recommendations"][0]),
            {"sequence", "action", "owner", "priority", "expected_effect", "due"},
        )
        preview = self.client.get(payload["preview_url"])
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.headers["content-type"], "application/pdf")
        self.assertIn("inline", preview.headers["content-disposition"])
        self.assertEqual(preview.headers["x-content-type-options"], "nosniff")
        self.assertEqual(preview.headers["cache-control"], "no-store")
        self.assertNotIn("x-frame-options", preview.headers)
        self.assertNotIn("content-security-policy", preview.headers)
        self.assertTrue(preview.content.startswith(b"%PDF"))
        word = self.client.get(payload["downloads"]["docx"])
        self.assertEqual(word.status_code, 200)
        self.assertIn(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            word.headers["content-type"],
        )

        created = self.client.post(
            "/api/workflows",
            json={"report_id": payload["report_id"]},
        )
        self.assertEqual(created.status_code, 200, created.text)
        workflow = created.json()
        self.assertEqual(workflow["state"], "pending_review")
        self.assertEqual(workflow["candidate"]["validation_status"], "PASS")
        self.assertEqual(workflow["candidate"]["report_number"], payload["report_number"])
        generated_stats = self.client.get("/api/workflow-stats").json()
        self.assertEqual(
            generated_stats["generated_count"], baseline_stats["generated_count"] + 1
        )
        repeated = self.client.post(
            "/api/workflows",
            json={"report_id": payload["report_id"]},
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(
            repeated.json()["candidate"]["candidate_id"],
            workflow["candidate"]["candidate_id"],
        )

        approval = {
            "candidate_id": workflow["candidate"]["candidate_id"],
            "reviewer": "演示审批员",
            "assignee_name": "演示责任人",
            "department": "采购部",
            "role": "采购专员",
            "deadline": (date.today() + timedelta(days=7)).isoformat(),
            "priority": workflow["candidate"]["suggested_priority"],
            "notify_method": "wechat",
            "comment": "已核对报告证据和责任信息",
            "confirmation": "CONFIRM",
        }
        wrong_confirmation = {**approval, "confirmation": "YES"}
        rejected = self.client.post(
            f"/api/workflows/{payload['report_id']}/approve",
            json=wrong_confirmation,
        )
        self.assertEqual(rejected.status_code, 422)
        expired = {
            **approval,
            "deadline": (date.today() - timedelta(days=1)).isoformat(),
        }
        expired_response = self.client.post(
            f"/api/workflows/{payload['report_id']}/approve",
            json=expired,
        )
        self.assertEqual(expired_response.status_code, 422)
        approved = self.client.post(
            f"/api/workflows/{payload['report_id']}/approve",
            json=approval,
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["state"], "approved")
        self.assertEqual(approved.json()["payload"]["assignee"]["name"], "演示责任人")
        confirmed_stats = self.client.get("/api/workflow-stats").json()
        self.assertEqual(
            confirmed_stats["confirmed_count"], baseline_stats["confirmed_count"] + 1
        )

        wrong_submit = self.client.post(
            f"/api/workflows/{payload['report_id']}/submit",
            json={"confirmation": "YES"},
        )
        self.assertEqual(wrong_submit.status_code, 422)
        submitted = self.client.post(
            f"/api/workflows/{payload['report_id']}/submit",
            json={"confirmation": "SUBMIT"},
        )
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertEqual(submitted.json()["state"], "sent")
        self.assertEqual(submitted.json()["submission"]["http_status"], 200)
        self.assertIn(
            "已发送至 演示责任人",
            submitted.json()["submission"]["response_payload"]["data"]["notify_status"]["wechat"],
        )
        delivered_stats = self.client.get("/api/workflow-stats").json()
        self.assertEqual(
            delivered_stats["delivered_count"], baseline_stats["delivered_count"] + 1
        )
        state = self.client.get(f"/api/workflows/{payload['report_id']}")
        self.assertEqual(state.status_code, 200)
        self.assertEqual(state.json()["submission"], submitted.json()["submission"])

        invalid = self.client.get("/api/reports/../../etc/download/pdf")
        self.assertIn(invalid.status_code, {404, 422})

    def test_web_llm_request_without_key_still_generates_deterministic_report(self) -> None:
        response = self.client.post(
            "/api/reports",
            json={
                "analysis_type": "月度成本分析",
                "product": "板蓝根颗粒",
                "month": "2026-05",
                "use_llm": True,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        generation = response.json()["generation"]
        self.assertEqual(generation["status"], "fallback")
        self.assertEqual(generation["mode"], "deterministic")
        workflow_response = self.client.post(
            "/api/workflows",
            json={"report_id": response.json()["report_id"], "use_llm": True},
        )
        self.assertEqual(workflow_response.status_code, 200, workflow_response.text)
        workflow = workflow_response.json()
        self.assertEqual(workflow["state"], "pending_review")
        self.assertEqual(workflow["generation"]["status"], "fallback")
        self.assertEqual(workflow["generation"]["mode"], "deterministic")
        self.assertEqual(workflow["candidate"]["state"], "pending_review")
        for forbidden in ("assignee", "deadline", "reviewer", "notify_method"):
            self.assertNotIn(forbidden, workflow["candidate"])

    def test_quarterly_report_exports_independent_artifacts(self) -> None:
        response = self.client.post(
            "/api/reports",
            json={
                "analysis_type": "季度成本分析",
                "product": "银黄口服液",
                "quarter": "2026-Q2",
                "use_llm": False,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["analysis_type"], "季度成本分析")
        self.assertEqual(payload["period"], "2026-Q2")
        self.assertTrue(payload["workflow_supported"])
        self.assertIn("差异归因分析文本", payload["web_content"]["narratives"])
        self.assertGreaterEqual(len(payload["web_content"]["recommendations"]), 1)
        preview = self.client.get(payload["preview_url"])
        self.assertEqual(preview.status_code, 200)
        self.assertTrue(preview.content.startswith(b"%PDF"))
        markdown = self.client.get(payload["downloads"]["md"])
        self.assertIn("季度成本分析报告", markdown.text)
        self.assertIn("2026-Q2", markdown.text)
        contract_response = self.client.get(payload["downloads"]["json"])
        self.assertEqual(contract_response.status_code, 200)
        contract = ReportContract.model_validate(contract_response.json())
        self.assertEqual(contract.analysis_type, "季度成本分析")
        self.assertEqual(contract.period, "2026-Q2")
        self.assertEqual(len(contract.fields), 107)
        self.assertEqual(len(contract.dynamic_tables), 6)
        self.assertEqual(contract.fields["材料贡献度"].rule, "要素总成本变动额÷总成本变动额×100%")
        self.assertIn("贡献度", contract.fields["材料成本归因分析文本"].value)
        self.assertIn("建议", contract.fields["材料成本归因分析文本"].value)
        word = self.client.get(payload["downloads"]["docx"])
        self.assertEqual(word.status_code, 200)
        self.assertEqual(len(Document(BytesIO(word.content)).inline_shapes), 3)
        created = self.client.post("/api/workflows", json={"report_id": payload["report_id"]})
        self.assertEqual(created.status_code, 200, created.text)
        workflow = created.json()
        self.assertEqual(workflow["candidate"]["analysis_type"], "季度成本分析")
        self.assertEqual(workflow["candidate"]["analysis_period"], "2026-Q2")
        approval = {
            "candidate_id": workflow["candidate"]["candidate_id"],
            "reviewer": "季度审批员",
            "assignee_name": "季度责任人",
            "department": "财务部",
            "role": "成本会计",
            "deadline": (date.today() + timedelta(days=7)).isoformat(),
            "priority": workflow["candidate"]["suggested_priority"],
            "notify_method": "wechat",
            "comment": "已核对季度口径",
            "confirmation": "CONFIRM",
        }
        approved = self.client.post(
            f"/api/workflows/{payload['report_id']}/approve", json=approval
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["payload"]["source"]["analysis_type"], "季度成本分析")

    def test_special_report_uses_the_unified_contract_and_template_pipeline(self) -> None:
        response = self.client.post(
            "/api/reports",
            json={
                "analysis_type": "专题分析",
                "product": "银黄口服液",
                "month": "2026-05",
                "topic": "原材料涨价专项",
                "use_llm": False,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["analysis_type"], "专题分析")
        self.assertEqual(payload["topic"], "原材料涨价专项")
        self.assertIn("差异归因分析文本", payload["web_content"]["narratives"])
        contract = ReportContract.model_validate(
            self.client.get(payload["downloads"]["json"]).json()
        )
        self.assertEqual(contract.analysis_type, "专题分析")
        self.assertEqual(contract.period, "2026-05")
        self.assertEqual(contract.topic, "原材料涨价专项")
        self.assertEqual(len(contract.fields), 107)
        self.assertEqual(len(contract.dynamic_tables), 6)
        self.assertEqual(Path(contract.word_template_path).name, "月度成本分析报告模板.docx")
        self.assertEqual(len(contract.word_template_sha256), 64)
        markdown = self.client.get(payload["downloads"]["md"]).text
        self.assertIn("原材料涨价专项", markdown)
        self.assertIn("专题分析", markdown)
        word = self.client.get(payload["downloads"]["docx"])
        self.assertEqual(word.status_code, 200)
        self.assertEqual(len(Document(BytesIO(word.content)).inline_shapes), 3)

    def test_material_price_special_report_rejects_a_falling_material_month(self) -> None:
        response = self.client.post(
            "/api/reports",
            json={
                "analysis_type": "专题分析",
                "product": "银黄口服液",
                "month": "2026-06",
                "topic": "原材料涨价专项",
                "use_llm": False,
            },
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("直接材料成本未上涨", response.json()["detail"])

    def test_workflow_rejects_non_loopback_rpa_target(self) -> None:
        with self.assertRaisesRegex(Exception, "只允许连接本机"):
            create_dashboard_app(
                data_dir=ROOT,
                report_output_dir=Path(self.temporary.name) / "invalid-rpa",
                rpa_base_url="https://example.com",
            )


if __name__ == "__main__":
    unittest.main()

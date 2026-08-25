"""Regression tests for the three-scenario model-output acceptance gate."""

from __future__ import annotations

import unittest
from pathlib import Path

from app.evaluation import GOLDEN_LLM_SCENARIOS, evaluate_llm_contract
from app.llm.models import NARRATIVE_FIELDS
from app.reporting import build_report_contract
from app.reporting.models import ReportGeneration


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX_ROOT = PROJECT_ROOT / "06_知识证据索引"


def accepted_copy(contract):
    fields = {name: value.model_copy(deep=True) for name, value in contract.fields.items()}
    for name in NARRATIVE_FIELDS:
        fields[name] = fields[name].model_copy(
            update={
                "status": "generated",
                "rule": "大模型受控改写；数值、方向与引用来自确定性报告契约",
            }
        )
    return contract.model_copy(
        deep=True,
        update={
            "fields": fields,
            "generation": ReportGeneration(
                mode="llm",
                status="generated",
                provider_protocol="chat_completions",
                model="qwen3.8-max",
                request_id="req_golden_test",
                attempt_count=1,
            ),
        },
    )


class LlmGoldenEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.baselines = {
            (scenario["product"], scenario["month"]): build_report_contract(
                PROJECT_ROOT,
                INDEX_ROOT,
                scenario["product"],
                scenario["month"],
                generated_date="2026-08-21",
            )
            for scenario in GOLDEN_LLM_SCENARIOS
        }

    def result_for(self, scenario, enhanced=None):
        baseline = self.baselines[(scenario["product"], scenario["month"])]
        return evaluate_llm_contract(
            baseline,
            enhanced or accepted_copy(baseline),
            scenario,
            expected_model="qwen3.8-max",
        )

    def test_all_three_baseline_preserving_model_outputs_pass(self) -> None:
        for scenario in GOLDEN_LLM_SCENARIOS:
            with self.subTest(scenario=scenario["name"]):
                result = self.result_for(scenario)
                self.assertEqual(result["status"], "PASS", result["checks"])

    def test_protected_numeric_field_change_fails(self) -> None:
        scenario = GOLDEN_LLM_SCENARIOS[0]
        baseline = self.baselines[(scenario["product"], scenario["month"])]
        enhanced = accepted_copy(baseline)
        fields = {name: value.model_copy(deep=True) for name, value in enhanced.fields.items()}
        fields["本月单位成本"] = fields["本月单位成本"].model_copy(update={"value": "99.99"})
        result = self.result_for(scenario, enhanced.model_copy(update={"fields": fields}))
        failed = {check["name"] for check in result["checks"] if check["status"] == "FAIL"}
        self.assertIn("受保护契约未改变", failed)
        self.assertIn("黄金数值：本月单位成本", failed)
        self.assertIn("100个非叙述字段逐项一致", failed)

    def test_governed_recommendation_wording_can_change(self) -> None:
        scenario = GOLDEN_LLM_SCENARIOS[0]
        baseline = self.baselines[(scenario["product"], scenario["month"])]
        enhanced = accepted_copy(baseline)
        tables = {
            name: table.model_copy(deep=True)
            for name, table in enhanced.dynamic_tables.items()
        }
        recommendation_table = tables["改进建议表格"]
        recommendation_table.rows = [
            [row[0], row[1] + "并形成复核记录", row[2], row[3], row[4], row[5]]
            for row in recommendation_table.rows
        ]
        result = self.result_for(
            scenario,
            enhanced.model_copy(update={"dynamic_tables": tables}),
        )
        self.assertEqual(result["status"], "PASS", result["checks"])

    def test_non_recommendation_dynamic_table_change_fails(self) -> None:
        scenario = GOLDEN_LLM_SCENARIOS[0]
        baseline = self.baselines[(scenario["product"], scenario["month"])]
        enhanced = accepted_copy(baseline)
        tables = {
            name: table.model_copy(deep=True)
            for name, table in enhanced.dynamic_tables.items()
        }
        table = tables["对标差异表格"]
        table.rows[0][1] = "99.99"
        result = self.result_for(
            scenario,
            enhanced.model_copy(update={"dynamic_tables": tables}),
        )
        failed = {check["name"] for check in result["checks"] if check["status"] == "FAIL"}
        self.assertIn("受保护契约未改变", failed)
        self.assertIn("5张受保护动态表逐项一致", failed)

    def test_recommendation_sequence_or_approval_change_fails(self) -> None:
        scenario = GOLDEN_LLM_SCENARIOS[0]
        baseline = self.baselines[(scenario["product"], scenario["month"])]
        for column, value in ((0, "9"), (5, "已完成")):
            with self.subTest(column=column):
                enhanced = accepted_copy(baseline)
                tables = {
                    name: table.model_copy(deep=True)
                    for name, table in enhanced.dynamic_tables.items()
                }
                tables["改进建议表格"].rows[0][column] = value
                result = self.result_for(
                    scenario,
                    enhanced.model_copy(update={"dynamic_tables": tables}),
                )
                failed = {
                    check["name"] for check in result["checks"] if check["status"] == "FAIL"
                }
                self.assertIn("改进建议表格受控生成", failed)

    def test_false_market_causality_fails(self) -> None:
        scenario = GOLDEN_LLM_SCENARIOS[0]
        baseline = self.baselines[(scenario["product"], scenario["month"])]
        enhanced = accepted_copy(baseline)
        fields = {name: value.model_copy(deep=True) for name, value in enhanced.fields.items()}
        fields["需关注问题"] = fields["需关注问题"].model_copy(
            update={"value": fields["需关注问题"].value + "市场价格上涨导致成本费用增加。"}
        )
        result = self.result_for(scenario, enhanced.model_copy(update={"fields": fields}))
        failed = {check["name"] for check in result["checks"] if check["status"] == "FAIL"}
        self.assertIn("无禁止结论或采购因果", failed)
        self.assertIn("受控生成护栏复验", failed)

    def test_equivalent_non_quantification_and_yoy_rise_are_accepted(self) -> None:
        scenario = GOLDEN_LLM_SCENARIOS[2]
        baseline = self.baselines[(scenario["product"], scenario["month"])]
        enhanced = accepted_copy(baseline)
        fields = {name: value.model_copy(deep=True) for name, value in enhanced.fields.items()}
        anomaly = fields["成本异常排查分析"].value.replace(
            "不量化其单位成本影响",
            "无法量化该事件对单位成本的影响",
        )
        fields["成本异常排查分析"] = fields["成本异常排查分析"].model_copy(
            update={"value": anomaly + " 单位成本同比上升0.59%。"}
        )
        result = self.result_for(scenario, enhanced.model_copy(update={"fields": fields}))
        self.assertEqual(result["status"], "PASS", result["checks"])

    def test_wrong_yoy_direction_is_rejected(self) -> None:
        scenario = GOLDEN_LLM_SCENARIOS[1]
        baseline = self.baselines[(scenario["product"], scenario["month"])]
        enhanced = accepted_copy(baseline)
        fields = {name: value.model_copy(deep=True) for name, value in enhanced.fields.items()}
        fields["本月亮点"] = fields["本月亮点"].model_copy(
            update={"value": "单位成本同比下降5.21%。"}
        )
        result = self.result_for(scenario, enhanced.model_copy(update={"fields": fields}))
        failed = {check["name"] for check in result["checks"] if check["status"] == "FAIL"}
        self.assertIn("受控生成护栏复验", failed)

    def test_wrong_positive_budget_appraisal_is_rejected(self) -> None:
        scenario = GOLDEN_LLM_SCENARIOS[1]
        baseline = self.baselines[(scenario["product"], scenario["month"])]
        enhanced = accepted_copy(baseline)
        fields = {name: value.model_copy(deep=True) for name, value in enhanced.fields.items()}
        fields["本月亮点"] = fields["本月亮点"].model_copy(
            update={"value": "单位成本同比5.21%，优于预算6.71%。"}
        )
        result = self.result_for(scenario, enhanced.model_copy(update={"fields": fields}))
        failed = {check["name"] for check in result["checks"] if check["status"] == "FAIL"}
        self.assertIn("受控生成护栏复验", failed)

    def test_fallback_generation_fails_formal_acceptance(self) -> None:
        scenario = GOLDEN_LLM_SCENARIOS[1]
        baseline = self.baselines[(scenario["product"], scenario["month"])]
        enhanced = baseline.model_copy(
            deep=True,
            update={
                "generation": ReportGeneration(
                    mode="deterministic",
                    status="fallback",
                    provider_protocol="chat_completions",
                    model="qwen3.8-max",
                    warnings=["模拟网络失败"],
                )
            },
        )
        result = self.result_for(scenario, enhanced)
        failed = {check["name"] for check in result["checks"] if check["status"] == "FAIL"}
        self.assertIn("生成模式", failed)
        self.assertIn("生成状态", failed)
        self.assertIn("生成告警为空", failed)


if __name__ == "__main__":
    unittest.main()

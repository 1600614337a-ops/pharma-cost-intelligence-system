"""Acceptance rules for model-assisted wording in the three golden scenarios."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from app.llm.guardrails import CAUSAL_PATTERNS, LlmGuardrailError, validate_draft
from app.llm.models import NARRATIVE_FIELDS, NarrativeDraft
from app.reporting.contract import FORBIDDEN_PATTERNS
from app.reporting.models import ReportContract


GOLDEN_LLM_SCENARIOS: tuple[dict[str, Any], ...] = (
    {
        "name": "银黄口服液 2026-05",
        "product": "银黄口服液",
        "month": "2026-05",
        "expected": {
            "本月单位成本": "11.21",
            "上月单位成本": "10.90",
            "单位成本环比": "2.84%",
            "材料贡献度": "67.09%",
            "单位成本同比": "4.67%",
            "单位成本预算偏差": "5.75%",
            "本月工时": "231.72",
        },
        "benchmark_difference": "-0.39",
        "required_terms": ("金银花", "6.15%", "市场行情不等于企业实际采购价"),
        "opposite_direction_pattern": r"单位成本(?:环比|较上月).{0,12}(?:下降|降低|减少)",
    },
    {
        "name": "板蓝根颗粒 2026-05",
        "product": "板蓝根颗粒",
        "month": "2026-05",
        "expected": {
            "本月单位成本": "7.47",
            "上月单位成本": "7.24",
            "单位成本环比": "3.18%",
            "材料贡献度": "69.90%",
            "单位成本同比": "5.21%",
            "单位成本预算偏差": "6.71%",
            "本月工时": "164.71",
        },
        "benchmark_difference": "-0.50",
        "required_terms": ("板蓝根", "8.47%", "市场行情不等于企业实际采购价"),
        "opposite_direction_pattern": r"单位成本(?:环比|较上月).{0,12}(?:下降|降低|减少)",
    },
    {
        "name": "六味地黄胶囊 2026-03",
        "product": "六味地黄胶囊",
        "month": "2026-03",
        "expected": {
            "本月单位成本": "17.02",
            "上月单位成本": "17.60",
            "单位成本环比": "-3.30%",
            "产量环比": "25.00%",
            "单位成本同比": "0.59%",
            "单位成本预算偏差": "-0.47%",
            "本月工时": "603.43",
        },
        "benchmark_difference": "-1.16",
        "required_terms": ("单位成本实际下降", "胶囊填充机故障"),
        "required_patterns": (
            {
                "label": "设备事件成本影响不可量化",
                "pattern": r"(?:(?:不|无|未|不能|无法)量化.{0,16}(?:单位成本)?影响|(?:单位成本)?影响.{0,16}(?:不|无|未|不能|无法)量化)",
            },
        ),
        "opposite_direction_pattern": r"单位成本(?:环比|较上月).{0,12}(?:上升|上涨|增加)",
    },
)


def _canonical(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def protected_contract_fingerprint(contract: ReportContract) -> str:
    """Fingerprint every model-protected part of a report contract."""

    protected_fields = {
        name: field.model_dump(mode="json")
        for name, field in contract.fields.items()
        if name not in NARRATIVE_FIELDS
    }
    payload = {
        "identity": {
            "contract_version": contract.contract_version,
            "analysis_version": contract.analysis_version,
            "formula_version": contract.formula_version,
            "knowledge_index_version": contract.knowledge_index_version,
            "product": contract.product,
            "month": contract.month,
            "analysis_type": contract.analysis_type,
            "period": contract.period,
            "topic": contract.topic,
            "report_number": contract.report_number,
            "markdown_template_sha256": contract.markdown_template_sha256,
            "word_template_sha256": contract.word_template_sha256,
        },
        "fields": protected_fields,
        "supplemental_fields": {
            name: value.model_dump(mode="json")
            for name, value in contract.supplemental_fields.items()
        },
        "dynamic_tables": {
            name: value.model_dump(mode="json")
            for name, value in contract.dynamic_tables.items()
            if name != "改进建议表格"
        },
        "recommendation_table_contract": {
            "name": contract.dynamic_tables["改进建议表格"].name,
            "headers": contract.dynamic_tables["改进建议表格"].headers,
            "sequences": [
                row[0] for row in contract.dynamic_tables["改进建议表格"].rows
            ],
        },
        "evidence": contract.evidence.model_dump(mode="json"),
        "validation_status": contract.validation_status,
        "validation_issues": contract.validation_issues,
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _narrative_text(contract: ReportContract) -> str:
    return "\n".join(contract.fields[name].value for name in NARRATIVE_FIELDS)


def evaluate_llm_contract(
    baseline: ReportContract,
    enhanced: ReportContract,
    scenario: dict[str, Any],
    *,
    run_index: int = 1,
    expected_model: str | None = None,
) -> dict[str, Any]:
    """Compare one model-assisted contract with its deterministic baseline."""

    checks: list[dict[str, Any]] = []

    def record(name: str, passed: bool, actual: object, expected: object) -> None:
        checks.append(
            {
                "name": name,
                "status": "PASS" if passed else "FAIL",
                "actual": actual,
                "expected": expected,
            }
        )

    record("确定性基线契约", baseline.validation_status == "PASS", baseline.validation_status, "PASS")
    record("增强后契约", enhanced.validation_status == "PASS", enhanced.validation_status, "PASS")
    record("生成模式", enhanced.generation.mode == "llm", enhanced.generation.mode, "llm")
    record("生成状态", enhanced.generation.status == "generated", enhanced.generation.status, "generated")
    record("生成告警为空", not enhanced.generation.warnings, enhanced.generation.warnings, [])
    record("请求标识存在", bool(enhanced.generation.request_id), bool(enhanced.generation.request_id), True)
    if expected_model:
        record("模型名称", enhanced.generation.model == expected_model, enhanced.generation.model, expected_model)

    baseline_fingerprint = protected_contract_fingerprint(baseline)
    enhanced_fingerprint = protected_contract_fingerprint(enhanced)
    record(
        "受保护契约未改变",
        enhanced_fingerprint == baseline_fingerprint,
        enhanced_fingerprint,
        baseline_fingerprint,
    )
    record("报告字段数量", len(enhanced.fields) == 107, len(enhanced.fields), 107)
    record("动态表格数量", len(enhanced.dynamic_tables) == 6, len(enhanced.dynamic_tables), 6)

    for field_name, expected_value in scenario["expected"].items():
        actual = enhanced.fields[field_name].value
        record(f"黄金数值：{field_name}", actual == expected_value, actual, expected_value)

    benchmark_row = next(
        row
        for row in enhanced.dynamic_tables["对标差异表格"].rows
        if row[0] == "单位成本"
    )
    record(
        "黄金数值：一厂减二厂单位成本差异",
        benchmark_row[3] == scenario["benchmark_difference"],
        benchmark_row[3],
        scenario["benchmark_difference"],
    )

    protected_names = sorted(set(baseline.fields) - set(NARRATIVE_FIELDS))
    changed_protected = [
        name for name in protected_names if enhanced.fields[name] != baseline.fields[name]
    ]
    record("100个非叙述字段逐项一致", not changed_protected, changed_protected, [])
    record(
        "补充指标逐项一致",
        enhanced.supplemental_fields == baseline.supplemental_fields,
        enhanced.supplemental_fields == baseline.supplemental_fields,
        True,
    )
    protected_table_names = sorted(
        set(baseline.dynamic_tables) - {"改进建议表格"}
    )
    changed_protected_tables = [
        name
        for name in protected_table_names
        if enhanced.dynamic_tables.get(name) != baseline.dynamic_tables[name]
    ]
    record("5张受保护动态表逐项一致", not changed_protected_tables, changed_protected_tables, [])

    baseline_recommendations = baseline.dynamic_tables["改进建议表格"]
    enhanced_recommendations = enhanced.dynamic_tables.get("改进建议表格")
    recommendation_structure_issues: list[str] = []
    if enhanced_recommendations is None:
        recommendation_structure_issues.append("缺少改进建议表格")
    else:
        if enhanced_recommendations.headers != baseline_recommendations.headers:
            recommendation_structure_issues.append("表头改变")
        if len(enhanced_recommendations.rows) != len(baseline_recommendations.rows):
            recommendation_structure_issues.append("建议数量改变")
        for index, row in enumerate(enhanced_recommendations.rows):
            if len(row) != 6:
                recommendation_structure_issues.append(f"第{index + 1}行列数不是6")
                continue
            if index >= len(baseline_recommendations.rows):
                continue
            if row[0] != baseline_recommendations.rows[index][0]:
                recommendation_structure_issues.append(f"第{index + 1}行序号改变")
            if row[5] != "待业务审批":
                recommendation_structure_issues.append(f"第{index + 1}行绕过业务审批")
    record(
        "改进建议表格受控生成",
        not recommendation_structure_issues,
        recommendation_structure_issues,
        [],
    )
    record(
        "知识引用逐项一致",
        enhanced.evidence == baseline.evidence,
        enhanced.evidence == baseline.evidence,
        True,
    )

    invalid_narratives: list[str] = []
    changed_sources: list[str] = []
    missing_source_files: list[str] = []
    for name in NARRATIVE_FIELDS:
        field = enhanced.fields[name]
        if (
            field.status != "generated"
            or not field.value.strip()
            or field.rule != "大模型受控改写；数值、方向与引用来自确定性报告契约"
        ):
            invalid_narratives.append(name)
        if field.source_refs != baseline.fields[name].source_refs:
            changed_sources.append(name)
        for source in field.source_refs:
            if not Path(source).is_file():
                missing_source_files.append(source)
    record("七个受控叙述字段有效", not invalid_narratives, invalid_narratives, [])
    record("叙述字段来源未改变", not changed_sources, changed_sources, [])
    record("叙述来源文件真实存在", not missing_source_files, sorted(set(missing_source_files)), [])

    unavailable_regressions = [
        name
        for name, field in baseline.fields.items()
        if field.status == "unavailable"
        and (
            enhanced.fields[name].status != "unavailable"
            or enhanced.fields[name].value != "暂无数据"
        )
    ]
    record("缺失数据保持暂无数据", not unavailable_regressions, unavailable_regressions, [])

    citation_count = sum(bool(value) for value in enhanced.evidence.model_dump().values())
    record("八类受治理证据引用完整", citation_count == 8, citation_count, 8)

    narrative = _narrative_text(enhanced)
    for term in scenario["required_terms"]:
        record(f"关键事实或边界：{term}", term in narrative, term in narrative, True)
    for requirement in scenario.get("required_patterns", ()):
        matched = bool(re.search(requirement["pattern"], narrative))
        record(f"关键事实或边界：{requirement['label']}", matched, matched, True)
    opposite = scenario["opposite_direction_pattern"]
    direction_text = enhanced.fields["成本异常排查分析"].value
    record(
        "单位成本环比方向未写反",
        not bool(re.search(opposite, direction_text)),
        bool(re.search(opposite, direction_text)),
        False,
    )
    unit_change = scenario["expected"]["单位成本环比"]
    unit_change_candidates = {unit_change, unit_change.lstrip("+-")}
    unit_change_present = any(value in narrative for value in unit_change_candidates)
    record(
        "单位成本环比进入叙述",
        unit_change_present,
        sorted(value for value in unit_change_candidates if value in narrative),
        sorted(unit_change_candidates),
    )

    forbidden_hits = [
        pattern
        for pattern in (*FORBIDDEN_PATTERNS, *CAUSAL_PATTERNS)
        if re.search(pattern, narrative)
    ]
    record("无禁止结论或采购因果", not forbidden_hits, forbidden_hits, [])

    try:
        draft_payload: dict[str, object] = {
            name: enhanced.fields[name].value for name in NARRATIVE_FIELDS
        }
        if enhanced_recommendations is not None:
            draft_payload["改进建议列表"] = [
                {
                    "sequence": row[0],
                    "action": row[1],
                    "owner": row[2],
                    "priority": row[3],
                    "expected_effect": row[4],
                    "due": row[5],
                }
                for row in enhanced_recommendations.rows
                if len(row) == 6
            ]
        draft = NarrativeDraft.model_validate(draft_payload)
        validate_draft(baseline, draft)
        guardrail_issue = None
    except (LlmGuardrailError, ValueError) as exc:
        guardrail_issue = str(exc)
    record("受控生成护栏复验", guardrail_issue is None, guardrail_issue, None)

    status = "PASS" if all(item["status"] == "PASS" for item in checks) else "FAIL"
    narrative_fingerprint = hashlib.sha256(narrative.encode("utf-8")).hexdigest()
    return {
        "scenario": scenario["name"],
        "product": scenario["product"],
        "month": scenario["month"],
        "run_index": run_index,
        "report_number": enhanced.report_number,
        "status": status,
        "generation": enhanced.generation.model_dump(mode="json"),
        "protected_fingerprint": enhanced_fingerprint,
        "narrative_fingerprint": narrative_fingerprint,
        "narratives": {
            name: enhanced.fields[name].value for name in NARRATIVE_FIELDS
        },
        "checks": checks,
    }

"""Build the complete 107-field report contract without generative arithmetic."""

from __future__ import annotations

import hashlib
import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from app.analysis import analyze_cost
from app.data_quality import load_validated_data
from app.data_quality.normalization import normalize_market_prices
from app.knowledge import search_knowledge

from .benchmark_guidance import BenchmarkGap, build_benchmark_guidance
from .models import DynamicTable, ReportContract, ReportEvidence, ReportFieldValue


CONTRACT_VERSION = "1.2.4"
MARKDOWN_TEMPLATE = Path("04_报告模板") / "月度成本分析报告模板.md"
WORD_TEMPLATE = Path("04_报告模板") / "月度成本分析报告模板.docx"
PRODUCT_CODE = {"银黄口服液": "YH", "板蓝根颗粒": "BLG", "六味地黄胶囊": "LWDH"}
PRODUCT_TASK_SEQUENCE = {"银黄口服液": "101", "板蓝根颗粒": "201", "六味地黄胶囊": "301"}
PRODUCT_CATEGORY = {"银黄口服液": "口服液类", "板蓝根颗粒": "颗粒剂类", "六味地黄胶囊": "胶囊剂类"}
PROCESS_QUERY = {
    "银黄口服液": "银黄口服液 关键工艺参数 提取收率 0.08元/盒",
    "板蓝根颗粒": "板蓝根颗粒 关键工艺参数 提取收率 0.03元/盒",
    "六味地黄胶囊": "六味地黄胶囊 关键工艺参数 胶囊填充",
}
ANOMALY_QUERY = {
    "银黄口服液": "银黄口服液 水提取 收率 灌装 成本异常 处置规则",
    "板蓝根颗粒": "板蓝根颗粒 制粒 收率 干燥 成本异常 处置规则",
    "六味地黄胶囊": "六味地黄胶囊 胶囊填充机 2026-03 维修 成本异常 处置规则",
}
DYNAMIC_NAMES = {
    "原材料成本明细表格",
    "近6个月成本趋势表格",
    "原材料价格跟踪表格",
    "对标差异表格",
    "改进建议表格",
    "整改任务表格",
}
SUPPLEMENTAL_REPORT_METRICS = {
    "去年材料成本",
    "材料同比",
    "去年人工成本",
    "人工同比",
    "去年制造费用",
    "制造费用同比",
}
FORBIDDEN_PATTERNS = (
    r"采购价环比上涨\s*12%",
    r"板蓝根.{0,8}上涨\s*10%",
    r"设备故障导致.{0,20}单位成本上涨",
)


def template_placeholders(text: str) -> set[str]:
    return set(re.findall(r"\{\{([^{}]+)\}\}", text))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _q(value: Decimal, pattern: str) -> Decimal:
    return value.quantize(Decimal(pattern), rounding=ROUND_HALF_UP)


def _money(value: Decimal | None) -> str:
    return "暂无数据" if value is None else f"{_q(value, '0.01'):,.2f}"


def _qty(value: Decimal | int | None) -> str:
    return "暂无数据" if value is None else f"{Decimal(value):,.0f}"


def _number(value: Decimal | int | None) -> str:
    return "暂无数据" if value is None else f"{_q(Decimal(value), '0.01'):,.2f}"


def _pct(value: Decimal | None, status: str = "available") -> str:
    if value is None:
        return "不适用" if status == "not_applicable" else "暂无数据"
    return f"{_q(value, '0.01'):.2f}%"


def _signed_money(value: Decimal | None, status: str = "available") -> str:
    if value is None:
        return "不适用" if status == "not_applicable" else "暂无数据"
    rounded = _q(value, "0.01")
    prefix = "+" if rounded > 0 else ""
    return f"{prefix}{rounded:,.2f}"


def _signed_pct(value: Decimal | None, status: str = "available") -> str:
    if value is None:
        return "不适用" if status == "not_applicable" else "暂无数据"
    rounded = _q(value, "0.01")
    prefix = "+" if rounded > 0 else ""
    return f"{prefix}{rounded:.2f}%"


def _direction(delta: Decimal | None) -> str:
    if delta is None:
        return "暂无数据"
    return "上升" if delta > 0 else "下降" if delta < 0 else "持平"


def _directional_pct(
    delta: Decimal | None,
    rate: Decimal | None,
    status: str = "available",
) -> str:
    if rate is None:
        return _pct(rate, status)
    return f"{_direction(delta)}{_pct(abs(rate), status)}"


def _citation(result) -> str:
    return result.hits[0].citation.display if result.hits else "暂无可靠知识证据"


def _field(name: str, value: str, status: str, refs: list[str], rule: str) -> ReportFieldValue:
    return ReportFieldValue(name=name, value=value, status=status, source_refs=refs, rule=rule)


def _markdown_rows(table: DynamicTable) -> str:
    return "\n".join("| " + " | ".join(row) + " |" for row in table.rows)


def build_report_contract(
    data_dir: str | Path,
    index_dir: str | Path,
    product: str,
    month: str,
    *,
    generated_date: str | None = None,
) -> ReportContract:
    root = Path(data_dir).resolve()
    generated_date = generated_date or date.today().isoformat()
    analysis = analyze_cost(root, product, month)
    quality, bundle = load_validated_data(root)
    summary = {item.name: item for item in analysis.summary}
    structure = {item.name: item for item in analysis.cost_structure}
    contribution = {item.name: item for item in analysis.total_cost_contributions}
    materials = {item.name: item for item in analysis.material_drivers}
    manufacturing = {item.name: item for item in analysis.manufacturing_drivers}
    benchmark = {item.name: item for item in analysis.factory_benchmark}
    report_metrics = {item.name: item for item in analysis.report_metrics}
    source_refs = [source.path for source in analysis.sources]

    recipe_terms = " ".join(item.name for item in analysis.material_drivers[:4])
    recipe = search_knowledge(
        index_dir,
        f"{product} 处方 {recipe_terms}",
        product=product,
        document_types=["配方"],
        top_k=1,
    )
    process = search_knowledge(index_dir, PROCESS_QUERY[product], product=product, document_types=["工艺"], top_k=1)
    gmp = search_knowledge(index_dir, "生产全过程 记录 偏差 调查 批记录 追溯", regulatory_claim=True, top_k=1)
    equipment = search_knowledge(index_dir, f"{product} 设备 维修 故障", product=product, document_types=["设备"], top_k=1)
    factory_benchmark_knowledge = search_knowledge(
        index_dir,
        f"{product} {month} 中药二厂 同集团 成本对标基线",
        product=product,
        document_types=["对标基线"],
        top_k=1,
    )
    anomaly_history = search_knowledge(
        index_dir,
        ANOMALY_QUERY[product],
        product=product,
        document_types=["异常处理"],
        top_k=1,
    )
    industry_item = next(item for item in analysis.industry_benchmark if "单位成本" in item.name)
    market_materials = "、".join(item.material_name for item in analysis.market_evidence)
    evidence = ReportEvidence(
        recipe_citation=_citation(recipe),
        process_citation=_citation(process),
        gmp_citation=_citation(gmp),
        industry_citation=(
            f"行业成本基准数据_2026.csv，{PRODUCT_CATEGORY[product]}，"
            f"单位成本P50={_money(industry_item.p50)}元/盒"
        ),
        market_citation=(
            f"药材市场价格行情_2026年上半年.csv，精确同名物料：{market_materials}；"
            "仅作市场相关性参考，不代表企业采购量价"
            if market_materials
            else None
        ),
        factory_benchmark_citation=(
            _citation(factory_benchmark_knowledge)
            if factory_benchmark_knowledge.hits
            else None
        ),
        equipment_citation=_citation(equipment) if equipment.hits else None,
        anomaly_history_citation=(
            _citation(anomaly_history) if anomaly_history.hits else None
        ),
    )

    material_market = {item.material_name: item for item in analysis.market_evidence}
    material_rows: list[list[str]] = []
    for number, item in enumerate(analysis.material_drivers, start=1):
        market = material_market.get(item.name)
        if market and market.relationship == "same_direction":
            reason = "与同名市场行情同向，仅作外部相关性参考"
        elif market:
            reason = "与同名市场行情方向不一致，需进一步核查"
        else:
            reason = "无精确同名市场证据，不判断市场价格影响"
        material_rows.append([str(number), item.name, _money(item.current), _money(item.previous), _pct(item.change_rate_pct, item.status), reason])

    rows = [row for row in bundle.plant1_summary if row.product == product and row.month <= month]
    rows.sort(key=lambda row: row.month)
    trend_rows: list[list[str]] = []
    prior = None
    for row in rows[-6:]:
        rate = None if prior is None else (row.unit_cost - prior.unit_cost) / prior.unit_cost * Decimal("100")
        trend_rows.append([row.month, _qty(row.quantity_boxes), _money(row.direct_material), _money(row.direct_labor), _money(row.manufacturing_overhead), _money(row.unit_cost), _pct(rate)])
        prior = row

    market_points = normalize_market_prices(bundle.market_prices)
    market_index = {(p.material_name, p.month): p for p in market_points}
    market_rows: list[list[str]] = []
    for item in analysis.market_evidence:
        january = market_index.get((item.material_name, f"{month[:4]}-01"))
        current = market_index.get((item.material_name, month))
        if not january or not current:
            continue
        rate = None if january.price == 0 else (current.price - january.price) / january.price * Decimal("100")
        market_rows.append([item.material_name, _money(january.price), _money(current.price), _pct(rate), _direction(current.price - january.price), "仅作外部相关性参考，不代表企业采购价"])

    benchmark_rows = [[item.name, _money(item.target_value), _money(item.benchmark_value), _money(item.difference), _pct(item.difference_rate_pct, item.status), "有利" if item.direction == "favorable" else "不利" if item.direction == "unfavorable" else "持平"] for item in analysis.factory_benchmark]
    unit = summary["单位成本"]
    top_material = analysis.material_drivers[0]
    alert_text = "未触发成本要素±10%阈值告警。" if not analysis.alerts else "；".join(f"{a.name}{_direction(a.delta)}{_pct(abs(a.change_rate_pct))}" for a in analysis.alerts)
    material_summary = summary["直接材料"]
    material_contribution = contribution["直接材料"]
    material_unit_change = (
        "暂无数据"
        if material_summary.delta is None
        else f"{_direction(material_summary.delta)}{_money(abs(material_summary.delta))}元/盒"
    )
    material_total_change = (
        "暂无数据"
        if material_contribution.delta_total_cost is None
        else f"{_signed_money(material_contribution.delta_total_cost)}元"
    )
    top_material_change = (
        "暂无数据"
        if top_material.delta is None
        else f"{_signed_money(top_material.delta)}元/盒"
    )
    top_material_sentence = (
        f"{top_material.name}单位消耗成本变动暂无数据，缺少上月明细，暂不判定最大驱动。"
        if top_material.delta is None
        else f"{top_material.name}单位消耗成本变动{top_material_change}，为最大明细驱动。"
    )
    material_text = (
        f"直接材料单位成本环比{material_unit_change}；"
        f"直接材料总成本变动额{material_total_change}，"
        f"对总成本变动贡献度为{_signed_pct(material_contribution.contribution_pct, material_contribution.status)}。"
        f"{top_material_sentence}"
    )
    if top_material.name in material_market:
        ev = material_market[top_material.name]
        relationship = {
            "same_direction": "一致",
            "opposite_direction": "不一致",
            "no_change": "无同步变动",
        }[ev.relationship]
        price_delta = ev.current_price - ev.previous_price
        material_text += (
            f"同期同名市场价环比{_directional_pct(price_delta, ev.price_change_rate_pct)}，"
            f"方向关系为{relationship}；市场行情不等于企业实际采购价，不能据此断言采购因果。"
        )
    material_text += (
        f"建议：①复核{top_material.name}对应的采购合同、订单、入库单和发票，补齐企业实际采购量价证据；"
        "②核对相关批次的领料、退料和投料记录，确认单位耗用成本变动来源；"
        "③如需归因至工艺收率，应补充批次收率和工艺参数后再作判断。"
    )
    mfg_explanations = {name: f"单位费用环比{_directional_pct(item.delta, item.change_rate_pct, item.status)}；{'超过' if item.change_rate_pct is not None and abs(item.change_rate_pct) > 10 else '未超过'}±10%告警阈值。" for name, item in manufacturing.items()}
    diff_text = "；".join(f"{item.name}一厂较二厂{('低' if (item.difference or 0) < 0 else '高' if (item.difference or 0) > 0 else '持平')}{_money(abs(item.difference or Decimal('0')))}元/盒" for item in analysis.factory_benchmark)
    special_event_note = None
    if product == "六味地黄胶囊" and month == "2026-03":
        special_event_note = (
            "设备记录存在胶囊填充机故障及维修事件，但当月单位成本实际下降；"
            "缺少费用归属与分摊依据，不将该事件写成成本上涨原因。"
        )
    difference_attribution_text, governed_recommendations = build_benchmark_guidance(
        product=product,
        period=month,
        factory=analysis.request.factory,
        benchmark_factory=analysis.request.benchmark_factory,
        gaps=[
            BenchmarkGap(
                name=item.name,
                difference=item.difference,
                difference_rate_pct=item.difference_rate_pct,
                direction=item.direction,
            )
            for item in analysis.factory_benchmark
        ],
        top_material=top_material.name,
        top_material_delta=top_material.delta,
        special_event_note=special_event_note,
    )
    yoy = report_metrics["单位成本同比"]
    budget_variance = report_metrics["单位成本预算偏差"]
    anomaly = (
        f"单位成本环比{_directional_pct(unit.delta, unit.change_rate_pct, unit.status)}，"
        f"同比{_pct(yoy.value, yoy.status)}，预算偏差{_pct(budget_variance.value, budget_variance.status)}。"
        f"{alert_text}"
    )
    if product == "六味地黄胶囊" and month == "2026-03":
        anomaly += "设备文档记录胶囊填充机故障及维修事件，但当月单位成本实际下降；缺少费用归属和分摊依据，不量化其单位成本影响。"
    highlight = f"一厂单位成本较二厂低{_money(abs(benchmark['单位成本'].difference or Decimal('0')))}元/盒；单位成本同比{_pct(yoy.value, yoy.status)}、预算偏差{_pct(budget_variance.value, budget_variance.status)}。" if benchmark["单位成本"].direction == "favorable" else f"单位成本环比{_directional_pct(unit.delta, unit.change_rate_pct, unit.status)}，同比{_pct(yoy.value, yoy.status)}。"
    attention = material_text
    recommendations = [
        [
            item.sequence,
            item.action,
            item.owner,
            item.priority,
            item.expected_effect,
            item.due,
        ]
        for item in governed_recommendations
    ]
    tasks = [[f"TASK-{month.replace('-', '')}-{PRODUCT_TASK_SEQUENCE[product]}", f"复核{top_material.name}成本变动证据", "审批时指定", "medium", f"{month}{product}月度分析", "审批时确定"]]
    tables = {
        "原材料成本明细表格": DynamicTable(name="原材料成本明细表格", headers=["序号", "原材料名称", "本月单位消耗成本", "上月单位消耗成本", "环比", "证据边界"], rows=material_rows),
        "近6个月成本趋势表格": DynamicTable(name="近6个月成本趋势表格", headers=["月份", "产量(盒)", "材料", "人工", "制造费用", "单位成本", "环比"], rows=trend_rows),
        "原材料价格跟踪表格": DynamicTable(name="原材料价格跟踪表格", headers=["原材料", "年初价", "本月价", "涨幅", "趋势", "证据边界"], rows=market_rows or [["暂无精确同名数据", "暂无数据", "暂无数据", "暂无数据", "暂无数据", "不推测"]]),
        "对标差异表格": DynamicTable(name="对标差异表格", headers=["维度", "一厂", "二厂", "差异", "差异率", "方向"], rows=benchmark_rows),
        "改进建议表格": DynamicTable(name="改进建议表格", headers=["序号", "建议事项", "责任部门", "优先级", "预期效果", "建议完成时间"], rows=recommendations),
        "整改任务表格": DynamicTable(name="整改任务表格", headers=["任务编号", "任务标题", "责任人", "优先级", "来源", "截止时间"], rows=tasks),
    }

    values: dict[str, ReportFieldValue] = {}
    def put(name: str, value: str, status: str = "available", refs: list[str] | None = None, rule: str = "确定性映射") -> None:
        values[name] = _field(name, value, status, refs or source_refs, rule)

    put("报告标题", f"{month} {product}月度成本分析报告", "generated", [], "固定标题模板")
    put("报告编号", f"CA-{month.replace('-', '')}-{PRODUCT_CODE[product]}-001", "generated", [], "固定编号规则")
    put("报告类型", "月度成本分析", "generated", [], "固定报告类型")
    put("分析月份", month); put("产品名称", product); put("产品规格", next(r.specification for r in bundle.plant1_summary if r.product == product))
    put("编制日期", generated_date, "generated", [], "生成参数")
    metric_fields = {
        "产量": ("本月产量", "上月产量", "产量环比"), "单位成本": ("本月单位成本", "上月单位成本", "单位成本环比"),
        "总成本": ("本月总成本", "上月总成本", "总成本环比"), "直接材料": ("本月材料成本", "上月材料成本", "材料成本环比"),
        "直接人工": ("本月人工成本", "上月人工成本", "人工成本环比"), "制造费用": ("本月制造费用", "上月制造费用", "制造费用环比"),
    }
    for metric, names in metric_fields.items():
        item=summary[metric]; formatter=_qty if metric=="产量" else _money
        put(names[0], formatter(item.current)); put(names[1], formatter(item.previous), "available" if item.previous is not None else "unavailable"); put(names[2], _pct(item.change_rate_pct,item.status), item.status)
    aliases = {
        "材料金额":"本月材料成本", "材料环比":"材料成本环比", "人工金额":"本月人工成本", "人工单位成本":"本月人工成本", "上月人工单位成本":"上月人工成本", "人工环比":"人工成本环比",
        "制造费用金额":"本月制造费用", "制造费用合计":"本月制造费用", "上月制造费用合计":"上月制造费用", "制造费用合计环比":"制造费用环比", "单位成本":"本月单位成本", "总环比":"单位成本环比",
    }
    for target, source in aliases.items(): values[target]=values[source].model_copy(update={"name":target})
    for component in ("直接材料","直接人工","制造费用"):
        prefix={"直接材料":"材料","直接人工":"人工","制造费用":"制造费用"}[component]
        put(f"{prefix}占比",_pct(structure[component].share_pct,structure[component].status),structure[component].status)
        put(f"{prefix}贡献度",_pct(contribution[component].contribution_pct,contribution[component].status),contribution[component].status)
    mfg_fields={"折旧费":"折旧","动力费(水电气)":"动力","人工(间接)":"间接人工","检验费":"检验","其他制造费用":"其他"}
    for category,prefix in mfg_fields.items():
        item=manufacturing[category]; put(f"本月{prefix}",_money(item.current)); put(f"上月{prefix}",_money(item.previous),item.status); put(f"{prefix}环比",_pct(item.change_rate_pct,item.status),item.status); put(f"{prefix}变动说明",mfg_explanations[category],"generated")
    quantity_metric_names = {"去年同月产量", "预算产量"}
    money_metric_names = {
        "去年单位成本", "去年材料成本", "去年人工成本", "去年制造费用",
        "去年总成本", "预算人工成本", "预算制造费用",
        "预算单位成本", "预算总成本", "预算材料成本",
    }
    supplemental_fields: dict[str, ReportFieldValue] = {}
    for name, item in report_metrics.items():
        if item.unit == "%":
            value = _pct(item.value, item.status)
        elif name in quantity_metric_names:
            value = _qty(item.value)
        elif name in money_metric_names:
            value = _money(item.value)
        else:
            value = _number(item.value)
        rule = item.reason or "V1.1结构化数据计算"
        if name in SUPPLEMENTAL_REPORT_METRICS:
            supplemental_fields[name] = _field(name, value, item.status, source_refs, rule)
        else:
            put(name, value, item.status, [], rule)
    generated_text={"波动告警描述":alert_text,"材料成本归因分析文本":material_text,"成本异常排查分析":anomaly,"差异结构拆解分析":diff_text,"差异归因分析文本":difference_attribution_text,"本月亮点":highlight,"需关注问题":attention}
    for name,text in generated_text.items(): put(name,text,"generated")
    references={"配方文档引用":evidence.recipe_citation,"工艺文档引用":evidence.process_citation,"GMP文档引用":evidence.gmp_citation,"行业基准引用":evidence.industry_citation}
    for name,text in references.items(): put(name,text,"generated",[text],"受治理引用")
    for name,table in tables.items(): put(name,_markdown_rows(table),"generated",[],"动态表格")

    md_path=(root/MARKDOWN_TEMPLATE); word_path=(root/WORD_TEMPLATE)
    expected=template_placeholders(md_path.read_text(encoding="utf-8")); issues=[]
    missing=expected-set(values); extra=set(values)-expected
    if missing: issues.append(f"缺少占位符：{sorted(missing)}")
    if extra: issues.append(f"额外占位符：{sorted(extra)}")
    all_text="\n".join(item.value for item in values.values())
    for pattern in FORBIDDEN_PATTERNS:
        if re.search(pattern,all_text): issues.append(f"命中禁止表达：{pattern}")
    unavailable = {item.name for item in analysis.report_metrics if item.status == "unavailable"}
    for name in unavailable:
        governed = values.get(name) or supplemental_fields.get(name)
        if governed and governed.value != "暂无数据":
            issues.append(f"不可用字段被补值：{name}")
    return ReportContract(contract_version=CONTRACT_VERSION,analysis_version=analysis.analysis_version,formula_version=analysis.formula_version,knowledge_index_version=recipe.index_version,product=product,month=month,report_number=values["报告编号"].value,generated_date=generated_date,markdown_template_path=str(md_path),markdown_template_sha256=_sha(md_path),word_template_path=str(word_path),word_template_sha256=_sha(word_path),fields=values,supplemental_fields=supplemental_fields,dynamic_tables=tables,evidence=evidence,validation_status="PASS" if not issues else "FAIL",validation_issues=issues)

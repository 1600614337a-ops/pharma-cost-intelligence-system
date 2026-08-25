"use strict";

const state = { charts: {}, result: null, report: null, workflow: null, options: null, heatmap: null };
const $ = (id) => document.getElementById(id);
const navigationItems = Array.from(document.querySelectorAll('.nav-item[href^="#"]'));
let navigationLockUntil = 0;
const chartResizeObserver = typeof ResizeObserver === "undefined" ? null : new ResizeObserver((entries) => {
  entries.forEach((entry) => {
    const chart = state.charts[entry.target.id];
    if (chart && entry.contentRect.width > 0 && entry.contentRect.height > 0) chart.resize();
  });
});

const formatMoney = (value, digits = 2) => value === null || value === undefined
  ? "暂无数据"
  : Number(value).toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const formatPct = (value) => value === null || value === undefined ? "暂无数据" : `${Number(value).toFixed(2)}%`;
const formatModelName = (value) => {
  if (!value) return "";
  return String(value).toLowerCase() === "qwen3.8-max" ? "Qwen3.8-Max" : String(value);
};
const formatSigned = (value, suffix = "") => {
  if (value === null || value === undefined) return "暂无数据";
  const number = Number(value);
  return `${number > 0 ? "+" : ""}${number.toFixed(2)}${suffix}`;
};
const directionClass = (value, lowerIsBetter = true) => {
  if (!value) return "neutral-text";
  return lowerIsBetter ? (value < 0 ? "down" : "up") : (value > 0 ? "up" : "down");
};

async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  let payload;
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok) throw new Error(payload.detail || `请求失败（HTTP ${response.status}）`);
  return payload;
}

function renderTrackingStats(stats) {
  $("trackingGenerated").textContent = Number(stats.generated_count).toLocaleString("zh-CN");
  $("trackingConfirmed").textContent = Number(stats.confirmed_count).toLocaleString("zh-CN");
  $("trackingDelivered").textContent = Number(stats.delivered_count).toLocaleString("zh-CN");
  $("trackingFailed").textContent = Number(stats.failed_count).toLocaleString("zh-CN");
}

async function refreshTrackingStats() {
  try {
    renderTrackingStats(await requestJson("/api/workflow-stats"));
  } catch {
    ["trackingGenerated", "trackingConfirmed", "trackingDelivered", "trackingFailed"]
      .forEach((id) => { $(id).textContent = "不可用"; });
  }
}

function setSelectOptions(select, values) {
  select.replaceChildren(...values.map((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    return option;
  }));
}

function showError(message) {
  $("errorNotice").textContent = message;
  $("errorNotice").hidden = false;
}

function setActiveNavigation(item) {
  navigationItems.forEach((candidate) => {
    const active = candidate === item;
    candidate.classList.toggle("active", active);
    if (active) candidate.setAttribute("aria-current", "page");
    else candidate.removeAttribute("aria-current");
  });
  const readingTitle = $("readingPositionTitle");
  if (readingTitle && item) readingTitle.textContent = item.querySelector(".nav-text")?.textContent?.trim() || "成本分析";
}

function navigationTarget(item) {
  const selector = item.getAttribute("href");
  return selector ? document.querySelector(selector) : null;
}

function emphasizeNavigationTarget(target) {
  target.classList.remove("navigation-focus");
  requestAnimationFrame(() => target.classList.add("navigation-focus"));
  window.setTimeout(() => target.classList.remove("navigation-focus"), 1000);
}

function syncActiveNavigation() {
  if (performance.now() < navigationLockUntil || $("dashboard").hidden) return;
  const threshold = Math.min(220, window.innerHeight * .28);
  let active = navigationItems[0];
  const atPageBottom = window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 8;
  if (atPageBottom) {
    const lastVisible = [...navigationItems].reverse().find((item) => {
      const target = navigationTarget(item);
      return target && !target.hidden;
    });
    if (lastVisible) {
      setActiveNavigation(lastVisible);
      return;
    }
  }
  navigationItems.forEach((item) => {
    const target = navigationTarget(item);
    if (target && !target.hidden && target.getBoundingClientRect().top <= threshold) active = item;
  });
  setActiveNavigation(active);
}

function initializeNavigation() {
  navigationItems.forEach((item) => item.addEventListener("click", (event) => {
    event.preventDefault();
    const target = navigationTarget(item);
    if (!target || $("dashboard").hidden) {
      showError("分析内容仍在加载，请稍候再选择目录");
      return;
    }
    setActiveNavigation(item);
    navigationLockUntil = performance.now() + 750;
    history.replaceState(null, "", item.getAttribute("href"));
    target.scrollIntoView({ behavior: "smooth", block: "start" });
    emphasizeNavigationTarget(target);
  }));
  window.addEventListener("scroll", syncActiveNavigation, { passive: true });
}

function setLoading(active) {
  $("analyzeButton").disabled = active;
  $("reportButton").disabled = active || !state.result;
  $("loadingState").hidden = !active;
  if (active) {
    $("dashboard").hidden = true;
    $("errorNotice").hidden = true;
  }
}

function componentMetric(result, name) {
  const currentRow = (result.cost_structure || []).find((item) => item.name === name);
  const changeRow = (result.waterfall || []).find((item) => item.name === name);
  if (!currentRow || currentRow.unit_cost === null || currentRow.unit_cost === undefined) {
    return { current: null, delta: null, change_rate_pct: null, reason: currentRow && currentRow.reason ? currentRow.reason : "暂无成本要素数据" };
  }
  const current = Number(currentRow.unit_cost);
  if (!changeRow || changeRow.delta_unit_cost === null || changeRow.delta_unit_cost === undefined) {
    return { current, delta: null, change_rate_pct: null, reason: changeRow && changeRow.reason ? changeRow.reason : "暂无上期数据" };
  }
  const delta = Number(changeRow.delta_unit_cost);
  const previous = current - delta;
  return {
    current,
    delta,
    change_rate_pct: previous === 0 ? null : delta / previous * 100,
    reason: previous === 0 ? "上期值为零，环比不可计算" : null,
  };
}

function changeDirection(value) {
  if (value === null || value === undefined || Number(value) === 0) return "持平";
  return Number(value) > 0 ? "上升" : "下降";
}

function absoluteRate(value) {
  return `${Math.abs(Number(value)).toFixed(2)}%`;
}

function summaryCurrentValue(metric, unit) {
  if (!metric || metric.current === null || metric.current === undefined) return "暂无数据";
  if (unit === "盒") return `${Number(metric.current).toLocaleString("zh-CN", { maximumFractionDigits: 0 })}盒`;
  if (unit === "元") return `${formatMoney(metric.current, 2)}元`;
  return `${formatMoney(metric.current)}${unit}`;
}

function summaryMetricClause(label, metric, unit, baseline) {
  const current = summaryCurrentValue(metric, unit);
  if (!metric || metric.change_rate_pct === null || metric.change_rate_pct === undefined) {
    return `${label}${current}，${metric && metric.reason ? metric.reason : `暂无${baseline}数据`}`;
  }
  return `${label}${current}，较${baseline}${changeDirection(metric.change_rate_pct)}${absoluteRate(metric.change_rate_pct)}`;
}

function buildAnalysisSummary(result) {
  const quarterly = result.meta.analysis_type === "季度成本分析";
  const baseline = quarterly ? "上季度" : "上月";
  const parts = [
    [
      summaryMetricClause("单位成本", result.kpis.unit_cost, "元/盒", baseline),
      summaryMetricClause("产量", result.kpis.quantity, "盒", baseline),
      summaryMetricClause("总成本", result.kpis.total_cost, "元", baseline),
    ].join("；"),
  ];
  const factorChanges = (result.waterfall || [])
    .filter((item) => item.delta_unit_cost !== null && item.delta_unit_cost !== undefined)
    .sort((left, right) => Math.abs(Number(right.delta_unit_cost)) - Math.abs(Number(left.delta_unit_cost)));
  if (factorChanges.length) {
    const factor = factorChanges[0];
    parts.push(`三项成本要素中，${factor.name}环比变动额最大（${changeDirection(factor.delta_unit_cost)}${formatMoney(Math.abs(Number(factor.delta_unit_cost)))}元/盒）`);
  }
  const efficiency = comparisonValue(result, "效率环比");
  if (efficiency && efficiency.value !== null && efficiency.value !== undefined) {
    parts.push(`人工效率较${baseline}${changeDirection(efficiency.value)}${absoluteRate(efficiency.value)}`);
  }
  const benchmark = result.kpis.factory_benchmark;
  if (benchmark && benchmark.difference !== null && benchmark.difference !== undefined) {
    const comparison = Number(benchmark.difference) < 0 ? "低" : Number(benchmark.difference) > 0 ? "高" : "持平";
    const difference = Number(benchmark.difference) === 0 ? "" : `${formatMoney(Math.abs(Number(benchmark.difference)))}元/盒`;
    parts.push(`${result.meta.factory}单位成本较${result.meta.benchmark_factory}${comparison}${difference}`);
  }
  const alertCount = (result.alerts || []).length;
  parts.push(alertCount ? `共触发${alertCount}项成本波动告警` : "未触发成本要素±10%阈值告警");
  return [
    `${parts[0]}。`,
    `${parts.slice(1).join("；")}。`,
  ];
}

function laborRateSentence(result, name, subject, baseline) {
  const metric = comparisonValue(result, name);
  if (!metric || metric.value === null || metric.value === undefined) return null;
  return `${subject}较${baseline}${changeDirection(metric.value)}${absoluteRate(metric.value)}`;
}

function renderLaborInterpretation(result) {
  const quarterly = result.meta.analysis_type === "季度成本分析";
  const baseline = quarterly ? "上季度" : "上月";
  const observations = [
    laborRateSentence(result, "工时环比", "单位产出耗时", baseline),
    laborRateSentence(result, "时薪环比", "平均小时工资", baseline),
    laborRateSentence(result, "效率环比", "人工效率", baseline),
  ].filter(Boolean);
  const laborCost = result.kpis.direct_labor || componentMetric(result, "直接人工");
  if (laborCost && laborCost.change_rate_pct !== null && laborCost.change_rate_pct !== undefined) {
    observations.push(`综合结果为直接人工单位成本较${baseline}${changeDirection(laborCost.change_rate_pct)}${absoluteRate(laborCost.change_rate_pct)}`);
  }
  $("laborInterpretation").textContent = observations.length
    ? `${observations.join("；")}。这些指标用于判断人工投入、效率与单位人工成本的方向是否一致，不直接证明具体业务原因。`
    : "人工指标暂无完整对比数据，当前不能判断投入、效率与单位人工成本之间的变化关系。";
  const hoursRate = comparisonValue(result, "工时环比");
  const efficiencyRate = comparisonValue(result, "效率环比");
  const efficiencyPressure = (hoursRate && Number(hoursRate.value) > 0) || (efficiencyRate && Number(efficiencyRate.value) < 0);
  $("laborManagementNote").textContent = efficiencyPressure
    ? "效率指标出现压力，建议核查排产、人员配置、停机等待和非生产工时记录；标准工时和工艺收率缺失前，不量化标准差异。"
    : "建议持续跟踪排产、人员配置和非生产工时记录；标准工时和工艺收率缺失前，不量化标准差异。";
}

function renderHeader(result) {
  const { meta, kpis } = result;
  const period = meta.period || meta.month;
  const title = meta.analysis_type === "专题分析" ? `${meta.topic} · ${meta.product}` : `${period} · ${meta.product}`;
  $("scenarioTitle").textContent = title;
  $("pageTitle").textContent = meta.analysis_type;
  $("reportId").textContent = meta.report_number;
  const quarterly = meta.analysis_type === "季度成本分析";
  $("conclusionLabel").textContent = meta.analysis_type === "专题分析" ? "专题分析摘要" : quarterly ? "本季度分析摘要" : "本月分析摘要";
  const trendContext = result.trend_context || {};
  $("trendPeriodLabel").textContent = trendContext.kicker || (quarterly ? "季度序列" : "数据范围");
  $("trendTitle").textContent = trendContext.title || "单位成本趋势";
  $("trendBoundary").textContent = trendContext.boundary_note || "仅展示截至分析期的可得企业数据，不补造缺失月份。";
  $("trendChart").setAttribute("aria-label", trendContext.caption || "单位成本趋势图");
  $("waterfallPeriodLabel").textContent = quarterly ? "季度环比拆解" : "环比拆解";
  $("bridgePeriodLabel").textContent = quarterly ? "季度环比桥接" : "环比桥接";
  $("structurePeriodLabel").textContent = quarterly ? "本季度构成" : "本月构成";
  $("driverCurrentLabel").textContent = quarterly ? "本季度" : "本月";
  $("driverChangeLabel").textContent = quarterly ? "季度环比" : "环比变动";
  const benchmark = kpis.factory_benchmark;
  $("benchmarkTitle").textContent = `${meta.factory} vs ${meta.benchmark_factory}`;
  $("benchmarkValue").textContent = benchmark.difference === null ? "暂无数据" : formatSigned(benchmark.difference);
  $("benchmarkValue").className = `benchmark-number ${benchmark.direction === "favorable" ? "favorable" : benchmark.direction === "unfavorable" ? "up" : "neutral-text"}`;
  $("benchmarkChange").textContent = benchmark.difference_rate_pct === null
    ? "暂无对标数据"
    : `${benchmark.direction === "favorable" ? "有利" : benchmark.direction === "unfavorable" ? "不利" : "持平"} · ${formatSigned(benchmark.difference_rate_pct, "%")}`;
  $("headlineInsight").innerHTML = buildAnalysisSummary(result)
    .map((paragraph) => `<p>${escapeHtml(paragraph)}</p>`)
    .join("");
}

function comparisonValue(result, name) {
  return result.comparisons && result.comparisons[name] ? result.comparisons[name] : null;
}

function formatComparisonMetric(metric) {
  if (!metric || metric.value === null || metric.value === undefined) return "暂无数据";
  if (metric.unit === "%") return formatSigned(metric.value, "%");
  if (metric.unit === "盒") return Number(metric.value).toLocaleString("zh-CN", { maximumFractionDigits: 0 });
  return `${Number(metric.value).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${metric.unit ? ` ${metric.unit}` : ""}`;
}

function primaryComparisonValue(value, unit) {
  if (value === null || value === undefined) return "暂无数据";
  return unit === "盒"
    ? Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 0 })
    : Number(value).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function semanticTone(value, favorableWhenUp = false) {
  if (value === null || value === undefined || Number(value) === 0) return "metric-neutral";
  const favorable = favorableWhenUp ? Number(value) > 0 : Number(value) < 0;
  return favorable ? "metric-good" : "metric-alert";
}

function trendArrow(value) {
  if (value === null || value === undefined || Number(value) === 0) return "→";
  return Number(value) > 0 ? "↗" : "↘";
}

function budgetState(metric, favorableWhenUp = false) {
  if (!metric || metric.value === null || metric.value === undefined) return "预算暂无";
  const value = Number(metric.value);
  if (value === 0) return "贴合预算";
  return `${value > 0 ? "高于" : "低于"}预算`;
}

function deltaMeterClass(metric) {
  if (!metric || metric.value === null || metric.value === undefined) return "meter-0";
  const magnitude = Math.abs(Number(metric.value));
  if (magnitude === 0) return "meter-0";
  if (magnitude < 1) return "meter-1";
  if (magnitude < 3) return "meter-2";
  if (magnitude < 6) return "meter-3";
  if (magnitude < 10) return "meter-4";
  if (magnitude < 15) return "meter-5";
  return "meter-6";
}

function comparisonPoint(title, baselineLabel, baseline, delta, favorableWhenUp) {
  const value = delta && delta.value !== null && delta.value !== undefined ? Number(delta.value) : null;
  const tone = semanticTone(value, favorableWhenUp);
  return `<div class="comparison-point">
    <div class="comparison-point-head"><span>${escapeHtml(title)}</span><strong class="${tone}">${trendArrow(value)} ${formatComparisonMetric(delta)}</strong></div>
    <small>${escapeHtml(baselineLabel)} ${formatComparisonMetric(baseline)}</small>
    <div class="metric-delta-meter" aria-hidden="true"><i class="${tone} ${deltaMeterClass(delta)}"></i></div>
  </div>`;
}

function kpiBaselineMetric(kpi, unit) {
  return {
    value: kpi && kpi.previous !== undefined ? kpi.previous : null,
    unit,
  };
}

function kpiChangeMetric(kpi) {
  return {
    value: kpi && kpi.change_rate_pct !== undefined ? kpi.change_rate_pct : null,
    unit: "%",
  };
}

function renderComparisons(result) {
  const panel = $("comparisonSection");
  const laborPanel = $("laborEfficiencySection");
  if (!result.comparisons) {
    panel.hidden = true;
    laborPanel.hidden = true;
    return;
  }
  panel.hidden = false;
  laborPanel.hidden = false;
  const quarterly = result.meta.analysis_type === "季度成本分析";
  $("businessPeriodLabel").textContent = quarterly ? "本季度实际 · 上季度 · 去年同期 · 季度预算" : "本月实际 · 上月 · 去年同月 · 年度预算";
  $("laborPeriodLabel").textContent = quarterly ? "本季度对上季度" : "本月对上月";
  $("comparisonBoundary").textContent = "绿色表示经营有利，红色表示经营不利。";
  const rows = [
    { label: "单位成本", kpi: result.kpis.unit_cost, current: result.kpis.unit_cost.current, unit: "元/盒", priorName: "去年单位成本", yoyName: "单位成本同比", budgetName: "预算单位成本", varianceName: "单位成本预算偏差", favorableWhenUp: false, costComponent: false },
    { label: "产量", kpi: result.kpis.quantity, current: result.kpis.quantity.current, unit: "盒", priorName: "去年同月产量", yoyName: "产量同比", budgetName: "预算产量", varianceName: "产量预算偏差", favorableWhenUp: true, costComponent: false },
    { label: "总成本", kpi: result.kpis.total_cost, current: result.kpis.total_cost.current, unit: "元", priorName: "去年总成本", yoyName: "总成本同比", budgetName: "预算总成本", varianceName: "总成本预算偏差", favorableWhenUp: false, costComponent: false },
    { label: "直接材料", kpi: result.kpis.direct_material, current: result.kpis.direct_material.current, unit: "元/盒", priorName: "去年材料成本", yoyName: "材料同比", budgetName: "预算材料成本", varianceName: "材料预算偏差", favorableWhenUp: false, costComponent: true },
    { label: "直接人工", kpi: result.kpis.direct_labor, current: result.kpis.direct_labor.current, unit: "元/盒", priorName: "去年人工成本", yoyName: "人工同比", budgetName: "预算人工成本", varianceName: "人工预算偏差", favorableWhenUp: false, costComponent: true },
    { label: "制造费用", kpi: result.kpis.manufacturing_overhead, current: result.kpis.manufacturing_overhead.current, unit: "元/盒", priorName: "去年制造费用", yoyName: "制造费用同比", budgetName: "预算制造费用", varianceName: "制造费用预算偏差", favorableWhenUp: false, costComponent: true },
  ];
  $("yearBudgetCards").innerHTML = rows.map((row) => {
    const prior = comparisonValue(result, row.priorName);
    const yoy = comparisonValue(result, row.yoyName);
    const budget = comparisonValue(result, row.budgetName);
    const variance = comparisonValue(result, row.varianceName);
    const varianceTone = semanticTone(variance && variance.value, row.favorableWhenUp);
    return `<article class="business-metric-card">
      <div class="business-card-head"><div class="business-card-title"><h5>${row.label}</h5>${row.costComponent ? '<span class="business-component-label">成本要素</span>' : ""}</div><span class="metric-state ${varianceTone}">${budgetState(variance, row.favorableWhenUp)}</span></div>
      <div class="business-primary"><strong>${primaryComparisonValue(row.current, row.unit)}</strong><span>${row.unit}</span></div>
      ${comparisonPoint("环比变动", quarterly ? "上季度" : "上月", kpiBaselineMetric(row.kpi, row.unit), kpiChangeMetric(row.kpi), row.favorableWhenUp)}
      ${comparisonPoint("同比变动", quarterly ? "去年同期" : "去年同月", prior, yoy, row.favorableWhenUp)}
      ${comparisonPoint("预算偏差", "预算值", budget, variance, row.favorableWhenUp)}
    </article>`;
  }).join("");
  const laborRows = [
    { label: "人工工时", description: "单位产出耗时", icon: "时", currentName: "本月工时", priorName: "上月工时", rateName: "工时环比", favorableWhenUp: false },
    { label: "平均小时工资", description: "平均工时成本", icon: "薪", currentName: "本月时薪", priorName: "上月时薪", rateName: "时薪环比", favorableWhenUp: false },
    { label: "人工效率", description: "人均日产出", icon: "效", currentName: "本月效率", priorName: "上月效率", rateName: "效率环比", favorableWhenUp: true },
  ];
  $("laborMetricCards").innerHTML = laborRows.map((row) => {
    const current = comparisonValue(result, row.currentName);
    const previous = comparisonValue(result, row.priorName);
    const rate = comparisonValue(result, row.rateName);
    const tone = semanticTone(rate && rate.value, row.favorableWhenUp);
    return `<article class="labor-metric-card">
      <span class="labor-metric-icon" aria-hidden="true">${row.icon}</span>
      <div class="labor-metric-copy"><span>${row.description}</span><h5>${row.label}</h5><div class="labor-primary"><strong>${primaryComparisonValue(current && current.value, current && current.unit)}</strong><span>${current && current.unit ? current.unit : ""}</span></div></div>
      <div class="labor-trend"><span>${quarterly ? "较上季度" : "较上月"}</span><strong class="${tone}">${trendArrow(rate && rate.value)} ${formatComparisonMetric(rate)}</strong><small>${quarterly ? "上季度" : "上月"} ${formatComparisonMetric(previous)}</small></div>
    </article>`;
  }).join("");
  renderLaborInterpretation(result);
}

function commonChartOption() {
  return {
    animationDuration: 550,
    textStyle: { fontFamily: "Microsoft YaHei UI, sans-serif", color: "#52645d" },
    tooltip: { backgroundColor: "rgba(16,42,37,.94)", borderWidth: 0, textStyle: { color: "#fff", fontSize: 12 } },
    aria: { enabled: true, decal: { show: false } },
  };
}

function chartFor(id) {
  if (!window.echarts) throw new Error("ECharts 图表库未加载，请检查网络后刷新页面");
  if (!state.charts[id]) {
    const container = $(id);
    state.charts[id] = window.echarts.init(container);
    if (chartResizeObserver) chartResizeObserver.observe(container);
  }
  return state.charts[id];
}

function renderTrend(rows) {
  const chart = chartFor("trendChart");
  chart.setOption({
    ...commonChartOption(),
    grid: { left: 24, right: 34, top: 42, bottom: 28, containLabel: true },
    legend: { top: 8, right: 20, itemWidth: 18, itemHeight: 3, textStyle: { fontSize: 11 } },
    tooltip: { ...commonChartOption().tooltip, trigger: "axis", valueFormatter: (value) => `${formatMoney(value)} 元/盒` },
    xAxis: { type: "category", boundaryGap: false, data: rows.map((row) => row.month), axisLine: { lineStyle: { color: "#d7e1dc" } }, axisTick: { show: false }, axisLabel: { fontSize: 11 } },
    yAxis: { type: "value", scale: true, name: "元/盒", nameTextStyle: { fontSize: 11 }, splitLine: { lineStyle: { color: "#edf1ef" } }, axisLabel: { fontSize: 11 } },
    series: [
      { name: "单位成本", type: "line", smooth: .25, symbolSize: 7, data: rows.map((row) => row.unit_cost), lineStyle: { width: 3, color: "#165c49" }, itemStyle: { color: "#165c49" }, areaStyle: { color: { type: "linear", x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: "rgba(22,92,73,.22)" }, { offset: 1, color: "rgba(22,92,73,0)" }] } } },
      { name: "直接材料", type: "line", smooth: .25, symbol: "none", data: rows.map((row) => row.direct_material), lineStyle: { width: 1.5, color: "#d99832" }, itemStyle: { color: "#d99832" } },
    ],
  }, true);
}

function renderContribution(items) {
  const chart = chartFor("contributionChart");
  const available = items.filter((item) => item.contribution_pct !== null && item.contribution_pct !== undefined);
  const total = available.reduce((sum, item) => sum + Number(item.contribution_pct), 0);
  $("contributionTotal").textContent = available.length === items.length ? `合计 ${formatPct(total)}` : "贡献度不适用";
  if (!available.length) {
    chart.setOption({
      ...commonChartOption(),
      xAxis: { show: false }, yAxis: { show: false }, series: [],
      graphic: [{
        type: "text", left: "center", top: "middle",
        style: { text: items[0]?.reason || "暂无可计算贡献度", fill: "#6f817a", font: "13px Microsoft YaHei UI, sans-serif" },
      }],
    }, true);
    return;
  }
  const values = available.map((item) => Number(item.contribution_pct));
  const hasNegative = values.some((value) => value < 0);
  const axisExtent = Math.max(100, Math.ceil(Math.max(...values.map(Math.abs)) / 10) * 10) * 1.08;
  chart.setOption({
    ...commonChartOption(),
    grid: { left: 24, right: 66, top: 32, bottom: 38, containLabel: true },
    tooltip: { ...commonChartOption().tooltip, trigger: "item", formatter: ({ data }) => (
      `${escapeHtml(data.name)}<br>总成本变动额：<strong>${formatSigned(data.delta)} 元</strong><br>对总成本变动贡献度：<strong>${formatPct(data.value)}</strong>`
    ) },
    xAxis: {
      type: "value", min: hasNegative ? -axisExtent : 0, max: axisExtent, name: "对总成本变动贡献度（%）", nameLocation: "middle", nameGap: 27,
      axisLabel: { color: "#687a73", fontSize: 11, formatter: (value) => `${Number(value).toFixed(0)}%` },
      splitLine: { lineStyle: { color: "#edf1ef" } }, axisLine: { show: false }, axisTick: { show: false },
    },
    yAxis: {
      type: "category", inverse: true, data: available.map((item) => item.name),
      axisLabel: { color: "#344b43", fontSize: 12, fontWeight: 650 }, axisLine: { show: false }, axisTick: { show: false },
    },
    series: [{
      name: "贡献度", type: "bar", barWidth: 24,
      data: available.map((item) => {
        const delta = Number(item.delta_total_cost);
        return {
          name: item.name, value: Number(item.contribution_pct), delta,
          itemStyle: { color: delta < 0 ? "#21835f" : delta > 0 ? "#b94343" : "#82958d", borderRadius: delta >= 0 ? [0, 3, 3, 0] : [3, 0, 0, 3] },
          label: { position: Number(item.contribution_pct) >= 0 ? "right" : "left" },
        };
      }),
      label: { show: true, color: "#344b43", fontSize: 12, fontWeight: 700, formatter: ({ value }) => formatPct(value) },
      markLine: { silent: true, symbol: "none", data: [{ xAxis: 0 }], lineStyle: { color: "#a5b7b0", width: 1.2 }, label: { show: false } },
    }],
  }, true);
}

function waterfallData(items) {
  const usable = items.filter((item) => item.delta_unit_cost !== null);
  let cumulative = 0;
  const names = [];
  const bridges = usable.map((item, index) => {
    const delta = Number(item.delta_unit_cost);
    names.push(item.name);
    const start = cumulative;
    cumulative += delta;
    return { name: item.name, value: [index, start, cumulative, delta, 0] };
  });
  names.push("总变动");
  bridges.push({ name: "总变动", value: [names.length - 1, 0, cumulative, cumulative, 1] });
  return { names, bridges, total: cumulative };
}

function renderWaterfall(items) {
  const chart = chartFor("waterfallChart");
  const data = waterfallData(items);
  $("waterfallHint").textContent = data.total < 0 ? "负数表示成本下降" : data.total > 0 ? "正数表示成本上升" : "本月成本持平";
  chart.setOption({
    ...commonChartOption(),
    grid: { left: 18, right: 24, top: 40, bottom: 44, containLabel: true },
    tooltip: { ...commonChartOption().tooltip, trigger: "item", formatter: (params) => {
      if (params.seriesName !== "成本变动") return "";
      return `${params.name}<br><strong>${formatSigned(params.data.value[3])} 元/盒</strong>`;
    } },
    xAxis: { type: "category", data: data.names, axisTick: { show: false }, axisLine: { lineStyle: { color: "#d7e1dc" } }, axisLabel: { fontSize: 11, interval: 0 } },
    yAxis: { type: "value", name: "元/盒", boundaryGap: ["18%", "18%"], nameTextStyle: { fontSize: 11 }, splitLine: { lineStyle: { color: "#edf1ef" } }, axisLabel: { fontSize: 11 } },
    series: [
      {
        name: "成本变动",
        type: "custom",
        encode: { x: 0, y: [1, 2] },
        renderItem: (params, api) => {
          const category = Number(api.value(0));
          const startPoint = api.coord([category, api.value(1)]);
          const endPoint = api.coord([category, api.value(2)]);
          const delta = Number(api.value(3));
          const isTotal = Number(api.value(4)) === 1;
          const bandWidth = api.size([1, 0])[0];
          const width = Math.min(52, bandWidth * .46);
          const rawHeight = Math.abs(endPoint[1] - startPoint[1]);
          const height = Math.max(2, rawHeight);
          const top = Math.min(startPoint[1], endPoint[1]) - (height - rawHeight) / 2;
          const bottom = top + height;
          const fill = isTotal ? "#165c49" : delta >= 0 ? "#b94343" : "#21835f";
          const children = [{
            type: "rect",
            shape: { x: startPoint[0] - width / 2, y: top, width, height },
            style: { fill, opacity: .96 },
          }, {
            type: "text",
            style: {
              x: startPoint[0],
              y: delta >= 0 ? top - 8 : bottom + 8,
              text: formatSigned(delta),
              textAlign: "center",
              textVerticalAlign: delta >= 0 ? "bottom" : "top",
              fill: "#42534d",
              font: "11px Segoe UI, sans-serif",
            },
          }];
          if (!isTotal) {
            children.push({
              type: "line",
              shape: {
                x1: startPoint[0] + width / 2,
                y1: endPoint[1],
                x2: startPoint[0] + bandWidth - width / 2,
                y2: endPoint[1],
              },
              style: { stroke: "#aebfb8", lineWidth: 1, lineDash: [4, 3] },
              silent: true,
            });
          }
          return { type: "group", children };
        },
        data: data.bridges,
      },
      {
        name: "零轴",
        type: "line",
        silent: true,
        symbol: "none",
        data: data.names.map(() => 0),
        lineStyle: { color: "#9eb5ac", width: 1, type: "dashed" },
      },
    ],
  }, true);
}

function totalCostBridgeData(bridge) {
  if (!bridge || bridge.status !== "available" || bridge.previous_total_cost === null) {
    return { names: [], bridges: [], status: "unavailable", reason: bridge?.reason || "暂无上期数据" };
  }
  const previous = Number(bridge.previous_total_cost);
  const current = Number(bridge.current_total_cost);
  const quantityEffect = Number(bridge.quantity_effect);
  const unitCostEffect = Number(bridge.unit_cost_effect);
  const afterQuantity = previous + quantityEffect;
  return {
    names: ["上期总成本", "产量影响", "单位成本影响", "本期总成本"],
    bridges: [
      { name: "上期总成本", value: [0, 0, previous, previous, 1, 0] },
      { name: "产量影响", value: [1, previous, afterQuantity, quantityEffect, 0, 1] },
      { name: "单位成本影响", value: [2, afterQuantity, afterQuantity + unitCostEffect, unitCostEffect, 0, 2] },
      { name: "本期总成本", value: [3, 0, current, current, 1, 3] },
    ],
    status: "available",
    total: Number(bridge.total_cost_delta),
    reconciliation: Number(bridge.reconciliation_difference),
  };
}

function renderTotalCostBridge(bridge) {
  const chart = chartFor("totalCostBridgeChart");
  const data = totalCostBridgeData(bridge);
  if (data.status !== "available") {
    $("totalCostBridgeHint").textContent = data.reason;
    chart.setOption({
      ...commonChartOption(),
      xAxis: { show: false }, yAxis: { show: false }, series: [],
      graphic: [{
        type: "text", left: "center", top: "middle",
        style: { text: data.reason, fill: "#6f817a", font: "13px Microsoft YaHei UI, sans-serif" },
      }],
    }, true);
    return;
  }
  $("totalCostBridgeHint").textContent = `总成本净变动 ${data.total > 0 ? "+" : ""}${formatMoney(data.total, 2)} 元`;
  chart.setOption({
    ...commonChartOption(),
    grid: { left: 24, right: 30, top: 48, bottom: 46, containLabel: true },
    tooltip: { ...commonChartOption().tooltip, trigger: "item", formatter: (params) => {
      if (params.seriesName !== "总成本桥接") return "";
      const isTotal = Number(params.data.value[4]) === 1;
      const amount = Number(params.data.value[3]);
      return `${params.name}<br><strong>${isTotal ? formatMoney(amount, 2) : `${amount > 0 ? "+" : ""}${formatMoney(amount, 2)}`} 元</strong>`;
    } },
    xAxis: {
      type: "category", data: data.names, axisTick: { show: false },
      axisLine: { lineStyle: { color: "#d7e1dc" } },
      axisLabel: { color: "#344b43", fontSize: 13, fontWeight: 650, interval: 0 },
    },
    yAxis: {
      type: "value", name: "元", min: 0, boundaryGap: ["0%", "14%"],
      nameTextStyle: { color: "#687a73", fontSize: 12 },
      splitLine: { lineStyle: { color: "#edf1ef" } },
      axisLabel: { color: "#687a73", fontSize: 12, formatter: (value) => Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 0 }) },
    },
    series: [{
      name: "总成本桥接",
      type: "custom",
      encode: { x: 0, y: [1, 2] },
      renderItem: (params, api) => {
        const category = Number(api.value(0));
        const startPoint = api.coord([category, api.value(1)]);
        const endPoint = api.coord([category, api.value(2)]);
        const delta = Number(api.value(3));
        const isTotal = Number(api.value(4)) === 1;
        const kind = Number(api.value(5));
        const bandWidth = api.size([1, 0])[0];
        const width = Math.min(86, bandWidth * .42);
        const rawHeight = Math.abs(endPoint[1] - startPoint[1]);
        const height = Math.max(3, rawHeight);
        const top = Math.min(startPoint[1], endPoint[1]) - (height - rawHeight) / 2;
        const bottom = top + height;
        const fill = kind === 0 ? "#789087"
          : kind === 1 ? "#d99832"
            : kind === 2 ? (delta >= 0 ? "#b94343" : "#21835f")
              : "#165c49";
        const label = isTotal
          ? formatMoney(delta, 2)
          : `${delta > 0 ? "+" : ""}${formatMoney(delta, 2)}`;
        const children = [{
          type: "rect",
          shape: { x: startPoint[0] - width / 2, y: top, width, height },
          style: { fill, opacity: .97 },
        }, {
          type: "text",
          style: {
            x: startPoint[0], y: delta >= 0 ? top - 10 : bottom + 10,
            text: label, textAlign: "center", textVerticalAlign: delta >= 0 ? "bottom" : "top",
            fill: "#344b43", font: "600 12px Segoe UI, Microsoft YaHei UI, sans-serif",
          },
        }];
        if (category < data.names.length - 1) {
          children.push({
            type: "line",
            shape: {
              x1: startPoint[0] + width / 2, y1: endPoint[1],
              x2: startPoint[0] + bandWidth - width / 2, y2: endPoint[1],
            },
            style: { stroke: "#aebfb8", lineWidth: 1.2, lineDash: [5, 4] },
            silent: true,
          });
        }
        return { type: "group", children };
      },
      data: data.bridges,
    }],
  }, true);
}

function renderStructure(items) {
  const chart = chartFor("structureChart");
  const totalUnitCost = items.reduce((total, item) => total + Number(item.unit_cost || 0), 0);
  chart.setOption({
    ...commonChartOption(),
    color: ["#165c49", "#d6b46b", "#82a89b"],
    tooltip: { ...commonChartOption().tooltip, trigger: "item", formatter: (p) => `${p.name}<br><strong>${formatMoney(p.value)} 元/盒</strong><br>${Number(p.data.share).toFixed(2)}%` },
    legend: { show: false },
    series: [
      {
        name: "成本结构", type: "pie", radius: ["39%", "61%"], center: ["50%", "50%"],
        avoidLabelOverlap: true, padAngle: 2, itemStyle: { borderRadius: 2, borderColor: "#fff", borderWidth: 2 },
        label: {
          show: true, position: "outside", alignTo: "edge", edgeDistance: 12, bleedMargin: 4,
          formatter: (p) => `{name|${p.name}}\n{value|${formatMoney(p.value)} 元/盒 · ${Number(p.data.share).toFixed(2)}%}`,
          rich: {
            name: { color: "#29483e", fontSize: 12, fontWeight: 700, lineHeight: 19 },
            value: { color: "#66766f", fontFamily: "Segoe UI, Microsoft YaHei UI, sans-serif", fontSize: 11, fontWeight: 600, lineHeight: 17 },
          },
        },
        labelLine: { show: true, length: 12, length2: 8, smooth: .18, lineStyle: { color: "#9cb3aa", width: 1.2 } },
        emphasis: { scaleSize: 5 },
        data: items.map((item) => ({ name: item.name, value: item.unit_cost, share: item.share_pct })),
      },
      {
        name: "单位成本合计", type: "pie", radius: [0, 0], center: ["50%", "50%"],
        silent: true, animation: false, tooltip: { show: false }, z: 10,
        labelLine: { show: false },
        label: {
          show: true, position: "center",
          formatter: `{total|${formatMoney(totalUnitCost, 2)}}\n{unit|元/盒}\n{caption|本月单位成本}`,
          rich: {
            total: { color: "#14231f", fontFamily: "Segoe UI, sans-serif", fontSize: 22, fontWeight: 700, lineHeight: 28 },
            unit: { color: "#66766f", fontSize: 11, fontWeight: 600, lineHeight: 16 },
            caption: { color: "#66766f", fontSize: 11, lineHeight: 16 },
          },
        },
        data: [{ value: 1, itemStyle: { color: "transparent" } }],
      },
    ],
  }, true);
}

function renderBenchmarkTree(result) {
  const panel = $("benchmarkTreeSection");
  const unitNode = result.benchmark_tree && result.benchmark_tree.children
    ? result.benchmark_tree.children.find((node) => node.kind === "total")
    : null;
  if (!unitNode) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  const chartText = (value) => String(value ?? "").replace(/[{}|]/g, "");
  const toneFor = (node) => node.kind === "plant1_material" || node.status === "unavailable"
    ? "missing"
    : node.direction === "favorable" ? "good" : node.direction === "unfavorable" ? "bad" : "neutral";
  const outcomeFor = (node) => toneFor(node) === "good"
    ? "经营有利"
    : toneFor(node) === "bad" ? "经营不利" : toneFor(node) === "missing" ? "对标明细暂缺" : "基本持平";
  const palette = {
    good: { fill: "#edf8f3", border: "#21835f" },
    bad: { fill: "#fff2f0", border: "#b94343" },
    neutral: { fill: "#f3f6f4", border: "#72857d" },
    missing: { fill: "#fff8e9", border: "#c18a2c" },
  };
  const nodeLabel = (node) => {
    const tone = toneFor(node);
    if (node.kind === "plant1_material") {
      return `{title|${chartText(node.name)}}\n{value|${chartText(result.meta.factory)} ${formatMoney(node.target_value)} 元/盒}\n{missing|对标明细暂缺}`;
    }
    const title = node.kind === "root" ? result.meta.product : node.name;
    const eyebrow = node.kind === "root" ? "{eyebrow|单位成本总差异}\n" : "";
    return `${eyebrow}{title|${chartText(title)}}\n{value|${formatSigned(node.difference)} 元/盒 · ${formatSigned(node.difference_rate_pct, "%")}}\n{${tone}|${outcomeFor(node)}}`;
  };
  const decorate = (node) => {
    const tone = toneFor(node);
    const root = node.kind === "root";
    const material = node.kind === "plant1_material";
    return {
      ...node,
      symbol: "roundRect",
      symbolSize: root ? [220, 100] : material ? [206, 76] : [194, 82],
      itemStyle: {
        color: palette[tone].fill,
        borderColor: palette[tone].border,
        borderWidth: root ? 3 : 2,
        shadowBlur: 10,
        shadowColor: "rgba(24,61,50,.10)",
        shadowOffsetY: 3,
      },
      label: {
        show: true,
        position: "inside",
        align: "center",
        verticalAlign: "middle",
        formatter: nodeLabel(node),
        rich: {
          eyebrow: { color: "#60736b", fontSize: 11, fontWeight: 700, lineHeight: 16 },
          title: { color: "#14231f", fontSize: root ? 17 : 15, fontWeight: 800, lineHeight: root ? 24 : 22 },
          value: { color: "#31443d", fontSize: root ? 14 : 13, fontWeight: 700, lineHeight: 22 },
          good: { color: "#156b4d", backgroundColor: "#dcefe6", borderRadius: 10, padding: [3, 8], fontSize: 12, fontWeight: 800, lineHeight: 22 },
          bad: { color: "#a93636", backgroundColor: "#f8dfdc", borderRadius: 10, padding: [3, 8], fontSize: 12, fontWeight: 800, lineHeight: 22 },
          neutral: { color: "#53675f", backgroundColor: "#e5ebe8", borderRadius: 10, padding: [3, 8], fontSize: 12, fontWeight: 800, lineHeight: 22 },
          missing: { color: "#8a6017", backgroundColor: "#f7e8c5", borderRadius: 10, padding: [3, 8], fontSize: 12, fontWeight: 800, lineHeight: 22 },
        },
      },
      children: (node.children || []).map(decorate),
    };
  };
  const difference = unitNode.difference;
  const outcome = outcomeFor(unitNode);
  if (difference === null || difference === undefined) {
    $("benchmarkTreeHeadline").textContent = `${result.meta.product}暂无可用对标差异`;
  } else if (Number(difference) === 0) {
    $("benchmarkTreeHeadline").textContent = `${result.meta.product}单位成本与${result.meta.benchmark_factory}持平`;
  } else {
    const relation = Number(difference) < 0 ? "低" : "高";
    $("benchmarkTreeHeadline").textContent = `${result.meta.product}单位成本较${result.meta.benchmark_factory}${relation} ${formatMoney(Math.abs(Number(difference)))} 元/盒，${outcome}`;
  }
  $("benchmarkTreeBasis").textContent = `${result.meta.factory} − ${result.meta.benchmark_factory} · ${result.meta.period || result.meta.month} · 差异率 ${formatSigned(unitNode.difference_rate_pct, "%")}`;
  const componentDescriptions = (unitNode.children || []).map((node) => (
    `${node.name}${formatSigned(node.difference)}元/盒，${outcomeFor(node)}`
  ));
  const missingMaterialNames = (unitNode.children || [])
    .flatMap((node) => node.children || [])
    .filter((node) => node.kind === "plant1_material")
    .map((node) => node.name);
  const relationDescription = difference === null || difference === undefined
    ? "暂无可用的单位成本总差异"
    : `单位成本总差异${formatSigned(difference)}元/盒，${outcome}`;
  const boundaryDescription = missingMaterialNames.length
    ? `直接材料下钻展示${result.meta.factory}的${missingMaterialNames.join("、")}单位消耗成本；${result.meta.benchmark_factory}未提供原材料明细，因此叶子节点不计算两厂差异。`
    : "没有可继续下钻的材料明细。";
  const treeDescription = `${result.meta.product}${result.meta.period || result.meta.month}成本差异结构树。${relationDescription}。一级分支：${componentDescriptions.join("；")}。${boundaryDescription}`;
  $("benchmarkTreeChart").setAttribute("aria-label", treeDescription);
  const displayRoot = {
    ...unitNode,
    name: result.meta.product,
    metric_name: "单位成本总差异",
    kind: "root",
  };
  const chart = chartFor("benchmarkTreeChart");
  chart.setOption({
    ...commonChartOption(),
    aria: { enabled: true, description: treeDescription, decal: { show: false } },
    tooltip: {
      ...commonChartOption().tooltip,
      trigger: "item",
      formatter: ({ data }) => {
        if (data.kind === "plant1_material") {
          return `${escapeHtml(data.name)}<br>${escapeHtml(result.meta.factory)}：<strong>${formatMoney(data.target_value)} 元/盒</strong><br>${escapeHtml(result.meta.benchmark_factory)}：暂无数据<br><span>对标厂未提供原材料明细</span>`;
        }
        if (data.difference === null || data.difference === undefined) return escapeHtml(data.name);
        const title = data.kind === "root" ? `${data.name}·单位成本总差异` : data.name;
        return `${escapeHtml(title)}<br>${escapeHtml(result.meta.factory)}：${formatMoney(data.target_value)} 元/盒<br>${escapeHtml(result.meta.benchmark_factory)}：${formatMoney(data.benchmark_value)} 元/盒<br>差异：<strong>${formatSigned(data.difference)} 元/盒</strong><br>差异率：${formatSigned(data.difference_rate_pct, "%")}<br>评价：${outcomeFor(data)}`;
      },
    },
    series: [{
      type: "tree",
      data: [decorate(displayRoot)],
      top: "8%", left: "12%", bottom: "8%", right: "18%",
      orient: "LR", expandAndCollapse: true, initialTreeDepth: 3,
      roam: false,
      lineStyle: { color: "#9eb9ae", width: 2, curveness: .42 },
      emphasis: { focus: "descendant", lineStyle: { width: 3, color: "#5d8c7a" } },
    }],
  }, true);
}

function renderBenchmarkAttribution(result) {
  const narrative = String(result.narratives?.["差异归因分析文本"] || "").trim();
  const paragraphs = narrative ? narrative.split(/\n\s*\n/).map((item) => item.trim()).filter(Boolean) : [];
  $("benchmarkAttributionText").innerHTML = paragraphs.length
    ? paragraphs.map((paragraph) => `<p>${escapeHtml(paragraph)}</p>`).join("")
    : '<p class="neutral-text">暂无可用的归因分析文本。</p>';

  const recommendations = Array.isArray(result.recommendations) ? result.recommendations : [];
  if (!recommendations.length) {
    $("benchmarkRecommendationList").innerHTML = '<p class="neutral-text">暂无可执行建议，不根据缺失数据推测。</p>';
    return;
  }
  $("benchmarkRecommendationList").innerHTML = recommendations.map((item, index) => {
    return `<div class="benchmark-recommendation-item">
      <span class="benchmark-recommendation-number">${escapeHtml(item.sequence || index + 1)}</span>
      <div class="benchmark-recommendation-body">
        <strong>${escapeHtml(item.action)}</strong>
        <div class="benchmark-recommendation-meta">
          <span>责任部门：${escapeHtml(item.owner || "待业务确认")}</span>
          <span>优先级：${escapeHtml(item.priority || "中")}</span>
        </div>
      </div>
      <span class="benchmark-rpa-ready" title="需经人工审核后下发">可转RPA</span>
    </div>`;
  }).join("");
}

function renderMaterialNarrative(value) {
  const text = String(value || "暂无材料成本归因说明");
  const marker = "建议：";
  const markerIndex = text.indexOf(marker);
  const lines = markerIndex < 0
    ? [text]
    : [text.slice(0, markerIndex).trim(), text.slice(markerIndex).trim()].filter(Boolean);
  $("materialNarrative").innerHTML = lines.map((line) => `<span>${escapeHtml(line)}</span>`).join("");
}

function applyReportGeneratedContent(report) {
  const webContent = report.web_content;
  if (!state.result || !webContent) return;
  if (webContent.narratives) {
    state.result.narratives = { ...state.result.narratives, ...webContent.narratives };
  }
  if (Array.isArray(webContent.recommendations)) {
    state.result.recommendations = webContent.recommendations;
  }
  const generation = report.generation || {};
  $("benchmarkNarrativeMode").textContent = generation.status === "generated"
    ? `大模型受控生成 · ${formatModelName(generation.model) || "已配置模型"} · RPA候选建议`
    : generation.status === "fallback"
      ? "确定性安全回退 · RPA候选建议"
      : "确定性分析 · RPA候选建议";
  renderHeader(state.result);
  renderMaterialNarrative(state.result.narratives["材料成本归因分析文本"]);
  renderBenchmarkAttribution(state.result);
}

function heatmapMetricValue(cell, metric) {
  if (metric === "要素单位成本") return cell.current;
  if (metric === "一厂减二厂差异") return cell.factory_difference;
  return cell.change_rate_pct;
}

function renderHeatmap() {
  if (!state.heatmap || $("dashboard").hidden) return;
  const payload = state.heatmap;
  const metric = $("heatmapMetric").value;
  const values = payload.cells.map((cell) => heatmapMetricValue(cell, metric)).filter((value) => value !== null && value !== undefined).map(Number);
  const signed = metric !== "要素单位成本";
  const magnitude = Math.max(...values.map((value) => Math.abs(value)), 1);
  const minimum = signed ? -magnitude : Math.min(...values, 0);
  const maximum = signed ? magnitude : Math.max(...values, 1);
  const metricUnit = metric === "环比变动率" ? "%" : "元/盒";
  $("heatmapNote").textContent = metric === "要素单位成本"
    ? "四个成本要素同时展开；颜色越深表示要素单位成本越高，不同产品基数需结合环比判断。"
    : metric === "一厂减二厂差异"
      ? "四个要素共享色标；绿色表示一厂低于二厂（有利），红色表示一厂高于二厂（不利）。"
      : "四个成本要素同时展开；红色上升、绿色下降、灰色无上月数据，⚠表示绝对环比超过10%。";

  const container = $("heatmapChart");
  const compact = container.clientWidth < 900;
  const layouts = compact
    ? payload.factors.map((_, index) => ({ left: "20%", width: "75%", top: `${6 + index * 23}%`, height: "15%", titleTop: `${1 + index * 23}%` }))
    : [
        { left: "8%", width: "39%", top: "8%", height: "27%", titleTop: "3%" },
        { left: "56%", width: "39%", top: "8%", height: "27%", titleTop: "3%" },
        { left: "8%", width: "39%", top: "51%", height: "27%", titleTop: "46%" },
        { left: "56%", width: "39%", top: "51%", height: "27%", titleTop: "46%" },
      ];
  const chart = chartFor("heatmapChart");
  chart.resize();
  chart.setOption({
    ...commonChartOption(),
    title: payload.factors.map((factor, index) => ({
      text: factor,
      left: layouts[index].left,
      top: layouts[index].titleTop,
      textStyle: { color: "#165c49", fontSize: compact ? 13 : 14, fontWeight: 700 },
    })),
    grid: layouts,
    tooltip: { ...commonChartOption().tooltip, trigger: "item", formatter: (params) => {
      const item = params.data.cell;
      const raw = params.data.raw;
      const metricText = raw === null || raw === undefined ? "暂无数据" : `${metric === "要素单位成本" ? formatMoney(raw) : formatSigned(raw)} ${metricUnit}`;
      return `${item.product} · ${item.month}<br>${item.factor}｜${metric}：<strong>${metricText}</strong><br>`
        + `一厂：${formatMoney(item.current)} 元/盒<br>上月：${formatMoney(item.previous)} 元/盒<br>`
        + `二厂：${formatMoney(item.benchmark)} 元/盒${item.alert ? "<br><strong>⚠ 超过10%告警阈值</strong>" : ""}`;
    } },
    xAxis: payload.factors.map((_, index) => ({
      type: "category", gridIndex: index, data: payload.months,
      axisTick: { show: false }, axisLine: { lineStyle: { color: "#cbd8d2" } },
      axisLabel: { fontSize: compact ? 9 : 10, interval: 0 },
    })),
    yAxis: payload.factors.map((_, index) => ({
      type: "category", gridIndex: index, data: payload.products,
      axisTick: { show: false }, axisLine: { lineStyle: { color: "#cbd8d2" } },
      axisLabel: { fontSize: compact ? 10 : 11 },
    })),
    visualMap: {
      min: minimum, max: maximum, seriesIndex: [0, 1, 2, 3], calculable: true, orient: "horizontal", left: "center", bottom: compact ? 4 : 12,
      precision: 2,
      text: metric === "环比变动率" ? ["成本上升", "成本下降"] : metric === "一厂减二厂差异" ? ["一厂较高", "一厂较低"] : ["较高", "较低"],
      textStyle: { fontSize: 11, color: "#66766f" },
      inRange: { color: signed ? ["#21835f", "#eaf2ee", "#f6f4ee", "#c24949"] : ["#edf4f1", "#8bb6a6", "#165c49"] },
    },
    series: payload.factors.map((factor, index) => ({
      name: factor, type: "heatmap", xAxisIndex: index, yAxisIndex: index,
      data: payload.cells.filter((cell) => cell.factor === factor).map((cell) => {
        const raw = heatmapMetricValue(cell, metric);
        return {
          value: [payload.months.indexOf(cell.month), payload.products.indexOf(cell.product), raw === null || raw === undefined ? 0 : Number(raw)],
          raw, cell,
          itemStyle: raw === null || raw === undefined ? { color: "#edf1ef", borderColor: "#fff", borderWidth: 3 } : { borderColor: "#fff", borderWidth: 3 },
        };
      }),
      label: { show: true, fontSize: 11, formatter: (params) => {
        const raw = params.data.raw;
        if (raw === null || raw === undefined) return "—";
        const text = metric === "环比变动率" ? `${formatSigned(raw)}%` : metric === "要素单位成本" ? formatMoney(raw) : formatSigned(raw);
        return `${params.data.cell.alert && metric === "环比变动率" ? "⚠ " : ""}${text}`;
      } },
      emphasis: { itemStyle: { shadowBlur: 10, shadowColor: "rgba(16,42,37,.28)" } },
    })),
  }, true);
}

function renderDriverContribution(items) {
  const rows = (items || []).slice(0, 6);
  const chart = chartFor("driverContributionChart");
  chart.setOption({
    ...commonChartOption(),
    grid: { left: 8, right: 62, top: 18, bottom: 34, containLabel: true },
    tooltip: {
      ...commonChartOption().tooltip,
      trigger: "axis",
      axisPointer: { type: "shadow" },
      formatter: (params) => {
        const index = params[0] && params[0].dataIndex;
        const item = rows[index];
        if (!item) return "暂无数据";
        return `${escapeHtml(item.name)}<br>本月：${formatMoney(item.current)} 元/盒<br>`
          + `环比变动：${formatSigned(item.delta)} 元/盒<br>`
          + `单位成本贡献度：<strong>${formatPct(item.contribution_pct)}</strong>`;
      },
    },
    xAxis: {
      type: "value",
      name: "贡献度（%）",
      nameLocation: "middle",
      nameGap: 27,
      axisLabel: { formatter: "{value}%", fontSize: 11 },
      splitLine: { lineStyle: { color: "#e6ece9" } },
    },
    yAxis: {
      type: "category",
      inverse: true,
      data: rows.map((item) => item.name),
      axisTick: { show: false },
      axisLine: { show: false },
      axisLabel: { color: "#3f554d", fontSize: 12, width: 112, overflow: "truncate" },
    },
    series: [{
      name: "单位成本贡献度",
      type: "bar",
      barMaxWidth: 22,
      data: rows.map((item) => {
        const value = item.contribution_pct === null || item.contribution_pct === undefined ? 0 : Number(item.contribution_pct);
        return {
          value,
          itemStyle: { color: value >= 0 ? "#21835f" : "#c18a2c", borderRadius: value >= 0 ? [0, 3, 3, 0] : [3, 0, 0, 3] },
          label: { position: value >= 0 ? "right" : "left" },
        };
      }),
      label: { show: true, color: "#33483f", fontSize: 12, fontWeight: 700, formatter: ({ value }) => `${Number(value).toFixed(2)}%` },
      markLine: { silent: true, symbol: "none", data: [{ xAxis: 0 }], lineStyle: { color: "#9db2aa", width: 1 }, label: { show: false } },
    }],
  }, true);
}

function renderTables(result) {
  $("driverRows").innerHTML = result.material_drivers.slice(0, 6).map((item) => `<tr>
    <td>${escapeHtml(item.name)}</td><td>${formatMoney(item.current)}</td>
    <td class="${directionClass(item.delta)}">${formatSigned(item.delta)}</td><td>${formatPct(item.contribution_pct)}</td>
  </tr>`).join("");
  const period = result.meta.period || result.meta.month;
  $("benchmarkProduct").textContent = result.meta.product;
  $("benchmarkFactoryPair").textContent = `${result.meta.factory} vs ${result.meta.benchmark_factory}`;
  $("benchmarkPeriod").textContent = period;
  $("benchmarkTargetHeader").textContent = `${result.meta.factory}（元/盒）`;
  $("benchmarkPeerHeader").textContent = `${result.meta.benchmark_factory}（元/盒）`;
  const benchmarkOrder = new Map([["单位成本", 0], ["直接材料", 1], ["直接人工", 2], ["制造费用", 3]]);
  const benchmarkRows = [...result.factory_benchmark]
    .sort((left, right) => (benchmarkOrder.get(left.name) ?? 99) - (benchmarkOrder.get(right.name) ?? 99));
  $("benchmarkRows").innerHTML = benchmarkRows.map((item, index) => {
    const available = item.status === "available" && item.difference !== null && item.difference !== undefined;
    const outcome = !available ? "暂无数据" : item.direction === "favorable" ? "有利" : item.direction === "unfavorable" ? "不利" : "持平";
    const tone = !available ? "neutral" : item.direction === "favorable" ? "good" : item.direction === "unfavorable" ? "bad" : "neutral";
    const productCell = index === 0
      ? `<td class="benchmark-product-cell" rowspan="${benchmarkRows.length}"><span>当前产品</span><strong>${escapeHtml(result.meta.product)}</strong><small>${escapeHtml(period)}</small></td>`
      : "";
    return `<tr class="benchmark-row benchmark-row-${tone}">
      ${productCell}
      <td class="benchmark-factor-cell"><strong>${escapeHtml(item.name)}</strong><small>同期同品</small></td>
      <td class="benchmark-value-cell">${formatMoney(item.target_value)}</td>
      <td class="benchmark-value-cell">${formatMoney(item.benchmark_value)}</td>
      <td class="benchmark-variance benchmark-variance-${tone}">${formatSigned(item.difference)}</td>
      <td class="benchmark-variance benchmark-variance-${tone}">${formatSigned(item.difference_rate_pct, "%")}</td>
      <td class="benchmark-outcome-cell"><span class="outcome-pill outcome-${tone}">${outcome}</span></td>
    </tr>`;
  }).join("");
  renderMaterialNarrative(result.narratives["材料成本归因分析文本"]);
  $("alertBadge").textContent = result.alerts.length ? `${result.alerts.length} 项告警` : "无阈值告警";
  $("alertBadge").className = result.alerts.length ? "badge" : "badge neutral";
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
}

function renderEvidence(result) {
  const labels = {
    recipe_citation: "产品配方",
    process_citation: "生产工艺",
    gmp_citation: "GMP 规范（原文）",
    industry_citation: "行业基准",
    market_citation: "药材市场行情",
    factory_benchmark_citation: "同集团工厂对标",
    equipment_citation: "设备记录",
    anomaly_history_citation: "历史异常处置",
  };
  $("evidenceGrid").innerHTML = Object.entries(result.evidence)
    .filter(([, value]) => value)
    .map(([key, value]) => `<article class="evidence-item" data-evidence-key="${escapeHtml(key)}"><span>${labels[key] || key}</span><p>${escapeHtml(value)}</p></article>`)
    .join("");
  $("sourceList").innerHTML = result.sources.map((source) => `<li>${escapeHtml(source.kind)}：${escapeHtml(source.path)}${source.key ? `（${escapeHtml(source.key)}）` : ""}</li>`).join("");
}

async function render(result) {
  state.result = result;
  $("dashboard").hidden = false;
  syncActiveNavigation();
  renderHeader(result);
  renderComparisons(result);
  renderTables(result);
  renderEvidence(result);
  $("workflowPanel").hidden = false;
  resetWorkflowPanel();

  // ECharts must read the container after the previously hidden dashboard has laid out.
  await new Promise((resolve) => requestAnimationFrame(resolve));
  renderTrend(result.trend);
  renderContribution(result.total_cost_contributions);
  renderWaterfall(result.waterfall);
  renderTotalCostBridge(result.total_cost_bridge);
  renderStructure(result.cost_structure);
  renderDriverContribution(result.material_drivers);
  renderBenchmarkTree(result);
  $("benchmarkNarrativeMode").textContent = "确定性分析 · RPA候选建议";
  renderBenchmarkAttribution(result);
  renderHeatmap();
  await new Promise((resolve) => requestAnimationFrame(resolve));
  Object.values(state.charts).forEach((chart) => chart.resize());
}

async function analyze() {
  setLoading(true);
  state.result = null;
  state.report = null;
  state.workflow = null;
  $("reportPanel").hidden = true;
  try {
    const analysisType = $("analysisType").value;
    const payload = { analysis_type: analysisType, product: $("productSelect").value };
    if (analysisType === "季度成本分析") payload.quarter = $("quarterSelect").value;
    else payload.month = $("monthSelect").value;
    if (analysisType === "专题分析") payload.topic = $("topicSelect").value;
    const result = await requestJson("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    await render(result);
    $("reportButton").disabled = false;
    $("reportButton").title = "";
  } catch (error) {
    showError(error.message);
  } finally {
    setLoading(false);
  }
}

function renderReportArtifacts(report) {
  state.report = report;
  state.workflow = null;
  $("reportStatus").textContent = `${report.period || report.month} · ${report.product} · ${report.report_number}`;
  const generation = report.generation;
  $("reportGeneration").textContent = generation.status === "generated"
    ? `大模型受控生成 · ${formatModelName(generation.model)}`
    : generation.status === "fallback"
      ? `确定性文本 · 已安全降级：${generation.warnings.join("；")}`
      : "确定性文本 · 未调用外部模型";
  $("previewLink").href = report.preview_url;
  $("pdfLink").href = report.downloads.pdf;
  $("wordLink").href = report.downloads.docx;
  applyReportGeneratedContent(report);
  $("reportPanel").hidden = false;
  resetWorkflowPanel();
  $("workflowPanel").hidden = false;
  $("reportPanel").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function resetWorkflowPanel() {
  $("approvalForm").reset();
  $("workflowEmpty").hidden = false;
  $("candidateCard").hidden = true;
  $("approvalForm").hidden = false;
  $("approvedCard").hidden = true;
  $("submissionAction").hidden = true;
  $("receiptCard").hidden = true;
  const reportReady = Boolean(state.report && state.report.workflow_supported !== false);
  const periodReport = Boolean(state.report && state.report.workflow_supported === false);
  $("workflowEmptyTitle").textContent = reportReady ? "从已验证报告形成整改任务" : periodReport ? "本报告暂不进入整改闭环" : "整改闭环尚未开始";
  $("workflowEmptyMessage").textContent = reportReady
    ? "finding、建议、来源和幂等键均来自当前报告，不在浏览器端重新生成。"
    : periodReport ? "季度/专题报告已完成独立导出；当前整改候选契约仍只接受107字段月度报告，避免错误套用月度任务口径。" : "请先运行分析并生成报告；报告通过校验后，才能形成可追溯整改候选。";
  $("candidateButton").disabled = !reportReady;
  $("candidateButton").textContent = reportReady ? "形成整改候选" : periodReport ? "该类型暂不支持闭环" : "请先生成报告";
  $("workflowState").textContent = reportReady ? "等待形成候选" : "等待生成报告";
  const completed = { analysis: Boolean(state.result), report: reportReady };
  document.querySelectorAll(".workflow-steps li").forEach((item) => {
    item.classList.toggle("complete", Boolean(completed[item.dataset.step]));
  });
  const deadline = new Date();
  deadline.setDate(deadline.getDate() + 7);
  $("deadlineInput").value = deadline.toISOString().slice(0, 10);
  $("submitConfirm").checked = false;
}

function markWorkflowSteps(workflow) {
  const completed = {
    analysis: true,
    report: true,
    candidate: Boolean(workflow),
    approve: Boolean(workflow && workflow.review),
    submit: ["sent", "duplicate_local", "duplicate_remote"].includes(workflow && workflow.state),
  };
  document.querySelectorAll(".workflow-steps li").forEach((item) => {
    item.classList.toggle("complete", Boolean(completed[item.dataset.step]));
  });
}

function renderWorkflow(workflow) {
  state.workflow = workflow;
  const candidate = workflow.candidate;
  $("workflowEmpty").hidden = true;
  $("candidateCard").hidden = false;
  $("candidateTaskId").textContent = candidate.task_id;
  $("candidatePriority").textContent = ({ high: "高", medium: "中", low: "低" })[candidate.suggested_priority] || candidate.suggested_priority;
  const taskGeneration = workflow.generation || {};
  $("candidateValidation").textContent = taskGeneration.status === "generated"
    ? `PASS · 大模型受控候选 · ${formatModelName(taskGeneration.model)}`
    : taskGeneration.status === "fallback"
      ? "PASS · 确定性回退候选"
      : "PASS · 确定性候选";
  $("candidateValidation").title = taskGeneration.warnings && taskGeneration.warnings.length
    ? taskGeneration.warnings.join("；")
    : "候选仍需人工审批，不会自动提交RPA";
  $("candidateTitle").textContent = candidate.task_title;
  $("candidateFinding").textContent = candidate.finding;
  $("candidateSuggestion").textContent = candidate.suggestion;
  const sources = $("candidateSources");
  sources.replaceChildren(...candidate.source_refs.map((value) => {
    const item = document.createElement("li");
    item.textContent = value;
    return item;
  }));
  $("priorityInput").value = candidate.suggested_priority;
  if (!$("departmentInput").value && candidate.suggested_department) {
    $("departmentInput").value = candidate.suggested_department;
  }

  const approved = Boolean(workflow.review);
  $("approvalForm").hidden = approved;
  $("approvedCard").hidden = !approved;
  if (approved) {
    $("approvedBy").textContent = workflow.review.reviewer;
    $("approvedAt").textContent = workflow.review.decided_at;
    $("approvedAssignee").textContent = workflow.payload.assignee.name + "（" + workflow.payload.assignee.department + "）";
    $("approvedDeadline").textContent = "截止 " + workflow.payload.deadline + " · " + workflow.payload.notify_method;
  }

  const submission = workflow.submission;
  $("submissionAction").hidden = !approved || Boolean(submission && submission.state !== "failed");
  $("receiptCard").hidden = !submission;
  if (submission) {
    const succeeded = ["sent", "duplicate_local", "duplicate_remote"].includes(submission.state);
    $("receiptCard").classList.toggle("failed", !succeeded);
    $("receiptState").textContent = succeeded ? "模拟RPA回执已记录" : "模拟RPA提交失败，可在服务启动后重试";
    $("receiptMessage").textContent = submission.message;
    const notify = submission.response_payload && submission.response_payload.data
      ? submission.response_payload.data.notify_status
      : null;
    $("receiptNotify").textContent = notify && (notify.wechat || notify.email || notify.sms)
      ? (notify.wechat || notify.email || notify.sms)
      : "HTTP " + (submission.http_status ?? "未连接") + " · 尝试 " + submission.attempt_count + " 次";
    $("submitButton").textContent = submission.state === "failed" ? "重新发送至模拟 RPA" : "发送至模拟 RPA";
  }
  $("workflowState").textContent = ({
    pending_review: "候选待人工确认",
    approved: "人工确认完成",
    sent: "模拟任务已发送",
    duplicate_local: "任务已发送（幂等拦截）",
    duplicate_remote: "模拟服务已有该任务",
    failed: "模拟服务暂不可用",
  })[workflow.state] || workflow.state;
  markWorkflowSteps(workflow);
}

async function createCandidate() {
  if (!state.report) {
    showError("请先完成分析并生成报告，再形成整改候选");
    return;
  }
  const button = $("candidateButton");
  button.disabled = true;
  button.textContent = "正在形成…";
  try {
    const workflow = await requestJson("/api/workflows", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        report_id: state.report.report_id,
        use_llm: Boolean(state.report.generation && state.report.generation.status !== "not_requested"),
      }),
    });
    renderWorkflow(workflow);
    await refreshTrackingStats();
  } catch (error) {
    showError(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "形成整改候选";
  }
}

async function approveCandidate(event) {
  event.preventDefault();
  if (!state.workflow || !$("approvalConfirm").checked) {
    showError("请先核对任务内容并勾选人工确认");
    return;
  }
  const button = $("approveButton");
  button.disabled = true;
  try {
    const form = new FormData(event.currentTarget);
    const payload = Object.fromEntries(form.entries());
    payload.role = payload.role || null;
    payload.comment = payload.comment || null;
    payload.candidate_id = state.workflow.candidate.candidate_id;
    payload.confirmation = "CONFIRM";
    const workflow = await requestJson("/api/workflows/" + encodeURIComponent(state.report.report_id) + "/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    renderWorkflow(workflow);
    await refreshTrackingStats();
  } catch (error) {
    showError(error.message);
  } finally {
    button.disabled = false;
  }
}

async function submitCandidate() {
  if (!state.workflow || !$("submitConfirm").checked) {
    showError("请先确认仅执行本机模拟RPA提交");
    return;
  }
  const button = $("submitButton");
  button.disabled = true;
  button.textContent = "正在发送…";
  try {
    const workflow = await requestJson("/api/workflows/" + encodeURIComponent(state.report.report_id) + "/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirmation: "SUBMIT" }),
    });
    renderWorkflow(workflow);
    await refreshTrackingStats();
  } catch (error) {
    showError(error.message);
  } finally {
    button.disabled = false;
    if (!state.workflow || !state.workflow.submission || state.workflow.submission.state === "failed") {
      button.textContent = "重新发送至模拟 RPA";
    }
  }
}

async function generateReport() {
  if (!state.result) return;
  const button = $("reportButton");
  button.disabled = true;
  button.querySelector("span:first-child").textContent = "正在生成…";
  const progress = $("reportProgress");
  const progressBar = $("reportProgressBar");
  const progressFill = $("reportProgressFill");
  const progressStage = $("reportProgressStage");
  const progressTitle = $("reportProgressTitle");
  const stages = [
    [12, "校验结构化数据"],
    [34, "检索知识证据"],
    [56, "生成受控叙述"],
    [76, "排版Word报告"],
    [90, "渲染并校验PDF"],
  ];
  let stageIndex = 0;
  const setProgress = (value, label) => {
    progressFill.style.width = `${value}%`;
    progressBar.setAttribute("aria-valuenow", String(value));
    progressBar.setAttribute("aria-valuetext", label);
    progressStage.textContent = label;
  };
  progress.hidden = false;
  progress.classList.remove("failed");
  progressTitle.textContent = "正在生成报告";
  setProgress(...stages[stageIndex]);
  const timer = window.setInterval(() => {
    if (stageIndex < stages.length - 1) stageIndex += 1;
    setProgress(...stages[stageIndex]);
  }, 6500);
  try {
    const report = await requestJson("/api/reports", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        analysis_type: state.result.meta.analysis_type,
        product: state.result.meta.product,
        month: state.result.meta.analysis_type === "季度成本分析" ? null : state.result.meta.month,
        quarter: state.result.meta.analysis_type === "季度成本分析" ? state.result.meta.period : null,
        topic: state.result.meta.topic || null,
        use_llm: $("useLlmToggle").checked,
      }),
    });
    window.clearInterval(timer);
    setProgress(100, "报告生成完成");
    progressTitle.textContent = "报告已生成";
    renderReportArtifacts(report);
  } catch (error) {
    window.clearInterval(timer);
    progress.classList.add("failed");
    progressTitle.textContent = "报告生成失败";
    progressStage.textContent = "请检查提示后重试";
    progressBar.setAttribute("aria-valuetext", "生成失败");
    showError(error.message);
  } finally {
    button.disabled = false;
    button.querySelector("span:first-child").textContent = "生成报告";
  }
}

async function initialize() {
  try {
    const options = await requestJson("/api/options");
    state.options = options;
    setSelectOptions($("analysisType"), options.analysis_types);
    setSelectOptions($("productSelect"), options.products);
    setSelectOptions($("monthSelect"), options.months);
    setSelectOptions($("quarterSelect"), options.quarters);
    setSelectOptions($("topicSelect"), options.topics);
    state.heatmap = await requestJson("/api/heatmap");
    setSelectOptions($("heatmapMetric"), state.heatmap.metrics);
    $("heatmapMetric").value = "环比变动率";
    $("productSelect").value = options.products.includes("银黄口服液") ? "银黄口服液" : options.products[0];
    $("monthSelect").value = options.months.at(-1);
    $("quarterSelect").value = options.quarters.at(-1);
    const llm = options.llm;
    $("useLlmToggle").disabled = !llm.ready;
    $("useLlmToggle").checked = false;
    $("llmStatus").textContent = llm.ready ? `可用 · ${formatModelName(llm.model)}` : `未启用 · ${llm.issue}`;
    await refreshTrackingStats();
    await analyze();
  } catch (error) {
    showError(error.message);
  }
}

function updateAnalysisControls() {
  const analysisType = $("analysisType").value;
  $("monthControl").hidden = analysisType === "季度成本分析";
  $("quarterControl").hidden = analysisType !== "季度成本分析";
  $("topicControl").hidden = analysisType !== "专题分析";
  $("pageTitle").textContent = analysisType;
  const llmReady = Boolean(state.options && state.options.llm.ready);
  $("useLlmToggle").disabled = !llmReady;
  $("llmStatus").textContent = llmReady
    ? `月度/季度/专题可用 · ${formatModelName(state.options.llm.model)}`
    : `未启用 · ${state.options.llm.issue}`;
  state.result = null;
  state.report = null;
  state.workflow = null;
  $("reportPanel").hidden = true;
  resetWorkflowPanel();
  $("reportButton").disabled = true;
}

$("analyzeButton").addEventListener("click", analyze);
$("reportButton").addEventListener("click", generateReport);
$("candidateButton").addEventListener("click", createCandidate);
$("approvalForm").addEventListener("submit", approveCandidate);
$("submitButton").addEventListener("click", submitCandidate);
["productSelect", "monthSelect", "quarterSelect", "topicSelect"].forEach((id) => $(id).addEventListener("change", () => {
  state.result = null;
  state.report = null;
  state.workflow = null;
  $("reportPanel").hidden = true;
  resetWorkflowPanel();
  $("reportButton").disabled = true;
}));
$("analysisType").addEventListener("change", updateAnalysisControls);
$("heatmapMetric").addEventListener("change", renderHeatmap);
window.addEventListener("resize", () => Object.values(state.charts).forEach((chart) => chart.resize()));
initializeNavigation();
initialize();

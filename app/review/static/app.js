const state = { token: sessionStorage.getItem("reviewToken") || "", authMode: "local_token", principal: null, testSubmitEnabled: false, candidates: [], selectedId: null, detail: null };

const el = (id) => document.getElementById(id);
const authOverlay = el("authOverlay");
const toast = el("toast");

function notify(message, isError = false) {
  toast.textContent = message;
  toast.className = `toast show${isError ? " error" : ""}`;
  window.setTimeout(() => { toast.className = "toast"; }, 2800);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", "X-Review-Token": state.token, ...(options.headers || {}) },
  });
  let payload = null;
  try { payload = await response.json(); } catch (_) { payload = { detail: "响应不是JSON" }; }
  if (!response.ok) {
    if (response.status === 401) authOverlay.classList.remove("hidden");
    throw new Error(payload.detail || `请求失败 ${response.status}`);
  }
  return payload;
}

function setMetric(id, value) { el(id).textContent = String(value); }
function stateLabel(value) {
  return ({ pending_review: "待审核", approved: "已批准", rejected: "已拒绝", not_submitted: "未提交", dry_run: "已预演", sent: "已提交", failed: "失败", idle: "未执行", running: "执行中", succeeded: "回执已核验", reconcile_required: "待对账" })[value] || value;
}

async function loadDashboard() {
  const [principal, dashboard, candidates] = await Promise.all([api("/api/me"), api("/api/dashboard"), api("/api/candidates")]);
  state.principal = principal;
  state.testSubmitEnabled = dashboard.test_submit_enabled;
  el("userIdentity").textContent = `${principal.display_name} · ${principal.role}`;
  el("testSubmitPill").classList.toggle("hidden", !dashboard.test_submit_enabled);
  state.candidates = candidates;
  setMetric("metricTotal", dashboard.total);
  setMetric("metricPending", dashboard.pending_review);
  setMetric("metricApproved", dashboard.approved);
  setMetric("metricRejected", dashboard.rejected);
  setMetric("metricDryRun", dashboard.dry_run);
  setMetric("metricReconcile", dashboard.reconcile_required);
  const auditBadge = el("auditBadge");
  auditBadge.textContent = dashboard.audit.status === "PASS" ? `审计链通过 · ${dashboard.audit.checked_events}` : "审计链异常";
  auditBadge.className = `audit-badge${dashboard.audit.status === "PASS" ? " pass" : ""}`;
  renderQueue();
  if (!state.selectedId && candidates.length) state.selectedId = candidates[0].candidate.candidate_id;
  if (state.selectedId) await loadDetail(state.selectedId);
}

function renderQueue() {
  const list = el("candidateList");
  list.replaceChildren();
  if (!state.candidates.length) {
    const empty = document.createElement("p"); empty.textContent = "暂无任务候选"; list.append(empty); return;
  }
  state.candidates.forEach((item) => {
    const candidate = item.candidate;
    const button = document.createElement("button");
    button.type = "button";
    button.className = `candidate-item${candidate.candidate_id === state.selectedId ? " active" : ""}`;
    const meta = document.createElement("span"); meta.className = "eyebrow"; meta.textContent = `${candidate.analysis_month} · ${candidate.product}`;
    const title = document.createElement("strong"); title.textContent = candidate.task_title;
    const small = document.createElement("small");
    const status = document.createElement("span"); status.textContent = stateLabel(item.review_state);
    const task = document.createElement("span"); task.textContent = candidate.task_id;
    small.append(status, task); button.append(meta, title, small);
    button.addEventListener("click", async () => { state.selectedId = candidate.candidate_id; renderQueue(); await loadDetail(state.selectedId); });
    list.append(button);
  });
}

function addTextRow(container, label, value) {
  const line = document.createElement("div");
  const strong = document.createElement("strong"); strong.textContent = `${label}：`;
  line.append(strong, document.createTextNode(value || "—")); container.append(line);
}

function renderDetail(detail) {
  state.detail = detail;
  const candidate = detail.candidate;
  el("emptyMessage").classList.add("hidden"); el("candidateDetail").classList.remove("hidden");
  el("detailMeta").textContent = `${candidate.candidate_id} · 版本 ${detail.version}`;
  el("detailTitle").textContent = candidate.task_title;
  el("findingText").textContent = candidate.finding;
  el("suggestionText").textContent = candidate.suggestion;
  el("reportNumber").textContent = candidate.report_number;
  el("analysisMonth").textContent = candidate.analysis_month;
  el("productName").textContent = candidate.product;
  el("suggestedPriority").textContent = candidate.suggested_priority;
  const reviewBadge = el("reviewState"); reviewBadge.textContent = stateLabel(detail.review_state); reviewBadge.className = `state-badge ${detail.review_state}`;
  el("submissionState").textContent = stateLabel(detail.submission_state);
  el("executionState").textContent = stateLabel(detail.execution_state);
  el("sourceCount").textContent = `${candidate.source_refs.length} 个原始文件`;
  const sources = el("sourceList"); sources.replaceChildren();
  candidate.source_refs.forEach((source) => { const li = document.createElement("li"); li.textContent = source; sources.append(li); });

  const permissions = new Set(state.principal?.permissions || []);
  const isPending = detail.review_state === "pending_review";
  el("dryRunButton").classList.add("hidden");
  el("authorizeButton").classList.add("hidden");
  el("executeTestButton").classList.add("hidden");
  el("reviewForms").classList.toggle("hidden", !isPending || !permissions.has("review"));
  el("reviewResult").classList.toggle("hidden", isPending);
  if (!isPending && detail.review) {
    el("reviewTime").textContent = detail.review.decided_at;
    const summary = el("reviewSummary"); summary.replaceChildren();
    addTextRow(summary, "决定", stateLabel(detail.review.decision));
    addTextRow(summary, "审批人", detail.review.reviewer);
    addTextRow(summary, "备注", detail.review.comment);
    if (detail.review.reviewed_task.payload) {
      const payload = detail.review.reviewed_task.payload;
      addTextRow(summary, "责任人", `${payload.assignee.name}（${payload.assignee.department}）`);
      addTextRow(summary, "截止日期", payload.deadline);
      addTextRow(summary, "通知方式", payload.notify_method);
    }
    el("dryRunButton").classList.toggle("hidden", detail.review_state !== "approved" || !permissions.has("dry_run"));
    const authorization = detail.submission_authorization;
    el("authorizeButton").classList.toggle("hidden", detail.review_state !== "approved" || Boolean(authorization) || !permissions.has("authorize"));
    el("authorizationStatus").textContent = authorization
      ? `提交已由 ${authorization.authorizer_name} 于 ${authorization.authorized_at} 授权`
      : "尚未完成第二人提交授权；外部提交仍关闭。";
    const job = detail.execution_job;
    el("executionStatus").textContent = job
      ? `测试执行：${stateLabel(job.state)}；方式 ${job.operation}；目标 ${job.endpoint_origin}`
      : "尚未执行测试环境提交。";
    const isAuthorizer = authorization?.authorizer_user_id === state.principal?.user_id;
    const canExecuteTest = state.testSubmitEnabled && permissions.has("execute_test") && isAuthorizer && detail.execution_state !== "succeeded" && detail.execution_state !== "running";
    const executeButton = el("executeTestButton");
    executeButton.classList.toggle("hidden", !canExecuteTest);
    executeButton.textContent = detail.execution_state === "reconcile_required" ? "继续远端对账（不重复POST）" : "提交到本机测试RPA并核验回执";
  }
  const auditBody = el("auditTable"); auditBody.replaceChildren();
  detail.audit_events.forEach((event) => {
    const row = document.createElement("tr");
    [event.sequence_no, event.event_type, event.actor, event.occurred_at, `${event.event_hash.slice(0, 16)}…`].forEach((value) => {
      const cell = document.createElement("td"); cell.textContent = String(value); row.append(cell);
    });
    auditBody.append(row);
  });
}

async function loadDetail(candidateId) { renderDetail(await api(`/api/candidates/${encodeURIComponent(candidateId)}`)); }

function formPayload(form) { return Object.fromEntries(new FormData(form).entries()); }

el("authForm").addEventListener("submit", async (event) => {
  event.preventDefault(); state.token = el("tokenInput").value; el("authError").textContent = "";
  try { await loadDashboard(); sessionStorage.setItem("reviewToken", state.token); authOverlay.classList.add("hidden"); }
  catch (error) { el("authError").textContent = error.message; }
});

el("approvalForm").addEventListener("submit", async (event) => {
  event.preventDefault(); const payload = formPayload(event.currentTarget); payload.expected_version = state.detail.version;
  if (!payload.role) payload.role = null; if (!payload.comment) payload.comment = null;
  try {
    const detail = await api(`/api/candidates/${encodeURIComponent(state.selectedId)}/approve`, { method: "POST", body: JSON.stringify(payload) });
    renderDetail(detail); await loadDashboard(); notify("任务已批准并写入审计库，尚未外发");
  } catch (error) { notify(error.message, true); }
});

el("rejectionForm").addEventListener("submit", async (event) => {
  event.preventDefault(); const payload = formPayload(event.currentTarget); payload.expected_version = state.detail.version;
  try {
    const detail = await api(`/api/candidates/${encodeURIComponent(state.selectedId)}/reject`, { method: "POST", body: JSON.stringify(payload) });
    renderDetail(detail); await loadDashboard(); notify("候选已拒绝并记录原因");
  } catch (error) { notify(error.message, true); }
});

el("dryRunButton").addEventListener("click", async () => {
  if (!window.confirm("确认执行安全预演？该操作不会调用外部RPA接口。")) return;
  try {
    const detail = await api(`/api/candidates/${encodeURIComponent(state.selectedId)}/dry-run`, { method: "POST", body: JSON.stringify({ expected_version: state.detail.version }) });
    renderDetail(detail); await loadDashboard(); notify("dry-run完成，未调用外部接口");
  } catch (error) { notify(error.message, true); }
});

el("authorizeButton").addEventListener("click", async () => {
  if (!window.confirm("确认以第二人身份授权后续提交？本系统当前仍不会调用外部接口。")) return;
  try {
    const detail = await api(`/api/candidates/${encodeURIComponent(state.selectedId)}/authorize-submission`, { method: "POST", body: JSON.stringify({ expected_version: state.detail.version, comment: null }) });
    renderDetail(detail); await loadDashboard(); notify("第二人授权已写入审计链；外部提交仍关闭");
  } catch (error) { notify(error.message, true); }
});

el("executeTestButton").addEventListener("click", async () => {
  if (!window.confirm("确认提交到本机测试RPA？系统会执行回执反查；生产地址仍被禁止。")) return;
  try {
    const detail = await api(`/api/candidates/${encodeURIComponent(state.selectedId)}/execute-test`, { method: "POST", body: JSON.stringify({ expected_version: state.detail.version, confirmation: "TEST" }) });
    renderDetail(detail); await loadDashboard(); notify("测试提交已完成，远端回执已写入审计链");
  } catch (error) { notify(error.message, true); }
});

el("refreshButton").addEventListener("click", async () => { try { await loadDashboard(); notify("数据已刷新"); } catch (error) { notify(error.message, true); } });
el("lockButton").addEventListener("click", () => { state.token = ""; state.principal = null; sessionStorage.removeItem("reviewToken"); el("tokenInput").value = ""; el("userIdentity").textContent = "未连接"; authOverlay.classList.remove("hidden"); });

async function bootstrapAuthentication() {
  try {
    const response = await fetch("/health", { headers: { "Accept": "application/json" } });
    const health = await response.json();
    state.authMode = health.auth_mode || "local_token";
    if (state.authMode === "signed_proxy") {
      el("authDescription").textContent = "正在通过企业SSO身份网关建立受控会话。";
      el("tokenLabel").classList.add("hidden");
      el("connectButton").classList.add("hidden");
      el("lockButton").classList.add("hidden");
      await loadDashboard();
      authOverlay.classList.add("hidden");
      return;
    }
    if (state.token) {
      await loadDashboard();
      authOverlay.classList.add("hidden");
    }
  } catch (error) {
    el("authError").textContent = error.message;
    authOverlay.classList.remove("hidden");
  }
}

bootstrapAuthentication();

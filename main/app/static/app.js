// 前端状态和页面渲染逻辑都集中在这个文件里。
const $ = (id) => document.getElementById(id);

const state = {
  families: [],
  profiles: [],
  reports: [],
  tasks: [],
  logs: [],
  auditLogs: [],
  todayPriorities: [],
  workbenchOverview: {},
  serviceQuality: {},
  devices: [],
  opsHealth: {},
  backups: [],
  retention: {},
  backupDrills: {},
  arkConfig: {},
  importTemplates: [],
  agentEval: {},
  templates: [],
  outputs: [],
  accounts: [],
  conversations: [],
  chatMessages: [],
  currentUser: JSON.parse(localStorage.getItem("chatUser") || "null"),
  selectedCoachName: localStorage.getItem("coachFilter") || "",
  selectedChatFamilyId: "",
  selectedFamilyId: "",
};

// 把后端返回的 Agent 类型映射成前端展示名称和颜色。
const AGENTS = {
  family_profile: { name: "家庭画像", className: "agent-profile" },
  weekly_report: { name: "AI周报", className: "agent-weekly" },
  ai_reply: { name: "AI回复", className: "agent-reply" },
  checkin_pbl: { name: "打卡/PBL", className: "agent-checkin" },
};

// 统一封装 fetch，减少重复的错误处理代码。
async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (!headers.has("X-Actor")) headers.set("X-Actor", encodeURIComponent(currentActor()));
  if (state.currentUser?.admin_token && !headers.has("Authorization")) headers.set("Authorization", `Bearer ${state.currentUser.admin_token}`);
  const res = await fetch(path, { ...options, headers });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function currentActor() {
  if (!state.currentUser) return "控制端";
  return `${state.currentUser.role}:${state.currentUser.display_name || state.currentUser.username}`;
}

function userRoleBadge(user) {
  const labels = { admin: "管理员", coach: "陪跑师", readonly: "只读", parent: "家长" };
  const kind = user.role === "admin" ? "danger" : user.role === "coach" ? "ok" : user.role === "readonly" ? "warn" : "";
  return badge(labels[user.role] || user.role, kind);
}

// 把普通文本转成安全的 HTML 字符串，防止页面插入未转义内容。
function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

// 页面顶部的短消息提醒。
function toast(message) {
  const el = $("toast");
  el.textContent = message;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2600);
}

// 顶部调试状态：所有异步按钮都会在这里显示加载、完成或失败。
function setActionStatus(message, kind = "") {
  const el = $("actionStatus");
  if (!el) return;
  el.textContent = message || "";
  el.className = kind;
}

async function withAction(label, fn) {
  const started = Date.now();
  document.body.classList.add("busy");
  setActionStatus(`${label}...`, "loading");
  console.debug(`[action:start] ${label}`);
  try {
    const result = await fn();
    const ms = Date.now() - started;
    setActionStatus(`${label}完成 · ${ms}ms`, "ok");
    console.debug(`[action:done] ${label}`, { ms, result });
    return result;
  } catch (err) {
    const message = err?.message || String(err);
    setActionStatus(`${label}失败：${message}`, "error");
    if (label === "刷新数据" && isInitialDataEmpty()) renderGlobalError(message);
    toast(`${label}失败：${message}`);
    console.error(`[action:error] ${label}`, err);
    return null;
  } finally {
    document.body.classList.remove("busy");
  }
}

// 渲染一个小标签，统一状态样式。
function badge(text, kind = "") {
  return `<span class="badge ${kind}">${esc(text)}</span>`;
}

function displayValue(value, fallback = "未登记") {
  if (value === 0) return "0";
  const text = String(value ?? "").trim();
  return text || fallback;
}

function stageProfile(family) {
  const items = [
    ["年级", family.child_grade],
    ["课程阶段", family.course_stage],
    ["Unit 进度", family.unit_progress],
    ["PBL 次数", family.pbl_count],
    ["打卡完成率", family.checkin_rate],
    ["下一里程碑", family.next_milestone, "wide"],
  ];
  return `
    <div class="stage-grid">
      ${items.map(([label, value, wide]) => `
        <div class="stage-card ${wide || ""}">
          <span>${esc(label)}</span>
          <strong>${esc(displayValue(value))}</strong>
        </div>
      `).join("")}
    </div>
  `;
}

function followupRecords(records = []) {
  return records.length ? records.slice(0, 8).map((item) => `
    <article class="followup-card followup-${esc(item.status)}">
      <div class="timeline-head">
        ${badge(item.followup_type || "跟进", item.status === "已完成" ? "ok" : item.status === "需升级" ? "danger" : "warn")}
        <strong>${esc(item.owner || item.created_by || "未分配")}</strong>
        <time>${esc(item.occurred_at || "")}</time>
      </div>
      <p>${esc(item.content)}</p>
      ${item.result ? `<p class="muted">结果：${esc(item.result)}</p>` : ""}
      ${item.next_action ? `<p class="muted">下一步：${esc(item.next_action)}</p>` : ""}
    </article>
  `).join("") : emptyState("暂无跟进记录", "电话、私信、群提醒、补课、投诉和续报沟通会沉淀在这里。");
}

function statePanel(kind, title, detail = "", actionHtml = "") {
  const labels = { empty: "空状态", loading: "加载中", error: "错误", risk: "风险" };
  return `
    <div class="state-card state-${kind}">
      <span class="state-kicker">${esc(labels[kind] || "状态")}</span>
      <strong>${esc(title)}</strong>
      ${detail ? `<p>${esc(detail)}</p>` : ""}
      ${actionHtml ? `<div class="actions left">${actionHtml}</div>` : ""}
    </div>
  `;
}

function emptyState(title = "暂无数据", detail = "当前没有可展示内容。", actionHtml = "") {
  return statePanel("empty", title, detail, actionHtml);
}

function loadingState(title = "正在加载", detail = "正在连接控制端并同步最新数据。") {
  return statePanel("loading", title, detail);
}

function errorState(title = "加载失败", detail = "请检查服务状态后重试。", actionHtml = '<button onclick="refreshAll()">重试</button>') {
  return statePanel("error", title, detail, actionHtml);
}

function riskState(title, detail, actionHtml = "") {
  return statePanel("risk", title, detail, actionHtml);
}

function sendModeBadge(mode) {
  if (mode === "real_send") return badge("真实发送", "warn");
  if (mode === "dry_run" || !mode) return badge("试运行", "ok");
  return badge(`未知模式：${mode}`);
}

function reportSendStatusBadge(status) {
  const labels = {
    not_created: "未建任务",
    task_created: "已建任务",
    pending: "待发送",
    assigned: "发送中",
    sent: "已发送",
    failed: "发送失败",
    dry_run: "试运行完成",
    skipped: "已跳过",
    cancelled: "已取消",
  };
  const kind = status === "sent" || status === "dry_run" ? "ok" : (status === "failed" || status === "assigned" ? "warn" : "");
  return badge(labels[status || "not_created"] || status || "未建任务", kind);
}

function sendReasonCell(log) {
  const level = log.send_reason_level || (log.status === "failed" ? "danger" : "");
  const trace = Array.isArray(log.send_trace) && log.send_trace.length
    ? `<p class="muted">${esc(log.send_trace.join(" / "))}</p>`
    : "";
  return `${badge(log.send_stage || "发送结果", level)}<strong>${esc(log.send_reason_label || "未分类")}</strong>${trace}`;
}

function taskAllowedOperations(task) {
  if (Array.isArray(task.allowed_operations)) return task.allowed_operations;
  if (state.currentUser?.role === "readonly") return ["view"];
  return ["view", "edit", "review", "assign_device", "dry_run", "web_send", "cancel", "confirm_real_send"];
}

function taskCan(task, operation) {
  return taskAllowedOperations(task).includes(operation);
}

function taskOperationBadges(task) {
  const labels = task.operation_labels || {};
  const ops = taskAllowedOperations(task);
  const warnings = Array.isArray(task.operation_warnings) ? task.operation_warnings : [];
  const chips = ops.map((op) => badge(labels[op] || op, op === "confirm_real_send" ? "danger" : (op === "dry_run" ? "ok" : ""))).join("");
  const warningText = warnings.length ? `<p class="muted">${esc(warnings.join(" "))}</p>` : "";
  return `<div class="op-layer"><strong>${esc(task.workflow_stage || "待处理")}</strong><div>${chips || badge("仅查看")}</div>${warningText}</div>`;
}

function sendModeSelect(task) {
  const mode = task.send_mode || "dry_run";
  const canEdit = taskCan(task, "edit") || taskCan(task, "confirm_real_send");
  const canRealSend = taskCan(task, "confirm_real_send") || mode === "real_send";
  return `
    <select id="task-mode-${task.id}" ${canEdit ? "" : "disabled"}>
      <option value="dry_run" ${mode === "dry_run" ? "selected" : ""}>试运行</option>
      <option value="real_send" ${mode === "real_send" ? "selected" : ""} ${canRealSend ? "" : "disabled"}>真实发送</option>
    </select>
    ${sendModeBadge(mode)}
  `;
}

function deviceSelect(task) {
  const current = task.device_id || "";
  const disabled = taskCan(task, "assign_device") ? "" : "disabled";
  const options = ['<option value="">自动领取</option>'].concat(
    state.devices.map((device) => {
      const label = `${device.device_id}${device.name ? ` · ${device.name}` : ""}`;
      return `<option value="${esc(device.device_id)}" ${current === device.device_id ? "selected" : ""}>${esc(label)}</option>`;
    })
  );
  return `<select id="task-device-${task.id}" ${disabled}>${options.join("")}</select>`;
}

// 渲染通用表格，减少重复 HTML 拼接。
function table(headers, rows) {
  if (!rows.length) return emptyState("暂无表格数据", "当前筛选条件下没有记录。");
  return `<div class="table-wrap"><table><thead><tr>${headers.map((h) => `<th>${h.label}</th>`).join("")}</tr></thead><tbody>${
    rows.map((row) => `<tr>${headers.map((h) => `<td>${h.render ? h.render(row) : esc(row[h.key])}</td>`).join("")}</tr>`).join("")
  }</tbody></table></div>`;
}

const GLOBAL_STATE_TARGETS = [
  "kpis",
  "serviceFunnel",
  "todoBoard",
  "priorityList",
  "recentOutputs",
  "serviceQualitySummary",
  "serviceQualityTable",
  "chatConversations",
  "chatMessages",
  "chatAiOutputs",
  "wecomFamilies",
  "wecomPreview",
  "familySummary",
  "familyTable",
  "familyDetail",
  "profileTable",
  "reportList",
  "replyFamilies",
  "replyContext",
  "replyOutputs",
  "checkinBoard",
  "taskTable",
  "logTable",
  "auditTable",
  "opsHealthBoard",
  "backupBoard",
  "deviceTable",
  "importTemplateTable",
  "agentEvalBoard",
  "templateTable",
];

function isInitialDataEmpty() {
  return !state.families.length && !state.tasks.length && !state.outputs.length && !state.logs.length;
}

function fillStateTargets(html, onlyEmpty = true) {
  GLOBAL_STATE_TARGETS.forEach((id) => {
    const el = $(id);
    if (!el) return;
    if (!onlyEmpty || !el.innerHTML.trim()) el.innerHTML = html;
  });
}

function renderGlobalLoading() {
  fillStateTargets(loadingState("正在加载控制端数据", "首次加载会同步家庭、任务、日志、设备和 Agent 评测。"), false);
}

function renderGlobalError(detail) {
  fillStateTargets(errorState("控制端数据加载失败", detail || "请确认后端服务、数据库和管理端鉴权配置正常。"), false);
}

// 切换侧边栏标签页，并同步页面标题。
let devicePollTimer = null;
function switchTab(tabId) {
  document.querySelectorAll(".sidebar button").forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === tabId));
  document.querySelectorAll(".panel").forEach((panel) => panel.classList.toggle("active", panel.id === tabId));
  const active = document.querySelector(`.sidebar button[data-tab="${tabId}"]`);
  $("pageTitle").textContent = active ? (active.dataset.title || active.textContent.trim()) : "工作台";
  // 设备页每 5 秒轮询刷新在线状态；离开则停止。
  if (devicePollTimer) { clearInterval(devicePollTimer); devicePollTimer = null; }
  if (tabId === "devices") {
    const poll = async () => {
      try {
        const [devices, opsHealth, backups, retention] = await Promise.all([api("/api/devices"), api("/api/ops/health"), api("/api/ops/backups"), api("/api/ops/retention")]);
        state.devices = devices;
        state.opsHealth = opsHealth;
        state.backups = backups;
        state.retention = retention;
        renderOpsHealth();
        renderBackups();
        renderDevices();
      } catch (err) { /* 忽略轮询错误 */ }
    };
    poll();
    devicePollTimer = setInterval(poll, 5000);
  }
}

// 生成家庭下拉框选项。
function optionList(selected = state.selectedFamilyId) {
  return state.families.map((family) => {
    const label = `${family.parent_nickname || family.family_id} · ${family.family_id}`;
    return `<option value="${esc(family.family_id)}" ${family.family_id === selected ? "selected" : ""}>${esc(label)}</option>`;
  }).join("");
}

// 记录当前选中的家庭，并刷新详情页。
function setSelectedFamily(familyId, tab = "familyDetailPanel") {
  state.selectedFamilyId = familyId || state.selectedFamilyId || state.families[0]?.family_id || "";
  switchTab(tab);
  refreshFamilyDetail();
}

// 根据 family_id 找到家长显示名。
function familyName(familyId) {
  const family = state.families.find((item) => item.family_id === familyId);
  return family?.parent_nickname || familyId;
}

function timelineKind(kind) {
  return ({
    message: "聊天",
    checkin: "打卡",
    ai_output: "AI",
    weekly_report: "周报",
    followup: "跟进",
    send_log: "发送",
  })[kind] || kind;
}

function timelineCard(item) {
  const modeText = item.send_mode === "real_send" ? "真实发送" : item.send_mode === "dry_run" ? "试运行" : item.send_mode;
  const mode = item.send_mode ? ` · ${esc(modeText)}` : "";
  const risk = item.risk_level ? ` · 风险：${esc(item.risk_level)}` : "";
  const device = item.device_id ? ` · 设备：${esc(item.device_id)}` : "";
  const source = item.source ? ` · ${esc(item.source)}` : "";
  const status = item.status ? ` · ${esc(item.status)}` : "";
  const shot = item.screenshot_path ? ` · <a class="dl-link" href="${esc(item.screenshot_path)}" target="_blank" rel="noopener">截图</a>` : "";
  return `
    <article class="timeline-item timeline-${esc(item.kind)}">
      <div class="timeline-head">
        ${badge(timelineKind(item.kind), item.kind === "send_log" || item.kind === "checkin" || item.kind === "followup" ? "ok" : "")}
        <strong>${esc(item.title)}</strong>
        <time>${esc(item.occurred_at)}</time>
      </div>
      <p>${esc(item.content)}</p>
      <span class="muted">${esc(item.target_name || "")}${source}${status}${mode}${risk}${device}${shot}</span>
    </article>
  `;
}

function outputEvidence(output) {
  try {
    return JSON.parse(output.evidence_json || "{}");
  } catch {
    return {};
  }
}

function evidenceView(output) {
  const evidence = outputEvidence(output);
  const summaries = evidence.evidence_summary || [];
  const messages = evidence.source_messages || [];
  if (!summaries.length && !messages.length) return emptyState("暂无可追溯证据", "该输出还没有绑定来源消息或依据摘要。");
  return `
    <div class="evidence-box">
      ${summaries.length ? `<p><strong>依据摘要：</strong>${summaries.map(esc).join("；")}</p>` : ""}
      ${messages.length ? messages.map((msg) => `
        <blockquote>
          <strong>#${esc(msg.message_id)} ${esc(msg.message_time)} ${esc(msg.speaker)}</strong>
          <p>${esc(msg.content)}</p>
          <span class="muted">${esc(msg.source || "")}${msg.checkin_status ? ` · ${esc(msg.checkin_status)}` : ""}</span>
        </blockquote>
      `).join("") : ""}
    </div>
  `;
}

// 顶部 KPI 卡片反映整体待办和风险状态。
function renderKpis() {
  const pendingTasks = state.tasks.filter((task) => task.status === "pending").length;
  const reviewOutputs = state.outputs.filter((item) => item.status === "needs_review").length;
  const highRisk = state.profiles.filter((item) => (item.service_risks || "").includes("退费") || (item.service_risks || "").includes("投诉")).length;
  const approvedReports = state.reports.filter((item) => item.status === "approved").length;
  if (highRisk > 0) {
    $("kpis").innerHTML = riskState("存在高风险家庭", `当前有 ${highRisk} 个家庭出现退费/投诉等风险信号，请优先处理。`, '<button onclick="switchTab(\'adminDashboard\')">查看管理看板</button>');
    $("kpis").innerHTML += [
      ["待审核内容", reviewOutputs, "Agent 生成后待确认"],
      ["待发送任务", pendingTasks, "审核后可发送"],
      ["已确认周报", approvedReports, "可加入发送任务"],
    ].map(([label, value, hint]) => `
      <article class="kpi">
        <span>${esc(label)}</span>
        <strong>${esc(value)}</strong>
        <small>${esc(hint)}</small>
      </article>
    `).join("");
    return;
  }
  $("kpis").innerHTML = [
    ["高风险家庭", highRisk, "需主管关注"],
    ["待审核内容", reviewOutputs, "Agent 生成后待确认"],
    ["待发送任务", pendingTasks, "审核后可发送"],
    ["已确认周报", approvedReports, "可加入发送任务"],
  ].map(([label, value, hint]) => `
    <article class="kpi">
      <span>${esc(label)}</span>
      <strong>${esc(value)}</strong>
      <small>${esc(hint)}</small>
    </article>
  `).join("");
}

function renderCoachFilter() {
  const el = $("coachFilter");
  if (!el) return;
  const coaches = [...new Set(state.families.map((family) => family.coach_name).filter(Boolean))].sort();
  const options = ['<option value="">全部陪跑师</option>'].concat(
    coaches.map((name) => `<option value="${esc(name)}" ${name === state.selectedCoachName ? "selected" : ""}>${esc(name)}</option>`)
  );
  el.innerHTML = options.join("");
}

async function setCoachFilter(coachName) {
  state.selectedCoachName = coachName || "";
  localStorage.setItem("coachFilter", state.selectedCoachName);
  await refreshWorkbenchOverview();
}

async function refreshWorkbenchOverview() {
  const suffix = state.selectedCoachName ? `&coach_name=${encodeURIComponent(state.selectedCoachName)}` : "";
  state.workbenchOverview = await api(`/api/workbench/overview?limit=8${suffix}`);
  renderWorkbenchOverview();
}

function renderServiceFunnel() {
  const stages = state.workbenchOverview?.service_funnel?.stages || [];
  $("serviceFunnel").innerHTML = stages.length ? stages.map((stage) => `
    <article class="funnel-row funnel-${esc(stage.stage)}">
      <div class="funnel-top">
        ${badge(stage.stage, stage.stage === "风险" ? "danger" : stage.stage === "需跟进" || stage.stage === "续报" ? "warn" : "ok")}
        <strong>${esc(stage.family_count)}</strong>
      </div>
      <div class="funnel-families">
        ${(stage.families || []).map((family) => `
          <button onclick="setSelectedFamily('${esc(family.family_id)}')" title="${esc(family.reason || "")}">
            ${esc(family.family_name)}
          </button>
        `).join("") || '<span class="muted">暂无</span>'}
      </div>
    </article>
  `).join("") : emptyState("暂无服务状态数据", "导入家庭或同步企微会话后，这里会按正常、需跟进、风险、续报、已结课聚合。");
}

function renderTodoBoard() {
  const categories = state.workbenchOverview?.todos?.categories || [];
  $("todoBoard").innerHTML = categories.length ? categories.map((category) => `
    <article class="todo-column">
      <div class="todo-head">
        <strong>${esc(category.label)}</strong>
        ${badge(category.count || 0, category.count ? "warn" : "ok")}
      </div>
      <div class="stack">
        ${(category.items || []).map((item) => `
          <button class="todo-item" onclick="setSelectedFamily('${esc(item.family_id)}')">
            <strong>${esc(item.family_name)}</strong>
            <span>${esc(item.reason)}</span>
            <small>${esc((item.evidence || "").slice(0, 80))}</small>
          </button>
        `).join("") || emptyState("暂无待办", "这个分类当前没有需要处理的家庭。")}
      </div>
    </article>
  `).join("") : emptyState("暂无聚合待办", "导入数据并生成 Agent 输出后，待办会自动汇总到这里。");
}

function renderWorkbenchOverview() {
  renderCoachFilter();
  renderServiceFunnel();
  renderTodoBoard();
}

function percent(value) {
  return `${Math.round((Number(value) || 0) * 100)}%`;
}

function renderServiceQuality() {
  const summaryEl = $("serviceQualitySummary");
  const tableEl = $("serviceQualityTable");
  if (!summaryEl || !tableEl) return;
  const totals = state.serviceQuality?.totals || {};
  summaryEl.innerHTML = [
    ["陪跑师", totals.coach_count || 0],
    ["家庭数", totals.family_count || 0],
    ["风险家庭", totals.risk_family_count || 0],
    ["发送完成率", percent(totals.send_completion_rate)],
  ].map(([label, value]) => `<article class="summary"><span>${esc(label)}</span><strong>${esc(value)}</strong></article>`).join("");
  tableEl.innerHTML = table([
    { label: "陪跑师", key: "coach_name" },
    { label: "家庭", key: "family_count" },
    { label: "风险/跟进", render: (r) => `${badge(`风险 ${r.risk_family_count}`, r.risk_family_count ? "danger" : "ok")} ${badge(`跟进 ${r.followup_family_count}`, r.followup_family_count ? "warn" : "ok")}` },
    { label: "续报/结课", render: (r) => `${esc(r.renewal_family_count)} / ${esc(r.closed_family_count)}` },
    { label: "待审核", render: (r) => `AI ${esc(r.review_output_count)} · 周报 ${esc(r.review_report_count)}` },
    { label: "待发送", key: "pending_task_count" },
    { label: "发送完成率", render: (r) => `${badge(percent(r.send_completion_rate), r.send_failure_rate ? "warn" : "ok")} <span class="muted">失败 ${percent(r.send_failure_rate)}</span>` },
    { label: "风险家庭", render: (r) => (r.risk_families || []).map((family) => `<button onclick="setSelectedFamily('${esc(family.family_id)}')">${esc(family.family_name)}</button>`).join("") || "—" },
  ], state.serviceQuality?.coaches || []);
}

function statusBadge(status) {
  if (status === "critical") return badge("严重", "danger");
  if (status === "warn") return badge("预警", "warn");
  return badge("正常", "ok");
}

function formatBytes(bytes) {
  const size = Number(bytes || 0);
  if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
  if (size >= 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${size} B`;
}

function renderOpsHealth() {
  const el = $("opsHealthBoard");
  if (!el) return;
  const health = state.opsHealth || {};
  const components = health.components || [];
  el.innerHTML = `
    <div class="section-head compact-head">
      <h3>整体状态：${statusBadge(health.overall_status || "warn")}</h3>
      <span class="muted">${esc(health.generated_at || "")}</span>
    </div>
    ${table([
      { label: "组件", render: (r) => `<strong>${esc(r.label)}</strong>` },
      { label: "状态", render: (r) => statusBadge(r.status) },
      { label: "详情", key: "detail" },
    ], components)}
  `;
}

function renderBackups() {
  const el = $("backupBoard");
  if (!el) return;
  const backups = state.backups || [];
  const retention = state.retention || {};
  const policy = retention.policy || {};
  const deleted = retention.deleted || {};
  const retentionDetail = retention.detail || `过期对象 ${retention.expired_count ?? 0} 个`;
  el.innerHTML = `
    <p class="muted">基础版采用非破坏式演练：校验备份文件可读、核心表存在和 SQLite 完整性，不覆盖当前业务库。</p>
    <div class="secondary-box">
      <strong>日志保留策略</strong>
      <p class="muted">${esc(retentionDetail)}；发送日志 ${esc(policy.send_log_days || "-")} 天，截图 ${esc(policy.screenshot_days || "-")} 天，运行日志 ${esc(policy.runtime_log_days || "-")} 天。</p>
      ${retention.executed ? `<p>${badge("已清理", "ok")} <span class="muted">发送日志 ${esc(deleted.send_logs || 0)} 条，截图 ${esc(deleted.screenshots || 0)} 个，运行日志 ${esc(deleted.runtime_logs || 0)} 个。</span></p>` : ""}
      <button onclick="refreshRetention()">刷新保留策略</button>
      <button onclick="pruneRetention()">确认清理过期对象</button>
    </div>
    ${table([
      { label: "备份文件", render: (r) => `<strong>${esc(r.filename)}</strong>` },
      { label: "大小", render: (r) => formatBytes(r.size_bytes) },
      { label: "创建时间", key: "created_at" },
      {
        label: "演练结果",
        render: (r) => {
          const drill = state.backupDrills[r.filename];
          if (!drill) return '<span class="muted">未演练</span>';
          return `${badge(drill.passed ? "通过" : "失败", drill.passed ? "ok" : "danger")} <span class="muted">${drill.missing_tables?.length ? `缺表：${esc(drill.missing_tables.join("、"))}` : `完整性：${esc(drill.integrity)}`}</span>`;
        },
      },
      {
        label: "操作",
        render: (r) => `<button onclick="runBackupDrill('${esc(r.filename)}')">恢复演练</button><a class="dl-link" href="/api/ops/backups/${encodeURIComponent(r.filename)}">下载</a>`,
      },
    ], backups)}
  `;
}

// 侧栏优先处理列表，帮助用户先看最紧急的事。
function renderPriorityList() {
  const items = state.todayPriorities || [];
  $("priorityList").innerHTML = items.length ? items.map((item) => `
    <article class="row-card priority-card priority-${esc(item.level)}">
      <div>
        ${badge(`优先级${item.level}`, item.level === "高" ? "warn" : item.level === "中" ? "ok" : "")}
        <strong>${esc(item.family_name || familyName(item.family_id))}</strong>
        <p>${(item.reasons || []).map(esc).join("；")}</p>
        <span class="muted">分数 ${esc(item.score)} · ${esc(item.suggested_action)}${item.last_message_at ? ` · 最近沟通 ${esc(item.last_message_at)}` : ""}</span>
      </div>
      <div class="cell-actions">
        <button onclick="setSelectedFamily('${esc(item.family_id)}')">看时间线</button>
        ${item.pending_task_count ? `<button onclick="switchTab('tasks')">处理发送</button>` : ""}
        ${item.review_output_count || item.review_report_count ? `<button onclick="switchTab('reports')">审核内容</button>` : ""}
      </div>
    </article>
  `).join("") : emptyState("今日暂无高优先级事项", "可继续同步企微或生成 Agent 内容，系统会自动把风险和待办推到这里。");
}

// 输出卡片，展示 AI 生成的内容。
function outputCard(output, compact = false) {
  const agent = AGENTS[output.agent_type] || { name: output.agent_type, className: "" };
  const textId = `output-${output.id}`;
  return `
    <article class="ai-card ${agent.className}">
      <div class="card-top">
        <div>
          ${badge(agent.name)}
          ${badge(output.risk_level || "低", output.risk_level === "高" ? "danger" : output.risk_level === "中" ? "warn" : "ok")}
          ${badge(output.status)}
          ${output.need_human_review === "Y" ? badge("需人工", "warn") : ""}
          <strong>${esc(familyName(output.family_id))}</strong>
        </div>
        <small>${esc(output.created_at || "")}</small>
      </div>
      ${compact ? `<pre>${esc(output.display_text).slice(0, 220)}</pre>` : `
        <textarea id="${textId}">${esc(output.edited_output || output.display_text)}</textarea>
        <details>
          <summary>查看证据链与原始 JSON</summary>
          ${evidenceView(output)}
          <pre>${esc(output.raw_json)}</pre>
        </details>
        <div class="actions left">
          <button onclick="saveOutput(${output.id})">保存审核稿</button>
          <button onclick="createTaskFromOutput(${output.id})">加入发送任务</button>
        </div>
      `}
    </article>
  `;
}

// 渲染最近的输出结果。
function renderRecentOutputs() {
  $("recentOutputs").innerHTML = state.outputs.length ? state.outputs.slice(0, 6).map((item) => outputCard(item, true)).join("") : emptyState("暂无 Agent 输出", "导入聊天记录后，可先批量分析或在家庭详情中生成画像、周报和回复。");
}

// 渲染家庭摘要信息。
function renderFamilySummary() {
  const pendingTasks = state.tasks.filter((task) => task.status === "pending").length;
  const reportReview = state.reports.filter((report) => report.status !== "approved").length;
  const noContact = state.families.filter((family) => !family.message_count).length;
  $("familySummary").innerHTML = [
    ["家庭总数", state.families.length],
    ["待发送任务", pendingTasks],
    ["周报待审核", reportReview],
    ["近7天未沟通", noContact],
  ].map(([label, value]) => `<article class="summary"><span>${esc(label)}</span><strong>${esc(value)}</strong></article>`).join("");
}

// 渲染家庭列表。
function renderFamilies() {
  $("familyTable").innerHTML = table([
    { label: "家庭编号", key: "family_id" },
    { label: "家长", key: "parent_nickname" },
    { label: "年级", key: "child_grade" },
    { label: "课程阶段", render: (r) => esc(displayValue(r.course_stage)) },
    { label: "Unit", render: (r) => esc(displayValue(r.unit_progress)) },
    { label: "打卡率", render: (r) => esc(displayValue(r.checkin_rate)) },
    { label: "陪跑师", key: "coach_name" },
    { label: "消息数", key: "message_count" },
    { label: "操作", render: (r) => `
      <div class="cell-actions">
        <button onclick="setSelectedFamily('${esc(r.family_id)}')">查看</button>
        <button onclick="runAgentForFamily('profile','${esc(r.family_id)}')">画像</button>
        <button onclick="runAgentForFamily('weekly','${esc(r.family_id)}')">周报</button>
        <button onclick="prepareReply('${esc(r.family_id)}')">回复</button>
        <button onclick="runAgentForFamily('checkin','${esc(r.family_id)}')">打卡</button>
      </div>
    ` },
  ], state.families);
  renderFamilySummary();
}

// 渲染网页通讯测试页。
function renderWebChat() {
  $("loginStatus").innerHTML = state.currentUser
    ? `${userRoleBadge(state.currentUser)}<strong>${esc(state.currentUser.display_name)}</strong><p class="muted">${esc(state.currentUser.username)}</p>`
    : emptyState("请先登录测试账号", "登录陪跑师或家长账号后，可以在网页会话里测试 AI 回复闭环。");
  const rows = state.conversations.length ? state.conversations : state.families;
  $("chatConversations").innerHTML = rows.length ? rows.map((item) => `
    <button class="list-item ${item.family_id === state.selectedChatFamilyId ? "selected" : ""}" onclick="selectChat('${esc(item.family_id)}')">
      <strong>${esc(item.parent_nickname || item.family_id)}</strong>
      <span>${esc(item.child_grade || "未知年级")} · ${esc(item.message_count || 0)} 条 · ${esc(item.last_speaker || "")}</span>
      <small>${esc(item.last_message || "")}</small>
    </button>
  `).join("") : emptyState("暂无会话", "点击“生成模拟账号与对话”，或先导入家庭聊天记录。");
  renderChatMessages();
  renderChatOutputs();
}

// 渲染当前聊天消息，让它更接近真实陪跑师对话。
function renderChatMessages() {
  const family = state.families.find((item) => item.family_id === state.selectedChatFamilyId);
  $("chatTitle").textContent = family ? family.parent_nickname : "请选择会话";
  $("chatMeta").textContent = family ? `${family.family_id} · ${family.child_grade || "未知年级"} · ${family.coach_name || "未分配"}` : "";
  if (!state.selectedChatFamilyId) {
    $("chatMessages").innerHTML = emptyState("请选择家庭会话", "从左侧选择一个家庭后，这里会展示聊天上下文。");
    return;
  }
  $("chatMessages").innerHTML = state.chatMessages.length ? state.chatMessages.map((msg) => {
    const isCoach = (msg.speaker || "").includes("老师") || (state.currentUser?.display_name && msg.speaker === state.currentUser.display_name && state.currentUser.role === "coach");
    return `
      <div class="bubble ${isCoach ? "coach" : "parent"}">
        <strong>${esc(msg.speaker)}</strong>
        <p>${esc(msg.content)}</p>
        <span>${esc(msg.message_time || "")}</span>
      </div>
    `;
  }).join("") : emptyState("暂无消息", "当前会话还没有聊天记录，可以发送一条测试消息或同步企微。");
}

function renderChatOutputs() {
  if (!state.selectedChatFamilyId) {
    $("chatAiOutputs").innerHTML = emptyState("请选择会话", "选择会话后，可以快速生成回复、审核并发送。");
    return;
  }
  const family = state.families.find((item) => item.family_id === state.selectedChatFamilyId);
  const profile = state.profiles.find((item) => item.family_id === state.selectedChatFamilyId);
  const pendingTasks = state.tasks.filter((item) => item.family_id === state.selectedChatFamilyId && item.status === "pending").slice(0, 4);
  const outputs = state.outputs.filter((item) => item.family_id === state.selectedChatFamilyId).slice(0, 4);
  $("chatAiOutputs").innerHTML = `
    <article class="assist-card">
      <div class="card-top">
        <div>
          ${badge("当前家庭", "ok")}
          <strong>${esc(family?.parent_nickname || state.selectedChatFamilyId)}</strong>
        </div>
        <button onclick="setSelectedFamily('${esc(state.selectedChatFamilyId)}')">档案</button>
      </div>
      ${profile ? `
        <dl>
          <dt>沟通风格</dt><dd>${esc(profile.communication_style || "未识别")}</dd>
          <dt>关注点</dt><dd>${esc(profile.pain_points || "暂无")}</dd>
          <dt>满意度</dt><dd>${esc(displayValue(profile.satisfaction_level, "未识别"))}</dd>
          <dt>续报意向</dt><dd>${esc(displayValue(profile.renewal_intent, "未识别"))}</dd>
          <dt>建议动作</dt><dd>${esc(profile.suggested_actions || "暂无")}</dd>
        </dl>
      ` : emptyState("暂无画像", "点击“完整分析”会生成家庭画像。")}
    </article>
    <section class="assist-section">
      <div class="section-head compact-head">
        <h3>待审核回复</h3>
        <button onclick="switchTab('tasks')">全部</button>
      </div>
      <div class="stack">
        ${pendingTasks.length ? pendingTasks.map((task) => `
          <article class="task-card">
            <strong>${esc(task.scene || "AI回复")}</strong>
            ${taskOperationBadges(task)}
            <textarea id="chat-task-${task.id}" ${taskCan(task, "edit") ? "" : "readonly"}>${esc(task.content)}</textarea>
            <div class="actions left">
              ${taskCan(task, "edit") ? `<button onclick="saveTaskFromChat(${task.id})">保存</button>` : ""}
              ${taskCan(task, "web_send") ? `<button onclick="sendTaskFromChat(${task.id})">发送</button>` : ""}
              ${taskCan(task, "cancel") ? `<button onclick="cancelTask(${task.id})">取消</button>` : ""}
              ${taskAllowedOperations(task).length === 1 ? '<span class="muted">仅可查看</span>' : ""}
            </div>
          </article>
        `).join("") : emptyState("暂无待发送回复", "点击“快速生成回复”后，AI 回复会先进入审核发送队列。")}
      </div>
    </section>
    <section class="assist-section">
      <div class="section-head compact-head"><h3>最近 AI 结果</h3></div>
      <div class="stack">${outputs.length ? outputs.map((item) => outputCard(item, true)).join("") : emptyState("暂无 AI 结果", "同步或生成后，这里会展示最近的画像、回复和周报。")}</div>
    </section>
  `;
}

async function selectChat(familyId) {
  return withAction("切换会话", async () => {
    state.selectedChatFamilyId = familyId;
    state.selectedFamilyId = familyId;
    state.chatMessages = await api(`/api/test-chat/messages/${encodeURIComponent(familyId)}`);
    renderWebChat();
  });
}

// 渲染企微会话登记和同步检查页。
function renderWecomPage() {
  $("wecomFamilies").innerHTML = state.families.length ? state.families.map((family) => `
    <article class="row-card">
      <div>
        ${badge(family.service_status || "企微待同步")}
        <strong>${esc(family.parent_nickname || family.family_id)}</strong>
        <p>${esc(family.family_id)} · ${esc(family.child_grade || "未知年级")} · ${esc(family.coach_name || "未填写陪跑师")} · ${esc(family.message_count)} 条消息</p>
      </div>
      <div class="cell-actions">
        <button onclick="previewWecomFamily('${esc(family.family_id)}')">检查</button>
        <button onclick="prepareReply('${esc(family.family_id)}')">看回复</button>
      </div>
    </article>
  `).join("") : emptyState("还没有登记企微会话", "先填写上方表单，例如：艺博展讯。登记后 RPA 才能按名称搜索同步。");
  if (!state.selectedFamilyId) {
    $("wecomPreview").innerHTML = emptyState("请选择一个会话", "选择左侧已登记会话后，这里会汇总聊天记录和 AI 输出。");
  }
}

// 企微同步后，用这个面板集中看聊天记录和四类 Agent 输出。
async function previewWecomFamily(familyId, remember = true) {
  return withAction("检查会话", async () => {
    if (!familyId) {
      $("wecomPreview").innerHTML = emptyState("请选择一个会话", "选择左侧已登记会话后再检查同步结果。");
      return;
    }
    if (remember) state.selectedFamilyId = familyId;
    const data = await api(`/api/families/${encodeURIComponent(familyId)}`);
    const outputs = state.outputs.filter((item) => item.family_id === familyId).slice(0, 8);
    $("wecomPreview").innerHTML = `
      <div class="profile-pane">
        <h3>${esc(data.family.parent_nickname || data.family.family_id)}</h3>
        <p class="muted">${esc(data.family.family_id)} · RPA 命令：.\\.venv\\Scripts\\python.exe rpa\\wecom_sender.py --sync-target "${esc(data.family.parent_nickname || data.family.family_id)}"</p>
      </div>
      <div class="messages">${data.messages.slice(-30).map((m) => `
        <div class="msg">
          <strong>${esc(m.message_time)} ${esc(m.speaker)}</strong>
          <p>${esc(m.content)}</p>
          <span class="muted">${esc(m.source)} ${esc(m.checkin_status || "")}</span>
        </div>
      `).join("") || emptyState("暂无已同步聊天记录", "运行 RPA 同步后，最新聊天会出现在这里。")}</div>
      <div class="stack output-preview">${outputs.length ? outputs.map((item) => outputCard(item, true)).join("") : emptyState("暂无 AI 输出", "同步到新消息后会自动生成回复、画像、周报和打卡/PBL 结果。")}</div>
    `;
  });
}

// 刷新家庭详情页。
async function refreshFamilyDetail() {
  if (!state.families.length) {
    $("familySelect").innerHTML = "";
    $("familyDetail").innerHTML = emptyState("请先导入家庭数据", "导入 CSV/XLSX 或载入样例后，家庭档案会在这里展示。");
    return;
  }
  state.selectedFamilyId = state.selectedFamilyId || state.families[0].family_id;
  $("familySelect").innerHTML = optionList();
  const data = await api(`/api/families/${encodeURIComponent(state.selectedFamilyId)}`);
  const outputs = state.outputs.filter((item) => item.family_id === state.selectedFamilyId).slice(0, 8);
  const timeline = data.timeline || [];
  const followups = data.followups || [];
  $("familyDetail").innerHTML = `
    <section class="profile-pane">
      <h3>${esc(data.family.parent_nickname || data.family.family_id)}</h3>
      <p class="muted">${esc(data.family.family_id)} · ${esc(data.family.child_grade || "未知年级")} · ${esc(data.family.coach_name || "未分配陪跑师")}</p>
      ${stageProfile(data.family)}
      ${data.profile ? `
        <dl>
          <dt>沟通风格</dt><dd>${esc(data.profile.communication_style)}</dd>
          <dt>关注点</dt><dd>${esc(data.profile.pain_points)}</dd>
          <dt>满意度</dt><dd>${esc(displayValue(data.profile.satisfaction_level, "未识别"))}</dd>
          <dt>风险信号</dt><dd>${esc(data.profile.service_risks)}</dd>
          <dt>续报意向</dt><dd>${esc(displayValue(data.profile.renewal_intent, "未识别"))}</dd>
          <dt>建议动作</dt><dd>${esc(data.profile.suggested_actions)}</dd>
        </dl>
      ` : emptyState("暂无画像", "点击右侧“生成画像”，系统会基于聊天记录提炼沟通风格、风险和建议动作。")}
    </section>
    <section>
      <h3>跟进记录</h3>
      <form class="compact-form followup-form" onsubmit="addFollowup(event, '${esc(data.family.family_id)}')">
        <select name="followup_type">
          <option>电话</option>
          <option selected>私信</option>
          <option>群提醒</option>
          <option>周报</option>
          <option>补课</option>
          <option>投诉</option>
          <option>续报沟通</option>
        </select>
        <input name="owner" placeholder="负责人，可空" value="${esc(data.family.coach_name || "")}" />
        <textarea name="content" placeholder="记录本次跟进内容" required></textarea>
        <input name="result" placeholder="结果/结论，可空" />
        <input name="next_action" placeholder="下一步动作，可空" />
        <select name="status">
          <option>待跟进</option>
          <option>已完成</option>
          <option>需升级</option>
        </select>
        <button>记录跟进</button>
      </form>
      <div class="followup-list">${followupRecords(followups)}</div>
      <h3>家庭时间线</h3>
      <div class="timeline">${timeline.map(timelineCard).join("") || emptyState("暂无时间线事件", "聊天、打卡、周报和发送日志会统一沉淀到这里。")}</div>
    </section>
    <section class="ai-pane">
      <h3>AI操作区</h3>
      <div class="agent-buttons">
        <button onclick="runFamilyAiBundle('${esc(data.family.family_id)}')">一键生成并复核</button>
        <button onclick="runAgentForFamily('profile','${esc(data.family.family_id)}')">生成画像</button>
        <button onclick="runAgentForFamily('weekly','${esc(data.family.family_id)}')">生成周报</button>
        <button onclick="prepareReply('${esc(data.family.family_id)}')">生成回复</button>
        <button onclick="runAgentForFamily('checkin','${esc(data.family.family_id)}')">识别打卡/PBL</button>
      </div>
      <div class="stack">${outputs.length ? outputs.map((item) => outputCard(item, false)).join("") : emptyState("暂无本家庭 AI 结果", "使用上方按钮一键生成画像、周报、回复和打卡/PBL 后，可在这里直接复核。")}</div>
    </section>
  `;
}

// 渲染个人资料列表。
function renderProfiles() {
  $("profileTable").innerHTML = table([
    { label: "家庭", key: "family_id" },
    { label: "信任", render: (r) => `${badge(r.trust_level || "C")} ${esc(r.trust_trend || "")}` },
    { label: "关注点", key: "pain_points" },
    { label: "沟通风格", key: "communication_style" },
    { label: "满意度", render: (r) => esc(displayValue(r.satisfaction_level, "未识别")) },
    { label: "续报意向", render: (r) => esc(displayValue(r.renewal_intent, "未识别")) },
    { label: "孩子状态", key: "child_summary" },
    { label: "风险", render: (r) => (r.service_risks || "").includes("退费") || (r.service_risks || "").includes("投诉") ? `<span class="danger-text">${esc(r.service_risks)}</span>` : esc(r.service_risks) },
    { label: "建议动作", key: "suggested_actions" },
    { label: "操作", render: (r) => `<button onclick="runAgentForFamily('profile','${esc(r.family_id)}')">重新生成</button>` },
  ], state.profiles);
}

// 渲染报告列表。
function renderReports() {
  $("reportList").innerHTML = state.reports.length ? state.reports.map((r) => `
    <article class="report-card">
      <div class="card-top">
        <div>
          <strong>${esc(familyName(r.family_id))}</strong>
          ${badge(r.status, r.status === "approved" ? "ok" : "warn")}
          ${reportSendStatusBadge(r.send_status || "not_created")}
          <span class="muted">${esc(r.week_label)}${r.send_task_id ? ` · 任务 #${esc(r.send_task_id)}` : ""}</span>
        </div>
        <button onclick="createReportTask(${r.id})">${r.send_task_id ? "同步发送任务" : "加入发送任务"}</button>
      </div>
      <div class="report-grid">
        <p><strong>总结</strong>${esc(r.overall_state)}</p>
        <p><strong>亮点</strong>${esc(r.main_changes)}</p>
        <p><strong>关注</strong>${esc(r.parent_focus)}</p>
        <p><strong>建议</strong>${esc(r.teacher_suggestion)}</p>
      </div>
      <textarea id="report-${r.id}">${esc(r.final_text || "")}</textarea>
      <div class="actions left"><button onclick="approveReport(${r.id})">确认通过</button></div>
    </article>
  `).join("") : emptyState("暂无周报", "批量生成周报或在家庭详情中生成单个家庭周报。");
}

// 渲染回复页面。
function renderReplyPage() {
  $("replyFamilySelect").innerHTML = optionList(state.selectedFamilyId);
  $("replyFamilies").innerHTML = state.families.length ? state.families.map((family) => `
    <button class="list-item ${family.family_id === state.selectedFamilyId ? "selected" : ""}" onclick="prepareReply('${esc(family.family_id)}', false)">
      <strong>${esc(family.parent_nickname || family.family_id)}</strong>
      <span>${esc(family.message_count)} 条消息</span>
    </button>
  `).join("") : emptyState("暂无家庭", "请先导入家庭数据或登记企微会话。");
  renderReplyContext();
  $("replyOutputs").innerHTML = state.outputs.filter((item) => item.agent_type === "ai_reply").slice(0, 8).map((item) => outputCard(item)).join("") || emptyState("暂无回复建议", "选择家庭并点击生成回复后，建议会进入这里等待审核。");
}

// 渲染回复上下文。
async function renderReplyContext() {
  if (!state.selectedFamilyId) {
    $("replyContext").innerHTML = emptyState("请选择家庭", "选择家庭后会展示最近 10 条聊天上下文。");
    return;
  }
  const data = await api(`/api/families/${encodeURIComponent(state.selectedFamilyId)}`);
  $("replyContext").innerHTML = data.messages.slice(-10).map((m) => `
    <div class="msg"><strong>${esc(m.speaker)}</strong><p>${esc(m.content)}</p><span class="muted">${esc(m.message_time)}</span></div>
  `).join("") || emptyState("暂无聊天上下文", "导入聊天记录或同步企微后，AI 回复会更准确。");
}

// 渲染打卡记录。
function renderCheckins() {
  const outputs = state.outputs.filter((item) => item.agent_type === "checkin_pbl");
  $("checkinBoard").innerHTML = outputs.length ? outputs.map((item) => outputCard(item)).join("") : emptyState("暂无打卡/PBL 识别结果", "可从家庭列表或本页批量识别生成。");
}

// 渲染任务列表。
function renderTasks() {
  if ($("sendAllBtn")) {
    const canBulkSend = state.tasks.some((task) => taskCan(task, "web_send"));
    $("sendAllBtn").disabled = !canBulkSend;
    $("sendAllBtn").title = canBulkSend ? "发送全部可网页发送任务" : "当前角色或任务状态不允许批量发送";
  }
  $("taskTable").innerHTML = table([
    { label: "ID", key: "id" },
    { label: "家庭", render: (r) => esc(familyName(r.family_id)) },
    { label: "对象", key: "target_name" },
    { label: "来源/场景", key: "scene" },
    { label: "状态", render: (r) => badge(r.status, r.status === "sent" ? "ok" : r.status === "cancelled" ? "" : "warn") },
    { label: "操作分层", render: taskOperationBadges },
    { label: "发送设备", render: (r) => deviceSelect(r) },
    { label: "企微模式", render: (r) => sendModeSelect(r) },
    { label: "最终内容", render: (r) => `<textarea id="task-${r.id}" ${taskCan(r, "edit") ? "" : "readonly"}>${esc(r.content)}</textarea>` },
    { label: "操作", render: (r) => `
      <div class="cell-actions">
        ${taskCan(r, "edit") || taskCan(r, "confirm_real_send") ? `<button onclick="saveTask(${r.id})">保存/审核</button>` : ""}
        ${taskCan(r, "dry_run") ? `<button onclick="queueTaskDryRun(${r.id})">企微试运行</button>` : ""}
        ${taskCan(r, "web_send") ? `<button onclick="sendTask(${r.id})">网页发送</button>` : ""}
        ${taskCan(r, "cancel") ? `<button onclick="cancelTask(${r.id})">取消</button>` : ""}
        ${taskAllowedOperations(r).length === 1 ? '<span class="muted">仅可查看</span>' : ""}
      </div>
    ` },
  ], state.tasks);
}

// 渲染日志列表。
function renderLogs() {
  $("logTable").innerHTML = table([
    { label: "时间", key: "sent_at" },
    { label: "任务", key: "task_id" },
    { label: "家庭", render: (r) => esc(familyName(r.family_id)) },
    { label: "对象", key: "target_name" },
    { label: "状态", key: "status" },
    { label: "模式", render: (r) => sendModeBadge(r.send_mode || "dry_run") },
    { label: "阶段/原因", render: sendReasonCell },
    { label: "截图", render: (r) => r.screenshot_path ? `<a class="dl-link" href="${esc(r.screenshot_path)}" target="_blank" rel="noopener">查看</a>` : "—" },
    { label: "详情", key: "detail" },
  ], state.logs);
}

function renderAuditLogs() {
  $("auditTable").innerHTML = table([
    { label: "时间", key: "created_at" },
    { label: "对象", render: (r) => `${esc(r.entity_type)}#${esc(r.entity_id)}` },
    { label: "动作", key: "action" },
    { label: "操作人/设备", key: "actor" },
    { label: "摘要", key: "summary" },
  ], state.auditLogs);
}

// 渲染设备监控列表。
function renderDevices() {
  $("deviceTable").innerHTML = table([
    { label: "设备ID", key: "device_id" },
    { label: "名称", key: "name" },
    { label: "在线", render: (r) => badge(r.online ? "在线" : "离线", r.online ? "ok" : "") },
    { label: "企微", render: (r) => badge(r.wecom_ok === "Y" ? "正常" : (r.wecom_ok || "未知"), r.wecom_ok === "Y" ? "ok" : "") },
    { label: "最后心跳", key: "last_heartbeat" },
    { label: "负责会话", key: "conversation_count" },
    { label: "待发", render: (r) => (r.task_counts?.pending ?? 0) + (r.task_counts?.assigned ?? 0) },
    { label: "已发", render: (r) => r.task_counts?.sent ?? 0 },
    { label: "失败", render: (r) => r.task_counts?.failed ?? 0 },
    { label: "最近错误", key: "last_error" },
    { label: "接入包", render: (r) => `<a class="dl-link" href="/api/devices/${encodeURIComponent(r.device_id)}/package?server_url=${encodeURIComponent(location.origin)}">下载接入包</a>` },
  ], state.devices);
}

// 渲染 ARK 云端定位密钥配置状态。
function renderArkConfig() {
  const el = $("arkStatus");
  if (!el) return;
  const a = state.arkConfig || {};
  el.textContent = a.configured
    ? `已配置：${a.api_key_masked}　模型 ${a.endpoint_id || "qwen-vl-plus"}`
    : "未配置 —— 被控端云端定位需要它，请填入阿里百炼 API-KEY";
}

function renderImportTemplates() {
  const el = $("importTemplateTable");
  if (!el) return;
  el.innerHTML = table([
    { label: "模板", render: (r) => `<strong>${esc(r.name)}</strong><p class="muted">${esc(r.description)}</p>` },
    { label: "业务类型", key: "business_type" },
    { label: "版本", render: (r) => `v${esc(r.version)}` },
    { label: "必填字段", render: (r) => (r.required_fields || []).map((field) => badge(field, "ok")).join("") },
    { label: "下载", render: (r) => `<a class="dl-link" href="/api/import/templates/${encodeURIComponent(r.key)}/csv">CSV模板</a>` },
  ], state.importTemplates || []);
}

function renderAgentEval() {
  const el = $("agentEvalBoard");
  if (!el) return;
  const evalResult = state.agentEval || {};
  const results = evalResult.results || [];
  const summary = evalResult.total
    ? `<div class="summary-grid mini-summary">
        <article class="summary"><span>评测用例</span><strong>${esc(evalResult.total)}</strong></article>
        <article class="summary"><span>通过</span><strong>${esc(evalResult.passed)}</strong></article>
        <article class="summary"><span>失败</span><strong>${esc(evalResult.failed)}</strong></article>
        <article class="summary"><span>通过率</span><strong>${esc(Math.round((evalResult.pass_rate || 0) * 100))}%</strong></article>
      </div>`
    : "";
  el.innerHTML = `
    ${summary}
    ${table([
      { label: "用例", render: (r) => `<strong>${esc(r.id)}</strong><p class="muted">${esc(r.input)}</p>` },
      { label: "Agent", key: "agent_type" },
      { label: "场景", render: (r) => `${badge(r.actual_scene, r.checks?.scene_match ? "ok" : "danger")}<span class="muted">期望：${esc(r.expected_scene)}</span>` },
      { label: "风险", render: (r) => `${badge(r.actual_risk_level, r.actual_risk_level === "高" ? "warn" : "ok")}<span class="muted">期望：${esc(r.expected_risk_level)}</span>` },
      { label: "结果", render: (r) => badge(r.passed ? "通过" : "失败", r.passed ? "ok" : "danger") },
    ], results)}
  `;
}

// 渲染模板列表。
function renderTemplates() {
  $("templateTable").innerHTML = table([
    { label: "模板名", render: (r) => `<input id="tpl-name-${r.id}" value="${esc(r.name)}" />` },
    { label: "场景", render: (r) => `<input id="tpl-scene-${r.id}" value="${esc(r.scene)}" />` },
    { label: "时间", render: (r) => `<input id="tpl-time-${r.id}" value="${esc(r.send_time)}" />` },
    { label: "内容", render: (r) => `<textarea id="tpl-content-${r.id}">${esc(r.content)}</textarea>` },
    { label: "启用", render: (r) => badge(r.enabled, r.enabled === "Y" ? "ok" : "") },
    { label: "操作", render: (r) => `<button onclick="saveTemplate(${r.id})">保存</button><button onclick="toggleTemplate(${r.id})">${r.enabled === "Y" ? "停用" : "启用"}</button>` },
  ], state.templates);
}

// 渲染所有内容。
function renderAll() {
  renderKpis();
  renderWorkbenchOverview();
  renderServiceQuality();
  renderPriorityList();
  renderRecentOutputs();
  renderWebChat();
  renderWecomPage();
  renderFamilies();
  renderProfiles();
  renderReports();
  renderReplyPage();
  renderCheckins();
  renderTasks();
  renderLogs();
  renderAuditLogs();
  renderOpsHealth();
  renderBackups();
  renderDevices();
  renderArkConfig();
  renderImportTemplates();
  renderAgentEval();
  renderTemplates();
}

// 刷新所有内容。
async function refreshAll() {
  return withAction("刷新数据", async () => {
    if (isInitialDataEmpty()) renderGlobalLoading();
    const coachSuffix = state.selectedCoachName ? `&coach_name=${encodeURIComponent(state.selectedCoachName)}` : "";
    const [families, profiles, reports, templates, tasks, logs, auditLogs, todayPriorities, workbenchOverview, serviceQuality, outputs, accounts, conversations, devices, opsHealth, backups, retention, arkConfig, importTemplates, agentEval] = await Promise.all([
      api("/api/families"),
      api("/api/profiles"),
      api("/api/reports"),
      api("/api/templates"),
      api("/api/send-tasks"),
      api("/api/send-logs"),
      api("/api/audit-logs?entity_type=send_task&limit=200"),
      api("/api/workbench/today-priorities?limit=12"),
      api(`/api/workbench/overview?limit=8${coachSuffix}`),
      api("/api/admin/service-quality"),
      api("/api/ai-outputs"),
      api("/api/test-chat/accounts"),
      api("/api/test-chat/conversations"),
      api("/api/devices"),
      api("/api/ops/health"),
      api("/api/ops/backups"),
      api("/api/ops/retention"),
      api("/api/ark-config").catch(() => ({})),
      api("/api/import/templates"),
      api("/api/agent/evaluations/run", { method: "POST" }),
    ]);
    Object.assign(state, { families, profiles, reports, templates, tasks, logs, auditLogs, todayPriorities, workbenchOverview, serviceQuality, outputs, accounts, conversations, devices, opsHealth, backups, retention, arkConfig, importTemplates, agentEval });
    state.selectedFamilyId = state.selectedFamilyId || families[0]?.family_id || "";
    state.selectedChatFamilyId = state.selectedChatFamilyId || families[0]?.family_id || "";
    if (state.selectedChatFamilyId) {
      try {
        state.chatMessages = await api(`/api/test-chat/messages/${encodeURIComponent(state.selectedChatFamilyId)}`);
      } catch {
        state.chatMessages = [];
      }
    }
    renderAll();
    if (document.querySelector("#familyDetailPanel.active")) await refreshFamilyDetail();
  });
}

// 为家庭运行代理。
async function runAgentForFamily(kind, familyId = state.selectedFamilyId) {
  return withAction(`生成${kind}`, async () => {
    if (!familyId) return toast("请先选择家庭");
    const path = {
      profile: "/api/agent/profile",
      weekly: "/api/agent/weekly-report",
      reply: "/api/agent/reply",
      checkin: "/api/agent/checkin-pbl",
    }[kind];
    const result = await api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ family_id: familyId, source: "UI按钮触发" }),
    });
    state.selectedFamilyId = familyId;
    toast(`已生成：${result.family_name || familyId}`);
    await refreshAll();
  });
}

async function runFamilyAiBundle(familyId = state.selectedFamilyId) {
  return withAction("一键生成AI操作区", async () => {
    if (!familyId) return toast("请先选择家庭");
    const result = await api(`/api/families/${encodeURIComponent(familyId)}/ai-bundle`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: "家庭详情一键生成" }),
    });
    state.selectedFamilyId = familyId;
    toast(`已生成 ${result.outputs?.length || 0} 个 AI 结果，待人工复核`);
    await refreshAll();
  });
}

// 批量处理代理。
async function batchAgent(kind) {
  return withAction(`批量生成${kind}`, async () => {
    for (const family of state.families) {
      await runAgentForFamily(kind, family.family_id);
    }
    toast("批量处理完成");
  });
}

// 准备回复。
function prepareReply(familyId, goTab = true) {
  state.selectedFamilyId = familyId;
  if (goTab) switchTab("replies");
  renderReplyPage();
}

// 运行回复代理。
async function runReplyAgent(tone) {
  return withAction("生成回复", async () => {
    const familyId = $("replyFamilySelect").value || state.selectedFamilyId;
    state.selectedFamilyId = familyId;
    await api("/api/agent/reply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ family_id: familyId, message: $("replyMessage").value, tone, source: `AI回复-${tone}` }),
    });
    $("replyMessage").value = "";
    toast("回复建议已生成");
    await refreshAll();
    switchTab("replies");
  });
}

// 保存输出。
async function saveOutput(id) {
  return withAction("保存审核稿", async () => {
    const edited_output = $(`output-${id}`).value;
    await api(`/api/ai-outputs/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ edited_output, status: "approved" }),
    });
    toast("审核稿已保存");
    await refreshAll();
  });
}

// 从输出创建任务。
async function createTaskFromOutput(id) {
  return withAction("加入发送任务", async () => {
    const textarea = $(`output-${id}`);
    const content = textarea ? textarea.value : "";
    await api(`/api/ai-outputs/${id}/send-task`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
    toast("已加入待发送任务");
    await refreshAll();
    switchTab("tasks");
  });
}

// 审核报告。
async function approveReport(id) {
  return withAction("确认周报", async () => {
    const final_text = $(`report-${id}`).value;
    await api(`/api/reports/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ final_text, status: "approved" }),
    });
    toast("周报已确认");
    await refreshAll();
  });
}

// 创建报告任务。
async function createReportTask(id) {
  return withAction("创建周报任务", async () => {
    const report = state.reports.find((item) => item.id === id);
    if (!report) return;
    const final_text = $(`report-${id}`).value;
    await api(`/api/reports/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ final_text, status: "approved" }),
    });
    const res = await api(`/api/reports/${id}/send-task`, { method: "POST" });
    toast(res.created ? "周报已加入发送任务" : "周报已绑定已有发送任务");
    await refreshAll();
    switchTab("tasks");
  });
}

// 保存任务。
async function saveTask(id) {
  return withAction("保存任务", async () => {
    const task = state.tasks.find((item) => item.id === id);
    const sendMode = $(`task-mode-${id}`)?.value || "dry_run";
    const confirmRealSend = sendMode === "real_send" && task?.send_mode !== "real_send";
    if (confirmRealSend) {
      const ok = window.confirm(`确认将任务 ${id} 设置为真实发送？\n目标：${task?.target_name || ""}\n真实发送会触达企业微信会话，请先确认内容无误。`);
      if (!ok) return;
    }
    await api(`/api/send-tasks/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...task,
        content: $(`task-${id}`).value,
        device_id: $(`task-device-${id}`)?.value || "",
        send_mode: sendMode,
        confirm_real_send: confirmRealSend,
      }),
    });
    toast("任务已保存");
    await refreshAll();
  });
}

async function saveTaskFromChat(id) {
  const editor = $(`chat-task-${id}`);
  if ($(`task-${id}`) && editor) $(`task-${id}`).value = editor.value;
  return withAction("保存回复", async () => {
    const task = state.tasks.find((item) => item.id === id);
    await api(`/api/send-tasks/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...task, content: editor?.value || task?.content || "", send_mode: task?.send_mode || "dry_run" }),
    });
    toast("回复已保存");
    await refreshAll();
    switchTab("webChat");
  });
}

// 取消任务。
async function cancelTask(id) {
  return withAction("取消任务", async () => {
    await api(`/api/send-tasks/${id}/cancel`, { method: "POST" });
    toast("任务已取消");
    await refreshAll();
  });
}

// 把任务显式加入企微 dry-run 队列：被控端只定位、粘贴、清空，不按发送键。
async function queueTaskDryRun(id) {
  return withAction("企微试运行", async () => {
    const task = state.tasks.find((item) => item.id === id);
    const editor = $(`task-${id}`);
    await api(`/api/send-tasks/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...task,
        content: editor?.value || task?.content || "",
        device_id: $(`task-device-${id}`)?.value || "",
        send_mode: "dry_run",
        status: "pending",
      }),
    });
    await api(`/api/send-tasks/${id}/dry-run`, { method: "POST" });
    toast("已加入企微试运行队列；被控端会定位、粘贴并清空，不会真实发送");
    await refreshAll();
  });
}

// 发送任务到网页通讯会话。
async function sendTask(id) {
  return withAction("发送任务", async () => {
    await api(`/api/send-tasks/${id}/web-send`, { method: "POST" });
    toast("已发送到网页通讯");
    await refreshAll();
  });
}

async function sendTaskFromChat(id) {
  return withAction("发送回复", async () => {
    const editor = $(`chat-task-${id}`);
    const task = state.tasks.find((item) => item.id === id);
    if (editor && task && editor.value !== task.content) {
      await api(`/api/send-tasks/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...task, content: editor.value, send_mode: task?.send_mode || "dry_run" }),
      });
    }
    await api(`/api/send-tasks/${id}/web-send`, { method: "POST" });
    toast("已发送到当前会话");
    await refreshAll();
    switchTab("webChat");
  });
}

async function addFollowup(event, familyId) {
  event.preventDefault();
  await withAction("记录跟进", async () => {
    const data = Object.fromEntries(new FormData(event.target).entries());
    await api(`/api/families/${encodeURIComponent(familyId)}/followups`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    event.target.reset();
    toast("跟进记录已保存");
    await refreshAll();
  });
}

// 保存模板。
async function saveTemplate(id) {
  return withAction("保存模板", async () => {
    await api(`/api/templates/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: $(`tpl-name-${id}`).value,
        scene: $(`tpl-scene-${id}`).value,
        send_time: $(`tpl-time-${id}`).value,
        content: $(`tpl-content-${id}`).value,
        enabled: state.templates.find((item) => item.id === id)?.enabled || "Y",
      }),
    });
    toast("模板已保存");
    await refreshAll();
  });
}

// 切换模板状态。
async function toggleTemplate(id) {
  return withAction("切换模板", async () => {
    await api(`/api/templates/${id}/toggle`, { method: "POST" });
    toast("模板状态已更新");
    await refreshAll();
  });
}

// 初始化事件监听。
document.querySelectorAll(".sidebar button").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

// 处理家庭选择变化。
$("familySelect").onchange = (event) => {
  state.selectedFamilyId = event.target.value;
  refreshFamilyDetail();
};

// 处理回复家庭选择变化。
$("replyFamilySelect").onchange = (event) => {
  state.selectedFamilyId = event.target.value;
  renderReplyPage();
};

// 导入样例数据。
$("sampleBtn").onclick = async () => {
  await withAction("载入样例", async () => {
    const res = await api("/api/sample-data", { method: "POST" });
    toast(`样例已导入：${res.families} 个家庭，${res.messages} 条消息`);
    await refreshAll();
  });
};

// 生成所有数据。
$("generateBtn").onclick = async () => {
  await withAction("批量周报/画像", async () => {
    const res = await api("/api/generate/all", { method: "POST" });
    toast(`已生成 ${res.generated_families} 个家庭`);
    await refreshAll();
  });
};

// 扫描打卡记录。
$("scanBtn").onclick = async () => {
  await withAction("批量识别打卡", async () => {
    const res = await api("/api/scan-checkins", { method: "POST" });
    toast(`新增打卡记录 ${res.checkin_records_created} 条`);
    await refreshAll();
  });
};

// 处理文件导入。
$("fileInput").onchange = async (event) => {
  await withAction("导入文件", async () => {
    const file = event.target.files[0];
    if (!file) return;
    const body = new FormData();
    body.append("file", file);
    const res = await api("/api/import", { method: "POST", body });
    event.target.value = "";
    toast(`导入完成：${res.families} 个家庭，${res.messages} 条消息`);
    await refreshAll();
  });
};

// 提交模板表单。
$("templateForm").onsubmit = async (event) => {
  event.preventDefault();
  await withAction("新增模板", async () => {
    const data = Object.fromEntries(new FormData(event.target).entries());
    await api("/api/templates", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...data, enabled: "Y" }) });
    event.target.reset();
    toast("模板已新增");
    await refreshAll();
  });
};

// 添加设备：注册并生成 token，之后可在列表点「下载接入包」。
$("deviceForm").onsubmit = async (event) => {
  event.preventDefault();
  await withAction("添加设备", async () => {
    const data = Object.fromEntries(new FormData(event.target).entries());
    const conversations = (data.conversations || "").split(/[,，]/).map((s) => s.trim()).filter(Boolean);
    const dev = await api("/api/devices", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ device_id: data.device_id, name: data.name || "", conversations }) });
    event.target.reset();
    toast(`设备已添加：${dev.device_id}，可在列表点「下载接入包」发给对方`);
    await refreshAll();
  });
};

async function refreshBackups() {
  const [backups, retention] = await Promise.all([api("/api/ops/backups"), api("/api/ops/retention")]);
  state.backups = backups;
  state.retention = retention;
  renderBackups();
}

async function createBackup() {
  await withAction("创建数据备份", async () => {
    const backup = await api("/api/ops/backups", { method: "POST" });
    state.backupDrills = {};
    toast(`备份已创建：${backup.filename}`);
    await refreshBackups();
    state.opsHealth = await api("/api/ops/health");
    renderOpsHealth();
  });
}

async function refreshRetention() {
  await withAction("刷新保留策略", async () => {
    state.retention = await api("/api/ops/retention");
    renderBackups();
  });
}

async function pruneRetention() {
  const expired = state.retention?.expired_count ?? 0;
  if (!window.confirm(`将清理 ${expired} 个/条过期日志与截图证据。该操作不可撤销，确认继续？`)) return;
  await withAction("清理过期日志", async () => {
    state.retention = await api("/api/ops/retention/prune", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm_execute: true }),
    });
    state.logs = await api("/api/send-logs");
    state.opsHealth = await api("/api/ops/health");
    renderBackups();
    renderLogs();
    renderOpsHealth();
    toast("过期日志清理完成");
  });
}

async function runBackupDrill(filename) {
  await withAction("恢复演练", async () => {
    const result = await api(`/api/ops/backups/${encodeURIComponent(filename)}/restore-drill`, { method: "POST" });
    state.backupDrills[filename] = result;
    renderBackups();
    toast(result.passed ? `恢复演练通过：${filename}` : `恢复演练失败：${filename}`);
  });
}

// 保存 ARK 云端定位密钥。
$("arkConfigForm").onsubmit = async (event) => {
  event.preventDefault();
  await withAction("保存ARK密钥", async () => {
    const data = Object.fromEntries(new FormData(event.target).entries());
    await api("/api/ark-config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ api_key: data.api_key, endpoint_id: data.endpoint_id || "qwen-vl-plus" }) });
    event.target.reset();
    toast("ARK 密钥已保存并生效");
    await refreshAll();
  });
};

// 手动登记企微会话。
$("wecomForm").onsubmit = async (event) => {
  event.preventDefault();
  await withAction("保存企微会话", async () => {
    const data = Object.fromEntries(new FormData(event.target).entries());
    await api("/api/families", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...data, service_status: "企微待同步" }),
    });
    event.target.reset();
    toast("企微会话已保存");
    await refreshAll();
    switchTab("wecom");
  });
};

// 网页通讯登录。
$("loginForm").onsubmit = async (event) => {
  event.preventDefault();
  await withAction("登录", async () => {
    const data = Object.fromEntries(new FormData(event.target).entries());
    const user = await api("/api/test-chat/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    state.currentUser = user;
    localStorage.setItem("chatUser", JSON.stringify(user));
    toast(`已登录：${user.display_name}`);
    await refreshAll();
    switchTab("webChat");
  });
};

// 网页通讯注册。
$("registerForm").onsubmit = async (event) => {
  event.preventDefault();
  await withAction("注册账号", async () => {
    const data = Object.fromEntries(new FormData(event.target).entries());
    await api("/api/test-chat/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    event.target.reset();
    toast("账号已注册");
    await refreshAll();
  });
};

// 网页通讯发送消息。
$("chatForm").onsubmit = async (event) => {
  event.preventDefault();
  await withAction("发送消息", async () => {
    if (!state.currentUser) return toast("请先登录账号");
    if (!state.selectedChatFamilyId) return toast("请先选择会话");
    const data = Object.fromEntries(new FormData(event.target).entries());
    await api("/api/test-chat/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ family_id: state.selectedChatFamilyId, username: state.currentUser.username, content: data.content }),
    });
    event.target.reset();
    toast("消息已发送并写入数据库");
    await refreshAll();
    switchTab("webChat");
  });
};

// 生成一批账号和真实感聊天记录。
$("seedChatBtn").onclick = async () => {
  await withAction("生成模拟对话", async () => {
    const res = await api("/api/test-chat/seed", { method: "POST" });
    toast(`已生成 ${res.families} 个家庭、${res.messages} 条对话`);
    await refreshAll();
    switchTab("webChat");
  });
};

// 对当前会话快速生成可审核回复，只调用一次大模型。
$("chatReplyBtn").onclick = async () => {
  await withAction("快速生成回复", async () => {
    if (!state.selectedChatFamilyId) return toast("请先选择会话");
    const res = await api("/api/test-chat/reply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ family_id: state.selectedChatFamilyId, create_task: true }),
    });
    toast(`回复已生成：${Math.round((res.elapsed_ms || 0) / 1000)} 秒`);
    await refreshAll();
    switchTab("webChat");
  });
};

// 对当前会话完整生成画像和回复，适合首次建档或阶段复盘。
$("chatFullAiBtn").onclick = async () => {
  await withAction("完整分析", async () => {
    if (!state.selectedChatFamilyId) return toast("请先选择会话");
    const res = await api("/api/test-chat/ai", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ family_id: state.selectedChatFamilyId, create_task: true }),
    });
    toast(`画像和回复已生成：${Math.round((res.elapsed_ms || 0) / 1000)} 秒`);
    await refreshAll();
    switchTab("webChat");
  });
};

// 从已批准的报告创建任务。
$("taskFromReportsBtn").onclick = async () => {
  await withAction("从周报创建任务", async () => {
    const res = await api("/api/send-tasks/from-approved-reports", { method: "POST" });
    toast(`创建周报发送任务 ${res.created} 个`);
    await refreshAll();
  });
};

// 批量审核所有报告。
$("approveAllBtn").onclick = async () => {
  await withAction("批量审核周报", async () => {
    const res = await api("/api/reports/approve-all", { method: "POST" });
    toast(`批量审核 ${res.approved} 份周报`);
    await refreshAll();
  });
};

// 从场景创建任务。
$("taskFromScenesBtn").onclick = async () => {
  await withAction("从场景创建任务", async () => {
    const res = await api("/api/send-tasks/from-scenes", { method: "POST" });
    toast(`创建场景回复任务 ${res.created} 个`);
    await refreshAll();
  });
};

// 发送所有任务到网页通讯会话。
$("sendAllBtn").onclick = async () => {
  await withAction("发送全部任务", async () => {
    const res = await api("/api/send-tasks/web-send-all", { method: "POST" });
    toast(`已发送 ${res.sent} 个任务${res.skipped ? `，跳过 ${res.skipped} 个` : ""}`);
    await refreshAll();
  });
};

// 检查服务健康状态。
window.addEventListener("unhandledrejection", (event) => {
  console.error("[unhandledrejection]", event.reason);
  setActionStatus(`未捕获错误：${event.reason?.message || event.reason}`, "error");
});

window.addEventListener("error", (event) => {
  console.error("[window:error]", event.error || event.message);
  setActionStatus(`页面错误：${event.message}`, "error");
});

api("/health").then(() => $("health").textContent = "本地服务正常").catch(() => $("health").textContent = "服务异常");
refreshAll().catch((err) => toast(`加载失败：${err.message}`));

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
  parentDashboard: {},
  devices: [],
  opsHealth: {},
  backups: [],
  retention: {},
  backupDrills: {},
  arkConfig: {},
  importTemplates: [],
  agentEval: {},
  replyAgentConfig: {},
  templates: [],
  outputs: [],
  accounts: [],
  conversations: [],
  chatMessages: [],
  currentUser: JSON.parse(localStorage.getItem("controlUser") || localStorage.getItem("chatUser") || "null"),
  authStatus: {},
  selectedCampusName: localStorage.getItem("campusFilter") || "",
  selectedCoachName: localStorage.getItem("coachFilter") || "",
  selectedChatFamilyId: "",
  selectedFamilyId: "",
  expandedTaskIds: new Set(),
};

// 把后端返回的 Agent 类型映射成前端展示名称和颜色。
const AGENTS = {
  family_profile: { name: "家庭画像", className: "agent-profile" },
  weekly_report: { name: "AI周报", className: "agent-weekly" },
  ai_reply: { name: "AI回复", className: "agent-reply" },
  checkin_pbl: { name: "打卡/PBL", className: "agent-checkin" },
};

const DEFAULT_REPLY_AGENT_CONFIG = {
  auto_reply_enabled: false,
  auto_create_send_task: true,
  send_mode: "dry_run",
  tone: "standard",
  reply_agent: "ai_reply_agent",
  enabled_agents: ["context_agent", "scene_agent", "reply_agent", "safety_agent"],
  high_risk_policy: "manual",
  skip_recent_hours: 8,
  max_batch: 200,
  available_agents: [
    { key: "context_agent", name: "上下文 Agent", description: "读取家庭画像、最近聊天、周报和话术模板" },
    { key: "scene_agent", name: "场景识别 Agent", description: "识别请假、催打卡、投诉、续费等回复场景" },
    { key: "reply_agent", name: "回复生成 Agent", description: "生成可直接进入发送队列的家长回复" },
    { key: "safety_agent", name: "安全兜底 Agent", description: "命中高风险、投诉、退费等内容时转人工" },
  ],
};

const PENDING_TABS = {
  parentDashboard: "家长画像功能待优化，当前暂不开放选择。",
};

// 统一封装 fetch，减少重复的错误处理代码。
async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (!headers.has("X-Actor")) headers.set("X-Actor", encodeURIComponent(currentActor()));
  if (state.currentUser?.admin_token && !headers.has("Authorization")) headers.set("Authorization", `Bearer ${state.currentUser.admin_token}`);
  if (!state.currentUser?.admin_token && state.currentUser?.parent_token && !headers.has("Authorization")) headers.set("Authorization", `Bearer ${state.currentUser.parent_token}`);
  const res = await fetch(path, { ...options, headers });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function currentActor() {
  if (!state.currentUser) return "控制端";
  return `${state.currentUser.role}:${state.currentUser.display_name || state.currentUser.username}`;
}

function userRoleBadge(user) {
  const labels = { admin: "超管", coach: "陪跑师", readonly: "只读", parent: "家长" };
  const kind = user.role === "admin" ? "danger" : user.role === "coach" ? "ok" : user.role === "readonly" ? "warn" : "";
  return badge(labels[user.role] || user.role, kind);
}

function isControlUser(user = state.currentUser) {
  return ["admin", "coach", "readonly"].includes(user?.role);
}

function isAdminUser(user = state.currentUser) {
  return user?.role === "admin";
}

function safeApi(path, fallback, options = {}) {
  return api(path, options).catch(() => fallback);
}

function saveCurrentUser(user) {
  state.currentUser = user;
  if (user) {
    localStorage.setItem("controlUser", JSON.stringify(user));
    localStorage.setItem("chatUser", JSON.stringify(user));
  } else {
    localStorage.removeItem("controlUser");
    localStorage.removeItem("chatUser");
    localStorage.removeItem(STATE_CACHE_KEY);
  }
  renderAuthState();
}

// 数据快照缓存：刷新页面时先用上次数据立即渲染（stale-while-revalidate），
// 网络返回后静默更新，避免"页面出来了、内容还在转圈"。
const STATE_CACHE_KEY = "stateCacheV1";
const STATE_CACHE_FIELDS = [
  "families", "profiles", "reports", "templates", "tasks", "logs", "outputs",
  "conversations", "todayPriorities", "workbenchOverview", "devices", "auditLogs",
  "serviceQuality", "importTemplates", "replyAgentConfig", "opsHealth", "backups",
  "retention", "accounts", "arkConfig",
];

function saveStateCache() {
  try {
    const data = { __user: state.currentUser?.username || "" };
    STATE_CACHE_FIELDS.forEach((key) => { data[key] = state[key]; });
    localStorage.setItem(STATE_CACHE_KEY, JSON.stringify(data));
  } catch { /* 存储超限或隐私模式时放弃缓存，不影响功能 */ }
}

function loadStateCache() {
  try {
    const data = JSON.parse(localStorage.getItem(STATE_CACHE_KEY) || "null");
    if (!data || data.__user !== (state.currentUser?.username || "")) return false;
    STATE_CACHE_FIELDS.forEach((key) => {
      if (data[key] !== undefined) state[key] = data[key];
    });
    return true;
  } catch {
    return false;
  }
}

// 顶层视图切换：landing 首页介绍 / auth 登录注册 / app 业务系统，三态互斥。
function showView(view) {
  const body = document.body;
  body.classList.remove("view-landing", "view-auth", "view-app");
  body.classList.add("view-" + view);
  if (view !== "app") window.scrollTo(0, 0);
}

function showLanding() {
  showView("landing");
}

// 进入登录/注册页并定位到指定标签。
function showAuth(tab) {
  showView("auth");
  switchAuthTab(tab === "register" ? "register" : "login");
}

// 在登录和注册面板之间切换。
function switchAuthTab(name) {
  const isRegister = name === "register";
  $("authTabLogin")?.classList.toggle("active", !isRegister);
  $("authTabRegister")?.classList.toggle("active", isRegister);
  $("authPanelLogin")?.classList.toggle("active", !isRegister);
  $("authPanelRegister")?.classList.toggle("active", isRegister);
}

// 兼容既有调用：true=退出业务系统回首页，false=进入业务系统。
function setAuthGateVisible(visible) {
  showView(visible ? "landing" : "app");
}

function logoutCurrentUser() {
  saveCurrentUser(null);
  toast("已退出登录");
  setAuthGateVisible(true);
}

function accountSettingsEndpoint() {
  return state.currentUser?.parent_token && !state.currentUser?.admin_token
    ? "/api/parent/auth/me"
    : "/api/admin/auth/me";
}

function openAccountSettings() {
  if (!state.currentUser) {
    toast("请先登录");
    return;
  }
  const form = $("accountSettingsForm");
  if (form) {
    form.username.value = state.currentUser.username || "";
    form.display_name.value = state.currentUser.display_name || "";
    form.current_password.value = "";
    form.new_password.value = "";
  }
  const dialog = $("accountSettingsDialog");
  if (dialog?.showModal) dialog.showModal();
  else if (dialog) dialog.setAttribute("open", "open");
}

function closeAccountSettings() {
  const dialog = $("accountSettingsDialog");
  if (dialog?.close) dialog.close();
  else if (dialog) dialog.removeAttribute("open");
}

function userCampusText(user) {
  const value = Array.isArray(user?.campus_names) ? user.campus_names.join("、") : user?.campus_names;
  return String(value || "").trim();
}

function userInitial(user) {
  const name = String(user?.display_name || user?.username || "访客").trim();
  return name.slice(0, 1).toUpperCase();
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

function renderAuthState() {
  const user = state.currentUser;
  const status = state.authStatus || {};
  const authBar = $("authBar");
  if (authBar) {
    authBar.innerHTML = user
      ? `<span>${userRoleBadge(user)} ${esc(user.display_name || user.username)}</span><button onclick="openAccountSettings()">账号设置</button><button onclick="logoutCurrentUser()">退出</button>`
      : `<button onclick="showAuth('login')">${status.bootstrap_required ? "注册超管" : "登录"}</button>`;
  }
  if ($("landingStatus")) {
    $("landingStatus").textContent = user
      ? `已登录：${user.display_name || user.username}，点击进入控制台。`
      : (status.bootstrap_required
        ? "系统还没有账号，点击“注册账号”创建第一个超管。"
        : "账号已就绪，登录即可进入控制台。");
  }
  if ($("authGateStatus")) {
    $("authGateStatus").textContent = user
      ? `已登录：${user.display_name || user.username}`
      : (status.bootstrap_required ? "检测到首次使用，请先注册超管账号" : "请输入控制端账号后进入系统");
  }
  if ($("authHint")) {
    $("authHint").textContent = status.message || (status.bootstrap_required ? "首次注册账号将自动成为超管" : "请登录控制端账号");
  }
  if ($("adminRegisterRole")) {
    $("adminRegisterRole").disabled = !!status.bootstrap_required;
    $("adminRegisterRole").value = status.bootstrap_required ? "admin" : ($("adminRegisterRole").value || "coach");
  }
  if ($("adminRegisterNote")) {
    $("adminRegisterNote").textContent = status.bootstrap_required
      ? "当前系统还没有控制端账号，本次注册将自动成为超管。"
      : "已有超管后，只有超管登录状态下才能继续创建账号。";
  }
  if ($("authCurrentUser")) {
    $("authCurrentUser").innerHTML = user
      ? `<strong>当前登录：</strong>${userRoleBadge(user)} ${esc(user.display_name || user.username)}<p class="muted">校区范围：${esc(userCampusText(user) || "全部校区")}</p>`
      : emptyState("未登录", "请先登录；如果是首次使用，请在右侧注册第一个超管账号。");
  }
  if ($("sidebarUser")) {
    $("sidebarUser").innerHTML = user
      ? `<div class="sidebar-avatar">${esc(userInitial(user))}</div>
         <div><strong>${esc(user.display_name || user.username)}</strong><small>${userRoleBadge(user)} ${esc(userCampusText(user) || "全部校区")}</small></div>`
      : `<div class="sidebar-avatar">访</div><div><strong>未登录</strong><small>请登录控制端账号</small></div>`;
  }
}

async function refreshAuthStatus() {
  state.authStatus = await api("/api/admin/auth/status");
  renderAuthState();
  return state.authStatus;
}

function displayValue(value, fallback = "未登记") {
  if (value === 0) return "0";
  const text = String(value ?? "").trim();
  return text || fallback;
}

function scopedPath(path, params = {}, { includeCoach = false, includeCampus = true } = {}) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") query.set(key, value);
  });
  if (includeCoach && state.selectedCoachName) query.set("coach_name", state.selectedCoachName);
  if (includeCampus && state.selectedCampusName) query.set("campus_name", state.selectedCampusName);
  const suffix = query.toString();
  return suffix ? `${path}?${suffix}` : path;
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

function sendTaskStatusBadge(status) {
  const labels = {
    pending: "待处理",
    approved: "已审核",
    assigned: "发送中",
    sent: "已发送",
    failed: "发送失败",
    dry_run: "试运行完成（未发送）",
    skipped: "已跳过",
    cancelled: "已取消",
  };
  const kind = status === "sent" || status === "dry_run" ? "ok" : (status === "failed" || status === "assigned" ? "warn" : "");
  return badge(labels[status] || status || "未知", kind);
}

function aiOutputStatusBadge(status) {
  const labels = {
    needs_review: "待人工审核",
    approved: "已保存审核稿",
    task_created: "已建发送任务",
  };
  const kind = status === "needs_review" ? "warn" : (status === "approved" || status === "task_created" ? "ok" : "");
  return badge(labels[status] || status || "未知状态", kind);
}

function aiRiskBadge(riskLevel) {
  const level = riskLevel || "低";
  const kind = level === "高" ? "danger" : (level === "中" ? "warn" : "ok");
  return badge(`风险${level}`, kind);
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

function canManualVerifyLog(log) {
  if (!isAdminUser() || (log.send_mode || "") !== "real_send") return false;
  return log.manual_verify_allowed === true;
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

function taskRetryCell(task) {
  const retry = `重试 ${task.retry_count || 0}/${task.max_retries ?? 2}`;
  const next = task.next_retry_at ? `<p class="muted">下次：${esc(task.next_retry_at)}</p>` : "";
  const alert = task.retry_alert ? badge("需人工告警", "danger") : "";
  const lastError = task.last_error ? `<p class="muted">${esc(task.last_error).slice(0, 80)}</p>` : "";
  return `${badge(retry, task.retry_alert ? "danger" : task.next_retry_at ? "warn" : "")}${alert}${next}${lastError}`;
}

function taskReadinessCell(task) {
  const readiness = task.send_readiness || {};
  const status = readiness.status || "";
  const kind = status === "ready" || status === "done" ? "ok" : (status === "blocked" || status === "review" ? "danger" : "warn");
  const reasons = Array.isArray(readiness.reasons) && readiness.reasons.length
    ? `<p class="muted">${esc(readiness.reasons.join("；")).slice(0, 160)}</p>`
    : "";
  const actions = Array.isArray(readiness.actions) ? readiness.actions : [];
  const buttons = actions.map((action) => {
    if (action.action === "queue_conversation_check") {
      const label = action.existing_task_id ? `证明校验中 #${action.existing_task_id}` : (action.label || "刷新会话证明");
      const disabled = action.available === false ? "disabled" : "";
      return `<button ${disabled} onclick="queueConversationProof('${esc(action.device_id)}', '${esc(action.target_name)}', '${esc(action.family_id || manualTaskFamilyId(action.target_name))}', '任务发送前刷新证明')">${esc(label)}</button>`;
    }
    if (action.action === "enable_real_send" && isAdminUser()) {
      const disabled = action.available === false ? "disabled" : "";
      return `<button class="danger-action" ${disabled} onclick="toggleDeviceRealSend('${esc(action.device_id)}', true)">${esc(action.label || "开启设备真发")}</button>`;
    }
    return "";
  }).filter(Boolean).join("");
  const actionHtml = buttons ? `<div class="cell-actions">${buttons}</div>` : "";
  return `${badge(readiness.label || "未检查", kind)}${reasons}${actionHtml}`;
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

function toggleTaskDetails(id, open) {
  const key = Number(id);
  if (open) state.expandedTaskIds.add(key);
  else state.expandedTaskIds.delete(key);
}

function taskActionButtons(task) {
  return `
    <div class="cell-actions">
      ${taskCan(task, "edit") || taskCan(task, "confirm_real_send") ? `<button onclick="saveTask(${task.id})">保存/审核</button>` : ""}
      ${taskCan(task, "dry_run") ? `<button title="只定位、粘贴并清空，不按发送键" onclick="queueTaskDryRun(${task.id})">企微试运行（不发送）</button>` : ""}
      ${taskCan(task, "confirm_real_send") ? `<button class="danger-action" title="确认后加入企业微信真实发送队列" onclick="queueTaskRealSend(${task.id})">企微真实发送</button>` : ""}
      ${taskCan(task, "retry") ? `<button onclick="retryTask(${task.id})">失败重试</button>` : ""}
      ${taskCan(task, "web_send") ? `<button onclick="sendTask(${task.id})">网页发送</button>` : ""}
      ${taskCan(task, "cancel") ? `<button onclick="cancelTask(${task.id})">取消</button>` : ""}
      ${taskAllowedOperations(task).length === 1 ? '<span class="muted">仅可查看</span>' : ""}
    </div>
  `;
}

function taskReviewCard(task) {
  const expanded = state.expandedTaskIds.has(Number(task.id)) ? "open" : "";
  const preview = (task.content || "").trim().replace(/\s+/g, " ").slice(0, 140);
  const readiness = task.send_readiness || {};
  const readinessKind = readiness.status === "ready" || readiness.status === "done" ? "ok" : (readiness.status === "blocked" || readiness.status === "review" ? "danger" : "warn");
  const retryAlert = task.retry_alert ? badge("需人工告警", "danger") : "";
  return `
    <details class="task-card task-review-card" ${expanded} ontoggle="toggleTaskDetails(${task.id}, this.open)">
      <summary class="task-card-summary">
        <span class="task-summary-id">#${esc(task.id)}</span>
        <div class="task-summary-target">
          <strong>${esc(task.target_name || "未填写目标")}</strong>
          <small>${esc(familyName(task.family_id))} · ${esc(task.scene || "未命名场景")}</small>
        </div>
        <p class="task-summary-preview" title="${esc(task.content || "")}">${esc(preview || "暂无内容")}</p>
        <div class="task-summary-status">
          ${sendTaskStatusBadge(task.status)}
          ${sendModeBadge(task.send_mode || "dry_run")}
        </div>
        <div class="task-summary-readiness">
          ${readiness.label ? badge(readiness.label, readinessKind) : ""}
          ${retryAlert}
        </div>
        <span class="task-expand-label"></span>
      </summary>
      <div class="task-card-detail">
        <div class="task-detail-table">
          <section><span>操作分层</span>${taskOperationBadges(task)}</section>
          <section><span>重试/告警</span>${taskRetryCell(task)}</section>
          <section><span>发送准备</span>${taskReadinessCell(task)}</section>
          <section><span>发送设备</span>${deviceSelect(task)}</section>
          <section><span>企微模式</span>${sendModeSelect(task)}</section>
        </div>
        <label class="task-content-editor">
          <span>最终内容</span>
          <textarea id="task-${task.id}" ${taskCan(task, "edit") ? "" : "readonly"}>${esc(task.content)}</textarea>
        </label>
        ${taskActionButtons(task)}
      </div>
    </details>
  `;
}

function manualTaskFamilyId(target) {
  const clean = String(target || "").trim().replace(/[^\u4e00-\u9fa5a-zA-Z0-9_-]+/g, "_").slice(0, 48);
  return `MANUAL_${clean || "TASK"}`.slice(0, 64);
}

function deviceConversationList(device) {
  if (Array.isArray(device.conversation_list)) return device.conversation_list;
  try {
    const rows = JSON.parse(device.conversations || "[]");
    return Array.isArray(rows) ? rows.map((item) => String(item || "").trim()).filter(Boolean) : [];
  } catch {
    return [];
  }
}

function devicesForTarget(target) {
  const clean = String(target || "").trim();
  if (!clean) return [];
  return state.devices.filter((device) => deviceConversationList(device).includes(clean));
}

function syncManualTaskDeviceSelection(force = false) {
  const target = $("manualTaskTarget")?.value || "";
  const mode = $("manualTaskMode")?.value || "dry_run";
  const select = $("manualTaskDevice");
  const hint = $("manualTaskDeviceHint");
  if (!select || !hint) return;
  if (mode !== "real_send") {
    hint.textContent = "试运行可以自动领取；真实发送会按目标会话绑定到唯一负责设备。";
    return;
  }
  const candidates = devicesForTarget(target);
  if (candidates.length === 1) {
    const device = candidates[0];
    if (force || !select.value || select.value !== device.device_id) select.value = device.device_id;
    hint.textContent = `已按目标「${target}」自动选择人员设备：${device.device_id}${device.name ? ` · ${device.name}` : ""}`;
  } else if (candidates.length > 1) {
    hint.textContent = `目标「${target}」绑定了多个设备：${candidates.map((item) => item.device_id).join("、")}，请明确选择发送人。`;
  } else if (target.trim()) {
    hint.textContent = `目标「${target}」还没有绑定负责设备；请先到设备监控给对应人员设备配置负责会话。`;
  } else {
    hint.textContent = "填写目标后会自动匹配唯一负责设备；真实发送不会随机派发。";
  }
}

function renderManualTaskForm() {
  const select = $("manualTaskDevice");
  if (!select) return;
  const current = select.value || "";
  const options = ['<option value="">自动领取（仅试运行）</option>'].concat(
    state.devices.map((device) => {
      const proof = device.conversation_proof_label || "0 个会话24小时内可读";
      const health = `${device.online ? "在线" : "离线"} / 企微${device.wecom_ok === "Y" ? "正常" : (device.wecom_ok || "未知")} / ${device.allow_real_send ? "可真发" : "仅试运行"} / ${proof}`;
      const label = `${device.device_id}${device.name ? ` · ${device.name}` : ""} · ${health}`;
      return `<option value="${esc(device.device_id)}" ${current === device.device_id ? "selected" : ""}>${esc(label)}</option>`;
    })
  );
  select.innerHTML = options.join("");
  syncManualTaskDeviceSelection(false);
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
  "parentDashboardSummary",
  "parentDashboardContent",
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
  "replyConfigPanel",
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

// 家庭管理三个子页共用侧边栏「家庭管理」入口。
const TAB_OWNERS = { familyDetailPanel: "families", profiles: "families" };
const TAB_TITLES = { families: "家庭管理", familyDetailPanel: "家庭档案", profiles: "AI画像" };

function switchTab(tabId) {
  if (PENDING_TABS[tabId]) {
    toast(PENDING_TABS[tabId]);
    setActionStatus(PENDING_TABS[tabId]);
    return false;
  }
  localStorage.setItem("activeTab", tabId);
  const ownerTab = TAB_OWNERS[tabId] || tabId;
  document.querySelectorAll(".sidebar button").forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === ownerTab));
  document.querySelectorAll(".panel").forEach((panel) => panel.classList.toggle("active", panel.id === tabId));
  document.querySelectorAll(".subtabs button[data-tab]").forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === tabId));
  const active = document.querySelector(`.sidebar button[data-tab="${ownerTab}"]`);
  $("pageTitle").textContent = TAB_TITLES[tabId] || (active ? (active.dataset.title || active.textContent.trim()) : "工作台");
  // Agent 评测很重，只在进入系统设置页时惰性运行，不占刷新关键路径。
  if (tabId === "templates") refreshAgentEval();
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
  return true;
}

// 切换面板内的子页签分组（系统设置、设备监控等页内二级导航）。
function switchPanelGroup(panelId, key) {
  const panel = document.getElementById(panelId);
  if (!panel) return;
  panel.querySelectorAll(".panel-group").forEach((group) => group.classList.toggle("active", group.dataset.group === key));
  panel.querySelectorAll(".subtabs button[data-group]").forEach((btn) => btn.classList.toggle("active", btn.dataset.group === key));
}

// 陪跑会话左侧列表按家长名/编号/最近消息过滤。
function filterChatList(keyword) {
  state.chatFilter = String(keyword || "");
  renderWebChat();
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
// 指标图标用内联 SVG，避免 Unicode 字形在不同系统下渲染不一致。
const metricIcon = (paths) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;

const METRIC_ICONS = {
  alert: metricIcon('<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>'),
  smile: metricIcon('<circle cx="12" cy="12" r="10"/><path d="M8 14s1.5 2 4 2 4-2 4-2"/><line x1="9" y1="9" x2="9.01" y2="9"/><line x1="15" y1="9" x2="15.01" y2="9"/>'),
  todo: metricIcon('<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/><polyline points="9 13 11 15 15 11"/>'),
  chat: metricIcon('<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>'),
  review: metricIcon('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><polyline points="9 15 11 17 15 13"/>'),
  send: metricIcon('<line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>'),
  clock: metricIcon('<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>'),
  frown: metricIcon('<circle cx="12" cy="12" r="10"/><path d="M16 16s-1.5-2-4-2-4 2-4 2"/><line x1="9" y1="9" x2="9.01" y2="9"/><line x1="15" y1="9" x2="15.01" y2="9"/>'),
  flag: metricIcon('<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/>'),
  heart: metricIcon('<path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/>'),
  calendarX: metricIcon('<rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/><line x1="10" y1="14" x2="14" y2="18"/><line x1="14" y1="14" x2="10" y2="18"/>'),
  shield: metricIcon('<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>'),
};

function renderKpis() {
  const pendingTasks = state.tasks.filter((task) => task.status === "pending").length;
  const reviewOutputs = state.outputs.filter((item) => item.status === "needs_review").length;
  const reviewReports = state.reports.filter((report) => report.status !== "approved").length;
  const highRisk = state.profiles.filter((item) => (item.service_risks || "").includes("退费") || (item.service_risks || "").includes("投诉")).length;
  const todoCount = (state.workbenchOverview?.todos?.categories || []).reduce((sum, item) => sum + Number(item.count || 0), 0);
  const totalMessages = state.families.reduce((sum, family) => sum + Number(family.message_count || 0), 0);
  const satisfactionValues = state.profiles.map((profile) => {
    const raw = String(profile.satisfaction_level || "").trim();
    const numeric = Number.parseFloat(raw);
    if (!Number.isNaN(numeric)) return numeric > 5 ? numeric / 20 : numeric;
    if (raw.includes("低") || raw.includes("不满")) return 2.8;
    if (raw.includes("高") || raw.includes("满意")) return 4.8;
    if (raw.includes("中") || raw.includes("一般")) return 4.0;
    return null;
  }).filter((value) => value !== null);
  const avgSatisfaction = satisfactionValues.length
    ? (satisfactionValues.reduce((sum, value) => sum + value, 0) / satisfactionValues.length).toFixed(1)
    : "—";
  const metrics = [
    { label: "高风险家庭", value: highRisk, unit: "户", hint: highRisk ? `今日需优先处理 ${highRisk}` : "当前无高风险", icon: METRIC_ICONS.alert, tone: highRisk ? "danger" : "ok" },
    { label: "服务满意度", value: avgSatisfaction, unit: "分", hint: satisfactionValues.length ? "基于画像评级估算" : "生成画像后统计", icon: METRIC_ICONS.smile, tone: "green" },
    { label: "待跟进事项", value: todoCount || state.todayPriorities.length, unit: "项", hint: "PBL/请假/负面反馈", icon: METRIC_ICONS.todo, tone: "blue" },
    { label: "本周沟通频次", value: totalMessages, unit: "次", hint: "已归档聊天记录", icon: METRIC_ICONS.chat, tone: "purple" },
    { label: "待审核内容", value: reviewOutputs + reviewReports, unit: "条", hint: "AI 输出与周报", icon: METRIC_ICONS.review, tone: "orange" },
    { label: "待发送任务", value: pendingTasks, unit: "条", hint: "需审核后触达", icon: METRIC_ICONS.send, tone: "sky" },
  ];
  $("kpis").innerHTML = metrics.map((item) => `
    <article class="kpi metric-card metric-${esc(item.tone)}">
      <div class="metric-icon">${item.icon}</div>
      <div>
        <span>${esc(item.label)}</span>
        <strong>${esc(item.value)}<small>${esc(item.unit)}</small></strong>
        <em>${esc(item.hint)}</em>
      </div>
    </article>
  `).join("");
}

function renderInsightCards() {
  const el = $("insightCards");
  if (!el) return;
  const noContact = state.families.filter((family) => !Number(family.message_count || 0)).length;
  const negative = state.profiles.filter((profile) => /(退费|投诉|不满意|没效果|风险)/.test(profile.service_risks || "")).length;
  const dropout = state.todayPriorities.filter((item) => item.level === "高" || (item.reasons || []).join("").includes("掉队")).length;
  const styles = new Set(state.profiles.map((profile) => profile.communication_style).filter(Boolean));
  const absence = (state.workbenchOverview?.todos?.categories || [])
    .filter((category) => /请假|补课|缺课/.test(category.label || ""))
    .reduce((sum, category) => sum + Number(category.count || 0), 0);
  const riskSignals = state.profiles.filter((profile) => (profile.service_risks || "").trim()).length;
  const cards = [
    ["长期未沟通", noContact, "超过阈值需主动触达", METRIC_ICONS.clock, "blue"],
    ["家长负面反馈", negative, "投诉/退费/不满信号", METRIC_ICONS.frown, "danger"],
    ["学生掉队提醒", dropout, "高优先级家庭", METRIC_ICONS.flag, "green"],
    ["家长沟通风格", styles.size || "—", "画像中已识别类型", METRIC_ICONS.heart, "orange"],
    ["缺课/请假", absence, "请假补课待确认", METRIC_ICONS.calendarX, "purple"],
    ["家长风险信号", riskSignals, "需人工复核", METRIC_ICONS.shield, "danger"],
  ];
  el.innerHTML = cards.map(([label, value, hint, icon, tone]) => `
    <article class="insight-card insight-${esc(tone)}">
      <div class="metric-icon">${icon}</div>
      <div>
        <strong>${esc(label)}</strong>
        <span>${esc(value)} <small>${esc(hint)}</small></span>
        <button onclick="${label === "缺课/请假" ? "switchTab('checkins')" : label.includes("风险") ? "switchTab('profiles')" : "switchTab('families')"}">查看全部</button>
      </div>
    </article>
  `).join("");
}

function renderWorkSuggestions() {
  const el = $("workSuggestions");
  if (!el) return;
  const pendingTasks = state.tasks.filter((task) => task.status === "pending").length;
  const reviewOutputs = state.outputs.filter((item) => item.status === "needs_review").length;
  const highRisk = state.profiles.filter((item) => /(退费|投诉|不满意|没效果)/.test(item.service_risks || "")).length;
  const suggestions = [
    highRisk ? `优先处理 ${highRisk} 个高风险家庭，先查时间线再决定是否主管介入。` : "保持当前风险巡检节奏，重点关注新导入家庭。",
    pendingTasks ? `审核 ${pendingTasks} 条待发送任务，真实企微发送前先完成试运行。` : "今日暂无待发送任务，可先生成关键家庭回复草稿。",
    reviewOutputs ? `复核 ${reviewOutputs} 条 AI 输出，避免未审核内容直接进入发送队列。` : "AI 输出已清空，可补充生成周报或家庭画像。",
    "定期观察学习报告、增强家长信任感。",
    "关注高风险家庭动态，提前预防风险。",
  ];
  el.innerHTML = `<strong>工作建议</strong>${suggestions.map((item) => `<span>${esc(item)}</span>`).join("")}`;
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

function renderCampusFilters() {
  const campuses = [...new Set(state.families.map((family) => (family.campus_name || "").trim()).filter(Boolean))].sort();
  if (state.selectedCampusName && !campuses.includes(state.selectedCampusName)) campuses.unshift(state.selectedCampusName);
  const options = ['<option value="">全部校区</option>'].concat(
    campuses.map((name) => `<option value="${esc(name)}" ${name === state.selectedCampusName ? "selected" : ""}>${esc(name)}</option>`)
  );
  ["campusFilter", "adminCampusFilter"].forEach((id) => {
    const el = $(id);
    if (el) el.innerHTML = options.join("");
  });
}

async function setCoachFilter(coachName) {
  state.selectedCoachName = coachName || "";
  localStorage.setItem("coachFilter", state.selectedCoachName);
  await refreshWorkbenchOverview();
}

async function setCampusFilter(campusName) {
  state.selectedCampusName = campusName || "";
  localStorage.setItem("campusFilter", state.selectedCampusName);
  renderCampusFilters();
  await Promise.all([refreshWorkbenchOverview(), refreshTodayPriorities(), refreshServiceQuality()]);
}

async function refreshWorkbenchOverview() {
  state.workbenchOverview = await api(scopedPath("/api/workbench/overview", { limit: 8 }, { includeCoach: true }));
  renderWorkbenchOverview();
}

async function refreshTodayPriorities() {
  state.todayPriorities = await api(scopedPath("/api/workbench/today-priorities", { limit: 12 }));
  renderPriorityList();
}

async function refreshServiceQuality() {
  state.serviceQuality = await api(scopedPath("/api/admin/service-quality"));
  renderServiceQuality();
}

async function refreshParentDashboard() {
  state.parentDashboard = await api("/api/parent/dashboard");
  renderParentDashboard();
}

async function ackParentReport(reportId) {
  return withAction("签收周报", async () => {
    await api(`/api/parent/reports/${reportId}/ack`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note: "家长已在看板确认查看" }),
    });
    toast("周报已签收");
    await refreshParentDashboard();
  });
}

async function submitParentReportFeedback(reportId) {
  return withAction("提交周报反馈", async () => {
    const score = Number($(`parent-feedback-score-${reportId}`)?.value || 5);
    const note = $(`parent-feedback-note-${reportId}`)?.value || "";
    await api(`/api/parent/reports/${reportId}/feedback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ score, note }),
    });
    toast(score <= 2 ? "反馈已提交，陪跑师会优先跟进" : "反馈已提交");
    await refreshParentDashboard();
  });
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
  renderCampusFilters();
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
    ["校区", totals.campus_count || 0],
    ["家庭数", totals.family_count || 0],
    ["风险家庭", totals.risk_family_count || 0],
    ["发送完成率", percent(totals.send_completion_rate)],
  ].map(([label, value]) => `<article class="summary"><span>${esc(label)}</span><strong>${esc(value)}</strong></article>`).join("");
  tableEl.innerHTML = table([
    { label: "陪跑师", key: "coach_name" },
    { label: "校区", render: (r) => esc((r.campus_names || []).join("、") || "未分配") },
    { label: "家庭", key: "family_count" },
    { label: "风险/跟进", render: (r) => `${badge(`风险 ${r.risk_family_count}`, r.risk_family_count ? "danger" : "ok")} ${badge(`跟进 ${r.followup_family_count}`, r.followup_family_count ? "warn" : "ok")}` },
    { label: "续报/结课", render: (r) => `${esc(r.renewal_family_count)} / ${esc(r.closed_family_count)}` },
    { label: "待审核", render: (r) => `AI ${esc(r.review_output_count)} · 周报 ${esc(r.review_report_count)}` },
    { label: "待发送", key: "pending_task_count" },
    { label: "发送完成率", render: (r) => `${badge(percent(r.send_completion_rate), r.send_failure_rate ? "warn" : "ok")} <span class="muted">失败 ${percent(r.send_failure_rate)}</span>` },
    { label: "风险家庭", render: (r) => (r.risk_families || []).map((family) => `<button onclick="setSelectedFamily('${esc(family.family_id)}')">${esc(family.family_name)}</button>`).join("") || "—" },
  ], state.serviceQuality?.coaches || []);
}

function renderParentDashboard() {
  const summaryEl = $("parentDashboardSummary");
  const contentEl = $("parentDashboardContent");
  if (!summaryEl || !contentEl) return;
  const data = state.parentDashboard || {};
  const family = data.family || {};
  const progress = data.progress || {};
  const report = data.weekly_report;
  const profile = data.profile || {};
  if (!state.currentUser?.parent_token) {
    summaryEl.innerHTML = "";
    contentEl.innerHTML = emptyState("请先登录家长账号", "在陪跑会话页登录家长账号后，这里会展示该家庭的阶段进度和已审核周报。");
    return;
  }
  summaryEl.innerHTML = [
    ["课程阶段", displayValue(family.course_stage, "未登记")],
    ["Unit 进度", displayValue(family.unit_progress, "未登记")],
    ["打卡记录", progress.checkin_count || 0],
    ["PBL 次数", family.pbl_count || 0],
    ["周报状态", report ? displayValue(report.send_status, "已审核") : "待陪跑师审核"],
    ["家长签收", report ? (report.parent_ack_at ? "已签收" : "待签收") : "暂无周报"],
    ["周报反馈", report ? (report.parent_feedback_score ? `${report.parent_feedback_score}/5` : "待反馈") : "暂无周报"],
  ].map(([label, value]) => `<article class="summary"><span>${esc(label)}</span><strong>${esc(value)}</strong></article>`).join("");
  contentEl.innerHTML = `
    <section>
      <article class="detail-card">
        ${badge("家庭进度", "ok")}
        <h3>${esc(family.parent_nickname || family.family_id || "我的家庭")}</h3>
        <p class="muted">${esc(family.child_grade || "未知年级")} · ${esc(family.campus_name || "未分配校区")} · ${esc(family.coach_name || "未分配陪跑师")}</p>
        ${stageProfile(family)}
        <p><strong>下一里程碑</strong> ${esc(displayValue(family.next_milestone || progress.suggested_next_action, "陪跑师会在周报或群内同步下一步安排"))}</p>
        <p><strong>孩子状态摘要</strong> ${esc(displayValue(profile.child_summary, "暂无画像摘要"))}</p>
        <p><strong>建议配合</strong> ${esc(displayValue(profile.suggested_actions || report?.teacher_suggestion, "保持当前沟通节奏"))}</p>
      </article>
    </section>
    <section>
      <article class="detail-card">
        ${badge("已审核周报")}
        <h3>${esc(report?.week_label || "暂无可见周报")}</h3>
        ${report ? `
          <p><strong>整体状态</strong>${esc(report.overall_state || "未填写")}</p>
          <p><strong>主要变化</strong>${esc(report.main_changes || "未填写")}</p>
          <p><strong>家长关注</strong>${esc(report.parent_focus || "未填写")}</p>
          <p><strong>下步建议</strong>${esc(report.teacher_suggestion || "未填写")}</p>
          <pre>${esc(report.final_text || "")}</pre>
          <p class="muted">${report.parent_ack_at ? `已于 ${esc(report.parent_ack_at)} 签收` : "阅读后请点击签收，方便陪跑师确认家长已看到周报。"}</p>
          ${report.parent_ack_at ? "" : `<button onclick="ackParentReport(${report.id})">我已查看周报</button>`}
          <div class="compact-form">
            <select id="parent-feedback-score-${report.id}">
              ${[5, 4, 3, 2, 1].map((score) => `<option value="${score}" ${Number(report.parent_feedback_score || 5) === score ? "selected" : ""}>${score}分 · ${score >= 4 ? "满意" : score === 3 ? "一般" : "需跟进"}</option>`).join("")}
            </select>
            <textarea id="parent-feedback-note-${report.id}" placeholder="可选：说说本周周报或服务感受">${esc(report.parent_feedback_note || "")}</textarea>
            <button onclick="submitParentReportFeedback(${report.id})">${report.parent_feedback_at ? "更新反馈" : "提交反馈"}</button>
            ${report.parent_feedback_at ? `<p class="muted">已反馈：${esc(report.parent_feedback_score)}/5 · ${esc(report.parent_feedback_at)}</p>` : ""}
          </div>
        ` : emptyState("周报待审核", "陪跑师审核通过后，家长端才会展示正式周报。")}
      </article>
      <article class="detail-card">
        ${badge("最近沟通")}
        <div class="stack">
          ${(data.recent_messages || []).map((msg) => `
            <div class="timeline-item">
              <strong>${esc(msg.speaker || "未知")}</strong>
              <p>${esc(msg.content || "")}</p>
              <span class="muted">${esc(msg.message_time || "")}${msg.checkin_status ? ` · ${esc(msg.checkin_status)}` : ""}</span>
            </div>
          `).join("") || emptyState("暂无沟通记录", "陪跑师同步或家长发送消息后，这里会出现最近沟通。")}
        </div>
      </article>
    </section>
  `;
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

function textPreview(value, limit = 96) {
  const text = String(value || "").trim().replace(/\s+/g, " ");
  return text.length > limit ? `${text.slice(0, limit)}...` : text;
}

function currentReplyAgentConfig() {
  const raw = state.replyAgentConfig || {};
  return {
    ...DEFAULT_REPLY_AGENT_CONFIG,
    ...raw,
    available_agents: raw.available_agents || DEFAULT_REPLY_AGENT_CONFIG.available_agents,
    enabled_agents: Array.isArray(raw.enabled_agents) ? raw.enabled_agents : DEFAULT_REPLY_AGENT_CONFIG.enabled_agents,
  };
}

function renderReplyAgentConfig(replyOutputs = []) {
  const el = $("replyConfigPanel");
  if (!el) return;
  const cfg = currentReplyAgentConfig();
  const enabled = new Set(cfg.enabled_agents || []);
  const autoStatus = $("replyAutoStatus");
  if (autoStatus) {
    autoStatus.className = `badge ${cfg.auto_reply_enabled ? "ok" : ""}`;
    autoStatus.textContent = cfg.auto_reply_enabled ? "自动回复已开启" : "自动回复已关闭";
  }
  const pendingCount = replyOutputs.filter((item) => item.status === "needs_review").length;
  const taskCount = replyOutputs.filter((item) => item.status === "task_created").length;
  const agentCards = (cfg.available_agents || []).map((agent) => `
    <label class="reply-agent-card ${enabled.has(agent.key) ? "active" : ""}">
      <input type="checkbox" name="enabled_agents" value="${esc(agent.key)}" ${enabled.has(agent.key) ? "checked" : ""}>
      <strong>${esc(agent.name)}</strong>
      <span>${esc(agent.description || "")}</span>
    </label>
  `).join("");
  el.innerHTML = `
    <form id="replyAgentConfigForm" class="reply-config-form" onsubmit="saveReplyAgentConfig(event)">
      <section class="reply-switch-card ${cfg.auto_reply_enabled ? "enabled" : ""}">
        <div>
          <span class="eyebrow">总开关</span>
          <h3>自动 AI 回复</h3>
          <p>开启后，企微/RPA 同步到新的家长消息时自动运行回复 Agent；低风险内容不再逐条人工审核。</p>
        </div>
        <label class="switch-control">
          <input type="checkbox" name="auto_reply_enabled" ${cfg.auto_reply_enabled ? "checked" : ""}>
          <span></span>
        </label>
      </section>
      <section class="reply-config-card">
        <div class="section-head compact-head">
          <div>
            <h3>回复策略</h3>
            <span class="muted">控制回复风格、去重窗口和是否自动进入发送任务。</span>
          </div>
        </div>
        <div class="reply-config-fields">
          <label>回复 Agent
            <select name="reply_agent">
              <option value="ai_reply_agent" ${cfg.reply_agent === "ai_reply_agent" ? "selected" : ""}>AI回复 Agent（完整上下文）</option>
              <option value="quick_reply_agent" ${cfg.reply_agent === "quick_reply_agent" ? "selected" : ""}>快速回复 Agent（轻量）</option>
            </select>
          </label>
          <label>回复语气
            <select name="tone">
              <option value="standard" ${cfg.tone === "standard" ? "selected" : ""}>标准</option>
              <option value="gentle" ${cfg.tone === "gentle" ? "selected" : ""}>更温和</option>
              <option value="short" ${cfg.tone === "short" ? "selected" : ""}>更简短</option>
            </select>
          </label>
          <label>发送模式
            <select name="send_mode">
              <option value="dry_run" ${cfg.send_mode === "dry_run" ? "selected" : ""}>试运行队列</option>
              <option value="real_send" ${cfg.send_mode === "real_send" ? "selected" : ""}>真实发送队列</option>
            </select>
          </label>
          <label>去重窗口（小时）
            <input name="skip_recent_hours" type="number" min="0" max="168" value="${esc(cfg.skip_recent_hours)}">
          </label>
          <label>单批上限
            <input name="max_batch" type="number" min="1" max="500" value="${esc(cfg.max_batch)}">
          </label>
          <label>高风险策略
            <select name="high_risk_policy">
              <option value="manual" ${cfg.high_risk_policy === "manual" ? "selected" : ""}>转人工，不自动建任务</option>
              <option value="create_task" ${cfg.high_risk_policy === "create_task" ? "selected" : ""}>尝试建任务（安全词仍会拦截）</option>
            </select>
          </label>
        </div>
        <label class="inline-check">
          <input type="checkbox" name="auto_create_send_task" ${cfg.auto_create_send_task ? "checked" : ""}>
          自动把低风险回复加入发送任务
        </label>
      </section>
      <section class="reply-config-card">
        <div class="section-head compact-head">
          <div>
            <h3>参与回复的 Agents</h3>
            <span class="muted">这里配置链路能力，而不是逐条审核 AI 文案。</span>
          </div>
        </div>
        <div class="reply-agent-grid">${agentCards}</div>
      </section>
      <section class="reply-config-card reply-config-status">
        <h3>当前运行状态</h3>
        <p>${cfg.auto_reply_enabled ? "新消息会触发自动回复链路。" : "自动回复已关闭，只保留手动生成和历史记录。"}</p>
        <div>
          ${badge(cfg.send_mode === "real_send" ? "真实发送队列" : "试运行队列", cfg.send_mode === "real_send" ? "warn" : "ok")}
          ${badge(cfg.auto_create_send_task ? "自动建任务" : "只生成记录", cfg.auto_create_send_task ? "ok" : "")}
          ${badge(`待人工 ${pendingCount}`, pendingCount ? "warn" : "ok")}
          ${badge(`已建任务 ${taskCount}`, taskCount ? "ok" : "")}
        </div>
        <button type="submit">保存自动回复配置</button>
      </section>
    </form>
  `;
}

function renderReplyMetrics(replyOutputs) {
  const el = $("replyMetrics");
  if (!el) return;
  const cfg = currentReplyAgentConfig();
  const pendingCount = replyOutputs.filter((item) => item.status === "needs_review").length;
  const taskCount = replyOutputs.filter((item) => item.status === "task_created").length;
  const humanCount = replyOutputs.filter((item) => item.risk_level === "高" || item.need_human_review === "Y").length;
  const familyCount = new Set(replyOutputs.map((item) => item.family_id).filter(Boolean)).size;
  const cards = [
    ["自动回复", cfg.auto_reply_enabled ? "开" : "关", cfg.auto_reply_enabled ? "新消息自动触发" : "仅保留手动生成"],
    ["自动建任务", cfg.auto_create_send_task ? "开" : "关", cfg.send_mode === "real_send" ? "真实发送队列" : "试运行队列"],
    ["需人工关注", humanCount || pendingCount, "高风险/安全拦截"],
    ["覆盖家庭", familyCount, `最近 ${replyOutputs.length} 条回复`],
  ];
  el.innerHTML = cards.map(([label, value, detail]) => `
    <article class="summary reply-summary-card">
      <span>${esc(label)}</span>
      <strong>${esc(value)}</strong>
      <small>${esc(detail)}</small>
    </article>
  `).join("");
}

function replyOutputSort(a, b) {
  const order = { needs_review: 0, approved: 1, task_created: 2 };
  const statusDiff = (order[a.status] ?? 9) - (order[b.status] ?? 9);
  if (statusDiff) return statusDiff;
  return Number(b.id || 0) - Number(a.id || 0);
}

function replyRecordCard(output) {
  const content = output.edited_output || output.display_text || "";
  const preview = textPreview(content || "暂无回复内容", 120);
  const source = output.source || "AI回复";
  const createdAt = output.created_at || output.updated_at || "";
  return `
    <details class="reply-draft-card reply-record-card">
      <summary class="reply-draft-summary">
        <div class="reply-draft-main">
          <div class="reply-draft-meta">
            ${aiOutputStatusBadge(output.status)}
            ${aiRiskBadge(output.risk_level)}
            ${output.need_human_review === "Y" ? badge("需人工", "warn") : badge("可自动", "ok")}
            <strong>${esc(familyName(output.family_id))}</strong>
          </div>
        </div>
        <p class="reply-draft-preview" title="${esc(content || "")}">${esc(preview)}</p>
        <small>${esc(createdAt)}</small>
        <span class="reply-expand-label"></span>
      </summary>
      <div class="reply-draft-body">
        <div class="reply-draft-info">
          <section><span>来源</span><strong>${esc(source)}</strong></section>
          <section><span>家庭编号</span><strong>${esc(output.family_id)}</strong></section>
          <section><span>输出编号</span><strong>#${esc(output.id)}</strong></section>
        </div>
        <pre>${esc(content)}</pre>
        <details class="reply-evidence">
          <summary>证据链与原始 JSON</summary>
          ${evidenceView(output)}
          <pre>${esc(output.raw_json)}</pre>
        </details>
      </div>
    </details>
  `;
}

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
    { label: "校区", render: (r) => esc(displayValue(r.campus_name, "未分配")) },
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

// 渲染陪跑会话工作台。
function renderWebChat() {
  const campusText = userCampusText(state.currentUser);
  $("loginStatus").innerHTML = state.currentUser
    ? `<span>当前账号</span><strong>${userRoleBadge(state.currentUser)}${esc(state.currentUser.display_name || state.currentUser.username)}</strong><small>${esc(campusText || "全部校区")}</small>`
    : `<span>当前账号</span><strong>未登录</strong><small>请先在登录页进入控制端</small>`;
  const rows = state.conversations.length ? state.conversations : state.families;
  const keyword = String(state.chatFilter || "").trim().toLowerCase();
  const visible = keyword
    ? rows.filter((item) => [item.parent_nickname, item.family_id, item.last_message, item.coach_name]
        .some((field) => String(field || "").toLowerCase().includes(keyword)))
    : rows;
  const list = visible.length ? visible.map((item) => `
    <button class="list-item ${item.family_id === state.selectedChatFamilyId ? "selected" : ""}" onclick="selectChat('${esc(item.family_id)}')">
      <strong>${esc(item.parent_nickname || item.family_id)}</strong>
      <span>${esc(item.child_grade || "未知年级")} · ${esc(item.message_count || 0)} 条 · ${esc(item.last_speaker || "")}</span>
      <small>${esc(item.last_message || "")}</small>
    </button>
  `).join("") : emptyState(keyword ? "没有匹配的会话" : "暂无会话", keyword ? "换一个关键词，或清空搜索框查看全部会话。" : "请先同步企业微信会话，或导入真实聊天记录。");
  $("chatConversations").innerHTML = list;
  renderChatMessages();
}

function renderChatSendHint(family) {
  const hint = $("chatSendHint");
  const directBtn = document.querySelector("#chatForm .danger-action");
  if (!hint || !directBtn) return;
  directBtn.disabled = true;
  if (!state.selectedChatFamilyId || !family) {
    hint.textContent = "请选择会话后再输入内容；人工直发会跳过审核，直接进入对应电脑的企微真实发送队列。";
    return;
  }
  if (!["admin", "coach"].includes(state.currentUser?.role || "")) {
    hint.textContent = "当前角色不能直接真实发送；请使用控制端超管或陪跑师账号。";
    return;
  }
  const target = (family.parent_nickname || "").trim();
  if (!target) {
    hint.textContent = "当前会话缺少企微目标名，无法定位到企业微信群/私聊。";
    return;
  }
  const candidates = devicesForTarget(target);
  if (candidates.length !== 1) {
    hint.textContent = candidates.length
      ? `目标「${target}」绑定了多个设备：${candidates.map((item) => item.device_id).join("、")}；为避免串号，请先在设备监控明确唯一负责设备。`
      : `目标「${target}」还没有绑定负责设备；请先到设备监控给对应人员设备配置负责会话。`;
    return;
  }
  const device = candidates[0];
  const hardIssues = [];
  if (!device.online) hardIssues.push("设备离线");
  if (device.wecom_ok !== "Y") hardIssues.push(`企微${device.wecom_ok || "未知"}`);
  if (!device.allow_real_send) hardIssues.push("真实发送开关未开启");
  if (hardIssues.length) {
    hint.textContent = `目标「${target}」对应设备 ${device.device_id} 暂不可真发：${hardIssues.join("、")}。`;
    return;
  }
  directBtn.disabled = false;
  // 设备忙（在途任务/结果待补传）不再禁用直发：任务排队，设备空闲后依次领取。
  const queued = state.tasks.filter((item) => item.device_id === device.device_id
    && (item.send_mode || "") === "real_send"
    && ["pending", "assigned"].includes(item.status)).length;
  const outboxPending = Number(device.outbox_pending_count || 0);
  const busyParts = [];
  if (queued) busyParts.push(`前面还有 ${queued} 条排队/发送中`);
  if (outboxPending) busyParts.push(`${outboxPending} 条结果待补传`);
  hint.textContent = busyParts.length
    ? `设备 ${device.device_id} 正忙（${busyParts.join("，")}）；新消息会自动排队，空闲后依次发送并回读确认。`
    : `将由唯一负责设备 ${device.device_id}${device.name ? ` · ${device.name}` : ""} 发送到「${target}」；不进入审核队列，发送后仍会回读确认。`;
}

// 渲染当前聊天消息，让它更接近真实陪跑师对话。
function renderChatMessages() {
  const family = state.families.find((item) => item.family_id === state.selectedChatFamilyId);
  $("chatTitle").textContent = family ? family.parent_nickname : "请选择会话";
  $("chatMeta").textContent = family ? `${family.family_id} · ${family.child_grade || "未知年级"} · ${family.campus_name || "未分配校区"} · ${family.coach_name || "未分配"}` : "";
  renderChatSendHint(family);
  if (!state.selectedChatFamilyId) {
    $("chatMessages").innerHTML = emptyState("请选择家庭会话", "从左侧会话分组选择一个企微会话后，这里会展示聊天上下文。");
    return;
  }
  // 已入队未回写的真发任务也显示在会话里，直发后立刻有反馈，不用等设备回写。
  const inflight = state.tasks.filter((task) => task.family_id === state.selectedChatFamilyId
    && (task.send_mode || "") === "real_send"
    && ["pending", "approved", "assigned"].includes(task.status));
  // 发送失败的也要看得见（最近3条），否则消息会从"发送中"直接无声消失。
  const failed = state.tasks
    .filter((task) => task.family_id === state.selectedChatFamilyId
      && (task.send_mode || "") === "real_send"
      && task.status === "failed")
    .slice(-3);
  if (!state.chatMessages.length && !inflight.length && !failed.length) {
    $("chatMessages").innerHTML = emptyState("暂无消息", "当前会话还没有聊天记录，可以发送一条测试消息或同步企微。");
    return;
  }
  let lastDay = "";
  const parts = [];
  state.chatMessages.forEach((msg) => {
    const day = chatDayLabel(msg.message_time);
    if (day && day !== lastDay) {
      parts.push(`<div class="chat-date-sep"><span>${esc(day)}</span></div>`);
      lastDay = day;
    }
    const kind = chatMessageKind(msg);
    parts.push(`
      <article class="chat-message-row ${kind.className}">
        <strong class="chat-message-speaker"><em class="chat-message-kind">${esc(kind.label)}</em>${esc(msg.speaker || "未知")}</strong>
        <p class="chat-message-content">${esc(msg.content)}</p>
        <time class="chat-message-time">${esc(formatChatTime(msg.message_time))}</time>
      </article>
    `);
  });
  inflight.forEach((task) => {
    const label = task.status === "assigned" ? "发送中" : "排队中";
    parts.push(`
      <article class="chat-message-row queued">
        <strong class="chat-message-speaker"><em class="chat-message-kind">${esc(label)}</em>我</strong>
        <p class="chat-message-content">${esc(task.content)}</p>
        <time class="chat-message-time">${esc(formatChatTime(task.scheduled_at || task.created_at || ""))}</time>
      </article>
    `);
  });
  failed.forEach((task) => {
    parts.push(`
      <article class="chat-message-row send-failed">
        <strong class="chat-message-speaker"><em class="chat-message-kind">发送失败</em>我</strong>
        <p class="chat-message-content">${esc(task.content)}<small>若已在企微看到本条，可到发送日志人工核验，核验后会补进会话记录</small></p>
        <time class="chat-message-time"><button onclick="switchTab('logs')">详情</button></time>
      </article>
    `);
  });
  $("chatMessages").innerHTML = parts.join("");
}

function renderChatOutputs() {
  if (!$("chatAiOutputs")) return;
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
        <h3>待发送回复</h3>
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
              ${taskCan(task, "web_send") ? `<button onclick="sendTaskFromChat(${task.id})">网页发送</button>` : ""}
              ${taskCan(task, "cancel") ? `<button onclick="cancelTask(${task.id})">取消</button>` : ""}
              ${taskAllowedOperations(task).length === 1 ? '<span class="muted">仅可查看</span>' : ""}
            </div>
          </article>
        `).join("") : emptyState("暂无待发送回复", "人工输入可直接发企微；AI 回复仍会先生成草稿，便于必要时编辑。")}
      </div>
    </section>
    <section class="assist-section">
      <div class="section-head compact-head"><h3>最近 AI 结果</h3></div>
      <div class="stack">${outputs.length ? outputs.map((item) => outputCard(item, true)).join("") : emptyState("暂无 AI 结果", "同步或生成后，这里会展示最近的画像、回复和周报。")}</div>
    </section>
  `;
}

function chatMessageKind(msg) {
  const source = String(msg.source || "");
  const speaker = String(msg.speaker || "");
  const text = `${source} ${speaker}`;
  const isAi = /AI|Agent|智能|机器人|ai_reply|网页通讯发送任务/i.test(text);
  return isAi ? { label: "AI", className: "ai" } : { label: "真人", className: "human" };
}

// 有日期分隔条兜底日期信息，行内时间只显示时:分。
function formatChatTime(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const parsed = new Date(raw);
  if (!Number.isNaN(parsed.getTime())) {
    return parsed.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
  }
  const tail = raw.replace("T", " ").slice(11, 16);
  return tail || raw.slice(0, 16);
}

// 消息按天分组的分隔条文案，如「06-30 周一」。
function chatDayLabel(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const parsed = new Date(raw);
  if (!Number.isNaN(parsed.getTime())) {
    const week = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][parsed.getDay()];
    const pad = (num) => String(num).padStart(2, "0");
    return `${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ${week}`;
  }
  return raw.replace("T", " ").slice(0, 10);
}

async function selectChat(familyId) {
  return withAction("切换会话", async () => {
    state.selectedChatFamilyId = familyId;
    state.selectedFamilyId = familyId;
    state.chatMessages = await api(`/api/conversations/${encodeURIComponent(familyId)}/messages`);
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
        <p>${esc(family.family_id)} · ${esc(family.child_grade || "未知年级")} · ${esc(family.campus_name || "未分配校区")} · ${esc(family.coach_name || "未填写陪跑师")} · ${esc(family.message_count)} 条消息</p>
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
    $("familyDetail").innerHTML = emptyState("请先导入家庭数据", "导入 CSV/XLSX 或同步企业微信后，家庭档案会在这里展示。");
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
        <p class="muted">${esc(data.family.family_id)} · ${esc(data.family.child_grade || "未知年级")} · ${esc(data.family.campus_name || "未分配校区")} · ${esc(data.family.coach_name || "未分配陪跑师")}</p>
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
          ${badge(r.parent_ack_at ? "家长已签收" : "家长未签收", r.parent_ack_at ? "ok" : "warn")}
          ${r.parent_feedback_score ? badge(`反馈${r.parent_feedback_score}/5`, r.parent_feedback_score <= 2 ? "danger" : r.parent_feedback_score === 3 ? "warn" : "ok") : badge("未反馈", "warn")}
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
  const replyOutputs = state.outputs
    .filter((item) => item.agent_type === "ai_reply")
    .sort(replyOutputSort);
  renderReplyAgentConfig(replyOutputs);
  renderReplyMetrics(replyOutputs);
  const replyRows = replyOutputs.slice(0, 20).map((item) => replyRecordCard(item)).join("");
  $("replyOutputs").innerHTML = replyRows
    ? `<div class="reply-draft-list">
        <div class="reply-draft-header">
          <span>状态 / 家庭</span>
          <span>回复预览</span>
          <span>时间</span>
          <span>详情</span>
        </div>
        ${replyRows}
      </div>`
    : emptyState("暂无自动回复记录", "开启自动 AI 回复并同步企微消息后，这里会展示最近生成和入队情况。");
}

// 渲染回复上下文。
async function renderReplyContext() {
  if (!$("replyContext")) return;
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
  renderManualTaskForm();
  if ($("sendAllBtn")) {
    const canBulkSend = state.tasks.some((task) => taskCan(task, "web_send"));
    $("sendAllBtn").disabled = !canBulkSend;
    $("sendAllBtn").title = canBulkSend ? "发送全部可网页发送任务；不经过企微被控端" : "当前角色或任务状态不允许批量发送";
  }
  const ids = new Set(state.tasks.map((task) => Number(task.id)));
  Array.from(state.expandedTaskIds).forEach((id) => {
    if (!ids.has(id)) state.expandedTaskIds.delete(id);
  });
  $("taskTable").innerHTML = state.tasks.length
    ? `<div class="task-review-list">
        <div class="task-review-header">
          <span>ID</span>
          <span>对象 / 场景</span>
          <span>内容预览</span>
          <span>状态</span>
          <span>准备度</span>
          <span>详情</span>
        </div>
        ${state.tasks.map(taskReviewCard).join("")}
      </div>`
    : emptyState("暂无待发送任务", "AI 回复、周报或手动企微任务创建后会出现在这里。");
}

// 渲染日志列表：单行摘要 + 展开详情，轨迹/校验/截图/人工核验折叠进详情区。
function renderLogs() {
  $("logTable").innerHTML = state.logs.length
    ? `<div class="task-review-list">
        <div class="log-review-header">
          <span>时间</span>
          <span>对象 / 家庭</span>
          <span>状态</span>
          <span>结果 / 原因</span>
          <span>群内校验</span>
          <span>详情</span>
        </div>
        ${state.logs.map(logReviewCard).join("")}
      </div>`
    : emptyState("暂无发送日志", "任务发送后会在这里记录状态、失败原因、设备和截图证据。");
}

const LOG_VERIFY_LABELS = {
  confirmed: "群内已回读",
  failed: "群内未确认",
  unknown: "待人工核对",
  not_applicable: "无需校验",
};

function logVerifyBadge(log) {
  const status = log.verify_status || "";
  if (!status) return '<span class="muted">—</span>';
  const kind = status === "confirmed" ? "ok" : (status === "failed" || status === "unknown" ? "danger" : "");
  return badge(LOG_VERIFY_LABELS[status] || status, kind);
}

function formatLogTime(value) {
  const raw = String(value || "").trim();
  if (!raw) return "—";
  const parsed = new Date(raw);
  if (!Number.isNaN(parsed.getTime())) {
    const pad = (num) => String(num).padStart(2, "0");
    return `${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`;
  }
  return raw.replace("T", " ").slice(5, 16);
}

function logReviewCard(log) {
  const reasonLevel = log.send_reason_level || (log.status === "failed" ? "danger" : "");
  const trace = Array.isArray(log.send_trace) && log.send_trace.length ? log.send_trace.join(" / ") : "";
  const screenshot = log.screenshot_path
    ? `<a class="dl-link" href="${esc(log.screenshot_path)}" target="_blank" rel="noopener">查看截图</a>`
    : "—";
  const manualActions = canManualVerifyLog(log)
    ? `<div class="actions left">
        <button class="danger-action" onclick="manualVerifySendLog(${log.id}, true)">人工确认已发</button>
        <button onclick="manualVerifySendLog(${log.id}, false)">人工确认未发</button>
      </div>`
    : "";
  return `
    <details class="task-review-card">
      <summary class="log-card-summary">
        <time>${esc(formatLogTime(log.sent_at))}</time>
        <div class="log-summary-target">
          <strong>${esc(log.target_name || "—")}</strong>
          <small>${esc(familyName(log.family_id))} · 任务#${esc(log.task_id ?? "—")}</small>
        </div>
        <div class="log-summary-status">${sendTaskStatusBadge(log.status)}${sendModeBadge(log.send_mode || "dry_run")}</div>
        <p class="log-summary-reason">${badge(log.send_stage || "发送结果", reasonLevel)}<span>${esc(log.send_reason_label || "未分类")}</span></p>
        <div class="log-summary-verify">${logVerifyBadge(log)}</div>
        <span class="task-expand-label"></span>
      </summary>
      <div class="task-card-detail">
        <div class="task-detail-table">
          <section><span>任务 / 家庭</span><strong>#${esc(log.task_id ?? "—")} · ${esc(familyName(log.family_id))}</strong></section>
          <section><span>阶段</span><strong>${esc(log.send_stage || "发送结果")}</strong></section>
          <section><span>设备</span><strong>${esc(log.device_id || "—")}</strong></section>
          <section><span>校验时间</span><strong>${esc(log.verified_at || "—")}</strong></section>
          <section><span>截图证据</span><strong>${screenshot}</strong></section>
        </div>
        ${trace ? `<p class="muted">发送轨迹：${esc(trace)}</p>` : ""}
        ${log.verify_detail ? `<p class="muted">校验详情：${esc(log.verify_detail)}</p>` : ""}
        ${log.detail ? `<pre>${esc(log.detail)}</pre>` : ""}
        ${manualActions}
      </div>
    </details>
  `;
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

function deviceRealSendControl(device) {
  const enabled = device.allow_real_send === true;
  const label = enabled ? "真实发送已开启" : "仅试运行";
  const button = enabled ? "关闭真发" : "开启真发";
  return `
    ${badge(label, enabled ? "danger" : "ok")}
    <button class="${enabled ? "" : "danger-action"}" onclick="toggleDeviceRealSend('${esc(device.device_id)}', ${enabled ? "false" : "true"})">${button}</button>
  `;
}

function deviceConversationScopeControl(device) {
  const enabled = device.allow_any_conversation === true;
  const label = enabled ? "全会话" : "白名单";
  const button = enabled ? "关闭全会话" : "开启全会话";
  return `
    ${badge(label, enabled ? "danger" : "ok")}
    <button class="${enabled ? "" : "danger-action"}" onclick="toggleDeviceAnyConversation('${esc(device.device_id)}', ${enabled ? "false" : "true"})">${button}</button>
  `;
}

function deviceOutboxStatus(device) {
  const pending = Number(device.outbox_pending_count || 0);
  if (pending <= 0) {
    return badge(device.outbox_status_label || "结果已同步", "ok");
  }
  const detail = device.outbox_last_error ? `<p class="muted">${esc(device.outbox_last_error)}</p>` : "";
  return `${badge(device.outbox_status_label || `待补传 ${pending} 条`, "danger")}${detail}`;
}

function deviceConversationProofStatus(device) {
  const count = Number(device.conversation_proof_count || 0);
  const label = device.conversation_proof_label || `${count} 个会话24小时内可读`;
  const ready = device.conversation_proof_ready === true;
  const total = Number(device.conversation_proof_total || 0);
  const missingTargets = Array.isArray(device.conversation_proof_missing_targets) ? device.conversation_proof_missing_targets : [];
  const missing = missingTargets.length
    ? `<p class="muted">缺失/过期：${esc(missingTargets.slice(0, 5).join("、"))}${missingTargets.length > 5 ? "…" : ""}</p>`
    : "";
  const detail = device.last_conversation_proof_target
    ? `<p class="muted">最近：${esc(device.last_conversation_proof_target)} · ${esc(device.last_conversation_proof_at || "")}</p>`
    : "";
  return `${badge(label, ready ? "ok" : (total ? "warn" : ""))}${missing}${detail}`;
}

function deviceRealSendStats(device) {
  const attempted = Number(device.real_send_attempted_24h || 0);
  const failed = Number(device.real_send_confirm_failed_24h || 0);
  const rate = Number(device.real_send_confirm_rate_24h ?? 100);
  const kind = attempted <= 0 ? "" : (failed > 0 || rate < 100 ? "danger" : "ok");
  const detail = attempted > 0
    ? `<p class="muted">确认 ${esc(device.real_send_confirmed_24h || 0)}/${esc(attempted)}，失败/未知 ${esc(failed)}</p>`
    : "";
  return `${badge(device.real_send_success_label || "近24小时暂无真实发送", kind)}${detail}`;
}

// 渲染设备监控列表。
function renderDevices() {
  $("deviceTable").innerHTML = table([
    { label: "设备ID", key: "device_id" },
    { label: "名称", key: "name" },
    { label: "在线", render: (r) => badge(r.online ? "在线" : "离线", r.online ? "ok" : "") },
    { label: "企微", render: (r) => badge(r.wecom_ok === "Y" ? "正常" : (r.wecom_ok || "未知"), r.wecom_ok === "Y" ? "ok" : "") },
    { label: "会话可读证明", render: deviceConversationProofStatus },
    { label: "结果补传", render: deviceOutboxStatus },
    { label: "真发闭环", render: deviceRealSendStats },
    { label: "真实发送开关", render: deviceRealSendControl },
    { label: "会话范围", render: deviceConversationScopeControl },
    { label: "最后心跳", key: "last_heartbeat" },
    { label: "负责会话", key: "conversation_count" },
    { label: "待发", render: (r) => (r.task_counts?.pending ?? 0) + (r.task_counts?.assigned ?? 0) },
    { label: "已发", render: (r) => r.task_counts?.sent ?? 0 },
    { label: "失败", render: (r) => r.task_counts?.failed ?? 0 },
    { label: "最近错误", key: "last_error" },
    { label: "只读校验", render: (r) => `
      <button onclick="requestConversationProof('${esc(r.device_id)}')">刷新单个</button>
      <button ${Number(r.conversation_proof_missing_count || 0) > 0 ? "" : "disabled"} onclick="requestMissingConversationProofs('${esc(r.device_id)}')">补齐缺失</button>
      <button onclick="requestAllConversationProofs('${esc(r.device_id)}')">巡检全部</button>
    ` },
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
  renderAuthState();
  renderKpis();
  renderWorkbenchOverview();
  renderServiceQuality();
  renderParentDashboard();
  renderPriorityList();
  renderRecentOutputs();
  renderInsightCards();
  renderWorkSuggestions();
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
// Agent 评测只在进入系统设置页时运行一次，避免每次刷新都跑整套评测集。
let agentEvalLoading = false;

async function refreshAgentEval(force = false) {
  if (!isAdminUser() || agentEvalLoading) return;
  if (!force && state.agentEval?.total) {
    renderAgentEval();
    return;
  }
  agentEvalLoading = true;
  const el = $("agentEvalBoard");
  if (el && !state.agentEval?.total) el.innerHTML = loadingState("正在运行 Agent 评测", "固定样本评测场景识别、安全边界和回复质量。");
  try {
    state.agentEval = await api("/api/agent/evaluations/run", { method: "POST" });
    renderAgentEval();
  } catch (err) {
    if (el) el.innerHTML = errorState("Agent 评测运行失败", String(err?.message || err), '<button onclick="refreshAgentEval(true)">重试</button>');
  } finally {
    agentEvalLoading = false;
  }
}

async function refreshAll() {
  return withAction("刷新数据", async () => {
    if (isInitialDataEmpty()) renderGlobalLoading();
    const adminOnly = isAdminUser();
    // 服务器在境外、浏览器同域并发只有 6 条连接，20 个接口一次性等齐要排 3-4 轮 RTT。
    // 拆两梯队：核心数据先到先渲染，管理/运维/配置类靠后补齐（两梯队并发，梯队一先占用连接）。
    const corePromise = Promise.all([
      api("/api/families"),
      api("/api/profiles"),
      api("/api/reports"),
      api("/api/send-tasks"),
      api("/api/send-logs"),
      api("/api/ai-outputs"),
      api("/api/conversations"),
      api(scopedPath("/api/workbench/today-priorities", { limit: 12 })),
      api(scopedPath("/api/workbench/overview", { limit: 8 }, { includeCoach: true })),
      adminOnly ? api("/api/devices") : api("/api/conversations/send-devices"),
    ]);
    const restPromise = Promise.all([
      api("/api/templates"),
      adminOnly ? api("/api/audit-logs?entity_type=send_task&limit=200") : Promise.resolve([]),
      adminOnly ? api(scopedPath("/api/admin/service-quality")) : Promise.resolve({}),
      safeApi("/api/test-chat/accounts", []),
      adminOnly ? api("/api/ops/health") : Promise.resolve({}),
      adminOnly ? api("/api/ops/backups") : Promise.resolve([]),
      adminOnly ? api("/api/ops/retention") : Promise.resolve({}),
      adminOnly ? safeApi("/api/ark-config", {}) : Promise.resolve({}),
      adminOnly ? api("/api/import/templates") : Promise.resolve([]),
      safeApi("/api/agent/reply-config", DEFAULT_REPLY_AGENT_CONFIG),
    ]);
    const [families, profiles, reports, tasks, logs, outputs, conversations, todayPriorities, workbenchOverview, devices] = await corePromise;
    Object.assign(state, { families, profiles, reports, tasks, logs, outputs, conversations, todayPriorities, workbenchOverview, devices });
    state.selectedFamilyId = state.selectedFamilyId || families[0]?.family_id || "";
    state.selectedChatFamilyId = state.selectedChatFamilyId || families[0]?.family_id || "";
    renderAll();
    // 会话消息与二梯队并行补齐，各自到达后局部重渲染。
    const chatPromise = state.selectedChatFamilyId
      ? api(`/api/conversations/${encodeURIComponent(state.selectedChatFamilyId)}/messages`)
          .then((messages) => { state.chatMessages = messages; renderChatMessages(); })
          .catch(() => { state.chatMessages = state.chatMessages || []; })
      : Promise.resolve();
    const [templates, auditLogs, serviceQuality, accounts, opsHealth, backups, retention, arkConfig, importTemplates, replyAgentConfig] = await restPromise;
    Object.assign(state, { templates, auditLogs, serviceQuality, accounts, opsHealth, backups, retention, arkConfig, importTemplates, replyAgentConfig });
    renderAll();
    await chatPromise;
    saveStateCache();
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

async function saveReplyAgentConfig(event) {
  event.preventDefault();
  const form = event.target;
  const data = new FormData(form);
  const payload = {
    auto_reply_enabled: data.has("auto_reply_enabled"),
    auto_create_send_task: data.has("auto_create_send_task"),
    send_mode: data.get("send_mode") || "dry_run",
    tone: data.get("tone") || "standard",
    reply_agent: data.get("reply_agent") || "ai_reply_agent",
    high_risk_policy: data.get("high_risk_policy") || "manual",
    skip_recent_hours: Number(data.get("skip_recent_hours") || 0),
    max_batch: Number(data.get("max_batch") || 200),
    enabled_agents: data.getAll("enabled_agents"),
  };
  if (payload.auto_reply_enabled && payload.send_mode === "real_send") {
    const ok = confirm("你正在开启真实发送队列：低风险 AI 回复会自动进入企微真实发送链路，仍受设备白名单和安全词拦截。确认保存吗？");
    if (!ok) return;
  }
  return withAction("保存自动回复配置", async () => {
    state.replyAgentConfig = await api("/api/agent/reply-config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    toast(payload.auto_reply_enabled ? "自动 AI 回复已开启" : "自动 AI 回复已关闭");
    renderReplyPage();
  });
}

async function autoDraftReplies() {
  return withAction("自动生成待审回复", async () => {
    if (!confirm("将为当前可访问家庭批量生成 AI 待审草稿；不会创建发送任务，也不会触发企业微信发送。继续吗？")) return;
    const result = await api("/api/agent/replies/auto-draft", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tone: "standard", source: "自动回复草稿" }),
    });
    toast(`已生成 ${result.created || 0} 条待审回复，跳过 ${result.skipped || 0} 个家庭`);
    await refreshAll();
    switchTab("replies");
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
    const familyId = $("replyFamilySelect")?.value || state.selectedFamilyId;
    state.selectedFamilyId = familyId;
    await api("/api/agent/reply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ family_id: familyId, message: $("replyMessage")?.value || "", tone, source: `AI回复-${tone}` }),
    });
    if ($("replyMessage")) $("replyMessage").value = "";
    toast("回复建议已生成");
    await refreshAll();
    switchTab("replies");
  });
}

// 保存输出。
function outputTextarea(id) {
  return document.querySelector(`.panel.active textarea#output-${id}`) || $(`output-${id}`);
}

async function saveOutput(id) {
  return withAction("保存审核稿", async () => {
    const textarea = outputTextarea(id);
    if (!textarea) throw new Error("未找到审核内容");
    const edited_output = textarea.value;
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
    const textarea = outputTextarea(id);
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
        status: confirmRealSend ? "pending" : (task?.status || "pending"),
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
    const nextContent = editor?.value || task?.content || "";
    const nextDeviceId = $(`task-device-${id}`)?.value || "";
    const needsContentSave = taskCan(task, "edit") && nextContent !== (task?.content || "");
    const needsDeviceSave = taskCan(task, "assign_device") && nextDeviceId !== (task?.device_id || "");
    if (needsContentSave || needsDeviceSave) {
      await api(`/api/send-tasks/${id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...task,
          content: nextContent,
          device_id: nextDeviceId,
          send_mode: "dry_run",
          status: "pending",
        }),
      });
    }
    await api(`/api/send-tasks/${id}/dry-run`, { method: "POST" });
    toast("已加入企微试运行队列；被控端会定位、粘贴并清空，不会真实发送");
    await refreshAll();
  });
}

// 把任务加入企微真实发送队列：服务端记录确认，被控端仍会按设备策略二次校验。
async function queueTaskRealSend(id) {
  return withAction("企微真实发送", async () => {
    const task = state.tasks.find((item) => item.id === id);
    const target = task?.target_name || "";
    const ok = window.confirm(
      `确认通过企业微信真实发送任务 ${id}？\n目标：${target}\n此操作会触达真实企微会话，请确认内容、对象和设备无误。\n\n安全条件：设备监控里的“真实发送开关”必须开启，Windows 被控端才会真正按发送键。`
    );
    if (!ok) return;
    const editor = $(`task-${id}`);
    const nextContent = editor?.value || task?.content || "";
    const nextDeviceId = $(`task-device-${id}`)?.value || "";
    await api(`/api/send-tasks/${id}/real-send`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: nextContent, device_id: nextDeviceId }),
    });
    toast("已加入企微真实发送队列；被控端会按设备真实发送开关和目标校验后执行");
    await refreshAll();
  });
}

async function retryTask(id) {
  return withAction("失败重试", async () => {
    await api(`/api/send-tasks/${id}/retry`, { method: "POST" });
    toast("已重新加入发送队列");
    await refreshAll();
  });
}

async function manualVerifySendLog(id, confirmed) {
  const detail = window.prompt(
    confirmed
      ? "请填写你在目标群/私聊看到本次内容的证据（例如最后一条内容、时间或截图编号）："
      : "请填写你核对后确认未发送成功的证据（例如目标会话最后一条内容或异常现象）："
  );
  if (detail === null) return;
  if (!detail.trim()) return toast("必须填写人工核验证据");
  if (confirmed) {
    const ok = window.confirm("确认已经在企业微信目标群/私聊中看到本次内容？确认后任务会归档为已发送，且不会自动重发。");
    if (!ok) return;
  }
  return withAction("人工核验发送结果", async () => {
    await api(`/api/send-logs/${id}/manual-verification`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirmed, detail }),
    });
    toast(confirmed ? "已人工确认发送成功并落库" : "已人工确认未发送成功并落库");
    await refreshAll();
  });
}

// 发送任务到网页通讯会话。
async function sendTask(id) {
  return withAction("网页发送任务", async () => {
    await api(`/api/send-tasks/${id}/web-send`, { method: "POST" });
    toast("已发送到网页通讯；不经过企微被控端");
    await refreshAll();
  });
}

async function sendTaskFromChat(id) {
  return withAction("网页发送回复", async () => {
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
    toast("已发送到当前网页会话；不经过企微被控端");
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
if ($("replyFamilySelect")) {
  $("replyFamilySelect").onchange = (event) => {
    state.selectedFamilyId = event.target.value;
    renderReplyPage();
  };
}

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

if ($("manualTaskTarget")) $("manualTaskTarget").addEventListener("input", () => syncManualTaskDeviceSelection(true));
if ($("manualTaskMode")) $("manualTaskMode").addEventListener("change", () => syncManualTaskDeviceSelection(true));

// 控制端直接创建企微发送任务，适合群聊/私聊测试和临时通知。
$("manualTaskForm").onsubmit = async (event) => {
  event.preventDefault();
  await withAction("创建企微发送任务", async () => {
    const data = Object.fromEntries(new FormData(event.target).entries());
    const target = (data.target_name || "").trim();
    const content = (data.content || "").trim();
    const mode = data.send_mode || "dry_run";
    const deviceId = (data.device_id || "").trim();
    if (!target) throw new Error("请填写企微目标群/私聊");
    if (!content) throw new Error("请填写发送内容");
    const payload = {
      family_id: (data.family_id || "").trim() || manualTaskFamilyId(target),
      target_name: target,
      scene: (data.scene || "").trim() || "控制端手动下发",
      content,
      device_id: deviceId,
      send_mode: mode,
      confirm_real_send: mode === "real_send",
    };
    const preflight = await api("/api/send-tasks/preflight", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (mode === "real_send") {
      const resolvedDeviceId = (preflight.resolved_device_id || preflight.device_id || deviceId || "").trim();
      if (resolvedDeviceId) {
        payload.device_id = resolvedDeviceId;
        if ($("manualTaskDevice")) $("manualTaskDevice").value = resolvedDeviceId;
      }
    }
    if (!preflight.ok) {
      const detail = (preflight.reasons || []).join("\n");
      if (mode === "real_send") {
        const readinessActions = Array.isArray(preflight.readiness?.actions) ? preflight.readiness.actions : [];
        const enableAction = readinessActions.find((action) => action.action === "enable_real_send" && action.device_id);
        if (enableAction && isAdminUser()) {
          const ok = window.confirm(
            `发送预检未通过：\n${detail}\n\n是否先开启设备 ${enableAction.device_id} 的真实发送开关？\n开启后请重新提交真实发送任务。`
          );
          if (ok) {
            await saveDeviceRealSendPolicy(enableAction.device_id, true);
            toast("设备真实发送已开启，请重新提交真实发送任务");
            await refreshAll();
          }
          return;
        }
        const hardReasons = Array.isArray(preflight.hard_reasons) ? preflight.hard_reasons : [];
        if (hardReasons.length) {
          throw new Error(`发送预检未通过：\n${hardReasons.join("\n")}`);
        }
        const hint = preflight.conversation_check_hint || {};
        if (hint.action === "queue_conversation_check") {
          const existing = hint.existing_task_id ? `\n已有待执行校验任务：#${hint.existing_task_id}` : "";
          const ok = window.confirm(`发送预检未通过：\n${detail}\n\n是否先下发只读会话校验？\n设备：${hint.device_id}\n目标：${hint.target_name}${existing}\n\n校验只会打开会话并读取消息，不会发送。`);
          if (ok && hint.available !== false) {
            await queueConversationProof(hint.device_id, hint.target_name, hint.family_id || manualTaskFamilyId(hint.target_name), "下发预检修复校验");
          } else if (ok) {
            toast("已有会话校验任务在队列中，请等待被控端回写后再创建真实发送");
            await refreshAll();
            switchTab("tasks");
          }
          return;
        }
        throw new Error(`发送预检未通过：\n${detail}`);
      }
      const keep = window.confirm(`发送预检提示：\n${detail || preflight.label}\n\n是否仍创建试运行任务？`);
      if (!keep) return;
    }
    if (mode === "real_send") {
      if (!payload.device_id) throw new Error("真实发送必须绑定唯一负责设备；请先在设备监控配置负责会话或手动选择设备");
      const ok = window.confirm(`确认创建企微真实发送任务？\n目标：${target}\n设备：${payload.device_id}\n预检：${preflight.label}\n\n创建后会进入该设备真实发送队列，请确认目标、设备和内容无误。`);
      if (!ok) return;
    }
    await api("/api/send-tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    event.target.reset();
    toast(mode === "real_send" ? "真实发送任务已创建；请看发送准备和群内校验" : "试运行任务已创建");
    await refreshAll();
    switchTab("tasks");
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

async function saveDeviceRealSendPolicy(deviceId, enabled) {
  const device = state.devices.find((item) => item.device_id === deviceId) || {};
  await api(`/api/devices/${encodeURIComponent(deviceId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: device.name || "",
      note: device.note || "",
      conversations: deviceConversationList(device),
      allow_real_send: enabled,
      allow_any_conversation: device.allow_any_conversation === true,
    }),
  });
}

async function toggleDeviceRealSend(deviceId, enabled) {
  const actionText = enabled ? "开启真实发送" : "关闭真实发送";
  if (enabled) {
    const ok = window.confirm(`确认给设备 ${deviceId} 开启真实发送？\n开启后，该设备领取 real_send 任务时会在企业微信真实按发送键。`);
    if (!ok) return;
  }
  await withAction(actionText, async () => {
    await saveDeviceRealSendPolicy(deviceId, enabled);
    toast(enabled ? "设备真实发送已开启" : "设备真实发送已关闭");
    await refreshAll();
  });
}

async function toggleDeviceAnyConversation(deviceId, enabled) {
  const device = state.devices.find((item) => item.device_id === deviceId);
  const actionText = enabled ? "开启全会话范围" : "关闭全会话范围";
  if (enabled) {
    const ok = window.confirm(`确认给设备 ${deviceId} 开启全会话范围？\n开启后，控制端可以把任意群聊或人员私聊任务派给这台电脑，RPA 会通过企微搜索目标会话后再发送。`);
    if (!ok) return;
  }
  await withAction(actionText, async () => {
    let conversations = [];
    try {
      conversations = JSON.parse(device?.conversations || "[]");
    } catch {
      conversations = [];
    }
    await api(`/api/devices/${encodeURIComponent(deviceId)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: device?.name || "",
        note: device?.note || "",
        conversations,
        allow_real_send: device?.allow_real_send === true,
        allow_any_conversation: enabled,
      }),
    });
    toast(enabled ? "设备会话范围已切到全会话" : "设备会话范围已切回白名单");
    await refreshAll();
  });
}

async function queueConversationProof(deviceId, target, familyId, actionText = "刷新会话可读证明") {
  await withAction(actionText, async () => {
    await api(`/api/devices/${encodeURIComponent(deviceId)}/conversation-checks`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target_name: target, family_id: familyId || manualTaskFamilyId(target) }),
    });
    toast(`已下发只读校验：${deviceId} -> ${target}；被控端会打开会话并回读，不会发送`);
    await refreshAll();
    switchTab("tasks");
  });
}

async function requestConversationProof(deviceId) {
  const device = state.devices.find((item) => item.device_id === deviceId);
  let defaultTarget = "";
  try {
    defaultTarget = JSON.parse(device?.conversations || "[]")[0] || "";
  } catch {
    defaultTarget = "";
  }
  const target = (window.prompt(`输入要让设备 ${deviceId} 只读校验的群/私聊名称`, defaultTarget) || "").trim();
  if (!target) return;
  await queueConversationProof(deviceId, target, manualTaskFamilyId(target));
}

async function requestAllConversationProofs(deviceId) {
  const device = state.devices.find((item) => item.device_id === deviceId);
  const count = Number(device?.conversation_count || 0);
  const ok = window.confirm(`确认让设备 ${deviceId} 巡检全部负责会话？\n会逐个打开群/私聊并读取可见消息，不会粘贴或发送。${count ? `\n预计会话数：${count}` : ""}`);
  if (!ok) return;
  await withAction("巡检全部会话证明", async () => {
    const result = await api(`/api/devices/${encodeURIComponent(deviceId)}/conversation-checks/batch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    toast(`已下发会话巡检：新增 ${result.queued_count || 0} 个，跳过 ${result.skipped_count || 0} 个；被控端不会发送消息`);
    await refreshAll();
    switchTab("tasks");
  });
}

async function requestMissingConversationProofs(deviceId) {
  const device = state.devices.find((item) => item.device_id === deviceId);
  const targets = Array.isArray(device?.conversation_proof_missing_targets) ? device.conversation_proof_missing_targets : [];
  const ok = window.confirm(`确认让设备 ${deviceId} 只补齐缺失/过期的会话证明？\n校验只会打开群/私聊并读取可见消息，不会粘贴或发送。${targets.length ? `\n目标：${targets.slice(0, 8).join("、")}${targets.length > 8 ? "…" : ""}` : ""}`);
  if (!ok) return;
  await withAction("补齐缺失会话证明", async () => {
    const result = await api(`/api/devices/${encodeURIComponent(deviceId)}/conversation-checks/batch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ missing_only: true }),
    });
    toast(`已下发缺失证明校验：新增 ${result.queued_count || 0} 个，跳过 ${result.skipped_count || 0} 个`);
    await refreshAll();
    switchTab("tasks");
  });
}

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

// 控制端登录和首个超管注册。
$("adminLoginForm").onsubmit = async (event) => {
  event.preventDefault();
  await withAction("控制端登录", async () => {
    const data = Object.fromEntries(new FormData(event.target).entries());
    const user = await api("/api/admin/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    saveCurrentUser(user);
    toast(`已登录：${user.display_name || user.username}`);
    await refreshAll();
    setAuthGateVisible(false);
    switchTab(user.role === "readonly" ? "dashboard" : "tasks");
  });
};

$("adminRegisterForm").onsubmit = async (event) => {
  event.preventDefault();
  await withAction("注册控制端账号", async () => {
    const wasBootstrap = !!state.authStatus.bootstrap_required;
    const data = Object.fromEntries(new FormData(event.target).entries());
    const user = await api("/api/admin/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    event.target.reset();
    await refreshAuthStatus();
    if (wasBootstrap) {
      saveCurrentUser(user);
      toast("首个超管账号已创建并登录");
      await refreshAll();
      setAuthGateVisible(false);
      switchTab("dashboard");
      return;
    }
    toast(`账号已创建：${user.display_name || user.username}`);
    await refreshAll();
  });
};

if ($("accountSettingsForm")) $("accountSettingsForm").onsubmit = async (event) => {
  event.preventDefault();
  await withAction("保存账号设置", async () => {
    if (!state.currentUser) return toast("请先登录");
    const data = Object.fromEntries(new FormData(event.target).entries());
    const user = await api(accountSettingsEndpoint(), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    saveCurrentUser(user);
    closeAccountSettings();
    toast("账号设置已更新");
    if (user.role === "parent") {
      await refreshParentDashboard();
      renderAll();
      return;
    }
    await refreshAll();
  });
};

// 兼容旧版网页通讯登录入口；当前陪跑会话页不再内置登录表单。
if ($("loginForm")) $("loginForm").onsubmit = async (event) => {
  event.preventDefault();
  await withAction("登录", async () => {
    const data = Object.fromEntries(new FormData(event.target).entries());
    const user = await api("/api/test-chat/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    saveCurrentUser(user);
    toast(`已登录：${user.display_name}`);
    if (user.role === "parent") {
      await refreshParentDashboard();
      renderWebChat();
      switchTab("parentDashboard");
      return;
    }
    await refreshAll();
    switchTab("webChat");
  });
};

// 兼容旧版网页通讯注册入口；控制端账号在登录页/账号管理中提前配置。
if ($("registerForm")) $("registerForm").onsubmit = async (event) => {
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

// 会话工作台人工直发：跳过审核，直接创建企微真实发送任务，由唯一负责设备执行。
$("chatForm").onsubmit = async (event) => {
  event.preventDefault();
  await withAction("企微直发", async () => {
    if (!state.currentUser) return toast("请先登录控制端账号");
    if (!state.selectedChatFamilyId) return toast("请先选择会话");
    const data = Object.fromEntries(new FormData(event.target).entries());
    const content = (data.content || "").trim();
    if (!content) throw new Error("请填写发送内容");
    const family = state.families.find((item) => item.family_id === state.selectedChatFamilyId);
    const target = (family?.parent_nickname || state.selectedChatFamilyId).trim();
    const ok = window.confirm(`确认将这条消息直接发送到企业微信「${target}」？\n\n${content.slice(0, 120)}${content.length > 120 ? "…" : ""}\n\n该操作跳过审核队列，由唯一负责设备执行，发送后会回读确认。`);
    if (!ok) return;
    const res = await api(`/api/conversations/${encodeURIComponent(state.selectedChatFamilyId)}/direct-send`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
    event.target.reset();
    toast(`已直发入队：${res.device_id} -> ${res.target_name}`);
    await refreshAll();
    switchTab("webChat");
  });
};

if ($("chatLocalOnlyBtn")) $("chatLocalOnlyBtn").onclick = async () => {
  await withAction("仅记入系统", async () => {
    if (!state.currentUser) return toast("请先登录账号");
    if (!state.selectedChatFamilyId) return toast("请先选择会话");
    const form = $("chatForm");
    const data = Object.fromEntries(new FormData(form).entries());
    const content = (data.content || "").trim();
    if (!content) throw new Error("请填写消息内容");
    await api("/api/conversations/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ family_id: state.selectedChatFamilyId, username: state.currentUser.username, content }),
    });
    form.reset();
    toast("消息已仅写入系统，不触发企微发送");
    await refreshAll();
    switchTab("webChat");
  });
};

// 对当前会话快速生成可审核回复，只调用一次大模型。
if ($("chatReplyBtn")) $("chatReplyBtn").onclick = async () => {
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
if ($("chatFullAiBtn")) $("chatFullAiBtn").onclick = async () => {
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
  await withAction("网页发送全部任务", async () => {
    const res = await api("/api/send-tasks/web-send-all", { method: "POST" });
    toast(`已网页发送 ${res.sent} 个任务${res.skipped ? `，跳过 ${res.skipped} 个` : ""}；不经过企微被控端`);
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

// 恢复刷新前所在的页签；页签不存在或被禁用时回落工作台。
function restoreActiveTab() {
  const stored = localStorage.getItem("activeTab") || "";
  if (!stored || stored === "dashboard") return;
  if (PENDING_TABS[stored] || !document.querySelector(`.panel#${CSS.escape(stored)}`)) return;
  switchTab(stored);
}

async function bootApp() {
  api("/health").then(() => $("health").textContent = "本地服务正常").catch(() => $("health").textContent = "服务异常");
  // 有本地登录态：立即进应用视图，优先用上次数据快照渲染（无快照则显示加载骨架），
  // 并且数据请求与鉴权检查并行发出，省一轮 RTT。token 失效时统一退回登录页。
  let dataPromise = null;
  if (state.currentUser && state.currentUser.role !== "parent") {
    setAuthGateVisible(false);
    if (loadStateCache()) renderAll();
    else renderGlobalLoading();
    restoreActiveTab();
    dataPromise = refreshAll();
    dataPromise.catch(() => {});
  }
  try {
    await refreshAuthStatus();
    if (state.authStatus.auth_required && !state.currentUser) {
      setAuthGateVisible(true);
      return;
    }
    if (state.currentUser?.role === "parent") {
      await refreshParentDashboard();
      renderAll();
      setAuthGateVisible(false);
      switchTab("parentDashboard");
      return;
    }
    if (dataPromise) {
      await dataPromise;
    } else {
      await refreshAll();
      setAuthGateVisible(false);
      restoreActiveTab();
    }
  } catch (err) {
    if (String(err?.message || "").includes("401")) {
      saveCurrentUser(null);
      setAuthGateVisible(true);
      toast("登录已失效，请重新登录");
      return;
    }
    setAuthGateVisible(true);
    toast(`加载失败：${err.message}`);
  }
}

bootApp();

// 前端状态和页面渲染逻辑都集中在这个文件里。
const $ = (id) => document.getElementById(id);

const state = {
  families: [],
  profiles: [],
  reports: [],
  tasks: [],
  logs: [],
  devices: [],
  arkConfig: {},
  templates: [],
  outputs: [],
  accounts: [],
  conversations: [],
  chatMessages: [],
  currentUser: JSON.parse(localStorage.getItem("chatUser") || "null"),
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
  const res = await fetch(path, options);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
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

function sendModeSelect(task) {
  const mode = task.send_mode || "dry_run";
  return `
    <select id="task-mode-${task.id}">
      <option value="dry_run" ${mode === "dry_run" ? "selected" : ""}>试运行</option>
      <option value="real_send" ${mode === "real_send" ? "selected" : ""}>真实发送</option>
    </select>
    ${mode === "real_send" ? badge("高风险", "warn") : badge("安全", "ok")}
  `;
}

function deviceSelect(task) {
  const current = task.device_id || "";
  const options = ['<option value="">自动领取</option>'].concat(
    state.devices.map((device) => {
      const label = `${device.device_id}${device.name ? ` · ${device.name}` : ""}`;
      return `<option value="${esc(device.device_id)}" ${current === device.device_id ? "selected" : ""}>${esc(label)}</option>`;
    })
  );
  return `<select id="task-device-${task.id}">${options.join("")}</select>`;
}

// 渲染通用表格，减少重复 HTML 拼接。
function table(headers, rows) {
  if (!rows.length) return '<p class="empty">暂无数据</p>';
  return `<div class="table-wrap"><table><thead><tr>${headers.map((h) => `<th>${h.label}</th>`).join("")}</tr></thead><tbody>${
    rows.map((row) => `<tr>${headers.map((h) => `<td>${h.render ? h.render(row) : esc(row[h.key])}</td>`).join("")}</tr>`).join("")
  }</tbody></table></div>`;
}

// 切换侧边栏标签页，并同步页面标题。
let devicePollTimer = null;
function switchTab(tabId) {
  document.querySelectorAll(".sidebar button").forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === tabId));
  document.querySelectorAll(".panel").forEach((panel) => panel.classList.toggle("active", panel.id === tabId));
  const active = document.querySelector(`.sidebar button[data-tab="${tabId}"]`);
  $("pageTitle").textContent = active ? active.textContent : "工作台";
  // 设备页每 5 秒轮询刷新在线状态；离开则停止。
  if (devicePollTimer) { clearInterval(devicePollTimer); devicePollTimer = null; }
  if (tabId === "devices") {
    const poll = async () => {
      try { state.devices = await api("/api/devices"); renderDevices(); } catch (err) { /* 忽略轮询错误 */ }
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

// 顶部 KPI 卡片反映整体待办和风险状态。
function renderKpis() {
  const pendingTasks = state.tasks.filter((task) => task.status === "pending").length;
  const reviewOutputs = state.outputs.filter((item) => item.status === "needs_review").length;
  const highRisk = state.profiles.filter((item) => (item.service_risks || "").includes("退费") || (item.service_risks || "").includes("投诉")).length;
  const approvedReports = state.reports.filter((item) => item.status === "approved").length;
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

// 侧栏优先处理列表，帮助用户先看最紧急的事。
function renderPriorityList() {
  const pendingReports = state.reports.filter((item) => item.status !== "approved").slice(0, 4);
  const pendingTasks = state.tasks.filter((item) => item.status === "pending").slice(0, 4);
  const riskProfiles = state.profiles.filter((item) => (item.service_risks || "").includes("风险") || (item.service_risks || "").includes("退费")).slice(0, 4);
  const items = [
    ...riskProfiles.map((item) => ({ type: "风险画像", family_id: item.family_id, text: item.service_risks, action: `<button onclick="setSelectedFamily('${esc(item.family_id)}')">查看画像</button>` })),
    ...pendingReports.map((item) => ({ type: "待审核周报", family_id: item.family_id, text: item.week_label, action: `<button onclick="switchTab('reports')">查看周报</button>` })),
    ...pendingTasks.map((item) => ({ type: "待发送", family_id: item.family_id, text: item.scene, action: `<button onclick="switchTab('tasks')">处理任务</button>` })),
  ];
  $("priorityList").innerHTML = items.length ? items.map((item) => `
    <article class="row-card">
      <div>${badge(item.type)} <strong>${esc(familyName(item.family_id))}</strong><p>${esc(item.text)}</p></div>
      ${item.action}
    </article>
  `).join("") : '<p class="empty">暂无待处理事项。先导入样例或生成 Agent 内容。</p>';
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
          <strong>${esc(familyName(output.family_id))}</strong>
        </div>
        <small>${esc(output.created_at || "")}</small>
      </div>
      ${compact ? `<pre>${esc(output.display_text).slice(0, 220)}</pre>` : `
        <textarea id="${textId}">${esc(output.edited_output || output.display_text)}</textarea>
        <details>
          <summary>查看原始 JSON 与依据</summary>
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
  $("recentOutputs").innerHTML = state.outputs.length ? state.outputs.slice(0, 6).map((item) => outputCard(item, true)).join("") : '<p class="empty">暂无 Agent 输出。</p>';
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
    ? `${badge(state.currentUser.role === "coach" ? "陪跑师" : "家长", state.currentUser.role === "coach" ? "ok" : "")}<strong>${esc(state.currentUser.display_name)}</strong><p class="muted">${esc(state.currentUser.username)}</p>`
    : '<p class="empty">请先登录测试账号。</p>';
  const rows = state.conversations.length ? state.conversations : state.families;
  $("chatConversations").innerHTML = rows.length ? rows.map((item) => `
    <button class="list-item ${item.family_id === state.selectedChatFamilyId ? "selected" : ""}" onclick="selectChat('${esc(item.family_id)}')">
      <strong>${esc(item.parent_nickname || item.family_id)}</strong>
      <span>${esc(item.child_grade || "未知年级")} · ${esc(item.message_count || 0)} 条 · ${esc(item.last_speaker || "")}</span>
      <small>${esc(item.last_message || "")}</small>
    </button>
  `).join("") : '<p class="empty">暂无会话。点击“生成模拟账号与对话”。</p>';
  renderChatMessages();
  renderChatOutputs();
}

// 渲染当前聊天消息，让它更接近真实陪跑师对话。
function renderChatMessages() {
  const family = state.families.find((item) => item.family_id === state.selectedChatFamilyId);
  $("chatTitle").textContent = family ? family.parent_nickname : "请选择会话";
  $("chatMeta").textContent = family ? `${family.family_id} · ${family.child_grade || "未知年级"} · ${family.coach_name || "未分配"}` : "";
  if (!state.selectedChatFamilyId) {
    $("chatMessages").innerHTML = '<p class="empty">从左侧选择一个家庭会话。</p>';
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
  }).join("") : '<p class="empty">暂无消息。</p>';
}

function renderChatOutputs() {
  if (!state.selectedChatFamilyId) {
    $("chatAiOutputs").innerHTML = '<p class="empty">选择会话后，可以快速生成回复、审核并发送。</p>';
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
          <dt>建议动作</dt><dd>${esc(profile.suggested_actions || "暂无")}</dd>
        </dl>
      ` : '<p class="empty">暂无画像。点击“完整分析”会生成家庭画像。</p>'}
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
            <textarea id="chat-task-${task.id}">${esc(task.content)}</textarea>
            <div class="actions left">
              <button onclick="saveTaskFromChat(${task.id})">保存</button>
              <button onclick="sendTaskFromChat(${task.id})">发送</button>
              <button onclick="cancelTask(${task.id})">取消</button>
            </div>
          </article>
        `).join("") : '<p class="empty">暂无待发送回复。点击“快速生成回复”。</p>'}
      </div>
    </section>
    <section class="assist-section">
      <div class="section-head compact-head"><h3>最近 AI 结果</h3></div>
      <div class="stack">${outputs.length ? outputs.map((item) => outputCard(item, true)).join("") : '<p class="empty">暂无 AI 结果。</p>'}</div>
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
  `).join("") : '<p class="empty">还没有登记企微会话。先填写上方表单，例如：艺博展讯。</p>';
  if (!state.selectedFamilyId) {
    $("wecomPreview").innerHTML = '<p class="empty">请选择一个会话。</p>';
  }
}

// 企微同步后，用这个面板集中看聊天记录和四类 Agent 输出。
async function previewWecomFamily(familyId, remember = true) {
  return withAction("检查会话", async () => {
    if (!familyId) {
      $("wecomPreview").innerHTML = '<p class="empty">请选择一个会话。</p>';
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
      `).join("") || '<p class="empty">暂无已同步聊天记录。运行 RPA 后会出现在这里。</p>'}</div>
      <div class="stack output-preview">${outputs.length ? outputs.map((item) => outputCard(item, true)).join("") : '<p class="empty">暂无 AI 输出。同步到新消息后会自动生成回复、画像、周报和打卡/PBL结果。</p>'}</div>
    `;
  });
}

// 刷新家庭详情页。
async function refreshFamilyDetail() {
  if (!state.families.length) {
    $("familySelect").innerHTML = "";
    $("familyDetail").innerHTML = '<p class="empty">请先导入家庭数据。</p>';
    return;
  }
  state.selectedFamilyId = state.selectedFamilyId || state.families[0].family_id;
  $("familySelect").innerHTML = optionList();
  const data = await api(`/api/families/${encodeURIComponent(state.selectedFamilyId)}`);
  const outputs = state.outputs.filter((item) => item.family_id === state.selectedFamilyId).slice(0, 4);
  $("familyDetail").innerHTML = `
    <section class="profile-pane">
      <h3>${esc(data.family.parent_nickname || data.family.family_id)}</h3>
      <p class="muted">${esc(data.family.family_id)} · ${esc(data.family.child_grade || "未知年级")} · ${esc(data.family.coach_name || "未分配陪跑师")}</p>
      ${data.profile ? `
        <dl>
          <dt>沟通风格</dt><dd>${esc(data.profile.communication_style)}</dd>
          <dt>关注点</dt><dd>${esc(data.profile.pain_points)}</dd>
          <dt>风险信号</dt><dd>${esc(data.profile.service_risks)}</dd>
          <dt>建议动作</dt><dd>${esc(data.profile.suggested_actions)}</dd>
        </dl>
      ` : '<p class="empty">暂无画像，点击右侧“生成画像”。</p>'}
    </section>
    <section>
      <h3>时间线</h3>
      <div class="messages">${data.messages.map((m) => `
        <div class="msg">
          <strong>${esc(m.message_time)} ${esc(m.speaker)}</strong>
          <p>${esc(m.content)}</p>
          <span class="muted">${esc(m.source)} ${esc(m.checkin_status || "")}</span>
        </div>
      `).join("") || '<p class="empty">暂无消息。</p>'}</div>
    </section>
    <section class="ai-pane">
      <h3>AI操作区</h3>
      <div class="agent-buttons">
        <button onclick="runAgentForFamily('profile','${esc(data.family.family_id)}')">生成画像</button>
        <button onclick="runAgentForFamily('weekly','${esc(data.family.family_id)}')">生成周报</button>
        <button onclick="prepareReply('${esc(data.family.family_id)}')">生成回复</button>
        <button onclick="runAgentForFamily('checkin','${esc(data.family.family_id)}')">识别打卡/PBL</button>
      </div>
      <div class="stack">${outputs.length ? outputs.map((item) => outputCard(item, true)).join("") : '<p class="empty">暂无本家庭 AI 结果。</p>'}</div>
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
        <div><strong>${esc(familyName(r.family_id))}</strong> ${badge(r.status, r.status === "approved" ? "ok" : "warn")} <span class="muted">${esc(r.week_label)}</span></div>
        <button onclick="createReportTask(${r.id})">加入发送任务</button>
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
  `).join("") : '<p class="empty">暂无周报。</p>';
}

// 渲染回复页面。
function renderReplyPage() {
  $("replyFamilySelect").innerHTML = optionList(state.selectedFamilyId);
  $("replyFamilies").innerHTML = state.families.length ? state.families.map((family) => `
    <button class="list-item ${family.family_id === state.selectedFamilyId ? "selected" : ""}" onclick="prepareReply('${esc(family.family_id)}', false)">
      <strong>${esc(family.parent_nickname || family.family_id)}</strong>
      <span>${esc(family.message_count)} 条消息</span>
    </button>
  `).join("") : '<p class="empty">暂无家庭。</p>';
  renderReplyContext();
  $("replyOutputs").innerHTML = state.outputs.filter((item) => item.agent_type === "ai_reply").slice(0, 8).map((item) => outputCard(item)).join("") || '<p class="empty">暂无回复建议。</p>';
}

// 渲染回复上下文。
async function renderReplyContext() {
  if (!state.selectedFamilyId) return;
  const data = await api(`/api/families/${encodeURIComponent(state.selectedFamilyId)}`);
  $("replyContext").innerHTML = data.messages.slice(-10).map((m) => `
    <div class="msg"><strong>${esc(m.speaker)}</strong><p>${esc(m.content)}</p><span class="muted">${esc(m.message_time)}</span></div>
  `).join("") || '<p class="empty">暂无聊天上下文。</p>';
}

// 渲染打卡记录。
function renderCheckins() {
  const outputs = state.outputs.filter((item) => item.agent_type === "checkin_pbl");
  $("checkinBoard").innerHTML = outputs.length ? outputs.map((item) => outputCard(item)).join("") : `
    <p class="empty">暂无打卡/PBL识别结果。可从家庭列表或本页批量识别生成。</p>
  `;
}

// 渲染任务列表。
function renderTasks() {
  $("taskTable").innerHTML = table([
    { label: "ID", key: "id" },
    { label: "家庭", render: (r) => esc(familyName(r.family_id)) },
    { label: "对象", key: "target_name" },
    { label: "来源/场景", key: "scene" },
    { label: "状态", render: (r) => badge(r.status, r.status === "sent" ? "ok" : r.status === "cancelled" ? "" : "warn") },
    { label: "发送设备", render: (r) => deviceSelect(r) },
    { label: "企微模式", render: (r) => sendModeSelect(r) },
    { label: "最终内容", render: (r) => `<textarea id="task-${r.id}">${esc(r.content)}</textarea>` },
    { label: "操作", render: (r) => `
      <div class="cell-actions">
        <button onclick="saveTask(${r.id})">保存</button>
        ${r.status === "pending" ? `<button onclick="sendTask(${r.id})">发送</button><button onclick="cancelTask(${r.id})">取消</button>` : ""}
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
    { label: "截图", render: (r) => r.screenshot_path ? `<a class="dl-link" href="${esc(r.screenshot_path)}" target="_blank" rel="noopener">查看</a>` : "—" },
    { label: "详情", key: "detail" },
  ], state.logs);
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
  renderDevices();
  renderArkConfig();
  renderTemplates();
}

// 刷新所有内容。
async function refreshAll() {
  return withAction("刷新数据", async () => {
    const [families, profiles, reports, templates, tasks, logs, outputs, accounts, conversations, devices, arkConfig] = await Promise.all([
      api("/api/families"),
      api("/api/profiles"),
      api("/api/reports"),
      api("/api/templates"),
      api("/api/send-tasks"),
      api("/api/send-logs"),
      api("/api/ai-outputs"),
      api("/api/test-chat/accounts"),
      api("/api/test-chat/conversations"),
      api("/api/devices"),
      api("/api/ark-config").catch(() => ({})),
    ]);
    Object.assign(state, { families, profiles, reports, templates, tasks, logs, outputs, accounts, conversations, devices, arkConfig });
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
    if (report.status !== "approved") await approveReport(id);
    const family = state.families.find((item) => item.family_id === report.family_id);
    await api("/api/send-tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        family_id: report.family_id,
        target_name: family?.parent_nickname || report.family_id,
        scene: "周报发送",
        content: $(`report-${id}`).value,
        device_id: "",
        send_mode: "dry_run",
      }),
    });
    toast("周报已加入发送任务");
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

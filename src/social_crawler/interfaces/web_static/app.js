"use strict";
const icons = {
  chevron: '<path d="m9 5 7 7-7 7"/>',
  paperclip: '<path d="m9 13 6-6a3 3 0 0 1 4 4l-8 8a5 5 0 0 1-7-7l9-9"/>',
  file: '<path d="M13 2H5v20h14V8zM13 2v6h6M8 12h8M8 16h6"/>',
  edit: '<path d="m15 4 5 5M4 20l5-1L21 7l-5-5L4 14z"/>',
  copy: '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  dashboard:
    '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  library:
    '<rect x="4" y="4" width="16" height="17" rx="2"/><path d="M8 4V2m8 2V2M8 9h8M8 13h8M8 17h5"/>',
  settings:
    '<path d="m9 3-1 3-3 1v4l2 1v3l-2 1 2 4 3-1 2 2 3-2 3 1 2-4-2-1v-3l2-1V7l-3-1-1-3z"/><circle cx="12" cy="12" r="3"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
  play: '<path d="m8 5 11 7-11 7z"/>',
  refresh:
    '<path d="M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-2l2 3M4 16l2 3a7 7 0 0 0 12-2"/>',
  activity: '<path d="M2 12h5l3-8 4 16 3-8h5"/>',
  comment:
    '<path d="M21 11a8 8 0 0 1-8 8H6l-4 3 1-7a8 8 0 1 1 18-4zM7 10h10M7 14h6"/>',
  shield:
    '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z"/><path d="m8 12 3 3 5-6"/>',
  spark:
    '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5z"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  link: '<path d="M14 3h7v7m0-7L10 14M10 4H4v16h16v-6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  db: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/>',
  heart: '<path d="M12 21 3 12C-3 4 9-1 12 6c3-7 15-2 9 6z"/>',
};
const icon = (name) =>
  `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.library}</svg>`;
const $ = (selector, parent = document) => parent.querySelector(selector);
const $$ = (selector, parent = document) => [
  ...parent.querySelectorAll(selector),
];
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const number = (value) => new Intl.NumberFormat("zh-CN").format(value || 0);
const when = (ts) =>
  ts
    ? new Date(ts * 1000).toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      })
    : "—";
const PLATFORM_ORDER = ["xhs", "rednote", "douyin"];
const PLATFORM_OPTIONS = [
  ["xhs", "小红书"],
  ["rednote", "小红书国际版"],
  ["douyin", "抖音"],
];
const platformName = (p) =>
  ({ xhs: "小红书", rednote: "小红书国际版", douyin: "抖音" })[p] || p;
const logo = (p) =>
  `<img class="platform-logo ${p}" src="/favicon.svg" alt="" aria-hidden="true" decoding="async">`;
const platformIdentity = (p) =>
  `<div class="platform-identity"><div class="brand-slot">${logo(p)}</div><span class="platform-caption">${p === "rednote" ? "rednote · 国际版" : platformName(p)}</span></div>`;
const isXhsFamily = (p) => ["xhs", "rednote"].includes(p);
const platformIntro = (p) =>
  p === "xhs"
    ? "探索生活里的灵感"
    : p === "rednote"
      ? "浏览小红书国际版内容"
      : "发现视频里的趋势";
const labels = {
  completed: "已完成",
  running: "采集中",
  queued: "排队中",
  pending: "待启动",
  partial: "部分完成",
  blocked: "已暂停",
  canceled: "已停止",
  failed: "失败",
  interrupted: "已中断",
};
const statusBadge = (s, custom) =>
  `<span class="badge ${s === "completed" ? "good" : s === "running" ? "running" : ["failed", "blocked"].includes(s) ? "bad" : ["partial", "pending", "queued", "interrupted"].includes(s) ? "warn" : ""}">${esc(custom || labels[s] || uiLabel("states", s, "未知状态"))}</span>`;
const safeURL = (value) => {
  try {
    const u = new URL(value);
    return ["http:", "https:"].includes(u.protocol) ? u.href : "";
  } catch {
    return "";
  }
};
function preferredMedia(platform, id, data) {
  const downloaded = (data.media_downloads || []).filter(
    (asset) => asset.status === "downloaded",
  );
  const asset =
    downloaded.find((row) => row.kind === "video") ||
    downloaded.find((row) => row.kind === "image") ||
    downloaded[0];
  if (asset) {
    const local = new URL("/api/media", window.location.origin);
    local.search = new URLSearchParams({
      platform,
      id,
      index: asset.media_index,
    });
    return { url: safeURL(local.href), video: asset.kind === "video", local: true };
  }
  const url = safeURL((data.media || [])[0]);
  return {
    url,
    video: platform === "douyin" && /video|\.mp4|mime_type=video/i.test(url),
    local: false,
  };
}
const state = {
  page: "overview",
  platform: "xhs",
  settingsPlatform: "xhs",
  selectedAccountId: "",
  accountFilters: { platform: "", q: "" },
  token: "",
  settings: null,
  dashboard: { runs: [], stats: {}, jobs: [] },
  runFilters: defaultRunFilters(),
  contentFilters: defaultContentFilters(),
  filter: "",
  query: "",
  resultsPage: 1,
  runFilter: "",
  seenJobs: new Set(),
  databaseError: "",
  modalRun: null,
  risk: { groups: [], risk_events: [] },
};
let toastTimer,
  pollBusy = false,
  librarySequence = 0;
function defaultRunFilters() {
  return {
    platform: "",
    source_type: "",
    status: "",
    mode: "",
    time_field: "executed_at",
    date_from: "",
    date_to: "",
    q: "",
    page: 1,
  };
}
function defaultContentFilters() {
  return {
    time_field: "published_at",
    date_from: "",
    date_to: "",
    time_known: "",
    author: "",
    mode: "",
    detail: "",
    sort: "collected_at",
  };
}
function options(items, value) {
  return items
    .map(
      ([v, label]) =>
        `<option value="${esc(v)}" ${String(value ?? "") === v ? "selected" : ""}>${esc(label)}</option>`,
    )
    .join("");
}
function consistencyPolicyField(value = "warn") {
  return `<label><span class="label">环境一致性策略</span><select class="field" name="consistency_policy">${options(
    [
      ["warn", "告警并继续（推荐）"],
      ["strict", "严重不一致时拒绝运行"],
      ["off", "关闭检查"],
    ],
    value || "warn",
  )}</select><small class="field-help">登录时保存环境快照；采集时比较浏览器和网络请求身份。告警模式不会中断任务。</small></label>`;
}
function browserProviderFields(account) {
  if (account.platform && !isXhsFamily(account.platform) && account.platform !== "douyin") return "";
  const provider = account.browser_provider || "chromium";
  const providerOptions = [
    ["chromium", "内置 Chromium（默认）"],
    ["adspower", "AdsPower 环境"],
    ...(isXhsFamily(account.platform) ? [["kameleo", "Kameleo 环境"]] : []),
  ];
  const providerHelp = account.platform === "douyin"
    ? "AdsPower 提供登录状态与 Web 签名；未配置代理时两条链路均直连，配置代理时需保持出口一致。"
    : "AdsPower / Kameleo 模式由对应环境管理浏览器代理，无需在项目代理池中重复配置。";
  const profileField = (kind, label, current, help) => `<div class="browser-profile-picker" data-browser-profile-provider="${kind}" ${provider === kind ? "" : "hidden"}><label><span class="label">${label}</span><select class="field" name="${kind}_profile_id" data-browser-profile-select><option value="">请选择浏览器环境</option>${current ? `<option value="${esc(current)}" selected>当前绑定 · ${esc(current)}</option>` : ""}</select><small class="field-help" data-browser-profile-status>${esc(help)}</small></label><button class="button small" type="button" data-refresh-browser-profiles="${kind}">${icon("refresh")}刷新环境</button></div>`;
  return `<label><span class="label">浏览器运行方式</span><select class="field" name="browser_provider">${options(
    providerOptions,
    provider,
  )}</select><small class="field-help" data-browser-provider-help>${providerHelp}</small></label>${profileField("adspower", "AdsPower 浏览器环境", account.adspower_profile_id, "从当前 API key 有权访问的环境中选择；同一环境只能绑定一个账号。")}${isXhsFamily(account.platform) ? profileField("kameleo", "Kameleo 浏览器环境", account.kameleo_profile_id, "从当前 Kameleo 工作区选择；仅支持桌面 Chrome 环境。") : ""}`;
}

async function loadBrowserProfiles(form, provider, context = {}, force = false) {
  const picker = $(`[data-browser-profile-provider="${provider}"]`, form);
  const select = picker && $("[data-browser-profile-select]", picker);
  const status = picker && $("[data-browser-profile-status]", picker);
  if (!picker || !select || (!force && picker.dataset.loaded === "true")) return;
  const current = select.value;
  select.disabled = true;
  status.textContent = "正在读取浏览器环境…";
  try {
    const query = new URLSearchParams({
      provider,
      platform: context.platformSelect?.value || context.platform || "xhs",
    });
    if (context.accountId) query.set("account_id", context.accountId);
    const result = await api(`/api/browser-profiles?${query}`);
    select.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = result.items.length ? "请选择浏览器环境" : "没有可用的浏览器环境";
    select.append(placeholder);
    let currentFound = false;
    for (const item of result.items) {
      const option = document.createElement("option");
      option.value = item.id;
      const notes = [item.detail, item.state, item.bound_account_name ? `已绑定：${item.bound_account_name}` : ""]
        .filter(Boolean)
        .join(" · ");
      option.textContent = `${item.name}${notes ? ` · ${notes}` : ""} · ${item.id}`;
      option.disabled = !item.available && item.id !== current;
      if (item.id === current) {
        option.selected = true;
        currentFound = true;
      }
      select.append(option);
    }
    if (current && !currentFound) {
      const option = document.createElement("option");
      option.value = current;
      option.textContent = `当前绑定（环境列表中未返回） · ${current}`;
      option.selected = true;
      select.append(option);
    }
    picker.dataset.loaded = "true";
    status.textContent = result.items.length
      ? `已读取 ${result.items.length} 个环境；已绑定其他账号或不兼容的环境不可选择。`
      : "当前账号没有返回可选择的浏览器环境。";
  } catch (error) {
    status.textContent = error.message;
  } finally {
    select.disabled = picker.hidden;
  }
}

function bindBrowserProfileFields(form, context = {}) {
  const providerSelect = form.elements.browser_provider;
  if (!providerSelect) return;
  const sync = (force = false) => {
    const platform = context.platformSelect?.value || context.platform || "xhs";
    const supported = provider => provider === "adspower"
      ? isXhsFamily(platform) || platform === "douyin"
      : provider === "kameleo" && isXhsFamily(platform);
    for (const option of providerSelect.options)
      if (["adspower", "kameleo"].includes(option.value)) option.disabled = !supported(option.value);
    if (["adspower", "kameleo"].includes(providerSelect.value) && !supported(providerSelect.value))
      providerSelect.value = "chromium";
    const help = $("[data-browser-provider-help]", form);
    if (help) help.textContent = platform === "douyin"
      ? "AdsPower 提供登录状态与 Web 签名；未配置代理时两条链路均直连，配置代理时需保持出口一致。"
      : "AdsPower / Kameleo 模式由对应环境管理浏览器代理，无需在项目代理池中重复配置。";
    for (const picker of $$('[data-browser-profile-provider]', form)) {
      const active = supported(picker.dataset.browserProfileProvider)
        && picker.dataset.browserProfileProvider === providerSelect.value;
      picker.hidden = !active;
      const select = $("[data-browser-profile-select]", picker);
      select.disabled = !active;
      select.required = active;
    }
    if (supported(providerSelect.value))
      loadBrowserProfiles(form, providerSelect.value, context, force);
  };
  providerSelect.addEventListener("change", () => sync());
  context.platformSelect?.addEventListener("change", () => sync());
  $$('[data-refresh-browser-profiles]', form).forEach((button) => {
    button.onclick = () => {
      const picker = button.closest("[data-browser-profile-provider]");
      delete picker.dataset.loaded;
      loadBrowserProfiles(form, button.dataset.refreshBrowserProfiles, context, true);
    };
  });
  sync();
}
function todayShanghai() {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}
function stamp(value) {
  if (!value) return "未知";
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return "未知";
  return new Date(n * (n >= 1e11 ? 1 : 1000)).toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}
function toast(message) {
  $("#toast").textContent = messageLabel(message);
  $("#toast").classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("#toast").classList.remove("visible"), 4500);
}
async function api(path, body, method) {
  method = method || (body === undefined ? "GET" : "POST");
  const res = await fetch(path, {
    method,
    headers:
      method === "GET"
        ? {}
        : {
            "Content-Type": "application/json",
            "X-Console-Token": state.token,
          },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data;
  try {
    data = await res.json();
  } catch {
    throw new Error("服务响应异常，请确认本地服务正在运行");
  }
  if (!res.ok) throw new Error(messageLabel(data.error) || "操作失败");
  return data;
}
async function action(button, fn) {
  if (button?.disabled) return;
  const original = button?.innerHTML;
  if (button) {
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span>处理中';
  }
  try {
    await fn();
  } catch (e) {
    toast(e.message);
  } finally {
    if (button?.isConnected) {
      button.disabled = false;
      button.innerHTML = original;
    }
  }
}
function empty(title, detail = "") {
  return `<div class="empty">${icon("library")}<p>${esc(title)}</p><small>${esc(detail)}</small></div>`;
}
function heading(kicker, title, description, actionHTML = "") {
  return `<div class="page-heading"><div><div class="eyebrow">${kicker}</div><h1>${title}</h1><p>${description}</p></div>${actionHTML}</div>`;
}
function go(page) {
  if (!["overview", "workbench", "accounts", "proxies", "library", "analysis", "automation", "settings"].includes(page))
    page = "overview";
  state.page = page;
  state.modalRun = null;
  $("#modal").close();
  location.hash = page;
  $$(".nav-item").forEach((b) =>
    b.classList.toggle("active", b.dataset.page === page),
  );
  $("#current-page").textContent = {
    overview: "总览大屏",
    workbench: "采集工作台",
    accounts: "账号池",
    proxies: "代理列表",
    library: "内容库",
    analysis: "Agent 分析",
    automation: "风控观测",
    settings: "系统配置",
  }[page];
  render();
  window.scrollTo(0, 0);
}
function render() {
  document.body.classList.toggle("analysis-page", state.page === "analysis");
  if (!state.settings) return;
  if (state.page === "overview") renderOverview();
  else if (state.page === "workbench") renderWorkbench();
  else if (state.page === "accounts") renderAccountPool();
  else if (state.page === "proxies") renderProxyPool();
  else if (state.page === "library") renderLibrary();
  else if (state.page === "analysis") renderAnalysis();
  else if (state.page === "automation") renderAutomation();
  else renderSystemSettings();
}
function databaseNotice() {
  return state.databaseError
    ? `<div class="notice">${esc(state.databaseError)} <a href="#settings">配置数据库 →</a></div>`
    : "";
}
function statsHTML() {
  const s = state.dashboard.stats;
  const active = state.dashboard.stats.running || 0;
  return [
    ["累计采集内容", s.contents, "library", "数据库已保存", "条内容"],
    ["评论与回复", s.comments, "comment", "关联内容沉淀", "条互动"],
    ["采集任务", s.runs, "dashboard", "双平台工作记录", "次任务"],
    [
      "正在运行",
      active,
      "activity",
      active ? "采集任务进行中" : "准备好下一次发现",
      "实时状态",
    ],
  ]
    .map(
      ([label, value, ic, caption, unit]) =>
        `<div class="stat"><div class="stat-label">${label}</div><div class="stat-icon">${icon(ic)}</div><strong>${state.databaseError ? "—" : number(value)}</strong><small><span>${active && ic === "activity" ? "●" : "↗"}</span>${caption} <span class="muted">/ ${unit}</span></small></div>`,
    )
    .join("");
}
function accountStatusHTML(p) {
  const a = state.settings.accounts[p];
  const profileManaged = a.cookie_state === "managed";
  const check = [...state.dashboard.jobs]
    .reverse()
    .find(
      (j) =>
        j.platform === p &&
        j.kind === "online" &&
        j.status === "completed" &&
        j.current !== false,
    );
  const login = check?.result?.login_state;
  const badge =
    login === "verified"
      ? "已登录"
      : login === "expired"
        ? "登录失效"
        : login === "unverified"
          ? "会话可用"
          : profileManaged
            ? "浏览器环境管理"
          : a.cookie_state === "configured"
            ? "登录凭据已配置"
            : "待配置";
  return `<div class="account-status">${logo(p)}<div><strong>${platformName(p)}</strong><small>${esc(a.account_ref)}</small></div><span class="badge ${["configured", "managed"].includes(a.cookie_state) ? "good" : "warn"}">${badge}</span></div>`;
}
function sourceSummary(config) {
  if (config?.source_type === "posts") return `指定帖子 · ${config.post_targets?.length || 0} 个目标`;
  return (config?.keywords || []).join(" · ");
}
function collectionFormHTML() {
  return `<form id="collect-form"><div class="panel-body"><div class="step-title"><span class="step">1</span>选择采集平台</div><div class="platform-picker">${PLATFORM_ORDER.map((p) => `<button type="button" class="platform-choice ${state.platform === p ? "selected" : ""}" data-platform="${p}" aria-pressed="${state.platform === p}">${logo(p)}<span><strong>${platformName(p)}</strong><small>${platformIntro(p)}</small></span><span class="radio"></span></button>`).join("")}</div><div class="notice compact">手动真实采集可以指定账号；保持自动分配时会选择同平台当前可用且最久未使用的账号。定时作业仍由账号池自动分配。</div>
 <div class="step-title"><span class="step">2</span><span>想采集什么内容？</span></div><label><span class="label">采集来源</span><select class="field" name="source_type"><option value="keyword">关键词搜索</option><option value="posts">指定帖子</option></select></label><div data-keyword-input><textarea class="field" id="keywords" name="keywords" rows="2" placeholder="输入关键词，如：咖啡、周末旅行、家居灵感" required maxlength="2000"></textarea><div class="suggestions"><span>试试这些</span>${["咖啡", "城市漫步", "家居灵感"].map((k) => `<button type="button" data-keyword="${k}">+ ${k}</button>`).join("")}<span>多个关键词用逗号或换行分隔</span></div></div><div data-post-input hidden><label><span class="label">帖子链接 / 分享文本 / 帖子标识</span><textarea class="field" name="post_targets" rows="4" maxlength="409600" placeholder="每行一个帖子，最多 100 个；支持完整链接及分享短链"></textarea></label><p class="hint">小红书与小红书国际版 建议粘贴带访问参数的完整分享链接。无效目标逐条展示；其余目标继续采集。再次执行会刷新所选帖子。</p></div>
 <div class="step-title"><span class="step">3</span>设置采集范围</div><label><span class="label">评论范围</span><select class="field" name="comment_scope"><option value="replies">一级评论与回复</option><option value="comments">仅一级评论</option><option value="none">仅帖子详情</option></select></label><p class="hint">默认每帖 5 条一级评论，展开 1 个评论楼层，每楼最多 3 条回复；可在下方调整。</p><div class="form-grid"><label><span class="label">每个关键词 · 内容数</span><input class="field" name="content_limit" type="number" min="1" max="1000" value="10" required></label><label><span class="label">每条内容 · 一级评论</span><input class="field" name="comment_limit" type="number" min="0" max="10000" value="5" required></label><label><span class="label">排序方式</span><select class="field" name="sort"><option value="latest" selected>最新发布</option><option value="general">综合排序</option></select></label><label data-xhs-adapter><span class="label">采集方式</span><select class="field" name="adapter"><option value="browser">浏览器网络响应</option><option value="httpx">直接接口请求（试用）</option><option value="curl_cffi">模拟浏览器指纹请求（试用）</option></select><small class="field-help">接口请求模式复用已有 登录凭据；采集阶段不启动持久浏览器。模拟浏览器指纹方式仍需小范围测试。</small></label></div><label class="checkbox-label"><input name="download_media" type="checkbox">下载图片和视频到本地存储<small class="field-help">默认关闭；开启后仅在取得帖子详情时下载到持久存储。</small></label>
 <details class="advanced"><summary>高级设置 · 回复、请求频率与预算</summary><div class="form-grid">${[
   ["reply_parents", "采集回复的父评论数", 1, 0, 100],
   ["reply_limit", "每条父评论的回复数", 3, 0, 2000],
   ["min_interval", "请求间隔（秒）", 5, 0, 300],
   ["max_requests", "请求上限", 200, 1, 100000],
   ["max_pages", "最大页数", 100, 1, 1000],
   ["max_seconds", "运行时长（秒）", 900, 1, 86400],
   ["request_timeout", "请求超时（秒）", 30, 1, 120],
 ]
   .map(
     ([key, label, value, min, max]) =>
       `<label><span class="label">${label}</span><input class="field" type="number" name="${key}" value="${value}" min="${min}" max="${max}" required>${key === "max_requests" ? '<small class="field-help">单次任务的请求总上限，与风控监测中的滚动窗口额度分别计算。</small>' : ""}</label>`,
   )
   .join("")}<label><span class="label">浏览器运行方式</span><select class="field" name="browser_mode"><option value="inherit">跟随账号默认</option><option value="headed">显示浏览器窗口</option><option value="headless">隐藏浏览器窗口</option></select><small class="field-help">只影响本次任务，不修改账号默认值。</small></label></div></details>
 <div class="step-title"><span class="step">4</span>选择执行方式</div><div class="form-grid two"><label><span class="label">触发方式</span><select class="field" name="trigger_type"><option value="manual">手动触发一次</option><option value="schedule">增加定时作业</option></select></label><label data-account-field><span class="label">执行账号</span><select class="field" name="account_id"><option value="">自动分配</option></select><small class="field-help">指定后不会切换到其他账号；账号忙碌时任务会排队。</small></label></div>
 <div data-schedule-fields hidden><div class="form-grid two"><label><span class="label">作业名称</span><input class="field" name="schedule_name" placeholder="例如：工作日品牌观察"></label><label><span class="label">常用周期</span><select class="field" name="cron_preset"><option value="0 */6 * * *">每 6 小时</option><option value="0 9 * * *">每天 09:00</option><option value="0 9 * * 1-5">工作日 09:00</option><option value="0 9 * * 1">每周一 09:00</option><option value="0 9 1 * *">每月 1 日 09:00</option><option value="custom">自定义</option></select></label><label><span class="label">定时表达式</span><input class="field cron-field" name="cron_expression" value="0 */6 * * *" placeholder="分 时 日 月 周" spellcheck="false"><small class="field-help">5 段格式：分 时 日 月 周，例如 */30 9-18 * * 1-5</small></label><label><span class="label">时区</span><input class="field" name="timezone" value="Asia/Shanghai" list="schedule-timezones"><datalist id="schedule-timezones"><option value="Asia/Shanghai"><option value="UTC"><option value="Asia/Tokyo"><option value="America/New_York"></datalist></label></div><p class="hint">支持通配符、列表、范围和步长；最短执行间隔 15 分钟。每次到点都会生成独立任务，再由账号池分配执行账号。</p></div></div>
 <div class="form-actions"><span class="hint" data-account-availability></span><button class="button primary" type="submit">${icon("play")}<span data-submit-label>开始采集</span></button></div></form>`;
}
function openCollection(triggerType = "manual") {
  state.modalRun = null;
  showModal("新建采集", collectionFormHTML());
  $("#modal").classList.add("collection-modal");
  const collectionForm = $("#collect-form");
  const renderAccountAvailability = () => {
    const rows = (state.settings.account_pool || []).filter(
      (account) => account.platform === state.platform && account.status === "ready",
    );
    const select = $("[name=account_id]");
    const selected = select.value;
    select.innerHTML = options(
      [
        ["", "自动分配（最久未使用）"],
        ...rows.map((account) => [
          account.id,
          `${account.name} · ${account.account_ref}`,
        ]),
      ],
      selected,
    );
    $("[data-account-availability]").textContent = rows.length
      ? `${platformName(state.platform)}当前有 ${rows.length} 个可用账号，可自动分配或手动指定。`
      : `${platformName(state.platform)}当前没有可用账号，真实采集暂时无法提交。`;
  };
  renderAccountAvailability();
  const renderSourceFields = () => {
    const posts = $("[name=source_type]", collectionForm).value === "posts";
    $("[data-keyword-input]").hidden = posts;
    $("#keywords").required = !posts;
    $("[data-post-input]").hidden = !posts;
    $("[name=post_targets]").required = posts;
    for (const name of ["content_limit", "sort"])
      $(`[name=${name}]`).closest("label").hidden = posts;
  };
  $("[name=source_type]", collectionForm).onchange = renderSourceFields;
  renderSourceFields();
  const renderCommentFields = () => {
    const scope = $("[name=comment_scope]").value;
    $("[name=comment_limit]").closest("label").hidden = scope === "none";
    for (const name of ["reply_parents", "reply_limit"])
      $(`[name=${name}]`).closest("label").hidden = scope !== "replies";
  };
  $("[name=comment_scope]").onchange = renderCommentFields;
  renderCommentFields();
  const renderAdapterFields = () => {
    $("[data-xhs-adapter]").hidden = !isXhsFamily(state.platform);
    $("[name=browser_mode]").closest("label").hidden =
      isXhsFamily(state.platform) && $("[name=adapter]").value !== "browser";
  };
  $("[name=adapter]").onchange = renderAdapterFields;
  renderAdapterFields();
  $$("[data-platform]").forEach(
    (b) =>
      (b.onclick = () => {
        state.platform = b.dataset.platform;
        $$("[data-platform]").forEach((x) => {
          x.classList.toggle("selected", x === b);
          x.setAttribute("aria-pressed", String(x === b));
        });
        renderAccountAvailability();
        renderAdapterFields();
      }),
  );
  $$("[data-keyword]").forEach(
    (b) =>
      (b.onclick = () => {
        const input = $("#keywords");
        input.value = [input.value.trim(), b.dataset.keyword]
          .filter(Boolean)
          .join("，");
        input.focus();
      }),
  );
  const trigger = $("[name=trigger_type]");
  bindCronPreset($("#collect-form"));
  const renderExecutionFields = () => {
    const scheduled = trigger.value === "schedule";
    $("[data-schedule-fields]").hidden = !scheduled;
    $("[data-account-field]").hidden = scheduled;
    $("[name=account_id]").disabled = scheduled;
    $("[name=schedule_name]").required = scheduled;
    $("[name=cron_expression]").required = scheduled;
    $("[name=timezone]").required = scheduled;
    $("[data-submit-label]").textContent = scheduled ? "创建定时作业" : "开始采集";
  };
  if (triggerType === "schedule") trigger.value = "schedule";
  trigger.onchange = renderExecutionFields;
  renderExecutionFields();
  $("#collect-form").onsubmit = (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    action($("[type=submit]", form), async () => {
      const values = Object.fromEntries(new FormData(form));
      const config = {
        platform: state.platform,
        source_type: values.source_type,
        keywords: values.source_type === "posts" ? [] : values.keywords
          .split(/[,，\n]+/).map((s) => s.trim()).filter(Boolean),
        post_targets: values.source_type === "posts"
          ? values.post_targets.split(/\n+/).map((s) => s.trim()).filter(Boolean) : [],
        sort: values.sort,
      };
      if (isXhsFamily(state.platform)) config.adapter = values.adapter;
      config.download_media = values.download_media === "on";
      for (const key of [
        "content_limit",
        "comment_limit",
        "reply_parents",
        "reply_limit",
        "min_interval",
        "max_requests",
        "max_pages",
        "max_seconds",
        "request_timeout",
      ])
        config[key] = Number(values[key]);
      if (values.comment_scope === "none") config.comment_limit = 0;
      if (values.comment_scope !== "replies") config.reply_parents = config.reply_limit = 0;
      if (values.browser_mode !== "inherit")
        config.headless = values.browser_mode === "headless";
      if (values.trigger_type === "schedule") {
        await api("/api/schedules", {
          name: values.schedule_name,
          kind: "cron",
          cron_expression: values.cron_expression,
          timezone: values.timezone,
          config,
        });
        await reloadControlPlane();
        $("#modal").close();
        if (state.page === "workbench") renderWorkbench();
        toast("定时作业已创建；到点后会自动选择可用账号");
      } else {
        const request = { config };
        if (values.account_id)
          request.account_id = values.account_id;
        const job = await api("/api/runs", request);
        toast(
          values.account_id
            ? "采集任务已创建，将使用指定账号执行"
            : "采集任务已创建，系统正在分配可用账号",
        );
        await refresh();
        openRun(job.run_id);
      }
    });
  };
}
function runFilterHTML() {
  const f = state.runFilters;
  return `<form id="run-filters" class="filter-form task-filters">
    <label><span class="label">平台</span><select name="platform" class="field">${options(
      [
        ["", "全部平台"],
        ["xhs", "小红书"],
        ["rednote", "小红书国际版"],
        ["douyin", "抖音"],
      ],
      f.platform,
    )}</select></label>
    <label><span class="label">采集来源</span><select name="source_type" class="field">${options([["", "全部"], ["keyword", "关键词"], ["posts", "指定帖子"]], f.source_type)}</select></label>
    <label><span class="label">任务状态</span><select name="status" class="field">${options(
      [
        ["", "全部状态"],
        ["running", "运行中 / 已中断"],
        ["pending", "待启动"],
        ["completed", "已完成"],
        ["partial", "部分完成"],
        ["blocked", "已暂停"],
        ["canceled", "已停止"],
      ],
      f.status,
    )}</select></label>
    <label><span class="label">日期维度</span><select name="time_field" class="field">${options(
      [
        ["executed_at", "执行日期（含恢复）"],
        ["created_at", "任务创建日期"],
      ],
      f.time_field,
    )}</select></label>
    <label><span class="label">开始日期</span><input class="field" name="date_from" type="date" value="${esc(f.date_from)}"></label>
    <label><span class="label">结束日期</span><input class="field" name="date_to" type="date" value="${esc(f.date_to)}"></label>
    <label class="filter-keyword"><span class="label">关键词 / 帖子标识 / 任务 / 账号</span><input class="field" name="q" placeholder="查找采集任务或执行账号" value="${esc(f.q)}"></label>
    <div class="filter-submit"><button class="button primary" type="submit">${icon("search")}筛选</button><button class="button" type="button" id="reset-run-filters">重置</button></div>
  </form>`;
}
function renderOverview() {
  const runs = state.dashboard.runs || [];
  const accounts = state.settings.account_pool || [];
  const statuses = [
    ["completed", "已完成"],
    ["running", "运行中"],
    ["partial", "部分完成"],
    ["blocked", "风控暂停"],
    ["failed", "失败"],
  ];
  const maximum = Math.max(1, ...statuses.map(([key]) => runs.filter((r) => r.status === key).length));
  const accountCounts = {
    ready: accounts.filter((a) => a.status === "ready").length,
    busy: new Set(
      state.dashboard.jobs
        .filter((job) => ["queued", "running"].includes(job.status) && job.account_id)
        .map((job) => job.account_id),
    ).size,
    attention: accounts.filter((a) =>
      ["cooling", "probe_due", "recovering", "canary_running", "login_required"].includes(a.status),
    ).length,
  };
  $("#main").innerHTML =
    heading(
      "运行概览",
      "总览大屏",
      "采集规模、任务状态、账号容量和风控信号的运行概览。",
      `<div class="stack"><button class="button" id="refresh-overview">${icon("refresh")}刷新</button><button class="button primary" data-new-collection>${icon("plus")}新建采集</button></div>`,
    ) +
    `<div id="database-notice">${databaseNotice()}</div><section class="stats">${statsHTML()}</section>
    <div class="overview-grid"><section class="panel overview-chart"><div class="panel-title"><h2>近期任务状态</h2><span>最近 ${runs.length} 条记录</span></div><div class="panel-body status-bars">${statuses.map(([key, label]) => { const value = runs.filter((run) => run.status === key).length; return `<div class="status-bar"><span>${label}</span><div>${value ? `<span class="status-bar-fill ${key}" style="width:${(value / maximum) * 100}%"></span>` : ""}</div><strong>${value}</strong></div>`; }).join("")}</div></section>
    <section class="panel"><div class="panel-title"><h2>账号池容量</h2><button class="button small subtle" data-goto="accounts">管理账号 ${icon("arrow")}</button></div><div class="panel-body capacity-grid"><div><strong>${accountCounts.ready}</strong><span>可运行</span></div><div><strong>${accountCounts.busy}</strong><span>执行中 / 排队</span></div><div><strong>${accountCounts.attention}</strong><span>需要处理</span></div><div><strong>${accounts.length}</strong><span>账号总数</span></div></div></section>
    <section class="panel"><div class="panel-title"><h2>采集环境</h2><span>按平台</span></div><div class="panel-body overview-accounts">${PLATFORM_ORDER.map((platform) => { const rows = accounts.filter((a) => a.platform === platform); return `<div><div class="brand-slot">${logo(platform)}</div><span><strong>${platformName(platform)}</strong><small>${rows.filter((a) => a.status === "ready").length} 可用 / ${rows.length} 总计</small></span></div>`; }).join("")}</div></section>
    <section class="panel"><div class="panel-title"><h2>服务运行</h2><button class="button small subtle" data-goto="settings">系统配置 ${icon("arrow")}</button></div><div class="panel-body system-summary"><div><span>数据库</span><strong>${state.settings.database.startsWith("sqlite") ? "未配置 PostgreSQL" : "PostgreSQL"}</strong></div><div><span>采集进程</span><strong>${state.settings.runtime.workers}</strong></div><div><span>队列</span><strong>${state.dashboard.jobs.filter((j) => j.status === "queued").length}</strong></div><div><span>风控事件</span><strong>${(state.risk.risk_events || []).length}</strong></div></div></section></div>`;
  $("#refresh-overview").onclick = (e) => action(e.currentTarget, () => refresh(true));
  bindNavigation();
}
function renderWorkbench() {
  $("#main").innerHTML =
    heading(
      "任务管理",
      "采集工作台",
      "按关键词发现内容，或采集指定帖子的详情、评论与回复。",
      `<div class="stack"><button class="button" id="refresh-dashboard">${icon("refresh")}刷新</button><button class="button primary" id="new-collection">${icon("plus")}新建采集</button></div>`,
    ) +
    `<div id="database-notice">${databaseNotice()}</div><section class="panel table-panel" id="workbench-section-runs" role="tabpanel" aria-labelledby="workbench-tab-runs"><div class="panel-title"><h2>采集任务记录 <span class="badge" id="run-count">${state.dashboard.total ?? 0}</span></h2><span>自动更新 · 日期按北京时间筛选</span></div>${runFilterHTML()}<div id="runs-table">${runsTable()}</div></section>`;
  $("#new-collection").onclick = openCollection;
  $("#refresh-dashboard").onclick = (e) =>
    action(e.currentTarget, () => refresh(true));
  $("#run-filters").onsubmit = (e) => {
    e.preventDefault();
    state.runFilters = {
      ...Object.fromEntries(new FormData(e.currentTarget)),
      page: 1,
    };
    action($("[type=submit]", e.currentTarget), () => refresh());
  };
  $("#reset-run-filters").onclick = () => {
    state.runFilters = defaultRunFilters();
    renderWorkbench();
    refresh().catch((e) => toast(e.message));
  };
  bindNavigation();
  bindRunActions();
  ConsoleLists.setupWorkbench();
}
function runsTable() {
  if (state.databaseError && !state.dashboard.runs.length)
    return empty("暂时无法读取采集记录", "请检查数据库连接后刷新");
  if (!state.dashboard.runs.length)
    return empty("没有匹配的采集任务", "调整筛选条件，或点击右上角新建采集");
  return `<div class="table-wrap"><table><thead><tr><th>采集关键词 / 任务标识</th><th>触发任务</th><th>执行账号</th><th class="platform-column">平台</th><th>状态</th><th>帖子数 / 评论数</th><th>创建时间</th><th>操作</th></tr></thead><tbody>${state.dashboard.runs.map((r) => {
    const trigger = r.schedule_name || (r.mode === "offline" ? "历史测试" : "手动采集");
    const account = r.account_name || (r.mode === "offline" ? "不使用账号" : "历史任务 · 未记录");
    return `<tr><td><div class="run-name">${esc(sourceSummary(r.config))}</div><div class="run-id">${r.id.slice(0, 12)} ${r.mode === "offline" ? "· 历史测试数据" : ""}</div></td><td><div class="run-context-name">${esc(trigger)}</div><div class="run-id">${r.schedule_id ? `定时 · ${r.schedule_id.slice(0, 8)}` : "单次任务"}</div></td><td><div class="run-context-name">${esc(account)}</div><div class="run-id">${r.account_id ? r.account_id.slice(0, 8) : "—"}</div></td><td class="platform-column">${platformIdentity(r.platform)}</td><td>${statusBadge(r.interrupted ? "interrupted" : r.status, r.cancel_requested && r.status === "running" ? "停止中" : null)}</td><td><div class="run-result-counts" aria-label="${number(r.post_count)} 个帖子，${number(r.comment_count)} 条评论"><span><strong>${number(r.post_count)}</strong><small>帖子</small></span><span><strong>${number(r.comment_count)}</strong><small>评论</small></span></div></td><td class="muted">${when(r.created_at)}</td><td><div class="table-actions"><button data-run="${r.id}">详情</button><button data-results="${r.id}">结果</button>${r.status === "running" && !r.interrupted ? `<button data-cancel="${r.id}">停止</button>` : ""}</div></td></tr>`;
  }).join("")}</tbody></table></div><div class="task-pagination"><span>共 ${number(state.dashboard.total)} 条 · ${state.dashboard.page} / ${Math.max(1, Math.ceil(state.dashboard.total / state.dashboard.page_size))} 页</span><div class="stack"><button class="button small" id="runs-prev" ${state.dashboard.page <= 1 ? "disabled" : ""}>上一页</button><button class="button small" id="runs-next" ${state.dashboard.page * state.dashboard.page_size >= state.dashboard.total ? "disabled" : ""}>下一页</button></div></div>`;
}
function bindNavigation() {
  $$("[data-new-collection]").forEach((b) => (b.onclick = openCollection));
  $$("[data-goto]").forEach((b) => (b.onclick = () => go(b.dataset.goto)));
}
function bindRunActions() {
  if ($("#runs-prev"))
    $("#runs-prev").onclick = () => {
      state.runFilters.page--;
      refresh().catch((e) => toast(e.message));
    };
  if ($("#runs-next"))
    $("#runs-next").onclick = () => {
      state.runFilters.page++;
      refresh().catch((e) => toast(e.message));
    };
  $$("[data-run]").forEach((b) => (b.onclick = () => openRun(b.dataset.run)));
  $$("[data-results]").forEach(
    (b) =>
      (b.onclick = () => {
        state.runFilter = b.dataset.results;
        state.resultsPage = 1;
        go("library");
      }),
  );
  $$("[data-cancel]").forEach(
    (b) =>
      (b.onclick = () =>
        action(b, async () => {
          const res = await api(`/api/runs/${b.dataset.cancel}/cancel`, {});
          toast(res.message);
          await refresh();
        })),
  );
}
async function refresh(notify = false) {
  let dashboard;
  try {
    dashboard = await api(
      "/api/dashboard?" + new URLSearchParams(state.runFilters),
    );
  } catch (e) {
    state.databaseError = e.message;
    if ($("#database-notice"))
      $("#database-notice").innerHTML = databaseNotice();
    if ($("#stats")) $("#stats").innerHTML = statsHTML();
    const jobs = await api("/api/jobs");
    state.dashboard.jobs = jobs.jobs;
    if ($("#account-status"))
      $("#account-status").innerHTML = PLATFORM_ORDER
        .map(accountStatusHTML)
        .join("");
    if (state.page === "settings") {
      refreshInlineChecks();
      return;
    }
    throw e;
  }
  if (["accounts", "proxies"].includes(state.page)) return;
  state.databaseError = "";
  if ($("#database-notice")) $("#database-notice").innerHTML = "";
  state.dashboard = dashboard;
  $("#library-count").textContent = number(dashboard.stats.contents);
  if (state.page === "overview") {
    renderOverview();
  } else if (state.page === "workbench" && $("#runs-table")) {
    $("#runs-table").innerHTML = runsTable();
    $("#run-count").textContent = dashboard.total;
    bindRunActions();
  }
  const newlyFinished = [];
  for (const job of dashboard.jobs) {
    if (!["queued", "running"].includes(job.status) && !state.seenJobs.has(job.id)) {
      state.seenJobs.add(job.id);
      newlyFinished.push(job);
    }
  }
  if (newlyFinished.length) {
    for (const job of newlyFinished)
      toast(
        job.status === "failed"
          ? job.message
          : job.kind === "collection"
            ? `任务${labels[job.result?.status] || "已结束"}`
            : job.result?.message || "检测完成",
      );
    const boot = await api("/api/bootstrap");
    state.settings = boot.settings;
    if (state.page === "system") renderSystemSettings();
    if (state.page === "workbench" && $("#account-status"))
      $("#account-status").innerHTML = PLATFORM_ORDER
        .map(accountStatusHTML)
        .join("");
    if (
      state.page === "settings" &&
      newlyFinished.some(
        (job) =>
          job.platform === state.settingsPlatform &&
          job.result?.account_ref_updated,
      )
    )
      renderAccountPool();
    if (state.page === "library") await loadResults();
  }
  if (state.page === "settings") refreshInlineChecks();
  if (state.modalRun && $("#modal").open) await refreshRun(state.modalRun);
  if (notify) toast("数据已刷新");
}
const contentTimeOptions = [
  ["published_at", "作者发布时间"],
  ["source_updated_at", "作者最后更新时间"],
  ["first_collected_at", "首次采集时间"],
  ["collected_at", "最后采集时间"],
];
function contentFilterHTML() {
  const f = state.contentFilters;
  return `<section class="panel library-filters"><div class="filter-intro"><span>${icon("clock")}按内容的生命周期筛选</span><div class="stack"><button class="button small" id="today-published">今天发布的笔记</button><button class="button small" id="today-collected">今天采集的内容</button><div class="library-filter-actions"><button class="button primary" type="submit" form="content-filters">${icon("search")}应用筛选</button><button class="button" type="button" id="clear-content-filters">重置</button></div></div></div>
<form class="filter-form" id="content-filters">
<label><span class="label">时间维度</span><select class="field" name="time_field">${options(contentTimeOptions, f.time_field)}</select></label>
<label><span class="label">开始日期</span><input class="field" type="date" name="date_from" value="${esc(f.date_from)}"></label>
<label><span class="label">结束日期</span><input class="field" type="date" name="date_to" value="${esc(f.date_to)}"></label>
<label><span class="label">时间信息</span><select class="field" name="time_known">${options(
    [
      ["", "全部"],
      ["known", "时间已知"],
      ["unknown", "仅时间未知"],
    ],
    f.time_known,
  )}</select></label>
<label><span class="label">作者 / 作者 ID</span><input class="field" name="author" value="${esc(f.author)}" placeholder="筛选作者"></label>

<label><span class="label">内容完整度</span><select class="field" name="detail">${options(
    [
      ["", "全部内容"],
      ["complete", "已获取详情"],
      ["partial", "摘要 / 部分内容"],
    ],
    f.detail,
  )}</select></label>
<label><span class="label">排序 · 最新优先</span><select class="field" name="sort">${options(contentTimeOptions, f.sort)}</select></label>
</form><p class="filter-note">日期按北京时间计算，包含结束当天。作者更新时间由平台提供，未提供时显示“未知”；历史数据的首次采集时间不作推测。</p></section>`;
}
function contentMetric(value) {
  if (value == null || value === "") return "—";
  return Number.isFinite(Number(value)) ? number(Number(value)) : esc(value);
}
function contentTimesHTML(item) {
  return `<dl class="content-times"><div><dt>作者发布时间</dt><dd>${stamp(item.published_at)}</dd></div><div><dt>作者最后更新时间</dt><dd>${stamp(item.source_updated_at)}</dd></div><div><dt>首次采集时间</dt><dd>${stamp(item.first_collected_at)}</dd></div><div><dt>最后采集时间</dt><dd>${stamp(item.observed_at)}</dd></div></dl>`;
}
function renderLibrary() {
  $("#main").innerHTML =
    heading(
      "采集成果",
      "内容库",
      "每一条内容，都是下一次发现的起点。",
      `<button class="button primary" data-new-collection>${icon("plus")}新建采集</button>`,
    ) +
    `
 <div class="toolbar"><div class="filter-group">${[
   ["", "全部内容"],
   ["xhs", "小红书"],
   ["rednote", "小红书国际版"],
   ["douyin", "抖音"],
 ]
   .map(
     ([p, n]) =>
       `<button data-filter="${p}" class="${state.filter === p ? "active" : ""}">${n}</button>`,
   )
   .join(
     "",
   )}</div><form class="search-form" id="library-search"><input class="field" name="q" placeholder="搜索已采集的标题、正文或作者" aria-label="搜索已采集内容" value="${esc(state.query)}"><button class="button" type="submit">${icon("search")}搜索</button></form></div>
 ${contentFilterHTML()}${state.runFilter ? `<div class="notice">正在查看任务 ${esc(state.runFilter.slice(0, 12))} 的关联内容（展示数据库中的最新记录）。 <button class="button small subtle" id="clear-run">查看全部</button></div>` : ""}<div id="library-results"><div class="loading"><span class="spinner"></span>正在读取内容库</div></div>`;
  $("#content-filters").onsubmit = (e) => {
    e.preventDefault();
    state.contentFilters = Object.fromEntries(new FormData(e.currentTarget));
    if (state.contentFilters.time_known === "unknown") {
      state.contentFilters.date_from = "";
      state.contentFilters.date_to = "";
      $("[name=date_from]", e.currentTarget).value = "";
      $("[name=date_to]", e.currentTarget).value = "";
    }
    state.resultsPage = 1;
    loadResults();
  };
  $("#today-published").onclick = () => {
    state.contentFilters = {
      ...state.contentFilters,
      time_field: "published_at",
      date_from: todayShanghai(),
      date_to: todayShanghai(),
      time_known: "known",
      sort: "published_at",
    };
    state.resultsPage = 1;
    renderLibrary();
  };
  $("#today-collected").onclick = () => {
    state.contentFilters = {
      ...state.contentFilters,
      time_field: "collected_at",
      date_from: todayShanghai(),
      date_to: todayShanghai(),
      time_known: "known",
      sort: "collected_at",
    };
    state.resultsPage = 1;
    renderLibrary();
  };
  $("#clear-content-filters").onclick = () => {
    state.contentFilters = defaultContentFilters();
    state.filter = "";
    state.query = "";
    state.runFilter = "";
    state.resultsPage = 1;
    renderLibrary();
  };
  bindNavigation();
  $$("[data-filter]").forEach(
    (b) =>
      (b.onclick = () => {
        state.filter = b.dataset.filter;
        state.resultsPage = 1;
        $$("[data-filter]").forEach((x) =>
          x.classList.toggle("active", x === b),
        );
        loadResults();
      }),
  );
  $("#library-search").onsubmit = (e) => {
    e.preventDefault();
    state.query = new FormData(e.currentTarget).get("q");
    state.resultsPage = 1;
    loadResults();
  };
  if ($("#clear-run"))
    $("#clear-run").onclick = () => {
      state.runFilter = "";
      renderLibrary();
    };
  loadResults();
}
function libraryPagination(data) {
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  const current = data.page;
  const start = Math.max(1, Math.min(current - 1, pages - 2));
  const visible = [...new Set([1, start, start + 1, start + 2, pages])]
    .filter((page) => page <= pages).sort((a, b) => a - b);
  const buttons = visible.map((page, index) =>
    `${index && page - visible[index - 1] > 1 ? '<span class="page-gap" aria-hidden="true">…</span>' : ""}<button class="button page-number" data-library-page="${page}" aria-label="第 ${page} 页" ${page === current ? 'aria-current="page" disabled' : ""}>${page}</button>`,
  ).join("");
  return `<nav class="library-pagination" aria-label="内容库分页"><span class="page-summary" role="status">第 ${current} / ${pages} 页 · 共 ${number(data.total)} 条</span><div class="library-page-buttons"><button class="button" id="prev-page" data-library-page="${current - 1}" ${current <= 1 ? "disabled" : ""}>上一页</button>${buttons}<button class="button" id="next-page" data-library-page="${current + 1}" ${current >= pages ? "disabled" : ""}>下一页</button></div><form id="library-page-jump" class="page-jump"><label for="library-target-page">跳至</label><input class="field" id="library-target-page" name="page" type="number" inputmode="numeric" min="1" max="${pages}" step="1" required aria-label="目标页码" placeholder="${current}"><span>页</span><button class="button" type="submit">跳转</button></form></nav>`;
}
async function loadResults({ page = state.resultsPage, scroll = false } = {}) {
  const sequence = ++librarySequence;
  const results = $("#library-results");
  results.setAttribute("aria-busy", "true");
  $$(".library-pagination button, .library-pagination input", results).forEach((el) => { el.disabled = true; });
  try {
    const query = new URLSearchParams({
      platform: state.filter,
      q: state.query,
      page,
      run_id: state.runFilter,
      ...state.contentFilters,
    });
    const data = await api("/api/results?" + query);
    if (sequence !== librarySequence || state.page !== "library") return;
    const pages = Math.max(1, Math.ceil(data.total / data.page_size));
    if (data.page > pages) return loadResults({ page: pages, scroll });
    state.resultsPage = data.page;
    $("#library-results").innerHTML =
      `<div class="result-count" tabindex="-1">共 ${number(data.total)} 条内容 <span>· 点击卡片预览正文与评论</span></div>` +
      (data.items.length
        ? `<div class="content-grid">${data.items
            .map((item) => {
              const d = item.data;
              const media = preferredMedia(item.platform, item.id, d);
              const { url, video } = media;
              return `<button class="content-card" data-content="${esc(item.id)}" data-content-platform="${item.platform}"><div class="card-cover"><span class="cover-art">${video ? "▶" : "✳"}</span>${url && !video ? `<img src="${esc(url)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ""}<span class="cover-type">${platformName(item.platform)} · ${item.source_mode === "offline" ? "历史测试数据" : video ? "视频" : "内容"}</span></div><div class="card-info"><h3>${esc(d.title || d.text || "未命名内容")}</h3><p class="card-meta"><span class="card-author">${esc(d.author_name || "未知作者")}</span><span class="card-metrics"><span title="平台点赞数" aria-label="点赞 ${contentMetric(d.metrics?.digg_count ?? d.metrics?.liked_count ?? d.metrics?.likedCount)}">${icon("heart")}${contentMetric(d.metrics?.digg_count ?? d.metrics?.liked_count ?? d.metrics?.likedCount)}</span><span title="平台评论数；未提供时显示 —" aria-label="评论 ${contentMetric(d.metrics?.comment_count ?? d.metrics?.commentCount ?? d.metrics?.comments_count)}">${icon("comment")}${contentMetric(d.metrics?.comment_count ?? d.metrics?.commentCount ?? d.metrics?.comments_count)}</span></span></p><div class="card-timestamps"><span>发布 ${stamp(item.published_at)}</span><span>采集 ${stamp(item.observed_at)}</span></div></div></button>`;
            })
            .join(
              "",
            )}</div>${libraryPagination(data)}`
        : empty(
            state.query ? "没有找到匹配内容" : "这里还没有内容",
            state.query
              ? "试试其他关键词，或切换平台筛选"
              : "创建采集任务后，内容会自动出现在这里",
          ));
    $$("[data-content]").forEach(
      (b) =>
        (b.onclick = () =>
          openContent(b.dataset.contentPlatform, b.dataset.content)),
    );
    bindMediaErrors();
    $$("[data-library-page]", results).forEach((button) => {
      button.onclick = () => loadResults({ page: Number(button.dataset.libraryPage), scroll: true });
    });
    if ($("#library-page-jump")) $("#library-page-jump").onsubmit = (event) => {
      event.preventDefault();
      const target = Number(new FormData(event.currentTarget).get("page"));
      if (Number.isInteger(target) && target >= 1 && target <= pages && target !== data.page)
        loadResults({ page: target, scroll: true });
    };
    if (scroll) {
      const count = $(".result-count", results);
      count.focus({ preventScroll: true });
      count.scrollIntoView({ block: "start", behavior: "instant" });
    }
  } catch (e) {
    if (sequence === librarySequence && $("#library-results"))
      $("#library-results").innerHTML =
        `<div class="error-panel"><p>${esc(e.message)}</p><button class="button" id="retry-library">重试</button></div>`;
    if ($("#retry-library")) $("#retry-library").onclick = loadResults;
  } finally {
    if (sequence === librarySequence) results.removeAttribute("aria-busy");
  }
}
function bindMediaErrors() {
  $$("img").forEach(
    (img) =>
      (img.onerror = () => {
        img.classList.add("media-failed");
      }),
  );
  $$("video").forEach(
    (video) =>
      (video.onerror = () => {
        video.replaceWith(
          Object.assign(document.createElement("p"), {
            className: "empty",
            textContent: "媒体链接暂时无法播放，请打开原文查看。",
          }),
        );
      }),
  );
}
function showModal(title, body) {
  const modal = $("#modal");
  modal.classList.remove("collection-modal", "account-modal");
  $("#modal-body").innerHTML =
    `<div class="modal-header"><h2>${title}</h2><button class="button subtle small" id="close-modal" aria-label="关闭">${icon("close")}</button></div><div class="modal-content">${body}</div>`;
  $("#close-modal").onclick = () => modal.close();
  if (!modal.open) modal.showModal();
  modal.scrollTop = 0;
}
async function openContent(platform, id) {
  state.modalRun = null;
  showModal(
    "内容预览",
    '<div class="loading"><span class="spinner"></span>正在读取内容</div>',
  );
  try {
    const data = await api(
      "/api/content?" + new URLSearchParams({ platform, id }),
    );
    if (!$("#modal").open) return;
    const d = data.item.data,
      media = preferredMedia(platform, id, d),
      url = media.url,
      video = media.video,
      original = safeURL(d.url);
    showModal(
      `${platformName(platform)} · 内容预览`,
      `<div class="preview-grid"><div class="preview-media">${url ? (video ? `<video controls preload="none" src="${esc(url)}"></video>` : `<img src="${esc(url)}" alt="内容媒体预览" referrerpolicy="no-referrer">`) : empty("暂无媒体预览", "可打开原文查看")}</div><div class="preview-copy"><span class="badge good">${platformName(platform)}</span><h3>${esc(d.title || "未命名内容")}</h3><p>${esc(d.author_name || "未知作者")} · ${stamp(data.item.published_at)}</p><p>${esc(d.text || "暂无正文")}</p><div class="metrics"><span>♡ ${number(d.metrics?.digg_count || d.metrics?.liked_count || 0)} 赞</span><span>${number(data.comments.length)} 条已采评论</span></div>${original ? `<a class="button" href="${esc(original)}" target="_blank" rel="noopener noreferrer">打开原文 ${icon("link")}</a>` : ""}<button class="button primary" id="analyze-post">分析这个帖子</button>${contentTimesHTML(data.item)}<div class="secondary-caption">${d.detail_complete ? "已获取内容详情" : "当前为搜索摘要或部分内容"} · 远程媒体可能受平台访问限制</div></div></div><section class="comments"><h3>评论与回复 <span class="badge">${data.comments.length}</span></h3>${data.comments.length ? data.comments.map((c) => `<div class="comment ${c.root_id ? "reply" : ""}"><strong>${c.root_id ? "↳ " : ""}${esc(c.data.author_name || "用户")}</strong><p>${esc(c.data.text)}</p></div>`).join("") : empty("暂无已采集评论", "任务未采集评论，或平台没有返回评论")}<div class="secondary-caption">最多展示 500 条已保存评论。</div></section>`,
    );
    $("#analyze-post").onclick = (e) => action(e.currentTarget, () => beginAnalysis({kind: "post", platform, content_id: id}));
    bindMediaErrors();
  } catch (e) {
    showModal("内容预览", `<p>${esc(e.message)}</p>`);
  }
}
async function openRun(id) {
  state.modalRun = id;
  const run = state.dashboard.runs.find((r) => r.id === id);
  showModal(
    "采集任务详情",
    `<div class="stack">${logo(run?.platform || "xhs")}<h3 id="modal-run-keywords">${esc(sourceSummary(run?.config) || id.slice(0, 12))}</h3><span id="modal-run-status">${statusBadge(run?.interrupted ? "interrupted" : run?.status || "pending")}</span></div><p class="secondary-caption">${esc(id)} · ${run?.mode === "offline" ? "历史测试数据" : "真实采集"}</p>${run ? `<div class="run-context"><span>触发任务<strong>${esc(run.schedule_name || (run.mode === "offline" ? "历史测试" : "手动采集"))}</strong></span><span>执行账号<strong>${esc(run.account_name || (run.mode === "offline" ? "不使用账号" : "历史任务 · 未记录"))}</strong></span><span>请求次数 / 任务上限<strong id="modal-run-budget">${runBudgetText(run)}</strong><small>与风控窗口额度分别计算</small></span></div>` : ""}<div class="check-actions"><button class="button small primary" id="analyze-run">分析本次采集结果</button><button class="button small" id="run-results">查看采集结果 ${icon("arrow")}</button><a class="button small" href="/api/runs/${id}/export" download>导出数据文件 ${icon("download")}</a></div><details class="run-configuration" id="run-configuration"><summary>采集配置<span>采集方式、目标与运行参数</span></summary><div id="run-config-content"><p class="hint">正在读取任务配置</p></div></details><div id="run-action-area"></div><div id="run-progress"><div class="loading"><span class="spinner"></span>加载任务进度</div></div>`,
  );
  $("#analyze-run").onclick = (e) => action(e.currentTarget, () => beginAnalysis({kind: "run", run_id: id}));
  $("#run-results").onclick = () => {
    state.runFilter = id;
    state.resultsPage = 1;
    go("library");
  };
  await refreshRun(id);
}
function runConfigurationHTML(run, context) {
  const c = run.config || {}, env = context?.environment || {};
  const offline = run.mode === "offline", posts = c.source_type === "posts";
  const transport = offline ? "历史测试数据" : run.platform === "douyin"
    ? "HTTP 接口 · curl_cffi（浏览器指纹）"
    : ({ browser: "浏览器网络响应", httpx: "HTTP 接口 · httpx", curl_cffi: "HTTP 接口 · curl_cffi（浏览器指纹）" }[c.adapter || "browser"] || "未记录");
  const value = (v, unit = "") => v == null ? "未记录" : `${v}${unit}`;
  const enabled = (v) => v == null ? "未记录" : v ? "开启" : "关闭";
  const rows = [
    ["采集来源", posts ? "指定帖子" : "关键词搜索"], ["采集方式", transport],
    ["浏览器运行设置", offline ? "不使用浏览器" : c.headless == null ? "跟随账号默认" : c.headless ? "隐藏浏览器窗口" : "显示浏览器窗口"],
    ...(!offline && typeof env.headless === "boolean" ? [["本次浏览器模式", env.headless ? "隐藏浏览器窗口" : "显示浏览器窗口"]] : []),
    ...(!offline && env.browser_provider ? [["浏览器提供方", ({ adspower: "AdsPower", kameleo: "Kameleo", playwright: "Playwright", chromium: "Chromium" })[env.browser_provider] || env.browser_provider]] : []),
    ...(!posts ? [["搜索排序", c.sort === "latest" ? "最新发布" : c.sort === "general" ? "综合排序" : "未记录"], ["每个关键词内容上限", value(c.content_limit, " 条")]] : []),
    ["每帖一级评论上限", c.unlimited_comments ? "不限条数（仍受请求预算约束）" : value(c.comment_limit, " 条")],
    ["展开回复的父评论上限", value(c.reply_parents, " 个")], ["每条父评论回复上限", value(c.reply_limit, " 条")],
    ["最小请求间隔", value(c.min_interval, " 秒")], ["请求超时", value(c.request_timeout, " 秒")],
    ["网络重试次数", value(c.network_retries, " 次")], ["任务请求上限", value(c.max_requests, " 次")],
    ["运行时长上限", value(c.max_seconds, " 秒")], ["最大翻页数", value(c.max_pages, " 页")],
    ["连续无进展上限", value(c.max_no_progress, " 次")], ["下载图片和视频", enabled(c.download_media)],
  ];
  const targets = posts ? c.post_targets : c.keywords;
  return `<p class="hint">本次任务保存的配置；追加预算后，上限会同步更新。</p><dl class="run-config-grid">${rows.map(([label, text]) => `<div><dt>${label}</dt><dd>${esc(text)}</dd></div>`).join("")}</dl><div class="run-config-targets"><h4>${posts ? "指定帖子" : "关键词"} <span class="badge">${targets?.length || 0}</span></h4>${targets?.length ? `<ol>${targets.map((target) => `<li>${esc(target)}</li>`).join("")}</ol>` : '<p class="hint">未记录</p>'}</div>`;
}
function runBudgetText(run) {
  const used = Math.max(0, Number(run?.requests) || 0);
  const limit = Math.max(0, Number(run?.config?.max_requests) || 0);
  return `${number(used)} / ${number(limit)} · 剩余 ${number(Math.max(0, limit - used))}`;
}
async function refreshRun(id) {
  try {
    const data = await api("/api/runs/" + id);
    if (!$("#modal").open || state.modalRun !== id || !$("#run-progress"))
      return;
    const listed = state.dashboard.runs.find((r) => r.id === id);
    const run = data.run
      ? { ...data.run, interrupted: listed?.interrupted }
      : listed;
    $("#modal-run-status").innerHTML = statusBadge(
      run?.interrupted ? "interrupted" : run?.status || "pending",
    );
    if(run && $('#modal-run-keywords'))$('#modal-run-keywords').textContent=sourceSummary(run.config);
    if (run && $("#modal-run-budget"))
      $("#modal-run-budget").textContent = runBudgetText(run);
    if (run && $("#run-config-content")) {
      const configHTML = runConfigurationHTML(run, data.context);
      if ($("#run-config-content").innerHTML !== configHTML) $("#run-config-content").innerHTML = configHTML;
    }
    const area = $("#run-action-area");
    const desired =
      run?.mode !== "online" || run?.status === "completed"
        ? "completed"
        : run?.status === "running" && !run?.interrupted
          ? data.browser_viewer ? "running-viewer" : "running"
          : "resumable";
    if (area.dataset.kind !== desired) {
      area.dataset.kind = desired;
      area.innerHTML =
        desired.startsWith("running")
          ? `<div class="check-actions">${data.browser_viewer ? '<button class="button small primary" id="watch-run">观看采集画面</button>' : ''}<button class="button small danger" id="stop-run">停止采集并保存进度</button></div>`
          : desired === "resumable"
            ? `<form class="budget-fields" id="resume-form"><label><span class="label">追加单次任务请求预算</span><input class="field" type="number" name="requests" min="0" max="100000" value="100" required><small class="field-help">追加到上方任务上限，不修改风控窗口额度。</small></label><label><span class="label">追加时长（秒）</span><input class="field" type="number" name="seconds" min="0" max="86400" value="900" required></label><button class="button primary" type="submit">恢复采集</button></form>`
            : "";
      if ($("#stop-run"))
        $("#stop-run").onclick = (e) =>
          action(e.currentTarget, async () => {
            await api(`/api/runs/${id}/cancel`, {});
            toast("已请求停止，正在保存进度");
          });
      if ($("#watch-run"))
        $("#watch-run").onclick = () =>
          openAccountBrowser(data.browser_viewer.account_id);
      if ($("#resume-form"))
        $("#resume-form").onsubmit = (e) => {
          e.preventDefault();
          const form = e.currentTarget;
          action($("[type=submit]", form), async () => {
            const f = new FormData(form);
            await api(`/api/runs/${id}/resume`, {
              extra_requests: Number(f.get("requests")),
              extra_seconds: Number(f.get("seconds")),
            });
            toast("任务已恢复");
            await refresh();
          });
        };
    }
    $("#run-progress").innerHTML =
      `${postSourcesHTML(data)}<div class="table-wrap"><table><thead><tr><th>采集阶段</th><th>已保存 / 目标</th><th>状态</th><th>停止原因</th></tr></thead><tbody>${data.tasks.map((t) => `<tr><td>${esc(operationLabel(t.operation))}${t.keyword ? " · " + esc(t.keyword) : ""}${t.content_id ? `<small class="cell-note">${esc(t.content_id)}${t.root_id ? " / " + esc(t.root_id) : ""}</small>` : ""}</td><td>${t.count} / ${t.target}</td><td>${statusBadge(t.status)}</td><td class="muted">${esc(reasonLabel(t.stop_reason))}</td></tr>`).join("")}</tbody></table></div><div class="event-list"><h3>运行日志</h3>${data.events
        .slice(-15)
        .reverse()
        .map(
          (e) =>
            `<div class="event"><time>${when(e.at)}</time><span>${esc(eventLabel(e))}</span></div>`,
        )
        .join("")}</div>`;
  } catch (e) {
    if ($("#run-progress"))
      $("#run-progress").innerHTML = `<p>${esc(e.message)}</p>`;
  }
}
function postSourcesHTML(data) {
  if (!data.post_sources?.length) return "";
  const config = data.run?.config || {};
  return `<p class="hint">每帖一级评论上限 ${config.comment_limit}；最多展开 ${config.reply_parents} 个评论楼层，每楼回复上限 ${config.reply_limit}。达到上限不代表平台全量。</p><div class="table-wrap"><table><thead><tr><th>指定目标</th><th>解析 / 详情状态</th><th>说明</th></tr></thead><tbody>${data.post_sources.map((source) => {
    const task = data.tasks.find((t) => t.id === source.task_id);
    return `<tr><td>${source.position + 1}. ${esc(source.content_id || source.display.slice(0, 100))}</td><td>${source.status === "invalid" ? "输入无效" : statusBadge(task?.status || source.status)}</td><td>${esc(source.error ? messageLabel(source.error) : reasonLabel(task?.stop_reason))}</td></tr>`;
  }).join("")}</tbody></table></div>`;
}
function uiLabel(group, value, fallback = "未识别") {
  return state.settings?.ui_labels?.[group]?.[value] || fallback;
}
function operationLabel(value, fallback = "全部动作") {
  return value ? uiLabel("operations", value, "其他动作") : fallback;
}
function reasonLabel(value) {
  if (!value) return "—";
  return uiLabel("reasons", value, /[\u3400-\u9fff]/.test(value) ? value : "未识别原因");
}
function resultLabel(value) {
  return uiLabel("states", value, reasonLabel(value));
}
function accountName(id) {
  return state.settings?.account_pool?.find((a) => a.id === id)?.name || "历史账号（已移除）";
}
function quotaSubjectLabel(quota) {
  if (["platform", "operation"].includes(quota.dimension)) return platformName(quota.subject);
  if (quota.dimension === "account") return accountName(quota.subject);
  return quota.subject;
}
function bindQuotaForm(form, quotas) {
  const sync = () => {
    const dimension = form.elements.dimension.value;
    const subject = $("[data-quota-subject]", form);
    let choices;
    if (["platform", "operation"].includes(dimension)) {
      choices = PLATFORM_OPTIONS;
    } else if (dimension === "account") {
      choices = (state.settings.account_pool || []).map((account) => [
        account.id, `${platformName(account.platform)} · ${account.name}`,
      ]);
    }
    if (choices) {
      subject.innerHTML = `<span class="label">${dimension === "account" ? "执行账号" : "采集平台"}</span><select class="field" name="subject" required>${options(choices.length ? choices : [["", "请先添加账号"]], choices[0]?.[0])}</select>`;
    } else {
      const groups = [...new Set([
        ...quotas.filter((q) => q.dimension === "ip_group").map((q) => q.subject),
        ...(state.risk.network_observations || []).map((row) => row.ip_group),
      ].filter(Boolean))];
      subject.innerHTML = `<span class="label">出口地址组</span><input class="field" name="subject" required list="quota-address-groups" placeholder="选择已有出口或填写地址组"><datalist id="quota-address-groups">${groups.map((group) => `<option value="${esc(group)}"></option>`).join("")}</datalist>`;
    }
    const operations = Object.entries(state.settings.ui_labels.operations);
    const previous = form.elements.operation.value;
    form.elements.operation.innerHTML = options(
      dimension === "operation" ? operations : [["", "全部动作"], ...operations],
      previous || (dimension === "operation" ? operations[0][0] : ""),
    );
    form.elements.operation.required = dimension === "operation";
  };
  form.elements.dimension.onchange = sync;
  sync();
}
function messageLabel(value) {
  if (!value) return "";
  const prefix = /^(采集链路检测：|账号当前不可运行：)([a-z_]+)$/;
  const match = value.match(prefix);
  if (match) return match[1] + reasonLabel(match[2]);
  if (state.settings?.ui_labels?.errors?.[value]) return uiLabel("errors", value);
  if (/^[a-z_]+$/.test(value)) return resultLabel(value);
  if (!/[\u3400-\u9fff]/.test(value)) return "操作未完成，请检查配置与连接";
  return value.replaceAll("Cookie", "登录凭据").replaceAll("Profile", "浏览器会话")
    .replaceAll("canary", "试跑").replaceAll("Canary", "试跑")
    .replaceAll("localStorage.xmst", "本地签名材料");
}
function eventLabel(event) {
  let label = uiLabel("events", event.kind, "其他运行事件");
  if (event.data?.operation) label += " · " + operationLabel(event.data.operation);
  if (event.data?.new_items !== undefined) label += " · 新增 " + event.data.new_items + " 条";
  const reason = event.data?.reason || event.data?.error?.kind || event.data?.kind;
  if (reason) label += " · " + reasonLabel(reason);
  return label;
}
function input(
  name,
  label,
  value,
  placeholder = "",
  type = "text",
  description = "",
) {
  return `<label><span class="label">${label}</span><input class="field" name="${name}" type="${type}" value="${esc(value || "")}" placeholder="${esc(placeholder)}" autocomplete="off">${description ? `<small class="field-help">${esc(description)}</small>` : ""}</label>`;
}
function proxyFields(value) {
  try {
    const u = new URL(value);
    return {
      proxy_scheme: u.protocol.replace(":", ""),
      proxy_host: u.hostname.replace(/^\[|\]$/g, ""),
      proxy_port:
        u.port ||
        { http: "80", https: "443", socks5: "1080", socks5h: "1080" }[
          u.protocol.replace(":", "")
        ],
      proxy_username: decodeURIComponent(u.username),
      proxy_password: decodeURIComponent(u.password),
    };
  } catch {
    return {
      proxy_scheme: "socks5",
      proxy_host: "",
      proxy_port: "",
      proxy_username: "",
      proxy_password: "",
    };
  }
}
function proxyValue(values) {
  const host = values.proxy_host.includes(":")
    ? `[${values.proxy_host}]`
    : values.proxy_host;
  const auth = values.proxy_username
    ? `${encodeURIComponent(values.proxy_username)}:${encodeURIComponent(values.proxy_password)}@`
    : "";
  return `${values.proxy_scheme}://${auth}${host}:${values.proxy_port}`;
}
function latestCheck(kind, p = state.settingsPlatform, accountId = null) {
  return [...state.dashboard.jobs]
    .reverse()
    .find(
      (j) =>
        j.kind === kind &&
        (kind === "database" || j.platform === p) &&
        (kind === "database" || (j.account_id || null) === accountId),
    );
}
function inlineResult(kind, p = state.settingsPlatform, accountId = null) {
  const job = latestCheck(kind, p, accountId);
  if (!job) return '<span class="inline-status neutral">未检测</span>';
  if (job.current === false)
    return '<span class="inline-status neutral">配置已变更，请重新检测</span>';
  const r = job.result || {};
  const pending = ["queued", "running"].includes(job.status);
  const failed =
    job.status === "failed" ||
    (r.cookie_state && !["configured", "managed"].includes(r.cookie_state)) ||
    ["restricted", "unavailable"].includes(r.state) ||
    r.login_state === "expired";
  const warn = r.login_state === "unverified" || r.state === "direct";
  const style =
    pending
      ? "pending"
      : failed
        ? "bad"
        : warn
          ? "warn"
          : "good";
  const label =
    pending
      ? job.status === "queued"
        ? "等待检测…"
        : "正在检测…"
      : job.message ||
        r.login_message ||
        r.cookie_message ||
        r.message ||
        "检测通过";
  let html = `<span class="inline-status ${style}"><b>${pending ? "◌" : failed ? "!" : warn ? "–" : "✓"}</b>${esc(messageLabel(label))}${job.finished_at ? `<small>${stamp(job.finished_at)}</small>` : ""}</span>`;
  if (r.profile_message)
    html += `<span class="check-detail">${esc(messageLabel(r.profile_message))} · ${esc(messageLabel(r.proxy_message))}</span>`;
  if (r.account_message)
    html += `<span class="check-detail">${esc(messageLabel(r.account_message))}</span>`;
  if (kind === "proxy" && !pending) {
    if (r.ip) {
      const l = r.location || {};
      html += `<dl class="proxy-location"><div><dt>出口地址</dt><dd>${esc(r.ip)}</dd></div><div><dt>位置</dt><dd>${esc([l.country_code, l.region, l.city].filter(Boolean).join(" / ") || "未知")}</dd></div><div><dt>经纬度</dt><dd>${l.longitude != null && l.latitude != null ? esc(`${l.longitude}, ${l.latitude}`) : "未知"}</dd></div><div><dt>邮编</dt><dd>${esc(l.postal || "未知")}</dd></div><div><dt>运营商</dt><dd>${esc(r.isp || "未知")}</dd></div></dl><span class="check-detail">地址归属地近似值 · ${esc(r.geo_source)}${r.latency_ms != null ? " · " + r.latency_ms + " 毫秒" : ""}</span>`;
    } else if (r.geo_message)
      html += `<span class="check-detail">${esc(r.geo_message)}</span>`;
  } else if (r.latency_ms != null)
    html += `<span class="check-detail">耗时 ${r.latency_ms} 毫秒</span>`;
  return html;
}
function checkControl(kind, label) {
  return `<div class="check-block"><button class="button small" type="button" data-check="${kind}">${icon(kind === "proxy" || kind === "online" ? "activity" : "shield")}${label}</button><div data-check-result="${kind}" class="check-feedback">${inlineResult(kind)}</div></div>`;
}

function replaceJob(job) {
  state.dashboard.jobs = state.dashboard.jobs.filter((item) => item.id !== job.id);
  state.dashboard.jobs.push(job);
}

async function waitForJob(jobId, onUpdate) {
  while (true) {
    await new Promise((resolve) => setTimeout(resolve, 700));
    const response = await api("/api/jobs");
    state.dashboard.jobs = response.jobs;
    const job = response.jobs.find((item) => item.id === jobId);
    if (!job) throw new Error("操作记录不存在，请重试");
    if (onUpdate) onUpdate(job);
    if (!["queued", "running"].includes(job.status)) return job;
  }
}

function refreshInlineChecks() {
  if (state.page !== "settings") return;
  $$("[data-check-result]").forEach((el) => {
    const form = el.closest("form");
    if (form?.dataset.dirty === "true") return;
    el.innerHTML = inlineResult(el.dataset.checkResult);
  });
}

const accountStateLabel = (value) =>
  ({
    ready: "可运行",
    cooling: "冷却中",
    probe_due: "待恢复检测",
    recovering: "恢复观察中",
    canary_running: "恢复试跑中",
    login_required: "需要登录",
    disabled: "已停用",
  })[value] || resultLabel(value);

async function reloadControlPlane() {
  const [boot, risk] = await Promise.all([
    api("/api/bootstrap"),
    api("/api/risk?days=7"),
  ]);
  state.settings = boot.settings;
  state.dashboard.jobs = boot.settings.checks || state.dashboard.jobs;
  state.risk = risk;
}

const cronPresets = [
  ["0 */6 * * *", "每 6 小时"],
  ["0 9 * * *", "每天 09:00"],
  ["0 9 * * 1-5", "工作日 09:00"],
  ["0 9 * * 1", "每周一 09:00"],
  ["0 9 1 * *", "每月 1 日 09:00"],
];
function bindCronPreset(form) {
  const preset = $("[name=cron_preset]", form);
  const expression = $("[name=cron_expression]", form);
  if (!preset || !expression) return;
  const syncPreset = () => {
    preset.value = cronPresets.some(([value]) => value === expression.value.trim())
      ? expression.value.trim()
      : "custom";
  };
  preset.onchange = () => {
    if (preset.value !== "custom") expression.value = preset.value;
    expression.focus();
  };
  expression.oninput = syncPreset;
  syncPreset();
}
function editableCron(schedule) {
  if (schedule.kind === "cron") return schedule.cron_expression || "";
  if (schedule.kind === "daily") {
    const [hour, minute] = (schedule.daily_time || "09:00").split(":");
    return `${Number(minute)} ${Number(hour)} * * *`;
  }
  const minutes = Number(schedule.interval_minutes);
  if (minutes < 60 && 60 % minutes === 0) return `*/${minutes} * * * *`;
  if (minutes % 60 === 0 && minutes <= 1440 && 24 % (minutes / 60) === 0)
    return `0 */${minutes / 60} * * *`;
  return "";
}
function scheduleEditorHTML(schedule) {
  const config = schedule.config;
  const expression = editableCron(schedule);
  const browserMode =
    config.headless === true ? "headless" : config.headless === false ? "headed" : "inherit";
  return `<form id="schedule-edit-form"><div class="panel-body"><div class="form-grid two"><label><span class="label">作业名称</span><input class="field" name="name" value="${esc(schedule.name)}" maxlength="120" required></label><label><span class="label">平台</span><select class="field" name="platform">${options(PLATFORM_OPTIONS, config.platform)}</select></label>${config.source_type === "posts" ? `<label class="full-span"><span class="label">指定帖子 · 当前 ${config.post_targets.length} 个目标</span><textarea class="field" name="post_targets" rows="3" maxlength="409600" placeholder="留空保留当前目标；修改时重新粘贴完整链接，每行一个"></textarea><small class="field-help">访问参数保存在任务配置中；修改执行周期不会丢失原始链接。</small></label>` : `<label class="full-span"><span class="label">关键词</span><textarea class="field" name="keywords" rows="2" required maxlength="2000">${esc(config.keywords.join("，"))}</textarea></label>`}<label><span class="label">常用周期</span><select class="field" name="cron_preset">${cronPresets.map(([value, label]) => `<option value="${esc(value)}">${esc(label)}</option>`).join("")}<option value="custom">自定义</option></select></label><label><span class="label">定时表达式</span><input class="field cron-field" name="cron_expression" value="${esc(expression)}" placeholder="分 时 日 月 周" spellcheck="false" required><small class="field-help">例如：*/30 9-18 * * 1-5（工作日 9–18 点每 30 分钟）</small></label><label><span class="label">时区</span><input class="field" name="timezone" value="${esc(schedule.timezone || "Asia/Shanghai")}" required></label></div>${expression ? "" : '<div class="notice compact">这是旧版固定间隔，无法无损换算为标准定时表达式。请设置新的 定时表达式后保存。</div>'}<details class="advanced"><summary>采集范围与运行参数</summary><label class="checkbox-label"><input name="download_media" type="checkbox" ${config.download_media ? "checked" : ""}>下载图片和视频到本地存储<small class="field-help">默认关闭；开启后仅在取得帖子详情时下载到持久存储。</small></label><div class="form-grid">${[
    ["content_limit", "每个关键词 · 内容数", config.content_limit, 1, 1000],
    ["comment_limit", "每条内容 · 一级评论", config.comment_limit, 0, 10000],
    ["reply_parents", "采集回复的父评论数", config.reply_parents, 0, 100],
    ["reply_limit", "每条父评论的回复数", config.reply_limit, 0, 2000],
    ["min_interval", "请求间隔（秒）", config.min_interval, 0, 300],
    ["max_requests", "请求上限", config.max_requests, 1, 100000],
    ["max_pages", "最大页数", config.max_pages, 1, 1000],
    ["max_seconds", "运行时长（秒）", config.max_seconds, 1, 86400],
    ["request_timeout", "请求超时（秒）", config.request_timeout, 1, 120],
  ].map(([key, label, value, min, max]) => `<label><span class="label">${label}</span><input class="field" type="number" name="${key}" value="${value}" min="${min}" max="${max}" required></label>`).join("")}<label><span class="label">排序方式</span><select class="field" name="sort">${options([["latest", "最新发布"], ["general", "综合排序"]], config.sort)}</select></label><label data-edit-adapter><span class="label">采集方式</span><select class="field" name="adapter">${options([["browser", "浏览器网络响应"], ["httpx", "直接接口请求（试用）"], ["curl_cffi", "模拟浏览器指纹请求（试用）"]], config.adapter || "browser")}</select></label><label><span class="label">浏览器运行方式</span><select class="field" name="browser_mode">${options([["inherit", "跟随账号默认"], ["headed", "显示浏览器窗口"], ["headless", "隐藏浏览器窗口"]], browserMode)}</select></label></div></details></div><div class="form-actions"><span class="hint">保存后将按新表达式重新计算下次执行时间。</span><div class="stack"><button class="button" type="button" data-cancel-edit>取消</button><button class="button primary" type="submit">保存修改</button></div></div></form>`;
}
function openScheduleEditor(scheduleId) {
  const schedule = (state.settings.schedules || []).find((row) => row.id === scheduleId);
  if (!schedule) return;
  showModal("编辑定时作业", scheduleEditorHTML(schedule));
  $("#modal").classList.add("collection-modal");
  const form = $("#schedule-edit-form");
  bindCronPreset(form);
  const renderPlatformFields = () => {
    $("[data-edit-adapter]", form).hidden = !isXhsFamily(form.elements.platform.value);
    for (const name of ["content_limit", "sort"])
      form.elements[name].closest("label").hidden = schedule.config.source_type === "posts";
  };
  form.elements.platform.onchange = renderPlatformFields;
  renderPlatformFields();
  $("[data-cancel-edit]", form).onclick = () => $("#modal").close();
  form.onsubmit = (event) => {
    event.preventDefault();
    action($("[type=submit]", form), async () => {
      const values = Object.fromEntries(new FormData(form));
      const config = { ...schedule.config };
      config.platform = values.platform;
      if (config.source_type === "posts") {
        if (values.post_targets.trim()) config.post_targets = values.post_targets.split(/\n+/).map((v) => v.trim()).filter(Boolean);
        else delete config.post_targets;
      } else config.keywords = values.keywords.split(/[,，\n]+/).map((value) => value.trim()).filter(Boolean);
      config.sort = values.sort;
      config.download_media = values.download_media === "on";
      for (const key of ["content_limit", "comment_limit", "reply_parents", "reply_limit", "min_interval", "max_requests", "max_pages", "max_seconds", "request_timeout"])
        config[key] = Number(values[key]);
      if (isXhsFamily(values.platform)) config.adapter = values.adapter;
      else delete config.adapter;
      if (values.browser_mode === "inherit") delete config.headless;
      else config.headless = values.browser_mode === "headless";
      await api(`/api/schedules/${schedule.id}`, {
        name: values.name,
        kind: "cron",
        cron_expression: values.cron_expression,
        timezone: values.timezone,
        config,
      });
      await reloadControlPlane();
      $("#modal").close();
      renderWorkbench();
      toast("定时作业已更新");
    });
  };
}

function renderAutomation() {
  ConsoleLists.renderRisk();
}

function bindScheduleActions() {
  bindNavigation();
  $$('[data-schedule-toggle]').forEach(
    (button) =>
      (button.onclick = () =>
        action(button, async () => {
          await api(`/api/schedules/${button.dataset.scheduleToggle}/enabled`, {
            enabled: button.dataset.enabled === "true",
          });
          await reloadControlPlane();
          renderWorkbench();
        })),
  );
  $$('[data-schedule-edit]').forEach((button) => {
    button.onclick = () => openScheduleEditor(button.dataset.scheduleEdit);
  });
  $$('[data-schedule-delete]').forEach((button) => {
    button.onclick = async () => {
      if (!await confirmAction(`确定删除定时作业“${button.dataset.name}”吗？历史采集记录会保留。`)) return;
      action(button, async () => {
        await api(`/api/schedules/${button.dataset.scheduleDelete}`, undefined, "DELETE");
        await reloadControlPlane();
        renderWorkbench();
        toast("定时作业已删除，历史采集记录已保留");
      });
    };
  });
}

async function ensureProxyDiagnostic(p) {
  if (
    !state.settings.accounts[p].proxy_url ||
    state.dashboard.jobs.some((j) => ["queued", "running"].includes(j.status))
  )
    return;
  const existing = latestCheck("proxy", p);
  if (existing && existing.current !== false) return;
  try {
    const job = await api("/api/check", { kind: "proxy", platform: p });
    state.dashboard.jobs.push(job);
    refreshInlineChecks();
  } catch {
    /* Busy environments are left for the explicit check button. */
  }
}
function renderSettings() {
  const p = state.settingsPlatform,
    a = state.settings.accounts[p],
    proxy = proxyFields(a.proxy_url),
    identityField =
      isXhsFamily(p)
        ? input(
            "expected_user_id",
            "预期用户标识",
            a.expected_user_id,
            "可选，用于核对账号身份",
            "text",
            "小红书与小红书国际版；在线检测时核对实际登录用户，防止用错账号。",
          )
        : "",
    platformFields =
      p === "douyin"
        ? input(
            "impersonate",
            "网络请求指纹预设",
            a.impersonate,
            "",
            "text",
            "仅抖音；控制网络请求的连接指纹，应与浏览器身份标识匹配。",
          ) +
          input(
            "douyin_webid",
            "网页设备标识",
            a.douyin_webid,
            "",
            "text",
            "仅抖音；可选的请求身份参数，没有稳定值时留空。",
          )
        : "";
  $("#main").innerHTML =
    heading(
      "ACCOUNTS & CONNECTIONS",
      "账号与连接",
      "配置与检测放在一起，每条连接的状态一目了然。",
    ) +
    `
  <div class="settings-single"><div class="settings-tabs">${PLATFORM_ORDER.map((x) => `<button class="button ${x === p ? "selected" : ""}" data-settings-platform="${x}">${logo(x)}${platformName(x)}</button>`).join("")}</div>
  <section class="panel"><div class="panel-title"><h2>账号与会话</h2><span>本地配置已自动读取</span></div>
  <form id="account-form"><div class="panel-body"><section class="settings-section"><div class="form-grid two">${input("account_ref", "账号标识", a.account_ref, "成功识别身份后默认使用登录昵称", "text", "本地账号别名，用于区分任务和锁定账号资源；不是平台用户标识。")}${input("profile_dir", "浏览器数据目录", a.profile_dir, "", "text", "保存浏览器本地状态；不同账号应使用不同目录。")}${input("cookie_file", "登录凭据文件路径", a.cookie_file, "", "text", "采集读取的登录凭据文件；浏览器登录成功后会更新它。")}${identityField}</div><p class="hint">使用默认账号标识时，在线检测成功并取得昵称后会自动更新；手动填写的标识不会被覆盖。</p>
  <details class="advanced"><summary>浏览器与会话高级配置</summary><div class="form-grid two">${browserProviderFields({...a, platform: p})}${input("user_agent", "浏览器身份标识", a.user_agent, "留空使用平台默认值", "text", "浏览器和请求对外报告的客户端身份，不建议随任务频繁改变。")}${input("browser_channel", "浏览器类型", a.browser_channel, "留空使用内置浏览器", "text", "指定采集使用的浏览器类型；留空使用系统内置浏览器。")}${input("locale", "浏览器语言", a.locale, "留空沿用快照/平台默认值")}${input("timezone_id", "浏览器时区", a.timezone_id, "留空沿用快照/平台默认值")}${consistencyPolicyField(a.consistency_policy)}<label class="checkbox-label"><input name="auto_session_recovery" type="checkbox" ${a.auto_session_recovery !== false ? "checked" : ""}>登录失效时自动尝试一次浏览器会话恢复</label>${platformFields}${input("session_version", "会话版本", a.session_version, "", "text", "人工维护的会话材料版本标签，用于审计和绑定摘要。")}${input("binding_version", "绑定版本", a.binding_version, "", "text", "整套浏览器身份、代理和浏览器指纹配置的版本标签。")}</div><label class="checkbox-label"><input name="headless" type="checkbox" ${a.headless ? "checked" : ""}>默认使用隐藏浏览器窗口运行采集浏览器</label><p class="hint">这是账号默认值；新建采集任务时可以临时覆盖。</p></details>${checkControl("account", "检测账号配置")}</section>
  <section class="settings-section"><div class="section-heading"><h3>登录凭据配置</h3><span class="badge">${a.cookie_count} 个有效期内字段</span></div><label><span class="label">登录凭据文本 / 浏览器导出数据</span><textarea class="field cookie-editor" name="cookie_text" rows="5" autocomplete="off" spellcheck="false" placeholder="粘贴 登录凭据文本或浏览器导出数据">${esc(a.cookie_text)}</textarea></label><p class="hint">已回填当前 登录凭据文件，可直接查看与编辑。修改后保存再检测。</p>${checkControl("cookie", "检查登录凭据")}${checkControl("online", "在线有效性 / 登录检测")}${checkControl("recover", "尝试恢复登录态")}${checkControl("login", "强制重新登录")}</section>
  <section class="settings-section"><div class="section-heading"><h3>代理配置</h3><span class="muted">连接结果反映实际代理出口</span></div>
  <div class="proxy-config-grid"><div class="proxy-inputs"><div class="proxy-endpoint"><label><span class="label">协议</span><select class="field" name="proxy_scheme">${options(
    [
      ["socks5", "通用代理"],
      ["socks5h", "通用代理（远端解析域名）"],
      ["http", "普通网页代理"],
      ["https", "加密网页代理"],
    ],
    proxy.proxy_scheme,
  )}</select></label>${input("proxy_host", "主机", proxy.proxy_host, "地址或域名")}${input("proxy_port", "端口", proxy.proxy_port, "端口", "number")}</div><div class="form-grid two">${input("proxy_username", "代理账号", proxy.proxy_username, "无认证时留空")}${input("proxy_password", "代理密码", proxy.proxy_password, "无认证时留空")}</div><details class="advanced"><summary>代理配置来源</summary><div class="form-grid two">${input("proxy_file", "代理文件", a.proxy_file, "未配置")}${input("proxy_env", "代理环境变量", a.proxy_env, "与文件二选一")}</div></details><label class="checkbox-label"><input name="clear_proxy" type="checkbox">关闭此账号代理，改为直连</label></div><div class="proxy-check">${checkControl("proxy", "检查代理")}</div></div>
  </section></div><div class="form-actions"><span class="hint">修改会使已有检测标记失效，保存后可重新检测。</span><button class="button primary" type="submit">保存账号配置</button></div></form></section>
  <section class="panel database-panel"><div class="panel-title"><h2>${icon("db")}数据库配置</h2><span>全局共享</span></div><form id="database-form"><div class="panel-body"><p class="hint">当前：${esc(state.settings.database)}</p><label><span class="label">数据库连接地址</span><input class="field" name="database_url" type="password" placeholder="postgresql+psycopg://user:password@host:5432/db" autocomplete="new-password"></label><p class="hint">真实采集使用采集数据库；留空保留当前配置。</p><button class="button" type="submit">保存数据库配置</button>${checkControl("database", "测试数据库连接")}</div></form></section></div>`;
  $$("[data-settings-platform]").forEach(
    (b) =>
      (b.onclick = () => {
        state.settingsPlatform = b.dataset.settingsPlatform;
        renderSettings();
      }),
  );
  bindBrowserProfileFields($("#account-form"), { platform: p });
  $("#account-form").onsubmit = (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    action($("[type=submit]", form), async () => {
      const f = Object.fromEntries(new FormData(form));
      const environment = {};
      for (const k of [
        "account_ref",
        "profile_dir",
        "cookie_file",
        "expected_user_id",
        "user_agent",
        "browser_channel",
        "browser_provider",
        "adspower_profile_id",
        "kameleo_profile_id",
        "locale",
        "timezone_id",
        "consistency_policy",
        "impersonate",
        "douyin_webid",
        "session_version",
        "binding_version",
        "proxy_file",
        "proxy_env",
      ]) {
        if (!Object.prototype.hasOwnProperty.call(f, k)) continue;
        environment[k] =
          f[k] ||
          ([
            "expected_user_id",
            "user_agent",
            "browser_channel",
            "adspower_profile_id",
            "kameleo_profile_id",
            "locale",
            "timezone_id",
            "proxy_file",
            "proxy_env",
          ].includes(k)
            ? null
            : "");
      }
      environment.adspower_profile_id =
        f.browser_provider === "adspower" ? f.adspower_profile_id || null : null;
      environment.kameleo_profile_id =
        f.browser_provider === "kameleo" ? f.kameleo_profile_id || null : null;
      environment.headless = !!f.headless;
      environment.auto_session_recovery = !!f.auto_session_recovery;
      if (
        !f.clear_proxy &&
        Object.keys(proxy).some((k) => f[k] !== String(proxy[k] ?? "")) &&
        (!f.proxy_host || !f.proxy_port)
      )
        throw new Error("请填写代理主机与端口，或勾选直连");
      state.settings = await api("/api/settings", {
        platform: p,
        environment,
        cookie_text:
          f.cookie_text.trim() !== a.cookie_text ? f.cookie_text : undefined,
        proxy_url:
          Object.keys(proxy).some((k) => f[k] !== String(proxy[k] ?? "")) &&
          !f.clear_proxy
            ? proxyValue(f)
            : undefined,
        clear_proxy: !!f.clear_proxy,
      });
      state.dashboard.jobs = state.settings.checks || [];
      toast("账号配置已保存");
      renderSettings();
    });
  };
  $("#database-form").onsubmit = (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    action($("[type=submit]", form), async () => {
      state.settings = await api("/api/settings", {
        database_url: new FormData(form).get("database_url"),
      });
      state.dashboard.jobs = state.settings.checks || [];
      toast("数据库配置已保存");
      renderSettings();
      await refresh();
    });
  };
  $$("[data-check]").forEach(
    (b) =>
      (b.onclick = () =>
        action(b, async () => {
          if (b.dataset.check === "login") {
            openAccountBrowser(`legacy-${p}`);
            return;
          }
          const job = await api("/api/check", {
            kind: b.dataset.check,
            platform: p,
          });
          replaceJob(job);
          refreshInlineChecks();
          toast(
            b.dataset.check === "login"
              ? "正在打开登录窗口，请在浏览器中完成扫码"
              : "正在检测，请留在当前页面等待结果",
          );
          const finished = await waitForJob(job.id, refreshInlineChecks);
          state.seenJobs.add(finished.id);
          await reloadControlPlane();
          refreshInlineChecks();
          toast(
            finished.message ||
              finished.result?.login_message ||
              finished.result?.message ||
              "检测完成",
          );
        })),
  );
  for (const form of [$("#account-form"), $("#database-form")])
    form.addEventListener("input", () => {
      form.dataset.dirty = "true";
      $$("[data-check]", form).forEach((b) => (b.disabled = true));
      $$("[data-check-result]", form).forEach(
        (el) =>
          (el.innerHTML =
            '<span class="inline-status neutral">配置已修改，保存后重新检测</span>'),
      );
    });
  ensureProxyDiagnostic(p);
}

function openAccountBrowser(accountId, viewer, mode = 'inspect') {
  const url = `/account-browser.html?account=${encodeURIComponent(accountId)}&mode=${mode}`;
  if (viewer) { viewer.location = url; viewer.focus(); return viewer; }
  const opened = window.open(url, `account-browser-${accountId}`);
  if (!opened) toast("浏览器窗口被拦截，请允许本站打开弹窗后重试");
  return opened;
}

function loginPoolAccount(button, account) {
  openAccountBrowser(account.id, null, 'login');
}

const accountBrowserEvents = new BroadcastChannel('account-browser');
accountBrowserEvents.onmessage = async event => {
  if (event.data?.type !== 'account-login-saved') return;
  await reloadAccountList();
  if (state.page === 'accounts') renderAccountPool();
  toast('登录已保存，可以开始采集');
};

function openLoginViewer() {
  return window.open('/account-browser.html', 'crawler-account-login');
}

function accountProxyField(account = {}) {
  const proxies = state.settings.proxies || [];
  const managed = ["adspower", "kameleo"].includes(account.browser_provider);
  const managedHybrid = managed && account.platform === "douyin";
  const directLabel = managed ? "不使用项目代理（HTTP 直连）" : "本机直连（不使用代理）";
  const choices = [["", directLabel], ...proxies.map(proxy => [proxy.id, `${proxy.name} · ${proxy.endpoint || "来源不可用"}`])];
  // A missing inventory row must never silently switch an existing account to direct.
  if (account.proxy_id && !proxies.some(proxy => proxy.id === account.proxy_id))
    choices.push([account.proxy_id, "当前代理（请刷新代理列表）"]);
  const managedHelp = managedHybrid
    ? "AdsPower 无代理时保持直连；AdsPower 配置代理时，请选择相同出口的项目代理。运行前会实测两条链路。"
    : "浏览器和媒体使用对应环境网络；HTTP 接口未选代理时使用直连，并与环境出口实测核对。";
  return `<label><span class="label">${managed ? "HTTP 接口代理（可选）" : "使用代理"}</span><select class="field" name="proxy_id">${options(choices, account.proxy_id || "")}</select><small class="field-help">${managed ? managedHelp : proxies.length ? "代理地址与认证信息在「代理列表」中统一管理。" : "还没有代理，可先到左侧「代理列表」添加。"}</small></label>`;
}

function accountDetailHTML(account) {
  const managed = ["adspower", "kameleo"].includes(account.browser_provider);
  const loginSummary = managed ? "登录由浏览器环境管理" : account.cookie_state === "configured" ? "已保存登录" : "尚未保存登录";
  const cookiePath = managed ? "" : input("cookie_file", "登录凭据文件路径", account.cookie_file);
  const credentialSection = managed
    ? `<section class="account-section"><div class="section-heading"><h3>登录与会话</h3><span class="badge good">浏览器环境管理</span></div><p class="hint">采集直接使用 ${account.browser_provider === "adspower" ? "AdsPower" : "Kameleo"} 环境中的登录状态，不读取、比较或覆盖项目 Cookie 文件。</p></section>`
    : `<section class="account-section"><div class="section-heading"><h3>登录与会话凭据</h3><span class="badge">${account.cookie_count || 0} 个有效字段</span></div><label><span class="label">登录凭据文本 / 浏览器导出数据</span><textarea class="field cookie-editor" name="cookie_text" rows="6" autocomplete="off" spellcheck="false" placeholder="可手工粘贴，或在账号列表点击“登录”自动获取">${esc(account.cookie_text)}</textarea></label></section>`;
  return `<form id="account-detail-form" data-account-id="${account.id}">
  <div class="account-detail-summary">${logo(account.platform)}<div><strong>${esc(account.name)}</strong><small>${platformName(account.platform)} · ${loginSummary}</small></div><span class="badge">${account.status === "disabled" ? "已禁用" : "已启用"}</span></div>
  <section class="account-section"><div class="section-heading"><h3>基本信息与连接</h3><span>选择账号使用的网络</span></div><div class="form-grid two">${input("name", "账号名称", account.name, "用于识别账号")}${accountProxyField(account)}</div></section>
  <details class="advanced account-advanced"><summary>高级配置与登录凭据<span>浏览器环境、设备指纹及会话管理</span></summary>
  <section class="account-section" data-account-section="environment"><div class="section-heading"><h3>浏览器与会话环境</h3><span>每个账号独立</span></div><div class="form-grid two">${input("account_ref", "账号资源标识", account.account_ref)}${input("expected_user_id", "预期用户标识", account.expected_user_id, "可选，小红书系列")}${input("profile_dir", "浏览器数据目录", account.profile_dir, "", "text", "不同账号使用独立目录，保存时会检查唯一性。")}${cookiePath}${browserProviderFields(account)}${input("browser_channel", "浏览器类型", account.browser_channel, "留空使用内置浏览器")}${consistencyPolicyField(account.consistency_policy)}</div><div class="account-checks"><label class="checkbox-label"><input name="headless" type="checkbox" ${account.headless ? "checked" : ""}>默认隐藏浏览器窗口执行采集</label><label class="checkbox-label"><input name="auto_session_recovery" type="checkbox" ${account.auto_session_recovery !== false ? "checked" : ""}>登录失效时自动尝试一次浏览器会话恢复</label></div></section>
  <section class="account-section" data-account-section="fingerprint"><div class="section-heading"><h3>设备指纹</h3><span>与常用登录环境保持一致</span></div><div class="form-grid two"><div class="account-field-wide">${input("user_agent", "浏览器身份标识", account.user_agent, "留空使用平台默认值")}</div>${input("locale", "浏览器语言", account.locale, "留空沿用快照 / 平台默认值")}${input("timezone_id", "浏览器时区", account.timezone_id, "留空沿用快照 / 平台默认值")}${input("session_version", "会话版本", account.session_version)}${input("binding_version", "环境绑定版本", account.binding_version)}${isXhsFamily(account.platform) || account.platform === "douyin" ? input("impersonate", "网络请求指纹预设", account.impersonate, "例如 chrome150；与登录浏览器版本一致") + (account.platform === "douyin" ? input("douyin_webid", "网页设备标识", account.douyin_webid, "可选") : "") : ""}</div></section>
  ${credentialSection}
  </details><div class="form-actions"><button class="button" type="button" id="close-account-detail">取消</button><button class="button primary" type="submit">保存账号配置</button></div></form>`;
}

function bindAccountDetail(account) {
  const form = $("#account-detail-form");
  form.elements.name.required = true;
  form.elements.name.maxLength = 80;
  bindBrowserProfileFields(form, {
    platform: account.platform,
    accountId: account.id,
  });
  $("#close-account-detail").onclick = () => $("#modal").close();
  form.onsubmit = (e) => {
    e.preventDefault();
    action($("[type=submit]", form), async () => {
      const values = Object.fromEntries(new FormData(form));
      const environment = {};
      for (const key of ["account_ref", "profile_dir", "cookie_file", "expected_user_id", "user_agent", "browser_channel", "browser_provider", "adspower_profile_id", "kameleo_profile_id", "locale", "timezone_id", "consistency_policy", "session_version", "binding_version", "impersonate", "douyin_webid"])
        if (Object.prototype.hasOwnProperty.call(values, key))
          environment[key] = values[key] || (["expected_user_id", "user_agent", "browser_channel", "adspower_profile_id", "kameleo_profile_id", "locale", "timezone_id"].includes(key) ? null : "");
      environment.adspower_profile_id =
        values.browser_provider === "adspower"
          ? values.adspower_profile_id || null
          : null;
      environment.kameleo_profile_id =
        values.browser_provider === "kameleo"
          ? values.kameleo_profile_id || null
          : null;
      environment.headless = !!values.headless;
      environment.auto_session_recovery = !!values.auto_session_recovery;
      await api(`/api/accounts/${account.id}`, {
        name: values.name,
        environment,
        cookie_text: values.cookie_text != null && values.cookie_text.trim() !== account.cookie_text ? values.cookie_text : undefined,
        proxy_id: values.proxy_id,
      });
      await reloadAccountList();
      $("#modal").close();
      toast("账号配置已保存");
      if (state.page === "proxies") renderProxyPool();
      else if (state.page === "accounts") renderAccountPool();
    });
  };
}

async function openAccountDetail(accountId) {
  showModal("账号详情与配置", '<div id="account-detail-loading" class="loading"><span class="spinner"></span>正在读取账号配置</div>');
  $("#modal").classList.add("account-modal");
  const loading = $("#account-detail-loading");
  try {
    const [account, proxies] = await Promise.all([
      api(`/api/accounts/${encodeURIComponent(accountId)}`), api("/api/proxies"),
    ]);
    state.settings.proxies = proxies.items;
    if (!loading.isConnected || !$("#modal").open) return;
    showModal("账号详情与配置", accountDetailHTML(account));
    $("#modal").classList.add("account-modal");
    bindAccountDetail(account);
  } catch (e) {
    if (loading.isConnected) loading.textContent = e.message;
  }
}

function openNewAccount() {
  showModal(
    "添加账号",
    `<form id="new-account-form" class="new-account-form"><div class="form-grid two"><label><span class="label">平台</span><select class="field" name="platform">${options(PLATFORM_OPTIONS, "xhs")}</select></label>${input("name", "账号名称", "", "例如：国际版账号一")}</div><details class="advanced account-advanced"><summary>高级浏览器配置（可选）</summary><div class="form-grid two">${browserProviderFields({ platform: "xhs", browser_provider: "chromium" })}${input("user_agent", "浏览器身份标识", "", "留空使用平台默认值")}${input("browser_channel", "浏览器类型", "", "留空使用内置浏览器")}${input("locale", "浏览器语言", "", "小红书国际版建议与账号常用环境一致")}${input("timezone_id", "浏览器时区", "", "例如 America/Los_Angeles")}${consistencyPolicyField("warn")}${input("session_version", "会话版本", "1")}${input("binding_version", "绑定版本", "1")}</div><label class="checkbox-label"><input name="headless" type="checkbox" checked>采集任务默认隐藏浏览器窗口</label></details><div class="generated-path-note">浏览器数据目录和登录凭据文件将按账号自动生成，并在服务端检查唯一性，避免覆盖其他账号。</div><section class="settings-section"><div class="section-heading"><h3>登录方式</h3></div><label><span class="label">创建后操作</span><select class="field" name="login_mode"><option value="browser">打开浏览器登录并自动获取登录凭据</option><option value="cookie">现在粘贴登录凭据 / 浏览器导出数据</option><option value="later">稍后登录</option></select></label><label data-new-cookie hidden><span class="label">登录凭据文本 / 浏览器导出数据</span><textarea class="field" name="cookie_text" rows="5"></textarea></label></section><section class="settings-section"><div class="section-heading"><h3>网络连接</h3><span>可稍后在详情中更换</span></div>${accountProxyField()}</section><div class="form-actions"><button class="button" type="button" id="cancel-new-account">取消</button><button class="button primary" type="submit">创建账号</button></div></form>`,
  );
  $("#modal").classList.add("account-modal");
  $("#cancel-new-account").onclick = () => $("#modal").close();
  const form = $("#new-account-form");
  form.elements.name.required = true;
  form.elements.name.maxLength = 80;
  bindBrowserProfileFields(form, { platformSelect: form.elements.platform });
  $("[name=login_mode]", form).onchange = (event) => {
    const cookie = $("[data-new-cookie]", form);
    cookie.hidden = event.target.value !== "cookie";
    $("[name=cookie_text]", form).required = event.target.value === "cookie";
  };
  form.onsubmit = (e) => {
    e.preventDefault();
    const loginViewer =
      new FormData(form).get("login_mode") === "browser"
        ? openLoginViewer()
        : null;
    action($("[type=submit]", form), async () => {
      const values = Object.fromEntries(new FormData(form));
      const created = await api("/api/accounts", {
        platform: values.platform,
        name: values.name,
        cookie_text: values.login_mode === "cookie" ? values.cookie_text : undefined,
        proxy_id: values.proxy_id,
        environment: {
          browser_provider: values.browser_provider || "chromium",
          adspower_profile_id:
            values.browser_provider === "adspower"
              ? values.adspower_profile_id || null
              : null,
          kameleo_profile_id:
            values.browser_provider === "kameleo"
              ? values.kameleo_profile_id || null
              : null,
          user_agent: values.user_agent || null,
          browser_channel: values.browser_channel || null,
          locale: values.locale || null,
          timezone_id: values.timezone_id || null,
          consistency_policy: values.consistency_policy || "warn",
          headless: !!values.headless,
          session_version: values.session_version,
          binding_version: values.binding_version,
        },
      });
      state.selectedAccountId = created.id;
      await reloadAccountList();
      $("#modal").close();
      if (state.page === "accounts") renderAccountPool();
      if (values.login_mode === "browser") {
        openAccountBrowser(created.id, loginViewer, 'login');
        toast("账号已创建，请在浏览器中完成登录");
      } else {
        toast(values.login_mode === "cookie" ? "账号已创建并写入登录凭据" : "账号已创建，可随时登录");
      }
      if (state.page === "accounts") renderAccountPool();
    });
  };
}

async function reloadAccountList() {
  const boot = await api("/api/bootstrap");
  state.settings = boot.settings;
}

function renderProxyPool() {
  const all = state.settings.proxies || [];
  const query = (state.proxyQuery || "").trim().toLowerCase();
  const rows = all.filter(proxy => `${proxy.name} ${proxy.endpoint}`.toLowerCase().includes(query));
  $("#main").innerHTML = heading("连接管理", "代理列表", "统一维护代理地址与认证信息，在账号详情中选择使用。",
    `<button class="button primary" id="add-proxy">${icon("plus")}添加代理</button>`) +
    `<section class="panel table-panel"><div class="panel-title"><h2>代理列表 <span class="badge">${all.length}</span></h2><button class="button small subtle" id="refresh-proxies">刷新列表</button></div>
    <form id="proxy-filters" class="filter-form proxy-filters"><label><span class="label">代理名称 / 地址</span><input class="field" name="q" value="${esc(state.proxyQuery || "")}" placeholder="搜索代理名称、主机或端口"></label><div class="filter-submit"><button class="button primary" type="submit">${icon("search")}筛选</button><button class="button" type="button" id="reset-proxy-filters">重置</button></div></form>
    ${rows.length ? `<div class="table-wrap"><table class="proxy-table"><thead><tr><th>代理</th><th>连接地址</th><th>关联账号</th><th>操作</th></tr></thead><tbody>${rows.map(proxy => `<tr><td><strong>${esc(proxy.name)}</strong><small class="cell-note">${!proxy.configured ? "连接配置待修复" : proxy.authenticated ? "需要身份认证" : "无需身份认证"}</small></td><td><span class="badge">${esc(proxy.scheme.toUpperCase() || "待配置")}</span><div class="proxy-address">${esc(proxy.endpoint || "代理来源不可用，请编辑配置")}</div></td><td><div class="proxy-account-links">${proxy.accounts.length ? proxy.accounts.map(account => `<button class="text-button" data-proxy-account="${esc(account.id)}">${esc(account.name)}</button>`).join("") : '<span class="muted">未分配</span>'}</div><small class="cell-note">${proxy.accounts.length ? `${proxy.accounts.length} 个账号使用中 · 更换后可删除` : "可在账号详情中选择"}</small></td><td><div class="account-row-actions"><button class="button small" data-proxy-edit="${proxy.id}">编辑</button><button class="button small danger" data-proxy-delete="${proxy.id}" ${proxy.accounts.length ? 'disabled title="请先为关联账号更换代理或选择本机直连"' : ""}>删除</button></div></td></tr>`).join("")}</tbody></table></div>` : empty(all.length ? "没有匹配的代理" : "还没有代理", all.length ? "试试其他名称或地址，或重置筛选。" : "添加一个代理，再到账号详情中为账号选择网络连接。")}</section>`;
  $("#add-proxy").onclick = () => openProxyEditor();
  $("#refresh-proxies").onclick = event => action(event.currentTarget, async () => {
    await reloadAccountList();
    if (state.page === "proxies") renderProxyPool();
  });
  $("#proxy-filters").onsubmit = event => {
    event.preventDefault();
    state.proxyQuery = new FormData(event.currentTarget).get("q");
    renderProxyPool();
  };
  $("#reset-proxy-filters").onclick = () => { state.proxyQuery = ""; renderProxyPool(); };
  $$('[data-proxy-edit]').forEach(button => button.onclick = () => action(button, () => openProxyEditor(button.dataset.proxyEdit)));
  $$('[data-proxy-account]').forEach(button => button.onclick = () => openAccountDetail(button.dataset.proxyAccount));
  $$('[data-proxy-delete]').forEach(button => button.onclick = async () => {
    const proxy = all.find(row => row.id === button.dataset.proxyDelete);
    if (!await confirmAction(`确定删除代理“${proxy.name}”吗？`)) return;
    action(button, async () => {
      await api(`/api/proxies/${proxy.id}`, undefined, "DELETE");
      await reloadAccountList();
      if (state.page === "proxies") renderProxyPool();
      toast("代理已删除");
    });
  });
}

async function openProxyEditor(proxyId) {
  const proxy = proxyId ? await api(`/api/proxies/${proxyId}`) : {name: "", proxy_url: ""};
  const fields = proxyFields(proxy.proxy_url);
  const count = (state.settings.proxies || []).find(row => row.id === proxyId)?.accounts.length || 0;
  showModal(proxyId ? "编辑代理" : "添加代理", `<form id="proxy-form">
    <section class="account-section">${input("name", "代理名称", proxy.name, "例如：上海固定出口")}</section>
    <section class="account-section"><div class="section-heading"><h3>连接地址</h3></div><div class="proxy-endpoint"><label><span class="label">协议</span><select class="field" name="proxy_scheme">${options([["socks5", "SOCKS5"], ["socks5h", "SOCKS5H（远端解析）"], ["http", "HTTP"], ["https", "HTTPS"]], fields.proxy_scheme)}</select></label>${input("proxy_host", "主机", fields.proxy_host, "IP 地址或域名")}${input("proxy_port", "端口", fields.proxy_port, "1–65535", "number")}</div></section>
    <section class="account-section"><div class="section-heading"><h3>身份认证</h3><span>无需认证时留空</span></div><div class="form-grid two">${input("proxy_username", "代理账号", fields.proxy_username, "可选")}${input("proxy_password", "代理密码", fields.proxy_password, "可选", "password")}</div></section>
    ${count ? `<p class="proxy-edit-note">此代理被 ${count} 个账号使用，保存连接信息后将同步生效。关联账号运行任务或打开浏览器时，请先结束后再修改。</p>` : ""}
    <div class="form-actions"><button class="button" id="cancel-proxy" type="button">取消</button><button class="button primary" type="submit">保存代理</button></div></form>`);
  $("#modal").classList.add("account-modal");
  const form = $("#proxy-form");
  for (const key of ["name", "proxy_host", "proxy_port"]) form.elements[key].required = true;
  form.elements.name.maxLength = 80;
  form.elements.proxy_port.min = 1;
  form.elements.proxy_port.max = 65535;
  $("#cancel-proxy").onclick = () => $("#modal").close();
  form.onsubmit = event => {
    event.preventDefault();
    action($("[type=submit]", form), async () => {
      const values = Object.fromEntries(new FormData(form));
      values.proxy_host = values.proxy_host.trim().replace(/^\[|\]$/g, "");
      if (!values.name.trim() || !values.proxy_host) throw new Error("请填写代理名称与主机");
      if (values.proxy_password && !values.proxy_username) throw new Error("填写代理密码时，也需要填写代理账号");
      const changed = Object.keys(fields).some(key => String(values[key]) !== String(fields[key]));
      await api(proxyId ? `/api/proxies/${proxyId}` : "/api/proxies", {
        name: values.name,
        proxy_url: !proxyId || changed ? proxyValue(values) : undefined,
      });
      await reloadAccountList();
      $("#modal").close();
      if (state.page === "proxies") renderProxyPool();
      toast("代理已保存");
    });
  };
}

function renderAccountPool() {
  const all = state.settings.account_pool || [];
  const filters = state.accountFilters;
  const rows = all.filter(account =>
    (!filters.platform || account.platform === filters.platform) &&
    (!filters.q || `${account.name} ${account.account_ref}`.toLowerCase().includes(filters.q.toLowerCase())));
  $("#main").innerHTML = heading("账号管理", "账号池", "管理登录会话、浏览器环境与代理分配。",
    `<button class="button primary" id="add-account">${icon("plus")}添加账号</button>`) +
    `<section class="panel table-panel"><div class="panel-title"><h2>账号列表 <span class="badge">${rows.length}</span></h2><button class="button small subtle" id="refresh-accounts">刷新列表</button></div>
    <form id="account-filters" class="filter-form account-filters"><label><span class="label">平台</span><select class="field" name="platform">${options([["", "全部平台"], ...PLATFORM_OPTIONS], filters.platform)}</select></label><label class="filter-keyword"><span class="label">账号名称 / 标识</span><input class="field" name="q" value="${esc(filters.q)}" placeholder="搜索账号"></label><div class="filter-submit"><button class="button primary" type="submit">${icon("search")}筛选</button><button class="button" type="button" id="reset-account-filters">重置</button></div></form>
    ${rows.length ? `<div class="table-wrap"><table class="account-table"><thead><tr><th>账号</th><th>网络连接</th><th>使用设置</th><th>操作</th></tr></thead><tbody>${rows.map(account => {
      const enabled = account.status !== "disabled";
      const assigned = (state.settings.proxies || []).find(proxy => proxy.id === account.proxy_id);
      const managed = ["adspower", "kameleo"].includes(account.browser_provider);
      const networkName = managed ? `${account.browser_provider === "adspower" ? "AdsPower" : "Kameleo"} 环境代理` : assigned?.name || (account.proxy_file || account.proxy_env ? "已有代理" : "本机直连");
      const networkDetail = account.browser_provider === "adspower" ? account.adspower_profile_id : account.browser_provider === "kameleo" ? account.kameleo_profile_id : assigned?.endpoint || "";
      const browserAction = account.collection_viewer ? "观看采集" : "打开浏览器";
      const loginState = managed ? "登录由浏览器环境管理" : account.cookie_state === "configured" ? "已保存登录" : "尚未保存登录";
      return `<tr><td><div class="account-identity">${logo(account.platform)}<div><button class="account-name" data-account-detail="${account.id}">${esc(account.name)} ${icon("chevron")}</button><p>${platformName(account.platform)} · ${esc(account.expected_user_id || account.account_ref)}</p><small>${loginState}</small></div></div></td><td><div class="account-network"><strong>${esc(networkName)}</strong><small>${esc(networkDetail)}</small></div></td><td><button class="account-enable ${enabled ? "is-enabled" : ""}" role="switch" aria-checked="${enabled}" aria-label="${enabled ? "禁用" : "启用"}账号 ${esc(account.name)}" data-account-toggle="${account.id}" data-enable="${!enabled}"><span></span>${enabled ? "已启用" : "已禁用"}</button></td><td><div class="account-row-actions"><button class="button small" data-account-browser="${account.id}">${browserAction}</button><button class="button small" data-account-menu="${account.id}" popovertarget="account-menu-${account.id}" aria-label="${esc(account.name)}的更多操作">••• 更多</button><div class="account-action-menu" id="account-menu-${account.id}" popover><small>账号操作</small>${managed ? "" : `<button data-account-login="${account.id}">${account.cookie_state === "configured" ? "重新登录" : "登录"}</button>`}<button data-account-edit="${account.id}">账号设置</button><button data-account-browser="${account.id}">${browserAction}</button><hr><button class="danger" data-account-delete="${account.id}">删除账号</button></div></div></td></tr>`;
    }).join("")}</tbody></table></div>` : empty(all.length ? "没有匹配账号" : "还没有账号", all.length ? "调整筛选条件，或重置筛选查看全部账号。" : "添加账号并登录，即可开始采集。")}</section>`;
  $("#add-account").onclick = openNewAccount;
  $("#refresh-accounts").onclick = (event) => action(event.currentTarget, async () => {
    await reloadAccountList();
    if (state.page === "accounts") renderAccountPool();
  });
  $("#account-filters").onsubmit = (e) => {
    e.preventDefault();
    state.accountFilters = Object.fromEntries(new FormData(e.currentTarget));
    renderAccountPool();
  };
  $("#reset-account-filters").onclick = () => {
    state.accountFilters = { platform: "", q: "" };
    renderAccountPool();
  };
  $$('[data-account-detail]').forEach(button => {
    button.onclick = () => openAccountDetail(button.dataset.accountDetail);
  });
  const closeMenus = () => $$('.account-action-menu:popover-open').forEach(menu => menu.hidePopover());
  $$('[data-account-menu]').forEach(button => {
    const menu = document.getElementById(button.getAttribute("popovertarget"));
    menu.addEventListener("beforetoggle", event => {
      if (event.newState !== "open") return;
      const rect = button.getBoundingClientRect();
      menu.style.left = `${Math.max(12, Math.min(rect.right - 196, innerWidth - 208))}px`;
      menu.style.top = `${Math.max(12, Math.min(rect.bottom + 8, innerHeight - 374))}px`;
    });
  });
  $$('[data-account-browser]').forEach(button => {
    button.onclick = () => {
      closeMenus();
      openAccountBrowser(button.dataset.accountBrowser);
    };
  });
  $$('[data-account-edit]').forEach(button => {
    button.onclick = () => {
      closeMenus();
      openAccountDetail(button.dataset.accountEdit);
    };
  });
  $$('[data-account-login]').forEach(button => {
    button.onclick = () => { closeMenus(); loginPoolAccount(button, all.find(account => account.id === button.dataset.accountLogin)); };
  });
  $$('[data-account-toggle]').forEach(button => {
    button.onclick = () => action(button, async () => {
      const enable = button.dataset.enable === 'true';
      await api(`/api/accounts/${button.dataset.accountToggle}/status`, {status:enable ? 'ready' : 'disabled'});
      await reloadAccountList();
      if (state.page === 'accounts') renderAccountPool();
      toast(enable ? '账号已启用' : '账号已禁用，不再分配新任务');
    });
  });
  $$('[data-account-delete]').forEach(button => {
    button.onclick = async () => {
      const account = all.find(row => row.id === button.dataset.accountDelete);
      closeMenus();
      if (!await confirmAction(`确定删除账号“${account.name}”吗？历史记录和本地会话文件会保留。`)) return;
      action(button, async () => {
        const result = await api(`/api/accounts/${account.id}`, undefined, "DELETE");
        await reloadAccountList();
        toast(result.message);
        if (state.page === "accounts") renderAccountPool();
      });
    };
  });
}

async function loadAgentSettingsForm(container) {
  if (!container) return;
  container.innerHTML = '<p class="hint">正在读取模型配置…</p>';
  try {
    const config = await api("/api/analysis/settings");
    if (!container.isConnected) return;
    container.innerHTML = `<form id="agent-settings-form" autocomplete="off">
      <div id="agent-settings-error" class="notice" ${config.validation_error ? "" : "hidden"}>${esc(config.validation_error || "")}</div>
      <div class="form-grid">
      <label><span class="label">接口地址</span><input class="field" name="api_url" type="url" required value="${esc(config.api_url)}" placeholder="https://api.anthropic.com"></label>
      <label><span class="label">模型名称</span><input class="field" name="model" required maxlength="200" value="${esc(config.model)}" placeholder="sonnet"></label>
      <label><span class="label">API Key</span><input class="field" name="api_key" type="password" autocomplete="new-password" placeholder="${config.api_key_configured ? "已保存，留空保持不变" : "填写 API Key"}"></label>
      </div>
      <details class="advanced"><summary>高级运行参数</summary><div class="form-grid">
        <label><span class="label">备用模型</span><input class="field" name="fallback_model" maxlength="200" value="${esc(config.fallback_model || '')}" placeholder="主模型不可用时使用"></label>
        <label><span class="label">最大轮次</span><input class="field" name="max_turns" type="number" min="1" max="1000" step="1" value="${config.max_turns ?? ''}" placeholder="留空不限制"></label>
        <label><span class="label">本轮预算上限（USD）</span><input class="field" name="max_budget_usd" type="number" min="0.01" max="100000" step="0.01" value="${config.max_budget_usd ?? ''}" placeholder="留空不限制"></label>
        <label><span class="label">消息缓冲区（MiB）</span><input class="field" name="max_buffer_size_mb" type="number" required min="1" max="64" step="1" value="${config.max_buffer_size_mb}"></label>
        <label><span class="label">推理模式</span><select class="field" name="thinking_mode">
          <option value="default" ${config.thinking_mode === 'default' ? 'selected' : ''}>模型默认</option>
          <option value="adaptive" ${config.thinking_mode === 'adaptive' ? 'selected' : ''}>自适应</option>
          <option value="enabled" ${config.thinking_mode === 'enabled' ? 'selected' : ''}>固定 Token 预算</option>
          <option value="disabled" ${config.thinking_mode === 'disabled' ? 'selected' : ''}>关闭扩展思考</option>
        </select></label>
        <label><span class="label">思考 Token 预算</span><input class="field" name="thinking_budget_tokens" type="number" min="1024" max="128000" step="1" value="${config.thinking_budget_tokens ?? ''}" placeholder="仅固定模式需要"></label>
        <label><span class="label">推理强度</span><select class="field" name="effort">
          <option value="" ${!config.effort ? 'selected' : ''}>模型默认</option>
          <option value="low" ${config.effort === 'low' ? 'selected' : ''}>低</option>
          <option value="medium" ${config.effort === 'medium' ? 'selected' : ''}>中</option>
          <option value="high" ${config.effort === 'high' ? 'selected' : ''}>高</option>
          <option value="xhigh" ${config.effort === 'xhigh' ? 'selected' : ''}>超高</option>
          <option value="max" ${config.effort === 'max' ? 'selected' : ''}>最大</option>
        </select></label>
      </div><p class="hint">留空表示使用 SDK 默认值。推理模式、强度、备用模型和费用统计是否生效取决于模型服务兼容性。</p></details>
      <p class="hint" id="agent-key-status">${config.api_key_configured ? "密钥已配置，保存后不回显。" : "尚未配置密钥。"}</p>
      <p class="hint">请填写模型服务地址和该服务支持的模型名称；是否可用以实际检测结果为准。</p>
      <p class="hint">当前 SDK 服务地址：<span id="agent-effective-url">${esc(config.sdk_base_url)}</span></p>
      <div class="form-actions"><div class="stack"><button class="button primary" type="submit">保存模型配置</button><button class="button" id="agent-test" type="button">检测可用性</button></div><small class="hint">保存不调用模型；检测会发送一条简短消息。</small></div>
      <div class="notice" id="agent-test-result" role="status" hidden></div></form>`;
    const form = $("form", container);
    form.onsubmit = (e) => {
      e.preventDefault();
      const submittedForm = e.currentTarget;
      action($("[type=submit]", submittedForm), async () => {
        const body = Object.fromEntries(new FormData(submittedForm));
        const saved = await api("/api/analysis/settings", body);
        analysisState.configured = saved.configured;
        analysisState.model = saved.model;
        $("[name=api_key]", submittedForm).value = "";
        $("[name=api_key]", submittedForm).placeholder = saved.api_key_configured ? "已保存，留空保持不变" : "填写 API Key";
        $("#agent-key-status", submittedForm).textContent = saved.api_key_configured ? "密钥已配置，保存后不回显。" : "尚未配置密钥。";
        $("#agent-effective-url", submittedForm).textContent = saved.sdk_base_url;
        $("#agent-settings-error", submittedForm).hidden = true;
        $("#agent-settings-error", submittedForm).textContent = "";
        updateAnalysisControls();
        toast("模型配置已保存，下一次发送生效");
      });
    };
    $("#agent-test", form).onclick = (event) => {
      const button = event.currentTarget;
      const result = $("#agent-test-result", form);
      result.hidden = true;
      action(button, async () => {
        try {
          const checked = await api(
            "/api/analysis/settings/check",
            Object.fromEntries(new FormData(form)),
          );
          result.textContent = `检测成功 · ${checked.model}：${checked.reply}`;
          result.hidden = false;
        } catch (error) {
          result.textContent = `检测失败：${error.message}`;
          result.hidden = false;
          throw error;
        }
      });
    };
  } catch (error) {
    if (container.isConnected) container.innerHTML = `<div class="notice">${esc(error.message)}</div>`;
  }
}

function renderSystemSettings() {
  const runtime = state.settings.runtime;
  const quotas = state.risk.quotas || [];
  const running = state.dashboard.jobs.filter((job) => job.status === "running").length;
  const queued = state.dashboard.jobs.filter((job) => job.status === "queued").length;
  $("#main").innerHTML =
    heading(
      "服务设置",
      "系统配置",
      "数据库、采集进程 和本地服务运行参数集中管理。",
    ) +
    `<section class="stats system-stats"><div class="stat"><div class="stat-label">采集进程数</div><strong>${runtime.workers}</strong><small>修改环境变量后重启生效</small></div><div class="stat"><div class="stat-label">运行中作业</div><strong>${running}</strong><small>包含采集与环境检测</small></div><div class="stat"><div class="stat-label">排队作业</div><strong>${queued}</strong><small>等待采集进程或资源释放</small></div><div class="stat"><div class="stat-label">运行模式</div><strong class="text-value">单实例</strong><small>数据库租约完成前保持一个应用实例</small></div></section>
    <section class="panel system-config-panel" data-settings-section="model"><div class="panel-title"><h2>Agent 模型配置</h2><span>保存后下一次发送生效</span></div><div class="panel-body" id="agent-settings-panel"></div></section>
    <section class="panel system-config-panel" data-settings-section="general"><div class="panel-title"><h2>${icon("db")}采集数据库配置</h2><span>真实采集与定时调度共享</span></div><form id="system-database-form"><div class="panel-body"><div class="system-config-row"><div><span class="label">当前连接</span><strong>${esc(state.settings.database)}</strong></div><span>${state.settings.database.startsWith("sqlite") ? statusBadge("partial", "演示数据库") : statusBadge("completed", "已连接")}</span></div><label><span class="label">新的数据库连接地址</span><input class="field" name="database_url" type="password" placeholder="postgresql+psycopg://user:password@host:5432/db" autocomplete="new-password"></label><p class="hint">留空保留当前地址。连接密码不会出现在日志和指标中。</p><div class="form-actions"><button class="button primary" type="submit">保存数据库配置</button></div></div></form></section>
    <section class="panel system-config-panel" data-settings-section="risk"><div class="panel-title"><h2>账号自动冷却</h2><span>默认关闭 · 保存后立即生效</span></div><form id="cooldown-policy-form"><div class="panel-body"><label class="checkbox-label"><input type="checkbox" name="automatic_cooldown" ${state.settings.automatic_cooldown ? "checked" : ""}>遇到限流、验证码、访问拒绝或出口异常时自动冷却账号</label><p class="hint">关闭时仍停止本次失败任务并记录原因，但不增加账号冷却等待；登录失效仍提示需要登录。开启后按错误类型等待 30 分钟至 24 小时（平台返回等待时间时优先采用），到期后自动执行恢复检测和小范围试跑。手动重新登录成功后始终可直接试跑。已有冷却可在账号池检查可用性。</p><div class="form-actions"><button class="button primary" type="submit">保存冷却设置</button></div></div></form></section>
    <section class="panel system-config-panel" data-settings-section="risk"><div class="panel-title"><h2>风控窗口额度策略</h2><span>滚动安全额度；每次修改生成新版本</span></div><div class="panel-body"><p class="hint">这里只配置平台、动作、账号和出口地址组的滚动风控额度；单次任务请求预算请在“新建采集”的高级设置或任务详情中调整。</p><details class="policy-editor"><summary>新增 / 更新策略<span>需要调整额度时展开</span></summary><form id="quota-policy-form" class="filter-form"><label><span class="label">维度</span><select class="field" name="dimension">${options([["platform", "平台"], ["operation", "动作"], ["account", "账号"], ["ip_group", "出口地址组"]], "platform")}</select></label><label data-quota-subject></label><label><span class="label">采集动作</span><select class="field" name="operation"></select></label>${input("window_seconds", "窗口秒数", "900", "900", "number")}${input("request_limit", "请求上限", "1", "1", "number")}<div class="filter-submit"><button class="button primary" type="submit">新增策略版本</button></div></form></details><div id="quota-list"></div></div></section>
    <section class="panel system-config-panel" data-settings-section="general"><div class="panel-title"><h2>运行参数</h2><span>容器环境</span></div><div class="panel-body system-kv"><div><span>调度器</span><strong>已启用 · 5 秒轮询</strong></div><div><span>最短定时间隔</span><strong>15 分钟</strong></div><div><span>账号分配</span><strong>同平台 · 可用状态 · 最久未使用</strong></div><div><span>并发保护</span><strong>账号 / 浏览器目录 / 代理端点串行</strong></div></div></section>`;
  ConsoleLists.setupSettings();
  $("#cooldown-policy-form").onsubmit = (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    action($("[type=submit]", form), async () => {
      state.settings = await api("/api/settings", {
        automatic_cooldown: new FormData(form).has("automatic_cooldown"),
      });
      toast("冷却设置已保存");
      renderSystemSettings();
    });
  };
  $("#system-database-form").onsubmit = (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    action($("[type=submit]", form), async () => {
      state.settings = await api("/api/settings", {
        database_url: new FormData(form).get("database_url"),
      });
      toast("数据库配置已保存");
      renderSystemSettings();
      await refresh();
    });
  };
  bindQuotaForm($("#quota-policy-form"), quotas);
  $("#quota-policy-form").onsubmit = (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    action($("[type=submit]", form), async () => {
      const body = Object.fromEntries(new FormData(form));
      await api("/api/quota-policies", body);
      await reloadControlPlane();
      toast("额度策略版本已创建");
      renderSystemSettings();
    });
  };
}
const analysisState = {
  id: "", sessions: [], data: null, draft: "", configured: false, model: "",
  busy: false, selecting: false, sequence: 0, polling: false, initialized: false,
  attachments: [], uploading: false, search: "", mode: "readonly",
};
const analysisStatuses = {
  idle: "等待提问", running: "正在分析", stopping: "正在停止", stopped: "已停止",
  completed: "已完成", failed: "分析失败", interrupted: "已中断",
};
const analysisTools = {
  coverage: "查看数据范围", search: "检索帖子与评论",
  list_runs: "查看采集任务", run_data: "读取任务快照",
  list_media: "查看帖子媒体", inspect_media: "读取图片或视频画面",
};
function analysisScopeText(scope = {kind: "all"}) {
  if (scope.kind === "post") return `${platformName(scope.platform)} · 帖子 ${scope.content_id}`;
  if (scope.kind === "run") return `采集任务 ${scope.run_id.slice(0, 12)} · 已保存快照`;
  return "自由分析 · 可查询已采集的帖子、评论和任务";
}
const analysisModeLabel = mode => mode === 'workspace' ? '工作区 Agent' : '只读分析';
const analysisSessionMode = session => session?.scope?.mode === 'workspace' ? 'workspace' : 'readonly';
async function beginAnalysis(scope, mode = "") {
  if (analysisState.busy || analysisState.uploading) throw new Error("请等待消息或附件提交完成");
  if (!mode) {
    showModal("选择 Agent 执行模式", `<div class="analysis-mode-options">
      <button class="analysis-mode-card" data-analysis-mode-choice="readonly"><strong>只读分析</strong><span>仅使用采集数据查询工具，不读取工作区文件，不执行命令。</span><small>适合内容洞察、评论归纳和任务结果分析</small></button>
      <button class="analysis-mode-card danger" data-analysis-mode-choice="workspace"><strong>工作区 Agent</strong><span>开放 Claude Code 工具、Skills 和所有命令权限，命令自动执行。</span><small>可处理媒体和文件，也可能修改或删除工作区内容</small></button>
    </div>`);
    $$('[data-analysis-mode-choice]', $('#modal')).forEach(button => {
      button.onclick = () => action(button, async () => {
        $('#modal').close();
        await beginAnalysis(scope, button.dataset.analysisModeChoice);
      });
    });
    return;
  }
  const data = await api("/api/analysis/sessions", {scope: {...scope, mode}});
  analysisState.id = data.session.id;
  analysisState.data = data;
  analysisState.mode = mode;
  analysisState.attachments = [];
  analysisState.draft = scope.kind === "post"
    ? "分析这个帖子的主要内容和评论中的观点、需求与争议，附上原文证据。"
    : "分析本次采集结果的主要话题、用户需求和不同观点，说明样本范围并附上证据。";
  go("analysis");
}
function renderAnalysis() {
  $('#main').innerHTML = `<div class="analysis-page-heading"><div><span class="eyebrow">内容洞察</span><h1>Agent 分析</h1></div><div class="analysis-page-controls"><button class="button" id="analysis-settings">${icon('settings')}模型配置</button></div></div>
  <div id="analysis-config"></div><div class="analysis-layout">
    <aside class="panel analysis-history"><div class="analysis-history-head"><h2>会话</h2><button class="button small" id="analysis-new">${icon('plus')}新建</button></div>
      <div class="analysis-search">${icon('search')}<input id="analysis-search" aria-label="搜索会话" placeholder="搜索会话…" autocomplete="off"></div>
      <div id="analysis-sessions"></div><div class="analysis-history-foot">${icon('shield')}仅在你发送时开始分析</div></aside>
    <section class="panel analysis-chat"><header class="analysis-chat-head"><div class="analysis-chat-heading"><h2 id="analysis-title">自由分析</h2><small id="analysis-scope"></small></div>
      <div class="analysis-chat-actions"><label class="analysis-mode-picker"><span>本会话模式</span><select class="field" id="analysis-mode" aria-label="本会话模式"><option value="readonly">只读分析</option><option value="workspace">工作区 Agent</option></select></label><span class="badge" id="analysis-status"></span><button class="icon-button" id="analysis-rename" title="重命名会话" aria-label="重命名会话">${icon('edit')}</button><button class="icon-button" id="analysis-export" title="导出 Markdown" aria-label="导出 Markdown">${icon('download')}</button></div></header>
      <div class="analysis-scroll-area"><div id="analysis-messages" class="analysis-messages" role="log" aria-label="分析消息"></div><button id="analysis-bottom" class="button small" hidden>↓ 回到最新</button></div>
      <div id="analysis-error" role="status"></div><form id="analysis-form" class="analysis-composer">
        <div id="analysis-attachments" class="attachment-list"></div><label class="sr-only" for="analysis-prompt">分析问题</label>
        <textarea id="analysis-prompt" rows="2" maxlength="20000" placeholder="提出问题，或添加图片和文件一起分析…" required></textarea>
        <div class="analysis-actions"><div class="stack"><button class="icon-button" id="analysis-attach" type="button" aria-label="添加图片或附件" title="添加图片或附件">${icon('paperclip')}</button><span id="analysis-model" class="analysis-model"></span><small id="analysis-upload-status" role="status"></small></div><div class="stack"><small class="composer-shortcut">Enter 发送 · Shift+Enter 换行</small><button class="button" id="analysis-stop" type="button" hidden>停止分析</button><button class="button primary" id="analysis-send" type="submit">发送 ${icon('arrow')}</button></div></div>
        <input id="analysis-file" type="file" multiple hidden accept=".png,.jpg,.jpeg,.webp,.gif,.txt,.md,.csv,.json,.log,.pdf,.docx">
        <p class="composer-note">支持 Markdown · 拖入文件或粘贴图片 · 最多 6 个附件，单个 8 MB</p>
  </form></section></div>`;
  $('#analysis-settings').onclick = () => { showModal('Agent 模型配置', '<div id="agent-settings-panel"></div>'); loadAgentSettingsForm($('#agent-settings-panel')); };
  const modePicker = $('#analysis-mode');
  modePicker.value = analysisState.id ? analysisSessionMode(analysisState.data?.session) : analysisState.mode;
  modePicker.disabled = Boolean(analysisState.id);
  modePicker.onchange = () => { analysisState.mode = modePicker.value; updateAnalysisControls(); };
  const input = $('#analysis-prompt'); input.value = analysisState.draft;
  input.oninput = () => { analysisState.draft = input.value; resizeAnalysisInput(); };
  input.onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (!$('#analysis-send').disabled) $('#analysis-form').requestSubmit(); } };
  $('#analysis-search').value = analysisState.search;
  $('#analysis-search').oninput = e => { analysisState.search = e.target.value; paintAnalysisHistory(); };
  $('#analysis-new').onclick = () => {
    if (analysisState.busy || analysisState.uploading) return;
    analysisState.sequence++; analysisState.selecting = false; analysisState.id = ''; analysisState.data = null;
    analysisState.mode = 'readonly'; analysisState.draft = ''; analysisState.attachments = []; renderAnalysis(); $('#analysis-prompt').focus();
  };
  $('#analysis-form').onsubmit = async e => {
    e.preventDefault(); if ($('#analysis-send').disabled) return;
    const prompt = input.value.trim(); if (!prompt) return;
    analysisState.busy = true; updateAnalysisControls();
    try {
      await ensureAnalysisSession();
      await api(`/api/analysis/sessions/${analysisState.id}/messages`, {message: prompt, attachments: analysisState.attachments.map(a => a.id)});
      analysisState.draft = ''; analysisState.attachments = []; input.value = ''; resizeAnalysisInput(); paintAnalysisAttachments();
      await loadAnalysisSession(analysisState.id, false); await loadAnalysisList();
      $('#analysis-messages').scrollTop = $('#analysis-messages').scrollHeight;
    } catch (error) { toast(error.message); }
    finally { analysisState.busy = false; updateAnalysisControls(); }
  };
  $('#analysis-stop').onclick = e => action(e.currentTarget, async () => { const id = analysisState.id; await api(`/api/analysis/sessions/${id}/stop`, {}); if (id === analysisState.id) await loadAnalysisSession(id, false); });
  $('#analysis-attach').onclick = () => $('#analysis-file').click();
  $('#analysis-file').onchange = e => { uploadAnalysisFiles([...e.target.files]); e.target.value = ''; };
  const composer = $('#analysis-form');
  composer.ondragover = e => { if (e.dataTransfer.types.includes('Files')) { e.preventDefault(); composer.classList.add('dragover'); } };
  composer.ondragleave = e => { if (!composer.contains(e.relatedTarget)) composer.classList.remove('dragover'); };
  composer.ondrop = e => { e.preventDefault(); composer.classList.remove('dragover'); uploadAnalysisFiles([...e.dataTransfer.files]); };
  input.onpaste = e => { const files = [...e.clipboardData.files]; if (files.length) { e.preventDefault(); uploadAnalysisFiles(files); } };
  $('#analysis-rename').onclick = () => {
    if (!analysisState.id) return;
    const id = analysisState.id;
    showModal('重命名会话', `<form id="analysis-rename-form"><label class="label" for="analysis-name">会话名称</label><input class="field" id="analysis-name" maxlength="100" required value="${esc(analysisState.data.session.title)}"><div class="form-actions"><button class="button primary">保存名称</button></div></form>`);
    $('#analysis-rename-form').onsubmit = e => { e.preventDefault(); action($('button', e.currentTarget), async () => { const d = await api(`/api/analysis/sessions/${id}/rename`, {title: $('#analysis-name').value}); if (analysisState.id === id) analysisState.data = d; $('#modal').close(); paintAnalysis(); await loadAnalysisList(); }); };
  };
  $('#analysis-export').onclick = () => {
    if (!analysisState.id) return;
    const a = document.createElement('a'); a.href = `/api/analysis/sessions/${analysisState.id}/export`; a.download = 'analysis.md'; a.click();
  };
  $('#analysis-messages').onscroll = updateAnalysisScroll;
  $('#analysis-bottom').onclick = () => { const m = $('#analysis-messages'); m.scrollTo({top: m.scrollHeight, behavior: 'smooth'}); };
  $('#analysis-messages').onclick = analysisMessageAction;
  paintAnalysis(); paintAnalysisAttachments(); resizeAnalysisInput();
  loadAnalysisList().catch(error => toast(error.message));
  if (analysisState.id) loadAnalysisSession(analysisState.id, false).catch(error => toast(error.message));
  if (!analysisState.initialized) {
    analysisState.initialized = true;
    setInterval(async () => {
      if (state.page !== 'analysis' || document.hidden || analysisState.polling || analysisState.selecting || !analysisState.id) return;
      if (!['running','stopping'].includes(analysisState.data?.session.status)) return;
      analysisState.polling = true;
      try { await loadAnalysisSession(analysisState.id, false); if (!['running','stopping'].includes(analysisState.data?.session.status)) await loadAnalysisList(); }
      catch (error) { if ($('#analysis-error')) $('#analysis-error').textContent = `读取进度失败：${error.message}`; }
      finally { analysisState.polling = false; }
    }, 800);
  }
}
async function ensureAnalysisSession() {
  if (!analysisState.id) { const d = await api('/api/analysis/sessions', {scope: {kind: 'all', mode: analysisState.mode}}); analysisState.id = d.session.id; analysisState.data = d; }
}
function resizeAnalysisInput() { const e = $('#analysis-prompt'); if (e) { e.style.height = 'auto'; e.style.height = Math.min(e.scrollHeight, 160) + 'px'; } }
async function uploadAnalysisFiles(files) {
  if (!files.length || analysisState.uploading || analysisState.busy || analysisState.selecting) return;
  if (files.length + analysisState.attachments.length > 6) return toast('每条消息最多添加 6 个附件');
  if (files.some(f => !f.size || f.size > 8 * 1024 * 1024)) return toast('请选择非空文件，单个附件不能超过 8 MB');
  if ([...files, ...analysisState.attachments].reduce((n,f) => n + f.size, 0) > 20 * 1024 * 1024) return toast('每条消息的附件合计不能超过 20 MB');
  analysisState.uploading = true; updateAnalysisControls();
  try {
    await ensureAnalysisSession();
    for (const file of files) {
      if ($('#analysis-upload-status')) $('#analysis-upload-status').textContent = `正在上传 ${file.name}`;
      const encoded = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result.split(',')[1]); reader.onerror = () => reject(new Error('读取文件失败')); reader.readAsDataURL(file); });
      const meta = await api(`/api/analysis/sessions/${analysisState.id}/attachments`, {name: file.name, content: encoded});
      analysisState.attachments.push(meta); paintAnalysisAttachments();
    }
    paintAnalysis(); await loadAnalysisList();
  } catch (error) { toast(error.message); }
  finally { analysisState.uploading = false; if ($('#analysis-upload-status')) $('#analysis-upload-status').textContent = ''; updateAnalysisControls(); paintAnalysisAttachments(); }
}
const attachmentSize = size => size >= 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(size / 1024))} KB`;
function attachmentHTML(a, pending = false) {
  return `<div class="attachment-card ${a.is_image ? 'is-image' : ''}">${a.is_image ? `<button type="button" class="attachment-thumb" data-preview-image="${esc(a.url)}" aria-label="预览 ${esc(a.name)}"><img src="${esc(a.url)}" alt="${esc(a.name)}" loading="lazy"></button>` : `<span class="attachment-icon">${icon('file')}</span>`}<div class="attachment-meta"><a href="${esc(a.url)}?download=1" download="${esc(a.name)}">${esc(a.name)}</a><small>${attachmentSize(a.size)}${a.truncated ? ' · 已截取前 10 万字' : ''}</small></div>${pending ? `<button type="button" class="icon-button" data-remove-attachment="${esc(a.id)}" aria-label="移除 ${esc(a.name)}" ${analysisState.uploading ? 'disabled' : ''}>${icon('close')}</button>` : ''}</div>`;
}
function paintAnalysisAttachments() {
  const el = $('#analysis-attachments'); if (!el) return;
  el.innerHTML = analysisState.attachments.map(a => attachmentHTML(a, true)).join('') + (analysisState.attachments.some(a => a.is_image) ? '<small class="attachment-vision-note">图片随消息发送，需使用支持图片理解的模型</small>' : '');
  el.onclick = e => { const remove = e.target.closest('[data-remove-attachment]'); if (remove && !analysisState.uploading && !analysisState.busy) { analysisState.attachments = analysisState.attachments.filter(a => a.id !== remove.dataset.removeAttachment); paintAnalysisAttachments(); } const preview = e.target.closest('[data-preview-image]'); if (preview) previewAnalysisImage(preview.dataset.previewImage); };
}
function previewAnalysisImage(url) { showModal('图片预览', `<div class="analysis-image-preview"><img src="${esc(url)}" alt="图片预览" referrerpolicy="no-referrer"><a class="button small" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${icon('link')}打开原图</a></div>`); }
async function loadAnalysisList() {
  const d = await api('/api/analysis/sessions'); analysisState.sessions = d.sessions; analysisState.configured = d.configuration.configured; analysisState.model = d.configuration.model;
  paintAnalysisHistory(); updateAnalysisControls();
}
async function loadAnalysisSession(id, clearDraft = true) {
  if (clearDraft && (analysisState.uploading || analysisState.busy)) return;
  const sequence = ++analysisState.sequence;
  if (clearDraft) analysisState.selecting = true;
  updateAnalysisControls();
  try {
    const d = await api(`/api/analysis/sessions/${id}`); if (sequence !== analysisState.sequence) return;
    analysisState.id = id; analysisState.data = d; analysisState.mode = analysisSessionMode(d.session);
    if (clearDraft) { analysisState.draft = ''; analysisState.attachments = []; if ($('#analysis-prompt')) $('#analysis-prompt').value = ''; paintAnalysisAttachments(); }
    paintAnalysis();
    if (clearDraft && $('#analysis-messages')) $('#analysis-messages').scrollTop = $('#analysis-messages').scrollHeight;
  } finally { if (sequence === analysisState.sequence) analysisState.selecting = false; updateAnalysisControls(); }
}
function updateAnalysisControls() {
  if (state.page !== 'analysis' || !$('#analysis-send')) return;
  const status = analysisState.data?.session.status; const running = ['running','stopping'].includes(status);
  $('#analysis-status').textContent = analysisState.busy ? '正在提交' : analysisStatuses[status || 'idle'];
  $('#analysis-status').classList.toggle('is-running', running);
  $('#analysis-send').disabled = analysisState.busy || analysisState.selecting || analysisState.uploading || running || !analysisState.configured;
  $('#analysis-stop').hidden = !running; $('#analysis-stop').disabled = status === 'stopping';
  $('#analysis-new').disabled = analysisState.busy || analysisState.uploading;
  $$('[data-analysis-session]').forEach(b => { b.disabled = analysisState.busy || analysisState.uploading; });
  $('#analysis-attach').disabled = analysisState.busy || analysisState.uploading || analysisState.selecting;
  $('#analysis-prompt').readOnly = analysisState.busy;
  if ($('#analysis-mode')) { const picker = $('#analysis-mode'); picker.value = analysisState.id ? analysisSessionMode(analysisState.data?.session) : analysisState.mode; picker.disabled = Boolean(analysisState.id) || analysisState.busy || analysisState.selecting; picker.title = analysisState.id ? '本会话模式已锁定；如需更换请新建会话' : '仅用于即将创建的当前会话'; }
  $('#analysis-rename').disabled = !analysisState.id || analysisState.selecting || analysisState.busy;
  $('#analysis-export').disabled = !analysisState.data?.messages.length || analysisState.selecting;
  $('#analysis-model').textContent = analysisState.configured ? analysisState.model : '未配置模型';
  $('#analysis-config').innerHTML = `${analysisState.configured ? '' : '<div class="notice">填写模型配置后即可发送。历史记录和附件可随时查看。</div>'}${analysisState.mode === 'workspace' ? '<div class="notice analysis-agent-warning"><strong>工作区 Agent 已启用：</strong>模型可以读取和修改工作区文件、执行任意命令，并自动使用发现的 Skills。请只发送你愿意授予这些权限的任务。</div>' : ''}`;
}
function paintAnalysisHistory() {
  const el = $('#analysis-sessions'); if (!el) return;
  const list = analysisState.sessions.filter(s => s.title.toLowerCase().includes(analysisState.search.toLowerCase()));
  let group = '';
  const today = new Date(); today.setHours(0,0,0,0);
  const html = list.map(s => {
    const label = s.updated_at * 1000 >= today.getTime() ? '今天' : s.updated_at * 1000 >= today.getTime() - 86400000 ? '昨天' : '更早';
    const heading = group !== label ? `<div class="analysis-date-group">${label}</div>` : ''; group = label;
    const status = s.id === analysisState.id ? analysisState.data?.session.status || s.status : s.status;
    const kind = s.scope.kind === 'post' ? '单帖' : s.scope.kind === 'run' ? '采集任务' : '自由分析';
    const mode = analysisModeLabel(analysisSessionMode(s));
    return `${heading}<button class="analysis-session ${s.id === analysisState.id ? 'selected' : ''}" data-analysis-session="${esc(s.id)}" title="${esc(s.title)}" ${analysisState.busy || analysisState.uploading ? 'disabled' : ''}><span class="session-title">${icon(s.scope.kind === 'post' ? 'file' : s.scope.kind === 'run' ? 'db' : 'comment')}<strong>${esc(s.title)}</strong></span><span class="session-meta"><span>${kind} · ${mode}</span><span class="session-state ${status === 'running' ? 'is-running' : ''}">${esc(analysisStatuses[status])}</span><time>${new Date(s.updated_at * 1000).toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit',hour12:false})}</time></span></button>`;
  }).join('') || `<div class="history-empty">${icon('comment')}<p>${analysisState.search ? '没有找到匹配会话' : '还没有会话'}</p><small>${analysisState.search ? '试试其他关键词' : '从右侧开始一次新的分析'}</small></div>`;
  if (el.dataset.last !== html) { el.innerHTML = html; el.dataset.last = html; }
  el.onclick = e => { const b = e.target.closest('[data-analysis-session]'); if (b && !b.disabled) loadAnalysisSession(b.dataset.analysisSession).catch(error => toast(error.message)); };
}
function analysisText(text) { return AnalysisMarkdown.render(text, analysisState.id); }
function analysisMessageAction(e) {
  const citation = e.target.closest('[data-analysis-citation]');
  if (citation) { const id = analysisState.id; action(citation, async () => { const r = await api('/api/analysis/evidence?' + new URLSearchParams({session_id:id,citation:citation.dataset.analysisCitation})); showModal('分析证据', `<div class="analysis-evidence"><span class="badge">${platformName(r.platform)} · ${r.kind === 'content' ? '帖子' : '评论'}</span><h3>${esc(r.data.title || '原文内容')}</h3><p class="hint">${esc(r.data.author_name || '未知作者')} · ${esc(r.id)}</p><div class="analysis-evidence-text">${esc(r.data.text || '无正文')}</div><p class="hint">所属帖子：${esc(r.content_id)}</p></div>`); }); return; }
  const image = e.target.closest('[data-preview-image]'); if (image) return previewAnalysisImage(image.dataset.previewImage);
  const code = e.target.closest('[data-copy-code]');
  const copy = e.target.closest('[data-copy-message]');
  if (copy || code) { const text = code ? code.closest('pre').querySelector('code').textContent : analysisState.data.messages.find(m => String(m.id) === copy.dataset.copyMessage)?.text; navigator.clipboard.writeText(text || '').then(() => toast('已复制')).catch(() => toast('无法访问剪贴板，请手动选择复制')); }
  const starter = e.target.closest('[data-analysis-starter]'); if (starter) { $('#analysis-prompt').value = starter.dataset.analysisStarter; analysisState.draft = starter.dataset.analysisStarter; resizeAnalysisInput(); $('#analysis-prompt').focus(); }
}
function updateAnalysisScroll() { const m = $('#analysis-messages'); if (m) $('#analysis-bottom').hidden = m.scrollHeight - m.scrollTop - m.clientHeight < 120; }
function paintAnalysis() {
  if (state.page !== 'analysis' || !$('#analysis-messages')) return;
  const d = analysisState.data;
  $('#analysis-title').textContent = d?.session.title || '新的分析'; $('#analysis-title').title = d?.session.title || '新的分析';
  $('#analysis-scope').textContent = analysisScopeText(d?.session.scope);
  $('#analysis-error').innerHTML = d?.session.error ? `<div class="notice">${esc(d.session.error)}</div>` : '';
  const messages = $('#analysis-messages'); const nearBottom = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 100;
  const blocks = []; let tools = [];
  const flushTools = () => {
    if (!tools.length) return;
    const refs = tools.flatMap(m => m.role === 'sources' ? m.data.items : []); const calls = tools.filter(m => m.role === 'tool');
    blocks.push({key:'tools-'+tools[0].id, html:`<details class="analysis-activity"><summary>${icon('search')}查询过程 <span>${calls.length} 次查询${refs.length ? ` · ${refs.length} 条证据` : ''}</span></summary><div class="analysis-activity-body">${calls.map(m => `<div class="analysis-tool">${icon('check')}${esc(analysisTools[m.text] || '查询数据')}</div>`).join('')}<div class="analysis-source-list">${refs.map((r,i) => `<button class="analysis-citation" data-analysis-citation="${esc(r.citation)}">${r.kind === 'content' ? '帖子' : '评论'} ${i+1} · ${esc(r.id.slice(-8))}</button>`).join('')}</div></div></details>`}); tools = [];
  };
  for (const m of d?.messages || []) {
    if (['tool','sources'].includes(m.role)) { tools.push(m); continue; }
    flushTools(); const user = m.role === 'user';
    blocks.push({key:'message-'+m.id, html:`<article class="analysis-message ${user ? 'user' : 'assistant'}"><div class="message-avatar">${user ? '你' : icon('spark')}</div><div class="message-content"><div class="message-byline"><strong>${user ? '你' : '拾集 Agent'}</strong><small>${user ? '' : esc(m.data?.model || d?.session.model || '')}</small><time>${new Date(m.created_at*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false})}</time></div><div class="analysis-message-text markdown-body">${analysisText(m.text)}</div>${m.data?.attachments?.length ? `<div class="attachment-list">${m.data.attachments.map(a => attachmentHTML(a)).join('')}</div>` : ''}<div class="message-actions"><button data-copy-message="${m.id}" type="button" title="复制消息">${icon('copy')}复制</button></div></div></article>`});
  }
  flushTools();
  if (['running','stopping'].includes(d?.session.status)) blocks.push({key:'progress',html:`<div class="analysis-progress"><span class="typing-dots"><i></i><i></i><i></i></span>${d.session.status === 'stopping' ? '正在停止…' : 'Agent 正在分析…'}</div>`});
  if (!blocks.length) blocks.push({key:'empty', html:`<div class="analysis-welcome"><div class="welcome-icon">${icon('spark')}</div><span class="eyebrow">从数据里，发现答案</span><h2>${d?.session.scope.kind === 'post' ? '一起读懂这个帖子' : d?.session.scope.kind === 'run' ? '看看这次采集发现了什么' : '今天想了解什么？'}</h2><p>查找内容、比较观点、整理发现。<br>每一个结论，都可以回到原始证据。</p><div class="analysis-starters">${['总结主要话题，并引用原文证据','评论里有哪些反复出现的需求？','比较不同观点，整理成表格'].map(t => `<button data-analysis-starter="${t}">${icon('arrow')}${t}</button>`).join('')}</div><small>也可以添加图片、PDF 或文档，随问题一起发送</small></div>`});
  const existing = new Map([...messages.children].map(el => [el.dataset.key,el]));
  blocks.forEach((b, index) => {
    let el = existing.get(b.key);
    if (!el) { el = document.createElement('div'); el.dataset.key = b.key; }
    if (el.dataset.html !== b.html) { const open = el.querySelector('details')?.open; el.innerHTML = b.html; el.dataset.html = b.html; if (open && el.querySelector('details')) el.querySelector('details').open = true; }
    if (messages.children[index] !== el) messages.insertBefore(el, messages.children[index] || null);
    existing.delete(b.key);
  });
  existing.forEach(el => el.remove());
  if (nearBottom) messages.scrollTop = messages.scrollHeight;
  updateAnalysisScroll(); paintAnalysisHistory(); updateAnalysisControls();
}

async function init() {
  $$("[data-icon]").forEach((e) => (e.innerHTML = icon(e.dataset.icon)));
  $("#date-label").textContent = new Date().toLocaleDateString("zh-CN", {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
  $$("[data-page]").forEach((b) => (b.onclick = () => go(b.dataset.page)));
  $(".brand").onclick = (e) => {
    e.preventDefault();
    go("overview");
  };
  $("#modal").addEventListener("close", () => {
    state.modalRun = null;
    $$("video", $("#modal")).forEach((v) => v.pause());
    if ($("#agent-settings-form", $("#modal"))) $("#modal-body").replaceChildren();
  });
  $("#modal").addEventListener("click", (e) => {
    if (e.target === $("#modal")) {
      const r = $("#modal").getBoundingClientRect();
      if (
        e.clientX < r.left ||
        e.clientX > r.right ||
        e.clientY < r.top ||
        e.clientY > r.bottom
      )
        $("#modal").close();
    }
  });
  window.addEventListener("hashchange", () => {
    const page = location.hash.slice(1);
    if (page !== state.page) go(page);
  });
  $("#main").innerHTML =
    '<div class="loading"><span class="spinner"></span>正在连接本地工作空间</div>';
  try {
    const [bootstrap, risk, dashboard] = await Promise.allSettled([
      api("/api/bootstrap"),
      api("/api/risk?days=7"),
      api("/api/dashboard?" + new URLSearchParams(state.runFilters)),
    ]);
    if (bootstrap.status === "rejected") throw bootstrap.reason;
    const boot = bootstrap.value;
    state.token = boot.token;
    state.settings = boot.settings;
    if (risk.status === "fulfilled") state.risk = risk.value;
    else toast(risk.reason.message);
    state.dashboard.jobs = boot.settings.checks || [];
    state.seenJobs = new Set(state.dashboard.jobs.filter(j=>!["queued", "running"].includes(j.status)).map(j=>j.id));
    if (dashboard.status === "fulfilled") {
      state.dashboard = dashboard.value;
      $("#library-count").textContent = number(state.dashboard.stats.contents);
    } else {
      state.databaseError = dashboard.reason.message;
      toast(state.databaseError);
    }
    go(location.hash.slice(1) || "overview");
  } catch (e) {
    $("#main").innerHTML =
      `<div class="error-panel"><h2>暂时无法连接本地服务</h2><p>${esc(e.message)}</p><button class="button" id="retry-init">重新连接</button></div>`;
    $("#retry-init").onclick = () => location.reload();
  }
  setInterval(async () => {
    if (pollBusy || document.hidden || !state.settings || ["accounts", "proxies"].includes(state.page)) return;
    pollBusy = true;
    try {
      await refresh();
      $(".local-status").innerHTML =
        '<span class="online-dot"></span>本地服务已连接';
    } catch {
      $(".local-status").textContent = state.databaseError
        ? "数据库未连接 · 服务可用"
        : "连接异常 · 请检查本地服务";
      try {
        const jobs = await api("/api/jobs");
        state.dashboard.jobs = jobs.jobs;
        refreshInlineChecks();
      } catch {}
    } finally {
      pollBusy = false;
    }
  }, 3500);
}
init();

/* Bounded, independently paged console sections. Forms survive tab switches. */
"use strict";
window.ConsoleLists = (() => {
  const riskTabs = [["risk_events", "风险事件"], ["groups", "动作指标"], ["network_observations", "出口观测"], ["recovery_probes", "恢复检测"], ["quotas", "窗口额度"]];
  const workbenchTabs = [["runs", "采集任务"], ["schedules", "定时作业"]];
  let workbenchTab = "runs";
  const schedules = { page: 1, page_size: 10, filters: { platform: "" } };
  const settingsTabs = [["general", "采集与运行"], ["model", "Agent 模型"], ["risk", "风控策略"]];
  const risk = { tab: "risk_events", filters: { days: "7", platform: "", account_id: "", operation: "" }, pages: {} };
  const policies = { page: 1, page_size: 10, filters: { dimension: "", q: "" } };
  let settingsTab = "general";
  const dimensions = [["", "全部维度"], ["platform", "平台"], ["operation", "动作"], ["account", "账号"], ["ip_group", "出口地址组"]];
  const field = (name, label, values, value) => `<label><span class="label">${label}</span><select class="field" name="${name}">${options(values, value)}</select></label>`;
  function tabs(items, selected, scope) {
    return `<div class="section-tabs" role="tablist" aria-label="${scope === "risk" ? "观测分类" : scope === "workbench" ? "任务分类" : "配置分类"}">${items.map(([key, label]) => `<button type="button" role="tab" id="${scope}-tab-${key}" data-section-tab="${key}" aria-selected="${key === selected}" aria-controls="${scope}-section-${key}" tabindex="${key === selected ? 0 : -1}">${label}</button>`).join("")}</div>`;
  }
  function bindTabs(container, select) {
    const buttons = $$('[role="tab"]', container);
    buttons.forEach((button, i) => {
      button.onclick = () => select(button.dataset.sectionTab);
      button.onkeydown = (event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (i + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
        buttons[next].click(); buttons[next].focus();
      };
    });
  }
  function table(headers, rows) {
    return `<div class="table-wrap console-table"><table><thead><tr>${headers.map((h) => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
  }
  function row(cells) { return `<tr>${cells.map((cell) => `<td>${cell}</td>`).join("")}</tr>`; }
  function content(section, rows, settings = false) {
    if (!rows.length) return empty("没有匹配记录", section === "quotas" ? "调整筛选条件，或新增额度策略。" : "调整筛选条件或时间范围后重试。");
    if (section === "risk_events") return table(["时间", "平台 / 账号", "动作", "风险原因", "处理方式"], rows.map((r) => row([when(r.at), `${platformName(r.platform)} · ${esc(accountName(r.account_id))}`, esc(operationLabel(r.operation, "运行前检查")), statusBadge("blocked", reasonLabel(r.kind)), esc(uiLabel("actions", r.action, "等待人工处理"))])));
    if (section === "groups") return table(["账号 / 平台", "动作", "尝试", "成功率", "平均耗时", "产出"], rows.map((g) => row([`${esc(accountName(g.account_id))} · ${platformName(g.platform)}`, esc(operationLabel(g.operation)), number(g.attempts), `${(g.success_rate * 100).toFixed(1)}%`, `${number(g.avg_duration_ms)} 毫秒`, `${number(g.items)} 条`])));
    if (section === "network_observations") return table(["时间", "平台 / 账号", "出口 / 地址组", "请求 / 浏览器出口", "结果"], rows.map((n) => row([when(n.observed_at), `${platformName(n.platform)} · ${esc(accountName(n.account_id))}`, `${esc(n.egress_ip || "—")}<small class="cell-note">${esc(n.ip_group || "—")}</small>`, `${esc(n.http_egress_ip || "—")}<small class="cell-note">${esc(n.browser_egress_ip || "—")}</small>`, statusBadge(n.outcome === "success" ? "completed" : "blocked", resultLabel(n.error_kind || n.outcome))])));
    if (section === "recovery_probes") return table(["时间", "账号", "次数", "请求", "结果", "下次允许"], rows.map((p) => row([when(p.created_at), esc(accountName(p.account_id)), p.attempt, p.request_count, esc(resultLabel(p.error_kind || p.outcome || p.status)), when(p.next_allowed_at)])));
    if (section === "quotas") return table(["维度", "对象", "动作", "窗口", ...(settings ? ["上限", "版本"] : ["使用 / 上限", "剩余", "重置"])], rows.map((q) => row([esc(uiLabel("dimensions", q.dimension, "其他维度")), esc(quotaSubjectLabel(q)), esc(operationLabel(q.operation)), `${number(q.window_seconds)} 秒`, ...(settings ? [q.request_limit, `第 ${q.version} 版`] : [`${q.used} / ${q.request_limit}`, q.remaining, when(q.window_end)])])));
    return table(["作业", "平台", "采集目标", "周期", "下次执行", "状态", "操作"], rows.map((s) => {
      const cadence = s.kind === "cron" ? s.cron_expression : s.kind === "interval" ? `每 ${s.interval_minutes} 分钟（旧版）` : `每天 ${s.daily_time}（旧版）`;
      return row([`<strong>${esc(s.name)}</strong>`, platformName(s.config.platform), esc(sourceSummary(s.config)), `<code class="cron-display">${esc(cadence)}</code><small class="cell-note">${esc(s.timezone)}</small>`, when(s.next_run_at), s.enabled ? statusBadge("completed", "运行中") : statusBadge("blocked", "已暂停"), `<div class="table-actions"><button data-schedule-toggle="${s.id}" data-enabled="${!s.enabled}">${s.enabled ? "暂停" : "启用"}</button><button data-schedule-edit="${s.id}">编辑</button><button class="danger" data-schedule-delete="${s.id}" data-name="${esc(s.name)}">删除</button></div>`]);
    }));
  }
  function pager(data) {
    return `<div class="console-pagination" tabindex="-1"><span role="status">共 ${number(data.total)} 条 · 第 ${data.page} / ${data.pages} 页</span><div class="pagination-actions">${field("page_size", "每页条数", [["10", "10 条 / 页"], ["20", "20 条 / 页"], ["50", "50 条 / 页"]], String(data.page_size))}<button class="button small" data-page-step="first" ${data.page <= 1 ? "disabled" : ""}>首页</button><button class="button small" data-page-step="prev" ${data.page <= 1 ? "disabled" : ""}>上一页</button><button class="button small" data-page-step="next" ${data.page >= data.pages ? "disabled" : ""}>下一页</button><button class="button small" data-page-step="last" ${data.page >= data.pages ? "disabled" : ""}>末页</button></div></div>`;
  }
  function attachPager(container, pageState, data, load) {
    $('[name="page_size"]', container).onchange = (event) => { pageState.page_size = Number(event.target.value); pageState.page = 1; load("size"); };
    $$('[data-page-step]', container).forEach((button) => {
      button.onclick = () => { pageState.page = ({ first: 1, prev: data.page - 1, next: data.page + 1, last: data.pages })[button.dataset.pageStep]; load(button.dataset.pageStep); };
    });
  }
  function makeLoader(container, section, pageState, filters, settings = false) {
    let sequence = 0;
    return async function load(focus) {
      const current = ++sequence;
      container.setAttribute("aria-busy", "true");
      container.innerHTML = '<div class="loading"><span class="spinner"></span>正在加载记录</div>';
      try {
        let data;
        if (section === "schedules") {
          const rows = (state.settings.schedules || []).filter((s) => !filters.platform || s.config.platform === filters.platform);
          const pages = Math.max(1, Math.ceil(rows.length / pageState.page_size));
          const page = Math.min(pageState.page, pages);
          data = { items: rows.slice((page - 1) * pageState.page_size, page * pageState.page_size), page, page_size: pageState.page_size, pages, total: rows.length };
        } else {
          data = await api('/api/risk?' + new URLSearchParams({ ...filters, section, page: pageState.page, page_size: pageState.page_size }));
        }
        if (current !== sequence || !container.isConnected) return;
        pageState.page = data.page;
        container.innerHTML = content(section, data.items, settings) + pager(data);
        attachPager(container, pageState, data, load);
        if (section === "schedules") bindScheduleActions();
        if (focus) requestAnimationFrame(() => {
          if (current !== sequence || !container.isConnected) return;
          const target = focus === "size" ? $('.select-trigger', container) : $(`[data-page-step="${focus}"]:not(:disabled)`, container);
          (target || $('.console-pagination', container)).focus({ preventScroll: true });
          const panel = container.closest(".panel");
          if (panel.getBoundingClientRect().top < 0) panel.scrollIntoView({ block: "start", behavior: "instant" });
        });
      } catch (error) {
        if (current !== sequence || !container.isConnected) return;
        container.innerHTML = `<div class="console-load-error" role="alert"><p>${esc(error.message)}</p><button class="button" data-retry>重新加载</button></div>`;
        $('[data-retry]', container).onclick = () => load();
      } finally {
        if (current === sequence && container.isConnected) container.removeAttribute("aria-busy");
      }
    };
  }
  function setupWorkbench() {
    const runs = $('#workbench-section-runs');
    runs.insertAdjacentHTML('beforebegin', tabs(workbenchTabs, workbenchTab, 'workbench'));
    runs.insertAdjacentHTML('afterend', '<section class="panel table-panel console-list-panel" id="workbench-section-schedules" role="tabpanel" aria-labelledby="workbench-tab-schedules" hidden></section>');
    const host = $('#workbench-section-schedules');
    const mount = () => {
      host.innerHTML = `<div class="panel-title"><h2>定时作业</h2><span>管理执行周期；每次执行生成独立采集任务</span></div><form class="filter-form console-filters" id="schedule-filters">${field('platform', '平台', [['', '全部平台'], ...PLATFORM_OPTIONS], schedules.filters.platform)}<div class="filter-submit"><button class="button primary" type="submit">应用筛选</button><button class="button" type="button" data-reset>重置</button></div></form><div data-list-results></div>`;
      const form = $('#schedule-filters');
      form.onsubmit = (event) => { event.preventDefault(); schedules.filters = Object.fromEntries(new FormData(form)); schedules.page = 1; mount(); };
      $('[data-reset]', form).onclick = () => { schedules.filters = { platform: '' }; schedules.page = 1; mount(); };
      makeLoader($('[data-list-results]', host), 'schedules', schedules, schedules.filters)();
    };
    const select = (key) => {
      workbenchTab = key;
      runs.hidden = key !== 'runs';
      host.hidden = key !== 'schedules';
      const tablist = $('#workbench-tab-runs').parentElement;
      $$('[role="tab"]', tablist).forEach((tab) => { tab.setAttribute('aria-selected', String(tab.dataset.sectionTab === key)); tab.tabIndex = tab.dataset.sectionTab === key ? 0 : -1; });
      $('#new-collection').innerHTML = `${icon('plus')}${key === 'schedules' ? '新建定时作业' : '新建采集'}`;
      $('#new-collection').onclick = () => openCollection(key === 'schedules' ? 'schedule' : 'manual');
      $('#refresh-dashboard').onclick = (event) => action(event.currentTarget, async () => {
        if (key === 'schedules') { await reloadControlPlane(); if (host.isConnected && workbenchTab === key) mount(); toast('定时作业已刷新'); }
        else await refresh(true);
      });
      if (key === 'schedules') mount();
    };
    bindTabs($('#workbench-tab-runs').parentElement, select);
    select(workbenchTab);
  }
  function renderRisk() {
    $('#main').innerHTML = heading("运行监测", "风控观测", "按分类查看记录；各列表独立分页，可按时间、平台和账号查找。", `<button class="button primary" data-new-collection>${icon("plus")}新建采集</button>`) + tabs(riskTabs, risk.tab, "risk") + '<div id="risk-section-host"></div>';
    bindNavigation();
    const mount = () => {
      $$('[role="tab"]', $('#main')).forEach((tab) => { tab.setAttribute("aria-selected", String(tab.dataset.sectionTab === risk.tab)); tab.tabIndex = tab.dataset.sectionTab === risk.tab ? 0 : -1; });
      const section = risk.tab;
      const pageState = risk.pages[section] ||= { page: 1, page_size: 10 };
      const filters = risk.filters;
      const accounts = (state.settings.account_pool || []).filter((a) => !filters.platform || a.platform === filters.platform);
      const quota = section === "quotas", schedule = section === "schedules";
      const title = riskTabs.find(([key]) => key === section)[1];
      $('#risk-section-host').innerHTML = `<section class="panel table-panel console-list-panel" id="risk-section-${section}" role="tabpanel" aria-labelledby="risk-tab-${section}"><div class="panel-title"><h2>${title}</h2><span>${quota ? "当前生效策略 · 滚动额度" : schedule ? "全部定时作业 · 按平台筛选" : `近 ${filters.days} 天 · ${section === "groups" ? "按账号、平台和动作汇总" : "最新记录优先"}`}</span></div><form class="filter-form console-filters" id="observation-filters">${quota ? field("dimension", "维度", dimensions, filters.dimension || "") + `<label><span class="label">对象 / 账号名称</span><input class="field" name="q" value="${esc(filters.q || "")}" placeholder="搜索对象或账号"></label>` : `${schedule ? "" : field("days", "时间范围", [["7", "近 7 天"], ["30", "近 30 天"], ["90", "近 90 天"]], filters.days)}${field("platform", "平台", [["", "全部平台"], ...PLATFORM_OPTIONS], filters.platform)}${schedule ? "" : field("account_id", "账号", [["", "全部账号"], ...accounts.map((a) => [a.id, `${platformName(a.platform)} · ${a.name}`])], filters.account_id)}${["groups", "risk_events"].includes(section) ? field("operation", "动作", [["", "全部动作"], ...Object.entries(state.settings.ui_labels.operations)], filters.operation) : ""}` }<div class="filter-submit"><button class="button primary" type="submit">应用筛选</button><button class="button" type="button" data-reset>重置</button><button class="button" type="button" data-refresh>刷新</button></div></form><div data-list-results></div></section>`;
      const form = $('#observation-filters');
      const container = $('[data-list-results]', $('#risk-section-host'));
      const requestFilters = () => quota ? { dimension: filters.dimension || "", q: filters.q || "" } : schedule ? { platform: filters.platform } : { days: filters.days, platform: filters.platform, account_id: filters.account_id, ...(["groups", "risk_events"].includes(section) ? { operation: filters.operation } : {}) };
      const load = makeLoader(container, section, pageState, requestFilters());
      form.onsubmit = (event) => {
        event.preventDefault();
        Object.assign(filters, Object.fromEntries(new FormData(form)));
        risk.pages = {};
        mount();
      };
      $('[data-refresh]', form).onclick = async (event) => {
        if (schedule) await action(event.currentTarget, async () => { await reloadControlPlane(); if (state.page === "automation" && risk.tab === section) mount(); });
        else load();
      };
      $('[data-reset]', form).onclick = () => { risk.filters = { days: "7", platform: "", account_id: "", operation: "" }; risk.pages = {}; mount(); };
      const platform = $('[name="platform"]', form);
      if (platform && !schedule) platform.onchange = () => {
        const account = $('[name="account_id"]', form);
        account.innerHTML = options([["", "全部账号"], ...(state.settings.account_pool || []).filter((a) => !platform.value || a.platform === platform.value).map((a) => [a.id, `${platformName(a.platform)} · ${a.name}`])], "");
      };
      load();
    };
    bindTabs($('.section-tabs', $('#main')), (key) => { risk.tab = key; mount(); });
    mount();
  }
  function setupSettings() {
    $('.system-stats').insertAdjacentHTML('afterend', tabs(settingsTabs, settingsTab, "settings"));
    const panels = $$('[data-settings-section]');
    const groups = {};
    settingsTabs.forEach(([key]) => {
      const group = document.createElement('div');
      group.id = `settings-section-${key}`;
      group.setAttribute('role', 'tabpanel');
      group.setAttribute('aria-labelledby', `settings-tab-${key}`);
      panels.find((panel) => panel.dataset.settingsSection === key).before(group);
      panels.filter((panel) => panel.dataset.settingsSection === key).forEach((panel) => group.append(panel));
      groups[key] = group;
    });
    let modelLoaded = false, policyLoaded = false;
    const select = (key) => {
      settingsTab = key;
      Object.entries(groups).forEach(([name, group]) => { group.hidden = name !== key; });
      $$('[role="tab"]', $('.section-tabs', $('#main'))).forEach((tab) => { tab.setAttribute('aria-selected', String(tab.dataset.sectionTab === key)); tab.tabIndex = tab.dataset.sectionTab === key ? 0 : -1; });
      if (key === 'model' && !modelLoaded) { modelLoaded = true; loadAgentSettingsForm($('#agent-settings-panel')); }
      if (key === 'risk' && !policyLoaded) { policyLoaded = true; mountPolicies(); }
    };
    bindTabs($('.section-tabs', $('#main')), select);
    select(settingsTab);
  }
  function mountPolicies() {
    const host = $('#quota-list');
    host.innerHTML = `<div class="section-heading"><h3>当前生效策略</h3><span>历史版本由系统保留</span></div><form id="quota-list-filters" class="filter-form console-filters">${field("dimension", "筛选维度", dimensions, policies.filters.dimension)}<label><span class="label">对象 / 账号名称</span><input class="field" name="q" value="${esc(policies.filters.q)}" placeholder="搜索对象或账号"></label><div class="filter-submit"><button class="button primary" type="submit">筛选策略</button><button class="button" type="button" data-reset>重置</button></div></form><div data-list-results></div>`;
    const load = makeLoader($('[data-list-results]', host), 'quotas', policies, policies.filters, true);
    $('#quota-list-filters').onsubmit = (event) => { event.preventDefault(); policies.filters = Object.fromEntries(new FormData(event.currentTarget)); policies.page = 1; mountPolicies(); };
    $('[data-reset]', host).onclick = () => { policies.filters = { dimension: '', q: '' }; policies.page = 1; mountPolicies(); };
    load();
  }
  return { renderRisk, setupSettings, setupWorkbench };
})();

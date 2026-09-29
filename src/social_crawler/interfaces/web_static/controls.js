/* Shared, progressively enhanced controls. Native fields remain the form data source. */
"use strict";
(() => {
  let sequence = 0;
  let active = null;
  const controls = new Map();
  const chevron = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 10 5 5 5-5"/></svg>';
  const calendarIcon = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="3"/><path d="M7 3v4m10-4v4M3 11h18"/></svg>';
  const make = (tag, className, text) => {
    const el = document.createElement(tag);
    el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  };
  function button(className, text, label) {
    const el = make("button", className, text);
    el.type = "button";
    if (label) el.setAttribute("aria-label", label);
    return el;
  }
  const labelFor = (field) => field.labels?.[0]?.querySelector(".label")?.textContent.trim() || field.getAttribute("aria-label") || field.name;
  function emit(field) {
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function close(restore = false) {
    if (!active) return;
    const previous = active;
    active = null;
    previous.trigger.setAttribute("aria-expanded", "false");
    previous.trigger.removeAttribute("aria-activedescendant");
    previous.popup.remove();
    if (restore && previous.trigger.isConnected) previous.trigger.focus();
  }
  function position() {
    if (!active) return;
    const { anchor, popup } = active;
    if (!anchor.isConnected || !anchor.getClientRects().length) return close();
    const r = anchor.getBoundingClientRect();
    const viewport = window.visualViewport;
    const left = viewport?.offsetLeft || 0, top = viewport?.offsetTop || 0;
    const width = viewport?.width || innerWidth, height = viewport?.height || innerHeight;
    const below = top + height - r.bottom - 12, above = r.top - top - 12;
    popup.style.width = `${Math.min(Math.max(r.width, popup.classList.contains("date-popup") ? 300 : 220), width - 24)}px`;
    popup.style.maxHeight = `${Math.min(380, Math.max(below, above, 100))}px`;
    const h = popup.getBoundingClientRect().height;
    popup.style.left = `${Math.max(left + 12, Math.min(r.left, left + width - popup.offsetWidth - 12))}px`;
    popup.style.top = `${Math.max(top + 12, below >= h || below >= above ? r.bottom + 6 : r.top - h - 6)}px`;
  }
  function open(control, popup) {
    close();
    active = { ...control, popup };
    popup.id = `control-popup-${++sequence}`;
    popup.setAttribute("popover", "manual");
    // Keep popovers inside the owning dialog's focus scope and above its top layer.
    (control.field.closest("dialog") || document.body).append(popup);
    if (popup.showPopover) popup.showPopover();
    control.trigger.setAttribute("aria-controls", popup.id);
    control.trigger.setAttribute("aria-expanded", "true");
    position();
  }
  function listPopup(control, choices, selected, commit, editable = false) {
    const popup = make("div", "control-popup option-popup");
    popup.setAttribute("role", "listbox");
    popup.setAttribute("aria-label", labelFor(control.field));
    let index = Math.max(0, choices.findIndex((choice) => choice.value === selected && !choice.disabled));
    if (choices[index]?.disabled) index = choices.findIndex((choice) => !choice.disabled);
    const items = choices.map((choice, i) => {
      const item = make("div", "control-option", choice.label);
      item.id = `control-option-${++sequence}`;
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", String(choice.value === selected));
      item.setAttribute("aria-disabled", String(!!choice.disabled));
      item.addEventListener("pointermove", () => { if (!choice.disabled) highlight(i); });
      item.addEventListener("pointerdown", (event) => event.preventDefault());
      item.addEventListener("click", () => {
        if (choice.disabled) return;
        close(true);
        commit(choice.value);
      });
      popup.append(item);
      return item;
    });
    if (!choices.length) popup.append(make("div", "control-empty", editable ? "没有匹配建议，可继续输入" : "暂无可选项"));
    open(control, popup);
    const highlight = (next) => {
      index = next;
      items.forEach((item, i) => item.classList.toggle("is-active", i === index));
      if (items[index]) {
        control.trigger.setAttribute("aria-activedescendant", items[index].id);
        items[index].scrollIntoView({ block: "nearest" });
      }
    };
    highlight(index);
    let query = "", lastTyped = 0;
    active.keydown = (event) => {
      if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        if (editable && ["Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const enabled = choices.map((choice, i) => choice.disabled ? -1 : i).filter((i) => i >= 0);
        if (!enabled.length) return;
        const current = enabled.indexOf(index);
        highlight(event.key === "Home" ? enabled[0] : event.key === "End" ? enabled.at(-1) : enabled[(current + (event.key === "ArrowDown" ? 1 : -1) + enabled.length) % enabled.length]);
      } else if (event.key === "Enter" || (!editable && event.key === " ")) {
        event.preventDefault();
        if (choices[index] && !choices[index].disabled) { close(true); commit(choices[index].value); }
      } else if (!editable && event.key.length === 1 && !event.ctrlKey && !event.metaKey) {
        event.preventDefault();
        query = (Date.now() - lastTyped < 700 ? query : "") + event.key.toLocaleLowerCase();
        lastTyped = Date.now();
        const found = choices.findIndex((choice) => !choice.disabled && choice.label.toLocaleLowerCase().startsWith(query));
        if (found >= 0) highlight(found);
      }
    };
  }
  function enhanceSelect(field) {
    const wrap = make("span", "control-wrap select-wrap");
    field.before(wrap);
    wrap.append(field);
    field.classList.add("control-native");
    field.tabIndex = -1;
    field.setAttribute("aria-hidden", "true");
    const trigger = button("field select-trigger");
    trigger.setAttribute("role", "combobox");
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    const text = make("span", "control-value");
    trigger.append(text);
    trigger.insertAdjacentHTML("beforeend", chevron);
    wrap.append(trigger);
    const control = { field, trigger, anchor: trigger, sync() {
      const value = field.selectedOptions[0]?.label || "请选择";
      if (text.textContent !== value) text.textContent = value;
      if (trigger.disabled !== field.disabled) trigger.disabled = field.disabled;
      trigger.setAttribute("aria-label", labelFor(field));
      trigger.setAttribute("aria-required", String(field.required));
    } };
    const show = () => {
      if (field.disabled) return;
      control.sync();
      listPopup(control, [...field.options].map((option) => ({ value: option.value, label: option.label, disabled: option.disabled || option.parentElement.disabled })), field.value, (value) => {
        field.value = value;
        control.sync();
        emit(field);
      });
    };
    trigger.onclick = () => active?.field === field ? close() : show();
    trigger.onkeydown = (event) => {
      if (active?.field !== field && ["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) { event.preventDefault(); show(); }
    };
    field.addEventListener("focus", () => trigger.focus());
    field.addEventListener("click", (event) => { event.preventDefault(); trigger.focus(); show(); });
    field.addEventListener("change", control.sync);
    field.addEventListener("invalid", (event) => { event.preventDefault(); trigger.focus(); trigger.setAttribute("aria-invalid", "true"); });
    field.addEventListener("change", () => trigger.removeAttribute("aria-invalid"));
    controls.set(field, control);
    control.sync();
  }
  const dateValue = (date) => `${String(date.getFullYear()).padStart(4, "0")}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  const parseDate = (value) => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
    const date = new Date(`${value}T12:00:00`);
    return Number.isNaN(date.getTime()) || dateValue(date) !== value ? null : date;
  };
  function enhanceDate(field) {
    // Use an ISO text field so OS locale does not introduce mm/dd/yyyy or a native calendar.
    field.type = "text";
    field.classList.add("date-input");
    field.placeholder = "年-月-日";
    field.autocomplete = "off";
    field.title = "日期格式：YYYY-MM-DD";
    const wrap = make("span", "control-wrap date-wrap");
    field.before(wrap);
    wrap.append(field);
    const trigger = button("date-trigger", undefined, `选择${labelFor(field)}`);
    trigger.innerHTML = calendarIcon;
    trigger.setAttribute("aria-haspopup", "dialog");
    trigger.setAttribute("aria-expanded", "false");
    wrap.append(trigger);
    const control = { field, trigger, anchor: wrap, sync() {
      if (trigger.disabled !== (field.disabled || field.readOnly)) trigger.disabled = field.disabled || field.readOnly;
      const value = field.value;
      const message = value && !parseDate(value) ? "请输入有效日期，格式为 YYYY-MM-DD"
        : value && field.min && value < field.min ? `日期不能早于 ${field.min}`
        : value && field.max && value > field.max ? `日期不能晚于 ${field.max}` : "";
      field.setCustomValidity(message);
    } };
    controls.set(field, control);
    control.sync();
    trigger.onclick = () => {
      if (active?.field === field) return close();
      const popup = make("div", "control-popup date-popup");
      popup.setAttribute("role", "dialog");
      popup.setAttribute("aria-label", `选择${labelFor(field)}`);
      let focused = parseDate(field.value) || new Date();
      if (field.min && dateValue(focused) < field.min) focused = parseDate(field.min) || focused;
      if (field.max && dateValue(focused) > field.max) focused = parseDate(field.max) || focused;
      let month = new Date(focused.getFullYear(), focused.getMonth(), 1, 12);
      const allowed = (value) => (!field.min || value >= field.min) && (!field.max || value <= field.max);
      const commit = (value) => { field.value = value; control.sync(); close(true); emit(field); };
      const render = (focusDay = false) => {
        popup.replaceChildren();
        const header = make("div", "calendar-header");
        const move = (amount) => {
          const next = new Date(month.getFullYear(), month.getMonth() + amount, 1, 12);
          if (next.getFullYear() < 1 || next.getFullYear() > 9999) return;
          month = next;
          render();
          popup.querySelector(`[data-month-step="${amount}"]`).focus();
        };
        const nav = (amount, text, label) => {
          const el = button("calendar-nav", text, label);
          el.dataset.monthStep = amount;
          el.onclick = () => move(amount);
          return el;
        };
        const title = make("strong", "calendar-title", `${month.getFullYear()} 年 ${month.getMonth() + 1} 月`);
        title.setAttribute("aria-live", "polite");
        header.append(nav(-12, "«", "上一年"), nav(-1, "‹", "上个月"), title, nav(1, "›", "下个月"), nav(12, "»", "下一年"));
        popup.append(header);
        const weekdays = make("div", "calendar-weekdays");
        ["一", "二", "三", "四", "五", "六", "日"].forEach((day) => weekdays.append(make("span", "", day)));
        popup.append(weekdays);
        const grid = make("div", "calendar-grid");
        grid.setAttribute("role", "group");
        grid.setAttribute("aria-label", "日期，使用方向键移动，回车选择");
        const start = new Date(month);
        start.setDate(1 - (month.getDay() + 6) % 7);
        const today = dateValue(new Date());
        if (focused.getFullYear() !== month.getFullYear() || focused.getMonth() !== month.getMonth()) {
          focused = new Date(month);
          if (field.min && dateValue(focused) < field.min) focused = parseDate(field.min) || focused;
        }
        for (let i = 0; i < 42; i++) {
          const day = new Date(start); day.setDate(start.getDate() + i);
          const value = dateValue(day);
          const el = button("calendar-day", day.getDate(), `${day.getFullYear()}年${day.getMonth() + 1}月${day.getDate()}日`);
          el.dataset.date = value;
          el.classList.toggle("outside-month", day.getMonth() !== month.getMonth());
          el.classList.toggle("is-selected", value === field.value);
          el.classList.toggle("is-today", value === today);
          el.setAttribute("aria-pressed", String(value === field.value));
          if (value === today) el.setAttribute("aria-current", "date");
          el.disabled = !allowed(value) || day.getFullYear() < 1 || day.getFullYear() > 9999;
          el.tabIndex = value === dateValue(focused) ? 0 : -1;
          el.onclick = () => commit(value);
          el.onkeydown = (event) => {
            if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].includes(event.key)) return;
            event.preventDefault();
            focused = new Date(day);
            if (event.key.startsWith("Page")) {
              const target = new Date(day.getFullYear(), day.getMonth() + (event.key === "PageDown" ? 1 : -1), 1, 12);
              focused = new Date(target.getFullYear(), target.getMonth(), Math.min(day.getDate(), new Date(target.getFullYear(), target.getMonth() + 1, 0).getDate()), 12);
            } else focused.setDate(day.getDate() + ({ ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7, Home: -(day.getDay() + 6) % 7, End: 6 - (day.getDay() + 6) % 7 })[event.key]);
            if (focused.getFullYear() < 1 || focused.getFullYear() > 9999 || !allowed(dateValue(focused))) return;
            month = new Date(focused.getFullYear(), focused.getMonth(), 1, 12);
            render(true);
          };
          grid.append(el);
        }
        popup.append(grid);
        const footer = make("div", "calendar-footer");
        const clear = button("calendar-action", "清除");
        clear.disabled = field.required || !field.value;
        clear.onclick = () => commit("");
        const todayButton = button("calendar-action", "今天");
        todayButton.disabled = !allowed(today);
        todayButton.onclick = () => commit(today);
        footer.append(clear, todayButton);
        popup.append(footer);
        position();
        if (focusDay) {
          const target = popup.querySelector(`[data-date="${dateValue(focused)}"]:not(:disabled)`) || popup.querySelector(".calendar-day:not(:disabled)");
          target?.focus();
        }
      };
      open(control, popup);
      render(true);
    };
  }
  function enhanceSuggestions(field) {
    const listId = field.getAttribute("list");
    const list = document.getElementById(listId);
    if (!list) return;
    field.dataset.suggestionList = listId;
    field.removeAttribute("list");
    field.setAttribute("role", "combobox");
    field.setAttribute("aria-autocomplete", "list");
    field.setAttribute("aria-haspopup", "listbox");
    field.setAttribute("aria-expanded", "false");
    const control = { field, trigger: field, anchor: field, sync() {} };
    controls.set(field, control);
    const show = () => {
      if (field.disabled || field.readOnly) return;
      const choices = [...list.options].filter((option) => option.value.toLowerCase().includes(field.value.toLowerCase())).map((option) => ({ value: option.value, label: option.label || option.value }));
      listPopup(control, choices, field.value, (value) => { field.value = value; emit(field); }, true);
    };
    field.addEventListener("click", show);
    field.addEventListener("input", (event) => { if (event.isTrusted) show(); });
    field.addEventListener("keydown", (event) => {
      if (active?.field !== field && event.key === "ArrowDown") { event.preventDefault(); show(); }
    });
  }
  function scan() {
    for (const [field, control] of controls) {
      if (!field.isConnected) { if (active?.field === field) close(); controls.delete(field); }
      else control.sync();
    }
    document.querySelectorAll('select.field:not([multiple]), input.field[type="date"], input.field[list]').forEach((field) => {
      if (controls.has(field)) return;
      if (field.tagName === "SELECT") enhanceSelect(field);
      else if (field.type === "date") enhanceDate(field);
      else enhanceSuggestions(field);
    });
    if (active && (active.field.disabled || !active.anchor.getClientRects().length)) close();
  }
  // The app replaces forms and option lists during navigation and dependent-field updates.
  new MutationObserver((records) => {
    if (active && records.some((record) => active.field.contains(record.target))) close();
    scan();
  }).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled", "required", "selected", "value", "min", "max", "hidden", "readonly"] });
  document.addEventListener("pointerdown", (event) => {
    if (active && !active.popup.contains(event.target) && !active.anchor.contains(event.target)) close();
  }, true);
  document.addEventListener("keydown", (event) => {
    if (!active) return;
    if (event.key === "Escape") { event.preventDefault(); event.stopImmediatePropagation(); close(true); }
    else if (event.key === "Tab") {
      if (active.popup.classList.contains("date-popup") && active.popup.contains(document.activeElement)) {
        const stops = [...active.popup.querySelectorAll('button:not(:disabled):not([tabindex="-1"])')];
        const next = stops.indexOf(document.activeElement) + (event.shiftKey ? -1 : 1);
        if (stops[next]) { event.preventDefault(); stops[next].focus(); }
        else close(true);
      } else close();
    } else active.keydown?.(event);
  }, true);
  document.addEventListener("focusin", (event) => {
    if (active && !active.popup.contains(event.target) && !active.anchor.contains(event.target)) close();
  });
  document.addEventListener("close", () => close(), true);
  document.addEventListener("input", scan);
  document.addEventListener("change", scan);
  document.addEventListener("reset", () => setTimeout(() => { close(); scan(); }, 0));
  window.addEventListener("resize", position);
  window.addEventListener("scroll", (event) => { if (active && !active.popup.contains(event.target)) position(); }, true);
  window.visualViewport?.addEventListener("resize", position);
  window.visualViewport?.addEventListener("scroll", position);
  scan();

  window.confirmAction = (message) => new Promise((resolve) => {
    close();
    const previous = document.activeElement;
    const dialog = make("dialog", "confirm-dialog");
    const title = make("h2", "", "确认删除");
    title.id = `confirm-title-${++sequence}`;
    const description = make("p", "", message);
    description.id = `confirm-description-${sequence}`;
    dialog.setAttribute("aria-labelledby", title.id);
    dialog.setAttribute("aria-describedby", description.id);
    const actions = make("div", "confirm-actions");
    const cancel = button("button", "取消");
    const confirm = button("button danger", "确认删除");
    cancel.onclick = () => dialog.close();
    confirm.onclick = () => dialog.close("confirm");
    actions.append(cancel, confirm);
    dialog.append(title, description, actions);
    document.body.append(dialog);
    dialog.addEventListener("close", () => {
      const accepted = dialog.returnValue === "confirm";
      dialog.remove();
      if (previous?.isConnected) previous.focus();
      resolve(accepted);
    }, { once: true });
    dialog.showModal();
    cancel.focus();
  });
})();

/* 共用展示层：原生字段继续负责表单值、约束和业务事件。 */
window.NavrenFormControls = (() => {
  'use strict';
  if (window.NavrenFormControls) return window.NavrenFormControls;
  const records = new Map(), supported = new Set(['date', 'datetime-local', 'time']);
  let active = null, serial = 0, queued = false;
  const pad = value => String(value).padStart(2, '0');
  const english = () => window.NavrenI18n?.language === 'en' || /^en\b/.test(document.documentElement.lang);
  const copy = (zh, en) => english() ? en : zh;
  const node = (tag, className, text) => { const value = document.createElement(tag); if (className) value.className = className; if (text != null) value.textContent = text; return value; };
  function icon(kind) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true');
    svg.innerHTML = ({caret:'<path d="m7 10 5 5 5-5"/>',check:'<path d="m5 12 4 4L19 6"/>',calendar:'<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M8 3v4m8-4v4M3 11h18"/>',clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',left:'<path d="m14 6-6 6 6 6"/>'})[kind];
    return svg;
  }
  function button(className, text, action) { const result = node('button', className, text); result.type = 'button'; if (action) result.addEventListener('click', action); return result; }
  function attr(target, name, value) { if (value == null) { if (target.hasAttribute(name)) target.removeAttribute(name); } else if (target.getAttribute(name) !== String(value)) target.setAttribute(name, String(value)); }
  function fieldName(source) {
    const direct = source.getAttribute('aria-label'); if (direct) return direct;
    const reference = source.getAttribute('aria-labelledby');
    if (reference) { const text = reference.split(/\s+/).map(id => document.getElementById(id)?.textContent || '').join(' ').trim(); if (text) return text; }
    const labels = [...(source.labels || [])].map(label => { const clone = label.cloneNode(true); clone.querySelectorAll('.nv-control,.nv-control-popup,select,input,textarea,button').forEach(child => child.remove()); return clone.textContent.trim(); }).join(' ').trim();
    return labels || source.title || copy(source.tagName === 'SELECT' ? '选择选项' : '选择日期或时间', source.tagName === 'SELECT' ? 'Choose option' : 'Choose date or time');
  }
  const disabled = source => source.disabled || source.readOnly || source.matches(':disabled');
  const visible = record => record.source.isConnected && record.trigger.isConnected && !record.source.hidden && !record.source.parentElement?.closest('[hidden],[inert],[aria-hidden="true"]') && !record.trigger.closest('[hidden],[inert],[aria-hidden="true"]') && (typeof record.trigger.checkVisibility !== 'function' || record.trigger.checkVisibility({checkVisibilityCSS: true})) && !document.documentElement.classList.contains('navren-private-locked');
  function eligible(source) {
    if (!source?.isConnected || !source.parentElement || source.hidden || source.closest('.nv-control-popup,[data-navren-control="native"]')) return false;
    if (source.tagName === 'SELECT') return !source.multiple && source.size <= 1;
    return source.tagName === 'INPUT' && supported.has(source.type);
  }
  function dateParts(value) {
    const match = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?)?$/.exec(value || '');
    if (!match) return null;
    const [year, month, day, hour, minute, second] = match.slice(1, 7).map(value => value == null ? 0 : Number(value));
    if (year < 1 || month < 1 || month > 12 || hour > 23 || minute > 59 || second > 59) return null;
    const date = new Date(0); date.setUTCHours(hour, minute, second, Number((match[7] || '').padEnd(3, '0'))); date.setUTCFullYear(year, month - 1, day);
    if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return null;
    return { year, month, day, hour, minute, second, ordinal: date.getTime() };
  }
  function timeParts(value) {
    const match = /^(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?$/.exec(value || ''); if (!match) return null;
    const [hour, minute, second] = match.slice(1, 4).map(value => value == null ? 0 : Number(value)); if (hour > 23 || minute > 59 || second > 59) return null;
    return { hour, minute, second, ordinal: ((hour * 60 + minute) * 60 + second) * 1000 + Number((match[4] || '').padEnd(3, '0')) };
  }
  const dateValue = parts => String(parts.year).padStart(4, '0') + '-' + pad(parts.month) + '-' + pad(parts.day);
  const timeValue = (parts, seconds = false) => pad(parts.hour) + ':' + pad(parts.minute) + (seconds ? ':' + pad(parts.second) : '');
  function todayParts() { const date = new Date(); return {year: date.getFullYear(), month: date.getMonth() + 1, day: date.getDate(), hour: date.getHours(), minute: date.getMinutes(), second: 0}; }
  const parse = source => source.type === 'time' ? timeParts : dateParts;
  function range(source) {
    const reader = parse(source), min = reader(source.min), max = reader(source.max), step = source.step === 'any' ? 0 : Number(source.step) > 0 ? Number(source.step) : source.type === 'date' ? 1 : 60;
    const factor = source.type === 'date' ? 86400000 : 1000;
    const base = min?.ordinal ?? reader(source.getAttribute('value'))?.ordinal ?? 0;
    return {min, max, step: step * factor, base};
  }
  function allowed(source, value) {
    if (!value) return !source.required;
    const parsed = parse(source)(value); if (!parsed) return false;
    const bounds = range(source), ordinal = parsed.ordinal;
    const within = source.type === 'time' && bounds.min && bounds.max && bounds.min.ordinal > bounds.max.ordinal ? ordinal >= bounds.min.ordinal || ordinal <= bounds.max.ordinal : (!bounds.min || ordinal >= bounds.min.ordinal) && (!bounds.max || ordinal <= bounds.max.ordinal);
    return within && (!bounds.step || Math.abs((ordinal - bounds.base) / bounds.step - Math.round((ordinal - bounds.base) / bounds.step)) < 1e-8);
  }
  function dayAllowed(source, value) {
    if (source.type === 'date') return allowed(source, value);
    const day = dateParts(value); if (!day) return false;
    const bounds = range(source), first = Math.max(day.ordinal, bounds.min?.ordinal ?? day.ordinal), last = Math.min(day.ordinal + 86400000 - 1, bounds.max?.ordinal ?? day.ordinal + 86400000 - 1);
    return first <= last && (!bounds.step || bounds.base + Math.ceil((first - bounds.base) / bounds.step) * bounds.step <= last);
  }
  function display(source) {
    if (source.tagName === 'SELECT') return source.options[source.selectedIndex]?.label || copy('请选择', 'Choose');
    if (!source.value) return source.getAttribute('placeholder') || copy(source.type === 'time' ? '选择时间' : source.type === 'date' ? '选择日期' : '选择日期和时间', source.type === 'time' ? 'Choose time' : source.type === 'date' ? 'Choose date' : 'Choose date and time');
    if (source.type === 'time') return source.value;
    return source.value.replaceAll('-', '/').replace('T', ' ');
  }
  function close(restoreFocus = false) {
    const previous = active; if (!previous) return;
    active = null; previous.record.trigger.setAttribute('aria-expanded', 'false');
    if (typeof previous.popup.hidePopover === 'function') { try { previous.popup.hidePopover(); } catch (_) {} }
    previous.popup.remove();
    if (restoreFocus && visible(previous.record) && !disabled(previous.record.source)) previous.record.trigger.focus({preventScroll: true});
  }
  function position() {
    if (!active) return;
    const {record, popup} = active;
    if (!visible(record) || disabled(record.source)) { close(); return; }
    const rect = record.trigger.getBoundingClientRect(), viewport = window.visualViewport;
    const left = viewport?.offsetLeft || 0, top = viewport?.offsetTop || 0, width = viewport?.width || window.innerWidth, height = viewport?.height || window.innerHeight, gutter = 12, gap = 6;
    const calendar = record.source.tagName !== 'SELECT', viewportHeight = Math.max(0, height - gutter * 2);
    if (!calendar) popup.style.width = Math.min(Math.max(rect.width, 180), Math.max(0, width - gutter * 2)) + 'px';
    popup.style.maxHeight = calendar ? 'none' : viewportHeight + 'px';
    const actualWidth = popup.offsetWidth || (record.source.tagName === 'SELECT' ? Math.max(rect.width, 180) : 320), actualHeight = popup.offsetHeight || 300;
    const above = rect.top - top - gutter - gap, below = top + height - gutter - rect.bottom - gap, useAbove = below < actualHeight && above > below;
    const shiftIntoViewport = calendar && above < actualHeight && below < actualHeight && actualHeight <= viewportHeight;
    const availableHeight = shiftIntoViewport ? viewportHeight : Math.max(0, useAbove ? above : below), fittedHeight = Math.min(actualHeight, availableHeight);
    popup.style.maxHeight = availableHeight + 'px';
    popup.style.left = Math.max(left + gutter, Math.min(rect.left, left + width - actualWidth - gutter)) + 'px';
    const preferredTop = useAbove ? rect.top - fittedHeight - gap : rect.bottom + gap;
    popup.style.top = (shiftIntoViewport ? Math.max(top + gutter, Math.min(preferredTop, top + height - fittedHeight - gutter)) : Math.max(top + gutter, preferredTop)) + 'px';
    popup.dataset.placement = useAbove ? 'above' : 'below';
  }
  function focusOption(index) {
    if (!active || active.kind !== 'select') return;
    const choices = [...active.popup.querySelectorAll('.nv-control-option')], available = choices.filter(choice => !choice.disabled);
    const choice = choices.find(item => Number(item.dataset.index) === index && !item.disabled) || available[0]; if (!choice) return;
    active.index = Number(choice.dataset.index); choices.forEach(item => { item.classList.toggle('is-active', item === choice); item.tabIndex = item === choice ? 0 : -1; });
    choice.focus({preventScroll: true}); choice.scrollIntoView?.({block: 'nearest'});
  }
  function commit(record, value, selectedIndex) {
    if (active?.record !== record || !visible(record) || disabled(record.source)) return;
    const source = record.source;
    if (source.tagName === 'SELECT') { const option = source.options[selectedIndex]; if (!option || option.disabled || option.parentElement?.disabled) return; }
    else if (value && !allowed(source, value)) return;
    const before = source.value, beforeIndex = source.selectedIndex;
    close(true);
    if (source.tagName === 'SELECT') source.selectedIndex = selectedIndex; else source.value = value;
    refresh(record);
    if (source.value === before && source.selectedIndex === beforeIndex) return;
    source.dispatchEvent(new Event('input', {bubbles: true})); source.dispatchEvent(new Event('change', {bubbles: true}));
  }
  function renderSelect(state) {
    const {record, popup} = state, source = record.source; popup.replaceChildren();
    const turn = state.turn = (state.turn || 0) + 1;
    let group = null;
    [...source.options].forEach((option, index) => {
      if (option.hidden || option.parentElement?.hidden) return;
      const parent = option.parentElement;
      if (parent.tagName === 'OPTGROUP' && group !== parent) { group = parent; popup.append(node('div', 'nv-control-group', parent.label)); }
      if (parent.tagName !== 'OPTGROUP') group = null;
      const choice = button('nv-control-option', null, () => { if (active === state && state.turn === turn && source.options[index] === option) commit(record, option.value, index); });
      choice.dataset.index = index; choice.id = popup.id + 'Option' + index; choice.setAttribute('role', 'option'); choice.setAttribute('aria-selected', String(index === source.selectedIndex)); choice.tabIndex = -1; choice.disabled = option.disabled || !!parent.disabled;
      choice.append(node('span', 'nv-control-option-label', option.label), icon('check')); popup.append(choice);
    });
    if (!popup.children.length) popup.append(node('p', 'nv-control-empty', copy('暂无选项', 'No options')));
    position();
  }
  function moveSelect(event) {
    const state = active; if (!state || state.kind !== 'select') return;
    const items = [...state.popup.querySelectorAll('.nv-control-option:not(:disabled)')], current = items.findIndex(item => Number(item.dataset.index) === state.index);
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault(); const target = event.key === 'Home' ? items[0] : event.key === 'End' ? items.at(-1) : items[Math.max(0, Math.min(items.length - 1, current + (event.key === 'ArrowDown' ? 1 : -1)))]; focusOption(Number(target?.dataset.index));
    } else if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); const item = items.find(choice => Number(choice.dataset.index) === state.index); if (item) commit(state.record, state.record.source.options[state.index].value, state.index); }
    else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault(); const now = Date.now(); state.search = now - (state.searchAt || 0) < 650 ? (state.search || '') + event.key.toLocaleLowerCase() : event.key.toLocaleLowerCase(); state.searchAt = now;
      const repeated = [...state.search].every(character => character === state.search[0]); const term = repeated ? state.search[0] : state.search;
      const ordered = items.slice(current + 1).concat(items.slice(0, current + 1)); const found = ordered.find(item => item.textContent.trim().toLocaleLowerCase().startsWith(term)); if (found) focusOption(Number(found.dataset.index));
    }
  }
  function needsSeconds(source) { return Number(source.step) > 0 && Number(source.step) < 60 || /:\d{2}:\d{2}/.test(source.value + source.min + source.max); }
  function draftValue(state) { const source = state.record.source; return source.type === 'time' ? timeValue(state.draft, needsSeconds(source)) : dateValue(state.draft) + (source.type === 'datetime-local' ? 'T' + timeValue(state.draft, needsSeconds(source)) : ''); }
  function nearestDraft(source) {
    const selected = parse(source)(source.value); if (selected) return {...selected};
    const today = todayParts(), current = source.type === 'time' ? timeValue(today, needsSeconds(source)) : dateValue(today) + (source.type === 'datetime-local' ? 'T' + timeValue(today, needsSeconds(source)) : '');
    const bounds = range(source), parsed = parse(source)(current); let ordinal = parsed.ordinal;
    if (bounds.min && ordinal < bounds.min.ordinal) ordinal = bounds.min.ordinal;
    if (bounds.max && ordinal > bounds.max.ordinal) ordinal = bounds.max.ordinal;
    if (bounds.step) ordinal = bounds.base + Math.ceil((ordinal - bounds.base) / bounds.step) * bounds.step;
    const result = new Date(ordinal);
    return {year: result.getUTCFullYear(), month: result.getUTCMonth() + 1, day: result.getUTCDate(), hour: result.getUTCHours(), minute: result.getUTCMinutes(), second: result.getUTCSeconds()};
  }
  function monthPermitted(source, year, month) {
    if (year < 1 || year > 9999) return false;
    const start = dateParts(String(year).padStart(4, '0') + '-' + pad(month) + '-01'), end = new Date(start.ordinal); end.setUTCMonth(end.getUTCMonth() + 1); const bounds = range(source);
    return (!bounds.min || end.getTime() - 1 >= bounds.min.ordinal) && (!bounds.max || start.ordinal <= bounds.max.ordinal);
  }
  function shiftMonth(state, delta, focus) {
    const ordinal = state.year * 12 + state.month - 1 + delta, year = Math.floor(ordinal / 12), month = ordinal % 12 + 1;
    if (!monthPermitted(state.record.source, year, month)) return;
    state.year = year; state.month = month; renderDate(state, focus);
  }
  function focusDay(state, value) {
    const buttons = [...state.popup.querySelectorAll('.nv-control-day:not(:disabled)')]; const target = buttons.find(item => item.dataset.date === value) || buttons.find(item => item.getAttribute('aria-selected') === 'true') || buttons.find(item => !item.classList.contains('is-outside')) || buttons[0];
    state.popup.querySelectorAll('.nv-control-day').forEach(item => { item.tabIndex = item === target ? 0 : -1; }); target?.focus({preventScroll: true});
  }
  function dateKey(event, state) {
    const cell = event.target.closest('.nv-control-day'); if (!cell) return;
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); cell.click(); return; }
    const parsed = dateParts(cell.dataset.date), date = new Date(parsed.ordinal), shifts = {ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7};
    if (event.key in shifts) date.setUTCDate(date.getUTCDate() + shifts[event.key]);
    else if (event.key === 'Home') date.setUTCDate(date.getUTCDate() - date.getUTCDay());
    else if (event.key === 'End') date.setUTCDate(date.getUTCDate() + 6 - date.getUTCDay());
    else if (event.key === 'PageUp' || event.key === 'PageDown') { const delta = (event.key === 'PageUp' ? -1 : 1) * (event.shiftKey ? 12 : 1); const original = date.getUTCDate(); date.setUTCDate(1); date.setUTCMonth(date.getUTCMonth() + delta); const last = new Date(date); last.setUTCMonth(last.getUTCMonth() + 1); last.setUTCDate(0); date.setUTCDate(Math.min(original, last.getUTCDate())); }
    else return;
    event.preventDefault(); const value = dateValue({year: date.getUTCFullYear(), month: date.getUTCMonth() + 1, day: date.getUTCDate()});
    if (!dayAllowed(state.record.source, value)) return;
    state.year = date.getUTCFullYear(); state.month = date.getUTCMonth() + 1; renderDate(state, value);
  }
  function renderDate(state, focus) {
    if (active !== state) return;
    const {record, popup} = state, source = record.source; popup.replaceChildren();
    const turn = state.turn = (state.turn || 0) + 1;
    const live = () => active === state && state.turn === turn && visible(record) && !disabled(source);
    if (source.type !== 'time') {
      const header = node('div', 'nv-control-calendar-header');
      const previous = button('nv-control-nav', null, () => { if (live()) { shiftMonth(state, state.mode === 'months' ? -12 : -1); state.popup.querySelector('.nv-control-nav')?.focus(); } }); previous.append(icon('left')); previous.setAttribute('aria-label', copy(state.mode === 'months' ? '上一年' : '上一月', state.mode === 'months' ? 'Previous year' : 'Previous month'));
      const next = button('nv-control-nav is-next', null, () => { if (live()) { shiftMonth(state, state.mode === 'months' ? 12 : 1); state.popup.querySelector('.nv-control-nav.is-next')?.focus(); } }); next.append(icon('left')); next.setAttribute('aria-label', copy(state.mode === 'months' ? '下一年' : '下一月', state.mode === 'months' ? 'Next year' : 'Next month'));
      const monthLabel = english() ? new Intl.DateTimeFormat('en', {month: 'long', timeZone: 'UTC'}).format(new Date(Date.UTC(2024, state.month - 1, 1))) + ' ' + state.year : state.year + '年' + state.month + '月';
      const title = button('nv-control-calendar-title', state.mode === 'months' ? String(state.year) + copy('年', '') : monthLabel, () => { if (!live()) return; state.mode = state.mode === 'months' ? 'days' : 'months'; renderDate(state); (state.popup.querySelector('.nv-control-year') || state.popup.querySelector('.nv-control-day[tabindex="0"]'))?.focus(); }); title.append(icon('caret')); title.setAttribute('aria-label', copy('选择年份和月份', 'Choose year and month'));
      header.append(previous, title, next); popup.append(header);
      const base = dateParts(String(state.year).padStart(4, '0') + '-' + pad(state.month) + '-01'), before = new Date(base.ordinal), after = new Date(base.ordinal); before.setUTCMonth(before.getUTCMonth() - 1); after.setUTCMonth(after.getUTCMonth() + 1);
      previous.disabled = !monthPermitted(source, state.mode === 'months' ? state.year - 1 : before.getUTCFullYear(), state.mode === 'months' ? state.month : before.getUTCMonth() + 1); next.disabled = !monthPermitted(source, state.mode === 'months' ? state.year + 1 : after.getUTCFullYear(), state.mode === 'months' ? state.month : after.getUTCMonth() + 1);
      if (state.mode === 'months') {
        const yearLabel = node('label', 'nv-control-year-field', copy('年份', 'Year')), year = node('input', 'nv-control-year'); year.type = 'number'; year.min = '1'; year.max = '9999'; year.value = state.year; year.setAttribute('aria-label', copy('年份', 'Year'));
        const changeYear = () => { const value = Number(year.value); if (!Number.isInteger(value) || value < 1 || value > 9999 || !live()) return; state.year = value; renderDate(state); const replacement = state.popup.querySelector('.nv-control-year'); replacement?.focus(); };
        year.addEventListener('change', changeYear); year.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); changeYear(); } }); yearLabel.append(year); popup.append(yearLabel);
        const months = node('div', 'nv-control-month-grid');
        for (let month = 1; month <= 12; month++) { const choice = button('nv-control-month', english() ? new Intl.DateTimeFormat('en', {month: 'short', timeZone: 'UTC'}).format(new Date(Date.UTC(2024, month - 1, 1))) : month + '月', () => { if (!live()) return; state.month = month; state.mode = 'days'; renderDate(state); focusDay(state); }); choice.disabled = !monthPermitted(source, state.year, month); choice.setAttribute('aria-pressed', String(month === state.month)); months.append(choice); } popup.append(months);
      } else {
        const week = node('div', 'nv-control-weekdays'); (english() ? ['Su','Mo','Tu','We','Th','Fr','Sa'] : ['日','一','二','三','四','五','六']).forEach(day => week.append(node('span', null, day))); week.setAttribute('aria-hidden', 'true'); popup.append(week);
        const grid = node('div', 'nv-control-days'); grid.setAttribute('role', 'grid'); grid.setAttribute('aria-label', monthLabel); const first = new Date(base.ordinal); first.setUTCDate(first.getUTCDate() - first.getUTCDay()); const today = dateValue(todayParts()); let row;
        for (let index = 0; index < 42; index++) {
          if (index % 7 === 0) { row = node('div', 'nv-control-day-row'); row.setAttribute('role', 'row'); grid.append(row); }
          const date = new Date(first.getTime() + index * 86400000), parts = {year: date.getUTCFullYear(), month: date.getUTCMonth() + 1, day: date.getUTCDate()}, value = dateValue(parts);
          const day = button('nv-control-day', parts.day, () => {
            if (!live() || !dayAllowed(source, value)) return;
            Object.assign(state.draft, parts); state.year = parts.year; state.month = parts.month;
            if (source.type === 'date') commit(record, value); else { state.mode = 'days'; renderDate(state, value); }
          }); day.dataset.date = value; day.tabIndex = -1; day.setAttribute('role', 'gridcell'); day.setAttribute('aria-label', value.replaceAll('-', '/')); day.setAttribute('aria-selected', String(value === dateValue(state.draft))); day.disabled = !dayAllowed(source, value);
          if (parts.month !== state.month) day.classList.add('is-outside'); if (value === today) { day.classList.add('is-today'); day.setAttribute('aria-current', 'date'); } row.append(day);
        } popup.append(grid);
        const chosen = [...grid.querySelectorAll('button:not(:disabled)')].find(item => item.getAttribute('aria-selected') === 'true') || grid.querySelector('button:not(:disabled):not(.is-outside)'); if (chosen) chosen.tabIndex = 0;
      }
    } else popup.append(node('div', 'nv-control-time-title', copy('选择时间', 'Choose time')));
    let apply;
    if (source.type !== 'date') {
      const time = node('div', 'nv-control-time-fields');
      for (const [part, label, max] of [['hour', copy('时', 'Hour'), 23], ['minute', copy('分', 'Minute'), 59], ...(needsSeconds(source) ? [['second', copy('秒', 'Second'), 59]] : [])]) {
        const field = node('label', 'nv-control-time-field'), value = node('input', 'nv-control-time-input'); field.append(node('span', null, label)); value.type = 'number'; value.min = '0'; value.max = String(max); value.step = '1'; value.value = pad(state.draft[part]); value.inputMode = 'numeric'; value.setAttribute('aria-label', label);
        const update = () => { if (!live()) return; const parsed = Number(value.value); if (value.value !== '' && Number.isInteger(parsed) && parsed >= 0 && parsed <= max) state.draft[part] = parsed; state.timeValid = [...time.querySelectorAll('input')].every(input => input.value !== '' && input.checkValidity()); if (apply) apply.disabled = !state.timeValid || !allowed(source, draftValue(state)); };
        value.addEventListener('input', update); value.addEventListener('change', update); value.addEventListener('blur', () => { if (value.value !== '' && value.checkValidity()) value.value = pad(Number(value.value)); }); value.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); update(); if (!apply.disabled) apply.click(); } }); field.append(value); time.append(field);
      } popup.append(time); state.timeValid = true;
    }
    const footer = node('div', 'nv-control-calendar-footer'), clear = button('nv-control-clear', copy('清除', 'Clear'), () => { if (live()) commit(record, ''); });
    const current = button('nv-control-today', copy(source.type === 'time' ? '现在' : '今天', source.type === 'time' ? 'Now' : 'Today'), () => { if (!live()) return; const parts = todayParts(); if (source.type === 'date') commit(record, dateValue(parts)); else { state.draft = {...parts}; state.year = parts.year; state.month = parts.month; state.mode = 'days'; renderDate(state); } });
    current.disabled = source.type === 'date' ? !allowed(source, dateValue(todayParts())) : source.type === 'datetime-local' ? !dayAllowed(source, dateValue(todayParts())) : false;
    footer.append(clear, current);
    if (source.type !== 'date') { apply = button('nv-control-apply', copy('确定', 'Apply'), () => { if (live() && state.timeValid && allowed(source, draftValue(state))) commit(record, draftValue(state)); }); apply.disabled = !allowed(source, draftValue(state)); footer.append(apply); }
    popup.append(footer); position(); if (focus) focusDay(state, focus);
  }
  function open(record, direction) {
    if (!record || !visible(record) || disabled(record.source)) return;
    close(); refresh(record);
    const popup = node('div', 'nv-control-popup ' + (record.source.tagName === 'SELECT' ? 'nv-control-listbox' : 'nv-control-calendar'));
    popup.id = record.popupId; popup.setAttribute('role', record.source.tagName === 'SELECT' ? 'listbox' : 'dialog'); popup.setAttribute('aria-label', fieldName(record.source)); popup.setAttribute('popover', 'manual'); popup.dataset.i18nSkip = 'true';
    const source = record.source, draft = source.tagName === 'SELECT' ? null : nearestDraft(source);
    const state = active = {record, popup, kind: source.tagName === 'SELECT' ? 'select' : 'date', index: source.selectedIndex, draft, year: draft?.year, month: draft?.month, mode: 'days'};
    (source.closest('dialog[open]') || document.body).append(popup); if (typeof popup.showPopover === 'function') { try { popup.showPopover(); } catch (_) {} }
    record.trigger.setAttribute('aria-expanded', 'true');
    popup.addEventListener('keydown', event => {
      if (active !== state) return;
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(true); }
      else if (event.key === 'Tab') { const items = [...popup.querySelectorAll('button:not(:disabled),input:not(:disabled)')].filter(item => item.tabIndex >= 0); const index = items.indexOf(document.activeElement); if (event.shiftKey && index <= 0 || !event.shiftKey && index === items.length - 1) close(true); }
      else if (state.kind === 'select') moveSelect(event); else dateKey(event, state);
    });
    if (state.kind === 'select') { renderSelect(state); const choices = [...popup.querySelectorAll('.nv-control-option:not(:disabled)')]; focusOption(direction === 'last' ? Number(choices.at(-1)?.dataset.index) : direction === 'first' ? Number(choices[0]?.dataset.index) : source.selectedIndex); }
    else { renderDate(state); if (source.type === 'time') popup.querySelector('input')?.focus({preventScroll: true}); else focusDay(state, dateValue(draft)); }
  }
  function hook(record, target, property) {
    if (Object.hasOwn(target, property)) return;
    let prototype = Object.getPrototypeOf(target), descriptor;
    while (prototype && !(descriptor = Object.getOwnPropertyDescriptor(prototype, property))) prototype = Object.getPrototypeOf(prototype);
    if (!descriptor?.get || !descriptor?.set) return;
    // select 是带索引属性的 WebIDL 对象；实例上的 accessor 会阻断部分 DOM 的赋值。
    // 私有原型只代理此控件，原生 getter/setter 仍使用原字段作为 receiver。
    let hookTarget = target;
    if (target.tagName === 'SELECT') {
      if (!record.selectPrototype) {
        const original = Object.getPrototypeOf(target), layer = Object.create(original);
        Object.setPrototypeOf(target, layer); record.selectPrototype = {target, original, layer};
      }
      hookTarget = record.selectPrototype.layer;
    }
    const own = {configurable: true, enumerable: descriptor.enumerable, get() { return descriptor.get.call(this); }, set(value) { descriptor.set.call(this, value); if (records.get(record.source) === record) refresh(record); }};
    Object.defineProperty(hookTarget, property, own); record.hooks.push({target: hookTarget, property, own});
  }
  function refresh(record) {
    if (!record || records.get(record.source) !== record) return;
    const {source, trigger, label, error} = record;
    if (!eligible(source)) { destroy(source); return; }
    const text = display(source); if (label.textContent !== text) label.textContent = text;
    trigger.disabled = disabled(source); attr(trigger, 'aria-label', fieldName(source) + '：' + text); attr(trigger, 'aria-required', source.required ? 'true' : null);
    trigger.classList.toggle('is-placeholder', !source.value); record.field.style.display = source.style.display === 'none' ? 'none' : '';
    if (source.validity.valid) { error.hidden = true; error.textContent = ''; attr(trigger, 'aria-invalid', null); }
    attr(trigger, 'aria-describedby', [source.getAttribute('aria-describedby'), error.hidden ? null : error.id].filter(Boolean).join(' ') || null);
    if (source.tagName === 'SELECT') [...source.options].forEach(option => { if (!record.optionHooks.has(option)) { hook(record, option, 'selected'); record.optionHooks.add(option); } });
    if (active?.record === record) {
      if (!visible(record) || disabled(source)) { close(); return; }
      const focused = document.activeElement, date = focused?.dataset.date, index = focused?.dataset.index, inside = active.popup.contains(focused);
      if (active.kind === 'select') { renderSelect(active); if (inside) focusOption(index == null ? source.selectedIndex : Number(index)); }
      else { const parts = parse(source)(source.value); if (parts) { active.draft = {...parts}; active.year = parts.year; active.month = parts.month; } renderDate(active, inside ? date : null); }
    }
  }
  function mount(source) {
    if (records.has(source)) return records.get(source).controller;
    if (!eligible(source)) return null;
    const field = node('span', 'nv-control'), label = node('span', 'nv-control-value'), trigger = button('nv-control-trigger'), error = node('span', 'nv-control-error'); error.hidden = true; error.setAttribute('role', 'alert');
    const id = ++serial, popupId = 'navrenControlPopup' + id, tabindex = source.getAttribute('tabindex'), ariaHidden = source.getAttribute('aria-hidden');
    trigger.id = source.id ? source.id + 'Control' : 'navrenControlTrigger' + id; trigger.setAttribute('aria-haspopup', source.tagName === 'SELECT' ? 'listbox' : 'dialog'); trigger.setAttribute('aria-expanded', 'false'); trigger.setAttribute('aria-controls', popupId);
    if (source.tagName !== 'SELECT') trigger.append(icon(source.type === 'time' ? 'clock' : 'calendar')); trigger.append(label, icon('caret')); field.append(trigger, error);
    source.classList.add('nv-control-source'); source.tabIndex = -1; source.setAttribute('aria-hidden', 'true'); source.insertAdjacentElement('afterend', field);
    const record = {source, field, trigger, label, error, popupId, tabindex, ariaHidden, hooks: [], optionHooks: new WeakSet()}; records.set(source, record);
    trigger.addEventListener('click', () => active?.record === record ? close(true) : open(record));
    trigger.addEventListener('keydown', event => {
      if (['ArrowDown','ArrowUp','Home','End'].includes(event.key)) { event.preventDefault(); open(record, event.key === 'Home' ? 'first' : event.key === 'End' ? 'last' : null); }
      else if (source.tagName === 'SELECT' && event.key.length === 1 && event.key !== ' ' && !event.ctrlKey && !event.metaKey && !event.altKey) { open(record); moveSelect(event); }
    });
    const update = () => refresh(record), focus = () => { if (!disabled(source)) trigger.focus({preventScroll: true}); }, invalid = event => { event.preventDefault(); if (!visible(record)) return; attr(trigger, 'aria-invalid', 'true'); error.hidden = false; error.textContent = source.validationMessage; attr(trigger, 'aria-describedby', [source.getAttribute('aria-describedby'), error.id].filter(Boolean).join(' ')); const first = source.form && [...source.form.elements].find(field => field.willValidate && !field.validity.valid); if (!first || first === source) trigger.focus({preventScroll: true}); };
    const activate = event => { event.preventDefault(); if (active?.record !== record) open(record); };
    error.id = popupId + 'Error'; source.addEventListener('input', update); source.addEventListener('change', update); source.addEventListener('focus', focus); source.addEventListener('invalid', invalid); source.addEventListener('click', activate);
    record.removeListeners = () => { source.removeEventListener('input', update); source.removeEventListener('change', update); source.removeEventListener('focus', focus); source.removeEventListener('invalid', invalid); source.removeEventListener('click', activate); };
    hook(record, source, 'value'); if (source.tagName === 'SELECT') hook(record, source, 'selectedIndex'); else { hook(record, source, 'readOnly'); hook(record, source, 'valueAsDate'); hook(record, source, 'valueAsNumber'); }
    record.controller = {source, trigger, open: () => open(record), close: () => { if (active?.record === record) close(true); }, refresh: update, destroy: () => destroy(source)}; refresh(record); return record.controller;
  }
  function destroy(source) {
    const record = records.get(source); if (!record) return;
    if (active?.record === record) close(); records.delete(source); record.removeListeners(); record.field.remove(); source.classList.remove('nv-control-source');
    attr(source, 'tabindex', record.tabindex); attr(source, 'aria-hidden', record.ariaHidden);
    for (const {target, property, own} of record.hooks) { const current = Object.getOwnPropertyDescriptor(target, property); if (current?.set === own.set) delete target[property]; }
    const layer = record.selectPrototype;
    if (layer && Object.getPrototypeOf(layer.target) === layer.layer) Object.setPrototypeOf(layer.target, layer.original);
  }
  function enhance(root = document) {
    if (root.matches?.('select,input')) mount(root);
    root.querySelectorAll?.('select,input[type="date"],input[type="datetime-local"],input[type="time"]').forEach(mount);
    return window.NavrenFormControls;
  }
  function schedule() {
    if (queued) return; queued = true;
    queueMicrotask(() => { queued = false; for (const [source, record] of records) { if (!source.isConnected || !record.field.isConnected) destroy(source); else refresh(record); } enhance(); if (active) position(); });
  }
  const internal = target => target.nodeType === 1 ? target.closest('.nv-control,.nv-control-popup') : target.parentElement?.closest('.nv-control,.nv-control-popup');
  const observer = new MutationObserver(changes => { if (changes.some(change => !internal(change.target))) schedule(); });
  function init() { enhance(); observer.observe(document.documentElement, {subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ['disabled','readonly','multiple','size','min','max','step','required','value','selected','label','hidden','inert','aria-hidden','aria-label','aria-labelledby','aria-describedby','class','style','type','lang','data-theme','data-navren-control']}); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once: true}); else init();
  document.addEventListener('pointerdown', event => { if (active && !active.popup.contains(event.target) && !active.record.trigger.contains(event.target)) close(); });
  document.addEventListener('focusin', event => { if (active && event.target !== document.body && !active.popup.contains(event.target) && !active.record.trigger.contains(event.target) && event.target !== active.record.source) close(); });
  document.addEventListener('keydown', event => { if (active && event.key === 'Escape') { event.preventDefault(); close(true); } });
  document.addEventListener('reset', event => { if (event.target.tagName === 'FORM') { close(); setTimeout(() => { for (const record of records.values()) if (record.source.form === event.target) refresh(record); }, 0); } }, true);
  document.addEventListener('scroll', event => { if (active && !active.popup.contains(event.target)) close(); }, true);
  window.addEventListener('resize', position); window.visualViewport?.addEventListener('resize', position); window.visualViewport?.addEventListener('scroll', () => close());
  for (const event of ['navren:view','navren:privacy-reset','navren:identity-revoked','pagehide']) window.addEventListener(event, () => close());
  window.addEventListener('navren:language', () => { close(); records.forEach(refresh); });
  return {mount, enhance, refresh: source => refresh(records.get(source)), close, destroy};
})();

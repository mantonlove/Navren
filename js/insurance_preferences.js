/* 账号保障关注方向：只读取已核验账号快照，保存成功后确认展示。 */
window.NavrenInsurancePreferences = (() => {
  const topics = ['health', 'property', 'agriculture', 'equipment', 'liability', 'pension', 'accident'];
  const labels = {
    health: ['健康与医疗', 'Health and medical'], property: ['房产保障', 'Property cover'],
    agriculture: ['农业保障', 'Agriculture cover'], equipment: ['设备保障', 'Equipment cover'],
    liability: ['经营责任', 'Business liability'], pension: ['养老规划', 'Pension planning'],
    accident: ['意外保障', 'Accident cover']
  };
  const initialFocusRequested = new URL(location.href).searchParams.get('focustarget') === 'mineInsurancePreferences';
  let record = null, epoch = 0, request = 0, pageActive = true, scheduled = false, initialFocusDone = false, focusWait = null;
  const node = (tag, text, cls) => { const element = document.createElement(tag); if (text != null) element.textContent = text; if (cls) element.className = cls; return element; };
  const text = (zh, en) => window.NavrenI18n?.language === 'en' ? en : zh;
  const identityValid = value => value && typeof value.registered_user_id === 'string' && /^[a-f0-9]{32}$/.test(value.registered_user_id) && typeof value.workspace_id === 'string' && value.workspace_id.length > 0 && typeof value.account_id === 'string' && value.account_id.length > 0 && Number.isSafeInteger(value.privacy_revision) && value.privacy_revision >= 0;
  const same = (a, b) => identityValid(a) && identityValid(b) && a.registered_user_id === b.registered_user_id && a.workspace_id === b.workspace_id && a.account_id === b.account_id && a.privacy_revision === b.privacy_revision;
  const accountReady = () => typeof Storage !== 'undefined' && typeof Storage.accountReady === 'function' && Storage.accountReady();
  const clean = keys => Array.isArray(keys) ? [...new Set(keys.filter(key => topics.includes(key)))].slice(0, 3) : [];
  function read(identity) {
    if (!accountReady() || !same(identity, window.BankClient?.identity?.())) return [];
    return clean(Storage.get('navren_insurancePreferences'));
  }
  function homeVisible(host) {
    if (!pageActive || document.body.dataset.page !== 'mine' || !host?.isConnected || host.closest('[hidden]')) return false;
    const current = window.NavrenViews?.current?.();
    if (current) return current.page === 'mine' && (!current.view || current.view === 'home');
    const url = new URL(location.href); return !(url.hash.slice(1) || url.searchParams.get('view')) || (url.hash.slice(1) || url.searchParams.get('view')) === 'home';
  }
  function active(item, control) {
    return record === item && item.epoch === epoch && accountReady() && homeVisible(item.host) && same(item.identity, window.BankClient?.identity?.()) && (!item.guard || item.guard.valid()) && (!control || control.isConnected && item.host.contains(control));
  }
  function stopFocusWait() {
    if (!focusWait) return;
    focusWait.observer.disconnect(); clearTimeout(focusWait.timer); focusWait = null;
  }
  function focusInitial(item) {
    if (!initialFocusRequested || initialFocusDone || !active(item)) return;
    const ready = () => !document.documentElement.classList.contains('navren-private-locked') && !document.querySelector('.page-scroll')?.hasAttribute('aria-busy');
    const finish = () => {
      if (!active(item) || !ready()) return;
      initialFocusDone = true; stopFocusWait();
      // restore() 已排入页面的默认定位；这里随后排入同一帧，仅覆盖此次显式跳转。
      requestAnimationFrame(() => { if (active(item) && ready()) { item.host.tabIndex = -1; item.host.focus({ preventScroll: true }); item.host.scrollIntoView?.({ block: 'start', behavior: 'auto' }); } });
    };
    if (ready()) { finish(); return; }
    if (focusWait?.item === item) return;
    stopFocusWait();
    const observer = new MutationObserver(finish);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    const scroll = document.querySelector('.page-scroll'); if (scroll) observer.observe(scroll, { attributes: true, attributeFilter: ['aria-busy'] });
    focusWait = { item, observer, timer: setTimeout(() => { initialFocusDone = true; stopFocusWait(); }, 20000) };
  }
  function draw(item, focus) {
    if (!active(item)) return;
    const prior = item.host.contains(document.activeElement) ? document.activeElement : null;
    const restore = focus || prior?.dataset.insuranceNeed || prior?.dataset.insuranceAction;
    item.host.dataset.i18nSkip = '';
    const heading = node('div', null, 'insurance-preferences-heading'), title = node('h3', text('保险需求', 'Insurance interests'));
    title.id = 'mineInsurancePreferencesTitle';
    heading.append(title);
    const actions = node('div', null, 'insurance-preferences-actions');
    const action = (key, label, handler, cls = '', target = actions) => {
      const button = node('button', label, 'p-btn ' + cls); button.type = 'button'; button.dataset.insuranceAction = key;
      button.disabled = Boolean(item.busy);
      button.onclick = () => { if (!item.busy && active(item, button)) handler(); }; target.append(button); return button;
    };
    const save = async () => {
      if (item.busy || !active(item)) return;
      const keys = clean(item.draft);
      item.dirty = true; item.busy = true; item.notice = text('正在保存…', 'Saving…'); draw(item);
      try {
        await Storage.set('navren_insurancePreferences', keys);
        if (!active(item)) return;
        const saved = read(item.identity);
        if (JSON.stringify(saved) !== JSON.stringify(keys)) throw new Error(text('已保存偏好与本次选择不一致，请重新读取。', 'Saved preferences differ from this selection. Reload before editing.'));
        item.saved = [...saved]; item.draft = [...saved]; item.dirty = false; item.editing = false; item.notice = text('已保存。', 'Saved.');
        window.dispatchEvent(new CustomEvent('navren:insurance-preferences-updated', { detail: { registered_user_id: item.identity.registered_user_id, workspace_id: item.identity.workspace_id, account_id: item.identity.account_id, privacy_revision: item.identity.privacy_revision } }));
      } catch (error) {
        if (active(item)) item.notice = error.message || text('未能保存，请稍后重试。', 'Could not save. Please try again.');
      } finally {
        item.busy = false;
        if (active(item)) draw(item, item.editing ? undefined : 'edit');
      }
    };
    const children = [heading];
    item.host.classList.toggle('is-editing', Boolean(item.editing));
    item.host.setAttribute('aria-busy', String(Boolean(item.busy)));
    if (item.editing) {
      heading.append(node('span', text('最多 3 项，按选择顺序', 'Up to 3, in selection order'), 'insurance-preferences-limit'));
      const choices = node('div', null, 'insurance-preferences-choices'); choices.setAttribute('role', 'group'); choices.setAttribute('aria-label', title.textContent);
      for (const key of topics) {
        const index = item.draft.indexOf(key), label = text(...labels[key]);
        const choice = node('button', (index >= 0 ? (index + 1) + ' · ' : '') + label, 'insurance-preferences-choice');
        choice.type = 'button'; choice.dataset.insuranceNeed = key; choice.setAttribute('aria-pressed', String(index >= 0)); choice.disabled = Boolean(item.busy) || index < 0 && item.draft.length >= 3;
        choice.onclick = () => {
          if (item.busy || !active(item, choice) || !item.editing) return;
          const position = item.draft.indexOf(key);
          if (position >= 0) item.draft.splice(position, 1); else if (item.draft.length < 3) item.draft.push(key); else return;
          item.dirty = true; item.notice = ''; draw(item, key);
        };
        choices.append(choice);
      }
      action('automatic', text('按已有资料展示', 'Use existing records'), () => { item.draft = []; save(); });
      action('cancel', text('取消', 'Cancel'), () => { item.saved = read(item.identity); item.draft = [...item.saved]; item.dirty = false; item.editing = false; item.notice = text('未保存的选择已取消。', 'Unsaved changes cancelled.'); draw(item, 'edit'); });
      action('save', text('保存', 'Save'), save, 'insurance-preferences-save');
      children.push(choices, actions);
    } else {
      const summary = node('div', null, 'insurance-preferences-summary'); summary.setAttribute('aria-label', text('已保存的保险需求', 'Saved insurance interests'));
      if (item.saved.length) for (const [index, key] of item.saved.entries()) {
        const tag = node('span', null, 'insurance-preferences-tag'); tag.dataset.insuranceSummary = key;
        tag.append(node('span', index + 1, 'insurance-preferences-order'), node('span', text(...labels[key]))); summary.append(tag);
      } else summary.append(node('span', text('未设置，按已有资料展示', 'Not set; use existing records'), 'insurance-preferences-empty'));
      heading.append(summary);
      action('edit', text('修改', 'Edit'), () => { item.saved = read(item.identity); item.draft = [...item.saved]; item.dirty = false; item.editing = true; item.notice = ''; draw(item, item.draft[0] || topics[0]); }, 'insurance-preferences-edit', heading);
    }
    const status = node('p', item.notice || (item.editing ? text('已选择 ', 'Selected ') + item.draft.length + text(' 项', ' topics') : ''), 'insurance-preferences-status');
    status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    status.hidden = !status.textContent; children.push(status);
    item.host.replaceChildren(...children);
    if (restore) [...item.host.querySelectorAll('button')].find(button => button.dataset.insuranceNeed === restore || button.dataset.insuranceAction === restore)?.focus({ preventScroll: true });
  }
  function clear() {
    epoch++; request++; record = null; initialFocusDone = true; stopFocusWait();
    const host = document.getElementById('mineInsurancePreferences');
    if (host) {
      host.removeAttribute('aria-busy');
      host.replaceChildren(node('p', text('账号偏好尚未核对，请稍后重试。', 'Account preferences are not verified. Please try again shortly.'), 'insurance-preferences-status'));
    }
  }
  async function refresh() {
    const host = document.getElementById('mineInsurancePreferences'); if (!homeVisible(host)) return;
    const token = ++request, capturedEpoch = epoch;
    try {
      const ensured = await window.BankClient.ensure();
      await Storage.ready();
      const identity = window.BankClient.identity();
      const guard = await window.BankClient.guard?.();
      if (token !== request || capturedEpoch !== epoch || !accountReady() || !homeVisible(host) || !same(ensured, identity) || guard && !guard.valid()) return;
      if (!record || !same(record.identity, identity) || record.host !== host) {
        const saved = read(identity); record = { host, identity: { ...identity }, epoch, guard, saved, draft: [...saved], dirty: false, editing: false, busy: false, notice: '' };
      } else {
        record.guard = guard;
        if (!record.dirty) { record.saved = read(identity); record.draft = [...record.saved]; }
      }
      draw(record);
      focusInitial(record);
    } catch {
      if (token === request && capturedEpoch === epoch && homeVisible(host)) {
        record = null; host.replaceChildren(node('p', text('当前账户身份暂未核对，请稍后重试。', 'Could not verify the current account. Try again shortly.'), 'insurance-preferences-status'));
      }
    }
  }
  function schedule() {
    if (scheduled) return; scheduled = true;
    Promise.resolve().then(() => { scheduled = false; return refresh(); });
  }
  document.addEventListener('DOMContentLoaded', schedule);
  window.addEventListener('navren:view', event => {
    if (event.detail?.page !== 'mine' || event.detail?.view && event.detail.view !== 'home') { initialFocusDone = true; stopFocusWait(); }
    request++; schedule();
  });
  window.addEventListener('pageshow', () => { pageActive = true; schedule(); });
  window.addEventListener('pagehide', () => { pageActive = false; clear(); });
  for (const event of ['navren:privacy-reset', 'navren:identity-revoked']) window.addEventListener(event, clear);
  window.addEventListener('navren:account-switched', schedule);
  window.addEventListener('navren:language', () => { if (record && active(record)) draw(record); else schedule(); });
  window.addEventListener('navren:account-state', event => {
    if (event.detail?.status !== 'ready' || !accountReady()) { clear(); return; }
    if (record && active(record) && record.dirty && !record.busy &&
        JSON.stringify(read(record.identity)) !== JSON.stringify(record.saved)) {
      record.notice = text('其他页面已更新偏好，当前未保存的选择仍保留。', 'Another page updated preferences. Your unsaved choices are kept.');
    }
    schedule();
  });
  return Object.freeze({ read, refresh });
})();

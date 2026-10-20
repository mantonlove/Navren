/** 已审核回复的对话选择。只发送下一轮文字，不提供银行操作授权。 */
window.NavrenReplyInteractions = (() => {
  const schema = 'navren.reply-interaction.v1', records = new Map();
  let sequence = 0;
  const labels = {
    confirm:['确认','Confirm'],reject:['拒绝','Decline'],confirmed:['已确认','Confirmed'],rejected:['已拒绝','Declined'],
    selected:['已选择','Selected'],submitting:['正在提交选择…','Sending your choice…'],
    expired:['此选择已失效，请重新查询。','This choice has expired. Please ask again.'],
    historical:['历史记录，请重新查询后再选择。','History only. Ask again to make a choice.']
  };
  const t = key => labels[key][window.NavrenI18n?.language === 'en' ? 1 : 0];
  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  function plain(value, limit) {
    return typeof value === 'string' && !!value.trim() && value.length <= limit && !/[\p{Cc}\p{Cf}\p{Cs}]/u.test(value) && !/https?:\/\/|javascript:|data:|<\/?[a-z][^>]*>/i.test(value);
  }
  function normalize(value, binding) {
    if (!value || value.schema_version !== schema || value.status !== 'awaiting_input' || value.scope !== 'conversation_only' ||
      !['confirmation','single_choice'].includes(value.kind) || !plain(value.prompt,240) ||
      !plain(value.interaction_id,200) || !plain(value.workspace_id,200) || !plain(value.response_id,200) ||
      !Number.isSafeInteger(value.privacy_revision) || value.privacy_revision < 0 ||
      value.workspace_id !== binding?.workspace_id || value.response_id !== binding?.response_id || value.privacy_revision !== binding?.privacy_revision ||
      typeof value.expires_at !== 'string' || !/^\d{4}-\d{2}-\d{2}T.+(?:Z|[+-]\d{2}:\d{2})$/.test(value.expires_at) || !Number.isFinite(Date.parse(value.expires_at)) ||
      !Array.isArray(value.options) || value.options.length < 2 || value.options.length > 4) return null;
    const ids = new Set(), texts = new Set(), options = [];
    for (const item of value.options) {
      if (!item || typeof item.id !== 'string' || !/^[a-z][a-z0-9_-]{0,31}$/.test(item.id) || !plain(item.label,48)) return null;
      const unique = item.label.trim().normalize('NFKC').toLocaleLowerCase();
      if (ids.has(item.id) || texts.has(unique)) return null;
      ids.add(item.id); texts.add(unique); options.push({id:item.id,label:item.label.trim()});
    }
    if (value.kind === 'confirmation' && (options.length !== 2 || options[0].id !== 'confirm' || options[0].label !== '确认' || options[1].id !== 'reject' || options[1].label !== '拒绝')) return null;
    return {schema_version:schema,status:'awaiting_input',scope:'conversation_only',workspace_id:value.workspace_id,response_id:value.response_id,
      privacy_revision:value.privacy_revision,interaction_id:value.interaction_id,expires_at:value.expires_at,kind:value.kind,prompt:value.prompt.trim(),options};
  }
  function selectionText(spec, option) {
    return spec.kind === 'confirmation' && option.id === 'reject'
      ? '针对上一条问题「'+spec.prompt+'」，我拒绝继续。'
      : '针对上一条问题「'+spec.prompt+'」，我选择「'+option.label+'」。请继续处理原请求。';
  }
  function snapshot(message) {
    const record = records.get(message); if (!record) return null;
    return JSON.parse(JSON.stringify({...record.spec,display_state:record.state,selected_id:record.selected || null}));
  }
  function publish(record) { record.context.onState?.(snapshot(record.message)); }
  function expire(record, state = 'expired') {
    if (record.state === 'selected') return;
    clearTimeout(record.timer); record.state = state; draw(record); publish(record);
  }
  function isCurrent(record) {
    return records.get(record.message) === record && record.message.isConnected && record.box.isConnected && !record.context.historical &&
      record.context.isCurrent?.() === true && Date.now() < Date.parse(record.spec.expires_at);
  }
  function status(record) {
    if (record.state === 'selected') {
      const choice = record.spec.options.find(option => option.id === record.selected);
      if (record.spec.kind === 'confirmation') return t(record.selected === 'reject' ? 'rejected' : 'confirmed');
      return t('selected') + ' · ' + (choice?.label || '');
    }
    if (record.state === 'submitting') return t('submitting');
    return t(record.state === 'historical' ? 'historical' : 'expired');
  }
  function draw(record) {
    record.box.replaceChildren(); record.box.setAttribute('aria-busy', String(record.state === 'submitting'));
    // 选择一提交就收起整组回答；历史或已消费的选择只留下状态。
    if (record.state === 'awaiting_input') {
      const prompt = node('p',record.spec.prompt,'reply-interaction-prompt'); prompt.id = record.promptId; prompt.setAttribute('translate','no'); record.box.append(prompt);
      const group = node('div',null,'reply-interaction-options'); group.setAttribute('role','group'); group.setAttribute('aria-labelledby',prompt.id);
      for (const option of record.spec.options) {
        const label = record.spec.kind === 'confirmation' ? t(option.id) : option.label;
        const b = node('button',label,'reply-interaction-button'); b.type = 'button'; b.dataset.replyChoice = option.id;
        b.disabled = record.state !== 'awaiting_input'; b.classList.toggle('is-primary',record.spec.kind === 'confirmation' && option.id === 'confirm');
        b.classList.toggle('is-selected',record.selected === option.id); b.setAttribute('aria-pressed',String(record.selected === option.id));
        b.onclick = () => choose(record,option); group.append(b);
      }
      record.box.append(group);
    }
    if (record.state !== 'awaiting_input') { const note = node('p',status(record),'reply-interaction-status'); note.setAttribute('role','status'); record.box.append(note); }
  }
  async function choose(record, option) {
    if (record.state !== 'awaiting_input') return;
    if (!isCurrent(record)) { expire(record); return; }
    if (record.context.isBusy?.()) return;
    record.state = 'submitting'; record.selected = option.id; draw(record); publish(record);
    try {
      const accepted = await record.context.send(selectionText(record.spec,option),record.spec);
      if (record.state !== 'submitting') return;
      if (accepted !== true) { expire(record); return; }
      clearTimeout(record.timer); record.state = 'selected'; record.selected = option.id; draw(record); publish(record);
    } catch { expire(record); }
  }
  function render(message, value, context = {}) {
    const spec = normalize(value,context), bubble = message?.querySelector('.bubble'); if (!spec || !bubble) return false;
    const previous = records.get(message); if (previous) clearTimeout(previous.timer);
    bubble.querySelectorAll('.reply-interaction').forEach(n => n.remove());
    const box = node('div',null,'reply-interaction'); box.dataset.i18nSkip = ''; bubble.append(box);
    const selected = value.display_state === 'selected' && spec.options.some(option => option.id === value.selected_id) ? value.selected_id : null;
    const state = selected ? 'selected' : context.historical ? 'historical' : Date.parse(spec.expires_at) <= Date.now() ? 'expired' : 'awaiting_input';
    const record = {message,box,spec,context,state,selected,promptId:'reply-interaction-prompt-'+(++sequence)}; records.set(message,record); draw(record);
    if (state === 'awaiting_input') record.timer = setTimeout(() => expire(record),Math.min(2147483647,Math.max(1,Date.parse(spec.expires_at)-Date.now())));
    return true;
  }
  function invalidateAll(keepInteractionId = null) {
    for (const [message,record] of records) {
      if (!message.isConnected) { clearTimeout(record.timer); records.delete(message); continue; }
      if (record.spec.interaction_id !== keepInteractionId && ['awaiting_input','submitting'].includes(record.state)) expire(record,'superseded');
    }
  }
  function clear() { for (const record of records.values()) clearTimeout(record.timer); records.clear(); }
  window.addEventListener('navren:language',() => { for (const record of records.values()) if (record.box.isConnected) draw(record); });
  window.addEventListener('navren:privacy-reset',clear); window.addEventListener('pagehide',() => invalidateAll());
  return {normalize,render,snapshot,invalidateAll,clear,selectionText};
})();

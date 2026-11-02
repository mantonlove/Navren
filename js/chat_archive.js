/* 注册用户只读聊天归档。仅消费受权 reader 的 v2 投影与已绑定媒体，不恢复执行权限。 */
(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.NavrenChatArchive = api;
})(typeof globalThis === 'object' ? globalThis : this, function () {
  'use strict';
  const CONTRACT = 'navren.registered-dialogue-archive/2';
  const LIMIT = 20; // Turns, not flattened message count or model context size.
  const HEX = /^[0-9a-f]{32}$/;
  const SUBJECT = /^[A-Za-z0-9:_-]{16,192}$/;
  const HASH = /^[a-f0-9]{64}$/;
  const OWNER = /^[a-f0-9]{12}4[a-f0-9]{3}[89ab][a-f0-9]{15}$/; // DTO v2 shape, not client authority.
  const PERSONAS = new Set(['tycoon', 'grain_farmer', 'working_landlady', 'retired_entrepreneur']);
  const IDENTITY_FIELDS = ['registered_user_id', 'workspace_id', 'account_id', 'privacy_revision', 'auth_subject'];
  const RESTART = new Set(['PAGE_BINDING_CHANGED', 'PERSONAL_REVISION_CHANGED', 'INVALID_ARCHIVE_DTO',
    'CURSOR_CYCLE', 'TURN_CONFLICT', 'SEQUENCE_CONFLICT', 'PAGE_ORDER_CONFLICT', 'MESSAGE_ID_CONFLICT',
    'STALE_RESPONSE', 'STALE_IDENTITY', 'UNBOUND_RESPONSE', 'SESSION_CHANGED', 'WORKSPACE_CHANGED',
    'CSRF_DENIED', 'BANK_RELOGIN_REQUIRED', 'PRIVACY_REVISION_CHANGED', 'SNAPSHOT_CHANGED', 'ARCHIVE_EPOCH_CHANGED']);
  const REFERENCE_PARAMETERS = Object.freeze({
    event: ['edition_date', 'event_version', 'revision'], article: ['reading_mode'], weekly: ['reading_mode'],
    graph: ['accounting_basis', 'edge_id', 'expected_version', 'from', 'insight_id', 'node_id', 'portfolio', 'source', 'to', 'view'],
    calendar_item: [], calendar: ['accounting_basis', 'from', 'include_life_events', 'kinds', 'period', 'portfolio', 'simulation_overlay', 'source', 'to', 'view'],
    product: [], goal: [], research_plan: [], operation: [], report: ['month', 'year'], account: [], card: [],
    holding: [], loan: [], deposit: [], subscription: [], mandate: [], task: [], plan: [], support: [],
    permission: [], scheduled_payment: [], split_bill: []
  });
  // Exact blocked names/aliases from dto-v2 inert + interface-v2. Resolver is an
  // explicit Root denial while the Agent schema omission remains a dependency.
  const BLOCKED_DISPLAY_KEYS = new Set(['actions', 'action', 'bankactions', 'followups', 'followupquestions',
    'readonlyactions', 'confirmation', 'confirmationtoken', 'confirmtoken', 'retry', 'retrytoken', 'pay', 'payment',
    'token', 'servicesignature', 'wireref', 'messagestate', 'callback', 'endpoint', 'href', 'url', 'intent',
    'contextref', 'availability', 'operationid', 'draftid', 'servicenonce', 'confirmationcontext',
    'proto', 'constructor', 'prototype']);
  const SAFE_STATUS = Object.freeze({
    withheld: '该条历史回复当前无法核验，正文暂不展示。',
    not_published: '这次回复未发布。',
    archive_unavailable: '这条历史回复未完整保存，暂时无法恢复。'
  });

  function fail(code) {
    const error = new Error(code);
    error.name = 'ChatArchiveError';
    error.code = code;
    return error;
  }
  function own(value, key) { return Object.prototype.hasOwnProperty.call(value, key); }
  function integer(value, minimum) { return Number.isSafeInteger(value) && value >= minimum; }
  function text(value, maximum, minimum) {
    if (typeof value !== 'string') return false;
    const length = Array.from(value).length; // JSON Schema counts Unicode code points, not UTF-16 units.
    return length >= (minimum === undefined ? 1 : minimum) && length <= maximum;
  }
  function identity(value) {
    if (!value || typeof value !== 'object' || typeof value.registered_user_id !== 'string' ||
        !HEX.test(value.registered_user_id) || typeof value.workspace_id !== 'string' ||
        !HEX.test(value.workspace_id) || !text(value.account_id, 160) ||
        !integer(value.privacy_revision, 0) || typeof value.auth_subject !== 'string' ||
        !SUBJECT.test(value.auth_subject)) throw fail('UNBOUND_IDENTITY');
    return Object.freeze(Object.fromEntries(IDENTITY_FIELDS.map(key => [key, value[key]])));
  }
  function same(a, b) { return IDENTITY_FIELDS.every(key => a[key] === b[key]); }
  function copy(value, seen, depth) {
    if (value === null || typeof value === 'string' || typeof value === 'boolean') return value;
    if (typeof value === 'number' && Number.isFinite(value)) return value;
    if (!value || typeof value !== 'object' || depth > 64 || seen.has(value) ||
        Object.getOwnPropertySymbols(value).length ||
        (!Array.isArray(value) && Object.prototype.toString.call(value) !== '[object Object]')) throw fail('INVALID_ARCHIVE_DTO');
    seen.add(value);
    const result = Array.isArray(value) ? [] : Object.create(null);
    for (const key of Object.keys(value)) {
      const descriptor = Object.getOwnPropertyDescriptor(value, key);
      if (!descriptor || !own(descriptor, 'value')) throw fail('INVALID_ARCHIVE_DTO');
      Object.defineProperty(result, key, { value: copy(descriptor.value, seen, depth + 1),
        enumerable: true, writable: true, configurable: true });
    }
    if (Array.isArray(value) && Object.keys(value).length !== value.length) throw fail('INVALID_ARCHIVE_DTO');
    seen.delete(value);
    return result;
  }
  function clone(value) { return copy(value, new Set(), 0); }
  function freeze(value) {
    if (value && typeof value === 'object') {
      Object.keys(value).forEach(key => freeze(value[key]));
      Object.freeze(value);
    }
    return value;
  }
  function keys(value, required, optional) {
    const allowed = new Set(required.concat(optional || []));
    if (!value || typeof value !== 'object' || Array.isArray(value) ||
        required.some(key => !own(value, key)) || Object.keys(value).some(key => !allowed.has(key))) throw fail('INVALID_ARCHIVE_DTO');
  }
  function canonical(value) {
    if (value === null || typeof value !== 'object') return JSON.stringify(value);
    if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
    return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
  }
  function iso(seconds) {
    if (typeof seconds !== 'number' || !Number.isFinite(seconds)) throw fail('INVALID_ARCHIVE_DTO');
    const date = new Date(seconds * 1000);
    if (!Number.isFinite(date.getTime())) throw fail('INVALID_ARCHIVE_DTO');
    return date.toISOString();
  }
  function readonlyFlags(value) {
    if (value.historical_readonly !== true || value.current_fact_eligible !== false ||
        value.execution_authority !== false) throw fail('INVALID_ARCHIVE_DTO');
  }
  function inert(value, depth) {
    if (depth > 32) throw fail('INVALID_ARCHIVE_DTO');
    if (value === null || ['string', 'boolean'].includes(typeof value) ||
        (typeof value === 'number' && Number.isFinite(value))) return;
    if (Array.isArray(value)) { value.forEach(child => inert(child, depth + 1)); return; }
    if (!value || typeof value !== 'object') throw fail('INVALID_ARCHIVE_DTO');
    for (const key of Object.keys(value)) {
      const compact = key.replace(/[_-]/g, '').toLowerCase();
      if (BLOCKED_DISPLAY_KEYS.has(compact) || compact.endsWith('token') || compact.startsWith('resolver') ||
          (/^on.+/i).test(key)) throw fail('INVALID_ARCHIVE_DTO');
      inert(value[key], depth + 1);
    }
  }
  function archivedDisplay(value) {
    keys(value, ['contract', 'presentation', 'reply_presentation', 'metric_records', 'widgets',
      'historical_readonly', 'current_fact_eligible', 'execution_authority']);
    readonlyFlags(value);
    if (value.contract !== 'navren.archived-display/1' || !Array.isArray(value.metric_records) ||
        !Array.isArray(value.widgets)) throw fail('INVALID_ARCHIVE_DTO');
    if (value.presentation !== null) {
      if (!value.presentation || Array.isArray(value.presentation) ||
          value.presentation.contract !== 'navren.presentation/1') throw fail('INVALID_ARCHIVE_DTO');
    }
    if (value.reply_presentation !== null) {
      if (!value.reply_presentation || Array.isArray(value.reply_presentation) ||
          value.reply_presentation.schema_version !== 'navren.reply-presentation.v1') throw fail('INVALID_ARCHIVE_DTO');
    }
    inert(value.presentation, 0); inert(value.reply_presentation, 0); inert(value.metric_records, 0);
    for (const widget of value.widgets) {
      keys(widget, ['type', 'presentation']);
      if (widget.type !== 'financial_presentation_snapshot' || !widget.presentation ||
          Array.isArray(widget.presentation) || widget.presentation.contract !== 'navren.presentation/1') throw fail('INVALID_ARCHIVE_DTO');
      inert(widget.presentation, 0);
    }
  }
  function referenceMetadata(references, attachments, content) {
    if (!Array.isArray(references) || references.length > 3 || !Array.isArray(attachments) || attachments.length > 9 ||
        (content === '' && references.length < 1 && !attachments.some(item=>item?.contract==='navren.chat-media/1'))) throw fail('INVALID_ARCHIVE_DTO');
    for (const ref of references) {
      keys(ref, ['contract', 'kind', 'id', 'source_scope', 'version', 'label', 'parameters',
        'historical_readonly', 'current_fact_eligible', 'execution_authority']);
      readonlyFlags(ref);
      if (ref.contract !== 'navren.archived-reference/1' || typeof ref.kind !== 'string' || !own(REFERENCE_PARAMETERS, ref.kind) ||
          !text(ref.id, 128) || !text(ref.source_scope, 128) || !/^[A-Za-z0-9_.:-]+$/.test(ref.source_scope) ||
          !(ref.version === null || Number.isSafeInteger(ref.version) || typeof ref.version === 'string') ||
          !text(ref.label, 200)) throw fail('INVALID_ARCHIVE_DTO');
      keys(ref.parameters, [], REFERENCE_PARAMETERS[ref.kind]);
      for (const [key, value] of Object.entries(ref.parameters)) {
        if (key === 'reading_mode') { if (value !== 'historical_audit') throw fail('INVALID_ARCHIVE_DTO'); }
        else if (key === 'event_version' || key === 'expected_version') {
          if (!(Number.isSafeInteger(value) || typeof value === 'string')) throw fail('INVALID_ARCHIVE_DTO');
        } else if (key === 'revision') {
          if (!integer(value, 1) || value > 999999999) throw fail('INVALID_ARCHIVE_DTO');
        } else if (key === 'year') {
          if (!integer(value, 2025) || value > 2100) throw fail('INVALID_ARCHIVE_DTO');
        } else if (key === 'include_life_events' || key === 'simulation_overlay') {
          if (typeof value !== 'boolean') throw fail('INVALID_ARCHIVE_DTO');
        } else if (key === 'kinds') {
          if (!Array.isArray(value) || value.length > 16 || value.some(kind => !text(kind, 64))) throw fail('INVALID_ARCHIVE_DTO');
        } else if (!text(value, 160)) throw fail('INVALID_ARCHIVE_DTO');
      }
    }
    mediaList(attachments, true);
  }
  const DOCUMENT_MIME = Object.freeze({
    'application/pdf':'pdf','application/vnd.openxmlformats-officedocument.wordprocessingml.document':'docx',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':'xlsx',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation':'pptx',
    'text/plain':'txt','text/markdown':'md','text/csv':'csv'});
  function documentFields(value) {
    const name=value.file_name, format=DOCUMENT_MIME[value.mime_type];
    if (!text(name,180) || name !== name.trim() || /[\/\\]/u.test(name) || /\p{C}/u.test(name) ||
        name.lastIndexOf('.') <= 0 || name.slice(name.lastIndexOf('.')).toLowerCase() !== '.'+format ||
        value.document_format !== format || !integer(value.text_chars,0) || value.text_chars > 120000 ||
        !(value.extraction_status === 'text' && value.text_chars > 0 && value.extraction_warning === null ||
          value.extraction_status === 'no_text' && value.text_chars === 0 && text(value.extraction_warning,200))) throw fail('INVALID_ARCHIVE_DTO');
  }
  function mediaDescriptor(value) {
    const document=value?.kind === 'document';
    const required=['contract','media_id','kind','mime_type','bytes','sha256','duration_ms','storage_status',
      'content_url','historical_readonly','execution_authority','audio_format'];
    keys(value, document ? required.concat(['file_name','document_format','text_chars','extraction_status','extraction_warning']) : required);
    const mime = new Set(['image/png','image/jpeg','image/webp','image/gif','audio/wav','audio/webm','audio/ogg','audio/mpeg','audio/mp4','audio/pcm',...Object.keys(DOCUMENT_MIME)]);
    if (value.contract !== 'navren.chat-media/1' || typeof value.media_id !== 'string' || !HASH.test(value.media_id) ||
        typeof value.sha256 !== 'string' || !HASH.test(value.sha256) || !['image','audio','document'].includes(value.kind) ||
        !mime.has(value.mime_type) || (value.kind === 'image') !== value.mime_type.startsWith('image/') ||
        (value.kind === 'audio') !== value.mime_type.startsWith('audio/') || document !== own(DOCUMENT_MIME,value.mime_type) ||
        !integer(value.bytes,1) || value.bytes > (value.kind === 'audio' ? 26214400 : 10485760) ||
        value.storage_status !== 'bound' || value.content_url !== '/api/finance/chat-media/' + value.media_id ||
        value.historical_readonly !== true || value.execution_authority !== false ||
        !(value.duration_ms === null || integer(value.duration_ms,1) && value.duration_ms <= 600000)) throw fail('INVALID_ARCHIVE_DTO');
    if (document) {
      if (value.duration_ms !== null || value.audio_format !== null) throw fail('INVALID_ARCHIVE_DTO');
      documentFields(value);
    }
    const format=value.audio_format;
    if (format !== null) {
      keys(format,['encoding','sample_rate','channels']);
      if (format.encoding !== 'pcm_s16le' || ![8000,16000,22050,24000,44100,48000].includes(format.sample_rate) ||
          ![1,2].includes(format.channels)) throw fail('INVALID_ARCHIVE_DTO');
    }
    if ((value.mime_type === 'audio/pcm') !== (format !== null)) throw fail('INVALID_ARCHIVE_DTO');
  }
  function mediaList(attachments,legacyAllowed=false) {
    if (!Array.isArray(attachments)) throw fail('INVALID_ARCHIVE_DTO');
    const ids=new Set();let legacy=0;
    for (const attachment of attachments) {
      if (legacyAllowed && attachment?.storage_status === 'not_archived') {
        keys(attachment,['kind','storage_status']);
        if (attachment.kind !== 'image' || ++legacy > 1) throw fail('INVALID_ARCHIVE_DTO');
      } else {
        mediaDescriptor(attachment);
        if ((!legacyAllowed && attachment.kind !== 'audio') || ids.has(attachment.media_id)) throw fail('INVALID_ARCHIVE_DTO');
        ids.add(attachment.media_id);
      }
    }
    if (ids.size > 8) throw fail('INVALID_ARCHIVE_DTO');
  }
  function assistantDTO(value, page) {
    const basic = ['message_id', 'role', 'status', 'content', 'historical_readonly', 'current_fact_eligible', 'execution_authority'];
    const published = ['language', 'source_versions_state', 'original_personal_revision', 'original_privacy_revision',
      'original_response_hash', 'original_review', 'publication', 'readonly_components'];
    keys(value, basic, published.concat('attachments'));
    if (!text(value.message_id, 160) || value.role !== 'assistant' || value.historical_readonly !== true ||
        value.current_fact_eligible !== false || value.execution_authority !== false) throw fail('INVALID_ARCHIVE_DTO');
    if (own(value, 'readonly_components')) archivedDisplay(value.readonly_components);
    if (own(value,'attachments')) mediaList(value.attachments);
    if (value.status === 'withheld' || value.status === 'not_published' || value.status === 'archive_unavailable') {
      if (value.content !== null || own(value,'attachments') && value.attachments.length) throw fail('INVALID_ARCHIVE_DTO');
      return;
    }
    if (!['published_original', 'published_local_rule', 'published_verified_facts'].includes(value.status) ||
        published.some(key => !own(value, key)) || !text(value.content, 6000) ||
        !['zh', 'en'].includes(value.language) || !['unchanged', 'changed'].includes(value.source_versions_state) ||
        !integer(value.original_personal_revision, 0) || value.original_personal_revision > page.personal_revision ||
        !integer(value.original_privacy_revision, 0) || value.original_privacy_revision > page.privacy_revision ||
        typeof value.original_response_hash !== 'string' || !HASH.test(value.original_response_hash)) throw fail('INVALID_ARCHIVE_DTO');
    const review = value.original_review;
    keys(review, ['source', 'status', 'reviewer_model', 'review_id'], ['rule_id']);
    if (!(review.reviewer_model === null || typeof review.reviewer_model === 'string') ||
        !(review.review_id === null || typeof review.review_id === 'string')) throw fail('INVALID_ARCHIVE_DTO');
    if (value.status === 'published_original') {
      if (review.source !== 'agent_review' || review.status !== 'approved_at_publication') throw fail('INVALID_ARCHIVE_DTO');
    } else if (value.status === 'published_verified_facts') {
      if (review.source !== 'host_verified_facts' || review.status !== 'not_required' || review.reviewer_model !== null || review.review_id !== null || own(review,'rule_id')) throw fail('INVALID_ARCHIVE_DTO');
    } else if (review.source !== 'local_rule' || review.status !== 'not_required' || !text(review.rule_id, 160)) {
      throw fail('INVALID_ARCHIVE_DTO');
    }
    keys(value.publication, ['state', 'published_at', 'client_acknowledged']);
    if (value.publication.state !== 'server_send_completed' ||
        typeof value.publication.client_acknowledged !== 'boolean') throw fail('INVALID_ARCHIVE_DTO');
    iso(value.publication.published_at);
  }
  function pageDTO(raw, binding, revision, snapshot, archiveEpoch, cursor, seen) {
    const page = clone(raw);
    keys(page, ['contract', 'registered_user_id', 'workspace_id', 'persona_id', 'privacy_revision',
      'personal_revision', 'items', 'next_cursor', 'has_more', 'model_context_limit', 'execution_authority', 'archive_coverage',
      'archive_epoch', 'snapshot_id']);
    if (page.contract !== CONTRACT || page.model_context_limit !== 16 || page.execution_authority !== false ||
        !integer(page.privacy_revision, 0) || !integer(page.personal_revision, 0) || !integer(page.archive_epoch, 0) ||
        typeof page.snapshot_id !== 'string' || !HASH.test(page.snapshot_id) ||
        typeof page.registered_user_id !== 'string' || !OWNER.test(page.registered_user_id) || !PERSONAS.has(page.persona_id) ||
        !Array.isArray(page.items) || page.items.length > LIMIT || typeof page.has_more !== 'boolean' ||
        !(page.next_cursor === null || text(page.next_cursor, 4096)) ||
        page.has_more !== (page.next_cursor !== null)) throw fail('INVALID_ARCHIVE_DTO');
    if (page.registered_user_id !== binding.registered_user_id || page.workspace_id !== binding.workspace_id ||
        page.persona_id !== binding.account_id || page.privacy_revision !== binding.privacy_revision) throw fail('PAGE_BINDING_CHANGED');
    if (revision !== null && revision !== page.personal_revision) throw fail('PERSONAL_REVISION_CHANGED');
    if (snapshot !== null && snapshot !== page.snapshot_id) throw fail('SNAPSHOT_CHANGED');
    if (archiveEpoch !== null && archiveEpoch !== page.archive_epoch) throw fail('ARCHIVE_EPOCH_CHANGED');
    keys(page.archive_coverage, ['source', 'legacy_public_history_imported']);
    if (page.archive_coverage.source !== 'registered_user_archive' ||
        page.archive_coverage.legacy_public_history_imported !== false) throw fail('INVALID_ARCHIVE_DTO');
    if (page.next_cursor !== null && (page.next_cursor === cursor || seen.has(page.next_cursor))) throw fail('CURSOR_CYCLE');
    let previous = 0;
    for (const item of page.items) {
      keys(item, ['request_id', 'sequence', 'received_at', 'user', 'assistant']);
      if (!text(item.request_id, 160) || !integer(item.sequence, 1) || item.sequence < previous) throw fail('INVALID_ARCHIVE_DTO');
      previous = item.sequence;
      iso(item.received_at);
      keys(item.user, ['message_id', 'role', 'status', 'content', 'references', 'attachments']);
      if (!text(item.user.message_id, 165) || item.user.message_id !== item.request_id + ':user' ||
          item.user.role !== 'user' || item.user.status !== 'archived' || !text(item.user.content, 8000, 0)) throw fail('INVALID_ARCHIVE_DTO');
      referenceMetadata(item.user.references, item.user.attachments, item.user.content);
      assistantDTO(item.assistant, page);
      if (item.assistant.message_id !== item.request_id) throw fail('INVALID_ARCHIVE_DTO');
    }
    return page;
  }
  function project(item) {
    const base = { request_id: item.request_id, turn_sequence: item.sequence, read_only: true,
      historical_readonly: true, current_fact_eligible: false, execution_authority: false };
    const user = { ...base, message_id: item.user.message_id, position: 0, role: 'user', kind: 'user-text',
      text: item.user.content, created_at: iso(item.received_at), status: 'archived',
      references: clone(item.user.references), attachments: clone(item.user.attachments) };
    const a = item.assistant;
    const readable = a.status === 'published_original' || a.status === 'published_local_rule' || a.status === 'published_verified_facts';
    const assistant = { ...base, message_id: a.message_id, position: 1, role: 'assistant',
      kind: readable ? 'published-readback' : 'safe-status', text: readable ? a.content : SAFE_STATUS[a.status],
      created_at: iso(readable ? a.publication.published_at : item.received_at), status: a.status,
      ...(readable ? { language: a.language, source_versions_state: a.source_versions_state,
        review_source: a.original_review.source, readonly_components: clone(a.readonly_components),
        attachments:clone(a.attachments||[]) } : {}) };
    // Neither original packets, actions, HTML nor current execution credentials
    // leave this projection. Trust comes from the injected authenticated reader.
    const receivedMediaOnly = user.text === '' && user.references.length === 0 &&
      user.attachments.some(item => item.contract === 'navren.chat-media/1') && (a.status === 'not_published' || a.status === 'archive_unavailable');
    return freeze(receivedMediaOnly ? [user] : [user, assistant]);
  }
  function restartCode(code) {
    return RESTART.has(code) || (typeof code === 'string' && code.startsWith('DIALOGUE_ARCHIVE_') &&
      code !== 'DIALOGUE_ARCHIVE_STORAGE_REQUIRED');
  }
  function create(options) {
    if (!options || typeof options.identityProvider !== 'function' || typeof options.readPage !== 'function' ||
        (own(options, 'onChange') && typeof options.onChange !== 'function')) throw fail('INVALID_ADAPTER');
    let epoch = 0, binding = null, pending = null, personalRevision = null, snapshot = null, archiveEpoch = null;
    let nextCursor = null, hasMore = null;
    let pagesLoaded = 0, phase = 'detached', errorState = null;
    let turns = new Map(), sequences = new Map(), messageIDs = new Map(), cursors = new Set();
    function clearPages() {
      turns.clear(); sequences.clear(); messageIDs.clear(); cursors.clear();
      personalRevision = null; snapshot = null; archiveEpoch = null; nextCursor = null; hasMore = null; pagesLoaded = 0;
    }
    function state() {
      const ordered = Array.from(turns.values()).sort((a, b) => a.sequence - b.sequence);
      return freeze({ contract: CONTRACT, epoch, phase,
        binding: binding ? { registered_user_id: binding.registered_user_id, workspace_id: binding.workspace_id,
          account_id: binding.account_id, privacy_revision: binding.privacy_revision } : null,
        personal_revision: personalRevision, snapshot_id: snapshot, archive_epoch: archiveEpoch,
        next_cursor: nextCursor, has_more: hasMore,
        pages_loaded: pagesLoaded, turns_loaded: turns.size,
        is_empty: phase === 'ready' && pagesLoaded > 0 && hasMore === false ? turns.size === 0 : null,
        messages: ordered.flatMap(turn => turn.messages), error: errorState ? { ...errorState } : null });
    }
    function notify() {
      if (options.onChange) {
        try { options.onChange(state()); }
        catch (_) { detach(false); throw fail('VIEW_CALLBACK_FAILED'); }
      }
    }
    function detach(announce) {
      epoch += 1;
      const old = pending; pending = null; binding = null;
      clearPages(); phase = 'detached'; errorState = null;
      if (old) old.abort.abort();
      if (announce) notify();
    }
    function ticket() {
      if (!binding) throw fail('UNBOUND_IDENTITY');
      return { epoch, binding };
    }
    function current(job) { return binding !== null && epoch === job.epoch && same(binding, job.binding); }
    function guard(job) {
      if (!current(job)) throw fail('STALE_REQUEST');
      let provided;
      try { provided = identity(options.identityProvider()); }
      catch (_) { if (current(job)) detach(true); throw fail('UNBOUND_IDENTITY'); }
      if (!current(job)) throw fail('STALE_REQUEST');
      if (!same(provided, job.binding)) { detach(true); throw fail('IDENTITY_CHANGED'); }
    }
    function attach() {
      detach(false);
      const started = epoch;
      let provided;
      try { provided = identity(options.identityProvider()); }
      catch (_) { throw fail('UNBOUND_IDENTITY'); }
      if (epoch !== started) throw fail('STALE_REQUEST');
      binding = provided; phase = 'idle';
      const job = ticket(); guard(job); notify(); guard(job);
      return state();
    }
    function boundRead(job) {
      guard(job);
      const source = Promise.resolve(options.readPage({ cursor: job.cursor, limit: LIMIT, signal: job.abort.signal }));
      let onAbort;
      const aborted = new Promise((_, reject) => {
        onAbort = () => reject(fail('STALE_REQUEST'));
        if (job.abort.signal.aborted) onAbort();
        else job.abort.signal.addEventListener('abort', onAbort, { once: true });
      });
      return Promise.race([source, aborted]).finally(() => job.abort.signal.removeEventListener('abort', onAbort));
    }
    async function perform(job) {
      try {
        guard(job); notify(); guard(job);
        let raw;
        try { raw = await boundRead(job); }
        catch (error) {
          guard(job);
          throw fail(restartCode(error && error.code) ? error.code : 'PAGE_READ_FAILED');
        }
        guard(job);
        const page = pageDTO(raw, job.binding, personalRevision, snapshot, archiveEpoch, job.cursor, cursors);
        const nextTurns = new Map(turns), nextSequences = new Map(sequences), nextIDs = new Map(messageIDs);
        let minimum = null;
        for (const sequence of sequences.keys()) if (minimum === null || sequence < minimum) minimum = sequence;
        for (const item of page.items) {
          guard(job);
          const fingerprint = canonical(item);
          const existing = nextTurns.get(item.request_id);
          if (existing) {
            if (existing.fingerprint !== fingerprint) throw fail('TURN_CONFLICT');
            continue;
          }
          if (nextSequences.has(item.sequence)) throw fail('SEQUENCE_CONFLICT');
          if (pagesLoaded > 0 && minimum !== null && item.sequence >= minimum) throw fail('PAGE_ORDER_CONFLICT');
          const messages = project(item);
          for (const message of messages) {
            if (nextIDs.has(message.message_id)) throw fail('MESSAGE_ID_CONFLICT');
            nextIDs.set(message.message_id, item.request_id);
          }
          nextTurns.set(item.request_id, { fingerprint, sequence: item.sequence, messages });
          nextSequences.set(item.sequence, item.request_id);
        }
        guard(job);
        turns = nextTurns; sequences = nextSequences; messageIDs = nextIDs;
        personalRevision = page.personal_revision; snapshot = page.snapshot_id; archiveEpoch = page.archive_epoch;
        nextCursor = page.next_cursor; hasMore = page.has_more;
        if (job.cursor !== null) cursors.add(job.cursor);
        if (nextCursor !== null) cursors.add(nextCursor);
        pagesLoaded += 1; phase = 'ready'; errorState = null;
        notify(); guard(job);
        return state();
      } catch (error) {
        if (!current(job)) throw fail('STALE_REQUEST');
        guard(job);
        const code = error && error.name === 'ChatArchiveError' ? error.code : 'INVALID_ARCHIVE_DTO';
        const restart = restartCode(code);
        if (restart) clearPages();
        phase = 'error';
        errorState = { code, message: restart ? '聊天历史已变化，请重新加载。' : '聊天历史未能加载，请重试。',
          recovery: restart ? 'restart' : 'retry' };
        notify(); guard(job);
        throw fail(code);
      } finally { if (pending === job) pending = null; }
    }
    function loadNext() {
      const job = ticket(); guard(job);
      if (pending) return pending.promise;
      if (errorState && errorState.recovery === 'restart') throw fail('RESTART_REQUIRED');
      if (pagesLoaded > 0 && hasMore === false) return Promise.resolve(state());
      Object.assign(job, { cursor: nextCursor, abort: new AbortController(), promise: null });
      job.promise = Promise.resolve().then(() => perform(job));
      pending = job; phase = 'loading'; errorState = null;
      return job.promise;
    }
    function getState() { if (binding) guard(ticket()); return state(); }
    return Object.freeze({ attach, loadNext, getState, unload: () => detach(true) });
  }
  return Object.freeze({ contract: CONTRACT, status: 'V2_MEDIA_CANDIDATE_PENDING_JOINT_ADOPTION', page_turn_limit: LIMIT, create });
});

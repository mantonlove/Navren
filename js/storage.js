// USS isolated wiring candidate: embedded verified private-cache factory.
const NavrenPrivateStorageCache = (() => {
  'use strict';

  const PREFIX = 'navren:private-cache:provisional:v1:';
  const SCHEMA = 'navren-private-cache-provisional-v1';
  const HEX_ID = /^[0-9a-f]{32}$/;
  const AUTH_SUBJECT = /^[A-Za-z0-9:_-]{16,192}$/;
  const SLOT = /^[a-z][a-z0-9_-]{0,63}$/;
  const FIELDS = ['registered_user_id', 'workspace_id', 'privacy_revision', 'auth_subject'];

  function failure(code) {
    const error = new Error(code);
    error.name = 'PrivateCacheError';
    error.code = code;
    return error;
  }
  function own(object, key) { return Object.prototype.hasOwnProperty.call(object, key); }
  function revisionOK(value) {
    return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
  }
  function identity(value) {
    if (!value || typeof value !== 'object' ||
        typeof value.registered_user_id !== 'string' || !HEX_ID.test(value.registered_user_id) ||
        typeof value.workspace_id !== 'string' || !HEX_ID.test(value.workspace_id) ||
        !revisionOK(value.privacy_revision) ||
        typeof value.auth_subject !== 'string' || !AUTH_SUBJECT.test(value.auth_subject)) {
      throw failure('UNBOUND_IDENTITY');
    }
    return Object.freeze({
      registered_user_id: value.registered_user_id,
      workspace_id: value.workspace_id,
      privacy_revision: value.privacy_revision,
      auth_subject: value.auth_subject
    });
  }
  function same(a, b) { return FIELDS.every(key => a[key] === b[key]); }
  function partitionKey(binding) {
    return PREFIX + encodeURIComponent(binding.registered_user_id) + ':' +
      encodeURIComponent(binding.workspace_id);
  }
  function copyJSON(value, seen, depth) {
    if (value === null || typeof value === 'string' || typeof value === 'boolean') return value;
    if (typeof value === 'number' && Number.isFinite(value)) return value;
    if (!value || typeof value !== 'object' || depth > 64 || seen.has(value)) {
      throw failure('INVALID_CACHE_VALUE');
    }
    seen.add(value);
    const result = Array.isArray(value) ? [] : Object.create(null);
    if (!Array.isArray(value) && Object.prototype.toString.call(value) !== '[object Object]') {
      throw failure('INVALID_CACHE_VALUE');
    }
    for (const key of Object.keys(value)) {
      const descriptor = Object.getOwnPropertyDescriptor(value, key);
      if (!descriptor || !own(descriptor, 'value')) throw failure('INVALID_CACHE_VALUE');
      Object.defineProperty(result, key, {
        value: copyJSON(descriptor.value, seen, depth + 1),
        enumerable: true, configurable: true, writable: true
      });
    }
    if (Array.isArray(value) && Object.keys(value).length !== value.length) {
      throw failure('INVALID_CACHE_VALUE');
    }
    seen.delete(value);
    return result;
  }
  function clone(value) { return copyJSON(value, new Set(), 0); }

  function create(options) {
    if (!options || typeof options.identityProvider !== 'function' || !options.storage ||
        !['getItem', 'setItem', 'removeItem'].every(key => typeof options.storage[key] === 'function')) {
      throw failure('INVALID_ADAPTER');
    }
    const storage = options.storage;
    const leases = new WeakMap();
    const memory = new Map();
    const privacyBarriers = new Set();
    let epoch = 0;
    let current = null;
    let operating = false;

    function detach() {
      epoch += 1;
      current = null;
      memory.clear();
    }
    function provided() {
      try { return identity(options.identityProvider()); }
      catch (_) { detach(); throw failure('UNBOUND_IDENTITY'); }
    }
    function run(action) {
      if (operating) {
        detach();
        throw failure('REENTRANT_CACHE_OPERATION');
      }
      operating = true;
      try { return action(); }
      finally { operating = false; }
    }
    function detachedGuard(binding, expectedEpoch) {
      if (epoch !== expectedEpoch || !same(provided(), binding) || epoch !== expectedEpoch) {
        throw failure('IDENTITY_CHANGED');
      }
    }
    function readRaw(key) {
      try { return storage.getItem(key); }
      catch (_) { detach(); throw failure('CACHE_UNAVAILABLE'); }
    }
    function writeRaw(key, raw) {
      try { storage.setItem(key, raw); }
      catch (_) { detach(); throw failure('CACHE_UNAVAILABLE'); }
    }
    function removeRaw(key) {
      try { storage.removeItem(key); }
      catch (_) { detach(); throw failure('CACHE_UNAVAILABLE'); }
    }
    function parse(raw) {
      try { return JSON.parse(raw); }
      catch (_) { throw failure('INVALID_CACHE_RECORD'); }
    }
    function metaFor(binding) {
      const raw = readRaw(partitionKey(binding) + ':meta');
      if (raw === null) return { raw: null, generation: 0, state: 'active' };
      const value = parse(raw);
      if (!value || value.schema !== SCHEMA ||
          value.registered_user_id !== binding.registered_user_id ||
          value.workspace_id !== binding.workspace_id ||
          !Number.isSafeInteger(value.generation) || value.generation < 1 ||
          !['active', 'quarantined'].includes(value.state) ||
          !revisionOK(value.privacy_revision)) throw failure('INVALID_CACHE_RECORD');
      return { ...value, raw };
    }
    function basicGuard(lease) {
      const held = lease && typeof lease === 'object' ? leases.get(lease) : null;
      if (!current || !held || held.epoch !== epoch || !same(held.binding, current)) {
        throw failure('STALE_CACHE_LEASE');
      }
      if (!same(provided(), held.binding) || held.epoch !== epoch || current === null) {
        detach();
        throw failure('IDENTITY_CHANGED');
      }
      if (privacyBarriers.has(partitionKey(held.binding))) {
        detach();
        throw failure('CACHE_QUARANTINED');
      }
      return held;
    }
    function guard(lease) {
      const held = basicGuard(lease);
      const meta = metaFor(held.binding);
      basicGuard(lease); // A storage adapter may change identity while reading.
      if (meta.state === 'quarantined' || meta.generation !== held.generation) {
        detach();
        throw failure(meta.state === 'quarantined' ? 'CACHE_QUARANTINED' : 'CACHE_GENERATION_CHANGED');
      }
      return held;
    }
    function issue(binding, generation) {
      epoch += 1;
      memory.clear();
      current = binding;
      const lease = Object.freeze(Object.create(null));
      leases.set(lease, { binding, generation, epoch });
      return lease;
    }
    function attach() {
      detach();
      const expectedEpoch = epoch;
      const binding = provided();
      if (privacyBarriers.has(partitionKey(binding))) throw failure('CACHE_QUARANTINED');
      const meta = metaFor(binding);
      detachedGuard(binding, expectedEpoch);
      if (meta.state === 'quarantined') throw failure('CACHE_QUARANTINED');
      return issue(binding, meta.generation);
    }
    function slotName(slot) {
      if (typeof slot !== 'string' || !SLOT.test(slot)) throw failure('INVALID_CACHE_SLOT');
      return slot;
    }
    function envelope(binding, generation, raw) {
      if (raw === null) return null;
      const value = parse(raw);
      if (!value || value.schema !== SCHEMA ||
          value.registered_user_id !== binding.registered_user_id ||
          value.workspace_id !== binding.workspace_id ||
          !revisionOK(value.privacy_revision) || !Number.isSafeInteger(value.generation) ||
          value.generation < 0 || !value.values || Array.isArray(value.values) ||
          typeof value.values !== 'object') throw failure('INVALID_CACHE_RECORD');
      if (value.privacy_revision !== binding.privacy_revision || value.generation !== generation) return null;
      if (Object.keys(value.values).some(key => !SLOT.test(key))) throw failure('INVALID_CACHE_RECORD');
      return value;
    }
    function read(lease, slot) {
      slotName(slot);
      const held = guard(lease);
      const raw = readRaw(partitionKey(held.binding) + ':data');
      guard(lease);
      const record = envelope(held.binding, held.generation, raw);
      if (!record || !own(record.values, slot)) { memory.delete(slot); return null; }
      const value = clone(record.values[slot]);
      guard(lease);
      memory.set(slot, value);
      return clone(value);
    }
    function rollbackIfUnchanged(key, written, previous) {
      // Native Storage is synchronous. This also undoes a reentrant test adapter's
      // write only if no other writer has replaced the exact bytes meanwhile.
      try {
        if (storage.getItem(key) === written) {
          if (previous === null) storage.removeItem(key);
          else storage.setItem(key, previous);
        }
      } catch (_) { /* No private value may be returned after the guard failed. */ }
    }
    function write(lease, slot, value) {
      slotName(slot);
      const held = guard(lease);
      const copied = clone(value);
      guard(lease);
      const key = partitionKey(held.binding) + ':data';
      const previous = readRaw(key);
      guard(lease);
      const existing = envelope(held.binding, held.generation, previous);
      const values = existing ? clone(existing.values) : Object.create(null);
      Object.defineProperty(values, slot, { value: copied, enumerable: true, configurable: true, writable: true });
      const raw = JSON.stringify({
        schema: SCHEMA,
        registered_user_id: held.binding.registered_user_id,
        workspace_id: held.binding.workspace_id,
        privacy_revision: held.binding.privacy_revision,
        generation: held.generation,
        values
      });
      guard(lease);
      writeRaw(key, raw);
      try { guard(lease); }
      catch (error) { rollbackIfUnchanged(key, raw, previous); throw error; }
      memory.set(slot, copied);
    }
    function nextMeta(binding, previous, state) {
      if (!Number.isSafeInteger(previous.generation + 1)) throw failure('CACHE_GENERATION_EXHAUSTED');
      return JSON.stringify({
        schema: SCHEMA,
        registered_user_id: binding.registered_user_id,
        workspace_id: binding.workspace_id,
        privacy_revision: binding.privacy_revision,
        generation: previous.generation + 1,
        state
      });
    }
    function purgeCache(lease) {
      const held = guard(lease);
      const meta = metaFor(held.binding);
      guard(lease);
      // A generation bump rejects already issued leases in other tabs too.
      writeRaw(partitionKey(held.binding) + ':meta', nextMeta(held.binding, meta, 'active'));
      try { removeRaw(partitionKey(held.binding) + ':data'); }
      finally { detach(); }
    }
    function quarantinePrivacy(lease) {
      const held = guard(lease);
      const meta = metaFor(held.binding);
      guard(lease);
      privacyBarriers.add(partitionKey(held.binding));
      detach(); // Revoke memory and all issued requests before persistence.
      // Persist denial before removing payload. Even removeItem failure leaves a
      // tombstone that refuses all attach/read/write until explicit re-verification.
      writeRaw(partitionKey(held.binding) + ':meta', nextMeta(held.binding, meta, 'quarantined'));
      try { removeRaw(partitionKey(held.binding) + ':data'); }
      finally { detach(); }
    }
    function resumeAfterServerVerification() {
      detach();
      const expectedEpoch = epoch;
      if (typeof options.reverifyIdentity !== 'function') throw failure('REVERIFICATION_REQUIRED');
      const binding = provided();
      const meta = metaFor(binding);
      if (meta.state !== 'quarantined' && !privacyBarriers.has(partitionKey(binding))) throw failure('CACHE_NOT_QUARANTINED');
      let verified;
      try { verified = identity(options.reverifyIdentity()); }
      catch (_) { throw failure('REVERIFICATION_REQUIRED'); }
      if (!same(binding, verified)) throw failure('IDENTITY_CHANGED');
      detachedGuard(binding, expectedEpoch);
      if (readRaw(partitionKey(binding) + ':meta') !== meta.raw) throw failure('CACHE_GENERATION_CHANGED');
      detachedGuard(binding, expectedEpoch);
      // Re-verification never revives the removed cache, even if revision is equal.
      removeRaw(partitionKey(binding) + ':data');
      detachedGuard(binding, expectedEpoch);
      const key = partitionKey(binding) + ':meta';
      const raw = nextMeta(binding, meta, 'active');
      writeRaw(key, raw);
      try {
        detachedGuard(binding, expectedEpoch);
        if (readRaw(key) !== raw) throw failure('CACHE_GENERATION_CHANGED');
        detachedGuard(binding, expectedEpoch);
      } catch (error) {
        rollbackIfUnchanged(key, raw, meta.raw);
        detach();
        throw error;
      }
      privacyBarriers.delete(partitionKey(binding));
      return issue(binding, meta.generation + 1);
    }
    function inspect() {
      return Object.freeze({
        attached: current !== null,
        epoch,
        memory_slots: memory.size,
        registered_user_id: current ? current.registered_user_id : null,
        workspace_id: current ? current.workspace_id : null,
        privacy_revision: current ? current.privacy_revision : null
      });
    }
    return Object.freeze({
      attach: () => run(attach),
      read: (lease, slot) => run(() => read(lease, slot)),
      write: (lease, slot, value) => run(() => write(lease, slot, value)),
      detach, logout: detach,
      purgeCache: lease => run(() => purgeCache(lease)),
      quarantinePrivacy: lease => run(() => quarantinePrivacy(lease)),
      resumeAfterServerVerification: () => run(resumeAfterServerVerification), inspect
    });
  }
  return Object.freeze({ status: 'PROVISIONAL_NOT_ADOPTED', create });
})();

/**
 * Navren — localStorage 读写封装
 * 统一管理所有本地存储的键值读写与默认值
 */

const STORAGE_KEYS = {
  MODE: 'qingzhou_mode',
  THEME: 'navren_theme',
  LANGUAGE: 'navren_language',
  FONT_SIZE: 'qingzhou_fontSize',
  MODE_FONT_SIZES: 'qingzhou_modeFontSizes',
  VOICE_ENABLED: 'qingzhou_voiceEnabled',
  VOICE_PRESET: 'qingzhou_voicePreset',
  RISK_PROFILE: 'qingzhou_riskProfile',
  USER_INFO: 'qingzhou_userInfo',
  USER_PROFILE: 'qingzhou_userProfile',
  PREFERENCES: 'qingzhou_preferences',
  CHAT_HISTORY: 'qingzhou_chatHistory',
  PROFILE_HISTORY: 'qingzhou_profileHistory',
  CHAT_KEY_MOMENTS: 'qingzhou_chatKeyMoments',
  ALLOCATION: 'qingzhou_allocation',
  PRIVACY_READ: 'qingzhou_privacyRead',
  VOICE_GUIDE_READ: 'qingzhou_voiceGuideRead'
};

const DEFAULTS = {
  [STORAGE_KEYS.THEME]: {id:'light'},
  [STORAGE_KEYS.LANGUAGE]: 'zh-CN',
  [STORAGE_KEYS.MODE]: 'classic',
  [STORAGE_KEYS.FONT_SIZE]: 'medium',
  [STORAGE_KEYS.VOICE_ENABLED]: false,
  [STORAGE_KEYS.PREFERENCES]: {
    recommendByRisk: true,
    recommendByHorizon: true,
    marketHotPush: false,
    marketHotVisible: true,
    weeklyReport: true
  }
};

const Storage = (() => {
  // Only explicitly inventoried literal private keys enter this partition.
  const privateSlots = new Map([
    ['qingzhou_chatHistory', 'chat-history'],
    ['qingzhou_chatKeyMoments', 'chat-key-moments'],
    ['qingzhou_riskProfile', 'risk-profile'],
    ['qingzhou_userProfile', 'user-profile'],
    ['qingzhou_profileHistory', 'profile-history'],
    ['qingzhou_userInfo', 'user-info'],
    ['qingzhou_followupMemory', 'followup-memory'],
    ['qingzhou_holdings', 'holdings'],
    ['qingzhou_lifeStages', 'life-stages'],
    ['qingzhou_allocation', 'allocation'],
    ['navren_legacy_plan_workspace', 'legacy-plan-workspace'],
    ['qingzhou_bookmarks', 'bookmarks'],
    ['navren_reading_history', 'reading-history'],
    ['qingzhou_handoffPackage', 'handoff-package'],
    ['qingzhou_lastVisit', 'last-visit'],
    ['qingzhou_accountPhone', 'account-phone'],
    ['qingzhou_accountEmail', 'account-email'],
    ['qingzhou_preferences', 'preferences']
  ]);
  const accountKeys = new Set(['navren_theme','navren_language','qingzhou_mode','qingzhou_fontSize','qingzhou_modeFontSizes','qingzhou_voiceEnabled','qingzhou_voicePreset','qingzhou_preferences','navren_insurancePreferences','qingzhou_bookmarks','navren_reading_history','qingzhou_privacyRead','qingzhou_voiceGuideRead','navren_onboarded','qingzhou_tipSeen_chat','qingzhou_tipSeen_recommend','qingzhou_tipSeen_account','qingzhou_tipSeen_mine']);
  const deviceKeys = new Set([
    ...Object.values(STORAGE_KEYS).filter(key => !privateSlots.has(key)),
    'navren_back_gesture_until', 'navren_onboarded',
    'qingzhou_tipSeen_chat', 'qingzhou_tipSeen_recommend',
    'qingzhou_tipSeen_account', 'qingzhou_tipSeen_mine'
  ]);
  const fields = ['registered_user_id', 'workspace_id', 'privacy_revision', 'auth_subject'];
  let cache = null, lease = null, binding = null;
  function error(code) { const e = new Error(code); e.code = code; return e; }
  function privateIdentity() {
    try {
      const value = typeof BankClient !== 'undefined' ? BankClient.identity() : null;
      if (!value || typeof value.registered_user_id !== 'string' || !/^[0-9a-f]{32}$/.test(value.registered_user_id) ||
          typeof value.workspace_id !== 'string' || !/^[0-9a-f]{32}$/.test(value.workspace_id) ||
          !Number.isSafeInteger(value.privacy_revision) || value.privacy_revision < 0 ||
          typeof value.auth_subject !== 'string' || !/^[A-Za-z0-9:_-]{16,192}$/.test(value.auth_subject)) return null;
      return Object.freeze(Object.fromEntries(fields.map(key => [key, value[key]])));
    } catch (_) { return null; }
  }
  function unloadPrivateContext() {
    if (cache) cache.detach();
    lease = binding = null;
  }
  function context() {
    const next = privateIdentity();
    if (!next) { unloadPrivateContext(); throw error('UNBOUND_IDENTITY'); }
    if (!cache) cache = NavrenPrivateStorageCache.create({
      storage: {
        getItem: key => localStorage.getItem(key),
        setItem: (key, value) => localStorage.setItem(key, value),
        removeItem: key => localStorage.removeItem(key)
      },
      identityProvider: privateIdentity
    });
    if (!lease || !binding || fields.some(key => binding[key] !== next[key])) {
      unloadPrivateContext();
      lease = cache.attach(); binding = next;
    }
    return lease;
  }
  function privateWrite(key, value) {
    try { cacheOrContextWrite(key, value); }
    catch (e) { unloadPrivateContext(); throw e; }
  }
  function cacheOrContextWrite(key, value) {
    const held = context();
    cache.write(held, privateSlots.get(key), value);
  }
  function invalidatePrivateCache() {
    try { const held = context(); cache.purgeCache(held); }
    finally { unloadPrivateContext(); }
  }
  return {
    privateIdentity, unloadPrivateContext, invalidatePrivateCache,
    accountReady: () => window.NavrenAccountData?.readyState() === true,
    accountStatus: () => window.NavrenAccountData?.status() || 'unbound',
    ready: options => window.NavrenAccountData.ensure(options),
    setMany: values => window.NavrenAccountData.setMany(values),
    update: (key, mutate) => window.NavrenAccountData.update(key, mutate),
    legacyOwned(key) {
      if (!privateSlots.has(key)) return null;
      try { const held = context(); return cache.read(held, privateSlots.get(key)); } catch (_) { return null; }
    },
    get(key) {
      if (accountKeys.has(key)) return window.NavrenAccountData?.get(key) ?? null;
      if (privateSlots.has(key)) {
        try { const held = context(); return cache.read(held, privateSlots.get(key)); }
        catch (_) { unloadPrivateContext(); return null; }
      }
      if (!deviceKeys.has(key)) return null;
      try {
        const raw = localStorage.getItem(key);
        return raw ? JSON.parse(raw) : (DEFAULTS[key] ?? null);
      } catch { return DEFAULTS[key] ?? null; }
    },
    set(key, value) {
      if (accountKeys.has(key)) return window.NavrenAccountData.setMany({[key]: value});
      if (privateSlots.has(key)) { privateWrite(key, value); return; }
      if (!deviceKeys.has(key)) throw error('UNCLASSIFIED_STORAGE_KEY');
      try { localStorage.setItem(key, JSON.stringify(value)); }
      catch (e) { console.warn('localStorage write failed:', key, e); }
    },
    remove(key) {
      if (accountKeys.has(key)) throw error('ACCOUNT_STATE_EXPLICIT_VALUE_REQUIRED');
      if (privateSlots.has(key)) { privateWrite(key, null); return; }
      if (!deviceKeys.has(key)) throw error('UNCLASSIFIED_STORAGE_KEY');
      localStorage.removeItem(key);
    },
    /** Only the active private cache plus the original device settings reset. */
    clearAll() {
      invalidatePrivateCache();
      Object.values(STORAGE_KEYS).filter(key => deviceKeys.has(key) && !accountKeys.has(key)).forEach(key => localStorage.removeItem(key));
    },
    /** Logout unloads memory; all persistent partitions and device settings stay. */
    logout() { unloadPrivateContext(); window.NavrenAccountData?.invalidate(); },
    getProfileHistory() { return this.get(STORAGE_KEYS.PROFILE_HISTORY) || []; },
    addProfileHistory(record) {
      const history = this.getProfileHistory(); history.push(record);
      this.set(STORAGE_KEYS.PROFILE_HISTORY, history);
    },
    getKeyMoments() { return this.get(STORAGE_KEYS.CHAT_KEY_MOMENTS) || []; },
    addKeyMoment(event) {
      const moments = this.getKeyMoments(); moments.push({ time: new Date().toISOString(), event });
      this.set(STORAGE_KEYS.CHAT_KEY_MOMENTS, moments);
    }
  };
})();

/** 账号状态只从已验证 Cookie 会话读取；本地信号只通知重读，不携带个人内容。 */
window.NavrenAccountData = (() => {
  const contract = 'navren.account-state/1', path = '/account-state';
  const settings = {navren_theme:'theme',navren_language:'language',qingzhou_mode:'mode',qingzhou_fontSize:'font_size',qingzhou_modeFontSizes:'mode_font_sizes',qingzhou_voiceEnabled:'voice_enabled',qingzhou_voicePreset:'voice_preset'};
  const markers = {navren_onboarded:'onboarded',qingzhou_privacyRead:'privacy_read',qingzhou_voiceGuideRead:'voice_guide_read'};
  const bindingFields = ['registered_user_id','workspace_id','privacy_revision','auth_subject','session_generation','csrf','auth_expires_at','lease_expires_at'];
  const sections = ['profile','settings','preferences','insurance','favorites','reading','markers'];
  const clone = value => value == null ? value : JSON.parse(JSON.stringify(value));
  const stable = value => value && typeof value === 'object' ? Array.isArray(value) ? '['+value.map(stable).join(',')+']' : '{'+Object.keys(value).sort().map(key=>JSON.stringify(key)+':'+stable(value[key])).join(',')+'}' : JSON.stringify(value);
  const failure = code => Object.assign(new Error(code === 'ACCOUNT_STATE_VERSION_CONFLICT' ? '资料已在其他页面更新，请重新读取后再修改。' : '账号资料尚未核实保存，请重试。'), {code});
  let snapshot = null, binding = null, epoch = 0, flight = null, writing = false, hydrating = false, hidden = false, status = 'unbound', signalOwner = null, signalRevision = -1;
  const channel = typeof BroadcastChannel === 'function' ? new BroadcastChannel('navren-account-state-v1') : null;
  const signalKey = owner => 'navren:account-state:v1:' + owner;
  function current(identity) {
    const now = window.BankClient?.identity?.();
    return !!identity && !!now && bindingFields.every(key => identity[key] === now[key]) &&
      Number.isFinite(now.lease_expires_at) && Date.now()/1000 < now.lease_expires_at;
  }
  function readyState() { return !hidden && !hydrating && !!snapshot && current(binding); }
  function notify(next, keys = []) { status=next; window.dispatchEvent(new CustomEvent('navren:account-state', {detail:{status,keys}})); }
  function invalidate() {
    epoch++; snapshot = binding = null; flight = null; hydrating = false; writing = false;
    signalOwner = null; signalRevision = -1;
    notify('unbound');
  }
  function validate(data, identity) {
    if (!data || data.contract !== contract || data.registered_user_id !== identity.registered_user_id ||
      data.workspace_binding !== identity.workspace_id || data.persona_id !== identity.account_id ||
      !Number.isSafeInteger(data.revision) || data.revision < 0 || !Array.isArray(data.initialized_sections) ||
      data.initialized_sections.some(key => !sections.includes(key)) || !data.sections || sections.some(key => !Object.hasOwn(data.sections,key))) throw failure('UNBOUND_ACCOUNT_STATE');
    const s=data.sections, p=s.settings, m=s.markers;
    if (!p || !['light','dark'].includes(p.theme) || !['zh-CN','en'].includes(p.language) || !['classic','senior','youth'].includes(p.mode) ||
      fontSizeInPixels(p.font_size) === null || !p.mode_font_sizes || Object.entries(p.mode_font_sizes).some(([key,value])=>!['classic','senior','youth'].includes(key)||fontSizeInPixels(value)===null) ||
      typeof p.voice_enabled !== 'boolean' || !['female_sweet','male_warm','male_podcast'].includes(p.voice_preset) ||
      !s.preferences || ['recommendByRisk','recommendByHorizon','marketHotPush','marketHotVisible','weeklyReport'].some(key=>typeof s.preferences[key] !== 'boolean') ||
      !s.insurance || !Array.isArray(s.insurance.topics) || !Array.isArray(s.favorites) || !Array.isArray(s.reading) ||
      !m || ['onboarded','privacy_read','voice_guide_read'].some(key=>typeof m[key] !== 'boolean') || !m.tips || ['chat','recommend','account','mine'].some(key=>typeof m.tips[key] !== 'boolean')) throw failure('INVALID_ACCOUNT_STATE');
    if (s.favorites.some(item => !['market_news','market_weekly'].includes(item.category) && item.workspace_id !== identity.workspace_id)) throw failure('UNBOUND_ACCOUNT_FAVORITE');
    return clone(data);
  }
  function assertCurrent(token, identity) { if (hidden || token !== epoch || !current(identity)) throw failure('STALE_ACCOUNT_STATE'); }
  function publish() {
    const message={registered_user_id:binding.registered_user_id,revision:snapshot.revision,nonce:crypto.randomUUID()};
    try { localStorage.setItem(signalKey(message.registered_user_id), JSON.stringify(message)); } catch (_) {}
    channel?.postMessage(message);
  }
  async function postSection(section, value, operation, token, identity) {
    assertCurrent(token,identity);
    const expected = snapshot.revision;
    const payload={expected_revision:expected,section,value:clone(value)};
    if (operation === 'migrate') Object.assign(payload,{operation,source:{kind:'owned_browser_cache',workspace_id:identity.workspace_id,auth_subject:identity.auth_subject,privacy_revision:identity.privacy_revision}});
    const saved = validate(await BankClient.post(path,payload),identity);
    assertCurrent(token,identity);
    if (saved.revision <= expected) throw failure('UNCONFIRMED_ACCOUNT_STATE');
    // 读回持久快照；确认丢失或并发覆盖时不回显“保存成功”。
    const read = validate(await BankClient.get(path),identity);
    assertCurrent(token,identity);
    if (read.revision < saved.revision) throw failure('STALE_ACCOUNT_STATE');
    snapshot=read; binding={...identity};
    if (stable(read.sections[section]) !== stable(saved.sections[section])) throw failure('ACCOUNT_STATE_VERSION_CONFLICT');
    return read;
  }
  async function migrateOwned(token, identity) {
    const source = Storage.privateIdentity();
    if (!source || ['registered_user_id','workspace_id','privacy_revision','auth_subject'].some(key=>source[key]!==identity[key])) return;
    const candidates = {};
    const prefs=Storage.legacyOwned('qingzhou_preferences');
    if (prefs && !Array.isArray(prefs) && Object.keys(prefs).length && Object.entries(prefs).every(([key,value])=>Object.hasOwn(snapshot.sections.preferences,key)&&typeof value==='boolean')) candidates.preferences={...snapshot.sections.preferences,...prefs};
    const favorites=Storage.legacyOwned('qingzhou_bookmarks');
    const favoriteFields=new Set('category kind object_kind object_id id text source url published_at version archive timestamp time_view source_scope workspace_id privacy_revision request_id message_id account_id personal_revision auth_subject'.split(' '));
    if (Array.isArray(favorites)) {
      const rows=favorites.filter(item=>item&&typeof item==='object'&&(item.registered_user_id==null||item.registered_user_id===identity.registered_user_id)&&Object.keys(item).every(key=>key==='registered_user_id'||favoriteFields.has(key))&&(['market_news','market_weekly'].includes(item.category)||item.workspace_id===identity.workspace_id&&Number.isSafeInteger(item.privacy_revision)&&item.privacy_revision>=0)).map(item=>Object.fromEntries(Object.entries(item).filter(([key])=>favoriteFields.has(key))));
      if (rows.length && rows.length<=500) candidates.favorites=rows;
    }
    const reading=Storage.legacyOwned('navren_reading_history');
    const readingFields=new Set('kind id version source_scope product_source archive label updated_at time_view object_kind object_id'.split(' '));
    if (Array.isArray(reading)) {
      const rows=reading.filter(item=>item&&['public','market','public_market'].includes(item.source_scope)).map(item=>Object.fromEntries(Object.entries(item).filter(([key])=>readingFields.has(key))));
      if (rows.length && rows.length<=200) candidates.reading=rows;
    }
    for (const [section,value] of Object.entries(candidates)) {
      assertCurrent(token,identity);
      if (snapshot.initialized_sections.includes(section)) continue;
      try { await postSection(section,value,'migrate',token,identity); publish(); }
      catch (error) {
        assertCurrent(token,identity);
        if (!['ACCOUNT_STATE_ALREADY_INITIALIZED','ACCOUNT_STATE_VERSION_CONFLICT'].includes(error.code)) throw error;
        snapshot=validate(await BankClient.get(path),identity); assertCurrent(token,identity);
      }
    }
  }
  async function ensure({refresh=false}={}) {
    if (hidden) throw failure('ACCOUNT_STATE_UNAVAILABLE');
    if (!refresh && readyState()) return clone(snapshot);
    if (flight) return flight;
    const token=epoch;
    if (!snapshot) { hydrating=true; notify('loading'); }
    // 下一微任务再进入，保证同步依赖错误发生前 flight 已完成赋值。
    const task=Promise.resolve().then(async()=>{
      try {
        const identity={...await BankClient.ensure()}; assertCurrent(token,identity);
        let read;
        try { read=await BankClient.get(path); }
        catch (error) {
          // 其他页面可能在快照发布前更新资料；仅同一有效绑定内补读一次。
          assertCurrent(token,identity);
          if (error.status!==409 || error.code!=='ACCOUNT_STATE_VERSION_CONFLICT') throw error;
          read=await BankClient.get(path);
        }
        const data=validate(read,identity); assertCurrent(token,identity);
        if (snapshot && binding?.registered_user_id===identity.registered_user_id && data.revision<snapshot.revision) throw failure('STALE_ACCOUNT_STATE');
        snapshot=data; binding=identity;
        await migrateOwned(token,identity); assertCurrent(token,identity);
        hydrating=false; notify('ready'); return clone(snapshot);
      } catch (error) {
        if (token===epoch) {
          hydrating=false;
          if (!snapshot || !current(binding)) { snapshot=binding=null; notify('unavailable'); }
        }
        throw error;
      } finally { if (flight===task) flight=null; followSignal(token); }
    });
    flight=task; return task;
  }
  function sectionFor(key) {
    if (Object.hasOwn(settings,key)) return 'settings';
    if (Object.hasOwn(markers,key)||key.startsWith('qingzhou_tipSeen_')) return 'markers';
    return ({qingzhou_preferences:'preferences',navren_insurancePreferences:'insurance',qingzhou_bookmarks:'favorites',navren_reading_history:'reading'})[key];
  }
  function get(key) {
    if (!readyState()) return null;
    const s=snapshot.sections;
    if (Object.hasOwn(settings,key)) return clone(key==='navren_theme'?{id:s.settings.theme}:s.settings[settings[key]]);
    if (Object.hasOwn(markers,key)) return s.markers[markers[key]];
    if (key.startsWith('qingzhou_tipSeen_')) return s.markers.tips[key.slice(17)] ?? null;
    if (key==='navren_insurancePreferences') return clone(s.insurance.topics);
    return clone(s[sectionFor(key)] ?? null);
  }
  async function setMany(values) {
    if (!readyState()) throw failure('ACCOUNT_STATE_NOT_READY');
    if (writing) throw failure('ACCOUNT_STATE_BUSY');
    const keys=Object.keys(values), selected=new Set(keys.map(sectionFor));
    if (!keys.length || selected.size!==1 || selected.has(undefined)) throw failure('ACCOUNT_STATE_INVALID');
    const section=[...selected][0], identity={...binding}, token=epoch;
    let value=clone(snapshot.sections[section]);
    for (const [key,item] of Object.entries(values)) {
      if (section==='settings') value[settings[key]]=key==='navren_theme'?item?.id:clone(item);
      else if (Object.hasOwn(markers,key)) value[markers[key]]=item;
      else if (key.startsWith('qingzhou_tipSeen_')) value.tips[key.slice(17)]=item;
      else value=key==='navren_insurancePreferences'?{topics:clone(item)}:clone(item);
    }
    writing=true;
    try { await postSection(section,value,'replace',token,identity); notify('ready',keys); publish(); return true; }
    catch (error) {
      if (token===epoch && current(identity) && ['ACCOUNT_STATE_VERSION_CONFLICT','ACCOUNT_STATE_ALREADY_INITIALIZED'].includes(error.code)) {
        try { await ensure({refresh:true}); } catch (_) {}
      }
      throw error;
    } finally { if (token===epoch) { writing=false; followSignal(token); } }
  }
  function update(key, mutate) {
    try { if (!readyState()) throw failure('ACCOUNT_STATE_NOT_READY'); return setMany({[key]:mutate(get(key))}); }
    catch (error) { return Promise.reject(error); }
  }
  function followSignal(token) {
    if (token!==epoch || hidden || flight || writing || signalRevision<0) return;
    const identity=window.BankClient?.identity?.();
    if (!identity || identity.registered_user_id!==signalOwner) return;
    if (snapshot && snapshot.revision>=signalRevision) { signalOwner=null; signalRevision=-1; return; }
    // 每次信号最多补一次读取；信号不能成为可信版本或无限重试依据。
    const owner=signalOwner; signalOwner=null; signalRevision=-1;
    queueMicrotask(()=>{if(token===epoch&&!hidden&&window.BankClient?.identity?.()?.registered_user_id===owner)ensure({refresh:true}).catch(()=>{});});
  }
  function syncSignal(message) {
    const identity=window.BankClient?.identity?.();
    if (!identity || message?.registered_user_id!==identity.registered_user_id || !Number.isSafeInteger(message.revision) || message.revision<0 || snapshot&&message.revision<=snapshot.revision) return;
    signalOwner=identity.registered_user_id; signalRevision=Math.max(signalRevision,message.revision);
    followSignal(epoch);
  }
  if (channel) channel.onmessage=event=>syncSignal(event.data);
  window.addEventListener('storage',event=>{const owner=window.BankClient?.identity?.()?.registered_user_id;if (!owner || event.key!==signalKey(owner)) return; try{syncSignal(JSON.parse(event.newValue));}catch(_){} });
  for (const type of ['navren:privacy-reset','navren:identity-revoked','navren:account-change','navren:account-switched']) window.addEventListener(type,()=>{invalidate();if(type!=='navren:identity-revoked')queueMicrotask(()=>{if(!hidden&&window.BankClient?.identity?.())ensure().catch(()=>{});});});
  window.addEventListener('navren:business-identity',()=>{if(!hidden)ensure().catch(()=>{});});
  window.addEventListener('pagehide',()=>{hidden=true;invalidate();});
  window.addEventListener('pageshow',()=>{hidden=false;if(window.BankClient)ensure({refresh:true}).catch(()=>{});});
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible'&&!hidden&&window.BankClient)ensure({refresh:true}).catch(()=>{});});
  document.addEventListener('DOMContentLoaded',()=>{if(window.BankClient)ensure().catch(()=>{});});
  return Object.freeze({ensure,readyState,status:()=>status,get,setMany,update,invalidate,itemKey:stable});
})();

// ══════ 共享字体工具（chat.js & mine.js 共用，消除重复定义） ══════
const FONT_SIZE_MAP = { small:'15px', medium:'16px', large:'20px' };
const FONT_SIZE_MIN = 14;
const FONT_SIZE_MAX = 24;
const MODE_DEFAULT_FONT = { classic:'medium', senior:'large', youth:'small' };

function fontSizeInPixels(size) {
  const pixels = typeof size === 'string' && Object.hasOwn(FONT_SIZE_MAP, size)
    ? parseInt(FONT_SIZE_MAP[size], 10)
    : typeof size === 'number' || typeof size === 'string' && /^\d{2}$/.test(size) ? Number(size) : NaN;
  return Number.isInteger(pixels) && pixels >= FONT_SIZE_MIN && pixels <= FONT_SIZE_MAX ? pixels : null;
}

/** 模式和字号统一入口；每个模式保留自己的字号选择。 */
function getInterfaceMode() {
  const candidate = Storage.get(STORAGE_KEYS.MODE);
  return Object.hasOwn(MODE_DEFAULT_FONT, candidate) ? candidate : 'classic';
}
function getModeFont(mode = getInterfaceMode()) {
  const sizes = Storage.get('qingzhou_modeFontSizes') || {};
  if (fontSizeInPixels(sizes[mode]) !== null) return sizes[mode];
  const currentSize = Storage.get(STORAGE_KEYS.FONT_SIZE);
  if (mode === Storage.get(STORAGE_KEYS.MODE) && fontSizeInPixels(currentSize) !== null) return currentSize;
  return MODE_DEFAULT_FONT[mode] || 'medium';
}
async function setModeFont(mode, size) {
  const pixels = fontSizeInPixels(size);
  if (!Object.hasOwn(MODE_DEFAULT_FONT, mode) || pixels === null) return false;
  const saved = typeof size === 'string' && Object.hasOwn(FONT_SIZE_MAP, size) ? size : pixels;
  await Storage.setMany({qingzhou_modeFontSizes: {...(Storage.get('qingzhou_modeFontSizes') || {}), [mode]: saved}, [STORAGE_KEYS.FONT_SIZE]: saved});
  return fontSizeInPixels(getModeFont(mode)) === pixels;
}

function applyFontSize(size) {
  const pxi = fontSizeInPixels(size) ?? 16;
  const px = pxi + 'px';
  document.documentElement.style.fontSize = px;
  // chat.html 使用的变量名
  document.documentElement.style.setProperty('--font-size-base', px);
  document.documentElement.style.setProperty('--font-size-msg', px);
  document.documentElement.style.setProperty('--font-size-sm', (pxi-1)+'px');
  document.documentElement.style.setProperty('--font-size-xs', Math.max(13,pxi-3)+'px');
  document.documentElement.style.setProperty('--font-size-lg', (pxi+4)+'px');
  document.documentElement.style.setProperty('--font-size-xl', (pxi+10)+'px');
  // mine.html 使用的变量名
  document.documentElement.style.setProperty('--fs-base', px);
  document.documentElement.style.setProperty('--fs-sm', (pxi-1)+'px');
  document.documentElement.style.setProperty('--fs-lg', (pxi+4)+'px');
  document.documentElement.style.setProperty('--fs-xl', (pxi+10)+'px');
  window.dispatchEvent(new CustomEvent('navren:font-size',{detail:{pixels:pxi}}));
}

// 百炼 Qwen-Audio-3.1 系统音色；沿用旧偏好 ID，保持已有选择。
const VOICE_PRESETS = [
  { id: 'female_sweet', name: '知性女声', icon: '🎙️', desc: '龙安温 · 清晰平和', rate: 1.0, cloudVoice: 'longanwen_v3.1' },
  { id: 'male_warm', name: '温暖男声', icon: '🎧', desc: '龙安洋 · 自然明朗', rate: 1.0, cloudVoice: 'longanyang_v3.1' },
  { id: 'male_podcast', name: '沉稳男声', icon: '🎵', desc: '龙三叔 · 沉稳质感', rate: 1.0, cloudVoice: 'longsanshu_v3.1' }
];

function getVoicePreset() {
  const id = Storage.get(STORAGE_KEYS.VOICE_PRESET) || 'female_sweet';
  return VOICE_PRESETS.find(v => v.id === id) || VOICE_PRESETS[0];
}

/** 异步加载系统语音列表（voices 在首次调用后才异步就绪） */
let _cachedVoices = null;
function loadVoices() {
  if (_cachedVoices && _cachedVoices.length > 0) return Promise.resolve(_cachedVoices);
  return new Promise((resolve) => {
    if (typeof speechSynthesis === 'undefined') { resolve([]); return; }
    const voices = speechSynthesis.getVoices();
    if (voices && voices.length > 0) {
      _cachedVoices = voices;
      resolve(voices);
      return;
    }
    speechSynthesis.onvoiceschanged = () => {
      _cachedVoices = speechSynthesis.getVoices();
      resolve(_cachedVoices);
    };
    // Timeout fallback
    setTimeout(() => {
      if (!_cachedVoices) { _cachedVoices = speechSynthesis.getVoices(); }
      resolve(_cachedVoices);
    }, 2000);
  });
}

/** 根据预设找到最匹配的系统语音 */
function matchVoice(preset, voices) {
  if (!voices || voices.length === 0) return null;
  const zhVoices = voices.filter(v => v.lang === 'zh-CN');
  if (zhVoices.length === 0) return null;

  // 精确匹配预设指定的语音名
  if (preset.voiceName) {
    const exact = zhVoices.find(v => v.name === preset.voiceName);
    if (exact) return exact;
    const partial = zhVoices.find(v => v.name.includes(preset.voiceName));
    if (partial) return partial;
  }

  // 回退：第一个zh-CN语音
  return zhVoices[0];
}

// ── 全局键盘快捷键 ──
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    // 关闭所有打开的弹窗
    document.querySelectorAll('.modal-overlay:not(.hidden)').forEach(m => m.classList.add('hidden'));
  }
});

/** 场景引导提示：首次访问页面时显示 */
async function showContextualTip(page) {
  const key = 'qingzhou_tipSeen_' + page;
  if (!Storage.accountReady() || Storage.get(key)) return;

  const tips = {
    chat: '💡 试试点击预设问题快速开始，或直接说出您的理财需求。左侧图标可切换推荐/账户/我的。',
    recommend: '💡 这里展示配置方案和市场动态。在「我的」中可开关市场热点和周报。',
    account: '查看资产、持仓和账单，或管理银行代扣与资金设置。',
    mine: '可在这里调整阅读方式，也可以补充个人档案。蓝色边框表示已填写。'
  };

  const msg = tips[page];
  if (!msg) return;

  try { await Storage.set(key, true); } catch (_) { return; }
  if (!Storage.accountReady()) return;
  const toast = document.createElement('div');
  toast.className = 'toast';
  toast.style.cssText = 'bottom:100px;max-width:360px;text-align:center;line-height:1.6;font-size:12px';
  toast.textContent = msg;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 6000);
}

// ══════ 跨页面共享工具函数 ══════

/** 带模式参数的页面跳转 */
function goTo(url) {
  const mode = Storage.get('qingzhou_mode') || 'classic';
  NavrenApp.navigate(url, mode);
}

/** Toast 消息提示 */
function showToast(msg) {
  const existing = document.querySelector('.toast');
  if (existing) existing.remove();
  const toast = document.createElement('div');
  toast.className = 'toast';
  toast.textContent = msg;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 3000);
  return toast;
}

/** HTML 转义 */
function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

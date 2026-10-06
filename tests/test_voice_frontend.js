const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'js', 'chat.js'), 'utf8');

function chatContext() {
  const calls = { finish: 0, stop: 0, send: 0, inputEvents: 0 };
  const button = {
    innerHTML: '<svg></svg>', dataset: {}, title: '',
    classList: { toggle() {}, remove() {}, add() {} },
    setAttribute() {}, removeAttribute() {},
  };
  const status = { textContent: '', classList: { toggle() {} } };
  const input = { value: '', dispatchEvent() { calls.inputEvents++; } };
  const document = {
    addEventListener() {},
    getElementById(id) {
      return id === 'voiceBtn' ? button : id === 'voiceStatus' ? status : id === 'userInput' ? input : null;
    },
  };
  const context = vm.createContext({
    document,
    window: { addEventListener() {} },
    Event: class Event {},
    clearTimeout() {},
    NavrenSpeech: {
      activeKind: 'asr',
      startASR(options) {
        options.onState('connecting');
        options.onState('listening');
        options.onTranscript('请帮我查询模拟账户余额。', false);
        options.onTranscript('请帮我查询模拟账户余额。', true);
        return Promise.resolve('请帮我查询模拟账户余额。');
      },
      finishRecording() {
        calls.finish++;
        vm.runInContext("voiceButton('finishing')", context);
      },
      stop() { calls.stop++; },
    },
  });
  vm.runInContext(source, context, { filename: 'js/chat.js' });
  context.sendMessage = () => { calls.send++; };
  return { calls, button, input, context };
}

test('clicking while final ASR text is being finalized cancels the voice turn', async () => {
  const { calls, button, context } = chatContext();
  vm.runInContext("voiceButton('finishing')", context);

  await vm.runInContext('toggleVoice()', context);

  assert.equal(calls.stop, 1);
  assert.equal(calls.finish, 0);
  assert.equal(vm.runInContext('isListening', context), false);
  assert.equal(button.title, '语音输入');
});

test('clicking while listening finishes recording instead of cancelling', async () => {
  const { calls, button, context } = chatContext();
  vm.runInContext("voiceButton('listening')", context);

  await vm.runInContext('toggleVoice()', context);

  assert.equal(calls.finish, 1);
  assert.equal(calls.stop, 0);
  assert.equal(vm.runInContext('voiceInputState', context), 'finishing');
  assert.equal(button.title, '正在整理语音内容，点击取消；Esc 取消');
});

test('clicking while connecting cancels the pending voice session', async () => {
  const { calls, button, context } = chatContext();
  vm.runInContext("voiceButton('connecting')", context);

  await vm.runInContext('toggleVoice()', context);

  assert.equal(calls.stop, 1);
  assert.equal(calls.finish, 0);
  assert.equal(vm.runInContext('isListening', context), false);
  assert.equal(button.title, '语音输入');
});

test('final ASR text is inserted into the composer and sent through the normal chat action', async () => {
  const { calls, input, context } = chatContext();

  await vm.runInContext('toggleVoice()', context);

  assert.equal(input.value, '请帮我查询模拟账户余额。');
  assert.equal(calls.inputEvents, 2);
  assert.equal(calls.send, 1);
  assert.equal(vm.runInContext('voiceInputState', context), 'idle');
});

// Minimal DOM for exercising real event handlers without a browser or network.
class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.dataset = {}; this.listeners = {};
    this.classList = { toggle() {}, remove() {}, add() {} };
    this.value = ''; this.textContent = ''; this.className = '';
  }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.append(child); }
  replaceChildren(...children) { this.children = children; }
  setAttribute() {}
  removeAttribute() {}
  addEventListener(name, handler) { this.listeners[name] = handler; }
  focus() {}
  remove() {}
  querySelector(selector) {
    const nodes = this.children.flatMap(child => [child, ...child.descendants()]);
    return nodes.find(child => selector.startsWith('.')
      ? child.className.split(' ').includes(selector.slice(1)) : child.tagName === selector) || null;
  }
  descendants() { return this.children.flatMap(child => [child, ...child.descendants()]); }
}

function dom() {
  const elements = new Map();
  const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  return { addEventListener() {}, createElement: tag => new Element(tag),
    getElementById: get, querySelector: get, querySelectorAll: () => [] };
}

test('reply playback passes its server speech receipt to the shared controller', async () => {
  const { context } = chatContext();
  const control = new Element('button'); control.className = 'message-audio-control';
  const message = new Element(); message.dataset.speechReceipt = 'server-receipt'; message.append(control);
  const playback = [];
  context.Storage = { get: () => true };
  context.getVoicePreset = () => ({ id: 'female_sweet', rate: 1 });
  context.NavrenSpeech.speak = (text, options) => { playback.push({ text, options }); return Promise.resolve(); };
  await context.speakMessage(message, '请核对这项草案。');
  assert.equal(playback[0].options.speechReceipt, 'server-receipt');
  assert.equal(playback[0].text, '请核对这项草案。');
});

test('candidate confirmation sends only the source binding captured when rendered', async () => {
  const { context } = chatContext();
  context.document = dom(); context.showToast = () => {};
  const requests = [];
  context.BankClient = { post: async (path, payload) => { requests.push({ path, payload }); } };
  const message = new Element(), bubble = new Element(); bubble.className = 'bubble'; message.append(bubble);
  const proposal = { proposal_id: 'proposal-1', source_ref: 'source-1', confirmation_token: 'token-1',
    expected_revision: 7, category: 'goal', value: '准备明年的学费', source_type: 'conversation_user_statement' };
  context.renderMemoryProposals(message, [proposal]);
  proposal.expected_revision = 8; proposal.value = '客户端篡改文本';
  const save = bubble.descendants().find(node => node.textContent === '确认保存');
  await save.listeners.click();
  assert.equal(requests[0].path, '/memories');
  assert.deepEqual(JSON.parse(JSON.stringify(requests[0].payload)), {
    proposal_id: 'proposal-1', source_ref: 'source-1', confirmation_token: 'token-1', expected_revision: 7
  });
});

test('TTS session sends receipt and only fixed voice preview purpose without reply text', async () => {
  const sessionBodies = [];
  const context = vm.createContext({
    window: { addEventListener() {}, AudioContext: class {
      state = 'running'; resume() { return Promise.resolve(); } close() { return Promise.resolve(); }
    } }, document: dom(), DOMException, AbortController, setTimeout, clearTimeout,
    BankClient: { agentHeaders: async () => ({}) },
    fetch: async (_url, options) => {
      sessionBodies.push(JSON.parse(options.body));
      return { ok: false, json: async () => ({ error: { message: '测试拒绝，无网络请求' } }) };
    }
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'js', 'speech.js'), 'utf8'), context);
  await assert.rejects(context.window.NavrenSpeech.speak('私人账户原文', { speechReceipt: 'verified-speech' }), /测试拒绝/);
  await assert.rejects(context.window.NavrenSpeech.speak('任意客户端文字不能充当试听', { purpose: 'voice_preview' }), /测试拒绝/);
  await assert.rejects(context.window.NavrenSpeech.speak('伪造用途', { purpose: 'export_private' }), /测试拒绝/);
  assert.deepEqual(sessionBodies, [
    { kind: 'tts', speech_receipt: 'verified-speech' },
    { kind: 'tts', speech_receipt: '', purpose: 'voice_preview' },
    { kind: 'tts', speech_receipt: '' }
  ]);
});

test('unavailable local ASR rejects before opening media or creating an ASR session', async () => {
  const calls = { media: 0, audioContext: 0, session: 0, status: 0 };
  const context = vm.createContext({
    window: { addEventListener() {}, AudioContext: class { constructor() { calls.audioContext++; } } },
    document: dom(), DOMException, AbortController, setTimeout, clearTimeout,
    navigator: { mediaDevices: { getUserMedia: async () => { calls.media++; } } },
    BankClient: { agentHeaders: async () => ({}) },
    fetch: async url => {
      if (url === '/api/speech/status') { calls.status++; return { ok: true, json: async () => ({ configured: true, asr_available: false }) }; }
      calls.session++; throw Error('不应创建语音会话');
    }
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'js', 'speech.js'), 'utf8'), context);
  await assert.rejects(context.window.NavrenSpeech.startASR({}), /语音输入需要本地识别服务，目前请使用文字输入/);
  assert.deepEqual(calls, { media: 0, audioContext: 0, session: 0, status: 1 });
  assert.equal(context.window.NavrenSpeech.activeKind, null);
});

function mineContext() {
  const document = dom(), requests = [], listeners = new Map();
  const context = vm.createContext({ document, window: {
    addEventListener: (name, fn) => listeners.set(name, fn),
    dispatchEvent: event => listeners.get(event.type)?.(event)
  }, Event, confirm: () => true, closeModal() {}, showToast() {},
    BankClient: { post: async (path, payload) => {
      requests.push({ path, payload }); throw new Error('个人资料版本冲突');
    } } });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'js', 'mine.js'), 'utf8'), context);
  context.loadFinancialMemories = async () => {};
  vm.runInContext("financialMemoryState={enabled:false,personal_revision:7,items:[{node_id:'memory-1',category:'goal',value:'学费',relations:[{relation_id:'relation-1',relation:'related_to',value:'备用金'}]}]}", context);
  return { context, document, requests };
}

test('voice preset preview requests fixed preview purpose and retains the risk notice', async () => {
  const { context } = mineContext();
  const calls = [];
  context.VOICE_PRESETS = [{ id: 'female_sweet', rate: 1 }];
  context.getInterfaceMode = () => 'senior';
  context.NavrenSpeech = { speak: (text, options) => { calls.push({ text, options }); return Promise.resolve(); } };
  context.previewVoice('female_sweet');
  assert.equal(calls[0].options.purpose, 'voice_preview');
  assert.equal(calls[0].options.rate, 0.85);
  assert.match(calls[0].text, /理财有风险，请仔细阅读产品说明/);
});

test('rendered delete and relation actions keep their original personal revision on conflict', async () => {
  const { context, document, requests } = mineContext();
  context.renderFinancialMemories();
  const nodes = document.getElementById('financialMemoryList').descendants();
  const remove = nodes.find(node => node.textContent === '删除');
  const unlink = nodes.find(node => node.textContent === '解除');
  vm.runInContext('financialMemoryState.personal_revision=12', context);
  await remove.listeners.click(); await unlink.listeners.click();
  assert.equal(requests.length, 2);
  assert.equal(requests[0].payload.expected_revision, 7);
  assert.equal(requests[1].payload.expected_revision, 7);
});

test('an open memory editor keeps its revision when other personal data changes', async () => {
  const { context, document, requests } = mineContext();
  context.openFinancialMemoryEditor();
  document.getElementById('memoryCategory').value = 'goal';
  const textarea = document.getElementById('modalBody').descendants().find(node => node.tagName === 'textarea');
  textarea.value = '明年学费';
  vm.runInContext('financialMemoryState.personal_revision=12', context);
  await document.getElementById('modalConfirm').onclick();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].payload.expected_revision, 7);
});

test('memory consent captures revision before the privacy reset clears local state', async () => {
  const { context, requests } = mineContext();
  await context.window.toggleFinancialMemory();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, '/memories/settings');
  assert.equal(requests[0].payload.expected_revision, 7);
});

test('profile confirm and delete use rendered revision and only confirm sends a token', async () => {
  const document = dom(), requests = [], data = { personal_revision: 9, nodes: [
    { observation_id: 'observation-1', status: 'candidate', quote: '我明年需要学费', label: '学费', confirmation_token: 'bound-token' }
  ] };
  const context = vm.createContext({ document, window: { addEventListener() {} },
    confirm: () => true, closeModal() {}, showToast() {}, renderRiskProfile() {}, renderArchive() {},
    loadFinancialMemories: async () => {}, NavrenGraph: { pause() {}, invalidate() {} },
    BankClient: { ensure: async () => {}, guard: async () => ({ valid: () => true }),
      get: async path => path === '/overview' ? { profile: { risk_level: 2 } } : data,
      post: async (path, payload) => { requests.push({ path, payload }); throw Error('个人资料版本冲突'); }
    } });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'js', 'mine_workspace.js'), 'utf8'), context);
  await context.window.NavrenMine.render({ page: 'mine', view: 'profile' });
  const buttons = document.getElementById('mineObservationList').descendants();
  data.personal_revision = 15; data.nodes[0].confirmation_token = 'new-token';
  await buttons.find(node => node.textContent === '确认是我的陈述').onclick();
  await buttons.find(node => node.textContent === '删除这项资料').onclick();
  assert.deepEqual(JSON.parse(JSON.stringify(requests.map(r => r.payload))), [
    { decision: 'confirm', expected_revision: 9, confirmation_token: 'bound-token' },
    { decision: 'delete', expected_revision: 9 }
  ]);
});

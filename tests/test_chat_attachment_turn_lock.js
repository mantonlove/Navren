/* 真实sendMessage的异步回合锁测试：只用内存VM、原件夹具和可控Deferred。 */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const {File} = require('node:buffer');
const {webcrypto} = require('node:crypto');

const chatSource = fs.readFileSync(path.join(__dirname, '../js/chat.js'), 'utf8');
const start = chatSource.indexOf('async function sendMessage(');
const end = chatSource.indexOf('// 仅渲染本轮实际收到的活动', start);
assert(start >= 0 && end > start, '必须执行当前chat.js中的真实sendMessage函数');
const sendSource = chatSource.slice(start, end);

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
  return {promise, resolve, reject};
}

async function until(predicate, message) {
  for (let turn = 0; turn < 30; turn++) {
    if (predicate()) return;
    await new Promise(resolve => setImmediate(resolve));
  }
  assert.fail(message);
}

function fixture({holdUploads = false, reference = null} = {}) {
  let identity = {registered_user_id: '1'.repeat(32), workspace_id: 'a'.repeat(32), privacy_revision: 0};
  let selectedReference = reference;
  let staged = [new File(['A文档'], 'A.txt', {type: 'text/plain'})];
  let history = [];
  const input = {value: '概括A附件'};
  const api = [], tickets = [], uploads = [], rendered = [];
  const noOp = () => {};
  const context = {
    console, Promise, Object, JSON, Number, Boolean, crypto: webcrypto, setTimeout, clearTimeout,
    chatSending: false, chatArchiveReady: true, chatArchiveBusy: false, pendingVoiceNameReview: null,
    chatDraftRevision: 0, chatReferenceRevision: 0, chatRequestVersion: 0,
    chatAttachmentUploads: new WeakMap(), chatAttachmentUploadKeys: new WeakMap(),
    voiceResumeTimer: null, voiceRequest: 0, voiceConversation: false, currentMode: 'classic',
    NavrenChatPrivacyVersion: 0, NavrenSpeech: {stop: noOp}, NavrenPresentation: {text: value => value},
    BankClient: {
      identity: () => identity,
      guard: () => {const workspace = identity.workspace_id; return {valid: () => identity.workspace_id === workspace};},
      post: (url, body) => {const pending = deferred(); tickets.push({url, body, pending}); return pending.promise;}
    },
    document: {getElementById: () => input},
    currentChatReference: () => selectedReference,
    chatBinding: () => ({...identity}), chatReferencePacket: value => value,
    clearFollowUpQuestions: noOp, syncChatReference: noOp, clearVoiceNameReview: noOp,
    clearChatReference: () => {selectedReference = null;}, voiceButton: noOp,
    newChatMessageId: () => webcrypto.randomUUID(), updateChatSendState: noOp,
    chatUserMessageHtml: text => text,
    addMessage: (role, content) => {rendered.push({role, content}); return {dataset: {}, querySelector: () => null};},
    removeImage: () => {staged = [];},
    Storage: {set: (key, value) => {if (key === 'qingzhou_chatHistory') history = value;}, get: () => null},
    currentChatHistory: () => history.filter(row => row.workspace_id === identity.workspace_id),
    addLoadingDots: () => ({remove: noOp}), scrollToBottom: noOp,
    Api: {sendMessage: (text, mode, image, sentiment, progress, options) => {
      const pending = deferred(); api.push({text, options, pending}); return pending.promise;
    }},
    NavrenChatAttachments: {files: () => staged.slice(), matches: files => files.length === staged.length && files.every((file, i) => file === staged[i]),
      mountFiles: noOp, consume: sent => {staged = staged.filter(file => !sent.includes(file));}},
    NavrenChatMedia: {
      upload: file => {
        const pending = deferred();
        const media = {media_id: String(uploads.length + 1).padStart(64, '0'), kind: 'document', mime_type: file.type};
        uploads.push({file, media, pending}); if (!holdUploads) pending.resolve(media); return pending.promise;
      },
      validate: value => value
    },
    verifiedReplyProjection: () => null, renderReplyText: value => value, highlightProductNames: value => value,
    renderReplyInteraction: noOp, renderMemoryContextCard: noOp, renderMemoryProposals: noOp,
    renderServiceNegotiationPrompt: noOp, addBookmarkButton: noOp,
    normalizeFollowUpQuestions: () => [], renderFollowUpQuestions: noOp, checkRiskReassessmentNudge: noOp,
    finishRuntimeProgress: noOp, shouldShowAgentChain: () => false
  };
  context.window = context;
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(sendSource, context, {filename: 'chat.js:sendMessage'});
  function switchToB() {
    // 与resetPrivateChat相同：身份事件使旧版本失效，并让新回合可开始。
    identity = {registered_user_id: '2'.repeat(32), workspace_id: 'b'.repeat(32), privacy_revision: 0};
    context.chatRequestVersion++;
    context.NavrenChatPrivacyVersion++;
    context.chatSending = false;
    selectedReference = null;
    input.value = '概括B附件';
    staged = [new File(['B文档'], 'B.txt', {type: 'text/plain'})];
  }
  async function finishB(promise, request) {
    assert.equal(context.chatSending, true, 'B请求进行中必须持有发送锁');
    request.pending.resolve({reply: 'B附件回答', bank: true, isFallback: false, products: []});
    await promise;
    assert.equal(context.chatSending, false, 'B自身完成后必须释放发送锁');
    assert.equal(rendered.filter(row => row.role === 'ai').length, 1, '只有B可渲染助手答复');
    assert.equal(rendered.find(row => row.role === 'ai').content, 'B附件回答');
  }
  return {context, api, tickets, uploads, switchToB, finishB};
}

test('A旧Agent请求返回时，不能清掉B附件请求持有的发送锁', async () => {
  const f = fixture();
  const a = f.context.sendMessage();
  await until(() => f.api.length === 1, 'A未到达真实函数中的Agent等待点');
  f.switchToB();
  const b = f.context.sendMessage();
  await until(() => f.api.length === 2, 'B未到达Agent等待点');
  assert.equal(f.context.chatSending, true);
  f.api[0].pending.resolve({reply: 'A已中断', isFallback: true});
  await a;
  assert.equal(f.context.chatSending, true, 'A的旧请求收尾不得释放B锁');
  await f.finishB(b, f.api[1]);
});

test('A旧关联票据返回时，不能清掉B附件请求持有的发送锁', async () => {
  const f = fixture({reference: {kind: 'article', id: 'selected-A'}});
  const a = f.context.sendMessage();
  await until(() => f.tickets.length === 1, 'A未到达关联票据等待点');
  f.switchToB();
  const b = f.context.sendMessage();
  await until(() => f.api.length === 1, 'B未到达Agent等待点');
  f.tickets[0].pending.resolve({context_id: 'old-A-ticket'});
  await a;
  assert.equal(f.api.length, 1, '失效A票据不得继续提交Agent');
  assert.equal(f.context.chatSending, true, 'A的旧票据收尾不得释放B锁');
  await f.finishB(b, f.api[0]);
});

test('A旧附件上传返回时，不能清掉B上传锁；B后续可正常完成', async () => {
  const f = fixture({holdUploads: true});
  const a = f.context.sendMessage();
  await until(() => f.uploads.length === 1, 'A未到达附件上传等待点');
  f.switchToB();
  const b = f.context.sendMessage();
  await until(() => f.uploads.length === 2, 'B未到达附件上传等待点');
  f.uploads[0].pending.resolve(f.uploads[0].media);
  await a;
  assert.equal(f.api.length, 0, '失效A上传不得提交Agent');
  assert.equal(f.context.chatSending, true, 'A的旧上传收尾不得释放B锁');
  f.uploads[1].pending.resolve(f.uploads[1].media);
  await until(() => f.api.length === 1, 'B上传后未到达Agent等待点');
  await f.finishB(b, f.api[0]);
});

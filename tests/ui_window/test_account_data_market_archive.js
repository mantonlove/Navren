/* Real failed request archive -> actual market/discover consumer payload regression. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM, VirtualConsole} = require('jsdom');
const project = process.env.NAVREN_PROJECT || path.resolve(__dirname, '../..');
const clone = value => value == null ? value : JSON.parse(JSON.stringify(value));
const stable = value => value && typeof value === 'object'
  ? Array.isArray(value) ? '[' + value.map(stable).join(',') + ']'
    : '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + stable(value[key])).join(',') + '}'
  : JSON.stringify(value);
const failureFile = process.env.NAVREN_ARCHIVE_FAILURE || path.join(project, 'output/account-data-completion-20261017/market-save-failure.json');
const failure = JSON.parse(fs.readFileSync(failureFile, 'utf8'));
assert.equal(failure.status, 422, 'The fixture must be the actual failed request');
const failedRow = failure.post.value[0];
assert.ok(failedRow.archive.position && failedRow.archive.origin && failedRow.archive.primary_article_id && failedRow.archive.article_versions && failedRow.archive.selected_at,
  'Do not simplify away the real extra archive fields');
const binding = {registered_user_id:'a'.repeat(32),workspace_id:'1'.repeat(32),privacy_revision:0,auth_subject:'registered:' + 'a'.repeat(32)};
const expectedArchive = value => ({edition_date:value.edition_date,revision:Number(value.revision),event_version:Number(value.event_version)});
const eventFromFailure = () => ({
  id:failedRow.object_id, version:failedRow.version, headline:failedRow.text,
  published_at:failedRow.published_at, archive:clone(failedRow.archive),
  category:'household_finance', theme:'household', status:'published', availability:'active',
  summary:'真实失败请求的公开事件身份；正文为隔离测试桩。', facts:[], analysis:'隔离测试桩。', uncertainty:'隔离测试桩。',
  articles:[{
    id:failedRow.archive.primary_article_id,
    version:failedRow.archive.article_versions[failedRow.archive.primary_article_id],
    publisher:failedRow.source, title:failedRow.text, url:failedRow.url,
    published_at:failedRow.published_at, published_precision:'minute',
    status:'active', link_status:'verified', read_scope:'full_text', access_status:'free_full_text',
  }],
});
const captures = [], results = [];

function fixture() {
  const errors = [], virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', error => errors.push(error));
  const dom = new JSDOM('<body></body>', {url:'https://navren.test/recommend.html',runScripts:'outside-only',virtualConsole});
  const w = dom.window;
  const state = {revision:25,ready:true,notices:[],requests:[],response:null,rows:new Map([['qingzhou_bookmarks',[]],['navren_reading_history',[]]])};
  w.NavrenAccountData = {itemKey:stable};
  w.Storage = {
    accountReady:() => state.ready, get:key => clone(state.rows.get(key) ?? null),
    update:async (key, mutate) => {
      const value = mutate(clone(state.rows.get(key) ?? null));
      const section = {qingzhou_bookmarks:'favorites',navren_reading_history:'reading'}[key];
      assert.ok(section, 'Only the necessary consumer sections may be written');
      captures.push({operation:key,post:{expected_revision:state.revision,section,value:clone(value)}});
      await Promise.resolve();
      state.rows.set(key, clone(value)); state.revision++;
      w.dispatchEvent(new w.CustomEvent('navren:account-state',{detail:{status:'ready',keys:[key]}}));
      return true;
    },
  };
  w.BankClient = {identity:() => clone(binding)};
  w.showToast = text => state.notices.push(text);
  w.setInterval = () => 1;
  w.NavrenHelp = {button:() => w.document.createElement('button')};
  w.NavrenViews = {current:() => ({view:'home'})};
  w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open',''); };
  w.HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); this.dispatchEvent(new w.Event('close')); };
  w.fetch = async url => {
    state.requests.push(String(url));
    assert.ok(state.response, 'No unplanned request may be made');
    return {ok:true,json:async () => clone(state.response)};
  };
  w.eval(fs.readFileSync(process.env.NAVREN_MARKET_SOURCE || path.join(project,'js/market_ui.js'),'utf8'));
  w.eval(fs.readFileSync(process.env.NAVREN_DISCOVER_SOURCE || path.join(project,'js/discover_workspace.js'),'utf8'));
  return {w,state,close() {
    state.ready=false;w.dispatchEvent(new w.Event('pagehide'));dom.window.close();
    assert.equal(errors.length,0,errors.map(error => error.stack).join('\n'));
  }};
}
function assertPublic(row, archive, kind, expectedVersion = Number(archive.event_version)) {
  assert.deepEqual(Object.keys(row.archive).sort(),['edition_date','event_version','revision'],kind + ' archive must contain exactly the contract fields');
  assert.deepEqual(row.archive,expectedArchive(archive));
  assert.equal(row.source_scope,'public_market');
  assert.equal(row.version,expectedVersion);
  for (const key of ['workspace_id','privacy_revision','auth_subject','registered_user_id']) assert.ok(!Object.hasOwn(row,key),kind + ' must remain a public record');
}
async function check(name, run) {
  try { await run(); results.push({name,passed:true}); }
  catch (error) { results.push({name,passed:false,error:error.stack}); throw error; }
}
async function assertPreciseOpen(f, record, event) {
  const {w,state} = f;
  state.response=clone(event);
  const count=state.requests.length;
  await w.MarketUI.openBookmark(clone(record));
  assert.equal(state.requests.length,count+1);
  assert.equal(state.requests.at(-1),'/api/market/hotspots/history/' + encodeURIComponent(record.archive.edition_date) + '/events/' + encodeURIComponent(record.object_id) + '?revision=' + encodeURIComponent(record.archive.revision));
  const dialog=w.document.querySelector('.market-panel-layer[open]');
  assert.ok(dialog, 'The exact saved archive must open');
  assert.ok(dialog.querySelector('.market-panel-title'), 'Matching archive must render its detail, not an error/fallback');
  assert.ok(dialog.textContent.includes('解读版本 ' + record.archive.event_version));
  const ref=w.MarketUI.archiveReference({id:record.object_id,version:record.version,headline:record.text,archive:clone(record.archive)});
  assert.equal(ref.version,record.archive.event_version);
  assert.deepEqual(clone(ref.parameters),expectedArchive(record.archive));
  dialog.close();
}

(async () => {
  const f=fixture();
  try {
    await check('real-failure-shape-projects-public-contract',async () => {
      const event=eventFromFailure();
      assert.equal(await f.w.MarketUI.toggleBookmark('event',clone(event)),true);
      assert.equal(await f.w.DiscoverWorkspace.recordMarketReading(clone(event)),true);
      const favorite=f.state.rows.get('qingzhou_bookmarks')[0],reading=f.state.rows.get('navren_reading_history')[0];
      assertPublic(favorite,event.archive,'favorite',event.version);assertPublic(reading,event.archive,'reading');
      assert.equal(favorite.object_id,failedRow.object_id);assert.equal(reading.id,failedRow.object_id);
      assert.equal(favorite.url,failedRow.url);assert.equal(favorite.category,'market_news');
      await assertPreciseOpen(f,favorite,event);
    });
    await check('revision-and-event-version-identities-stay-distinct',async () => {
      const base=eventFromFailure();
      const revisionEvent={...clone(base),archive:{...clone(base.archive),revision:base.archive.revision+1}};
      // Main-object version is deliberately different: favorites retain it, while
      // archive references and reading retain the exact archived event version.
      const versionEvent={...clone(revisionEvent),version:base.version+10,archive:{...clone(revisionEvent.archive),event_version:base.archive.event_version+1}};
      for (const event of [revisionEvent,versionEvent]) {
        assert.equal(await f.w.MarketUI.toggleBookmark('event',clone(event)),true);
        const favorites=f.state.rows.get('qingzhou_bookmarks');assert.equal(favorites.length,1,'Replacing the same event must retain one exact saved version');
        assertPublic(favorites[0],event.archive,'replacement',event.version);await assertPreciseOpen(f,favorites[0],event);
        assert.equal(await f.w.DiscoverWorkspace.recordMarketReading(clone(event)),true);
      }
      assert.equal(await f.w.DiscoverWorkspace.recordMarketReading(clone(versionEvent)),true);
      const reading=f.state.rows.get('navren_reading_history');
      assert.equal(reading.length,3,'Archive revision and event version each distinguish reading identity; exact repeat deduplicates');
      assert.equal(new Set(reading.map(row => stable(row.archive))).size,3);
      for (const row of reading) assertPublic(row,row.archive,'reading identity');
      const saved=f.state.rows.get('qingzhou_bookmarks')[0];
      assert.equal(saved.version,versionEvent.version);assert.notEqual(saved.version,saved.archive.event_version);
      assert.equal(reading[0].version,versionEvent.archive.event_version);
      assert.equal(await f.w.MarketUI.toggleBookmark('event',clone(versionEvent)),true);
      assert.equal(f.state.rows.get('qingzhou_bookmarks').length,0,'Exact same identity toggles off rather than adding a second copy');
      assert.equal(saved.archive.event_version,versionEvent.archive.event_version);
    });
  } finally { f.close(); }
})().then(() => finish(true)).catch(error => { console.error(error.stack || error); finish(false);process.exitCode=1; });

function finish(passed) {
  const latestNonempty = section => [...captures].reverse().find(row => row.post.section===section && row.post.value.length)?.post.value || [];
  const packet={schema:'navren.market-archive-shape-probe/1',passed,results,favorites:latestNonempty('favorites'),reading:latestNonempty('reading'),payloadSnapshots:captures,actual_http_requests:0,browser_operations:0,backend_validation:'not run here; captured consumer payloads are supplied for independent Base validator'};
  if (process.env.NAVREN_ARCHIVE_PAYLOADS) fs.writeFileSync(process.env.NAVREN_ARCHIVE_PAYLOADS,JSON.stringify(packet,null,2)+'\n');
  console.log(JSON.stringify(packet));
}

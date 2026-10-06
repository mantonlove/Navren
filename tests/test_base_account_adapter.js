/* Real client/adapter code in VM; mocked HTTP owns identity, not local state. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const {execFileSync} = require('node:child_process');

const root = path.join(__dirname, '..');
const bankSource = fs.readFileSync(path.join(root, 'js/bank_client.js'), 'utf8');
const adapterSource = fs.readFileSync(path.join(root, 'js/base_account_adapter.js'), 'utf8');
const clean = value => JSON.parse(JSON.stringify(value));
const A = {account_id:'tycoon', workspace_id:'server-workspace-a', csrf:'csrf-a', privacy_revision:7};
const B = {account_id:'grain_farmer', workspace_id:'server-workspace-b', csrf:'csrf-b', privacy_revision:2};
const expected = identity => ({expected_workspace_id:identity.workspace_id, expected_privacy_revision:identity.privacy_revision});
const fixtures = [
  ['tycoon','A市首富'],['grain_farmer','种粮大户'],
  ['working_landlady','坚持上班的包租婆'],['retired_entrepreneur','退休后重新创业的阿姨']
];

function response(data, status=200, binding=data) {
  const headers = new Map();
  if (binding?.workspace_id) headers.set('x-navren-workspace', binding.workspace_id);
  if (binding?.privacy_revision != null) headers.set('x-navren-privacy-revision', String(binding.privacy_revision));
  return {ok:status>=200&&status<300,status,json:async()=>clean(data),
    headers:{get:key=>headers.get(key.toLowerCase())??null}};
}

function harness(fetcher, {bus=[], storage=new Map(), loading=false}={}) {
  const events=[], listeners=new Map(), documentListeners=new Map(), requests=[], configured=[];
  const on = (map,name,callback) => map.set(name,[...(map.get(name)||[]),callback]);
  class FakeBroadcastChannel {
    constructor(name){this.name=name;this.onmessage=null;bus.push(this);}
    postMessage(data){for(const peer of bus)if(peer!==this&&peer.name===this.name)queueMicrotask(()=>peer.onmessage?.({data}));}
    close(){const index=bus.indexOf(this);if(index>=0)bus.splice(index,1);}
  }
  const context=vm.createContext({
    AbortController, Event, setTimeout, clearTimeout, queueMicrotask, console,
    BroadcastChannel:FakeBroadcastChannel,
    localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,String(value)),removeItem:key=>storage.delete(key)},
    document:{readyState:loading?'loading':'complete',addEventListener:(name,callback)=>on(documentListeners,name,callback)},
    NavrenAccountSwitcher:{configure:adapter=>configured.push(adapter)},
    addEventListener:(name,callback)=>on(listeners,name,callback),
    dispatchEvent:event=>{events.push(event.type);for(const callback of listeners.get(event.type)||[])callback(event);return true;},
    fetch:async(url,options)=>{requests.push({url,options});return fetcher(url,options);}
  });
  context.window=context;
  vm.runInContext(bankSource,context,{filename:'js/bank_client.js'});
  vm.runInContext(adapterSource,context,{filename:'js/base_account_adapter.js'});
  return {context,bank:context.BankClient,adapter:context.NavrenBaseAccountAdapter,
    requests,events,storage,configured,bus,
    ready(){for(const fn of documentListeners.get('DOMContentLoaded')||[])fn();}};
}

async function tick(){await new Promise(resolve=>setImmediate(resolve));}

test('adapter registers after DOM readiness and identity comes only from HTTP cookie session', async()=>{
  const storage=new Map([['navren_current_account','retired_entrepreneur'],['navren_workspace_id','forged-workspace']]);
  const h=harness((url,options)=>{
    assert.equal(url,'/api/finance/session');
    assert.equal(options.credentials,'same-origin');assert.equal(options.cache,'no-store');
    assert.equal(options.method,'POST');assert.deepEqual(JSON.parse(options.body),{});
    assert.equal(options.headers['X-QZ-CSRF'],undefined);
    return response(A);
  },{storage,loading:true});
  assert.equal(h.configured.length,0);h.ready();assert.equal(h.configured.length,1);
  assert.equal(h.configured[0],h.adapter);assert.equal(h.adapter.safe_switch,true);
  assert.deepEqual(clean(await h.bank.ensure()),A);
  assert.equal(h.bank.identity().workspace_id,A.workspace_id);
  assert.equal(storage.get('navren_workspace_id'),'forged-workspace','local value is ignored, not treated as credentials');
});

test('catalogue maps four server IDs/labels without deriving an authorization workspace',async()=>{
  const catalog={workspace_id:A.workspace_id,workspace_binding:A.workspace_id,
    privacy_revision:A.privacy_revision,current_account_id:A.account_id,data_scope:'navren_synthetic',
    accounts:fixtures.map(([user_id,display_name])=>({user_id,display_name,source_version:'fixture-v1'}))};
  const h=harness(url=>url.endsWith('/session')?response(A):response(catalog));
  const result=await h.adapter.catalogue();
  assert.equal(result.schema_version,1);
  assert.deepEqual(clean(result.accounts.map(a=>[a.account_id,a.label])),fixtures);
  assert.equal(result.workspace_binding,A.workspace_id);
  assert.equal(h.requests[1].url,'/api/finance/demo/accounts');
});

test('successful switch sends current CSRF and expected identity then accepts only the server session',async()=>{
  const storage=new Map([['qingzhou_chatHistory','private-a'],['qingzhou_chatKeyMoments','private-moment-a']]);
  const h=harness((url,options)=>{
    if(url.endsWith('/session'))return response(A);
    assert.equal(url,'/api/finance/demo/select');assert.equal(options.method,'POST');
    assert.equal(options.headers['X-QZ-CSRF'],A.csrf);
    assert.deepEqual(JSON.parse(options.body),{user_id:B.account_id,...expected(A)});
    assert.equal(options.credentials,'same-origin');return response(B);
  },{storage});
  await h.bank.ensure();const guard=h.bank.guard();
  const selected=await h.adapter.select(B.account_id,expected(A));
  assert.deepEqual(clean(selected),B);assert.deepEqual(clean(h.bank.identity()),B);
  assert.equal(guard.valid(),false);assert.equal(storage.has('qingzhou_chatHistory'),false);
  assert.equal(storage.has('qingzhou_chatKeyMoments'),false);
  assert.ok(h.events.includes('navren:privacy-reset'));
});

test('a stale expected workspace stops before sending a switch or invalidating the current identity',async()=>{
  const h=harness(()=>response(A));await h.bank.ensure();const guard=h.bank.guard();
  await assert.rejects(h.adapter.select(B.account_id,expected(B)),/账户已变化/);
  assert.equal(h.requests.length,1);assert.deepEqual(clean(h.bank.identity()),A);
  assert.equal(guard.valid(),true);
});

test('HTTP switch error leaves identity unknown until fresh HTTP verification',async()=>{
  const h=harness(url=>url.endsWith('/session')?response(A):response({error:{code:'WORKSPACE_CHANGED',message:'原工作区已经变化'}},409));
  await h.bank.ensure();const guard=h.bank.guard();
  await assert.rejects(h.adapter.select(B.account_id,expected(A)),/原工作区已经变化/);
  assert.equal(h.bank.identity(),null);assert.equal(guard.valid(),false);
  assert.deepEqual(clean(await h.bank.ensure()),A,'recovery follows server cookie, not requested account');
  assert.equal(h.requests.filter(r=>r.url.endsWith('/session')).length,2);
});

test('network error and server selection mismatch never create optimistic local identity',async()=>{
  for(const failure of ['network','identity','malformed']){
    const h=harness(url=>{
      if(url.endsWith('/session'))return response(A);
      if(failure==='network')throw new Error('offline');
      if(failure==='identity')return response({...B,account_id:'retired_entrepreneur'});
      return response({...B,csrf:null});
    });
    await h.bank.ensure();
    await assert.rejects(h.adapter.select(B.account_id,expected(A)));
    assert.equal(h.bank.identity(),null,failure);
  }
});

test('old GET arriving after successful switch is aborted and rejected even if transport ignores abort',async()=>{
  let resolveRead;
  const h=harness(url=>{
    if(url.endsWith('/session'))return response(A);
    if(url.endsWith('/demo/select'))return response(B);
    return new Promise(resolve=>{resolveRead=resolve;});
  });
  await h.bank.ensure();
  const old=h.bank.get('/overview').then(()=>null,error=>error);
  await tick();const oldRequest=h.requests.find(r=>r.url.endsWith('/overview'));
  await h.adapter.select(B.account_id,expected(A));
  assert.equal(oldRequest.options.signal.aborted,true);
  resolveRead(response({...A,total_cash_minor:10000}));
  const error=await old;assert.equal(error.code,'STALE_RESPONSE');
  assert.deepEqual(clean(h.bank.identity()),B);
});

test('another page receives revocation, discards its cached session and requires HTTP identity',async()=>{
  const bus=[];let identity=A;
  const first=harness(url=>url.endsWith('/demo/select')?(identity=B,response(B)):response(identity),{bus});
  const second=harness(()=>response(identity),{bus});
  await first.bank.ensure();await second.bank.ensure();const guard=second.bank.guard();
  await first.adapter.select(B.account_id,expected(A));await tick();
  assert.equal(second.bank.identity(),null);assert.equal(guard.valid(),false);
  assert.ok(second.events.includes('navren:identity-revoked'));
  assert.deepEqual(clean(await second.bank.ensure()),B);
});

test('a session bootstrap started before another page switches cannot restore its old identity',async()=>{
  const bus=[];let resolveSession;
  const first=harness(url=>url.endsWith('/demo/select')?response(B):response(A),{bus});
  const second=harness(()=>new Promise(resolve=>{resolveSession=resolve;}),{bus});
  await first.bank.ensure();
  const bootstrap=second.bank.ensure().then(()=>null,error=>error);
  await tick();
  await first.adapter.select(B.account_id,expected(A));await tick();
  assert.equal(second.bank.identity(),null);
  resolveSession(response(A));
  const error=await bootstrap;
  assert.equal(error?.code,'STALE_RESPONSE','an HTTP response from before revocation is no longer the current identity');
  assert.equal(second.bank.identity(),null);
});

test('foreign HTTP workspace binding is rejected before an account response can be used',async()=>{
  const h=harness(url=>url.endsWith('/session')?response(A):response({...B,total_cash_minor:999},200,B));
  await h.bank.ensure();const guard=h.bank.guard();
  await assert.rejects(h.bank.get('/overview'),error=>error.code==='STALE_IDENTITY');
  assert.equal(h.bank.identity(),null);assert.equal(guard.valid(),false);
});

test('contradictory response header and JSON workspace cannot pass by trusting one binding only',async()=>{
  const h=harness(url=>url.endsWith('/session')?response(A):response({...B,total_cash_minor:999},200,A));
  await h.bank.ensure();
  await assert.rejects(h.bank.get('/overview'),error=>['STALE_IDENTITY','UNBOUND_RESPONSE'].includes(error.code));
  assert.equal(h.bank.identity(),null);
});

test('SESSION_CHANGED without a broadcast still revokes cached identity and pending guards',async()=>{
  const h=harness(url=>url.endsWith('/session')?response(A):response({error:{code:'SESSION_CHANGED',message:'账户已在其他页面切换'}},409));
  await h.bank.ensure();const guard=h.bank.guard();
  await assert.rejects(h.bank.get('/overview'),error=>error.code==='SESSION_CHANGED');
  assert.equal(h.bank.identity(),null);
  assert.equal(guard.valid(),false);
  assert.ok(h.events.includes('navren:privacy-reset'));
});

test('real HTTP pages load the base adapter after the existing bank client and selector',()=>{
  // No imported account data, background jobs, models or paid speech are needed for static delivery.
  const python=fs.existsSync(path.join(root,'.venv/bin/python'))?path.join(root,'.venv/bin/python'):'python3';
  const script=`
import json, re, urllib.request
from scripts.verify_base_runtime import temporary_runtime
pages=[]
with temporary_runtime(initialize=False) as (server, base, imported):
    for page in ('index.html','chat.html','recommend.html','account.html','mine.html','copilot.html'):
        with urllib.request.urlopen(base+'/'+page, timeout=10) as response:
            html=response.read().decode()
            pages.append({'page':page,'status':response.status,'release':response.headers.get('X-Navren-Release'),
                'scripts':re.findall(r'<script[^>]+src=[\\\"\\\']([^\\\"\\\']+)',html)})
print(json.dumps(pages))
`;
  const pages=JSON.parse(execFileSync(python,['-c',script],{cwd:root,timeout:30000,encoding:'utf8'}));
  assert.equal(pages.length,6);
  for(const page of pages){
    assert.equal(page.status,200,page.page);assert.ok(page.release,page.page);
    const find=name=>page.scripts.findIndex(url=>url.split('?')[0].replace(/^\//,'')===`js/${name}.js`);
    const adapter=find('base_account_adapter');
    assert.ok(adapter>=0,`${page.page}: server must inject the adapter`);
    assert.equal(page.scripts.filter(url=>url.includes('base_account_adapter.js')).length,1,page.page);
    assert.equal(page.scripts[adapter].split('?v=')[1],page.release,page.page);
    if(page.page!=='index.html'){
      assert.ok(find('bank_client')>=0&&find('bank_client')<find('account_switcher'),page.page);
      assert.ok(find('account_switcher')<adapter,`${page.page}: adapter registers after selector exists`);
    }
  }
});

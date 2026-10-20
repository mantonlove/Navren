/* Full real NavrenChatArchive consumer + actual four-turn backend DTO. */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const repo=process.env.NAVREN_PROJECT||path.resolve(__dirname,'../..');
const source=fs.readFileSync(process.env.NAVREN_ARCHIVE_SOURCE||path.join(repo,'js/chat_archive.js'),'utf8');
const dto=JSON.parse(fs.readFileSync(process.env.NAVREN_ARCHIVE_DTO||path.join(repo,'output/account-data-completion-20261017/actual-archive-current-privacy-dto.json'),'utf8'));
const clone=value=>JSON.parse(JSON.stringify(value));
assert.equal(dto.items.length,4);assert.equal(dto.privacy_revision,10);
const published=dto.items.findIndex(turn=>Object.hasOwn(turn.assistant,'original_privacy_revision'));
assert.ok(published>=0);assert.equal(dto.items[published].assistant.original_privacy_revision,0);
const originalMedia=dto.items.flatMap(turn=>[...turn.user.attachments,...(turn.assistant.attachments||[])]);
assert.equal(originalMedia.length,3);
const results=[],observations=[];
let totalReads=0,networkCalls=0;

function fixture(page=clone(dto),onRead){
  const sandbox={module:{exports:{}},AbortController,console,fetch:()=>{networkCalls++;throw Error('Actual HTTP forbidden');}};
  vm.createContext(sandbox);vm.runInContext(source,sandbox,{filename:process.env.NAVREN_ARCHIVE_SOURCE||'actual-chat_archive.js'});
  const binding={registered_user_id:dto.registered_user_id,workspace_id:dto.workspace_id,account_id:dto.persona_id,privacy_revision:dto.privacy_revision,auth_subject:'controlled-authenticated:'+dto.registered_user_id};
  const f={identity:binding,page,reads:[],changes:[]};
  f.client=sandbox.module.exports.create({identityProvider:()=>clone(f.identity),readPage:async options=>{
    totalReads++;f.reads.push({cursor:options.cursor,limit:options.limit,aborted:options.signal.aborted});
    assert.equal(options.cursor,null);assert.equal(options.limit,20);assert.equal(options.signal.aborted,false);
    if(onRead)await onRead(f);return clone(f.page);
  },onChange:state=>f.changes.push(clone(state))});
  f.client.attach();return f;
}
async function read(page,onRead){const f=fixture(page,onRead);try{return {f,state:await f.client.loadNext()};}catch(error){f.error=error;throw Object.assign(error,{fixture:f});}}
async function rejected(page,expected='INVALID_ARCHIVE_DTO',onRead){
  const f=fixture(page,onRead);
  try{await assert.rejects(f.client.loadNext(),error=>error.code===expected);assert.equal(f.client.getState().messages.length,0,'Rejected data must not enter the projected messages');return f;}
  finally{f.client.unload();}
}
async function check(name,run){try{await run();results.push({name,passed:true});}catch(error){results.push({name,passed:false,error:error.stack});throw error;}}
function mutated(change){const value=clone(dto);change(value);return value;}

(async()=>{
  await check('actual-four-turn-dto-with-historical-privacy-and-three-original-descriptors',async()=>{
    let loaded;try{loaded=await read(clone(dto));}catch(error){assert.fail('Actual historical privacy DTO should load: '+error.code);}
    const {f,state}=loaded;
    try{
      assert.equal(state.phase,'ready');assert.equal(state.turns_loaded,4);assert.equal(state.pages_loaded,1);assert.equal(state.personal_revision,dto.personal_revision);assert.equal(state.binding.privacy_revision,10);assert.equal(state.has_more,false);
      // Two received-media-only turns project only their user side; two published
      // turns project both sides. No original unpublished assistant is revived.
      assert.equal(state.messages.length,6);assert.equal(state.messages.filter(row=>row.kind==='published-readback').length,2);
      const media=state.messages.flatMap(message=>message.attachments||[]);
      assert.deepEqual(clone(media),originalMedia,'Keep all three original media descriptors byte-for-byte');
      for(const message of state.messages){assert.equal(message.read_only,true);assert.equal(message.historical_readonly,true);assert.equal(message.current_fact_eligible,false);assert.equal(message.execution_authority,false);assert.ok(Object.isFrozen(message));}
      for(const descriptor of media){assert.equal(descriptor.historical_readonly,true);assert.equal(descriptor.execution_authority,false);assert.ok(Object.isFrozen(descriptor));}
      assert.ok(state.messages.some(row=>row.text===dto.items[published].assistant.content));
      f.client.getState();assert.equal(f.reads.length,1,'Projection/getState must not fetch original media or hydrate old components');assert.equal(networkCalls,0);
      observations.push({name:'actual DTO readback',turns_loaded:state.turns_loaded,messages:state.messages.length,page_current_privacy:state.binding.privacy_revision,original_privacy:dto.items.filter(turn=>Object.hasOwn(turn.assistant,'original_privacy_revision')).map(turn=>turn.assistant.original_privacy_revision),original_descriptors:clone(media),reads:f.reads,all_historical_readonly:true});
    }finally{f.client.unload();}
  });
  await check('original-privacy-is-a-bounded-nonnegative-history-revision',async()=>{
    const same=mutated(page=>page.items[published].assistant.original_privacy_revision=page.privacy_revision);
    const loaded=await read(same);assert.equal(loaded.state.turns_loaded,4);loaded.f.client.unload();
    for(const value of [dto.privacy_revision+1,-1,0.5,'0'])await rejected(mutated(page=>page.items[published].assistant.original_privacy_revision=value));
    await rejected(mutated(page=>page.items[published].assistant.original_personal_revision=page.personal_revision+1));
  });
  await check('current-owner-workspace-subject-persona-and-page-privacy-stay-bound',async()=>{
    for(const [field,value]of [['registered_user_id','b'.repeat(12)+'4bbb8'+'b'.repeat(15)],['workspace_id','c'.repeat(32)],['persona_id','tycoon'],['privacy_revision',9]])await rejected(mutated(page=>page[field]=value),'PAGE_BINDING_CHANGED');
    // auth_subject belongs to the authenticated reader binding, never to DTO payload.
    const f=await rejected(clone(dto),'STALE_REQUEST',current=>{current.identity.auth_subject='controlled-other-auth-subject';});
    assert.equal(f.client.getState().phase,'detached');
    await rejected(mutated(page=>page.auth_subject='forged-extra-subject'));
  });
  await check('readonly-current-fact-and-execution-flags-remain-strict',async()=>{
    for(const [field,value]of [['historical_readonly',false],['current_fact_eligible',true],['execution_authority',true]])await rejected(mutated(page=>page.items[published].assistant[field]=value));
    await rejected(mutated(page=>page.execution_authority=true));
    await rejected(mutated(page=>page.items[0].user.attachments[0].execution_authority=true));
    await rejected(mutated(page=>page.items[published].assistant.readonly_components.execution_authority=true));
  });
  await check('old-components-stay-inert-and-cannot-carry-actions-or-resolvers',async()=>{
    const safe=mutated(page=>page.items[published].assistant.readonly_components.widgets=[{type:'financial_presentation_snapshot',presentation:{contract:'navren.presentation/1',title:'只读历史指标',value:123}}]);
    const {f,state}=await read(safe);
    try{const assistant=state.messages.find(message=>message.message_id===dto.items[published].assistant.message_id);assert.equal(assistant.readonly_components.widgets.length,1);assert.equal(assistant.readonly_components.widgets[0].presentation.value,123);assert.equal(f.reads.length,1);assert.equal(networkCalls,0);assert.ok(Object.isFrozen(assistant.readonly_components.widgets[0]));}
    finally{f.client.unload();}
    for(const name of ['actions','resolver'])await rejected(mutated(page=>page.items[published].assistant.readonly_components.presentation={contract:'navren.presentation/1',[name]:[{operation:'pay'}]}));
  });
})().then(()=>finish(true)).catch(error=>{console.error(error.stack||error);finish(false);process.exitCode=1;});
function finish(passed){const result={schema:'navren.chat-archive-historical-privacy/1',passed,results,observations,controlled_page_reads:totalReads,actual_http_requests:networkCalls,browser_operations:0,scope:'Full unchanged NavrenChatArchive consumes actual four-turn backend DTO through a controlled authenticated reader. Synthetic auth_subject only models current reader binding. No original-media fetch, old-component hydration, execution, backend or actual browser acceptance.'};if(process.env.NAVREN_ARCHIVE_RESULT)fs.writeFileSync(process.env.NAVREN_ARCHIVE_RESULT,JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result));}

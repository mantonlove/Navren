/* 历史收藏只读展示定点检查：完整真实 mine/storage；确认账号快照为受控替身。 */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
const {JSDOM,VirtualConsole}=require('jsdom');
const root=path.resolve(__dirname,'../..');
const output=process.argv[2];
const mineFile=process.argv[3]?path.resolve(process.argv[3]):path.join(root,'js/mine.js');
const storageFile=path.join(root,'js/storage.js');
const sha=value=>crypto.createHash('sha256').update(value).digest('hex');
const mineSource=fs.readFileSync(mineFile,'utf8'),storageSource=fs.readFileSync(storageFile,'utf8');
const pins={[path.relative(root,mineFile)]:sha(mineSource),'js/storage.js':sha(storageSource)};
const clone=value=>value==null?value:JSON.parse(JSON.stringify(value));
const owner='aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa',workspace='11111111111141118111111111111111';
const text='本人在授权版本 0 保存的历史收藏';
const item=extra=>({category:'chat_message',message_id:'history-owned-message',text,workspace_id:workspace,privacy_revision:0,timestamp:'2026-10-17T06:00:00Z',...extra});
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const results=[];

async function fixture(options={}){
  const errors=[],mutations=[],requests=[];
  const console=new VirtualConsole();console.on('jsdomError',error=>errors.push(String(error)));
  const dom=new JSDOM('<!doctype html><html><body data-page="mine"><section id="bookmarksList"></section></body></html>',{
    url:'https://navren.invalid/mine.html?view=saved',runScripts:'dangerously',virtualConsole:console});
  const w=dom.window,d=w.document;
  if(d.readyState!=='complete')await new Promise(resolve=>w.addEventListener('load',resolve,{once:true}));
  const service={ready:options.ready!==false,status:options.ready===false?'unavailable':'ready',identity:{registered_user_id:owner,workspace_id:workspace,privacy_revision:options.currentPrivacy??1},rows:clone(options.rows||[item()])};
  if(options.identity===null)service.identity=null;
  const original=JSON.stringify(service.rows);
  w.BankClient={identity:()=>clone(service.identity)};
  const forbidden=(method,...args)=>{mutations.push({method,args:clone(args)});throw Error('历史展示不应写入资料');};
  w.fixtureStorage={accountReady:()=>service.ready,accountStatus:()=>service.status,ready:async()=>{if(!service.ready)throw Error('ACCOUNT_UNAVAILABLE');return {};},
    get:key=>service.ready&&key==='qingzhou_bookmarks'?clone(service.rows):null,
    set:(...args)=>forbidden('set',...args),setMany:(...args)=>forbidden('setMany',...args),update:(...args)=>forbidden('update',...args)};
  w.fetch=(...args)=>{requests.push(args);throw Error('历史展示不应发起网络请求');};
  // 模块原样通过独立 classic script 元素求值；加载前 DOMContentLoaded 已结束，仅显式调用目标消费者。
  function load(source){const script=d.createElement('script');script.textContent=source;d.body.append(script);}
  load(storageSource);load('Object.assign(Storage,window.fixtureStorage);');load(mineSource);
  const render=()=>w.renderBookmarks();render();
  function noSideEffects(){assert.equal(JSON.stringify(service.rows),original,'no owner/privacy rewrite of confirmed snapshot');assert.deepEqual(mutations,[]);assert.deepEqual(requests,[]);
    const row=d.querySelector('.mine-saved-item');if(row){assert.equal(row.querySelectorAll('a').length,0,'private historical text has no execution or navigation entry');
      assert.deepEqual([...row.querySelectorAll('button')].map(button=>button.textContent),['删除收藏'],'only collection deletion remains, no execution action');}
  }
  return {w,d,dom,service,errors,render,noSideEffects,body:()=>d.querySelector('.mine-saved-item > p')?.textContent,
    async finish(){await tick();noSideEffects();assert.deepEqual(errors,[]);dom.window.close();}};
}
async function check(id,name,operation,options){let f;try{f=await fixture(options);await operation(f);await f.finish();results.push({id,name,passed:true});}
catch(error){f?.dom.window.close();results.push({id,name,passed:false,error:error.stack||String(error)});}}
const hidden=f=>{assert.notEqual(f.body(),text);assert.match(f.body()||'',/正文已隐藏/);};

(async()=>{
  await check('BH01','本人历史收藏 privacy 0→1 保留只读正文、不补写归属或版本',async f=>{
    assert.equal(f.body(),text);assert.equal(Object.hasOwn(f.service.rows[0],'registered_user_id'),false);
    f.service.identity.privacy_revision=1;f.w.dispatchEvent(new f.w.CustomEvent('navren:account-state',{detail:{status:'ready',keys:['qingzhou_bookmarks']}}));
    assert.equal(f.body(),text);assert.equal(f.service.rows[0].privacy_revision,0);assert.equal(Object.hasOwn(f.service.rows[0],'registered_user_id'),false);
    assert.equal(f.d.querySelector('.mine-saved-item > p').getAttribute('translate'),'no');
  },{currentPrivacy:0});
  await check('BH02','明确匹配当前注册用户的历史收藏可读',async f=>assert.equal(f.body(),text),{rows:[item({registered_user_id:owner})]});
  await check('BH03','账号不可用时不展示任何收藏正文',async f=>{assert.equal(f.d.querySelector('.mine-saved-item'),null);assert.match(f.d.getElementById('bookmarksList').textContent,/暂未读取/);assert.ok(!f.d.body.textContent.includes(text));},{ready:false});
  await check('BH04','其他注册 owner 的私人收藏隐藏',async f=>hidden(f),{rows:[item({registered_user_id:'bbbbbbbbbbbb4bbb8bbbbbbbbbbbbbbb',privacy_revision:1})]});
  await check('BH05','其他 workspace 的私人收藏隐藏',async f=>hidden(f),{rows:[item({workspace_id:'22222222222242228222222222222222',privacy_revision:1})]});
  await check('BH06','未来 privacy 版本隐藏',async f=>hidden(f),{rows:[item({privacy_revision:2})]});
  for(const [id,name,revision] of [['BH07','缺失',undefined],['BH08','负数',-1],['BH09','小数',0.5],['BH10','字符串','0'],['BH11','空值',null]]){
    const row=item({privacy_revision:revision});if(revision===undefined)delete row.privacy_revision;
    await check(id,name+' privacy 版本隐藏',async f=>hidden(f),{rows:[row]});
  }
  await check('BH12','身份尚未核实时隐藏私人收藏',async f=>hidden(f),{identity:null,rows:[item({privacy_revision:1})]});
  const drift=Object.entries(pins).filter(([file,hash])=>sha(fs.readFileSync(path.join(root,file)))!==hash).map(([file])=>file);
  const report={status:results.every(row=>row.passed)&&!drift.length?'PASS_SCOPED_BOOKMARK_HISTORY':'FAIL',passed:results.filter(row=>row.passed).length,failed:results.filter(row=>!row.passed).length,cases:results,
    source_sha256:pins,source_drift:drift,
    boundaries:{actual_full_modules:['js/storage.js initialization',path.relative(root,mineFile)],actual_consumer:'renderBookmarks and its real accountRestoreText/memoryMessage helpers',script_context:'classic scripts after natural DOMContentLoaded; no unrelated whole-page init',
      synthesized:['Storage confirmed account snapshot/accountReady and mutation trap','current BankClient.identity metadata only','minimal saved-list DOM'],
      not_covered:['real Storage HTTP owner validation or server persistence','old model context revocation/authorization guards (unchanged, not replaced or asserted)','financial-memory read behavior (unchanged)','browser visual acceptance'],
      historical_private_rows_have_no_execution_entry:true,metadata_rewrites:0,real_network_calls:0,database_writes:0,paid_calls:0}};
  if(output){fs.mkdirSync(output,{recursive:true});fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(report,null,2)+'\n');}
  for(const row of results)process.stdout.write((row.passed?'PASS ':'FAIL ')+row.id+' '+row.name+'\n'+(row.error?row.error+'\n':''));
  process.stdout.write(JSON.stringify({status:report.status,passed:report.passed,failed:report.failed,source_drift:drift})+'\n');process.exitCode=report.status.startsWith('PASS')?0:1;
})().catch(error=>{process.stderr.write(error.stack+'\n');process.exitCode=1;});

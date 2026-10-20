/* 首个样式表之前的真实 HTML 脚本，以及主题等待、离页和失效边界。 */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
const {JSDOM,VirtualConsole}=require('jsdom');
const root=path.resolve(__dirname,'../..'),output=process.argv[2],baseline=process.argv[3];
const key='navren:paint-theme:v1';
const pages=['account.html','chat.html','recommend.html','mine.html','messages.html','index.html','copilot.html'];
const sourceRoot=baseline?path.resolve(baseline):root;
const design=fs.readFileSync(path.join(sourceRoot,'js/design.js'),'utf8');
const results=[];
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function settle(){for(let i=0;i<4;i++)await tick();}
async function check(id,fn){try{await fn();results.push({id,passed:true});}catch(error){results.push({id,passed:false,error:error.stack||String(error)});}}
function parseHead(page,value,blocked=false){
  const html=fs.readFileSync(path.join(sourceRoot,page),'utf8');
  const firstStyle=html.search(/<link\b[^>]*rel=["']stylesheet["']/i);
  assert(firstStyle>0,page+' has stylesheet');
  const head=html.slice(0,firstStyle),errors=[],console=new VirtualConsole();
  console.on('jsdomError',error=>errors.push(String(error)));
  const dom=new JSDOM(head+'</head><body></body></html>',{url:'https://navren.invalid/'+page,runScripts:'dangerously',virtualConsole:console,beforeParse(w){
    if(blocked)Object.defineProperty(w,'localStorage',{get(){throw Error('storage denied');}});
    else if(value!=null)w.localStorage.setItem(key,value);
  }});
  const state={theme:dom.window.document.documentElement.dataset.theme,scheme:dom.window.document.documentElement.style.colorScheme,background:dom.window.document.documentElement.style.backgroundColor,errors,head};
  dom.window.close();return state;
}
async function fixture({cached='dark',canonical='dark',loading=false}={}){
  const errors=[],console=new VirtualConsole();console.on('jsdomError',error=>errors.push(String(error)));
  const dom=new JSDOM('<!doctype html><html><body><div class="app"><header class="header"></header></div></body></html>',{url:'https://navren.invalid/account.html',runScripts:'outside-only',pretendToBeVisual:true,virtualConsole:console});
  const w=dom.window,d=w.document;if(d.readyState!=='complete')await new Promise(resolve=>w.addEventListener('load',resolve,{once:true}));await settle();
  let parseState=loading?'loading':'complete';
  if(loading)Object.defineProperty(d,'readyState',{get:()=>parseState,configurable:true});
  let ready=false,resolve,reject;const pending=new Promise((yes,no)=>{resolve=yes;reject=no;});
  if(cached){w.localStorage.setItem(key,cached);d.documentElement.dataset.theme=cached;d.documentElement.style.colorScheme=cached;}
  const values={navren_theme:{id:canonical},qingzhou_mode:'classic'};
  w.STORAGE_KEYS={THEME:'navren_theme',MODE:'qingzhou_mode'};
  w.Storage={accountReady:()=>ready,ready:()=>pending,get:k=>values[k]};
  w.MODE_DEFAULT_FONT={classic:'medium',senior:'large',youth:'small'};w.getModeFont=()=> 'medium';w.applyFontSize=()=>{};
  w.NavrenApp={href:value=>value};w.BankClient={};
  const notify=status=>w.dispatchEvent(new w.CustomEvent('navren:account-state',{detail:{status}}));
  // 与实际 storage.js 相同，先登记默认阶段的离页清理，再加载设计模块。
  w.addEventListener('pagehide',()=>{ready=false;notify('unbound');});
  w.addEventListener('navren:identity-revoked',()=>{ready=false;notify('unbound');});
  w.addEventListener('navren:account-switched',()=>{ready=false;notify('unbound');});
  w.eval(design);w.NavrenDesign.mountSettings(d.querySelector('.header'));await settle();
  return {w,d,errors,notify,completeParse:()=>{parseState='complete';},theme:()=>d.documentElement.dataset.theme,paint:()=>w.localStorage.getItem(key),
    async accept(){ready=true;notify('ready');resolve({});await settle();},
    async fail(){ready=false;notify('unavailable');reject(Error('account unavailable'));await settle();},
    async close(){resolve({});await settle();dom.window.close();assert.deepEqual(errors,[]);}};
}
(async()=>{
  await check('HEAD01',()=>{
    let expected;
    for(const page of pages){const dark=parseHead(page,'dark');assert.equal(dark.theme,'dark',page+' dark before styles');assert.equal(dark.scheme,'dark');assert.equal(dark.background,'rgb(13, 23, 37)');assert.deepEqual(dark.errors,[]);
      const script=dark.head.match(/<script data-navren-theme-bootstrap>[\s\S]*?<\/script>/)?.[0];assert(script,page+' inline prepaint script');expected??=script;assert.equal(script,expected,page+' uses the same bootstrap');
      const light=parseHead(page,'light');assert.equal(light.theme,'light');assert.equal(light.background,'rgb(237, 243, 250)');}
  });
  await check('HEAD02',()=>{for(const page of pages)for(const [value,blocked] of [[null,false],['not-a-theme',false],['dark',true]]){const state=parseHead(page,value,blocked);assert.equal(state.theme,undefined);assert.deepEqual(state.errors,[]);}});
  await check('THEME01',async()=>{const f=await fixture();try{
    assert.equal(f.theme(),'dark','confirmed previous paint survives module startup');f.notify('loading');assert.equal(f.theme(),'dark');assert.equal(f.paint(),'dark');
    assert.equal(f.d.getElementById('navrenThemeButton').disabled,true);assert.equal(await f.w.NavrenDesign.applyTheme({id:'light'}),false,'paint hint does not authorize changes');
    await f.accept();assert.equal(f.theme(),'dark');assert.equal(f.paint(),'dark');assert.equal(f.d.getElementById('navrenThemeButton').disabled,false);
  }finally{await f.close();}});
  await check('THEME02',async()=>{const f=await fixture();try{
    await f.accept();f.w.dispatchEvent(new f.w.Event('pagehide'));assert.equal(f.theme(),'dark','storage cleanup must not flash departing page');assert.equal(f.paint(),'dark','next-page paint survives pagehide');
    f.w.dispatchEvent(new f.w.Event('pageshow'));await settle();assert.equal(f.theme(),'dark','bfcache/return can keep paint while refreshing');
  }finally{await f.close();}});
  await check('THEME03',async()=>{const f=await fixture();try{
    await f.accept();f.w.dispatchEvent(new f.w.Event('navren:identity-revoked'));assert.equal(f.theme(),'light');assert.equal(f.paint(),null);assert.equal(f.d.getElementById('navrenThemeButton').disabled,true);
  }finally{await f.close();}});
  await check('THEME04',async()=>{const f=await fixture({canonical:'light'});try{assert.equal(f.theme(),'dark');await f.accept();assert.equal(f.theme(),'light','server preference wins');assert.equal(f.paint(),'light');}finally{await f.close();}});
  await check('THEME05',async()=>{const f=await fixture();try{assert.equal(f.theme(),'dark');await f.fail();assert.equal(f.theme(),'dark','failed preference reads keep only the display color');assert.equal(f.paint(),'dark');assert.equal(f.d.getElementById('navrenThemeButton').disabled,true);assert.equal(await f.w.NavrenDesign.applyTheme({id:'light'}),false);}finally{await f.close();}});
  await check('THEME06',async()=>{const f=await fixture({loading:true});try{
    await f.fail();assert.equal(f.theme(),'dark','a loaded BankClient does not mean the deferred welcome/session dependency is ready');assert.equal(f.paint(),'dark');
    assert.equal(f.d.getElementById('navrenThemeButton').disabled,true);f.completeParse();await f.accept();assert.equal(f.theme(),'dark');assert.equal(f.d.getElementById('navrenThemeButton').disabled,false);
  }finally{await f.close();}});
  await check('THEME07',async()=>{const f=await fixture();try{await f.accept();f.w.dispatchEvent(new f.w.Event('navren:account-switched'));await settle();assert.equal(f.theme(),'light');assert.equal(f.paint(),null);assert.equal(f.d.getElementById('navrenThemeButton').disabled,true);}finally{await f.close();}});
  const files=['js/design.js',...pages];const result={status:results.every(x=>x.passed)?'PASS':'FAIL',baseline:!!baseline,results,source_sha256:Object.fromEntries(files.map(p=>[p,crypto.createHash('sha256').update(fs.readFileSync(path.join(sourceRoot,p))).digest('hex')])),scope:{actual_html_before_first_stylesheet:true,actual_complete_design_module:true,account_adapter:'controlled read-only deferred fixture',real_browser_first_frames:false,real_sms_or_financial_actions:0,model_calls:0}};
  if(output){fs.mkdirSync(output,{recursive:true});fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(result,null,2)+'\n');}
  for(const row of results)process.stdout.write((row.passed?'PASS ':'FAIL ')+row.id+(row.passed?'':' '+row.error.split('\n')[0])+'\n');
  process.stdout.write(JSON.stringify({status:result.status,passed:results.filter(x=>x.passed).length,failed:results.filter(x=>!x.passed).length})+'\n');process.exitCode=result.status==='PASS'?0:1;
})().catch(error=>{process.stderr.write(error.stack+'\n');process.exitCode=1;});

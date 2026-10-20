'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom');
const source=fs.readFileSync(path.join(__dirname,'../js/research_workspace.js'),'utf8');
const passed=[];
async function check(name,run){
  const dom=new JSDOM('',{url:'https://navren.invalid/recommend.html',runScripts:'outside-only'}),w=dom.window;
  let identity={registered_user_id:'a'.repeat(32),workspace_id:'b'.repeat(32),privacy_revision:1,auth_subject:'registered-fixture',csrf:'fixture-only'},view=1;
  w.AbortController=AbortController;
  w.BankClient={identity:()=>identity,ensure:async()=>identity,guard:()=>{const stamp=JSON.stringify(identity),revision=view;return {valid:()=>stamp===JSON.stringify(identity)&&revision===view};}};
  w.NavrenViews={revision:()=>view};w.eval(source);
  const calls=[],contract=w.ResearchWorkspace.contract;
  const response=(data,status=200)=>({ok:status===200,status,headers:new Headers(),json:async()=>data});
  const modern=url=>response(url.includes('/catalog?')?{contract,data_scope:'public',provider_revision:'fixture-provider',items:[]}:{contract,...identity,workspace_binding:identity.workspace_id,revision:0,records:[],capabilities:{private_persistence:true,public_provider_loaded:true}});
  const error=(code,status)=>response({error:{code,message:'隔离传输夹具'}},status);
  let transport=modern;
  w.fetch=async(url,options)=>{calls.push({url,headers:options.headers});return transport(url,options);};
  const c={w,calls,modern,error,setTransport:fn=>{transport=fn;},changeIdentity:()=>{identity={...identity,workspace_id:'c'.repeat(32)};},changeView:()=>{view++;}};
  try{await run(c);passed.push(name);}finally{w.ResearchWorkspace.clear();dom.window.close();}
}
async function main(){
  await check('实际旧路由400和404并发返回时稳定使用既有目录',async c=>{
    c.setTransport(url=>c.error(url.includes('/catalog?')?'INVALID_QUERY':'NOT_FOUND',url.includes('/catalog?')?400:404));
    assert.equal(await c.w.ResearchWorkspace.available(),false);assert.equal(c.w.ResearchWorkspace.probeStatus.state,'unsupported');
    const count=c.calls.length;await c.w.ResearchWorkspace.available();assert.equal(c.calls.length,count);assert.equal(c.w.ResearchWorkspace.configured,false);
    c.setTransport(c.modern);assert.equal(await c.w.ResearchWorkspace.available({force:true}),true);assert.equal(c.w.ResearchWorkspace.probeStatus.state,'ready');
  });
  await check('双404的无研究路由版本同样兼容',async c=>{
    c.setTransport(()=>c.error('NOT_FOUND',404));assert.equal(await c.w.ResearchWorkspace.available(),false);assert.equal(c.w.ResearchWorkspace.probeStatus.state,'unsupported');
  });
  await check('新公共接口正常但私人接口404属于故障不可静默隐藏',async c=>{
    c.setTransport(url=>url.includes('/catalog?')?c.modern(url):c.error('NOT_FOUND',404));assert.equal(await c.w.ResearchWorkspace.available(),false);assert.equal(c.w.ResearchWorkspace.probeStatus.state,'unavailable');
  });
  await check('503保留可重试故障且下一次恢复可直接启用',async c=>{
    c.setTransport(url=>url.includes('/catalog?')?c.error('RESEARCH_DEPENDENCY_UNAVAILABLE',503):c.modern(url));assert.equal(await c.w.ResearchWorkspace.available(),false);assert.equal(c.w.ResearchWorkspace.probeStatus.state,'unavailable');
    c.setTransport(c.modern);assert.equal(await c.w.ResearchWorkspace.available(),true);assert.ok(c.calls.filter(c=>c.url.endsWith('/workspace')).every(c=>c.headers['X-Navren-Workspace']==='b'.repeat(32)));
  });
  await check('双请求中身份拒绝不能误判为旧版本',async c=>{
    c.setTransport(url=>url.includes('/catalog?')?c.error('INVALID_QUERY',400):c.error('REGISTRATION_REQUIRED',401));assert.equal(await c.w.ResearchWorkspace.available(),false);assert.equal(c.w.ResearchWorkspace.probeStatus.state,'unavailable');assert.equal(c.w.ResearchWorkspace.probeStatus.failure.http_status,401);
  });
  await check('请求途中切换账号不采用迟到响应',async c=>{
    let first=true;c.setTransport(async url=>{const r=c.modern(url);if(first){first=false;await new Promise(r=>setTimeout(r,5));c.changeIdentity();}return r;});
    assert.equal(await c.w.ResearchWorkspace.available(),false);assert.equal(c.w.ResearchWorkspace.configured,false);assert.equal(c.w.ResearchWorkspace.probeStatus.state,'idle');
  });
  await check('仅视图变化重读一次且保留完整身份守卫',async c=>{
    let count=0;c.setTransport(async url=>{const r=c.modern(url);if(url.includes('/catalog?')&&++count===1){await new Promise(r=>setTimeout(r,5));c.changeView();}return r;});
    assert.equal(await c.w.ResearchWorkspace.available({timeout:500}),true);assert.equal(count,2);assert.equal(c.w.ResearchWorkspace.probeStatus.attempts[0].code,'RESEARCH_STALE_RESPONSE');
  });
  await check('登录初始化不返回也按整次探测期限退出',async c=>{
    c.w.BankClient.ensure=()=>new Promise(()=>{});const start=Date.now();assert.equal(await c.w.ResearchWorkspace.available({timeout:35}),false);assert.ok(Date.now()-start<300);assert.equal(c.w.ResearchWorkspace.configured,false);
  });
  await check('公开目录合同错误不可当作兼容旧版本',async c=>{
    c.setTransport(url=>url.includes('/catalog?')?{ok:true,status:200,headers:new Headers(),json:async()=>({items:[]})}:c.modern(url));assert.equal(await c.w.ResearchWorkspace.available(),false);assert.equal(c.w.ResearchWorkspace.probeStatus.state,'unavailable');
  });
  console.log(JSON.stringify({passed,models:0,scope:'兼容探测与失败隔离；不代替正式页面或整体PI验收'},null,2));
}
main().catch(e=>{console.error(e);process.exitCode=1;});

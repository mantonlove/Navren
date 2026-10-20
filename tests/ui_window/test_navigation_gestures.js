/* 全产品回退手势：真实控制器 + 禁网 JSDOM 事件夹具。
 * 虚拟时钟与几何仅验证状态机；不等于实体触控板或浏览器原生手势验收。 */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {JSDOM}=require('jsdom');
const base=path.resolve(__dirname,'../..'),read=file=>fs.readFileSync(path.join(base,file),'utf8');
const source=read('js/navigation_gestures.js'),viewsSource=read('js/view_context.js');
const sourceHash=crypto.createHash('sha256').update(source).digest('hex');
const passed=[];
async function check(name,fn){await fn();passed.push(name);}
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};

async function setup(options={}){
  const page=options.page||'mine',view=options.view||'graph';
  const html=options.htmlFile?read(options.htmlFile):'<!doctype html><html><body data-page="'+page+'"><main class="main"><div class="page-scroll"><h1 data-view-heading>Fixture</h1><div id="surface">Content</div></div></main></body></html>';
  const dom=new JSDOM(html,{url:options.url||'https://navren.invalid/'+page+'.html?view='+view,...(options.referrer?{referrer:options.referrer}:{}),runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window,d=w.document,backs=[],network=[],timers=new Map();let now=options.now||10000,timerID=0;
  w.Date.now=()=>now;Object.defineProperty(w.performance,'now',{value:()=>now});
  // JSDOM has no layout engine. Connected fixture elements receive deterministic geometry;
  // the production visible() still applies real hidden/display/visibility conditions.
  w.Element.prototype.getClientRects=function(){return this.isConnected?[{x:0,y:0,width:320,height:48,top:0,left:0,right:320,bottom:48}]:[];};
  w.setTimeout=(callback,delay=0,...args)=>{const id=++timerID;timers.set(id,{at:now+Number(delay||0),callback,args});return id;};
  w.clearTimeout=id=>timers.delete(id);w.requestAnimationFrame=callback=>w.setTimeout(()=>callback(now),16);w.cancelAnimationFrame=w.clearTimeout;
  const advance=async ms=>{const end=now+ms;for(let i=0;i<1000;i++){const next=[...timers].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];if(!next)break;now=next[1].at;timers.delete(next[0]);next[1].callback(...next[1].args);await flush();}now=end;await flush();};
  const identity={workspace_id:'gesture-fixture',privacy_revision:1};
  w.BankClient={identity:()=>({...identity}),refreshIdentity:async()=>({...identity})};
  w.NavrenApp={href:url=>url,navigate:url=>{throw Error('未预期的跨页 navigate: '+url);}};
  const realBack=w.history.back.bind(w.history);
  w.history.back=()=>{backs.push({at:now,url:w.location.href});if(options.realHistory)realBack();};
  for(const kind of ['fetch','XMLHttpRequest','WebSocket'])w[kind]=(...args)=>{network.push({kind,args});throw Error('回退夹具禁止网络');};
  if(options.storage)for(const [key,value]of Object.entries(options.storage))w.sessionStorage.setItem(key,value);
  if(options.controller!==false){w.eval(viewsSource);w.NavrenViews.register(page,async()=>{});await w.NavrenViews.start();await flush();await advance(20);}
  w.history.replaceState({...w.history.state,navrenDepth:options.depth??2},'',w.location.href);
  w.eval(source);d.dispatchEvent(new w.Event('DOMContentLoaded',{bubbles:true}));await flush();
  const surface=d.getElementById('surface')||d.querySelector('.welcome-auth-header')||d.body;
  function wheel(dx=-30,dy=0,extra={},target=surface){
    const event=new w.WheelEvent('wheel',{bubbles:true,cancelable:true,deltaMode:0,deltaX:dx,deltaY:dy,...extra});
    Object.defineProperty(event,'timeStamp',{value:now});target.dispatchEvent(event);return event;
  }
  function touch(type,points,extra={},target=surface){
    const event=new w.Event(type,{bubbles:true,cancelable:true});
    const touches=points.map(([x,y,id],index)=>({identifier:id??index,clientX:x,clientY:y,target}));
    Object.defineProperties(event,{touches:{value:touches},targetTouches:{value:touches},changedTouches:{value:touches},timeStamp:{value:now}});
    for(const [key,value]of Object.entries(extra))Object.defineProperty(event,key,{value});target.dispatchEvent(event);return event;
  }
  async function wheelStroke(extra={},target=surface){const events=[];for(let i=0;i<6;i++){events.push(wheel(-30,0,extra,target));await advance(20);}return events;}
  async function touchStroke(points=[[100,160,1],[100,240,2]],extra={},target=surface){
    touch('touchstart',points,extra,target);const events=[];
    for(const dx of [30,60,90,120,160,200]){events.push(touch('touchmove',points.map(([x,y,id])=>[x+dx,y,id]),extra,target));await advance(20);}
    touch('touchend',[],extra,target);return events;
  }
  const storage=()=>Object.fromEntries(Array.from({length:w.sessionStorage.length},(_,i)=>w.sessionStorage.key(i)).map(key=>[key,w.sessionStorage.getItem(key)]));
  return {dom,w,d,backs,network,surface,wheel,touch,wheelStroke,touchStroke,advance,storage,now:()=>now,close:()=>dom.window.close()};
}

async function setupWelcome(options={}){
  const ctx=await setup({page:'index',view:'home',depth:0,controller:false,realHistory:true,htmlFile:'outputs/welcome-scene/index.html',url:'https://navren.invalid/index.html',...options});
  const requests=[],phases=[];
  ctx.w.matchMedia=()=>({matches:false,addEventListener(){},removeEventListener(){}});
  // The real welcome controller eagerly reads its session. Keep this request in memory
  // and unresolved: no authentication endpoint is contacted and no POST can continue.
  ctx.w.fetch=(url,request={})=>{requests.push({url,method:request.method||'GET'});assert.equal(url,'/api/welcome/auth/session');assert.equal(request.method,undefined);return new Promise(()=>{});};
  const loginSource=read('js/welcome_login.js');assert.ok(loginSource.includes('export function createWelcomeLogin'));
  ctx.w.eval(loginSource.replace('export function createWelcomeLogin','function createWelcomeLogin'));
  const login=ctx.w.createWelcomeLogin({canvas:ctx.d.getElementById('welcomeScene'),onPhaseChange:phase=>phases.push(phase)});
  await flush();await ctx.advance(20);
  const click=async id=>{ctx.d.getElementById(id).dispatchEvent(new ctx.w.MouseEvent('click',{bubbles:true,cancelable:true}));await flush();await ctx.advance(20);};
  const mode=()=>ctx.w.history.state?.navrenWelcome?.view===''?'welcome':ctx.w.history.state?.navrenWelcome?.view;
  return {...ctx,login,requests,phases,click,mode,loginSource};
}

async function run(){
  async function scenario(fn,options){const ctx=await setup(options);try{await fn(ctx);assert.equal(ctx.network.length,0);}finally{ctx.close();}}
  await check('右向像素 wheel 达到真实阈值并等待松手后只回退一级',()=>scenario(async c=>{
    const events=await c.wheelStroke();assert.equal(c.backs.length,0);await c.advance(250);assert.equal(c.backs.length,1);assert.ok(events.every(event=>event.defaultPrevented));
  }));
  await check('小幅未达阈值的候选仅显示临时提示，不执行回退',()=>scenario(async c=>{
    c.wheel(-25);await c.advance(20);c.wheel(-25);const cue=c.d.querySelector('.navren-back-gesture');assert.ok(cue&&!cue.hidden);assert.equal(cue.getAttribute('aria-hidden'),'true');await c.advance(1000);assert.equal(c.backs.length,0);assert.ok(cue.hidden||!cue.isConnected);
  }));
  for(const [name,dx,dy,extra]of [
    ['反向',30,0,{}],['纵向',0,30,{}],['斜向',-20,20,{}],['线单位鼠标滚轮',-30,0,{deltaMode:1}],['页单位鼠标滚轮',-30,0,{deltaMode:2}],
    ['Ctrl 缩放',-30,0,{ctrlKey:true}],['Meta',-30,0,{metaKey:true}],['Shift',-30,0,{shiftKey:true}],['Alt',-30,0,{altKey:true}]
  ])await check(name+' 不劫持回退',()=>scenario(async c=>{const events=[];for(let i=0;i<6;i++){events.push(c.wheel(dx,dy,extra));await c.advance(20);}await c.advance(1000);assert.equal(c.backs.length,0);assert.ok(events.every(e=>!e.defaultPrevented));}));
  await check('非 cancelable 首事件保留浏览器原生处理，不产生第二次回退',()=>scenario(async c=>{
    const events=await c.wheelStroke({cancelable:false});await c.advance(1000);assert.equal(c.backs.length,0);assert.ok(events.every(e=>!e.defaultPrevented));
  }));
  await check('已取消首帧后，浏览器不可取消的同向尾帧能完成一次回退',()=>scenario(async c=>{
    const first=c.wheel(-35);assert.ok(first.defaultPrevented);await c.advance(25);const second=c.wheel(-35,0,{cancelable:false});await c.advance(25);const third=c.wheel(-35,0,{cancelable:false});assert.ok(!second.defaultPrevented&&!third.defaultPrevented);await c.advance(250);assert.equal(c.backs.length,1);
  }));
  await check('已被局部组件消费的 wheel 不再次回退',()=>scenario(async c=>{
    c.surface.addEventListener('wheel',e=>e.preventDefault());await c.wheelStroke();await c.advance(1000);assert.equal(c.backs.length,0);
  }));
  await check('无站内历史的主页不回退、不走到外部网站',()=>scenario(async c=>{
    await c.wheelStroke();await c.advance(1000);assert.equal(c.backs.length,0);assert.equal(c.w.NavrenViews.current().view,'home');
  },{view:'home',depth:0}));
  await check('外部 referrer 加浏览器历史不构成站内回退资格',()=>scenario(async c=>{
    c.w.history.pushState({navrenDepth:0},'',c.w.location.href);await c.wheelStroke();await c.advance(1000);assert.equal(c.backs.length,0);assert.equal(c.w.NavrenViews.current().view,'home');
  },{view:'home',depth:0,referrer:'https://outside.invalid/article'}));
  await check('直接进入子页时使用真实 NavrenViews.back 的主页 fallback',()=>scenario(async c=>{
    await c.wheelStroke();await c.advance(250);assert.equal(c.backs.length,0);assert.equal(c.w.NavrenViews.current().view,'home');assert.equal(c.w.history.state.navrenDepth,0);
  },{view:'settings',depth:0}));
  await check('真实同源跨页 referrer 和 history.length 支持回退',()=>scenario(async c=>{
    c.w.history.pushState({...c.w.history.state,navrenDepth:0},'',c.w.location.href);await c.wheelStroke();await c.advance(250);assert.equal(c.backs.length,1);
  },{page:'account',view:'home',depth:0,referrer:'https://navren.invalid/mine.html'}));
  for(const referrer of ['https://navren.invalid/index.html','https://navren.invalid/'])await check('欢迎入口 '+new URL(referrer).pathname+' 是合法上一页',()=>scenario(async c=>{
    c.w.history.pushState({...c.w.history.state,navrenDepth:0},'',c.w.location.href);await c.wheelStroke();await c.advance(250);assert.equal(c.backs.length,1);
  },{view:'home',depth:0,referrer}));
  await check('无视图控制器的欢迎页保留真实站内 history.back',()=>scenario(async c=>{
    c.w.history.pushState({},'',c.w.location.href);await c.wheelStroke();await c.advance(250);assert.equal(c.backs.length,1);
  },{page:'index',view:'home',depth:0,controller:false,referrer:'https://navren.invalid/mine.html'}));
  await check('欢迎页直接打开且没有站内历史时不回退',()=>scenario(async c=>{
    await c.wheelStroke();await c.advance(1000);assert.equal(c.backs.length,0);
  },{page:'index',view:'home',depth:0,controller:false}));
  await check('触屏双指同向平移，松手后只回退一级',()=>scenario(async c=>{
    const events=await c.touchStroke();await flush();assert.equal(c.backs.length,1);assert.ok(events.some(e=>e.defaultPrevented));
  }));
  await check('单指触屏平移保留原生滚动',()=>scenario(async c=>{const events=await c.touchStroke([[100,160,1]]);await c.advance(1000);assert.equal(c.backs.length,0);assert.ok(events.every(e=>!e.defaultPrevented));}));
  await check('三指触屏不充当双指回退',()=>scenario(async c=>{await c.touchStroke([[100,160,1],[100,240,2],[100,320,3]]);await c.advance(1000);assert.equal(c.backs.length,0);}));
  await check('双指反向平移不回退',()=>scenario(async c=>{
    c.touch('touchstart',[[300,160,1],[300,240,2]]);for(const dx of [30,70,120,180]){c.touch('touchmove',[[300-dx,160,1],[300-dx,240,2]]);await c.advance(30);}c.touch('touchend',[]);await c.advance(1000);assert.equal(c.backs.length,0);
  }));
  await check('双指纵向滚动不回退',()=>scenario(async c=>{
    c.touch('touchstart',[[100,160,1],[100,240,2]]);for(const dy of [30,70,120,180]){c.touch('touchmove',[[100,160+dy,1],[100,240+dy,2]]);await c.advance(30);}c.touch('touchend',[]);assert.equal(c.backs.length,0);
  }));
  await check('双指缩放即使中心向右也不回退',()=>scenario(async c=>{
    c.touch('touchstart',[[100,160,1],[100,240,2]]);for(const [dx,pinch]of [[30,15],[70,30],[130,60],[200,100]]){c.touch('touchmove',[[100+dx,160-pinch,1],[100+dx,240+pinch,2]]);await c.advance(30);}c.touch('touchend',[]);assert.equal(c.backs.length,0);
  }));
  await check('双指分叉而非一致右移不回退',()=>scenario(async c=>{
    c.touch('touchstart',[[100,160,1],[100,240,2]]);for(const dx of [30,70,120,180]){c.touch('touchmove',[[100+dx*4,160,1],[100-dx,240,2]]);await c.advance(30);}c.touch('touchend',[]);assert.equal(c.backs.length,0);
  }));
  await check('单指开始再加入第二指的整段手势不回退',()=>scenario(async c=>{
    c.touch('touchstart',[[100,160,1]]);c.touch('touchstart',[[100,160,1],[100,240,2]]);for(const dx of [50,100,150,200]){c.touch('touchmove',[[100+dx,160,1],[100+dx,240,2]]);await c.advance(30);}c.touch('touchend',[]);assert.equal(c.backs.length,0);
  }));
  await check('touchcancel 丢弃已达到门槛的候选',()=>scenario(async c=>{
    c.touch('touchstart',[[100,160,1],[100,240,2]]);c.touch('touchmove',[[180,160,1],[180,240,2]]);await c.advance(80);c.touch('touchmove',[[260,160,1],[260,240,2]]);c.touch('touchcancel',[]);await c.advance(1000);assert.equal(c.backs.length,0);
  }));
  for(const [name,html]of [
    ['输入框','<input id="protected" value="draft">'],['下拉框','<select id="protected"><option>Voice</option></select>'],['多行输入','<textarea id="protected">draft</textarea>'],['可编辑内容','<div id="protected" contenteditable="true">draft</div>'],['市场封面流','<div class="market-deck"><div id="protected">Card</div></div>'],['SVG 图谱','<svg><g id="protected"><circle r="10"/></g></svg>'],['Canvas','<canvas id="protected"></canvas>'],['拖动元素','<div id="protected" draggable="true">Drag</div>']
  ])await check(name+' 所在区域不抢 wheel 或双指手势',()=>scenario(async c=>{
    c.surface.insertAdjacentHTML('beforeend',html);const target=c.d.getElementById('protected');await c.wheelStroke({},target);await c.advance(1000);await c.touchStroke(undefined,{},target);assert.equal(c.backs.length,0);
  }));
  await check('实际 overflow 横滚内容及其子元素保留横向浏览',()=>scenario(async c=>{
    const scroller=c.d.createElement('div');scroller.style.overflowX='auto';Object.defineProperties(scroller,{clientWidth:{value:200},scrollWidth:{value:600}});const target=c.d.createElement('span');scroller.append(target);c.surface.append(scroller);await c.wheelStroke({},target);await c.advance(1000);await c.touchStroke(undefined,{},target);assert.equal(c.backs.length,0);
  }));
  await check('可见 dialog 阻止背景回退',()=>scenario(async c=>{
    c.surface.insertAdjacentHTML('beforeend','<dialog open>Modal</dialog>');await c.wheelStroke();await c.advance(1000);await c.touchStroke();assert.equal(c.backs.length,0);
  }));
  await check('未保存表单阻止手势回退',()=>scenario(async c=>{
    c.surface.insertAdjacentHTML('beforeend','<form data-navren-dirty="true">Draft</form>');await c.wheelStroke();await c.advance(1000);await c.touchStroke();assert.equal(c.backs.length,0);
  }));
  await check('私有资料锁阻止手势回退',()=>scenario(async c=>{
    c.d.documentElement.classList.add('navren-private-locked');await c.wheelStroke();await c.advance(1000);await c.touchStroke();assert.equal(c.backs.length,0);
  }));
  await check('身份核对 gate 阻止手势回退',()=>scenario(async c=>{
    c.surface.insertAdjacentHTML('beforeend','<div id="navrenIdentityGate">Retry</div>');await c.wheelStroke();await c.advance(1000);await c.touchStroke();assert.equal(c.backs.length,0);
  }));
  await check('非空编辑焦点阻止背景手势导致草稿丢失',()=>scenario(async c=>{
    const input=c.d.createElement('input');input.value='draft';c.surface.append(input);input.focus();await c.wheelStroke();await c.advance(1000);await c.touchStroke();assert.equal(c.backs.length,0);
  }));
  for(const [name,html,edit]of [
    ['普通文本','<form><input id="edited" value="initial"></form>',field=>{field.value='changed';}],
    ['checkbox','<form><input id="edited" type="checkbox"></form>',field=>{field.checked=true;}],
    ['radio','<form><input id="edited" type="radio"></form>',field=>{field.checked=true;}],
    ['下拉选择','<form><select id="edited"><option selected>A</option><option>B</option></select></form>',field=>{field.selectedIndex=1;}]
  ])await check(name+' 程序初始化或已应用筛选不误判为未保存表单',()=>scenario(async c=>{
    c.surface.insertAdjacentHTML('beforeend',html);edit(c.d.getElementById('edited'));await c.wheelStroke();await c.advance(250);assert.equal(c.backs.length,1);
  }));
  await check('未修改表单不会误判为未保存编辑',()=>scenario(async c=>{
    c.surface.insertAdjacentHTML('beforeend','<form><input value="initial"><input type="checkbox" checked><select><option>A</option><option>B</option></select></form>');await c.wheelStroke();await c.advance(250);assert.equal(c.backs.length,1);
  }));
  await check('隐藏或 disabled 表单字段不阻断正常回退',()=>scenario(async c=>{
    c.surface.insertAdjacentHTML('beforeend','<form hidden><input id="hidden-edit" value="initial"></form><form><input id="disabled-edit" disabled value="initial"></form>');c.d.getElementById('hidden-edit').value='changed';c.d.getElementById('disabled-edit').value='changed';await c.wheelStroke();await c.advance(250);assert.equal(c.backs.length,1);
  }));
  await check('隐藏 dialog 不阻断正常回退',()=>scenario(async c=>{
    c.surface.insertAdjacentHTML('beforeend','<dialog open hidden>Hidden</dialog>');await c.wheelStroke();await c.advance(250);assert.equal(c.backs.length,1);
  }));
  await check('touch 不可取消时保留原生处理',()=>scenario(async c=>{
    c.touch('touchstart',[[100,160,1],[100,240,2]],{cancelable:false});c.touch('touchmove',[[240,160,1],[240,240,2]],{cancelable:false});await c.advance(80);c.touch('touchend',[],{cancelable:false});assert.equal(c.backs.length,0);
  }));
  await check('首 wheel 单次高速越过距离也不能跳过持续时间与事件数门槛',()=>scenario(async c=>{
    c.wheel(-180);await c.advance(1000);assert.equal(c.backs.length,0);
  }));
  await check('有效 wheel 的微弱衰减尾部仍可完成同一段',()=>scenario(async c=>{
    c.wheel(-55);await c.advance(30);c.wheel(-55);await c.advance(30);c.wheel(-2);await c.advance(250);assert.equal(c.backs.length,1);
  }));
  await check('重复加载模块不会注册第二套处理器',()=>scenario(async c=>{
    c.w.eval(source);await c.wheelStroke();await c.advance(250);assert.equal(c.backs.length,1);assert.equal(c.d.querySelectorAll('.navren-back-gesture').length,1);
  }));
  await check('同一 wheel 段的惯性不会连续回退',()=>scenario(async c=>{
    await c.wheelStroke();await c.advance(250);assert.equal(c.backs.length,1);for(let i=0;i<12;i++){c.wheel(-20);await c.advance(150);}await c.advance(400);assert.equal(c.backs.length,1);await c.advance(800);await c.wheelStroke();await c.advance(250);assert.equal(c.backs.length,2);
  }));
  await check('跨页 session marker 抑制同段惯性，安静后允许新的手势',async()=>{
    const first=await setup();let storage;try{await first.wheelStroke();await first.advance(250);assert.equal(first.backs.length,1);storage=first.storage();assert.ok(storage.navren_back_gesture_until);}finally{first.close();}
    await scenario(async c=>{await c.wheelStroke();await c.advance(250);assert.equal(c.backs.length,0);await c.advance(1500);await c.wheelStroke();await c.advance(250);assert.equal(c.backs.length,1);},{storage,now:10390});
  });
  await check('wheel 候选中途切换视图，不迟到回退新页面',()=>scenario(async c=>{
    await c.wheelStroke();await c.w.NavrenViews.open('settings');await flush();await c.advance(1000);assert.equal(c.backs.length,0);assert.equal(c.w.NavrenViews.current().view,'settings');
  }));
  await check('touch 候选中途切换视图，不迟到回退新页面',()=>scenario(async c=>{
    c.touch('touchstart',[[100,160,1],[100,240,2]]);c.touch('touchmove',[[180,160,1],[180,240,2]]);await c.advance(80);c.touch('touchmove',[[260,160,1],[260,240,2]]);await c.w.NavrenViews.open('settings');c.touch('touchend',[]);await c.advance(1000);assert.equal(c.backs.length,0);assert.equal(c.w.NavrenViews.current().view,'settings');
  }));
  await check('候选提交前新增 dirty form 时安全取消',()=>scenario(async c=>{
    await c.wheelStroke();c.surface.insertAdjacentHTML('beforeend','<form data-navren-dirty="true">Draft</form>');await c.advance(1000);assert.equal(c.backs.length,0);
  }));
  await check('隐私 epoch 重置丢弃候选',()=>scenario(async c=>{
    await c.wheelStroke();c.w.dispatchEvent(new c.w.Event('navren:privacy-reset'));await c.advance(1000);assert.equal(c.backs.length,0);
  }));
  await check('window pagehide 丢弃未完成候选',()=>scenario(async c=>{
    await c.wheelStroke();c.w.dispatchEvent(new c.w.Event('pagehide'));await c.advance(1000);assert.equal(c.backs.length,0);
  }));
  await check('候选中途方向反转，整段丢弃而不是延后回退',()=>scenario(async c=>{
    c.wheel(-60);await c.advance(30);c.wheel(20);await c.advance(30);c.wheel(-90);await c.advance(1000);assert.equal(c.backs.length,0);
  }));
  await check('纵向首段后出现横向尾部，不误认作新手势',()=>scenario(async c=>{
    c.wheel(0,30);await c.advance(30);c.wheel(-90);await c.advance(30);c.wheel(-90);await c.advance(1000);assert.equal(c.backs.length,0);
  }));
  for(const name of ['pointerdown','keydown'])await check(name+' 用户操作取消尚未提交的 wheel 候选',()=>scenario(async c=>{
    await c.wheelStroke();c.surface.dispatchEvent(new c.w.Event(name,{bubbles:true}));await c.advance(1000);assert.equal(c.backs.length,0);
  }));
  await check('已断开的原目标不能提交迟到回退',()=>scenario(async c=>{
    await c.wheelStroke();c.surface.remove();await c.advance(1000);assert.equal(c.backs.length,0);
  }));
  await check('失效或过远的 session marker 不永久阻断回退',()=>scenario(async c=>{
    await c.wheelStroke();await c.advance(250);assert.equal(c.backs.length,1);
  },{storage:{navren_back_gesture_until:'999999999999999'}}));
  for(const file of ['index.html','chat.html','account.html','mine.html','recommend.html','outputs/welcome-scene/index.html'])await check(file+' 唯一接线共享 JS/CSS，既有控制器保持先加载',()=>{
    const dom=new JSDOM(read(file)),d=dom.window.document;
    try{
      const scripts=[...d.querySelectorAll('script[src]')],gesture=scripts.filter(script=>/^\/?js\/navigation_gestures\.js(?:\?|$)/.test(script.getAttribute('src')));
      const styles=[...d.querySelectorAll('link[rel="stylesheet"]')].filter(link=>/^\/?css\/navigation_gestures\.css(?:\?|$)/.test(link.getAttribute('href')));
      assert.equal(gesture.length,1);assert.equal(styles.length,1);assert.ok(d.body.contains(gesture[0]));
      const views=scripts.find(script=>/^js\/view_context\.js(?:\?|$)/.test(script.getAttribute('src')));if(views)assert.ok(scripts.indexOf(views)<scripts.indexOf(gesture[0]));
      assert.equal(new URL(gesture[0].getAttribute('src'),'https://navren.invalid').searchParams.get('ui'),sourceHash.slice(0,12),'JS 接线应使用最终hash避免缓存旧模块');
      const cssHash=crypto.createHash('sha256').update(read('css/navigation_gestures.css')).digest('hex');assert.equal(new URL(styles[0].getAttribute('href'),'https://navren.invalid').searchParams.get('ui'),cssHash.slice(0,12));
    }finally{dom.window.close();}
  });
  await check('copilot 维持即时重定向，无额外短暂手势 UI',()=>{
    const html=read('copilot.html');assert.ok(html.includes("new URL('mine.html', location.href)"));assert.ok(html.includes('location.replace(target.href)'));assert.ok(!html.includes('navigation_gestures'));
  });
  async function welcomeScenario(fn,options){const c=await setupWelcome(options);try{await fn(c);assert.equal(c.network.length,0);assert.deepEqual(c.requests.map(request=>request.method),['GET']);assert.ok(![...c.d.querySelectorAll('#welcomeExperienceAccounts input')].some(input=>input.checked),'测试不得选择体验账户');}finally{c.login.dispose();c.close();}}
  await check('实际欢迎模板初始化为唯一 welcome history 状态，无认证动作',()=>welcomeScenario(async c=>{
    assert.equal(c.mode(),'welcome');assert.equal(c.w.history.state.navrenDepth,0);assert.equal(c.w.history.length,1);assert.equal(c.d.querySelector('.welcome-copy').hidden,false);assert.equal(c.d.getElementById('welcomeAuthShell').hidden,true);
  }));
  await check('欢迎“前往”进入 login，真实 pushState 深度只加一级',()=>welcomeScenario(async c=>{
    await c.click('welcomeEnter');assert.equal(c.mode(),'login');assert.equal(c.w.history.state.navrenDepth,1);assert.equal(c.w.history.length,2);assert.equal(c.d.getElementById('welcomeLogin').dataset.mode,'login');assert.equal(new URL(c.w.location.href).searchParams.get('view'),'login');
  }));
  await check('欢迎 login 右滑通过真实 history.back/popstate 恢复 welcome',()=>welcomeScenario(async c=>{
    await c.click('welcomeEnter');await c.wheelStroke();await c.advance(250);assert.equal(c.backs.length,1);assert.equal(c.mode(),'welcome');assert.equal(c.d.querySelector('.welcome-copy').hidden,false);assert.equal(c.d.getElementById('welcomeAuthShell').hidden,true);
  }));
  await check('注册页回退只恢复上一层 login，已填写值保留',()=>welcomeScenario(async c=>{
    await c.click('welcomeEnter');const phone=c.d.getElementById('welcomePhone'),password=c.d.getElementById('welcomePassword');phone.value='13700000000';password.value='fixture-only-password';await c.click('welcomeLoginSwitch');assert.equal(c.mode(),'register');assert.equal(c.w.history.state.navrenDepth,2);await c.wheelStroke();await c.advance(250);assert.equal(c.mode(),'login');assert.equal(c.d.getElementById('welcomeLogin').dataset.mode,'login');assert.equal(phone.value,'13700000000');assert.equal(password.value,'fixture-only-password');
  }));
  await check('从注册进入体验后，回退恢复 register 而非固定 login',()=>welcomeScenario(async c=>{
    await c.click('welcomeEnter');await c.click('welcomeLoginSwitch');await c.click('welcomeExperience');assert.equal(c.mode(),'experience');assert.equal(c.w.history.state.navrenDepth,3);await c.wheelStroke();await c.advance(250);assert.equal(c.mode(),'register');assert.equal(c.d.getElementById('welcomeLogin').dataset.mode,'register');assert.equal(c.d.getElementById('welcomeCredentials').hidden,false);assert.equal(c.d.getElementById('welcomeExperienceAccounts').hidden,true);
  }));
  await check('浏览器 forward/popstate 恢复体验选择阶段而不选择账户',()=>welcomeScenario(async c=>{
    await c.click('welcomeEnter');await c.click('welcomeExperience');c.w.history.back();await c.advance(20);assert.equal(c.mode(),'login');c.w.history.forward();await c.advance(20);assert.equal(c.mode(),'experience');assert.equal(c.d.getElementById('welcomeLogin').dataset.mode,'experience');assert.equal(c.d.getElementById('welcomeExperienceAccounts').hidden,false);
  }));
  await check('体验页按钮返回手机登录也写入独立历史阶段',()=>welcomeScenario(async c=>{
    await c.click('welcomeEnter');await c.click('welcomeExperience');await c.click('welcomeExperienceReturn');assert.equal(c.mode(),'login');assert.equal(c.w.history.state.navrenDepth,3);c.w.history.back();await c.advance(20);assert.equal(c.mode(),'experience');
  }));
  for(const view of ['login','register','experience'])await check('直接 URL '+view+' 只恢复阶段，不增加伪历史或认证',()=>welcomeScenario(async c=>{
    assert.equal(c.mode(),view);assert.equal(c.w.history.state.navrenDepth,0);assert.equal(c.w.history.length,1);assert.equal(c.d.getElementById('welcomeLogin').dataset.mode,view);await c.wheelStroke();await c.advance(1000);assert.equal(c.backs.length,0);assert.equal(c.mode(),view);
  },{url:'https://navren.invalid/index.html?view='+view}));
  await check('欢迎真实 busy form 阻止手势提交，已填写值与验证状态保留',()=>welcomeScenario(async c=>{
    await c.click('welcomeEnter');const phone=c.d.getElementById('welcomePhone'),password=c.d.getElementById('welcomePassword'),form=c.d.getElementById('welcomeLoginForm');phone.value='13700000000';password.value='fixture-only-password';form.dispatchEvent(new c.w.Event('submit',{bubbles:true,cancelable:true}));await flush();assert.equal(form.getAttribute('aria-busy'),'true');await c.wheelStroke();await c.advance(1000);assert.equal(c.backs.length,0);assert.equal(c.mode(),'login');assert.equal(form.getAttribute('aria-busy'),'true');assert.equal(phone.value,'13700000000');assert.equal(password.value,'fixture-only-password');assert.equal(c.d.getElementById('welcomeLoginSubmit').disabled,true);
  }));
  await check('busy 期间浏览器原生 history.back 不丢验证状态或输入',()=>welcomeScenario(async c=>{
    await c.click('welcomeEnter');const phone=c.d.getElementById('welcomePhone'),password=c.d.getElementById('welcomePassword'),form=c.d.getElementById('welcomeLoginForm');phone.value='13700000000';password.value='fixture-only-password';form.dispatchEvent(new c.w.Event('submit',{bubbles:true,cancelable:true}));await flush();c.w.history.back();await c.advance(20);assert.equal(c.mode(),'login');assert.equal(new URL(c.w.location.href).searchParams.get('view'),'login');assert.equal(form.getAttribute('aria-busy'),'true');assert.equal(c.d.getElementById('welcomeAuthShell').hidden,false);assert.equal(phone.value,'13700000000');assert.equal(password.value,'fixture-only-password');
  }));
  await check('欢迎输入非空且正在编辑时不劫持手势',()=>welcomeScenario(async c=>{
    await c.click('welcomeEnter');const phone=c.d.getElementById('welcomePhone');phone.value='13700000000';phone.focus();await c.wheelStroke();await c.advance(1000);assert.equal(c.backs.length,0);assert.equal(c.mode(),'login');
  }));
  await check('欢迎 dispose 清除 click/popstate 控制器监听',()=>welcomeScenario(async c=>{
    await c.click('welcomeEnter');await c.click('welcomeLoginSwitch');c.login.dispose();c.w.history.back();await c.advance(20);assert.equal(c.d.getElementById('welcomeLogin').dataset.mode,'register');await c.click('welcomeLoginSwitch');assert.equal(c.d.getElementById('welcomeLogin').dataset.mode,'register');
  }));
  assert.equal(crypto.createHash('sha256').update(read('js/navigation_gestures.js')).digest('hex'),sourceHash,'生产模块在测试中改变，不能沿用旧hash验收');
  console.log(JSON.stringify({source_sha256:sourceHash,passed,limitation:'禁网事件夹具；未验证实体触控板'},null,2));
}
if(require.main===module)run().catch(error=>{console.error(error);process.exitCode=1;});

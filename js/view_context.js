/** 完整子视图与显式提问引用。历史只保留视图元数据，不保存授权或执行指令。 */
window.NavrenViews=(()=>{
  const renderers=new Map(),safePages={account:'account.html',mine:'mine.html',recommend:'recommend.html',chat:'chat.html',copilot:'copilot.html'};
  const aliases={account:{overview:'home',wealth:'holdings',records:'activity',tasks:'plans'},mine:{appearance:'settings'},recommend:{products:'research',marketHotHeading:'home'}};
  const fields=['object_kind','object_id','source_scope','portfolio_id','object_version','as_of','date','period','edition_date','revision','time_view','filters','selected','listposition','scrollposition','focustarget','return_context_id'];
  let restoreRequest=0;
  let context=null,generation=0,referenceValue=null,started=false,openingChat=false;
  const scrollRoot=()=>document.querySelector('.page-scroll')||document.querySelector('.chat-messages');
  const clone=o=>JSON.parse(JSON.stringify(o));
  function metadata(input={}){return Object.fromEntries(fields.filter(k=>input[k]!==undefined).map(k=>[k,clone(input[k])]));}
  function normalize(c){const page=c.page||document.body.dataset.page;return {...metadata(c),page,view:aliases[page]?.[c.view]||c.view||'home',...(c.workspace_id?{workspace_id:c.workspace_id,privacy_revision:c.privacy_revision}:{})};}
  function fromURL(){
    const p=new URLSearchParams(location.search),page=document.body.dataset.page;
    const c={page,view:location.hash.slice(1)||p.get('view')||'home'};
    for(const k of fields)if(!['filters','selected','listposition','scrollposition','focustarget'].includes(k)&&p.has(k))c[k]=p.get(k);
    if(p.has('portfolio'))c.portfolio_id=p.get('portfolio');
    if(p.has('calendar_on'))c.date=p.get('calendar_on');
    if(page==='mine')c.filters={graph_view:p.get('graph_view')||'overview',...(p.has('from')?{from:p.get('from')}:{}),...(p.has('to')?{to:p.get('to')}:{})};
    if(page==='recommend'&&c.view==='market_history')c.filters={category:p.get('category')||'all'};
    return normalize(c);
  }
  function urlFor(c){
    const url=new URL(safePages[c.page]||safePages.chat,location.href);
    for(const k of ['object_kind','object_id','source_scope','portfolio_id','object_version','as_of','date','period','edition_date','revision','time_view'])if(c[k]!=null)url.searchParams.set(k,String(c[k]));
    url.searchParams.set('view',c.view);url.hash=c.view==='home'?'':c.view;
    if(c.page==='mine'){for(const key of ['graph_view','from','to'])if(c.filters?.[key])url.searchParams.set(key,c.filters[key]);}
    if(c.page==='recommend'&&c.view==='market_history'&&c.filters?.category)url.searchParams.set('category',c.filters.category);
    return window.NavrenApp?NavrenApp.href(url.href,typeof getInterfaceMode==='function'?getInterfaceMode():undefined):url.pathname+url.search+url.hash;
  }
  function savePosition(){
    if(!context)return;context.scrollposition=scrollRoot()?.scrollTop||0;
    const active=document.activeElement;context.focustarget=active?.id||null;
    history.replaceState({...history.state,navren:clone(context)},'',location.href);
  }
  async function render(c,restore=false){
    generation++;const turn=generation;
    context=normalize(c);const identity=window.BankClient?.identity();
    if(identity)Object.assign(context,{workspace_id:identity.workspace_id,privacy_revision:identity.privacy_revision});
    referenceValue=null;
    window.dispatchEvent(new CustomEvent('navren:view',{detail:clone(context)}));
    await renderers.get(context.page)?.(clone(context));
    if(turn!==generation)return;
    requestAnimationFrame(()=>{
      if(turn!==generation)return;
      if(scrollRoot())scrollRoot().scrollTop=restore?(context.scrollposition||0):0;
      const focus=restore&&context.focustarget?document.getElementById(context.focustarget):document.querySelector('[data-view-heading]');
      if(focus&&!focus.closest('[hidden]')){if(!focus.hasAttribute('tabindex'))focus.tabIndex=-1;focus.focus({preventScroll:true});}
    });
  }
  async function open(view,meta={},options={}){
    restoreRequest++;savePosition();let next=normalize({page:document.body.dataset.page,view,...meta});
    if(document.body.dataset.page==='account'&&view==='public'){
      NavrenApp.navigate(urlFor({page:'recommend',view:'research'}));return;
    }
    if(next.page!==document.body.dataset.page){NavrenApp.navigate(urlFor(next));return;}
    next={...next,workspace_id:BankClient.identity()?.workspace_id,privacy_revision:BankClient.identity()?.privacy_revision};
    history[options.replace?'replaceState':'pushState']({...history.state,navren:clone(next),navrenDepth:(history.state?.navrenDepth||0)+(options.replace?0:1)},'',urlFor(next));
    if(options.render!==false)await render(next);else context=next;
  }
  function update(meta,options={replace:true}){
    if(!context)return;context=normalize({...context,...metadata(meta)});
    history[options.replace===false?'pushState':'replaceState']({...history.state,navren:clone(context)},'',urlFor(context));
  }
  function back(){
    let internalOrigin=false;try{const origin=new URL(document.referrer);internalOrigin=origin.origin===location.origin&&Object.values(safePages).includes(origin.pathname.split('/').pop());}catch(_){}
    if(history.state?.navrenDepth>0||internalOrigin&&history.length>1)history.back();else open('home',{}, {replace:true});
  }
  async function verify(){
    const session=await BankClient.refreshIdentity();
    if(context&&(context.workspace_id&&context.workspace_id!==session.workspace_id||context.privacy_revision!=null&&context.privacy_revision!==session.privacy_revision)){
      context=null;window.dispatchEvent(new Event('navren:privacy-reset'));
    }
    return session;
  }
  async function restore(){
    const request=++restoreRequest;
    generation++;referenceValue=null;
    const root=scrollRoot();document.documentElement.classList.add('navren-private-locked');if(root)root.setAttribute('aria-busy','true');
    try{
      const identity=await verify();if(request!==restoreRequest)return;let saved=history.state?.navren;
      try{const returned=JSON.parse(sessionStorage.getItem('navren_return_view')||'null');if(returned?.page===document.body.dataset.page){sessionStorage.removeItem('navren_return_view');if(returned.workspace_id===identity.workspace_id&&returned.privacy_revision===identity.privacy_revision)saved=returned;}}catch(_){}
      const valid=saved?.page===document.body.dataset.page&&saved.view===fromURL().view&&saved.workspace_id===identity.workspace_id&&saved.privacy_revision===identity.privacy_revision;
      const next=valid?saved:fromURL();
      if(next.page==='account'&&next.view==='public'){NavrenApp.navigate(urlFor({page:'recommend',view:'research'}));return;}
      history.replaceState({...history.state,navren:{...next,workspace_id:identity.workspace_id,privacy_revision:identity.privacy_revision}},'',urlFor(next));
      await render(next,valid);
      if(request===restoreRequest){document.documentElement.classList.remove('navren-private-locked');document.getElementById('navrenIdentityGate')?.remove();}
    }catch(e){
      if(request!==restoreRequest)return;
      const box=document.createElement('div');box.id='navrenIdentityGate';box.className='view-identity-error';box.role='alert';box.textContent='账户身份暂时无法核对，请重试。';
      const b=document.createElement('button');b.type='button';b.textContent='重新核对';b.onclick=()=>{box.remove();restore();};box.append(b);document.getElementById('navrenIdentityGate')?.remove();document.querySelector('.main')?.append(box);
    }finally{if(request===restoreRequest)root?.removeAttribute('aria-busy');}
  }
  function reference(ref){
    referenceValue=ref?clone(ref):null;
    if(!ref)return Promise.resolve();
    return ask('',referenceValue).catch(error=>{if(typeof showToast==='function')showToast(error.message);});
  }
  async function ask(question,ref=referenceValue){
    if(openingChat||!question?.trim()&&!ref)return;
    openingChat=true;
    try{
      const before=BankClient.identity(),view=generation;
      const session=await verify();
      if(view!==generation||before&&(before.workspace_id!==session.workspace_id||before.privacy_revision!==session.privacy_revision))throw new Error('账户或资料授权已变化，请重新选择引用后提问。');
      savePosition();const guard=BankClient.guard();
      const p=await BankClient.post('/page-contexts',{question:question?.trim()||'',draft_only:true,reference:ref?clone(ref):null,return_view:context||{page:document.body.dataset.page,view:'home'}});
      if(!guard.valid(p))throw new Error('账户或当前视图已变化，请重新提问。');
      NavrenApp.navigate('chat.html?context='+encodeURIComponent(p.context_id));
    }finally{openingChat=false;}
  }
  async function start(){if(started)return;started=true;await restore();}
  window.addEventListener('popstate',()=>{if(started)restore();});
  window.addEventListener('hashchange',()=>{if(started&&history.state?.navren?.view!==fromURL().view)restore();});
  window.addEventListener('navren:privacy-reset',()=>{generation++;referenceValue=null;window.NavrenMarketContext=null;window.NavrenGraphContext=null;});
  window.addEventListener('pageshow',e=>{if(started&&e.persisted)restore();});
  window.addEventListener('focus',()=>{if(started)verify().catch(()=>{});});
  window.addEventListener('pagehide',savePosition);
  return {register:(page,renderer)=>renderers.set(page,renderer),start,open,update,back,reference,ask,url:urlFor,current:()=>context?clone(context):null,snapshot:()=>{savePosition();return context?clone(context):null;},revision:()=>generation,verify};
})();

window.addEventListener('beforeunload',event=>{if(document.querySelector('form[data-navren-dirty="true"]')){event.preventDefault();event.returnValue='';}});

window.addEventListener('pagehide',()=>{document.documentElement.classList.add('navren-private-locked');});
window.addEventListener('pageshow',async event=>{
  if(!event.persisted||!['chat','copilot'].includes(document.body.dataset.page))return;
  const before=BankClient.identity();
  const retry=async()=>{try{
    const current=await BankClient.refreshIdentity();
    if(before&&(before.workspace_id!==current.workspace_id||before.privacy_revision!==current.privacy_revision)){location.reload();return;}
    document.documentElement.classList.remove('navren-private-locked');document.getElementById('navrenIdentityGate')?.remove();
  }catch(_){
    document.getElementById('navrenIdentityGate')?.remove();
    const gate=document.createElement('div');gate.id='navrenIdentityGate';gate.className='view-identity-error';gate.role='alert';gate.textContent='账户身份暂时无法核对，请重新核对后继续。';
    const button=document.createElement('button');button.type='button';button.textContent='重新核对';button.onclick=retry;gate.append(button);document.querySelector('.main')?.append(gate);
  }};
  await retry();
});

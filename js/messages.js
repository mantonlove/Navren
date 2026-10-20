/** 持久消息的展示与已读操作。金融执行仍由关联对象的确认流程负责。 */
window.NavrenMessages = (() => {
  'use strict';
  const categories={all:'全部分类',payments:'收付款',due:'到期事项',security:'安全',service:'服务'};
  const validStatuses=new Set(['pending','completed','cancelled','declined','info']);
  const words={
    '消息':'Messages','正在核对消息…':'Loading messages…','全部已读':'Mark all read','刷新消息':'Refresh messages',
    '全部':'All','未读':'Unread','已读':'Read','消息视图':'Message view','消息分类':'Message category','消息列表':'Message list',
    '全部分类':'All categories','收付款':'Payments','到期事项':'Due dates','安全':'Security','服务':'Service',
    '加载更多':'Load more','正在读取…':'Loading…','查看账单':'View bill','查看事项':'View event','查看账户':'View account',
    '查看回执':'View receipt','查看详情':'View details','待核实':'To verify','本人设置':'Set by you','截止':'Due',
    '关闭':'Close','暂无消息':'No messages yet','新的收付款和到期提醒会显示在这里。':'New payment updates and due reminders will appear here.',
    '没有未读消息':'You are all caught up','所有消息均已阅读。':'All messages have been read.',
    '这个分类暂无消息':'No messages in this category','可以切换分类，查看其他消息。':'Choose another category to see other messages.',
    '消息暂时无法读取':'Messages are temporarily unavailable','请重新读取，核对当前消息状态。':'Reload to check the latest message status.',
    '重新读取':'Reload','账户或资料授权已变化':'Your account or permissions have changed',
    '重新核对后再查看消息。':'Verify your identity before viewing messages.', '重新核对':'Verify identity',
    '已标记为已读。':'Marked as read.','现有消息已全部标记为已读。':'All existing messages marked as read.',
    '消息资料无法核对，请重新读取。':'Message data could not be verified. Please reload.',
    '消息的账户或授权版本已变化，请重新核对。':'The account or permission version changed. Please verify your identity.',
    '正在打开详情…':'Opening details…','详情暂时无法读取，请重新读取。':'Details are temporarily unavailable. Please reload.',
    '元':'CNY','日期待核实':'Date to verify'
  };
  const iconPaths={payments:'<path d="M4 7h16M16 3l4 4-4 4M20 17H4m4-4-4 4 4 4"/>',due:'<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M7 3v4m10-4v4M3 11h18m-10 4h4"/>',security:'<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-5"/>',service:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10h.01"/>',empty:'<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/>'};
  const state={view:'all',category:'all',items:[],unread_count:null,pending_count:null,has_more:false,offset:0,limit:30,as_of:null};
  let epoch=0,busy=false,mutating=false,openingDetail=false,destroyed=false,revoked=false,detailDialog=null,phase='loading',statusText='',statusError=false;
  const byId=id=>document.getElementById(id);
  const t=value=>window.NavrenI18n?.language==='en'?(words[value]||value):value;
  function node(tag,text,cls){const e=document.createElement(tag);if(text!=null)e.textContent=t(text);if(cls)e.className=cls;return e;}
  function button(text,fn,cls){const b=node('button',text,cls);b.type='button';b.onclick=fn;return b;}
  function icon(kind){const e=document.createElementNS('http://www.w3.org/2000/svg','svg');e.setAttribute('viewBox','0 0 24 24');e.setAttribute('aria-hidden','true');e.innerHTML=iconPaths[kind]||iconPaths.service;return e;}
  function error(message){const e=new Error(t(message));e.code='MESSAGE_DATA_INVALID';return e;}
  function count(n){return Number.isSafeInteger(n)&&n>=0;}
  function validateItem(item){
    if(!item||typeof item.id!=='string'||!/^[a-f0-9]{32}$/.test(item.id)||!Object.hasOwn(categories,item.category)||item.category==='all'||!validStatuses.has(item.status)||typeof item.title!=='string'||typeof item.body!=='string'||item.title.length>2000||item.body.length>30000||
      (item.amount_minor!=null&&!Number.isSafeInteger(item.amount_minor))||(item.counterparty!=null&&(!item.counterparty||typeof item.counterparty.display_name!=='string'||typeof item.counterparty.user_id!=='string'))||!['confirmed','candidate','user_defined'].includes(item.certainty)||
      (item.action!=null&&(!['aa_bill','calendar','account','operation'].includes(item.action.type)||typeof item.action.object_id!=='string'||!item.action.object_id||item.action.object_id.length>400)))throw error('消息资料无法核对，请重新读取。');
    return item;
  }
  function validateRead(data,id){if(!data||!count(data.unread_count)||!count(data.pending_count))throw error('消息资料无法核对，请重新读取。');const item=validateItem(data.item);if(item.id!==id||item.read_at==null)throw error('消息资料无法核对，请重新读取。');return item;}
  function validateList(data,offset){
    if(!data||!Array.isArray(data.items)||!count(data.unread_count)||!count(data.pending_count)||data.offset!==offset||data.limit!==state.limit||typeof data.has_more!=='boolean'||data.items.length>state.limit||data.has_more&&data.items.length!==state.limit)throw error('消息资料无法核对，请重新读取。');
    const ids=new Set();for(const item of data.items){validateItem(item);if(ids.has(item.id)||state.category!=='all'&&item.category!==state.category||state.view==='unread'&&item.read_at!=null)throw error('消息资料无法核对，请重新读取。');ids.add(item.id);}return data;
  }
  function dateValue(value){
    if(value==null)return null;
    if(typeof value==='number'&&Number.isFinite(value))return new Date(value<100000000000?value*1000:value);
    if(typeof value==='string'&&/^\d{4}-\d{2}-\d{2}(?:T|$)/.test(value))return new Date(value.length===10?value+'T12:00:00+08:00':value);
    return null;
  }
  function formatDate(value,withTime=false){const d=dateValue(value);if(!d||!Number.isFinite(d.getTime()))return t('日期待核实');return new Intl.DateTimeFormat(window.NavrenI18n?.language==='en'?'en-GB':'zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',...(withTime?{hour:'2-digit',minute:'2-digit',hour12:false}:{})}).format(d);}
  function amount(item){
    const e=node('span',null,'mc-card-amount');e.textContent=new Intl.NumberFormat(window.NavrenI18n?.language==='en'?'en-GB':'zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2}).format(item.amount_minor/100);
    e.append(node('small',item.currency==='CNY'||item.currency==null?'元':item.currency));return e;
  }
  function status(text,isError=false){statusText=text||'';statusError=isError;const host=byId('messageStatus');host.textContent=t(statusText);host.dataset.error=String(isError);}
  function syncControls(){
    document.title='Navren · '+t('消息');
    const all=byId('messageReadAll');all.textContent=t('全部已读');all.disabled=busy||mutating||openingDetail||revoked||!count(state.unread_count)||state.unread_count===0;
    byId('messageRefresh').disabled=busy||mutating||openingDetail;byId('messageRefresh').setAttribute('aria-label',t('刷新消息'));byId('messageRefresh').title=t('刷新消息');
    document.querySelectorAll('[data-message-view]').forEach(b=>{b.setAttribute('aria-pressed',String(b.dataset.messageView===state.view));b.disabled=mutating||openingDetail||revoked;b.textContent=t(b.dataset.messageView==='all'?'全部':'未读');});
    for(const option of byId('messageCategory').options){const label=t(categories[option.value]);if(option.textContent!==label)option.textContent=label;}byId('messageCategory').value=state.category;byId('messageCategory').disabled=mutating||openingDetail||revoked;
    byId('messageCategory').setAttribute('aria-label',t('消息分类'));
    byId('messageViews').setAttribute('aria-label',t('消息视图'));byId('messageList').setAttribute('aria-label',t('消息列表'));byId('messageMore').textContent=t(busy?'正在读取…':'加载更多');byId('messageMore').disabled=busy||mutating||openingDetail;
    byId('messageList').querySelectorAll('.mc-card-open').forEach(control=>control.disabled=busy||mutating||openingDetail||revoked);
    document.querySelectorAll('[data-message-label]').forEach(e=>e.textContent=t(e.dataset.messageLabel));
    document.querySelector('[data-navren-messages] .nav-label').textContent=t('消息');
    const summary=byId('messageSummary');
    if(revoked)summary.textContent=t('账户或资料授权已变化');
    else if(count(state.unread_count))summary.textContent=window.NavrenI18n?.language==='en'?`${state.unread_count} unread`:`${state.unread_count} 条未读`;
    else summary.textContent=t(phase==='error'?'消息暂时无法读取':'正在核对消息…');
  }
  function publishSummary(data){window.dispatchEvent(new CustomEvent('navren:message-summary',{detail:{workspace_id:data.workspace_id,privacy_revision:data.privacy_revision,unread_count:data.unread_count,pending_count:data.pending_count}}));}
  function adopt(data){state.unread_count=data.unread_count;state.pending_count=data.pending_count;state.as_of=data.as_of;publishSummary(data);}
  function empty(title,body,retry){const box=node('div',null,'mc-empty'),picture=node('div',null,'mc-empty-icon');picture.append(icon('empty'));box.append(picture,node('h3',title),node('p',body));if(retry)box.append(button(retry[0],retry[1],'mc-secondary-button'));return box;}
  function renderEmpty(){
    if(state.view==='unread')return empty('没有未读消息','所有消息均已阅读。');
    if(state.category!=='all')return empty('这个分类暂无消息','可以切换分类，查看其他消息。');
    return empty('暂无消息','新的收付款和到期提醒会显示在这里。');
  }
  function metadata(item){
    const meta=node('div',null,'mc-card-meta');
    const unread=item.read_at==null,readState=node('span',unread?'未读':'已读','mc-read-state');readState.dataset.readState=unread?'unread':'read';meta.append(readState);
    if(item.certainty==='candidate')meta.append(node('span','待核实'));else if(item.certainty==='user_defined')meta.append(node('span','本人设置'));
    if(item.counterparty)meta.append(node('span',item.counterparty.display_name));
    if(item.due_at){const due=node('span');due.textContent=t('截止')+' '+formatDate(item.due_at);meta.append(due);}
    return meta;
  }
  function appendBody(host,item){
    const reminder='这是现有账本的待办/计划日期，提醒不代表已支付或已到账，请核对当前事项。';
    if(item.body&&item.body!==reminder&&item.body!=='原日期已过。'+reminder)host.append(node('p',item.body,'mc-card-body'));
  }
  function focusCard(id){const card=[...byId('messageList').querySelectorAll('[data-message-id]')].find(e=>e.dataset.messageId===id);(card?.querySelector('.mc-card-open')||byId('messageCenterHeading')).focus({preventScroll:true});}
  function card(item){
    const e=node('article',null,'mc-card');e.role='listitem';e.dataset.messageId=item.id;e.dataset.unread=String(item.read_at==null);
    const pic=node('div',null,'mc-card-icon');pic.append(icon(item.category));const main=node('div',null,'mc-card-main'),top=node('div',null,'mc-card-top'),category=node('span',null,'mc-category-label');
    category.append(node('span',categories[item.category]));
    const time=node('time',formatDate(item.created_at,true));const d=dateValue(item.created_at);if(d&&Number.isFinite(d.getTime()))time.dateTime=d.toISOString();top.append(category,time);
    const title=node('div',null,'mc-card-title-row');title.append(node('h3',item.title));if(item.amount_minor!=null)title.append(amount(item));
    main.append(top,title);appendBody(main,item);main.append(metadata(item));
    const open=button('',null,'mc-card-open');open.setAttribute('aria-label',t('查看详情')+'：'+item.title);
    e.onclick=()=>openDetail(item.id,open);e.append(pic,main,open);return e;
  }
  function syncReadItem(item){
    const id=item.id,index=state.items.findIndex(i=>i.id===id),list=byId('messageList');
    if(index!==-1){
      const row=[...list.querySelectorAll('[data-message-id]')].find(e=>e.dataset.messageId===id);
      if(state.view==='unread'){state.items.splice(index,1);state.offset=Math.max(0,state.offset-1);row?.remove();if(!state.items.length)list.replaceChildren(renderEmpty());}
      else{state.items[index]=item;row?.replaceWith(card(item));}
    }
    syncControls();
  }
  function paint(){const list=byId('messageList');if(revoked)list.replaceChildren();else if(phase==='loading')list.replaceChildren(empty('正在核对消息…',''));else if(phase==='error')list.replaceChildren(empty('消息暂时无法读取','请重新读取，核对当前消息状态。',['重新读取',()=>load({verify:true})]));else list.replaceChildren(...(state.items.length?state.items.map(card):[renderEmpty()]));byId('messagePagination').hidden=revoked||phase!=='ready'||!state.has_more;syncControls();status(statusText,statusError);}
  function current(token,guard,data){return !destroyed&&!revoked&&token===epoch&&guard.valid(data);}
  async function load({append=false,verify=false,background=false,detail=false}={}){
    if(openingDetail&&!detail)return;
    if(background&&(busy||mutating||openingDetail||revoked||destroyed||document.visibilityState!=='visible'||byId('messageCategoryControl')?.getAttribute('aria-expanded')==='true'))return;
    const target=background?Math.max(state.offset,state.limit):state.limit;
    const token=++epoch;busy=true;if(!background)status('');byId('messageList').setAttribute('aria-busy','true');
    if(!append&&!background){phase='loading';state.items=[];state.offset=0;state.has_more=false;byId('messageList').replaceChildren(empty('正在核对消息…',''));byId('messagePagination').hidden=true;}
    syncControls();
    try{
      if(verify)await BankClient.refreshIdentity();else await BankClient.ensure();if(token!==epoch||destroyed||revoked)return;
      const guard=BankClient.guard();let offset=append?state.offset:0,data,items=[];const ids=new Set(append?state.items.map(i=>i.id):[]);
      do{
        const query=new URLSearchParams({view:'all',category:state.category,unread:String(state.view==='unread'),offset:String(offset),limit:String(state.limit)});
        data=await BankClient.get('/messages?'+query);if(!current(token,guard,data))return;validateList(data,offset);
        if(data.items.some(i=>ids.has(i.id)))throw error('消息资料无法核对，请重新读取。');for(const item of data.items)ids.add(item.id);items.push(...data.items);offset+=data.items.length;
      }while(background&&data.has_more&&offset<target);
      if(background&&byId('messageCategoryControl')?.getAttribute('aria-expanded')==='true')return;
      const active=document.activeElement,focusedCard=active?.closest?.('[data-message-id]'),focus=focusedCard?{id:focusedCard.dataset.messageId}:{controlId:active?.id},scroll=background?document.querySelector('.page-scroll')?.scrollTop:null;
      state.items=append?[...state.items,...items]:items;state.offset=offset;state.has_more=data.has_more;phase='ready';adopt(data);busy=false;status('');paint();
      if(background){if(scroll!=null)document.querySelector('.page-scroll').scrollTop=scroll;if(focus.id)focusCard(focus.id);else if(focus.controlId){const control=byId(focus.controlId);if(control&&!control.disabled&&control.getClientRects().length)control.focus({preventScroll:true});}}
    }catch(e){if(token!==epoch||destroyed||revoked)return;busy=false;if(!append&&!background)phase='error';status('消息暂时无法读取',true);if(background)syncControls();else paint();}
    finally{if(token===epoch){busy=false;byId('messageList').setAttribute('aria-busy','false');syncControls();}}
  }
  async function readAll(){
    if(mutating||openingDetail||busy||revoked)return;mutating=true;syncControls();const token=epoch,guard=BankClient.guard();
    try{const data=await BankClient.post('/messages/read-all',{});if(!current(token,guard,data))return;if(!count(data.updated_count)||!count(data.unread_count)||!count(data.pending_count))throw error('消息资料无法核对，请重新读取。');await load();if(!revoked)status('现有消息已全部标记为已读。');}
    catch(e){if(current(token,guard))status(e.message||t('消息暂时无法读取'),true);}finally{mutating=false;syncControls();}
  }
  function actionURL(item){
    const action=item.action;if(!action)return null;const url=new URL('account.html',location.href),q=url.searchParams;
    if(action.type==='aa_bill'){q.set('view','home');q.set('focus_aa',action.object_id);}
    else if(action.type==='calendar'){q.set('view','calendar-item');q.set('object_kind','calendar_item');q.set('object_id',action.object_id);q.set('source_scope','simulation');if(action.date&&/^\d{4}-\d{2}-\d{2}$/.test(action.date))q.set('calendar_on',action.date);}
    else if(action.type==='operation'){q.set('view','operation-detail');q.set('object_kind','operation');q.set('object_id',action.object_id);q.set('source_scope','simulation');}
    else if(action.type==='account'){
      const kind=action.object_kind||'account',views={account:'account-detail',card:'card-detail',loan:'record-detail',subscription:'subscription-detail',mandate:'subscription-detail',scheduled_payment:'payment-detail',order:'activity',policy:'archive'};
      if(!Object.hasOwn(views,kind))return null;q.set('view',views[kind]);q.set('object_kind',kind);q.set('object_id',action.object_id);q.set('source_scope',action.source_scope==='connected_bank'?'connected_bank':'simulation');
    }
    return window.NavrenApp?.href?NavrenApp.href(url.pathname+url.search+url.hash,typeof getInterfaceMode==='function'?getInterfaceMode():undefined):url.pathname+url.search+url.hash;
  }
  function closeDetail(){if(detailDialog){detailDialog.close();detailDialog.remove();detailDialog=null;}}
  function showDetail(item,trigger){
    closeDetail();const d=node('dialog',null,'mc-detail-dialog');d.dataset.i18nSkip='';const head=node('div',null,'mc-detail-head'),heading=node('h2',item.title);heading.id='messageDetailHeading';d.setAttribute('aria-labelledby',heading.id);
    const close=button('×',()=>{closeDetail();focusCard(item.id);},'mc-icon-button');close.setAttribute('aria-label',t('关闭'));head.append(heading,close);d.append(head);appendBody(d,item);if(item.amount_minor!=null)d.append(amount(item));d.append(metadata(item));
    d.addEventListener('cancel',()=>{closeDetail();focusCard(item.id);});document.body.append(d);detailDialog=d;d.showModal();close.focus();
  }
  async function openDetail(id,trigger){
    if(mutating||openingDetail||busy||revoked)return;openingDetail=true;trigger.disabled=true;syncControls();const token=epoch,guard=BankClient.guard();status('正在打开详情…');
    try{
      const data=await BankClient.get('/messages/'+encodeURIComponent(id));if(!current(token,guard,data))return;let item=validateItem(data.item);if(item.id!==id)throw error('消息资料无法核对，请重新读取。');
      if(item.read_at==null){const marked=await BankClient.post('/messages/'+encodeURIComponent(id)+'/read',{});if(!current(token,guard,marked))return;item=validateRead(marked,id);syncReadItem(item);adopt(marked);}
      else{
        const stale=state.items.some(i=>i.id===id&&i.read_at==null);syncReadItem(item);
        if(stale){const summary=await BankClient.get('/messages/summary');if(!current(token,guard,summary))return;if(!count(summary.unread_count)||!count(summary.pending_count))throw error('消息资料无法核对，请重新读取。');adopt(summary);}
      }
      const target=actionURL(item);status('');if(target){if(window.NavrenApp)NavrenApp.navigate(target);else location.assign(target);return;}
      showDetail(item,trigger);await load({detail:true});
    }catch(e){if(current(token,guard))status(e.message||t('详情暂时无法读取，请重新读取。'),true);}finally{openingDetail=false;if(trigger.isConnected)trigger.disabled=false;syncControls();}
  }
  function invalidate(){epoch++;busy=mutating=false;revoked=true;state.items=[];state.unread_count=state.pending_count=null;state.has_more=false;state.offset=0;closeDetail();byId('messagePagination').hidden=true;byId('messageList').replaceChildren();byId('messageIdentityGate')?.remove();const gate=empty('账户或资料授权已变化','重新核对后再查看消息。',['重新核对',()=>location.reload()]);gate.id='messageIdentityGate';gate.classList.add('mc-identity-gate');gate.role='alert';document.querySelector('.main').append(gate);status('');syncControls();}
  function init(){
    document.querySelectorAll('[data-message-view]').forEach(b=>b.addEventListener('click',()=>{if(mutating||openingDetail||revoked)return;state.view=b.dataset.messageView;load();}));
    byId('messageCategory').addEventListener('change',()=>{if(mutating||openingDetail||revoked)return;state.category=byId('messageCategory').value;load();});
    byId('messageRefresh').onclick=()=>revoked?location.reload():load({verify:true});byId('messageMore').onclick=()=>load({append:true});byId('messageReadAll').onclick=readAll;
    window.addEventListener('navren:privacy-reset',invalidate);window.addEventListener('navren:identity-revoked',invalidate);window.addEventListener('navren:account-switched',invalidate);
    window.addEventListener('navren:language',()=>{paint();if(revoked){const gate=byId('messageIdentityGate');if(gate){gate.querySelector('h3').textContent=t('账户或资料授权已变化');gate.querySelector('p').textContent=t('重新核对后再查看消息。');gate.querySelector('button').textContent=t('重新核对');}}if(detailDialog)closeDetail();});
    window.addEventListener('navren:message-summary-updated',event=>{const data=event.detail,identity=BankClient.identity();if(destroyed||revoked||busy||mutating||openingDetail||document.visibilityState!=='visible'||!identity||data?.workspace_id!==identity.workspace_id||data.privacy_revision!==identity.privacy_revision||!count(data.unread_count)||!count(data.pending_count))return;load({background:true});});
    window.addEventListener('pagehide',()=>{destroyed=true;epoch++;state.items=[];state.unread_count=state.pending_count=null;closeDetail();byId('messageIdentityGate')?.remove();byId('messageList').replaceChildren();syncControls();});
    window.addEventListener('pageshow',e=>{if(e.persisted){destroyed=false;revoked=false;byId('messageIdentityGate')?.remove();load({verify:true});}});
    document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible'&&!revoked&&!destroyed&&!mutating)load({verify:true});});
    load();
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
  return {refresh:()=>load({verify:true})};
})();

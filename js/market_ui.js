/* 热点、周报、原文与对话共用对象ID，公共只读缓存和安全DOM。 */
window.MarketUI=(()=>{
  const recoveryEnglish={'当日暂无已核验的新报道，可查看历史热点。':'No verified new reports today. View past highlights.','更新暂未成功，仅保留当日已核验报道。':'Update unavailable. Only verified reports published today are retained.','原文发布':'Original published','原文更新':'Original updated','精选日期':'Selected on','日期待核验':'Date not verified','解读已修订':'Insights revised','解读版本':'Insights version','发布依据不完整，需重建后重新审核。':'Publication evidence is incomplete. The report needs rebuilding and review.','需要重建并重新审核，当前不可读。':'Rebuilding and review are required before this report can be read.','原文已修订，需重新审核。':'The original sources have changed. Review is required.','原文授权已撤回，当前不可读。':'Source access has been revoked. This report is unavailable.','原文暂不可读取，当前周报不可读。':'The original sources and this report are currently unavailable.','最近一次更新失败。':'The latest update failed.','暂无可用的已核验来源。':'No verified sources are currently available.','尚无通过核验的本期周报。':'No verified report is available for this issue.','本期周报未通过发布核验，当前不可读。':'This issue has not passed publication checks and is unavailable.','本期正在更新，尚未完成发布。':'This issue is being updated and is not published yet.','本期期号':'Current issue','最近可读周报':'Latest readable report','最近留存周报':'Latest stored report','未通过当前发布核验。':'Current publication checks have not passed.','下次检查':'Next check','可读':'Readable','暂无':'None','第':'v','版':''};
  Object.assign(recoveryEnglish,{'最近精选':'Latest highlights','当日精选':'Today’s highlights','等待每日精选':'Waiting for verified highlights','更新暂未成功，最新核验未完成，暂保留上次已核验内容。':'Update failed. Current verification is incomplete; previously verified content is temporarily retained.','本日尚无新版，展示最近已核验精选。':'No new edition today. Showing the latest verified highlights.','周报更新暂未成功，暂保留上次已核验周报。':'The report update failed. The previously verified report is temporarily retained.'});
  Object.assign(recoveryEnglish,{'市场内容正在更新。':'Market content is being updated.','市场内容正在更新，当前仍展示已核验内容。':'Market content is being updated. Verified content remains visible.','最近一次更新失败，保留已核验内容。':'The latest update failed. Verified content is retained.','最近一次更新未完整完成。':'The latest update was incomplete.','最近一次更新未完整完成，保留已核验内容。':'The latest update was incomplete. Verified content is retained.'});
  const t=text=>window.NavrenI18n?.language==='en'&&Object.hasOwn(recoveryEnglish,text)?recoveryEnglish[text]:window.NavrenI18n?.t(text)||text;
  const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  const button=(text,fn)=>{const b=el('button',text,'market-button');b.type='button';b.onclick=fn;return b;};
  const accountReady=()=>Storage.accountReady?.()===true;
  const itemKey=value=>window.NavrenAccountData.itemKey(value);
  let accountEpoch=0;
  const bookmarkWrites=new Set();
  const accountIdentity=()=>{const i=window.BankClient?.identity?.();return i&&JSON.stringify([i.registered_user_id,i.workspace_id,i.privacy_revision,i.auth_subject]);};
  const accountCurrent=(epoch,identity)=>epoch===accountEpoch&&accountReady()&&identity===accountIdentity();
  function favoriteIdentity(row){return {category:row.category||'chat_message',kind:row.object_kind||row.kind||null,id:row.object_id||row.id||null,version:String(row.version??''),time_view:row.time_view||null,source_scope:'public_market',archive:row.archive?{edition_date:row.archive.edition_date,revision:Number(row.archive.revision),event_version:Number(row.archive.event_version)}:null};}
  const favoriteFamily=row=>{const identity=favoriteIdentity(row);return identity.time_view==='historical_audit'?itemKey(identity):itemKey({category:identity.category,kind:identity.kind,id:identity.id,time_view:identity.time_view});};
  function favoriteRecord(kind,obj){const a=obj.articles?.[0]||obj.reading?.[0]||obj;return {category:kind==='weekly'?'market_weekly':'market_news',object_kind:kind,object_id:obj.id||obj.period,text:obj.headline||obj.title_cn||obj.title||obj.id||obj.period,source:a.publisher||'Navren周报',...(a.url?{url:a.url}:{}),...(obj.published_at||obj.period_start?{published_at:obj.published_at||obj.period_start}:{}),version:obj.version,source_scope:'public_market',...(obj.time_view?{time_view:obj.time_view}:{}),...(obj.archive?{archive:{edition_date:obj.archive.edition_date,revision:Number(obj.archive.revision),event_version:Number(obj.archive.event_version)}}:{}),timestamp:new Date().toISOString()};}
  async function changeFavorite(record,{toggle=false,control}={}){
    const epoch=accountEpoch,identity=accountIdentity(),write={};
    try{
      if(!accountReady())throw new Error('账号收藏尚未载入，请稍后重试。');
      const family=favoriteFamily(record),exact=itemKey(favoriteIdentity(record)),before=Storage.get('qingzhou_bookmarks')||[],matches=before.filter(row=>favoriteFamily(row)===family),found=matches.find(row=>itemKey(favoriteIdentity(row))===exact);
      if(found&&!toggle){showToast(t('已经收藏'));return true;}
      const expected=matches.map(itemKey).sort(),remove=!!found&&toggle;
      bookmarkWrites.add(write);if(control)control.dataset.accountSaving='true';syncBookmarks();
      await Storage.update('qingzhou_bookmarks',value=>{
        const rows=Array.isArray(value)?value:[],current=rows.filter(row=>favoriteFamily(row)===family).map(itemKey).sort();
        if(itemKey(current)!==itemKey(expected))throw new Error('收藏已在其他页面更新，请重新核对后操作。');
        const removed=new Set(remove?[itemKey(found)]:expected),next=rows.filter(row=>!removed.has(itemKey(row)));
        if(!remove)next.push(record);
        if(next.length>500)throw new Error('收藏已达500项，请先删除不需要的收藏。');
        return next;
      });
      if(!accountCurrent(epoch,identity))return false;
      showToast(t(remove?'已取消收藏':'已收藏'));return true;
    }catch(error){if(accountCurrent(epoch,identity)||epoch===accountEpoch&&!accountReady())showToast(error.message||t('收藏未保存，请重试。'));return false;}
    finally{bookmarkWrites.delete(write);if(control)delete control.dataset.accountSaving;syncBookmarks();}
  }
  const date=(x,precision='minute')=>x?new Date(x).toLocaleString(window.NavrenI18n?.locale||'zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',...(precision==='date'?{}:{hour:'2-digit',minute:'2-digit'})}):t('暂无核验日期');
  const allowed=new Set(['www.stats.gov.cn','www.pbc.gov.cn','www.federalreserve.gov','www.ecb.europa.eu','www.news.cn','finance.cctv.com','tv.cctv.com']);
  function safeLink(url){try{const u=new URL(url);return u.protocol==='https:'&&allowed.has(u.hostname)&&u.pathname.length>12&&(u.hostname!=='tv.cctv.com'||/^\/2026\/(?:0[1-9]|1[0-2])\/(?:0[1-9]|[12]\d|3[01])\/VIDE[A-Za-z0-9]+\.shtml$/.test(u.pathname))&&!u.username&&!u.password&&(!u.port||u.port==='443');}catch{return false;}}
  const readable=a=>!!a&&a.status==='active'&&a.link_status==='verified'&&safeLink(a.url)&&((a.read_scope==='full_text'&&a.access_status==='free_full_text')||(a.content_mode==='source_brief'&&a.read_scope==='source_brief'&&a.access_status==='free_video_with_brief'&&a.source_verified===true&&a.video_content_read===false&&a.reported_time_kind==='source_page_updated_at'&&a.source_check?.method==='official_page_title_brief_date_binding'));
  function link(text,url,article){const a=el('a',text);if(safeLink(url)&&(!article||readable(article))){a.href=url;a.target='_blank';a.rel='noopener noreferrer';}return a;}
  let marketClock=null,marketBoundaryTimer=null,marketBoundaryDue=false;
  const marketNow=()=>marketClock?marketClock.time+Math.max(0,Date.now()-marketClock.received):Date.now();
  const chinaDay=value=>new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(value));
  const validDay=value=>typeof value==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(value)&&Number.isFinite(Date.parse(value+'T00:00:00+08:00'))&&chinaDay(Date.parse(value+'T00:00:00+08:00'))===value;
  function scheduleMarketBoundary(){
    clearTimeout(marketBoundaryTimer);marketBoundaryTimer=null;if(!accountReady())return;
    const now=marketNow(),midnight=Date.parse(chinaDay(now)+'T00:00:00+08:00');
    const next=[8.5,12.5,18.5,24].map(hour=>midnight+hour*3600000).find(time=>time>now);
    marketBoundaryTimer=setTimeout(()=>{
      marketBoundaryTimer=null;marketBoundaryDue=true;rolloverHotspots();
      // 到点先读取新版；阅读面板只延后换卡，不延后定时拉取。
      if(document.visibilityState!=='hidden'&&window.NavrenViews?.current()?.time_view!=='historical_audit'){marketBoundaryDue=false;refreshMarket();}
      scheduleMarketBoundary();
    },Math.max(1,next-now));
  }
  async function get(path,options={}){const r=await fetch('/api/market/'+path,{cache:'no-store',credentials:'same-origin',signal:options.signal});const d=await r.json();if(!r.ok){const e=new Error(d.error?.message||t('市场缓存暂不可用'));e.status=r.status;e.code=d.error?.code;throw e;}if(path==='hotspots'){const headerTime=Date.parse(r.headers?.get('date')),time=Number.isFinite(headerTime)?headerTime:Date.parse(d.server_now);if(Number.isFinite(time))marketClock={time,received:Date.now()};}return path==='weeklies/latest'?dueWeekly(d):d;}
  function historyVersion(value){
    if(!['number','string'].includes(typeof value)||! /^[1-9][0-9]{0,8}$/.test(String(value))||!Number.isSafeInteger(Number(value)))throw new Error('历史版次必须是明确的正整数。');
    return Number(value);
  }
  function historySelection(input){
    const kind=input?.object_kind??input?.kind,id=input?.object_id??input?.id,version=historyVersion(input?.object_version??input?.version);
    if(input?.time_view!=='historical_audit'||!['article','weekly'].includes(kind)||typeof id!=='string'||input.source_scope!=null&&!['public','market','public_market'].includes(input.source_scope))throw new Error('缺少明确的历史资料身份与查阅模式。');
    if(kind==='article'&&!/^[A-Za-z0-9_.:-]{1,128}$/.test(id))throw new Error('历史文章身份无效。');
    if(kind==='weekly'){
      const parsed=new Date(id+'T00:00:00Z');
      if(!/^\d{4}-\d{2}-\d{2}$/.test(id)||Number(id.slice(0,4))<1||!Number.isFinite(parsed.valueOf())||parsed.toISOString().slice(0,10)!==id||input.period!=null&&input.period!==id)throw new Error('历史周报期号无效。');
    }
    return {object_kind:kind,object_id:id,object_version:version,time_view:'historical_audit',source_scope:'public_market',...(kind==='weekly'?{period:id}:{})};
  }
  function historyReference(input,label='历史市场资料'){
    const c=historySelection(input);return {kind:c.object_kind,id:c.object_id,version:c.object_version,source_scope:'market',label,parameters:{reading_mode:'historical_audit'}};
  }
  let historyEpoch=0;
  const historyRoots=new Set();
  const historyIdentity=()=>{const i=window.BankClient?.identity?.();return i&&JSON.stringify([i.workspace_id,i.privacy_revision]);};
  function clearHistory(){historyEpoch++;for(const root of historyRoots){if(root.isConnected)root.replaceChildren(el('p','账户或资料授权已变化，历史内容已清除。','discover-note'));}historyRoots.clear();}
  for(const name of ['navren:privacy-reset','navren:identity-revoked','navren:account-switched','pagehide'])window.addEventListener(name,clearHistory);
  async function getHistory(input,options={}){
    const epoch=historyEpoch,identity=historyIdentity();if(!identity)throw new Error('账户身份尚未核对，请重新读取资料。');
    const c=historySelection(input),path='history/'+(c.object_kind==='article'?'articles/':'weeklies/')+encodeURIComponent(c.object_id)+'/versions/'+c.object_version;
    const response=await fetch('/api/market/'+path,{cache:'no-store',credentials:'same-origin',signal:options.signal});
    let data;try{data=await response.json();}catch(_){throw new Error('历史资料响应无法核对，请重新读取同一版次。');}
    if(epoch!==historyEpoch||identity!==historyIdentity())throw new Error('账户或资料授权已变化，旧历史响应已丢弃。');
    if(!response.ok){const e=new Error('所选历史版本当前无法读取，本次没有替换为最新资料。');e.status=response.status;e.code=data.error?.code||data.reason||'history_unavailable';e.metadata={kind:c.object_kind,id:c.object_id,version:c.object_version};throw e;}
    const wanted=data.requested,object=c.object_kind==='article'?data.article:data.report;
    if(data.status!=='historical_readable'||data.reading_mode!=='historical_audit'||data.historical_only!==true||data.current_evidence_eligible!==false||data.execution_authority!==false||wanted?.kind!==c.object_kind||wanted?.id!==c.object_id||wanted?.version!==c.object_version||wanted?.reading_mode!=='historical_audit'||!object||(object.id??object.period)!==c.object_id||object.version!==c.object_version||object.status!=='historical_readable')throw new Error('历史资料身份或只读标记不一致，正文停止展示。');
    return data;
  }
  async function historyBookmark(input,label='历史市场资料',control){
    try{const c=historySelection(input);return await changeFavorite({category:c.object_kind==='weekly'?'market_weekly':'market_news',kind:c.object_kind,object_kind:c.object_kind,object_id:c.object_id,version:c.object_version,time_view:'historical_audit',source_scope:'public_market',text:label,source:'历史市场资料',timestamp:new Date().toISOString()},{control});}
    catch(error){showToast(error.message);return false;}
  }
  function historicalText(root,text,packet){
    if(typeof text!=='string'||!text)return;
    if(packet.historical_numeric_rendering_supported!==true&&/[0-9零〇一二三四五六七八九十百千万亿两]/.test(text)){root.append(el('p','该段含历史数值，数值引用资格尚未开放，正文暂不展示。','discover-note'));return;}
    root.append(el('p',text));
  }
  function historyUnavailable(root,input,error){
    historyRoots.delete(root);
    root.replaceChildren(el('p','所选历史版本当前无法读取，本次没有替换为最新资料。','discover-error'));
    try{const c=historySelection(input);root.append(el('p',(c.object_kind==='weekly'?'周报期号：':'文章身份：')+c.object_id+' · 第 '+c.object_version+' 版 · 历史查阅','discover-note'));}catch(_){root.append(el('p','历史身份或版次不完整。','discover-note'));}
    if(error?.status)root.append(el('p','读取状态：'+error.status,'discover-note'));
    root.append(button('返回',()=>window.NavrenViews?.back()));
  }
  function renderHistory(root,packet,input,options={}){
    historyRoots.add(root);
    const c=historySelection(input),object=c.object_kind==='article'?packet.article:packet.report;
    root.replaceChildren(button('‹ 返回',()=>window.NavrenViews?.back()));
    const reader=el('article',undefined,'discover-weekly-reader');reader.dataset.historyKind=c.object_kind;reader.dataset.historyId=c.object_id;reader.dataset.historyVersion=String(c.object_version);reader.dataset.timeView='historical_audit';
    reader.append(el('p','历史查阅 · 第 '+c.object_version+' 版 · 只读','discover-note'),el('h2',object.title_cn||object.title||c.object_id),el('p',packet.notice||'选定版本的历史资料，不代表当前事实或可投资金额。','discover-note'));
    historicalText(reader,object.evidence_excerpt||object.summary,packet);
    for(const change of object.changes||[]){const section=el('section',undefined,'market-panel-section');section.append(el('h3',change.heading||'原版变化'));historicalText(section,change.text,packet);historicalText(section,change.conditions,packet);reader.append(section);}
    historicalText(reader,object.transmission,packet);
    const articles=c.object_kind==='article'?[object]:(object.source_articles||[]);
    for(const article of articles){const section=el('section',undefined,'market-panel-section');section.append(el('h3',article.title_cn||article.title||article.id),el('p','原版来源：'+(article.publisher||article.source_id||'未提供')+' · 版本 '+article.version,'discover-note'));historicalText(section,article.evidence_excerpt,packet);reader.append(section);}
    const controls=el('div',undefined,'discover-actions'),askButton=button('引用这个历史版本',()=>window.NavrenViews?.reference(historyReference(c,object.title_cn||object.title||'历史市场资料'))),save=button('收藏这个历史版本',()=>historyBookmark(c,object.title_cn||object.title||'历史市场资料',save));save.dataset.accountFavorite='true';save.disabled=!accountReady();askButton.dataset.marketReference='true';controls.append(askButton,save);reader.append(controls);
    const form=el('form'),label=el('label','查阅明确版次'),inputVersion=el('input'),submit=el('button','读取这个版次');inputVersion.type='number';inputVersion.min='1';inputVersion.max='999999999';inputVersion.step='1';inputVersion.required=true;inputVersion.value=String(c.object_version);inputVersion.setAttribute('aria-label','历史资料版次');submit.type='submit';label.append(inputVersion);form.append(label,submit);form.onsubmit=e=>{e.preventDefault();try{const next=historySelection({...c,object_version:historyVersion(inputVersion.value)});window.NavrenViews?.open(next.object_kind==='weekly'?'weekly':'reading',next).catch(error=>showToast(error.message));}catch(error){showToast(error.message);}};reader.append(form);root.append(reader);
    return object;
  }
  async function openHistory(input){
    try{const c=historySelection(input),identity=await window.BankClient?.refreshIdentity();if(input.workspace_id&&(input.workspace_id!==identity?.workspace_id||input.privacy_revision!==identity?.privacy_revision))throw new Error('账户或授权已变化，请重新选择历史资料。');
      const view=c.object_kind==='weekly'?'weekly':'reading';if(document.body.dataset.page==='recommend'&&window.NavrenViews)return await NavrenViews.open(view,c);
      if(!window.NavrenViews)throw new Error('历史视图暂不可用，请刷新页面。');window.NavrenApp.navigate(NavrenViews.url({page:'recommend',view,...c}));
    }catch(error){showToast(error.message);}
  }
  function ask(kind,id,text='',reference){
    if(reference?.parameters&&Object.hasOwn(reference.parameters,'reading_mode')){
      try{const ref=historyReference({kind,id,version:reference.version,time_view:reference.parameters.reading_mode},reference.label);return window.NavrenViews?.ask(text,ref).catch(error=>showToast(error.message));}catch(error){showToast(error.message);return;}
    }if(window.NavrenViews?.ask)return window.NavrenViews.ask(text,{kind,id,source_scope:'market',label:t('公开市场资料')}).catch(error=>showToast(error.message));showToast(t('资料关联暂不可用，请刷新后重试。'));}
  function source(a){const box=el('div',undefined,'market-source');box.append(link(a.publisher+' · 阅读原文',a.url,a),el('span',originalTime({articles:[a]}).textContent));
    if(a.origin_publisher&&![a.publisher,a.publisher.replace('财经','')].includes(a.origin_publisher))box.append(el('span','稿件来源：'+a.origin_publisher));
    if(!readable(a))box.append(el('span','当前原文暂不可直接阅读','market-limit'));
    return box;}
  function bookmark(kind,obj,options={}){if(obj?.time_view==='historical_audit')return historyBookmark({kind,id:obj.id||obj.period,version:obj.version,time_view:obj.time_view},obj.headline||obj.title_cn||obj.title,options.control);return changeFavorite(favoriteRecord(kind,obj),options);}
  function error(root,e){root.replaceChildren(el('p',e.message||'市场缓存暂不可用','market-limit'));}
  function prefs(){return accountReady()?Storage.get('qingzhou_preferences'):null;}
  function waiting(root){root.replaceChildren(el('p',t('账号偏好尚未载入，请稍后重新读取。'),'market-limit'),button(t('重新读取'),async()=>{try{await Storage.ready({refresh:false});await recommend();}catch(error){showToast(error.message);}}));}
  function disabled(root,key,name){const enable=button('重新显示'+name,async()=>{const epoch=accountEpoch,identity=accountIdentity();enable.disabled=true;try{if(!accountReady())throw new Error('账号偏好尚未载入，请重新读取。');await Storage.update('qingzhou_preferences',value=>({...value,[key]:true}));if(accountCurrent(epoch,identity))await recommend();}catch(error){if(epoch===accountEpoch)showToast(error.message);}finally{if(enable.isConnected)enable.disabled=!accountReady();}});root.replaceChildren(el('p','你已关闭'+name+'的页面展示。'),enable);}
  function eventCard(e){
    const card=el('article',undefined,'market-event-card'),a=e.articles[0];
    const kind={official_release:'官方原始发布',official_interpretation:'官方数据解读',official_speech:'官方演讲观点',official_interview:'官方采访观点',media_report:'媒体报道'};
    const meta=el('p',a.publisher+' · '+originalTime(e).textContent+' · '+(kind[a.identity]||'公开材料'),'market-meta');
    const h=el('h3');h.append(link(e.headline,a.url,a));card.append(meta,h,el('p',e.summary,'market-summary'));
    const details=el('details',undefined,'market-explanation');details.append(el('summary','Navren 解读'));
    details.append(el('h4','发生了什么'));for(const f of e.facts)details.append(el('p',f.text));
    details.append(el('h4','传导与条件'),el('p',e.analysis),el('h4','仍有哪些不确定性'),el('p',e.uncertainty));
    for(const article of e.articles)details.append(source(article));
    const actions=el('div',undefined,'market-actions');actions.append(details,button('问 Navren',()=>ask('event',e.id)),button('收藏',()=>bookmark('event',e)));
    card.append(actions);return card;
  }
  function readingCard(a){const card=el('article',undefined,'market-reading-card');const h=el('h3');h.append(link(a.title_cn||a.title,a.url,a));card.append(h,source(a),button('问 Navren',()=>ask('article',a.id)),button('收藏',()=>bookmark('article',a)));return card;}
  let deck=null,hotData=null,hotRequest=0,pendingHot=null,historicalPanel=null;
  const topics={macro:'宏观数据',rates:'利率政策',household:'民生金融',international:'国际市场',investment:'投资市场',fintech:'金融科技',general:'综合金融'};
  const categories={household:'民生金融',macro_policy:'宏观政策',investment:'投资市场',fintech:'金融科技',international:'国际市场',general:'综合金融'};
  function categoryLabel(e){const row=hotData?.categories?.find(x=>x.id===e.category);return window.NavrenI18n?.language==='en'&&row?.name_en?row.name_en:t(row?.name||categories[e.category]||'类型未披露');}
  const identities={official_release:'公告',official_interpretation:'数据',official_speech:'观点',official_interview:'观点',media_report:'报道'};
  const fullDate=(x,precision='date')=>x?new Date(x).toLocaleString(window.NavrenI18n?.locale||'zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',...(precision==='date'?{}:{hour:'2-digit',minute:'2-digit'})}):t('暂无核验日期');
  const primaryArticle=e=>{const id=e.primary_article_id||e.archive?.primary_article_id;return id?e.articles?.find(a=>a.id===id):e.articles?.length===1?e.articles[0]:undefined;};
  function editionDate(e){const selected=hotData?.events?.find(x=>x.id===e.id&&x.version===e.version);return e.archive?.edition_date||selected?.archive?.edition_date||(selected?hotData.edition_date:null);}
  function originalPublication(e){
    const a=primaryArticle(e),stamp=a?.published_at;
    // 页面更新时间、精选时间和解读时间均不能代替报道的首次发布日期。
    if(!a||(a.reported_time_kind||e.reported_time_kind)==='source_page_updated_at'||typeof stamp!=='string'||!/(Z|[+-]\d{2}:\d{2})$/i.test(stamp))return null;
    const time=Date.parse(stamp);return Number.isFinite(time)?{stamp,time}:null;
  }
  function originalTime(e,{compact=false}={}){
    const published=originalPublication(e),stamp=published?.stamp;
    const node=el('time',t('原文发布')+' '+(published?(compact?date(stamp,'date'):fullDate(stamp,'date')):t('日期待核验')));
    if(published)node.dateTime=stamp;node.dataset.marketTimeKind='source_published';return node;
  }
  function previewMeta(preview,e,publisher){preview.append(el('span',publisher,'market-flow-publisher'));const time=originalTime(e,{compact:true});time.className='market-flow-publisher';preview.append(time);}
  const categoryThemes={macro_policy:'macro',household:'household',international:'international',investment:'investment',fintech:'fintech',general:'general'};
  const theme=e=>e.category==='macro_policy'&&(e.theme==='rates'||e.topic_id==='rates')?'rates':Object.hasOwn(categoryThemes,e.category)?categoryThemes[e.category]:Object.hasOwn(topics,e.theme)?e.theme:Object.hasOwn(topics,e.topic_id)?e.topic_id: e.articles?.[0]?.source_id?.startsWith('nbs')?'macro': ['fed','ecb'].includes(e.articles?.[0]?.source_id)?'international': /住房|购房|消费贷款/.test(e.headline||'')?'household': e.articles?.[0]?.source_id==='pbc'?'rates':'general';
  function archiveReference(e){return {kind:'event',id:e.id,version:e.archive?.event_version??e.version,source_scope:'market',label:e.headline,...(e.archive?{parameters:{edition_date:e.archive.edition_date,revision:e.archive.revision,event_version:e.archive.event_version}}:{})};}
  function historyEntry(){const a=el('a',undefined,'market-history-entry');a.id='marketHistoryEntry';a.href='recommend.html?view=market_history';a.setAttribute('aria-label',t('历史热点'));const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');const p=document.createElementNS(svg.namespaceURI,'path');p.setAttribute('d','M12 3a9 9 0 1 1-8.4 5.8M3 3v6h6M12 7v5l3 2');svg.append(p);a.append(svg,el('span',t('历史热点')));a.onclick=e=>{if(e.button!==0||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;e.preventDefault();NavrenViews.update({listposition:deck?.root?.dataset.eventId||null,focustarget:a.id});NavrenViews.open('market_history');};return a;}
  function svgButton(label,path,fn){const b=button('',fn);b.className='market-icon';b.setAttribute('aria-label',label);b.title=label;const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');const p=document.createElementNS(svg.namespaceURI,'path');p.setAttribute('d',path);svg.append(p);b.append(svg);return b;}
  const archiveKey=archive=>archive?[archive.edition_date,Number(archive.revision),Number(archive.event_version)].join(':'):'current';
  function closeForAction(d){d.dataset.marketActionClose='true';d.close();}
  function latestEvent(d,id,version){closeForAction(d);if(window.NavrenViews?.current()?.view==='reading')return NavrenViews.open('reading',{object_kind:'event',object_id:id,object_version:version,source_scope:'public_market'});return openEvent(id,version);}
  function displayCopy(e,field){if(sourceTextSummary(e)||sourceBriefSummary(e))return field==='title'?e.headline:field==='summary'?e.summary:'';const key='display_'+field,english=window.NavrenI18n?.language==='en'&&e[key+'_en'];return typeof english==='string'&&english?english:e[key]||(field==='title'?e.headline:field==='summary'?e.summary:'')||'';}
  function bookmarkIcon(e){
    const b=svgButton('收藏热点','m12 3 2.8 5.7 6.3.9-4.5 4.4 1 6.2-5.6-2.9-5.6 2.9 1-6.2L3.7 9.6l6.3-.9Z',()=>bookmark('event',e,{toggle:true,control:b}));b.dataset.bookmarkId=e.id;b.dataset.bookmarkVersion=e.version;b.dataset.deckFocus='bookmark';b.__accountFavorite=favoriteRecord('event',e);if(e.archive){b.dataset.bookmarkArchiveDate=e.archive.edition_date;b.dataset.bookmarkArchiveRevision=e.archive.revision;}syncBookmark(b);return b;
  }
  function syncBookmark(b){const kind=b.dataset.bookmarkKind||'event',archive=b.dataset.bookmarkArchiveDate?{edition_date:b.dataset.bookmarkArchiveDate,revision:b.dataset.bookmarkArchiveRevision,event_version:b.dataset.bookmarkVersion}:null,record=b.__accountFavorite||{category:kind==='weekly'?'market_weekly':'market_news',object_kind:kind,object_id:b.dataset.bookmarkId,version:b.dataset.bookmarkVersion,...(archive?{archive}:{})},matches=accountReady()?(Storage.get('qingzhou_bookmarks')||[]).filter(row=>favoriteFamily(row)===favoriteFamily(record)):[],found=matches.find(row=>itemKey(favoriteIdentity(row))===itemKey(favoriteIdentity(record)))||matches[0],same=!!found&&itemKey(favoriteIdentity(found))===itemKey(favoriteIdentity(record));b.disabled=!accountReady()||bookmarkWrites.size>0||b.dataset.accountSaving==='true';b.setAttribute('aria-pressed',String(same));b.classList.toggle('is-saved',same);const label=found?(same?(kind==='weekly'?'取消收藏周报':'取消收藏热点'):archive?'替换为此归档收藏（已收藏其他版本）':'替换为当前版本收藏（已收藏其他版本）'):(kind==='weekly'?'收藏周报':'收藏热点');b.setAttribute('aria-label',t(label));b.title=t(label);}
  function syncBookmarks(){document.querySelectorAll('[data-bookmark-id]').forEach(syncBookmark);document.querySelectorAll('[data-account-favorite]').forEach(b=>{b.disabled=!accountReady()||bookmarkWrites.size>0||b.dataset.accountSaving==='true';});}
  function bindDisplay(e,packet){
    const d=packet?.review?.status==='approved'?packet.events?.find(x=>x.id===e.id):null;
    if(!d||d.event_version!==e.version||d.content_version!==e.content_version||!e.articles.every(a=>d.article_versions?.[a.id]===a.version))return e;
    if(!d.display_title||!d.display_summary||!Object.hasOwn(topics,d.topic_id))return e;
    if(d.display_fact){const f=d.display_fact;if(!f.evidence_refs?.length||!f.evidence_refs.every(r=>e.facts[r.fact_index]?.article_id===r.article_id)||(f.value&&(!f.unit||!f.period)))return e;}
    return {...e,display_title:d.display_title,display_summary:d.display_summary,display_fact:d.display_fact,display_conditions:d.display_conditions,display_version:d.display_version,topic_id:d.topic_id,card_template:d.card_template};
  }
  function sourceBriefSummary(e){
    const a=e?.articles?.[0];
    if(e?.content_mode!=='source_brief'||e.review?.status!=='source_verified'||e.review?.method!=='official_page_title_brief_date_binding'||e.status!=='published'||e.availability&&e.availability!=='active'||e.articles?.length!==1||!readable(a)||a.source_verified!==true||(!e.summary||!a.source_brief?.includes(e.summary))||e.headline!==a.title||e.reported_time_kind!=='source_page_updated_at')return '';
    return typeof e.summary==='string'?e.summary:'';
  }
  // 原文摘录独立于模型分析审批；正文与摘句的核对由同源公共服务完成。
  // 仅接受公共服务与冻结正文核对过的两条编辑归纳，不授予模型分析审批。
  const frozenEditorialSummaries={"e_text_hist_70dd4381e925a2358d33253e":{"source_text_sha256":"92ce70bde9c3749dae834b0b89a5013c121c02c0f97a2d63b164d36000cbbe5d","summary_sha256":"ae3548b03413345f957cfe52c7935b5658402502aa8efea22c25d0243aa31750"},"e_text_hist_f41457868b61606f78b031f4":{"source_text_sha256":"7851c82d406bdb10f3b943b7361811d398789356d98d0389580315154ea9f0c1","summary_sha256":"1da3bdb4a9e2a37931a219fcc5c1b3cdb3f6c1858387cf6db125a1561501fd65"}};
  function supportedEditorialSummary(e){
    const expected=frozenEditorialSummaries[e?.id],check=e?.agent_editorial_check;
    return !!expected&&e.source_supported_summary===true&&check?.status==='source_supported_summary'&&check.method==='frozen_original_text_editorial_check'&&check.payload_sha256==='38759450a48ae35421723231eb6a84b84d15d8cadc615851b2de78741850425d'&&check.source_text_sha256===expected.source_text_sha256&&check.summary_sha256===expected.summary_sha256;
  }
  function sourceTextSummary(e){
    const a=e?.articles?.[0],quotes=e?.source_summary_quotes||e?.summary_quotes||a?.summary_quotes||a?.source_check?.summary_quotes||e?.source_check?.summary_quotes;
    if(e?.content_mode!=='source_verified_text'||e.review?.status!=='source_verified_text'||e.review?.method!=='official_page_title_text_date_binding'||e.status!=='published'||e.availability&&e.availability!=='active'||e.cover_status==='source_unavailable'||e.articles?.length!==1||!readable(a)||a.source_verified!==true||a.source_content_format!=='text_article'||a.source_check?.method!=='official_page_title_text_date_binding'||a.read_scope!=='full_text'||a.access_status!=='free_full_text'||new URL(a.url).hostname==='tv.cctv.com'||!Array.isArray(quotes)||!quotes.length||quotes.some(q=>typeof q!=='string'||!q.trim())||typeof e.summary!=='string'||(e.summary!==quotes.filter(q=>e.summary.includes(q)).join('')&&!supportedEditorialSummary(e))||(e.headline!==a.title&&e.headline!==a.title_cn))return '';
    return e.summary;
  }
  const sourcePanelCopy=x=>window.NavrenI18n?.language==='en'?({'可点击「问 Navren」，结合这篇原文继续解读事件及其影响。':'Choose “Ask Navren” to explore this article and its implications.','当前资料范围以所列原文为准，后续变化需结合新的公开材料核对。':'The information is limited to the cited article. Later developments require new source material.'}[x]||t(x)):x;
  function briefCard(e,textSummary=''){
    const a=e.articles[0],card=el('article',undefined,'market-deck-card');card.dataset.topic=theme(e);card.dataset.coverStatus=textSummary?'source_verified_text':'source_verified_brief';card.dataset.version=e.version;card.dataset.editionDate=e.archive?.edition_date||'';card.dataset.editionRevision=e.archive?.revision||'';
    const top=el('div',undefined,'market-deck-top');top.dataset.deckDragSurface='true';const tags=el('div',undefined,'market-deck-tags');tags.append(el('span',categoryLabel(e)));top.append(tags,originalTime(e),bookmarkIcon(e));
    const body=el('div',undefined,'market-deck-body'),copy=el('div',undefined,'market-deck-copy'),title=el('h3',e.headline);title.setAttribute('data-i18n-skip','');const summary=el('p',textSummary||sourceBriefSummary(e));summary.setAttribute('data-i18n-skip','');copy.append(title,summary);body.append(copy);
    const bottom=el('footer',undefined,'market-deck-bottom');bottom.dataset.deckDragSurface='true';bottom.append(el('span',a.publisher));const actions=el('div',undefined,'market-deck-actions'),original=link(t('查看原文'),a.url,a),detail=button(t('Navren解读'),()=>e.archive?openHistorical(e):openEvent(e.id,e.version));original.dataset.deckFocus='original';detail.dataset.deckFocus='explanation';actions.append(detail,original);bottom.append(actions);
    const full=el('div',undefined,'market-flow-full');full.append(top,body,bottom);const preview=el('div',undefined,'market-flow-preview');preview.setAttribute('aria-hidden','true');preview.append(el('span',categoryLabel(e),'market-flow-topic'),el('span',categoryLabel(e),'market-flow-topic-short'),el('h4',e.headline));previewMeta(preview,e,a.publisher);card.append(full,preview);return card;
  }
  function renderSourceBrief(e,content,sheet,d,textSummary=''){
    const a=primaryArticle(e)||e.articles[0],hero=el('section',undefined,'market-panel-hero'),meta=el('div',undefined,'market-panel-meta');
    meta.append(el('span',a.publisher),originalTime(e));hero.append(meta,el('h3',e.headline,'market-panel-title'));
    prose(hero,textSummary||sourceBriefSummary(e),'market-panel-prose market-panel-lead');content.append(hero);
    const facts=panelSection(t('已核验事实')),quotes=e.source_summary_quotes||a.source_check?.summary_quotes||[],seen=new Set();
    for(const f of e.facts||[]){if(f.article_id!==a.id||typeof f.text!=='string'||seen.has(f.text)||textSummary&&!quotes.includes(f.text))continue;seen.add(f.text);const row=el('div',undefined,'market-panel-fact');prose(row,f.text);facts.append(row);}
    if(!seen.size)prose(facts,textSummary||sourceBriefSummary(e));content.append(facts);
    const analysis=panelSection(t('Navren 分析')),conditions=panelSection(t('条件与不确定性'),'market-panel-conditions'),sources=panelSection(t('原始资料'));
    const approved=e.review?.status==='approved'&&e.articles.every(readable)&&typeof e.analysis==='string'&&e.analysis.trim();
    prose(analysis,approved?e.analysis:sourcePanelCopy('可点击「问 Navren」，结合这篇原文继续解读事件及其影响。'));
    prose(conditions,e.uncertainty||sourcePanelCopy('当前资料范围以所列原文为准，后续变化需结合新的公开材料核对。'));
    sources.append(source(a,e));content.append(analysis,conditions,sources);
    const actions=el('footer',undefined,'market-panel-actions'),askButton=button(t('问 Navren'),()=>{closeForAction(d);if(window.NavrenViews?.ask)NavrenViews.ask('',archiveReference(e)).catch(error=>showToast(error.message));else ask('event',e.id);});
    askButton.dataset.marketReference='true';actions.append(askButton,bookmarkIcon(e));sheet.append(actions);
  }
  function frontCard(e){
    const textSummary=sourceTextSummary(e);if(textSummary||sourceBriefSummary(e))return briefCard(e,textSummary);
    const a=primaryArticle(e)||{publisher:t('来源暂不可读'),identity:'unknown'},card=el('article',undefined,'market-deck-card');card.dataset.topic=theme(e);card.dataset.version=e.archive?.event_version??e.version;if(e.archive){card.dataset.editionDate=e.archive.edition_date;card.dataset.editionRevision=e.archive.revision;}
    const top=el('div',undefined,'market-deck-top');top.dataset.deckDragSurface='true';
    const tags=el('div',undefined,'market-deck-tags');tags.append(el('span',categoryLabel(e)),el('span',identities[a.identity]||'公开材料'));
    if(e.version>1)tags.append(el('span',t('解读已修订'),'market-deck-revised'));
    top.append(tags,originalTime(e),bookmarkIcon(e));
    const body=el('div',undefined,'market-deck-body'),copy=el('div',undefined,'market-deck-copy');copy.append(el('h3',displayCopy(e,'title')));
    const usable=!e.availability||e.availability==='active';copy.append(el('p',usable?displayCopy(e,'summary'):t({revised:'内容已修订，历史正文停止展示。',withdrawn:'这条资料已撤下，保留当日选稿记录。',temporarily_unavailable:'原文暂不可访问，请稍后核查。'}[e.availability]||'归档资料当前不可读。')));if(usable&&e.display_conditions)copy.append(el('small',displayCopy(e,'conditions'),'market-deck-condition'));body.append(copy);
    const f=usable?e.display_fact:null;if(f){const fact=el('div',undefined,'market-deck-fact');fact.append(el('span',f.label));if(f.value){const metric=el('p',undefined,'market-deck-value');metric.append(el('strong',f.value),el('span',f.unit));fact.append(metric,el('span',f.period));}else fact.append(el('p',f.text));body.append(fact);}
    const bottom=el('footer',undefined,'market-deck-bottom');bottom.dataset.deckDragSurface='true';bottom.append(el('span',a.publisher));const actions=el('div',undefined,'market-deck-actions'),original=link(t('查看原文'),a.url,a);original.dataset.deckFocus='original';const read=button(t('Navren解读'),()=>e.archive?openHistorical(e):openEvent(e.id,e.version));read.dataset.deckFocus='explanation';actions.append(read,original);bottom.append(actions);
    const full=el('div',undefined,'market-flow-full');full.append(top,body,bottom);
    const preview=el('div',undefined,'market-flow-preview');preview.setAttribute('aria-hidden','true');preview.append(el('span',categoryLabel(e),'market-flow-topic'),el('span',categoryLabel(e),'market-flow-topic-short'),el('h4',displayCopy(e,'title')));previewMeta(preview,e,a.publisher);
    card.append(full,preview);return card;
  }
  function panel(title,topic='general',options={}){
    document.querySelectorAll('.market-panel-layer[open]').forEach(closeForAction);
    const trigger=document.activeElement,d=el('dialog',undefined,'market-panel-layer'),sheet=el('div',undefined,'market-panel'),backdrop=el('div',undefined,'market-panel-backdrop'),header=el('header'),heading=el('h2',title),content=el('div',undefined,'market-panel-content');
    sheet.dataset.topic=Object.hasOwn(topics,topic)?topic:'general';backdrop.setAttribute('aria-hidden','true');
    const masthead=el('div',undefined,'market-panel-heading');masthead.append(el('span',options.categoryLabel||topics[sheet.dataset.topic],'market-panel-topic'),heading);
    heading.id='market-panel-title-'+(++panel.sequence);d.setAttribute('aria-labelledby',heading.id);header.append(masthead,button('关闭',()=>d.close()));sheet.append(header,content);d.append(backdrop,sheet);document.body.append(d);
    let backdropPointer=null;
    // 实际遮罩接收鼠标和触摸；不依赖浏览器对 ::backdrop 的事件转发。
    d.addEventListener('pointerdown',e=>{backdropPointer=e.button===0&&e.isPrimary&&e.target===backdrop?{x:e.clientX,y:e.clientY}:null;});
    d.addEventListener('pointercancel',()=>{backdropPointer=null;});
    d.addEventListener('click',e=>{const start=backdropPointer;backdropPointer=null;if(start&&e.target===backdrop&&Math.hypot(e.clientX-start.x,e.clientY-start.y)<=10){e.preventDefault();e.stopPropagation();d.close();}});
    backdrop.addEventListener('wheel',e=>e.preventDefault(),{passive:false});
    backdrop.addEventListener('touchmove',e=>e.preventDefault(),{passive:false});
    const unlock=lockPageScroll();
    d.addEventListener('close',()=>{d.remove();unlock();if(document.querySelector('dialog[open]'))return;const equivalent=trigger?.dataset.deckFocus?[...document.querySelectorAll('#marketHot [data-deck-focus]')].find(n=>n.dataset.deckFocus===trigger.dataset.deckFocus):null;
      const candidates=[trigger,equivalent,...document.querySelectorAll('.market-deck-nav:not(:disabled)')];
      for(const n of candidates){if(n?.isConnected&&!n.disabled&&n.getClientRects().length){n.focus({preventScroll:true});if(document.activeElement===n)return;}}
      const heading=options.focusFallback?.()||document.getElementById('marketHistoryDateHeading')||document.querySelector('.market-hot-label h2');if(heading){heading.tabIndex=-1;heading.focus({preventScroll:true});}
    },{once:true});d.showModal();return {d,content,sheet};
  }panel.sequence=0;
  let scrollLockCount=0,scrollLockSnapshot=null;
  function lockPageScroll(){
    // dialog.close 的事件异步到达；旧面板不得解除随后打开的新面板的滚动锁。
    if(scrollLockCount++===0){const nodes=[document.documentElement,document.body,...document.querySelectorAll('.page-scroll,.chat-messages')];scrollLockSnapshot=nodes.map(n=>({n,overflow:n.style.overflow,top:n.scrollTop,left:n.scrollLeft}));for(const {n} of scrollLockSnapshot)n.style.overflow='hidden';}
    let released=false;return ()=>{if(released)return;released=true;if(--scrollLockCount!==0)return;const saved=scrollLockSnapshot;scrollLockSnapshot=null;for(const {n,overflow,top,left} of saved||[]){n.style.overflow=overflow;n.scrollTop=top;n.scrollLeft=left;}};
  }
  function prose(parent,text,cls='market-panel-prose'){
    for(const paragraph of String(text||'').split(/\r?\n+/).map(x=>x.trim()).filter(Boolean))parent.append(el('p',paragraph,cls));
  }
  function panelSection(title,cls=''){
    const section=el('section',undefined,'market-panel-section'+(cls?' '+cls:''));section.append(el('h3',title));return section;
  }
  function panelSource(a,index){
    const row=el('article',undefined,'market-panel-source'),body=el('div'),title=el('h4');
    title.append(link(a.title_cn||a.title||a.publisher+'原始资料',a.url,a));body.append(title,source(a));
    if(readable(a)&&typeof a.id==='string'&&Number.isSafeInteger(a.version)&&a.version>0)body.append(button('查阅此原文版本',()=>openHistory({object_kind:'article',object_id:a.id,object_version:a.version,time_view:'historical_audit'})));
    row.append(el('span',String(index+1).padStart(2,'0'),'market-panel-source-index'),body);return row;
  }
  async function openEvent(id,version){
    const topic=deck?.cards.get(id)?.card.dataset.topic||theme(hotData?.events.find(e=>e.id===id)||{});
    const selected=hotData?.events.find(e=>e.id===id),{d,content,sheet}=panel('Navren 解读',topic,{categoryLabel:categoryLabel(selected||{})});content.append(el('p','正在核对事件版本…','market-limit'));
    try{const e=await get('events/'+encodeURIComponent(id));if(!d.isConnected||!d.open)return;content.replaceChildren();
      if(e.status!=='published'||!e.articles?.length||!e.articles.every(readable)){content.append(el('p','原资料已变更或撤下，当前事件等待重新审核。','market-limit'));return;}
      if(version!=null&&Number(version)!==e.version){content.append(el('p',t('内容已修订，历史正文停止展示。'),'market-panel-notice market-limit'),button(t('查看最新修订'),()=>latestEvent(d,id,e.version)));return;}
      sheet.querySelector('.market-panel-topic').textContent=categoryLabel(e);
      const textSummary=sourceTextSummary(e);if(textSummary){renderSourceBrief(e,content,sheet,d,textSummary);return;}
      const a=primaryArticle(e)||{publisher:t('来源暂不可读'),identity:'unknown'},hero=el('section',undefined,'market-panel-hero'),meta=el('div',undefined,'market-panel-meta');
      meta.append(el('span',a.publisher),originalTime(e),el('span',(identities[a.identity]||'公开材料')+' · '+t('解读版本')+' '+e.version));
      hero.append(meta,el('h3',e.headline,'market-panel-title'));prose(hero,e.summary,'market-panel-prose market-panel-lead');content.append(hero);
      const facts=panelSection('已核验事实'),factSources=new Map();
      for(const f of e.facts){const item=el('div',undefined,'market-panel-fact');prose(item,f.text);facts.append(item);const evidence=e.articles.find(a=>a.id===f.article_id);if(evidence)factSources.set(evidence.url||evidence.id,evidence);}
      if(factSources.size){const citation=el('div',undefined,'market-panel-evidence');citation.dataset.multiple=factSources.size>1;
        for(const evidence of factSources.values())citation.append(link('事实来源 · '+(factSources.size===1?evidence.publisher:(evidence.title_cn||evidence.title||evidence.publisher))+' ↗',evidence.url,evidence));facts.append(citation);}
      content.append(facts);
      const analysis=panelSection('Navren 分析'),conditions=panelSection('条件与不确定性','market-panel-conditions'),sources=panelSection('原始资料');
      prose(analysis,e.analysis);prose(conditions,e.uncertainty);e.articles.forEach((article,i)=>sources.append(panelSource(article,i)));content.append(analysis,conditions,sources);
      const actions=el('footer',undefined,'market-panel-actions'),askButton=button('问 Navren',()=>{closeForAction(d);if(window.NavrenViews?.ask)NavrenViews.ask('',archiveReference(e)).catch(error=>showToast(error.message));else ask('event',e.id);});askButton.dataset.marketReference='true';actions.append(askButton,bookmarkIcon(e));sheet.append(actions);
    }catch(e){if(d.isConnected&&d.open){content.replaceChildren(el('p',e.message,'market-limit'),button('重新读取',()=>{closeForAction(d);openEvent(id,version);}));}}
  }
  function renderHistoricalEvent(e,content,sheet,d){
    const textSummary=sourceTextSummary(e);if(textSummary||sourceBriefSummary(e)){renderSourceBrief(e,content,sheet,d,textSummary);return;}
    const a=e.articles[0],hero=el('section',undefined,'market-panel-hero'),meta=el('div',undefined,'market-panel-meta');
    meta.append(el('span',t('精选日期')+' '+e.archive.edition_date),el('span',t('日版次')+' '+e.archive.revision),el('span',t('解读版本')+' '+e.archive.event_version));
    if(e.archive.origin==='reconstructed')meta.append(el('span',t('历史补录')),el('span',t('补录时间')+' '+fullDate(e.archive.selected_at,'minute')));if(e.after_revision_material){meta.append(el('span',t('事后修订资料')));if(e.revision_confirmed_at)meta.append(el('span',t('可确认修订时间')+' '+fullDate(e.revision_confirmed_at,'minute')));}
    hero.append(meta,el('h3',e.headline,'market-panel-title'),el('p',(primaryArticle(e)?.publisher||a.publisher)+' · '+originalTime(e,{edition:false}).textContent,'market-panel-meta'));prose(hero,e.summary,'market-panel-prose market-panel-lead');content.append(hero);
    const facts=panelSection(t('已核验事实')),evidence=new Map();for(const f of e.facts||[]){const row=el('div',undefined,'market-panel-fact');prose(row,f.text);facts.append(row);const source=e.articles.find(a=>a.id===f.article_id);if(source)evidence.set(source.url||source.id,source);}
    if(evidence.size){const references=el('div',undefined,'market-panel-evidence');for(const source of evidence.values())references.append(link(t('事实来源')+' · '+source.publisher+' ↗',source.url,source));facts.append(references);}content.append(facts);
    const analysis=panelSection(t('Navren 分析')),limits=panelSection(t('条件与不确定性')),sources=panelSection(t('原始资料'));prose(analysis,e.analysis);prose(limits,e.uncertainty);[...new Map(e.articles.map(a=>[a.url||a.id,a])).values()].forEach((a,i)=>sources.append(panelSource(a,i)));content.append(analysis,limits,sources);
    const actions=el('footer',undefined,'market-panel-actions'),askButton=button(t('问 Navren'),()=>{closeForAction(d);NavrenViews.ask('',archiveReference(e)).catch(err=>showToast(err.message));});askButton.dataset.marketReference='true';actions.append(askButton,bookmarkIcon(e));sheet.append(actions);
  }
  function renderHistoricalState(e,content,sheet,d){
    content.replaceChildren();sheet.querySelector('.market-panel-actions')?.remove();
    sheet.querySelector('.market-panel-topic').textContent=categoryLabel(e);
    if(e.availability!=='active'){
      content.append(el('h3',e.headline,'market-panel-title'),el('p',t({revised:'内容已修订，历史正文停止展示。',withdrawn:'这条资料已撤下，保留当日选稿记录。',temporarily_unavailable:'原文暂不可访问，请稍后核查。'}[e.availability]||'归档资料当前不可读。'),'market-panel-notice'),el('p',t('归档日')+' '+e.archive.edition_date+' · '+t('日版次')+' '+e.archive.revision+' · '+t('事件版本')+' '+e.archive.event_version,'market-panel-meta'));
      if(e.archive.origin==='reconstructed')content.append(el('p',t('历史补录')+' · '+t('补录时间')+' '+fullDate(e.archive.selected_at,'minute'),'market-panel-meta'));
      if(e.after_revision_material){content.append(el('p',t('事后修订资料'),'market-panel-notice'));if(e.revision_confirmed_at)content.append(el('p',t('可确认修订时间')+' '+fullDate(e.revision_confirmed_at,'minute'),'market-panel-meta'));}
      if(e.availability==='revised'&&e.latest_pointer?.id&&e.latest_pointer.version)content.append(button(t('查看最新修订'),()=>latestEvent(d,e.latest_pointer.id,e.latest_pointer.version)));
    }else renderHistoricalEvent(e,content,sheet,d);
    window.NavrenI18n?.apply(d);
  }
  async function openHistorical(item,options={}){
    const archive=item.archive;if(!archive?.edition_date||!Number.isInteger(Number(archive.revision))||Number(archive.revision)<1||!Number.isInteger(Number(archive.event_version))||Number(archive.event_version)<1)throw new Error(t('归档身份不完整，暂不能打开详情。'));
    const {d,content,sheet}=panel(t('Navren 解读'),theme(item),{focusFallback:options.focusFallback,categoryLabel:categoryLabel(item)});sheet.dataset.archiveDate=archive.edition_date;sheet.dataset.archiveRevision=archive.revision;content.append(el('p',t('正在核对归档版本…'),'market-limit'));
    try{const e=await get('hotspots/history/'+encodeURIComponent(archive.edition_date)+'/events/'+encodeURIComponent(item.id)+'?revision='+encodeURIComponent(archive.revision));if(!d.isConnected||!d.open)return;
      if(e.id!==item.id||e.archive?.edition_date!==archive.edition_date||Number(e.archive?.revision)!==Number(archive.revision)||Number(e.archive?.event_version)!==Number(archive.event_version))throw Error(t('归档版本不一致，未展示正文。'));content.replaceChildren();
      if(e.availability==='active'&&(e.status!=='published'||!e.articles?.length||!e.articles.every(readable)))throw Error(t('归档资料当前不可读。'));
      historicalPanel={d,content,sheet,event:e};d.addEventListener('close',()=>{if(historicalPanel?.d===d)historicalPanel=null;},{once:true});renderHistoricalState(e,content,sheet,d);return d;
    }catch(error){if(d.isConnected&&d.open)content.replaceChildren(el('p',t('服务读取失败，请重试。'),'market-limit'),button(t('重试'),()=>{closeForAction(d);openHistorical(item,options);}));return d;}
  }
  function checkedHotspots(data,day){
    const now=marketNow(),today=chinaDay(now);
    if(!Array.isArray(data?.events)||data.events.length>6)throw Error(t('精选清单格式异常，保留最近成功内容。'));
    const revision=Number(data.revision),events=!validDay(day)||day>today||data.edition_date!==day?[]:data.events.filter(e=>{
      const published=originalPublication(e),archive=e.archive;
      return e.status==='published'&&e.availability==='active'&&e.articles?.length&&e.articles.every(readable)&&published&&published.time<=now&&chinaDay(published.time)===day&&archive?.edition_date===day&&Number.isSafeInteger(Number(archive.revision))&&Number(archive.revision)>0&&Number.isSafeInteger(Number(archive.event_version))&&Number(archive.event_version)>0&&Number(archive.event_version)===Number(e.version)&&(!Number.isSafeInteger(revision)||revision<1||Number(archive.revision)===revision);
    });
    const retained=day<today&&events.length>0;
    return {...data,server_today:today,requested_edition_date:today,edition_date:events.length?day:today,events,display_mode:events.length?(retained?'retained':'current'):'empty',is_fallback:!!retained,status:events.length?'published':'no_qualified_hotspots'};
  }
  function hotNotice(data){
    const heading=document.getElementById('marketHotHeading');heading?.querySelector('.market-refresh-notice')?.remove();
    const messages=[];
    if(typeof data==='string')messages.push(t(data));
    else if(data){const stream=data.coverage?.schedule?.streams?.hotspots,hasContent=!!data.events?.length;
      if(stream?.updating===true)messages.push(t(hasContent?'市场内容正在更新，当前仍展示已核验内容。':'市场内容正在更新。'));
      if(stream?.last_result==='failed')messages.push(t(hasContent?'最近一次更新失败，保留已核验内容。':'最近一次更新失败。'));
      else if(stream?.last_result==='partial')messages.push(t(hasContent?'最近一次更新未完整完成，保留已核验内容。':'最近一次更新未完整完成。'));
      if(!messages.length&&data.is_fallback)messages.push(t('本日尚无新版，展示最近已核验精选。'));
    }
    if(messages.length)heading?.append(el('p',messages.join(' '),'market-refresh-notice'));
  }
  function rolloverHotspots(){
    if(!hotData)return;const today=chinaDay(marketNow()),retained=hotData.events.length>0&&hotData.edition_date<today;
    hotData={...hotData,server_today:today,requested_edition_date:today,edition_date:hotData.events.length?hotData.edition_date:today,display_mode:retained?'retained':hotData.events.length?'current':'empty',is_fallback:retained};hotHeader();
  }
  function discardInvalidDisplayed(packet){
    if(!hotData?.events.length||packet.edition_date!==hotData.edition_date||!Array.isArray(packet.events))return;
    const checked=checkedHotspots(packet,packet.edition_date),valid=new Set(checked.events.map(e=>e.id+':'+archiveKey(e.archive)));
    const events=hotData.events.filter(e=>valid.has(e.id+':'+archiveKey(e.archive)));if(events.length===hotData.events.length)return;
    // 明确失效的原文不因拖动或阅读面板延长展示，也不从本地存储恢复。
    pendingHot=null;deck?.destroy();deck=null;hotData={...checked,events};
    const root=document.getElementById('marketHot');root?.replaceChildren();hotHeader();
  }
  async function latestReadableHotspots(data,isCurrent){
    const today=chinaDay(marketNow()),current=checkedHotspots(data,today),currentEdition={edition_date:today,status:data.status,revision:data.edition_date===today?data.revision:null};
    const observation={coverage:data.coverage,current_edition:data.current_edition||currentEdition};
    if(current.events.length===6)return {...current,...observation};
    if(data.display_mode==='retained'&&data.is_fallback===true&&validDay(data.edition_date)&&data.edition_date<today){const retained=checkedHotspots(data,data.edition_date);if(retained.events.length===6)return {...retained,...observation};}
    const index=await get('hotspots/history');if(!isCurrent())return null;
    if(!Array.isArray(index?.dates)||index.dates.length>62)throw Error(t('精选清单格式异常，保留最近成功内容。'));
    if(hotData?.events.length){const previous=index.dates.find(row=>row.date===hotData.edition_date);if(previous&&previous.readable_count===0)discardInvalidDisplayed({edition_date:previous.date,revision:previous.revision,events:[]});}
    const candidates=index.dates.filter(row=>validDay(row.date)&&row.date<today&&row.readable_count===6&&Number.isSafeInteger(Number(row.revision))&&Number(row.revision)>0).sort((a,b)=>b.date.localeCompare(a.date));
    for(const candidate of candidates.slice(0,3)){
      // 此路由不接受revision参数；把目录版次和实际日版次、各卡归档身份逐一核对。
      const packet=await get('hotspots/history/'+encodeURIComponent(candidate.date));if(!isCurrent())return null;
      if(packet.edition_date!==candidate.date||Number(packet.revision)!==Number(candidate.revision))continue;
      discardInvalidDisplayed(packet);const retained=checkedHotspots(packet,candidate.date);
      if(retained.events.length===6)return {...retained,...observation};
    }
    if(candidates.length>3)throw Error('最近精选核验尚未完成，请稍后重试。');
    return {...current,...observation};
  }
  function applyHotspots(){
    if(!prefs()?.marketHotVisible){pendingHot=null;return;}if(!pendingHot||deck&&deck.state!=='idle'||document.querySelector('dialog[open]'))return;const root=document.getElementById('marketHot');if(!root)return;
    const packet=pendingHot;pendingHot=null;hotData=checkedHotspots(packet,packet.edition_date);
    if(deck)deck.update(hotData.events);else deck=new MarketDeck(root,{events:hotData.events,renderCard:frontCard,theme,historyEntry});
    const empty=root.querySelector('.market-deck-empty');if(empty)empty.textContent=t('当日暂无已核验的新报道，可查看历史热点。');hotHeader();hotNotice(hotData);scheduleMarketBoundary();
  }
  window.addEventListener('navren:market-deck-idle',applyHotspots);document.addEventListener?.('close',()=>queueMicrotask(()=>{applyHotspots();if(marketBoundaryDue)visibleRefresh();}),true);
  async function refreshHotspots(){
    const root=document.getElementById('marketHot');if(!root)return;const request=++hotRequest,epoch=accountEpoch,identity=accountIdentity();
    if(!prefs()){waiting(root);return;}
    if(!prefs().marketHotVisible){pendingHot=null;deck?.destroy();deck=null;hotData=null;root.classList.remove('market-deck');disabled(root,'marketHotVisible','市场热点');return;}
    rolloverHotspots();scheduleMarketBoundary();
    if(!deck){root.classList.remove('market-deck');root.replaceChildren(el('div','正在读取已核验市场内容…','market-deck-skeleton'));}
    try{const data=await get('hotspots');
      if(request!==hotRequest||!accountCurrent(epoch,identity))return;if(!prefs()?.marketHotVisible){refreshHotspots();return;}
      discardInvalidDisplayed(data);const packet=await latestReadableHotspots(data,()=>request===hotRequest&&accountCurrent(epoch,identity));
      if(!packet||request!==hotRequest||!accountCurrent(epoch,identity))return;pendingHot=packet;hotNotice(packet);applyHotspots();
    }catch(e){if(request!==hotRequest||!accountCurrent(epoch,identity))return;pendingHot=null;rolloverHotspots();if(deck&&hotData?.events.length){hotNotice('更新暂未成功，最新核验未完成，暂保留上次已核验内容。');return;}deck=null;hotData=null;root.classList.remove('market-deck');root.replaceChildren(el('p',t('服务读取失败，请重试。'),'market-limit'),button(t('重试'),refreshHotspots),historyEntry());}
  }
  function hotHeader(){const heading=document.getElementById('marketHotHeading');if(!heading)return;let title=heading.querySelector('.market-hot-label');if(!title){title=el('div',undefined,'market-hot-label');title.append(el('h2',t('市场热点')));heading.append(title);}if(window.NavrenHelp?.button&&!title.querySelector('[data-market-help]')){const help=window.NavrenHelp.button('market');help.dataset.marketHelp='true';title.append(help);}let stamp=heading.querySelector('.market-edition-date');if(!stamp){stamp=el('span',undefined,'market-edition-date');heading.append(stamp);}const day=hotData?.edition_date;stamp.textContent=day?t(hotData.is_fallback?'最近精选':'当日精选')+' · '+day:t('等待每日精选');}
  window.addEventListener('pagehide',()=>{clearTimeout(marketBoundaryTimer);marketBoundaryTimer=null;});
  window.addEventListener('pageshow',e=>{if(e.persisted){scheduleMarketBoundary();visibleRefresh();}});
  function dialog(title){const d=el('dialog',undefined,'market-dialog'),head=el('header'),unlock=lockPageScroll();head.append(el('h2',title),button('关闭',()=>d.close()));d.append(head);document.body.append(d);d.addEventListener('close',()=>{d.remove();unlock();},{once:true});d.showModal();return d;}
  function periodLabel(w){
    // 服务器保存右开区间；卡面显示完整周的周一至周日，不把下一周周一混入本期。
    const end=new Date(new Date(w.period_end_exclusive).getTime()-1).toLocaleDateString('en-CA',{timeZone:'Asia/Shanghai'});
    const start=w.period_start.slice(0,10);return start+' — '+end;
  }
  function weeklyBookmark(w){
    const b=svgButton('收藏周报','m12 3 2.8 5.7 6.3.9-4.5 4.4 1 6.2-5.6-2.9-5.6 2.9 1-6.2L3.7 9.6l6.3-.9Z',()=>bookmark('weekly',w,{toggle:true,control:b}));b.__accountFavorite=favoriteRecord('weekly',w);
    b.dataset.bookmarkKind='weekly';b.dataset.bookmarkId=w.period;b.dataset.bookmarkVersion=w.version;b.dataset.weeklyAction='save';syncBookmark(b);return b;
  }
  function weeklyCard(data){
    const w=data.report,card=el('article',undefined,'market-weekly-card'),top=el('header',undefined,'market-weekly-top');
    const label=el('div',undefined,'market-weekly-label');label.append(el('span','Navren','market-weekly-brand'),el('span','市场周报'));
    const meta=el('div',undefined,'market-weekly-meta');meta.append(el('span',periodLabel(w)),el('span','第'+w.version+'版'));
    top.append(label,meta);card.append(top);
    const body=el('div',undefined,'market-weekly-body'),lead=el('div',undefined,'market-weekly-lead'),h=el('h3'),title=button(w.title,()=>openWeekly(w.period));title.className='market-weekly-title';title.dataset.weeklyAction='read-title';title.setAttribute('data-i18n-skip','');h.append(title);
    lead.append(h,el('p',w.summary,'market-weekly-summary'));body.append(lead);
    const contents=el('ol',undefined,'market-weekly-contents');contents.setAttribute('aria-label','本期重点');
    (w.changes||[]).forEach((c,i)=>{const item=el('li'),read=button('',()=>openWeekly(w.period,'weekly-change-'+i));read.dataset.weeklyAction='change-'+i;read.append(el('span',String(i+1).padStart(2,'0'),'market-weekly-number'),el('span',c.heading,'market-weekly-topic'),el('span','↗','market-weekly-arrow'));item.append(read);contents.append(item);});
    if(contents.childElementCount)body.append(contents);card.append(body);
    const foot=el('footer',undefined,'market-weekly-footer'),timing=el('div',undefined,'market-weekly-timing');timing.append(el('span','每周一更新 · 北京时间'));
    const published=el('time','发布于 '+fullDate(w.released_at,'minute'));published.dateTime=w.released_at;timing.append(published);
    const actions=el('div',undefined,'market-weekly-actions'),read=button('阅读完整周报',()=>openWeekly(w.period)),chat=button('问 Navren',()=>ask('weekly',w.period));
    read.classList.add('market-weekly-primary');read.dataset.weeklyAction='read';chat.dataset.weeklyAction='ask';actions.append(read,chat,weeklyBookmark(w));foot.append(timing,actions);card.append(foot);return card;
  }
  const weeklyReasons={generation_binding_unavailable:'发布依据不完整，需重建后重新审核。',needs_rebuild:'需要重建并重新审核，当前不可读。',rebuild_required:'需要重建并重新审核，当前不可读。',source_revised:'原文已修订，需重新审核。',source_permission_revoked:'原文授权已撤回，当前不可读。',reading_unavailable:'原文暂不可读取，当前周报不可读。',failed:'最近一次更新失败。',no_sources:'暂无可用的已核验来源。',missing:'尚无通过核验的本期周报。',no_verified_weekly:'尚无通过核验的本期周报。'};
  const weeklyReadable=w=>w?.status==='published'&&w.source_articles?.length&&w.source_articles.every(readable);
  function dueWeekly(data){
    const now=marketNow(),midnight=Date.parse(chinaDay(now)+'T00:00:00+08:00'),weekday=new Date(midnight+8*3600000).getUTCDay();
    const monday=midnight-((weekday+6)%7)*86400000,release=monday+8.5*3600000;
    const due=chinaDay(monday-(now<release?14:7)*86400000),target=data.coverage?.schedule?.streams?.weekly?.target;
    // 旧接口在周一午夜提前推进expected_period；实际应到期号以08:30为界。
    return {...data,expected_period:validDay(target)&&target===due?target:due};
  }
  function weeklyState(data){
    data=dueWeekly(data);
    const expected=data.expected_period,current=data.history?.find(h=>h.period===expected),stream=data.coverage?.schedule?.streams?.weekly;
    const matching=stream?.target===expected?stream:null;
    let status=(current?.status!=='published'?current?.status:null)||(matching?.publication_status&&matching.publication_status!=='published'?matching.publication_status:null)||current?.status||(data.report?.period===expected?data.report.status:null)||(data.status!=='published'&&data.status!=='no_verified_weekly'?data.status:null)||'missing';
    if(status==='missing'&&(matching?.rebuild_required===true||matching?.last_result==='rebuild_required'))status='needs_rebuild';
    if(status==='published'&&!weeklyReadable(data.report))status='reading_unavailable';
    const reasons=[];if(status!=='published')reasons.push(t(weeklyReasons[status]||'本期周报未通过发布核验，当前不可读。'));
    if(matching?.last_result==='failed')reasons.push(t(weeklyReasons.failed));
    if(data.coverage?.state==='unavailable')reasons.push(t(weeklyReasons.no_sources));
    if(matching?.updating===true)reasons.push(t('本期正在更新，尚未完成发布。'));
    const box=el('div',undefined,'market-weekly-placeholder');box.dataset.weeklyStatus=status;
    box.append(el('p',(expected?t('本期期号')+' '+expected+' · ':'')+[...new Set(reasons)].join(' ')));
    const w=weeklyReadable(data.report)?data.report:null;
    box.append(el('p',w?t('最近可读周报')+'：'+w.period+' · '+t('第')+w.version+t('版'):t('最近可读周报')+'：'+t('暂无')));
    if(!w&&data.history?.length){const last=data.history[0];box.append(el('p',t('最近留存周报')+'：'+last.period+' · '+t(weeklyReasons[last.status]||'未通过当前发布核验。')));}
    if(matching?.next_check_at&&Number.isFinite(new Date(matching.next_check_at).valueOf()))box.append(el('p',t('下次检查')+' '+fullDate(matching.next_check_at,'minute')));
    return box;
  }
  async function openWeekly(period='latest',anchor=''){
    const {d,content,sheet}=panel('市场周报','general');sheet.classList.add('market-weekly-reader');
    content.append(el('p','正在读取已发布周报…','market-limit'));
    try{const data=dueWeekly(await get('weeklies/'+encodeURIComponent(period))),w=data.report;if(!d.isConnected)return;content.replaceChildren();
      if(!w){content.append(weeklyState(data));return;}
      if(!weeklyReadable(w)){content.append(weeklyState(data));return;}
      const hero=el('section',undefined,'market-panel-hero'),meta=el('div',undefined,'market-panel-meta');meta.append(el('span',periodLabel(w)),el('span','第'+w.version+'版'),el('span','北京时间'));hero.append(meta,el('h3',w.title,'market-panel-title'));prose(hero,w.summary,'market-panel-prose market-panel-lead');content.append(hero);
      const articles=new Map((w.source_articles||w.reading||[]).map(a=>[a.id,a]));
      (w.changes||[]).forEach((c,i)=>{const section=panelSection(c.heading,'market-weekly-change');section.id='weekly-change-'+i;section.querySelector('h3').prepend(el('span',String(i+1).padStart(2,'0'),'market-weekly-number'));prose(section,c.text);if(c.conditions){const condition=el('div',undefined,'market-weekly-condition');condition.append(el('strong','关注条件'));prose(condition,c.conditions);section.append(condition);}content.append(section);});
      const transmission=panelSection('传导与分歧');prose(transmission,w.transmission);content.append(transmission);
      if(w.observations?.length){const observations=panelSection('关键数据对照');for(const x of w.observations)observations.append(el('p',x.label+'：'+x.value+' '+x.unit),link('指标原始来源',x.url));content.append(observations);}
      if(w.next_week_schedule?.length){const schedule=panelSection('下周关注');for(const item of w.next_week_schedule){const row=el('div',undefined,'market-schedule');row.append(el('strong',item.title+' · '+item.date),link('官方日程',item.url),button('加入财务日历提醒',()=>{d.close();ask('weekly',w.period,'请为周报中“'+item.title+'”准备一个日历提醒确认卡。');}));schedule.append(row);}content.append(schedule);}
      // 所有段落使用同一资料包：统一放在文末，按具体原文去重。
      const library=panelSection('原始资料'),unique=[...new Map([...articles.values()].map(a=>[a.url||a.id,a])).values()];unique.forEach((a,i)=>library.append(panelSource(a,i)));content.append(library);
      const limits=panelSection('阅读说明','market-panel-conditions');for(const notice of [w.observation_notice,w.schedule_notice,...(w.limitations||[])].filter(Boolean))prose(limits,notice);content.append(limits);
      const actions=el('footer',undefined,'market-panel-actions');actions.append(button('问 Navren',()=>{d.close();ask('weekly',w.period);}),weeklyBookmark(w));sheet.append(actions);
      if(anchor)content.querySelector('#'+anchor)?.scrollIntoView({block:'start',behavior:'instant'});
    }catch(e){if(d.isConnected)content.replaceChildren(el('p',e.message,'market-limit'),button('重新读取',()=>{d.close();openWeekly(period,anchor);}));}
  }
  let weeklySignature='',weeklyFlight=null,refreshFlight=null,refreshTimer=null;
  async function refreshWeekly(){
    const root=document.getElementById('weeklyReport');if(!root)return;
    if(!prefs()){waiting(root);return;}
    if(!prefs().weeklyReport){weeklySignature='';disabled(root,'weeklyReport','市场周报');return;}
    if(weeklyFlight)return weeklyFlight;
    if(!weeklySignature)root.replaceChildren(el('p','正在读取已发布周报…','market-weekly-placeholder'));
    const epoch=accountEpoch,identity=accountIdentity(),task=(async()=>{try{const data=dueWeekly(await get('weeklies/latest'));if(!accountCurrent(epoch,identity)||!prefs()?.weeklyReport)return;
      root.querySelector('.market-weekly-refresh-notice')?.remove();
      const signature=JSON.stringify([data.report,data.expected_period,data.status,data.history,data.coverage?.state,data.coverage?.schedule?.streams?.weekly]);if(signature===weeklySignature)return;
      const focus=document.activeElement?.closest('#weeklyReport [data-weekly-action]')?.dataset.weeklyAction;
      weeklySignature=signature;root.replaceChildren();const w=data.report;
      if(weeklyReadable(w))root.append(weeklyCard(data));
      if(data.history?.length){const archive=el('details',undefined,'market-weekly-archive');archive.append(el('summary','历史周报'));const select=el('select');select.setAttribute('aria-label','历史周报');const placeholder=el('option','选择历史期号');placeholder.value='';select.append(placeholder);for(const h of data.history){const option=el('option',h.period+' · 第'+h.version+'版'+(h.status==='published'?' · '+t('可读'):' · '+t(weeklyReasons[h.status]||'未通过当前发布核验。')));option.value=JSON.stringify({object_kind:'weekly',object_id:h.period,object_version:h.version,time_view:'historical_audit'});select.append(option);}select.onchange=()=>{if(select.value)openHistory(JSON.parse(select.value));select.value='';};archive.append(select);root.append(archive);}
      if(focus)root.querySelector('[data-weekly-action="'+focus+'"]')?.focus({preventScroll:true});
    }catch(e){if(!accountCurrent(epoch,identity))return;if(!weeklySignature)root.replaceChildren(el('p',e.message,'market-weekly-placeholder'),button('重试',refreshWeekly));else{root.querySelector('.market-weekly-refresh-notice')?.remove();root.append(el('p',t('周报更新暂未成功，暂保留上次已核验周报。'),'market-weekly-refresh-notice'));}}finally{if(weeklyFlight===task)weeklyFlight=null;}})();weeklyFlight=task;return task;
  }
  function refreshMarket(){if(refreshFlight)return refreshFlight;const task=Promise.allSettled([refreshHotspots(),refreshWeekly()]).finally(()=>{if(refreshFlight===task)refreshFlight=null;});refreshFlight=task;return task;}
  function clearAccountMarket(){
    accountEpoch++;hotRequest++;bookmarkWrites.clear();clearTimeout(marketBoundaryTimer);marketBoundaryTimer=null;marketBoundaryDue=false;pendingHot=hotData=null;deck?.destroy();deck=null;weeklySignature='';weeklyFlight=refreshFlight=null;
    clearHistory();document.querySelectorAll('.market-panel-layer[open],.market-dialog[open]').forEach(d=>d.close());
    for(const id of ['marketHot','weeklyReport']){const root=document.getElementById(id);if(root){root.classList.remove('market-deck');waiting(root);}}
    hotHeader();hotNotice(null);syncBookmarks();
  }
  for(const name of ['navren:privacy-reset','navren:identity-revoked','navren:account-switched'])window.addEventListener(name,clearAccountMarket);
  window.addEventListener('navren:account-state',event=>{
    if(event.detail?.status!=='ready'||!accountReady()){clearAccountMarket();return;}
    const keys=event.detail?.keys||[];syncBookmarks();if(!keys.length||keys.includes('qingzhou_preferences'))refreshMarket();
  });
  function visibleRefresh(){if(window.NavrenViews?.current()?.time_view==='historical_audit')return;if(document.visibilityState!=='hidden'&&!document.querySelector('dialog[open]')){marketBoundaryDue=false;rolloverHotspots();scheduleMarketBoundary();refreshMarket();}}
  async function recommend(){
    const selected=new URLSearchParams(location.search);if(selected.has('time_view')&&['weekly','reading'].includes(selected.get('view')||location.hash.slice(1)))return;
    if(!document.getElementById('marketHot')||!document.getElementById('weeklyReport'))return;
    if(!accountReady()){for(const id of ['marketHot','weeklyReport'])waiting(document.getElementById(id));try{await Storage.ready({refresh:false});}catch(_){return;}if(!accountReady())return;}
    hotHeader();if(!refreshTimer){refreshTimer=setInterval(visibleRefresh,300000);document.addEventListener('visibilitychange',visibleRefresh);window.addEventListener('focus',visibleRefresh);}
    await refreshMarket();
  }
  function sources(message,rows){if(!rows?.length)return;const box=el('section',undefined,'market-chat-sources');box.append(el('h4','对应原文与来源'));for(const a of rows)box.append(source(a));message.querySelector('.bubble').append(box);}
  async function openBookmark(b){try{
    if(b?.time_view==='historical_audit'){await openHistory(b);return;}
    if(!b.object_kind||!b.object_id){showToast('这是旧样例收藏，不能作为当前核验资讯。');return;}
    if(b.object_kind==='weekly'){await openWeekly(b.object_id);return;}
    if(b.object_kind==='event'){if(b.archive)await openHistorical({id:b.object_id,version:b.version,archive:b.archive});else await openEvent(b.object_id,b.version);return;}
    const data=await get((b.object_kind==='event'?'events/':'articles/')+encodeURIComponent(b.object_id));
    const d=dialog('收藏的市场资料');if(!['active','published'].includes(data.status)||(!data.articles&&!readable(data))){d.append(el('p','原内容已变更或暂不可直接阅读，请以最新资料为准。','market-limit'));return;}
    if(b.version!==data.version)d.append(el('p','内容已有修订，下面展示最新版本。','market-limit'));
    d.append(data.articles?eventCard(data):readingCard(data));
  }catch(e){showToast(e.message);}}
  window.addEventListener('navren:language',()=>{hotHeader();if(hotData&&!pendingHot){pendingHot=hotData;applyHotspots();}if(historicalPanel?.d.isConnected&&historicalPanel.d.open){const {d,content,sheet,event}=historicalPanel,top=content.scrollTop;sheet.querySelector('header h2').textContent=t('Navren 解读');sheet.querySelector('header .market-button').textContent=t('关闭');renderHistoricalState(event,content,sheet,d);content.scrollTop=top;}syncBookmarks();});
  return {clearHistory,historyVersion,historySelection,historyReference,getHistory,historyBookmark,renderHistory,historyUnavailable,openHistory,recommend,openWeekly,openBookmark,sources,ask,get,refreshHotspots,refreshWeekly,refreshMarket,bindDisplay,periodLabel,weeklyCard,openHistorical,archiveReference,historyEntry,sourceLink:link,readable,theme,displayCopy,toggleBookmark:(kind,obj,control)=>bookmark(kind,obj,{toggle:true,control}),restoreHotspot:id=>deck?.restore(id),hotspot:()=>({event_id:deck?.root?.dataset.eventId,data:hotData,state:deck?.state}),applyHotspots};
})();
// 资讯组件自行启动，页面组织调整时仍可独立挂载；重复调用由内部去重。
if(typeof document.getElementById==='function'){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',()=>window.MarketUI.recommend(),{once:true});else window.MarketUI.recommend();}

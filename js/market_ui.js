/* 热点、周报、原文与对话共用对象ID，公共只读缓存和安全DOM。 */
window.MarketUI=(()=>{
  const t=text=>window.NavrenI18n?.t(text)||text;
  const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  const button=(text,fn)=>{const b=el('button',text,'market-button');b.type='button';b.onclick=fn;return b;};
  const date=(x,precision='minute')=>x?new Date(x).toLocaleString(window.NavrenI18n?.locale||'zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',...(precision==='date'?{}:{hour:'2-digit',minute:'2-digit'})}):t('暂无核验日期');
  const allowed=new Set(['www.stats.gov.cn','www.pbc.gov.cn','www.federalreserve.gov','www.ecb.europa.eu','www.news.cn','finance.cctv.com','tv.cctv.com']);
  function safeLink(url){try{const u=new URL(url);return u.protocol==='https:'&&allowed.has(u.hostname)&&u.pathname.length>12&&(u.hostname!=='tv.cctv.com'||/^\/2026\/(?:0[1-9]|1[0-2])\/(?:0[1-9]|[12]\d|3[01])\/VIDE[A-Za-z0-9]+\.shtml$/.test(u.pathname))&&!u.username&&!u.password&&(!u.port||u.port==='443');}catch{return false;}}
  const readable=a=>!!a&&a.status==='active'&&a.link_status==='verified'&&safeLink(a.url)&&((a.read_scope==='full_text'&&a.access_status==='free_full_text')||(a.content_mode==='source_brief'&&a.read_scope==='source_brief'&&a.access_status==='free_video_with_brief'&&a.source_verified===true&&a.video_content_read===false&&a.reported_time_kind==='source_page_updated_at'&&a.source_check?.method==='official_page_title_brief_date_binding'));
  function link(text,url,article){const a=el('a',text);if(safeLink(url)&&(!article||readable(article))){a.href=url;a.target='_blank';a.rel='noopener noreferrer';}return a;}
  async function get(path,options={}){const r=await fetch('/api/market/'+path,{cache:'no-store',credentials:'same-origin',signal:options.signal});const d=await r.json();if(!r.ok){const e=new Error(d.error?.message||t('市场缓存暂不可用'));e.status=r.status;e.code=d.error?.code;throw e;}return d;}
  function ask(kind,id,text){if(window.NavrenViews?.ask)return window.NavrenViews.ask(text,{kind,id,source_scope:'market',label:t('公开市场资料')}).catch(error=>showToast(error.message));const p=new URLSearchParams({market_kind:kind,market_id:id,q:text});NavrenApp.navigate('chat.html?'+p,getInterfaceMode());}
  function source(a){const box=el('div',undefined,'market-source');box.append(link(a.publisher+' · 阅读原文',a.url,a),el('span',((a.reported_time_kind==='source_page_updated_at'?t('时间')+' ':'')+date(a.published_at||a.reported_at,a.published_precision||a.reported_precision||'date'))));
    if(a.origin_publisher&&![a.publisher,a.publisher.replace('财经','')].includes(a.origin_publisher))box.append(el('span','稿件来源：'+a.origin_publisher));
    if(!readable(a))box.append(el('span','当前原文暂不可直接阅读','market-limit'));
    return box;}
  function bookmark(kind,obj){const rows=Storage.get('qingzhou_bookmarks')||[];const id=obj.id||obj.period;
    if(rows.some(x=>x.object_kind===kind&&x.object_id===id)){showToast('已经收藏');return;}
    const a=obj.articles?.[0]||obj.reading?.[0]||obj;
    rows.push({category:kind==='weekly'?'market_weekly':'market_news',object_kind:kind,object_id:id,
      text:obj.headline||obj.title_cn||obj.title,source:a.publisher||'Navren周报',url:a.url||null,
      published_at:obj.published_at||obj.period_start,version:obj.version,...(obj.archive?{archive:{...obj.archive}}:{}),timestamp:new Date().toISOString()});
    Storage.set('qingzhou_bookmarks',rows);showToast('已收藏');}
  function error(root,e){root.replaceChildren(el('p',e.message||'市场缓存暂不可用','market-limit'));}
  function prefs(){return {...{marketHotVisible:true,weeklyReport:true},...(Storage.get('qingzhou_preferences')||{})};}
  function disabled(root,key,name){root.replaceChildren(el('p','你已关闭'+name+'的页面展示。'),button('重新显示'+name,()=>{const p=prefs();p[key]=true;Storage.set('qingzhou_preferences',p);recommend();}));}
  function eventCard(e){
    const card=el('article',undefined,'market-event-card'),a=e.articles[0];
    const kind={official_release:'官方原始发布',official_interpretation:'官方数据解读',official_speech:'官方演讲观点',official_interview:'官方采访观点',media_report:'媒体报道'};
    const meta=el('p',a.publisher+' · '+date(e.published_at,a.published_precision||'date')+' · '+(kind[a.identity]||'公开材料'),'market-meta');
    const h=el('h3');h.append(link(e.headline,a.url,a));card.append(meta,h,el('p',e.summary,'market-summary'));
    const details=el('details',undefined,'market-explanation');details.append(el('summary','Navren 解读'));
    details.append(el('h4','发生了什么'));for(const f of e.facts)details.append(el('p',f.text));
    details.append(el('h4','传导与条件'),el('p',e.analysis),el('h4','仍有哪些不确定性'),el('p',e.uncertainty));
    for(const article of e.articles)details.append(source(article));
    const actions=el('div',undefined,'market-actions');actions.append(details,button('问 Navren',()=>ask('event',e.id,'请解读这条已核验市场事件，并说明它如何影响银行客户的资金安排。')),button('收藏',()=>bookmark('event',e)));
    card.append(actions);return card;
  }
  function readingCard(a){const card=el('article',undefined,'market-reading-card');const h=el('h3');h.append(link(a.title_cn||a.title,a.url,a));card.append(h,source(a),button('问 Navren',()=>ask('article',a.id,'请解读这篇公开报道，区分事实、媒体观点和分析。')),button('收藏',()=>bookmark('article',a)));return card;}
  let deck=null,hotData=null,hotRequest=0,pendingHot=null,historicalPanel=null;
  const topics={macro:'宏观数据',rates:'利率政策',household:'民生金融',international:'国际市场',general:'综合'};
  const identities={official_release:'公告',official_interpretation:'数据',official_speech:'观点',official_interview:'观点',media_report:'报道'};
  const fullDate=(x,precision='date')=>x?new Date(x).toLocaleString(window.NavrenI18n?.locale||'zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',...(precision==='date'?{}:{hour:'2-digit',minute:'2-digit'})}):t('暂无核验日期');
  const theme=e=>Object.hasOwn(topics,e.theme)?e.theme:Object.hasOwn(topics,e.topic_id)?e.topic_id: e.articles?.[0]?.source_id?.startsWith('nbs')?'macro': ['fed','ecb'].includes(e.articles?.[0]?.source_id)?'international': /住房|购房|消费贷款/.test(e.headline||'')?'household': e.articles?.[0]?.source_id==='pbc'?'rates':'general';
  function archiveReference(e){return {kind:'event',id:e.id,version:e.archive?.event_version??e.version,source_scope:'market',label:e.headline,...(e.archive?{parameters:{edition_date:e.archive.edition_date,revision:e.archive.revision,event_version:e.archive.event_version}}:{})};}
  function historyEntry(){const a=el('a',undefined,'market-history-entry');a.id='marketHistoryEntry';a.href='recommend.html?view=market_history';a.setAttribute('aria-label',t('历史热点'));const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');const p=document.createElementNS(svg.namespaceURI,'path');p.setAttribute('d','M12 3a9 9 0 1 1-8.4 5.8M3 3v6h6M12 7v5l3 2');svg.append(p);a.append(svg,el('span',t('历史热点')));a.onclick=e=>{if(e.button!==0||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;e.preventDefault();NavrenViews.update({listposition:deck?.root?.dataset.eventId||null,focustarget:a.id});NavrenViews.open('market_history');};return a;}
  function svgButton(label,path,fn){const b=button('',fn);b.className='market-icon';b.setAttribute('aria-label',label);b.title=label;const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');const p=document.createElementNS(svg.namespaceURI,'path');p.setAttribute('d',path);svg.append(p);b.append(svg);return b;}
  const archiveKey=archive=>archive?[archive.edition_date,Number(archive.revision),Number(archive.event_version)].join(':'):'current';
  function closeForAction(d){d.dataset.marketActionClose='true';d.close();}
  function latestEvent(d,id,version){closeForAction(d);if(window.NavrenViews?.current()?.view==='reading')return NavrenViews.open('reading',{object_kind:'event',object_id:id,object_version:version,source_scope:'public_market'});return openEvent(id,version);}
  function displayCopy(e,field){if(sourceTextSummary(e)||sourceBriefSummary(e))return field==='title'?e.headline:field==='summary'?e.summary:'';const key='display_'+field,english=window.NavrenI18n?.language==='en'&&e[key+'_en'];return typeof english==='string'&&english?english:e[key]||(field==='title'?e.headline:field==='summary'?e.summary:'')||'';}
  function bookmarkIcon(e){
    const b=svgButton('收藏热点','m12 3 2.8 5.7 6.3.9-4.5 4.4 1 6.2-5.6-2.9-5.6 2.9 1-6.2L3.7 9.6l6.3-.9Z',()=>{
      const rows=Storage.get('qingzhou_bookmarks')||[],index=rows.findIndex(x=>x.object_kind==='event'&&x.object_id===e.id);
      if(index>=0&&Number(rows[index].version)===e.version&&archiveKey(rows[index].archive)===archiveKey(e.archive)){rows.splice(index,1);Storage.set('qingzhou_bookmarks',rows);showToast('已取消收藏');}
      else {if(index>=0){rows.splice(index,1);Storage.set('qingzhou_bookmarks',rows);}bookmark('event',e);}
      syncBookmarks();
    });b.dataset.bookmarkId=e.id;b.dataset.bookmarkVersion=e.version;b.dataset.deckFocus='bookmark';if(e.archive){b.dataset.bookmarkArchiveDate=e.archive.edition_date;b.dataset.bookmarkArchiveRevision=e.archive.revision;}syncBookmark(b);return b;
  }
  function syncBookmark(b){const kind=b.dataset.bookmarkKind||'event',found=(Storage.get('qingzhou_bookmarks')||[]).find(x=>x.object_kind===kind&&x.object_id===b.dataset.bookmarkId),archive=b.dataset.bookmarkArchiveDate?{edition_date:b.dataset.bookmarkArchiveDate,revision:b.dataset.bookmarkArchiveRevision,event_version:b.dataset.bookmarkVersion}:null,same=!!found&&Number(found.version)===Number(b.dataset.bookmarkVersion)&&archiveKey(found.archive)===archiveKey(archive);b.setAttribute('aria-pressed',same);b.classList.toggle('is-saved',same);const label=found?(same?(kind==='weekly'?'取消收藏周报':'取消收藏热点'):archive?'替换为此归档收藏（已收藏其他版本）':'替换为当前版本收藏（已收藏其他版本）'):(kind==='weekly'?'收藏周报':'收藏热点');b.setAttribute('aria-label',t(label));b.title=t(label);}
  function syncBookmarks(){document.querySelectorAll('[data-bookmark-id]').forEach(syncBookmark);}
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
    const top=el('div',undefined,'market-deck-top');top.dataset.deckDragSurface='true';const tags=el('div',undefined,'market-deck-tags');tags.append(el('span',categoryLabel(e)));const time=el('time',t('时间')+' '+fullDate(e.reported_at,e.reported_precision||'minute'));time.dateTime=e.reported_at;top.append(tags,time,bookmarkIcon(e));
    const body=el('div',undefined,'market-deck-body'),copy=el('div',undefined,'market-deck-copy'),title=el('h3',e.headline);title.setAttribute('data-i18n-skip','');const summary=el('p',textSummary||sourceBriefSummary(e));summary.setAttribute('data-i18n-skip','');copy.append(title,summary);body.append(copy);
    const bottom=el('footer',undefined,'market-deck-bottom');bottom.dataset.deckDragSurface='true';bottom.append(el('span',a.publisher));const actions=el('div',undefined,'market-deck-actions'),original=link(t('查看原文'),a.url,a),detail=button(t('Navren 解读').replace('Navren 解读','Navren解读'),()=>e.archive?openHistorical(e):openEvent(e.id,e.version));original.dataset.deckFocus='original';detail.dataset.deckFocus='explanation';actions.append(detail,original);bottom.append(actions);
    const full=el('div',undefined,'market-flow-full');full.append(top,body,bottom);const preview=el('div',undefined,'market-flow-preview');preview.setAttribute('aria-hidden','true');preview.append(el('span',categoryLabel(e),'market-flow-topic'),el('span',categoryLabel(e),'market-flow-topic-short'),el('h4',e.headline),el('span',a.publisher,'market-flow-publisher'));card.append(full,preview);return card;
  }
  function renderSourceBrief(e,content,sheet,d,textSummary=''){
    const a=e.articles[0],hero=el('section',undefined,'market-panel-hero'),meta=el('div',undefined,'market-panel-meta'),time=el('time',t('时间')+' '+fullDate(a.published_at||e.reported_at,a.published_precision||e.reported_precision||'minute'));
    time.dateTime=a.published_at||e.reported_at;meta.append(el('span',a.publisher),time);hero.append(meta,el('h3',e.headline,'market-panel-title'));
    prose(hero,textSummary||sourceBriefSummary(e),'market-panel-prose market-panel-lead');content.append(hero);
    const facts=panelSection(t('已核验事实')),quotes=e.source_summary_quotes||a.source_check?.summary_quotes||[],seen=new Set();
    for(const f of e.facts||[]){if(f.article_id!==a.id||typeof f.text!=='string'||seen.has(f.text)||textSummary&&!quotes.includes(f.text))continue;seen.add(f.text);const row=el('div',undefined,'market-panel-fact');prose(row,f.text);facts.append(row);}
    if(!seen.size)prose(facts,textSummary||sourceBriefSummary(e));content.append(facts);
    const analysis=panelSection(t('Navren 分析')),conditions=panelSection(t('条件与不确定性'),'market-panel-conditions'),sources=panelSection(t('原始资料'));
    const approved=e.review?.status==='approved'&&e.articles.every(readable)&&typeof e.analysis==='string'&&e.analysis.trim();
    prose(analysis,approved?e.analysis:sourcePanelCopy('可点击「问 Navren」，结合这篇原文继续解读事件及其影响。'));
    prose(conditions,e.uncertainty||sourcePanelCopy('当前资料范围以所列原文为准，后续变化需结合新的公开材料核对。'));
    sources.append(source(a,e));content.append(analysis,conditions,sources);
    const actions=el('footer',undefined,'market-panel-actions'),askButton=button(t('问 Navren'),()=>{closeForAction(d);const prompt=t(e.archive?'请解读这一天选中的事件版本，区分当时事实与后来修订。':'请解读这条已核验市场事件，并说明它如何影响银行客户的资金安排。');if(window.NavrenViews?.ask)NavrenViews.ask(prompt,archiveReference(e)).catch(error=>showToast(error.message));else ask('event',e.id,prompt);});
    askButton.dataset.marketReference='true';actions.append(askButton,bookmarkIcon(e));sheet.append(actions);
  }
  function frontCard(e){
    const textSummary=sourceTextSummary(e);if(textSummary||sourceBriefSummary(e))return briefCard(e,textSummary);
    const a=e.articles?.[0]||{publisher:t('来源暂不可读'),identity:'unknown'},card=el('article',undefined,'market-deck-card');card.dataset.topic=theme(e);card.dataset.version=e.archive?.event_version??e.version;if(e.archive){card.dataset.editionDate=e.archive.edition_date;card.dataset.editionRevision=e.archive.revision;}
    const top=el('div',undefined,'market-deck-top');top.dataset.deckDragSurface='true';
    const tags=el('div',undefined,'market-deck-tags');tags.append(el('span',topics[theme(e)]),el('span',identities[a.identity]||'公开材料'));
    if(e.version>1)tags.append(el('span','已修订','market-deck-revised'));
    const time=el('time',fullDate(e.published_at,a.published_precision||'date'));time.dateTime=e.published_at;
    top.append(tags,time,bookmarkIcon(e));
    const body=el('div',undefined,'market-deck-body'),copy=el('div',undefined,'market-deck-copy');copy.append(el('h3',displayCopy(e,'title')));
    const usable=!e.availability||e.availability==='active';copy.append(el('p',usable?displayCopy(e,'summary'):t({revised:'内容已修订，历史正文停止展示。',withdrawn:'这条资料已撤下，保留当日选稿记录。',temporarily_unavailable:'原文暂不可访问，请稍后核查。'}[e.availability]||'归档资料当前不可读。')));if(usable&&e.display_conditions)copy.append(el('small',displayCopy(e,'conditions'),'market-deck-condition'));body.append(copy);
    const f=usable?e.display_fact:null;if(f){const fact=el('div',undefined,'market-deck-fact');fact.append(el('span',f.label));if(f.value){const metric=el('p',undefined,'market-deck-value');metric.append(el('strong',f.value),el('span',f.unit));fact.append(metric,el('span',f.period));}else fact.append(el('p',f.text));body.append(fact);}
    const bottom=el('footer',undefined,'market-deck-bottom');bottom.dataset.deckDragSurface='true';bottom.append(el('span',a.publisher));const actions=el('div',undefined,'market-deck-actions'),original=link(t('查看原文'),a.url,a);original.dataset.deckFocus='original';const read=button(t('Navren 解读').replace('Navren 解读','Navren解读'),()=>e.archive?openHistorical(e):openEvent(e.id,e.version));read.dataset.deckFocus='explanation';actions.append(read,original);bottom.append(actions);
    const full=el('div',undefined,'market-flow-full');full.append(top,body,bottom);
    const preview=el('div',undefined,'market-flow-preview');preview.setAttribute('aria-hidden','true');preview.append(el('span',topics[theme(e)],'market-flow-topic'),el('span',({macro:'宏观',rates:'利率',household:'民生',international:'国际',general:'综合'})[theme(e)],'market-flow-topic-short'),el('h4',displayCopy(e,'title')),el('span',a.publisher,'market-flow-publisher'));
    card.append(full,preview);return card;
  }
  function panel(title,topic='general',options={}){
    document.querySelectorAll('.market-panel-layer[open]').forEach(closeForAction);
    const trigger=document.activeElement,d=el('dialog',undefined,'market-panel-layer'),sheet=el('div',undefined,'market-panel'),backdrop=el('div',undefined,'market-panel-backdrop'),header=el('header'),heading=el('h2',title),content=el('div',undefined,'market-panel-content');
    sheet.dataset.topic=Object.hasOwn(topics,topic)?topic:'general';backdrop.setAttribute('aria-hidden','true');
    const masthead=el('div',undefined,'market-panel-heading');masthead.append(el('span',topics[sheet.dataset.topic],'market-panel-topic'),heading);
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
    row.append(el('span',String(index+1).padStart(2,'0'),'market-panel-source-index'),body);return row;
  }
  async function openEvent(id,version){
    const topic=deck?.cards.get(id)?.card.dataset.topic||theme(hotData?.events.find(e=>e.id===id)||{});
    const {d,content,sheet}=panel('Navren 解读',topic);content.append(el('p','正在核对事件版本…','market-limit'));
    try{const e=await get('events/'+encodeURIComponent(id));if(!d.isConnected||!d.open)return;content.replaceChildren();
      if(e.status!=='published'||!e.articles?.length||!e.articles.every(readable)){content.append(el('p','原资料已变更或撤下，当前事件等待重新审核。','market-limit'));return;}
      if(version!=null&&Number(version)!==e.version){content.append(el('p',t('内容已修订，历史正文停止展示。'),'market-panel-notice market-limit'),button(t('查看最新修订'),()=>latestEvent(d,id,e.version)));return;}
      const textSummary=sourceTextSummary(e);if(textSummary){renderSourceBrief(e,content,sheet,d,textSummary);return;}
      const a=e.articles[0],hero=el('section',undefined,'market-panel-hero'),meta=el('div',undefined,'market-panel-meta'),time=el('time',fullDate(e.published_at,a.published_precision||'date'));
      time.dateTime=e.published_at;meta.append(el('span',a.publisher),time,el('span',(identities[a.identity]||'公开材料')+' · 第'+e.version+'版'));
      hero.append(meta,el('h3',e.headline,'market-panel-title'));prose(hero,e.summary,'market-panel-prose market-panel-lead');content.append(hero);
      const facts=panelSection('已核验事实'),factSources=new Map();
      for(const f of e.facts){const item=el('div',undefined,'market-panel-fact');prose(item,f.text);facts.append(item);const evidence=e.articles.find(a=>a.id===f.article_id);if(evidence)factSources.set(evidence.url||evidence.id,evidence);}
      if(factSources.size){const citation=el('div',undefined,'market-panel-evidence');citation.dataset.multiple=factSources.size>1;
        for(const evidence of factSources.values())citation.append(link('事实来源 · '+(factSources.size===1?evidence.publisher:(evidence.title_cn||evidence.title||evidence.publisher))+' ↗',evidence.url,evidence));facts.append(citation);}
      content.append(facts);
      const analysis=panelSection('Navren 分析'),conditions=panelSection('条件与不确定性','market-panel-conditions'),sources=panelSection('原始资料');
      prose(analysis,e.analysis);prose(conditions,e.uncertainty);e.articles.forEach((article,i)=>sources.append(panelSource(article,i)));content.append(analysis,conditions,sources);
      const actions=el('footer',undefined,'market-panel-actions'),askButton=button('问 Navren',()=>{closeForAction(d);if(window.NavrenViews?.ask)NavrenViews.ask(t('请解读这条已核验市场事件，并说明它如何影响银行客户的资金安排。'),archiveReference(e)).catch(error=>showToast(error.message));else ask('event',e.id,'请解读这条已核验市场事件，并说明它如何影响银行客户的资金安排。');});askButton.dataset.marketReference='true';actions.append(askButton,bookmarkIcon(e));sheet.append(actions);
    }catch(e){if(d.isConnected&&d.open){content.replaceChildren(el('p',e.message,'market-limit'),button('重新读取',()=>{closeForAction(d);openEvent(id,version);}));}}
  }
  function renderHistoricalEvent(e,content,sheet,d){
    const textSummary=sourceTextSummary(e);if(textSummary||sourceBriefSummary(e)){renderSourceBrief(e,content,sheet,d,textSummary);return;}
    const a=e.articles[0],hero=el('section',undefined,'market-panel-hero'),meta=el('div',undefined,'market-panel-meta');
    meta.append(el('span',t('归档日')+' '+e.archive.edition_date),el('span',t('日版次')+' '+e.archive.revision),el('span',t('事件版本')+' '+e.archive.event_version));
    if(e.archive.origin==='reconstructed')meta.append(el('span',t('历史补录')),el('span',t('补录时间')+' '+fullDate(e.archive.selected_at,'minute')));if(e.after_revision_material){meta.append(el('span',t('事后修订资料')));if(e.revision_confirmed_at)meta.append(el('span',t('可确认修订时间')+' '+fullDate(e.revision_confirmed_at,'minute')));}
    hero.append(meta,el('h3',e.headline,'market-panel-title'),el('p',a.publisher+' · '+fullDate(e.reported_at||e.published_at,e.reported_precision||a.published_precision||'date'),'market-panel-meta'));prose(hero,e.summary,'market-panel-prose market-panel-lead');content.append(hero);
    const facts=panelSection(t('已核验事实')),evidence=new Map();for(const f of e.facts||[]){const row=el('div',undefined,'market-panel-fact');prose(row,f.text);facts.append(row);const source=e.articles.find(a=>a.id===f.article_id);if(source)evidence.set(source.url||source.id,source);}
    if(evidence.size){const references=el('div',undefined,'market-panel-evidence');for(const source of evidence.values())references.append(link(t('事实来源')+' · '+source.publisher+' ↗',source.url,source));facts.append(references);}content.append(facts);
    const analysis=panelSection(t('Navren 分析')),limits=panelSection(t('条件与不确定性')),sources=panelSection(t('原始资料'));prose(analysis,e.analysis);prose(limits,e.uncertainty);[...new Map(e.articles.map(a=>[a.url||a.id,a])).values()].forEach((a,i)=>sources.append(panelSource(a,i)));content.append(analysis,limits,sources);
    const actions=el('footer',undefined,'market-panel-actions'),askButton=button(t('问 Navren'),()=>{closeForAction(d);NavrenViews.ask(t('请解读这一天选中的事件版本，区分当时事实与后来修订。'),archiveReference(e)).catch(err=>showToast(err.message));});askButton.dataset.marketReference='true';actions.append(askButton,bookmarkIcon(e));sheet.append(actions);
  }
  function renderHistoricalState(e,content,sheet,d){
    content.replaceChildren();sheet.querySelector('.market-panel-actions')?.remove();
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
    const {d,content,sheet}=panel(t('Navren 解读'),item.theme||theme(item),{focusFallback:options.focusFallback});sheet.dataset.archiveDate=archive.edition_date;sheet.dataset.archiveRevision=archive.revision;content.append(el('p',t('正在核对归档版本…'),'market-limit'));
    try{const e=await get('hotspots/history/'+encodeURIComponent(archive.edition_date)+'/events/'+encodeURIComponent(item.id)+'?revision='+encodeURIComponent(archive.revision));if(!d.isConnected||!d.open)return;
      if(e.id!==item.id||e.archive?.edition_date!==archive.edition_date||Number(e.archive?.revision)!==Number(archive.revision)||Number(e.archive?.event_version)!==Number(archive.event_version))throw Error(t('归档版本不一致，未展示正文。'));content.replaceChildren();
      if(e.availability==='active'&&(e.status!=='published'||!e.articles?.length||!e.articles.every(readable)))throw Error(t('归档资料当前不可读。'));
      historicalPanel={d,content,sheet,event:e};d.addEventListener('close',()=>{if(historicalPanel?.d===d)historicalPanel=null;},{once:true});renderHistoricalState(e,content,sheet,d);return d;
    }catch(error){if(d.isConnected&&d.open)content.replaceChildren(el('p',t('服务读取失败，请重试。'),'market-limit'),button(t('重试'),()=>{closeForAction(d);openHistorical(item,options);}));return d;}
  }
  function applyHotspots(){if(!prefs().marketHotVisible){pendingHot=null;return;}if(!pendingHot||deck&&deck.state!=='idle'||document.querySelector('dialog[open]'))return;const root=document.getElementById('marketHot');if(!root)return;hotData=pendingHot;pendingHot=null;if(deck)deck.update(hotData.events);else deck=new MarketDeck(root,{events:hotData.events,renderCard:frontCard,theme,historyEntry});const empty=root.querySelector('.market-deck-empty');if(empty)empty.textContent=t(hotData.status==='missing_archive'?'尚未建立每日精选档案。':'当日没有合格精选。');hotHeader();document.querySelector('.market-refresh-notice')?.remove();}
  window.addEventListener('navren:market-deck-idle',applyHotspots);document.addEventListener?.('close',()=>queueMicrotask(applyHotspots),true);
  async function refreshHotspots(){
    const root=document.getElementById('marketHot');if(!root)return;const request=++hotRequest;
    if(!prefs().marketHotVisible){pendingHot=null;deck?.destroy();deck=null;hotData=null;root.classList.remove('market-deck');disabled(root,'marketHotVisible','市场热点');return;}
    if(!deck){root.classList.remove('market-deck');root.replaceChildren(el('div','正在读取已核验市场内容…','market-deck-skeleton'));}
    try{const data=await get('hotspots');
      if(request!==hotRequest)return;if(!prefs().marketHotVisible){refreshHotspots();return;}
      if(!Array.isArray(data.events)||data.events.length>6)throw Error(t('精选清单格式异常，保留最近成功内容。'));
      pendingHot={...data,events:data.events};applyHotspots();
    }catch(e){if(request!==hotRequest)return;if(deck&&hotData){const heading=document.getElementById('marketHotHeading');heading?.querySelector('.market-refresh-notice')?.remove();heading?.append(el('p',t('更新暂未成功，仍显示最近成功精选。'),'market-refresh-notice'));return;}deck=null;hotData=null;root.classList.remove('market-deck');root.replaceChildren(el('p',t('服务读取失败，请重试。'),'market-limit'),button(t('重试'),refreshHotspots),historyEntry());}
  }
  function hotHeader(){const heading=document.getElementById('marketHotHeading');if(!heading)return;if(!heading.querySelector('.market-hot-label')){const title=el('div',undefined,'market-hot-label');title.append(el('h2',t('市场热点')),NavrenHelp.button('market'));heading.append(title);}let stamp=heading.querySelector('.market-edition-date');if(!stamp){stamp=el('span',undefined,'market-edition-date');heading.append(stamp);}const day=hotData?.edition_date;stamp.textContent=day?(hotData.server_today===day?t('当日精选'):t('最近精选'))+' · '+day:t('等待每日精选');}
  window.addEventListener('storage',e=>{if(e.key==='qingzhou_preferences')refreshMarket();if(e.key==='qingzhou_bookmarks')syncBookmarks();});
  window.addEventListener('pageshow',e=>{if(e.persisted)refreshMarket();});
  function dialog(title){const d=el('dialog',undefined,'market-dialog'),head=el('header'),unlock=lockPageScroll();head.append(el('h2',title),button('关闭',()=>d.close()));d.append(head);document.body.append(d);d.addEventListener('close',()=>{d.remove();unlock();},{once:true});d.showModal();return d;}
  function periodLabel(w){
    // 服务器保存右开区间；卡面显示完整周的周一至周日，不把下一周周一混入本期。
    const end=new Date(new Date(w.period_end_exclusive).getTime()-1).toLocaleDateString('en-CA',{timeZone:'Asia/Shanghai'});
    const start=w.period_start.slice(0,10);return start+' — '+end;
  }
  function weeklyBookmark(w){
    const b=svgButton('收藏周报','m12 3 2.8 5.7 6.3.9-4.5 4.4 1 6.2-5.6-2.9-5.6 2.9 1-6.2L3.7 9.6l6.3-.9Z',()=>{
      const rows=Storage.get('qingzhou_bookmarks')||[],i=rows.findIndex(x=>x.object_kind==='weekly'&&x.object_id===w.period);
      if(i>=0&&rows[i].version===w.version){rows.splice(i,1);Storage.set('qingzhou_bookmarks',rows);showToast('已取消收藏');}
      else{if(i>=0){rows.splice(i,1);Storage.set('qingzhou_bookmarks',rows);}bookmark('weekly',w);}
      syncBookmarks();
    });
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
    if(w.period!==data.expected_period)card.append(el('p','本期尚在整理，先阅读最近已发布周报。','market-weekly-notice'));
    const foot=el('footer',undefined,'market-weekly-footer'),timing=el('div',undefined,'market-weekly-timing');timing.append(el('span','每周一更新 · 北京时间'));
    const published=el('time','发布于 '+fullDate(w.released_at,'minute'));published.dateTime=w.released_at;timing.append(published);
    const actions=el('div',undefined,'market-weekly-actions'),read=button('阅读完整周报',()=>openWeekly(w.period)),chat=button('问 Navren',()=>ask('weekly',w.period,'请解释这期市场周报的主要变化与条件。'));
    read.classList.add('market-weekly-primary');read.dataset.weeklyAction='read';chat.dataset.weeklyAction='ask';actions.append(read,chat,weeklyBookmark(w));foot.append(timing,actions);card.append(foot);return card;
  }
  async function openWeekly(period='latest',anchor=''){
    const {d,content,sheet}=panel('市场周报','general');sheet.classList.add('market-weekly-reader');
    content.append(el('p','正在读取已发布周报…','market-limit'));
    try{const data=await get('weeklies/'+encodeURIComponent(period)),w=data.report;if(!d.isConnected)return;content.replaceChildren();
      if(!w){content.append(el('p','暂无已发布完整周报。','market-limit'));return;}
      if(w.status!=='published'||!w.source_articles?.length||!w.source_articles.every(readable)){content.append(el('p','原文已修订或暂不可直接阅读，当前周报等待重新审核。','market-limit'));return;}
      const hero=el('section',undefined,'market-panel-hero'),meta=el('div',undefined,'market-panel-meta');meta.append(el('span',periodLabel(w)),el('span','第'+w.version+'版'),el('span','北京时间'));hero.append(meta,el('h3',w.title,'market-panel-title'));prose(hero,w.summary,'market-panel-prose market-panel-lead');content.append(hero);
      const articles=new Map((w.source_articles||w.reading||[]).map(a=>[a.id,a]));
      (w.changes||[]).forEach((c,i)=>{const section=panelSection(c.heading,'market-weekly-change');section.id='weekly-change-'+i;section.querySelector('h3').prepend(el('span',String(i+1).padStart(2,'0'),'market-weekly-number'));prose(section,c.text);if(c.conditions){const condition=el('div',undefined,'market-weekly-condition');condition.append(el('strong','关注条件'));prose(condition,c.conditions);section.append(condition);}content.append(section);});
      const transmission=panelSection('传导与分歧');prose(transmission,w.transmission);content.append(transmission);
      if(w.observations?.length){const observations=panelSection('关键数据对照');for(const x of w.observations)observations.append(el('p',x.label+'：'+x.value+' '+x.unit),link('指标原始来源',x.url));content.append(observations);}
      if(w.next_week_schedule?.length){const schedule=panelSection('下周关注');for(const item of w.next_week_schedule){const row=el('div',undefined,'market-schedule');row.append(el('strong',item.title+' · '+item.date),link('官方日程',item.url),button('加入财务日历提醒',()=>{d.close();ask('weekly',w.period,'请为周报中“'+item.title+'”准备一个日历提醒确认卡。');}));schedule.append(row);}content.append(schedule);}
      // 所有段落使用同一资料包：统一放在文末，按具体原文去重。
      const library=panelSection('原始资料'),unique=[...new Map([...articles.values()].map(a=>[a.url||a.id,a])).values()];unique.forEach((a,i)=>library.append(panelSource(a,i)));content.append(library);
      const limits=panelSection('阅读说明','market-panel-conditions');for(const notice of [w.observation_notice,w.schedule_notice,...(w.limitations||[])].filter(Boolean))prose(limits,notice);content.append(limits);
      const actions=el('footer',undefined,'market-panel-actions');actions.append(button('问 Navren',()=>{d.close();ask('weekly',w.period,'请解释这期市场周报最重要的变化与传导条件。');}),weeklyBookmark(w));sheet.append(actions);
      if(anchor)content.querySelector('#'+anchor)?.scrollIntoView({block:'start',behavior:'instant'});
    }catch(e){if(d.isConnected)content.replaceChildren(el('p',e.message,'market-limit'),button('重新读取',()=>{d.close();openWeekly(period,anchor);}));}
  }
  let weeklySignature='',weeklyFlight=null,refreshFlight=null,refreshTimer=null;
  async function refreshWeekly(){
    const root=document.getElementById('weeklyReport');if(!root)return;
    if(!prefs().weeklyReport){weeklySignature='';disabled(root,'weeklyReport','市场周报');return;}
    if(weeklyFlight)return weeklyFlight;
    if(!weeklySignature)root.replaceChildren(el('p','正在读取已发布周报…','market-weekly-placeholder'));
    weeklyFlight=(async()=>{try{const data=await get('weeklies/latest');if(!prefs().weeklyReport)return;
      const signature=JSON.stringify([data.report?.period,data.report?.version,data.report?.status,data.expected_period,data.history]);if(signature===weeklySignature)return;
      const focus=document.activeElement?.closest('#weeklyReport [data-weekly-action]')?.dataset.weeklyAction;
      weeklySignature=signature;root.replaceChildren();const w=data.report;
      if(w?.status==='published'&&w.source_articles?.length&&w.source_articles.every(readable))root.append(weeklyCard(data));else root.append(el('p','本期周报正在整理，发布后会自动显示。','market-weekly-placeholder'));
      if(data.history?.length){const archive=el('details',undefined,'market-weekly-archive');archive.append(el('summary','历史周报'));const select=el('select');select.setAttribute('aria-label','历史周报');const placeholder=el('option','选择历史期号');placeholder.value='';select.append(placeholder);for(const h of data.history){const option=el('option',h.period+' · 第'+h.version+'版'+(h.status==='published'?'':' · 等待修订'));option.value=h.period;select.append(option);}select.onchange=()=>{if(select.value)openWeekly(select.value);select.value='';};archive.append(select);root.append(archive);}
      if(focus)root.querySelector('[data-weekly-action="'+focus+'"]')?.focus({preventScroll:true});
    }catch(e){if(!weeklySignature)root.replaceChildren(el('p',e.message,'market-weekly-placeholder'),button('重试',refreshWeekly));}finally{weeklyFlight=null;}})();return weeklyFlight;
  }
  function refreshMarket(){if(refreshFlight)return refreshFlight;refreshFlight=Promise.allSettled([refreshHotspots(),refreshWeekly()]).finally(()=>{refreshFlight=null;});return refreshFlight;}
  function visibleRefresh(){if(document.visibilityState!=='hidden'&&!document.querySelector('dialog[open]'))refreshMarket();}
  async function recommend(){
    if(!document.getElementById('marketHot')||!document.getElementById('weeklyReport'))return;
    hotHeader();if(!refreshTimer){refreshTimer=setInterval(visibleRefresh,300000);document.addEventListener('visibilitychange',visibleRefresh);window.addEventListener('focus',visibleRefresh);}
    await refreshMarket();
  }
  function sources(message,rows){if(!rows?.length)return;const box=el('section',undefined,'market-chat-sources');box.append(el('h4','对应原文与来源'));for(const a of rows)box.append(source(a));message.querySelector('.bubble').append(box);}
  async function openBookmark(b){try{
    if(!b.object_kind||!b.object_id){showToast('这是旧样例收藏，不能作为当前核验资讯。');return;}
    if(b.object_kind==='weekly'){await openWeekly(b.object_id);return;}
    if(b.object_kind==='event'){if(b.archive)await openHistorical({id:b.object_id,version:b.version,archive:b.archive});else await openEvent(b.object_id,b.version);return;}
    const data=await get((b.object_kind==='event'?'events/':'articles/')+encodeURIComponent(b.object_id));
    const d=dialog('收藏的市场资料');if(!['active','published'].includes(data.status)||(!data.articles&&!readable(data))){d.append(el('p','原内容已变更或暂不可直接阅读，请以最新资料为准。','market-limit'));return;}
    if(b.version!==data.version)d.append(el('p','内容已有修订，下面展示最新版本。','market-limit'));
    d.append(data.articles?eventCard(data):readingCard(data));
  }catch(e){showToast(e.message);}}
  window.addEventListener('navren:language',()=>{hotHeader();if(hotData&&!pendingHot){pendingHot=hotData;applyHotspots();}if(historicalPanel?.d.isConnected&&historicalPanel.d.open){const {d,content,sheet,event}=historicalPanel,top=content.scrollTop;sheet.querySelector('header h2').textContent=t('Navren 解读');sheet.querySelector('header .market-button').textContent=t('关闭');sheet.querySelector('.market-panel-topic').textContent=t(topics[sheet.dataset.topic]);renderHistoricalState(event,content,sheet,d);content.scrollTop=top;}syncBookmarks();});
  return {recommend,openWeekly,openBookmark,sources,ask,get,refreshHotspots,refreshWeekly,refreshMarket,bindDisplay,periodLabel,weeklyCard,openHistorical,archiveReference,historyEntry,sourceLink:link,readable,theme,displayCopy,restoreHotspot:id=>deck?.restore(id),hotspot:()=>({event_id:deck?.root?.dataset.eventId,data:hotData,state:deck?.state}),applyHotspots};
})();
// 资讯组件自行启动，页面组织调整时仍可独立挂载；重复调用由内部去重。
if(typeof document.getElementById==='function'){if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',()=>window.MarketUI.recommend(),{once:true});else window.MarketUI.recommend();}

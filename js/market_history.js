/* 只读的每日精选档案。日期窗口、分类与版本由公共资讯服务决定。 */
window.MarketHistory=(()=>{
  const t=x=>window.NavrenI18n?.t(x)||x;
  const locale=()=>window.NavrenI18n?.locale||'zh-CN';
  const node=(tag,text,cls)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;};
  const control=(label,fn,cls='market-history-control')=>{const b=node('button',t(label),cls);b.type='button';b.onclick=fn;return b;};
  const iso=d=>d.toISOString().slice(0,10);
  const stamp=d=>new Date(d+'T12:00:00Z');
  const shifted=(d,days)=>iso(new Date(stamp(d).getTime()+days*86400000));
  const validDate=d=>typeof d==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(d)&&Number.isFinite(stamp(d).getTime())&&iso(stamp(d))===d;
  const format=(d,full=false)=>validDate(d)?new Intl.DateTimeFormat(locale(),{timeZone:'UTC',...(full?{year:'numeric'}:{}),month:'long',day:'numeric'}).format(stamp(d)):d;
  const unavailable={revised:'内容已修订，历史正文停止展示。',withdrawn:'这条资料已撤下，保留当日选稿记录。',temporarily_unavailable:'原文暂不可访问，请稍后核查。'};
  let request=0,abort=null,state=null,ui=null,active=false;
  const inWindow=d=>!!state?.index&&validDate(d)&&d>=state.index.window_start&&d<=state.index.window_end;
  function pause(){active=false;++request;abort?.abort();closeCalendar();}
  function clearIdentity(){const wasActive=active;pause();state=null;ui=null;if(wasActive)document.getElementById('discoverSubview')?.replaceChildren(node('p',t('账户或授权已变化，历史内容已清除。'),'market-history-empty'));}
  function categories(){return state.index.categories||[];}
  function categoryLabel(id){const row=categories().find(x=>x.id===id);return row?(locale()==='en-US'?row.name_en||t(row.name):row.name):t('全部类型');}
  function categorySelect(){const chosen=state.category;ui.category.replaceChildren();const all=node('option',t('全部类型'));all.value='';ui.category.append(all);for(const category of categories()){const option=node('option',categoryLabel(category.id));option.value=category.id;ui.category.append(option);}ui.category.value=chosen;}
  function saveContext(){window.NavrenViews?.update({date:state.date,filters:{...(state.context.filters||{}),category:state.category},listposition:state.context.listposition||null});}
  function monthTitle(){return new Intl.DateTimeFormat(locale(),{timeZone:'UTC',year:'numeric',month:'long'}).format(stamp(state.month+'-01'));}
  function closeCalendar(){if(!ui)return;ui.calendar.classList.remove('is-open');ui.chooser.setAttribute('aria-expanded','false');}
  function calendar(){
    if(!state.index)return;const focused=document.activeElement?.dataset.historyDay;
    ui.month.textContent=monthTitle();ui.previous.setAttribute('aria-label',t('上个月'));ui.next.setAttribute('aria-label',t('下个月'));
    const first=stamp(state.month+'-01'),previous=iso(new Date(Date.UTC(first.getUTCFullYear(),first.getUTCMonth()-1,1))).slice(0,7),next=iso(new Date(Date.UTC(first.getUTCFullYear(),first.getUTCMonth()+1,1))).slice(0,7);
    ui.previous.disabled=previous<state.index.window_start.slice(0,7);ui.next.disabled=next>state.index.window_end.slice(0,7);
    const weekdays=node('div',null,'market-history-weekdays');for(const [i,day] of ['一','二','三','四','五','六','日'].entries()){const label=node('span',locale()==='en-US'?['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][i]:day);label.setAttribute('aria-label',t('星期'+day));weekdays.append(label);}
    const grid=node('div',null,'market-history-days');grid.setAttribute('role','group');grid.setAttribute('aria-label',t('选择精选日期'));
    const begin=shifted(state.month+'-01',-(first.getUTCDay()+6)%7),last=iso(new Date(Date.UTC(first.getUTCFullYear(),first.getUTCMonth()+1,0))),cells=Math.ceil(((stamp(last)-stamp(begin))/86400000+1)/7)*7;
    for(let i=0;i<cells;i++){const day=shifted(begin,i),record=state.index.dates.find(row=>row.date===day),b=control('',()=>chooseDate(day),'market-history-day');b.textContent=String(Number(day.slice(-2)));b.id='market-history-day-'+day;b.dataset.historyDay=day;b.disabled=!inWindow(day);b.classList.toggle('is-outside-month',day.slice(0,7)!==state.month);b.classList.toggle('has-archive',record?.archive_state==='archived'||record?.archive_state==='no_qualified');b.classList.toggle('has-category',record?.matched_count>0);b.setAttribute('aria-pressed',day===state.date);if(day===state.index.server_today)b.setAttribute('aria-current','date');
      b.setAttribute('aria-label',format(day,true)+' · '+t(record?.archive_state==='missing'||!record?'尚未建立档案':record.archive_state==='no_qualified'?'当日没有合格精选。':'有精选档案'));b.addEventListener('keydown',event=>dayKey(event,day));grid.append(b);}
    ui.calendarGrid.replaceChildren(weekdays,grid);if(focused)document.getElementById('market-history-day-'+focused)?.focus({preventScroll:true});
  }
  function dayKey(event,day){let destination;const dow=(stamp(day).getUTCDay()+6)%7;if(event.key==='ArrowLeft')destination=shifted(day,-1);else if(event.key==='ArrowRight')destination=shifted(day,1);else if(event.key==='ArrowUp')destination=shifted(day,-7);else if(event.key==='ArrowDown')destination=shifted(day,7);else if(event.key==='Home')destination=shifted(day,-dow);else if(event.key==='End')destination=shifted(day,6-dow);else if(event.key==='Escape'){event.preventDefault();closeCalendar();ui.chooser.focus({preventScroll:true});return;}if(!destination)return;event.preventDefault();if(!inWindow(destination))return;if(destination.slice(0,7)!==state.month){state.month=destination.slice(0,7);calendar();}document.getElementById('market-history-day-'+destination)?.focus({preventScroll:true});}
  function monthMove(direction){const first=stamp(state.month+'-01');state.month=iso(new Date(Date.UTC(first.getUTCFullYear(),first.getUTCMonth()+direction,1))).slice(0,7);calendar();}
  function shell(root){
    root.replaceChildren();const back=node('a',t('返回今日热点'),'market-history-back');back.id='marketHistoryBack';back.href='recommend.html';back.onclick=event=>{if(event.button!==0||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;event.preventDefault();if(window.NavrenViews)window.NavrenViews.back();else location.href=back.href;};
    const header=node('div',null,'market-history-top');header.append(back);root.append(header);
    const layout=node('div',null,'market-history-layout'),filters=node('div',null,'market-history-filters'),chooser=control('',()=>{const isOpen=calendarBox.classList.toggle('is-open');chooser.setAttribute('aria-expanded',isOpen);if(isOpen)calendarBox.querySelector('[aria-pressed="true"]')?.focus({preventScroll:true});},'market-history-date-chooser');chooser.id='marketHistoryDateChooser';chooser.setAttribute('aria-haspopup','dialog');chooser.setAttribute('aria-expanded','false');chooser.setAttribute('aria-controls','marketHistoryCalendar');
    const label=node('label',null,'market-history-category'),select=node('select');select.id='marketHistoryCategory';select.setAttribute('aria-label',t('筛选精选类型'));select.onchange=()=>{state.category=select.value;state.context.filters={...(state.context.filters||{}),category:state.category};saveContext();load();};label.append(select);filters.append(chooser,label);
    const calendarBox=node('section',null,'market-history-calendar');calendarBox.id='marketHistoryCalendar';calendarBox.setAttribute('aria-label',t('近30天精选日历'));const monthHead=node('div',null,'market-history-month'),previous=control('‹',()=>monthMove(-1)),month=node('strong'),next=control('›',()=>monthMove(1));monthHead.append(previous,month,next);const calendarGrid=node('div');calendarBox.append(monthHead,calendarGrid);
    const results=node('section',null,'market-history-results'),heading=node('h2',null,'market-history-date-heading');heading.id='marketHistoryDateHeading';heading.tabIndex=-1;const count=node('p',null,'market-history-count');count.setAttribute('role','status');const cards=node('div',null,'market-history-cards');const resultHeader=node('div',null,'market-history-results-header');resultHeader.append(heading,filters);results.append(resultHeader,count,cards);layout.append(calendarBox,results);root.append(layout);
    ui={root,back,layout,filters,chooser,category:select,calendar:calendarBox,month,previous,next,calendarGrid,heading,count,cards};
  }
  function chooseDate(day){if(!inWindow(day))return;state.date=day;state.month=day.slice(0,7);state.context.date=day;state.context.listposition=null;saveContext();calendar();closeCalendar();if(window.matchMedia('(max-width:720px)').matches)ui.chooser.focus({preventScroll:true});load();}
  function loading(){ui.heading.textContent=state.date?format(state.date,true):t('历史热点');ui.chooser.textContent=(state.date?format(state.date):t('选择日期'))+' ▾';ui.count.textContent=t('正在读取当日精选…');ui.cards.replaceChildren();ui.cards.setAttribute('aria-busy','true');for(let i=0;i<2;i++)ui.cards.append(node('div',null,'market-history-skeleton'));}
  function card(item){
    const usable=item.availability==='active',article=node('article',null,'market-history-card');article.dataset.topic=MarketUI.theme(item);article.dataset.eventId=item.id;
    const meta=node('div',null,'market-history-card-meta'),time=node('time');const reported=item.reported_at||item.published_at;time.dateTime=reported||'';time.textContent=reported?new Intl.DateTimeFormat(locale(),{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',...((item.reported_precision||'date')==='date'?{}:{hour:'2-digit',minute:'2-digit'})}).format(new Date(reported)):t('报道日期未披露');if(reported)time.textContent=t('时间')+' '+time.textContent;meta.append(node('span',categoryLabel(item.category)),time);if(item.after_revision_material)meta.append(node('span',t('事后修订资料')));
    const badges=node('div',null,'market-history-badges');if(item.archive?.origin==='reconstructed')badges.append(node('span',t('历史补录')));
    const title=node('h3'),open=control('',()=>read(item),'market-history-title');open.id='market-history-item-'+state.date+'-'+encodeURIComponent(item.id);open.textContent=MarketUI.displayCopy(item,'title');open.setAttribute('data-i18n-skip','');title.append(open);const summary=node('p',usable?MarketUI.displayCopy(item,'summary'):t(unavailable[item.availability]||'归档资料当前不可读。'),'market-history-summary');if(usable)summary.setAttribute('data-i18n-skip','');
    const footer=node('footer'),source=item.articles?.find(a=>a.id===item.archive.primary_article_id)||item.articles?.[0],publisher=node('span',source?.publisher||t('来源暂不可读'));publisher.setAttribute('data-i18n-skip','');footer.append(publisher);if(usable&&source&&MarketUI.readable(source))footer.append(MarketUI.sourceLink(t('阅读原文 ↗'),source.url,source));article.append(meta);if(badges.childElementCount)article.append(badges);article.append(title,summary,footer);
    article.onclick=event=>{if(event.target.closest('a,button,input,select')||window.getSelection?.().toString())return;open.focus({preventScroll:true});read(item);};return article;
  }
  function read(item){const id='market-history-item-'+state.date+'-'+encodeURIComponent(item.id);state.context.listposition=item.id;window.NavrenViews?.update({date:state.date,filters:{...(state.context.filters||{}),category:state.category},listposition:item.id,focustarget:id,scrollposition:document.querySelector('.page-scroll')?.scrollTop||0});window.DiscoverWorkspace?.recordMarketReading(item);MarketUI.openHistorical(item,{focusFallback:()=>document.getElementById(id)||document.getElementById('marketHistoryDateHeading')});}
  function display(data){
    if(data.edition_date!==state.date)throw Error(t('返回日期与所选日期不一致。'));if(!Array.isArray(data.events)||data.events.length>6||data.events.some(e=>e.archive?.edition_date!==state.date||Number(e.archive?.revision)!==Number(data.revision)))throw Error(t('精选清单格式异常，保留最近成功内容。'));state.day=data;ui.cards.replaceChildren();ui.cards.removeAttribute('aria-busy');ui.heading.textContent=format(state.date,true);ui.chooser.textContent=format(state.date)+' ▾';ui.count.textContent='';
    if(data.events.length){for(const item of data.events)ui.cards.append(card(item));return;}
    let message=state.index.status==='no_archives'&&!state.context.date?'近30天尚无归档。':data.status==='missing_archive'?'所选日尚未建立档案。':data.status==='no_matching_category'?'该类型没有匹配项。':'当日没有合格精选。';ui.cards.append(node('p',t(message),'market-history-empty'));
    if(data.status==='no_matching_category')ui.cards.append(control('显示全部类型',()=>{state.category='';state.context.filters={...(state.context.filters||{}),category:''};ui.category.value='';saveContext();load();},'market-history-text-action'));
  }
  async function load(){
    const token=++request;abort?.abort();abort=new AbortController();state.day=null;loading();
    try{const index=await MarketUI.get('hotspots/history'+(state.category?'?category='+encodeURIComponent(state.category):''),{signal:abort.signal});if(!active||token!==request)return;
      if(!validDate(index.window_start)||!validDate(index.window_end)||index.window_end!==index.server_today||(stamp(index.window_end)-stamp(index.window_start))/86400000!==29||!Array.isArray(index.dates))throw Error(t('历史日期窗口无法核验。'));state.index=index;
      if(!state.date){state.date=[...index.dates].reverse().find(d=>d.readable_count>0)?.date||index.default_date||index.server_today;state.month=state.date.slice(0,7);saveContext();}else if(!inWindow(state.date))throw Object.assign(Error(t('所选日期不在最近30天窗口内。')),{status:400});if(!state.month)state.month=state.date.slice(0,7);
      categorySelect();calendar();loading();const data=await MarketUI.get('hotspots/history/'+encodeURIComponent(state.date)+(state.category?'?category='+encodeURIComponent(state.category):''),{signal:abort.signal});if(!active||token!==request)return;display(data);
    }catch(error){if(!active||token!==request||error.name==='AbortError')return;ui.cards.removeAttribute('aria-busy');ui.count.textContent='';ui.cards.replaceChildren(node('p',error.status===400?t('所选日期或类型不可用。'):t('服务读取失败，请重试。'),'market-history-empty'));if(error.status!==400&&error.status!==404)ui.cards.append(control('重试',load));}
  }
  async function render(context={}){pause();active=true;state={context:{...context,filters:{...(context.filters||{})}},date:context.date||null,category:context.filters?.category==='all'?'':context.filters?.category||'',month:context.date?.slice(0,7)||null,index:null,day:null};const root=document.getElementById('discoverSubview');if(!root)return;shell(root);return load();}
  function language(){if(!active||!ui)return;ui.back.textContent=t('返回今日热点');ui.category.setAttribute('aria-label',t('筛选精选类型'));ui.calendar.setAttribute('aria-label',t('近30天精选日历'));if(state.index){categorySelect();calendar();}if(state.day)display(state.day);else loading();}
  document.addEventListener('click',event=>{if(active&&ui?.calendar.classList.contains('is-open')&&!ui.calendar.contains(event.target)&&!ui.chooser.contains(event.target))closeCalendar();});
  document.addEventListener('keydown',event=>{if(active&&event.key==='Escape'&&ui?.calendar.classList.contains('is-open')){event.preventDefault();closeCalendar();ui.chooser.focus({preventScroll:true});}});
  window.addEventListener('navren:language',language);window.addEventListener('pagehide',pause);
  for(const name of ['navren:privacy-reset','navren:identity-revoked','navren:account-switched'])window.addEventListener(name,clearIdentity);
  return {render,pause,chooseDate,current:()=>state?{date:state.date,category:state.category}:null,read,validDate};
})();

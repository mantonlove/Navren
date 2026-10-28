/* Public Market consumer contract: an archived six-card duplicate cannot become the default edition. */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {JSDOM}=require('jsdom');

const now=Date.parse('2026-10-28T10:15:00+08:00');
const article=day=>({
  id:'a-'+day,version:1,publisher:'新华社',url:'https://www.news.cn/20261027/verified-original-report.html',
  published_at:day+'T01:00:00Z',status:'active',link_status:'verified',
  read_scope:'full_text',access_status:'free_full_text'
});
const cards=(day,revision)=>Array.from({length:6},(_,i)=>({
  id:'e-'+day+'-'+i,version:1,status:'published',availability:'active',
  headline:'核验报道 '+i,summary:'原文摘要',category:'international',
  articles:[article(day)],archive:{edition_date:day,revision,event_version:1,primary_article_id:'a-'+day}
}));
const packet=(day,revision,distinct=6)=>({
  edition_date:day,revision,server_today:'2026-10-28',total_count:6,
  readable_count:6,distinct_event_count:distinct,events:cards(day,revision)
});
const index={
  server_today:'2026-10-28',window_start:'2026-09-29',window_end:'2026-10-28',
  default_date:'2026-10-27',default_revision:3,status:'ready',categories:[],
  dates:[
    {date:'2026-10-27',revision:4,total_count:6,readable_count:5,distinct_event_count:6,complete_revision:3,archive_state:'archived'},
    {date:'2026-10-28',revision:1,total_count:6,readable_count:6,distinct_event_count:5,complete_revision:null,archive_state:'archived'}
  ]
};

async function home(){
  const dom=new JSDOM('<body><div id="marketHotHeading"></div><div id="marketHot"></div></body>',
    {url:'https://navren.test/recommend.html',runScripts:'outside-only'});
  const w=dom.window,calls=[];w.Date.now=()=>now;
  w.Storage={accountReady:()=>true,get:key=>key==='qingzhou_preferences'?{marketHotVisible:true,weeklyReport:false}:[]};
  w.BankClient={identity:()=>({registered_user_id:'test',workspace_id:'test',privacy_revision:1})};
  w.NavrenAccountData={itemKey:JSON.stringify};w.NavrenViews={current:()=>({view:'home'})};
  w.NavrenHelp={button:()=>w.document.createElement('button')};w.showToast=()=>{};
  w.MarketDeck=class{constructor(root,options){this.root=root;this.render=options.renderCard;this.state='idle';this.update(options.events);}
    update(events){this.events=events;this.root.replaceChildren(...events.map(this.render));}
    destroy(){this.root.replaceChildren();}};
  w.fetch=async url=>{
    calls.push(url);
    const value=url==='/api/market/hotspots'?packet('2026-10-28',1,5)
      :url==='/api/market/hotspots/history'?index
      :url==='/api/market/hotspots/history/2026-10-27?revision=3'?packet('2026-10-27',3):null;
    assert(value,'unexpected Market request '+url);
    return {ok:true,headers:{get:()=>new Date(now).toUTCString()},json:async()=>structuredClone(value)};
  };
  w.eval(fs.readFileSync('js/market_ui.js','utf8'));
  await w.MarketUI.refreshHotspots();
  assert(calls.includes('/api/market/hotspots/history/2026-10-27?revision=3'));
  assert.equal(w.MarketUI.hotspot().data.edition_date,'2026-10-27');
  assert.equal(w.MarketUI.hotspot().data.revision,3);
  assert.equal(w.MarketUI.hotspot().data.events.length,6);
  assert(w.document.getElementById('marketHotHeading').textContent.includes('最近精选 · 2026-10-27'));
  dom.window.close();
}

async function history(){
  const dom=new JSDOM('<body><div id="discoverSubview"></div></body>',
    {url:'https://navren.test/recommend.html',runScripts:'outside-only'});
  const w=dom.window,calls=[];w.matchMedia=()=>({matches:false});
  w.NavrenViews={update:()=>{}};
  w.MarketUI={
    get:async path=>{
      calls.push(path);
      if(path==='hotspots/history')return index;
      if(path==='hotspots/history/2026-10-27?revision=3')return packet('2026-10-27',3);
      throw Error('unexpected Market request '+path);
    },
    theme:()=> 'international',displayCopy:(event,field)=>event[field==='title'?'headline':'summary'],
    readable:()=>true,sourceLink:(label,url)=>{const a=w.document.createElement('a');a.textContent=label;a.href=url;return a;}
  };
  w.eval(fs.readFileSync('js/market_history.js','utf8'));
  await w.MarketHistory.render({});
  assert(calls.includes('hotspots/history/2026-10-27?revision=3'));
  assert.equal(w.MarketHistory.current().date,'2026-10-27');
  assert.equal(w.document.querySelectorAll('.market-history-card').length,6);
  dom.window.close();
}

(async()=>{await home();await history();console.log('通过：重复六卡不进入首页，历史默认读取最近完整的原版次。');})()
  .catch(error=>{console.error(error);process.exitCode=1;});

/* The real MarketUI consumer must keep a six-card same-day edition when its
 * verified public source includes an approved 第一财经 article. No network or models. */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {JSDOM}=require('jsdom');

const dom=new JSDOM('<body><div id="marketHotHeading"></div><div id="marketHot"></div><div id="weeklyReport"></div></body>',{url:'https://navren.test/recommend.html',runScripts:'outside-only'});
const w=dom.window,now=Date.parse('2026-10-27T20:55:00+08:00');
const calls=[];
w.Date.now=()=>now;
w.setTimeout=()=>1;
w.clearTimeout=()=>{};
w.setInterval=()=>1;
w.NavrenHelp={button:()=>w.document.createElement('button')};
w.NavrenAccountData={itemKey:JSON.stringify};
w.BankClient={identity:()=>({registered_user_id:'fixture',workspace_id:'fixture',privacy_revision:1})};
w.Storage={accountReady:()=>true,get:key=>key==='qingzhou_preferences'?{marketHotVisible:true,weeklyReport:false}:[]};
w.NavrenViews={current:()=>({view:'home'})};
w.showToast=()=>{};
w.MarketDeck=class{constructor(root,options){this.root=root;this.render=options.renderCard;this.state='idle';this.update(options.events);}update(events){this.root.replaceChildren(...events.map(this.render));}destroy(){this.root.replaceChildren();}};

const article=(id,url)=>({id,version:1,publisher:id==='yicai'?'第一财经':'新华网',url,published_at:'2026-10-27T12:20:11+08:00',published_precision:'second',status:'active',read_scope:'full_text',link_status:'verified',access_status:'free_full_text'});
const official='https://www.news.cn/fortune/20261027/14c8c3e6bd60471ca01d7fe998cd84cf/c.html';
const yicai='https://www.yicai.com/news/103410851.html';
const cards=[0,1,2,3,4,5].map(i=>{const id=i===0?'yicai':'official-'+i,a=article(id,i===0?yicai:official);return {id,version:1,status:'published',availability:'active',headline:id,summary:'已核验摘要',primary_article_id:a.id,articles:[a],category:'investment',archive:{edition_date:'2026-10-27',revision:3,event_version:1}};});
const fixture={edition_date:'2026-10-27',revision:3,events:cards,is_fallback:true,display_mode:'retained'};
let packet={...fixture,events:[{...cards[0],articles:[{...cards[0].articles[0],url:official}]},...cards.slice(1)]};
w.fetch=async url=>{calls.push(url);const body=url==='/api/market/hotspots'?packet:url==='/api/market/hotspots/history'?{dates:[]}:null;if(body===null)throw Error('unexpected API '+url);return {ok:true,headers:{get:()=>new Date(now).toUTCString()},json:async()=>structuredClone(body)};};
const pageReady=new Promise(resolve=>{if(w.document.readyState==='loading')w.document.addEventListener('DOMContentLoaded',resolve,{once:true});else resolve();});
w.eval(fs.readFileSync(process.env.MARKET_UI_SOURCE||'js/market_ui.js','utf8'));

(async()=>{
 await pageReady;
 await w.MarketUI.recommend();
 assert.equal(w.MarketUI.hotspot().data?.events?.length,6,'页面初始化须先完成');
 calls.length=0;
 if(process.env.MARKET_PACKET_PATH){
  packet=JSON.parse(fs.readFileSync(process.env.MARKET_PACKET_PATH,'utf8'));
  assert.equal(packet.edition_date,'2026-10-27');
  assert.equal(packet.events.length,6);
  await w.MarketUI.refreshHotspots();
  assert.equal(w.MarketUI.hotspot().data?.events?.length,6,'实际公开同日包应通过六卡前端筛选；请求：'+calls.join(','));
  assert(!calls.includes('/api/market/hotspots/history'),'实际公开同日包不得转查旧版');
  packet=fixture;
 }
 else packet=fixture;
 await w.MarketUI.refreshHotspots();
 assert.equal(w.MarketUI.hotspot().data.events.length,6,'同日有效六卡必须完整保留');
 assert(w.document.getElementById('marketHotHeading').textContent.includes('当日精选 · 2026-10-27'));
 assert(!calls.includes('/api/market/hotspots/history'),'同日六卡不得转查旧版');

 const invalid=[
  'http://www.yicai.com/news/103410851.html',
  'https://www.yicai.com.evil/news/103410851.html',
  'https://user:pass@www.yicai.com/news/103410851.html',
  'https://www.yicai.com:8443/news/103410851.html',
  'https://www.yicai.com/other/103410851.html',
  'https://www.yicai.com/news/103410851.html?redirect=1',
  'https://www.yicai.com/news/103410851.html#other'
 ];
 for(const url of invalid){
  packet={...packet,events:[{...cards[0],articles:[{...cards[0].articles[0],url}]},...cards.slice(1)]};
  await w.MarketUI.refreshHotspots();
  assert.equal(w.MarketUI.hotspot().data.events.length,5,url);
 }
 for(const mutation of [{status:'withdrawn'},{link_status:'unverified'},{access_status:'subscription_required'}]){
  packet={...packet,events:[{...cards[0],articles:[{...cards[0].articles[0],...mutation}]},...cards.slice(1)]};
  await w.MarketUI.refreshHotspots();
  assert.equal(w.MarketUI.hotspot().data.events.length,5,JSON.stringify(mutation));
 }
 for(const invalid of [
  {...cards[0],articles:[{...cards[0].articles[0],published_at:'2026-10-26T12:20:11+08:00'}]},
  {...cards[0],archive:{...cards[0].archive,revision:2}}
 ]){
  packet={...packet,events:[invalid,...cards.slice(1)]};
  await w.MarketUI.refreshHotspots();
  assert.equal(w.MarketUI.hotspot().data.events.length,5,'原文首发日或归档版次不符仍须拒绝');
 }
 console.log('PASS verified Yicai current six; PASS seven exact-link, three source-state, two date/revision boundaries');
 dom.window.close();
})().catch(error=>{console.error(error);dom.window.close();process.exitCode=1;});

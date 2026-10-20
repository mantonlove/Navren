/* 全产品研究榜与精确只读详情的禁网DOM检查；文件名保留历史约定。 */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),{JSDOM}=require('jsdom');
const dom=new JSDOM('<!doctype html><span class="header-location"></span><h1 id="discoverHeading"></h1><p id="discoverIntro"></p><main id="discoverHome"><div id="productPreview"></div><details class="discover-research-help"><p></p></details></main><div id="discoverSubview" hidden></div>',{url:'https://fixture.test/recommend.html',runScripts:'outside-only',pretendToBeVisual:true}),w=dom.window,d=w.document;
const clone=value=>JSON.parse(JSON.stringify(value));let assertions=0;
function check(value,label){assert.ok(value,label);assertions++;}
function equal(actual,expected,label){assert.deepEqual(actual,expected,label);assertions++;}
const calls=[],writes=[],networkAttempts=[],amountActions=[],references=[],asks=[],navigations=[];
let identity={workspace_id:'research-fixture-a',account_id:'research-subject-a',privacy_revision:42,as_of:'2026-10-09'},identityEpoch=0,nextResearchDelay=null,catalogueDelay=null,failResearch=false,detailOverride=null;
const NOW='2026-10-09T10:00:00+08:00',VERSION='a'.repeat(64),CATALOG='c'.repeat(64),RANK_URL='https://fund.eastmoney.com/data/fundranking.html';
const OriginalDate=w.Date;w.Date=class extends OriginalDate{constructor(...args){super(...(args.length?args:[NOW]));}static now(){return OriginalDate.parse(NOW);}};
const style=d.createElement('style');style.textContent=':root{--danger:rgb(181,72,66);--success:rgb(35,120,91);--ink:rgb(35,35,35)}\n'+fs.readFileSync(path.resolve(__dirname,'../../css/discover_workspace.css'),'utf8');d.head.append(style);
function row(code,name,value,type='股票基金',risk='R5',extra={}){return {code,name,type,risk,manager:'研究池目录管理人',source_scope:'public_product_research',executable:false,content_version:VERSION,catalog_version:CATALOG,source_url:'https://fund.eastmoney.com/'+code+'.html',rank_source_url:RANK_URL,as_of:'2026-10-08',stale:false,metric:value==null?null:{type:'one_week_return',label:'近1周涨跌幅',value_pct:value,as_of:'2026-10-08'},raw:{source_name:'来源简称'+code,unit_nav:'1.1000',cumulative_nav:'1.2000',source_period:'近1周',catalog_source_date:'2026-10-08',missing_reason:null},...extra};}
const baseline=[row('000005','目录股票基金B','5.900','股票基金','R5'),row('000004','目录货币基金B','0.000','货币基金','R1'),row('000006','目录债券基金C','-0.200','债券基金','R2')];
function ranking(products=baseline,extra={}){return {status:'ok',metric_type:'one_week_return',source_scope:'public_product_research',executable:false,ranking_complete:true,content_version:crypto.createHash('sha256').update(JSON.stringify(products)).digest('hex'),catalog_version:CATALOG,catalog_source_date:'2026-10-08',fetched_at:NOW,coverage:{candidate_count:8,type_counts:{股票基金:2,债券基金:2,货币基金:1,混合基金:1,QDII:1,保险:1},source_count:10,matched_count:8,eligible_count:8},provider:'天天基金／东方财富Choice公开数据',rank_source_url:RANK_URL,stale_after_days:7,products:clone(products),...extra};}
let board=ranking();const detailRows=new Map(baseline.map(p=>[p.code,clone(p)]));
const legacyMoney={status:'ok',metric_type:'seven_day_annualized',source_scope:'official_money_market',executable:false,content_version:'e'.repeat(64),coverage:[{name:'华夏基金管理有限公司',status:'ok',count:1,manager:'chinaamc',source_url:'https://www.chinaamc.com/'}],fetched_at:NOW,products:[{code:'000001',name:'华夏历史货币A',type:'货币基金',manager:'华夏基金管理有限公司',risk:'R1',source_scope:'official_money_market',executable:false,content_version:'e'.repeat(64),source_url:'https://www.chinaamc.com/',as_of:'2026-10-08',stale:false,metric:{type:'seven_day_annualized',value_pct:'1.500',as_of:'2026-10-08'}}]};
const legacyBank=[{id:'SIM-OLD-FUND',name:'旧模拟高收益基金',source:'simulation',version:1,risk_level:1,term_days:0,min_purchase_minor:10000,redemption_delay_days:0,profile_eligible:true}];
const legacyPublic={catalog_version:'legacy-public-fixture',source_date:'2022-07-14',stale:true,matched:1,offset:0,limit:500,has_more:false,products:[{code:'999999',name:'旧公开高涨幅基金',type:'公募基金',publish:'旧目录',risk:'R1',annual_return:'999.000',source_url:'https://www.webank.com/'}]};
const head={catalog_version:CATALOG,source_date:'2026-10-08',fetch_status:'ok',catalog_sync:{fetch_status:'ok',checked_at:NOW,source_date:'2026-10-08',background_refresh:false},total:8,matched:8,limit:1,offset:0,products:[clone(legacyPublic.products[0])],has_more:true};
w.BankClient={ensure:async()=>({...identity}),identity:()=>({...identity}),guard(){const before={...identity},epoch=identityEpoch;return {valid(response){return epoch===identityEpoch&&before.workspace_id===identity.workspace_id&&before.privacy_revision===identity.privacy_revision&&(!response||!response.workspace_id||response.workspace_id===before.workspace_id);}};},money:value=>(value/100).toFixed(2)+' 元',get:async request=>{
  calls.push(request);if(request==='/public-products?limit=1&offset=0')return clone(head);if(request==='/products'){if(catalogueDelay)await catalogueDelay;return clone(legacyBank);}if(request==='/public-products?limit=500&offset=0'){if(catalogueDelay)await catalogueDelay;return clone(legacyPublic);}if(request==='/public-money-products')return clone(legacyMoney);
  const url=new URL(request,'https://fixture.test');assert.equal(url.pathname,'/public-research-products','榜单及详情只读取新的研究接口');checkQuery(url.searchParams);if(failResearch)throw Error('模拟研究榜读取失败');const code=url.searchParams.get('code');
  const response=code?(detailOverride?clone(detailOverride):ranking(detailRows.has(code)?[detailRows.get(code)]:[])):clone(board);
  const pause=nextResearchDelay;nextResearchDelay=null;if(pause)await pause;return response;
}};
function checkQuery(query){assert.ok([...query.keys()].every(key=>key==='code'),'研究接口只允许精确code参数');if(query.has('code'))assert.match(query.get('code'),/^\d{6}$/,'详情使用六位份额代码');}
for(const method of ['post','put','patch','delete'])w.BankClient[method]=(...args)=>{writes.push({method,args});throw Error('只读研究不得提交银行操作');};
w.fetch=(...args)=>{networkAttempts.push(args);throw Error('隔离测试禁止真实网络');};
w.BankUI={amountForm:(...args)=>amountActions.push(args)};w.Storage={get:()=>null,set:()=>{throw Error('研究浏览不得保存金融操作');}};
w.MarketUI={recommend:async()=>{},ask:()=>{}};w.MarketHistory={pause:()=>{}};
w.NavrenViews={register:()=>{},open:(view,metadata)=>{navigations.push({view,...clone(metadata)});return w.DiscoverWorkspace.render({view,...metadata,...identity});},back:()=>{},reference:ref=>references.push(ref),ask:(...args)=>asks.push(args)};
w.eval(fs.readFileSync(path.resolve(__dirname,'../../js/discover_workspace.js'),'utf8'));
const UI=w.DiscoverWorkspace,target=d.getElementById('productPreview'),subview=d.getElementById('discoverSubview');
const cards=()=>[...target.querySelectorAll('.discover-preview')],codes=()=>cards().map(card=>new URL(card.querySelector('.discover-preview-link').href).searchParams.get('object_id'));
const values=()=>cards().map(card=>Number(card.querySelector('.discover-performance-value').textContent.replace('%','')));
// 此jsdom版本保留CSS变量原文；用同一DOM的变量值完成最终颜色核验。
function color(node){const computed=w.getComputedStyle(node).color,variable=/^var\((--[\w-]+)(?:,([^)]*))?\)$/.exec(computed);return(variable?w.getComputedStyle(d.documentElement).getPropertyValue(variable[1])||variable[2]:computed).replace(/\s+/g,'');}
const settle=async()=>{for(let i=0;i<6;i++)await new Promise(resolve=>setTimeout(resolve,0));};
async function visibleStable(root,ready,label){const deadline=Date.now()+3000;let previous=null,stable=0;while(Date.now()<deadline){const html=root.innerHTML;if(ready()&&html===previous)stable++;else stable=0;if(stable>=2)return;previous=html;await new Promise(resolve=>setTimeout(resolve,0));}throw Error('可见结果未完成或未稳定：'+label+'；'+root.textContent);}
const previewReady=()=>!target.textContent.includes('正在读取')&&!!(target.querySelector('.discover-preview,.discover-empty,.discover-error')||target.textContent.includes('账户权限已变化'));
async function home(force=false){await(force?UI.refresh(true):UI.render({view:'home',...identity}));await visibleStable(target,previewReady,'全产品研究榜');}
async function detail(code,version=VERSION,scope='public_product_research'){await UI.render({view:'product',object_kind:'product',object_id:code,object_version:version,source_scope:scope,...identity});await visibleStable(subview,()=>!subview.textContent.includes('正在读取产品资料')&&!!subview.querySelector('.discover-detail,.discover-error'),'精确产品详情');}
const readOnly=()=>!subview.querySelector('input,form')&&!/准备申购|填写申购金额|加入比较|引用到提问/.test(subview.textContent);

(async()=>{
  await home();equal(calls.filter(request=>request==='/public-research-products').length,1,'首页以无参GET读取后端完整排序的前三项');
  equal(codes(),['000005','000004','000006'],'首页保留已核验的近1周收益降序前三');
  equal(cards().map(card=>card.querySelector('h3').textContent),baseline.map(p=>p.name),'显示目录名称，代码与名称不串成来源简称');
  equal(values(),[5.9,0,-0.2],'正收益、真实零收益及负收益保留原值和排序');
  check(cards().every(card=>card.textContent.includes('近1周')&&!card.textContent.includes('年化')&&card.textContent.includes('2026-10-08')),'卡片明确近1周口径及原披露日期，不冒充年化');
  check(!target.textContent.includes('旧模拟')&&!target.textContent.includes('旧公开')&&!target.textContent.includes('华夏历史货币'),'首页不回退旧目录或货币榜');equal(calls.filter(request=>request==='/public-money-products').length,0,'新首页不读取旧货币研究池');
  check(!target.querySelector('input,button,svg,canvas'),'研究榜只提供资料链接，不创建金融操作或收益图');
  for(const card of cards()){
    const code=new URL(card.querySelector('.discover-preview-link').href).searchParams.get('object_id'),p=baseline.find(p=>p.code===code),links=[...card.querySelectorAll('a')];
    const source=links.find(link=>link.href===p.source_url),route=links.find(link=>new URL(link.href).searchParams.get('view')==='product');
    check(source&&source.target==='_blank'&&source.rel.includes('noopener')&&source.rel.includes('noreferrer'),'资料来源链接安全且对应精确份额：'+code);
    equal(Object.fromEntries(new URL(route.href).searchParams),{view:'product',object_kind:'product',object_id:code,source_scope:'public_product_research',object_version:VERSION},'详情链接保留研究来源、份额代码和内容版本：'+code);
    equal(color(card.querySelector('.discover-performance-value')),'rgb(181,72,66)','正负零收益数字统一显示红色：'+code);
  }
  cards()[0].querySelector('.discover-preview-link').click();await visibleStable(subview,()=>!!subview.querySelector('.discover-detail'),'卡片详情打开');
  check(calls.includes('/public-research-products?code=000005'),'详情使用精确code独立读取接口');equal(navigations.at(-1).source_scope,'public_product_research','卡片跳转沿用新的只读来源');
  equal(subview.querySelector('.discover-detail h2').textContent,baseline[0].name,'详情对应所选目录份额名称');check(!subview.textContent.includes('正在读取产品资料'),'成功详情移除加载提示');
  check(subview.textContent.includes('000005')&&subview.textContent.includes('股票基金'),'详情披露精确份额代码和原目录类型');check([...subview.querySelectorAll('a')].some(link=>link.href===baseline[0].source_url),'详情来源链接与所选份额一致');check(readOnly(),'新来源详情无金融操作、比较或引用入口');
  equal(color(subview.querySelector('.discover-performance-value')),'rgb(181,72,66)','详情收益数字同样显示红色');
  await detail('000004');equal(subview.querySelector('.discover-detail h2').textContent,baseline[1].name,'直接详情按code选取零收益货币份额');equal(Number(subview.querySelector('.discover-performance-value').textContent.replace('%','')),0,'精确详情保留真实零收益');
  await detail('000006','b'.repeat(64));check(subview.textContent.includes('更新')||subview.textContent.includes('新披露'),'版本变化明确显示更新提示');
  await detail('000099');check(!subview.querySelector('.discover-detail')&&/未找到|不存在|没有找到/.test(subview.textContent),'不存在的code不替换成其他产品');
  detailOverride=ranking([baseline[1]]);await detail('000005');check(!subview.querySelector('.discover-detail'),'精确code响应返回另一份额时停止展示');detailOverride=null;
  detailOverride=ranking([baseline[0],baseline[1]]);await detail('000005');check(!subview.querySelector('.discover-detail'),'精确详情返回超过一项时停止展示');detailOverride=null;
  const missing=row('000007','目录缺失周收益产品',null,'银行理财',null,{source_url:null,raw:{source_name:null,unit_nav:null,cumulative_nav:null,source_period:'近1周',catalog_source_date:'2026-10-08',catalog_source_url:'https://www.webank.com/',missing_reason:'近1周涨跌幅未披露'}});detailRows.set(missing.code,missing);
  await detail('000007');check(subview.querySelector('.discover-detail')&&!subview.textContent.includes('0.00%')&&!subview.textContent.includes('0.000%'),'合法缺失指标详情显示缺项，不伪造零收益');check(![...subview.querySelectorAll('a')].some(link=>link.href==='https://fund.eastmoney.com/000007.html'),'缺失收益的银行理财不伪造基金来源链接');check(readOnly(),'缺失指标详情保持只读');
  const old=row('000008','目录过期周收益产品','88.000','债券基金','R2',{as_of:'2026-09-01',stale:true,metric:{type:'one_week_return',label:'近1周涨跌幅',value_pct:'88.000',as_of:'2026-09-01'}});detailRows.set(old.code,old);
  await detail('000008');check(subview.querySelector('.discover-detail')&&/待更新|过期/.test(subview.textContent),'合法过期资料可精确查看并明确状态');check(readOnly(),'过期指标详情保持只读');
  await detail('000001','e'.repeat(64),'official_money_market');equal(subview.querySelector('.discover-detail h2').textContent,'华夏历史货币A','旧来源仅保留精确只读详情');check(readOnly(),'旧货币详情也不创建金融操作');

  await home();const stableCard=cards()[0];let releaseRefresh;nextResearchDelay=new Promise(resolve=>releaseRefresh=resolve);let homeFinished=false;
  const waitingHome=UI.refresh(true).then(()=>homeFinished=true);await settle();equal(codes(),baseline.map(p=>p.code),'成功资料重新校对等待期间保留上次榜单');check(!homeFinished&&cards()[0]===stableCard,'等待新响应时不清空或重建成功卡片');releaseRefresh();await waitingHome;await visibleStable(target,previewReady,'完成同版本校对');check(cards()[0]===stableCard,'同内容版本校对成功保持原DOM节点');
  for(const [index,[type,risk]] of [['股票基金','R5'],['混合基金','R4'],['债券基金','R2'],['货币基金','R1'],['QDII',null],['指数基金','R3'],['保险',null],['银行理财','R5']].entries()){
    const p=row('0001'+String(index).padStart(2,'0'),'目录'+type+'研究份额','1.000',type,risk,{manager:null});board=ranking([p]);board.coverage.eligible_count=1;await home(true);equal(codes(),[p.code],'相同周收益合同不按类型或风险排除：'+type+'/'+risk);
  }
  board=ranking(baseline.slice(0,2));board.coverage.eligible_count=2;await home(true);equal(cards().length,2,'有效指标不足三项时只呈现两项');check(!target.textContent.includes('旧模拟')&&!target.textContent.includes('华夏历史货币'),'不足三项不以其他来源补齐');
  board=ranking([]);board.coverage.eligible_count=0;await home(true);equal(cards().length,0,'无有效指标时不生成占位卡');check(/暂无|未获取|获取失败/.test(target.textContent),'无有效指标明确呈现缺项');
  const bad=[
    ['排名未完整',b=>b.ranking_complete=false],['缺少完整排名证明',b=>delete b.ranking_complete],['前三数量缺失',b=>b.products.pop()],['返回超过前三',b=>b.products.push(row('000009','额外份额','-1.000'))],
    ['收益顺序错误',b=>b.products.reverse()],['重复份额代码',b=>b.products[1].code=b.products[0].code],['顶层非周收益',b=>b.metric_type='seven_day_annualized'],['一天收益混入',b=>b.products[0].metric.type='one_day_return'],['一年收益混入',b=>b.products[0].metric.type='one_year_return'],
    ['指标标签错期',b=>b.products[0].metric.label='近1年涨跌幅'],['来源期间错期',b=>b.products[0].raw.source_period='近1年'],['非字符串收益',b=>b.products[0].metric.value_pct=5.9],['缺失指标混入榜单',b=>b.products[0].metric=null],
    ['过期状态混入榜单',b=>b.products[0].stale=true],['过期日期未标记',b=>{b.products[0].as_of='2026-09-01';b.products[0].metric.as_of='2026-09-01';}],['指标日期不一致',b=>b.products[0].metric.as_of='2026-10-07'],
    ['来源代码串份额',b=>b.products[0].source_url='https://fund.eastmoney.com/000004.html'],['来源域名错误',b=>b.products[0].source_url='https://example.com/000005.html'],['排名来源错误',b=>b.rank_source_url='https://example.com/ranking'],
    ['份额来源身份串用',b=>b.products[0].source_scope='official_money_market'],['目录版本串用',b=>b.products[0].catalog_version='d'.repeat(64)],['内容版本无效',b=>b.content_version='not-a-hash'],['份额内容版本无效',b=>b.products[0].content_version='not-a-hash'],['可执行产品混入',b=>b.products[0].executable=true]
  ];
  const contractFailures=[];
  for(const [label,mutate] of bad){board=ranking();mutate(board);target.replaceChildren();delete target.dataset.researchVersion;await home(true);try{check(!cards().length,'损坏合同不能发布新榜：'+label);}catch(error){contractFailures.push(error.message);}}
  board=ranking([], {status:'unavailable',ranking_complete:false});board.coverage.eligible_count=0;await home(true);check(!cards().length,'来源不可用时不保留上一次榜单');
  board=ranking();await home(true);const beforeFailed=cards()[0];failResearch=true;await home(true);equal(codes(),baseline.map(p=>p.code),'接口读取失败明确沿用上次已核验榜单');check(cards()[0]===beforeFailed&&/沿用上次/.test(target.parentElement.querySelector('.discover-sync-warning')?.textContent||''),'失败保留原卡片并标注沿用，不能当作完整新榜');failResearch=false;

  // 新增回归场景：官网目录已经换版，旧版本研究榜不能继续被当作当前候选。
  equal(target.dataset.catalogVersion,CATALOG,'成功榜单记录实际匹配的官网目录版本');
  head.catalog_version='d'.repeat(64);failResearch=true;await home(true);
  equal(cards().length,0,'官网换版且新收益读取失败时清除旧目录版本榜单');
  check(!beforeFailed.isConnected&&!target.textContent.includes(baseline[0].name),'跨目录版本失败不遗留旧产品卡或名称');
  check(!/沿用上次/.test(target.parentElement.querySelector('.discover-sync-warning')?.textContent||''),'跨目录版本失败不声称旧收益资料仍适用于当前目录');
  head.catalog_version=CATALOG;failResearch=false;await home(true);

  let release;const beforePendingReads=calls.filter(request=>request==='/public-research-products').length;nextResearchDelay=new Promise(resolve=>release=resolve);const pending=home(true);await settle();check(calls.filter(request=>request==='/public-research-products').length===beforePendingReads+1,'迟到响应场景确已进入研究接口读取');
  identityEpoch++;identity={...identity,workspace_id:'research-fixture-b',account_id:'research-subject-b',privacy_revision:43};w.dispatchEvent(new w.Event('navren:privacy-reset'));release();await pending;await settle();check(!cards().length&&target.textContent.includes('账户权限已变化'),'隐私失效后的迟到榜单不能恢复产品');check(!target.textContent.includes(baseline[0].name)&&!target.textContent.includes('5.90'),'隐私失效后旧份额与收益保持隐藏');
  board=ranking([row('000013','目录新账户研究产品','1.500')]);board.coverage.eligible_count=1;await home();equal(codes(),['000013'],'新身份重新读取榜单而不复用旧身份缓存');const researchReads=calls.filter(request=>request==='/public-research-products').length,headReads=calls.filter(request=>request==='/public-products?limit=1&offset=0').length;await home();equal(calls.filter(request=>request==='/public-research-products').length,researchReads+1,'同身份再次校对不受旧十分钟研究缓存遮挡');equal(calls.filter(request=>request==='/public-products?limit=1&offset=0').length,headReads+1,'每轮校对先取得最新官网head');
  equal(calls.filter(request=>request==='/public-money-products').length,1,'旧货币池只因明确旧来源详情被读取一次，首页始终不回退');check(calls.every(request=>['/products','/public-products?limit=500&offset=0','/public-products?limit=1&offset=0','/public-money-products'].includes(request)||/^\/public-research-products(?:\?code=\d{6})?$/.test(request)),'全部场景只读取目录和只读研究接口');
  equal(writes.length,0,'全部场景零银行写入');equal(amountActions.length,0,'全部场景零金额准备动作');equal(references.length,0,'只读详情零引用动作');equal(asks.length,0,'浏览研究榜零模型提问');equal(networkAttempts.length,0,'全部场景零真实网络请求');
  equal(contractFailures,[],'所有损坏的榜单合同均应拒绝展示');
  console.log(JSON.stringify({passed:true,assertions,reads:calls.length,research_reads:calls.filter(request=>request.startsWith('/public-research-products')).length,writes:writes.length,financial_actions:amountActions.length,network_requests:networkAttempts.length,evidence:'全产品研究榜完整排名合同、混合类型与风险、周收益口径、精确只读详情及隐私迟到响应的禁网DOM假响应'}));dom.window.close();
})().catch(error=>{console.error(error);dom.window.close();process.exitCode=1;});

/* 关键证据绑定和手势边界：node tests/test_market_deck.js。 */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const context={window:{addEventListener(){}},URL,performance:{now:()=>1000},clearTimeout, getSelection:()=>({toString:()=>'',removeAllRanges(){}}),document:{body:{classList:{contains:()=>false}}}};
vm.createContext(context);vm.runInContext(fs.readFileSync('js/market_ui.js','utf8'),context);vm.runInContext(fs.readFileSync('js/market_deck.js','utf8'),context);
const event={id:'e-1',version:2,content_version:'source-2',facts:[{article_id:'a-1'}],articles:[{id:'a-1',version:3}]};
const display={id:'e-1',event_version:2,content_version:'source-2',article_versions:{'a-1':3},display_title:'已审核短标题',display_summary:'已审核短说明',topic_id:'macro',display_fact:{label:'PMI',value:'50.1',unit:'%',period:'2026年9月',evidence_refs:[{article_id:'a-1',fact_index:0}]}};
const bind=(change={},review='approved')=>context.window.MarketUI.bindDisplay(event,{review:{status:review},events:[{...display,...change}]});
assert.equal(bind().display_title,display.display_title);
for(const change of [{id:'other'},{event_version:1},{content_version:'old'},{article_versions:{'a-1':2}},{topic_id:'institution-guessed'},{display_fact:{...display.display_fact,unit:''}},{display_fact:{...display.display_fact,evidence_refs:[{article_id:'other',fact_index:0}]}}])assert.equal(bind(change),event,'失配短版必须回退原事件');
assert.equal(bind({},'rejected'),event);
const Deck=context.window.MarketDeck,prototype=Deck.prototype;
assert.equal(Deck.relative(5,0,6),-1,'首条左边必须是末条');
assert.equal(Deck.relative(0,5,6),1,'末条右边必须是首条');
assert.equal(Deck.relative(1,0,2),1,'两条不循环制造副本');
for(const count of [3,4,6]){
  for(let id=0;id<count;id++){
    let previous=null;
    for(let j=0;j<=1000;j++){
      const r=Deck.relative(id,j/1000,count),pose=Deck.pose(r,count,{spacing:300});
      if(previous&&Math.abs(r-previous.r)>1)assert.ok(pose.opacity<.01&&previous.opacity<.01,'循环重定位只在透明区');
      previous={r,opacity:pose.opacity};
    }
  }
}
const a=Deck.pose(-.5,6,{spacing:300}),b=Deck.pose(.5,6,{spacing:300});
assert.equal(a.x,-150);assert.equal(b.x,150);assert.ok(a.scale<1&&b.scale<1,'半程必须共同缩放');assert.ok(a.angle>0&&b.angle<0,'半程共同向内转向');
assert.equal(Deck.layers(.5).preview,0,'半程不叠出两组标题');assert.equal(Deck.layers(1).full,0,'静止侧卡只显示简版');
// 实际刷新帧会跨过取模边界，不能只验证无限细的数学采样。
const style=()=>({setProperty(){}}),classes={toggle(){}},flow=Object.create(prototype);
flow.events=[{id:'a'},{id:'b'},{id:'c'}];flow.cards=new Map(flow.events.map(e=>[e.id,{node:{style:style(),dataset:{},classList:classes,setAttribute(k,v){this[k]=v;}},full:{setAttribute(){}},select:{},card:{classList:classes}}]));
flow.root={dataset:{},classList:classes};flow.spacing=300;flow.motion={matches:false};flow.index=0;flow.state='dragging';flow.direction=1;flow.queueDraw=()=>{};
flow.virtualPosition=.4;flow.draw();const c=flow.cards.get('c'),container=c.node;assert.ok(Number(c.node.style.opacity)>.1);
flow.virtualPosition=.6;flow.draw();assert.equal(c.node.style.opacity,0,'跨分支的实际帧必须完全透明');assert.equal(c.node.inert,true);assert.equal(c.node['aria-hidden'],true);assert.equal(c.node.dataset.renderedRelative,'1.5000','透明边界再进入');
context.performance.now=()=>1020;flow.draw();assert.equal(c.node.style.opacity,0,'保持透明而不是同帧闪回');
context.performance.now=()=>1100;flow.draw();assert.ok(Number(c.node.style.opacity)>.1);assert.equal(c.node,container,'原事件容器不变');
flow.draw(true);assert.equal(c.node.dataset.wrapHidden,false,'取消/提交清除换边临时状态');
const reduced=Deck.pose(1,6,{spacing:300,reduced:true});assert.equal(reduced.angle,0);assert.equal(reduced.z,0);assert.equal(reduced.x,300,'减少动态仍保留位置关系');
function gesture(){const g=Object.create(prototype);g.state='dragging';g.index=1;g.events=[{},{},{}];g.spacing=300;g.pointer={id:1,x:300,y:10,position:1,samples:[{x:300,t:0}]};g.stage={hasPointerCapture:()=>false};g.settle=t=>{g.result=t;};return g;}
let g=gesture();g.up({pointerId:1,clientX:195,timeStamp:150});assert.equal(g.result,2,'左划下一条');
g=gesture();g.up({pointerId:1,clientX:405,timeStamp:150});assert.equal(g.result,0,'右划上一条');
g=gesture();g.up({pointerId:1,clientX:250,timeStamp:1});assert.equal(g.result,1,'极短采样不能产生假速度');
g=gesture();g.up({pointerId:1,clientX:250,timeStamp:80});assert.equal(g.result,2,'可靠采样下快速划动');
g=gesture();g.up({pointerId:1,clientX:280,timeStamp:80});assert.equal(g.result,1,'不足30px不能靠速度提交');
g=gesture();g.events=[{},{}];g.index=g.pointer.position=1;g.up({pointerId:1,clientX:100,timeStamp:150});assert.equal(g.result,1,'两条末尾只能回弹');
g=gesture();g.state='pending';g.reset=()=>{g.state='idle';g.cancelled=true;};g.move({pointerId:1,clientX:304,clientY:40,timeStamp:50});assert.equal(g.cancelled,true,'纵向趋势交还页面');
g=gesture();assert.equal(g.goTo('removed'),false,'失效对象不能定位到其他事件');
console.log('通过：版本证据绑定、3/4/6条透明区循环、半程共同换位、减少动态、阈值与两条边界。');

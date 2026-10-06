/* 纯内存 CSS 合同验证：不请求网站，不以 JSDOM 推断实屏排版。 */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {JSDOM}=require('jsdom');
const base=path.resolve(__dirname,'../..');
const read=file=>fs.readFileSync(path.join(base,file),'utf8');
const dom=new JSDOM(`<!doctype html><html><head></head><body data-page="mine" class="mode-classic">
<main class="mine-workspace"><div class="finance-graph-stage">
  <div class="fg-view"><div class="fg-canvas"><svg class="fg-svg"><g class="fg-node">
    <circle r="16" fill="#417cb3"/><text font-size="16">账户名称</text><text font-size="11">CNY 10,000.00</text>
  </g><g class="fg-edge"><text font-size="11">关系</text></g></svg></div>
  <p class="fg-legend">金额、币种与期间图例</p><p class="fg-key">Solid / dashed / dotted connections</p>
  </div><aside class="fg-inspector"><h3>记录与依据</h3><p>金额：未知</p><blockquote>本人原话</blockquote>
    <details class="fg-evidence"><summary>依据</summary><p>2026-10-06 · 来源记录</p><a href="#source">Read the original record and its source</a></details>
  </aside></div><div class="fg-controls"><label>Source / 来源<select><option>Imported statement</option></select></label></div>
  <button class="fg-index-button">Read the evidence</button><button class="fg-path">按路径阅读关系</button>
  <div class="fg-insights"><article class="fg-insight"><h3>洞察</h3><strong>未知</strong><button>问 Navren</button></article></div>
  <div class="fg-timeline"><p>资金时间轴</p></div><p class="fg-consent">授权说明</p>
</main><dialog class="fg-dialog"><h2>核对资料</h2><p>不是资金回执</p><button>确认</button></dialog>
</body></html>`,{url:'https://navren.invalid/mine.html',runScripts:'outside-only'});
const w=dom.window,d=w.document;
let checks=0;
function check(name,fn){fn();checks++;}
for(const file of ['css/design.css','css/mine_workspace.css']){
  const style=d.createElement('style');style.textContent=read(file);d.head.append(style);
}
w.eval(read('js/storage.js'));
// JSDOM 保留 var()，这里只核对令牌绑定及实际字号配置，不计算像素布局。
function resolve(value){
  return value.replace(/var\((--[\w-]+)(?:,\s*([^)]*))?\)/g,(_match,key,fallback)=>
    d.documentElement.style.getPropertyValue(key).trim()||w.getComputedStyle(d.documentElement).getPropertyValue(key).trim()||fallback||'');
}
function font(selector){return resolve(w.getComputedStyle(d.querySelector(selector)).fontSize);}
function property(selector,name){return w.getComputedStyle(d.querySelector(selector)).getPropertyValue(name).replace(/\s+/g,'');}
function mediaRule(query,selector,propertyName,expected){
  for(const sheet of d.styleSheets)for(const rule of sheet.cssRules){
    if(!rule.media||rule.conditionText.replace(/\s+/g,'')!==query)continue;
    for(const child of rule.cssRules)if(child.selectorText===selector&&child.style.getPropertyValue(propertyName).replace(/\s+/g,'')===expected)return true;
  }
  return false;
}
try{
  for(const [size,baseSize,secondarySize]of [['small',15,14],['medium',16,15],['large',20,19]]){
    w.applyFontSize(size);
    check(`${size} 图例与依据跟随字号`,()=>{
      for(const selector of ['.fg-legend','.fg-key','.fg-evidence','.fg-evidence p','.fg-evidence a','.fg-edge text','.fg-node text:last-of-type','.fg-consent'])assert.equal(font(selector),secondarySize+'px',selector);
    });
    check(`${size} 详情和节点名称保留正文字号`,()=>{
      for(const selector of ['.fg-inspector > p','.fg-inspector blockquote','.fg-dialog p','.fg-timeline p','.fg-node text:first-of-type','.fg-index-button','.fg-path','.fg-insight button'])assert.equal(font(selector),baseSize+'px',selector);
    });
  }
  d.body.className='mode-senior';w.applyFontSize('large');
  check('关怀版详情不挤在窄侧栏',()=>assert.equal(property('.finance-graph-stage','grid-template-columns'),'minmax(0,1fr)'));
  check('关怀版动作保留较大触摸目标',()=>assert.ok(parseFloat(property('.fg-index-button','min-height'))>=48));
  check('小屏图谱上下布局合同存在',()=>assert.ok(mediaRule('(max-width:1000px)','body[data-page="mine"] .finance-graph-stage','grid-template-columns','minmax(0,1fr)')));
  check('小屏图例保留完整换行空间',()=>assert.ok(mediaRule('(max-width:640px)','body[data-page="mine"] :is(.fg-legend,.fg-key)','margin','1rem')));
  check('英语长出处可换行',()=>{
    for(const selector of ['.fg-key','.fg-evidence a','.fg-controls label','.fg-inspector p'])assert.equal(property(selector,'overflow-wrap'),'anywhere',selector);
    assert.equal(property('.fg-index-button','white-space'),'normal');
  });
  check('控制区与洞察宽度随字号伸缩且不超过容器',()=>{
    assert.equal(property('.fg-controls','grid-template-columns'),'repeat(auto-fit,minmax(min(100%,10rem),1fr))');
    assert.equal(property('.fg-insights','grid-template-columns'),'repeat(auto-fit,minmax(min(100%,15rem),1fr))');
    assert.equal(property('.fg-canvas','overflow'),'auto');
  });
  check('图例与二维画布使用语义主题令牌',()=>{
    d.documentElement.style.setProperty('--ink-70','#35465f');d.documentElement.style.setProperty('--surface-card','#fff9ef');
    assert.equal(resolve(w.getComputedStyle(d.querySelector('.fg-key')).color),'#35465f');
    assert.equal(resolve(w.getComputedStyle(d.querySelector('.fg-view')).background),'#fff9ef');
    assert.equal(resolve(w.getComputedStyle(d.querySelector('.fg-edge text')).stroke),'#fff9ef');
  });
  console.log(`图谱可读性 CSS 合同通过：${checks} 组检查；纯内存，无网络，未验证实屏或三维 WebGL 字号。`);
}finally{dom.window.close();}

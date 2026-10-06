/* 在实际页面 HTML 上运行共享壳与国际化；不加载资源或访问网络。 */
const assert=require('node:assert/strict'),fs=require('node:fs');
const {JSDOM}=require('jsdom');
const tick=()=>new Promise(r=>setImmediate(r));
(async()=>{
 let count=0;
 for(const page of ['chat','account','recommend','mine','copilot','index']){
  const dom=new JSDOM(fs.readFileSync(page+'.html','utf8'),{url:'https://navren.test/'+page+'.html',runScripts:'outside-only'}),w=dom.window,d=w.document,store=new Map();
  try{
   const saved={account:{id:'dark'},recommend:{id:'custom',color:'#96713f'},mine:{id:'blue'},copilot:{id:'sea'},index:{id:'sand'}}[page];
   if(saved)store.set('navren_theme',saved);
   d.documentElement.style.setProperty('--surface','#ffffff');d.documentElement.style.setProperty('--font-base','18px');
   w.fetch=()=>{throw Error('禁止网络访问');};
   w.Storage={get:k=>store.get(k),set:(k,v)=>store.set(k,v)};w.STORAGE_KEYS={THEME:'navren_theme',LANGUAGE:'navren_language'};
   w.MODE_DEFAULT_FONT={classic:'medium',senior:'large',youth:'medium'};w.getInterfaceMode=()=> 'youth';w.getModeFont=()=> 'medium';w.applyFontSize=()=>{};
   w.matchMedia=()=>({matches:false,addEventListener(){}});w.NavrenPresentation={name:v=>v,text:v=>v};
   w.NavrenApp={href:href=>new URL(href,w.location.href).href,navigate:()=>{throw Error('测试不会真实导航');}};
   w.eval(fs.readFileSync('js/design.js','utf8'));w.eval(fs.readFileSync('js/i18n.js','utf8'));await tick();
   const controls=d.getElementById('navrenThemeButton');assert(controls,page);
   assert.equal(d.documentElement.dataset.theme,page==='account'?'dark':'light');
   assert.equal(d.documentElement.style.getPropertyValue('--surface'),'');assert.equal(d.documentElement.style.getPropertyValue('--font-base'),'18px');
   if(saved)assert.deepEqual({...store.get('navren_theme')},{id:page==='account'?'dark':'light'});
   assert.equal(controls.querySelector('.shell-tool-label').textContent,'外观');
   assert.equal(d.querySelectorAll('[data-theme-choice]').length,2);assert(!d.querySelector('#navrenThemeColor,input[type="color"],.shell-theme-reset,.shell-theme-swatch'));
   assert(controls.closest(page==='index'?'.welcome-page':'.header'));assert.equal(d.querySelectorAll('#navrenThemeButton').length,1);
   w.NavrenDesign.mountSettings(d.querySelector('.header')||d.querySelector('.welcome-page'));assert.equal(d.querySelectorAll('#navrenThemeButton').length,1);
   if(page!=='index'){
    const settings=d.getElementById('navrenSettings');assert.equal(settings.parentElement,d.querySelector('.sidebar'));assert.equal(settings.nextElementSibling,null);
    assert.equal(settings.textContent.trim(),'设置');assert(!settings.classList.contains('nav-item'));assert.equal(d.querySelectorAll('.sidebar .nav-item').length,page==='copilot'?1:4);
    assert.equal(settings.querySelector('circle').getAttribute('r'),'3');assert.equal(new URL(settings.href).searchParams.get('view'),'settings');assert(!d.querySelector('.header .shell-personal-entry'));
   }
   d.getElementById('navrenLanguageButton').click();await tick();assert.equal(w.NavrenI18n.language,'en');assert.equal(d.querySelector('[data-language-label]').textContent,'En');
   if(page!=='index')assert.equal(d.getElementById('navrenSettings').textContent.trim(),'Settings');
   assert.equal(controls.querySelector('.shell-tool-label').textContent,'Appearance');assert.equal(controls.getAttribute('aria-label'),'Choose appearance mode');
   assert.equal(d.querySelector('[data-theme-choice="light"]').textContent,'Light');assert.equal(d.querySelector('[data-theme-choice="dark"]').textContent,'Dark');
   d.getElementById('navrenThemeButton').click();assert.equal(d.getElementById('navrenThemeMenu').hidden,false);
   const light=d.querySelector('[data-theme-choice="light"]'),dark=d.querySelector('[data-theme-choice="dark"]'),menu=d.getElementById('navrenThemeMenu');
   let event;w.addEventListener('navren:theme',e=>event=e.detail);
   dark.click();assert.equal(d.documentElement.dataset.theme,'dark');assert.equal(d.documentElement.style.colorScheme,'dark');assert.deepEqual({...store.get('navren_theme')},{id:'dark'});
   assert.equal(w.NavrenDesign.theme.id,'dark');assert.equal(event.id,'dark');assert.equal(event.color,'#79b8ff');assert.equal(dark.getAttribute('aria-checked'),'true');assert.equal(light.tabIndex,-1);
   dark.focus();dark.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Home',bubbles:true}));assert.equal(d.documentElement.dataset.theme,'light');assert.equal(d.activeElement,light);
   light.dispatchEvent(new w.KeyboardEvent('keydown',{key:'End',bubbles:true}));assert.equal(d.documentElement.dataset.theme,'dark');assert.equal(d.activeElement,dark);
   dark.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));assert.equal(menu.hidden,true);assert.equal(d.activeElement,controls);
   d.getElementById('navrenLanguageButton').click();await tick();assert.equal(d.querySelector('[data-language-label]').textContent,'中文');
   assert.equal(controls.querySelector('.shell-tool-label').textContent,'外观');assert.equal(light.textContent,'浅色');assert.equal(dark.textContent,'深色');assert.equal(d.documentElement.dataset.theme,'dark');
   light.click();assert.equal(d.documentElement.dataset.theme,'light');assert.deepEqual({...store.get('navren_theme')},{id:'light'});
   store.set('navren_theme',{id:'dark'});w.dispatchEvent(new w.StorageEvent('storage',{key:'navren_theme'}));assert.equal(d.documentElement.dataset.theme,'dark');assert.equal(dark.getAttribute('aria-checked'),'true');
   w.NavrenDesign.applyTheme({id:'lavender'});assert.equal(d.documentElement.dataset.theme,'light');assert.deepEqual({...store.get('navren_theme')},{id:'light'});
   if(page!=='index')assert.equal(d.getElementById('navrenSettings').textContent.trim(),'设置');
   count++;console.log('通过：'+page+' 设置位置、唯一控件、中英切换、浅深外观、偏好迁移及键盘操作');
  }finally{w.close();}
 }
 console.log(count+' 个实际页面 DOM 验证通过');
})().catch(error=>{console.error(error);process.exitCode=1;});

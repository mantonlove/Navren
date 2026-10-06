/** 共享界面：主题、语言、模式、导航与键盘操作。只管理展示偏好。 */
window.NavrenDesign = (() => {
  const themes = [
    {id:'light',name:'浅色',color:'#367dcc'},
    {id:'dark',name:'深色',color:'#79b8ff'}
  ];
  function normalizeTheme(value) {
    // 旧版本的色彩偏好统一回到浅色，外观只保留浅色和深色两种。
    return themes.find(theme=>theme.id===value?.id)||themes[0];
  }
  const savedTheme=Storage.get(STORAGE_KEYS.THEME);
  let currentTheme=normalizeTheme(savedTheme);
  function applyTheme(value, persist=true) {
    currentTheme=normalizeTheme(value);
    const root=document.documentElement;
    // 移除旧色板留下的行内样式，让 CSS 的完整明暗色板生效。
    for(const key of ['primary','primary-deep','primary-soft','primary-border','surface','surface-card','surface-raised','surface-hover','border','border-emphasis','accent-water'])root.style.removeProperty('--'+key);
    root.dataset.theme=currentTheme.id;
    root.style.colorScheme=currentTheme.id;
    const color=getComputedStyle(root).getPropertyValue('--primary').trim()||currentTheme.color;
    currentTheme={...currentTheme,color};
    if(persist)Storage.set(STORAGE_KEYS.THEME,{id:currentTheme.id});
    syncTools();
    window.dispatchEvent(new CustomEvent('navren:theme',{detail:{id:currentTheme.id,color}}));
  }
  function syncTools() {
    const english=window.NavrenI18n?.language==='en';
    const labels=english?{appearance:'Appearance',choose:'Choose appearance mode',light:'Light',dark:'Dark',close:'Close'}:{appearance:'外观',choose:'选择外观模式',light:'浅色',dark:'深色',close:'关闭'};
    document.querySelectorAll('[data-theme-choice]').forEach(button=>{
      const selected=button.dataset.themeChoice===currentTheme.id;
      button.setAttribute('aria-checked',String(selected));button.tabIndex=selected?0:-1;
      button.querySelector('[data-theme-label]').textContent=labels[button.dataset.themeChoice];
    });
    const themeButton=document.getElementById('navrenThemeButton');
    if(themeButton){themeButton.title=labels.choose;themeButton.setAttribute('aria-label',labels.choose);themeButton.querySelector('.shell-tool-label').textContent=labels.appearance;}
    const menu=document.getElementById('navrenThemeMenu');
    if(menu){
      menu.setAttribute('aria-label',labels.choose);menu.querySelector('.shell-theme-options').setAttribute('aria-label',labels.choose);
    }
    const languageButton=document.getElementById('navrenLanguageButton');
    if(languageButton){
      languageButton.querySelector('[data-language-label]').textContent=english?'En':'中文';
      const label=english?'Switch to 中文':'切换至 English';
      languageButton.title=label;languageButton.setAttribute('aria-label',label);languageButton.dataset.language=english?'en':'zh-CN';
    }
  }
  function mountSettings(target) {
    if(!target||target.dataset.mounted||document.getElementById('navrenThemeButton'))return;target.dataset.mounted='true';
    const tools=document.createElement('div');tools.className='shell-tools'+(target.matches('.header,.welcome-page')?'':' settings-tools');
    tools.dataset.i18nSkip='';
    tools.innerHTML=`<button type="button" class="shell-tool" id="navrenThemeButton" title="选择外观模式" aria-label="选择外观模式" aria-expanded="false" aria-controls="navrenThemeMenu"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20.8 13.1A9 9 0 0 1 10.9 3.2a9 9 0 1 0 9.9 9.9Z"/></svg><span class="shell-tool-label">外观</span></button><button type="button" class="shell-tool shell-language" id="navrenLanguageButton" aria-label="切换至 English"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3 3 15 0 18-3-3-3-15 0-18Z"/></svg><span data-language-label translate="no">中文</span></button><section class="shell-theme-menu" id="navrenThemeMenu" hidden role="dialog" aria-label="选择外观模式"><div class="shell-theme-options" role="radiogroup" aria-label="选择外观模式"></div></section>`;
    target.append(tools);
    const themeButton=tools.querySelector('#navrenThemeButton'),menu=tools.querySelector('#navrenThemeMenu'),group=tools.querySelector('.shell-theme-options');
    for(const theme of themes){
      const choice=document.createElement('button');choice.type='button';choice.className='shell-theme-choice';choice.dataset.themeChoice=theme.id;choice.role='radio';
      const label=document.createElement('span');label.dataset.themeLabel='';label.textContent=theme.name;choice.append(label);choice.onclick=()=>applyTheme(theme);group.append(choice);
    }
    function closeMenu(restore=false){menu.hidden=true;themeButton.setAttribute('aria-expanded','false');if(restore)themeButton.focus({preventScroll:true});}
    themeButton.onclick=()=>{
      const open=menu.hidden;menu.hidden=!open;themeButton.setAttribute('aria-expanded',String(open));
      if(open)(group.querySelector('[aria-checked="true"]')||group.firstElementChild).focus({preventScroll:true});
    };
    group.addEventListener('keydown',e=>{
      if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'].includes(e.key))return;
      e.preventDefault();const items=[...group.children],index=items.indexOf(document.activeElement),next=e.key==='Home'?0:e.key==='End'?items.length-1:(index+(['ArrowLeft','ArrowUp'].includes(e.key)?-1:1)+items.length)%items.length;
      applyTheme(themes[next]);items[next].focus();
    });
    menu.addEventListener('keydown',e=>{
      if(e.key==='Escape'){e.preventDefault();e.stopPropagation();closeMenu(true);}
      if(e.key==='Tab')closeMenu(true);
    });
    document.addEventListener('pointerdown',e=>{if(!menu.hidden&&!menu.contains(e.target)&&!themeButton.contains(e.target))closeMenu();});
    tools.querySelector('#navrenLanguageButton').onclick=()=>{closeMenu();NavrenI18n.setLanguage(NavrenI18n.language==='en'?'zh-CN':'en');};
    window.addEventListener('navren:language',syncTools);
    syncTools();
  }
  applyTheme(currentTheme,false);
  if(savedTheme&&(savedTheme.id!==currentTheme.id||Object.hasOwn(savedTheme,'color')))Storage.set(STORAGE_KEYS.THEME,{id:currentTheme.id});
  if(document.querySelector('.app'))document.documentElement.classList.add('navren-shell');
  function switchMode(mode) {
    try{const current=window.NavrenViews?.snapshot?.();if(current)sessionStorage.setItem('navren_return_view',JSON.stringify(current));}catch(_){}
    if (!Object.hasOwn(MODE_DEFAULT_FONT, mode)) return;
    Storage.set('qingzhou_mode', mode);
    const url = new URL(location.href);
    url.searchParams.set('mode', mode);
    // 页面动作只执行一次；切换外观不再次发送上一条提问。
    url.searchParams.delete('q');
    url.searchParams.delete('action');
    NavrenApp.navigate(url.pathname + url.search + url.hash);
  }
  const mode = getInterfaceMode();
  Storage.set('qingzhou_mode', mode);
  document.body.classList.add('mode-' + mode);
  applyFontSize(getModeFont(mode));
  document.addEventListener('DOMContentLoaded', () => {
    const header=document.querySelector('.header');
    const sidebar=document.querySelector('.sidebar');
    if(sidebar&&!document.getElementById('navrenSettings')){
      const link=document.createElement('a');link.id='navrenSettings';link.className='shell-settings-entry';
      link.href=NavrenApp.href('mine.html?view=settings#settings',mode);link.setAttribute('aria-label','设置');
      link.innerHTML='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9.2 5 .6-2h4.4l.6 2 1.8 1.1 2-.5 2.2 3.8-1.4 1.5v2.2l1.4 1.5-2.2 3.8-2-.5-1.8 1.1-.6 2H9.8l-.6-2-1.8-1.1-2 .5-2.2-3.8 1.4-1.5V9.9L3.2 8.4l2.2-3.8 2 .5Z"/><circle cx="12" cy="12" r="3"/></svg><span>设置</span>';
      sidebar.append(link);
    }
    mountSettings(header||document.querySelector('.welcome-page'));
    const picker = document.getElementById('interfaceMode');
    if (picker) { picker.value = mode; picker.addEventListener('change', () => switchMode(picker.value)); }
    document.querySelectorAll('.sidebar a, .brand').forEach(link => {
      const url = new URL(link.getAttribute('href'), location.href);
      if (!url.pathname.endsWith('index.html')) url.searchParams.set('mode', mode);
      link.href = NavrenApp.href(url.pathname + url.search + url.hash);
    });
    const input = document.getElementById('userInput');
    if (input) input.setAttribute('aria-label', '输入银行或生活财务问题');
    document.querySelectorAll('button[title]').forEach(button => {
      if (!button.textContent.trim() && !button.hasAttribute('aria-label')) button.setAttribute('aria-label', button.title);
    });
    document.querySelectorAll('.modal-head button').forEach(button => button.setAttribute('aria-label', '关闭对话框'));
    document.querySelectorAll('.modal-overlay').forEach(overlay => {
      overlay.setAttribute('role', 'dialog');
      overlay.setAttribute('aria-modal', 'true');
      const title = overlay.querySelector('.modal-head span');
      if (title?.id) overlay.setAttribute('aria-labelledby', title.id);
      let previousFocus = null;
      const focusable = () => [...overlay.querySelectorAll('button, input, select, textarea, a[href], [tabindex="0"]')].filter(el => !el.disabled && el.getClientRects().length);
      new MutationObserver(() => {
        if (overlay.classList.contains('hidden')) { previousFocus?.focus(); return; }
        previousFocus = document.activeElement;
        (overlay.querySelector('input, textarea') || focusable()[0])?.focus();
      }).observe(overlay, {attributes:true, attributeFilter:['class']});
      overlay.addEventListener('keydown', event => {
        if (event.key === 'Escape') {
          event.preventDefault(); overlay.querySelector('.modal-head button')?.click();
        } else if (event.key === 'Tab') {
          const items = focusable(), first = items[0], last = items[items.length-1];
          if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
          else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
        }
      });
    });
    // 原有折叠组件和开关也支持键盘，并反映当前状态。
    document.querySelectorAll('.col-head, .toggle, #profileAvatar').forEach(control => {
      control.tabIndex = 0;
      control.setAttribute('role', control.classList.contains('toggle') ? 'switch' : 'button');
      const sync = () => {
        if (control.classList.contains('toggle')) {
          control.setAttribute('aria-checked', String(control.classList.contains('on')));
          control.setAttribute('aria-label', control.closest('.trow')?.querySelector('.tl')?.textContent || '语音播报');
        } else if (control.classList.contains('col-head')) control.setAttribute('aria-expanded', String(control.parentElement.classList.contains('open')));
        else control.setAttribute('aria-label', '更换头像');
      };
      sync();
      control.addEventListener('click', sync);
      control.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); control.click(); }
      });
      new MutationObserver(sync).observe(control.classList.contains('col-head') ? control.parentElement : control, {attributes: true, attributeFilter: ['class']});
    });
  });
  window.addEventListener('storage',event=>{if(event.key===STORAGE_KEYS.THEME)applyTheme(Storage.get(STORAGE_KEYS.THEME),false);});
  return {switchMode,applyTheme,mountSettings,get theme(){return {...currentTheme};}};
})();

// 鼠标点击导航不画焦点框；Tab 和方向键仍保留明确的键盘定位。
(() => {
  const root = document.documentElement;
  root.dataset.navrenInput = 'pointer';
  window.addEventListener('pointerdown', () => { root.dataset.navrenInput = 'pointer'; }, true);
  window.addEventListener('keydown', event => {
    if (['Tab', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(event.key)) {
      root.dataset.navrenInput = 'keyboard';
    }
  }, true);

  function mountPrivacyDialog() {
    const guide = document.getElementById('privacyGuide');
    if (!guide || guide.tagName === 'DIALOG') return;
    const dialog = document.createElement('dialog');
    for (const attribute of guide.attributes) dialog.setAttribute(attribute.name, attribute.value);
    dialog.setAttribute('aria-labelledby', 'privacyTitle');
    dialog.setAttribute('aria-describedby', 'privacyIntro');
    dialog.setAttribute('aria-modal', 'true');
    dialog.tabIndex = -1;

    const heading = document.createElement('div');
    heading.className = 'privacy-heading';
    const title = guide.querySelector('#privacyTitle');
    heading.append(title);
    const content = document.createElement('div');
    content.className = 'privacy-content';
    const introduction = guide.querySelector('p');
    introduction.id = 'privacyIntro';
    content.append(introduction);
    for (const item of guide.querySelectorAll('li')) {
      const paragraph = document.createElement('p');
      paragraph.textContent = item.textContent;
      content.append(paragraph);
    }
    dialog.append(heading, content, guide.querySelector('.privacy-actions'));
    guide.replaceWith(dialog);
    // 移到 body 直接子层，避免主区域 flex 排版和裁剪影响弹窗。
    document.body.append(dialog);

    let previousFocus = null;
    let savedOverflow = '';
    const cleanTitle = () => {
      const text = title.textContent.replace(/^🔒\s*/, '');
      if (text !== title.textContent) title.textContent = text;
    };
    new MutationObserver(cleanTitle).observe(title, { childList: true, characterData: true, subtree: true });
    cleanTitle();

    function sync() {
      if (dialog.classList.contains('hidden')) {
        if (dialog.open) dialog.close();
      } else if (!dialog.open) {
        previousFocus = document.activeElement;
        savedOverflow = document.documentElement.style.overflow;
        document.documentElement.style.overflow = 'hidden';
        dialog.showModal();
        // 初始聚焦弹窗本身，按钮不会一打开就出现焦点外框。
        dialog.focus({ preventScroll: true });
      }
    }
    const dismiss = () => { dialog.classList.add('hidden'); sync(); };
    dialog.addEventListener('cancel', event => { event.preventDefault(); dismiss(); });
    dialog.addEventListener('click', event => {
      if (event.target !== dialog) return;
      const bounds = dialog.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dismiss();
    });
    dialog.addEventListener('close', () => {
      dialog.classList.add('hidden');
      document.documentElement.style.overflow = savedOverflow;
      if (previousFocus?.isConnected && previousFocus !== document.body) previousFocus.focus({ preventScroll: true });
      previousFocus = null;
    });
    // 沿用 chat.js 的显示和“我知道了”逻辑，关闭遮罩不写入授权。
    new MutationObserver(sync).observe(dialog, { attributes: true, attributeFilter: ['class'] });
    sync();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mountPrivacyDialog, { once: true });
  else mountPrivacyDialog();
})();

/* 月份只负责选择；业务读取继续由原 input 的 change 处理。 */
window.NavrenMonthPicker = (() => {
  const instances = new WeakMap();
  let active = null, sequence = 0;
  const valid = value => /^\d{4}-(0[1-9]|1[0-2])$/.test(value || '') && value.slice(0,4) !== '0000';
  const english = () => window.NavrenI18n?.language === 'en' || document.documentElement.lang === 'en';
  const text = (zh,en) => english() ? en : zh;
  const calendar = '<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M16 3v4M8 3v4M3 11h18"/>';
  const chevron = '<path d="m6 9 6 6 6-6"/>';
  const arrow = '<path d="m14 6-6 6 6 6"/>';
  function icon(path) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg','svg');
    svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');
    svg.innerHTML = path;return svg;
  }
  function element(tag,className,copy) {
    const node = document.createElement(tag);node.className = className;
    if(copy != null)node.textContent = copy;return node;
  }
  function button(className,copy,action) {
    const node = element('button',className,copy);node.type = 'button';node.onclick = action;return node;
  }
  function monthLabel(value) {
    if(!valid(value))return text('选择月份','Choose month');
    const year = Number(value.slice(0,4)), month = Number(value.slice(5));
    return english() ? new Intl.DateTimeFormat('en',{month:'short',timeZone:'UTC'}).format(new Date(Date.UTC(2024,month-1,1)))+' '+year : year+'年'+month+'月';
  }
  function close(restoreFocus = true) {
    const previous = active;if(!previous)return;
    active = null;previous.popup.remove();previous.trigger.setAttribute('aria-expanded','false');
    if(restoreFocus && previous.trigger.isConnected && !previous.trigger.closest('[hidden]'))previous.trigger.focus({preventScroll:true});
  }
  function position() {
    if(!active)return;
    const {popup,trigger} = active;
    if(!trigger.isConnected || trigger.closest('[hidden]')){close(false);return;}
    const rect = trigger.getBoundingClientRect(), width = popup.offsetWidth, height = popup.offsetHeight;
    const viewport = window.visualViewport, left = viewport?.offsetLeft || 0, top = viewport?.offsetTop || 0;
    const vw = viewport?.width || window.innerWidth, vh = viewport?.height || window.innerHeight, gap = 8, gutter = 12;
    popup.style.left = Math.max(left+gutter,Math.min(rect.left,left+vw-width-gutter))+'px';
    const below = rect.bottom+gap, above = rect.top-height-gap;
    popup.style.top = Math.max(top+gutter,Math.min(below+height<=top+vh-gutter ? below : above,top+vh-height-gutter))+'px';
    popup.style.maxHeight = Math.max(0,vh-gutter*2)+'px';
  }
  function mount(input,options = {}) {
    if(!input || input.type !== 'month' || !input.parentNode)return null;
    const existing = instances.get(input);
    if(existing){existing.configure(options);return existing;}
    let settings = {...options},destroyed = false;
    const wasHidden = input.hidden, popupId = 'navrenMonthPopup'+(++sequence);
    const field = element('span','navren-month-field'), label = element('span','navren-month-value');
    const trigger = button('navren-month-trigger',null,() => active?.input === input ? close() : open());
    trigger.id = input.id ? input.id+'Trigger' : 'navrenMonthTrigger'+sequence;
    trigger.setAttribute('aria-haspopup','dialog');trigger.setAttribute('aria-expanded','false');trigger.setAttribute('aria-controls',popupId);
    const caret = icon(chevron);caret.classList.add('navren-month-caret');trigger.append(icon(calendar),label,caret);
    input.hidden = true;input.classList.add('navren-month-native');input.insertAdjacentElement('afterend',field);field.append(trigger);
    function bounds() {
      const today = new Date(), fallback = today.getFullYear()+'-'+String(today.getMonth()+1).padStart(2,'0');
      const current = valid(settings.currentMonth) ? settings.currentMonth : valid(input.max) ? input.max : fallback;
      return {min:valid(input.min)?input.min:'0001-01',max:valid(input.max)?input.max:current,current};
    }
    const allowed = value => valid(value) && value >= bounds().min && value <= bounds().max;
    function refresh() {
      if(destroyed)return;
      label.textContent = monthLabel(input.value);trigger.disabled = input.disabled;
      const name = input.getAttribute('aria-label') || text('月份','Month');
      const translated = english() ? ({'报告月份':'Report month','交易月份':'Transaction month','流水月份':'Statement month','月份':'Month'}[name] || name) : name;
      trigger.setAttribute('aria-label',translated+'：'+label.textContent);
      if(active?.input === input){
        if(input.disabled){close();return;}
        const focused=document.activeElement,inside=active.popup.contains(focused),value=focused?.dataset.value;
        const selector=focused?.classList.contains('navren-month-year')?'.navren-month-year':focused?.classList.contains('navren-month-current')?'.navren-month-current':focused?.classList.contains('navren-month-nav')?'.navren-month-nav'+(focused.classList.contains('is-next')?'.is-next':':not(.is-next)'):null;
        render();
        if(inside){if(value!=null)focusCell(value);else{const next=selector&&active.popup.querySelector(selector);(next&&!next.disabled?next:active.popup.querySelector('.navren-month-year'))?.focus({preventScroll:true});}}
      }
    }
    function available() {return !destroyed && input.isConnected && trigger.isConnected && !input.disabled && bounds().min<=bounds().max && !trigger.closest('[hidden],[aria-hidden="true"]') && !document.documentElement.classList.contains('navren-private-locked');}
    function choose(value,popup) {
      if(active?.input!==input || active.popup!==popup || !available() || !allowed(value))return;
      const changed = input.value !== value;close();
      if(!changed)return;
      input.value = value;refresh();
      input.dispatchEvent(new Event('change',{bubbles:true}));
    }
    function focusCell(value) {
      if(active?.input !== input)return;
      const cells = [...active.popup.querySelectorAll('.navren-month-grid button:not(:disabled)')];
      const target = cells.find(cell => cell.dataset.value === String(value)) || cells.find(cell => cell.getAttribute('aria-selected') === 'true') || cells[0];
      cells.forEach(cell => cell.tabIndex = cell === target ? 0 : -1);target?.focus({preventScroll:true});
    }
    function render(focusValue) {
      if(active?.input !== input)return;
      const state = active, range = bounds(), minYear = Number(range.min.slice(0,4)), maxYear = Number(range.max.slice(0,4));
      state.year=Math.max(minYear,Math.min(state.year,maxYear));
      if(state.mode==='years' && (state.yearStart<minYear || state.yearStart>maxYear))state.yearStart=minYear+Math.floor((state.year-minYear)/12)*12;
      const {popup,year,mode} = state, yearStart = state.yearStart, yearEnd = Math.min(yearStart+11,maxYear);
      const renderTurn=state.renderTurn=(state.renderTurn||0)+1,live=()=>active===state&&state.renderTurn===renderTurn;
      popup.replaceChildren();
      const header = element('div','navren-month-header');
      const previous = button('navren-month-nav',null,() => {if(live())shift(-1);}), next = button('navren-month-nav',null,() => {if(live())shift(1);});
      previous.append(icon(arrow));next.append(icon(arrow));next.classList.add('is-next');
      previous.setAttribute('aria-label',text(mode==='years'?'前一组年份':'上一年',mode==='years'?'Previous years':'Previous year'));
      next.setAttribute('aria-label',text(mode==='years'?'后一组年份':'下一年',mode==='years'?'Next years':'Next year'));
      previous.disabled = mode==='years' ? yearStart<=minYear : year<=minYear;
      next.disabled = mode==='years' ? yearEnd>=maxYear : year>=maxYear;
      const title = button('navren-month-year',mode==='years'?yearStart+' — '+yearEnd:year+text('年',''),() => {
        if(!live())return;
        state.mode = mode==='years' ? 'months' : 'years';state.yearStart = minYear+Math.floor((state.year-minYear)/12)*12;
        render(mode==='years' ? input.value : state.year);
      });
      title.id = popupId+'Title';title.setAttribute('aria-label',text(mode==='years'?'返回月份选择':'选择年份',mode==='years'?'Choose month':'Choose year'));
      title.setAttribute('aria-expanded',String(mode==='years'));title.append(icon(chevron));header.append(previous,title,next);popup.append(header);
      popup.setAttribute('aria-labelledby',title.id);
      const grid = element('div','navren-month-grid');grid.setAttribute('role','grid');grid.setAttribute('aria-label',text(mode==='years'?'年份':'月份',mode==='years'?'Years':'Months'));
      const count=mode==='years'?yearEnd-yearStart+1:12;
      let row;
      for(let index=0;index<count;index++){
        if(index%4===0){row=element('div','navren-month-grid-row');row.setAttribute('role','row');grid.append(row);}
        const value = mode==='years' ? yearStart+index : String(year).padStart(4,'0')+'-'+String(index+1).padStart(2,'0');
        const copy = mode==='years' ? String(value) : english() ? new Intl.DateTimeFormat('en',{month:'short',timeZone:'UTC'}).format(new Date(Date.UTC(2024,index,1))) : (index+1)+'月';
        const choice = button('navren-month-cell',copy,() => {
          if(!live())return;
          if(mode==='years'){state.year=Number(value);state.mode='months';render(String(state.year).padStart(4,'0')+'-'+(input.value.slice(5)||'01'));}else choose(value,popup);
        });
        choice.dataset.value = String(value);choice.tabIndex = -1;choice.setAttribute('role','gridcell');
        choice.disabled = mode==='years' ? value<minYear || value>maxYear : !allowed(value);
        choice.setAttribute('aria-selected',String(mode==='years'?Number(input.value.slice(0,4))===value:input.value===value));
        choice.setAttribute('aria-label',mode==='years'?String(value)+text('年',''):monthLabel(value));
        if(mode==='months' && value===range.current)choice.classList.add('is-current');
        row.append(choice);
      }
      popup.append(grid);
      const footer = element('div','navren-month-footer'), current = button('navren-month-current',text('本月','This month'),() => {if(live())choose(range.current,popup);});
      current.disabled = !allowed(range.current);current.setAttribute('aria-label',text('选择本月：','Choose this month: ')+monthLabel(range.current));footer.append(current);popup.append(footer);
      if(focusValue != null)focusCell(focusValue);
      else {
        const cells=[...grid.querySelectorAll('button:not(:disabled)')];const target=cells.find(cell=>cell.getAttribute('aria-selected')==='true')||cells[0];if(target)target.tabIndex=0;
      }
      position();
    }
    function shift(direction) {
      if(active?.input !== input)return;
      const state=active, range=bounds(), minYear=Number(range.min.slice(0,4)), maxYear=Number(range.max.slice(0,4));
      if(state.mode==='years'){
        const next=state.yearStart+direction*12;if(next>maxYear || next+11<minYear)return;state.yearStart=next;
      }else {
        const next=state.year+direction;if(next<minYear || next>maxYear)return;state.year=next;
      }
      render();const navigation=state.popup.querySelector(direction<0?'.navren-month-nav':'.navren-month-nav.is-next');
      (navigation.disabled?state.popup.querySelector('.navren-month-year'):navigation)?.focus({preventScroll:true});
    }
    function moveCell(event) {
      const delta = {ArrowLeft:-1,ArrowRight:1,ArrowUp:-4,ArrowDown:4}[event.key];
      if(delta==null && event.key!=='Home' && event.key!=='End')return;
      const cell=event.target.closest('.navren-month-cell');if(!cell || active?.input!==input)return;
      event.preventDefault();const state=active, range=bounds(), cells=[...state.popup.querySelectorAll('.navren-month-cell')];
      if(event.key==='Home' || event.key==='End'){const enabled=cells.filter(node=>!node.disabled);focusCell((event.key==='Home'?enabled[0]:enabled.at(-1))?.dataset.value);return;}
      if(state.mode==='months'){
        const parsed=cell.dataset.value.split('-').map(Number);let ordinal=parsed[0]*12+parsed[1]-1+delta;
        const value=String(Math.floor(ordinal/12)).padStart(4,'0')+'-'+String(ordinal%12+1).padStart(2,'0');
        if(!allowed(value))return;state.year=Math.floor(ordinal/12);render(value);
      }else {
        const year=Number(cell.dataset.value)+delta;if(year<Number(range.min.slice(0,4)) || year>Number(range.max.slice(0,4)))return;
        state.yearStart=Number(range.min.slice(0,4))+Math.floor((year-Number(range.min.slice(0,4)))/12)*12;render(year);
      }
    }
    function open() {
      if(!available())return;close(false);refresh();const range=bounds();
      const initial=allowed(input.value)?input.value:range.max;
      const popup=element('div','navren-month-popup');popup.id=popupId;popup.setAttribute('role','dialog');popup.dataset.i18nSkip='true';
      active={input,trigger,popup,year:Number(initial.slice(0,4)),yearStart:0,mode:'months',refresh};document.body.append(popup);
      trigger.setAttribute('aria-expanded','true');
      popup.addEventListener('keydown',event=>{
        if(active?.popup!==popup)return;
        if(event.key==='Escape'){event.preventDefault();event.stopPropagation();close();return;}
        if(event.key==='Tab'){
          const items=[...popup.querySelectorAll('button:not(:disabled)')].filter(node=>node.tabIndex>=0), position=items.indexOf(document.activeElement);
          if(event.shiftKey&&position===0 || !event.shiftKey&&position===items.length-1)close();
        }else moveCell(event);
      });
      render(initial);
    }
    trigger.addEventListener('keydown',event=>{if(event.key==='ArrowDown' || event.key==='ArrowUp'){event.preventDefault();open();}});
    input.addEventListener('change',refresh);
    const controller={trigger,input,open,close:()=>{if(!destroyed&&active?.input===input)close();},refresh,configure(next){if(destroyed)return;settings={...settings,...next};refresh();},destroy(){if(destroyed)return;if(active?.input===input)close(false);destroyed=true;field.remove();input.hidden=wasHidden;input.classList.remove('navren-month-native');input.removeEventListener('change',refresh);instances.delete(input);}};
    instances.set(input,controller);refresh();return controller;
  }
  document.addEventListener('pointerdown',event=>{if(active && !active.popup.contains(event.target) && !active.trigger.contains(event.target))close(false);});
  document.addEventListener('focusin',event=>{if(active && event.target!==document.body && event.target!==document.documentElement && !active.popup.contains(event.target) && !active.trigger.contains(event.target))close(false);});
  document.addEventListener('keydown',event=>{if(active && event.key==='Escape'){event.preventDefault();close();}});
  document.addEventListener('scroll',position,true);window.addEventListener('resize',position);
  window.visualViewport?.addEventListener('resize',position);window.visualViewport?.addEventListener('scroll',position);
  for(const event of ['navren:view','navren:privacy-reset','navren:identity-revoked','pagehide'])window.addEventListener(event,()=>close(false));
  window.addEventListener('navren:language',()=>{close(false);document.querySelectorAll('input.navren-month-native').forEach(input=>instances.get(input)?.refresh());});
  new MutationObserver(()=>{if(active && (!active.input.isConnected || !active.trigger.isConnected || active.trigger.closest('[hidden]')))close(false);}).observe(document.body,{childList:true,subtree:true,attributes:true,attributeFilter:['hidden']});
  return {mount,close,refresh:input=>instances.get(input)?.refresh()};
})();

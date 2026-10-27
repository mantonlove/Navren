/* 只组织已获授权的历史DOM；不读取记录、不保存正文、不赋予历史执行权限。 */
(() => {
  'use strict';
  function create() {
    let expanded = window.NavrenApp?.chatHistoryEntry?.() === 'internal';
    let root = null, button = null, panel = null, animation = null, generation = 0;
    const reduced = matchMedia('(prefers-reduced-motion: reduce)');
    const english = () => window.NavrenI18n?.language === 'en';
    function copy() {
      const label=english() ? 'Conversation history' : '历史会话';
      if (button) button.querySelector('span').textContent=label;
      panel?.setAttribute('aria-label',label);
    }
    function finish() {
      if (!panel) return;
      panel.style.height = ''; panel.style.overflow = '';
      panel.hidden = !expanded; panel.inert = !expanded;
      panel.setAttribute('aria-hidden', String(!expanded));
    }
    function stop() { generation++; animation?.cancel(); animation = null; }
    function setExpanded(next, animate = true) {
      if (!panel || expanded === next) return;
      const height = panel.hidden ? 0 : panel.getBoundingClientRect().height;
      stop(); expanded = next; button.setAttribute('aria-expanded', String(expanded));
      root.dataset.expanded = String(expanded);
      if (!expanded && panel.contains(document.activeElement)) button.focus({preventScroll:true});
      panel.hidden = false; panel.inert = !expanded;
      panel.setAttribute('aria-hidden', String(!expanded));
      if (!animate || reduced.matches || typeof panel.animate !== 'function') { finish(); return; }
      const target = expanded ? panel.scrollHeight : 0, current = generation;
      panel.style.height = height + 'px'; panel.style.overflow = 'hidden';
      animation = panel.animate([{height:height+'px',opacity:expanded ? .4 : 1},
        {height:target+'px',opacity:expanded ? 1 : .4}], {duration:240,easing:'cubic-bezier(.2,.7,.25,1)'});
      animation.finished.then(() => { if (current === generation) { animation = null; finish(); } }, () => {});
    }
    function mount() {
      if (root) return;
      root = document.createElement('section'); root.className = 'chat-history'; root.dataset.chatHistory = '';
      button = document.createElement('button'); button.type = 'button'; button.className = 'chat-history-toggle';
      button.setAttribute('aria-controls','chatHistoryPanel');button.setAttribute('aria-expanded',String(expanded));
      button.innerHTML = '<span></span><svg viewBox="0 0 20 20" aria-hidden="true"><path d="m5 7 5 5 5-5"/></svg>';
      panel = document.createElement('div');panel.id = 'chatHistoryPanel';panel.className = 'chat-history-panel';
      panel.setAttribute('role','region');panel.setAttribute('aria-label',english()?'Conversation history':'历史会话');
      button.addEventListener('click',()=>setExpanded(!expanded));root.append(button,panel);
      root.dataset.expanded = String(expanded);copy();finish();
    }
    const motionChanged = () => { stop(); finish(); };
    reduced.addEventListener?.('change',motionChanged);
    window.addEventListener('navren:language',copy);
    return Object.freeze({
      render(nodes) {
        mount();stop();panel.replaceChildren(...nodes);finish();return root;
      },
      reset() {
        stop();root?.remove();root = button = panel = null;
        expanded = window.NavrenApp?.chatHistoryEntry?.() === 'internal';
      },
      isExpanded:()=>expanded,
      destroy() { stop();root?.remove();window.removeEventListener('navren:language',copy);reduced.removeEventListener?.('change',motionChanged); }
    });
  }
  window.NavrenChatHistoryView = Object.freeze({create});
})();

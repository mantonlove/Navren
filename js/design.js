/** 共享界面：模式、导航与键盘操作。只管理展示偏好。 */
window.NavrenDesign = (() => {
  function switchMode(mode) {
    if (!Object.hasOwn(MODE_DEFAULT_FONT, mode)) return;
    Storage.set('qingzhou_mode', mode);
    const url = new URL(location.href);
    url.searchParams.set('mode', mode);
    // 页面动作只执行一次；切换外观不再次发送上一条提问。
    url.searchParams.delete('q');
    url.searchParams.delete('action');
    NavrenApp.navigate(url.pathname + url.search);
  }
  const mode = getInterfaceMode();
  Storage.set('qingzhou_mode', mode);
  document.body.classList.add('mode-' + mode);
  applyFontSize(getModeFont(mode));
  document.addEventListener('DOMContentLoaded', () => {
    const picker = document.getElementById('interfaceMode');
    if (picker) { picker.value = mode; picker.addEventListener('change', () => switchMode(picker.value)); }
    document.querySelectorAll('.sidebar a, .brand').forEach(link => {
      const url = new URL(link.getAttribute('href'), location.href);
      if (!url.pathname.endsWith('index.html')) url.searchParams.set('mode', mode);
      link.href = NavrenApp.href(url.pathname + url.search);
    });
    const input = document.getElementById('userInput');
    if (input) input.setAttribute('aria-label', '输入理财问题');
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
  return {switchMode};
})();

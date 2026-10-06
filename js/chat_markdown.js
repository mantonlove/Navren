/** Local Marked + DOMPurify adapter. Model text never supplies UI controls. */
window.NavrenMarkdown = (() => {
  const escape = text => String(text ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const parser = window.marked?.Marked ? new window.marked.Marked({
    gfm: true, breaks: true, async: false,
    renderer: { html: token => escape(token.text) }
  }) : null;
  const allowedTags = ['p','br','strong','em','del','a','h1','h2','h3','h4','h5','h6','ul','ol','li','blockquote','pre','code','hr','table','thead','tbody','tr','th','td'];
  function render(text) {
    const source = String(text ?? '').replace(/^[\u200B\u200C\u200D\u200E\u200F\uFEFF]+/, '');
    if (!parser || !window.DOMPurify?.sanitize) return escape(source).replace(/\n/g, '<br>');
    try {
      const template = document.createElement('template');
      template.innerHTML = window.DOMPurify.sanitize(parser.parse(source), {
        ALLOWED_TAGS: allowedTags,
        ALLOWED_ATTR: ['href','title','start','colspan','rowspan','align'],
        ALLOW_DATA_ATTR: false, ALLOW_ARIA_ATTR: false
      });
      template.content.querySelectorAll('a').forEach(link => {
        try {
          const href = link.getAttribute('href');
          if (!href) throw Error('missing link');
          const url = new URL(href, location.href);
          if (!/^https?:$/.test(url.protocol)) throw Error('unsupported link');
          link.href = url.href;
          if (url.origin !== location.origin) {
            link.target = '_blank'; link.rel = 'noopener noreferrer';
          }
        } catch { link.removeAttribute('href'); }
      });
      template.content.querySelectorAll('table').forEach(table => {
        const scroll = document.createElement('div');
        scroll.className = 'message-table-scroll'; scroll.tabIndex = 0;
        table.replaceWith(scroll); scroll.append(table);
      });
      return template.innerHTML;
    } catch { return escape(source).replace(/\n/g, '<br>'); }
  }
  return { render };
})();

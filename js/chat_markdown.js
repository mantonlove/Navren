/** Local Marked + DOMPurify adapter. Model text never supplies UI controls. */
window.NavrenMarkdown = (() => {
  const escape = text => String(text ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const parser = window.marked?.Marked ? new window.marked.Marked({
    gfm: true, breaks: true, async: false,
    renderer: { html: token => escape(token.text) }
  }) : null;
  const allowedTags = ['p','br','strong','em','del','a','h1','h2','h3','h4','h5','h6','ul','ol','li','blockquote','pre','code','hr','table','thead','tbody','tr','th','td'];

  // These are presentation-only transforms: keep the model's words and figures
  // intact, and never infer a financial fact or an action from plain text.
  function improveProse(root) {
    for (const paragraph of [...root.querySelectorAll('p')]) {
      if (paragraph.childNodes.length !== 1 || paragraph.firstChild?.nodeType !== Node.TEXT_NODE) continue;
      const text = paragraph.textContent;
      if (text.length < 120) continue;
      const clauses = text.match(/[^。！？；]*[。！？；]|[^。！？；]+$/gu) || [];
      if (clauses.length < 2 || clauses.join('') !== text) continue;
      const groups = [];
      let current = '';
      for (const clause of clauses) {
        current += clause;
        if (current.length >= 90) { groups.push(current); current = ''; }
      }
      if (current) groups.push(current);
      if (groups.length < 2) continue;
      const replacement = document.createDocumentFragment();
      for (const part of groups) {
        const p = document.createElement('p');
        p.className = 'reply-paragraph';
        p.textContent = part;
        replacement.appendChild(p);
      }
      paragraph.replaceWith(replacement);
    }
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (!node.parentElement?.closest('a,code,pre,strong,em,th,td')) nodes.push(node);
    }
    const value = /(?<![\w\d.,])(?:[−-]?[¥￥][ \t]*\d[\d,]*(?:\.\d+)?|[−-]?\d[\d,]*(?:\.\d+)?[ \t]*(?:亿元|万元|元|%|％))(?![\d.])/gu;
    for (const node of nodes) {
      const text = node.nodeValue;
      let match, from = 0, found = false;
      value.lastIndex = 0;
      const fragment = document.createDocumentFragment();
      while ((match = value.exec(text))) {
        found = true;
        fragment.appendChild(document.createTextNode(text.slice(from, match.index)));
        const strong = document.createElement('strong');
        strong.className = 'reply-inline-value';
        strong.textContent = match[0];
        fragment.appendChild(strong);
        from = value.lastIndex;
      }
      if (found) {
        fragment.appendChild(document.createTextNode(text.slice(from)));
        node.replaceWith(fragment);
      }
    }
    // Some backends return adjacent labelled amounts without separators, for
    // example “支出：100元收入：200元”. A break before the next existing
    // label makes that run readable without changing the reply's textContent.
    // Require a long paragraph, multiple displayed amounts and an explicit
    // following label/colon; ordinary prose such as “100元以下” stays intact.
    for (const paragraph of root.querySelectorAll('p')) {
      if (paragraph.textContent.length < 90) continue;
      const amounts = [...paragraph.querySelectorAll('.reply-inline-value')]
        .filter(node => /元$/.test(node.textContent) || /^[−-]?[¥￥]/u.test(node.textContent));
      if (amounts.length < 2) continue;
      let inserted = 0;
      for (const amount of amounts) {
        if (inserted >= 5 || amount.parentElement !== paragraph) continue;
        const next = amount.nextSibling;
        if (next?.nodeType !== Node.TEXT_NODE ||
            !/^[ \t]*[\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9（）()、，·\- \t]{1,31}[:：]/u.test(next.nodeValue)) continue;
        const lineBreak = document.createElement('br');
        lineBreak.className = 'reply-metric-break';
        amount.after(lineBreak);
        inserted++;
      }
    }
  }
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
      improveProse(template.content);
      return template.innerHTML;
    } catch { return escape(source).replace(/\n/g, '<br>'); }
  }
  function renderPlain(text) {
    const template = document.createElement('template');
    const paragraph = document.createElement('p');
    paragraph.textContent = String(text ?? '');
    template.content.appendChild(paragraph);
    improveProse(template.content);
    return template.innerHTML;
  }
  return { render, renderPlain };
})();

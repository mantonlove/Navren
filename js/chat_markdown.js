/** Local Marked + DOMPurify adapter. Only registered templates emit local reading controls. */
window.NavrenMarkdown = (() => {
  const escape = text => String(text ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let componentCount = 0, panelCount = 0;
  // Marked 18 的分隔符规则会把“在**预计30天（仍需确认）**的条件”留下为字面。
  // 仅补充这种两侧紧邻汉字、以完整括号限定收尾的显式强调；不猜测重点。
  const hanBefore = /\p{Script=Han}$/u, hanAfter = /^\p{Script=Han}/u;
  function explicitCjkStrong(source) {
    if (!source.startsWith('**')) return null;
    const end = source.indexOf('**', 2);
    if (end < 3 || end > 242 || !hanAfter.test(source.slice(end + 2))) return null;
    const text = source.slice(2, end);
    // 嵌套 Markdown、实体、代码、链接或 HTML 继续交原 tokenizer，不在此重解释。
    if (text !== text.trim() || /[\u0000-\u001f\u007f-\u009f*`\\<>\[\]&_~]/.test(text) || text.includes('://') || !/[）)]$/.test(text)) return null;
    const pairs = {'）':'（', ')':'('}, stack = [];
    for (const character of text) {
      if (character === '（' || character === '(') stack.push(character);
      else if (character === '）' || character === ')') { if (stack.pop() !== pairs[character]) return null; }
    }
    if (stack.length || !/[（(]/.test(text)) return null;
    return {type:'navren-cjk-strong', raw:source.slice(0, end + 2), text};
  }
  const cjkStrong = {
    name:'navren-cjk-strong', level:'inline',
    start(source) {
      if (this.lexer.state.inRawBlock) return;
      let index = source.indexOf('**');
      while (index >= 0) {
        if ((index === 0 || hanBefore.test(source.slice(Math.max(0, index - 2), index))) && explicitCjkStrong(source.slice(index))) return index;
        index = source.indexOf('**', index + 2);
      }
    },
    tokenizer(source, tokens) {
      if (this.lexer.state.inRawBlock) return;
      const previous = tokens[tokens.length - 1];
      if (previous?.type !== 'text' || !hanBefore.test(String(previous.raw ?? previous.text ?? '').slice(-2))) return;
      return explicitCjkStrong(source) || undefined;
    },
    renderer(token) { return '<strong>' + escape(token.text) + '</strong>'; }
  };
  const parser = window.marked?.Marked ? new window.marked.Marked({
    gfm: true, breaks: true, async: false,
    extensions: [cjkStrong],
    renderer: {
      html: token => escape(token.text),
      code: token => {
        const isPanel = token.lang === 'navren-panel';
        const html = componentCount < 2 && (!isPanel || panelCount < 1) ? window.NavrenConversationComponents?.renderCode({lang: token.lang || '', text: token.text}) : null;
        if (html) { componentCount++; if (isPanel) panelCount++; return html; }
        const label = (token.lang || '').startsWith('navren-') ? token.lang + '\n' : '';
        return '<pre><code>' + escape(label + token.text) + '</code></pre>';
      }
    }
  }) : null;
  const allowedTags = ['p','br','strong','em','del','a','h1','h2','h3','h4','h5','h6','ul','ol','li','blockquote','pre','code','hr','table','thead','tbody','tr','th','td','section','div','span','dl','dt','dd','details','summary','label','input','progress','small'];
  /** 只整理原有显式阅读边界，不猜测结论、关键金额或业务状态。 */
  function decorateProse(fragment, source) {
    // 已由空行、列表、引用、标题或组件划分的正文沿原边界保留。
    const children = [...fragment.children];
    if (!/\r?\n[\t ]*\r?\n/.test(source) && children.length === 1 && children[0].tagName === 'P') {
      const paragraph = children[0], breaks = [...paragraph.querySelectorAll('br')];
      // 跨 strong/a 等行内结构的换行不拆，避免复制或拆断其原有语义。
      if (breaks.length && breaks.every(br => br.parentNode === paragraph)) {
        const groups = [[]];
        for (const node of [...paragraph.childNodes]) {
          if (node.nodeType === 1 && node.tagName === 'BR') groups.push([]);
          else groups[groups.length - 1].push(node);
        }
        if (groups.every(nodes => nodes.some(node => node.textContent.trim()))) {
          const lines = groups.map(nodes => {
            const line = document.createElement('p'); line.className = 'prose-line';
            line.append(...nodes); return line;
          });
          paragraph.replaceWith(...lines);
        }
      }
    }
    const paragraphs = [...fragment.children].filter(node => node.tagName === 'P');
    for (const paragraph of paragraphs) {
      const label = paragraph.textContent.trim();
      if (label.length > 1 && [...label].length <= 28 && /[:：]$/.test(label) &&
          !/[\r\n。！？]/.test(label) && !paragraph.querySelector('br,a,code')) {
        paragraph.classList.add('prose-section-label');
      }
    }
    // 导语只表示阅读起点，不增加“结论”“已核验”等含义或额外文字。
    paragraphs.find(paragraph => !paragraph.classList.contains('prose-section-label'))?.classList.add('prose-lead');
  }
  function render(text) {
    const source = String(text ?? '').replace(/^[\u200B\u200C\u200D\u200E\u200F\uFEFF]+/, '');
    if (!parser || !window.DOMPurify?.sanitize) return escape(source).replace(/\n/g, '<br>');
    try {
      componentCount = 0; panelCount = 0;
      const template = document.createElement('template');
      template.innerHTML = window.DOMPurify.sanitize(parser.parse(source), {
        ALLOWED_TAGS: allowedTags,
        ALLOWED_ATTR: ['href','title','start','colspan','rowspan','align','class','aria-label','type','value','max','open'],
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
      template.content.querySelectorAll('.ncc-table-wrap').forEach(scroll => {
        scroll.tabIndex = 0;
        scroll.setAttribute('aria-label', '完整对比内容');
      });
      template.content.querySelectorAll('table').forEach(table => {
        if (table.closest('.ncc-component')) return;
        const scroll = document.createElement('div');
        scroll.className = 'message-table-scroll'; scroll.tabIndex = 0;
        table.replaceWith(scroll); scroll.append(table);
      });
      decorateProse(template.content, source);
      return template.innerHTML;
    } catch { return escape(source).replace(/\n/g, '<br>'); }
  }
  return { render };
})();

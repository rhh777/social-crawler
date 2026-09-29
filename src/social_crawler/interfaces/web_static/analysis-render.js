/* Local, sanitized GFM renderer. No HTML or image URL supplied by a model is trusted. */
window.AnalysisMarkdown = (() => {
  const escape = (s) => String(s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const parser = new marked.Marked({gfm: true, breaks: true, renderer: {
    html({text}) { return escape(text); },
    image({href, text}) { return `<a href="${escape(href)}" data-markdown-image="true">${escape(text || '图片')}</a>`; },
  }});
  function render(text, sessionId) {
    const root = document.createElement('div');
    root.innerHTML = DOMPurify.sanitize(parser.parse(text || ''), {
      ALLOWED_TAGS: ['p','br','strong','em','del','blockquote','ul','ol','li','h1','h2','h3','h4','h5','h6','hr','pre','code','table','thead','tbody','tr','th','td','a','input'],
      ALLOWED_ATTR: ['href','title','class','start','align','type','checked','disabled','data-markdown-image'],
      ALLOW_DATA_ATTR: false,
    });
    root.querySelectorAll('input').forEach(el => { el.type = 'checkbox'; el.disabled = true; });
    root.querySelectorAll('a').forEach(a => {
      if (!a.getAttribute('href')) { a.replaceWith(document.createTextNode(a.textContent)); return; }
      let url;
      try { url = new URL(a.getAttribute('href') || '', location.origin); } catch { a.replaceWith(document.createTextNode(a.textContent)); return; }
      if (!['https:', 'http:', 'mailto:'].includes(url.protocol) || url.username || url.password) {
        a.replaceWith(document.createTextNode(a.textContent)); return;
      }
      if (a.dataset.markdownImage) {
        const local = url.origin === location.origin && new RegExp(`^/api/analysis/attachments/${sessionId}/[a-f0-9]{32}$`).test(url.pathname);
        if (!local && !['https:', 'http:'].includes(url.protocol)) { a.replaceWith(document.createTextNode(a.textContent)); return; }
        const button = document.createElement('button'); button.type = 'button'; button.className = 'markdown-image';
        button.dataset.previewImage = url.href;
        if (local) {
          const img = document.createElement('img'); img.src = url.href; img.alt = a.textContent; img.loading = 'lazy'; img.referrerPolicy = 'no-referrer'; button.append(img);
        } else { button.classList.add('external-image'); button.textContent = `查看图片 · ${a.textContent || url.hostname}`; }
        a.replaceWith(button);
      } else if (url.origin === location.origin && url.pathname === '/api/analysis/evidence') {
        if (url.searchParams.get('session_id') !== sessionId) { a.replaceWith(document.createTextNode(a.textContent)); return; }
        const button = document.createElement('button'); button.type = 'button'; button.className = 'analysis-citation';
        button.dataset.analysisCitation = url.searchParams.get('citation') || ''; button.textContent = a.textContent;
        a.replaceWith(button);
      } else { a.target = '_blank'; a.rel = 'noopener noreferrer'; a.referrerPolicy = 'no-referrer'; }
    });
    root.querySelectorAll('pre').forEach(pre => {
      const bar = document.createElement('div'); bar.className = 'code-toolbar';
      const language = document.createElement('span'); language.textContent = pre.querySelector('code')?.className.replace('language-', '') || '代码';
      const copy = document.createElement('button'); copy.type = 'button'; copy.textContent = '复制代码'; copy.dataset.copyCode = 'true';
      bar.append(language, copy); pre.prepend(bar);
    });
    root.querySelectorAll('table').forEach(table => { const wrap = document.createElement('div'); wrap.className = 'markdown-table'; table.replaceWith(wrap); wrap.append(table); });
    return root.innerHTML;
  }
  return {render};
})();

(function () {
  'use strict';

  var state = {
    build: null,
    documents: [],
    byId: new Map(),
    track: 'django_developer',
    current: null,
    searchLoaded: false,
    searchLoading: false,
    searchText: new Map(),
    selectedResult: -1
  };
  var el = {
    nav: document.getElementById('docs-nav'),
    navTree: document.getElementById('nav-tree'),
    navToggle: document.getElementById('nav-toggle'),
    main: document.getElementById('docs-main'),
    body: document.getElementById('article-body'),
    status: document.getElementById('article-status'),
    source: document.getElementById('source-link'),
    toc: document.getElementById('toc'),
    dialog: document.getElementById('search-dialog'),
    searchTrigger: document.getElementById('search-trigger'),
    searchInput: document.getElementById('search-input'),
    searchClose: document.getElementById('search-close'),
    searchMeta: document.getElementById('search-meta'),
    searchResults: document.getElementById('search-results')
  };

  function escapeHtml(value) {
    return String(value).replace(/[&<>'"]/g, function (char) {
      return {'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char];
    });
  }

  function slugify(value) {
    return value.toLowerCase().replace(/<[^>]+>/g, '').replace(/[^\w\- ]/g, '')
      .replace(/[-\s]+/g, '-').replace(/^-|-$/g, '') || 'section';
  }

  function requestedId() {
    var id = new URLSearchParams(location.search).get('doc');
    if (!id) { return 'django_developer/README'; }
    return id.replace(/^docs\//, '').replace(/\.md$/, '').replace(/^\/+/, '');
  }

  function setStatus(message, error) {
    el.status.textContent = message;
    el.status.classList.toggle('error', Boolean(error));
  }

  function prettySection(section) {
    if (section === 'overview') { return 'Overview'; }
    if (section === 'aws') { return 'AWS'; }
    return section.replace(/_/g, ' ').replace(/\b\w/g, function (char) { return char.toUpperCase(); });
  }

  function renderNavigation() {
    var sections = new Map();
    state.documents.filter(function (doc) { return doc.track === state.track; }).forEach(function (doc) {
      if (!sections.has(doc.section)) { sections.set(doc.section, []); }
      sections.get(doc.section).push(doc);
    });
    var order = Array.from(sections.keys()).sort(function (a, b) {
      if (a === 'overview') { return -1; }
      if (b === 'overview') { return 1; }
      return a.localeCompare(b);
    });
    el.navTree.innerHTML = order.map(function (section) {
      var links = sections.get(section).map(function (doc) {
        var current = state.current && state.current.id === doc.id;
        return '<a class="doc-link" href="/docs/?doc=' + encodeURIComponent(doc.id) + '"' +
          (current ? ' aria-current="page"' : '') + '>' + escapeHtml(doc.title) + '</a>';
      }).join('');
      return '<section><h2>' + escapeHtml(prettySection(section)) + '</h2>' + links + '</section>';
    }).join('');
  }

  function resolveRepoPath(source, target) {
    var parts = ['docs'].concat(source.split('/').slice(0, -1));
    target.split('/').forEach(function (part) {
      if (!part || part === '.') { return; }
      if (part === '..') { if (parts.length) { parts.pop(); } }
      else { parts.push(part); }
    });
    return parts.join('/');
  }

  function docIdFromRepoPath(repoPath) {
    var tracks = ['django_developer/', 'web_developer/'];
    for (var i = 0; i < tracks.length; i += 1) {
      var index = repoPath.indexOf(tracks[i]);
      if (index !== -1 && repoPath.endsWith('.md')) {
        return repoPath.slice(index).replace(/\.md$/, '');
      }
    }
    return null;
  }

  function rewriteHref(href, source) {
    if (!href || href.charAt(0) === '#' || /^(https?:|mailto:|tel:)/i.test(href)) {
      return {href: href, external: /^(https?:)/i.test(href)};
    }
    var pieces = href.split('#');
    var repoPath = resolveRepoPath(source, pieces[0]);
    var docId = docIdFromRepoPath(repoPath);
    if (docId && state.byId.has(docId)) {
      return {href: '/docs/?doc=' + encodeURIComponent(docId) + (pieces[1] ? '#' + pieces[1] : ''), doc: true};
    }
    return {href: 'https://github.com/NativeMojo/django-mojo/blob/' + state.build.commit + '/' + repoPath + (pieces[1] ? '#' + pieces[1] : ''), external: true};
  }

  function enhanceArticle(doc) {
    var used = {};
    var headings = Array.from(el.body.querySelectorAll('h1,h2,h3,h4,h5,h6'));
    headings.forEach(function (heading) {
      var base = slugify(heading.textContent);
      var count = used[base] || 0;
      used[base] = count + 1;
      heading.id = count ? base + '-' + count : base;
    });
    Object.keys(doc.aliases || {}).forEach(function (alias) {
      var target = document.getElementById(doc.aliases[alias]);
      if (!target || document.getElementById(alias)) { return; }
      var marker = document.createElement('span');
      marker.id = alias; marker.setAttribute('aria-hidden', 'true');
      marker.style.position = 'relative'; marker.style.top = '-80px';
      target.parentNode.insertBefore(marker, target);
    });
    el.body.querySelectorAll('a[href]').forEach(function (anchor) {
      var result = rewriteHref(anchor.getAttribute('href'), doc.source);
      anchor.setAttribute('href', result.href);
      if (result.doc) { anchor.classList.add('doc-link'); }
      if (result.external) { anchor.setAttribute('rel', 'noopener'); }
    });
    el.body.querySelectorAll('img[src]').forEach(function (image) {
      var src = image.getAttribute('src');
      if (!/^(https?:|data:)/i.test(src)) {
        var repoPath = resolveRepoPath(doc.source, src);
        image.src = 'https://raw.githubusercontent.com/NativeMojo/django-mojo/' + state.build.commit + '/' + repoPath;
      }
      image.loading = 'lazy';
    });
    el.body.querySelectorAll('pre').forEach(function (pre) {
      var button = document.createElement('button');
      button.type = 'button'; button.className = 'copy-code'; button.textContent = 'Copy';
      button.addEventListener('click', function () {
        if (!navigator.clipboard) { return; }
        navigator.clipboard.writeText(pre.querySelector('code') ? pre.querySelector('code').textContent : pre.textContent)
          .then(function () { button.textContent = 'Copied'; setTimeout(function () { button.textContent = 'Copy'; }, 1200); });
      });
      pre.appendChild(button);
    });
    el.toc.innerHTML = headings.filter(function (heading) { return Number(heading.tagName.slice(1)) <= 3; })
      .slice(1).map(function (heading) {
        return '<a href="#' + encodeURIComponent(heading.id) + '" data-level="' + heading.tagName.slice(1) + '">' + escapeHtml(heading.textContent) + '</a>';
      }).join('');
  }

  function articleError(doc, message) {
    var title = doc ? doc.title : 'Document not found';
    el.body.innerHTML = '<div class="article-error"><h1>' + escapeHtml(title) + '</h1><p>' + escapeHtml(message) + '</p><div>' +
      '<button class="btn btn--accent" id="retry-document" type="button">Retry</button>' +
      '<a class="btn btn--ghost" href="/docs/?doc=django_developer/README">Django docs</a>' +
      '<a class="btn btn--ghost" href="/docs/?doc=web_developer/README">API docs</a></div></div>';
    setStatus(message, true); el.toc.innerHTML = '';
    var retry = document.getElementById('retry-document');
    if (retry && doc) { retry.addEventListener('click', function () { loadDocument(doc.id, false); }); }
  }

  async function loadDocument(id, updateHistory) {
    var doc = state.byId.get(id);
    if (!doc) { articleError(null, 'The requested documentation path is not in this build.'); return; }
    state.current = doc; state.track = doc.track; renderNavigation();
    document.querySelectorAll('[data-track]').forEach(function (button) {
      button.setAttribute('aria-pressed', button.getAttribute('data-track') === state.track ? 'true' : 'false');
    });
    if (updateHistory) { history.pushState({doc:id}, '', '/docs/?doc=' + encodeURIComponent(id)); }
    setStatus('Loading ' + doc.source + '…'); el.body.innerHTML = ''; el.toc.innerHTML = '';
    el.source.href = 'https://github.com/NativeMojo/django-mojo/blob/' + state.build.commit + '/docs/' + doc.source;
    try {
      var url = 'https://raw.githubusercontent.com/NativeMojo/django-mojo/' + state.build.commit + '/docs/' + doc.source;
      var response = await fetch(url);
      if (!response.ok) { throw new Error('Source returned HTTP ' + response.status); }
      var markdown = await response.text();
      el.body.innerHTML = DOMPurify.sanitize(
        marked.parse(markdown, {gfm:true, breaks:false}),
        {USE_PROFILES:{html:true}}
      );
      enhanceArticle(doc);
      document.title = doc.title + ' — Django-MOJO';
      setStatus(doc.track === 'django_developer' ? 'Django developer reference' : 'Web & API reference');
      el.main.scrollTop = 0;
      if (location.hash) { setTimeout(function () { var target = document.getElementById(decodeURIComponent(location.hash.slice(1))); if (target) { target.scrollIntoView(); } }, 0); }
      closeMobileNav();
    } catch (error) {
      articleError(doc, 'This article could not be loaded from the pinned documentation source. ' + error.message);
    }
  }

  function closeMobileNav() {
    el.nav.classList.remove('open'); el.navToggle.setAttribute('aria-expanded', 'false'); document.body.classList.remove('no-scroll');
  }

  function openSearch() {
    if (!el.dialog.open) { el.dialog.showModal(); }
    el.searchInput.focus();
  }

  async function loadSearchShards() {
    if (state.searchLoaded || state.searchLoading) { return; }
    state.searchLoading = true; el.searchMeta.textContent = 'Loading full-text index…';
    try {
      var shards = await Promise.all(state.build.search.map(function (path) { return fetch('/' + path).then(function (response) { if (!response.ok) { throw new Error(path); } return response.json(); }); }));
      shards.forEach(function (shard) {
        if (shard.commit !== state.build.commit) { throw new Error('Search index commit mismatch'); }
        shard.entries.forEach(function (entry) { state.searchText.set(entry.id, entry.text); });
      });
      state.searchLoaded = true; el.searchMeta.textContent = 'Full-text index ready · ' + state.documents.length + ' articles';
    } catch (error) {
      el.searchMeta.textContent = 'Full-text index incomplete · title and heading search only';
    } finally {
      state.searchLoading = false; renderSearch(el.searchInput.value);
    }
  }

  function scoreDocument(doc, query) {
    var title = doc.title.toLowerCase(); var headings = doc.headings.map(function (item) { return item.title.toLowerCase(); }).join(' ');
    var metadata = doc.id.toLowerCase() + ' ' + title + ' ' + headings + ' ' + doc.excerpt.toLowerCase();
    var score = 0;
    if (title === query) { score += 120; }
    if (title.indexOf(query) === 0) { score += 70; }
    if (title.includes(query)) { score += 40; }
    if (headings.includes(query)) { score += 22; }
    if (metadata.includes(query)) { score += 10; }
    var body = state.searchText.get(doc.id) || '';
    if (body.includes(query)) { score += 6 + Math.min(12, body.split(query).length - 1); }
    return score;
  }

  function renderSearch(rawQuery) {
    var query = rawQuery.trim().toLowerCase(); state.selectedResult = -1;
    if (query.length < 2) { el.searchResults.innerHTML = '<p class="search-empty">Type at least two characters to search the knowledge base.</p>'; return; }
    if (!state.searchLoaded && !state.searchLoading) { loadSearchShards(); }
    var results = state.documents.map(function (doc) { return {doc:doc, score:scoreDocument(doc, query)}; })
      .filter(function (row) { return row.score > 0; }).sort(function (a,b) { return b.score-a.score || a.doc.title.localeCompare(b.doc.title); }).slice(0,30);
    if (!results.length) { el.searchResults.innerHTML = '<p class="search-empty">No documentation matched “' + escapeHtml(rawQuery) + '”.</p>'; return; }
    el.searchResults.innerHTML = results.map(function (row) {
      return '<a class="search-result doc-link" role="option" aria-selected="false" href="/docs/?doc=' + encodeURIComponent(row.doc.id) + '"><b>' + escapeHtml(row.doc.title) + '</b><span>' + escapeHtml(prettySection(row.doc.section)) + ' · ' + (row.doc.track === 'django_developer' ? 'Django developer' : 'Web / API') + '</span></a>';
    }).join('');
  }

  function moveSearchSelection(direction) {
    var results = Array.from(el.searchResults.querySelectorAll('.search-result'));
    if (!results.length) { return; }
    state.selectedResult = (state.selectedResult + direction + results.length) % results.length;
    results.forEach(function (result, index) { result.setAttribute('aria-selected', index === state.selectedResult ? 'true' : 'false'); });
    results[state.selectedResult].scrollIntoView({block:'nearest'});
  }

  async function initialize() {
    try {
      var buildResponse = await fetch('/data/build.json');
      if (!buildResponse.ok) { throw new Error('Build metadata returned HTTP ' + buildResponse.status); }
      state.build = await buildResponse.json();
      var shards = await Promise.all(state.build.catalogs.map(function (path) { return fetch('/' + path).then(function (response) { if (!response.ok) { throw new Error(path); } return response.json(); }); }));
      shards.forEach(function (shard) {
        if (shard.commit !== state.build.commit) { throw new Error('Catalog commit mismatch'); }
        state.documents = state.documents.concat(shard.entries);
      });
      state.documents.sort(function (a,b) { return a.id.localeCompare(b.id); });
      state.documents.forEach(function (doc) { state.byId.set(doc.id, doc); });
      await loadDocument(requestedId(), false);
    } catch (error) {
      articleError(null, 'The documentation catalog could not be loaded. ' + error.message);
    }
  }

  document.addEventListener('click', function (event) {
    var link = event.target.closest('a.doc-link');
    if (!link) { return; }
    var url = new URL(link.href, location.href); var id = url.searchParams.get('doc');
    if (!id || !state.byId.has(id)) { return; }
    event.preventDefault(); el.dialog.close(); loadDocument(id, true);
  });
  document.querySelectorAll('[data-track]').forEach(function (button) {
    button.addEventListener('click', function () { state.track = button.getAttribute('data-track'); renderNavigation(); document.querySelectorAll('[data-track]').forEach(function (item) { item.setAttribute('aria-pressed', item === button ? 'true' : 'false'); }); });
  });
  el.navToggle.addEventListener('click', function () { var open = !el.nav.classList.contains('open'); el.nav.classList.toggle('open', open); el.navToggle.setAttribute('aria-expanded', open ? 'true' : 'false'); document.body.classList.toggle('no-scroll', open); });
  el.searchTrigger.addEventListener('click', openSearch); el.searchClose.addEventListener('click', function () { el.dialog.close(); });
  el.searchInput.addEventListener('input', function () { renderSearch(el.searchInput.value); });
  el.searchInput.addEventListener('keydown', function (event) {
    if (event.key === 'ArrowDown') { event.preventDefault(); moveSearchSelection(1); }
    if (event.key === 'ArrowUp') { event.preventDefault(); moveSearchSelection(-1); }
    if (event.key === 'Enter' && state.selectedResult >= 0) { var selected = el.searchResults.querySelector('[aria-selected="true"]'); if (selected) { selected.click(); } }
  });
  document.addEventListener('keydown', function (event) {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); openSearch(); }
    if (event.key === '/' && !/input|textarea/i.test(document.activeElement.tagName)) { event.preventDefault(); openSearch(); }
  });
  window.addEventListener('popstate', function () { loadDocument(requestedId(), false); });
  initialize();
})();

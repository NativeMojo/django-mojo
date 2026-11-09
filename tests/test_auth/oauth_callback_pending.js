// Run the hosted login page's real inline scripts against a small fake DOM, a
// controllable clock and a MojoAuth stub whose OAuth completion is held, then
// drive the #8579 pending view through every outcome.
//
// stdin: {"url": <callback URL>, "tree": <element tree>, "scripts": [<inline script>...]}
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const input = JSON.parse(fs.readFileSync(0, 'utf8'));

// ---------------------------------------------------------------- fake DOM
function camel(name) { return name.replace(/-([a-z])/g, (_, c) => c.toUpperCase()); }
function kebab(name) { return name.replace(/[A-Z]/g, c => '-' + c.toLowerCase()); }

class El {
  constructor(doc, node, parent) {
    this.ownerDocument = doc;
    this.tagName = (node.tag || 'div').toUpperCase();
    this.attrs = new Map(Object.entries(node.attrs || {}));
    this.parentNode = parent || null;
    this.listeners = {};
    this.style = {};
    this.offsetWidth = 0;
    this.value = this.attrs.get('value') || '';
    this.type = this.attrs.get('type') || '';
    this._text = this.tagName === 'SCRIPT' && !this.attrs.has('id') ? '' : (node.text || '');
    this.children = (node.children || []).map(child => new El(doc, child, this));
  }
  getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  removeAttribute(name) { this.attrs.delete(name); }
  hasAttribute(name) { return this.attrs.has(name); }
  get id() { return this.getAttribute('id') || ''; }
  get className() { return this.getAttribute('class') || ''; }
  set className(value) { this.setAttribute('class', value); }
  get hidden() { return this.attrs.has('hidden'); }
  set hidden(on) { if (on) this.attrs.set('hidden', ''); else this.attrs.delete('hidden'); }
  get disabled() { return this.attrs.has('disabled'); }
  set disabled(on) { if (on) this.attrs.set('disabled', ''); else this.attrs.delete('disabled'); }
  get href() { return this.getAttribute('href') || ''; }
  set href(value) { this.setAttribute('href', value); }
  get classList() {
    const el = this;
    const list = () => el.className.split(/\s+/).filter(Boolean);
    const write = items => el.setAttribute('class', items.join(' '));
    return {
      contains: c => list().includes(c),
      add: (...cs) => write([...new Set([...list(), ...cs])]),
      remove: (...cs) => write(list().filter(c => !cs.includes(c))),
      toggle: (c, force) => {
        const on = force === undefined ? !list().includes(c) : !!force;
        if (on) write([...new Set([...list(), c])]); else write(list().filter(x => x !== c));
        return on;
      },
    };
  }
  get dataset() {
    const el = this;
    return new Proxy({}, {
      get: (_, key) => el.getAttribute('data-' + kebab(String(key))) ?? undefined,
      set: (_, key, value) => { el.setAttribute('data-' + kebab(String(key)), value); return true; },
    });
  }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  set innerHTML(value) { this._text = ''; this.children = []; }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  dispatch(type, extra) {
    const event = Object.assign({ type, target: this, defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; }, stopPropagation() {} }, extra || {});
    (this.listeners[type] || []).forEach(fn => fn.call(this, event));
    return event;
  }
  click() { if (!this.disabled) this.dispatch('click'); }
  focus() { this.ownerDocument.activeElement = this; }
  blur() { if (this.ownerDocument.activeElement === this) this.ownerDocument.activeElement = this.ownerDocument.body; }
  contains(other) { for (let n = other; n; n = n.parentNode) if (n === this) return true; return false; }
  *walk() { for (const child of this.children) { yield child; yield* child.walk(); } }
  matches(selector) { return selector.split(',').some(s => matchCompound(this, s.trim())); }
  querySelectorAll(selector) { return [...this.walk()].filter(el => el.matches(selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

function matchCompound(el, sel) {
  const m = /^([a-zA-Z][\w-]*)?((?:[#.][\w-]+|\[[^\]]+\]|:[\w-]+)*)$/.exec(sel);
  if (!m) throw new Error('fake DOM: unsupported selector ' + JSON.stringify(sel));
  if (m[1] && el.tagName !== m[1].toUpperCase()) return false;
  const parts = m[2].match(/[#.][\w-]+|\[[^\]]+\]|:[\w-]+/g) || [];
  return parts.every(part => {
    if (part[0] === '#') return el.id === part.slice(1);
    if (part[0] === '.') return el.classList.contains(part.slice(1));
    if (part[0] === ':') return part === ':checked' ? el.hasAttribute('checked') : false;
    const a = /^\[([\w-]+)(?:="?([^"]*)"?)?\]$/.exec(part);
    return a[2] === undefined ? el.hasAttribute(a[1]) : el.getAttribute(a[1]) === a[2];
  });
}

// ---------------------------------------------------------------- one page load
function boot(options) {
  options = options || {};
  const doc = { activeElement: null, listeners: {} };
  const root = new El(doc, input.tree, null);
  doc.documentElement = root.querySelector('html') || root;
  doc.body = root.querySelector('body');
  doc.activeElement = doc.body;
  doc.getElementById = id => root.querySelector('#' + id);
  doc.querySelector = sel => root.querySelector(sel);
  doc.querySelectorAll = sel => root.querySelectorAll(sel);
  doc.createElement = tag => new El(doc, { tag, attrs: {}, children: [] }, null);
  doc.addEventListener = (type, fn) => { (doc.listeners[type] = doc.listeners[type] || []).push(fn); };

  // Controllable clock: timers fire only when the test advances time.
  let now = 0, nextTimer = 1;
  const timers = new Map();
  const setTimeout = (fn, ms) => { const id = nextTimer++; timers.set(id, { at: now + (ms || 0), fn }); return id; };
  const clearTimeout = id => { timers.delete(id); };
  function advance(ms) {
    const until = now + ms;
    for (;;) {
      const due = [...timers.entries()].filter(([, t]) => t.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      timers.delete(due[0]);
      now = due[1].at;
      due[1].fn();
    }
    now = until;
  }

  // Location + history, recording every navigation the page asks for.
  let current = new URL(options.url || input.url);
  const navigations = [];
  const reloads = [];
  const location = {
    get href() { return current.href; },
    set href(value) { navigations.push(String(value)); },
    get origin() { return current.origin; },
    get pathname() { return current.pathname; },
    get search() { return current.search; },
    get hash() { return current.hash; },
    reload() { reloads.push(current.href); },
    assign(value) { navigations.push(String(value)); },
    replace(value) { navigations.push(String(value)); },
  };
  const replaced = [];
  const history = {
    state: null,
    replaceState(state, _title, url) { this.state = state; current = new URL(url, current); replaced.push(current.pathname + current.search); },
  };

  const store = new Map(Object.entries(options.storage || {}));
  const sessionStorage = options.storageThrows ? {
    getItem() { throw new Error('storage blocked'); },
    setItem() { throw new Error('storage blocked'); },
    removeItem() { throw new Error('storage blocked'); },
  } : {
    getItem: k => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: k => store.delete(k),
  };

  // MojoAuth stub: the completion is held until the test settles it.
  const completions = [], begins = [];
  let held = null;
  const MojoAuth = {
    init() {}, getTokenType: () => 'bearer', getRefreshToken: () => null,
    isPasskeySupported: () => false, isAuthenticated: () => false, logout() {},
    sanitizeMessage: text => text,
    getError(err) {
      if (!err) return 'We could not complete your request.';
      if (err.code === 429) return 'Too many attempts. Try again later.';
      if (err.error === 'geofence_blocked') return 'This service is not available in your country.';
      return err.error || err.message || 'We could not complete your request.';
    },
    completeOAuthLogin(provider) {
      completions.push({ provider, search: current.search });
      if (options.completionThrows) throw new Error('completion exploded');
      return new Promise((resolve, reject) => { held = { resolve, reject }; });
    },
    startOAuthLogin(provider) {
      begins.push(provider);
      return new Promise(() => {});
    },
    requestHandoffCode: () => new Promise(() => {}),
    loginWithMagicToken: () => new Promise(() => {}),
  };

  const windowListeners = {};
  const context = {
    document: doc, location, history, sessionStorage, MojoAuth,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
    URL, URLSearchParams, Promise, JSON, Math, Date, Array, Object, String, Number, Error,
    console,
    addEventListener: (type, fn) => { (windowListeners[type] = windowListeners[type] || []).push(fn); },
  };
  context.window = context;
  vm.createContext(context);
  for (const source of input.scripts) vm.runInContext(source, context);

  const $ = id => doc.getElementById(id);
  return {
    $, doc, advance, navigations, reloads, replaced, completions, begins, store,
    get url() { return current.pathname + current.search; },
    resolve(data) { held.resolve(data); return flush(); },
    reject(err) { held.reject(err); return flush(); },
    state: () => $('mat-pending').getAttribute('data-state'),
    title: () => $('mat-pending-title').textContent,
    sub: () => $('mat-pending-sub').textContent,
    pageshow: persisted => (windowListeners.pageshow || []).forEach(fn => fn({ persisted })),
  };
}

const flush = async () => { for (let i = 0; i < 5; i++) await new Promise(r => setImmediate(r)); };
const formHidden = page => page.doc.body.classList.contains('mat-oauth-pending')
  && page.$('mat-page').hasAttribute('inert') && page.$('mat-page').getAttribute('aria-hidden') === 'true';

// ---------------------------------------------------------------- scenarios
(async () => {
  // (a) held completion -> success -> redirect
  let p = boot({ storage: { oauth_provider: 'google' } });
  assert.equal(p.completions.length, 1, '(a) the callback must fire exactly one completion');
  assert.ok(p.completions[0].search.includes('code=cb-code-8579'), '(a) the completion must read the code at call time');
  assert.equal(p.completions[0].provider, 'google', '(a) the stored provider completes');
  assert.equal(p.url, '/auth?redirect=%2Fdashboard', '(a) code/state are stripped right after the call');
  assert.equal(p.store.has('oauth_provider'), false, '(a) the stored provider is consumed');
  assert.equal(p.state(), 'pending', '(a) the page lands pending');
  assert.ok(formHidden(p), '(a) the form stays hidden and inert while pending');
  assert.ok(p.sub().includes('Google'), '(a) the subtitle names the provider: ' + p.sub());
  p.advance(14999);
  assert.equal(p.state(), 'pending', '(a) still pending just under the slow threshold');
  await p.resolve({ user: { display_name: 'Ian Starnes' } });
  assert.equal(p.state(), 'success', '(a) a completed sign-in shows the success state');
  assert.equal(p.title(), 'Welcome back, Ian', '(a) greets the first name');
  assert.equal(p.$('mat-pending-initials').textContent, 'IS', '(a) shows the initials');
  assert.equal(p.$('mat-pending').getAttribute('data-named'), '1', '(a) named success');
  assert.ok(formHidden(p), '(a) the form never shows on success');
  p.advance(900);
  assert.equal(p.state(), 'redirect', '(a) moves on to the redirect state');
  assert.ok(/^Taking you to .+…$/.test(p.sub()), '(a) the redirect line names the destination: ' + p.sub());
  assert.deepEqual(p.navigations, ['https://auth.example.test/dashboard'], '(a) navigates to the preserved destination');
  assert.equal(p.completions.length, 1, '(a) still exactly one completion');

  // (b) refusal -> failure -> Try again begins a fresh provider redirect
  p = boot({ storage: { oauth_provider: 'github' } });
  await p.reject({ error: 'Your sign-in expired before it could be completed.', code: 400 });
  assert.equal(p.state(), 'failure', '(b) a refusal shows the failure state');
  assert.equal(p.title(), 'We couldn’t finish signing in', '(b) failure title');
  assert.equal(p.sub(), 'Your sign-in expired before it could be completed.', '(b) shows the server message');
  assert.equal(p.$('mat-pending-retry').hidden, false, '(b) Try again is offered');
  assert.equal(p.$('mat-pending-retry-label').textContent, 'Try again with GitHub', '(b) Try again names the provider');
  assert.equal(p.doc.activeElement, p.$('mat-pending-retry'), '(b) focus moves to Try again');
  assert.ok(formHidden(p), '(b) the form stays hidden on failure until asked for');
  p.$('mat-pending-retry').click();
  p.$('mat-pending-retry').click();
  assert.deepEqual(p.begins, ['github'], '(b) Try again starts one fresh begin flow');
  assert.equal(p.completions.length, 1, '(b) Try again never replays the completion');

  // (c) refusal -> use a different sign-in method restores the form in place
  p = boot({ storage: { oauth_provider: 'google' } });
  await p.reject({ error: 'That account is disabled.' });
  p.$('mat-pending-other').click();
  assert.equal(p.doc.body.classList.contains('mat-oauth-pending'), false, '(c) the pending body class is removed');
  assert.equal(p.$('mat-page').hasAttribute('inert'), false, '(c) the form is no longer inert');
  assert.equal(p.$('mat-page').hasAttribute('aria-hidden'), false, '(c) the form is exposed to screen readers');
  assert.equal(p.$('mat-overlay').hasAttribute('aria-hidden'), false, '(c) the overlay aria-hidden is restored');
  assert.equal(p.$('mat-message').textContent, 'That account is disabled.', '(c) the failure message stays visible');
  assert.ok(p.$('mat-message').classList.contains('is-error'), '(c) as an error');
  assert.ok(p.$('mat-page').contains(p.doc.activeElement) && p.doc.activeElement.tagName !== 'BODY',
            '(c) focus moves into the restored form');
  assert.equal(p.completions.length, 1, '(c) no second completion');

  // (d) slow -> late success still signs in
  p = boot({ storage: { oauth_provider: 'apple' } });
  p.advance(15000);
  assert.equal(p.state(), 'slow', '(d) the slow state appears at 15s');
  assert.equal(p.title(), 'Still signing you in', '(d) slow title');
  await p.resolve({ user: { display_name: 'Ian Starnes' } });
  assert.equal(p.state(), 'success', '(d) a late result still succeeds');
  p.advance(900);
  assert.equal(p.state(), 'redirect', '(d) and redirects');
  assert.equal(p.navigations.length, 1, '(d) one navigation');

  // (e) slow -> Start over abandons; a late result before unload is ignored
  p = boot({ storage: { oauth_provider: 'google' } });
  p.advance(15000);
  const restart = p.$('mat-pending-restart');
  assert.ok(restart.href.startsWith('/auth') && !restart.href.includes('code='), '(e) Start over opens a fresh sign-in page');
  restart.click();
  await p.resolve({ user: { display_name: 'Ian Starnes' } });
  p.advance(5000);
  assert.equal(p.state(), 'slow', '(e) an abandoned completion never changes the view');
  assert.deepEqual(p.navigations, [], '(e) an abandoned completion never navigates');
  assert.ok(formHidden(p), '(e) Start over never flashes the form');

  // (f) temporary password -> the set-password view
  p = boot({ storage: { oauth_provider: 'google' } });
  await p.resolve({ requires_password_change: true, forced_password_token: 'tp:fixture' });
  assert.equal(p.doc.body.classList.contains('mat-oauth-pending'), false, '(f) the pending view ends');
  assert.equal(p.$('mat-page').hasAttribute('inert'), false, '(f) the form is usable');
  assert.ok(p.$('view-set-password').classList.contains('is-active'), '(f) the set-password view opens');
  assert.deepEqual(p.navigations, [], '(f) never redirects a tokenless response');

  // (g) names
  for (const [data, label] of [[{ user: {} }, 'empty user'], [{ user: { display_name: '   ' } }, 'blank name'], [{}, 'no user']]) {
    p = boot({ storage: { oauth_provider: 'google' } });
    await p.resolve(data);
    assert.equal(p.title(), 'You’re signed in', `(g) ${label}: plain signed-in title`);
    assert.equal(p.$('mat-pending').getAttribute('data-named'), '0', `(g) ${label}: plain check, no initials`);
  }
  p = boot({ storage: { oauth_provider: 'google' } });
  await p.resolve({ user: { display_name: 'Ian Starnes' }, is_new_user: true });
  assert.equal(p.title(), 'Welcome, Ian', '(g) a brand-new account is welcomed, not welcomed back');
  p = boot({ storage: { oauth_provider: 'google' } });
  await p.resolve({ user: { display_name: 'Élodie de la Ünal' } });
  assert.equal(p.$('mat-pending-initials').textContent, 'ÉÜ', '(g) initials take the first and last words');

  // (h) settling just before the slow threshold is never overwritten by it
  p = boot({ storage: { oauth_provider: 'google' } });
  p.advance(14999);
  await p.resolve({ user: { display_name: 'Ian' } });
  p.advance(2000);
  assert.equal(p.state(), 'redirect', '(h) success at 14.999s stays on redirect');
  p = boot({ storage: { oauth_provider: 'google' } });
  p.advance(14999);
  await p.reject({ error: 'Nope.' });
  p.advance(2000);
  assert.equal(p.state(), 'failure', '(h) failure at 14.999s stays failure (no slow actions)');

  // (i) blocked sessionStorage still completes once
  p = boot({ storageThrows: true });
  assert.equal(p.completions.length, 1, '(i) blocked storage still fires one completion');
  assert.ok(p.sub().includes('your provider'), '(i) an unknown provider is described generically: ' + p.sub());

  // (j) a synchronous throw lands in failure, with the URL cleaned
  p = boot({ storage: { oauth_provider: 'google' }, completionThrows: true });
  await flush();
  assert.equal(p.state(), 'failure', '(j) a throwing completion shows the failure state');
  assert.equal(p.url, '/auth?redirect=%2Fdashboard', '(j) and still strips code/state');

  // (k) retrying cannot help a rate limit or a location block
  for (const err of [{ code: 429, error: 'Too many requests' }, { error: 'geofence_blocked', reason: 'country_not_allowed', code: 403 }]) {
    p = boot({ storage: { oauth_provider: 'google' } });
    await p.reject(err);
    assert.equal(p.state(), 'failure', '(k) failure state');
    assert.equal(p.$('mat-pending-retry').hidden, true, '(k) Try again is hidden for ' + JSON.stringify(err));
    assert.equal(p.doc.activeElement, p.$('mat-pending-other'), '(k) focus goes to the different-method link');
  }

  // (l) back from bfcache while still pending reloads instead of freezing
  p = boot({ storage: { oauth_provider: 'google' } });
  p.pageshow(true);
  assert.equal(p.reloads.length, 1, '(l) a persisted pageshow while pending reloads');
  p = boot({ storage: { oauth_provider: 'google' } });
  p.pageshow(false);
  assert.equal(p.reloads.length, 0, '(l) a normal pageshow does not');

  console.log('oauth callback pending state machine passed');
})().catch(error => { console.error(error && error.stack || error); process.exitCode = 1; });

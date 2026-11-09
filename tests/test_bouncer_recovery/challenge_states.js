// Run the real mojo-hosted-bouncer.js against a small fake DOM built from the
// RENDERED challenge page, with a controllable clock and a scripted fetch, and
// drive every state of the #9132 challenge: ready -> checking -> verified, the
// failure reasons, the review step and its receipt.
//
// stdin: {"script": <path>, "tree": <element tree>, "config": {...},
//         "tree_no_ticket": <element tree>, "config_no_ticket": {...}, "reference": "<ref>"}
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const SOURCE = fs.readFileSync(input.script, 'utf8');

// ---------------------------------------------------------------- fake DOM
function kebab(name) { return name.replace(/[A-Z]/g, c => '-' + c.toLowerCase()); }

class El {
  constructor(doc, node, parent) {
    this.ownerDocument = doc;
    this.tagName = (node.tag || 'div').toUpperCase();
    this.attrs = new Map(Object.entries(node.attrs || {}));
    this.parentNode = parent || null;
    this.listeners = {};
    this.value = this.attrs.get('value') || '';
    this._text = node.text || '';
    this.children = (node.children || []).map(child => new El(doc, child, this));
  }
  getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  removeAttribute(name) { this.attrs.delete(name); }
  hasAttribute(name) { return this.attrs.has(name); }
  get id() { return this.getAttribute('id') || ''; }
  get action() { return this.getAttribute('action') || ''; }
  get className() { return this.getAttribute('class') || ''; }
  set className(value) { this.setAttribute('class', value); }
  get hidden() { return this.attrs.has('hidden'); }
  set hidden(on) { if (on) this.attrs.set('hidden', ''); else this.attrs.delete('hidden'); }
  get disabled() { return this.attrs.has('disabled'); }
  set disabled(on) { if (on) this.attrs.set('disabled', ''); else this.attrs.delete('disabled'); }
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
  // No CSS here: an element is "rendered" when neither it nor an ancestor is [hidden].
  get offsetParent() {
    for (let n = this; n; n = n.parentNode) if (n.hidden) return null;
    return this.ownerDocument.body;
  }
  get nextSibling() {
    if (!this.parentNode) return null;
    const siblings = this.parentNode.children;
    return siblings[siblings.indexOf(this) + 1] || null;
  }
  insertBefore(node, ref) {
    if (ref === node) return node;
    if (node.parentNode) node.parentNode.children.splice(node.parentNode.children.indexOf(node), 1);
    node.parentNode = this;
    const at = ref ? this.children.indexOf(ref) : -1;
    if (at < 0) this.children.push(node); else this.children.splice(at, 0, node);
    return node;
  }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  dispatch(type, extra) {
    const event = Object.assign({ type, target: this, defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; }, stopPropagation() {} }, extra || {});
    (this.listeners[type] || []).forEach(fn => fn.call(this, event));
    return event;
  }
  click() { if (!this.disabled) return this.dispatch('click'); return null; }
  focus() { this.ownerDocument.activeElement = this; }
  *walk() { for (const child of this.children) { yield child; yield* child.walk(); } }
}

// ---------------------------------------------------------------- one page load
function boot(options) {
  options = options || {};
  const doc = { activeElement: null, listeners: {} };
  const root = new El(doc, options.tree || input.tree, null);
  const all = () => [...root.walk()];
  doc.body = all().find(el => el.tagName === 'BODY');
  doc.activeElement = doc.body;
  doc.getElementById = id => all().find(el => el.id === id) || null;
  doc.addEventListener = (type, fn) => { (doc.listeners[type] = doc.listeners[type] || []).push(fn); };

  let now = 1000000, nextTimer = 1;
  const timers = new Map();
  const schedule = (fn, ms, every) => { const id = nextTimer++; timers.set(id, { at: now + (ms || 0), fn, every }); return id; };
  function advance(ms) {
    const until = now + ms;
    for (;;) {
      const due = [...timers.entries()].filter(([, t]) => t.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      now = due[1].at;
      if (due[1].every) due[1].at = now + due[1].every; else timers.delete(due[0]);
      due[1].fn();
    }
    now = until;
  }

  const navigations = [], reloads = [];
  const location = {
    search: options.search || '',
    reload() { reloads.push('reload'); },
    replace(value) { navigations.push(String(value)); },
  };

  // Scripted fetch: each call is recorded and answered by options.respond.
  const calls = [];
  const held = [];
  function fetch(url, init) {
    const body = JSON.parse(init.body);
    calls.push({ url, body, signal: init.signal });
    const answer = (options.respond || (() => ({ status: 200, json: { status: true, data: {} } })))(url, body, calls.length);
    if (answer === 'hold') return new Promise((resolve, reject) => held.push({ resolve: a => resolve(response(a)), reject }));
    if (answer instanceof Error) return Promise.reject(answer);
    return Promise.resolve(response(answer));
  }
  function response(a) {
    const headers = Object.assign({ 'Content-Type': 'application/json' }, a.headers || {});
    return { status: a.status, ok: a.status >= 200 && a.status < 300,
      headers: { get: name => headers[name] ?? null }, json: async () => a.json };
  }

  const copied = [], selections = [];
  const context = {
    document: doc, location, fetch, console, URLSearchParams,
    Date: { now: () => now },
    setTimeout: (fn, ms) => schedule(fn, ms), clearTimeout: id => timers.delete(id),
    setInterval: (fn, ms) => schedule(fn, ms, ms), clearInterval: id => timers.delete(id),
    AbortController: class { constructor() { this.signal = { aborted: false }; } abort() { this.signal.aborted = true; } },
    crypto: { getRandomValues: arr => { for (let i = 0; i < arr.length; i++) arr[i] = (i * 37 + 11) & 255; return arr; } },
    sessionStorage: { setItem() {}, getItem: () => null, removeItem() {} },
    navigator: options.clipboard === undefined
      ? { clipboard: { writeText: text => { copied.push(text); return Promise.resolve(); } } }
      : options.clipboard,
    getSelection: () => ({ selectAllChildren: node => selections.push(node.id) }),
  };
  context.window = context;
  context.self = context;
  context.top = options.framed ? {} : context;
  vm.createContext(context);
  vm.runInContext(SOURCE, context);
  const config = Object.assign({}, options.config || input.config);
  vm.runInContext('MojoHostedBouncer.mount(document, ' + JSON.stringify(config) + ')', context);

  const $ = id => doc.getElementById(id);
  return {
    $, doc, advance, navigations, reloads, calls, held, copied, selections,
    state: () => $('mbg-root').getAttribute('data-state'),
    heading: () => $('mbg-heading').textContent,
    status: () => $('mbg-status').textContent,
    label: () => $('mbg-continue-label').textContent,
    ops: () => calls.filter(c => c.body.hosted_gate).map(c => c.body.hosted_gate.operation),
    actionsOrder: () => $('mbg-continue').parentNode.children.map(el => el.id),
  };
}

const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(r => setImmediate(r)); };
const ok = data => ({ status: 200, json: { status: true, data } });
const PASS = (url, body) => ok(body.hosted_gate.operation === 'confirm'
  ? { decision: 'allow', next_action: 'allow' } : { decision: 'allow', next_action: 'check_cookie' });
const REF = input.reference;

const COPY = {
  cookies: ['We couldn’t confirm this visit', 'Your browser didn’t keep this site’s security cookie. This can happen in private browsing or inside another app’s browser.'],
  limited: ['Too many tries', 'Please wait a moment before trying again.'],
  expired: ['This check expired', 'It took too long to finish. Try again to start a new check.'],
  restart: ['This check changed', 'Try again to start a new check.'],
  unavailable: ['Checks are briefly unavailable', 'Something on our side isn’t responding. Please try again shortly.'],
  operator: ['We couldn’t confirm this visit', 'Ask for a review, or contact the site operator for help.'],
  invalid: ['Something went wrong', 'We couldn’t read the response. Start over to begin a new check.'],
};

function assertRestart(p, promoted, tag) {
  const restart = p.$('mbg-restart');
  assert.equal(restart.hidden, false, `${tag}: Restart stays reachable in every failed state`);
  assert.equal(restart.classList.contains('is-primary'), promoted, `${tag}: Restart is ${promoted ? '' : 'not '}the primary`);
  assert.equal(restart.textContent, promoted ? 'Try again' : 'Start over', `${tag}: Restart label`);
  const order = p.actionsOrder();
  if (promoted) assert.equal(order[order.indexOf('mbg-continue') + 1], 'mbg-restart', `${tag}: a promoted Restart takes the primary slot: ${order}`);
  else assert.equal(order[order.indexOf('mbg-review-open') + 1], 'mbg-restart', `${tag}: a quiet Start over sits under Ask for a review: ${order}`);
}

(async () => {
  // (a) ready
  let p = boot();
  assert.equal(p.state(), 'ready', '(a) mounts in the ready state');
  assert.equal(p.$('mbg-root').getAttribute('data-js'), '1', '(a) mount marks the page as script-driven');
  assert.equal(p.heading(), 'One quick check', '(a) heading');
  assert.equal(p.status(), 'Select Continue to proceed securely.', '(a) status');
  assert.equal(p.label(), 'Continue', '(a) label');
  assert.equal(p.$('mbg-continue').disabled, false, '(a) Continue is enabled once mounted');
  assert.equal(p.$('mbg-restart').hidden, true, '(a) no Restart before a failure');

  // (b) check -> check_cookie -> confirm -> verified, once
  for (const [pageType, line] of [['login', 'Continuing to sign in…'], ['registration', 'Continuing to create your account…'], ['public_message', 'Continuing…']]) {
    p = boot({ respond: PASS, config: Object.assign({}, input.config, { page_type: pageType }) });
    p.$('mbg-continue').focus();
    p.$('mbg-continue').click();
    assert.equal(p.state(), 'checking', `(b) ${pageType}: checking while the assessment runs`);
    assert.equal(p.label(), 'Checking', '(b) the button says Checking');
    assert.equal(p.heading(), 'Checking…', '(b) heading while checking');
    assert.equal(p.$('mbg-continue').disabled, true, '(b) Continue is disabled while checking');
    await flush();
    assert.equal(p.state(), 'verified', `(b) ${pageType}: verified after the cookie is confirmed`);
    assert.equal(p.heading(), 'Verified', '(b) heading');
    assert.equal(p.status(), line, `(b) ${pageType}: the continuing line names the destination`);
    assert.deepEqual(p.ops(), ['check', 'confirm'], '(b) Continue confirms the cookie before navigating');
    assert.deepEqual(p.navigations, [input.config.redirect_url], '(b) exactly one location.replace');
    const gate = p.calls[0].body.signals.gate_challenge;
    assert.equal(gate.had_activation, true, '(b) activation is reported');
    assert.equal(gate.honeypot_filled, false, '(b) an empty honeypot reports false');
    assert.equal(p.calls[0].body.signals.behavior.activation_count, 1, '(b) one activation counted');
  }

  // (c) a filled honeypot is reported
  p = boot({ respond: PASS });
  p.$('mbg-hp-field').value = 'bot';
  p.$('mbg-continue').click();
  await flush();
  assert.equal(p.calls[0].body.signals.gate_challenge.honeypot_filled, true, '(c) a filled honeypot reports true');

  // (d) every reason's copy, render-time and from a submit outcome
  for (const [action, reason, promoted] of [['recovery', 'cookies', true], ['recovery', 'expired', true], ['recovery', 'restart', true],
    ['recovery', 'operator', true], ['error', 'unavailable', false], ['error', 'invalid', false], ['decoy', '', true], ['recovery', 'mystery', true]]) {
    const key = COPY[reason] ? reason : 'operator';
    p = boot({ config: Object.assign({}, input.config, { next_action: action, reason }) });
    const tag = `(d) render ${action}/${reason}`;
    assert.equal(p.state(), 'failed', `${tag}: failed state`);
    assert.equal(p.heading(), COPY[key][0], `${tag}: heading`);
    assert.equal(p.status(), COPY[key][1], `${tag}: one plain sentence`);
    assert.ok(!p.status().includes('Reference'), `${tag}: the sentence never appends the reference`);
    assert.equal(p.$('mbg-reference').textContent, REF, `${tag}: the reference value, once`);
    assert.equal(p.$('mbg-reference-row').hidden, false, `${tag}: the reference row is shown`);
    assert.equal(p.$('mbg-continue').hidden, promoted, `${tag}: Try again hidden exactly when it is today`);
    assert.equal(p.label(), 'Try again', `${tag}: the primary reads Try again`);
    assertRestart(p, promoted, tag);
    assert.notEqual(p.state(), 'review', `${tag}: the review never starts open`);

    // A decoy answer is a plain 200; recovery/error arrive as refusals.
    p = boot({ respond: () => ({ status: action === 'decoy' ? 200 : action === 'error' ? 400 : 409,
      json: { status: action === 'decoy', data: { next_action: action, reason, reference: '77-' + 'e'.repeat(24) } } }) });
    p.$('mbg-continue').focus();
    p.$('mbg-continue').click();
    await flush();
    assert.equal(p.heading(), COPY[key][0], `(d) outcome ${action}/${reason}: heading`);
    assert.equal(p.$('mbg-reference').textContent, '77-' + 'e'.repeat(24), `(d) outcome ${action}/${reason}: the outcome's reference replaces the render's`);
    assert.ok(!p.status().includes('Reference'), `(d) outcome ${action}/${reason}: no reference in the sentence`);
    if (promoted) assert.equal(p.doc.activeElement, p.$('mbg-heading'), `(d)/(o) ${action}/${reason}: focus moves to the heading when Try again disappears`);
    else assert.equal(p.doc.activeElement, p.$('mbg-continue'), `(d) ${action}/${reason}: focus stays on a visible, enabled Try again`);
  }

  // (e) cookies inside a frame offers the top-level escape; not framed, it stays hidden
  p = boot({ framed: true, config: Object.assign({}, input.config, { next_action: 'recovery', reason: 'cookies' }) });
  assert.equal(p.$('mbg-top-level').hidden, false, '(e) framed cookies failure shows the new-tab link');
  p = boot({ config: Object.assign({}, input.config, { next_action: 'recovery', reason: 'cookies' }) });
  assert.equal(p.$('mbg-top-level').hidden, true, '(e) the new-tab link stays hidden outside a frame');

  // (f) 429 with Retry-After 42: countdown on the button only, then Try again
  p = boot({ respond: () => ({ status: 429, headers: { 'Retry-After': '42' }, json: {} }) });
  p.$('mbg-continue').focus();
  p.$('mbg-continue').click();
  await flush();
  assert.equal(p.heading(), 'Too many tries', '(f) limited heading');
  assert.equal(p.status(), COPY.limited[1], '(f) the status line does not tick');
  assert.equal(p.label(), 'Try again in 0:42', '(f) the countdown starts at the Retry-After');
  assert.equal(p.$('mbg-continue').disabled, true, '(f) Try again is disabled while limited');
  assert.equal(p.doc.activeElement, p.$('mbg-heading'), '(o) a disabled Try again hands focus to the heading');
  assertRestart(p, false, '(f)');
  p.advance(1000);
  assert.equal(p.label(), 'Try again in 0:41', '(f) the countdown ticks');
  p.$('mbg-continue').dispatch('click');
  assert.equal(p.calls.length, 1, '(f) a click before the wait ends sends nothing');
  p.advance(41000);
  assert.equal(p.label(), 'Try again', '(f) the countdown ends on Try again');
  assert.equal(p.$('mbg-continue').disabled, false, '(f) and re-enables it');
  assert.equal(p.status(), COPY.limited[1], '(f) the status never changed during the countdown');

  // (g) interrupted connection once, then three times
  p = boot({ respond: () => new Error('offline') });
  p.$('mbg-continue').click();
  await flush();
  assert.equal(p.heading(), 'The connection was interrupted', '(g) network heading');
  assert.equal(p.status(), 'Check your connection and try again.', '(g) network sentence');
  assert.equal(p.$('mbg-continue').hidden, false, '(g) Try again stays after one interruption');
  assertRestart(p, false, '(g) x1');
  p.$('mbg-continue').click(); await flush();
  p.$('mbg-continue').click(); await flush();
  assert.equal(p.heading(), 'We couldn’t complete this check', '(g) three interruptions stop the check');
  assert.equal(p.status(), 'Try again to start a new check, or ask for a review.', '(g) stopped sentence');
  assert.equal(p.$('mbg-continue').hidden, true, '(g) no endless Try again');
  assertRestart(p, true, '(g) x3');
  p.$('mbg-continue').dispatch('click');
  assert.equal(p.calls.length, 3, '(g) a stopped check sends nothing more');

  // (h) retired slider/cooldown configs map to the Continue check
  for (const action of ['slider', 'cooldown']) {
    p = boot({ config: Object.assign({}, input.config, { next_action: action }) });
    assert.equal(p.state(), 'ready', `(h) ${action} maps to ready`);
    assert.equal(p.label(), 'Continue', `(h) ${action} shows Continue`);
    assert.equal(p.$('mbg-continue').disabled, false, `(h) ${action}: Continue is usable`);
  }

  // (i) help / back and failed -> review -> back
  p = boot();
  const event = p.$('mbg-help').click();
  assert.equal(event.defaultPrevented, true, '(i) the help link is handled in place');
  assert.equal(p.state(), 'review', '(i) Get help opens the review step');
  assert.equal(p.doc.activeElement, p.$('mbg-review-email'), '(i) focus moves to the email field');
  p.$('mbg-review-back').click();
  assert.equal(p.state(), 'ready', '(i) Back returns to ready');
  assert.equal(p.doc.activeElement, p.$('mbg-help'), '(i) focus returns to Get help');
  p = boot({ config: Object.assign({}, input.config, { next_action: 'recovery', reason: 'cookies' }) });
  p.$('mbg-review-open').click();
  assert.equal(p.state(), 'review', '(i) Ask for a review opens the step');
  p.$('mbg-review-back').click();
  assert.equal(p.state(), 'failed', '(i) Back returns to the failure');
  assert.equal(p.doc.activeElement, p.$('mbg-review-open'), '(i) focus returns to Ask for a review');

  // (j) review success: one POST to the independent intake, the server's receipt
  const RECEIPT = 'Your request is recorded for review. This does not grant access.';
  p = boot({ respond: url => (url === '/api/auth/bouncer/recovery' ? 'hold' : PASS),
    config: Object.assign({}, input.config, { next_action: 'recovery', reason: 'cookies' }) });
  p.$('mbg-review-open').click();
  p.$('mbg-review-email').value = 'visitor@example.test';
  p.$('mbg-review-note').value = 'Signing in from the app';
  p.$('mbg-review-form').dispatch('submit');
  p.$('mbg-review-form').dispatch('submit');
  assert.equal(p.calls.length, 1, '(j) a double submit sends one POST');
  const posted = p.calls[0];
  assert.equal(posted.url, '/api/auth/bouncer/recovery', '(j) the review posts to the intake endpoint');
  assert.deepEqual(Object.keys(posted.body).sort(), ['email', 'note', 'reported_failure', 'review_ticket'], '(j) the review body is exactly the intake contract');
  assert.equal(posted.body.review_ticket, input.config.review_ticket, '(j) the ticket');
  assert.equal(posted.body.reported_failure, 'cookies', '(j) the failure being reported');
  assert.equal(posted.body.hosted_gate, undefined, '(j) no hosted_gate: review is not an assessment');
  p.held[0].resolve(ok({ reference: 'review-9132', review_state: 'pending', message: RECEIPT }));
  await flush();
  assert.equal(p.state(), 'sent', '(j) the sent state');
  assert.equal(p.heading(), 'Request sent', '(j) heading');
  assert.equal(p.status(), RECEIPT, '(j) the status is the server message, nothing more');
  assert.ok(p.$('mbg-review-status').textContent.includes(RECEIPT) && p.$('mbg-review-status').textContent.includes('review-9132'),
    '(j) the receipt is also written to the review status');
  assert.equal(p.$('mbg-reference').textContent, 'review-9132', '(j) the receipt reference is shown');
  assert.equal(p.doc.activeElement, p.$('mbg-heading'), '(j) focus moves to the heading');
  assert.equal(p.$('mbg-heading').getAttribute('aria-describedby'), 'mbg-status mbg-reference-row', '(j) the heading is described by the receipt');
  assert.equal(p.$('mbg-review-submit').disabled, true, '(j) a sent request cannot be resent');

  // (k) review refusals keep the step open and the button usable
  for (const [answer, expected] of [[{ status: 429, json: {} }, 'Please wait a few minutes before sending another request.'],
    [{ status: 400, json: { status: false, data: { message: 'Enter a valid email.' } } }, 'Enter a valid email.'],
    [Object.assign(new Error('aborted'), { name: 'AbortError' }), 'We couldn’t confirm receipt. You can safely try this request again.']]) {
    p = boot({ respond: () => answer, config: Object.assign({}, input.config, { next_action: 'recovery', reason: 'operator' }) });
    p.$('mbg-review-open').click();
    p.$('mbg-review-email').value = 'visitor@example.test';
    p.$('mbg-review-form').dispatch('submit');
    await flush();
    assert.equal(p.state(), 'review', `(k) ${expected}: stays on the review step`);
    assert.equal(p.$('mbg-review-status').textContent, expected, '(k) explains the refusal');
    assert.equal(p.$('mbg-review-submit').disabled, false, '(k) the request can be sent again');
  }

  // (l) no review ticket: the form is hidden and the step explains why
  p = boot({ tree: input.tree_no_ticket, config: input.config_no_ticket });
  assert.equal(p.$('mbg-review-form').hidden, true, '(l) no ticket hides the form');
  p.$('mbg-review-open').click();
  assert.equal(p.state(), 'review', '(l) Ask for a review still opens the step');
  assert.equal(p.doc.activeElement, p.$('mbg-review-title'), '(l) focus goes to the review title');

  // (m) copy: clipboard, refused clipboard, no clipboard
  p = boot({ config: Object.assign({}, input.config, { next_action: 'recovery', reason: 'cookies' }) });
  p.$('mbg-copy').click();
  await flush();
  assert.deepEqual(p.copied, [REF], '(m) the reference is written to the clipboard');
  assert.equal(p.$('mbg-copy-status').textContent, 'Reference copied.', '(m) copy is confirmed');
  for (const clipboard of [{ clipboard: { writeText: () => Promise.reject(new Error('denied')) } }, {}]) {
    p = boot({ clipboard, config: Object.assign({}, input.config, { next_action: 'recovery', reason: 'cookies' }) });
    p.$('mbg-copy').click();
    await flush();
    assert.deepEqual(p.selections, ['mbg-reference'], '(m) without a usable clipboard the reference is selected');
    assert.ok(p.$('mbg-copy-status').textContent.startsWith('Reference selected'), '(m) with a hint');
  }

  // (n) Restart and Back to start each reload once
  p = boot({ config: Object.assign({}, input.config, { next_action: 'error', reason: 'invalid' }) });
  p.$('mbg-restart').click();
  assert.equal(p.reloads.length, 1, '(n) Start over reloads once (invalid can restart at once)');
  p = boot();
  p.$('mbg-sent-restart').click();
  assert.equal(p.reloads.length, 1, '(n) Back to start reloads once');

  console.log('hosted challenge states passed');
})().catch(error => { console.error(error && error.stack || error); process.exitCode = 1; });

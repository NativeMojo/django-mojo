// Execute the real hosted-page magic-link handler with a controllable clock.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const template = fs.readFileSync(process.argv[2], 'utf8');
const source = template.slice(template.lastIndexOf('    // ---- Magic-link sent state'), template.lastIndexOf('})();'));
let now = 1000, pending, calls = [], interval;
const elements = {};
for (const id of ['magic-intro', 'form-magic', 'magic-email', 'btn-magic', 'magic-sent', 'magic-sent-email', 'btn-magic-resend', 'btn-magic-change']) {
  elements[id] = { hidden: false, disabled: false, value: '', textContent: '', focus() {}, querySelector() { return this; } };
}
elements['magic-sent'].hidden = true;
const handlers = {};
const storage = new Map();
const context = {
  $: id => elements[id], cfg: { groupUuid: 'camactive' },
  m: { setLoading(b, active) { b.disabled = active; }, hideMessage() {}, showMessage() {} },
  onForm(id, fn) { handlers[id] = fn; }, on(id, event, fn) { handlers[id] = fn; },
  MojoAuth: { sendMagicLink(email) { calls.push(email); return new Promise((resolve, reject) => { pending = { resolve, reject }; }); }, getError() { return 'Try again'; } },
  Date: { now: () => now }, Math, JSON, Number,
  sessionStorage: { getItem: k => storage.get(k) || null, setItem: (k, v) => storage.set(k, v) },
  setInterval(fn) { interval = fn; return 1; }, clearInterval() {},
};
const submit = () => handlers['form-magic']({ preventDefault() {} });
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  vm.runInNewContext(source, context);
  elements['magic-email'].value = 'ian@example.com';
  submit(); submit();
  assert.equal(calls.length, 1, 'duplicate submits while sending must post once');
  pending.resolve(); await flush();
  assert.equal(elements['form-magic'].hidden, true, 'successful send replaces the form');
  assert.equal(elements['magic-sent'].hidden, false, 'sent state is visible');
  assert.equal(elements['magic-sent-email'].textContent, 'ian@example.com', 'sent address is text');
  assert.equal(elements['btn-magic-resend'].disabled, true, 'resend starts disabled');
  handlers['btn-magic-resend']();
  assert.equal(calls.length, 1, 'cooldown guards programmatic resend');
  // Reinitialization models a reload; session storage preserves the wait.
  vm.runInNewContext(source, context);
  assert.equal(elements['btn-magic-resend'].disabled, true, 'reload preserves cooldown');
  now += 60000; interval();
  assert.equal(elements['btn-magic-resend'].disabled, false, 'resend unlocks after a minute');
  handlers['btn-magic-resend']();
  assert.equal(calls.length, 2, 'one resend is allowed after the wait');
  pending.reject(new Error('network')); await flush();
  assert.equal(elements['btn-magic-resend'].disabled, false, 'failed resend can be retried');
  handlers['btn-magic-change']();
  assert.equal(elements['form-magic'].hidden, false, 'change email restores form');
  elements['magic-email'].value = '<new>@example.com';
  submit(); pending.resolve(); await flush();
  assert.equal(elements['magic-sent-email'].textContent, '<new>@example.com', 'changed email is plain text');
  assert.equal(calls[2], '<new>@example.com', 'changed email is submitted');
  console.log('magic-link form regression passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

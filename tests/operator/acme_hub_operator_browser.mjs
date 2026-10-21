// Real-browser retained UI checks. No TLS bypass, assertion fixture, or green
// process evidence is supplied by the default production-origin mode.
// Usage: node this-file --url https://guardian.example/_mojo/acme-upgrade/
//   --puppeteer /absolute/path/to/puppeteer-core.js --chrome /path/to/chrome
//   --out /tmp/operator-browser
// Explicit --source-fixture permits loopback HTTP for development rendering
// only, using the actual nonroot fixture HTTP handler. It cannot qualify TLS,
// WebAuthn, root authority, retained transport, or stopped-target operation.
import assert from 'node:assert/strict';
import {mkdtemp, mkdir, readFile, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
const args = process.argv.slice(2);
function option(name, fallback = null) { const index = args.indexOf('--' + name); return index < 0 ? fallback : args[index + 1]; }
const fixture = args.includes('--source-fixture');
const url = new URL(option('url', 'https://guardian.example/_mojo/acme-upgrade/'));
assert.equal(url.pathname, '/_mojo/acme-upgrade/', 'browser target must use the fixed retained entry');
assert.equal(url.hash, '', 'browser target may not contain a token or fragment');
if (fixture) assert.ok(url.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname), 'source fixture must be explicitly loopback HTTP');
else assert.ok(url.protocol === 'https:' && !url.port, 'real guardian requires dedicated HTTPS on standard port');
const modulePath = option('puppeteer', process.env.MOJO_OPERATOR_PUPPETEER);
assert.ok(modulePath, 'supply an installed puppeteer-core module path; this test adds no dependency');
const puppeteer = (await import(pathToFileURL(resolve(modulePath)))).default;
const out = resolve(option('out', '/tmp/mojo-acme-operator-browser'));
await mkdir(out, {recursive: true});
const profile = await mkdtemp(resolve(tmpdir(), 'mojo-operator-browser-'));
const browser = await puppeteer.launch({executablePath: option('chrome', process.env.MOJO_OPERATOR_CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'), headless: true, userDataDir: profile});
const errors = [], calls = [], screenshots = [], checks = [];
const page = await browser.newPage();
page.on('pageerror', error => errors.push(error.message));
page.on('request', request => { if (new URL(request.url()).pathname.startsWith('/_mojo/acme-upgrade/api/')) calls.push({method: request.method(), path: new URL(request.url()).pathname}); });
const record = (name, detail) => checks.push({name, detail});
async function screenshot(name, fullPage = false) { const path = resolve(out, name + '.png'); await page.screenshot({path, fullPage}); const bytes = await readFile(path); screenshots.push({path, sha256: createHash('sha256').update(bytes).digest('hex')}); }
try {
  await page.setViewport({width: 1440, height: 1050});
  await page.emulateMediaFeatures([{name: 'prefers-color-scheme', value: 'light'}]);
  await page.goto(url.href, {waitUntil: 'networkidle0'});
  if (fixture) {
    await page.evaluate(async () => {
      const {GuardianClient} = await import('./api.js');
      const {mountWizard} = await import('./wizard.js');
      const fixtureClient = new GuardianClient({origin: 'https://source-fixture.invalid', fetcher: (target, options) => fetch(new URL(target).pathname, options)});
      globalThis.__operatorBrowserFixture = mountWizard({client: fixtureClient});
      const banner = document.createElement('p'); banner.className = 'notice warning'; banner.id = 'source-fixture-warning'; banner.textContent = 'DEVELOPMENT SOURCE FIXTURE · Loopback HTTP · No root, TLS, passkey, provider, or stopped-target qualification'; document.querySelector('main').prepend(banner);
    });
  }
  await page.waitForFunction(() => document.querySelector('#observation').textContent.includes('Source: retained guardian') && !document.querySelector('#observation').textContent.includes('Awaiting readback'));
  await page.waitForFunction(() => document.querySelectorAll('#actions [data-action]').length === 12);
  const observed = await page.evaluate(() => ({title: document.title, text: document.body.innerText, actions: [...document.querySelectorAll('#actions [data-action]')].map(b => ({name: b.dataset.action, disabled: b.disabled})), origin: document.getElementById('guardian-origin').textContent, inputs: document.querySelectorAll('input, textarea').length, links: [...document.querySelectorAll('a[target="_blank"]')].map(a => ({url: a.href, rel: a.rel}))}));
  assert.match(observed.title, /Guarded ACME upgrade/, 'faithful page metadata must be present');
  assert.equal(observed.inputs, 0, 'unavailable credential selection must not turn into raw IDs or proof paste');
  assert.ok(observed.links.every(l => l.rel.includes('noopener') && l.rel.includes('noreferrer') && new URL(l.url).protocol === 'https:'), 'independent destinations must be safe top-level links');
  record('real handler readback', {origin: observed.origin, actions: observed.actions, source: fixture ? 'nonroot development source fixture, no operational authority' : 'actual guardian HTTPS origin'});
  await screenshot('desktop-light');
  await page.click('#theme'); await screenshot('desktop-dark');
  await page.setViewport({width: 390, height: 844}); await screenshot('narrow-dark');
  const overflow = await page.evaluate(() => ({viewport: innerWidth, document: document.documentElement.scrollWidth, dialogs: [...document.querySelectorAll('dialog[open]')].map(d => ({width: d.clientWidth, scroll: d.scrollWidth}))}));
  assert.ok(overflow.document <= overflow.viewport, '390px layout must have no horizontal document overflow');
  assert.ok(overflow.dialogs.every(d => d.scroll <= d.width), 'dialog content must fit the narrow viewport');
  record('390px layout', overflow);
  await page.click('#theme'); await screenshot('narrow-light');
  assert.equal(await page.$eval('#phase-disclosure', d => d.open), false, 'narrow stage navigation must begin with a compact current-stage summary');
  await page.click('#phase-disclosure>summary');
  assert.equal(await page.$eval('#phases', list => list.children.length), 7, 'expanded stage navigation must expose the complete list');
  assert.ok(await page.$eval('#phases', list => list.scrollWidth <= list.clientWidth), 'expanded stages must fit instead of silently clipping');
  await screenshot('narrow-light-stages');
  await page.click('#phase-disclosure>summary');
  record('complete narrow stage navigation', 'Current phase and step count expand to all seven readable stages');
  await page.keyboard.press('Tab');
  assert.ok(await page.evaluate(() => document.activeElement !== document.body), 'keyboard focus must reach an operable control');
  record('keyboard focus', await page.evaluate(() => ({element: document.activeElement.tagName, label: document.activeElement.textContent.trim() || document.activeElement.getAttribute('aria-label')})));
  const beforeRefresh = calls.filter(c => c.method === 'POST').length;
  await page.click('#refresh');
  await page.waitForFunction(() => document.querySelector('#announcement').textContent.length > 0);
  assert.equal(calls.filter(c => c.method === 'POST').length, beforeRefresh, 'refresh must never replay an operation');
  record('explicit readback', 'Refresh sent no POST');
  if (fixture) {
    assert.ok(observed.actions.length === 12 && observed.actions.every(a => a.disabled), 'nonroot source fixture may never enable operational authority');
    const beforeSignIn = calls.length;
    await page.click('#sign-in');
    await page.waitForFunction(() => !document.querySelector('#notice').hidden && document.querySelector('#notice').textContent.includes('not connected'));
    const attempted = calls.slice(beforeSignIn).filter(c => c.method === 'POST');
    assert.deepEqual(attempted.map(c => c.path), ['/_mojo/acme-upgrade/api/session/options'], 'unavailable sign-in must stop at real options refusal, with no assertion/action');
    record('real unavailable capability', {result: 'bounded refusal, zero assertion or operational sends', calls: attempted});
    await screenshot('narrow-sign-in-unavailable');
  }
  assert.equal(errors.length, 0, 'browser must have no uncaught JavaScript errors: ' + errors.join('; '));
  const evidence = {mode: fixture ? 'development_source_fixture' : 'actual_guardian_https', target: url.href, checks, calls, errors, screenshots, limitations: fixture ? ['This is actual nonroot source HTTP framing and local rendering only.', 'The explicit browser fixture mount does not qualify production HTTPS/origin/session/WebAuthn/root/transport/provider/Linux behavior.', 'No enabled operation, activation, recovery, or stopped-target success is claimed.', '390px headless Chrome is not a physical mobile device.'] : ['This driver records only the real states exercised; no unavailable operation is treated as success.', 'Stopped-target/root/Linux/provider qualification requires the separate final combined-source operator acceptance.', '390px headless Chrome is not a physical mobile device.']};
  await writeFile(resolve(out, 'evidence.json'), JSON.stringify(evidence, null, 2) + '\n');
  console.log(JSON.stringify({result: 'PASS', mode: evidence.mode, checks: checks.length, screenshots: screenshots.length, evidence: resolve(out, 'evidence.json')}));
} finally { await browser.close(); await rm(profile, {recursive: true, force: true}); }

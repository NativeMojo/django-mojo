// Development contracts only. Synthetic observations below are not Linux proof.
import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';
import {webcrypto} from 'node:crypto';
import {ACTIONS, GuardianClient, GuardianError, actionEligibility, credentialOptions, entryOperation, remainingSeconds, serializeAssertion, validateStatus, validDestination} from '../../mojo/deploy/acme_hub_operator_ui/api.js';
import {phaseFor, presentation, reasonText} from '../../mojo/deploy/acme_hub_operator_ui/wizard.js';

const origin = 'https://guardian.example';
const op = '11111111-1111-4111-8111-111111111111';
const review = '22222222-2222-4222-8222-222222222222';
const actor = '33333333-3333-4333-8333-333333333333';
const permit = '44444444-4444-4444-8444-444444444444';
const at = Date.parse('2026-10-20T18:00:00Z');
const iso = n => new Date(n).toISOString();
function fixture({enabled = [], state = 'prepared', authenticated = true, revision = 8} = {}) {
  return {schema_version: 1, installation: {uuid: actor, state: 'enrolled', enrolled: true}, operation: {id: op, state, phase: 'prepare', revision, outcome: 'pending', review, deadline_at: iso(at + 600000)}, session: {authenticated, actor: authenticated ? {account_uuid: actor} : null, fresh_until: null, csrf_token: 'development-csrf-not-authority'}, clock: {server_time: iso(at), deadline_at: iso(at + 600000)}, actions: ACTIONS.map(name => ({name, enabled: enabled.includes(name), reason: enabled.includes(name) ? null : 'dependent_integration_unavailable', requires_reauth: !['inspect', 'verify'].includes(name)})), facts: [{id: 'declared_fixture', label: 'Development fixture', state: 'pending', source: 'declared_test_fixture', observed_at: iso(at), items: [{key: 'not production evidence', value: '<script>fixture</script>', classification: 'fixture'}]}], nodes: [], bootstrap: {state: 'unavailable', candidate_id: null, independent_approval_url: null, error_code: 'database_enrollment_unavailable'}, producer_identity: {state: 'unavailable', missing_facts: ['producer_profile_unavailable']}, producer_coverage: {state: 'unavailable', missing_facts: ['producer_coverage_unavailable']}, provider_witness: {state: 'unavailable', missing_facts: ['provider_witness_unavailable']}, destinations: {operator_wizard: null, independent_approval: null, provider_console: null, history_console: null}};
}
const frame = (data, revision = data?.operation?.revision ?? 0) => ({version: 1, ok: true, data, error: null, request_id: 'a'.repeat(32), revision, observed_at: iso(at)});
const denied = (code, revision = 8) => ({version: 1, ok: false, data: null, error: {code, retryable: false}, request_id: 'b'.repeat(32), revision, observed_at: iso(at)});
const response = (data, status = 200) => new Response(JSON.stringify(data), {status, headers: {'Content-Type': 'application/json; charset=utf-8'}});
function harness(steps, overrides = {}) {
  const calls = [];
  const client = new GuardianClient({origin, now: () => at, crypto: webcrypto, ...overrides, fetcher: async (url, options) => {
    calls.push({url, options, body: options.body === undefined ? null : JSON.parse(options.body)});
    const next = steps.shift();
    assert.ok(next, `unexpected HTTP call ${options.method} ${url}`);
    if (next instanceof Error) throw next;
    if (typeof next === 'function') return next(calls.at(-1));
    return response(next.body ?? next, next.status ?? 200);
  }});
  return {client, calls, steps};
}
const binary = s => new TextEncoder().encode(s).buffer;
function assertion(assertionOrigin = origin) {
  return {id: 'AQID', rawId: Uint8Array.of(1, 2, 3).buffer, type: 'public-key', authenticatorAttachment: 'platform', getClientExtensionResults: () => ({}), response: {clientDataJSON: binary(JSON.stringify({type: 'webauthn.get', challenge: 'AQID', origin: assertionOrigin, crossOrigin: false})), authenticatorData: binary('auth'), signature: binary('signature'), userHandle: null}};
}
const options = () => ({request: permit, publicKey: {challenge: 'AQID', rpId: 'guardian.example', userVerification: 'required', allowCredentials: [{type: 'public-key', id: 'AQID', transports: ['internal']}], timeout: 60000}, expires_at: iso(at + 60000)});
const expected = {operation: op, revision: 8, review};

test('dedicated guardian entry rejects app origins, ports, callback parameters, fragments and ambiguous operation links', () => {
  assert.equal(entryOperation(new URL(origin + '/_mojo/acme-upgrade/?operation=' + op)), op, 'canonical operation link should be accepted');
  for (const value of ['http://guardian.example/_mojo/acme-upgrade/', origin + ':8443/_mojo/acme-upgrade/', origin + '/other/', origin + '/_mojo/acme-upgrade/?return=app', origin + '/_mojo/acme-upgrade/#token', origin + '/_mojo/acme-upgrade/?operation=' + op + '&operation=' + op]) assert.throws(() => entryOperation(new URL(value)), GuardianError, `unsafe entry accepted: ${value}`);
});
test('destinations are HTTPS-only and the guardian link is exact origin, fixed path and sole opaque operation', () => {
  assert.equal(validDestination({label: 'Open guarded operator wizard', url: origin + '/_mojo/acme-upgrade/?operation=' + op}, 'operator_wizard', origin).url, origin + '/_mojo/acme-upgrade/?operation=' + op, 'exact guardian destination must survive');
  for (const url of ['javascript:alert(1)', 'https://app.example/_mojo/acme-upgrade/', origin + '/_mojo/acme-upgrade/?token=secret', origin + '/_mojo/acme-upgrade/#x']) assert.throws(() => validDestination({label: 'Open guarded operator wizard', url}, 'operator_wizard', origin), GuardianError, 'unsafe destination must be rejected');
});
test('strict status accepts declared source facts but rejects fabricated authority, action drift and malformed projections', () => {
  assert.equal(validateStatus(fixture(), origin).facts[0].items[0].classification, 'fixture', 'fixture label must remain visible');
  for (const mutate of [s => s.actions[0].enabled = true, s => s.actions.reverse(), s => s.session.actor = null, s => s.operation.revision = 2 ** 60, s => s.nodes.push({id: 'fake'}), s => s.extra = 'proof', s => s.facts[0].items[0].classification = 'proved']) {
    const value = fixture(); mutate(value); assert.throws(() => validateStatus(value, origin), GuardianError, 'malformed status must fail closed');
  }
});
test('fresh readback is the only source of controls; profile, fixture and empty roster never enable an action', async () => {
  const {client, calls} = harness([frame(fixture())]); await client.refresh();
  assert.ok(ACTIONS.every(name => actionEligibility(client.snapshot, name, at) !== null), 'no server-disabled action may be inferred from facts');
  assert.equal(presentation(client.snapshot).actions.filter(a => a.reason === null).length, 0, 'all actions must be unavailable');
  assert.equal(calls.length, 1, 'status read must not trigger any operation POST');
});
test('deadline uses earliest guardian deadline, local elapsed time never extends it, and stale evidence loses authority', () => {
  const status = fixture({enabled: ['activate']}); status.clock.deadline_at = iso(at + 20000);
  const snapshot = {status, receivedAt: at, busy: false, uncertain: false};
  assert.equal(remainingSeconds(status, at, at + 15000), 5, 'earliest guardian deadline must control');
  assert.equal(actionEligibility(snapshot, 'activate', at + 20000), 'request_expired', 'expired action must be refused without waiting for the server');
  assert.equal(remainingSeconds(status, at, at - 10000), 20, 'clock reversal must not extend the guardian window');
  assert.equal(actionEligibility({...snapshot, status: fixture({enabled: ['activate']})}, 'activate', at + 31000), 'fresh_readback_required', 'stale status must lose authority');
});
test('repeated old clock snapshots cannot extend a completion window during readback', async () => {
  let now = at;
  const status = fixture({enabled: ['activate']}); status.clock.deadline_at = iso(at + 20000);
  const {client} = harness([frame(status), frame(status)], {now: () => now});
  await client.refresh(); now += 15000; await client.refresh();
  assert.equal(remainingSeconds(client.snapshot.status, client.snapshot.receivedAt, now, client.snapshot.clockFloor), 5, 're-reading the same server clock must preserve elapsed time');
});
for (const code of ['not_authenticated', 'operator_not_authorized', 'dependent_integration_unavailable', 'generation_changed', 'commit_unknown']) test(`failed read ${code} discards old authority and sends zero operation POSTs`, async () => {
  const status = fixture({enabled: ['activate', 'restore']});
  const {client, calls} = harness([frame(status), {body: denied(code), status: code === 'not_authenticated' ? 401 : code === 'operator_not_authorized' ? 403 : code === 'generation_changed' ? 409 : 503}]);
  await client.refresh(); assert.equal(actionEligibility(client.snapshot, 'activate', at), null, 'precondition should enable the selected fixture action');
  await client.refresh(); assert.equal(client.snapshot.status, null, 'failed read must discard previous rows and authority');
  await assert.rejects(client.run('activate', expected), GuardianError, 'even a previously reviewed confirmation must refuse after failed read');
  assert.equal(calls.filter(c => c.options.method === 'POST').length, 0, 'read failure must produce zero operation sends');
});
test('sensitive action performs fresh exact review-bound authentication and one operation send', async () => {
  const status = fixture({enabled: ['activate']}); const result = fixture({state: 'active'}); result.operation.revision = 9;
  const {client, calls} = harness([frame(status), frame(options(), 8), frame({status, authorization: permit, expires_at: iso(at + 60000)}, 8), frame(result, 9)], {credentials: {get: async () => assertion()}});
  await client.refresh(); await client.run('activate', expected);
  assert.equal(calls.length, 4, 'exactly status, options, verify, and one action should be sent');
  assert.deepEqual(calls[1].body, {purpose: 'action', action: 'activate', operation: op, expected_revision: 8, review}, 'authentication must bind the exact action tuple');
  assert.equal(calls[2].body.credential.response.userHandle, null, 'WebAuthn optional user handle must be preserved');
  assert.deepEqual(Object.keys(calls[3].body).sort(), ['authorization', 'expected_revision', 'operation', 'replay_key'], 'operation body must contain only frozen selectors and guardian permit');
  assert.equal(calls[3].body.authorization, permit, 'one same-session permit must be spent');
  assert.equal(calls[3].options.headers['X-Mojo-Acme-CSRF'], status.session.csrf_token, 'CSRF must come from current guardian session');
  assert.equal(calls[3].options.credentials, 'same-origin', 'application credentials must not be bridged');
  assert.equal(calls[3].options.redirect, 'error', 'a redirect may not carry the action to another origin');
  assert.equal(client.snapshot.revision, 9, 'resulting root revision must replace the reviewed state');
});
test('root drift during fresh authentication refuses the final action, even after successful WebAuthn', async () => {
  const status = fixture({enabled: ['activate']}); const drift = fixture({enabled: ['activate'], revision: 9});
  const {client, calls} = harness([frame(status), frame(options(), 8), frame({status: drift, authorization: permit, expires_at: iso(at + 60000)}, 9)], {credentials: {get: async () => assertion()}});
  await client.refresh(); await client.run('activate', expected);
  assert.equal(client.snapshot.status, null, 'changed review must discard controls');
  assert.equal(client.snapshot.error.code, 'generation_changed', 'drift must require new review');
  assert.equal(calls.filter(c => c.url.endsWith('/activate')).length, 0, 'no activate POST may follow tuple drift');
});
test('an unknown action response is never replayed; explicit refresh is read-only', async () => {
  const status = fixture({enabled: ['inspect']}); const {client, calls} = harness([frame(status), new Error('connection lost'), frame(fixture({state: 'commit_unknown'}))]);
  await client.refresh(); await client.run('inspect', expected);
  assert.equal(client.snapshot.error.unknown, true, 'transport loss after POST must retain unknown outcome');
  assert.equal(client.snapshot.status, null, 'unknown outcome must remove current authority');
  await assert.rejects(client.run('inspect', expected), GuardianError, 'repeat confirmation must refuse without new readback');
  await client.refresh(); assert.equal(calls.filter(c => c.options.method === 'POST').length, 1, 'readback must not replay the submitted POST');
  assert.equal(client.snapshot.status.operation.state, 'commit_unknown', 'unknown root state must remain unknown');
});
test('stale confirmation, missing root review, expired permit, and incompatible passkey produce no operation sends', async () => {
  for (const mode of ['stale', 'missing_review', 'expired', 'foreign_origin']) {
    const status = fixture({enabled: ['activate']}); if (mode === 'missing_review') status.operation.review = null;
    const steps = [frame(status)];
    if (mode === 'expired' || mode === 'foreign_origin') steps.push(frame(options(), 8));
    if (mode === 'expired') steps.push(frame({status, authorization: permit, expires_at: iso(at - 1)}, 8));
    const {client, calls} = harness(steps, {credentials: {get: async () => assertion(mode === 'foreign_origin' ? 'https://app.example' : origin)}});
    await client.refresh(); try { await client.run('activate', {...expected, ...(mode === 'stale' ? {revision: 7} : {})}); } catch (error) { assert.ok(error instanceof GuardianError, 'local refusal should be a bounded guardian error'); }
    assert.equal(calls.filter(c => c.url.endsWith('/activate')).length, 0, `${mode} must produce zero action sends`);
    if (mode === 'foreign_origin') assert.equal(calls.filter(c => c.url.endsWith('/session/verify')).length, 0, 'foreign-origin assertion must not be submitted');
  }
});
test('WebAuthn requires the pinned RP, existing allowed credential, UV, no extensions, and exact top-level guardian origin', () => {
  const converted = credentialOptions(options(), 'guardian.example', at);
  assert.deepEqual([...converted.challenge], [1, 2, 3], 'base64url challenge must become binary');
  for (const mutate of [o => o.publicKey.rpId = 'app.example', o => o.publicKey.userVerification = 'preferred', o => o.publicKey.allowCredentials = [], o => o.publicKey.extensions = {appid: 'https://app.example'}]) { const value = options(); mutate(value); assert.throws(() => credentialOptions(value, 'guardian.example', at), GuardianError, 'unbound options must fail closed'); }
  const wrong = assertion(); wrong.getClientExtensionResults = () => ({appid: true});
  assert.throws(() => serializeAssertion(wrong, origin), GuardianError, 'extension escape may not be relayed');
});
test('initial sign-in uses existing-credential get and cannot request registration or an operation permit', async () => {
  const status = fixture({authenticated: false}); const signed = fixture();
  const {client, calls} = harness([frame(status), frame(options(), 8), frame({status: signed, authorization: null, expires_at: iso(at + 60000)}, 8)], {credentials: {get: async () => assertion()}});
  await client.refresh(); await client.signIn();
  assert.deepEqual(calls[1].body, {purpose: 'sign_in', action: null, operation: null, expected_revision: 8, review: null}, 'sign-in must carry no operational authorization selector');
  assert.equal(client.snapshot.status.session.authenticated, true, 'only verified service readback should mark sign-in complete');
  assert.equal(calls.filter(c => ACTIONS.some(a => new URL(c.url).pathname === '/_mojo/acme-upgrade/api/' + a)).length, 0, 'sign-in must not submit an operation');
});
test('malformed, oversized and wrong-revision success responses discard authority', async () => {
  for (const next of [() => new Response('{', {headers: {'Content-Type': 'application/json'}}), () => new Response('x'.repeat(65537), {headers: {'Content-Type': 'application/json'}}), frame(fixture(), 9)]) {
    const {client} = harness([frame(fixture({enabled: ['activate']})), next]); await client.refresh(); await client.refresh();
    assert.equal(client.snapshot.status, null, 'unverifiable success must discard authority');
    assert.equal(client.snapshot.uncertain, true, 'a new readback is required');
  }
});
test('operation deep-link mismatch disables all human controls and unknown states never read as success', () => {
  const snapshot = {status: fixture({enabled: ACTIONS, state: 'commit_unknown'}), revision: 8, receivedAt: at, busy: false, uncertain: false};
  const view = presentation(snapshot, actor, at);
  assert.ok(view.actions.every(a => a.reason === 'operation_mismatch'), 'different operation context must disable every control');
  assert.match(presentation(snapshot, null, at).title, /unknown/, 'unknown state must be explicit');
  assert.equal(phaseFor(null), null, 'no current phase may be invented before readback');
  assert.match(reasonText('held_recovery_required'), /retained/, 'recovery guidance must preserve protections');
});
test('markup uses only retained assets and safe text rendering; no JSON, credential, command, proof checkbox or third-party resource inputs', async () => {
  const root = new URL('../../mojo/deploy/acme_hub_operator_ui/', import.meta.url);
  const html = await readFile(new URL('index.html', root), 'utf8'); const js = await readFile(new URL('wizard.js', root), 'utf8');
  assert.match(html, /<title>Guarded ACME upgrade/, 'metadata title must describe the actual surface');
  assert.doesNotMatch(html, /<input|<textarea|https?:\/\//, 'current unavailable selector UI must not substitute paste fields or external assets');
  assert.doesNotMatch(js, /innerHTML|insertAdjacentHTML|localStorage|sessionStorage|postMessage|serviceWorker\.register/, 'untrusted source facts must not execute or persist assertion material');
  assert.match(js, /rel = 'noopener noreferrer'/, 'independent destinations must not retain an opener');
});

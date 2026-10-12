// Exercise the shipped formatter; no server, credentials, or network required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const context = { DOMException };
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
const auth = context.MojoAuth;
let checks = 0;
function check(error, expected, label) {
    assert.equal(auth.getError(error), expected, label);
    checks += 1;
    assert.deepEqual(await contactDecline(null, { error: 'contact_declined' }),
        [{ text: fallback, type: 'error' }], 'Contact transport decline codes receive useful fallback copy');
    checks += 1;
}

check({ error: 'geofence_blocked', code: 403, reason: 'region_not_allowed',
    detail: 'Service is not available in your region.' },
    'This service is not available in your region. If you think this is a mistake, please contact support.',
    'A real geofence refusal must explain the restriction instead of showing its code');
check({ error: 'geofence_blocked', reason: 'country_not_allowed' },
    'This service is not available in your country. If you think this is a mistake, please contact support.', 'country restriction');
for (const [reason, connection] of [['vpn_detected', 'VPN'], ['proxy_detected', 'proxy'], ['tor_detected', 'Tor connection']]) {
    check({ error: 'geofence_blocked', reason },
        'This service does not allow access through a ' + connection + '. Please use your regular internet connection and try again.', reason);
}
check({ error: 'geofence_blocked', reason: 'lookup_failed' },
    'We could not verify your location. Please try again in a moment.', 'temporary location failure');
for (const reason of ['private_ip', 'datacenter_detected']) {
    check({ error: 'geofence_blocked', reason },
        'This service requires a regular home or mobile internet connection. Please switch networks and try again.', reason);
}
for (const reason of ['no_rules_strict', 'rule_invalid']) {
    check({ error: 'geofence_blocked', reason, detail: 'Internal configuration detail' },
        'Access is currently unavailable. Please contact support for help.', reason);
}
for (const reason of ['unknown_reason', '__proto__', 'constructor']) {
    check({ error: 'geofence_blocked', reason, detail: 'Internal configuration detail' },
        'We could not allow access from your current location or connection. Please contact support for help.', 'safe geofence fallback: ' + reason);
}
const fallback = 'We could not complete your request. Please try again or contact support for help.';
check({ error: 'account_declined', detail: 'Please contact support to review your account.' },
    'Please contact support to review your account.', 'API explanation beats a decline code');
check({ error: 'account_declined', message: 'Please verify your email before continuing.' },
    'Please verify your email before continuing.', 'API message remains readable');
check({ error: 'account_declined', errors: [{ message: 'Please check the code and try again.' }] },
    'Please check the code and try again.', 'nested explanation beats a decline code');
for (const error of [{ error: 'account_declined' }, { error: 'ACCOUNT_DECLINED' }, 'account_declined', { detail: {}, error: 'declined_code' }, null]) {
    check(error, fallback, 'unknown or malformed decline uses helpful copy');
}
check({ error: 'Invalid credentials' }, 'Invalid credentials', 'credential privacy message is preserved');
check(new Error('Check your sign-in details. See: https://example.com/spec'),
    'Check your sign-in details.', 'URL sanitization is preserved');
check({ code: 429, retry_after: 61, detail: 'Do not bypass the wait.' },
    'Too many attempts. Try again in 2 minutes.', 'rate-limit wait is preserved');
check(new DOMException('Raw browser spec https://example.com/spec', 'NotAllowedError'),
    'Passkey sign-in was cancelled, timed out, or no passkey was found on this device.', 'passkey guidance is preserved');

async function contactDecline(body, transportError) {
    let submit;
    const messages = [];
    const form = {
        dataset: { kind: 'contact' }, querySelectorAll: () => [], reset: () => {},
        addEventListener: (event, callback) => { submit = callback; }
    };
    const browser = {
        window: {
            _matConfig: {},
            _mat: { hideMessage: () => {}, setLoading: () => {},
                showMessage: (text, type) => messages.push({ text, type }) }
        },
        document: { getElementById: id => id === 'form-contact' ? form : {} },
        fetch: () => transportError ? Promise.reject(transportError) :
            Promise.resolve({ ok: false, status: 403, json: () => Promise.resolve(body) })
    };
    vm.createContext(browser);
    vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), browser);
    const template = path.join(path.dirname(process.argv[2]), '../../templates/account/contact.html');
    const script = fs.readFileSync(template, 'utf8').match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];
    vm.runInContext(script, browser);
    submit({ preventDefault: () => {} });
    await new Promise(resolve => setImmediate(resolve));
    return messages;
}

(async () => {
    const body = { error: 'contact_declined', detail: 'Please check your message and try again.' };
    assert.deepEqual(await contactDecline(body), [{ text: body.detail, type: 'error' }],
        'The real contact handler must display the API explanation');
    checks += 1;
    assert.deepEqual(await contactDecline(null, { error: 'contact_declined', message: 'Please reload and try again.' }),
        [{ text: 'Please reload and try again.', type: 'error' }], 'Contact transport failures use the same formatter');
    checks += 1;
    process.stdout.write(JSON.stringify({ passed: checks, failed: 0 }) + '\n');
})().catch(error => { process.stderr.write(String(error.stack || error)); process.exitCode = 1; });

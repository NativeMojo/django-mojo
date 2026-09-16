// Run the real client in a browser-shaped VM, recording its outgoing JSON.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const calls = [];
const stored = new Map();
const bytes = new Uint8Array([1]).buffer;
const context = {
    console, atob, btoa, Uint8Array,
    localStorage: {
        getItem: key => stored.get(key) || null,
        setItem: (key, value) => stored.set(key, value),
        removeItem: key => stored.delete(key)
    },
    window: {
        PublicKeyCredential: function () {},
        MojoBouncer: { getToken: () => 'test-bouncer', getDuid: () => 'test-device' }
    },
    navigator: { credentials: { get: async () => ({
        id: 'credential', rawId: bytes, type: 'public-key',
        response: { clientDataJSON: bytes, authenticatorData: bytes,
                    signature: bytes, userHandle: null }
    }) } },
    fetch: async (url, options) => {
        calls.push({ url, body: JSON.parse(options.body) });
        const data = url.endsWith('/begin')
            ? { challenge_id: 'test-challenge', publicKey: { challenge: 'AQ', allowCredentials: [] } }
            : { access_token: 'synthetic-access', refresh_token: 'synthetic-refresh' };
        return { ok: true, json: async () => ({ status: true, data }) };
    }
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
(async () => {
    const auth = context.MojoAuth;
    auth.init({ baseURL: 'https://auth.example.test' });
    const options = process.argv[4] === 'brand' ? { group_uuid: 'test-brand-4622' } : undefined;
    if (process.argv[3] === 'sms') await auth.verifySmsLogin('test-user', '123456', options);
    else if (process.argv[3] === 'named') await auth.loginWithPasskey('test-user', options);
    else await auth.loginWithPasskeyDiscoverable(options);
    assert.equal(stored.get('access_token'), 'synthetic-access');
    assert.equal(stored.get('refresh_token'), 'synthetic-refresh');
    process.stdout.write(JSON.stringify(calls));
})().catch(error => { console.error(error); process.exitCode = 1; });

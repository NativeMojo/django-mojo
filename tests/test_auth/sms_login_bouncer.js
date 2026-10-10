// Run the real client in a browser-shaped VM and print what startSmsLogin posts.
const fs = require('node:fs');
const vm = require('node:vm');

function run(provider) {
    const posts = [];
    const context = {
        console, atob, btoa, Uint8Array, Promise,
        localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        window: {},
        navigator: {},
        fetch: (url, options) => {
            posts.push({ url: url, body: JSON.parse(options.body) });
            return Promise.resolve({ ok: true, json: () => Promise.resolve({ status: true }) });
        }
    };
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
    const auth = context.MojoAuth;
    auth.init({ baseURL: 'https://api.example.test', bouncerTokenProvider: provider });
    return auth.startSmsLogin('+15555550100', { group_uuid: 'g-1' }).then(() => posts);
}

(async () => {
    const asked = [];
    const withProvider = await run((purpose) => { asked.push(purpose); return Promise.resolve('fresh-token'); });
    const withoutProvider = await run(null);
    process.stdout.write(JSON.stringify({
        asked: asked,
        with_provider: withProvider,
        without_provider: withoutProvider
    }));
})().catch((err) => { process.stderr.write(String(err && err.stack || err)); process.exit(1); });

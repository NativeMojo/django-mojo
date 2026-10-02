// Run the real client in a browser-shaped VM and print what it shows for a 429.
const fs = require('node:fs');
const vm = require('node:vm');
const context = {
    console, atob, btoa, Uint8Array,
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    window: {},
    navigator: {}
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
const auth = context.MojoAuth;
const limited = wait => ({ error: 'Rate limit exceeded', code: 429, status: false, retry_after: wait });
process.stdout.write(JSON.stringify({
    wait_840: auth.getError(limited(840)),
    wait_61: auth.getError(limited(61)),
    wait_30: auth.getError(limited(30)),
    no_wait: auth.getError({ error: 'Rate limit exceeded', code: 429, status: false }),
    other: auth.getError({ error: 'Invalid code', code: 400, status: false })
}));

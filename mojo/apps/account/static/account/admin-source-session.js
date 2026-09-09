/* Public coordination only. Frozen portal-mojo source-session contract v1. */
(function () {
  'use strict';
  if (window.MojoAdminSourceSession) return;
  const LOCK = 'mojo:admin-source-session:v1';
  const KEY = 'mojo:admin-source-generation:v1';
  let channel;
  function read(store = localStorage) {
    const raw = store.getItem(KEY);
    if (!raw) return {version: 1, generation: 'initial', state: 'active'};
    const value = JSON.parse(raw);
    if (Object.keys(value).sort().join(',') !== 'generation,state,version'
        || value.version !== 1 || !['active', 'revoked'].includes(value.state)
        || !/^(initial|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i.test(value.generation)) {
      throw new Error('Invalid Admin coordination state. Sign in again.');
    }
    return value;
  }
  const same = (a, b) => a.generation === b.generation && a.state === b.state;
  function bind(value) { sessionStorage.setItem(KEY, JSON.stringify(value)); }
  function requireSupport() {
    if (!navigator.locks?.request || !window.BroadcastChannel || !crypto.randomUUID) {
      throw new Error('This browser cannot coordinate secure Admin sessions. Use a supported browser.');
    }
    // Same-value writes prove both stores are usable without overwriting a
    // concurrent generation or inventing an initial authoritative record.
    for (const store of [localStorage, sessionStorage]) {
      const probe = `${KEY}:probe:${crypto.randomUUID()}`;
      store.setItem(probe, '1'); store.removeItem(probe);
    }
    if (!channel) {
      channel = new BroadcastChannel(LOCK);
      channel.onmessage = checkWakeup;
      window.addEventListener('storage', checkWakeup);
      window.addEventListener('pageshow', checkWakeup);
      window.addEventListener('focus', checkWakeup);
    }
  }
  function checkWakeup() {
    try { assertActive(); } catch (_) {
      window.dispatchEvent(new CustomEvent('mojo-admin:source-revoked'));
    }
  }
  function assertActive() {
    const current = read();
    if (current.state !== 'active' || !same(current, read(sessionStorage))) {
      throw new Error('Your Admin session was signed out. Sign in again.');
    }
    return current;
  }
  function start() {
    requireSupport();
    if (!sessionStorage.getItem(KEY)) bind(read());
    return assertActive();
  }
  function publish(value) {
    localStorage.setItem(KEY, JSON.stringify(value));
    channel.postMessage({type: 'generation', ...value});
  }
  async function explicitLogin(action) {
    requireSupport();
    const captured = read();
    const result = await action();
    if (!result?.access_token) return result;
    await navigator.locks.request(LOCK, async () => {
      if (!same(captured, read())) {
        if (window.MojoAuth?.getToken() === result.access_token) window.MojoAuth.logout();
        throw new Error('Sign-in was superseded by sign-out. Try again.');
      }
      const next = {version: 1, generation: crypto.randomUUID(), state: 'active'};
      publish(next); bind(next);
    });
    return result;
  }
  async function issue(auth) {
    start();
    return navigator.locks.request(LOCK, async () => {
      const before = assertActive();
      let token = auth.getToken();
      if ((!token || (auth.getTokenPayload?.()?.exp || 0) <= Date.now() / 1000) && auth.getRefreshToken?.()) {
        await auth.refreshToken();
        assertActive(); token = auth.getToken();
      }
      if (!same(before, assertActive()) || !token || auth.getTokenType() !== 'bearer') {
        throw new Error('An interactive sign-in is required.');
      }
      const response = await fetch('/api/account/admin/session', {method: 'POST', credentials: 'same-origin',
        headers: {Authorization: `Bearer ${token}`, 'Content-Type': 'application/json'}, body: '{}'});
      const payload = await response.json();
      if (!same(before, assertActive()) || auth.getToken() !== token) throw new Error('Sign-in changed during verification.');
      const data = payload.data;
      if (!response.ok || payload.status !== true || !data
          || !/^\/(?:[A-Za-z0-9_-]+\/)+$/.test(data.path)
          || !Number.isInteger(data.source_session_expires_in) || data.source_session_expires_in <= 0
          || !Number.isInteger(data.source_session_expires_at) || data.source_session_expires_at <= Date.now() / 1000) {
        throw new Error('Your Admin session could not be verified. Sign in again.');
      }
      return data;
    });
  }
  async function revoke(path, clearCredentials) {
    requireSupport();
    if (!/^\/(?:[A-Za-z0-9_-]+\/)+$/.test(path)) throw new Error('Invalid Admin path.');
    const tombstone = {version: 1, generation: crypto.randomUUID(), state: 'revoked'};
    publish(tombstone); bind(tombstone); clearCredentials();
    return navigator.locks.request(LOCK, async () => {
      if (same(tombstone, read())) localStorage.setItem(KEY, JSON.stringify(tombstone));
      const response = await fetch(`${path}_session`, {method: 'DELETE', credentials: 'same-origin'});
      await response.text();
      if (!response.ok) throw new Error('Sign-out is incomplete. Retry sign out.');
    });
  }
  window.MojoAdminSourceSession = Object.freeze({start, issue, revoke, explicitLogin, read});
})();

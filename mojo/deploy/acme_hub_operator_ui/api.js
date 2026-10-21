// Retained Guardian HTTP v1. All authority remains in the guardian.
export const PREFIX = '/_mojo/acme-upgrade/';
export const ACTIONS = Object.freeze(['inspect', 'enroll-retained', 'review-maintenance', 'establish-holds', 'witness', 'prepare', 'activate', 'cancel', 'reconcile', 'retry', 'restore', 'verify']);
export const READ_ACTIONS = new Set(['inspect', 'verify']);
export const STATES = new Set(['unavailable', 'bootstrap_only', 'enrollment_pending', 'enrolled', 'planning', 'holding', 'prepared', 'reviewing', 'activating', 'active', 'restoring', 'verified', 'held_recovery_required', 'commit_unknown', 'cancelled']);
export const ERRORS = new Set(['invalid_request', 'invalid_frame', 'origin_refused', 'csrf_refused', 'not_authenticated', 'reauth_required', 'operator_not_authorized', 'generation_changed', 'replay_refused', 'request_expired', 'session_unavailable', 'runtime_unavailable', 'profile_unavailable', 'operator_service_unavailable', 'independent_approval_unavailable', 'database_enrollment_unavailable', 'coordinator_unavailable', 'provider_witness_unavailable', 'producer_profile_unavailable', 'producer_identity_mismatch', 'producer_coverage_unavailable', 'application_coverage_unavailable', 'transport_unavailable', 'node_unavailable', 'floor_unavailable', 'configuration_busy', 'held_recovery_required', 'commit_unknown', 'qualification_environment_unavailable', 'dependent_integration_unavailable']);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const B64 = /^[A-Za-z0-9_-]+$/;
const own = (v, keys) => v !== null && typeof v === 'object' && !Array.isArray(v) && Object.keys(v).length === keys.length && keys.every(k => Object.hasOwn(v, k));
const text = (v, max = 256) => typeof v === 'string' && v.length > 0 && v.length <= max && !/[\u0000-\u001f\u007f]/.test(v);
const stamp = v => typeof v === 'string' && v.endsWith('Z') && Number.isFinite(Date.parse(v));
const optionalStamp = v => v === null || stamp(v);
const revision = v => Number.isSafeInteger(v) && v >= 0;
const require = (value, code = 'invalid_response') => { if (!value) throw new GuardianError(code); };
export const browserNow = () => globalThis.performance?.timeOrigin ? performance.timeOrigin + performance.now() : Date.now();

export class GuardianError extends Error {
  constructor(code, unknown = false) { super(code); this.name = 'GuardianError'; this.code = code; this.unknown = unknown; }
}

export function entryOperation(location) {
  require(location.protocol === 'https:' && (!location.port || location.port === '443'), 'guardian_origin_required');
  require(location.pathname === PREFIX && !location.hash, 'invalid_entry');
  const params = new URLSearchParams(location.search);
  require([...params.keys()].every(k => k === 'operation') && params.getAll('operation').length <= 1, 'invalid_entry');
  const operation = params.get('operation');
  require(operation === null || UUID.test(operation), 'invalid_entry');
  return operation;
}

export function validDestination(value, kind, origin) {
  if (value === null) return null;
  require(own(value, ['label', 'url']) && text(value.label, 100) && text(value.url, 2048));
  let url;
  try { url = new URL(value.url); } catch { throw new GuardianError('invalid_response'); }
  require(url.protocol === 'https:' && !url.username && !url.password && (!url.port || url.port === '443'));
  if (kind === 'operator_wizard') {
    require(url.origin === origin && url.pathname === PREFIX && !url.hash && value.label === 'Open guarded operator wizard');
    const operation = entryOperation(url);
    require(value.url === origin + PREFIX + (operation === null ? '' : '?operation=' + operation));
    return {label: value.label, url: value.url};
  }
  // No approved owner/repository or provider role policy is projected yet.
  // HTTPS alone cannot make an arbitrary response into a trusted destination.
  return null;
}

function factGroups(value) {
  require(Array.isArray(value) && value.length <= 64);
  for (const group of value) {
    require(own(group, ['id', 'label', 'state', 'source', 'observed_at', 'items']));
    require(text(group.id, 128) && text(group.label, 128) && text(group.state, 64) && text(group.source, 128) && optionalStamp(group.observed_at));
    require(Array.isArray(group.items) && group.items.length <= 64);
    for (const item of group.items) {
      require(own(item, ['key', 'value', 'classification']) && text(item.key, 128));
      require(['expected', 'observed', 'unavailable', 'fixture'].includes(item.classification));
      require(item.value === null || typeof item.value === 'boolean' || (typeof item.value === 'number' && Number.isFinite(item.value)) || (typeof item.value === 'string' && item.value.length <= 2048 && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(item.value)));
    }
  }
}

export function validateStatus(value, origin) {
  require(own(value, ['schema_version', 'installation', 'operation', 'session', 'clock', 'actions', 'facts', 'nodes', 'bootstrap', 'producer_identity', 'producer_coverage', 'provider_witness', 'destinations']) && value.schema_version === 1);
  const {installation: i, operation: o, session: s, clock: c, bootstrap: b} = value;
  require(own(i, ['uuid', 'state', 'enrolled']) && (i.uuid === null || UUID.test(i.uuid)) && STATES.has(i.state) && typeof i.enrolled === 'boolean');
  if (o !== null) {
    require(own(o, ['id', 'state', 'phase', 'revision', 'outcome', 'review', 'deadline_at']));
    require(UUID.test(o.id) && STATES.has(o.state) && text(o.phase, 128) && revision(o.revision) && ['not_started', 'pending', 'committed', 'unknown', 'refused'].includes(o.outcome) && (o.review === null || UUID.test(o.review)) && optionalStamp(o.deadline_at));
  }
  require(own(s, ['authenticated', 'actor', 'fresh_until', 'csrf_token']) && typeof s.authenticated === 'boolean' && optionalStamp(s.fresh_until));
  require(s.actor === null || (own(s.actor, ['account_uuid']) && UUID.test(s.actor.account_uuid)));
  require(!s.authenticated || s.actor !== null);
  require(s.csrf_token === null || text(s.csrf_token, 512));
  require(own(c, ['server_time', 'deadline_at']) && stamp(c.server_time) && optionalStamp(c.deadline_at));
  require(Array.isArray(value.actions) && value.actions.length === ACTIONS.length);
  value.actions.forEach((a, index) => require(own(a, ['name', 'enabled', 'reason', 'requires_reauth']) && a.name === ACTIONS[index] && typeof a.enabled === 'boolean' && (a.reason === null || ERRORS.has(a.reason)) && a.requires_reauth === !READ_ACTIONS.has(a.name) && (!a.enabled || a.reason === null)));
  factGroups(value.facts);
  require(Array.isArray(value.nodes) && value.nodes.length <= 64 && new Set(value.nodes.map(n => n?.id)).size === value.nodes.length);
  value.nodes.forEach(n => {
    require(own(n, ['id', 'state', 'missing_facts', 'observed_at', 'facts']) && text(n.id, 128) && text(n.state, 64) && optionalStamp(n.observed_at));
    require(Array.isArray(n.missing_facts) && n.missing_facts.length <= 64 && n.missing_facts.every(v => text(v, 128)));
    factGroups(n.facts);
  });
  require(own(b, ['state', 'candidate_id', 'independent_approval_url', 'error_code']) && text(b.state, 64) && (b.candidate_id === null || UUID.test(b.candidate_id)) && (b.error_code === null || ERRORS.has(b.error_code)));
  if (b.independent_approval_url !== null) validDestination({label: 'Open independent approval', url: b.independent_approval_url}, 'independent_approval', origin);
  for (const key of ['producer_identity', 'producer_coverage', 'provider_witness']) {
    const projection = value[key];
    require(projection && typeof projection === 'object' && !Array.isArray(projection) && text(projection.state, 64));
    if (projection.missing_facts !== undefined) require(Array.isArray(projection.missing_facts) && projection.missing_facts.length <= 64 && projection.missing_facts.every(v => text(v, 128)));
  }
  require(own(value.destinations, ['operator_wizard', 'independent_approval', 'provider_console', 'history_console']));
  Object.entries(value.destinations).forEach(([key, destination]) => validDestination(destination, key, origin));
  return value;
}

export function remainingSeconds(status, receivedAt, now = browserNow(), clockFloor = 0) {
  const deadlines = [status?.clock.deadline_at, status?.operation?.deadline_at].filter(Boolean).map(Date.parse);
  if (!deadlines.length) return null;
  // Advance the guardian clock by elapsed local time; the browser never extends it.
  return Math.max(0, Math.floor((Math.min(...deadlines) - Math.max(clockFloor, Date.parse(status.clock.server_time)) - Math.max(0, now - receivedAt)) / 1000));
}

export function actionEligibility(snapshot, name, now = browserNow()) {
  const {status, busy, uncertain, receivedAt} = snapshot;
  if (busy) return 'request_in_progress';
  if (uncertain || !status || now - receivedAt > 30000) return 'fresh_readback_required';
  const action = status.actions.find(a => a.name === name);
  if (!action?.enabled) return action?.reason || 'action_unavailable';
  if (!status.session.authenticated) return 'not_authenticated';
  if (!status.operation || !status.session.csrf_token) return 'fresh_readback_required';
  if (remainingSeconds(status, receivedAt, now, snapshot.clockFloor) === 0) return 'request_expired';
  if (action.requires_reauth && !UUID.test(status.operation.review || '')) return 'review_unavailable';
  return null;
}

function fromBase64(value, max) {
  require(typeof value === 'string' && value.length <= max && B64.test(value), 'invalid_authentication_options');
  try {
    const raw = atob(value.replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from(raw, char => char.charCodeAt(0));
  } catch { throw new GuardianError('invalid_authentication_options'); }
}
function toBase64(value) {
  require(value instanceof ArrayBuffer, 'authentication_refused');
  return btoa(String.fromCharCode(...new Uint8Array(value))).replace(/=/g, '').replace(/\+/g, '-').replace(/\//g, '_');
}

function eligibleRpId(value, hostname) {
  if (typeof value !== 'string' || value.length > 253 || value !== value.toLowerCase() || !value.includes('.')) return false;
  if (!value.split('.').every(label => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))) return false;
  return hostname === value || hostname.endsWith('.' + value);
}

export function credentialOptions(data, hostname, now = Date.now()) {
  require(own(data, ['request', 'publicKey', 'expires_at']) && UUID.test(data.request) && stamp(data.expires_at), 'invalid_authentication_options');
  const key = data.publicKey;
  require(key && typeof key === 'object' && Object.keys(key).every(k => ['challenge', 'rpId', 'allowCredentials', 'userVerification', 'timeout', 'extensions'].includes(k)), 'invalid_authentication_options');
  // The guardian returns its exact enrolled RP. Existing credentials may use a
  // parent RP on this dedicated child host; the browser enforces RP eligibility
  // (including public suffix rules), and root verifies its exact pin and origin.
  require(eligibleRpId(key.rpId, hostname) && key.userVerification === 'required' && Array.isArray(key.allowCredentials) && key.allowCredentials.length > 0 && key.allowCredentials.length <= 64, 'invalid_authentication_options');
  require(key.extensions === undefined || own(key.extensions, []), 'invalid_authentication_options');
  const timeout = Date.parse(data.expires_at) - now;
  require(timeout > 0, 'request_expired');
  return {challenge: fromBase64(key.challenge, 2048), rpId: key.rpId, userVerification: 'required', timeout: Math.min(timeout, typeof key.timeout === 'number' && key.timeout > 0 ? key.timeout : 60000), allowCredentials: key.allowCredentials.map(c => {
    require(c && Object.keys(c).every(k => ['id', 'type', 'transports'].includes(k)) && c.type === 'public-key', 'invalid_authentication_options');
    const result = {type: 'public-key', id: fromBase64(c.id, 2048)};
    if (c.transports !== undefined) {
      require(Array.isArray(c.transports) && c.transports.every(t => ['usb', 'nfc', 'ble', 'internal', 'hybrid', 'smart-card'].includes(t)), 'invalid_authentication_options');
      result.transports = c.transports;
    }
    return result;
  })};
}

export function serializeAssertion(credential, origin) {
  require(credential && credential.type === 'public-key' && typeof credential.id === 'string' && credential.id.length <= 2048 && B64.test(credential.id) && own(credential.getClientExtensionResults(), []), 'authentication_refused');
  const response = credential.response;
  let client;
  try { client = JSON.parse(new TextDecoder().decode(response.clientDataJSON)); } catch { throw new GuardianError('authentication_refused'); }
  require(client.type === 'webauthn.get' && client.origin === origin && (client.crossOrigin === undefined || client.crossOrigin === false) && (client.topOrigin === undefined || client.topOrigin === origin), 'authentication_refused');
  require([null, 'platform', 'cross-platform'].includes(credential.authenticatorAttachment ?? null), 'authentication_refused');
  return {id: credential.id, rawId: toBase64(credential.rawId), type: 'public-key', authenticatorAttachment: credential.authenticatorAttachment ?? null, clientExtensionResults: {}, response: {clientDataJSON: toBase64(response.clientDataJSON), authenticatorData: toBase64(response.authenticatorData), signature: toBase64(response.signature), userHandle: response.userHandle === null ? null : toBase64(response.userHandle)}};
}

export class GuardianClient {
  constructor({origin, fetcher = globalThis.fetch, credentials = globalThis.navigator?.credentials, crypto = globalThis.crypto, now = browserNow, onChange = () => {}}) {
    const url = new URL(origin);
    require(url.protocol === 'https:' && (!url.port || url.port === '443') && url.origin === origin, 'guardian_origin_required');
    this.origin = origin; this.fetcher = fetcher; this.credentials = credentials; this.crypto = crypto; this.now = now; this.onChange = onChange;
    this.snapshot = {status: null, revision: null, receivedAt: 0, busy: false, uncertain: false, error: null};
    this.sequence = 0;
  }
  update(change) { this.snapshot = {...this.snapshot, ...change}; this.onChange(this.snapshot); }
  async request(path, body) {
    const mutation = body !== undefined;
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), 20000);
    try {
      const csrf = this.snapshot.status?.session.csrf_token;
      if (mutation) require(text(csrf, 512), 'fresh_readback_required');
      const response = await this.fetcher(this.origin + PREFIX + 'api/' + path, {method: mutation ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: abort.signal, headers: mutation ? {'Content-Type': 'application/json', 'X-Mojo-Acme-CSRF': csrf} : {'Accept': 'application/json'}, ...(mutation ? {body: JSON.stringify(body)} : {})});
      require(response.headers.get('content-type')?.split(';')[0].trim() === 'application/json');
      const raw = await response.text();
      require(new TextEncoder().encode(raw).byteLength <= 65536);
      let frame;
      try { frame = JSON.parse(raw); } catch { throw new GuardianError('invalid_response', mutation); }
      require(own(frame, ['version', 'ok', 'data', 'error', 'request_id', 'revision', 'observed_at']) && frame.version === 1 && typeof frame.ok === 'boolean' && /^[0-9a-f]{32}$/.test(frame.request_id) && revision(frame.revision) && stamp(frame.observed_at));
      if (!frame.ok) {
        require(!response.ok && frame.data === null && own(frame.error, ['code', 'retryable']) && ERRORS.has(frame.error.code) && frame.error.retryable === false);
        throw new GuardianError(frame.error.code);
      }
      require(response.ok && frame.error === null);
      return frame;
    } catch (error) {
      if (error instanceof GuardianError) {
        // A malformed response cannot establish whether a submitted action ran.
        if (mutation && error.code === 'invalid_response') error.unknown = true;
        throw error;
      }
      throw new GuardianError(mutation ? 'outcome_unknown' : 'readback_unavailable', mutation);
    } finally { clearTimeout(timer); }
  }
  apply(frame) {
    const status = validateStatus(frame.data, this.origin);
    require(status.operation === null || status.operation.revision === frame.revision);
    const now = this.now();
    const previousClock = this.snapshot.status ? Math.max(this.snapshot.clockFloor || 0, Date.parse(this.snapshot.status.clock.server_time)) + Math.max(0, now - this.snapshot.receivedAt) : 0;
    const clockFloor = Math.max(previousClock, Date.parse(status.clock.server_time), Date.parse(frame.observed_at));
    this.update({status, revision: frame.revision, receivedAt: now, clockFloor, uncertain: false, error: null});
  }
  async refresh() {
    if (this.snapshot.busy) return;
    const sequence = ++this.sequence;
    try {
      const frame = await this.request('status');
      if (sequence === this.sequence) this.apply(frame);
    } catch (error) {
      if (sequence === this.sequence) this.update({status: null, revision: null, receivedAt: 0, uncertain: true, error});
    }
  }
  async assertion(options) {
    require(this.credentials?.get && globalThis.isSecureContext !== false, 'passkey_unavailable');
    const publicKey = credentialOptions(options, new URL(this.origin).hostname, this.now());
    let credential;
    try { credential = await this.credentials.get({publicKey}); } catch { throw new GuardianError('authentication_cancelled'); }
    require(Date.parse(options.expires_at) > this.now(), 'request_expired');
    return this.request('session/verify', {request: options.request, credential: serializeAssertion(credential, this.origin)});
  }
  async signIn() {
    if (this.snapshot.busy || !this.snapshot.status) return;
    await this.perform(async () => {
      const frame = await this.request('session/options', {purpose: 'sign_in', action: null, operation: null, expected_revision: this.snapshot.revision, review: null});
      const verified = await this.assertion(frame.data);
      require(own(verified.data, ['status', 'authorization', 'expires_at']) && verified.data.authorization === null && optionalStamp(verified.data.expires_at));
      this.apply({...verified, data: verified.data.status});
    });
  }
  async logout() {
    if (this.snapshot.busy || !this.snapshot.status) return;
    await this.perform(async () => this.apply(await this.request('logout', {})));
  }
  async perform(callback) {
    ++this.sequence;
    this.update({busy: true, error: null});
    try { await callback(); }
    catch (error) { this.update({status: null, revision: null, receivedAt: 0, uncertain: true, error: error instanceof GuardianError ? error : new GuardianError('outcome_unknown', true)}); }
    finally { this.update({busy: false}); }
  }
  async run(name, expected) {
    const reason = actionEligibility(this.snapshot, name, this.now());
    require(reason === null, reason || 'action_unavailable');
    const status = this.snapshot.status;
    require(expected.operation === status.operation.id && expected.revision === this.snapshot.revision && expected.review === status.operation.review, 'generation_changed');
    await this.perform(async () => {
      let authorization = null;
      if (!READ_ACTIONS.has(name)) {
        const options = await this.request('session/options', {purpose: 'action', action: name, operation: expected.operation, expected_revision: expected.revision, review: expected.review});
        require(options.revision === expected.revision, 'generation_changed');
        const verified = await this.assertion(options.data);
        require(own(verified.data, ['status', 'authorization', 'expires_at']) && UUID.test(verified.data.authorization || '') && stamp(verified.data.expires_at) && Date.parse(verified.data.expires_at) > this.now());
        const fresh = validateStatus(verified.data.status, this.origin);
        require(verified.revision === expected.revision && fresh.operation?.id === expected.operation && fresh.operation?.review === expected.review && fresh.operation?.revision === expected.revision, 'generation_changed');
        const check = {...this.snapshot, status: fresh, busy: false, receivedAt: this.now(), uncertain: false};
        require(actionEligibility(check, name, this.now()) === null, 'generation_changed');
        authorization = verified.data.authorization;
        this.apply({...verified, data: fresh});
        const finalReason = actionEligibility({...this.snapshot, busy: false}, name, this.now());
        require(finalReason === null, finalReason || 'generation_changed');
      }
      this.apply(await this.request(name, {operation: expected.operation, expected_revision: expected.revision, replay_key: this.crypto.randomUUID(), authorization}));
    });
  }
}

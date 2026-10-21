import {ACTIONS, GuardianClient, GuardianError, READ_ACTIONS, actionEligibility, browserNow, entryOperation, remainingSeconds, validDestination} from './api.js';

export const ACTION_COPY = Object.freeze({
  inspect: ['Inspect current state', 'Read actual installation, deployment, roster, and process observations.'],
  'enroll-retained': ['Enroll retained service', 'Enroll the approved service and exact node transport after possession and independent approval.'],
  'review-maintenance': ['Review maintenance', 'Review the current deployment, artifact, roster, and required maintenance protections.'],
  'establish-holds': ['Establish writer holds', 'Apply and read back the required protections across the frozen producer roster.'],
  witness: ['Check history witness', 'Obtain attributed provider outcomes and reconcile prior attempts before ledger cutover.'],
  prepare: ['Prepare and seal coverage', 'Confirm the actual floor sessions and reviewed producer coverage for this exact operation.'],
  activate: ['Activate configuration', 'Commit the reviewed configuration with the retained protections in place.'],
  cancel: ['Cancel before changes', 'Ask the guardian to reconcile whether cancellation is still safe. Existing protections remain authoritative.'],
  reconcile: ['Reconcile readback', 'Read the current floors, holds, deployment, and recorded outcome before another decision.'],
  retry: ['Retry this operation', 'Request a fresh attempt for the same immutable installation, artifact, deployment, roster, and profile.'],
  restore: ['Restore qualified writers', 'Restore only writers qualified by current readback. Partial failures retain their protections.'],
  verify: ['Verify final readback', 'Read the resulting configuration, producer state, and retained protections.'],
});
const PHASES = [['trust', 'Install and enroll'], ['qualification', 'Qualify deployment'], ['maintenance', 'Review and hold'], ['history', 'Witness and cutover'], ['prepare', 'Prepare coverage'], ['activation', 'Review and activate'], ['recovery', 'Read back and restore']];
const REASONS = {
  dependent_integration_unavailable: 'The reviewed foundation and application adapter are not connected to this retained service.',
  not_authenticated: 'Sign in with an enrolled existing passkey.',
  operator_not_authorized: 'This account is not currently authorized for this operation.',
  reauth_required: 'Fresh action authentication is required.',
  generation_changed: 'The root revision changed. Refresh and review the current operation.',
  replay_refused: 'The guardian refused a repeated request. Read the current outcome before another action.',
  request_expired: 'The guardian completion window or authentication challenge expired. Refresh to reconcile.',
  session_unavailable: 'The guardian session is unavailable. Refresh before signing in again.',
  runtime_unavailable: 'The retained runtime has not been qualified.',
  profile_unavailable: 'An approved root profile is unavailable.',
  operator_service_unavailable: 'The retained operator service is unavailable.',
  independent_approval_unavailable: 'Independent deployment approval is unavailable.',
  database_enrollment_unavailable: 'The existing account and public credential projection is unavailable.',
  coordinator_unavailable: 'The trusted coordinator is unavailable.',
  provider_witness_unavailable: 'A qualified provider history witness is unavailable.',
  producer_profile_unavailable: 'The root producer profile is unavailable.',
  producer_identity_mismatch: 'The actual producer identity does not match the approved profile.',
  producer_coverage_unavailable: 'Qualified coverage for every required producer is unavailable.',
  application_coverage_unavailable: 'The application producer roster has not been qualified.',
  transport_unavailable: 'The independently enrolled node transport is unavailable.',
  node_unavailable: 'An exact node observation is unavailable.',
  floor_unavailable: 'A required floor session is unavailable.',
  configuration_busy: 'Another configuration operation currently owns admission.',
  held_recovery_required: 'Protections are retained. Reconcile the current state before qualified recovery.',
  commit_unknown: 'The commit outcome is unknown. Reconcile actual configuration and protections.',
  qualification_environment_unavailable: 'The required qualification environment is unavailable.',
  csrf_refused: 'The guardian refused this session channel. Refresh before trying again.',
  origin_refused: 'The guardian refused this browser origin or channel.',
  invalid_request: 'The guardian refused the request shape. Refresh the current state.',
  invalid_frame: 'The guardian refused the request frame. Refresh the current state.',
  invalid_response: 'The service response could not be verified. Current controls have been discarded.',
  readback_unavailable: 'Current guardian readback is unavailable. Controls remain locked until a fresh read succeeds.',
  outcome_unknown: 'The submitted request has an unknown outcome. Refresh to reconcile; do not repeat it blindly.',
  authentication_cancelled: 'Passkey authentication did not complete. No operation action was submitted. Refresh before continuing.',
  authentication_refused: 'The passkey response did not match this guardian channel. No operation action was submitted.',
  invalid_authentication_options: 'The authentication request could not be verified for this guardian. No assertion was sent.',
  passkey_unavailable: 'This browser cannot use the required existing passkey here. Use a compatible secure browser and enrolled credential.',
  fresh_readback_required: 'Refresh the guardian state before continuing.',
  request_in_progress: 'A request is in progress. Wait for its outcome.',
  review_unavailable: 'The guardian has not issued a current action review.',
  action_unavailable: 'The guardian has not enabled this action.',
  guardian_origin_required: 'Open the root-pinned HTTPS guardian origin on its standard port.',
  invalid_entry: 'Open the registered guardian entry link. This address contains an unsupported path or parameter.',
  operation_mismatch: 'This link names a different operation from current root readback. Open the current registered operation before acting.',
};
const STATE_COPY = {
  unavailable: ['Complete the service prerequisites', 'The retained page is available. Operational authority is unavailable until the named source integrations and qualifications are present.'],
  bootstrap_only: ['Establish operator trust', 'Select an existing compatible credential, prove possession, and obtain independent approval before enrollment.'],
  enrollment_pending: ['Complete independent enrollment', 'Review actual enrollment progress and the independent approval destination. Approval is not inferred from a browser acknowledgment.'],
  enrolled: ['Qualify the exact deployment', 'Inspect the enrolled service and qualify the artifact, deployment, nodes, and producer scopes.'],
  planning: ['Review the maintenance scope', 'Review the guardian’s actual deployment and producer observations before establishing holds.'],
  holding: ['Read back writer protections', 'The guardian is establishing or reading retained holds. Read actual node and producer observations before preparing.'],
  prepared: ['Review the activation', 'Read the sealed coverage and exact root review. Activation requires a fresh, action-bound passkey assertion.'],
  reviewing: ['Review the exact configuration', 'The current guardian revision defines this review. Any change requires a new review and fresh authentication.'],
  activating: ['Await authoritative commit readback', 'A submitted activation is not completion. Reconcile actual configuration and protections before any retry or restore.'],
  active: ['Read back and restore', 'Read the activated configuration and restore only the writers qualified by current evidence.'],
  restoring: ['Read back restoration', 'Restore outcomes may be partial. Required protections remain visible until qualified readback confirms otherwise.'],
  verified: ['Inspect the final evidence', 'Review the guardian’s final readback and the source observations that support it.'],
  held_recovery_required: ['Reconcile retained protections', 'Writer protections remain in place. Reconcile the exact operation before a supported, freshly authenticated recovery action.'],
  commit_unknown: ['Reconcile the unknown outcome', 'Do not infer cancellation or success. Read the actual configuration, floors, holds, and deployment before another action.'],
  cancelled: ['Review reconciled cancellation', 'The guardian reports cancellation. Review its actual readback before beginning another operation.'],
};
export function words(value) { return String(value ?? '').replace(/[_-]+/g, ' '); }
export function reasonText(code) { return REASONS[code] || 'A required source observation is unavailable.'; }
export function phaseFor(status) {
  if (!status) return null;
  const state = status?.operation?.state || status?.installation.state;
  if (['commit_unknown', 'held_recovery_required', 'restoring', 'active', 'verified', 'cancelled'].includes(state)) return 'recovery';
  if (['unavailable', 'bootstrap_only', 'enrollment_pending'].includes(state)) return 'trust';
  if (status?.operation?.phase === 'witness') return 'history';
  if (state === 'enrolled') return 'qualification';
  if (['planning', 'holding'].includes(state)) return 'maintenance';
  if (state === 'prepared') return 'prepare';
  if (['reviewing', 'activating'].includes(state)) return 'activation';
  return 'recovery';
}
export function presentation(snapshot, operationContext = null, now = browserNow()) {
  const status = snapshot.status;
  if (!status) return {state: 'unavailable', title: snapshot.uncertain ? 'Fresh readback required' : 'Read the retained service', description: snapshot.uncertain ? 'Current authority has been discarded. Refresh to observe the guardian before any action.' : 'Waiting for the guardian’s current status.', actions: [], mismatch: false};
  const state = status.operation?.state || status.installation.state;
  const mismatch = operationContext !== null && status.operation?.id !== operationContext;
  const copy = STATE_COPY[state] || STATE_COPY.unavailable;
  return {state, title: mismatch ? 'Reconcile the operation link' : copy[0], description: mismatch ? reasonText('operation_mismatch') : copy[1], mismatch, actions: ACTIONS.map(name => ({name, reason: mismatch ? 'operation_mismatch' : actionEligibility(snapshot, name, now)}))};
}

function el(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = String(text); return node; }
function clear(node) { node.replaceChildren(); }
function set(id, value) { document.getElementById(id).textContent = String(value); }
function date(value) { return value ? new Date(value).toLocaleString(undefined, {dateStyle: 'medium', timeStyle: 'medium'}) : 'Not observed'; }
function badge(state) { const severity = ['commit_unknown', 'mismatch', 'refused'].includes(state) ? 'danger' : ['unavailable', 'pending', 'held_recovery_required', 'fixture'].includes(state) ? 'warning' : ['qualified', 'verified'].includes(state) ? 'success' : ''; return el('span', 'badge ' + severity, words(state)); }
function detail(container, key, value) { const row = el('div'); row.append(el('dt', '', key), el('dd', '', value)); container.append(row); }
function link(container, destination, kind, origin) { if (!destination) return; const safe = validDestination(destination, kind, origin); if (!safe) return; const a = el('a', 'destination-link', safe.label); a.href = safe.url; a.target = '_blank'; a.rel = 'noopener noreferrer'; container.append(a); }

export function mountWizard({client, operationContext = null} = {}) {
  const origin = globalThis.location.origin;
  let current = {status: null, revision: null, receivedAt: 0, busy: false, uncertain: false, error: null};
  let review = null;
  let returnFocus = null;
  const dialog = document.getElementById('review-dialog');
  const phaseMedia = matchMedia('(max-width: 700px)');
  const phaseDisclosure = document.getElementById('phase-disclosure');
  const resizePhases = () => { phaseDisclosure.open = !phaseMedia.matches; };
  resizePhases(); phaseMedia.addEventListener('change', resizePhases);
  const announce = value => set('announcement', value);
  function closeReview() { review = null; if (dialog.open) dialog.close(); if (returnFocus?.isConnected) returnFocus.focus(); else if (returnFocus?.dataset.action) document.querySelector(`[data-action="${returnFocus.dataset.action}"]`)?.focus(); }
  function button(name, primary = false) {
    const disabled = presentation(current, operationContext).actions.find(a => a.name === name)?.reason || null;
    const b = el('button', 'button ' + (primary ? 'primary' : 'secondary'), primary ? ACTION_COPY[name][0] : ({'enroll-retained': 'Enroll', 'review-maintenance': 'Review', 'establish-holds': 'Hold', witness: 'Check history', prepare: 'Prepare', activate: 'Activate', cancel: 'Cancel', reconcile: 'Reconcile', retry: 'Retry', restore: 'Restore', verify: 'Verify', inspect: 'Inspect'}[name]));
    b.type = 'button'; b.disabled = disabled !== null; b.dataset.action = name;
    b.addEventListener('click', () => openReview(name, b));
    return b;
  }
  function openReview(name, trigger) {
    if (presentation(current, operationContext).actions.find(a => a.name === name)?.reason !== null) return;
    const operation = current.status.operation;
    review = {name, operation: operation.id, revision: current.revision, review: operation.review};
    returnFocus = trigger;
    set('review-title', ACTION_COPY[name][0]); set('review-description', ACTION_COPY[name][1]);
    const facts = document.getElementById('review-facts'); clear(facts);
    detail(facts, 'Guardian', origin); detail(facts, 'Installation', current.status.installation.uuid || 'Not observed'); detail(facts, 'Operation', operation.id); detail(facts, 'Root revision', current.revision); detail(facts, 'Current state', words(operation.state)); detail(facts, 'Outcome', words(operation.outcome)); detail(facts, 'Deadline', date(operation.deadline_at || current.status.clock.deadline_at));
    // These are source-labelled facts, never editable proof or a client checklist.
    for (const group of current.status.facts) for (const item of group.items) detail(facts, `${group.label} · ${words(item.classification)} · ${item.key}`, item.value === null ? 'Unavailable' : item.value);
    set('review-warning', name === 'activate' ? 'Activation can change the shared configuration. This exact root revision and review will be authenticated and consumed once. An unknown response requires readback; it is not safe to repeat.' : name === 'restore' ? 'Restore is limited to qualified writers. A partial or unknown outcome can leave protections in place. Read back the result before another action.' : 'The guardian checks the current scope and root revision again. A refused, expired, or unknown request requires fresh readback. Closing this dialog submits no action.');
    set('review-auth', READ_ACTIONS.has(name) ? 'This action reads current state through the guardian. It does not use an action authorization permit.' : 'Continue opens your existing passkey for fresh, action-bound verification. No passkey, approval envelope, or provider data is pasted here.');
    const confirm = document.getElementById('confirm-review'); confirm.textContent = READ_ACTIONS.has(name) ? ACTION_COPY[name][0] : 'Authenticate and ' + ({activate: 'activate', restore: 'restore', retry: 'retry', cancel: 'cancel'}[name] || 'continue'); confirm.className = 'button ' + (name === 'activate' ? 'danger' : 'primary'); confirm.disabled = false;
    dialog.showModal(); const title = document.getElementById('review-title'); title.tabIndex = -1; title.focus();
  }
  function renderFacts(groups, container) {
    clear(container);
    if (!groups.length) { container.append(el('p', 'empty-state', 'No source observations are available. Absence is not qualification.')); return; }
    for (const group of groups) {
      const section = el('section', 'fact-group'); const heading = el('div', 'fact-heading'); heading.append(el('h3', '', group.label), badge(group.state));
      section.append(heading, el('p', 'fact-source', `Source: ${words(group.source)} · ${date(group.observed_at)}`));
      const items = el('dl', 'fact-items');
      for (const item of group.items) { const row = el('div'); const dt = el('dt', '', item.key); dt.append(el('span', 'classification', words(item.classification))); row.append(dt, el('dd', '', item.value === null ? 'Unavailable' : item.value)); items.append(row); }
      section.append(items); container.append(section);
    }
  }
  function render(snapshot) {
    current = snapshot;
    const view = presentation(snapshot, operationContext);
    const status = snapshot.status;
    if (review && (!status || snapshot.revision !== review.revision || status.operation?.id !== review.operation || status.operation?.review !== review.review || snapshot.busy || actionEligibility(snapshot, review.name) !== null)) closeReview();
    set('guardian-origin', origin); set('task-title', view.title); set('task-description', view.description); set('state', words(view.state));
    document.getElementById('state').className = badge(view.state).className;
    const notice = document.getElementById('notice'); notice.hidden = !snapshot.error && !view.mismatch;
    if (!notice.hidden) { notice.textContent = reasonText(snapshot.error?.code || 'operation_mismatch'); if (snapshot.error?.unknown) notice.textContent += ' Protections and the recorded outcome must be reconciled by the guardian.'; }
    document.getElementById('refresh').disabled = snapshot.busy;
    const signIn = document.getElementById('sign-in'); const signOut = document.getElementById('sign-out');
    signIn.hidden = !!status?.session.authenticated; signIn.disabled = snapshot.busy || !status?.session.csrf_token; signOut.hidden = !status?.session.authenticated; signOut.disabled = snapshot.busy;
    set('session-label', snapshot.busy ? 'Request in progress' : status?.session.authenticated ? 'Operator signed in' : status ? 'Operator not signed in' : 'Session not observed');
    set('actor', status?.session.actor?.account_uuid || ''); document.getElementById('session-dot').className = 'status-dot' + (status?.session.authenticated ? ' authenticated' : '');
    const phases = document.getElementById('phases'); clear(phases);
    const phaseIndex = PHASES.findIndex(([key]) => key === phaseFor(status));
    set('phase-current', phaseIndex < 0 ? 'Awaiting guardian state' : PHASES[phaseIndex][1]);
    set('phase-count', phaseIndex < 0 ? '7 stages' : `Step ${phaseIndex + 1} of ${PHASES.length}`);
    PHASES.forEach(([key, label], index) => { const item = el('li', key === phaseFor(status) ? 'current' : ''); if (key === phaseFor(status)) item.setAttribute('aria-current', 'step'); const copy = el('span', 'phase-copy', label); if (key === phaseFor(status)) copy.append(el('small', '', 'Current guardian state')); item.append(el('span', 'phase-number', index + 1), copy); phases.append(item); });
    set('operation-id', status?.operation?.id || 'No operation observed'); set('revision', snapshot.revision ?? 'Not observed'); set('outcome', status?.operation ? words(status.operation.outcome) : 'No operation observed');
    set('installation-id', status?.installation.uuid || 'No installation observed'); set('enrollment', status ? (status.installation.enrolled ? 'Confirmed by guardian' : 'Not confirmed') : 'Not observed');
    set('observation', status ? `Source: retained guardian · ${date(status.clock.server_time)}` : 'Source: retained guardian · Fresh readback required');
    const next = document.getElementById('next-action'); clear(next);
    const preferred = view.actions.find(a => a.reason === null && !['inspect', 'cancel', 'verify'].includes(a.name)) || view.actions.find(a => a.reason === null);
    if (preferred) { next.append(button(preferred.name, true), el('p', '', ACTION_COPY[preferred.name][1])); }
    else next.append(el('p', '', snapshot.busy ? 'Waiting for the guardian response. No request will be replayed automatically.' : status ? 'No operation action is currently available. Review the named prerequisites and current evidence below.' : 'Refresh status to obtain current authority.'));
    const actions = document.getElementById('actions'); clear(actions);
    for (const {name, reason} of view.actions) {
      const row = el('div', 'action-row'); const copy = el('div'); copy.append(el('h3', '', ACTION_COPY[name][0]), el('p', '', ACTION_COPY[name][1])); if (reason) copy.append(el('p', 'reason', reasonText(reason))); row.append(copy, button(name)); actions.append(row);
    }
    if (!view.actions.length) actions.append(el('p', 'empty-state', 'Controls have no current authority. Refresh status to read the supported actions.'));
    set('action-count', `${view.actions.filter(a => a.reason === null).length} available`);
    const disclosure = document.getElementById('controls-disclosure');
    if (status && !disclosure.dataset.initialized) { disclosure.open = view.actions.some(a => a.reason === null); disclosure.dataset.initialized = 'true'; }
    set('controls-summary', view.actions.length ? `View all ${view.actions.length} controls and prerequisites` : 'Awaiting current controls');
    const bootstrap = document.getElementById('bootstrap-panel'); bootstrap.hidden = !status || (!['unavailable', 'bootstrap_only', 'enrollment_pending'].includes(status.installation.state) && status.bootstrap.state === 'enrolled');
    if (status) { set('bootstrap-description', status.bootstrap.error_code ? reasonText(status.bootstrap.error_code) + ' Existing credential selection is unavailable until the guardian supplies approved choices.' : `Guardian enrollment state: ${words(status.bootstrap.state)}. Candidate selection is unavailable without approved existing account and credential choices.`); const destination = document.getElementById('bootstrap-link'); clear(destination); if (status.bootstrap.independent_approval_url) link(destination, {label: 'Open independent approval', url: status.bootstrap.independent_approval_url}, 'independent_approval', origin); }
    renderFacts(status?.facts || [], document.getElementById('facts'));
    const qualification = document.getElementById('qualification'); clear(qualification);
    for (const [key, label] of [['producer_identity', 'Producer identity'], ['producer_coverage', 'Producer coverage'], ['provider_witness', 'Provider history witness']]) {
      const projection = status?.[key]; const row = el('div', 'qualification-row'); const title = el('div', 'qualification-title'); title.append(el('strong', '', label), badge(projection?.state || 'unavailable')); row.append(title);
      row.append(el('p', '', projection?.missing_facts?.length ? projection.missing_facts.map(reasonText).join(' ') : projection ? 'Source: retained guardian. See the evidence ledger for the actual scope and observations.' : 'No current guardian observation.')); qualification.append(row);
    }
    set('node-count', status ? status.nodes.length : '—'); const nodes = document.getElementById('nodes'); clear(nodes);
    if (!status?.nodes.length) nodes.append(el('p', 'empty-state', 'No frozen node roster has been observed. An empty roster is not fleet qualification.'));
    for (const node of status?.nodes || []) { const row = el('div', 'node-row'); const title = el('div', 'fact-heading'); title.append(el('h3', '', node.id), badge(node.state)); row.append(title, el('p', '', `Source: retained guardian · ${date(node.observed_at)}`)); if (node.missing_facts.length) row.append(el('p', '', node.missing_facts.map(reasonText).join(' '))); if (node.facts.length) { const evidence = el('details'); evidence.append(el('summary', '', 'Read node evidence')); const content = el('div'); renderFacts(node.facts, content); evidence.append(content); row.append(evidence); } nodes.append(row); }
    const destinations = document.getElementById('destinations'); clear(destinations);
    for (const kind of ['independent_approval', 'provider_console', 'history_console']) if (status?.destinations[kind]) link(destinations, status.destinations[kind], kind, origin);
    if (!destinations.children.length) destinations.append(el('p', 'empty-state', 'No independent destination is currently registered. Missing approval or witness capability remains unavailable.'));
    tick();
  }
  function tick() {
    const seconds = remainingSeconds(current.status, current.receivedAt, browserNow(), current.clockFloor);
    const deadline = document.getElementById('deadline'); deadline.hidden = seconds === null;
    if (seconds !== null) { set('countdown', seconds ? `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, '0')}s remaining` : 'Window expired'); set('deadline-time', `Guardian deadline: ${date(current.status.operation?.deadline_at || current.status.clock.deadline_at)}`); deadline.classList.toggle('expired', seconds === 0); }
    // Expiration removes authority even without a network response.
    for (const b of document.querySelectorAll('[data-action]')) b.disabled = presentation(current, operationContext).actions.find(a => a.name === b.dataset.action)?.reason !== null;
    if (review && actionEligibility(current, review.name) !== null) { closeReview(); announce('The review expired. Refresh the current guardian state.'); }
  }
  client ||= new GuardianClient({origin}); client.onChange = render;
  document.getElementById('refresh').addEventListener('click', async () => { closeReview(); await client.refresh(); announce(client.snapshot.status ? 'Guardian status refreshed.' : 'Current status is unavailable.'); });
  document.getElementById('sign-in').addEventListener('click', async () => { closeReview(); await client.signIn(); announce(client.snapshot.status?.session.authenticated ? 'Operator signed in.' : 'Sign in did not complete. Read the current status.'); });
  document.getElementById('sign-out').addEventListener('click', async () => { closeReview(); await client.logout(); announce('Session readback updated.'); });
  document.getElementById('close-review').addEventListener('click', closeReview); document.getElementById('cancel-review').addEventListener('click', closeReview); dialog.addEventListener('cancel', () => { review = null; returnFocus?.focus(); });
  document.getElementById('confirm-review').addEventListener('click', async () => { if (!review) return; const selected = review; closeReview(); try { await client.run(selected.name, selected); } catch (error) { client.update({status: null, uncertain: true, error: error instanceof GuardianError ? error : new GuardianError('fresh_readback_required')}); } announce(client.snapshot.error ? reasonText(client.snapshot.error.code) : 'Guardian action readback received.'); });
  document.getElementById('theme').addEventListener('click', () => { const dark = document.documentElement.dataset.theme ? document.documentElement.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches; document.documentElement.dataset.theme = dark ? 'light' : 'dark'; document.getElementById('theme').setAttribute('aria-label', dark ? 'Use dark theme' : 'Use light theme'); });
  document.getElementById('theme').setAttribute('aria-label', matchMedia('(prefers-color-scheme: dark)').matches ? 'Use light theme' : 'Use dark theme');
  const timer = setInterval(tick, 1000);
  const poll = setInterval(() => { if (!document.hidden && !dialog.open) client.refresh(); }, 10000);
  render(client.snapshot); client.refresh();
  return {client, dispose() { clearInterval(timer); clearInterval(poll); phaseMedia.removeEventListener('change', resizePhases); closeReview(); }};
}

if (typeof document !== 'undefined' && document.getElementById('workspace')) {
  try { const operationContext = entryOperation(globalThis.location); if (globalThis.top !== globalThis.self || globalThis.opener) throw new GuardianError('origin_refused'); mountWizard({operationContext}); }
  catch (error) { set('guardian-origin', globalThis.location.origin); const notice = document.getElementById('notice'); notice.hidden = false; notice.textContent = reasonText(error.code); set('task-title', 'Open the registered guardian'); set('task-description', 'No service request has been submitted from this page.'); document.getElementById('refresh').disabled = true; }
}

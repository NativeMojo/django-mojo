// Name-first WebApp onboarding. A NEW app asks for exactly one thing — its
// name — and goes live on the workspace's apps domain with an address, HTTPS,
// and a starter page, all automatic; the first deploy is set up afterwards on
// the app's own page. The address-first machinery below (addressPhase and
// friends) still powers the repair and change-address flows, where the person
// really is choosing an address. No framework words in the primary copy — the
// raw record type appears only where someone types it at their own DNS host,
// and technical identifiers live under "Advanced".
//
// Ported from v1 with three changes and no others: it lands on v2's app page
// (`#/apps?webapp=…`) instead of v1's deployments route; the prerequisites it
// names that v2 has no screen for — System Setup — open the current Admin and
// say so, while Domains is a v2 destination and stays in-portal; and a run in
// flight is reported to the global operation store from the detail poll this
// file already runs. Every refusal, prerequisite and consequence sentence is
// v1's, word for word.
import {api, apiOnce, badge, formatDate, h, icon, listData, openModal} from '../../core.js';
import {copyButton, runAction} from '../../components/actions.js';
import {routeHref} from '../../components/routes.js';
import {removeOnboardingOperation, syncOnboardingOperation} from './operations.js';

const ONBOARDING_DRAFT_KEY = 'mojo-admin:webapp-onboarding-draft:v1';
const LAST_GROUP_KEY = 'mojo-admin:webapp-last-group:v1';
const NEW_GROUP_VALUE = 'new';
// The four things a newcomer moves through. Records and certificate work live
// inside "Set up"; naming (new apps) and address steering (repair) both live
// inside the first step.
const WIZARD_STEPS = ['Name & address', 'Set up', 'Deploy', 'Go live'];

function pendingDraft() {
  try {
    const value = JSON.parse(sessionStorage.getItem(ONBOARDING_DRAFT_KEY) || 'null');
    return value && typeof value === 'object' && typeof value.operation_id === 'string' ? value : null;
  } catch (_) { return null; }
}
function savePendingDraft(value) { sessionStorage.setItem(ONBOARDING_DRAFT_KEY, JSON.stringify(value)); }
function clearPendingDraft() { sessionStorage.removeItem(ONBOARDING_DRAFT_KEY); }

export function hasPendingWizard() { return Boolean(pendingDraft()?.submitted); }

// Whether the selected group may drive its own DNS. A brand-new workspace
// always can; an existing one only if the membership says so. This is what
// gates the "keep my DNS" and connected-domain paths — never a global grant.
function groupDnsAuthority(ctx, groupId) {
  if (groupId === NEW_GROUP_VALUE) return Boolean(ctx.can_create_webapp_group);
  const group = (ctx.webapp_groups || []).find((row) => String(row.id) === String(groupId));
  return Boolean(group?.can_manage_dns);
}

function initialGroup(ctx) {
  const groups = ctx.webapp_groups || [];
  if (groups.length) return String(groups[0].id);
  if (ctx.can_create_webapp_group) return NEW_GROUP_VALUE;
  return '';
}

function rememberGroup(groupId) {
  try { sessionStorage.setItem(LAST_GROUP_KEY, String(groupId)); } catch (_) { /* storage unavailable */ }
}

// The workspace a new app defaults into: the last one used here (the options
// payload does not say which workspace owns the apps domain), else the first
// eligible one.
function defaultGroup(ctx) {
  const groups = ctx.webapp_groups || [];
  let saved = null;
  try { saved = sessionStorage.getItem(LAST_GROUP_KEY); } catch (_) { saved = null; }
  if (saved && groups.some((row) => String(row.id) === String(saved))) return String(saved);
  return groups.length ? String(groups[0].id) : '';
}

// System Setup is a v1-only screen — v2 has no setup destination — so every
// prerequisite that names it opens the current Admin and says so out loud. The
// sentence that sends someone there is unchanged; only the address is.
function setupLink(state, finish, label = 'Open System Setup') {
  return h('a', {class: 'button ghost compact',
    href: `${state.ctx?.admin_path || '/admin/'}#/setup`, onclick: () => finish()},
  label, h('span', {class: 'apps-note', text: ' · opens the current Admin'}));
}

function field(label, input, help = '') {
  return h('label', {class: 'field'}, h('span', {text: label}), input, help ? h('small', {text: help}) : null);
}

function slugify(value) {
  return String(value || '').toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
}

// One DNS record the user copies to their own host. The record type shows only
// here — this is the single place someone actually types it.
function recordCard(record, purpose) {
  const value = Array.isArray(record.value) ? record.value.join(', ') : record.value;
  // A DNS host is exactly where someone is typing into another browser tab, so
  // a clipboard write that silently fails (no secure context) is worse here
  // than anywhere else. The shared button says so on its own face.
  const copy = (text) => copyButton(text, {label: 'Copy'});
  return h('div', {class: 'record-card'},
    purpose ? h('p', {class: 'record-purpose', text: purpose}) : null,
    h('div', {class: 'record-line'}, h('span', {class: 'record-key', text: 'Type'}), h('code', {text: record.type}), copy(record.type)),
    h('div', {class: 'record-line'}, h('span', {class: 'record-key', text: 'Name'}), h('code', {text: record.name}), copy(record.name)),
    h('div', {class: 'record-line'}, h('span', {class: 'record-key', text: 'Points to'}), h('code', {text: value}), copy(value)));
}

// The record purposes, in one place: the wizard shows them beside a record
// someone is typing at their DNS host, and the app page's records table shows
// the same records in a column. Two spellings of "what this record is for"
// would be two different explanations of one thing.
export function recordPurpose(position) {
  return position === 0 ? 'Points your address at us.'
    : 'Lets us issue your HTTPS certificate.';
}

// A name-first run has ONE input and lands on its own: a four-step progress bar
// over it would be counting steps nobody takes. The address-first flow (repair,
// change-address, no-eligible-workspace) really does walk them, and keeps it.
function stepBar(state, index) {
  if (state.nameFirst) return null;
  return h('ol', {class: 'wizard-steps', 'aria-label': 'Setup progress'},
    ...WIZARD_STEPS.map((label, position) => h('li', {
      class: position < index ? 'complete' : position === index ? 'current' : '',
      'aria-current': position === index ? 'step' : null,
    }, h('span', {text: position < index ? '✓' : String(position + 1)}), label)));
}

function intro(iconName, title, copy) {
  return h('div', {class: 'wizard-intro'}, icon(iconName), h('div', {}, h('strong', {text: title}), h('p', {text: copy})));
}

// ---- controller ------------------------------------------------------------

export function startWizard(ctx, reloadApps) {
  const draft = pendingDraft();
  if (draft?.submitted && draft.operation_id) { resumeWizard(ctx, reloadApps); return; }
  runWizard(ctx, reloadApps, null, null);
}

export function resumeWizard(ctx, reloadApps) {
  const draft = pendingDraft();
  runWizard(ctx, reloadApps, draft?.submitted ? draft : null, null);
}

// Move an existing app to a different address. Same wizard, but the app's
// identity is fixed so the onboarding operation adopts it (the backend swaps
// the serving address with no downtime) instead of creating a second app.
export function startChangeAddress(ctx, reloadApps, adopt) {
  runWizard(ctx, reloadApps, null, adopt);
}

function runWizard(ctx, reloadApps, resumeDraft, adopt) {
  // A new app starts at the one-input name phase. The address-first phases
  // remain the machinery for change-address/repair (adopt), and the only path
  // for someone whose sole option is creating a brand-new workspace — a new
  // workspace can have no apps domain yet, so name-only creation can't apply.
  const newAppEntry = (ctx.webapp_groups || []).length ? 'name' : 'address';
  const state = {
    ctx, reloadApps, disposed: false, pollTimer: null, adopt: adopt || null,
    phase: adopt ? 'address' : newAppEntry,
    // A name-first run is the one-decision flow: no step bar over it, and it
    // lands on the app itself instead of ending on a button. `finish` rides on
    // state so donePanel can close the wizard from a microtask.
    nameFirst: !adopt && newAppEntry === 'name', landed: false, finish,
    groupId: adopt ? String(adopt.group_id) : (defaultGroup(ctx) || initialGroup(ctx)),
    url: '', precheck: null, resolved: null, options: null,
    operation: null, message: '',
  };
  const body = h('div', {class: 'wizard-body'});
  const close = openModal({
    title: adopt ? `Change the address for ${adopt.display_name || 'your app'}` : 'New web app',
    subtitle: adopt ? 'Pick the new address. The current one keeps serving until the new one is ready.'
      : 'Pick a name. We handle the address, HTTPS, and a starter page — deploys come after.',
    content: body, wide: true,
    onClose: () => {
      state.disposed = true;
      if (state.pollTimer) clearTimeout(state.pollTimer);
      // This file is the only thing polling the run. Once it stops, nothing is
      // left to report progress or to notice the run finishing, so the banner
      // must not keep claiming it — the list page's resume banner is what says
      // a setup is still unfinished.
      removeOnboardingOperation(state.operation?.operation_id);
    },
  });
  state.close = close;

  function render() {
    if (state.disposed) return;
    let node;
    if (state.phase === 'name') node = namePhase(state, render, finish);
    else if (state.phase === 'address') node = addressPhase(state, render, finish);
    else if (state.phase === 'domain') node = domainPhase(state, render);
    else if (state.phase === 'external') node = externalPhase(state, render);
    else if (state.phase === 'identity') node = identityPhase(state, render, finish);
    else node = runPhase(state, render, finish);
    body.replaceChildren(node);
  }
  function finish() {
    clearPendingDraft();
    state.disposed = true;
    if (state.pollTimer) clearTimeout(state.pollTimer);
    removeOnboardingOperation(state.operation?.operation_id);
    close();
  }

  if (resumeDraft) {
    // Reconcile the saved operation before showing anything, so a reload lands
    // exactly where the person left off instead of minting a duplicate.
    body.replaceChildren(h('div', {class: 'wizard-loading'}, icon('refresh'), h('p', {text: 'Picking up where you left off…'})));
    api(`/api/edge/webapp/onboarding/detail?operation=${encodeURIComponent(resumeDraft.operation_id)}`)
      .then((operation) => { state.operation = operation; state.phase = 'run'; render(); driveRun(state, render); })
      .catch(() => { clearPendingDraft(); state.phase = state.adopt ? 'address' : newAppEntry; render(); });
    return;
  }
  render();
}

// ---- phase 1 (new apps): just a name ----------------------------------------
// One decision: what the app is called. Its address is `${slug}.${apps domain}`
// derived live from the name; availability is checked as you type; workspace,
// environment, and release storage sit under Advanced. When the workspace has
// no apps domain, say why in plain words and point at the places that fix it.

function namePhase(state, render, finish) {
  const ctx = state.ctx;
  const groups = ctx.webapp_groups || [];
  const main = h('div', {class: 'wizard-name-area'});
  const message = h('div', {class: 'form-message', role: 'alert'});
  const verdict = h('div', {class: 'verdict-area'});
  const name = h('input', {value: state.appName || '', placeholder: 'My app', autocomplete: 'off', 'aria-label': 'App name', autofocus: true});
  const preview = h('strong', {class: 'hostname-value', text: '—'});
  const create = h('button', {class: 'button primary', type: 'button', disabled: true}, 'Create app');
  const environment = h('select', {}, ...['production', 'staging', 'preview', 'development'].map((value) => h('option', {value, text: value})));
  const bucket = h('select');
  const group = h('select', {}, ...groups.map((row) => h('option', {value: String(row.id), text: row.name, selected: String(row.id) === state.groupId || null})));
  // Workspace and environment are choices most people never make: they live
  // under Advanced, and the workspace select is hidden outright when there is
  // only one to choose.
  const advanced = h('details', {class: 'wizard-advanced'}, h('summary', {text: 'Advanced'}),
    groups.length > 1 ? field('Workspace', group, 'Which workspace this app belongs to.') : null,
    field('Environment', environment),
    field('Release storage', bucket, 'Chosen automatically when only one is available.'));

  let checkTimer = null;
  let checkSeq = 0;
  let available = false;

  const appsDomain = () => state.options?.apps_domain || null;
  const currentSlug = () => slugify(name.value);

  function updateCreate() {
    create.disabled = !(appsDomain() && name.value.trim() && currentSlug()
      && available && (state.options?.buckets || []).length);
  }

  function availabilityLine(kind, iconName, text) {
    verdict.replaceChildren(h('div', {class: `verdict ${kind}`}, icon(iconName),
      h('div', {}, h('p', {text}))));
  }

  async function checkAvailability() {
    const slug = currentSlug();
    available = false; updateCreate();
    if (!appsDomain()) return;
    if (!slug) { verdict.replaceChildren(); return; }
    const host = `${slug}.${appsDomain().name}`;
    const seq = ++checkSeq;
    availabilityLine('steer', 'refresh', `Checking ${host}…`);
    try {
      const result = await api(`/api/edge/webapp/onboarding/precheck?group=${encodeURIComponent(state.groupId)}&url=${encodeURIComponent(host)}`);
      if (seq !== checkSeq || state.disposed) return;
      state.precheck = result;
      const kind = result.verdict;
      if (kind === 'ready' || kind === 'records_needed') {
        available = true;
        availabilityLine('ready', 'check', `${host} is available.`);
      } else if (kind === 'taken') {
        const who = result.app ? `your app “${result.app}”` : 'another site';
        availabilityLine('blocked', 'alert', `${host} is already used by ${who}. Pick a different name.`);
      } else if (kind === 'conflict') {
        availabilityLine('blocked', 'alert', `${host} already points somewhere else. Pick a different name.`);
      } else if (kind === 'configuration_required') {
        showNotReady(result.reason);
        return;
      } else {
        availabilityLine('blocked', 'alert', result.reason || 'That name cannot be used.');
      }
    } catch (error) { if (seq === checkSeq && !state.disposed) availabilityLine('blocked', 'alert', error.message); }
    updateCreate();
  }

  function scheduleCheck() {
    if (checkTimer) clearTimeout(checkTimer);
    checkTimer = setTimeout(checkAvailability, 350);
  }

  function updatePreview() {
    const active = appsDomain();
    preview.textContent = active ? `${currentSlug() || 'your-app'}.${active.name}` : '—';
  }

  function showNotReady(reason) {
    main.replaceChildren(
      h('div', {class: 'verdict blocked'}, icon('alert'),
        h('div', {}, h('strong', {text: 'New apps can’t go live here yet'}),
          h('p', {text: reason || state.options?.apps_domain_error || 'This workspace has no domain managed here yet.'}),
          h('div', {class: 'form-actions'},
            setupLink(state, finish),
            h('a', {class: 'button ghost compact', href: routeHref('domains'), onclick: () => finish()}, 'Open Domains')))),
      h('div', {class: 'form-actions'}, h('button', {class: 'button ghost', type: 'button', onclick: finish}, 'Close')));
  }

  function paintReady() {
    main.replaceChildren(
      field('App name', name, 'The only thing you need to pick. Everything else is set up for you.'),
      h('div', {class: 'hostname-preview'}, h('span', {text: 'Your app will open at'}), preview),
      verdict, advanced, message,
      h('div', {class: 'form-actions'},
        h('button', {class: 'button ghost', type: 'button', onclick: finish}, 'Cancel'), create));
    updatePreview();
  }

  async function loadOptions() {
    create.disabled = true;
    try {
      // options — including the apps domain the preview suffix comes from —
      // are per workspace, so a workspace change re-fetches them.
      const data = await api(`/api/edge/webapp/onboarding/options?group=${encodeURIComponent(state.groupId)}`);
      if (state.disposed) return;
      state.options = data;
      bucket.replaceChildren(...(data.buckets || []).map((value) => h('option', {value, text: value})));
      if (!data.apps_domain) { showNotReady(data.apps_domain_error); return; }
      paintReady();
      if (!(data.buckets || []).length) { message.textContent = 'No release storage is configured for this installation.'; return; }
      if (currentSlug()) scheduleCheck();
    } catch (error) { if (!state.disposed) { paintReady(); message.textContent = error.message; } }
  }

  name.addEventListener('input', () => {
    state.appName = name.value;
    available = false; updatePreview(); updateCreate(); scheduleCheck();
  });
  group.addEventListener('change', () => {
    state.groupId = group.value; rememberGroup(group.value);
    state.options = null; available = false; verdict.replaceChildren(); updateCreate();
    main.replaceChildren(h('div', {class: 'wizard-loading'}, icon('refresh'), h('p', {text: 'Loading workspace…'})));
    loadOptions();
  });
  create.addEventListener('click', () => {
    const slug = currentSlug();
    const domain = appsDomain();
    if (!domain || !slug) return undefined;
    message.textContent = '';
    rememberGroup(state.groupId);
    // Seed the run machine exactly like a resolved managed domain: the address
    // choice auto-submits from the run loop once the fetched state reaches it.
    state.resolved = {
      domainId: domain.id, label: slug, hostname: `${slug}.${domain.name}`,
      domainName: domain.name, provider: domain.provider, records: [],
    };
    const identity = {display_name: name.value.trim(), slug,
      environment: environment.value, bucket: bucket.value};
    // Success re-renders the wizard body into the run panel, taking this button
    // with it; a refusal leaves it here with the reason beside it.
    return runAction(create, () => createOperation(state, identity, render), {
      pendingLabel: 'Creating…', restoreOnSuccess: false,
      onError: (error) => { message.textContent = error.message; updateCreate(); },
    });
  });

  main.replaceChildren(h('div', {class: 'wizard-loading'}, icon('refresh'), h('p', {text: 'Getting things ready…'})));
  loadOptions();
  return h('div', {class: 'wizard-panel'},
    intro('deploy', 'What’s your app called?', 'Pick a name and we put it online — its own address, HTTPS, and a starter page, ready for your first deploy.'),
    main);
}

// ---- phase 1: the address --------------------------------------------------

function addressPhase(state, render, finish) {
  const ctx = state.ctx;
  const message = h('div', {class: 'form-message', role: 'alert', text: state.message || ''});
  const verdict = h('div', {class: 'verdict-area'});
  const input = h('input', {value: state.url, placeholder: 'myapp.example.com', autocomplete: 'off', 'aria-label': 'Web address', autofocus: true});
  const check = h('button', {class: 'button primary', type: 'button'}, icon('globe'), 'Check');

  // Which workspace this app belongs to. Hidden when there is exactly one and
  // no ability to make more — nothing to decide, nothing to show.
  const groupOptions = [];
  (ctx.webapp_groups || []).forEach((row) => groupOptions.push(h('option', {value: String(row.id), text: row.name, selected: String(row.id) === state.groupId || null})));
  if (ctx.can_create_webapp_group) groupOptions.push(h('option', {value: NEW_GROUP_VALUE, text: 'A new workspace', selected: state.groupId === NEW_GROUP_VALUE || null}));
  const group = h('select', {}, ...groupOptions);
  group.value = state.groupId;
  group.addEventListener('change', () => { state.groupId = group.value; });
  const showGroup = groupOptions.length > 1 && !state.adopt;

  // The Check button sits beside the input, outside the verdict area every
  // branch of renderVerdict replaces, so it survives its own result.
  function runCheck() {
    const url = input.value.trim();
    state.url = url; state.message = ''; message.textContent = ''; verdict.replaceChildren();
    if (!url) { message.textContent = 'Type the web address you want.'; return Promise.resolve(); }
    return runAction(check, async () => {
      const query = state.groupId === NEW_GROUP_VALUE ? 'group_intent=new'
        : `group=${encodeURIComponent(state.groupId)}`;
      const result = await api(`/api/edge/webapp/onboarding/precheck?${query}&url=${encodeURIComponent(url)}`);
      state.precheck = result;
      renderVerdict(result);
    }, {
      pendingLabel: 'Checking…',
      onError: (error) => { message.textContent = error.message; },
    });
  }

  function useSuggestion(suggested) { input.value = suggested; runCheck(); }

  function settle(verdictResult) {
    // A usable address: carry the domain + label forward and go name the app.
    const normalized = verdictResult.normalized || {};
    state.resolved = {
      domainId: verdictResult.domain?.id, label: normalized.label,
      hostname: normalized.hostname, domainName: normalized.domain_name,
      provider: verdictResult.domain?.provider, records: verdictResult.records || [],
      destination: verdictResult.destination || null,
    };
    state.phase = 'identity'; render();
  }

  function renderVerdict(result) {
    const kind = result.verdict;
    const normalized = result.normalized || {};
    if (kind === 'invalid') { message.textContent = result.reason || 'That address cannot be used.'; return; }
    if (['path', 'apex', 'deep_label'].includes(kind)) {
      verdict.replaceChildren(h('div', {class: 'verdict steer'}, icon('alert'),
        h('div', {}, h('strong', {text: result.reason}),
          result.suggestion ? h('p', {}, 'Try ', h('code', {text: result.suggestion}), ' instead.') : null,
          result.normalized?.scheme_note ? h('small', {text: 'We’ll use a secure https address.'}) : null),
        result.suggestion ? h('button', {class: 'button primary compact', type: 'button', onclick: () => useSuggestion(result.suggestion)}, 'Use it') : null));
      return;
    }
    if (kind === 'taken') {
      const who = result.app ? `already used by your app “${result.app}”` : 'already serving a site';
      verdict.replaceChildren(h('div', {class: 'verdict blocked'}, icon('alert'),
        h('div', {}, h('strong', {text: 'That address is taken'}), h('p', {text: `${normalized.hostname || state.url} is ${who}. Pick a different address.`}))));
      return;
    }
    if (kind === 'conflict') {
      verdict.replaceChildren(h('div', {class: 'verdict blocked'}, icon('alert'),
        h('div', {}, h('strong', {text: 'That address points somewhere else'}),
          h('p', {text: 'It already sends visitors to another service. Choose an address you are not using, or remove the existing record first.'}))));
      return;
    }
    if (kind === 'domain_unknown') { state.phase = 'domain'; render(); return; }
    if (kind === 'configuration_required') {
      // The installation cannot serve app addresses yet. Never invent a record;
      // send the operator to the check that fixes it.
      verdict.replaceChildren(h('div', {class: 'verdict blocked'}, icon('alert'),
        h('div', {}, h('strong', {text: 'This installation isn’t ready to serve app addresses yet'}),
          h('p', {text: result.reason || 'Finish System Setup, then try again.'}),
          setupLink(state, finish))));
      return;
    }
    if (kind === 'ready' && result.domain && result.domain.provider !== 'mojo') {
      // A managed domain: the platform adds the record and issues HTTPS itself.
      // Confirm what will happen before creating anything — no record to copy.
      const host = normalized.hostname || state.url;
      verdict.replaceChildren(h('div', {class: 'verdict ready'}, icon('check'),
        h('div', {}, h('strong', {text: `${result.domain.name} is managed here`}),
          h('p', {text: `We’ll add ${host} automatically, issue and renew its HTTPS certificate, and then show you how to deploy — no DNS changes needed from you.`}),
          h('button', {class: 'button primary compact', type: 'button', onclick: () => settle(result)}, 'Continue'))));
      return;
    }
    if (kind === 'ready' || kind === 'records_needed') { settle(result); return; }
    message.textContent = result.reason || 'That address cannot be used yet.';
  }

  check.addEventListener('click', runCheck);
  input.addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); runCheck(); } });

  return h('div', {class: 'wizard-panel'}, stepBar(state, 0),
    intro('globe', 'What web address do you want?', 'Type the address people will use to reach your app, like myapp.example.com. We will check it and set everything else up.'),
    showGroup ? field('Workspace', group, 'Which workspace this app belongs to.') : null,
    h('div', {class: 'address-entry'}, input, check),
    verdict, message,
    h('div', {class: 'form-actions'}, h('button', {class: 'button ghost', type: 'button', onclick: finish}, 'Cancel')));
}

// ---- phase 2: choosing how the domain is handled ---------------------------

function domainPhase(state, render) {
  const ctx = state.ctx;
  const options = state.precheck?.options || {};
  const hostname = state.precheck?.normalized?.hostname || state.url;
  const concreteGroup = state.groupId !== NEW_GROUP_VALUE;
  const canDns = groupDnsAuthority(ctx, state.groupId);
  const cards = h('div', {class: 'choice-cards'});

  const card = (title, copy, badgeText, onClick, recommended) => h('button', {
    class: `choice-card ${recommended ? 'recommended' : ''}`, type: 'button', onclick: onClick,
  }, badgeText ? h('span', {class: 'choice-badge', text: badgeText}) : null,
    h('strong', {text: title}), h('p', {text: copy}), icon('chevron'));

  // Keep your DNS where it is: the recommended path. Your domain never moves;
  // you add records we show you, and no credentials are handed over.
  if (options.external_available && concreteGroup && canDns) {
    cards.append(card('Keep my DNS where it is',
      'Your domain stays with whoever manages it now. You add one or two records we show you — nothing to hand over, nothing to buy. Easiest and recommended.',
      'Recommended', () => { state.phase = 'external'; render(); }, true));
  }
  if (concreteGroup && canDns) {
    cards.append(card('Use a domain you’ve already set up',
      'Point this app at a domain this workspace already controls. We add the record for you.',
      '', () => openConnectedPicker(state, render)));
  }
  cards.append(card('Buy a new domain',
    'Don’t have a domain yet? Buy one now and we’ll wire it up automatically.',
    '', () => { state.resolved = {mode: 'purchase'}; state.phase = 'identity'; render(); }));

  return h('div', {class: 'wizard-panel'}, stepBar(state, 0),
    intro('dns', 'How should we handle your domain?', `We didn’t find ${hostname} set up in this workspace yet. Choose what fits — in plain terms, none of these need any technical know-how.`),
    cards,
    !options.external_available && concreteGroup ? h('p', {class: 'muted small', text: 'Keeping your DNS elsewhere isn’t available on this installation yet.'}) : null,
    h('div', {class: 'form-actions'},
      h('button', {class: 'button ghost', type: 'button', onclick: () => { state.phase = 'address'; render(); }}, 'Back')));
}

function openConnectedPicker(state, render) {
  const message = h('div', {class: 'form-message', role: 'alert'});
  const select = h('select', {}, h('option', {value: '', text: 'Choose a domain'}));
  const label = h('input', {value: state.precheck?.normalized?.hostname?.split('.')[0] || 'app', autocomplete: 'off'});
  const preview = h('strong', {class: 'hostname-value'});
  const updatePreview = () => {
    const name = select.selectedOptions[0]?.dataset.name;
    preview.textContent = name ? `${(label.value.trim() || 'app')}.${name}` : '—';
  };
  const submit = h('button', {class: 'button primary', type: 'button'}, 'Use this domain');
  const close = openModal({title: 'Use a connected domain', content: h('div', {class: 'wizard-form'},
    field('Domain', select, 'A domain this workspace already controls.'),
    field('Which part of the address', label, 'The word before your domain, like app or www.'),
    h('div', {class: 'hostname-preview'}, h('span', {text: 'Your app will open at'}), preview),
    message, h('div', {class: 'form-actions'}, submit)), wide: false});
  select.addEventListener('change', updatePreview);
  label.addEventListener('input', updatePreview);
  submit.addEventListener('click', () => {
    if (!select.value) { message.textContent = 'Choose a domain first.'; return; }
    const name = select.selectedOptions[0]?.dataset.name;
    state.resolved = {domainId: Number(select.value), label: label.value.trim().toLowerCase(),
      hostname: `${label.value.trim().toLowerCase()}.${name}`, domainName: name, records: []};
    close(); state.phase = 'identity'; render();
  });
  (async () => {
    try {
      listData(await api(`/api/dnsman/domain?group=${encodeURIComponent(state.groupId)}`))
        .filter((row) => row.status === 'active' && row.verified !== false)
        .forEach((row) => select.append(h('option', {value: row.id, text: row.name, 'data-name': row.name})));
      if (select.options.length === 1) message.textContent = 'This workspace has no connected domains yet.';
      updatePreview();
    } catch (error) { message.textContent = error.message; }
  })();
}

// ---- phase 2b: keep-my-DNS external delegation -----------------------------

function externalPhase(state, render) {
  const hostname = state.precheck?.normalized?.hostname || state.url;
  const guessBase = hostname.split('.').slice(1).join('.') || hostname;
  const message = h('div', {class: 'form-message', role: 'alert'});
  const domainInput = h('input', {value: guessBase, autocomplete: 'off'});
  const start = h('button', {class: 'button primary', type: 'button'}, 'Set it up');
  const area = h('div', {class: 'verdict-area'});

  async function verifyDelegation(delegation) {
    const check = h('button', {class: 'button primary', type: 'button'}, 'I’ve added it — check now');
    const status = h('div', {class: 'form-message', role: 'alert'});
    const rec = {type: 'CNAME', name: delegation.source, value: delegation.target};
    area.replaceChildren(h('div', {class: 'records-block'},
      h('p', {text: 'Add this one record at your DNS host. It lets us issue your HTTPS certificate — it does not send any traffic yet.'}),
      recordCard(rec, ''),
      h('p', {class: 'muted small', text: 'If your DNS host has a proxy or an orange cloud (like Cloudflare), set this record to “DNS only” or the check will fail.'}),
      status, h('div', {class: 'form-actions'}, check)));
    check.addEventListener('click', () => runAction(check, async () => {
      status.textContent = '';
      const verified = await apiOnce('/api/dnsman/delegation/verify', {method: 'POST', body: JSON.stringify({delegation: delegation.id})});
      if (verified.state === 'verified') {
        // The domain now exists in this workspace; re-run the address check so
        // we pick up the app record it still needs (usually just one).
        const query = `group=${encodeURIComponent(state.groupId)}`;
        const result = await api(`/api/edge/webapp/onboarding/precheck?${query}&url=${encodeURIComponent(state.url)}`);
        state.precheck = result;
        const normalized = result.normalized || {};
        state.resolved = {domainId: result.domain?.id, label: normalized.label,
          hostname: normalized.hostname, domainName: normalized.domain_name,
          provider: result.domain?.provider, records: result.records || []};
        state.phase = 'identity'; render();
        return;
      }
      status.textContent = 'We can’t see that record yet. DNS can take a few minutes — wait a moment and check again.';
    }, {
      pendingLabel: 'Checking…',
      onError: (error) => { status.textContent = error.message; },
    }));
  }

  start.addEventListener('click', () => runAction(start, async () => {
    message.textContent = '';
    const delegation = await apiOnce('/api/dnsman/delegation/initiate', {method: 'POST', body: JSON.stringify({group: Number(state.groupId), name: domainInput.value.trim().toLowerCase()})});
    await verifyDelegation(delegation);
  }, {
    pendingLabel: 'Setting it up…',
    onError: (error) => { message.textContent = error.message; },
  }));

  return h('div', {class: 'wizard-panel'}, stepBar(state, 0),
    intro('certificate', 'Keep your DNS where it is', 'Confirm your domain and we’ll show you the exact record to add at your DNS host. Nothing here changes where your domain lives.'),
    field('Your domain', domainInput, 'The domain your address ends with — for example example.com.'),
    h('div', {class: 'form-actions'}, h('button', {class: 'button ghost', type: 'button', onclick: () => { state.phase = 'domain'; render(); }}, 'Back'), start),
    message, area);
}

// ---- phase 3: name the app + create the operation --------------------------

function identityPhase(state, render, finish) {
  const adopt = state.adopt;
  const purchase = state.resolved?.mode === 'purchase';
  const baseLabel = state.resolved?.label || state.precheck?.normalized?.hostname?.split('.')[0] || '';
  const message = h('div', {class: 'form-message', role: 'alert'});
  const defaultName = adopt ? (adopt.display_name || adopt.slug)
    : baseLabel ? baseLabel.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()) : '';
  const name = h('input', {value: defaultName, placeholder: 'My app', autocomplete: 'off'});
  const slug = h('input', {value: adopt ? adopt.slug : slugify(baseLabel), autocomplete: 'off'});
  const environment = h('select', {}, ...['production', 'staging', 'preview', 'development'].map((value) => h('option', {value, text: value})));
  if (adopt) environment.value = adopt.environment || 'production';
  const bucket = h('select');
  let slugEdited = Boolean(baseLabel) || Boolean(adopt);
  name.addEventListener('input', () => { if (!slugEdited) slug.value = slugify(name.value); });
  slug.addEventListener('input', () => { slugEdited = true; });
  if (adopt) [name, slug, environment].forEach((control) => { control.disabled = true; });

  const advanced = h('details', {class: 'wizard-advanced'}, h('summary', {text: 'Advanced'}),
    field('Internal identifier', slug, 'A short stable id used behind the scenes. Generated from the name.'),
    field('Environment', environment),
    field('Release storage', bucket, 'Chosen automatically when only one is available.'));

  const submit = h('button', {class: 'button primary', type: 'button'}, adopt ? 'Update address' : 'Create app');
  const heading = adopt
    ? intro('globe', 'Confirm the change', `We’ll move ${adopt.display_name || adopt.slug} to the new address. Nothing else about the app changes.`)
    : purchase
      ? intro('deploy', 'Name your app', 'Give it a friendly name. You’ll buy the domain on the next step.')
      : intro('deploy', 'Name your app', 'Almost done setting things up. Give your app a friendly name — everything technical is filled in for you.');

  async function loadOptions() {
    submit.disabled = true;
    try {
      const query = state.groupId === NEW_GROUP_VALUE ? 'group_intent=new' : `group=${encodeURIComponent(state.groupId)}`;
      const data = await api(`/api/edge/webapp/onboarding/options?${query}`);
      state.options = data;
      bucket.replaceChildren(...(data.buckets || []).map((value) => h('option', {value, text: value})));
      if (adopt) { bucket.value = adopt.bucket; bucket.disabled = true; }
      advanced.hidden = false;
      if (!(data.buckets || []).length) { message.textContent = 'No release storage is configured for this installation.'; return; }
      if (!data.destination) {
        // The connected-domain and purchase paths reach here without a precheck
        // verdict, so this is where an installation that cannot serve app
        // addresses is caught before the app is created.
        message.replaceChildren(
          h('span', {text: data.destination_error || 'This installation isn’t ready to serve app addresses yet. Finish System Setup, then try again.'}),
          setupLink(state, finish));
        return;
      }
      submit.disabled = false;
    } catch (error) { message.textContent = error.message; }
  }
  loadOptions();

  submit.addEventListener('click', () => {
    if (!adopt && !name.value.trim()) { message.textContent = 'Give your app a name.'; return undefined; }
    message.textContent = '';
    const identity = adopt
      ? {display_name: adopt.display_name, slug: adopt.slug, environment: adopt.environment, bucket: adopt.bucket}
      : {display_name: name.value.trim(), slug: slug.value.trim(), environment: environment.value, bucket: bucket.value};
    // Success re-renders the wizard into the run panel and takes this button
    // with it, so there is nothing to restore on that path.
    return runAction(submit, () => createOperation(state, identity, render), {
      pendingLabel: adopt ? 'Updating…' : 'Creating…', restoreOnSuccess: false,
      onError: (error) => { message.textContent = error.message; },
    });
  });

  return h('div', {class: 'wizard-panel'}, stepBar(state, 1), heading,
    field('App name', name, adopt ? 'Changing the address doesn’t rename your app.' : 'The friendly name shown in Admin.'),
    advanced, message,
    h('div', {class: 'form-actions'},
      h('button', {class: 'button ghost', type: 'button', onclick: () => { state.phase = purchase || state.precheck?.verdict === 'domain_unknown' ? 'domain' : 'address'; render(); }}, 'Back'),
      submit));
}

async function createOperation(state, identity, render) {
  const groupPayload = state.groupId === NEW_GROUP_VALUE ? {group_intent: 'new'} : {group: Number.parseInt(state.groupId, 10)};
  const operationId = crypto.randomUUID();
  // Adopting an existing app: the profile must match it exactly, or the backend
  // mints a second app instead of swapping this one's address.
  const adopt = state.adopt;
  const frozenPayload = {
    ...groupPayload, operation_id: operationId,
    slug: identity.slug, display_name: identity.display_name,
    environment: identity.environment, bucket: identity.bucket,
    github_repository: adopt ? (adopt.github_repository || '') : '',
    deployment_ref: adopt ? (adopt.deployment_ref || 'main') : 'main',
    build_output: adopt ? (adopt.build_output || 'dist') : 'dist',
  };
  savePendingDraft({operation_id: operationId, submitted: true, payload: frozenPayload});
  const result = await apiOnce('/api/edge/webapp/onboarding/create', {method: 'POST', body: JSON.stringify(frozenPayload)});
  state.operation = result.operation || result;
  state.githubSkipSubmitted = false;
  state.verifySubmitted = false;
  state.choiceError = '';
  // Kick the app step so the worker starts advancing. The address choice is NOT
  // submitted here: for an existing workspace the create response is still at
  // 'app' — only the worker moves the cursor to 'address' — so submitting on the
  // create response never fired. The run-panel state machine (maybeAutoAdvance)
  // submits it once it observes the 'address' cursor, with that fetch's revision.
  if (state.operation.cursor === 'app') {
    await submitChoiceRecovering(state, 'app', {});
  }
  state.phase = 'run'; render();
  driveRun(state, render);
}

// ---- phase 4: the durable run panel ----------------------------------------

async function submitChoice(state, step, choice) {
  const op = state.operation;
  const result = await apiOnce('/api/edge/webapp/onboarding/choose', {method: 'POST', body: JSON.stringify({
    operation: op.operation_id, revision: op.revision, step, choice,
  })});
  state.operation = result.operation || result;
}

function isWaitStateError(error) {
  // The lease/revision/cursor guards return transient refusals: the worker
  // hasn't moved the cursor yet, is mid-reconcile, or bumped the revision. A
  // re-fetch brings fresh state and the next attempt succeeds, so these are
  // wait-states, not failures. (400s all round, so the message is what tells
  // them apart from a real validation error.)
  const text = String((error && error.message) || '');
  return /current onboarding step is|reconciling|reload before/i.test(text);
}

// Submit a choice, updating state.operation. A transient wait-state refusal is
// swallowed so polling retries with the fresh revision; any other refusal is
// recorded on state.choiceError for the run panel to show honestly. Returns
// true only when the choice was actually recorded.
async function submitChoiceRecovering(state, step, choice) {
  try {
    await submitChoice(state, step, choice);
    return true;
  } catch (error) {
    if (isWaitStateError(error)) return false;
    state.choiceError = error.message;
    return false;
  }
}

function pendingAutoAddress(state) {
  // A resolved managed/connected domain still needs its address choice
  // submitted — but only once the worker has advanced to the address step.
  const op = state.operation;
  return Boolean(op && !state.choiceError && op.cursor === 'address'
    && state.resolved?.domainId && !(op.choices || {}).address);
}

function pendingAutoGithubSkip(state) {
  // GitHub is optional, and a new app configures deploys later on its own
  // page. When the fetched state parks at the github step with no repository
  // configured and no recorded choice, record the explicit skip — the server
  // only retries a step whose choice is recorded, so without this submit the
  // setup would sit waiting forever. Submitted once: the guard flag (and the
  // recorded choice itself) stop repeats; a wait-state refusal leaves the flag
  // unset so polling retries with fresh state, exactly like the address one.
  const op = state.operation;
  return Boolean(op && !state.choiceError && !state.githubSkipSubmitted
    && op.cursor === 'github' && !(op.choices || {}).github
    && !(op.profile || {}).github_repository);
}

function pendingAutoVerify(state) {
  // The final "is it serving?" check needs no input from the person. Record
  // it as soon as the fetched state reaches the verify step so setup finishes
  // on its own — same once-guard and wait-state handling as the github skip.
  const op = state.operation;
  return Boolean(op && !state.choiceError && !state.verifySubmitted
    && op.cursor === 'verify' && !(op.choices || {}).verify);
}

async function maybeAutoAdvance(state) {
  if (state.submittingChoice) return;
  state.submittingChoice = true;
  try {
    if (pendingAutoAddress(state)) {
      await submitChoiceRecovering(state, 'address',
        {domain: state.resolved.domainId, label: state.resolved.label});
    } else if (pendingAutoGithubSkip(state)) {
      if (await submitChoiceRecovering(state, 'github', {skip: true})) state.githubSkipSubmitted = true;
    } else if (pendingAutoVerify(state)) {
      if (await submitChoiceRecovering(state, 'verify', {})) state.verifySubmitted = true;
    }
  } finally { state.submittingChoice = false; }
}

function isAdvancing(op) {
  // Poll while the server is actively reconciling, or while a scheduled retry
  // (certificate issuance, propagation) is pending. A parked wait needs the
  // user, so we stop and show them what to do.
  if (!op) return false;
  if (op.status === 'active') return true;
  if (op.status === 'waiting' && op.next_attempt_at) return true;
  return false;
}

async function driveRun(state, render) {
  if (state.pollTimer) { clearTimeout(state.pollTimer); state.pollTimer = null; }
  // Poll while the server is reconciling OR while any auto-submitted choice is
  // still pending — the resolved-domain address, the github skip, or the final
  // verify (the worker moves the cursor after the create response, so this
  // loop is where each of those submits fires).
  const keepPolling = () => isAdvancing(state.operation) || pendingAutoAddress(state)
    || pendingAutoGithubSkip(state) || pendingAutoVerify(state);
  const step = async (fetchFirst) => {
    if (state.disposed || !state.operation) return;
    if (fetchFirst) {
      try {
        state.operation = await api(`/api/edge/webapp/onboarding/detail?operation=${encodeURIComponent(state.operation.operation_id)}`);
      } catch (_) { render(); return; }  // keep the last state; the user can retry
    }
    await maybeAutoAdvance(state);
    // Report the run globally from the state this loop just read. A run parked
    // on a question is not "running" — it is waiting for the person in front of
    // it — and syncOnboardingOperation clears it in exactly that case.
    syncOnboardingOperation(state.operation, {displayName: state.appName || ''});
    render();
    if (!state.disposed && keepPolling()) {
      state.pollTimer = setTimeout(() => step(true), fetchFirst ? 1800 : 1200);
    }
  };
  step(false);
}

function runPhase(state, render, finish) {
  const op = state.operation;
  if (!op) return h('div', {class: 'wizard-panel'}, h('p', {text: 'Loading…'}));
  if (op.status === 'succeeded' || op.cursor === 'complete') return donePanel(state, finish);
  if (op.status === 'failed') return failedPanel(state, render, finish);

  const index = op.cursor === 'github' ? 2 : op.cursor === 'verify' ? 3 : 1;
  // A step the run loop advances by itself renders as progress, not a form:
  // the github form appears only when a repository question actually needs the
  // person, and the verify button only when an automatic check parked.
  const parked = op.status === 'waiting' && !op.next_attempt_at;
  let body;
  if (op.cursor === 'app') body = appStep(state, render);
  else if (op.cursor === 'address') body = addressStep(state, render);
  else if (op.cursor === 'github') {
    body = pendingAutoGithubSkip(state) || (op.choices || {}).github?.skip
      ? busyRow('Finishing setup', 'Getting your app ready to go live…')
      : githubStep(state, render);
  } else if (op.cursor === 'verify') {
    body = pendingAutoVerify(state) || ((op.choices || {}).verify && !parked)
      ? busyRow('Checking your address', 'Making sure your app answers securely at its new address…')
      : verifyStep(state, render);
  } else body = h('p', {text: 'Setting things up…'});

  // Technical details for support: a status badge per evidence key, plus the
  // address specifics (destination + provenance, provider, write capability,
  // DNS/cert status) and the next automatic retry. All server-sanitized.
  const detailRows = Object.entries(op.evidence || {}).map(([key, value]) =>
    h('div', {}, h('dt', {text: key}), h('dd', {}, badge(value?.status || 'recorded'))));
  const address = (op.evidence || {}).address || {};
  const addRow = (label, value) => {
    if (value != null && value !== '') detailRows.push(h('div', {}, h('dt', {text: label}), h('dd', {text: String(value)})));
  };
  addRow('Address', address.hostname);
  if (address.destination) addRow('Destination', `${address.destination.value} (${address.destination.provenance})`);
  addRow('Provider', address.provider);
  if (address.provider != null) addRow('Platform-managed DNS', address.writable ? 'yes' : 'no');
  addRow('DNS', address.dns);
  addRow('Certificate', address.certificate);
  addRow('Next automatic retry', op.next_attempt_at);
  return h('div', {class: 'wizard-panel'}, stepBar(state, index),
    h('div', {class: 'run-heading'}, h('strong', {text: op.profile?.display_name || 'Your app'}),
      h('button', {class: 'button ghost compact', type: 'button', onclick: (event) => runAction(event.currentTarget,
        () => refresh(state, render), {announceLabel: 'Checking again…'})}, icon('refresh'), 'Check again')),
    (state.choiceError || op.last_error) ? h('div', {class: 'callout warning'}, icon('alert'), h('p', {text: state.choiceError || op.last_error})) : null,
    body,
    detailRows.length ? h('details', {class: 'run-evidence'}, h('summary', {text: 'Technical details'}), h('dl', {class: 'details'}, ...detailRows)) : null);
}

async function refresh(state, render) {
  try { state.operation = await api(`/api/edge/webapp/onboarding/detail?operation=${encodeURIComponent(state.operation.operation_id)}`); driveRun(state, render); }
  catch (_) { render(); }
}

function busyRow(title, detail) {
  return h('div', {class: 'run-busy'}, icon('activity'), h('div', {}, h('strong', {text: title}), h('p', {text: detail})));
}

function appStep(state, render) {
  const message = h('div', {class: 'form-message', role: 'alert'});
  return h('div', {class: 'wizard-choice'}, intro('deploy', 'Create your app', 'We’ll set up your app with the details you entered.'),
    h('button', {class: 'button primary', type: 'button', onclick: (event) => runAction(event.currentTarget, async () => {
      await submitChoice(state, 'app', {}); driveRun(state, render);
    }, {pendingLabel: 'Setting up…', onError: (error) => { message.textContent = error.message; }})}, 'Continue'), message);
}

function addressStep(state, render) {
  const op = state.operation;
  const evidence = (op.evidence || {}).address || {};
  const purchase = state.resolved?.mode === 'purchase' && !op.resources?.domain;
  const external = state.resolved?.provider === 'mojo';

  if (purchase) return purchaseStep(state, render);

  // A submit was refused for a real reason (not a transient wait-state): show it
  // honestly and offer a retry — never fall back to a fabricated records panel.
  if (state.choiceError) {
    const retry = h('button', {class: 'button primary', type: 'button'}, 'Try again');
    retry.addEventListener('click', () => runAction(retry, async () => {
      state.choiceError = '';
      await submitChoiceRecovering(state, 'address', addressChoicePayload(state));
      driveRun(state, render);
    }, {pendingLabel: 'Trying again…', restoreOnSuccess: false}));
    return h('div', {class: 'wizard-choice'},
      intro('alert', 'We couldn’t start setting up your address', state.choiceError),
      h('div', {class: 'form-actions'}, retry));
  }

  if (isAdvancing(op) || pendingAutoAddress(state)) {
    const issuing = evidence.dns === 'verified' && evidence.certificate && evidence.certificate !== 'failed';
    if (issuing) return busyRow('Issuing your HTTPS certificate',
      'This can take a few minutes. You can leave this open — it keeps checking.');
    return busyRow('Setting up your address',
      external ? 'Checking your DNS record and getting your address ready…'
        : 'Adding your address record and issuing HTTPS — automatic, no DNS changes needed from you.');
  }
  // Records come from server evidence. The precheck fallback survives ONLY for a
  // genuinely external domain whose records are complete — a managed domain
  // never renders a manual instruction the platform performs itself.
  let records = evidence.records || [];
  if (!records.length && external) {
    records = (state.resolved?.records || []).filter((r) => r && r.type && r.name && r.value);
  }
  if (records.length) {
    const message = h('div', {class: 'form-message', role: 'alert'});
    const check = h('button', {class: 'button primary', type: 'button'}, 'I’ve added them — check now');
    check.addEventListener('click', () => runAction(check, async () => {
      message.textContent = '';
      await submitChoice(state, 'address', addressChoicePayload(state)); driveRun(state, render);
    }, {pendingLabel: 'Checking…', onError: (error) => { message.textContent = error.message; }}));
    const certFailed = evidence.certificate === 'failed';
    return h('div', {class: 'wizard-choice'},
      intro('dns', certFailed ? 'One more record to add' : 'Add these records at your DNS host',
        certFailed ? 'The certificate couldn’t be issued yet — please make sure both records below are in place at your DNS host.'
          : 'Copy each record into your DNS host exactly as shown. When they’re in, check again.'),
      ...records.map((record, position) => recordCard(record, recordPurpose(position))),
      h('p', {class: 'muted small', text: 'Using Cloudflare or a proxy? Set these to “DNS only” (grey cloud) or the check will fail.'}),
      message, h('div', {class: 'form-actions'}, check));
  }
  // Waiting on a certificate that just needs time; offer a manual re-check.
  const message = h('div', {class: 'form-message', role: 'alert'});
  return h('div', {class: 'wizard-choice'}, busyRow('Still finishing your address',
    'Your certificate is taking a little longer than usual. Give it a moment, then check again.'),
    h('div', {class: 'form-actions'}, h('button', {class: 'button primary', type: 'button', onclick: (event) => runAction(event.currentTarget, async () => {
      await submitChoice(state, 'address', addressChoicePayload(state)); driveRun(state, render);
    }, {pendingLabel: 'Checking…', onError: (error) => { message.textContent = error.message; }})}, 'Check now')), message);
}

function addressChoicePayload(state) {
  const op = state.operation;
  const chosen = (op.choices || {}).address || {};
  if (chosen.domain) return {domain: chosen.domain, label: chosen.label};
  if (state.resolved?.domainId) return {domain: state.resolved.domainId, label: state.resolved.label};
  return {domain: chosen.domain, label: chosen.label};
}

function purchaseStep(state, render) {
  const message = h('div', {class: 'form-message', role: 'alert'});
  const guessBase = state.precheck?.normalized?.hostname?.split('.').slice(1).join('.') || '';
  const guessLabel = state.precheck?.normalized?.hostname?.split('.')[0] || 'www';
  const nameInput = h('input', {value: guessBase, placeholder: 'example.com', autocomplete: 'off'});
  const labelInput = h('input', {value: guessLabel, autocomplete: 'off'});
  const quoteButton = h('button', {class: 'button', type: 'button'}, 'Get a price');
  const confirm = h('div');
  let quote = null;
  quoteButton.addEventListener('click', () => runAction(quoteButton, async () => {
    message.textContent = '';
    quote = await apiOnce('/api/dnsman/registrar/quote', {method: 'POST', body: JSON.stringify({group: state.groupId, domain: nameInput.value.trim(), years: 1})});
    const domainConfirm = h('input', {autocomplete: 'off'});
    const priceConfirm = h('input', {inputmode: 'decimal', autocomplete: 'off'});
    confirm.replaceChildren(
      h('div', {class: 'callout warning'}, icon('alert'), h('div', {},
        h('strong', {text: `${quote.name} — ${quote.price} ${quote.currency}`}),
        h('p', {text: `This offer expires ${formatDate(quote.expires)}. Type the domain and price to confirm the purchase.`}))),
      field('Confirm the domain', domainConfirm), field('Confirm the price', priceConfirm),
      // This spends money on a one-shot, non-retried call. The scrim is the
      // point: the operator must not be able to click anything else — least of
      // all this button again — while the registrar is deciding.
      h('button', {class: 'button danger', type: 'button', onclick: (event) => runAction(event.currentTarget, async () => {
        await submitChoice(state, 'address', {purchase: quote.purchase, confirm_token: quote.token,
          confirm_domain: domainConfirm.value, confirm_price: priceConfirm.value, label: labelInput.value.trim().toLowerCase()});
        quote.token = null; quote = null; driveRun(state, render);
      }, {
        busy: {title: 'Registering your domain…', detail: 'This is a single attempt — it is not retried.'},
        restoreOnSuccess: false,
        onError: (error) => { message.textContent = error.message; },
      })}, 'Buy and continue'));
  }, {
    pendingLabel: 'Getting a price…',
    onError: (error) => { message.textContent = error.message; },
  }));
  return h('div', {class: 'wizard-choice'},
    intro('globe', 'Buy your domain', 'We’ll register it and wire it up automatically. You confirm the exact domain and price before anything is bought.'),
    field('Domain to buy', nameInput), field('Which part of the address', labelInput, 'The word before your domain, like app or www.'),
    quoteButton, confirm, message);
}

function githubStep(state, render) {
  const op = state.operation;
  const profile = op.profile || {};
  const evidence = (op.evidence || {}).github || {};
  const repository = h('input', {value: profile.github_repository || '', placeholder: 'your-org/your-repo', autocomplete: 'off'});
  const ref = h('input', {value: profile.deployment_ref || 'main', autocomplete: 'off'});
  const output = h('input', {value: profile.build_output || 'dist', autocomplete: 'off'});
  const message = h('div', {class: 'form-message', role: 'alert'});
  const skip = h('details', {class: 'wizard-advanced'}, h('summary', {text: 'Not using GitHub?'}),
    h('p', {class: 'muted small', text: 'You can deploy from any CI or by hand using the release instructions. Continue here to finish setup, then open Setup on the app to see how.'}),
    h('button', {class: 'button ghost compact', type: 'button', onclick: (event) => runAction(event.currentTarget, async () => {
      await submitChoice(state, 'github', {repository: repository.value, ref: ref.value, output: output.value, attest_unavailable: true}); driveRun(state, render);
    }, {pendingLabel: 'Skipping…', onError: (error) => { message.textContent = error.message; }})}, 'Skip GitHub for now'));

  return h('div', {class: 'wizard-choice'},
    intro('deploy', 'Connect GitHub', 'Point us at your repository. When you push, GitHub builds your app and deploys it — one secret and one file, set up on the next screen.'),
    evidence.status === 'unavailable' ? h('div', {class: 'callout warning'}, icon('alert'), h('p', {text: evidence.reason || 'We couldn’t verify that repository. Check the name, or continue without GitHub below.'})) : null,
    field('Repository', repository, 'For example your-org/your-repo.'),
    field('Branch to deploy from', ref), field('Build output folder', output, 'The folder your build produces, like dist or build.'),
    h('button', {class: 'button primary', type: 'button', onclick: (event) => runAction(event.currentTarget, async () => {
      await submitChoice(state, 'github', {repository: repository.value, ref: ref.value, output: output.value, attest_unavailable: false}); driveRun(state, render);
    }, {pendingLabel: 'Connecting…', onError: (error) => { message.textContent = error.message; }})}, 'Connect and continue'), skip, message);
}

function verifyStep(state, render) {
  const message = h('div', {class: 'form-message', role: 'alert'});
  return h('div', {class: 'wizard-choice'},
    intro('check', 'Go live', 'Everything is set up. We’ll confirm your address is serving your app over a secure connection.'),
    h('button', {class: 'button primary', type: 'button', onclick: (event) => runAction(event.currentTarget, async () => {
      await submitChoice(state, 'verify', {}); driveRun(state, render);
    }, {pendingLabel: 'Checking…', onError: (error) => { message.textContent = error.message; }})}, 'Check my address'), message);
}

function donePanel(state, finish) {
  const op = state.operation;
  const host = (op.evidence || {}).address?.hostname;
  const app = op.resources?.webapp;
  // An adopted app that already had a release is not starting from a welcome
  // page — the run put its current deploy back on the new address.
  const restoredDeploy = state.adopt?.current_release;
  if (state.nameFirst && app && !state.landed) {
    // A name-first run made exactly one decision, and the outcome is already
    // true: the app is live. Ending on a congratulations screen with a button
    // makes someone acknowledge a fact instead of acting on it — so close the
    // wizard and land on the app's own Set up deploys tab, where its live-now
    // banner says the same thing beside the thing to do next. The guard makes
    // it once-only: render() runs again on every poll tick.
    state.landed = true;
    queueMicrotask(() => {
      // finish() clears the durable draft first, so the arrival is a clean app
      // page rather than one offering to resume a run that already finished.
      finish();
      location.hash = routeHref('apps', {webapp: app, tab: 'setup'});
    });
    // Never visibly painted — the hash change lands in the same frame.
    return h('div', {class: 'wizard-panel'}, h('p', {text: 'Taking you to your app…'}));
  }
  // Adopt / repair, and any run with no app to land on: the explicit hand-off.
  return h('div', {class: 'wizard-panel'}, stepBar(state, 4),
    h('div', {class: 'result-state success'}, icon('check'),
      h('div', {}, h('strong', {text: 'You’re live!'}),
        h('p', {}, 'Your app is up', host ? h('span', {}, ' at ', h('code', {text: host})) : null,
          restoredDeploy ? ', applying its current deploy across your fleet.'
            : ', serving a welcome page until your first deploy.'))),
    h('div', {class: 'form-actions'},
      h('button', {class: 'button primary', type: 'button', onclick: () => { finish(); if (app) location.hash = routeHref('apps', {webapp: app, tab: 'setup'}); }}, 'Set up deploys'),
      h('button', {class: 'button ghost', type: 'button', onclick: finish}, 'Done')));
}

function failedPanel(state, render, finish) {
  return h('div', {class: 'wizard-panel'},
    h('div', {class: 'result-state danger'}, icon('alert'),
      h('div', {}, h('strong', {text: 'Setup couldn’t finish'}),
        h('p', {text: state.operation.last_error || 'Something went wrong. You can start over and try again.'}))),
    h('div', {class: 'form-actions'},
      h('button', {class: 'button ghost', type: 'button', onclick: (event) => runAction(event.currentTarget, async () => {
        try { await apiOnce('/api/edge/webapp/onboarding/cancel', {method: 'POST', body: JSON.stringify({operation: state.operation.operation_id})}); } catch (_) { /* best effort */ }
        removeOnboardingOperation(state.operation.operation_id);
        clearPendingDraft(); state.operation = null;
        state.phase = state.adopt || !(state.ctx.webapp_groups || []).length ? 'address' : 'name';
        state.resolved = null; render();
      }, {pendingLabel: 'Starting over…', restoreOnSuccess: false})}, 'Start over'),
      h('button', {class: 'button ghost', type: 'button', onclick: finish}, 'Close')));
}

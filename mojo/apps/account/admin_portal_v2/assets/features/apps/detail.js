// One app's own page, keyed on `?webapp=<id>` with the tab in `?tab=`, so "go
// set up deploys" is a link anyone can follow or share.
//
// Ported from v1's webappDetailPage with all six tabs and every flow intact.
// What v2 changes is the chrome and one sentence:
//
//   - the back pill replaces v1's "← All deployments" link;
//   - the header says WHICH deploy is serving, and says so truthfully. v1's
//     header read "serving your latest deploy" — which after a rollback is the
//     one thing that is not true. The version comes from `current_release`; the
//     "rolled back" state comes from `latest_deployment.status`; and the deploy
//     it was rolled back FROM is named only once the deployment read proves it,
//     never before.
import {api, badge, formatDate, h, icon, listData, statusTone, TableView} from '../../core.js';
import {backPill} from '../../app.js';
import {confirmAction, openModal} from '../../components/overlays.js';
import {decodeRouteState, restoreReturnLocation, routeHref} from '../../components/routes.js';
import {copyButton, loadInto, runAction} from '../../components/actions.js';
import {errorState, sectionTabs} from '../../components/views.js';
import {recordPurpose} from './wizard.js';
import {servingPanel} from './serving.js';
import {setupPanel} from './setup.js';
import {syncAppDeployment} from './operations.js';
import {
  actionFailed, certBadge, changeAddressFor, deleteWebApp, detailGrid, httpsLink,
  keyDialog, revokeKey, rollbackTo,
} from './shared.js';

const PAGE_TABS = [
  ['overview', 'Overview'], ['deploys', 'Deploys'],
  ['setup', 'Set up deploys'], ['serving', 'Serving'],
  ['key', 'Deploy key'], ['danger', 'Danger'],
];
// Releases an operator may make live again. `live` is here because a release
// can be the live one on another node set; `uploaded` never served, and is a
// promote rather than a rollback — see rollbackTo's `promote` flag.
const PROMOTABLE = new Set(['uploaded', 'live', 'superseded']);

// ---------------------------------------------------------------------------
// Addresses: the app's own address, plus any address of your own you point at
// it. Both serve the identical release. Everything shown here is server
// evidence — the browser never decides what your DNS host needs.
// ---------------------------------------------------------------------------

// Type / Name / Value, rendered verbatim from the response. Composing a record
// here would be a guess about someone else's DNS host.
function recordsTable(records) {
  const cell = (value) => h('div', {class: 'record-cell'}, h('code', {text: value}), copyButton(value));
  const text = (value) => (Array.isArray(value) ? value.join(', ') : String(value ?? ''));
  const rows = (records || []).map((record, position) => ({...record, purpose: recordPurpose(position)}));
  return new TableView({columns: [
    {label: 'What it does', render: (row) => h('span', {text: row.purpose})},
    {label: 'Type', render: (record) => cell(text(record.type))},
    {label: 'Name', render: (record) => cell(text(record.name))},
    {label: 'Value', render: (record) => cell(text(record.value))},
  ], rows, empty: 'Nothing to add — check again.'}).render();
}

function removeAddress(app, row, reload) {
  return confirmAction({title: `Remove ${row.hostname}?`, danger: true, confirmLabel: 'Remove address',
    copy: 'Visitors using this address will stop reaching your app. Your app’s own address keeps serving, and nothing is deleted.'}).then((answer) => {
    if (!answer.confirmed) return undefined;
    // The Remove button lives in the addresses table that reload() rebuilds,
    // so there is nothing here to pin an inline state to.
    return runAction(null, async () => {
      await api('/api/edge/webapp/detach_domain', {method: 'POST', body: JSON.stringify({webapp: app.id, vhost: row.vhost})});
      await reload();
    }, {
      key: `webapp-detach-domain:${app.id}:${row.vhost}`,
      busy: {title: `Removing ${row.hostname}…`, detail: 'Your app’s own address keeps serving.'},
      onError: (error) => actionFailed('The address was not removed', error),
    });
  });
}

// One card, one call: every address this app answers on, its own first.
function addressesCard(ctx, app, reload) {
  const manage = ctx.capabilities.manage_webapps;
  const body = h('div', {});
  const card = h('section', {class: 'address-block'},
    h('div', {class: 'run-heading'},
      h('h3', {class: 'section-subhead', text: 'Addresses'}),
      manage ? h('button', {class: 'button compact', type: 'button', onclick: () => addAddressDialog(ctx, app, reload)},
        icon('globe'), 'Add a custom domain') : null),
    body);
  // A failed read used to leave a bare sentence with no way to try again; the
  // shared loader owns both the wait and the in-panel retry.
  loadInto(body, async (current) => {
    const payload = await api(`/api/edge/webapp/aliases?webapp=${encodeURIComponent(app.id)}`);
    if (!current()) return;
    const rows = payload.addresses || [];
    body.replaceChildren(new TableView({columns: [
      {label: 'Address', render: (row) => h('div', {class: 'address-cell'}, h('code', {text: row.hostname}),
        row.role === 'primary' ? badge('Your app’s address', 'neutral') : null)},
      {label: 'HTTPS', render: (row) => certBadge(row.certificate)},
      {label: '', render: (row) => (manage && row.role === 'alias'
        ? h('button', {class: 'button ghost compact danger-text', type: 'button',
          onclick: () => removeAddress(app, row, reload)}, 'Remove') : null)},
    ], rows, empty: 'No addresses yet.'}).render());
  }, {message: 'Checking your addresses…'});
  return card;
}

// Add one address you already own. Every branch below is keyed on the server's
// own status — including whether this domain's DNS is ours to write.
function addAddressDialog(ctx, app, reload) {
  const input = h('input', {placeholder: 'www.example.com', autocomplete: 'off', spellcheck: 'false'});
  const message = h('div', {class: 'form-message', role: 'alert'});
  const area = h('div', {class: 'verdict-area'});
  const submit = h('button', {class: 'button primary', type: 'button'}, icon('globe'), 'Add address');
  // Opening this dialog and thinking better of it needs a way out that is not
  // the Escape key — every other form in the feature offers one.
  const cancel = h('button', {class: 'button ghost', type: 'button', onclick: () => close()}, 'Cancel');
  let done = false;
  // ONE counter over both the passive hint and the real submit. They race: a
  // preview in flight when Add lands must never paint over Add's result.
  let seq = 0;
  let previewTimer = null;
  const content = h('div', {},
    h('p', {class: 'modal-copy', text: 'Point an address you already own at this app. It serves exactly what your app’s own address serves.'}),
    h('label', {class: 'field'}, h('span', {text: 'Address'}), input),
    message, area, h('div', {class: 'form-actions'}, cancel, submit));
  const close = openModal({title: `Add a custom domain to ${app.slug}`,
    subtitle: 'One address, one label — like www or shop.', content,
    onClose: () => { if (done) reload(); }});

  const checkButton = (label) => h('button', {class: 'button primary', type: 'button',
    onclick: () => run(false)}, label);

  function paint(result) {
    const managed = result.dns === 'managed';
    if (result.status === 'attached') {
      done = true;
      area.replaceChildren(h('div', {class: 'result-state success'}, icon('check'),
        h('div', {}, h('strong', {text: `${result.hostname} is live`}),
          h('p', {text: managed
            ? 'This address now serves your app — and this domain’s DNS is managed here — records and HTTPS are handled for you.'
            : 'This address now serves your app, with HTTPS.'}))));
      // Two ways to close a finished dialog is one too many, and "Cancel" over
      // a done deal reads as if it could undo it.
      cancel.remove();
      submit.replaceWith(h('button', {class: 'button primary', type: 'button', onclick: () => close()}, 'Done'));
      return;
    }
    if (result.status === 'needs_domain') {
      // No records: nothing can be published until the domain itself is
      // connected here, which is its own deliberate flow.
      area.replaceChildren(h('div', {class: 'verdict steer'}, icon('globe'),
        h('div', {}, h('strong', {text: result.reason}),
          h('p', {}, 'Connect it first, then add this address. ',
            h('a', {href: routeHref('domains')}, 'Open Domains')))));
      return;
    }
    if (result.status === 'records_needed' || result.status === 'certificate_failed') {
      const failed = result.status === 'certificate_failed';
      area.replaceChildren(h('div', {class: 'records-block'},
        h('p', {text: result.reason}),
        recordsTable(result.records),
        h('p', {class: 'muted small', text: 'Using Cloudflare or a proxy? Set these to “DNS only” (grey cloud) or the check will fail.'}),
        h('div', {class: 'form-actions'},
          checkButton('I’ve added them — check now'),
          failed ? h('button', {class: 'button', type: 'button', onclick: () => run(true)}, 'Try again') : null)));
      return;
    }
    if (result.status === 'certificate_pending') {
      area.replaceChildren(
        h('div', {class: 'run-busy'}, icon('lock'),
          h('div', {}, h('strong', {text: 'Setting up HTTPS'}), h('p', {text: result.reason}))),
        h('div', {class: 'form-actions'}, checkButton('Check now')));
      return;
    }
    // Any status this build does not know: show what the server said, plainly.
    area.replaceChildren(h('div', {class: 'verdict'}, icon('alert'),
      h('div', {}, h('strong', {text: result.reason || result.status}))));
  }

  // The same verdict, before anything is written. Only `ready` is new copy:
  // needs_domain reuses paint's steer node verbatim, and `unusable` falls into
  // its unknown-status branch, so each sentence exists exactly once.
  function paintPreview(result) {
    if (result.status !== 'ready') { paint(result); return; }
    const managed = result.dns === 'managed';
    area.replaceChildren(managed
      ? h('div', {class: 'verdict ready'}, icon('check'),
        h('div', {}, h('strong', {text: `${result.domain.name} is managed here`}),
          h('p', {text: `We’ll point ${result.hostname} at your app and issue its HTTPS certificate. Nothing to add at your DNS host.`})))
      : h('div', {class: 'verdict steer'}, icon('dns'),
        h('div', {}, h('strong', {text: `${result.domain.name}’s DNS is at your own host`}),
          h('p', {text: 'Add the address and we’ll show you the exact record to publish, then check it for you.'}))));
  }

  // A hint, not an action: nothing is triggered, so there is no pending state
  // to pin and no error to report. A hint that cannot be given is simply not
  // given — the address still submits, and the write's own answer is the truth.
  function runPreview() {
    if (previewTimer) { clearTimeout(previewTimer); previewTimer = null; }
    if (done) return;
    const hostname = input.value.trim();
    if (!hostname) { area.replaceChildren(); return; }
    const mine = ++seq;
    api(`/api/edge/webapp/attach_preview?webapp=${encodeURIComponent(app.id)}&hostname=${encodeURIComponent(hostname)}`)
      .then((result) => { if (mine === seq && !done) paintPreview(result); })
      .catch(() => { /* silent by design */ });
  }

  function schedulePreview() {
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(runPreview, 350);
  }

  // The pending state goes on `submit`, never on the Check / Try again buttons
  // paint() renders: those live inside `area`, which every branch replaces.
  // submit sits outside it and outlives each result.
  function run(retryCertificate) {
    const hostname = input.value.trim();
    if (!hostname) { message.textContent = 'Type the address you want to point at this app.'; return Promise.resolve(); }
    message.textContent = '';
    area.replaceChildren();
    // Bump the shared counter: a preview already in flight is now stale, and
    // must not repaint the hint over whatever this call comes back with.
    if (previewTimer) { clearTimeout(previewTimer); previewTimer = null; }
    seq += 1;
    const payload = {webapp: app.id, hostname};
    // Only the explicit repair sets this — a plain check must never mint a new
    // certificate order.
    if (retryCertificate) payload.retry_certificate = true;
    return runAction(submit, async () => {
      const result = await api('/api/edge/webapp/attach_domain', {method: 'POST', body: JSON.stringify(payload)});
      message.textContent = '';
      paint(result);
    }, {
      pendingLabel: 'Checking…',
      // A refusal is the server's sentence, verbatim — never reworded here.
      onError: (error) => { message.textContent = error.message; },
    });
  }

  submit.addEventListener('click', () => run(false));
  input.addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); run(false); } });
  input.addEventListener('input', schedulePreview);
  // Leaving the field is the moment someone has finished typing a name; answer
  // then rather than making them wait out the debounce.
  input.addEventListener('blur', runPreview);
}

// ---------------------------------------------------------------------------
// the truthful header
// ---------------------------------------------------------------------------

/**
 * What this app is serving, in one line, from what the summary actually proves.
 *
 * The rule this exists for: after a rollback, the live release is deliberately
 * NOT the newest one, so "serving your latest deploy" is false exactly when it
 * matters most. Every branch here names a version or admits it cannot.
 */
function servingFacts(summary) {
  const address = summary.address || {};
  const release = summary.current_release;
  const deployment = summary.latest_deployment;
  if (!address.hostname) {
    return {line: 'This app is not reachable — setup never finished.', tone: 'warn'};
  }
  if (!release) {
    return {line: `${address.hostname} — serving a welcome page, nothing deployed yet`, tone: 'warn'};
  }
  const version = release.version || `#${release.id}`;
  const status = String(deployment?.status || '');
  if (status === 'rolled_back') {
    return {
      line: `${address.hostname} — serving ${version}`,
      // Named from the deployment record, not guessed: this deploy failed and
      // the fleet was put back on what `current_release` names.
      badge: {label: 'rolled back', tone: 'warning'},
      note: 'the last deploy failed and the fleet was put back',
      tone: 'danger',
    };
  }
  if (status === 'failed') {
    return {
      line: `${address.hostname} — serving ${version}`,
      badge: {label: 'last deploy failed', tone: 'danger'},
      note: 'the newest deploy did not land, and the rollback did not finish either',
      tone: 'danger',
    };
  }
  return {line: `${address.hostname} — serving ${version}`, tone: 'ok'};
}

/**
 * The one fact the summary cannot supply: WHICH deploy this app was rolled back
 * from. `latest_deployment` on the detail summary carries no release, so the
 * name is filled in only after the deployment list read proves it — and the
 * line stands on its own until then rather than guessing at a number.
 */
async function nameRolledBackFrom(app, summary, slot) {
  if (String(summary.latest_deployment?.status || '') !== 'rolled_back') return;
  try {
    const rows = listData(await api(
      `/api/edge/deployment?webapp=${encodeURIComponent(app.id)}&graph=list&sort=-created&size=5`));
    const newest = rows[0];
    const from = newest?.release?.version;
    const serving = summary.current_release?.version;
    // Only when the failed deploy really carried a different release than the
    // one serving now — otherwise there is nothing to name.
    if (!from || !serving || String(from) === String(serving) || !slot.isConnected) return;
    slot.textContent = ` — rolled back from ${from}`;
  } catch (_) {
    // The header already says it was rolled back. Failing to name the deploy it
    // came from is not worth an error state on a heading.
  }
}

// ---------------------------------------------------------------------------
// tab bodies
// ---------------------------------------------------------------------------

// `current` comes from loadInto's generation token: two fast tab clicks are
// two different buttons, so the re-entry guard does not cover them. The
// loser must not paint over the winner.
async function manageSection(ctx, app, summary, section, body, reload, current = () => true) {
  const manage = ctx.capabilities.manage_webapps;
  const address = summary.address || {};
  if (section === 'overview') {
    const healthValue = h('span', {class: 'muted', text: 'Checking…'});
    const certificate = address.certificate;
    body.replaceChildren(detailGrid([
      ['Address', address.hostname ? h('div', {class: 'address-cell'}, h('code', {text: address.hostname}),
        httpsLink(address.https_origin) ? h('a', {class: 'button ghost compact', href: httpsLink(address.https_origin), target: '_blank', rel: 'noopener'}, 'Open') : null) : 'No address yet'],
      ['Working right now', healthValue],
      ['SSL', certificate
        ? `${certificate.status}${certificate.not_after ? ` · renews or expires ${formatDate(certificate.not_after)}` : ''}`
        : address.hostname ? 'No certificate on record' : 'Needs an address first'],
      ['Live version', summary.current_release ? `${summary.current_release.version || summary.current_release.id} · ${summary.current_release.status}` : 'No deploys yet'],
      ['Environment', summary.webapp?.environment],
      ['Repository', summary.webapp?.repository || 'Not connected'],
      ['Deploy key', summary.deployment_key?.linked && summary.deployment_key?.active ? 'Active' : 'Not set up'],
    ]));
    if (!address.hostname && manage) {
      // An addressless app's next step is picking an address — offer it right
      // here so reaching the other tabs doesn't hide it.
      body.append(h('div', {class: 'row-actions'},
        h('button', {class: 'button primary', onclick: (event) => runAction(event.currentTarget,
          () => changeAddressFor(ctx, app, reload), {pendingLabel: 'Opening…'})}, icon('globe'), 'Set address')));
    }
    if (address.hostname) {
      // Every address this app answers on, and the way to add one of your own.
      body.append(addressesCard(ctx, app, reload));
      api(`/api/edge/webapp/health?webapp=${encodeURIComponent(app.id)}`)
        .then((res) => { const tone = res.status === 'healthy' ? 'success' : res.status === 'not_configured' ? 'neutral' : 'danger';
          const label = res.status === 'healthy' ? 'Yes — responding' : res.status === 'not_configured' ? 'Not serving yet' : `No — ${res.detail || 'unreachable'}`;
          healthValue.replaceWith(badge(label, tone)); })
        .catch(() => { healthValue.replaceWith(badge('Couldn’t check', 'neutral')); });
    } else { healthValue.replaceWith(badge('Not serving yet', 'neutral')); }
    return;
  }
  if (section === 'deploys') {
    const currentId = summary.current_release?.id;
    const releases = listData(await api(`/api/edge/release?webapp=${encodeURIComponent(app.id)}&sort=-created&size=25`));
    const deployments = listData(await api(`/api/edge/deployment?webapp=${encodeURIComponent(app.id)}&graph=list&sort=-created&size=15`));
    if (!current()) return;
    // The newest deployment is this app's own report of whether anything is
    // running right now — feed the banner from it.
    syncAppDeployment(app, deployments[0]);
    const releaseTable = new TableView({columns: [
      {label: 'Version', render: (r) => h('div', {}, h('strong', {text: r.version}), r.id === currentId ? badge('Live now', 'success') : null)},
      {label: 'State', render: (r) => badge(r.status, statusTone(r.status))},
      {label: 'Uploaded', render: (r) => formatDate(r.created)},
      // One endpoint, two truthful labels: a release that never served is
      // promoted, one that served before is rolled back to.
      {label: '', render: (r) => {
        if (!manage || r.id === currentId || !PROMOTABLE.has(r.status)) return null;
        const promote = r.status === 'uploaded';
        return h('button', {class: 'button compact', onclick: (event) => {
          event.stopPropagation(); return rollbackTo(app, r, reload, {promote});
        }}, promote ? 'Make this live' : 'Roll back to this');
      }},
    ], rows: releases, empty: 'No versions have been deployed yet.'}).render();
    const history = deployments.length ? h('ol', {class: 'timeline-view'}, ...deployments.map((d) => h('li', {},
      h('span', {class: 'timeline-dot'}), h('div', {},
        h('strong', {text: `${d.release?.version || 'release'} — ${d.status.replace(/_/g, ' ')}`}),
        h('time', {text: formatDate(d.created)}))))) : h('p', {class: 'muted', text: 'No deploy activity yet.'});
    body.replaceChildren(
      h('h3', {class: 'section-subhead', text: 'Versions'}),
      h('p', {class: 'muted small', text: 'Roll back to make an earlier version live again across your fleet. The version serving now stays stored either way.'}),
      releaseTable,
      h('h3', {class: 'section-subhead', text: 'Recent deploys'}), history);
    return;
  }
  if (section === 'serving') {
    // Everything about HOW the app is reached lives in serving.js: address,
    // certificate, shape, paths. The addresses table and the change-address
    // wizard stay here and are handed over, so there is one of each.
    await servingPanel(ctx, app, body, current, {
      reload,
      renderAddresses: () => addressesCard(ctx, app, reload),
      onChangeAddress: () => changeAddressFor(ctx, app, reload),
    });
    return;
  }
  if (section === 'key') {
    const payload = await api(`/api/edge/webapp/key_status?webapp=${encodeURIComponent(app.id)}`);
    if (!current()) return;
    const status = payload.status;
    const state = status.linked && status.active ? ['Active', 'success'] : status.last_action === 'revoke' ? ['Turned off', 'warning'] : ['Not set up', 'neutral'];
    // Filtered, not spread raw: replaceChildren() stringifies a null argument
    // into a text node, so v1's key tab printed a literal "null" under the
    // status row for any key that was never created or has been turned off.
    // h() drops nulls on its own; this is the one place the children reach the
    // DOM without passing through it.
    body.replaceChildren(...[
      h('div', {class: 'credential-status'}, h('div', {}, h('span', {text: 'GitHub Actions secret'}), h('strong', {text: 'MOJO_DEPLOY_KEY'})), badge(state[0], state[1])),
      status.linked ? detailGrid([['Created', formatDate(status.created)], ['Last used', formatDate(status.last_used)]]) : null,
      h('p', {class: 'muted small', text: 'This is the only credential your deploy needs. It can register releases for this app and nothing else.'}),
      manage ? h('div', {class: 'row-actions'},
        h('button', {class: 'button', 'data-webapp-key': app.id, onclick: (event) => runAction(event.currentTarget,
          () => keyDialog(app, reload), {pendingLabel: 'Opening…'})}, icon('key'), status.linked ? 'Rotate key' : 'Create key'),
        status.linked ? h('button', {class: 'button ghost', onclick: () => revokeKey(app, reload)}, 'Turn off') : null) : null,
    ].filter(Boolean));
    return;
  }
  // danger ('setup' renders through setupPanel, not here)
  //
  // Danger is destructive actions only. Changing the address is a normal day-2
  // change and lives on the Serving tab, beside the address it changes.
  if (!manage) { body.replaceChildren(h('p', {class: 'muted', text: 'You don’t have permission to change this app.'})); return; }
  const takeOffline = address.hostname ? h('button', {class: 'button', onclick: () => {
    return confirmAction({title: `Take ${app.slug} offline?`, danger: true, confirmLabel: 'Take offline', requireReason: true, reasonLabel: 'Why?',
      copy: 'Visitors will stop reaching your app. The app and its versions are kept — you can put it back on an address later.'}).then((answer) => {
      if (!answer.confirmed) return undefined;
      // Taking an app off its address invalidates every tab on this page, and
      // reload() rebuilds the button that started it: scrim, not inline.
      return runAction(null, async () => {
        await api('/api/edge/webapp/detach_address', {method: 'POST', body: JSON.stringify({webapp: app.id})}); await reload();
      }, {
        key: `webapp-detach-address:${app.id}`,
        busy: {title: `Taking ${app.slug} offline…`, detail: 'The app and its versions are kept.'},
        onError: (error) => actionFailed('The app was not taken offline', error),
      });
    });
  }}, 'Take offline') : null;
  const deleteApp = h('button', {class: 'button danger', onclick: () => deleteWebApp(app, async () => {
    body.dispatchEvent(new CustomEvent('mojo-webapp-deleted', {bubbles: true}));
  })}, icon('trash'), 'Delete app');
  // Filtered for the same reason as the key tab: an app with no address has no
  // "Take offline" row, and a raw null here would print itself.
  body.replaceChildren(...[
    takeOffline ? h('div', {class: 'danger-row'}, h('div', {}, h('strong', {text: 'Take offline'}), h('p', {class: 'muted small', text: 'Stop serving the app without deleting it.'})), takeOffline) : null,
    h('div', {class: 'danger-row danger'}, h('div', {}, h('strong', {text: 'Delete this app'}), h('p', {class: 'muted small', text: 'Remove the app and everything about it. Permanent.'})), deleteApp),
  ].filter(Boolean));
}

// ---------------------------------------------------------------------------
// the page
// ---------------------------------------------------------------------------

/**
 * Where the operator came from, if they were sent here by another screen.
 *
 * Activity links a WebApp record straight at this page and carries its own
 * location in `?return=`. The back pill always returns to Apps — that is the
 * one destination that is true whatever route the link came from — so the way
 * back to the record sits beside it, exactly as Activity itself does it.
 */
function returnAction(returnState) {
  const href = restoreReturnLocation(returnState);
  if (!href) return null;
  const label = decodeRouteState(href).route === 'activity'
    ? 'Return to activity' : 'Return';
  return h('a', {class: 'button ghost', href}, label);
}

export async function webappDetailPage(ctx, webappId, signal = null) {
  const root = h('div', {class: 'page apps-detail'});
  const manage = ctx.capabilities.manage_webapps;
  // Read once: switching tabs rewrites the hash, and the way back must survive
  // that rather than being dropped by the first click.
  const returnState = decodeRouteState().state.return || '';
  let summary = null;
  async function fetchSummary() {
    summary = await api(`/api/edge/webapp/summary?webapp=${encodeURIComponent(webappId)}`, {signal});
  }
  async function paint() {
    const app = summary.webapp;
    const address = summary.address || {};
    const tabs = PAGE_TABS.filter(([id]) => id !== 'danger' || manage);
    let active = decodeRouteState().state.tab;
    if (!tabs.some(([id]) => id === active)) active = 'overview';
    const body = h('div', {class: 'apps-detail-body'});
    const reload = async () => { await fetchSummary(); await paint(); };
    async function section(id) {
      // The setup panel is built synchronously — there is no await to cover,
      // and scheduling a loading state for it would only ever be a flash.
      if (id === 'setup') { body.replaceChildren(setupPanel(ctx, app, summary, reload)); return; }
      // Deploys, Serving and Deploy key each fire reads before they paint.
      // loadInto owns both states, and drops a render a newer tab click has
      // already superseded.
      await loadInto(body, (current) => manageSection(ctx, app, summary, id, body, reload, current),
        {message: 'Loading…', retry: () => section(id)});
    }
    const nav = sectionTabs({items: tabs.map(([id, label]) => ({id, label})), active, label: 'App sections', onChange: async (id) => {
      active = id;
      [...nav.querySelectorAll('button')].forEach((button, index) => button.classList.toggle('active', tabs[index][0] === id));
      history.replaceState({}, '', routeHref('apps', {
        webapp: webappId, tab: id === 'overview' ? '' : id, return: returnState}));
      await section(id);
    }});
    // The newest deployment this summary reports — the same fact the header
    // reads — is also what the global banner needs.
    syncAppDeployment(app, summary.latest_deployment);
    const facts = servingFacts(summary);
    // Filled in only if the deployment read proves which deploy it came from.
    const rolledFrom = h('span', {class: 'apps-serving-from'});
    root.replaceChildren(
      backPill('Apps', 'apps'),
      h('header', {class: 'page-header apps-detail-header'},
        h('div', {},
          h('div', {class: 'eyebrow', text: 'Apps'}),
          h('h1', {text: app.display_name || app.slug, tabindex: '-1'}),
          h('p', {class: `apps-serving tone-${facts.tone}`},
            h('span', {text: facts.line}), rolledFrom,
            facts.badge ? badge(facts.badge.label, facts.badge.tone) : null),
          facts.note ? h('p', {class: 'muted small', text: facts.note}) : null),
        h('div', {class: 'page-actions'},
          returnAction(returnState),
          httpsLink(address.https_origin)
            ? h('a', {class: 'button ghost', href: httpsLink(address.https_origin), target: '_blank', rel: 'noopener'}, 'Open') : null)),
      nav, body);
    nameRolledBackFrom(app, summary, rolledFrom);
    body.addEventListener('mojo-webapp-deleted', () => { location.hash = routeHref('apps'); });
    await section(active);
  }
  async function load() {
    try { await fetchSummary(); await paint(); }
    catch (error) {
      if (error?.name === 'AbortError') return;
      root.replaceChildren(backPill('Apps', 'apps'), errorState(error, load));
    }
  }
  await load();
  return root;
}

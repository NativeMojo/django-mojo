// Deployments: one page for everything running on the fleet — the API
// service and the django-mojo framework on top (platform-gated, built in
// api.js), one row per web app below. Onboarding lives in wizard.js; each app
// has its own full page (webappDetailPage, keyed on ?webapp=<id>) with five
// tabs, of which "Set up deploys" carries the three honest ways to ship a
// build. No framework words in the primary copy.
import {api, badge, formatDate, h, icon, listData, pageHeader, statusTone, TableView} from '../../core.js';
import {confirmAction, openModal} from '../../components/overlays.js';
import {decodeRouteState, routeHref} from '../../components/routes.js';
import {rowSection, statusHeadline, statusRow} from '../../components/rows.js';
import {announce, copyButton, loadInto, runAction} from '../../components/actions.js';
import {emptyState, errorState, sectionTabs} from '../../components/views.js';
import {hasPendingWizard, resumeWizard, startChangeAddress, startWizard} from './wizard.js';
import {
  FRAMEWORK_PATH, PLATFORM_SECTIONS_PATH, apiServiceRow, applyFrameworkUpdate,
  frameworkRow, openApiInspector, openFrameworkInspector,
} from './api.js';

const PAGE_TABS = [
  ['overview', 'Overview'], ['deploys', 'Deploys'],
  ['setup', 'Set up deploys'], ['key', 'Deploy key'], ['danger', 'Danger'],
];
const PROMOTABLE = new Set(['uploaded', 'live', 'superseded']);

function detailGrid(rows) {
  return h('dl', {class: 'detail-grid'}, ...rows.filter(Boolean).flatMap(([label, value]) => [
    h('dt', {text: label}), h('dd', {}, value instanceof Node ? value : String(value ?? '—')),
  ]));
}

// Only ever open the app's own origin as a real https link. The value is
// backend-built, but asserting the scheme keeps a stray javascript:/data: URL
// out of an href or window.open even if the source ever changed.
function httpsLink(origin) {
  return typeof origin === 'string' && origin.startsWith('https://') ? origin : null;
}

// A destructive action fails with no panel of its own to fail into: the view it
// belonged to is gone, or about to be. Say so where the operator is looking
// instead of letting the scrim vanish onto an unchanged screen.
function actionFailed(title, error) {
  const detail = error?.message || 'That did not work.';
  announce(detail);
  openModal({title, content: h('div', {class: 'callout warning'}, icon('alert'), h('p', {text: detail}))});
}

// The deploy key is shown once. When the dialog closes we scrub the node, the
// closure, and the response so the value cannot be read back from memory.
function oneTimeSecret(webapp, result, returnFocus) {
  let secret = result.token;
  const secretField = h('pre', {class: 'code-block is-inline', tabindex: '0', text: secret});
  const content = h('div', {},
    h('div', {class: 'callout warning'}, icon('alert'), h('div', {}, h('strong', {text: 'Copy this value now'}),
      h('p', {text: 'It can’t be shown again after you close this. If it’s lost, rotate the key to get a new one.'}))),
    // A div, not a label: a <label> with no labelable control inside labels
    // nothing. tabindex on the block keeps the value Tab-reachable, which the
    // textarea gave for free and a keyboard user needs to select and copy it.
    h('div', {class: 'field'}, h('span', {text: 'GitHub Actions secret: MOJO_DEPLOY_KEY'}), secretField),
    // A function, not a string: onClose scrubs `secret`, and the button must
    // never be able to hand back a value the dialog has already forgotten.
    copyButton(() => secret, {label: 'Copy secret', className: 'button primary'}),
    h('div', {class: 'command'}, h('code', {text: 'gh secret set MOJO_DEPLOY_KEY --repo YOUR_ORG/YOUR_REPO'})));
  openModal({title: `${webapp.slug} deployment key`, subtitle: 'The previous key is already inactive.', content, returnFocus, onClose: () => {
    secretField.textContent = ''; secret = ''; result.token = null;
  }});
}

function keyDialog(webapp, reload) {
  return (async () => {
    const payload = await api(`/api/edge/webapp/key_status?webapp=${encodeURIComponent(webapp.id)}`);
    const status = payload.status;
    const action = status.linked ? 'rotate' : 'mint';
    const actionLabel = status.linked ? 'Rotate key' : status.last_action === 'revoke' ? 'Create new key' : 'Create key';
    const message = h('div', {class: 'form-message', role: 'alert'});
    const submit = h('button', {class: `button ${status.linked ? 'danger' : 'primary'}`}, icon('key'), actionLabel);
    const content = h('div', {},
      h('p', {class: 'modal-copy', text: status.linked
        ? 'Rotating turns the current key off right away. Update the secret in GitHub before your next deploy, or it will fail.'
        : 'This creates the one secret your deploy needs. It’s shown once — copy it into GitHub.'}),
      status.linked ? detailGrid([['Created', formatDate(status.created)], ['Last used', formatDate(status.last_used)]]) : null,
      message, h('div', {class: 'form-actions'},
        status.linked ? h('button', {class: 'button ghost', onclick: () => revokeKey(webapp, reload)}, 'Turn key off') : null, submit));
    const close = openModal({title: `${webapp.slug} deploy key`, subtitle: 'Shown once, and auditable.', content, danger: status.linked});
    // Success closes this modal, so nothing is restored onto a detached button;
    // a refusal keeps the dialog open with the server's sentence in `message`.
    submit.addEventListener('click', () => runAction(submit, async () => {
      const result = await api('/api/edge/webapp/link_key', {method: 'POST', body: JSON.stringify({webapp: webapp.id, action, operation_id: crypto.randomUUID()})});
      close(); await reload();
      if (result.token) oneTimeSecret(webapp, result);
      else openModal({title: `${webapp.slug} secret unavailable`, subtitle: 'The key was created but its value was already consumed.',
        content: h('div', {class: 'callout warning'}, icon('alert'), h('p', {text: 'The one-time value couldn’t be recovered. Rotate the key to receive a fresh secret.'}))});
    }, {
      pendingLabel: `${actionLabel}…`, restoreOnSuccess: false,
      onError: (error) => { message.textContent = error.message; },
    }));
  })();
}

// Confirm first — the dialog is human input, and nothing paints while a person
// is reading it. What follows takes the key out from under every running
// deploy, so it gets the scrim rather than an inline state on a control the
// reload is about to rebuild.
function revokeKey(webapp, reload) {
  return confirmAction({title: `Turn off ${webapp.slug}’s deploy key?`, danger: true, confirmLabel: 'Turn key off',
    copy: 'Deploys will fail until you create a new key. Existing releases stay live.'}).then((answer) => {
    if (!answer.confirmed) return undefined;
    return runAction(null, async () => {
      await api('/api/edge/webapp/revoke_key', {method: 'POST', body: JSON.stringify({webapp: webapp.id, operation_id: crypto.randomUUID()})});
      await reload();
    }, {
      key: `webapp-revoke-key:${webapp.id}`,
      busy: {title: 'Turning the deploy key off…', detail: 'Deploys will fail until a new key is created.'},
      onError: (error) => actionFailed('The key was not turned off', error),
    });
  });
}

function workflowPanel(webapp, keyAction = null) {
  const panel = h('div', {class: 'setup-block'});
  // The workflow file is generated server-side, so this tab always awaits
  // before it can paint: a loading state before, and an in-panel failure with
  // a retry after — never a blank block that might or might not be finished.
  loadInto(panel, async (current) => {
    const result = await api('/api/edge/webapp/onboarding/workflow', {method: 'POST', body: JSON.stringify({webapp: webapp.id})});
    if (!current()) return;
    // A <pre>, not a <textarea>: this is text to read and copy, never to edit.
    // A textarea gave it the browser's default form chrome — square corners, a
    // resize grabber — in a page that is otherwise styled.
    const text = h('pre', {class: 'code-block', tabindex: '0', text: result.yaml});
    panel.replaceChildren(
      h('p', {text: 'Two things set up deploys from GitHub: one secret, and one file.'}),
      h('ol', {class: 'setup-list'},
        h('li', {}, h('strong', {text: 'Add the secret. '}), 'In your repo’s Settings → Secrets, add ', h('code', {text: 'MOJO_DEPLOY_KEY'}), '. ', keyAction),
        h('li', {}, h('strong', {text: 'Add the file. '}), 'Save this as ', h('code', {text: result.filename}), ' and push:')),
      text,
      copyButton(() => text.textContent, {label: 'Copy file', className: 'button primary'}));
  }, {message: 'Building your workflow file…'});
  return panel;
}

// Both the row-level "Set address" and the Danger tab's "Change address" run
// the same wizard flow, seeded from the full record.
async function changeAddressFor(ctx, app, reload) {
  const full = await api(`/api/edge/webapp/${encodeURIComponent(app.id)}`);
  startChangeAddress(ctx, reload, {
    group_id: full.group?.id, slug: full.slug, display_name: full.display_name,
    environment: full.environment, bucket: full.bucket, github_repository: full.github_repository,
    deployment_ref: full.deployment_ref, build_output: full.build_output,
  });
}

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
  return new TableView({columns: [
    {label: 'Type', render: (record) => cell(text(record.type))},
    {label: 'Name', render: (record) => cell(text(record.name))},
    {label: 'Value', render: (record) => cell(text(record.value))},
  ], rows: records || [], empty: 'Nothing to add — check again.'}).render();
}

function certBadge(certificate) {
  const state = certState(certificate);
  return badge(state.label, state.tone === 'ok' ? 'success' : state.tone === 'warn' ? 'warning' : 'danger');
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
      manage ? h('button', {class: 'button compact', type: 'button', onclick: () => addAddressDialog(app, reload)},
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
function addAddressDialog(app, reload) {
  const input = h('input', {placeholder: 'www.example.com', autocomplete: 'off', spellcheck: 'false'});
  const message = h('div', {class: 'form-message', role: 'alert'});
  const area = h('div', {class: 'verdict-area'});
  const submit = h('button', {class: 'button primary', type: 'button'}, icon('globe'), 'Add address');
  let done = false;
  const content = h('div', {},
    h('p', {class: 'modal-copy', text: 'Point an address you already own at this app. It serves exactly what your app’s own address serves.'}),
    h('label', {class: 'field'}, h('span', {text: 'Address'}), input),
    message, area, h('div', {class: 'form-actions'}, submit));
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

  // The pending state goes on `submit`, never on the Check / Try again buttons
  // paint() renders: those live inside `area`, which every branch replaces.
  // submit sits outside it and outlives each result.
  function run(retryCertificate) {
    const hostname = input.value.trim();
    if (!hostname) { message.textContent = 'Type the address you want to point at this app.'; return Promise.resolve(); }
    message.textContent = '';
    area.replaceChildren();
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
}

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
      // here so reaching the inspector (the only path to Delete) doesn't hide it.
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
    const releaseTable = new TableView({columns: [
      {label: 'Version', render: (r) => h('div', {}, h('strong', {text: r.version}), r.id === currentId ? badge('Live now', 'success') : null)},
      {label: 'State', render: (r) => badge(r.status, statusTone(r.status))},
      {label: 'Uploaded', render: (r) => formatDate(r.created)},
      {label: '', render: (r) => manage && r.id !== currentId && PROMOTABLE.has(r.status)
        ? h('button', {class: 'button compact', onclick: (event) => { event.stopPropagation(); return rollbackTo(app, r, reload); }}, 'Roll back to this') : null},
    ], rows: releases, empty: 'No versions have been deployed yet.'}).render();
    const history = deployments.length ? h('ol', {class: 'timeline-view'}, ...deployments.map((d) => h('li', {},
      h('span', {class: 'timeline-dot'}), h('div', {},
        h('strong', {text: `${d.release?.version || 'release'} — ${d.status.replace(/_/g, ' ')}`}),
        h('time', {text: formatDate(d.created)}))))) : h('p', {class: 'muted', text: 'No deploy activity yet.'});
    body.replaceChildren(
      h('h3', {class: 'section-subhead', text: 'Versions'}),
      h('p', {class: 'muted small', text: 'Roll back to make an earlier version live again across your fleet.'}),
      releaseTable,
      h('h3', {class: 'section-subhead', text: 'Recent deploys'}), history);
    return;
  }
  if (section === 'key') {
    const payload = await api(`/api/edge/webapp/key_status?webapp=${encodeURIComponent(app.id)}`);
    if (!current()) return;
    const status = payload.status;
    const state = status.linked && status.active ? ['Active', 'success'] : status.last_action === 'revoke' ? ['Turned off', 'warning'] : ['Not set up', 'neutral'];
    body.replaceChildren(
      h('div', {class: 'credential-status'}, h('div', {}, h('span', {text: 'GitHub Actions secret'}), h('strong', {text: 'MOJO_DEPLOY_KEY'})), badge(state[0], state[1])),
      status.linked ? detailGrid([['Created', formatDate(status.created)], ['Last used', formatDate(status.last_used)]]) : null,
      h('p', {class: 'muted small', text: 'This is the only credential your deploy needs. It can register releases for this app and nothing else.'}),
      manage ? h('div', {class: 'row-actions'},
        h('button', {class: 'button', 'data-webapp-key': app.id, onclick: (event) => runAction(event.currentTarget,
          () => keyDialog(app, reload), {pendingLabel: 'Opening…'})}, icon('key'), status.linked ? 'Rotate key' : 'Create key'),
        status.linked ? h('button', {class: 'button ghost', onclick: () => revokeKey(app, reload)}, 'Turn off') : null) : null);
    return;
  }
  // danger ('setup' renders through setupPanel, not here)
  if (!manage) { body.replaceChildren(h('p', {class: 'muted', text: 'You don’t have permission to change this app.'})); return; }
  const changeAddress = h('button', {class: 'button', onclick: (event) => runAction(event.currentTarget,
    () => changeAddressFor(ctx, app, reload), {pendingLabel: 'Opening…'})}, icon('globe'), 'Change address');
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
  body.replaceChildren(
    h('div', {class: 'danger-row'}, h('div', {}, h('strong', {text: 'Change address'}), h('p', {class: 'muted small', text: 'Move this app to a different address. The current one keeps serving until the new one is ready.'})), changeAddress),
    takeOffline ? h('div', {class: 'danger-row'}, h('div', {}, h('strong', {text: 'Take offline'}), h('p', {class: 'muted small', text: 'Stop serving the app without deleting it.'})), takeOffline) : null,
    h('div', {class: 'danger-row danger'}, h('div', {}, h('strong', {text: 'Delete this app'}), h('p', {class: 'muted small', text: 'Remove the app and everything about it. Permanent.'})), deleteApp));
}

// The one delete flow, shared by the app page's Danger tab and the list row's
// inline Delete for an app whose setup never finished.
function deleteWebApp(app, onDeleted) {
  return confirmAction({title: `Delete ${app.slug}?`, danger: true, confirmLabel: 'Delete app', requireReason: true, reasonLabel: 'Why?',
    copy: 'This removes the app, its address, its deploy key, and its deploy history for good. This cannot be undone.'}).then((answer) => {
    if (!answer.confirmed) return undefined;
    // Permanent, and it navigates away from the page the trigger lives on.
    return runAction(null, async () => {
      await api(`/api/edge/webapp/${encodeURIComponent(app.id)}`, {method: 'DELETE'});
      await onDeleted();
    }, {
      key: `webapp-delete:${app.id}`,
      busy: {title: `Deleting ${app.slug}…`, detail: 'Removing the app, its address, and its deploy history.'},
      onError: (error) => actionFailed('The app was not deleted', error),
    });
  });
}

function rollbackTo(app, release, reload) {
  return confirmAction({title: `Roll back to ${release.version}?`, danger: true, confirmLabel: 'Roll back', requireReason: true, reasonLabel: 'Why are you rolling back?',
    copy: `This makes ${release.version} live again across your fleet. Visitors will see that version within a few minutes.`}).then((answer) => {
    if (!answer.confirmed) return undefined;
    // A fleet-wide rollback must not be interrupted, and the row holding the
    // button is rebuilt by reload().
    return runAction(null, async () => {
      await api('/api/edge/webapp/rollback', {method: 'POST', body: JSON.stringify({webapp: app.id, release: release.id})});
      await reload();
    }, {
      // Two releases are two actions: keying on the app alone made a rollback
      // to 1.2 return an in-flight rollback to 1.1 and never run.
      key: `webapps:rollback:${app.id}:${release.id}`,
      busy: {title: `Rolling back to ${release.version}…`, detail: 'Visitors will see that version within a few minutes.'},
      onError: (error) => actionFailed('The rollback did not start', error),
    });
  });
}

// ---------------------------------------------------------------------------
// Upload a build: pick or drop the built folder, hash every file right here
// (crypto.subtle sha256), PUT each one straight to storage with the exact
// x-amz-checksum-sha256 header its signed URL binds, mark the release
// complete, and watch the deploy land — the same three calls CI makes.
// ---------------------------------------------------------------------------

// The server refuses more than this per release (its EDGE_RELEASE_MAX_FILES /
// EDGE_RELEASE_MAX_BYTES defaults); refusing here first saves the hashing.
const UPLOAD_LIMITS = {files: 5000, bytes: 1073741824};
const STORAGE_BLOCKED_COPY = 'The browser was blocked from uploading directly to storage — run the storage checkup in System Setup (bucket sharing rules), then try again.';
const DEPLOY_POLL_MS = 1800;

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return '0 B';
  if (bytes >= 1073741824) return `${(bytes / 1073741824).toFixed(1)} GB`;
  if (bytes >= 1048576) return `${(bytes / 1048576).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

// upload-YYYYMMDD-HHMMSS: unique enough per hand deploy, and within the
// server's version charset (letters, digits, ., _, -).
function defaultUploadVersion() {
  return `upload-${new Date().toISOString().slice(0, 19).replace(/[-:]/g, '').replace('T', '-')}`;
}

async function sha256Hex(file) {
  const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

// Manifest paths are relative to the picked root: backslashes normalized to
// forward slashes, leading ./ and / stripped.
function normalizeRelPath(raw) {
  let path = String(raw || '').replace(/\\/g, '/');
  while (path.startsWith('./')) path = path.slice(2);
  return path.replace(/^\/+/, '');
}

// A folder pick carries webkitRelativePath ("dist/assets/app.js"); the picked
// folder itself is the root, so its own name is not part of any path.
function pickedFiles(input) {
  return [...(input.files || [])].map((file) => {
    let path = normalizeRelPath(file.webkitRelativePath || file.name);
    if (file.webkitRelativePath && path.includes('/')) path = path.slice(path.indexOf('/') + 1);
    return {file, path};
  });
}

async function droppedFiles(dataTransfer) {
  const entries = [...(dataTransfer.items || [])]
    .map((item) => (item.webkitGetAsEntry ? item.webkitGetAsEntry() : null))
    .filter(Boolean);
  if (!entries.length) {
    return [...(dataTransfer.files || [])].map((file) => ({file, path: normalizeRelPath(file.name)}));
  }
  const out = [];
  async function walk(entry, base) {
    if (entry.isFile) {
      const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
      out.push({file, path: normalizeRelPath(base ? `${base}/${entry.name}` : entry.name)});
      return;
    }
    if (!entry.isDirectory) return;
    const reader = entry.createReader();
    let batch;
    do {
      batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
      for (const child of batch) await walk(child, base ? `${base}/${entry.name}` : entry.name);
    } while (batch.length);
  }
  for (const entry of entries) await walk(entry, '');
  // One dropped folder is the picked root — same rule as the folder picker.
  if (entries.length === 1 && entries[0].isDirectory) {
    const prefix = `${normalizeRelPath(entries[0].name)}/`;
    return out.map((row) => ({
      file: row.file,
      path: row.path.startsWith(prefix) ? row.path.slice(prefix.length) : row.path,
    }));
  }
  return out;
}

function uploadPanel(ctx, app, summary, reload) {
  const manage = ctx.capabilities.manage_webapps;
  const panel = h('div', {class: 'setup-block upload-build'});
  if (!manage) {
    panel.append(h('p', {class: 'muted', text: 'You don’t have permission to deploy this app.'}));
    return panel;
  }
  const state = {files: [], busy: false};
  const message = h('div', {class: 'form-message', role: 'alert'});
  const progress = h('p', {class: 'muted small', role: 'status'});
  const chosen = h('p', {class: 'muted small', text: 'Nothing picked yet.'});
  const version = h('input', {value: defaultUploadVersion(), autocomplete: 'off', spellcheck: 'false'});
  const deploy = h('button', {class: 'button primary', disabled: true}, icon('deploy'), 'Upload and deploy');

  const totalBytes = () => state.files.reduce((sum, row) => sum + row.file.size, 0);
  const overCaps = () => state.files.length > UPLOAD_LIMITS.files || totalBytes() > UPLOAD_LIMITS.bytes;
  const capsMessage = () =>
    `Too big to deploy: the server accepts at most ${UPLOAD_LIMITS.files.toLocaleString()} files and ` +
    `${formatBytes(UPLOAD_LIMITS.bytes)} per release. This pick is ` +
    `${state.files.length.toLocaleString()} files, ${formatBytes(totalBytes())}.`;
  const setProgress = (text) => { progress.textContent = text; };

  function fail(error) {
    state.busy = false;
    deploy.disabled = !state.files.length || overCaps();
    setProgress('');
    if (error?.storageBlocked || error?.name === 'TypeError') {
      message.replaceChildren(
        document.createTextNode(`${STORAGE_BLOCKED_COPY} `),
        h('a', {href: routeHref('setup'), text: 'Open System Setup'}));
      return;
    }
    message.textContent = error?.message || 'Something went wrong. Try again.';
  }

  function takeFiles(rows) {
    if (state.busy) return;
    const seen = new Map();
    (rows || []).forEach((row) => { if (row.file && row.path) seen.set(row.path, row); });
    if (!seen.size) {
      message.textContent = 'Nothing usable was picked. Choose the folder your build produced.';
      return;
    }
    state.files = [...seen.values()];
    chosen.textContent = `${state.files.length.toLocaleString()} files, ${formatBytes(totalBytes())}.`;
    message.textContent = '';
    setProgress('');
    if (overCaps()) { message.textContent = capsMessage(); deploy.disabled = true; return; }
    deploy.disabled = false;
  }

  // Polls /api/edge/release/deployment/<id> the way the app pages poll a run:
  // a setTimeout loop that stops when the panel leaves the document.
  function watchDeployment(deploymentId, versionName) {
    return new Promise((resolve) => {
      const step = async () => {
        if (!panel.isConnected) { resolve(); return; }
        let status = null;
        try {
          status = await api(`/api/edge/release/deployment/${encodeURIComponent(deploymentId)}`);
        } catch (_) { /* keep the last progress line; the next tick retries */ }
        if (status?.terminal) {
          state.busy = false;
          deploy.disabled = false;
          if (status.success) {
            setProgress('');
            progress.replaceChildren(
              document.createTextNode(`Deployed — ${status.version || versionName} is live. `),
              h('a', {href: routeHref('deployments', {webapp: app.id, tab: 'deploys'}), text: 'See your deploys'}));
          } else {
            fail(new Error(status.detail || `The deploy finished as ${String(status.status).replace(/_/g, ' ')}. Try again, or roll back from the Deploys tab.`));
          }
          resolve();
          return;
        }
        if (status) setProgress(`Deploying to your fleet — ${String(status.status).replace(/_/g, ' ')}…`);
        setTimeout(step, DEPLOY_POLL_MS);
      };
      step();
    });
  }

  async function run() {
    if (state.busy || !state.files.length) return;
    message.textContent = '';
    const name = version.value.trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(name)) {
      message.textContent = 'Version names use letters, digits, dots, dashes and underscores.';
      return;
    }
    if (overCaps()) { message.textContent = capsMessage(); return; }
    state.busy = true;
    deploy.disabled = true;
    try {
      const manifest = [];
      for (let i = 0; i < state.files.length; i += 1) {
        setProgress(`Reading file ${i + 1} of ${state.files.length}…`);
        const row = state.files[i];
        manifest.push({path: row.path, sha256: await sha256Hex(row.file), size: row.file.size});
      }
      setProgress('Registering the release…');
      const registered = await api('/api/edge/release', {
        method: 'POST', body: JSON.stringify({webapp: app.id, version: name, manifest})});
      // The register response carries one signed URL per file plus the exact
      // headers that URL was bound to (x-amz-checksum-sha256). An already
      // verified version returns no uploads and goes straight to complete.
      const uploads = registered.uploads || [];
      const byPath = new Map(state.files.map((row) => [row.path, row.file]));
      for (let i = 0; i < uploads.length; i += 1) {
        const upload = uploads[i];
        setProgress(`Uploading file ${i + 1} of ${uploads.length}…`);
        let response;
        try {
          response = await fetch(upload.url, {
            method: 'PUT', headers: upload.headers || {}, body: byPath.get(upload.path)});
        } catch (error) {
          const blocked = new Error(STORAGE_BLOCKED_COPY);
          blocked.storageBlocked = true;
          throw blocked;
        }
        if (!response.ok) {
          throw new Error(`Storage refused ${upload.path} (${response.status}). Try again.`);
        }
      }
      setProgress('Checking every file landed…');
      const completed = await api('/api/edge/release/complete', {
        method: 'POST', body: JSON.stringify({release: registered.release})});
      setProgress('Deploying to your fleet…');
      await watchDeployment(completed.deployment, name);
    } catch (error) {
      fail(error);
    }
  }

  const folderInput = h('input', {type: 'file', webkitdirectory: true, multiple: true, hidden: true,
    onchange: () => takeFiles(pickedFiles(folderInput))});
  const filesInput = h('input', {type: 'file', multiple: true, hidden: true,
    onchange: () => takeFiles(pickedFiles(filesInput))});
  const drop = h('div', {class: 'upload-drop', role: 'button', tabindex: '0',
    'aria-label': 'Drop your built site folder here, or press Enter to pick it',
    ondragover: (event) => { event.preventDefault(); drop.classList.add('active'); },
    ondragleave: () => drop.classList.remove('active'),
    ondrop: async (event) => {
      event.preventDefault();
      drop.classList.remove('active');
      if (!state.busy) takeFiles(await droppedFiles(event.dataTransfer));
    },
    onclick: () => { if (!state.busy) folderInput.click(); },
    onkeydown: (event) => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); if (!state.busy) folderInput.click(); }
    }},
  icon('deploy'),
  h('strong', {text: 'Drop your built site folder here'}),
  h('p', {class: 'muted small', text: 'or click to pick the folder'}));
  // The upload flow keeps its own progress machine — it has real per-file
  // progress to report, which a generic pending state would only hide. What it
  // gains here is the shared re-entry guard: `state.busy` is set after two
  // early returns, and a double click before then registered two deploys.
  deploy.addEventListener('click', () => runAction(deploy, run, {announceLabel: 'Uploading and deploying…'}));

  panel.append(
    h('p', {text: 'Deploy by hand: pick the folder your build produced, and it goes live the same way a CI deploy does.'}),
    drop, folderInput, filesInput,
    h('p', {class: 'muted small'}, 'Prefer files over a folder? ',
      h('a', {href: '#', onclick: (event) => { event.preventDefault(); if (!state.busy) filesInput.click(); }, text: 'Pick individual files'}), '.'),
    chosen,
    h('label', {class: 'field'}, h('span', {text: 'Version name'}), version),
    h('p', {class: 'muted small', text:
      `The server accepts at most ${UPLOAD_LIMITS.files.toLocaleString()} files and ${formatBytes(UPLOAD_LIMITS.bytes)} per release.`}),
    h('div', {class: 'row-actions'}, deploy),
    progress, message);
  return panel;
}

// "Set up deploys": the ways a first (or next) build actually reaches this
// app. GitHub Actions is the default because it is the one we can generate
// outright; the upload panel deploys a build straight from the browser, and
// the API panel names the three calls any CI can make.
const SETUP_WAYS = [
  ['github', 'GitHub Actions'], ['upload', 'Upload a build'],
  ['api', 'Any other CI / API'],
];

function setupPanel(ctx, app, summary, reload) {
  const manage = ctx.capabilities.manage_webapps;
  const body = h('div', {class: 'setup-way'});
  let active = 'github';
  const keyButton = () => (manage
    ? h('button', {class: 'button compact', type: 'button', onclick: (event) => runAction(event.currentTarget,
      () => keyDialog(app, reload), {pendingLabel: 'Opening…'})}, icon('key'),
    summary.deployment_key?.linked ? 'Rotate key — shown once' : 'Create key — shown once')
    : null);
  function paint() {
    if (active === 'github') { body.replaceChildren(workflowPanel(app, keyButton())); return; }
    if (active === 'upload') {
      body.replaceChildren(uploadPanel(ctx, app, summary, reload));
      return;
    }
    body.replaceChildren(h('div', {class: 'setup-block'},
      h('p', {text: 'Deploy from any CI — or by hand — with one credential and three calls.'}),
      h('ol', {class: 'setup-list'},
        h('li', {}, h('strong', {text: 'Create a deploy key. '}), 'It’s shown once; keep it wherever your CI keeps secrets. ', keyButton()),
        h('li', {}, h('strong', {text: 'Register a release. '}), 'Send the manifest of files your build produced.'),
        h('li', {}, h('strong', {text: 'Upload and complete. '}), 'PUT each file, mark the release complete, and it deploys on its own.')),
      h('p', {class: 'muted small'}, 'Request-by-request details are in ',
        h('a', {href: '#', onclick: (e) => e.preventDefault(), title: 'docs/web_developer/edge/releases.md'}, 'the release guide'),
        ' — releases register against this app’s id (', h('code', {text: `#${app.id}`}), ').')));
  }
  const tabs = sectionTabs({items: SETUP_WAYS.map(([id, label]) => ({id, label})), active, label: 'Ways to deploy', onChange: (id) => {
    active = id;
    [...tabs.querySelectorAll('button')].forEach((button, index) => button.classList.toggle('active', SETUP_WAYS[index][0] === id));
    paint();
  }});
  paint();
  return h('div', {class: 'setup-ways'}, tabs, body);
}

// ---------------------------------------------------------------------------
// the app's own page
// ---------------------------------------------------------------------------

// Full-page view keyed on ?webapp=<id> (tab in ?tab=), so "go set up deploys"
// is a link anyone can follow or share — no drawer to re-open.
async function webappDetailPage(ctx, webappId, signal = null) {
  const root = h('div', {class: 'page'});
  const manage = ctx.capabilities.manage_webapps;
  let summary = null;
  async function fetchSummary() {
    summary = await api(`/api/edge/webapp/summary?webapp=${encodeURIComponent(webappId)}`, {signal});
  }
  function backLink() {
    return h('p', {class: 'back-link'}, h('a', {href: routeHref('deployments')}, '← All deployments'));
  }
  async function paint() {
    const app = summary.webapp;
    const address = summary.address || {};
    const tabs = PAGE_TABS.filter(([id]) => id !== 'danger' || manage);
    let active = decodeRouteState().state.tab;
    if (!tabs.some(([id]) => id === active)) active = 'overview';
    const body = h('div', {class: 'inspector-section'});
    const reload = async () => { await fetchSummary(); await paint(); };
    async function section(id) {
      // The setup panel is built synchronously — there is no await to cover,
      // and scheduling a loading state for it would only ever be a flash.
      if (id === 'setup') { body.replaceChildren(setupPanel(ctx, app, summary, reload)); return; }
      // #2232: Deploys and Deploy key each fire reads before they paint. Until
      // they land the panel showed the previous tab's content, and a rejection
      // showed nothing at all. loadInto owns both states, and drops a render a
      // newer tab click has already superseded.
      await loadInto(body, (current) => manageSection(ctx, app, summary, id, body, reload, current),
        {message: 'Loading…', retry: () => section(id)});
    }
    const nav = sectionTabs({items: tabs.map(([id, label]) => ({id, label})), active, onChange: async (id) => {
      active = id;
      [...nav.querySelectorAll('button')].forEach((button, index) => button.classList.toggle('active', tabs[index][0] === id));
      history.replaceState({}, '', routeHref('deployments', {webapp: webappId, tab: id === 'overview' ? '' : id}));
      await section(id);
    }});
    const standing = summary.current_release ? 'serving your latest deploy'
      : address.hostname ? 'serving a welcome page — nothing deployed yet'
        : 'not reachable — setup never finished';
    root.replaceChildren(
      backLink(),
      pageHeader('Control plane', app.display_name || app.slug,
        address.hostname ? `${address.hostname} — ${standing}` : `This app is ${standing}.`, [
          httpsLink(address.https_origin) ? h('a', {class: 'button ghost', href: httpsLink(address.https_origin), target: '_blank', rel: 'noopener'}, 'Open') : null,
        ].filter(Boolean)),
      nav, body);
    body.addEventListener('mojo-webapp-deleted', () => { location.hash = routeHref('deployments'); });
    await section(active);
  }
  async function load() {
    try { await fetchSummary(); await paint(); }
    catch (error) {
      if (error?.name === 'AbortError') return;
      root.replaceChildren(backLink(), errorState(error, load));
    }
  }
  await load();
  return root;
}

function resumeBanner(ctx, reloadApps) {
  return h('section', {class: 'panel resume-banner'}, h('div', {class: 'panel-heading'},
    h('div', {}, h('h2', {text: 'You have a setup in progress'}), h('p', {text: 'Pick up where you left off, or start over.'})),
    h('button', {class: 'button primary', onclick: () => resumeWizard(ctx, reloadApps)}, 'Resume setup')));
}

// ---------------------------------------------------------------------------
// merged list page
// ---------------------------------------------------------------------------

// The address is the health story. SSL states map to plain words; the row is
// green only when the app has an address, a live release, and a valid cert.
function certState(certificate) {
  if (!certificate) return {label: 'SSL not issued yet', tone: 'warn'};
  const expired = certificate.not_after && new Date(certificate.not_after) < new Date();
  if (certificate.status === 'active' && !expired) return {label: 'SSL valid', tone: 'ok'};
  if (certificate.status === 'active') return {label: 'SSL expired', tone: 'danger'};
  if (['pending', 'issuing'].includes(certificate.status)) return {label: 'SSL issuing', tone: 'warn'};
  return {label: `SSL ${certificate.status}`, tone: 'danger'};
}

function webappRow(ctx, item, {reload}) {
  const app = item.webapp || {};
  const name = app.display_name || app.slug || `#${app.id}`;
  const address = item.address;
  const release = item.current_release;
  const deployment = item.latest_deployment;
  const manage = ctx.capabilities.manage_webapps;
  // Every row opens the app's own page — its Danger tab still carries "Set
  // address" and "Delete app", and Overview offers "Set address" directly.
  const openHref = routeHref('deployments', {webapp: app.id});
  if (!address) {
    // Setup was abandoned before the app got an address. Say so plainly and
    // keep both ways out inline: finish it, or delete it.
    return statusRow({tone: 'warn', name,
      value: 'Setup never finished — not reachable',
      detailNode: manage ? h('span', {class: 'row-inline-actions'},
        h('button', {class: 'button ghost compact', type: 'button', onclick: (event) => runAction(event.currentTarget,
          () => changeAddressFor(ctx, app, reload), {pendingLabel: 'Opening…'})}, 'Finish setup'),
        h('button', {class: 'button ghost compact danger-text', type: 'button', onclick: () => deleteWebApp(app, reload)}, 'Delete')) : null,
      action: {label: 'Open', href: openHref}});
  }
  if (!release) {
    // The address serves the built-in welcome page until a first deploy lands.
    return statusRow({tone: 'warn', name,
      value: `${address.hostname} · live with a welcome page — nothing deployed yet`,
      detailNode: h('a', {class: 'row-link', href: routeHref('deployments', {webapp: app.id, tab: 'setup'})}, 'Deploy something'),
      action: {label: 'Open', href: openHref}});
  }
  const ssl = certState(address.certificate);
  const deployedAt = formatDate(deployment?.finished || release.created);
  let tone = ssl.tone;
  let value = `${address.hostname} · ${ssl.label} · deployed ${deployedAt}`;
  if (deployment && deployment.status === 'failed') {
    tone = 'danger';
    value = `${address.hostname} · ${ssl.label} · last deploy failed ${formatDate(deployment.created)}`;
  }
  const version = release.version || String(release.id);
  return statusRow({tone, name, value,
    detailNode: h('span', {class: 'row-detail mono', text: String(version).slice(0, 10)}),
    action: {label: 'Open', href: openHref}});
}

export async function deploymentsPage(ctx, route = 'deployments', navigate = null, signal = null) {
  // The lane owns both routes; #/webapps canonicalizes to #/deployments with
  // its query state intact. replaceState fires no hashchange, so this render
  // is the only one.
  if (route === 'webapps') {
    history.replaceState({}, '', routeHref('deployments', decodeRouteState().state));
  }
  const wantPlatform = ctx.features?.platform?.capabilities?.view === true;
  const wantApps = ctx.features?.webapps?.enabled === true;
  // ?webapp=<id> is a page of its own, not a drawer over the list. Legacy
  // ?inspector=<id> deep links for apps (WebApp pks are ints; platform
  // deployment pks are UUIDs) redirect to it.
  const linkedState = decodeRouteState().state;
  const linkedWebapp = linkedState.webapp
    || (/^\d+$/.test(String(linkedState.inspector || '')) ? linkedState.inspector : null);
  if (wantApps && linkedWebapp) {
    if (!linkedState.webapp) {
      history.replaceState({}, '', routeHref('deployments', {webapp: linkedWebapp, tab: linkedState.tab}));
    }
    return webappDetailPage(ctx, linkedWebapp, signal);
  }
  const root = h('div', {class: 'page'});
  const state = {
    report: null, reportError: null,
    framework: null,
    apps: null, appsError: null,
    observedAt: null,
  };
  let linkedInspectorOpened = false;
  // Bounded auto-refresh while a deploy is in flight (same pattern as the
  // certificates page): 10s ticks, capped, hash-guarded, abort-cleared.
  let pollTicks = 0;
  let pollTimer = null;
  const clearPoll = () => { if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; } };
  signal?.addEventListener('abort', clearPoll);

  async function load(refresh = false) {
    clearPoll();
    const reads = [];
    if (wantPlatform) {
      reads.push(api(PLATFORM_SECTIONS_PATH, {signal})
        .then((value) => { state.report = value; state.reportError = null; })
        .catch((error) => { if (error?.name !== 'AbortError') { state.report = null; state.reportError = error; } }));
      reads.push(api(`${FRAMEWORK_PATH}${refresh ? '?refresh=1' : ''}`, {signal})
        .then((value) => { state.framework = value; })
        .catch(() => { state.framework = null; }));
    }
    if (wantApps) {
      reads.push(api('/api/edge/webapp/summaries', {signal})
        .then((value) => { state.apps = value; state.appsError = null; })
        .catch((error) => { if (error?.name !== 'AbortError') { state.apps = null; state.appsError = error; } }));
    }
    await Promise.all(reads);
    // The framework GET carries no `resolved`; the deployments section's pin
    // block does. Merge it so the held row can say what it resolves to.
    if (state.framework && state.report) {
      state.framework.resolved_pin =
        state.report.sections?.deployments?.data?.framework_pin?.resolved || null;
    }
    state.observedAt = new Date().toISOString();
    paint();
  }

  // Every callback that finishes work re-renders THROUGH a refetch — the old
  // page's render() did both, and the wizard/inspector callbacks rely on it.
  const render = () => load();

  function deploymentsSection() {
    return state.report?.sections?.deployments || null;
  }

  function apiSection() {
    return state.report?.sections?.api || null;
  }

  // An in-flight deploy is exactly when the operator is staring at this page;
  // it must catch the outcome without a hand refresh. A live coordination
  // lease or a not-yet-settled newest attempt keeps the poll alive.
  const POLL_STATUSES = new Set(['requested', 'canary', 'fleet', 'verified', 'partial']);

  function schedulePoll() {
    const data = deploymentsSection()?.data || null;
    const active = Boolean(data?.coordination?.state)
      || POLL_STATUSES.has((data?.items || [])[0]?.status);
    if (!active || pollTicks >= 36 || !location.hash.startsWith('#/deployments')) return;
    pollTicks += 1;
    pollTimer = setTimeout(() => load().catch(() => {}), 10000);
  }

  function apiRows() {
    if (!wantPlatform) return [];
    if (state.reportError) return [];
    const rows = [
      apiServiceRow(ctx, deploymentsSection(), {
        onOpen: () => openApiInspector(ctx, deploymentsSection(), render, apiSection()),
      }),
      frameworkRow(ctx, state.framework, {
        onOpen: () => openFrameworkInspector(ctx, state.framework, render),
        onUpdate: () => applyFrameworkUpdate(ctx, state.framework, render),
      }),
    ];
    return rows.filter(Boolean);
  }

  function appRows() {
    const items = state.apps?.items || [];
    return items.map((item) => webappRow(ctx, item, {reload: render}));
  }

  function headline(apiRowNodes, appRowNodes) {
    const tones = [...apiRowNodes, ...appRowNodes]
      .map((row) => row.dataset?.tone).filter(Boolean);
    const truncated = state.apps?.truncated === true;
    let tone = 'ok';
    let message = 'Everything running is current';
    if (!tones.length) {
      tone = 'muted'; message = 'Nothing is deployed yet';
    } else if (tones.includes('danger')) {
      tone = 'danger'; message = 'Something on the fleet needs attention';
    } else if (tones.includes('warn')) {
      tone = 'warn'; message = 'Some of the fleet needs attention';
    }
    return statusHeadline({tone, message,
      sub: truncated ? `Showing the first ${state.apps.limit} apps by name — the fleet has more.` : '',
      observedAt: state.observedAt,
      onRefresh: () => load(true)});
  }

  function paint() {
    const apiRowNodes = apiRows();
    const appRowNodes = appRows();
    const children = [
      pageHeader('Control plane', 'Deployments', 'Everything running on your fleet, and what version it’s at.', [
        ctx.capabilities.manage_webapps ? h('button', {class: 'button primary', onclick: () => startWizard(ctx, render)}, icon('plus'), 'New web app') : null,
      ].filter(Boolean)),
      hasPendingWizard() ? resumeBanner(ctx, render) : null,
      headline(apiRowNodes, appRowNodes),
      // Sections fail independently: a platform outage never hides the web
      // apps, and vice versa.
      state.reportError ? h('section', {class: 'row-section'},
        h('h2', {class: 'row-section-label', text: 'API'}),
        errorState(state.reportError, () => load())) : rowSection('API', apiRowNodes),
      state.appsError ? h('section', {class: 'row-section'},
        h('h2', {class: 'row-section-label', text: 'Web apps'}),
        errorState(state.appsError, () => load())) : null,
      wantApps && !state.appsError && state.apps && !appRowNodes.length
        ? h('section', {class: 'row-section'},
          h('h2', {class: 'row-section-label', text: 'Web apps'}),
          emptyState('No web apps yet', 'Choose “New web app” to put your first one online.'))
        : rowSection('Web apps', appRowNodes),
    ].filter(Boolean);
    root.replaceChildren(h('div', {class: 'row-page deployments-body'}, ...children));
    openLinkedInspector();
    schedulePoll();
  }

  // Legacy platform deep links: PlatformDeployment pks are UUIDs, so a
  // non-numeric `inspector` (or the reserved `deployment` key) opens the API
  // drill-in. WebApp links were dispatched to their own page above.
  function openLinkedInspector() {
    if (linkedInspectorOpened) return;
    const routeState = decodeRouteState().state;
    const deployKey = routeState.deployment
      || (routeState.inspector && !/^\d+$/.test(String(routeState.inspector)) ? routeState.inspector : null);
    if (deployKey && state.report) {
      linkedInspectorOpened = true;
      openApiInspector(ctx, deploymentsSection(), render, apiSection());
    }
  }

  await load();
  return root;
}

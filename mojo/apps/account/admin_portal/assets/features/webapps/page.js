// Web apps: the list, and one management view per app. Onboarding lives in
// wizard.js. Day-2 here is plain-language: what's live, deploy history with
// one-click rollback, the deploy key, the setup instructions again, and a
// clearly separated danger area. No framework words in the primary copy.
import {api, apiOnce, badge, formatDate, h, icon, listData, pageHeader, statusTone, TableView} from '../../core.js';
import {modelHeader} from '../../components/model.js';
import {confirmAction, openInspector, openModal} from '../../components/overlays.js';
import {activityHref, decodeRouteState, returnLocation} from '../../components/routes.js';
import {sectionTabs} from '../../components/views.js';
import {hasPendingWizard, resumeWizard, startChangeAddress, startWizard} from './wizard.js';

const MANAGE_SECTIONS = [
  ['overview', 'Overview'], ['deploys', 'Deploys'],
  ['key', 'Deploy key'], ['setup', 'Setup'], ['danger', 'Danger'],
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

// The deploy key is shown once. When the dialog closes we scrub the node, the
// closure, and the response so the value cannot be read back from memory.
function oneTimeSecret(webapp, result, returnFocus) {
  let secret = result.token;
  const secretField = h('textarea', {class: 'secret', readonly: true, rows: '4', text: secret});
  const content = h('div', {},
    h('div', {class: 'callout warning'}, icon('alert'), h('div', {}, h('strong', {text: 'Copy this value now'}),
      h('p', {text: 'It can’t be shown again after you close this. If it’s lost, rotate the key to get a new one.'}))),
    h('label', {class: 'field'}, h('span', {text: 'GitHub Actions secret: MOJO_DEPLOY_KEY'}), secretField),
    h('button', {class: 'button primary', onclick: async (event) => {
      await navigator.clipboard.writeText(secret); event.currentTarget.textContent = 'Copied';
    }}, icon('key'), 'Copy secret'),
    h('div', {class: 'command'}, h('code', {text: 'gh secret set MOJO_DEPLOY_KEY --repo YOUR_ORG/YOUR_REPO'})));
  openModal({title: `${webapp.slug} deployment key`, subtitle: 'The previous key is already inactive.', content, returnFocus, onClose: () => {
    secretField.value = ''; secretField.textContent = ''; secret = ''; result.token = null;
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
    submit.addEventListener('click', async () => {
      submit.disabled = true;
      try {
        const result = await api('/api/edge/webapp/link_key', {method: 'POST', body: JSON.stringify({webapp: webapp.id, action, operation_id: crypto.randomUUID()})});
        close(); await reload();
        if (result.token) oneTimeSecret(webapp, result);
        else openModal({title: `${webapp.slug} secret unavailable`, subtitle: 'The key was created but its value was already consumed.',
          content: h('div', {class: 'callout warning'}, icon('alert'), h('p', {text: 'The one-time value couldn’t be recovered. Rotate the key to receive a fresh secret.'}))});
      } catch (error) { message.textContent = error.message; submit.disabled = false; }
    });
  })();
}

function revokeKey(webapp, reload) {
  confirmAction({title: `Turn off ${webapp.slug}’s deploy key?`, danger: true, confirmLabel: 'Turn key off',
    copy: 'Deploys will fail until you create a new key. Existing releases stay live.'}).then(async (answer) => {
    if (!answer.confirmed) return;
    await api('/api/edge/webapp/revoke_key', {method: 'POST', body: JSON.stringify({webapp: webapp.id, operation_id: crypto.randomUUID()})});
    await reload();
  });
}

function workflowPanel(webapp) {
  const message = h('div', {class: 'form-message', role: 'alert'});
  const panel = h('div', {class: 'setup-block'}, message);
  api('/api/edge/webapp/onboarding/workflow', {method: 'POST', body: JSON.stringify({webapp: webapp.id})})
    .then((result) => {
      const text = h('textarea', {class: 'secret', readonly: true, rows: '18', text: result.yaml});
      panel.replaceChildren(
        h('p', {text: 'Two things set up deploys from GitHub: one secret, and one file.'}),
        h('ol', {class: 'setup-list'},
          h('li', {}, h('strong', {text: 'Add the secret. '}), 'In your repo’s Settings → Secrets, add ', h('code', {text: 'MOJO_DEPLOY_KEY'}), ' (create it in the Deploy key tab).'),
          h('li', {}, h('strong', {text: 'Add the file. '}), 'Save this as ', h('code', {text: result.filename}), ' and push:')),
        text,
        h('button', {class: 'button primary', onclick: async (event) => { await navigator.clipboard.writeText(text.value); event.currentTarget.textContent = 'Copied'; }}, 'Copy file'));
    })
    .catch((error) => { message.textContent = error.message; });
  return panel;
}

async function manageSection(ctx, app, summary, section, body, reload) {
  const manage = ctx.capabilities.manage_webapps;
  const address = summary.address || {};
  if (section === 'overview') {
    const healthValue = h('span', {class: 'muted', text: 'Checking…'});
    body.replaceChildren(detailGrid([
      ['Address', address.hostname ? h('div', {class: 'address-cell'}, h('code', {text: address.hostname}),
        httpsLink(address.https_origin) ? h('a', {class: 'button ghost compact', href: httpsLink(address.https_origin), target: '_blank', rel: 'noopener'}, 'Open') : null) : 'Not set up yet'],
      ['Working right now', healthValue],
      ['Live version', summary.current_release ? `${summary.current_release.version || summary.current_release.id} · ${summary.current_release.status}` : 'No deploys yet'],
      ['Environment', summary.webapp?.environment],
      ['Repository', summary.webapp?.repository || 'Not connected'],
      ['Deploy key', summary.deployment_key?.linked && summary.deployment_key?.active ? 'Active' : 'Not set up'],
    ]));
    if (address.hostname) {
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
    const releases = listData(await api(`/api/edge/webapp/release?webapp=${encodeURIComponent(app.id)}&sort=-created&size=25`));
    const deployments = listData(await api(`/api/edge/webapp/deployment?webapp=${encodeURIComponent(app.id)}&graph=list&sort=-created&size=15`));
    const releaseTable = new TableView({columns: [
      {label: 'Version', render: (r) => h('div', {}, h('strong', {text: r.version}), r.id === currentId ? badge('Live now', 'success') : null)},
      {label: 'State', render: (r) => badge(r.status, statusTone(r.status))},
      {label: 'Uploaded', render: (r) => formatDate(r.created)},
      {label: '', render: (r) => manage && r.id !== currentId && PROMOTABLE.has(r.status)
        ? h('button', {class: 'button compact', onclick: (event) => { event.stopPropagation(); rollbackTo(app, r, reload); }}, 'Roll back to this') : null},
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
    const status = payload.status;
    const state = status.linked && status.active ? ['Active', 'success'] : status.last_action === 'revoke' ? ['Turned off', 'warning'] : ['Not set up', 'neutral'];
    body.replaceChildren(
      h('div', {class: 'credential-status'}, h('div', {}, h('span', {text: 'GitHub Actions secret'}), h('strong', {text: 'MOJO_DEPLOY_KEY'})), badge(state[0], state[1])),
      status.linked ? detailGrid([['Created', formatDate(status.created)], ['Last used', formatDate(status.last_used)]]) : null,
      h('p', {class: 'muted small', text: 'This is the only credential your deploy needs. It can register releases for this app and nothing else.'}),
      manage ? h('div', {class: 'row-actions'},
        h('button', {class: 'button', 'data-webapp-key': app.id, onclick: () => keyDialog(app, reload)}, icon('key'), status.linked ? 'Rotate key' : 'Create key'),
        status.linked ? h('button', {class: 'button ghost', onclick: () => revokeKey(app, reload)}, 'Turn off') : null) : null);
    return;
  }
  if (section === 'setup') {
    body.replaceChildren(
      workflowPanel(app),
      h('details', {class: 'wizard-advanced'}, h('summary', {text: 'Not using GitHub?'}),
        h('p', {class: 'muted small'}, 'You can deploy from any CI or by hand. Follow the release instructions in ',
          h('a', {href: '#', onclick: (e) => e.preventDefault(), title: 'docs/web_developer/edge/releases.md'}, 'the release guide'),
          ' — register a release with your ', h('code', {text: 'MOJO_DEPLOY_KEY'}), ' against this app’s id (', h('code', {text: `#${app.id}`}), ').')));
    return;
  }
  // danger
  if (!manage) { body.replaceChildren(h('p', {class: 'muted', text: 'You don’t have permission to change this app.'})); return; }
  const changeAddress = h('button', {class: 'button', onclick: async () => {
    const full = await api(`/api/edge/webapp/${encodeURIComponent(app.id)}`);
    startChangeAddress(ctx, reload, {
      group_id: full.group?.id, slug: full.slug, display_name: full.display_name,
      environment: full.environment, bucket: full.bucket, github_repository: full.github_repository,
      deployment_ref: full.deployment_ref, build_output: full.build_output,
    });
  }}, icon('globe'), 'Change address');
  const takeOffline = address.hostname ? h('button', {class: 'button', onclick: () => {
    confirmAction({title: `Take ${app.slug} offline?`, danger: true, confirmLabel: 'Take offline', requireReason: true, reasonLabel: 'Why?',
      copy: 'Visitors will stop reaching your app. The app and its versions are kept — you can put it back on an address later.'}).then(async (answer) => {
      if (!answer.confirmed) return;
      await api('/api/edge/webapp/detach_address', {method: 'POST', body: JSON.stringify({webapp: app.id})}); await reload();
    });
  }}, 'Take offline') : null;
  const deleteApp = h('button', {class: 'button danger', onclick: () => {
    confirmAction({title: `Delete ${app.slug}?`, danger: true, confirmLabel: 'Delete app', requireReason: true, reasonLabel: 'Why?',
      copy: 'This removes the app, its address, its deploy key, and its deploy history for good. This cannot be undone.'}).then(async (answer) => {
      if (!answer.confirmed) return;
      await api(`/api/edge/webapp/${encodeURIComponent(app.id)}`, {method: 'DELETE'});
      body.dispatchEvent(new CustomEvent('mojo-webapp-deleted', {bubbles: true}));
    });
  }}, icon('trash'), 'Delete app');
  body.replaceChildren(
    h('div', {class: 'danger-row'}, h('div', {}, h('strong', {text: 'Change address'}), h('p', {class: 'muted small', text: 'Move this app to a different address. The current one keeps serving until the new one is ready.'})), changeAddress),
    takeOffline ? h('div', {class: 'danger-row'}, h('div', {}, h('strong', {text: 'Take offline'}), h('p', {class: 'muted small', text: 'Stop serving the app without deleting it.'})), takeOffline) : null,
    h('div', {class: 'danger-row danger'}, h('div', {}, h('strong', {text: 'Delete this app'}), h('p', {class: 'muted small', text: 'Remove the app and everything about it. Permanent.'})), deleteApp));
}

function rollbackTo(app, release, reload) {
  confirmAction({title: `Roll back to ${release.version}?`, danger: true, confirmLabel: 'Roll back', requireReason: true, reasonLabel: 'Why are you rolling back?',
    copy: `This makes ${release.version} live again across your fleet. Visitors will see that version within a few minutes.`}).then(async (answer) => {
    if (!answer.confirmed) return;
    await api('/api/edge/webapp/rollback', {method: 'POST', body: JSON.stringify({webapp: app.id, release: release.id})});
    await reload();
  });
}

async function openManage(ctx, webapp, reloadList) {
  const summary = await api(`/api/edge/webapp/summary?webapp=${encodeURIComponent(webapp.id)}`);
  const manage = ctx.capabilities.manage_webapps;
  const sections = MANAGE_SECTIONS.filter(([id]) => id !== 'danger' || manage);
  let active = 'overview';
  const body = h('div', {class: 'inspector-section'});
  const tabs = sectionTabs({items: sections.map(([id, label]) => ({id, label})), active, onChange: async (id) => {
    active = id; [...tabs.querySelectorAll('button')].forEach((button, index) => button.classList.toggle('active', sections[index][0] === id));
    await manageSection(ctx, webapp, summary, id, body, reload);
  }});
  const reload = async () => {
    Object.assign(summary, await api(`/api/edge/webapp/summary?webapp=${encodeURIComponent(webapp.id)}`));
    await manageSection(ctx, webapp, summary, active, body, reload); await reloadList();
  };
  const address = summary.address || {};
  const status = summary.current_release ? 'Live' : address.hostname ? 'Ready' : 'Setup';
  const header = modelHeader({iconName: 'deploy', primary: webapp.display_name || webapp.slug,
    secondary: address.hostname || 'No address yet', status, actions: [
      {label: 'Open in a new tab', capability: Boolean(httpsLink(address.https_origin)), run: () => { const safe = httpsLink(address.https_origin); if (safe) window.open(safe, '_blank', 'noopener'); }},
      {label: 'Related activity', run: () => { location.hash = activityHref('events', {type: 'model', id: webapp.id, model: 'WebApp'}, {return: returnLocation()}); }},
    ], context: {webapp}});
  const inspector = openInspector({title: `Web app · ${webapp.display_name || webapp.slug}`, content: h('div', {class: 'webapp-inspector'}, header, tabs, body), wide: true});
  body.addEventListener('mojo-webapp-deleted', async () => { inspector.close(); await reloadList(); });
  await manageSection(ctx, webapp, summary, active, body, reload);
}

function resumeBanner(ctx, reloadApps) {
  return h('section', {class: 'panel resume-banner'}, h('div', {class: 'panel-heading'},
    h('div', {}, h('h2', {text: 'You have a setup in progress'}), h('p', {text: 'Pick up where you left off, or start over.'})),
    h('button', {class: 'button primary', onclick: () => resumeWizard(ctx, reloadApps)}, 'Resume setup')));
}

export async function webappsPage(ctx) {
  const root = h('div', {class: 'page'}); let linkedInspectorOpened = false;
  async function render() {
    root.replaceChildren(pageHeader('Deployments', 'Web apps', 'Put a web app online at your own address, then manage it here.', [
      ctx.capabilities.manage_webapps ? h('button', {class: 'button primary', onclick: () => startWizard(ctx, render)}, icon('plus'), 'New web app') : null,
    ].filter(Boolean)));
    if (hasPendingWizard()) root.append(resumeBanner(ctx, render));
    const panel = h('section', {class: 'panel'}, h('div', {class: 'panel-heading'}, h('div', {}, h('h2', {text: 'Your apps'}), h('p', {text: 'Select an app to see its address, deploys, and settings.'}))));
    root.append(panel);
    try {
      const rows = listData(await api('/api/edge/webapp'));
      panel.append(new TableView({columns: [
        {label: 'App', render: (r) => h('div', {}, h('strong', {text: r.display_name || r.slug}), h('small', {class: 'mono', text: `#${r.id}`}))},
        {label: 'Address', render: (r) => r.vhost?.server_name ? h('code', {text: r.vhost.server_name}) : h('span', {class: 'muted', text: 'Not set up yet'})},
        {label: 'Live version', render: (r) => r.current_release ? badge(r.current_release.version || r.current_release.id, 'success') : badge('No deploys yet')},
        {label: 'Created', render: (r) => formatDate(r.created)},
        {label: '', render: () => icon('chevron')},
      ], rows, empty: 'No web apps yet. Choose “New web app” to put your first one online.', onSelect: (row) => openManage(ctx, row, render)}).render());
      const inspector = decodeRouteState().state.inspector;
      const linked = inspector && rows.find((row) => String(row.id) === String(inspector));
      if (linked && !linkedInspectorOpened) { linkedInspectorOpened = true; await openManage(ctx, linked, render); }
    } catch (error) { panel.append(h('div', {class: 'error-state'}, icon('alert'), h('p', {text: error.message}))); }
  }
  await render(); return root;
}

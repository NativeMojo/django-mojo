// API-side rows and drill-ins for the merged Deployments lane: the API
// service row (deploy history, per-attempt same-SHA recovery) and the
// django-mojo framework row (installed vs published, update, hold). Web-app
// rows and their management inspector stay in page.js — this file owns what
// the platform tier serves. Admin never chooses a new sha here: new deploys
// come from CI, and the controls are retry-same-SHA, verify, and converge.
import {api, apiOnce, badge, formatDate, h, icon, statusTone} from '../../core.js';
import {openInspector, openModal} from '../../components/overlays.js';
import {activityHref, returnLocation} from '../../components/routes.js';
import {statusRow} from '../../components/rows.js';

export const PLATFORM_SECTIONS_PATH = '/api/account/admin/platform?sections=deployments,api';
export const FRAMEWORK_PATH = '/api/account/admin/platform/framework';
const FRAMEWORK_UPDATE_PATH = '/api/account/admin/platform/framework/update';
const ADVANCED_SETTINGS_PATH = '/api/account/admin/advanced/settings';
const DEPLOY_ACTION_PATH = '/api/account/admin/platform/deploy/';
const DEPLOY_ACTIONS = [
  ['retry', 'Retry same SHA'], ['verify', 'Verify'], ['converge', 'Converge'],
];
const ACTIVE_STATUSES = new Set(['requested', 'canary', 'fleet', 'partial']);

// Never a dead control: when the update cannot happen, the drill-in says which
// thing is in the way instead of offering a button that fails.
const BLOCKED_COPY = {
  update_unavailable: 'No newer django-mojo release is published, or the update check is unavailable.',
  requires_superuser: 'The fleet is pinned to a version, and clearing the pin requires an active superuser.',
  no_converged_deployment: 'No deployment has ever converged on this fleet, so there is no proven commit to redeploy.',
  // INFRASTRUCTURE_MODE=external. can_update is already false in that mode, so
  // the row and the inspector reach this copy through the paths they already
  // had — the Update button is never offered, and the words say who owns it.
  infrastructure_external: 'external infrastructure mode — the update is applied by your infrastructure team\'s IaC.',
};

const PIN_COPY = {
  latest: 'Updates follow the newest published release.',
  hold: 'Updates are paused — deploys keep installing the version already proven on this fleet.',
  pinned: 'Deploys install exactly the pinned version until it is cleared.',
};

function shortId(value) {
  return String(value || '').slice(0, 10);
}

function provenCounts(row) {
  const evidence = Array.isArray(row.node_evidence) ? row.node_evidence : [];
  const roster = Array.isArray(row.frozen_roster) && row.frozen_roster.length
    ? row.frozen_roster.length : evidence.length;
  const proven = evidence.filter((item) => item && item.state === 'proven').length;
  return {proven, roster};
}

// statusRow renders its action as a .row-link anchor; wire the handler onto it
// (same pattern as the Maintenance lane).
function wireAction(row, run) {
  const link = row.querySelector('.row-link');
  if (link) {
    link.addEventListener('click', (event) => { event.preventDefault(); run(); });
  }
  return row;
}

function detailLink(label, run) {
  const link = h('a', {class: 'row-link', href: '#'}, label);
  link.addEventListener('click', (event) => { event.preventDefault(); run(); });
  return link;
}

// ---------------------------------------------------------------------------
// API service row + drill-in
// ---------------------------------------------------------------------------

export function apiServiceRow(ctx, deployments, {onOpen}) {
  const manage = ctx.features?.platform?.capabilities?.manage === true;
  const label = manage ? 'Deploy' : 'Open';
  if (!deployments || ['timeout', 'unavailable', 'unauthorized'].includes(deployments.status)) {
    // Degraded evidence is stated in the envelope's own vocabulary — never a
    // green row invented from missing data.
    return statusRow({tone: 'warn', name: 'API service',
      value: 'Deploy history is unavailable right now',
      detail: deployments ? deployments.status : 'no evidence'});
  }
  const rows = deployments.data?.items || [];
  if (!rows.length) {
    return wireAction(statusRow({tone: 'muted', name: 'API service',
      value: 'No deployments recorded yet',
      detail: 'new deploys arrive from CI',
      action: {label: 'Open', href: '#'}}), onOpen);
  }
  const latest = rows[0];
  const when = formatDate(latest.finished || latest.created);
  const {proven, roster} = provenCounts(latest);
  let tone = 'muted';
  let value = `Outcome unknown · ${when}`;
  if (latest.status === 'converged') {
    tone = 'ok'; value = `Deployed ${when} · all nodes converged`;
  } else if (latest.status === 'verified') {
    tone = 'ok'; value = `Deployed ${when} · verified on ${proven} of ${roster} nodes`;
  } else if (ACTIVE_STATUSES.has(latest.status)) {
    tone = 'warn'; value = `Deploying since ${formatDate(latest.created)} — ${proven} of ${roster} nodes proven`;
  } else if (latest.status === 'failed') {
    tone = 'danger'; value = `Deploy failed ${when} — ${proven} of ${roster} nodes proven`;
  } else if (latest.status === 'superseded') {
    value = `Superseded by a newer deploy · ${when}`;
  }
  if (deployments.status === 'stale' && tone === 'ok') {
    tone = 'warn'; value = `${value} · evidence is stale`;
  }
  const row = statusRow({tone, name: 'API service', value,
    detailNode: h('span', {class: 'row-detail mono', text: shortId(latest.sha)}),
    action: {label, href: '#'}});
  return wireAction(row, onOpen);
}

function attemptView(ctx, row, act, message) {
  const manage = ctx.features?.platform?.capabilities?.manage === true;
  const {proven, roster} = provenCounts(row);
  const buttons = manage ? h('div', {class: 'row-actions'},
    ...DEPLOY_ACTIONS.map(([action, label]) => h('button', {
      class: 'button compact',
      onclick: (event) => act(action, row, event.currentTarget, message),
    }, label))) : null;
  return h('article', {class: 'panel deploy-attempt'},
    h('div', {class: 'deploy-attempt-head'},
      h('code', {class: 'mono', text: shortId(row.sha)}),
      badge(row.status, statusTone(row.status)),
      h('span', {class: 'muted', text: formatDate(row.finished || row.created)})),
    h('p', {class: 'muted small',
      text: `${row.actor || 'unknown actor'} · ${row.source || 'unknown source'} · ${proven} of ${roster} nodes proven`}),
    buttons,
    h('details', {class: 'deploy-technical'}, h('summary', {text: 'Technical details'}),
      h('dl', {class: 'details'},
        h('div', {}, h('dt', {text: 'Commit'}), h('dd', {class: 'mono', text: row.sha})),
        h('div', {}, h('dt', {text: 'Attempt id'}), h('dd', {class: 'mono', text: row.id})),
        h('div', {}, h('dt', {text: 'Framework'}), h('dd', {text: row.framework_version || 'unknown'}))),
      h('a', {class: 'related-record', href: activityHref('events', {}, {
        search: row.id, return: returnLocation(),
      })}, h('strong', {text: 'Related deployment activity'}), icon('chevron'))));
}

export function openApiInspector(ctx, deployments, reload) {
  const data = deployments?.data || {};
  const rows = data.items || [];
  const coordination = data.coordination || {};
  const message = h('div', {class: 'form-message', role: 'alert'});
  let closeInspector = null;

  const act = async (action, row, button, alert) => {
    button.disabled = true; alert.textContent = '';
    try {
      await api(`${DEPLOY_ACTION_PATH}${action}`, {
        method: 'POST', body: JSON.stringify({deployment: row.id})});
      closeInspector?.close();
      await reload();
    } catch (error) {
      button.disabled = false;
      if (error?.code !== 'fresh_auth_required') alert.textContent = error.message;
    }
  };

  const content = h('div', {class: 'deploy-inspector'},
    h('p', {class: 'muted small',
      text: 'New deploys arrive from CI. Admin can retry a proven commit, request verification, or converge the fleet — it never chooses a new commit.'}),
    coordination.state ? h('p', {class: 'muted small',
      text: `Coordination: ${coordination.state}${coordination.deployment ? ` · ${shortId(coordination.deployment)}` : ''}`}) : null,
    message,
    rows.length
      ? h('div', {class: 'deploy-attempts'},
        ...rows.map((row) => attemptView(ctx, row, act, message)))
      : h('p', {class: 'muted', text: 'No platform deployment attempts have been recorded.'}));
  closeInspector = openInspector({title: 'API service · deploy history', content, wide: true});
  return closeInspector;
}

// ---------------------------------------------------------------------------
// django-mojo framework row + drill-in
// ---------------------------------------------------------------------------

export function frameworkRow(ctx, framework, {onOpen, onUpdate}) {
  if (!framework) {
    return wireAction(statusRow({tone: 'muted', name: 'django-mojo',
      value: 'Version status is unavailable',
      action: {label: 'Details', href: '#'}}), onOpen);
  }
  const installed = framework.installed || '—';
  const pin = framework.pin || {mode: 'latest', value: null};
  const manage = ctx.features?.platform?.capabilities?.manage === true;
  if (framework.update_available) {
    // The Update control belongs only to callers the endpoint would accept
    // (manage tier); viewers still see the fact, with the drill-in as action.
    const canUpdate = framework.can_update && manage;
    const row = statusRow({tone: 'warn', name: 'django-mojo',
      value: `${framework.latest} available — installed ${installed}`,
      detailNode: canUpdate ? detailLink('Details', onOpen) : null,
      action: {label: canUpdate ? 'Update' : 'Details', href: '#'}});
    return wireAction(row, canUpdate ? onUpdate : onOpen);
  }
  if (pin.mode === 'hold') {
    return wireAction(statusRow({tone: 'muted', name: 'django-mojo',
      value: `Held at ${framework.resolved_pin || installed} — updates paused`,
      action: {label: 'Details', href: '#'}}), onOpen);
  }
  if (pin.mode === 'pinned') {
    return wireAction(statusRow({tone: 'muted', name: 'django-mojo',
      value: `Pinned to ${pin.value} — updates paused`,
      action: {label: 'Details', href: '#'}}), onOpen);
  }
  if (framework.source === 'unavailable' || !framework.latest) {
    return wireAction(statusRow({tone: 'muted', name: 'django-mojo',
      value: `v${installed} installed · latest unknown`,
      action: {label: 'Details', href: '#'}}), onOpen);
  }
  if (framework.update_available) {
    // update_available but not can_update: behind, with a named blocker.
    return wireAction(statusRow({tone: 'warn', name: 'django-mojo',
      value: `${framework.latest} available — installed ${installed}`,
      detail: framework.blocked_reason ? framework.blocked_reason.replaceAll('_', ' ') : '',
      detailTone: 'warning',
      action: {label: 'Details', href: '#'}}), onOpen);
  }
  return wireAction(statusRow({tone: 'ok', name: 'django-mojo',
    value: `v${installed} · up to date`,
    action: {label: 'Details', href: '#'}}), onOpen);
}

// A bespoke modal rather than confirmAction(): the operator types the exact
// version, and the server re-checks the same echo (`confirm_version`).
function confirmFrameworkUpdate(framework) {
  return new Promise((resolve) => {
    let settled = false;
    const settle = (value) => { if (settled) return; settled = true; close(); resolve(value); };
    const typed = h('input', {autocomplete: 'off', placeholder: framework.latest});
    const cancel = h('button', {class: 'button ghost', type: 'button',
      onclick: () => settle(null)}, 'Cancel');
    const apply = h('button', {class: 'button danger', type: 'button', disabled: true,
      onclick: () => settle({version: framework.latest})}, 'Update django-mojo');
    const validate = () => { apply.disabled = typed.value.trim() !== framework.latest; };
    typed.addEventListener('input', validate);
    const pinned = framework.pin?.mode && framework.pin.mode !== 'latest';
    const content = h('div', {},
      h('div', {class: 'callout warning'}, icon('alert'),
        h('p', {text: 'This restarts every node\'s service — the canary migrates first, then the fleet rolls. Watch it in the API drill-in above.'})),
      pinned ? h('p', {class: 'modal-copy',
        text: 'It also returns the fleet to the automatic latest-version policy: the current pin is cleared and future deploys install the newest published release.'}) : null,
      h('dl', {class: 'details'},
        h('div', {}, h('dt', {text: 'Installed'}),
          h('dd', {class: 'mono', text: framework.installed || '—'})),
        h('div', {}, h('dt', {text: 'Updating to'}),
          h('dd', {class: 'mono', text: framework.latest || '—'}))),
      h('label', {class: 'field'},
        h('span', {text: `Type ${framework.latest} to confirm`}), typed),
      h('div', {class: 'form-actions'}, cancel, apply));
    const close = openModal({
      title: 'Update django-mojo?',
      subtitle: `${framework.installed || ''} → ${framework.latest || ''}`,
      content, danger: true,
      onClose: () => { if (!settled) { settled = true; resolve(null); } },
    });
    validate();
  });
}

export async function applyFrameworkUpdate(ctx, framework, reload) {
  if (!framework?.can_update) return;
  const choice = await confirmFrameworkUpdate(framework);
  if (!choice) return;
  try {
    // One shot, no transport retry: an ambiguous outcome is reconciled by
    // re-reading the framework overview and the deploy history.
    await apiOnce(FRAMEWORK_UPDATE_PATH, {method: 'POST', body: JSON.stringify({
      version: choice.version, confirm_version: choice.version,
    })});
  } catch (error) {
    if (error?.code === 'fresh_auth_required') return;
    openModal({title: 'Update not started', content: h('div', {class: 'callout warning'},
      icon('alert'), h('p', {text: error.message}))});
  }
  await reload();
}

async function writeFrameworkPin(value, reload) {
  // The existing owner-tier writer — surfaced, not reimplemented. "hold"
  // pauses updates at the proven version; "" resumes the latest policy.
  try {
    await apiOnce(ADVANCED_SETTINGS_PATH, {method: 'POST', body: JSON.stringify({
      framework_pin: value,
    })});
  } catch (error) {
    if (error?.code === 'fresh_auth_required') return;
    openModal({title: 'Hold not changed', content: h('div', {class: 'callout warning'},
      icon('alert'), h('p', {text: error.message}))});
  }
  await reload();
}

export function openFrameworkInspector(ctx, framework, reload) {
  const pin = framework?.pin || {mode: 'latest', value: null};
  const ownerEdit = ctx.capabilities?.settings_owner_edit === true;
  const manage = ctx.features?.platform?.capabilities?.manage === true;
  const rows = h('dl', {class: 'details'},
    h('div', {}, h('dt', {text: 'Installed'}),
      h('dd', {class: 'mono', text: framework?.installed || 'unknown'})),
    h('div', {}, h('dt', {text: 'Latest published'}),
      h('dd', {class: 'mono', text: framework?.latest || 'unknown'})),
    h('div', {}, h('dt', {text: 'Checked'}),
      h('dd', {text: formatDate(framework?.checked_at)})),
    h('div', {}, h('dt', {text: 'Source'}),
      h('dd', {text: framework?.source || 'unavailable'})));
  const inspector = openInspector({
    title: 'django-mojo framework',
    content: h('div', {class: 'framework-inspector'}, rows,
      h('p', {class: 'muted small', text: PIN_COPY[pin.mode] || PIN_COPY.latest}),
      framework?.can_update && manage
        ? h('div', {class: 'form-actions'}, h('button', {class: 'button primary',
          onclick: async () => { inspector.close(); await applyFrameworkUpdate(ctx, framework, reload); },
        }, `Update to ${framework.latest}`))
        : h('p', {class: 'muted small',
          text: framework?.can_update && !manage
            ? 'Updating requires platform manage access.'
            : (BLOCKED_COPY[framework?.blocked_reason] || 'No update is available right now.')}),
      ownerEdit ? h('div', {class: 'form-actions'},
        pin.mode === 'latest'
          ? h('button', {class: 'button ghost', onclick: async () => {
            inspector.close(); await writeFrameworkPin('hold', reload);
          }}, 'Hold updates')
          : h('button', {class: 'button ghost', onclick: async () => {
            inspector.close(); await writeFrameworkPin('', reload);
          }}, pin.mode === 'hold' ? 'Resume updates' : 'Clear pin and follow latest')) : null),
  });
  return inspector;
}

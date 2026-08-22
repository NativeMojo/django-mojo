import {api, apiOnce, h, icon, pageHeader} from '../../core.js';
import {runAction} from '../../components/actions.js';
import {openModal} from '../../components/overlays.js';
import {decodeRouteState, routeHref} from '../../components/routes.js';
import {rowSection, statusHeadline, statusRow} from '../../components/rows.js';
import {errorState, loadingState, permissionDeniedState} from '../../components/views.js';

const VERSIONS_PATH = '/api/aws/maintenance/versions';
const STATUS_PATH = '/api/aws/maintenance/status';
const APPLY_PATH = '/api/aws/maintenance/apply';
const FRAMEWORK_PATH = '/api/account/admin/platform/framework';
const FRAMEWORK_UPDATE_PATH = '/api/account/admin/platform/framework/update';

const DATABASE_KINDS = ['rds-instance', 'rds-cluster'];
const CACHE_KINDS = ['elasticache'];

// An upgrade is amber while there is still time and red once the published
// deadline is inside a month or already gone. A finding with no deadline is
// still worth doing and still amber — AWS simply has not said when.
const DEADLINE_SOON_DAYS = 30;

const POLL_INTERVAL = 10000;
const POLL_LIMIT = 180; // 30 minutes at one poll every ten seconds.

const BLOCKED_COPY = {
  update_unavailable: 'No newer django-mojo release is published, or PyPI is not answering.',
  requires_superuser: 'The fleet is pinned to a version. Only a superuser can clear the pin.',
  no_converged_deployment: 'No deployment has ever converged on this fleet, so there is no proven commit to redeploy.',
  infrastructure_external: 'external infrastructure mode — the update is applied by your infrastructure team\'s IaC.',
};

const EXTERNAL_DETAIL = 'external infrastructure mode — upgrades are applied by your infrastructure team\'s IaC';
const EXTERNAL_SUB = 'Infrastructure mode is external, so this portal does not apply upgrades.';

const KIND_LABELS = {
  'rds-instance': 'database', 'rds-cluster': 'cluster', elasticache: 'cache',
};

function findingTone(finding) {
  const days = finding.days_remaining;
  if (typeof days === 'number' && days <= DEADLINE_SOON_DAYS) return 'danger';
  return 'warn';
}

function deadlineText(finding) {
  const days = finding.days_remaining;
  if (typeof days !== 'number') return 'no end-of-life date published';
  if (days < 0) return `support ended ${Math.abs(days)} days ago`;
  return `support ends in ${days} days`;
}

function versionNode(finding) {
  return [
    h('span', {class: 'mono', text: finding.current_version || '—'}),
    h('span', {class: 'maintenance-arrow', text: ' → '}),
    h('span', {class: 'mono', text: finding.available_major || '—'}),
  ];
}

function warningRows(warnings) {
  return (warnings || []).map((warning) => statusRow({
    tone: 'warn', name: warning.iam_action || 'AWS access',
    value: 'Denied',
    detail: 'this API was refused, so its resources were not inventoried',
    detailTone: 'warning'}));
}

// ---------------------------------------------------------------------------
// Confirmation
//
// A bespoke modal rather than confirmAction(): the operator is choosing an
// apply window and echoing an identifier, and neither fits a yes/no dialog.
// The typed echo is what separates "the right database" from "the row I
// happened to click".
// ---------------------------------------------------------------------------

function consequenceCopy(finding) {
  const noun = KIND_LABELS[finding.kind] || 'resource';
  const base = finding.kind === 'elasticache'
    ? `Upgrading this ${noun} restarts its nodes. Clients reconnect, and cached data may be lost.`
    : `Upgrading this ${noun} takes it offline while AWS runs the engine upgrade. A Multi-AZ deployment fails over; a single instance is simply unavailable.`;
  return `${base} A major-version upgrade cannot be undone — going back means restoring a snapshot.`;
}

function windowChoice(onChange) {
  const name = `apply-window-${Math.random().toString(36).slice(2)}`;
  const option = (value, label, copy, checked) => {
    const input = h('input', {type: 'radio', name, value, checked: checked || null,
      onchange: () => onChange(value === 'now')});
    return h('label', {class: 'check-field maintenance-window'}, input,
      h('span', {}, h('strong', {text: label}), h('small', {text: copy})));
  };
  return h('div', {class: 'maintenance-windows'},
    option('window', 'At the next maintenance window',
      'AWS applies it during the resource\'s configured window.', true),
    option('now', 'Immediately', 'The outage starts as soon as you confirm.'));
}

function confirmUpgrade(finding) {
  return new Promise((resolve) => {
    let immediate = false;
    let settled = false;
    const settle = (value) => { if (settled) return; settled = true; close(); resolve(value); };
    const typed = h('input', {autocomplete: 'off', placeholder: finding.resource_id});
    const message = h('div', {class: 'form-message', role: 'alert'});
    const cancel = h('button', {class: 'button ghost', type: 'button',
      onclick: () => settle(null)}, 'Cancel');
    const apply = h('button', {class: 'button danger', type: 'button', disabled: true,
      onclick: () => settle({apply_immediately: immediate})}, 'Apply upgrade');
    const validate = () => { apply.disabled = typed.value.trim() !== finding.resource_id; };
    typed.addEventListener('input', validate);
    const content = h('div', {},
      h('div', {class: 'callout warning'}, icon('alert'),
        h('p', {text: consequenceCopy(finding)})),
      h('dl', {class: 'details'},
        h('div', {}, h('dt', {text: 'Current'}),
          h('dd', {class: 'mono', text: finding.current_version || '—'})),
        h('div', {}, h('dt', {text: 'Upgrading to'}),
          h('dd', {class: 'mono', text: finding.available_major || '—'}))),
      windowChoice((value) => { immediate = value; }),
      h('label', {class: 'field'},
        h('span', {text: `Type ${finding.resource_id} to confirm`}), typed),
      message,
      h('div', {class: 'form-actions'}, cancel, apply));
    const close = openModal({
      title: `Upgrade ${finding.resource_id}?`,
      subtitle: `${finding.engine || 'engine'} ${finding.current_version || ''} → ${finding.available_major || ''}`,
      content, danger: true,
      onClose: () => { if (!settled) { settled = true; resolve(null); } },
    });
    validate();
  });
}

function confirmFrameworkUpdate(overview) {
  return new Promise((resolve) => {
    let settled = false;
    const settle = (value) => { if (settled) return; settled = true; close(); resolve(value); };
    const typed = h('input', {autocomplete: 'off', placeholder: overview.latest});
    const message = h('div', {class: 'form-message', role: 'alert'});
    const cancel = h('button', {class: 'button ghost', type: 'button',
      onclick: () => settle(null)}, 'Cancel');
    const apply = h('button', {class: 'button danger', type: 'button', disabled: true,
      onclick: () => settle({version: overview.latest})}, 'Update django-mojo');
    const validate = () => { apply.disabled = typed.value.trim() !== overview.latest; };
    typed.addEventListener('input', validate);
    const pinned = overview.pin?.mode && overview.pin.mode !== 'latest';
    const content = h('div', {},
      h('div', {class: 'callout warning'}, icon('alert'),
        h('p', {text: 'This restarts every node\'s service — the canary migrates first, then the fleet rolls. Watch it on Deployments.'})),
      pinned
        ? h('p', {class: 'modal-copy',
          text: 'It also returns the fleet to the automatic latest-version policy: the current pin is cleared and future deploys install the newest published release.'})
        : null,
      h('dl', {class: 'details'},
        h('div', {}, h('dt', {text: 'Installed'}),
          h('dd', {class: 'mono', text: overview.installed || '—'})),
        h('div', {}, h('dt', {text: 'Updating to'}),
          h('dd', {class: 'mono', text: overview.latest || '—'}))),
      h('label', {class: 'field'},
        h('span', {text: `Type ${overview.latest} to confirm`}), typed),
      message,
      h('div', {class: 'form-actions'}, cancel, apply));
    const close = openModal({
      title: 'Update django-mojo?',
      subtitle: `${overview.installed || ''} → ${overview.latest || ''}`,
      content, danger: true,
      onClose: () => { if (!settled) { settled = true; resolve(null); } },
    });
    validate();
  });
}

// ---------------------------------------------------------------------------

export async function maintenancePage(ctx, signal = null) {
  if (!ctx.features?.platform?.capabilities?.maintenance) {
    return permissionDeniedState(
      'Infrastructure maintenance requires the manage_aws permission.');
  }

  // A MISSING flag means managed: an older server that never learned about
  // INFRASTRUCTURE_MODE must not have its controls silently disabled. Only an
  // explicit false — a server that said "external" — takes them away.
  const managed = ctx.features?.platform?.capabilities?.infrastructure_managed !== false;

  const body = h('div', {class: 'row-page maintenance-body'}, loadingState('Loading…'));
  const root = h('div', {class: 'maintenance-page'},
    pageHeader('Platform control plane', 'Maintenance',
      'Pending managed-service upgrades and the framework this installation runs, with the controls to apply them.'),
    body);
  // Resource id -> the progress line currently replacing its row.
  const progress = new Map();
  let report = null;
  let framework = null;
  let busy = false;

  function wait(milliseconds) {
    return new Promise((resolve) => {
      const timeout = setTimeout(resolve, milliseconds);
      signal?.addEventListener('abort',
        () => { clearTimeout(timeout); resolve(); }, {once: true});
    });
  }

  function note(resource, tone, text) {
    progress.set(resource, {tone, text});
    render();
  }

  // Poll until the resource settles, then report on the ENGINE VERSION, not on
  // the status: a resource that returns to "available" still on its old
  // version means the upgrade did not take effect, and saying "done" there is
  // the single most misleading thing this page could do.
  async function follow(finding) {
    const params = new URLSearchParams({
      kind: finding.kind, resource: finding.resource_id,
      target_version: finding.available_major,
    });
    for (let count = 0; count < POLL_LIMIT && !signal?.aborted; count += 1) {
      await wait(POLL_INTERVAL);
      if (signal?.aborted) return;
      let live;
      try {
        live = await api(`${STATUS_PATH}?${params.toString()}`, {signal});
      } catch (error) {
        if (error?.name === 'AbortError') return;
        note(finding.resource_id, 'warn',
          `AWS did not report status: ${error.message}`);
        continue;
      }
      if (!live.settled) {
        note(finding.resource_id, 'warn',
          `${live.status || 'working'} — waiting for AWS to finish`);
        continue;
      }
      if (live.upgraded) {
        note(finding.resource_id, 'ok', `upgraded to ${finding.available_major}`);
        await load();
        return;
      }
      note(finding.resource_id, 'danger',
        'the engine version is unchanged — the upgrade did not take effect');
      return;
    }
    if (!signal?.aborted) {
      note(finding.resource_id, 'warn',
        'still running after 30 minutes — check the AWS console');
    }
  }

  async function applyUpgrade(finding) {
    if (busy || !managed) return;
    const choice = await confirmUpgrade(finding);
    if (!choice) return;
    busy = true;
    note(finding.resource_id, 'warn', 'requesting the upgrade…');
    try {
      await apiOnce(APPLY_PATH, {method: 'POST', signal, body: JSON.stringify({
        kind: finding.kind, resource: finding.resource_id,
        target_version: finding.available_major,
        confirm_resource: finding.resource_id,
        apply_immediately: choice.apply_immediately,
      })});
    } catch (error) {
      busy = false;
      if (error?.code === 'fresh_auth_required') { progress.delete(finding.resource_id); render(); return; }
      note(finding.resource_id, 'danger', error.message);
      return;
    }
    busy = false;
    note(finding.resource_id, 'warn', 'requested — waiting for AWS');
    follow(finding);
  }

  async function applyFrameworkUpdate() {
    if (busy || !managed || !framework?.can_update) return;
    const choice = await confirmFrameworkUpdate(framework);
    if (!choice) return;
    busy = true;
    note('django-mojo', 'warn', 'requesting the update…');
    try {
      await apiOnce(FRAMEWORK_UPDATE_PATH, {method: 'POST', signal, body: JSON.stringify({
        version: choice.version, confirm_version: choice.version,
      })});
    } catch (error) {
      busy = false;
      if (error?.code === 'fresh_auth_required') { progress.delete('django-mojo'); render(); return; }
      note('django-mojo', 'danger', error.message);
      return;
    }
    busy = false;
    // A deploy already has its own page with per-node proof; polling status
    // here would be a worse copy of it.
    note('django-mojo', 'warn', 'deploy requested — follow it on Deployments');
  }

  function findingRow(finding) {
    const running = progress.get(finding.resource_id);
    if (running) {
      return statusRow({tone: running.tone, name: finding.resource_id,
        valueNode: versionNode(finding), detail: running.text,
        detailTone: running.tone === 'danger' ? 'danger'
          : running.tone === 'ok' ? '' : 'warning'});
    }
    if (!managed) {
      // The finding still matters — an out-of-support engine is out of support
      // whoever applies the fix. Only the control goes away, replaced by who
      // owns the change here.
      return statusRow({
        tone: findingTone(finding), name: finding.resource_id,
        valueNode: versionNode(finding),
        detail: `${deadlineText(finding)} · ${EXTERNAL_DETAIL}`,
        detailTone: 'warning',
      });
    }
    return statusRow({
      tone: findingTone(finding), name: finding.resource_id,
      valueNode: versionNode(finding),
      detail: deadlineText(finding),
      detailTone: findingTone(finding) === 'danger' ? 'danger' : 'warning',
      action: {label: 'Upgrade…', href: '#'},
    });
  }

  // † and human-input in one control, exactly as in capacity.js: the typed-echo
  // confirmation comes first (nothing paints while a human is reading it), and
  // the moment they confirm, note() replaces this row with its own progress
  // line. There is no surviving node to pin a pending state to, so the wrapper
  // is headless and contributes only the guard — one modal per row, per click.
  function upgradeRow(finding) {
    const row = findingRow(finding);
    const link = row.querySelector('.row-link');
    if (link) {
      link.addEventListener('click', (event) => {
        event.preventDefault();
        runAction(null, () => applyUpgrade(finding), {key: link});
      });
    }
    return row;
  }

  function findingsFor(kinds) {
    return (report?.findings || []).filter((row) => kinds.includes(row.kind));
  }

  function frameworkRow() {
    const running = progress.get('django-mojo');
    const installed = framework?.installed || '—';
    if (running) {
      return statusRow({tone: running.tone, name: 'django-mojo', value: installed,
        mono: true, detail: running.text,
        detailTone: running.tone === 'danger' ? 'danger' : 'warning',
        action: running.tone === 'warn'
          ? {label: 'Deployments', href: routeHref('deployments')} : null});
    }
    if (!framework) {
      return statusRow({tone: 'muted', name: 'django-mojo', value: installed,
        mono: true, detail: 'version status is unavailable'});
    }
    const pinned = framework.pin?.mode && framework.pin.mode !== 'latest';
    const pinDetail = pinned
      ? `pinned at ${framework.pin.value || framework.pin.mode}` : '';
    if (framework.can_update) {
      const row = statusRow({tone: 'warn', name: 'django-mojo', value: installed,
        mono: true, detail: pinDetail, detailTone: pinDetail ? 'warning' : '',
        action: {label: `Update to ${framework.latest}`, href: '#'}});
      const link = row.querySelector('.row-link');
      link?.addEventListener('click', (event) => {
        event.preventDefault();
        // Feature-scoped: features/webapps/api.js exports a different
        // applyFrameworkUpdate with its own guard, and INFLIGHT is global.
        runAction(null, () => applyFrameworkUpdate(), {key: 'platform:framework-update'});
      });
      return row;
    }
    // Never a dead control: when the update cannot happen the row says which
    // thing is in the way instead of offering a button that fails.
    const reason = BLOCKED_COPY[framework.blocked_reason] || '';
    const behind = framework.latest && framework.latest !== installed;
    return statusRow({
      tone: behind ? 'warn' : 'ok', name: 'django-mojo', value: installed, mono: true,
      detail: [pinDetail, behind ? `${framework.latest} available` : '', reason]
        .filter(Boolean).join(' · '),
      detailTone: behind ? 'warning' : ''});
  }

  // statusHeadline takes one sub line, so the mode note joins whatever else
  // the page had to say rather than displacing it.
  function sub(text) {
    return [text, managed ? '' : EXTERNAL_SUB].filter(Boolean).join(' ');
  }

  function headline() {
    if (report?.status === 'unavailable') {
      return statusHeadline({tone: 'muted',
        message: 'AWS is not answering, so pending upgrades are unknown.',
        sub: sub('No credentials are configured for this installation, or the endpoint is unreachable.'),
        onRefresh: () => load(true)});
    }
    const pending = (report?.findings || []).length;
    const urgent = (report?.findings || []).some(
      (row) => findingTone(row) === 'danger');
    return statusHeadline({
      tone: pending ? (urgent ? 'danger' : 'warn') : 'ok',
      message: pending
        ? `${pending} managed service${pending === 1 ? '' : 's'} can be upgraded`
        : 'No managed service is behind on a major version',
      sub: sub(report?.scheduled === false
        ? 'Scheduled drift scanning is off, so this page only reflects what it read just now.'
        : ''),
      observedAt: report?.generated_at,
      onRefresh: () => load(true)});
  }

  function render() {
    const database = findingsFor(DATABASE_KINDS);
    const cache = findingsFor(CACHE_KINDS);
    const quiet = report?.status === 'ok' && !database.length && !cache.length
      ? h('p', {class: 'maintenance-note',
        text: 'Every managed database and cache is on a currently supported major version.'})
      : null;
    body.replaceChildren(...[
      headline(),
      rowSection('Database', database.map(upgradeRow)),
      rowSection('Cache', cache.map(upgradeRow)),
      rowSection('Access', warningRows(report?.warnings)),
      quiet,
      rowSection('Framework', [frameworkRow()]),
    ].filter(Boolean));
    const focus = decodeRouteState().state.focus;
    const target = focus ? body.querySelector(`[data-focus="${CSS.escape(focus)}"]`) : null;
    if (target) requestAnimationFrame(() => target.focus({preventScroll: false}));
  }

  async function load(refresh = false) {
    if (!refresh) body.replaceChildren(loadingState('Loading…'));
    try {
      // The framework leg is independently permissioned, so a caller with
      // manage_aws but no platform read still gets the AWS half of the page.
      const [versions, overview] = await Promise.all([
        api(`${VERSIONS_PATH}${refresh ? '?refresh=1' : ''}`, {signal}),
        api(`${FRAMEWORK_PATH}${refresh ? '?refresh=1' : ''}`, {signal}).catch(() => null),
      ]);
      report = versions;
      framework = overview;
      render();
    } catch (error) {
      if (error?.name === 'AbortError') return;
      body.replaceChildren(errorState(error, () => load()));
    }
  }

  await load();
  return root;
}

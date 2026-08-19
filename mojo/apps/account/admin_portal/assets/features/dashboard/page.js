import {api, formatDate, h} from '../../core.js';
import {activityHref, routeHref} from '../../components/routes.js';
import {rowSection, statusHeadline, statusRow} from '../../components/rows.js';
import {featureDescriptors} from '../registry.js';
import {errorState, loadingState} from '../../components/views.js';

const HEADLINE_TONE = {ok: 'ok', down: 'danger', unknown: 'muted'};

function tone(source) {
  const status = source?.status || 'unknown';
  if (status === 'healthy') return 'ok';
  if (status === 'unhealthy') return 'danger';
  if (status === 'degraded') return 'warn';
  return 'muted';
}

// Two different denials read the same on the page: the operator's role cannot
// read the source, or the platform's AWS identity lacks the IAM grant.
function denied(source) {
  return source?.status === 'permission_denied'
    || Boolean(source?.reason_detail?.iam_action);
}

function restrictedRow(name, source) {
  const action = source?.reason_detail?.iam_action;
  return statusRow({tone: 'muted', name, value: 'Restricted',
    detail: action ? `needs ${action}` : ''});
}

function section(label, entries) {
  const rows = entries.filter((entry) => entry.source);
  if (rows.length && rows.every((entry) => denied(entry.source))) {
    const named = rows.find((entry) => entry.source.reason_detail?.iam_action);
    return rowSection(label, [restrictedRow('Restricted', (named || rows[0]).source)]);
  }
  return rowSection(label, rows.map((entry) => denied(entry.source)
    ? restrictedRow(entry.name, entry.source)
    : entry.build(entry.source)));
}

// The maintenance destination ships separately. Linking to a route the shell
// cannot resolve would send the operator to a silent fallback, so the link
// exists only once the feature is registered.
function maintenanceHref() {
  return featureDescriptors.some((feature) =>
    (feature.routes || []).includes('maintenance')) ? routeHref('maintenance') : null;
}

function loadBalancerRow(source) {
  const data = source.data || {};
  const ips = data.balancer?.elastic_ips || [];
  let value = `${data.healthy || 0} / ${data.registered || 0} targets healthy`;
  if (!data.configured) value = 'Not configured';
  else if (!data.registered) value = 'No registered targets';
  let detailNode = null;
  if (ips.length) detailNode = h('span', {class: 'row-detail mono', text: ips.join(', ')});
  else if (data.configured && data.elastic_ip_missing) {
    detailNode = h('span', {class: 'row-detail warning',
      text: 'no elastic IP — the address changes if it is replaced'});
  }
  return statusRow({tone: tone(source), name: 'Load balancer', value, detailNode});
}

function computeRow(source) {
  const data = source.data || {};
  const names = (data.instances || []).map((row) => row.name).filter(Boolean);
  const value = data.total ? `${data.up || 0} / ${data.total} up` : 'None running';
  return statusRow({tone: tone(source), name: 'EC2', value,
    detail: names.slice(0, 4).join(', ')});
}

function databaseRow(source) {
  const data = source.data || {};
  const engine = [data.engine, data.version].filter(Boolean).join(' ');
  const value = source.status === 'unhealthy' ? 'Unreachable'
    : ['Available', engine].filter(Boolean).join(' · ');
  const upgrade = data.drift?.available_major;
  if (!upgrade) return statusRow({tone: tone(source), name: 'RDS', value});
  const label = `${upgrade} available`;
  const href = maintenanceHref();
  return statusRow({tone: 'warn', name: 'RDS', value,
    action: href ? {label, href} : null,
    detail: href ? '' : label, detailTone: 'warning'});
}

function cacheRow(source) {
  const data = source.data || {};
  const engine = [data.engine, data.version].filter(Boolean).join(' ');
  const value = source.status === 'unhealthy' ? 'Unreachable'
    : ['Available', engine].filter(Boolean).join(' · ');
  return statusRow({tone: tone(source), name: 'Elasticache', value,
    detail: data.memory_used ? `${data.memory_used} used` : ''});
}

function certificatesRow(source, ctx) {
  const data = source.data || {};
  if (source.status === 'unconfigured' || !data.total) {
    return statusRow({tone: 'muted', name: 'SSL certs', value: 'Not managed here'});
  }
  const href = ctx.capabilities.network ? routeHref('certificates') : null;
  return statusRow({
    tone: tone(source), name: 'SSL certs',
    value: data.failing ? `${data.failing} not valid` : 'All valid',
    detail: data.soonest_renew ? `renews ${formatDate(data.soonest_renew)}` : '',
    action: href ? {label: 'Certificates', href} : null});
}

function publicApiRow(source, ctx) {
  const data = source.data || {};
  if (source.status === 'unconfigured' || !data.configured) {
    const href = ctx.capabilities.setup
      ? routeHref('setup', {focus: 'django.base_url', return: routeHref('dashboard')})
      : null;
    return statusRow({tone: 'muted', name: 'Public API', value: 'Not configured',
      action: href ? {label: 'Set it up', href} : null});
  }
  const probed = data.probe?.version || '';
  const node = data.node_version || '';
  if (source.status === 'unhealthy') {
    return statusRow({tone: 'danger', name: 'Public API',
      value: probed || 'Unreachable', mono: Boolean(probed),
      detail: 'the public address did not answer', detailTone: 'danger'});
  }
  const mismatch = Boolean(probed && node && probed !== node);
  return statusRow({tone: mismatch ? 'warn' : tone(source), name: 'Public API',
    value: probed || 'Reachable', mono: Boolean(probed),
    detail: mismatch ? `this node reports ${node}` : (probed && node ? 'up to date' : ''),
    detailTone: mismatch ? 'warning' : ''});
}

function frameworkRow(source) {
  const data = source.data || {};
  const installed = data.installed || '—';
  if (data.update_available && data.latest) {
    const label = `Update to ${data.latest}`;
    const href = maintenanceHref();
    return statusRow({tone: 'warn', name: 'django-mojo', value: installed, mono: true,
      action: href ? {label, href} : null,
      detail: href ? '' : label, detailTone: 'warning'});
  }
  const held = data.pin?.mode && data.pin.mode !== 'latest';
  const behind = Boolean(data.latest && data.latest !== installed);
  return statusRow({tone: tone(source), name: 'django-mojo', value: installed, mono: true,
    detail: held && behind ? `${data.latest} available · pinned` : ''});
}

function incidentsRow(source) {
  const data = source.data || {};
  const open = data.open || 0;
  const age = data.oldest_age_days;
  const value = open
    ? [`${open} open`, age == null ? null : `oldest ${age} day${age === 1 ? '' : 's'}`]
      .filter(Boolean).join(' · ')
    : 'None open';
  return statusRow({tone: open ? 'warn' : 'ok', name: 'Incidents', value,
    action: open ? {label: 'Review', href: activityHref('incidents')} : null});
}

function ticketsRow(source) {
  const open = source.data?.open || 0;
  return statusRow({tone: 'warn', name: 'Tickets', value: `${open} open`,
    action: {label: 'Review', href: activityHref('tickets')}});
}

function deploymentRow(source) {
  const item = (source.data?.items || [])[0];
  if (!item) return statusRow({tone: 'muted', name: 'Deployment', value: 'None recorded'});
  const when = formatDate(item.finished || item.created);
  return statusRow({tone: tone(source), name: 'Deployment',
    valueNode: [h('span', {class: 'mono', text: String(item.sha || '').slice(0, 7)}),
      ` · ${item.status} · ${when}`],
    action: {label: 'History', href: routeHref('deployments')}});
}

export async function dashboardPage(ctx) {
  const body = h('div', {class: 'row-page dashboard-body'}, loadingState('Loading…'));
  const root = h('div', {}, h('h1', {class: 'sr-only', text: 'Dashboard', tabindex: '-1'}), body);
  async function load(refresh = false) {
    body.replaceChildren(loadingState('Loading…'));
    try {
      const report = await api(`/api/account/admin/dashboard${refresh ? '?refresh=1' : ''}`);
      const sources = report.sources || {};
      const availability = report.availability || {};
      body.replaceChildren(
        statusHeadline({
          tone: HEADLINE_TONE[availability.state] || 'muted',
          message: availability.message || 'Status unknown — no source is reporting.',
          sub: report.attention?.message || '',
          observedAt: report.observed_at,
          onRefresh: () => load(true),
        }),
        section('Infrastructure', [
          {name: 'Load balancer', source: sources.load_balancer, build: loadBalancerRow},
          {name: 'EC2', source: sources.compute, build: computeRow},
          {name: 'RDS', source: sources.database, build: databaseRow},
          {name: 'Elasticache', source: sources.cache, build: cacheRow},
          {name: 'SSL certs', source: sources.certificates,
            build: (source) => certificatesRow(source, ctx)},
        ]),
        section('Software', [
          {name: 'Public API', source: sources.public_api,
            build: (source) => publicApiRow(source, ctx)},
          {name: 'django-mojo', source: sources.framework, build: frameworkRow},
        ]),
        section('Needs attention', [
          {name: 'Incidents', source: sources.incidents, build: incidentsRow},
          {name: 'Deployment', source: sources.last_deployment, build: deploymentRow},
          // A ticket queue at zero is not news; the row is absent, not green.
          {name: 'Tickets',
            source: sources.tickets?.data?.open ? sources.tickets : null,
            build: ticketsRow},
        ]));
    } catch (error) { body.replaceChildren(errorState(error, () => load())); }
  }
  await load(); return root;
}

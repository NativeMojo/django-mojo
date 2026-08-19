import {api, formatDate, h} from '../../core.js';
import {activityHref, routeHref} from '../../components/routes.js';
import {rowSection, statusHeadline, statusRow} from '../../components/rows.js';
import {featureDescriptors} from '../registry.js';
import {errorState, loadingState} from '../../components/views.js';
import {
  detailLink, openApiInspector, openFleetInspector, openSecurityInspector,
  openSourceInspector, wireAction,
} from './inspectors.js';

const HEADLINE_TONE = {ok: 'ok', down: 'danger', unknown: 'muted'};

// Plain words for a failing node check. The row never counts checks — "3 of 5
// passing" tells an operator to go count, not what is wrong.
const SANITY_COPY = {
  'django apps': 'the app registry did not load',
  'database': 'the database did not answer',
  'migrations': 'database migrations are not applied',
  'redis': 'Redis did not answer',
  'local request': 'this node did not answer its own API',
};

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

// One affordance rule for every drill-in: Details takes the row's action slot,
// unless the row already spends that slot on a cross-page link — then it rides
// in the detail slot instead. No row loses a destination it has today.
function detailsRow(spec, open) {
  if (spec.action) {
    return statusRow({...spec, detailNode: spec.detailNode || detailLink('Details', open)});
  }
  return wireAction(statusRow({...spec, action: {label: 'Details', href: '#'}}), open);
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
  return detailsRow({tone: tone(source), name: 'Load balancer', value, detailNode},
    () => openSourceInspector({
      title: 'Load balancer', source,
      facts: [
        ['Balancer', data.balancer?.name],
        ['Type', [data.balancer?.type, data.balancer?.scheme].filter(Boolean).join(' · ')],
        ['State', data.balancer?.state],
        ['Targets', `${data.healthy || 0} of ${data.registered || 0} healthy`],
        ['Elastic IPs', ips.join(', ') || 'none allocated'],
      ],
    }));
}

function computeRow(source, ctx) {
  const data = source.data || {};
  const names = (data.instances || []).map((row) => row.name).filter(Boolean);
  const value = data.total ? `${data.up || 0} / ${data.total} up` : 'None running';
  return detailsRow(
    {tone: tone(source), name: 'EC2', value, detail: names.slice(0, 4).join(', ')},
    () => openFleetInspector(ctx, source));
}

function databaseFacts(data) {
  return [
    ['Engine', [data.engine, data.version].filter(Boolean).join(' ')],
    ['Vendor', data.vendor],
    ['Reachable', data.reachable ? 'yes' : 'no'],
    ['Upgrade available', data.drift?.available_major],
    ['Upgrade deadline', data.drift?.deadline ? formatDate(data.drift.deadline) : null],
    ['Note', data.drift?.note],
  ];
}

function databaseRow(source) {
  const data = source.data || {};
  const engine = [data.engine, data.version].filter(Boolean).join(' ');
  const value = source.status === 'unhealthy' ? 'Unreachable'
    : ['Available', engine].filter(Boolean).join(' · ');
  const open = () => openSourceInspector({
    title: 'RDS', source, facts: databaseFacts(data)});
  const upgrade = data.drift?.available_major;
  if (!upgrade) return detailsRow({tone: tone(source), name: 'RDS', value}, open);
  const label = `${upgrade} available`;
  const href = maintenanceHref();
  return detailsRow({tone: 'warn', name: 'RDS', value,
    action: href ? {label, href} : null,
    detail: href ? '' : label, detailTone: 'warning'}, open);
}

function cacheRow(source) {
  const data = source.data || {};
  const engine = [data.engine, data.version].filter(Boolean).join(' ');
  const value = source.status === 'unhealthy' ? 'Unreachable'
    : ['Available', engine].filter(Boolean).join(' · ');
  return detailsRow({tone: tone(source), name: 'Elasticache', value,
    detail: data.memory_used ? `${data.memory_used} used` : ''},
  () => openSourceInspector({title: 'Elasticache', source, facts: [
    ['Engine', [data.engine, data.version].filter(Boolean).join(' ')],
    ['Reachable', data.reachable ? 'yes' : 'no'],
    ['Memory used', data.memory_used],
  ]}));
}

function certificatesRow(source, ctx) {
  const data = source.data || {};
  // The renewal date moved into the drill-in: the row's detail slot now
  // carries Details, and a date nobody has to act on is not a headline.
  const open = () => openSourceInspector({title: 'SSL certificates', source, facts: [
    ['Managed certificates', data.total],
    ['Active', data.active],
    ['Not valid', data.failing],
    ['Expiring within 30 days', data.expiring_within_30_days],
    ['Next renewal', data.soonest_renew
      ? `${formatDate(data.soonest_renew)}${data.soonest_renew_name ? ` · ${data.soonest_renew_name}` : ''}`
      : null],
    ['Soonest expiry', data.soonest_expiry ? formatDate(data.soonest_expiry) : null],
  ]});
  if (source.status === 'unconfigured' || !data.total) {
    return detailsRow({tone: 'muted', name: 'SSL certs', value: 'Not managed here'}, open);
  }
  const href = ctx.capabilities.network ? routeHref('certificates') : null;
  return detailsRow({
    tone: tone(source), name: 'SSL certs',
    value: data.failing ? `${data.failing} not valid` : 'All valid',
    action: href ? {label: 'Certificates', href} : null}, open);
}

// The first failing node check, in plain words. Never a count: the row says
// what is broken, and the drill-in lists every check.
function sanityFailure(sanitySource) {
  const checks = sanitySource?.data?.checks;
  if (!Array.isArray(checks) || !checks.length) return null;
  const failing = checks.filter((row) => !row.ok);
  if (!failing.length) return null;
  const first = SANITY_COPY[failing[0].name] || failing[0].name;
  return failing.length > 1 ? `${first} · +${failing.length - 1} more` : first;
}

function publicApiRow(source, ctx, sanitySource) {
  const data = source.data || {};
  const open = () => openApiInspector(source, sanitySource);
  if (source.status === 'unconfigured' || !data.configured) {
    const href = ctx.capabilities.setup
      ? routeHref('setup', {focus: 'django.base_url', return: routeHref('dashboard')})
      : null;
    return detailsRow({tone: 'muted', name: 'Public API', value: 'Not configured',
      action: href ? {label: 'Set it up', href} : null}, open);
  }
  const probed = data.probe?.version || '';
  const node = data.node_version || '';
  if (source.status === 'unhealthy') {
    return detailsRow({tone: 'danger', name: 'Public API',
      value: probed || 'Unreachable', mono: Boolean(probed),
      detail: 'the public address did not answer', detailTone: 'danger'}, open);
  }
  // A node that cannot pass its own checks is the more urgent fact, so it
  // takes the detail slot from the version comparison.
  const failure = sanityFailure(sanitySource);
  const mismatch = Boolean(probed && node && probed !== node);
  const detail = failure || (mismatch ? `this node reports ${node}`
    : (probed && node ? 'up to date' : ''));
  return detailsRow({tone: failure || mismatch ? 'warn' : tone(source), name: 'Public API',
    value: probed || 'Reachable', mono: Boolean(probed), detail,
    detailTone: failure || mismatch ? 'warning' : ''}, open);
}

function jobsRow(source) {
  const data = source.data || {};
  const counts = data.jobs || {};
  const open = () => openSourceInspector({title: 'Jobs', source, facts: [
    ['Scheduler', data.scheduler_active ? 'running' : 'not running'],
    ['Pending', counts.pending],
    ['Running', counts.running],
    ['Failed in the last hour', data.failed_recent],
    ['Failed all time', counts.failed],
  ]});
  if (!data.jobs) {
    // No counts came back — a timed-out or failed collector. Zero pending jobs
    // is a green queue; missing evidence is not, and must not read like one.
    return detailsRow({tone: 'muted', name: 'Jobs', value: 'No evidence',
      detail: String(source.reason || source.status || '').replaceAll('_', ' ')}, open);
  }
  const value = `${counts.pending || 0} pending · ${counts.running || 0} running`;
  if (!data.scheduler_active) {
    return detailsRow({tone: 'warn', name: 'Jobs', value,
      detail: 'scheduler is not running', detailTone: 'warning'}, open);
  }
  if (data.failed_recent) {
    return detailsRow({tone: 'warn', name: 'Jobs', value,
      detail: `${data.failed_recent} failed in the last hour`, detailTone: 'warning'}, open);
  }
  return detailsRow({tone: 'ok', name: 'Jobs', value, detail: 'scheduler active'}, open);
}

function frameworkRow(source) {
  const data = source.data || {};
  const installed = data.installed || '—';
  const open = () => openSourceInspector({title: 'django-mojo', source, facts: [
    ['Installed', installed, true],
    ['Latest published', data.latest, true],
    ['Checked', data.checked_at ? formatDate(data.checked_at) : null],
    ['Source', data.source],
    ['Update policy', data.pin?.mode],
  ]});
  if (data.update_available && data.latest) {
    const label = `Update to ${data.latest}`;
    const href = maintenanceHref();
    return detailsRow({tone: 'warn', name: 'django-mojo', value: installed, mono: true,
      action: href ? {label, href} : null,
      detail: href ? '' : label, detailTone: 'warning'}, open);
  }
  const held = data.pin?.mode && data.pin.mode !== 'latest';
  const behind = Boolean(data.latest && data.latest !== installed);
  return detailsRow({tone: tone(source), name: 'django-mojo', value: installed, mono: true,
    detail: held && behind ? `${data.latest} available · pinned` : ''}, open);
}

const SMS_PROVIDER_LABELS = {twilio: 'Twilio', aws: 'AWS SNS', mojo: 'Mojo remote'};

function smsRow(source) {
  const data = source.data || {};
  const provider = SMS_PROVIDER_LABELS[data.provider] || data.provider || 'Unknown';
  const value = `${provider} · ${data.is_active ? 'Active' : 'Inactive'}`;
  const verified = data.verified;
  let detail = '';
  let detailNode = null;
  if (verified && verified.ok === false) {
    detailNode = h('span', {class: 'row-detail warning',
      text: verified.message || 'last connection test failed'});
  } else if (data.test_mode) {
    detail = 'test mode — real messages still send';
  } else if (verified && verified.ok === true) {
    detail = 'connection verified';
  }
  return statusRow({tone: tone(source), name: 'Text messages', value, detail,
    detailNode, action: {label: 'Manage', href: routeHref('messaging-sms')}});
}

function emailRow(source) {
  const data = source.data || {};
  const posture = data.posture || {};
  const total = data.domains || 0;
  const value = `${data.sendable_domains || 0} of ${total} domain${total === 1 ? '' : 's'} ready`;
  let detail = '';
  let detailNode = null;
  if (posture.default_sender_conflict) {
    detailNode = h('span', {class: 'row-detail warning',
      text: 'more than one mailbox claims the system default'});
  } else if (data.default_sender) {
    detail = `sends as ${data.default_sender}`;
  } else {
    detailNode = h('span', {class: 'row-detail warning', text: 'no default sender'});
  }
  return statusRow({tone: tone(source), name: 'Email', value, detail, detailNode,
    action: {label: 'Manage', href: routeHref('messaging-email')}});
}

function incidentsRow(source, ctx) {
  const data = source.data || {};
  const open = data.open || 0;
  const age = data.oldest_age_days;
  const value = open
    ? [`${open} open`, age == null ? null : `oldest ${age} day${age === 1 ? '' : 's'}`]
      .filter(Boolean).join(' · ')
    : 'None open';
  // Security posture is its own permission tier and never rides in the
  // Dashboard payload — the drill-in fetches it, and only for callers the
  // platform-security tier would accept.
  const security = ctx.features?.platform?.capabilities?.security === true;
  return statusRow({tone: open ? 'warn' : 'ok', name: 'Incidents', value,
    detailNode: security ? detailLink('Details', () => openSecurityInspector(ctx)) : null,
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
          {name: 'EC2', source: sources.compute,
            build: (source) => computeRow(source, ctx)},
          {name: 'RDS', source: sources.database, build: databaseRow},
          {name: 'Elasticache', source: sources.cache, build: cacheRow},
          {name: 'SSL certs', source: sources.certificates,
            build: (source) => certificatesRow(source, ctx)},
          {name: 'Jobs', source: sources.jobs, build: jobsRow},
        ]),
        section('Software', [
          {name: 'Public API', source: sources.public_api,
            build: (source) => publicApiRow(source, ctx, sources.sanity)},
          {name: 'django-mojo', source: sources.framework, build: frameworkRow},
          // Absent, not red, until a system SMS provider actually exists.
          {name: 'Text messages',
            source: sources.sms?.data?.configured ? sources.sms : null,
            build: smsRow},
          // Same rule for email: no SES domain means no row, never a red one.
          {name: 'Email',
            source: sources.email?.data?.configured ? sources.email : null,
            build: emailRow},
        ]),
        section('Needs attention', [
          {name: 'Incidents', source: sources.incidents,
            build: (source) => incidentsRow(source, ctx)},
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

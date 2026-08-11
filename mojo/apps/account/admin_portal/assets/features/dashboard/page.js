import {api, badge, formatDate, h, icon, pageHeader} from '../../core.js';
import {routeHref} from '../../components/routes.js';
import {errorState, loadingState} from '../../components/views.js';

const LABELS = {
  public_api: 'Public API', fleet: 'Edge fleet', webapps: 'Web Apps',
  security: 'Detection', last_deployment: 'Last deployment',
  incidents: 'Open incidents', tickets: 'Open tickets',
};
const LINKS = {
  public_api: ['setup', 'Configure API'], fleet: ['platform', 'View fleet'],
  webapps: ['webapps', 'Open Web Apps'], security: ['activity', 'Open Activity'],
  last_deployment: ['deployments', 'Open deployments'],
  incidents: ['activity', 'Open incidents'], tickets: ['activity', 'Open tickets'],
};
const COPY = {
  healthy: 'Current evidence is healthy.',
  unhealthy: 'Current evidence proves a failure. Open the source and remediate it.',
  degraded: 'The source is reachable but needs operator attention.',
  stale: 'The last observation is too old to establish current health.',
  unconfigured: 'This source has no configured target or durable record yet.',
  unknown: 'The source did not return authoritative evidence. No health is inferred.',
  permission_denied: 'Your role cannot read this source. No request was issued for it.',
};

function tone(status) {
  if (status === 'healthy') return 'success';
  if (status === 'unhealthy') return 'danger';
  if (['degraded', 'stale', 'unconfigured'].includes(status)) return 'warning';
  return 'neutral';
}

function sourceValue(name, source) {
  const data = source.data || {};
  if (source.status === 'permission_denied') return 'Restricted';
  if (source.status === 'unknown') return 'Unknown';
  if (name === 'public_api') return data.probe?.version ? `v${data.probe.version}` : (data.configured ? 'Observed' : 'Not configured');
  if (name === 'fleet') return `${data.runners?.length || 0} runner${data.runners?.length === 1 ? '' : 's'}`;
  if (name === 'webapps') return `${data.count || 0} app${data.count === 1 ? '' : 's'}`;
  if (name === 'security') return `${data.open_incidents?.count ?? '—'} open`;
  if (name === 'last_deployment') return data.items?.[0]?.sha?.slice(0, 10) || 'None';
  if (name === 'incidents' || name === 'tickets') return String(data.open ?? '—');
  return '—';
}

function sourceLink(name) {
  const [route, label] = LINKS[name];
  const state = name === 'public_api' ? {focus: 'django.base_url', return: routeHref('dashboard')}
    : name === 'fleet' ? {focus: 'fleet', return: routeHref('dashboard')}
    : name === 'incidents' ? {tab: 'incidents', return: routeHref('dashboard')}
    : name === 'tickets' ? {tab: 'tickets', return: routeHref('dashboard')} : {};
  return h('a', {class: 'button ghost compact', href: routeHref(route, state)}, label);
}

function sourceCard(name, source) {
  const status = source.status || 'unknown';
  return h('article', {class: `dashboard-source ${tone(status)}`, 'data-source': name},
    h('header', {}, h('span', {text: LABELS[name]}), badge(status.replaceAll('_', ' '), tone(status))),
    h('strong', {text: sourceValue(name, source)}),
    h('p', {text: COPY[status] || COPY.unknown}),
    source.observed_at ? h('small', {text: `Observed ${formatDate(source.observed_at)}`}) : null,
    status !== 'permission_denied' ? sourceLink(name) : null);
}

function setupLink(ctx, report) {
  if (!ctx.capabilities.setup) return null;
  const needsSetup = Object.values(report.sources || {}).some((source) =>
    ['unhealthy', 'degraded', 'stale', 'unconfigured'].includes(source.status));
  return h('a', {class: `button ${needsSetup ? 'primary' : 'ghost'}`, href: routeHref('setup')},
    icon('settings'), needsSetup ? 'Open System Setup' : 'Review System Setup');
}

export async function dashboardPage(ctx) {
  const body = h('div', {class: 'dashboard-body'}, loadingState('Loading observable system evidence…'));
  const root = h('div', {class: 'page'},
    pageHeader('Control plane', 'Dashboard', 'Can customers use the system, and can we detect failures?'), body);
  async function load() {
    body.replaceChildren(loadingState('Loading observable system evidence…'));
    try {
      const report = await api('/api/account/admin/dashboard');
      const sources = report.sources || {};
      const environment = ['public_api', 'fleet', 'webapps', 'security'];
      const attention = ['incidents', 'tickets'];
      body.replaceChildren(
        h('section', {class: `dashboard-answer ${tone(report.overall)}`},
          h('div', {}, h('span', {text: 'Customer readiness'}), h('strong', {text: String(report.overall || 'unknown').replaceAll('_', ' ')}),
            h('p', {text: `${report.observable_sources || 0} independently permissioned source(s) contributed. Denied and unknown sources never become green.`})),
          setupLink(ctx, report)),
        h('section', {class: 'dashboard-section'},
          h('div', {class: 'section-title'}, h('h2', {text: 'Environment'}), h('p', {text: 'Four answers, each from its own authority.'})),
          h('div', {class: 'dashboard-grid'}, ...environment.map((name) => sourceCard(name, sources[name] || {status: 'unknown'})))),
        h('section', {class: 'dashboard-section dashboard-lower'},
          h('div', {class: 'attention-panel'}, h('div', {class: 'section-title'}, h('h2', {text: 'Open attention'}), h('p', {text: 'Actionable work, not an all-time event count.'})),
            h('div', {class: 'attention-grid'}, ...attention.map((name) => sourceCard(name, sources[name] || {status: 'unknown'})))),
          h('div', {class: 'deployment-panel'}, h('div', {class: 'section-title'}, h('h2', {text: 'Deployment'}), h('p', {text: 'Most recent durable attempt.'})),
            sourceCard('last_deployment', sources.last_deployment || {status: 'unknown'}))));
    } catch (error) { body.replaceChildren(errorState(error, load)); }
  }
  await load(); return root;
}

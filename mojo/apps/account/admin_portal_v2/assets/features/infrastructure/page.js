// Infrastructure — one destination, three tabs.
//
// v1 spent three sidebar entries on Fleet Scaling, Metrics and Maintenance.
// They are the same subject read three ways, so here they are tabs of one
// destination and the sidebar keeps its six entries.
//
// The tab is route state (`#/infrastructure?tab=metrics`), so every tab is
// linkable, survives a reload, and lands in the browser's history exactly like
// a page. A caller who lacks a tab's capability never sees it, and a link to a
// tab they cannot read falls back to the first one they can — the URL is
// rewritten to say so rather than showing one tab while claiming another.
import {h} from '../../core.js';
import {decodeRouteState, routeHref} from '../../components/routes.js';
import {permissionDeniedState, sectionTabs} from '../../components/views.js';
import {capacityTab} from './capacity.js';
import {metricsTab} from './metrics.js';
import {maintenanceTab} from './maintenance.js';

// Each tab names the ONE platform capability the bootstrap payload has to
// report before it is offered. The names are v1's, unchanged.
const TABS = [
  {
    id: 'capacity', label: 'Capacity', capability: 'capacity',
    copy: 'What you run, what it costs to change, and what every member is '
      + 'doing right now.',
    render: capacityTab,
  },
  {
    id: 'metrics', label: 'Metrics', capability: 'metrics',
    copy: 'CloudWatch time series for the EC2, RDS and ElastiCache resources '
      + 'this installation can see.',
    render: metricsTab,
  },
  {
    id: 'maintenance', label: 'Maintenance', capability: 'maintenance',
    copy: 'Pending managed-service upgrades and the framework this '
      + 'installation runs, with the controls to apply them.',
    render: maintenanceTab,
  },
];

/** The tabs this caller may actually read, in display order. */
export function visibleTabs(ctx) {
  const capabilities = ctx.features?.platform?.capabilities || {};
  return TABS.filter((tab) => capabilities[tab.capability] === true);
}

function activeTab(ctx, requested) {
  const tabs = visibleTabs(ctx);
  return tabs.find((tab) => tab.id === requested) || tabs[0] || null;
}

export async function infrastructurePage({ctx, navigate, signal = null}) {
  const tabs = visibleTabs(ctx);
  const requested = decodeRouteState().state.tab || '';
  const tab = activeTab(ctx, requested);
  if (!tab) {
    return permissionDeniedState(
      'Infrastructure requires the manage_aws permission on this installation.');
  }
  // A hash that named a tab this caller cannot read is corrected in place, so
  // the address bar and the screen never disagree.
  if (requested && requested !== tab.id) {
    history.replaceState({}, '', routeHref('infrastructure', {tab: tab.id}));
  }

  // Filled by the tab with the one control that has to survive its own body
  // reload — the Refresh button. The tab bar and the header are ours.
  const actions = h('div', {class: 'page-actions'});
  const bodyHost = h('div', {class: 'infrastructure-body'});
  const root = h('div', {class: 'page infrastructure-page'},
    h('header', {class: 'page-header'},
      h('div', {},
        h('div', {class: 'eyebrow', text: 'Infrastructure'}),
        h('h1', {text: tab.label, tabindex: '-1'}),
        h('p', {text: tab.copy})),
      actions),
    tabs.length > 1
      ? sectionTabs({
        items: tabs.map(({id, label}) => ({id, label})),
        active: tab.id,
        label: 'Infrastructure sections',
        onChange: (id) => navigate('infrastructure', {state: {tab: id}}),
      })
      : null,
    bodyHost);

  const body = await tab.render(ctx, signal, actions);
  bodyHost.replaceChildren(body);
  root.dispose = () => body.dispose?.();
  return root;
}

// Home — the scaffold version.
//
// Section 1 builds the shell, not the truthful Home screen: the worst-of
// headline, blockers, running operations and at-a-glance tiles land in section
// 2, fed by the same endpoints v1's Dashboard reads. Until then this page says
// only what it can actually prove — which destinations this caller can open,
// and that v1 is still there and still complete.

import {h, icon, pageHeader} from '../../core.js';
import {navigationFor} from '../registry.js';

function destinationRow(entry) {
  return h('a', {class: 'status-row is-navigable', href: `#/${entry.route}`},
    icon(entry.icon),
    h('strong', {text: entry.label}),
    h('span', {class: 'status-value', text: entry.summary || ''}),
    h('span', {class: 'row-link', text: 'Open'}));
}

const SUMMARIES = {
  home: 'You are here',
  apps: 'Applications and their deploys',
  infrastructure: 'Fleet capacity, metrics, maintenance',
  domains: 'Domains, DNS, certificates',
  access: 'People, groups, keys',
  settings: 'Installation settings and integrations',
};

export function homePage(ctx) {
  const adminPath = ctx.admin_path || '/admin/';
  const destinations = navigationFor(ctx).map(
    (entry) => ({...entry, summary: SUMMARIES[entry.route] || ''}));

  return h('div', {class: 'page'},
    pageHeader('Admin v2', 'Home',
      'The v2 portal is being built one destination at a time. Everything the '
      + 'current Admin does, it still does.'),

    h('section', {class: 'panel'},
      h('div', {class: 'panel-head'},
        h('h2', {text: 'Destinations'}),
        h('span', {class: 'badge accent', text: `${destinations.length} open to you`})),
      ...destinations.map(destinationRow)),

    h('section', {class: 'panel'},
      h('div', {class: 'panel-head'}, h('h2', {text: 'What is built here so far'})),
      h('div', {class: 'panel-body'},
        h('p', {text:
          'This Home page is the scaffold: it lists the destinations you can '
          + 'open and nothing more. The truthful Home — worst-of headline, '
          + 'blockers with their fixes, running operations, recent activity — '
          + 'arrives with the next section.'}),
        h('p', {class: 'callout', text:
          `The full v1 portal remains at ${adminPath}. Nothing has been removed `
          + 'or moved; v2 sits beside it until every section here is finished.'}),
        h('div', {},
          h('a', {class: 'button', href: adminPath}, 'Open the current Admin')))));
}

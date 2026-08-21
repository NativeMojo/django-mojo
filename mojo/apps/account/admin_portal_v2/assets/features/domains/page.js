// Domains — one destination, four views.
//
// v1 spent one sidebar entry ("Domains & DNS") on seven routes, four of which
// had no navigation at all: #/certificates and #/credentials were reachable
// only by typing the URL or following a link from a domain page.
//
// Here all four are tabs of one destination — but each keeps its OWN route
// (`#/domains`, `#/dns`, `#/certificates`, `#/credentials`), so every v1 link,
// bookmark and in-page cross-reference still lands exactly where it did, and
// the sidebar entry stays lit for all four through its `matches` list.
//
// The remaining three v1 routes — vhosts, routes, upstreams — are the operator
// half of an app's Serving tab, so they moved to Apps ▸ Serving (advanced).
import {h} from '../../core.js';
import {sectionTabs} from '../../components/views.js';
import {ensureGroupChoices} from '../../components/network.js';
import {domainsTab} from './domains.js';
import {dnsTab} from './dns.js';
import {certificatesTab} from './certificates.js';
import {credentialsTab} from './credentials.js';

// Each tab IS a route. `label` is what the tab bar and the topbar say; `copy`
// is the header sentence under it.
export const TABS = [
  {
    id: 'domains', label: 'Domains',
    copy: 'The names this installation controls, the trust behind them, and who '
      + 'may send as them.',
    render: domainsTab,
  },
  {
    id: 'dns', label: 'DNS records',
    copy: 'Edit the live provider zone as complete record sets; no database '
      + 'mirror can drift.',
    render: dnsTab,
  },
  {
    id: 'certificates', label: 'Certificates',
    copy: 'Issue and monitor TLS certificates without exposing private key '
      + 'material.',
    render: certificatesTab,
  },
  {
    id: 'credentials', label: 'Credentials',
    copy: 'Verified provider access without exposing stored secrets.',
    render: credentialsTab,
  },
];

export function tabFor(route) {
  return TABS.find((tab) => tab.id === route) || TABS[0];
}

export async function domainsPage(ctx, route, navigate) {
  // v1 loaded the group choices once per session before any of these pages
  // rendered; the dialogs on three of the four tabs need them.
  await ensureGroupChoices(ctx);
  const tab = tabFor(route);

  // Filled by the tab with the controls that have to survive its own body
  // reloads — Add record, Request certificate, Buy domain. The tab bar and the
  // header are ours.
  const actions = h('div', {class: 'page-actions'});
  const bodyHost = h('div', {class: 'domains-body'});
  const root = h('div', {class: 'page domains-page'},
    h('header', {class: 'page-header'},
      h('div', {},
        h('div', {class: 'eyebrow', text: 'Domains'}),
        h('h1', {text: tab.label, tabindex: '-1'}),
        h('p', {text: tab.copy})),
      actions),
    sectionTabs({
      items: TABS.map(({id, label}) => ({id, label})),
      active: tab.id,
      label: 'Domains sections',
      // Each tab is its own route, so this is a plain navigation — the browser
      // Back button walks the tabs exactly like pages.
      onChange: (id) => navigate(id),
    }),
    bodyHost);

  const body = await tab.render(ctx, actions);
  bodyHost.replaceChildren(body);
  root.dispose = () => body.dispose?.();
  return root;
}

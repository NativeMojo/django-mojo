import {domainsPage, tabFor} from './page.js';

// Domains — v1's "Domains & DNS" entry, plus the two routes v1 registered but
// never navigated to (Certificates and Credentials).
//
// Four routes, one destination, one sidebar entry. Each tab keeps its own v1
// route name so links and bookmarks land; `matches` keeps the entry lit for all
// four.
//
// The gates are v1's, unchanged: the feature opens on the `advanced` block, and
// the navigation entry appears for `network` OR `manage_network` — exactly the
// condition v1 put on its "Domains & DNS" entry. v1's other entry, Serving
// (manage_network alone), is now Apps ▸ Serving.
export default {
  id: 'domains',
  routes: ['domains', 'dns', 'certificates', 'credentials'],
  style: 'assets/features/domains/styles.css',
  enabled: (ctx) => ctx.features?.advanced?.enabled === true,
  navigation: (ctx) => (ctx.capabilities.network || ctx.capabilities.manage_network
    ? [{
      route: 'domains', label: 'Domains', icon: 'globe',
      matches: ['domains', 'dns', 'certificates', 'credentials'],
    }]
    : []),
  title: (route) => tabFor(route).label,
  render: ({ctx, route, navigate}) => domainsPage(ctx, route, navigate),
};

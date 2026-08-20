import {networkPage} from './page.js';

// The raw-evidence 'advanced' route is gone: its three sections are read
// through the Dashboard rows and their drill-ins now. advanced_overview()
// and GET /api/account/admin/advanced are unchanged — this feature simply
// stopped being their only reader.
const ROUTES = ['domains', 'credentials', 'dns', 'certificates', 'upstreams', 'vhosts', 'routes'];
const LABELS = {domains: 'Domains', credentials: 'Credentials', dns: 'DNS records', certificates: 'Certificates', upstreams: 'Upstreams', vhosts: 'Serving', routes: 'Routes'};
const ICONS = {domains: 'globe', credentials: 'key', dns: 'dns', certificates: 'certificate', upstreams: 'server', vhosts: 'deploy', routes: 'route'};

export default {
  id: 'advanced', routes: ROUTES, style: 'assets/features/advanced/styles.css',
  enabled: (ctx) => ctx.features?.advanced?.enabled === true,
  // Serving is the operator half of an app's Serving tab: the same rows, one
  // fleet at a time instead of one app at a time. It had no navigation entry
  // at all until #2229 — #/vhosts and #/routes were reachable only by typing
  // the URL. Gated on manage_network ALONE: these pages create, retire and
  // repoint serving rows across every tenant, which a read grant must not see.
  navigation: (ctx) => [
    ctx.capabilities.network || ctx.capabilities.manage_network ? {
      route: 'domains', matches: ['domains', 'dns'], label: 'Domains & DNS', icon: 'globe', section: 'Control plane',
    } : null,
    ctx.capabilities.manage_network ? {
      route: 'vhosts', matches: ['vhosts', 'routes', 'upstreams'], label: 'Serving', icon: 'route', section: 'Control plane',
    } : null,
  ].filter(Boolean),
  title: (route) => LABELS[route] || 'Advanced',
  render: ({ctx, route}) => networkPage(ctx, route),
};

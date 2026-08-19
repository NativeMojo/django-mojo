import {networkPage} from './page.js';

// The raw-evidence 'advanced' route is gone: its three sections are read
// through the Dashboard rows and their drill-ins now. advanced_overview()
// and GET /api/account/admin/advanced are unchanged — this feature simply
// stopped being their only reader.
const ROUTES = ['domains', 'credentials', 'dns', 'certificates', 'upstreams', 'vhosts', 'routes'];
const LABELS = {domains: 'Domains', credentials: 'Credentials', dns: 'DNS records', certificates: 'Certificates', upstreams: 'Upstreams', vhosts: 'Vhosts', routes: 'Routes'};
const ICONS = {domains: 'globe', credentials: 'key', dns: 'dns', certificates: 'certificate', upstreams: 'server', vhosts: 'deploy', routes: 'route'};

export default {
  id: 'advanced', routes: ROUTES, style: 'assets/features/advanced/styles.css',
  enabled: (ctx) => ctx.features?.advanced?.enabled === true,
  navigation: (ctx) => ctx.capabilities.network || ctx.capabilities.manage_network ? [{
    route: 'domains', matches: ['domains', 'dns'], label: 'Domains & DNS', icon: 'globe', section: 'Control plane',
  }] : [],
  title: (route) => LABELS[route] || 'Advanced',
  render: ({ctx, route}) => networkPage(ctx, route),
};

import {advancedControlPage, networkPage} from './page.js';

const ROUTES = ['advanced', 'domains', 'credentials', 'dns', 'certificates', 'upstreams', 'vhosts', 'routes'];
const LABELS = {advanced: 'Advanced', domains: 'Domains', credentials: 'Credentials', dns: 'DNS records', certificates: 'Certificates', upstreams: 'Upstreams', vhosts: 'Vhosts', routes: 'Routes'};
const ICONS = {advanced: 'settings', domains: 'globe', credentials: 'key', dns: 'dns', certificates: 'certificate', upstreams: 'server', vhosts: 'deploy', routes: 'route'};

export default {
  id: 'advanced', routes: ROUTES, style: 'assets/features/advanced/styles.css',
  enabled: (ctx) => ctx.features?.advanced?.enabled === true,
  navigation: () => [],
  title: (route) => LABELS[route] || 'Advanced',
  render: ({ctx, route}) => route === 'advanced' ? advancedControlPage(ctx) : networkPage(ctx, route),
};

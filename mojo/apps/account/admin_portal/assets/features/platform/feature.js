import {networkPage} from './page.js';

const ROUTES = ['domains', 'credentials', 'dns', 'certificates', 'upstreams', 'vhosts', 'routes'];
const LABELS = {domains: 'Domains', credentials: 'Credentials', dns: 'DNS records', certificates: 'Certificates', upstreams: 'Upstreams', vhosts: 'Vhosts', routes: 'Routes'};
const ICONS = {domains: 'globe', credentials: 'key', dns: 'dns', certificates: 'certificate', upstreams: 'server', vhosts: 'deploy', routes: 'route'};

export default {
  id: 'platform', routes: ROUTES, style: 'assets/features/platform/styles.css',
  enabled: (ctx) => ctx.features?.platform?.enabled === true,
  navigation: () => ROUTES.map((route) => ({route, label: LABELS[route], icon: ICONS[route], section: 'Network & hosting'})),
  title: (route) => LABELS[route] || 'Platform',
  render: ({ctx, route}) => networkPage(ctx, route),
};

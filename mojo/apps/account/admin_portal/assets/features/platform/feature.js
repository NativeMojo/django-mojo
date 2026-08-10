import {platformPage, setupPage} from './page.js';

export default {
  id: 'platform', routes: ['platform', 'deployments', 'setup'], style: 'assets/features/platform/styles.css',
  enabled: (ctx) => ctx.features?.platform?.enabled === true,
  navigation: () => [
    {route: 'platform', label: 'Platform', icon: 'server', section: 'Control plane'},
    {route: 'deployments', label: 'Deployments', icon: 'deploy', section: 'Control plane'},
    {route: 'setup', label: 'System Setup', icon: 'settings', section: 'Control plane'},
  ],
  title: (route) => ({platform: 'Platform', deployments: 'Deployments', setup: 'System Setup'}[route] || 'Platform'),
  render: ({ctx, route}) => route === 'setup' ? setupPage(ctx) : platformPage(ctx, route),
};

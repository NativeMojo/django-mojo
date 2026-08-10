import {platformPage, setupPage} from './page.js';
import {permissionDeniedState} from '../../components/views.js';

export default {
  id: 'platform', routes: ['platform', 'deployments', 'setup'], style: 'assets/features/platform/styles.css',
  enabled: (ctx) => ctx.features?.platform?.enabled === true,
  navigation: () => [{route: 'platform', matches: ['platform', 'deployments', 'setup'], label: 'Platform', icon: 'server', section: 'Control plane'}],
  title: (route) => ({platform: 'Platform', deployments: 'Deployments', setup: 'System Setup'}[route] || 'Platform'),
  render: ({ctx, route}) => route === 'setup'
    ? (ctx.capabilities.setup ? setupPage(ctx) : permissionDeniedState('System Setup requires an active literal superuser.'))
    : platformPage(ctx, route),
};

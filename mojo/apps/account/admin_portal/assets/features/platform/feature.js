import {platformPage, setupPage} from './page.js';
import {metricsPage} from './metrics.js';
import {permissionDeniedState} from '../../components/views.js';

export default {
  id: 'platform', routes: ['platform', 'deployments', 'setup', 'metrics'], style: 'assets/features/platform/styles.css',
  enabled: (ctx) => ctx.features?.platform?.enabled === true,
  navigation: (ctx) => {
    const capabilities = ctx.features?.platform?.capabilities || {};
    const entries = [];
    if (capabilities.view || capabilities.manage || capabilities.setup ||
        capabilities.security || capabilities.advanced) {
      entries.push({route: 'platform', matches: ['platform', 'deployments', 'setup'], label: 'Platform', icon: 'server', section: 'Control plane'});
    }
    if (capabilities.metrics) {
      entries.push({route: 'metrics', label: 'Metrics', icon: 'chart', section: 'Control plane'});
    }
    return entries;
  },
  title: (route) => ({platform: 'Platform', deployments: 'Deployments', setup: 'System Setup', metrics: 'Metrics'}[route] || 'Platform'),
  render: ({ctx, route, signal}) => route === 'metrics'
    ? metricsPage(ctx, signal)
    : route === 'setup'
      ? (ctx.capabilities.setup ? setupPage(ctx, signal) : permissionDeniedState('System Setup requires an active literal superuser.'))
      : platformPage(ctx, route),
};

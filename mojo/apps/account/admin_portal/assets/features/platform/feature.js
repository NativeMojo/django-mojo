import {platformPage, setupPage} from './page.js';
import {metricsPage} from './metrics.js';
import {maintenancePage} from './maintenance.js';
import {permissionDeniedState} from '../../components/views.js';

export default {
  // Deploy history lives in the merged Deployments lane (the webapps
  // feature); Platform is health evidence plus System Setup.
  id: 'platform', routes: ['platform', 'setup', 'metrics', 'maintenance'], style: 'assets/features/platform/styles.css',
  enabled: (ctx) => ctx.features?.platform?.enabled === true,
  navigation: (ctx) => {
    const capabilities = ctx.features?.platform?.capabilities || {};
    const entries = [];
    if (capabilities.view || capabilities.manage || capabilities.setup ||
        capabilities.security || capabilities.advanced) {
      entries.push({route: 'platform', matches: ['platform', 'setup'], label: 'Platform', icon: 'server', section: 'Control plane'});
    }
    if (capabilities.metrics) {
      entries.push({route: 'metrics', label: 'Metrics', icon: 'chart', section: 'Control plane'});
    }
    if (capabilities.maintenance) {
      entries.push({route: 'maintenance', label: 'Maintenance', icon: 'refresh', section: 'Control plane'});
    }
    return entries;
  },
  title: (route) => ({platform: 'Platform', setup: 'System Setup', metrics: 'Metrics', maintenance: 'Maintenance'}[route] || 'Platform'),
  render: ({ctx, route, signal}) => route === 'metrics'
    ? metricsPage(ctx, signal)
    : route === 'maintenance'
      ? maintenancePage(ctx, signal)
      : route === 'setup'
        ? (ctx.capabilities.setup ? setupPage(ctx, signal) : permissionDeniedState('System Setup requires an active literal superuser.'))
        : platformPage(ctx, route),
};

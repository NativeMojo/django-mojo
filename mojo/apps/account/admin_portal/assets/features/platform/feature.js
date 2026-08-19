import {setupPage} from './page.js';
import {metricsPage} from './metrics.js';
import {maintenancePage} from './maintenance.js';
import {permissionDeniedState} from '../../components/views.js';

export default {
  // Platform health evidence now lives on the Dashboard rows and their
  // drill-ins; deploy history lives in the merged Deployments lane. What is
  // left here is work an operator starts on purpose: Setup, Metrics,
  // Maintenance.
  id: 'platform', routes: ['setup', 'metrics', 'maintenance'], style: 'assets/features/platform/styles.css',
  enabled: (ctx) => ctx.features?.platform?.enabled === true,
  navigation: (ctx) => {
    const capabilities = ctx.features?.platform?.capabilities || {};
    const entries = [];
    if (capabilities.metrics) {
      entries.push({route: 'metrics', label: 'Metrics', icon: 'chart', section: 'Control plane'});
    }
    if (capabilities.maintenance) {
      entries.push({route: 'maintenance', label: 'Maintenance', icon: 'refresh', section: 'Control plane'});
    }
    if (capabilities.setup) {
      // Installation work, not day-to-day operation: `order` pins it below
      // every Control plane entry, and the badge is the only thing that pulls
      // an operator here when the installation is not finished yet.
      entries.push({route: 'setup', label: 'System Setup', icon: 'settings', section: 'System',
        order: 100, badge: capabilities.setup_attention === true});
    }
    return entries;
  },
  title: (route) => ({setup: 'System Setup', metrics: 'Metrics', maintenance: 'Maintenance'}[route] || 'System Setup'),
  render: ({ctx, route, signal}) => route === 'metrics'
    ? metricsPage(ctx, signal)
    : route === 'maintenance'
      ? maintenancePage(ctx, signal)
      : (ctx.capabilities.setup ? setupPage(ctx, signal) : permissionDeniedState('System Setup requires an active literal superuser.')),
};

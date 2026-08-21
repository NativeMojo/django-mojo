import {setupPage} from './page.js';
import {metricsPage} from './metrics.js';
import {maintenancePage} from './maintenance.js';
import {fleetPage} from './fleet.js';
import {permissionDeniedState} from '../../components/views.js';

export default {
  // Platform health evidence now lives on the Dashboard rows and their
  // drill-ins; deploy history lives in the merged Deployments lane. What is
  // left here is work an operator starts on purpose: Setup, Metrics,
  // Maintenance.
  id: 'platform', routes: ['setup', 'metrics', 'maintenance', 'fleet'], style: 'assets/features/platform/styles.css',
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
    if (capabilities.capacity) {
      // The same gate as the Dashboard's capacity drill-in: System Setup
      // authority AND the AWS grant. The page is where fleet size is changed
      // on purpose; the drill-in remains the in-context view.
      entries.push({route: 'fleet', label: 'Fleet Scaling', icon: 'server', section: 'Control plane'});
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
  title: (route) => ({setup: 'System Setup', metrics: 'Metrics', maintenance: 'Maintenance', fleet: 'Fleet Scaling'}[route] || 'System Setup'),
  render: ({ctx, route, signal}) => route === 'metrics'
    ? metricsPage(ctx, signal)
    : route === 'maintenance'
      ? maintenancePage(ctx, signal)
      : route === 'fleet'
        ? (ctx.features?.platform?.capabilities?.capacity === true
          ? fleetPage(ctx, signal)
          : permissionDeniedState('Fleet Scaling requires an active literal superuser with the manage_aws grant.'))
        : (ctx.capabilities.setup ? setupPage(ctx, signal) : permissionDeniedState('System Setup requires an active literal superuser.')),
};

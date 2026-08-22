import {homePage} from './page.js';
import {activityAvailable, activityPage} from './activity.js';

// Home is the installation's own destination: its dot is the worst state
// anywhere inside the portal. It stays enabled on the same block v1's Dashboard
// uses, so a caller who can see one can see the other.
//
// Home also owns Activity & logs. v1 spent a sidebar entry on it; v2 spends
// six and no more, so it is a sub-page reached from Home's activity preview and
// from every blocker that names a log, wearing the back pill like every other
// sub-page. The sidebar keeps Home lit while it is open (`matches`), because
// that is where the operator came from and where the pill returns them.
//
// A caller with no readable Activity source has no Activity page: the route
// falls back to Home exactly like any unknown route, hash and title included,
// rather than opening a page whose four tabs are all hidden.
export default {
  id: 'home',
  routes: ['home', 'activity'],
  style: 'assets/features/home/styles.css',
  enabled: (ctx) => ctx.features?.dashboard?.enabled === true,
  // Home owns the installation-wide views and the setup journey. Activity gets
  // its own entry beside the control plane; System Setup is still the current
  // Admin's page, so its entry is an external link that says so, carrying the
  // attention dot exactly as it did in v1.
  navigation: (ctx) => [
    {route: 'home', label: 'Home', icon: 'home', section: 'Control plane', order: 0},
    activityAvailable(ctx)
      ? {route: 'activity', label: 'Activity & logs', icon: 'activity', section: 'Control plane', order: 50}
      : null,
    ctx.capabilities?.setup === true
      ? {href: `${ctx.admin_path || '/admin/'}#/setup`, external: true, label: 'System Setup', icon: 'settings',
        section: 'System', order: 80, badge: ctx.capabilities.setup_attention === true}
      : null,
  ].filter(Boolean),
  title: (route, ctx) => (route === 'activity' && activityAvailable(ctx) ? 'Activity & logs' : 'Home'),
  // The signal is the page's own abort: Home polls while the platform reports a
  // running deployment, and a route change must stop that poll, not let it
  // repaint a page that is no longer on screen. Activity uses the same signal
  // to abandon an in-flight query when the operator leaves.
  render: ({ctx, route, signal}) => {
    if (route === 'activity') {
      if (activityAvailable(ctx)) return activityPage(ctx, signal);
      history.replaceState({}, '', '#/home');
    }
    return homePage(ctx, signal);
  },
};

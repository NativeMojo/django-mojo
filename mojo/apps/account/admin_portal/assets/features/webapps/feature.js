import {deploymentsPage} from './page.js';

export default {
  // The lane keeps feature id `webapps`; only its routes and label changed.
  // `#/webapps` canonicalizes to `#/deployments` inside the page render.
  id: 'webapps', routes: ['deployments', 'webapps'], style: 'assets/features/webapps/styles.css',
  // Platform viewers must still reach deploy history here now that the
  // Platform lane no longer carries a deployments route.
  enabled: (ctx) => ctx.features?.webapps?.enabled === true
    || ctx.features?.platform?.capabilities?.view === true,
  navigation: () => [{route: 'deployments', matches: ['deployments', 'webapps'], label: 'Deployments', icon: 'deploy', section: 'Control plane'}],
  title: () => 'Deployments',
  render: ({ctx, route, navigate, signal}) => deploymentsPage(ctx, route, navigate, signal),
};

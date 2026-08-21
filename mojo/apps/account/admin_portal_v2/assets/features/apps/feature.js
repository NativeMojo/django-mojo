import {appsPage} from './page.js';

// Apps carries v1's Deployments lane, so it takes v1's enablement exactly: the
// webapps block, or a platform viewer who reached deploy history through it.
export default {
  id: 'apps',
  routes: ['apps'],
  style: 'assets/features/apps/styles.css',
  enabled: (ctx) => ctx.features?.webapps?.enabled === true
    || ctx.features?.platform?.capabilities?.view === true,
  navigation: () => [{route: 'apps', label: 'Apps', icon: 'deploy'}],
  title: () => 'Apps',
  render: ({ctx}) => appsPage(ctx),
};

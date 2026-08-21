import {decodeRouteState} from '../../components/routes.js';
import {appsPage} from './page.js';

// Apps carries v1's Deployments lane, so it takes v1's enablement exactly: the
// webapps block, or a platform viewer who reached deploy history through it.
//
// Two routes, one destination. `apps` is v2's name; `deployments` is v1's, kept
// so existing links and bookmarks still land — the page canonicalizes it to
// `#/apps` with its query state intact, and the sidebar stays lit either way.
export default {
  id: 'apps',
  routes: ['apps', 'deployments'],
  style: 'assets/features/apps/styles.css',
  enabled: (ctx) => ctx.features?.webapps?.enabled === true
    || ctx.features?.platform?.capabilities?.view === true,
  navigation: () => [{
    route: 'apps', matches: ['apps', 'deployments'], label: 'Apps', icon: 'deploy',
  }],
  // `?webapp=<id>` is the app's own page, and the topbar says so. The app's name
  // is not known until the summary read lands, so the title names the kind of
  // page rather than claiming a name it does not have yet.
  title: () => (decodeRouteState().state.webapp ? 'App' : 'Apps'),
  render: ({ctx, route, navigate, signal}) => appsPage(ctx, route, navigate, signal),
};

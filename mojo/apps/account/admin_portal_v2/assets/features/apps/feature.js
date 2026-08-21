import {decodeRouteState} from '../../components/routes.js';
import {appsPage} from './page.js';
import {servingAdvancedPage} from './network.js';

// Apps carries v1's Deployments lane, so it takes v1's enablement exactly: the
// webapps block, or a platform viewer who reached deploy history through it.
//
// Three routes, one destination. `apps` is v2's name; `deployments` is v1's,
// kept so existing links and bookmarks still land — the page canonicalizes it
// to `#/apps` with its query state intact, and the sidebar stays lit either way.
//
// `apps-serving` is the operator half: v1's #/vhosts, #/routes and #/upstreams,
// which had no navigation entry of their own, as one sub-page with a back pill.
// It is inside Apps because they are the same records an app's Serving tab
// shows, read one fleet at a time instead of one app at a time. Its own
// manage_network gate lives in the page, not here — the Apps destination must
// stay open to callers who have no network grant at all.
export default {
  id: 'apps',
  routes: ['apps', 'deployments', 'apps-serving'],
  style: 'assets/features/apps/styles.css',
  enabled: (ctx) => ctx.features?.webapps?.enabled === true
    || ctx.features?.platform?.capabilities?.view === true,
  navigation: () => [{
    route: 'apps', matches: ['apps', 'deployments', 'apps-serving'],
    label: 'Apps', icon: 'deploy',
  }],
  // `?webapp=<id>` is the app's own page, and the topbar says so. The app's name
  // is not known until the summary read lands, so the title names the kind of
  // page rather than claiming a name it does not have yet.
  title: (route) => (route === 'apps-serving' ? 'Serving (advanced)'
    : decodeRouteState().state.webapp ? 'App' : 'Apps'),
  render: ({ctx, route, navigate, signal}) => (route === 'apps-serving'
    ? servingAdvancedPage(ctx, navigate)
    : appsPage(ctx, route, navigate, signal)),
};

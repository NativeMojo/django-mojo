import {decodeRouteState} from '../../components/routes.js';
import {accessPage, tabFor} from './page.js';

// Access — v1's People destination, with its keys and its trust evidence
// brought up beside it.
//
// The gate is v1's People gate, unchanged: the `people` block opens the
// destination, and the capabilities inside it (users, groups, manage_users,
// manage_groups, manage_api_keys, view_logins) decide which views render. A
// caller whose block is absent gets no sidebar entry at all — hidden, not
// shown-and-refused.
//
// Five routes, one sidebar entry. `users` and `groups` are v1's own route
// names, so every existing link lands; `keys` and `security` are the two new
// views; `access` is the name the rest of v2 links this destination by and
// resolves to the first view the caller can read. `matches` keeps the entry lit
// for all five.
export default {
  id: 'access',
  routes: ['access', 'users', 'groups', 'keys', 'security'],
  style: 'assets/features/access/styles.css',
  enabled: (ctx) => ctx.features?.people?.enabled === true,
  navigation: () => [{
    route: 'users', label: 'Access', icon: 'users', section: 'Control plane', order: 40,
    matches: ['access', 'users', 'groups', 'keys', 'security'],
  }],
  // A named record is a page of its own, and the topbar says which KIND of
  // page. The record's own name is not known until its read lands, so the
  // title never claims a name it does not have yet — same rule as Apps.
  title: (route, ctx) => {
    const state = decodeRouteState().state;
    const legacy = /^\d+$/.test(String(state.inspector || '')) ? state.inspector : '';
    const tab = tabFor(route, ctx);
    if (tab?.id === 'users' && (state.user || legacy)) return 'User';
    if (tab?.id === 'groups' && (state.group || legacy)) return 'Group';
    return tab?.label || 'Access';
  },
  render: ({ctx, route, navigate}) => accessPage(ctx, route, navigate),
};

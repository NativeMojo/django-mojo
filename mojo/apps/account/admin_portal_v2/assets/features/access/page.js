// Access — one destination, four views: People · Groups · Keys · Security
// activity.
//
// v1 called this People and spent one sidebar entry on two routes (#/users,
// #/groups). Both route names are kept exactly — every v1 link, bookmark and
// routeHref('users') call still lands — and two views join them:
//
//   * Keys (#/keys) surfaces what v1 buried three clicks deep inside a group
//     inspector. A group API key is part of its group, so the tab lists keys
//     UNDER the group they act as, from the same read. Deploy keys belong to an
//     app and are pointed at Apps rather than duplicated.
//   * Security activity (#/security) is the trust evidence beside the people it
//     is about: sign-ins (v1's `view_logins` read, which had no page-level view
//     at all) and security events, each linking into the full Activity viewer.
//
// Every view is its own route, like Domains: the browser Back button walks the
// tabs like pages, and the sidebar entry stays lit for all of them through its
// `matches` list. `#/access` is accepted too — it is the name the rest of v2
// links this destination by — and resolves to the first view this caller can
// read.

import {h} from '../../core.js';
import {decodeRouteState, routeHref} from '../../components/routes.js';
import {permissionDeniedState, sectionTabs} from '../../components/views.js';
import {capabilities} from './shared.js';
import {usersTab} from './users.js';
import {groupsTab} from './groups.js';
import {keysTab, keysTabVisible} from './keys.js';
import {securityTab, securityTabVisible} from './security.js';

// Each tab names the ONE thing the bootstrap payload has to report before it is
// offered, and the capability names are v1's, unchanged.
const TABS = [
  {
    id: 'users', label: 'People',
    copy: 'Members of this installation. Roles come from groups; superuser is '
      + 'granted alone, on purpose.',
    visible: (ctx) => capabilities(ctx).users === true,
    render: usersTab,
  },
  {
    id: 'groups', label: 'Groups',
    copy: 'What people belong to. A group carries the permissions, the '
      + 'membership and the API keys that act as it.',
    visible: (ctx) => capabilities(ctx).groups === true,
    render: groupsTab,
  },
  {
    id: 'keys', label: 'Keys',
    copy: 'Nothing is free-floating: API keys belong to a group and act with '
      + 'its permissions; deploy keys belong to an app.',
    visible: keysTabVisible,
    render: keysTab,
  },
  {
    id: 'security', label: 'Security activity',
    copy: 'The events that matter for trust — sign-ins, step-up confirmations '
      + 'and credential changes.',
    visible: securityTabVisible,
    render: securityTab,
  },
];

/** The views this caller may actually read, in display order. */
export function visibleTabs(ctx) {
  return TABS.filter((tab) => tab.visible(ctx) === true);
}

/**
 * The view a route asks for, or the first one this caller can read.
 *
 * `#/access` names the destination rather than a view, so it always resolves to
 * the first readable tab — as does a route naming a view this caller is not
 * offered.
 */
export function tabFor(route, ctx) {
  const tabs = visibleTabs(ctx);
  return tabs.find((tab) => tab.id === route) || tabs[0] || null;
}

export async function accessPage(ctx, route, navigate) {
  const tabs = visibleTabs(ctx);
  const tab = tabFor(route, ctx);
  if (!tab) {
    return permissionDeniedState(
      'Access needs one of the People, Groups, API key or sign-in permissions '
      + 'on this installation.');
  }
  // A hash that named a view this caller cannot read — or the destination
  // rather than a view — is corrected in place, so the address bar and the
  // screen never disagree. Route state (?user=, ?group=) is preserved: it is
  // what makes a deep link open the record.
  if (route !== tab.id) {
    const state = decodeRouteState().state;
    history.replaceState({}, '', routeHref(tab.id, state));
  }

  // Filled by the view with the one control that has to survive its own body
  // reloads — Invite user, New group. The tab bar and the header are ours.
  const actions = h('div', {class: 'page-actions'});
  const bodyHost = h('div', {class: 'access-body'});
  const root = h('div', {class: 'page access-page'},
    h('header', {class: 'page-header'},
      h('div', {},
        h('div', {class: 'eyebrow', text: 'Access'}),
        h('h1', {text: tab.label, tabindex: '-1'}),
        h('p', {text: tab.copy})),
      actions),
    tabs.length > 1
      ? sectionTabs({
        items: tabs.map(({id, label}) => ({id, label})),
        active: tab.id,
        label: 'Access sections',
        // Each view is its own route, so this is a plain navigation — the
        // browser Back button walks the views exactly like pages.
        onChange: (id) => navigate(id),
      })
      : null,
    bodyHost);

  const body = await tab.render(ctx, actions);
  bodyHost.replaceChildren(body);
  root.dispose = () => body.dispose?.();
  return root;
}

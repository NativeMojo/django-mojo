// Access ▸ People — v1's users list and user inspector, ported whole.
//
// Same reads (`/api/user`, `/api/account/admin/people/permission-bundles`,
// `/api/account/logins`), same five inspector sections, same lifecycle and
// record actions, same one-time secret dialog for a temporary password.
//
// What changed:
//   * the page header and tab bar belong to features/access/page.js, so this
//     module returns a body and hangs its one page-level control (Invite user)
//     in the actions slot it is handed;
//   * a search no longer repaints the whole page — the panel and its search box
//     survive, so the caret stays where the operator left it;
//   * a deep link opens the record: `?user=<id>` is the address v2 writes,
//     `?inspector=<id>` is v1's and still works.

import {api, apiEnvelope, badge, FormView, formatDate, h, icon, TableView} from '../../core.js';
import {loadInto, runAction, toast} from '../../components/actions.js';
import {modelHeader} from '../../components/model.js';
import {confirmAction, openInspector, openModal} from '../../components/overlays.js';
import {decodeRouteState} from '../../components/routes.js';
import {sectionTabs, timelineView} from '../../components/views.js';
import {activityLinks, activityTabVisible, capabilities, detailGrid, initials,
  inviteWarning, oneTimeSecret, post} from './shared.js';

const USER_SECTIONS = [
  ['overview', 'Overview'], ['identity', 'Identity'], ['access', 'Access'],
  ['signins', 'Sign-ins'], ['activity', 'Activity'],
];

function editUser(user, reload) {
  const form = new FormView({
    fields: [
      {name: 'display_name', label: 'Display name', required: true},
      {name: 'email', label: 'Email', type: 'email', required: true},
      {name: 'username', label: 'Username', required: true},
    ], value: user, submitLabel: 'Save identity', onSubmit: async (values) => {
      await post(`/api/user/${user.id}`, values); close(); await reload();
    },
  });
  const close = openModal({title: 'Edit user identity', content: form.render()});
}

function inviteUser(reload) {
  const form = new FormView({
    fields: [
      {name: 'display_name', label: 'Display name', required: true},
      {name: 'email', label: 'Email', type: 'email', required: true},
      {name: 'username', label: 'Username', help: 'Defaults to the email address.'},
    ], submitLabel: 'Create and send invite', onSubmit: async (values) => {
      if (!values.username) values.username = values.email;
      const user = await post('/api/user', values);
      await post(`/api/user/${user.id}`, {send_invite: {}});
      close(); await reload();
    },
  });
  const close = openModal({title: 'Invite user',
    subtitle: 'The invite link lets the user choose their own password.',
    content: form.render()});
}

async function userSection(ctx, user, section, body, reload) {
  const caps = capabilities(ctx);
  if (section === 'overview') {
    body.replaceChildren(detailGrid([
      ['User ID', user.id], ['Username', user.username], ['Email', user.email],
      ['Email verified', user.is_email_verified ? 'Yes' : 'No'],
      ['Last sign-in', formatDate(user.last_login)],
      ['Last activity', formatDate(user.last_activity)],
      ['Password state', user.requires_password_change
        ? 'Temporary — change required' : 'Normal'],
    ]));
    return;
  }
  if (section === 'identity') {
    body.replaceChildren(detailGrid([
      ['Display name', user.display_name], ['First name', user.first_name],
      ['Last name', user.last_name], ['Phone', user.phone_number],
      ['UUID', user.uuid || '—'],
    ]), caps.manage_users
      ? h('button', {class: 'button', onclick: () => editUser(user, reload)}, 'Edit identity')
      : null);
    return;
  }
  if (section === 'access') {
    if (!caps.manage_users) {
      body.replaceChildren(h('p', {class: 'muted',
        text: 'Permission bundles require global People management access.'}));
      return;
    }
    await loadInto(body, async (current) => {
      const contract = await api(
        `/api/account/admin/people/permission-bundles?user=${encodeURIComponent(user.id)}`);
      if (!current()) return;
      const selected = new Set(contract.selected || []);
      const switches = contract.bundles.map((bundle) => {
        const input = h('input', {type: 'checkbox', checked: selected.has(bundle.id), value: bundle.id});
        return h('label', {class: 'bundle-option'}, input,
          h('span', {}, h('strong', {text: bundle.label}),
            h('small', {text: bundle.permissions.join(', ')})));
      });
      // The Save button lives in the body its own success repaints, so it
      // carries the wait and is never restored onto the replaced node.
      const save = h('button', {class: 'button primary'}, 'Save access bundles');
      save.addEventListener('click', () => runAction(save, async () => {
        const names = switches.filter((row) => row.querySelector('input').checked)
          .map((row) => row.querySelector('input').value);
        await post('/api/account/admin/people/permission-bundles',
          {user: user.id, version: contract.version, selected: names});
        await userSection(ctx, user, 'access', body, reload);
      }, {pendingLabel: 'Saving…', restoreOnSuccess: false}));
      body.replaceChildren(h('div', {class: 'bundle-grid'}, ...switches),
        h('p', {class: 'muted',
          text: 'Unknown and Advanced-only permission keys are preserved.'}), save);
    }, {message: 'Loading access bundles…',
      retry: () => userSection(ctx, user, 'access', body, reload)});
    return;
  }
  if (section === 'signins') {
    if (!caps.view_logins) {
      body.replaceChildren(h('p', {class: 'muted',
        text: 'Sign-in evidence is not available with your current access.'}));
      return;
    }
    await loadInto(body, async (current) => {
      const events = (await apiEnvelope(
        `/api/account/logins?user=${encodeURIComponent(user.id)}&size=50&sort=-created`)).items;
      if (!current()) return;
      body.replaceChildren(timelineView(events.map((event) => ({
        title: `${event.source || 'Sign-in'} · ${event.ip_address || 'Unknown IP'}`,
        detail: [event.city, event.region, event.country_code, event.user_agent_info?.browser,
          event.is_new_country ? 'New country' : '',
          event.is_new_region ? 'New region' : ''].filter(Boolean).join(' · '),
        created: event.created,
      }))));
    }, {message: 'Loading sign-ins…',
      retry: () => userSection(ctx, user, 'signins', body, reload)});
    return;
  }
  body.replaceChildren(activityLinks(ctx, {type: 'user', id: user.id}));
}

export async function openUser(ctx, summary, reloadList) {
  let user = await api(`/api/user/${summary.id}`); let active = 'overview';
  const caps = capabilities(ctx);
  const sections = USER_SECTIONS
    .filter(([id]) => id !== 'signins' || caps.view_logins)
    .filter(([id]) => id !== 'activity'
      || ['logs', 'events', 'incidents', 'tickets'].some((tab) => activityTabVisible(ctx, tab)));
  const body = h('div', {class: 'inspector-section'});
  const tabs = sectionTabs({items: sections.map(([id, label]) => ({id, label})), active,
    onChange: async (id) => {
      active = id;
      [...tabs.querySelectorAll('button')].forEach((button, index) =>
        button.classList.toggle('active', sections[index][0] === id));
      await userSection(ctx, user, id, body, reload);
    }});
  const reload = async () => {
    user = await api(`/api/user/${user.id}`);
    await userSection(ctx, user, active, body, reload);
    await reloadList();
  };
  const manage = caps.manage_users;
  const header = modelHeader({iconName: 'users', avatar: initials(user),
    primary: user.display_name || user.username,
    secondary: `${user.email || user.username} · ${user.is_email_verified ? 'Email verified' : 'Email unverified'} · ${user.is_phone_verified ? 'Phone verified' : 'Phone unverified'}`,
    warning: user.requires_password_change
      ? 'Temporary password must be changed at next authentication.' : '',
    lifecycle: manage ? {active: user.is_active, label: user.username,
      onDisable: async ({reason}) => {
        await post(`/api/user/${user.id}`, {disable: {reason: 'admin', note: reason}});
        await reload();
      },
      onReactivate: async ({reason}) => {
        await post(`/api/user/${user.id}`, {reactivate: {note: reason}});
        await reload();
      }} : null,
    actions: [
      {label: 'Edit identity', capability: manage, run: () => editUser(user, reload)},
      {label: 'Resend invite', capability: manage, done: 'Invite sent.',
        run: async () => { await post(`/api/user/${user.id}`, {send_invite: {}}); }},
      {label: 'Send password-reset link', capability: manage, done: 'Password reset link sent.',
        run: async () => { await post('/api/account/admin/user/password/reset', {user: user.id}); }},
      {label: 'Set temporary password', capability: manage, danger: true, run: async () => {
        const result = await post('/api/account/admin/user/password/temporary', {user: user.id});
        oneTimeSecret('Temporary password', 'Temporary password', result.temporary_password);
      }},
      {label: 'Revoke sessions', capability: manage, danger: true, run: async () => {
        const answer = await confirmAction({title: 'Revoke all sessions?',
          copy: 'Every active token and websocket session for this user will be invalidated.',
          confirmLabel: 'Revoke sessions', danger: true});
        if (!answer.confirmed) return;
        await post(`/api/user/${user.id}`, {revoke_sessions: {}});
        await reload();
        toast('All sessions revoked.');
      }},
      {label: 'Copy safe identifiers', done: 'Identifiers copied.',
        run: () => navigator.clipboard.writeText(`user:${user.id}\nusername:${user.username}`)},
    ], context: {user},
  });
  const content = h('div', {class: 'access-inspector'}, header, tabs, body);
  openInspector({title: `User · ${user.display_name || user.username}`, content, wide: true});
  await userSection(ctx, user, active, body, reload);
}

export function usersTab(ctx, actions) {
  const caps = capabilities(ctx);
  const listBody = h('div', {});
  let generation = 0; let linkedInspectorOpened = false;

  const load = async (term = '') => {
    const mine = ++generation;
    await loadInto(listBody, async (current) => {
      const query = new URLSearchParams({size: '50', sort: '-last_activity'});
      if (term) query.set('search', term);
      const rows = (await apiEnvelope(`/api/user?${query}`)).items;
      if (!current() || mine !== generation) return;
      const open = (row) => openUser(ctx, row, () => load(term));
      listBody.replaceChildren(new TableView({
        columns: [
          {label: 'User', render: (row) => h('div', {class: 'identity'},
            h('span', {class: 'avatar', text: initials(row).toUpperCase()}),
            h('div', {}, h('strong', {text: row.display_name || row.username}),
              h('small', {text: row.email || row.username})))},
          {label: 'Status', render: (row) => badge(
            row.is_active ? (row.requires_password_change ? 'Password change' : 'Active') : 'Inactive',
            row.is_active ? (row.requires_password_change ? 'warning' : 'success') : 'danger')},
          {label: 'Last activity', render: (row) => formatDate(row.last_activity)},
          {label: '', render: () => icon('chevron')},
        ], rows, empty: 'No matching users.', onSelect: open}).render());
      // v2 writes `?user=<id>`; v1 wrote `?inspector=<id>`. Both open the
      // record, so a link either portal produced still lands.
      const state = decodeRouteState().state;
      const wanted = state.user || state.inspector;
      const linked = wanted && rows.find((row) => String(row.id) === String(wanted));
      if (linked && !linkedInspectorOpened) { linkedInspectorOpened = true; await open(linked); }
    }, {message: 'Loading users…', retry: () => load(term)});
  };

  if (caps.manage_users) {
    actions.append(h('button', {class: 'button primary',
      onclick: () => inviteUser(() => load(input.value.trim()))},
    icon('plus'), 'Invite user'));
  }

  const input = h('input', {placeholder: 'Search users', 'aria-label': 'Search users'});
  let timer;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => load(input.value.trim()), 250);
  });

  const panel = h('section', {class: 'panel'},
    h('div', {class: 'panel-head'},
      h('div', {}, h('h2', {text: 'Users'}),
        h('p', {text: 'Select a row to open the standard inspector.'})),
      h('label', {class: 'search'}, icon('search'), input)),
    listBody);

  const body = h('div', {class: 'access-tab'}, inviteWarning(ctx), panel);
  body.dispose = () => clearTimeout(timer);
  load();
  return body;
}

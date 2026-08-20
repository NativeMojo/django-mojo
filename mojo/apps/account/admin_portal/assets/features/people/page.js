import {api, apiEnvelope, badge, FormView, formatDate, h, icon, pageHeader, TableView} from '../../core.js';
import {copyButton, loadInto, runAction} from '../../components/actions.js';
import {modelHeader} from '../../components/model.js';
import {confirmAction, openInspector, openModal} from '../../components/overlays.js';
import {activityHref, decodeRouteState, returnLocation, routeHref} from '../../components/routes.js';
import {sectionTabs, timelineView} from '../../components/views.js';

export const ACTIVITY_QUERY_KEYS = Object.freeze([
  'tab', 'start', 'size', 'sort', 'search', 'date', 'user', 'group', 'ip',
  'hostname', 'incident', 'model', 'model_id',
]);

const USER_SECTIONS = [
  ['overview', 'Overview'], ['identity', 'Identity'], ['access', 'Access'],
  ['signins', 'Sign-ins'], ['activity', 'Activity'],
];
const GROUP_SECTIONS = [
  ['overview', 'Overview'], ['identity', 'Identity'], ['members', 'Members'],
  ['keys', 'API Keys'], ['permissions', 'Permissions'], ['activity', 'Activity'],
  ['advanced', 'Advanced'],
];

function capabilities(ctx) { return ctx.features?.people?.capabilities || {}; }
function post(path, body) { return api(path, {method: 'POST', body: JSON.stringify(body)}); }
function initials(row) { return (row.display_name || row.name || row.username || '?').slice(0, 2); }

function detailGrid(rows) {
  return h('dl', {class: 'detail-grid'}, ...rows.flatMap(([label, value]) => [
    h('dt', {text: label}), h('dd', {}, value instanceof Node ? value : String(value ?? '—')),
  ]));
}

function activityLinks(ctx, subject) {
  const caps = capabilities(ctx);
  const lanes = [
    ['logs', 'view_logs'], ['events', 'view_events'],
    ['incidents', 'view_incidents'], ['tickets', 'view_tickets'],
  ].filter(([, capability]) => caps[capability]);
  if (!lanes.length) return h('p', {class: 'muted', text: 'No related Activity lanes are available with your current access.'});
  return h('div', {class: 'activity-links'},
    lanes.map(([tab]) => h('a', {
      class: 'related-record', href: activityHref(tab, subject, {return: returnLocation()}),
    }, h('strong', {text: tab[0].toUpperCase() + tab.slice(1)}), icon('chevron'))));
}

function oneTimeSecret(title, label, value, returnFocus = null) {
  let secret = String(value || '');
  const input = h('input', {class: 'secret-value', readonly: true, value: secret,
    autocomplete: 'off', 'data-one-time-secret': 'true'});
  // A function, not the string: the dialog scrubs `secret` on close, and the
  // shared button reads it at click time so a copy after that copies nothing
  // rather than re-leaking a value the dialog promised to forget.
  const copy = copyButton(() => secret, {label: 'Copy once', copiedLabel: 'Copied',
    className: 'button primary'});
  const content = h('div', {class: 'secret-reveal'},
    h('p', {text: `${label} is displayed only in this dialog. Store it now.`}), input,
    h('div', {class: 'form-actions'}, copy));
  const close = openModal({title, content, returnFocus, onClose: () => {
    secret = ''; input.value = ''; copy.remove(); content.replaceChildren();
  }});
  return close;
}

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

async function inviteUser(reload) {
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
  const close = openModal({title: 'Invite user', subtitle: 'The invite link lets the user choose their own password.', content: form.render()});
}

async function userSection(ctx, user, section, body, reload) {
  const caps = capabilities(ctx);
  if (section === 'overview') {
    body.replaceChildren(detailGrid([
      ['User ID', user.id], ['Username', user.username], ['Email', user.email],
      ['Email verified', user.is_email_verified ? 'Yes' : 'No'],
      ['Last sign-in', formatDate(user.last_login)], ['Last activity', formatDate(user.last_activity)],
      ['Password state', user.requires_password_change ? 'Temporary — change required' : 'Normal'],
    ]));
    return;
  }
  if (section === 'identity') {
    body.replaceChildren(detailGrid([
      ['Display name', user.display_name], ['First name', user.first_name],
      ['Last name', user.last_name], ['Phone', user.phone_number], ['UUID', user.uuid || '—'],
    ]), caps.manage_users ? h('button', {class: 'button', onclick: () => editUser(user, reload)}, 'Edit identity') : null);
    return;
  }
  if (section === 'access') {
    if (!caps.manage_users) {
      body.replaceChildren(h('p', {class: 'muted', text: 'Permission bundles require global People management access.'})); return;
    }
    await loadInto(body, async (current) => {
      const contract = await api(`/api/account/admin/people/permission-bundles?user=${encodeURIComponent(user.id)}`);
      if (!current()) return;
      const selected = new Set(contract.selected || []);
      const switches = contract.bundles.map((bundle) => {
        const input = h('input', {type: 'checkbox', checked: selected.has(bundle.id), value: bundle.id});
        return h('label', {class: 'bundle-option'}, input, h('span', {}, h('strong', {text: bundle.label}),
          h('small', {text: bundle.permissions.join(', ')})));
      });
      // The Save button lives in the body its own success repaints, so it
      // carries the wait and is never restored onto the replaced node.
      const save = h('button', {class: 'button primary'}, 'Save access bundles');
      save.addEventListener('click', () => runAction(save, async () => {
        const names = switches.filter((row) => row.querySelector('input').checked).map((row) => row.querySelector('input').value);
        await post('/api/account/admin/people/permission-bundles', {user: user.id, version: contract.version, selected: names});
        await userSection(ctx, user, 'access', body, reload);
      }, {pendingLabel: 'Saving…', restoreOnSuccess: false}));
      body.replaceChildren(h('div', {class: 'bundle-grid'}, ...switches),
        h('p', {class: 'muted', text: 'Unknown and Advanced-only permission keys are preserved.'}), save);
    }, {message: 'Loading access bundles…',
      retry: () => userSection(ctx, user, 'access', body, reload)});
    return;
  }
  if (section === 'signins') {
    if (!caps.view_logins) {
      body.replaceChildren(h('p', {class: 'muted', text: 'Sign-in evidence is not available with your current access.'})); return;
    }
    await loadInto(body, async (current) => {
      const events = (await apiEnvelope(`/api/account/logins?user=${encodeURIComponent(user.id)}&size=50&sort=-created`)).items;
      if (!current()) return;
      body.replaceChildren(timelineView(events.map((event) => ({
        title: `${event.source || 'Sign-in'} · ${event.ip_address || 'Unknown IP'}`,
        detail: [event.city, event.region, event.country_code, event.user_agent_info?.browser,
          event.is_new_country ? 'New country' : '', event.is_new_region ? 'New region' : ''].filter(Boolean).join(' · '),
        created: event.created,
      }))));
    }, {message: 'Loading sign-ins…',
      retry: () => userSection(ctx, user, 'signins', body, reload)});
    return;
  }
  body.replaceChildren(activityLinks(ctx, {type: 'user', id: user.id}));
}

async function openUser(ctx, summary, reloadList) {
  let user = await api(`/api/user/${summary.id}`); let active = 'overview';
  const caps = capabilities(ctx);
  const sections = USER_SECTIONS.filter(([id]) =>
    id !== 'signins' || caps.view_logins).filter(([id]) =>
    id !== 'activity' || ['view_logs', 'view_events', 'view_incidents', 'view_tickets'].some((key) => caps[key]));
  const body = h('div', {class: 'inspector-section'});
  const tabs = sectionTabs({items: sections.map(([id, label]) => ({id, label})), active,
    onChange: async (id) => { active = id; [...tabs.querySelectorAll('button')].forEach((button, index) => button.classList.toggle('active', sections[index][0] === id)); await userSection(ctx, user, id, body, reload); }});
  const reload = async () => { user = await api(`/api/user/${user.id}`); await userSection(ctx, user, active, body, reload); await reloadList(); };
  const manage = capabilities(ctx).manage_users;
  const header = modelHeader({iconName: 'users', avatar: initials(user), primary: user.display_name || user.username,
    secondary: `${user.email || user.username} · ${user.is_email_verified ? 'Email verified' : 'Email unverified'} · ${user.is_phone_verified ? 'Phone verified' : 'Phone unverified'}`,
    warning: user.requires_password_change ? 'Temporary password must be changed at next authentication.' : '',
    lifecycle: manage ? {active: user.is_active, label: user.username,
      onDisable: async ({reason}) => { await post(`/api/user/${user.id}`, {disable: {reason: 'admin', note: reason}}); await reload(); },
      onReactivate: async ({reason}) => { await post(`/api/user/${user.id}`, {reactivate: {note: reason}}); await reload(); }} : null,
    actions: [
      {label: 'Edit identity', capability: manage, run: () => editUser(user, reload)},
      {label: 'Resend invite', capability: manage, run: async () => { await post(`/api/user/${user.id}`, {send_invite: {}}); }},
      {label: 'Send password-reset link', capability: manage, run: async () => { await post('/api/account/admin/user/password/reset', {user: user.id}); }},
      {label: 'Set temporary password', capability: manage, danger: true, run: async () => {
        const result = await post('/api/account/admin/user/password/temporary', {user: user.id}); oneTimeSecret('Temporary password', 'Temporary password', result.temporary_password);
      }},
      {label: 'Revoke sessions', capability: manage, danger: true, run: async () => {
        const answer = await confirmAction({title: 'Revoke all sessions?', copy: 'Every active token and websocket session for this user will be invalidated.', confirmLabel: 'Revoke sessions', danger: true});
        if (answer.confirmed) { await post(`/api/user/${user.id}`, {revoke_sessions: {}}); await reload(); }
      }},
      {label: 'Copy safe identifiers', run: () => navigator.clipboard.writeText(`user:${user.id}\nusername:${user.username}`)},
    ], context: {user},
  });
  const content = h('div', {class: 'people-inspector'}, header, tabs, body);
  openInspector({title: `User · ${user.display_name || user.username}`, content, wide: true});
  await userSection(ctx, user, active, body, reload);
}

function editGroup(group, reload) {
  const form = new FormView({fields: [
    {name: 'name', label: 'Group name', required: true},
    {name: 'kind', label: 'Kind', required: true},
    {name: 'uuid', label: 'UUID'},
    {name: 'parent', label: 'Parent group', type: 'relationship', relationship: {
      endpoint: '/api/group', graph: 'basic', valuePath: 'id', labelPath: 'name', allowClear: true,
      filters: group.id ? {id__ne: group.id} : {},
    }},
  ], value: {...group, parent: group.parent || ''}, submitLabel: 'Save group identity', onSubmit: async (values) => {
    values.parent = values.parent || null; await post(`/api/group/${group.id}`, values); close(); await reload();
  }});
  const close = openModal({title: 'Edit group identity', content: form.render(), wide: true});
}

function newGroup(reload) {
  const form = new FormView({fields: [
    {name: 'name', label: 'Group name', required: true}, {name: 'kind', label: 'Kind', required: true},
    {name: 'parent', label: 'Parent group', type: 'relationship', relationship: {endpoint: '/api/group', graph: 'basic', allowClear: true}},
  ], value: {kind: 'group'}, submitLabel: 'Create group', onSubmit: async (values) => {
    values.parent = values.parent || null; await post('/api/group', values); close(); await reload();
  }});
  const close = openModal({title: 'New group', content: form.render(), wide: true});
}

function addMember(group, reload) {
  const form = new FormView({fields: [
    {name: 'user', label: 'User', type: 'relationship', required: true, relationship: {endpoint: '/api/user', graph: 'basic', labelPath: 'display_name', detailPath: 'email'}},
    {name: 'role', label: 'Role', type: 'select', options: [{value: 'member', label: 'Member'}, {value: 'guest', label: 'Guest'}]},
  ], value: {role: 'member'}, submitLabel: 'Add member', onSubmit: async (values) => {
    await post('/api/group/member', {group: group.id, user: values.user, is_active: true, permissions: values.role === 'guest' ? {guest: true} : {}}); close(); await reload();
  }});
  const close = openModal({title: `Add member to ${group.name}`, content: form.render(), wide: true});
}

function createApiKey(group, reload) {
  const form = new FormView({fields: [{name: 'name', label: 'Key name', required: true}], submitLabel: 'Create API key', onSubmit: async (values) => {
    const result = await post('/api/group/apikey', {group: group.id, name: values.name, permissions: {}});
    close(); oneTimeSecret('New API key', 'API key token', result.token); await reload();
  }});
  const close = openModal({title: `New API key for ${group.name}`, content: form.render()});
}

async function groupSection(ctx, group, section, body, reload) {
  const caps = capabilities(ctx);
  if (section === 'overview') {
    body.replaceChildren(detailGrid([
      ['Group ID', group.id], ['UUID', group.uuid], ['Kind', group.kind], ['Parent', group.parent?.name || 'None'],
      ['Members', group.member_count ?? '—'], ['Last activity', formatDate(group.last_activity)],
    ])); return;
  }
  if (section === 'identity') {
    body.replaceChildren(detailGrid([['Name', group.name], ['Kind', group.kind], ['UUID', group.uuid], ['Parent', group.parent?.name || 'None']]),
      caps.manage_groups ? h('button', {class: 'button', onclick: () => editGroup(group, reload)}, 'Edit identity') : null); return;
  }
  if (section === 'members' || section === 'permissions') {
    await loadInto(body, async (current) => {
      const members = (await apiEnvelope(`/api/group/member?group=${encodeURIComponent(group.id)}&size=100`)).items;
      if (!current()) return;
      const rows = new TableView({columns: [
        {label: 'Member', render: (row) => h('div', {}, h('strong', {text: row.user?.display_name || row.user?.username || `User ${row.user?.id}`}), h('small', {text: row.user?.email || ''}))},
        {label: 'Role', render: (row) => badge(row.permissions?.guest ? 'Guest' : 'Member')},
        {label: 'Status', render: (row) => badge(row.is_active ? 'Active' : 'Inactive', row.is_active ? 'success' : 'danger')},
        {label: 'Permissions', render: (row) => h('span', {text: Object.keys(row.permissions || {}).filter((key) => row.permissions[key] && key !== 'guest').join(', ') || 'None'})},
      ], rows: members, empty: 'No members are visible in this group.'}).render();
      body.replaceChildren(rows, section === 'members' && caps.manage_groups ? h('button', {class: 'button', onclick: () => addMember(group, reload)}, 'Add member') : null);
    }, {message: 'Loading members…', retry: () => groupSection(ctx, group, section, body, reload)});
    return;
  }
  if (section === 'keys') {
    // † Every one of these three actions ends in reload(), which rebuilds the
    // table the button sits in, so there is no node to pin a pending state to.
    // They are credential operations, so they run behind the busy scrim, keyed
    // per key-and-action so one row's Rotate can never return another's promise.
    const keyAction = (row, action, task) => runAction(null, task,
      {key: `apikey:${row.id}:${action}`, busy: {title: `${action} ${row.name}…`,
        detail: 'The credential is being changed.'}});
    await loadInto(body, async (current) => {
      const keys = (await apiEnvelope(`/api/group/apikey?group=${encodeURIComponent(group.id)}&size=100`)).items;
      if (!current()) return;
      body.replaceChildren(new TableView({columns: [
        {label: 'Key', render: (row) => h('div', {}, h('strong', {text: row.name}), h('small', {text: `ID ${row.id}`}))},
        {label: 'Status', render: (row) => badge(row.is_active ? 'Active' : 'Inactive', row.is_active ? 'success' : 'danger')},
        {label: 'Last used', render: (row) => formatDate(row.last_used)},
        {label: 'Actions', render: (row) => h('div', {class: 'inline-actions'},
          h('button', {class: 'button compact', onclick: (event) => { event.stopPropagation(); const trigger = event.currentTarget; return keyAction(row, 'Rotating', async () => { const result = await post('/api/account/admin/apikey/action', {api_key: row.id, action: 'rotate'}); oneTimeSecret('Rotated API key', 'API key token', result.token, trigger); await reload(); }); }}, 'Rotate'),
          h('button', {class: 'button compact', onclick: (event) => { event.stopPropagation(); return keyAction(row, row.is_active ? 'Deactivating' : 'Reactivating', async () => { await post('/api/account/admin/apikey/action', {api_key: row.id, action: row.is_active ? 'deactivate' : 'reactivate'}); await reload(); }); }}, row.is_active ? 'Deactivate' : 'Reactivate'),
          // The confirm comes first and nothing paints while it is open.
          h('button', {class: 'button compact danger', onclick: (event) => { event.stopPropagation(); return confirmAction({title: `Revoke ${row.name}?`, copy: 'The API key record and credential will be deleted.', confirmLabel: 'Revoke key', danger: true}).then((answer) => answer.confirmed ? keyAction(row, 'Revoking', async () => { await post('/api/account/admin/apikey/action', {api_key: row.id, action: 'revoke'}); await reload(); }) : undefined); }}, 'Revoke'))},
      ], rows: keys, empty: 'No API keys exist for this group.'}).render(),
      caps.manage_api_keys ? h('button', {class: 'button', onclick: () => createApiKey(group, reload)}, 'Create API key') : null);
    }, {message: 'Loading API keys…', retry: () => groupSection(ctx, group, section, body, reload)});
    return;
  }
  if (section === 'activity') {
    body.replaceChildren(activityLinks(ctx, {type: 'group', id: group.id})); return;
  }
  body.replaceChildren(h('p', {class: 'muted', text: 'Advanced configuration is read-only here. Use the Advanced feature for raw JSON changes.'}),
    h('pre', {class: 'json-preview', text: JSON.stringify(group.metadata || {}, null, 2)}));
}

async function openGroup(ctx, summary, reloadList) {
  let group = await api(`/api/group/${summary.id}`); let active = 'overview';
  const caps = capabilities(ctx);
  const sections = GROUP_SECTIONS.filter(([id]) =>
    id !== 'keys' || caps.manage_api_keys).filter(([id]) =>
    id !== 'activity' || ['view_logs', 'view_events', 'view_incidents', 'view_tickets'].some((key) => caps[key]));
  const body = h('div', {class: 'inspector-section'});
  const tabs = sectionTabs({items: sections.map(([id, label]) => ({id, label})), active,
    onChange: async (id) => { active = id; [...tabs.querySelectorAll('button')].forEach((button, index) => button.classList.toggle('active', sections[index][0] === id)); await groupSection(ctx, group, id, body, reload); }});
  const reload = async () => { group = await api(`/api/group/${group.id}`); await groupSection(ctx, group, active, body, reload); await reloadList(); };
  const manage = capabilities(ctx).manage_groups;
  const header = modelHeader({iconName: 'users', avatar: initials(group), primary: group.name, secondary: `${group.kind} · ${group.uuid || 'UUID pending'}`,
    status: group.is_active ? 'Active' : 'Inactive', lifecycle: manage ? {active: group.is_active, label: group.name,
      onDisable: async ({reason}) => { await post(`/api/group/${group.id}`, {disable: {reason: 'admin', note: reason}}); await reload(); },
      onReactivate: async ({reason}) => { await post(`/api/group/${group.id}`, {reactivate: {note: reason}}); await reload(); }} : null,
    actions: [{label: 'Edit identity', capability: manage, run: () => editGroup(group, reload)},
      {label: 'Copy safe identifiers', run: () => navigator.clipboard.writeText(`group:${group.id}\nuuid:${group.uuid || ''}`)}], context: {group}});
  openInspector({title: `Group · ${group.name}`, content: h('div', {class: 'people-inspector'}, header, tabs, body), wide: true});
  await groupSection(ctx, group, active, body, reload);
}

export async function peoplePage(ctx, route) {
  const available = [['users', 'Users', capabilities(ctx).users], ['groups', 'Groups', capabilities(ctx).groups]].filter(([, , allowed]) => allowed);
  let active = available.some(([id]) => id === route) ? route : available[0]?.[0] || 'users'; let generation = 0;
  const root = h('div', {class: 'page people-page'}); let linkedInspectorOpened = false;
  async function render(term = '') {
    const mine = ++generation; const isUsers = active === 'users'; const caps = capabilities(ctx);
    const canManage = isUsers ? caps.manage_users : caps.manage_groups;
    root.replaceChildren(pageHeader('Identity & access', 'People', 'Manage users, groups, access, and authentication evidence.', [
      canManage ? h('button', {class: 'button primary', onclick: () => isUsers ? inviteUser(render) : newGroup(render)}, icon('plus'), isUsers ? 'Invite user' : 'New group') : null,
    ]));
    // † render() replaces the whole page, tab bar included, so a pending state
    // on the clicked tab would be detached before it painted — and a tab switch
    // SUPERSEDES rather than queues, so it must not be guarded at all. One key
    // for both tabs was worse than no key: `active` and the URL are set
    // synchronously here, then runAction found the key in flight and handed
    // back the *Users* render's promise, so render() never ran for Groups and
    // the screen kept showing users under a URL that said groups.
    //
    // render() already handles the race by itself: it takes `mine = ++generation`
    // and drops a superseded paint, and each pass builds a fresh `listBody`, so
    // loadInto's generation token cannot cross-write either. The affordance is
    // the skeleton loadInto paints below — the same answer metrics.js reaches.
    // See responsiveness.md: guard what queues, do not guard what supersedes.
    const tabs = h('nav', {class: 'tabs', 'aria-label': 'People views'}, ...available.map(([id, label]) => h('button', {class: id === active ? 'active' : '', onclick: () => { active = id; history.replaceState({}, '', routeHref(id)); return render(); }, text: label})));
    const input = h('input', {placeholder: `Search ${active}`, 'aria-label': `Search ${active}`, value: term});
    const panel = h('section', {class: 'panel'}, h('div', {class: 'panel-heading'}, h('div', {}, h('h2', {text: isUsers ? 'Users' : 'Groups'}), h('p', {text: 'Select a row to open the standard inspector.'})), h('label', {class: 'search'}, icon('search'), input)));
    // The table loads into a body node, never over the panel: the heading and
    // the search box beside it must survive the load and every re-search.
    const listBody = h('div', {});
    panel.append(listBody);
    root.append(tabs, panel);
    let timer; input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => render(input.value.trim()), 250); });
    await loadInto(listBody, async (current) => {
      const query = new URLSearchParams({size: '50', sort: isUsers ? '-last_activity' : 'name'}); if (term) query.set('search', term);
      const rows = (await apiEnvelope(`/api/${isUsers ? 'user' : 'group'}?${query}`)).items;
      if (!current() || mine !== generation) return;
      const columns = isUsers ? [
        {label: 'User', render: (row) => h('div', {class: 'identity'}, h('span', {class: 'avatar', text: initials(row).toUpperCase()}), h('div', {}, h('strong', {text: row.display_name || row.username}), h('small', {text: row.email || row.username})))},
        {label: 'Status', render: (row) => badge(row.is_active ? (row.requires_password_change ? 'Password change' : 'Active') : 'Inactive', row.is_active ? (row.requires_password_change ? 'warning' : 'success') : 'danger')},
        {label: 'Last activity', render: (row) => formatDate(row.last_activity)}, {label: '', render: () => icon('chevron')},
      ] : [
        {label: 'Group', render: (row) => h('strong', {text: row.name})}, {label: 'Kind', render: (row) => badge(row.kind || 'group')},
        {label: 'Parent', render: (row) => row.parent?.name || '—'}, {label: 'Status', render: (row) => badge(row.is_active ? 'Active' : 'Inactive', row.is_active ? 'success' : 'danger')}, {label: '', render: () => icon('chevron')},
      ];
      const open = (row) => isUsers ? openUser(ctx, row, render) : openGroup(ctx, row, render);
      listBody.replaceChildren(new TableView({columns, rows, empty: `No matching ${active}.`, onSelect: open}).render());
      const inspector = decodeRouteState().state.inspector;
      const linked = inspector && rows.find((row) => String(row.id) === String(inspector));
      if (linked && !linkedInspectorOpened) { linkedInspectorOpened = true; await open(linked); }
    }, {message: `Loading ${active}…`, retry: () => render(term)});
    return root;
  }
  await render(); return root;
}

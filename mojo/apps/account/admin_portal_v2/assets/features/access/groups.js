// Access ▸ Groups — v1's groups list and group inspector, ported whole.
//
// Same reads (`/api/group`, `/api/group/member`, `/api/group/apikey`), the same
// seven inspector sections including Members, API Keys, Permissions and the
// read-only Advanced JSON, and the same relationship-select parent picker.
//
// The API Keys section keeps its place here — a key belongs to its group — and
// shares its three controls with the Keys tab rather than restating them.

import {api, apiEnvelope, badge, FormView, formatDate, h, icon, TableView} from '../../core.js';
import {loadInto} from '../../components/actions.js';
import {modelHeader} from '../../components/model.js';
import {openInspector, openModal} from '../../components/overlays.js';
import {decodeRouteState} from '../../components/routes.js';
import {sectionTabs} from '../../components/views.js';
import {activityLinks, activityTabVisible, capabilities, detailGrid, initials,
  post} from './shared.js';
import {apiKeyActions, createApiKey} from './keys.js';

const GROUP_SECTIONS = [
  ['overview', 'Overview'], ['identity', 'Identity'], ['members', 'Members'],
  ['keys', 'API Keys'], ['permissions', 'Permissions'], ['activity', 'Activity'],
  ['advanced', 'Advanced'],
];

function editGroup(group, reload) {
  const form = new FormView({fields: [
    {name: 'name', label: 'Group name', required: true},
    {name: 'kind', label: 'Kind', required: true},
    {name: 'uuid', label: 'UUID'},
    {name: 'parent', label: 'Parent group', type: 'relationship', relationship: {
      endpoint: '/api/group', graph: 'basic', valuePath: 'id', labelPath: 'name',
      allowClear: true, filters: group.id ? {id__ne: group.id} : {},
    }},
  ], value: {...group, parent: group.parent || ''}, submitLabel: 'Save group identity',
  onSubmit: async (values) => {
    values.parent = values.parent || null;
    await post(`/api/group/${group.id}`, values); close(); await reload();
  }});
  const close = openModal({title: 'Edit group identity', content: form.render(), wide: true});
}

function newGroup(reload) {
  const form = new FormView({fields: [
    {name: 'name', label: 'Group name', required: true},
    {name: 'kind', label: 'Kind', required: true},
    {name: 'parent', label: 'Parent group', type: 'relationship',
      relationship: {endpoint: '/api/group', graph: 'basic', allowClear: true}},
  ], value: {kind: 'group'}, submitLabel: 'Create group', onSubmit: async (values) => {
    values.parent = values.parent || null;
    await post('/api/group', values); close(); await reload();
  }});
  const close = openModal({title: 'New group', content: form.render(), wide: true});
}

function addMember(group, reload) {
  const form = new FormView({fields: [
    {name: 'user', label: 'User', type: 'relationship', required: true,
      relationship: {endpoint: '/api/user', graph: 'basic', labelPath: 'display_name',
        detailPath: 'email'}},
    {name: 'role', label: 'Role', type: 'select',
      options: [{value: 'member', label: 'Member'}, {value: 'guest', label: 'Guest'}]},
  ], value: {role: 'member'}, submitLabel: 'Add member', onSubmit: async (values) => {
    await post('/api/group/member', {group: group.id, user: values.user, is_active: true,
      permissions: values.role === 'guest' ? {guest: true} : {}});
    close(); await reload();
  }});
  const close = openModal({title: `Add member to ${group.name}`, content: form.render(), wide: true});
}

async function groupSection(ctx, group, section, body, reload) {
  const caps = capabilities(ctx);
  if (section === 'overview') {
    body.replaceChildren(detailGrid([
      ['Group ID', group.id], ['UUID', group.uuid], ['Kind', group.kind],
      ['Parent', group.parent?.name || 'None'], ['Members', group.member_count ?? '—'],
      ['Last activity', formatDate(group.last_activity)],
    ]));
    return;
  }
  if (section === 'identity') {
    body.replaceChildren(detailGrid([['Name', group.name], ['Kind', group.kind],
      ['UUID', group.uuid], ['Parent', group.parent?.name || 'None']]),
    caps.manage_groups
      ? h('button', {class: 'button', onclick: () => editGroup(group, reload)}, 'Edit identity')
      : null);
    return;
  }
  if (section === 'members' || section === 'permissions') {
    await loadInto(body, async (current) => {
      const members = (await apiEnvelope(
        `/api/group/member?group=${encodeURIComponent(group.id)}&size=100`)).items;
      if (!current()) return;
      const rows = new TableView({columns: [
        {label: 'Member', render: (row) => h('div', {},
          h('strong', {text: row.user?.display_name || row.user?.username || `User ${row.user?.id}`}),
          h('small', {text: row.user?.email || ''}))},
        {label: 'Role', render: (row) => badge(row.permissions?.guest ? 'Guest' : 'Member')},
        {label: 'Status', render: (row) => badge(row.is_active ? 'Active' : 'Inactive',
          row.is_active ? 'success' : 'danger')},
        {label: 'Permissions', render: (row) => h('span', {text: Object.keys(row.permissions || {})
          .filter((key) => row.permissions[key] && key !== 'guest').join(', ') || 'None'})},
      ], rows: members, empty: 'No members are visible in this group.'}).render();
      body.replaceChildren(rows, section === 'members' && caps.manage_groups
        ? h('button', {class: 'button', onclick: () => addMember(group, reload)}, 'Add member')
        : null);
    }, {message: 'Loading members…', retry: () => groupSection(ctx, group, section, body, reload)});
    return;
  }
  if (section === 'keys') {
    await loadInto(body, async (current) => {
      const keys = (await apiEnvelope(
        `/api/group/apikey?group=${encodeURIComponent(group.id)}&size=100`)).items;
      if (!current()) return;
      body.replaceChildren(new TableView({columns: [
        {label: 'Key', render: (row) => h('div', {}, h('strong', {text: row.name}),
          h('small', {text: `ID ${row.id}`}))},
        {label: 'Status', render: (row) => badge(row.is_active ? 'Active' : 'Inactive',
          row.is_active ? 'success' : 'danger')},
        {label: 'Last used', render: (row) => formatDate(row.last_used)},
        {label: 'Actions', render: (row) => h('div', {class: 'inline-actions'},
          ...apiKeyActions(row, reload))},
      ], rows: keys, empty: 'No API keys exist for this group.'}).render(),
      caps.manage_api_keys
        ? h('button', {class: 'button', onclick: () => createApiKey(group, reload)}, 'Create API key')
        : null);
    }, {message: 'Loading API keys…',
      retry: () => groupSection(ctx, group, section, body, reload)});
    return;
  }
  if (section === 'activity') {
    body.replaceChildren(activityLinks(ctx, {type: 'group', id: group.id}));
    return;
  }
  body.replaceChildren(h('p', {class: 'muted',
    text: 'Advanced configuration is read-only here. Use the Advanced feature for raw JSON changes.'}),
  h('pre', {class: 'json-preview', text: JSON.stringify(group.metadata || {}, null, 2)}));
}

export async function openGroup(ctx, summary, reloadList) {
  let group = await api(`/api/group/${summary.id}`); let active = 'overview';
  const caps = capabilities(ctx);
  const sections = GROUP_SECTIONS
    .filter(([id]) => id !== 'keys' || caps.manage_api_keys)
    .filter(([id]) => id !== 'activity'
      || ['logs', 'events', 'incidents', 'tickets'].some((tab) => activityTabVisible(ctx, tab)));
  const body = h('div', {class: 'inspector-section'});
  const tabs = sectionTabs({items: sections.map(([id, label]) => ({id, label})), active,
    onChange: async (id) => {
      active = id;
      [...tabs.querySelectorAll('button')].forEach((button, index) =>
        button.classList.toggle('active', sections[index][0] === id));
      await groupSection(ctx, group, id, body, reload);
    }});
  const reload = async () => {
    group = await api(`/api/group/${group.id}`);
    await groupSection(ctx, group, active, body, reload);
    await reloadList();
  };
  const manage = caps.manage_groups;
  const header = modelHeader({iconName: 'users', avatar: initials(group), primary: group.name,
    secondary: `${group.kind} · ${group.uuid || 'UUID pending'}`,
    status: group.is_active ? 'Active' : 'Inactive',
    lifecycle: manage ? {active: group.is_active, label: group.name,
      onDisable: async ({reason}) => {
        await post(`/api/group/${group.id}`, {disable: {reason: 'admin', note: reason}});
        await reload();
      },
      onReactivate: async ({reason}) => {
        await post(`/api/group/${group.id}`, {reactivate: {note: reason}});
        await reload();
      }} : null,
    actions: [
      {label: 'Edit identity', capability: manage, run: () => editGroup(group, reload)},
      {label: 'Copy safe identifiers', done: 'Identifiers copied.',
        run: () => navigator.clipboard.writeText(`group:${group.id}\nuuid:${group.uuid || ''}`)},
    ], context: {group}});
  openInspector({title: `Group · ${group.name}`,
    content: h('div', {class: 'access-inspector'}, header, tabs, body), wide: true});
  await groupSection(ctx, group, active, body, reload);
}

export function groupsTab(ctx, actions) {
  const caps = capabilities(ctx);
  const listBody = h('div', {});
  let generation = 0; let linkedInspectorOpened = false;

  const load = async (term = '') => {
    const mine = ++generation;
    await loadInto(listBody, async (current) => {
      const query = new URLSearchParams({size: '50', sort: 'name'});
      if (term) query.set('search', term);
      const rows = (await apiEnvelope(`/api/group?${query}`)).items;
      if (!current() || mine !== generation) return;
      const open = (row) => openGroup(ctx, row, () => load(term));
      listBody.replaceChildren(new TableView({columns: [
        {label: 'Group', render: (row) => h('strong', {text: row.name})},
        {label: 'Kind', render: (row) => badge(row.kind || 'group')},
        {label: 'Parent', render: (row) => row.parent?.name || '—'},
        {label: 'Status', render: (row) => badge(row.is_active ? 'Active' : 'Inactive',
          row.is_active ? 'success' : 'danger')},
        {label: '', render: () => icon('chevron')},
      ], rows, empty: 'No matching groups.', onSelect: open}).render());
      const state = decodeRouteState().state;
      const wanted = state.group || state.inspector;
      const linked = wanted && rows.find((row) => String(row.id) === String(wanted));
      if (linked && !linkedInspectorOpened) { linkedInspectorOpened = true; await open(linked); }
    }, {message: 'Loading groups…', retry: () => load(term)});
  };

  if (caps.manage_groups) {
    actions.append(h('button', {class: 'button primary',
      onclick: () => newGroup(() => load(input.value.trim()))}, icon('plus'), 'New group'));
  }

  const input = h('input', {placeholder: 'Search groups', 'aria-label': 'Search groups'});
  let timer;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => load(input.value.trim()), 250);
  });

  const body = h('div', {class: 'access-tab'},
    h('section', {class: 'panel'},
      h('div', {class: 'panel-head'},
        h('div', {}, h('h2', {text: 'Groups'}),
          h('p', {text: 'Select a row to open the standard inspector.'})),
        h('label', {class: 'search'}, icon('search'), input)),
      listBody));
  body.dispose = () => clearTimeout(timer);
  load();
  return body;
}

import {api, badge, FormView, formatDate, h, icon, listData, openModal, pageHeader, TableView} from '../../core.js';

const PEOPLE_MODELS = {
  users: {
    label: 'Users', endpoint: '/api/user', capability: 'people',
    columns: [
      {label: 'Name', render: (r) => h('div', {class: 'identity'}, h('span', {class: 'avatar', text: (r.display_name || r.username || '?').slice(0, 2).toUpperCase()}), h('div', {}, h('strong', {text: r.display_name || r.username || 'Unnamed'}), h('small', {text: r.email || r.username || ''})))},
      {label: 'Status', render: (r) => badge(r.is_active === false ? 'Inactive' : 'Active', r.is_active === false ? 'danger' : 'success')},
      {label: 'Joined', render: (r) => formatDate(r.created)},
      {label: '', render: () => icon('chevron')},
    ],
    fields: [
      {name: 'display_name', label: 'Display name', required: true},
      {name: 'email', label: 'Email', type: 'email', required: true},
      {name: 'username', label: 'Username'},
      {name: 'password', label: 'Temporary password', type: 'password', autocomplete: 'new-password', help: 'Required for new password-based accounts.'},
      {name: 'is_active', label: 'Account is active', type: 'checkbox'},
    ],
  },
  groups: {
    label: 'Groups', endpoint: '/api/group', capability: 'groups',
    columns: [
      {label: 'Group', render: (r) => h('strong', {text: r.name || r.display_name || `Group ${r.id}`})},
      {label: 'Kind', render: (r) => badge(r.kind || 'standard')},
      {label: 'Status', render: (r) => badge(r.is_active === false ? 'Inactive' : 'Active', r.is_active === false ? 'danger' : 'success')},
      {label: '', render: () => icon('chevron')},
    ],
    fields: [
      {name: 'name', label: 'Group name', required: true},
      {name: 'is_active', label: 'Group is active', type: 'checkbox'},
    ],
  },
};

function editRecord(model, record, reload) {
  const isNew = !record?.id;
  const form = new FormView({fields: model.fields, value: record || {is_active: true}, submitLabel: isNew ? `Create ${model.label.slice(0, -1)}` : 'Save changes', onSubmit: async (values) => {
    const path = isNew ? model.endpoint : `${model.endpoint}/${record.id}`;
    await api(path, {method: isNew ? 'POST' : 'PUT', body: JSON.stringify(values)});
    close(); await reload();
  }});
  const close = openModal({title: `${isNew ? 'New' : 'Edit'} ${model.label.slice(0, -1)}`, subtitle: 'Only explicit fields are sent to the API.', content: form.render()});
}

export async function peoplePage(ctx, route) {
  let active = route === 'groups' ? 'groups' : 'users';
  const root = h('div', {class: 'page'});
  async function render() {
    const model = PEOPLE_MODELS[active];
    root.replaceChildren(pageHeader('Identity & access', 'People', 'Manage users and groups through the framework APIs.', [
      h('button', {class: 'button primary', onclick: () => editRecord(model, null, render)}, icon('plus'), `New ${model.label.slice(0, -1)}`),
    ]));
    const tabs = h('nav', {class: 'tabs', 'aria-label': 'People views'}, ...Object.entries(PEOPLE_MODELS).map(([key, value]) => h('button', {class: key === active ? 'active' : '', onclick: () => { active = key; history.replaceState({}, '', `#/${key}`); render(); }}, value.label)));
    const panel = h('section', {class: 'panel'}, h('div', {class: 'panel-heading'}, h('div', {}, h('h2', {text: model.label}), h('p', {text: 'Select a row to inspect or edit it.'})), h('label', {class: 'search'}, icon('search'), h('input', {placeholder: `Search ${model.label.toLowerCase()}`, 'aria-label': `Search ${model.label}`}))));
    root.append(tabs, panel);
    try {
      const rows = listData(await api(model.endpoint));
      let table = new TableView({columns: model.columns, rows, empty: `No ${model.label.toLowerCase()} are visible in your scope.`, onSelect: (row) => editRecord(model, row, render)}).render();
      panel.append(table);
      const input = panel.querySelector('.search input');
      input.addEventListener('input', () => {
        const term = input.value.toLowerCase();
        const filtered = rows.filter((row) => JSON.stringify(row).toLowerCase().includes(term));
        const replacement = new TableView({columns: model.columns, rows: filtered, onSelect: (row) => editRecord(model, row, render)}).render();
        table.replaceWith(replacement); table = replacement;
      });
    } catch (error) { panel.append(h('div', {class: 'error-state'}, icon('alert'), h('p', {text: error.message}))); }
  }
  await render(); return root;
}

import {api, badge, FormView, formatDate, h, icon, listData, openModal, pageHeader, TableView} from './core.js';

function card(title, value, copy, tone = '') {
  return h('article', {class: `kpi ${tone}`}, h('div', {class: 'kpi-label', text: title}), h('strong', {text: value}), h('p', {text: copy}));
}

export async function dashboardPage(ctx) {
  const caps = Object.values(ctx.capabilities).filter(Boolean).length;
  return h('div', {class: 'page'},
    pageHeader('Overview', 'System', 'A compact view of this MOJO control plane.'),
    h('section', {class: 'kpi-grid'},
      card('Framework', `v${ctx.version}`, 'Installed django-mojo version', 'accent'),
      card('Access', ctx.user.is_superuser ? 'Superuser' : 'Admin', 'Current control-plane role'),
      card('Groups', String(ctx.groups.length), 'Active memberships in scope'),
      card('Modules', String(caps), 'Available round-one modules')),
    h('section', {class: 'panel welcome'}, h('div', {class: 'panel-heading'}, h('div', {}, h('h2', {text: 'Round-one control center'}), h('p', {text: 'People management and WebApp deployment credentials are live. Fleet, network, operations, security, configuration, and charts follow on this foundation.'}))),
      h('div', {class: 'roadmap'}, ...['People', 'WebApps', 'Fleet', 'Network', 'Operations', 'Security', 'Configuration'].map((name, index) => h('div', {class: index < 2 ? 'roadmap-item live' : 'roadmap-item'}, h('span', {text: index < 2 ? 'Available' : 'Planned'}), h('strong', {text: name}))))));
}

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
      const table = new TableView({columns: model.columns, rows, empty: `No ${model.label.toLowerCase()} are visible in your scope.`, onSelect: (row) => editRecord(model, row, render)}).render();
      panel.append(table);
      const input = panel.querySelector('.search input');
      input.addEventListener('input', () => {
        const term = input.value.toLowerCase();
        const filtered = rows.filter((row) => JSON.stringify(row).toLowerCase().includes(term));
        table.replaceWith(new TableView({columns: model.columns, rows: filtered, onSelect: (row) => editRecord(model, row, render)}).render());
      });
    } catch (error) { panel.append(h('div', {class: 'error-state'}, icon('alert'), h('p', {text: error.message}))); }
  }
  await render(); return root;
}

function oneTimeSecret(webapp, result) {
  const secret = result.token;
  const content = h('div', {},
    h('div', {class: 'callout warning'}, icon('alert'), h('div', {}, h('strong', {text: 'Copy this value now'}), h('p', {text: 'It cannot be retrieved after this window closes. If it is lost, rotate the credential.'}))),
    h('label', {class: 'field'}, h('span', {text: 'GitHub Actions secret: MOJO_DEPLOY_KEY'}), h('textarea', {class: 'secret', readonly: true, rows: '4', text: secret})),
    h('button', {class: 'button primary', onclick: async (event) => { await navigator.clipboard.writeText(secret); event.currentTarget.textContent = 'Copied'; }}, icon('key'), 'Copy secret'),
    h('div', {class: 'command'}, h('code', {text: 'gh secret set MOJO_DEPLOY_KEY --repo YOUR_ORG/YOUR_REPO'})));
  openModal({title: `${webapp.slug} deployment key`, subtitle: 'The previous key is already inactive.', content});
}

async function credentialDialog(webapp, reload) {
  const payload = await api(`/api/edge/webapp/key_status?webapp=${encodeURIComponent(webapp.id)}`);
  const status = payload.status;
  const action = status.linked ? 'rotate' : 'mint';
  const actionLabel = status.linked ? 'Rotate key' : 'Create key';
  const confirm = h('input', {placeholder: webapp.slug, autocomplete: 'off'});
  const message = h('div', {class: 'form-message', role: 'alert'});
  const submit = h('button', {class: `button ${status.linked ? 'danger' : 'primary'}`, disabled: true}, icon('key'), actionLabel);
  confirm.addEventListener('input', () => { submit.disabled = confirm.value !== webapp.slug; });
  const content = h('div', {},
    h('div', {class: 'credential-status'}, h('div', {}, h('span', {text: 'GitHub Actions secret'}), h('strong', {text: 'MOJO_DEPLOY_KEY'})), badge(status.linked && status.active ? 'Active' : 'Not configured', status.linked && status.active ? 'success' : 'neutral')),
    status.linked ? h('dl', {class: 'details'}, h('div', {}, h('dt', {text: 'Created'}), h('dd', {text: formatDate(status.created)})), h('div', {}, h('dt', {text: 'Last used'}), h('dd', {text: formatDate(status.last_used)}))) : null,
    h('div', {class: 'callout'}, icon('alert'), h('p', {text: status.linked ? 'Rotation immediately disables the current key. Update GitHub before running another deployment.' : 'The token is restricted to registering releases for this WebApp.'})),
    h('label', {class: 'field'}, h('span', {text: `Type “${webapp.slug}” to confirm`}), confirm), message,
    h('div', {class: 'form-actions'}, submit,
      status.linked ? h('button', {class: 'button ghost', onclick: () => revokeCredential(webapp, reload)}, 'Revoke instead') : null));
  const close = openModal({title: `${webapp.slug} credential`, subtitle: 'Explicit, audited, and reveal-once.', content, danger: status.linked});
  submit.addEventListener('click', async () => {
    submit.disabled = true;
    try {
      const result = await api('/api/edge/webapp/link_key', {method: 'POST', body: JSON.stringify({webapp: webapp.id, action, operation_id: crypto.randomUUID()})});
      close(); await reload(); oneTimeSecret(webapp, result);
    } catch (error) { message.textContent = error.message; submit.disabled = false; }
  });
}

function revokeCredential(webapp, reload) {
  const confirm = h('input', {placeholder: webapp.slug, autocomplete: 'off'});
  const message = h('div', {class: 'form-message', role: 'alert'});
  const button = h('button', {class: 'button danger', disabled: true}, 'Revoke deployment key');
  confirm.addEventListener('input', () => { button.disabled = confirm.value !== webapp.slug; });
  const close = openModal({title: `Revoke ${webapp.slug} key?`, subtitle: 'Future releases will fail until a new key is created.', danger: true, content: h('div', {}, h('label', {class: 'field'}, h('span', {text: `Type “${webapp.slug}” to confirm`}), confirm), message, h('div', {class: 'form-actions'}, button))});
  button.addEventListener('click', async () => {
    button.disabled = true;
    try { await api('/api/edge/webapp/revoke_key', {method: 'POST', body: JSON.stringify({webapp: webapp.id, operation_id: crypto.randomUUID()})}); close(); await reload(); }
    catch (error) { message.textContent = error.message; button.disabled = false; }
  });
}

export async function webappsPage() {
  const root = h('div', {class: 'page'});
  async function render() {
    root.replaceChildren(pageHeader('Deployments', 'WebApps', 'Release destinations and their GitHub deployment credentials.'));
    const panel = h('section', {class: 'panel'}, h('div', {class: 'panel-heading'}, h('div', {}, h('h2', {text: 'Applications'}), h('p', {text: 'Manage MOJO_DEPLOY_KEY without exposing unrelated API keys.'}))));
    root.append(panel);
    try {
      const rows = listData(await api('/api/edge/webapp'));
      panel.append(new TableView({columns: [
        {label: 'WebApp', render: (r) => h('div', {}, h('strong', {text: r.slug}), h('small', {class: 'mono', text: `#${r.id}`}))},
        {label: 'Current release', render: (r) => r.current_release ? badge(r.current_release.version || r.current_release.id, 'success') : badge('No release')},
        {label: 'Created', render: (r) => formatDate(r.created)},
        {label: '', render: () => h('button', {class: 'button compact'}, icon('key'), 'Manage key')},
      ], rows, empty: 'No WebApps are visible in your scope.', onSelect: (row) => credentialDialog(row, render)}).render());
    } catch (error) { panel.append(h('div', {class: 'error-state'}, icon('alert'), h('p', {text: error.message}))); }
  }
  await render(); return root;
}

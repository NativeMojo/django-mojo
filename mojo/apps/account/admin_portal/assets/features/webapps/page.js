import {api, badge, formatDate, h, icon, listData, openModal, pageHeader, TableView} from '../../core.js';

function oneTimeSecret(webapp, result, returnFocus) {
  let secret = result.token;
  const secretField = h('textarea', {class: 'secret', readonly: true, rows: '4', text: secret});
  const content = h('div', {},
    h('div', {class: 'callout warning'}, icon('alert'), h('div', {}, h('strong', {text: 'Copy this value now'}), h('p', {text: 'It cannot be retrieved after this window closes. If it is lost, rotate the credential.'}))),
    h('label', {class: 'field'}, h('span', {text: 'GitHub Actions secret: MOJO_DEPLOY_KEY'}), secretField),
    h('button', {class: 'button primary', onclick: async (event) => { await navigator.clipboard.writeText(secret); event.currentTarget.textContent = 'Copied'; }}, icon('key'), 'Copy secret'),
    h('div', {class: 'command'}, h('code', {text: 'gh secret set MOJO_DEPLOY_KEY --repo YOUR_ORG/YOUR_REPO'})));
  openModal({title: `${webapp.slug} deployment key`, subtitle: 'The previous key is already inactive.', content, returnFocus, onClose: () => {
    secretField.value = ''; secretField.textContent = ''; secret = ''; result.token = null;
  }});
}

async function credentialDialog(webapp, reload) {
  const payload = await api(`/api/edge/webapp/key_status?webapp=${encodeURIComponent(webapp.id)}`);
  const status = payload.status;
  const action = status.linked ? 'rotate' : 'mint';
  const actionLabel = status.linked ? 'Rotate key' : status.last_action === 'revoke' ? 'Create new key' : 'Create key';
  const confirm = h('input', {placeholder: webapp.slug, autocomplete: 'off'});
  const message = h('div', {class: 'form-message', role: 'alert'});
  const submit = h('button', {class: `button ${status.linked ? 'danger' : 'primary'}`, disabled: true}, icon('key'), actionLabel);
  confirm.addEventListener('input', () => { submit.disabled = confirm.value !== webapp.slug; });
  const content = h('div', {},
    h('div', {class: 'credential-status'}, h('div', {}, h('span', {text: 'GitHub Actions secret'}), h('strong', {text: 'MOJO_DEPLOY_KEY'})), badge(status.linked && status.active ? 'Active' : status.last_action === 'revoke' ? 'Revoked' : 'Not configured', status.linked && status.active ? 'success' : status.last_action === 'revoke' ? 'warning' : 'neutral')),
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
      close(); await reload();
      const returnFocus = document.querySelector(`[data-webapp-key="${webapp.id}"]`);
      oneTimeSecret(webapp, result, returnFocus);
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

export async function webappsPage(ctx) {
  const root = h('div', {class: 'page'});
  async function render() {
    root.replaceChildren(pageHeader('Deployments', 'WebApps', 'Release destinations and their GitHub deployment credentials.'));
    const panel = h('section', {class: 'panel'}, h('div', {class: 'panel-heading'}, h('div', {}, h('h2', {text: 'Applications'}), h('p', {text: 'Manage MOJO_DEPLOY_KEY without exposing unrelated API keys.'}))));
    root.append(panel);
    try {
      const rows = listData(await api('/api/edge/webapp'));
      const statuses = new Map();
      await Promise.all(rows.map(async (row) => {
        try {
          const payload = await api(`/api/edge/webapp/key_status?webapp=${encodeURIComponent(row.id)}`);
          statuses.set(row.id, payload.status);
        } catch (_) { statuses.set(row.id, null); }
      }));
      panel.append(new TableView({columns: [
        {label: 'WebApp', render: (r) => h('div', {}, h('strong', {text: r.slug}), h('small', {class: 'mono', text: `#${r.id}`}))},
        {label: 'Current release', render: (r) => r.current_release ? badge(r.current_release.version || r.current_release.id, 'success') : badge('No release')},
        {label: 'Deploy key', render: (r) => { const status = statuses.get(r.id); return status?.linked && status?.active ? badge('Active', 'success') : status?.last_action === 'revoke' ? badge('Revoked', 'warning') : badge('Missing', 'neutral'); }},
        {label: 'Created', render: (r) => formatDate(r.created)},
        {label: '', render: (row) => ctx.capabilities.manage_webapps ? h('button', {class: 'button compact', 'data-webapp-key': row.id}, icon('key'), 'Manage key') : null},
      ], rows, empty: 'No WebApps are visible in your scope.', onSelect: ctx.capabilities.manage_webapps ? (row) => credentialDialog(row, render) : null}).render());
    } catch (error) { panel.append(h('div', {class: 'error-state'}, icon('alert'), h('p', {text: error.message}))); }
  }
  await render(); return root;
}

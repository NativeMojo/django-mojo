// Credentials — verified provider access, without exposing stored secrets.
//
// Ported from v1's advanced/page.js `credentialsPage` and `credentialDialog`,
// including the scrub-on-close handling: the API key and secret are submitted
// once, verified, encrypted server-side and never shown again, and every copy
// this page held (the inputs, the form values, the request payload) is wiped
// when the dialog closes or the submit finishes, whichever comes first.
import {FormView, h, icon, openModal, TableView} from '../../core.js';
import {loadInto} from '../../components/actions.js';
import {
  groupFields, loadCredentials, postOnce, providerMutation, statusBadge, tablePanel,
} from '../../components/network.js';

function credentialDialog(ctx, existing, reload) {
  const fields = [groupFields(ctx),
    ...(!existing ? [{name: 'provider', label: 'Provider', type: 'select', required: true, options: [{value: 'godaddy', label: 'GoDaddy'}], placeholder: 'Choose a provider'}] : []),
    {name: 'name', label: 'Credential name', required: true},
    {name: 'api_key', label: 'API key', type: 'password', required: true, autocomplete: 'new-password'},
    {name: 'api_secret', label: 'API secret', type: 'password', required: true, autocomplete: 'new-password'},
  ];
  const groupId = existing?.group?.id || existing?.group || '';
  const value = {group: groupId, provider: existing?.provider || 'godaddy', name: existing?.name || ''};
  let secretValues = null; let secretPayload = null;
  const form = new FormView({fields, value, submitLabel: existing ? 'Rotate and verify' : 'Link and verify', onSubmit: async (values, inputs) => {
    const payload = {...values, provider: existing?.provider || values.provider};
    secretValues = values; secretPayload = payload;
    if (existing) payload.credential = existing.id;
    const baseline = existing?.modified;
    try {
      await providerMutation(`credential:${existing?.id || `${values.group}:${values.name}`}`,
        () => postOnce('/api/dnsman/credential/link', payload),
        loadCredentials,
        (observed) => {
          const row = existing ? observed.find((item) => item.id === existing.id) : observed.find((item) => item.name === values.name && item.provider === payload.provider);
          if (row?.verified && (!existing || row.modified !== baseline)) return 'applied';
          return row ? 'unconfirmed' : 'not-applied';
        });
      close(); await reload();
    } finally {
      payload.api_key = ''; payload.api_secret = ''; values.api_key = ''; values.api_secret = '';
      if (inputs.api_key) inputs.api_key.value = ''; if (inputs.api_secret) inputs.api_secret.value = '';
      secretValues = null; secretPayload = null;
    }
  }});
  const content = form.render();
  const scrub = () => {
    content.querySelectorAll('input[type="password"]').forEach((input) => { input.value = ''; });
    if (secretValues) { secretValues.api_key = ''; secretValues.api_secret = ''; }
    if (secretPayload) { secretPayload.api_key = ''; secretPayload.api_secret = ''; }
    secretValues = null; secretPayload = null;
  };
  const close = openModal({title: existing ? `Rotate ${existing.name}` : 'Link DNS credential', subtitle: 'Provider secrets are submitted once, verified, encrypted, and never shown again.', content, danger: Boolean(existing), onClose: scrub});
}

export async function credentialsTab(ctx, actions) {
  const root = h('div', {class: 'domains-tab'});
  async function render() {
    actions.replaceChildren(...[
      ctx.capabilities.manage_network ? h('button', {class: 'button primary', onclick: () => credentialDialog(ctx, null, render)}, icon('key'), 'Link credential') : null,
    ].filter(Boolean));
    const panel = tablePanel('DNS provider credentials', 'Only masked suffixes and verification metadata are returned by the API.');
    root.replaceChildren(panel);
    // Into a body node, not the panel: the panel carries the heading this
    // loading state would otherwise replace.
    const body = h('div', {}); panel.append(body);
    await loadInto(body, async (current) => {
      const rows = await loadCredentials();
      if (!current()) return;
      body.replaceChildren(new TableView({rows, empty: 'No DNS credentials are linked.', onSelect: ctx.capabilities.manage_network ? (row) => credentialDialog(ctx, row, render) : null, columns: [
        {label: 'Credential', render: (row) => h('div', {}, h('strong', {text: row.name}), h('small', {text: row.provider}))},
        {label: 'Owner', render: (row) => row.group?.name || 'Platform'},
        {label: 'Verified', render: (row) => statusBadge(row.verified ? 'verified' : 'failed')},
        {label: 'Key', render: (row) => h('span', {class: 'mono', text: row.api_key_masked || 'Hidden'})},
        {label: 'Domains', render: (row) => String(row.domain_count || 0)},
      ]}).render());
    }, {message: 'Loading credentials…', retry: render});
  }
  await render();
  return root;
}

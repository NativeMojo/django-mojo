import {api, apiOnce, badge, h, icon, openModal, pageHeader} from '../../core.js';
import {confirmAction, openBusy} from '../../components/overlays.js';
import {routeHref} from '../../components/routes.js';
import {errorState, skeletonState} from '../../components/views.js';

const SOURCE_TONE = {database: 'success', duplicate_override: 'danger', invalid: 'danger', default: 'neutral'};

function shown(value) {
  if (value && typeof value === 'object' && Object.keys(value).length === 1 && 'configured' in value) return value.configured ? 'Configured' : 'Not configured';
  if (typeof value === 'boolean') return value ? 'Enabled' : 'Disabled';
  if (value == null || value === '') return 'Not configured';
  if (Array.isArray(value)) return value.length ? value.join(' · ') : 'None configured';
  if (typeof value === 'object') return 'Configured';
  return String(value);
}

function ownerHref(row) {
  if (!row.owner_route) return null;
  const [route, query = ''] = row.owner_route.split('?', 2);
  return routeHref(route, Object.fromEntries(new URLSearchParams(query)));
}

function technical(row) {
  return h('details', {class: 'settings-technical'},
    h('summary', {text: 'Technical details'}),
    h('dl', {class: 'settings-details'},
      h('div', {}, h('dt', {text: 'Key'}), h('dd', {class: 'mono', text: row.key})),
      h('div', {}, h('dt', {text: 'Scope'}), h('dd', {text: row.scope})),
      h('div', {}, h('dt', {text: 'Resolver'}), h('dd', {text: row.resolver})),
      h('div', {}, h('dt', {text: 'Raw value'}), h('dd', {text: row.raw_semantics})),
      h('div', {}, h('dt', {text: 'Effective value'}), h('dd', {text: row.effective_semantics})),
      row.constraints ? h('div', {}, h('dt', {text: 'Constraints'}), h('dd', {text: row.constraints})) : null,
      row.ignored_database_override ? h('div', {}, h('dt', {text: 'Ignored override'}), h('dd', {text: 'A database row exists but this value is deployment-only.'})) : null));
}

function tokenEditor(label, values) {
  const list = h('div', {class: 'settings-token-list'});
  let current = [...new Set((values || []).map((value) => String(value).trim()).filter(Boolean))];
  const input = h('input', {autocomplete: 'off', placeholder: `Add ${label.toLowerCase()}`});
  const render = () => list.replaceChildren(...current.map((value) => h('span', {class: 'settings-token'},
    h('span', {text: value}), h('button', {type: 'button', 'aria-label': `Remove ${value}`, onclick: () => {
      current = current.filter((item) => item !== value); render(); input.dispatchEvent(new Event('change', {bubbles: true}));
    }}, '×'))));
  const add = () => { const value = input.value.trim(); if (!value || current.includes(value)) return; current.push(value); input.value = ''; render(); input.dispatchEvent(new Event('change', {bubbles: true})); };
  input.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ',') { event.preventDefault(); add(); } });
  const node = h('div', {class: 'settings-token-editor'}, list, h('div', {class: 'settings-token-add'}, input,
    h('button', {class: 'button compact', type: 'button', onclick: add}, 'Add')));
  render();
  return {node, value: () => current};
}

function openCatalogEditor(row, mutate) {
  const initial = row.effective_value;
  const control = row.value_type === 'boolean'
    ? h('input', {type: 'checkbox', checked: initial === true})
    : h('input', {type: 'url', value: typeof initial === 'string' ? initial : '', autocomplete: 'url', placeholder: 'https://app.your-domain.com'});
  const message = h('div', {class: 'form-message', role: 'alert'});
  const save = h('button', {class: 'button primary', type: 'button', disabled: true}, 'Save change');
  const dirty = () => {
    const value = row.value_type === 'boolean' ? control.checked : control.value.trim();
    let invalid = false;
    if (row.value_type === 'origin' && value) {
      try { const url = new URL(value); invalid = url.protocol !== 'https:' || !url.hostname.includes('.') || Boolean(url.username || url.password || url.search || url.hash) || !['', '/'].includes(url.pathname) || !['', '443'].includes(url.port); }
      catch (_) { invalid = true; }
      control.setCustomValidity(invalid ? 'Use one public HTTPS hostname origin.' : '');
      message.textContent = invalid ? 'Use one public HTTPS hostname origin, without a path or credentials.' : '';
    }
    save.disabled = invalid || value === initial || value === '';
  };
  control.addEventListener('input', dirty); control.addEventListener('change', dirty);
  let close;
  save.addEventListener('click', async () => {
    const value = row.value_type === 'boolean' ? control.checked : control.value.trim();
    save.disabled = true; message.textContent = '';
    try { await mutate({action: 'set', key: row.key, value}); close(); }
    catch (error) { message.textContent = error.message; dirty(); }
  });
  close = openModal({title: row.label, subtitle: row.description, content: h('div', {},
    h('label', {class: row.value_type === 'boolean' ? 'check-field' : 'field'},
      row.value_type === 'boolean' ? control : h('span', {text: 'Public HTTPS origin'}),
      row.value_type === 'boolean' ? h('span', {text: row.label}) : control,
      row.value_type === 'boolean' ? null : h('small', {text: row.constraints})),
    message, h('div', {class: 'form-actions'}, save))});
}

function openAuthEditor(row, mutateOwner) {
  const value = row.effective_value || {}; const theme = value.theme || {};
  const title = h('input', {value: theme.app_title || '', autocomplete: 'off'});
  const accent = h('input', {value: theme.accent_color || '#6384ff', type: 'color'});
  const loginNames = ['password', 'sms', 'passkey', 'magic', 'google', 'apple', 'github'];
  const registrationNames = ['password', 'google', 'apple', 'github'];
  const checks = (names, selected, passwordLocked = false) => names.map((name) => [name, h('input', {
    type: 'checkbox', checked: selected.has(name), disabled: passwordLocked && name === 'password',
  })]);
  const login = checks(loginNames, new Set(value.login?.methods || loginNames), true);
  const registration = checks(registrationNames, new Set(value.registration?.methods || registrationNames));
  const enabled = h('input', {type: 'checkbox', checked: value.registration?.enabled !== false});
  const prompt = h('select', {}, ...['off', 'optional', 'required'].map((name) => h('option', {value: name, text: name})));
  prompt.value = value.registration?.passkey_prompt || 'off';
  const message = h('div', {class: 'form-message', role: 'alert'}); const save = h('button', {class: 'button primary', type: 'button', disabled: true}, 'Save authentication settings');
  const payload = () => {
    const selected = (rows) => rows.filter(([, input]) => input.checked).map(([name]) => name);
    return {'theme.app_title': title.value.trim(), 'theme.accent_color': accent.value,
      'login.methods': selected(login), 'registration.enabled': enabled.checked,
      'registration.methods': selected(registration), 'registration.passkey_prompt': prompt.value};
  };
  const original = JSON.stringify(payload());
  const dirty = () => { save.disabled = JSON.stringify(payload()) === original; };
  [title, accent, enabled, prompt, ...login.map(([, input]) => input), ...registration.map(([, input]) => input)].forEach((input) => {
    input.addEventListener('input', dirty); input.addEventListener('change', dirty);
  });
  let close;
  save.addEventListener('click', async () => {
    save.disabled = true; message.textContent = '';
    try {
      await mutateOwner({auth: payload()});
      close();
    } catch (error) { message.textContent = error.message; dirty(); }
  });
  close = openModal({title: 'Brand & authentication', subtitle: 'Password remains enabled for administrative recovery.', wide: true, content: h('div', {},
    h('div', {class: 'field-grid'}, h('label', {class: 'field'}, h('span', {text: 'Application title'}), title), h('label', {class: 'field'}, h('span', {text: 'Accent color'}), accent)),
    h('fieldset', {class: 'settings-methods'}, h('legend', {text: 'Login methods'}), ...login.map(([name, input]) => h('label', {class: 'check-field'}, input, h('span', {text: name})))),
    h('fieldset', {class: 'settings-methods'}, h('legend', {text: 'Registration'}), h('label', {class: 'check-field'}, enabled, h('span', {text: 'Registration enabled'})), ...registration.map(([name, input]) => h('label', {class: 'check-field'}, input, h('span', {text: name}))), h('label', {class: 'field'}, h('span', {text: 'Passkey prompt'}), prompt)),
    message, h('div', {class: 'form-actions'}, save))});
}

function openTopologyEditor(row, mutateOwner) {
  const value = row.effective_value || {}; const nodes = tokenEditor('Node', value.nodes); const pools = tokenEditor('Pool', value.pools);
  const message = h('div', {class: 'form-message', role: 'alert'}); const save = h('button', {class: 'button primary', type: 'button', disabled: true}, 'Save expected fleet');
  const original = JSON.stringify({nodes: nodes.value(), pools: pools.value()});
  const dirty = () => { save.disabled = JSON.stringify({nodes: nodes.value(), pools: pools.value()}) === original; };
  nodes.node.addEventListener('change', dirty); pools.node.addEventListener('change', dirty);
  let close;
  save.addEventListener('click', async () => { save.disabled = true; message.textContent = '';
    try { await mutateOwner({edge_topology: {nodes: nodes.value(), pools: pools.value()}}); close(); }
    catch (error) { message.textContent = error.message; dirty(); }
  });
  close = openModal({title: 'Expected fleet', subtitle: 'Add each expected node and pool explicitly.', content: h('div', {},
    h('label', {class: 'field'}, h('span', {text: 'Nodes'}), nodes.node), h('label', {class: 'field'}, h('span', {text: 'Pools'}), pools.node),
    message, h('div', {class: 'form-actions'}, save))});
}

function providerPanel(setup, configure, testProviders) {
  if (!setup) return null;
  const revision = setup.pending_restart
    ? `Published ${setup.published_revision?.slice(0, 8) || 'revision'}; waiting for config-sync restart.`
    : setup.loaded_revision ? `Fleet loaded revision ${setup.loaded_revision.slice(0, 8)}.` : 'No fleet override is loaded yet.';
  const open = () => {
    const geo = setup.geoip || {}; const sms = setup.sms || {};
    const fleetControls = (setup.fleet_fields || []).map((field) => {
      const value = geo[field.key];
      const input = field.value_type === 'boolean'
        ? h('input', {type: 'checkbox', checked: value === true})
        : h('input', {type: field.value_type === 'origin' ? 'url' : 'text', value: field.value_type === 'list' ? (value || []).join(', ') : (value ?? ''), autocomplete: 'off'});
      const node = field.value_type === 'boolean'
        ? h('label', {class: 'check-field'}, input, h('span', {text: field.label}))
        : h('label', {class: 'field'}, h('span', {text: field.label}), input,
          field.constraints ? h('small', {text: field.constraints}) : null);
      return {field, input, node};
    });
    const geoKey = h('input', {type: 'password', value: '', autocomplete: 'new-password', placeholder: geo.GEOIP_API_KEY_MOJO_CONFIGURED ? 'Configured — leave blank to keep' : 'API key'});
    const clearGeoKey = h('input', {type: 'checkbox'});
    const smsUrl = h('input', {type: 'url', value: sms.remote_url || '', autocomplete: 'url', placeholder: 'https://sms.example.com'});
    const smsKey = h('input', {type: 'password', value: '', autocomplete: 'new-password', placeholder: sms.api_key_configured ? 'Configured — leave blank to keep' : 'Remote API key'});
    const clearSmsKey = h('input', {type: 'checkbox'}); const testMode = h('input', {type: 'checkbox', checked: sms.test_mode === true});
    const message = h('div', {class: 'form-message', role: 'alert'});
    const testButton = h('button', {class: 'button', type: 'button'}, 'Test connections');
    const save = h('button', {class: 'button primary', type: 'button'}, 'Publish provider configuration');
    const payload = () => {
      const fleet = Object.fromEntries(fleetControls.map(({field, input}) => [field.key,
        field.value_type === 'boolean' ? input.checked : field.value_type === 'list'
          ? input.value.split(',').map((value) => value.trim()).filter(Boolean) : input.value.trim()]));
      return {expected_revision: setup.configuration_revision || null,
        geoip: {...fleet, GEOIP_API_KEY_MOJO: geoKey.value || null, clear_api_key: clearGeoKey.checked},
        sms: {remote_url: smsUrl.value.trim(), api_key: smsKey.value || null, clear_api_key: clearSmsKey.checked, test_mode: testMode.checked}};
    };
    let close;
    testButton.addEventListener('click', async () => {
      testButton.disabled = true; message.textContent = '';
      try { const result = await testProviders(payload()); message.textContent = result.success ? 'GeoIP and SMS connections verified.' : Object.values(result.results || {}).filter((item) => !item.success).map((item) => item.message).join(' '); }
      catch (error) { message.textContent = error.message; }
      finally { testButton.disabled = false; }
    });
    save.addEventListener('click', async () => {
      save.disabled = true; message.textContent = '';
      try { await configure(payload()); geoKey.value = ''; smsKey.value = ''; close(); } catch (error) { message.textContent = error.message; save.disabled = false; }
    });
    close = openModal({title: 'Mojo GeoIP and SMS', subtitle: 'Static GeoIP values publish fleet-wide; encrypted API keys stay in the database.', wide: true, content: h('div', {},
      h('h3', {text: 'GeoIP'}), h('div', {class: 'field-grid'}, ...fleetControls.map(({node}) => node)),
      h('label', {class: 'field'}, h('span', {text: 'MojoVerify GeoIP API key'}), geoKey),
      h('label', {class: 'check-field'}, clearGeoKey, h('span', {text: 'Clear the configured GeoIP API key'})),
      h('h3', {text: 'Remote SMS'}), h('div', {class: 'field-grid'},
        h('label', {class: 'field'}, h('span', {text: 'Remote django-mojo URL'}), smsUrl),
        h('label', {class: 'field'}, h('span', {text: 'Remote API key'}), smsKey)),
      h('label', {class: 'check-field'}, clearSmsKey, h('span', {text: 'Clear the configured SMS API key'})),
      h('label', {class: 'check-field'}, testMode, h('span', {text: 'SMS test mode'})),
      message, h('div', {class: 'form-actions'}, testButton, save))});
  };
  return h('section', {class: `settings-provider-panel ${setup.pending_restart ? 'is-pending' : ''}`},
    h('div', {}, h('h2', {text: 'Mojo providers'}), h('p', {text: revision}),
      !setup.available ? h('p', {class: 'settings-guidance', text: 'Fleet publishing is not configured. Set the Admin fleet bucket, prefix, delegated keys, KMS key, and config-sync restart posture in deployment settings.'}) : null),
    h('button', {class: 'button primary compact', type: 'button', disabled: !setup.available, onclick: open}, setup.published_revision ? 'Edit providers' : 'Configure providers'));
}

function card(row, actions) {
  const href = ownerHref(row); let action = null;
  if (row.duplicate_override && row.can_clear) action = h('button', {class: 'button danger compact', onclick: () => actions.clear(row, true)}, 'Clear conflicts');
  else if (row.can_write) action = h('button', {class: 'button compact', onclick: () => openCatalogEditor(row, actions.mutate)}, row.source === 'database' ? 'Edit' : 'Set');
  else if (row.can_owner_edit && row.key === 'AUTH_CONFIG') action = h('button', {class: 'button compact', onclick: () => openAuthEditor(row, actions.owner)}, 'Configure');
  else if (row.can_owner_edit && row.key === 'EDGE_EXPECTED_TOPOLOGY') action = h('button', {class: 'button compact', onclick: () => openTopologyEditor(row, actions.owner)}, 'Configure');
  else if (href) action = h('a', {class: 'button compact', href}, row.change_behavior === 'setup' ? 'Configure' : 'Open owner');
  return h('article', {class: `settings-card ${row.duplicate_override ? 'has-error' : ''}`, 'data-setting-key': row.key},
    h('div', {class: 'settings-card-main'}, h('div', {}, h('h3', {text: row.label}), h('p', {text: row.description})),
      h('div', {class: 'settings-card-actions'}, badge(row.source.replaceAll('_', ' '), SOURCE_TONE[row.source] || 'neutral'), action)),
    h('div', {class: 'settings-value', text: row.duplicate_override ? 'Conflicting database overrides' : shown(row.effective_value)}),
    row.writable === 'catalog' && row.source === 'database' && row.can_clear ? h('button', {class: 'settings-clear', onclick: () => actions.clear(row, false)}, 'Clear database override') : null,
    row.writable === 'none' && row.change_behavior === 'deploy' ? h('p', {class: 'settings-guidance', text: `Change ${row.key} in Django production settings and deploy.`}) : null,
    technical(row));
}

export async function settingsPage(ctx, signal) {
  const root = h('div', {class: 'page'}, skeletonState('Loading Settings', 7));
  let statusText = '';
  async function load() {
    root.replaceChildren(skeletonState('Loading Settings', 7));
    try {
      const report = await api('/api/account/admin/settings', {signal});
      const search = h('input', {type: 'search', placeholder: 'Search settings', 'aria-label': 'Search settings'});
      let active = 'All'; const chips = h('div', {class: 'settings-categories', 'aria-label': 'Settings categories'});
      const grid = h('div', {class: 'settings-grid'}); const empty = h('p', {class: 'settings-empty', text: 'No settings match this search.'});
      const mutate = async (payload) => { const busy = openBusy({title: 'Saving setting…', detail: 'The database override is being applied.'});
        try { await apiOnce('/api/account/admin/settings', {method: 'POST', body: JSON.stringify(payload)}); statusText = `${payload.key} saved.`; await load(); }
        finally { busy.close(); }
      };
      const owner = async (payload) => { const busy = openBusy({title: 'Saving configuration…', detail: 'The typed owner is validating this change.'});
        try { await apiOnce('/api/account/admin/advanced/settings', {method: 'POST', body: JSON.stringify(payload)}); statusText = 'Configuration saved.'; await load(); }
        finally { busy.close(); }
      };
      const configureProviders = async (providers) => { const busy = openBusy({title: 'Publishing fleet configuration…', detail: 'Writing the encrypted S3 override and encrypted database credentials.'});
        try { await apiOnce('/api/account/admin/settings', {method: 'POST', body: JSON.stringify({action: 'configure_providers', providers})}); statusText = 'Provider configuration published. Config-sync will roll the fleet restart.'; await load(); }
        finally { busy.close(); }
      };
      const testProviders = async (providers) => apiOnce('/api/account/admin/settings', {method: 'POST', body: JSON.stringify({action: 'test_providers', providers})});
      const clear = async (row, conflicts) => { const answer = await confirmAction({title: conflicts ? 'Clear conflicting overrides?' : 'Clear database override?', copy: conflicts ? `This removes every conflicting global ${row.key} row and reveals the deployment/default value.` : `This removes the global ${row.key} override and reveals the deployment/default value.`, confirmLabel: conflicts ? 'Clear conflicts' : 'Clear override', danger: true});
        if (answer.confirmed) await mutate({action: 'clear', key: row.key});
      };
      const render = () => {
        const term = search.value.trim().toLowerCase(); const rows = report.entries.filter((row) => (active === 'All' || row.section === active) && (!term || `${row.label} ${row.description} ${row.key}`.toLowerCase().includes(term)));
        grid.replaceChildren(...rows.map((row) => card(row, {mutate, owner, clear})), ...(rows.length ? [] : [empty]));
      };
      const categories = ['All', ...(report.sections || [])];
      const renderChips = () => chips.replaceChildren(...categories.map((name) => h('button', {type: 'button', class: name === active ? 'active' : '', 'aria-pressed': name === active ? 'true' : 'false', onclick: () => { active = name; renderChips(); render(); }}, name)));
      search.addEventListener('input', render); renderChips(); render();
      root.replaceChildren(pageHeader('Configuration', 'Settings', 'Understand effective configuration and change only the runtime values django-mojo supports.'),
        ...(report.setup_incomplete ? [h('div', {class: 'callout'}, icon('alert'), h('div', {}, h('strong', {text: 'Installation setup is incomplete'}), h('p', {text: 'Finish the required installation choices before tuning ongoing configuration.'})), h('a', {class: 'button compact', href: routeHref('setup')}, 'Continue Setup'))] : []),
        ...(statusText ? [h('div', {class: 'settings-success', role: 'status', text: statusText})] : []),
        providerPanel(report.provider_setup, configureProviders, testProviders),
        h('div', {class: 'settings-toolbar'}, h('label', {class: 'search'}, icon('search'), search), chips), grid);
    } catch (error) { if (error?.name !== 'AbortError' && error?.code !== 'fresh_auth_required') root.replaceChildren(errorState(error, load)); }
  }
  await load(); return root;
}

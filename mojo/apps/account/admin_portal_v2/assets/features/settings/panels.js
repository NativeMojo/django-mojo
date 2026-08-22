// One topic at a time.
//
// v1's settings panels, ported whole. Each panel owns exactly one topic, saves
// alone, and fails alone; technical detail lives down here, because the list is
// for reading and a panel is for changing one thing. The only v2 change is the
// chrome: the drill-in wears the same back pill every sub-page in this portal
// wears, instead of v1's small "← Settings" text link.

import {h} from '../../core.js';
import {backPill} from '../../app.js';
import {runAction} from '../../components/actions.js';
import {sentence} from './language.js';

// Which field an operator should look at, given what the remote said.
const KEY_CODES = new Set(['http_401', 'http_403', 'insufficient_permission']);
const URL_CODES = new Set(['timeout', 'http_404', 'connection_failed']);

function panel(title, subtitle, ...body) {
  return h('div', {class: 'page row-page settings-page settings-panel'},
    backPill('Settings', 'settings'),
    h('header', {class: 'page-header'},
      h('div', {},
        h('div', {class: 'eyebrow', text: 'Settings'}),
        h('h1', {text: title, tabindex: '-1'}),
        subtitle ? h('p', {text: subtitle}) : null)),
    ...body.filter(Boolean));
}

function field(label, control, hint) {
  return h('label', {class: 'field'}, h('span', {text: label}), control,
    hint ? h('small', {text: hint}) : null);
}

function checkField(label, control) {
  return h('label', {class: 'check-field'}, control, h('span', {text: label}));
}

// A stored key is shown by its last four characters and nothing else: enough to
// tell two keys apart, never enough to reconstruct one.
function secretRow({configured, hint, label, onReplace, onRemove, disabled}) {
  const right = h('span', {class: 'settings-secret-actions'});
  const node = h('div', {class: 'settings-secret-row'},
    h('span', {class: 'settings-secret-state', text: configured
      ? (hint ? `Key set · ····${hint}` : 'Key set') : 'No key set'}), right);
  const buttons = [
    h('button', {class: 'button compact', type: 'button', disabled,
      onclick: onReplace}, configured ? 'Replace' : 'Set'),
    configured ? h('button', {class: 'button ghost compact', type: 'button',
      disabled, onclick: () => onRemove(node, right)}, 'Remove') : null,
  ].filter(Boolean);
  right.replaceChildren(...buttons);
  node.setAttribute('aria-label', label);
  node.restore = () => right.replaceChildren(...buttons);
  return node;
}

// The confirm lives on the row itself, so the operator never loses sight of
// which key they are removing. A reload rebuilds the row and abandons it.
function inlineConfirm(row, right, copy, onConfirm) {
  right.replaceChildren(
    h('span', {class: 'settings-note', text: copy}),
    h('button', {class: 'button danger compact', type: 'button',
      onclick: onConfirm}, 'Remove'),
    h('button', {class: 'button ghost compact', type: 'button',
      onclick: () => row.restore()}, 'Cancel'));
}

function attachError(result, targets) {
  const code = result?.code || '';
  if (KEY_CODES.has(code) && targets.key) return targets.key;
  if (URL_CODES.has(code) && targets.url) return targets.url;
  return null;
}

function fieldMessage() {
  return h('div', {class: 'settings-field-error', role: 'alert'});
}

function providerSelect(value, providers) {
  const options = [...providers];
  // A value this build has never heard of is still the configured value;
  // dropping it from the list would silently re-point the fleet on save.
  if (value && !options.includes(value)) options.push(value);
  const node = h('select', {}, ...options.map((name) => h('option', {value: name, text: name})));
  node.value = value ?? '';
  return node;
}

function providerPicker(values, providers) {
  const selected = new Set(values || []);
  const names = [...providers];
  selected.forEach((name) => { if (!names.includes(name)) names.push(name); });
  const inputs = names.map((name) => [name, h('input', {
    type: 'checkbox', checked: selected.has(name)})]);
  const node = h('div', {class: 'settings-picker'},
    ...inputs.map(([name, input]) => checkField(name, input)));
  node.value = () => inputs.filter(([, input]) => input.checked).map(([name]) => name);
  return node;
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

export function technical(row) {
  return h('details', {class: 'settings-technical disclosure'},
    h('summary', {text: 'Technical details'}),
    h('dl', {class: 'settings-details'},
      h('div', {}, h('dt', {text: 'Key'}), h('dd', {class: 'mono', text: row.key})),
      h('div', {}, h('dt', {text: 'Scope'}), h('dd', {text: row.scope})),
      h('div', {}, h('dt', {text: 'Resolver'}), h('dd', {text: row.resolver})),
      h('div', {}, h('dt', {text: 'Raw value'}), h('dd', {text: row.raw_semantics})),
      h('div', {}, h('dt', {text: 'Effective value'}), h('dd', {text: row.effective_semantics})),
      row.constraints ? h('div', {}, h('dt', {text: 'Constraints'}), h('dd', {text: row.constraints})) : null,
      row.unit ? h('div', {}, h('dt', {text: 'Unit'}), h('dd', {text: row.unit})) : null,
      row.ignored_database_override ? h('div', {}, h('dt', {text: 'Ignored override'}), h('dd', {text: 'A database row exists but this value is deployment-only.'})) : null));
}

// `runAction` already restores the control and renders nothing on a 440 (the
// shared client prompted and retried), so that case never reaches onError.
//
// A save that succeeds reloads the page underneath this button, so the pending
// state is deliberately not restored: re-enabling a control that is about to be
// replaced only re-opens a double-submit window.
function saveHandler(button, message, run) {
  return () => runAction(button, async () => { message.textContent = ''; await run(); }, {
    pendingLabel: 'Saving…',
    restoreOnSuccess: false,
    onError: (error) => { message.textContent = error.message; },
  });
}

export function geoipPanel(report, actions) {
  const setup = report.provider_setup || {};
  const geo = setup.geoip || {};
  const providers = setup.geoip_providers || [];
  const message = h('div', {class: 'form-message', role: 'alert'});
  const result = h('div', {class: 'settings-result', role: 'status'});
  const locked = setup.available !== true;
  const errors = {};
  const controls = (setup.fleet_fields || []).map((entry) => {
    const value = geo[entry.key];
    let input;
    if (entry.key === 'GEOIP_PRIMARY_PROVIDER' || entry.key === 'GEOIP_FALLBACK_PROVIDER') {
      input = providerSelect(value, providers);
    } else if (entry.key === 'GEOIP_ADDITIONAL_PROVIDERS') {
      input = providerPicker(value, providers);
    } else if (entry.value_type === 'boolean') {
      input = h('input', {type: 'checkbox', checked: value === true});
    } else {
      input = h('input', {type: entry.value_type === 'origin' ? 'url' : 'text',
        value: entry.value_type === 'list' ? (value || []).join(', ') : (value ?? ''),
        autocomplete: 'off'});
    }
    if (locked) input.setAttribute('disabled', '');
    const error = fieldMessage();
    if (entry.key === 'GEOIP_MOJO_PROVIDER_URL') errors.url = error;
    // A pick-many control needs the row to itself; squeezed into a third of
    // the grid it wraps provider names mid-word.
    const wide = entry.value_type === 'list' ? 'settings-wide' : '';
    const node = entry.value_type === 'boolean'
      ? h('div', {class: wide}, checkField(entry.label, input), error)
      : h('div', {class: wide}, field(entry.label, input, entry.constraints), error);
    return {entry, input, node};
  });

  let replacement = null;
  const keyError = fieldMessage();
  errors.key = keyError;
  const keySlot = h('div', {class: 'settings-secret-slot'});
  const renderKey = () => {
    const row = secretRow({
      configured: geo.GEOIP_API_KEY_MOJO_CONFIGURED === true,
      hint: geo.GEOIP_API_KEY_MOJO_HINT || '', label: 'GeoIP API key',
      disabled: locked,
      onReplace: () => {
        replacement = h('input', {type: 'password', value: '',
          autocomplete: 'new-password', placeholder: 'New GeoIP API key'});
        keySlot.replaceChildren(field('GeoIP API key', replacement,
          'The stored key is replaced when this is saved.'),
        h('button', {class: 'button ghost compact', type: 'button', onclick: () => {
          replacement = null; renderKey();
        }}, 'Keep the stored key'), keyError);
        replacement.focus();
      },
      onRemove: (node, right) => inlineConfirm(node, right,
        'Remove the stored GeoIP key?', () => save(true)),
    });
    keySlot.replaceChildren(row, keyError);
  };
  renderKey();

  const payload = (clearKey) => {
    const geoip = Object.fromEntries(controls.map(({entry, input}) => [entry.key,
      typeof input.value === 'function' ? input.value()
        : entry.value_type === 'boolean' ? input.checked
          : entry.value_type === 'list'
            ? input.value.split(',').map((item) => item.trim()).filter(Boolean)
            : input.value.trim()]));
    geoip.GEOIP_API_KEY_MOJO = clearKey ? null : (replacement?.value || null);
    geoip.clear_api_key = Boolean(clearKey);
    return {expected_revision: setup.configuration_revision || null, geoip};
  };

  const clearErrors = () => Object.values(errors).forEach((node) => { node.textContent = ''; });
  const showFailure = (item) => {
    const target = attachError(item, errors);
    if (target) target.textContent = item.message;
    else message.textContent = item.message;
  };

  const test = h('button', {class: 'button', type: 'button', disabled: locked}, 'Test connection');
  // Saving and clearing the key are two different actions on one button, so
  // they carry two guard keys — a clear must never return a save's promise.
  const save = (clearKey) => runAction(saveButton, async () => {
    clearErrors(); message.textContent = ''; result.textContent = '';
    await actions.configureProviders('geoip', payload(clearKey));
  }, {key: `geoip-save:${clearKey ? 'clear' : 'write'}`, pendingLabel: 'Saving…',
    restoreOnSuccess: false, onError: (error) => { message.textContent = error.message; }});
  const saveButton = h('button', {class: 'button primary', type: 'button',
    disabled: locked, onclick: () => save(false)}, 'Save GeoIP');
  test.addEventListener('click', () => runAction(test, async () => {
    clearErrors(); message.textContent = ''; result.textContent = '';
    const answer = await actions.testProviders('geoip', payload(false));
    const item = (answer.results || {}).geoip || {};
    if (item.success) result.textContent = 'Connection OK';
    else showFailure(item);
  }, {pendingLabel: 'Testing…', onError: (error) => { message.textContent = error.message; }}));

  return panel('GeoIP', 'Where IP intelligence comes from, and the key used to ask for it.',
    h('p', {class: 'settings-note', text: 'These values apply to all servers.'}),
    locked ? h('p', {class: 'settings-guidance', text: 'Fleet publishing is not configured, so these values can be read but not changed. Set the Admin fleet bucket, prefix, delegated keys, KMS key, and config-sync restart posture in deployment settings.'}) : null,
    setup.pending_restart ? h('p', {class: 'settings-note', text: 'A published change is waiting for the config-sync restart.'}) : null,
    h('div', {class: 'field-grid'}, ...controls.map(({node}) => node)),
    keySlot, result, message,
    h('div', {class: 'form-actions'}, test, saveButton));
}

// v1's mojo-only SMS editor. In v2 the Text messages sub-page owns this
// configuration, so the catalog only falls back to this panel for a reader who
// cannot open that page — see the note at the top of page.js.
export function smsPanel(report, actions) {
  const setup = report.provider_setup || {};
  const sms = setup.sms || {};
  const message = h('div', {class: 'form-message', role: 'alert'});
  const result = h('div', {class: 'settings-result', role: 'status'});
  const urlError = fieldMessage();
  const keyError = fieldMessage();
  const errors = {url: urlError, key: keyError};
  const url = h('input', {type: 'url', value: sms.remote_url || '',
    autocomplete: 'url', placeholder: 'https://sms.example.com'});
  const testMode = h('input', {type: 'checkbox', checked: sms.test_mode === true});

  let replacement = null;
  const keySlot = h('div', {class: 'settings-secret-slot'});
  const renderKey = () => {
    const row = secretRow({
      configured: sms.api_key_configured === true, hint: sms.api_key_hint || '',
      label: 'Remote API key', disabled: false,
      onReplace: () => {
        replacement = h('input', {type: 'password', value: '',
          autocomplete: 'new-password', placeholder: 'New remote API key'});
        keySlot.replaceChildren(field('Remote API key', replacement,
          'The stored key is replaced when this is saved.'),
        h('button', {class: 'button ghost compact', type: 'button', onclick: () => {
          replacement = null; renderKey();
        }}, 'Keep the stored key'), keyError);
        replacement.focus();
      },
      onRemove: (node, right) => inlineConfirm(node, right,
        'Remove the stored SMS key?', () => save(true)),
    });
    keySlot.replaceChildren(row, keyError);
  };
  renderKey();

  const payload = (clearKey) => ({
    expected_revision: setup.configuration_revision || null,
    sms: {remote_url: url.value.trim(), api_key: clearKey ? null : (replacement?.value || null),
      clear_api_key: Boolean(clearKey), test_mode: testMode.checked},
  });
  const clearErrors = () => Object.values(errors).forEach((node) => { node.textContent = ''; });
  const test = h('button', {class: 'button', type: 'button'}, 'Test connection');
  const save = (clearKey) => runAction(saveButton, async () => {
    clearErrors(); message.textContent = ''; result.textContent = '';
    await actions.configureProviders('sms', payload(clearKey));
  }, {key: `sms-save:${clearKey ? 'clear' : 'write'}`, pendingLabel: 'Saving…',
    restoreOnSuccess: false, onError: (error) => { message.textContent = error.message; }});
  const saveButton = h('button', {class: 'button primary', type: 'button',
    onclick: () => save(false)}, 'Save SMS');
  test.addEventListener('click', () => runAction(test, async () => {
    clearErrors(); message.textContent = ''; result.textContent = '';
    const answer = await actions.testProviders('sms', payload(false));
    const item = (answer.results || {}).sms || {};
    if (item.success) result.textContent = 'Connection OK';
    else {
      const target = attachError(item, errors);
      if (target) target.textContent = item.message; else message.textContent = item.message;
    }
  }, {pendingLabel: 'Testing…', onError: (error) => { message.textContent = error.message; }}));

  return panel('Text messages', 'The django-mojo instance this platform asks to send SMS.',
    h('div', {class: 'field-grid'},
      h('div', {}, field('Remote django-mojo address', url, 'One HTTPS origin'), urlError)),
    keySlot,
    checkField('Test mode — accept sends without delivering them', testMode),
    result, message,
    h('div', {class: 'form-actions'}, test, saveButton));
}

export function authPanel(row, actions) {
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
  const message = h('div', {class: 'form-message', role: 'alert'});
  const save = h('button', {class: 'button primary', type: 'button', disabled: true}, 'Save authentication settings');
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
  save.addEventListener('click', saveHandler(save, message, async () => {
    await actions.owner({auth: payload()});
  }));
  return panel('Sign-in', 'Password stays enabled so an administrator can always recover access.',
    h('div', {class: 'field-grid'}, field('Application title', title), field('Accent color', accent)),
    h('fieldset', {class: 'settings-methods'}, h('legend', {text: 'Login methods'}),
      ...login.map(([name, input]) => checkField(name, input))),
    h('fieldset', {class: 'settings-methods'}, h('legend', {text: 'Registration'}),
      checkField('Registration enabled', enabled),
      ...registration.map(([name, input]) => checkField(name, input)),
      field('Passkey prompt', prompt)),
    message, h('div', {class: 'form-actions'}, save), technical(row));
}

export function topologyPanel(row, actions) {
  const value = row.effective_value || {};
  const nodes = tokenEditor('Node', value.nodes);
  const pools = tokenEditor('Pool', value.pools);
  const message = h('div', {class: 'form-message', role: 'alert'});
  const save = h('button', {class: 'button primary', type: 'button', disabled: true}, 'Save expected fleet');
  const original = JSON.stringify({nodes: nodes.value(), pools: pools.value()});
  const dirty = () => { save.disabled = JSON.stringify({nodes: nodes.value(), pools: pools.value()}) === original; };
  nodes.node.addEventListener('change', dirty); pools.node.addEventListener('change', dirty);
  save.addEventListener('click', saveHandler(save, message, async () => {
    await actions.owner({edge_topology: {nodes: nodes.value(), pools: pools.value()}});
  }));
  return panel('Expected fleet', 'Add each expected node and pool explicitly.',
    field('Nodes', nodes.node), field('Pools', pools.node),
    message, h('div', {class: 'form-actions'}, save), technical(row));
}

// Every catalog row has a home, including the ones nobody can change here:
// the drill-in is where provenance, semantics, and constraints live now.
export function settingPanel(row, actions) {
  const message = h('div', {class: 'form-message', role: 'alert'});
  const body = [];
  if (row.can_write) {
    const initial = row.effective_value;
    const control = row.value_type === 'boolean'
      ? h('input', {type: 'checkbox', checked: initial === true})
      : h('input', {type: 'url', value: typeof initial === 'string' ? initial : '',
        autocomplete: 'url', placeholder: 'https://app.your-domain.com'});
    const save = h('button', {class: 'button primary', type: 'button', disabled: true}, 'Save change');
    const current = () => (row.value_type === 'boolean' ? control.checked : control.value.trim());
    const dirty = () => {
      const value = current();
      let invalid = false;
      if (row.value_type === 'origin' && value) {
        try { const url = new URL(value); invalid = url.protocol !== 'https:' || !url.hostname.includes('.') || Boolean(url.username || url.password || url.search || url.hash) || !['', '/'].includes(url.pathname) || !['', '443'].includes(url.port); }
        catch (_) { invalid = true; }
        control.setCustomValidity(invalid ? 'Use one public HTTPS hostname origin.' : '');
        message.textContent = invalid ? 'Use one public HTTPS origin, without a path or credentials.' : '';
      }
      save.disabled = invalid || value === initial || value === '';
    };
    control.addEventListener('input', dirty); control.addEventListener('change', dirty);
    save.addEventListener('click', saveHandler(save, message, async () => {
      await actions.mutate({action: 'set', key: row.key, value: current()});
    }));
    body.push(row.value_type === 'boolean'
      ? checkField(row.label, control)
      : field('Public HTTPS origin', control, row.constraints));
    body.push(h('div', {class: 'form-actions'}, save));
  }
  const footer = [];
  if (row.duplicate_override && row.can_clear) {
    footer.push(h('button', {class: 'button danger compact', type: 'button',
      onclick: () => actions.clear(row, true)}, 'Clear conflicts'));
  } else if (row.can_clear && row.source === 'database') {
    footer.push(h('button', {class: 'button ghost compact', type: 'button',
      onclick: () => actions.clear(row, false)}, 'Reset to default'));
  }
  if (row.writable === 'none' && row.change_behavior === 'deploy') {
    footer.push(h('p', {class: 'settings-guidance',
      text: `Change ${row.key} in Django production settings and deploy.`}));
  }
  return panel(row.label, row.description,
    h('p', {class: 'settings-lead', text: sentence(row)}),
    ...body, message,
    footer.length ? h('div', {class: 'settings-footer'}, ...footer) : null,
    technical(row));
}

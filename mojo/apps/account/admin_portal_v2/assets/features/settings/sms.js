// Text messages — manage the SYSTEM PhoneConfig the installation's OTP, MFA
// and password-reset SMS route through, test the connection with zero side
// effects, and send one test message. Group overrides are visible, never
// editable here. Only a superuser may save the system row; everyone else in
// the phone-config tier gets a read-only page with the operator actions.
//
// v1's Text messages page, ported whole: the same summary read, the same
// verify-on-save editor, the same two test tools, the same capability gate
// (`capabilities.system_write`, which the server derives from
// messaging_sms_system_write). The v2 change is the chrome — this is a
// sub-page of Settings now, so it wears the back pill and Settings' eyebrow.

import {api, h} from '../../core.js';
import {backPill} from '../../app.js';
import {runAction} from '../../components/actions.js';
import {rowSection, statusRow} from '../../components/rows.js';
import {emptyState, errorState, loadingState} from '../../components/views.js';

const SUMMARY_URL = '/api/account/admin/messaging-sms/summary';
const MUTATE_URL = '/api/account/admin/messaging-sms';

const PROVIDER_LABELS = {twilio: 'Twilio', aws: 'AWS SNS', mojo: 'Mojo remote'};
const PROVIDER_OPTIONS = [
  {value: 'twilio', label: 'Twilio'},
  {value: 'aws', label: 'AWS SNS'},
  {value: 'mojo', label: 'Mojo remote instance'},
];

function field(label, control, hint) {
  return h('label', {class: 'field'}, h('span', {text: label}), control,
    hint ? h('small', {text: hint}) : null);
}

function secretHint(configured) {
  return configured ? 'A value is stored — leave blank to keep it.' : 'No value stored yet.';
}

function resultLine(result) {
  const tone = result.state === 'ok' ? 'ok' : result.state === 'test_mode' ? 'warn' : 'danger';
  return h('p', {class: `sms-result sms-result-${tone}`, role: 'status', text: result.message || ''});
}

function verifyLine(verify) {
  if (!verify) return null;
  const when = verify.at ? ` · ${verify.at.slice(0, 10)}` : '';
  return h('p', {class: verify.ok ? 'sms-note' : 'sms-note warning',
    text: (verify.ok ? 'Last connection test passed' : `Last connection test failed — ${verify.message || 'see the event log'}`) + when});
}

function systemSection(report, actions) {
  const system = report.system;
  if (!system) {
    return rowSection('System provider', [statusRow({
      tone: 'muted', name: 'System provider', value: 'Not configured',
      detail: 'every group falls back to settings-based Twilio delivery'})]);
  }
  const provider = PROVIDER_LABELS[system.provider] || system.provider;
  const secrets = Object.entries(system.secrets || {})
    .filter(([, set]) => set).map(([name]) => name);
  const rows = [
    statusRow({tone: system.is_active ? 'ok' : 'muted', name: 'Provider',
      value: `${provider} · ${system.is_active ? 'Active' : 'Inactive'}`,
      detail: system.name || ''}),
    statusRow({tone: 'muted', name: 'Secrets set',
      value: secrets.length ? secrets.join(', ') : 'none', mono: true}),
  ];
  if (system.provider === 'twilio') {
    rows.push(statusRow({tone: 'muted', name: 'From number',
      value: system.twilio_from_number || 'settings TWILIO_NUMBER', mono: true}));
  }
  if (system.provider === 'mojo') {
    rows.push(statusRow({tone: 'muted', name: 'Remote', value: system.mojo_remote_url || '—', mono: true}));
  }
  if (system.provider === 'aws') {
    rows.push(statusRow({tone: 'muted', name: 'Region', value: system.aws_region || '—', mono: true}));
  }
  if (system.test_mode) {
    rows.push(statusRow({tone: 'warn', name: 'Test mode', value: 'On',
      detail: 'connection tests are skipped — real messages still send'}));
  }
  const section = rowSection('System provider', rows);
  const verify = verifyLine(report.verify_state);
  if (verify) section.append(verify);
  section.append(actions);
  return section;
}

function overridesSection(report) {
  const overrides = report.group_overrides || {};
  const items = overrides.items || [];
  if (!items.length) return null;
  const rows = items.map((row) => statusRow({
    tone: 'muted', name: row.group?.name || `Group ${row.group?.id}`,
    value: `${PROVIDER_LABELS[row.provider] || row.provider}${row.test_mode ? ' · test mode' : ''}`,
    detail: row.name || ''}));
  if (overrides.truncated) {
    rows.push(statusRow({tone: 'muted', name: '…',
      value: `${overrides.count - items.length} more group overrides not shown`}));
  }
  const section = rowSection('Group overrides', rows);
  section.append(h('p', {class: 'sms-note',
    text: 'These groups do not fall back to the system provider. Overrides are managed by each group, not from this page.'}));
  return section;
}

function editorSection(report, reload) {
  const canWrite = report.capabilities?.system_write === true;
  const system = report.system;
  if (!canWrite) {
    return h('div', {class: 'sms-editor'},
      h('p', {class: 'sms-note', text: 'Only a superuser can change the system text-message provider — it routes every sign-in and recovery code on this installation.'}));
  }
  const message = h('p', {class: 'form-message', role: 'alert'});
  const providerSelect = h('select', {},
    ...PROVIDER_OPTIONS.map((option) => h('option', {value: option.value, text: option.label})));
  providerSelect.value = system?.provider || 'twilio';
  const nameInput = h('input', {autocomplete: 'off', value: system?.name || ''});
  const testMode = h('input', {type: 'checkbox', checked: Boolean(system?.test_mode)});
  const secrets = system?.secrets || {};

  const twilioFrom = h('input', {autocomplete: 'off', value: system?.twilio_from_number || '', placeholder: '+15551230000'});
  const twilioSid = h('input', {autocomplete: 'off', placeholder: secrets.twilio_account_sid ? 'stored' : 'AC…'});
  const twilioToken = h('input', {type: 'password', autocomplete: 'off', placeholder: secrets.twilio_auth_token ? 'stored' : ''});
  const awsRegion = h('input', {autocomplete: 'off', value: system?.aws_region || '', placeholder: 'us-east-1'});
  const awsSender = h('input', {autocomplete: 'off', value: system?.aws_sender_id || ''});
  const awsKey = h('input', {autocomplete: 'off', placeholder: secrets.aws_access_key_id ? 'stored' : ''});
  const awsSecret = h('input', {type: 'password', autocomplete: 'off', placeholder: secrets.aws_secret_access_key ? 'stored' : ''});
  const mojoUrl = h('input', {autocomplete: 'off', value: system?.mojo_remote_url || '', placeholder: 'https://sms.example.com'});
  const mojoKey = h('input', {type: 'password', autocomplete: 'off', placeholder: secrets.mojo_api_key ? 'stored' : ''});

  const panels = {
    twilio: h('div', {},
      field('From number', twilioFrom, 'Blank falls back to the TWILIO_NUMBER deployment setting.'),
      field('Account SID', twilioSid, secretHint(secrets.twilio_account_sid)),
      field('Auth token', twilioToken, 'Supply both credentials to use a dedicated account, or neither to send with the deployment credentials. Never half a pair.')),
    aws: h('div', {},
      field('Region', awsRegion),
      field('Sender ID', awsSender, 'Optional, up to 11 characters.'),
      field('Access key ID', awsKey, secretHint(secrets.aws_access_key_id)),
      field('Secret access key', awsSecret, secretHint(secrets.aws_secret_access_key))),
    mojo: h('div', {},
      field('Remote URL', mojoUrl, 'One HTTPS origin of the remote django-mojo instance.'),
      field('API key', mojoKey, secretHint(secrets.mojo_api_key))),
  };
  const panelHost = h('div', {});
  const showPanel = () => panelHost.replaceChildren(panels[providerSelect.value]);
  providerSelect.addEventListener('change', showPanel);
  showPanel();

  const save = h('button', {class: 'button', type: 'button'}, 'Verify & save');
  // A successful save reloads the page and replaces this editor, so the pending
  // state is not restored — it would land on a detached button, and restoring
  // one that survived would re-open the double-submit window.
  save.addEventListener('click', () => runAction(save, async () => {
    const provider = providerSelect.value;
    const payload = {action: 'save', provider,
      expected_revision: report.configuration_revision,
      test_mode: testMode.checked};
    if (nameInput.value.trim()) payload.name = nameInput.value.trim();
    if (provider === 'twilio') {
      if (twilioFrom.value.trim()) payload.twilio_from_number = twilioFrom.value.trim();
      if (twilioSid.value.trim()) payload.twilio_account_sid = twilioSid.value.trim();
      if (twilioToken.value) payload.twilio_auth_token = twilioToken.value;
    } else if (provider === 'aws') {
      if (awsRegion.value.trim()) payload.aws_region = awsRegion.value.trim();
      if (awsSender.value.trim()) payload.aws_sender_id = awsSender.value.trim();
      if (awsKey.value.trim()) payload.aws_access_key_id = awsKey.value.trim();
      if (awsSecret.value) payload.aws_secret_access_key = awsSecret.value;
    } else {
      payload.remote_url = mojoUrl.value.trim();
      if (mojoKey.value) payload.api_key = mojoKey.value;
    }
    message.textContent = 'Verifying with the provider…';
    await api(MUTATE_URL, {method: 'POST', body: JSON.stringify(payload)});
    twilioToken.value = ''; awsSecret.value = ''; mojoKey.value = '';
    await reload();
  }, {
    pendingLabel: 'Verifying…', restoreOnSuccess: false,
    onError: (error) => { message.textContent = error.message || 'The save was refused.'; },
  }));

  return h('div', {class: 'sms-editor'},
    h('h2', {text: 'Change the system provider'}),
    h('p', {class: 'sms-note', text: 'Every save is verified against the provider first and refused when the credentials do not answer. Test mode never skips that verification.'}),
    field('Provider', providerSelect),
    field('Configuration name', nameInput, 'Optional — a label for this configuration.'),
    panelHost,
    h('label', {class: 'check-field'}, testMode, h('span', {text: 'Test mode (connection tests short-circuit; real sends are NOT stopped)'})),
    message,
    h('div', {class: 'form-actions'}, save));
}

function actionsBar(report, results) {
  const testButton = h('button', {class: 'button ghost compact', type: 'button'}, 'Test connection');
  // The results block is a sibling of these buttons, not their container, so
  // both the pending state and the answer survive each other.
  testButton.addEventListener('click', () => runAction(testButton, async () => {
    const result = await api(MUTATE_URL, {method: 'POST', body: JSON.stringify({action: 'test_connection'})});
    results.replaceChildren(resultLine(result));
  }, {
    pendingLabel: 'Testing…',
    onError: (error) => { results.replaceChildren(resultLine({state: 'failed', message: error.message})); },
  }));
  const number = h('input', {autocomplete: 'off', placeholder: '+15551230000', 'aria-label': 'Test recipient number'});
  const sendButton = h('button', {class: 'button ghost compact', type: 'button'}, 'Send test message');
  sendButton.addEventListener('click', () => {
    if (!number.value.trim()) {
      results.replaceChildren(resultLine({state: 'failed', message: 'Enter a recipient number first.'}));
      return undefined;
    }
    return runAction(sendButton, async () => {
      const result = await api(MUTATE_URL, {method: 'POST',
        body: JSON.stringify({action: 'send_test', to_number: number.value.trim()})});
      const detail = result.test_number ? result.message
        : `${result.message} — SMS #${result.sms?.id} is ${result.sms?.status}` +
          (result.sms?.error_message ? ` (${result.sms.error_message})` : '');
      results.replaceChildren(resultLine({state: result.sent || result.test_number ? 'ok' : 'failed', message: detail}));
    }, {
      pendingLabel: 'Sending…',
      onError: (error) => { results.replaceChildren(resultLine({state: 'failed', message: error.message})); },
    });
  });
  return h('div', {class: 'sms-actions'}, testButton, number, sendButton);
}

export async function smsPage(ctx) {
  const body = h('div', {class: 'sms-body'}, loadingState('Loading…'));
  const root = h('div', {class: 'page row-page sms-page settings-sub-page'},
    backPill('Settings', 'settings'),
    h('header', {class: 'page-header'},
      h('div', {},
        h('div', {class: 'eyebrow', text: 'Settings · Integrations'}),
        h('h1', {text: 'Text messages', tabindex: '-1'}),
        h('p', {text: 'The provider behind every SMS this installation sends — sign-in codes included.'}))),
    body);
  async function load() {
    body.replaceChildren(loadingState('Loading…'));
    try {
      const report = await api(SUMMARY_URL);
      if (report.installed === false) {
        body.replaceChildren(emptyState('Text messaging is not installed',
          'This platform runs without the phonehub app, so there is nothing to configure here.'));
        return;
      }
      const results = h('div', {class: 'sms-results', 'aria-live': 'polite'});
      const actions = actionsBar(report, results);
      body.replaceChildren(...[
        systemSection(report, actions),
        results,
        overridesSection(report),
        editorSection(report, load),
      ].filter(Boolean));
    } catch (error) {
      body.replaceChildren(errorState(error, () => load()));
    }
  }
  await load();
  return root;
}

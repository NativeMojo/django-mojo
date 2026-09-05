import {badge, h, statusTone, TableView} from '../../core.js';
import {runAction} from '../../components/actions.js';
import {openModal} from '../../components/overlays.js';
import {actionSchemas, performSecurityAction, readSecurityDetail, sectionRows} from './api.js';

function policySchema(report) {
  const schema = report.sections.schemas?.data?.rule_policy;
  if (!schema?.aggregate?.properties || schema.aggregate.additional_properties !== false) {
    throw new Error('The server policy schema is unavailable.');
  }
  return schema;
}

function initialPolicy(schema, value = {}) {
  const result = {};
  for (const [name, field] of Object.entries(schema.aggregate.properties)) {
    if (Object.prototype.hasOwnProperty.call(value, name)) result[name] = value[name];
    else if (Object.prototype.hasOwnProperty.call(field, 'default')) result[name] = field.default;
  }
  result.is_active = false;
  return result;
}

function schemaInput(name, schema, value) {
  const types = Array.isArray(schema.type) ? schema.type : [schema.type];
  if (schema.enum || schema.enum_from) {
    const values = schema.enum || [];
    const input = h('select', {name}, ...values.map((item) => h('option', {
      value: item, text: String(item), selected: String(item) === String(value) || null,
    })));
    return {node: input, read: () => Number.isInteger(values[0]) ? Number(input.value) : input.value};
  }
  if (types.includes('boolean')) {
    const input = h('input', {name, type: 'checkbox', checked: value === true});
    return {node: input, read: () => input.checked};
  }
  if (schema.type === 'array') {
    const input = h('textarea', {name, rows: name === 'rules' ? 8 : 5,
      text: JSON.stringify(Array.isArray(value) ? value : [], null, 2)});
    return {node: input, read: () => JSON.parse(input.value)};
  }
  const numeric = types.includes('integer');
  const input = h('input', {name, type: numeric ? 'number' : 'text', value: value ?? '',
    min: schema.minimum ?? null, max: schema.maximum ?? null,
    maxlength: schema.max_length ?? null, required: schema.min_length > 0 || null});
  return {node: input, read: () => input.value === '' && types.includes('null')
    ? null : numeric ? Number(input.value) : input.value};
}

function policyEditor(report, value, submitLabel, onSubmit) {
  const schema = policySchema(report);
  const policy = initialPolicy(schema, value);
  const fields = {}; const visible = [
    'name', 'category', 'priority', 'bundle_minutes', 'bundle_by',
    'bundle_by_rule_set', 'match_by', 'trigger_count', 'trigger_window',
    'retrigger_every', 'handlers', 'rules', 'delete_on_resolution',
  ];
  const nodes = visible.map((name) => {
    const control = schemaInput(name, schema.aggregate.properties[name], policy[name]);
    fields[name] = control;
    return h('label', {class: name === 'handlers' || name === 'rules' ? 'field security-wide' : 'field'},
      h('span', {text: name.replaceAll('_', ' ')}), control.node,
      name === 'handlers' || name === 'rules'
        ? h('small', {text: 'Structured JSON accepted and validated by the server-owned schema.'}) : null);
  });
  const confirmation = h('input', {type: 'text', required: true, autocomplete: 'off'});
  const message = h('div', {class: 'form-message', role: 'alert'});
  const button = h('button', {class: 'button primary', type: 'submit'}, submitLabel);
  const form = h('form', {class: 'security-policy-form', onsubmit: (event) => {
    event.preventDefault();
    runAction(button, async () => {
      message.textContent = '';
      const next = {};
      for (const [name, control] of Object.entries(fields)) next[name] = control.read();
      next.is_active = false;
      await onSubmit(next, confirmation.value);
    }, {pendingLabel: 'Saving…', onError: (error) => { message.textContent = error.message; }});
  }}, ...nodes,
  h('label', {class: 'field security-wide'}, h('span', {text: 'Typed confirmation'}), confirmation),
  message, h('div', {class: 'form-actions'}, button));
  return {form, confirmation};
}

function confirmationFor(schema, id) {
  return String(schema?.confirmation?.value || '').replace('{id}', String(id));
}

function actionButton({report, row, action, label, refresh}) {
  const schema = actionSchemas(report)[action];
  if (!schema) return null;
  const button = h('button', {class: 'button ghost compact', type: 'button'}, label);
  button.addEventListener('click', () => {
    const expected = confirmationFor(schema, row.id);
    const input = h('input', {type: 'text', autocomplete: 'off', required: true});
    const catchAllExpected = action === 'ruleset.activate' && Number(row.rule_count) === 0
      ? String(schema.confirmation?.catch_all_value || '').replace('{id}', String(row.id)) : '';
    const catchAllInput = catchAllExpected
      ? h('input', {type: 'text', autocomplete: 'off', required: true}) : null;
    const message = h('div', {class: 'form-message', role: 'alert'});
    const confirm = h('button', {class: 'button primary', type: 'button'}, label);
    let close;
    confirm.addEventListener('click', () => runAction(confirm, async () => {
      if (input.value !== expected) throw new Error(`Type ${expected} exactly.`);
      const payload = {ruleset_id: row.id, expected_modified: row.modified, confirm: input.value};
      if (catchAllExpected) {
        if (catchAllInput.value !== catchAllExpected) {
          throw new Error(`Type ${catchAllExpected} exactly.`);
        }
        payload.confirm_catch_all = catchAllInput.value;
      }
      try {
        await performSecurityAction(report, action, payload, {reread: refresh});
      } catch (error) {
        if (error?.code === 'stale_revision') close();
        throw error;
      }
      close(); await refresh();
    }, {pendingLabel: `${label}…`, onError: (error) => { message.textContent = error.message; }}));
    close = openModal({title: label, content: h('div', {class: 'security-stack'},
      h('p', {text: `Type ${expected} to continue.`}), input,
      catchAllInput ? h('label', {class: 'field'},
        h('span', {text: `Catch-all confirmation: type ${catchAllExpected}`}), catchAllInput) : null,
      message,
      h('div', {class: 'form-actions'}, confirm))});
  });
  return button;
}

async function openEditor({report, row, refresh}) {
  const current = row ? await readSecurityDetail('rules', row.id) : {};
  const action = row ? 'ruleset.replace' : 'ruleset.create';
  const schema = actionSchemas(report)[action];
  let close;
  const editor = policyEditor(report, current, row ? 'Replace rule set' : 'Create rule set', async (policy, confirmation) => {
    const expected = confirmationFor(schema, row?.id);
    if (confirmation !== expected) throw new Error(`Type ${expected} exactly.`);
    const payload = {confirm: confirmation, ruleset: policy};
    if (row) Object.assign(payload, {ruleset_id: row.id, expected_modified: row.modified});
    try {
      await performSecurityAction(report, action, payload, {reread: refresh});
    } catch (error) {
      if (error?.code === 'stale_revision') close();
      throw error;
    }
    close(); await refresh();
  });
  editor.confirmation.placeholder = confirmationFor(schema, row?.id);
  const legacyEvidence = current.validation?.legacy === true
    ? h('section', {class: 'security-state warning'},
      h('strong', {text: 'Legacy policy — still active under established semantics'}),
      h('p', {text: 'Replacing this policy opts it into the governed schema. Its retained handler and metadata are shown below.'}),
      h('pre', {class: 'security-evidence', text: JSON.stringify({
        handler: current.handler, metadata: current.metadata,
      }, null, 2)})) : null;
  close = openModal({title: row ? `Replace ${row.name}` : 'Create rule set',
    content: h('div', {class: 'security-stack'}, legacyEvidence, editor.form), wide: true});
}

export function renderRules({ctx, report, refresh, loadPage}) {
  const envelope = report.sections.rules;
  if (['unavailable', 'failed'].includes(envelope.status)) {
    return h('div', {class: 'security-state unavailable', role: 'status'},
      h('strong', {text: 'Rules unavailable'}),
      h('p', {text: 'Policy controls are hidden until the server returns the governed schema and current revisions.'}));
  }
  policySchema(report); actionSchemas(report);
  const manage = ctx.features.security.capabilities.manage === true;
  const create = manage ? h('button', {class: 'button primary', type: 'button'}, 'Create rule set') : null;
  create?.addEventListener('click', () => runAction(create,
    () => openEditor({report, refresh}), {pendingLabel: 'Opening…'}));
  const rows = sectionRows(envelope);
  const table = new TableView({rows, empty: 'No rule sets are configured.', columns: [
    {label: 'Rule set', render: (row) => h('div', {}, h('strong', {text: row.name || `Rule set ${row.id}`}), h('small', {text: row.category || 'uncategorized'}))},
    {label: 'State', render: (row) => badge(row.is_active ? 'active' : 'inactive', statusTone(row.is_active ? 'active' : 'inactive'))},
    {label: 'Rules', key: 'rule_count'},
    {label: 'Validation', render: (row) => badge(row.validation.status === 'valid' ? 'valid' : 'legacy', row.validation.status === 'valid' ? 'success' : 'warning')},
    {label: 'Actions', render: (row) => manage ? h('div', {class: 'security-row-actions'},
      (() => { const edit = h('button', {class: 'button ghost compact', type: 'button'}, 'Edit'); edit.addEventListener('click', (event) => { event.stopPropagation(); runAction(edit, () => openEditor({report, row, refresh}), {pendingLabel: 'Opening…'}); }); return edit; })(),
      actionButton({report, row, action: row.is_active ? 'ruleset.deactivate' : 'ruleset.activate', label: row.is_active ? 'Deactivate' : 'Activate', refresh}),
      actionButton({report, row, action: 'ruleset.delete', label: 'Delete', refresh})) : 'View only'},
  ]}).render();
  const more = envelope.next_cursor
    ? h('button', {class: 'button ghost compact', type: 'button'}, 'Load more rule sets') : null;
  more?.addEventListener('click', () => runAction(
    more, () => loadPage('rules'), {pendingLabel: 'Loading…'}));
  return h('div', {class: 'security-stack'},
    h('div', {class: 'security-toolbar'},
      h('p', {class: 'muted', text: manage ? 'Writes use the deployment-configured authentication freshness window, the current revision and typed confirmation.' : 'View-only security access.'}), create), table, more);
}

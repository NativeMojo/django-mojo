import {badge, formatDate, h, statusTone, TableView} from '../../core.js';
import {runAction} from '../../components/actions.js';
import {openModal} from '../../components/overlays.js';
import {actionSchemas, performSecurityAction, readSecurityDetail, sectionRows} from './api.js';

function actionConfirmation(schema, id) {
  return String(schema?.confirmation?.value || '').replace('{id}', String(id));
}

function governedAction({report, action, row, idKey, label, refresh}) {
  const schema = actionSchemas(report)[action];
  if (!schema) return null;
  const launch = h('button', {class: 'button ghost compact', type: 'button'}, label);
  launch.addEventListener('click', (event) => {
    event.stopPropagation();
    const expected = actionConfirmation(schema, row.id);
    const input = h('input', {type: 'text', required: true, autocomplete: 'off'});
    const note = action.startsWith('recommendation.')
      ? h('textarea', {rows: 3, maxlength: schema.properties?.note?.max_length || 256}) : null;
    const message = h('div', {class: 'form-message', role: 'alert'});
    const confirm = h('button', {class: 'button primary', type: 'button'}, label);
    let close;
    confirm.addEventListener('click', () => runAction(confirm, async () => {
      if (input.value !== expected) throw new Error(`Type ${expected} exactly.`);
      const payload = {
        [idKey]: row.id, expected_modified: row.modified, confirm: input.value,
      };
      if (note) payload.note = note.value;
      try {
        await performSecurityAction(report, action, payload, {reread: refresh});
      } catch (error) {
        if (error?.code === 'stale_revision') close();
        throw error;
      }
      close(); await refresh();
    }, {pendingLabel: `${label}…`, onError: (error) => { message.textContent = error.message; }}));
    close = openModal({title: label, content: h('div', {class: 'security-stack'},
      h('p', {text: `Type ${expected} to continue. The action is never replayed after an ambiguous response.`}),
      input, note ? h('label', {class: 'field'}, h('span', {text: 'Operator note'}), note) : null,
      message, h('div', {class: 'form-actions'}, confirm))});
  });
  return launch;
}

function hostLine(label, hosts) {
  const safe = Array.isArray(hosts) ? hosts : [];
  return h('div', {class: 'security-host-line'}, h('strong', {text: label}),
    h('span', {text: safe.length ? safe.join(', ') : 'None'}));
}

async function enforcementDetail(row) {
  const content = h('div', {class: 'security-stack'}, h('p', {text: 'Loading complete evidence…'}));
  openModal({title: `${row.name || 'IPSet'} enforcement`, subtitle: row.description || '',
    content, wide: true});
  try {
    row = await readSecurityDetail('ipsets', row.id);
  } catch (error) {
    content.replaceChildren(h('p', {class: 'form-message', text: error.message}));
    return;
  }
  const truth = row.enforcement && typeof row.enforcement === 'object' ? row.enforcement : {};
  const desired = truth.desired && typeof truth.desired === 'object' ? truth.desired : {};
  content.replaceChildren(
      h('section', {class: 'security-truth'},
        h('div', {}, h('span', {text: 'Desired'}), h('strong', {text: desired.present === true ? 'Present' : desired.present === false ? 'Absent' : 'Unknown'})),
        h('div', {}, h('span', {text: 'Observed'}), h('strong', {text: truth.observed || 'unknown'})),
        h('div', {}, h('span', {text: 'Generation'}), h('strong', {text: truth.generation == null ? 'Unavailable' : String(truth.generation)})),
        h('div', {}, h('span', {text: 'Members'}), h('strong', {text: desired.count == null ? 'Unavailable' : String(desired.count)}))),
      h('section', {class: 'security-hosts'}, h('h3', {text: 'Captured checked hosts'}),
        hostLine('Expected', truth.expected_host_ids),
        hostLine('Responded', truth.responded_host_ids),
        hostLine('Succeeded', truth.succeeded_host_ids),
        hostLine('Failed', truth.failed_host_ids),
        hostLine('Missing', truth.missing_host_ids)),
      h('section', {}, h('h3', {text: 'Configuration and retained evidence'}),
        h('pre', {class: 'security-evidence', text: JSON.stringify({
          source: row.source, source_url: row.source_url,
          source_key: row.source_key, data: row.data, sync_error: row.sync_error,
        }, null, 2)})),
      h('p', {class: 'muted', text: `Observed through ${formatDate(truth.observation_cutoff)}. Authentication secrets are hidden; operational addresses, CIDRs, paths, errors and evidence remain visible.`}));
}

export function renderFirewall({ctx, report, refresh}) {
  const envelope = report.sections.ipsets;
  if (['unavailable', 'failed'].includes(envelope.status)) {
    return h('div', {class: 'security-state unavailable', role: 'status'},
      h('strong', {text: 'Firewall truth unavailable'}),
      h('p', {text: 'No desired, observed or verified state is inferred.'}));
  }
  const manage = ctx.features.security.capabilities.manage === true;
  const rows = sectionRows(envelope);
  const table = new TableView({rows, empty: 'No operator IPSets are configured.',
    onSelect: enforcementDetail, columns: [
      {label: 'IPSet', render: (row) => h('div', {}, h('strong', {text: row.name || `IPSet ${row.id}`}), h('small', {text: `${row.kind || 'custom'} · ${row.cidr_count ?? 0} members`}))},
      {label: 'Desired', render: (row) => badge(row.is_enabled ? 'present' : 'absent', row.is_enabled ? 'success' : 'neutral')},
      {label: 'Observed', render: (row) => badge(row.enforcement?.observed || row.enforcement_status || 'unknown', row.enforcement_ok ? 'success' : row.enforcement_status === 'partial' ? 'warning' : 'neutral')},
      {label: 'Hosts', render: (row) => `${row.enforcement?.succeeded_host_ids?.length || 0}/${row.enforcement?.expected_host_ids?.length || 0}`},
      {label: 'Actions', render: (row) => manage ? h('div', {class: 'security-row-actions'},
        governedAction({report, row, idKey: 'ipset_id', action: row.is_enabled ? 'ipset.disable' : 'ipset.enable', label: row.is_enabled ? 'Disable' : 'Enable', refresh}),
        governedAction({report, row, idKey: 'ipset_id', action: 'ipset.sync', label: 'Sync', refresh})) : 'View only'},
    ]}).render();
  return h('div', {class: 'security-stack'},
    envelope.status === 'partial' || envelope.status === 'stale'
      ? h('div', {class: 'security-state warning', role: 'status'},
        h('strong', {text: `Fleet evidence is ${envelope.status}`}),
        h('p', {text: `Bound to ${formatDate(envelope.cutoff)}. Missing hosts remain visible.`})) : null,
    h('p', {class: 'muted', text: 'Select a row for desired-versus-observed state and its captured checked-host receipt summary.'}), table);
}

function recommendationActions(ctx, report, row, refresh) {
  if (ctx.features.security.capabilities.manage !== true) return 'View only';
  const available = {
    proposed: [['recommendation.approve', 'Approve'], ['recommendation.reject', 'Reject'], ['recommendation.cancel', 'Cancel']],
    approved: [['recommendation.cancel', 'Cancel']],
    auto_approved: [['recommendation.cancel', 'Cancel']],
    executed: [['recommendation.reverse', 'Reverse']],
    partial: [['recommendation.reverse', 'Reverse']],
  }[row.state] || [];
  if (!available.length) return 'No action';
  return h('div', {class: 'security-row-actions'}, ...available.map(([action, label]) => governedAction({
    report, action, row, idKey: 'recommendation_id', label, refresh,
  })));
}

async function recommendationDetail(row) {
  const content = h('div', {class: 'security-stack'}, h('p', {text: 'Loading complete evidence…'}));
  openModal({title: `Recommendation ${row.id}`, subtitle: row.action || '', content, wide: true});
  try {
    row = await readSecurityDetail('recommendations', row.id);
  } catch (error) {
    content.replaceChildren(h('p', {class: 'form-message', text: error.message}));
    return;
  }
  const expired = row.expires_at && new Date(row.expires_at).valueOf() <= Date.now();
  const fields = [
    ['State', expired ? 'expired' : row.state], ['Action', row.action],
    ['Reason', row.reason_code], ['Confidence', row.confidence],
    ['Urgency', row.urgency], ['Requested scope', row.requested_scope],
    ['Requested TTL (seconds)', row.requested_ttl_seconds],
    ['Targets', row.target_count], ['Validated', row.validated_count],
    ['Protected', row.protected_count], ['Executed', row.executed_count],
    ['Failed', row.failed_count], ['Reversed', row.reversed_count],
    ['Expires', formatDate(row.expires_at)],
  ];
  content.replaceChildren(
      h('dl', {class: 'security-definition-list'}, ...fields.flatMap(([label, value]) => [
        h('dt', {text: label}), h('dd', {text: String(value ?? 'Unavailable')}),
      ])),
      h('section', {}, h('h3', {text: 'Explanation and target evidence'}),
        h('pre', {class: 'security-evidence', text: JSON.stringify(row, null, 2)})),
      h('p', {class: 'muted', text: 'Authentication secrets are hidden; operational addresses, reasons and enforcement errors remain visible.'}));
}

export function renderRecommendations({ctx, report, refresh}) {
  const envelope = report.sections.recommendations;
  if (['unavailable', 'failed'].includes(envelope.status)) {
    return h('div', {class: 'security-state unavailable', role: 'status'},
      h('strong', {text: 'Recommendations unavailable'}),
      h('p', {text: 'No recommendation state is inferred.'}));
  }
  const rows = sectionRows(envelope);
  return h('div', {class: 'security-stack'},
    h('p', {class: 'muted', text: 'Execution and reversal use current revisions, the deployment-configured authentication freshness window and exact typed confirmation.'}),
    new TableView({rows, empty: 'No recommendations in this window.',
      onSelect: recommendationDetail, columns: [
        {label: 'Recommendation', render: (row) => row.action || `Recommendation ${row.id}`},
        {label: 'State', render: (row) => badge(row.state || 'unknown', statusTone(row.state))},
        {label: 'Urgency', render: (row) => badge(row.urgency || 'unknown', statusTone(row.urgency))},
        {label: 'Targets', key: 'target_count'},
        {label: 'Actions', render: (row) => recommendationActions(ctx, report, row, refresh)},
      ]}).render());
}

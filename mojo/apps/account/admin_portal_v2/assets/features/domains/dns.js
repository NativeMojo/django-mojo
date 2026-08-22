// DNS records — the live provider zone, edited as complete record sets.
//
// Ported from v1's advanced/page.js `dnsPage`, `recordEditor` and
// `deleteRecord`. The provider is authoritative: every write is followed by a
// read-back, an unconfirmed result latches the record set, and no further
// write to it runs until Refresh clears the latch. All of that is v1's.
//
// ONE thing is new, and it is a client-side guard only (#2563): the editor
// refuses to delete an NS or SOA record set at the zone apex. The backend is
// unchanged and still permits it — this stops the accident in the one place
// the accident happens, and says why instead of silently hiding the control.
import {api, badge, h, icon, openModal, TableView} from '../../core.js';
import {loadInto, runAction} from '../../components/actions.js';
import {
  canonicalRecordName, confirmNetwork, DNS_TYPES, loadDomains, mutationFailed,
  networkMutations, normalizedValues, postOnce, providerMutation, recordIdentity,
  sameRecordSet, tablePanel,
} from '../../components/network.js';

// Delegation records: the zone's own authority. Deleting either at the apex
// takes the whole zone off the internet, and no amount of confirming makes
// that a thing this editor should carry out.
const PROTECTED_APEX_TYPES = new Set(['NS', 'SOA']);

const APEX_REFUSAL = 'NS and SOA at the zone apex are delegation records; '
  + 'deleting them breaks the zone — this editor refuses. Use the provider '
  + 'console if you truly need to.';

function isApexDelegation(domain, record) {
  const type = String(record.type || '').toUpperCase();
  if (!PROTECTED_APEX_TYPES.has(type)) return false;
  return canonicalRecordName(domain, record.name) === canonicalRecordName(domain, '@');
}

function refuseApexDelete(domain, record) {
  openModal({
    title: `${String(record.type).toUpperCase()} at the ${domain.name} apex is protected`,
    content: h('div', {class: 'callout warning'}, icon('alert'),
      h('p', {text: APEX_REFUSAL})),
  });
}

function queryParam(name) {
  const query = location.hash.split('?')[1] || '';
  return new URLSearchParams(query).get(name);
}

function recordEditor(domain, record, records, refresh) {
  const original = record ? {...record, record_values: [...(record.record_values || [])]} : null;
  const type = h('select', {disabled: Boolean(record)}, ...DNS_TYPES.map((value) => h('option', {value, text: value, selected: value === (record?.type || 'A')})));
  type.value = record?.type || 'A';
  const name = h('input', {value: record?.name || '', disabled: Boolean(record), placeholder: '@ or www'});
  const ttl = h('input', {type: 'number', min: '30', max: '86400', value: record?.ttl || 300});
  const values = h('textarea', {rows: '7', text: (record?.record_values || []).join('\n'), placeholder: 'One complete record value per line'});
  const message = h('div', {class: 'form-message', role: 'alert'});
  // responsiveness-exempt: the first await here is a confirm — a human deciding
  // whether to replace a record set. Nothing paints until they have answered;
  // the write that follows carries the scrim.
  const save = h('button', {class: 'button primary', onclick: async () => {
    const target = {type: type.value, name: name.value.trim(), ttl: Number(ttl.value), record_values: normalizedValues(values.value.split('\n'))};
    if (!target.name || !target.record_values.length) { message.textContent = 'Name and at least one value are required.'; return; }
    const removed = normalizedValues(original?.record_values || []).filter((value) => !target.record_values.includes(value));
    if (removed.length) {
      const confirmed = await confirmNetwork({title: 'Replace the complete record set?', copy: `Saving replaces every value for ${target.type} ${target.name}. These values will be removed: ${removed.join(', ')}`, confirmLabel: 'Replace record set', danger: true});
      if (!confirmed) return;
    }
    message.textContent = '';
    // A provider write that lands unconfirmed latches the whole record set;
    // nothing else on the page may be clicked while it is in flight.
    await runAction(save, async () => {
      const before = await refresh(false);
      const current = before.find((row) => recordIdentity(domain, row) === recordIdentity(domain, target));
      if (!original && current) {
        close(); recordEditor(domain, current, before, refresh); return;
      }
      if (original) {
        if (!current || !sameRecordSet(current, original)) throw new Error('The provider record set changed, including its TTL or values. Review the refreshed set and confirm again.');
      }
      const key = `dns:${domain.id}:${recordIdentity(domain, target)}`;
      await providerMutation(key,
        () => postOnce('/api/dnsman/dns', {domain: domain.id, ...target}),
        () => refresh(false),
        (observed) => {
          const applied = observed.find((row) => recordIdentity(domain, row) === recordIdentity(domain, target));
          return applied && sameRecordSet(applied, target) ? 'applied' : 'unconfirmed';
        });
      close(); await refresh(false);
    }, {
      busy: {title: 'Saving the record set…', detail: 'Writing to the provider, then reading the zone back.'},
      restoreOnSuccess: false,
      onError: (error) => { message.textContent = error.message; },
    });
  }}, icon('check'), 'Save record set');
  const close = openModal({title: record ? `Edit ${record.type} record` : 'Add DNS record', subtitle: 'The provider stores one complete set for each type and name.', wide: true, content: h('div', {},
    h('div', {class: 'field-grid'}, h('label', {class: 'field'}, h('span', {text: 'Type'}), type), h('label', {class: 'field'}, h('span', {text: 'Name'}), name), h('label', {class: 'field'}, h('span', {text: 'TTL'}), ttl)),
    // An apex delegation set may still be EDITED — replacing the nameserver
    // list is a legitimate migration. Only removing the set outright is refused,
    // and the editor says so where the operator is already reading.
    record && isApexDelegation(domain, record)
      ? h('p', {class: 'muted small', text: APEX_REFUSAL}) : null,
    h('label', {class: 'field'}, h('span', {text: 'Complete values'}), values, h('small', {text: 'MX, SRV, and CAA values retain their provider wire format; one value per line.'})), message, h('div', {class: 'form-actions'}, save))});
}

async function deleteRecord(domain, record, refresh) {
  // Client-side only, and deliberately before the confirm: there is no version
  // of this the operator can talk themselves into here.
  if (isApexDelegation(domain, record)) { refuseApexDelete(domain, record); return; }
  const confirmed = await confirmNetwork({title: `Delete ${record.type} record?`, copy: `Delete the complete ${record.type} set at ${record.name}? This request is attempted once and then reconciled from the provider.`, confirmLabel: 'Delete record set', danger: true});
  if (!confirmed) return;
  const key = `dns:${domain.id}:${recordIdentity(domain, record)}`;
  await runAction(null, async () => {
    await providerMutation(key,
      () => postOnce('/api/dnsman/dns/delete', {domain: domain.id, type: record.type, name: record.name, record_values: record.record_values}),
      () => refresh(false),
      (observed) => (observed.some((row) => recordIdentity(domain, row) === recordIdentity(domain, record)) ? 'unconfirmed' : 'applied'));
    await refresh(false);
  }, {
    key: `delete-${key}`,
    busy: {title: `Deleting ${record.type} ${record.name}…`, detail: 'Writing to the provider, then reading the zone back.'},
    onError: (error) => mutationFailed('The record set was not deleted', error),
  });
}

export async function dnsTab(ctx, actions) {
  const root = h('div', {class: 'domains-tab'}); let domains = []; let active = null; let records = [];
  async function fetchRecords(explicit = false) {
    if (!active) return [];
    const payload = await api(`/api/dnsman/dns?domain=${encodeURIComponent(active.id)}`);
    records = payload.records || [];
    if (explicit) networkMutations.clearPrefix(`dns:${active.id}:`);
    render(); return records;
  }
  function render() {
    const picker = h('select', {'aria-label': 'Managed domain'}, h('option', {value: '', text: 'Choose a domain'}), ...domains.map((row) => h('option', {value: row.id, text: row.name, selected: row.id === active?.id})));
    picker.value = active?.id || '';
    // A <select> gets aria-disabled and the announcement, never a label swap:
    // rewriting the chosen option under the pointer is its own bug.
    picker.addEventListener('change', () => runAction(picker, async () => {
      active = domains.find((row) => String(row.id) === picker.value) || null; records = [];
      if (active) await fetchRecords(false); else render();
    }, {announceLabel: 'Loading the zone…'}));
    actions.replaceChildren(...[
      h('label', {class: 'toolbar-select'}, icon('globe'), picker),
      active ? h('button', {class: 'button ghost', onclick: (event) => runAction(event.currentTarget,
        () => fetchRecords(true), {announceLabel: 'Refreshing the zone…'})}, icon('refresh'), 'Refresh') : null,
      active && ctx.capabilities.manage_network ? h('button', {class: 'button primary', onclick: () => recordEditor(active, null, records, fetchRecords)}, icon('plus'), 'Add record') : null,
    ].filter(Boolean));
    root.replaceChildren();
    if (!active) { root.append(h('section', {class: 'panel empty'}, h('p', {text: 'Choose a managed domain to load its authoritative provider zone.'}))); return; }
    const latched = [...networkMutations.refreshRequired].some((key) => key.startsWith(`dns:${active.id}:`));
    if (latched) root.append(h('div', {class: 'callout warning'}, icon('alert'), h('div', {}, h('strong', {text: 'Refresh required'}), h('p', {text: 'A provider result could not be confirmed. No further write to that record set will run until Refresh succeeds.'}))));
    const panel = tablePanel(active.name, `${records.length} live provider record sets`); root.append(panel);
    panel.append(new TableView({rows: records, empty: 'This provider zone has no records.', onSelect: ctx.capabilities.manage_network ? (row) => recordEditor(active, row, records, fetchRecords) : null, columns: [
      {label: 'Type', render: (row) => badge(row.type, 'neutral')},
      {label: 'Name', render: (row) => h('span', {class: 'mono', text: row.name})},
      {label: 'Complete values', render: (row) => h('div', {class: 'record-values'}, ...(row.record_values || []).map((value) => h('code', {text: value})))},
      {label: 'TTL', render: (row) => String(row.ttl)},
      // The control stays on a protected row on purpose: a missing button
      // teaches nothing, and clicking this one produces the sentence that does.
      {label: '', render: (row) => (ctx.capabilities.manage_network ? h('button', {
        class: 'icon-button danger-text',
        'aria-label': isApexDelegation(active, row)
          ? `${row.type} ${row.name} is protected and cannot be deleted here`
          : `Delete ${row.type} ${row.name}`,
        title: isApexDelegation(active, row) ? APEX_REFUSAL : '',
        onclick: (event) => { event.stopPropagation(); return deleteRecord(active, row, fetchRecords); },
      }, icon('trash')) : null)},
    ]}).render());
  }
  // Nothing can be shown until the domain list lands, and a failed read used to
  // leave a bare sentence with no way to try again.
  async function load() {
    await loadInto(root, async () => {
      domains = (await loadDomains()).filter((row) => row.status === 'active');
      active = domains.find((row) => String(row.id) === String(queryParam('domain'))) || domains[0] || null;
      if (active) await fetchRecords(false); else render();
    }, {message: 'Loading domains…', retry: load});
  }
  await load();
  return root;
}

// Capacity panel — add or remove an app node, an RDS reader, a cache replica.
//
// Mounted inside the Dashboard's fleet drill-in rather than on a page of its
// own: an operator decides to add or remove capacity while looking at the
// fleet, and a separate destination would mean reading the fleet in one place
// and acting on it in another.
//
// Two rules the panel never breaks:
//
//   1. Every control the server would refuse is ABSENT, replaced by the
//      reason. The report carries `actions[name].offered` and
//      `blocked_reason`; nothing here re-derives them, so a control can never
//      disagree with the gate behind it.
//   2. Every confirmation states the consequence in plain words and demands
//      the identifier typed back. Two of them also state something the
//      operator would otherwise assume wrongly — a reader this app does not
//      read from, and a replica that is failover capacity rather than speed.
import {api, apiOnce, h, icon} from '../../core.js';
import {runAction} from '../../components/actions.js';
import {openModal} from '../../components/overlays.js';
import {rowSection, statusHeadline, statusRow} from '../../components/rows.js';
import {errorState, loadingState} from '../../components/views.js';

const CAPACITY_PATH = '/api/aws/capacity';
const STATUS_PATH = '/api/aws/capacity/status';
const APPLY_PATH = '/api/aws/capacity/apply';

const POLL_INTERVAL = 10000;
// 90 minutes at one poll every ten seconds — above the server's
// CACHE_RESIZE_TIMEOUT (5400s), so a slow-but-succeeding rolling resize is
// never abandoned by the page while the server still allows it.
const POLL_LIMIT = 540;

const ADD_NODE = 'add_node';
const DRAIN_NODE = 'drain_node';
const TERMINATE_NODE = 'terminate_node';
const ADD_READER = 'add_reader';
const REMOVE_READER = 'remove_reader';
const SET_CACHE_REPLICAS = 'set_cache_replicas';
const ENABLE_STABLE_IPS = 'enable_stable_ips';
const DISABLE_STABLE_IPS = 'disable_stable_ips';

// Never a dead control: when the server would refuse, the row says which thing
// is in the way instead of offering a button that fails.
// Exported: the Fleet Scaling page (fleet.js) renders the same server offers
// and must say the same words for the same blocked_reason.
export const BLOCKED_COPY = {
  infrastructure_external: 'external infrastructure mode — capacity is applied by your infrastructure team\'s IaC',
  node_id_pinned: 'this fleet pins EDGE_NODE_ID, so a new node could never prove its own identity — remove the pin first',
  no_source_node: 'no healthy, running node is available to clone',
  instances_unavailable: 'EC2 did not answer, so node inventory and controls are unavailable',
  last_healthy_target: 'this is the last healthy target — removing it would take the fleet out of service',
  no_database: 'no RDS database was found in this region',
  databases_unavailable: 'RDS did not answer completely, so database controls are unavailable',
  no_reader: 'this database has no reader to remove',
  no_cache_group: 'no ElastiCache replication group was found in this region',
  caches_unavailable: 'ElastiCache did not answer completely, so cache controls are unavailable',
  resource_transitioning: 'AWS reports a member transition in progress — inspect and refresh instead of replaying a mutation',
  cluster_mode_unsupported: 'cluster-mode enabled — its replica count is a resharding decision, not a capacity change',
  already_enabled: 'on — every node holds its stable address',
  not_enabled: 'stable outbound IPs are off and nothing is attached',
  no_fleet_nodes: 'no node is registered behind a load balancer',
  fleet_unavailable: 'the serving tier could not be read, so the fleet is unknown — not empty',
  addresses_unavailable: 'the region\'s Elastic IPs could not be read',
  policy_unavailable: 'the stable-IPs policy could not be read — its state is unknown, not off',
};

export const PHASE_COPY = {
  capturing: 'capturing an image of a healthy node (no reboot)',
  launching: 'launching the new node',
  booting: 'waiting for it to join the job fleet',
  converging: 'deploying the fleet\'s last converged commit to it',
  proving: 'waiting for proof it is running that commit',
  registering: 'registering the proven node behind the balancer',
  settling: 'waiting for the balancer to report it healthy',
  draining: 'draining connections out of it',
  terminating: 'terminating',
  creating: 'creating',
  deleting: 'deleting',
  scaling: 'changing the replica count',
  resizing: 'changing the instance size',
  addressing: 'attaching the fleet\'s stable outbound address',
  planning: 'planning which node gets which address',
  associating: 'attaching stable addresses',
  verifying: 're-reading AWS to prove the result',
  detaching: 'detaching stable addresses',
  complete: 'done',
};

const EXTERNAL_SUB = 'Infrastructure mode is external, so this portal does not change capacity.';
const CONVERGENCE_NOTE = 'Fleet readiness reads pending until the convergence sweep finishes — that is expected right after an add.';

function phaseText(record) {
  if (record.state === 'failed') return record.message || 'failed';
  if (record.state === 'done') return record.message || 'done';
  const phases = record.phases || [];
  const index = phases.indexOf(record.phase);
  const step = index >= 0 ? `step ${index + 1} of ${phases.length} · ` : '';
  return `${step}${PHASE_COPY[record.phase] || record.phase || 'working'}`;
}

function blockedDetail(action) {
  if (!action || action.offered) return '';
  return BLOCKED_COPY[action.blocked_reason]
    || String(action.blocked_reason || 'this action is not available').replaceAll('_', ' ');
}

// Optional placement for every Add Node surface in the legacy portal. The
// returned DOM node is deliberately separate from the state API: callers must
// append `.element`, never the wrapper object itself.
export function addNodePlacementControls(report, initial = {}, callbacks = {}) {
  let sourceInstance = String(initial.source_instance || '');
  let subnetId = String(initial.subnet_id || '');
  const healthy = (report?.nodes?.instances || []).filter((row) => row.healthy);
  if (sourceInstance && !healthy.some((row) => row.id === sourceInstance)) sourceInstance = '';
  const groups = new Map((report?.nodes?.groups || []).map(
    (group) => [group.arn, group.name || group.arn]));
  const listId = `add-node-subnets-${Math.random().toString(36).slice(2)}`;

  const sourceLabel = (row) => {
    const fleets = (row.groups || []).map((arn) => groups.get(arn) || arn);
    const node = row.name && row.name !== row.id ? `${row.name} (${row.id})` : row.id;
    return [node, ...fleets, row.zone, row.subnet_id]
      .filter(Boolean).join(' · ');
  };
  const selected = () => healthy.find((row) => row.id === sourceInstance) || null;
  const values = () => ({
    ...(sourceInstance ? {source_instance: sourceInstance} : {}),
    ...(subnetId.trim() ? {subnet_id: subnetId.trim()} : {}),
  });
  const valid = () => !subnetId.trim() || (subnetId.trim().startsWith('subnet-')
    && subnetId.trim().length > 'subnet-'.length);

  const datalist = h('datalist', {id: listId});
  const summary = h('span', {class: 'placement-summary'});
  const error = h('span', {class: 'placement-error', role: 'alert'});
  const subnet = h('input', {type: 'text', value: subnetId, list: listId,
    'data-placement-control': 'subnet',
    autocomplete: 'off', 'aria-describedby': `${listId}-summary ${listId}-error`});
  summary.id = `${listId}-summary`;
  error.id = `${listId}-error`;

  const paint = () => {
    const row = selected();
    const zone = row?.zone || '';
    const subnetOptions = [...new Set(healthy
      .filter((candidate) => !zone || candidate.zone === zone)
      .map((candidate) => candidate.subnet_id).filter(Boolean))];
    datalist.replaceChildren(...subnetOptions.map((value) => h('option', {value})));
    subnet.placeholder = row?.subnet_id
      ? `Optional — source uses ${row.subnet_id}` : 'Optional subnet-…';
    const ok = valid();
    subnet.setAttribute('aria-invalid', ok ? 'false' : 'true');
    error.textContent = ok ? '' : 'Subnet must start with subnet- and include an identifier.';
    summary.textContent = sourceInstance
      ? `Clone ${sourceLabel(row)}${subnetId.trim() ? ` into ${subnetId.trim()}` : '; choose subnet automatically'}`
      : subnetId.trim() ? `Choose a healthy source automatically; place it in ${subnetId.trim()}`
        : 'Choose a healthy source and subnet automatically.';
  };

  let commitTimer = null;
  const commit = (focusControl = '') => {
    clearTimeout(commitTimer);
    // change precedes blur for a typed input. Queue both so blur can replace
    // the fallback identity with its explicit Tab/Shift+Tab destination.
    commitTimer = setTimeout(
      () => callbacks.onCommit?.(values(), valid(), focusControl), 0);
  };
  const source = h('select', {'data-placement-control': 'source',
    onchange: (event) => {
      sourceInstance = event.target.value;
      paint();
      callbacks.onInput?.(values(), valid());
      commit(event.currentTarget.dataset.placementControl);
    }},
  h('option', {value: '', text: 'Automatic (healthy source)'}),
  ...healthy.map((row) => h('option', {value: row.id, text: sourceLabel(row),
    selected: row.id === sourceInstance ? true : null})));
  subnet.addEventListener('input', () => {
    subnetId = subnet.value;
    paint();
    callbacks.onInput?.(values(), valid());
  });
  subnet.addEventListener('change', (event) => {
    commit(event.currentTarget.dataset.placementControl);
  });
  subnet.addEventListener('blur', (event) => {
    commit(event.relatedTarget?.dataset?.placementControl || '');
  });

  const element = h('div', {class: 'add-node-placement'},
    h('label', {class: 'placement-field'},
      h('span', {class: 'fleet-label', text: 'Clone from'}), source),
    h('label', {class: 'placement-field'},
      h('span', {class: 'fleet-label', text: 'Subnet'}), subnet, datalist),
    summary, error);
  paint();
  return {element, values, valid};
}

// ---------------------------------------------------------------------------
// Confirmation
//
// A bespoke modal rather than confirmAction(): the operator is reading a
// consequence, sometimes choosing a number, and always echoing an identifier.
// The typed echo is what separates "the right node" from "the row I happened
// to click".
// ---------------------------------------------------------------------------

function confirm({title, subtitle, expect, verb, consequence, notes = [], extra = null,
                  valid = () => true}) {
  return new Promise((resolve) => {
    let settled = false;
    const settle = (value) => { if (settled) return; settled = true; close(); resolve(value); };
    const typed = h('input', {autocomplete: 'off', placeholder: expect});
    const go = h('button', {class: 'button danger', type: 'button', disabled: true,
      onclick: () => settle({confirmed: true})}, verb);
    const validate = () => { go.disabled = typed.value.trim() !== expect || !valid(); };
    typed.addEventListener('input', validate);
    const content = h('div', {},
      h('div', {class: 'callout warning'}, icon('alert'), h('p', {text: consequence})),
      ...notes.filter(Boolean).map((text) => h('p', {class: 'modal-copy', text})),
      extra,
      h('label', {class: 'field'},
        h('span', {text: `Type ${expect} to confirm`}), typed),
      h('div', {class: 'form-actions'},
        h('button', {class: 'button ghost', type: 'button',
          onclick: () => settle(null)}, 'Cancel'),
        go));
    content.addEventListener('input', validate);
    content.addEventListener('change', validate);
    const close = openModal({
      title, subtitle, content, danger: true,
      onClose: () => { if (!settled) { settled = true; resolve(null); } },
    });
    validate();
  });
}

function selfCheckNote(report) {
  return report?.nodes?.self_check === 'matched' ? '' :
    'This portal could not match any registered instance to the node answering '
    + 'your request, so it cannot confirm you are not removing the node you are '
    + 'talking to. Check the instance id against the console before confirming.';
}

function replicaChoice(current, min, onChange) {
  const name = `replica-count-${Math.random().toString(36).slice(2)}`;
  const options = [];
  for (let value = min; value <= Math.max(current + 1, min + 2); value += 1) options.push(value);
  return h('div', {class: 'maintenance-windows'}, ...options.map((value) => {
    const input = h('input', {type: 'radio', name, value: String(value),
      checked: value === current ? true : null,
      onchange: () => onChange(value)});
    const copy = value === current ? 'no change'
      : value === 0 ? 'no standby at all — the primary becomes a single point of failure'
        : value > current ? `${value - current} more standby node(s)` : `${current - value} fewer standby node(s)`;
    return h('label', {class: 'check-field maintenance-window'}, input,
      h('span', {}, h('strong', {text: `${value} replica${value === 1 ? '' : 's'}`}),
        h('small', {text: copy})));
  }));
}

// ---------------------------------------------------------------------------

export async function capacityPanel(ctx, signal = null) {
  const body = h('div', {class: 'row-page capacity-body'}, loadingState('Loading capacity…'));
  const root = h('div', {class: 'dash-block capacity-panel'},
    h('h3', {class: 'dash-subhead', text: 'Capacity'}), body);

  // Key -> the progress line currently replacing that row's control.
  const progress = new Map();
  let report = null;
  let busy = false;

  // A MISSING flag means managed: an older server that never learned about
  // INFRASTRUCTURE_MODE must not have its controls silently disabled.
  const managed = () => report?.mode !== 'external';
  const action = (name) => report?.actions?.[name] || {offered: false, blocked_reason: null};

  function wait(milliseconds) {
    return new Promise((resolve) => {
      const timeout = setTimeout(resolve, milliseconds);
      signal?.addEventListener('abort',
        () => { clearTimeout(timeout); resolve(); }, {once: true});
    });
  }

  // The drill-in this panel lives in has no abort signal of its own, so a
  // closed inspector is detected by the panel leaving the document. Without
  // it, closing the overlay mid-operation would leave 90 minutes of polling
  // rendering into a detached tree.
  function stopped() {
    return signal?.aborted || !root.isConnected;
  }

  function note(key, tone, text) {
    progress.set(key, {tone, text});
    render();
  }

  // Poll the operation to a terminal state. Never "AWS accepted it" — the
  // service only reports done once the thing is proven, and a panel that
  // stopped watching at "requested" would be reporting a wish.
  async function follow(key, operation) {
    const params = new URLSearchParams({operation});
    for (let count = 0; count < POLL_LIMIT && !stopped(); count += 1) {
      await wait(POLL_INTERVAL);
      if (stopped()) return;
      let live;
      try {
        live = await api(`${STATUS_PATH}?${params.toString()}`, {signal});
      } catch (error) {
        if (error?.name === 'AbortError') return;
        note(key, 'warn', `progress unavailable: ${error.message}`);
        continue;
      }
      const warnings = (live.warnings || []).map((row) => row.message).join(' ');
      if (live.state === 'running') {
        note(key, 'warn', [phaseText(live), warnings].filter(Boolean).join(' · '));
        continue;
      }
      if (live.state === 'done') {
        note(key, 'ok', [phaseText(live), warnings].filter(Boolean).join(' · '));
        await load(true);
        return;
      }
      note(key, 'danger', [phaseText(live), warnings].filter(Boolean).join(' · '));
      await load(true);
      return;
    }
    if (!stopped()) {
      note(key, 'warn', 'still running after 90 minutes — check the AWS console');
    }
  }

  async function submit(key, payload) {
    if (busy || !managed()) return;
    busy = true;
    note(key, 'warn', 'requesting…');
    let started;
    try {
      started = await apiOnce(APPLY_PATH, {method: 'POST', signal,
        body: JSON.stringify(payload)});
    } catch (error) {
      busy = false;
      if (error?.code === 'fresh_auth_required') { progress.delete(key); render(); return; }
      note(key, 'danger', error.message);
      return;
    }
    busy = false;
    note(key, 'warn', phaseText(started));
    follow(key, started.id);
  }

  // ── actions ─────────────────────────────────────────────────────────────

  async function addNode() {
    const placement = addNodePlacementControls(report);
    const choice = await confirm({
      title: 'Add a node?',
      subtitle: 'Automatic placement unless you choose otherwise',
      expect: ADD_NODE,
      verb: 'Add node',
      consequence:
        'A new EC2 instance is cloned from a healthy fleet member — keeping its '
        + 'instance type, security groups and instance role. You may choose the '
        + 'healthy source and a same-zone subnet, or leave either automatic. It is registered behind '
        + 'the balancer ONLY after it proves it is running the fleet\'s last '
        + 'converged commit.',
      notes: [
        'If it never proves, it is left running and unregistered: it costs money '
        + 'and serves nothing, and you remove it with Terminate. That is the safe '
        + 'side of this trade — the other side is production traffic on a box '
        + 'running unknown code.',
        CONVERGENCE_NOTE,
      ],
      extra: placement.element,
      valid: placement.valid,
    });
    if (!choice) return;
    submit(ADD_NODE, {action: ADD_NODE, confirm_resource: ADD_NODE,
      ...placement.values()});
  }

  async function drainNode(row) {
    const choice = await confirm({
      title: `Drain ${row.id}?`,
      subtitle: row.name && row.name !== row.id ? row.name : '',
      expect: row.id,
      verb: 'Drain node',
      consequence:
        `This takes ${row.id} out of the serving path. Existing connections finish; `
        + 'no new ones arrive. The instance keeps running and keeps billing — '
        + 'draining does not terminate it.',
      notes: [
        selfCheckNote(report),
        row.primary ? 'This node is the certbot primary: it renews the fleet\'s '
          + 'certificates. Draining it does not move that job.' : '',
      ],
    });
    if (!choice) return;
    submit(row.id, {action: DRAIN_NODE, resource: row.id, confirm_resource: row.id});
  }

  async function terminateNode(row) {
    const choice = await confirm({
      title: `Terminate ${row.id}?`,
      subtitle: row.name && row.name !== row.id ? row.name : '',
      expect: row.id,
      verb: 'Terminate node',
      consequence:
        `This destroys ${row.id} permanently. It cannot be undone, and anything on `
        + 'its local disk goes with it.',
      notes: [
        'The server re-checks that the node has finished draining before it '
        + 'terminates anything — a node that is still registered is refused here.',
        selfCheckNote(report),
      ],
    });
    if (!choice) return;
    submit(row.id, {action: TERMINATE_NODE, resource: row.id, confirm_resource: row.id});
  }

  async function addReader(row) {
    const choice = await confirm({
      title: `Add a reader to ${row.identifier}?`,
      subtitle: row.kind === 'aurora' ? 'Aurora cluster' : 'standalone instance',
      expect: row.identifier,
      verb: 'Add reader',
      consequence:
        'A new database instance is created and kept in sync. It bills by the hour '
        + 'from the moment it exists, whether or not anything reads from it.',
      notes: [
        report?.reader_routing?.database?.active
          ? 'Reader routing is on: once this reader is available, safe reads '
            + 'spread across the cluster\'s replicas automatically.'
          : 'Reader routing is OFF on this node: every query goes to the primary. '
            + 'This adds standby read capacity and an endpoint string — nothing '
            + 'gets faster until DATABASE_READER_HOST is configured and the '
            + 'fleet restarts onto it.',
        row.reader_endpoint ? `This cluster's reader endpoint is ${row.reader_endpoint}.` : '',
      ],
    });
    if (!choice) return;
    submit(row.identifier, {action: ADD_READER, resource: row.identifier,
      confirm_resource: row.identifier});
  }

  async function removeReader(identifier) {
    const choice = await confirm({
      title: `Remove ${identifier}?`,
      subtitle: 'read replica',
      expect: identifier,
      verb: 'Remove reader',
      consequence:
        `This deletes ${identifier} with NO final snapshot. It is a copy of data `
        + 'that lives on the primary, so nothing unique is lost — but the instance '
        + 'itself does not come back.',
      notes: [
        'The server re-reads that this really is a reader before deleting anything. '
        + 'A primary database can never be removed here.',
      ],
    });
    if (!choice) return;
    submit(identifier, {action: REMOVE_READER, resource: identifier,
      confirm_resource: identifier});
  }

  async function setReplicas(row) {
    const current = Number(row.replica_count || 0);
    const min = Number(row.min_replicas || 0);
    let wanted = current;
    const choice = await confirm({
      title: `Change replicas on ${row.identifier}?`,
      subtitle: `${current} replica${current === 1 ? '' : 's'} today`,
      expect: row.identifier,
      verb: 'Apply replica count',
      consequence:
        'ElastiCache applies a replica-count change IMMEDIATELY — there is no '
        + 'maintenance-window option for it. Adding a replica syncs a new node; '
        + 'removing one takes a node away now.',
      notes: [
        report?.reader_routing?.redis?.active
          ? 'A replica in this group is failover capacity first. Reader reads are '
            + 'on, so lag-tolerant reads (metrics, dashboards) also use the '
            + 'reader endpoint — writes never get faster.'
          : 'A replica in this group is FAILOVER capacity, not read throughput: '
            + 'reader reads are off on this node, so adding one does not make '
            + 'anything faster.',
        min > 0 ? 'Automatic failover is on, so this group must keep at least one '
          + 'replica — zero is not offered.'
          : 'Automatic failover is off on this group. Going to zero leaves nothing '
          + 'to fail over to: if the primary dies, the cache is gone until it is '
          + 'rebuilt.',
      ],
      extra: replicaChoice(current, min, (value) => { wanted = value; }),
    });
    if (!choice) return;
    if (wanted === current) {
      note(row.identifier, 'warn', 'no change requested');
      return;
    }
    submit(row.identifier, {action: SET_CACHE_REPLICAS, resource: row.identifier,
      confirm_resource: row.identifier, count: wanted, apply_immediately: true});
  }

  // ── rows ────────────────────────────────────────────────────────────────

  // † and human-input in one control. `run` opens a typed-echo confirmation
  // first, so nothing may paint until the operator has answered; once they
  // have, submit() replaces this row with its own progress line, which means
  // there is no surviving node here to pin a pending state to either. So the
  // wrapper is headless: all it adds is the guard, keyed on the link, so a
  // double click cannot stack two confirmation modals for the same row.
  function wire(row, run) {
    const link = row.querySelector('.row-link');
    link?.addEventListener('click', (event) => {
      event.preventDefault();
      runAction(null, () => run(), {key: link});
    });
    return row;
  }

  function progressRow(key, name, value) {
    const running = progress.get(key);
    if (!running) return null;
    return statusRow({tone: running.tone, name, value, mono: true,
      detail: running.text,
      detailTone: running.tone === 'danger' ? 'danger'
        : running.tone === 'ok' ? '' : 'warning'});
  }

  function nodeRow(row) {
    const value = (row.registered ? row.state : row.instance_state) || 'unknown';
    const running = progressRow(row.id, row.id, value);
    if (running) return running;
    const labels = [
      row.name && row.name !== row.id ? row.name : '',
      row.public_ip ? `${row.public_ip}${row.stable_ip ? ' (stable)' : ''}` : '',
      row.self ? 'this node' : '',
      row.primary ? 'certbot primary' : '',
      row.added_by_capacity ? 'added here' : '',
    ].filter(Boolean).join(' · ');
    const drain = action(DRAIN_NODE);
    const terminate = action(TERMINATE_NODE);
    // A registered node is drained first; an already-drained one is terminated.
    // Never both at once — the two are separate decisions and the server
    // refuses a terminate that has not been drained.
    const draining = ['draining', 'unhealthy.draining'].includes(row.state);
    const drained = row.state === 'unused';
    let control = null;
    if (row.self) {
      control = null;
    } else if (drained && terminate.offered) {
      control = {label: 'Terminate…', run: () => terminateNode(row)};
    } else if (!drained && !draining && drain.offered && row.can_drain) {
      control = {label: 'Drain…', run: () => drainNode(row)};
    }
    const blocked = row.self
      ? 'this is the node answering your request'
      : draining ? 'draining — terminate becomes available when it finishes'
        : !row.registered ? 'not registered — inspect and reconcile provider state'
          : drained ? blockedDetail(terminate) : blockedDetail(drain);
    const node = statusRow({
      tone: row.healthy ? 'ok' : drained ? 'muted' : 'warn',
      name: row.id, value, mono: true,
      detail: [labels, control ? '' : blocked].filter(Boolean).join(' · '),
      detailTone: control ? '' : 'warning',
      action: control ? {label: control.label, href: '#'} : null,
    });
    return control ? wire(node, control.run) : node;
  }

  function addNodeRow() {
    const running = progressRow(ADD_NODE, 'New node', '');
    if (running) return running;
    const add = action(ADD_NODE);
    if (!add.offered) {
      return statusRow({tone: 'muted', name: 'Add a node', value: '',
        detail: blockedDetail(add), detailTone: 'warning'});
    }
    return wire(statusRow({tone: 'muted', name: 'Add a node',
      value: '', detail: 'clones a healthy member and proves it before it serves',
      action: {label: 'Add node…', href: '#'}}), addNode);
  }

  // ── stable outbound addresses ───────────────────────────────────────────

  async function enableStableIps() {
    const egress = report?.egress || {};
    const reserved = (egress.reserved || []).map((row) => row.public_ip).filter(Boolean);
    const toAllocate = Number(egress.to_allocate || 0);
    const pending = (egress.pending_nodes || []).length;
    const choice = await confirm({
      title: 'Turn on stable outbound IPs?',
      subtitle: pending ? `${pending} node(s) need an address` : 'already converged',
      expect: ENABLE_STABLE_IPS,
      verb: 'Enable stable IPs',
      consequence:
        'Every registered node gets a permanent Elastic IP for its outbound '
        + 'calls. Attaching swaps a node\'s public source address, which briefly '
        + 'cuts in-flight OUTBOUND connections; inbound service through the '
        + 'balancer is unaffected. When it finishes, the panel shows the exact '
        + 'addresses to give providers.',
      notes: [
        toAllocate > 0
          ? `${toAllocate} new address(es) will be allocated — no net new monthly `
            + 'cost while attached, since each replaces the node\'s identical '
            + 'auto-assigned-IPv4 charge. AWS\'s default quota is 5 public IPv4s '
            + 'per region; a quota error names the fix.'
          : 'No new address needs to be allocated.',
        reserved.length
          ? `Reserved address(es) are reused first: ${reserved.join(', ')}.` : '',
        'Only currently-registered nodes converge — a drained-but-running node '
        + 'is not included. Nodes added later receive their address '
        + 'automatically before they serve.',
      ],
    });
    if (!choice) return;
    submit(ENABLE_STABLE_IPS,
      {action: ENABLE_STABLE_IPS, confirm_resource: ENABLE_STABLE_IPS});
  }

  async function disableStableIps() {
    const egress = report?.egress || {};
    const list = (egress.addresses || []).join(', ');
    const choice = await confirm({
      title: 'Turn off stable outbound IPs?',
      subtitle: list,
      expect: DISABLE_STABLE_IPS,
      verb: 'Disable stable IPs',
      consequence:
        'Providers that allowlisted these addresses may start refusing this '
        + 'fleet\'s calls the moment they detach. Each node is left on a NEW '
        + 'auto-assigned address after a gap of up to a few minutes — or with '
        + 'no public address at all if its network interface was not launched '
        + 'with auto-assign — and outbound provider calls fail during that gap.',
      notes: [
        'The addresses are KEPT reserved, so re-enabling restores the exact '
        + 'same allowlisted addresses. From that moment each reservation bills '
        + '~$3.60/month on top of the node\'s auto-assigned address.',
        'Releasing the reserved addresses is deliberately not part of this '
        + 'switch — that stays an AWS console action.',
      ],
    });
    if (!choice) return;
    submit(DISABLE_STABLE_IPS,
      {action: DISABLE_STABLE_IPS, confirm_resource: DISABLE_STABLE_IPS});
  }

  function egressRows() {
    const egress = report?.egress || {};
    const running = progressRow(ENABLE_STABLE_IPS, 'Stable outbound IPs', '')
      || progressRow(DISABLE_STABLE_IPS, 'Stable outbound IPs', '');
    if (running) return [running];
    // A failed read is UNKNOWN, never an empty allowlist — or an "off" —
    // rendered as canonical.
    if (!egress.available || egress.policy_available === false) {
      return [statusRow({tone: 'warn', name: 'Stable outbound IPs',
        value: 'Unknown',
        detail: egress.fleet_available === false
          ? BLOCKED_COPY.fleet_unavailable
          : egress.addresses_available === false
            ? BLOCKED_COPY.addresses_unavailable
            : BLOCKED_COPY.policy_unavailable,
        detailTone: 'warning'})];
    }
    // Balancer-less install: no fleet for the toggle to manage, but a node
    // holding an address is still the answer an operator came for. Read-only —
    // no control is offered, and the rows never feed the offers.
    const fallback = egress.fallback_attached || [];
    const fleetless = !(egress.attached || []).length
      && !(egress.pending_nodes || []).length;
    if (fleetless && fallback.length) {
      const ips = [...new Set(fallback.map((row) => row.public_ip)
        .filter(Boolean))].join('  ');
      return [
        statusRow({tone: 'ok', name: 'Stable outbound IPs', value: 'external',
          detail: `give providers: ${ips} · managed outside this portal — no `
            + 'load balancer here, so there is nothing for this control to do',
          mono: true}),
        ...fallback.map((row) => statusRow({
          tone: 'muted', name: row.instance_name || row.instance,
          value: row.public_ip || '', mono: true,
          detail: 'stable address · attach or detach in the AWS console'})),
      ];
    }
    const rows = [];
    const pending = egress.pending_nodes || [];
    const list = (egress.addresses || []).join('  ');
    const enable = action(ENABLE_STABLE_IPS);
    const disable = action(DISABLE_STABLE_IPS);
    rows.push(statusRow({
      tone: egress.enabled ? (pending.length ? 'warn' : 'ok') : 'muted',
      name: 'Stable outbound IPs',
      value: egress.enabled ? (pending.length ? 'on (incomplete)' : 'on') : 'off',
      detail: list
        ? `give providers: ${list}`
          + (pending.length ? ` · ${pending.length} node(s) still without a stable address` : '')
        : (pending.length
          ? `${pending.length} node(s) without a stable address`
          : 'no addresses attached'),
      detailTone: pending.length ? 'warning' : '',
      mono: Boolean(list),
    }));
    if (enable.offered) {
      rows.push(wire(statusRow({tone: 'muted',
        name: egress.enabled ? 'Finish enabling' : 'Enable',
        value: '',
        detail: 'gives every registered node a permanent outbound address for provider allowlists',
        action: {label: 'Enable…', href: '#'}}), enableStableIps));
    } else if (!egress.enabled) {
      rows.push(statusRow({tone: 'muted', name: 'Enable', value: '',
        detail: blockedDetail(enable), detailTone: 'warning'}));
    }
    if (disable.offered) {
      rows.push(wire(statusRow({tone: 'muted', name: 'Disable', value: '',
        detail: 'detaches the addresses; the reservations are kept for re-enable',
        action: {label: 'Disable…', href: '#'}}), disableStableIps));
    }
    return rows;
  }

  function databaseRows() {
    const rows = [];
    for (const row of report?.databases || []) {
      const running = progressRow(row.identifier, row.identifier, row.status || '');
      if (running) { rows.push(running); continue; }
      const add = action(ADD_READER);
      const readers = row.readers || [];
      rows.push(wire(statusRow({
        tone: row.status === 'available' ? 'ok' : 'warn',
        name: row.identifier, value: row.kind === 'aurora' ? 'Aurora' : 'RDS',
        detail: [`${readers.length} reader${readers.length === 1 ? '' : 's'}`,
          add.offered ? '' : blockedDetail(add)].filter(Boolean).join(' · '),
        detailTone: add.offered ? '' : 'warning',
        action: add.offered ? {label: 'Add reader…', href: '#'} : null,
      }), () => addReader(row)));
      for (const reader of readers) {
        const readerRunning = progressRow(reader, reader, 'reader');
        if (readerRunning) { rows.push(readerRunning); continue; }
        const remove = action(REMOVE_READER);
        const member = (row.members || []).find((item) => item.id === reader) || {};
        const available = member.lifecycle_state === 'available';
        rows.push(wire(statusRow({
          tone: available ? 'muted' : 'warn', name: reader,
          value: available ? 'reader' : (member.status || 'unknown'), mono: true,
          detail: remove.offered && member.can_remove
            ? 'standby read capacity; this app reads from the primary'
            : !available ? 'provider transition in progress — inspect and refresh'
              : blockedDetail(remove),
          detailTone: remove.offered && member.can_remove ? '' : 'warning',
          action: remove.offered && member.can_remove
            ? {label: 'Remove…', href: '#'} : null,
        }), () => removeReader(reader)));
      }
    }
    return rows;
  }

  function cacheRows() {
    const rows = [];
    for (const row of report?.caches || []) {
      const running = progressRow(row.identifier, row.identifier, '');
      if (running) { rows.push(running); continue; }
      const change = action(SET_CACHE_REPLICAS);
      const offered = change.offered && !row.blocked_reason;
      const count = Number(row.replica_count || 0);
      rows.push(wire(statusRow({
        tone: row.status === 'available' ? 'ok' : 'warn',
        name: row.identifier,
        value: `${count} replica${count === 1 ? '' : 's'}`,
        detail: offered
          ? [row.automatic_failover_on ? 'automatic failover on' : 'automatic failover off',
            'failover capacity, not read throughput'].join(' · ')
          : blockedDetail(row.blocked_reason
            ? {offered: false, blocked_reason: row.blocked_reason} : change),
        detailTone: offered ? '' : 'warning',
        action: offered ? {label: 'Replicas…', href: '#'} : null,
      }), () => setReplicas(row)));
      for (const member of row.members || []) {
        const available = member.lifecycle_state === 'available';
        rows.push(statusRow({
          tone: available ? 'muted' : 'warn',
          name: member.id || 'cache member',
          value: available ? (member.role || 'member') : (member.status || 'unknown'),
          mono: true,
          detail: available ? 'provider reports available'
            : 'provider transition in progress — inspect and refresh',
          detailTone: available ? '' : 'warning',
        }));
      }
    }
    return rows;
  }

  function warningRows() {
    return (report?.warnings || []).map((warning) => statusRow({
      tone: 'warn', name: warning.iam_action || 'AWS access',
      value: 'Unavailable',
      detail: warning.message || 'this API did not answer',
      detailTone: 'warning'}));
  }

  function headline() {
    const instances = report?.nodes?.instances || [];
    const healthy = instances.filter((row) => row.healthy).length;
    const sub = [
      managed() ? '' : EXTERNAL_SUB,
      report?.node_id_pinned
        ? 'EDGE_NODE_ID is pinned, so nodes cannot be added from here.' : '',
      report?.nodes?.self_check === 'matched' ? ''
        : 'This portal could not identify which instance is serving your request.',
    ].filter(Boolean).join(' ');
    return statusHeadline({
      tone: !instances.length ? 'muted' : healthy === instances.length ? 'ok'
        : healthy ? 'warn' : 'danger',
      message: instances.length
        ? `${healthy} of ${instances.length} node${instances.length === 1 ? '' : 's'} healthy`
        : 'No owned EC2 node was found',
      sub,
      observedAt: report?.generated_at,
      onRefresh: () => load(true)});
  }

  function render() {
    body.replaceChildren(...[
      headline(),
      rowSection('Nodes', [...(report?.nodes?.instances || []).map(nodeRow), addNodeRow()]),
      rowSection('Outbound addresses', egressRows()),
      rowSection('Databases', databaseRows()),
      rowSection('Cache', cacheRows()),
      rowSection('Access', warningRows()),
    ].filter(Boolean));
  }

  async function load(refresh = false) {
    if (!refresh) body.replaceChildren(loadingState('Loading capacity…'));
    try {
      report = await api(`${CAPACITY_PATH}${refresh ? '?refresh=1' : ''}`, {signal});
      render();
    } catch (error) {
      if (error?.name === 'AbortError') return;
      body.replaceChildren(errorState(error, () => load()));
    }
  }

  await load();
  return root;
}

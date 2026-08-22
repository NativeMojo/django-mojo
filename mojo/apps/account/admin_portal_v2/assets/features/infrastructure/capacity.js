// Infrastructure ▸ Capacity — the whole fleet on one page: app nodes, Redis,
// database. Ported from v1's Fleet Scaling page, whose anatomy is this
// portal's north star: a summary strip, one panel per resource with a
// plain-language purpose line, a consequence under every control, a behavior
// callout, and one row per live member.
//
// The interaction model is stage-then-apply: every stepper edits a local
// desired state, the staged steps are sent to POST /capacity/plan, and the
// bottom bar renders the SERVER's plan — its wording, its ordering, its cost
// delta. Nothing here invents a description or a price. Confirming applies
// the plan by its id (POST /capacity/plan/apply); the server then runs the
// steps as one batch, each step re-validated against the live fleet the
// moment it runs, and one watcher polls that batch to a terminal state.
//
// Rules carried over from v1, none of them relaxed:
//   1. Every control the server would refuse is absent or disabled with the
//      reason (report.actions[..].offered / blocked_reason) — nothing here
//      re-derives an offer.
//   2. Progress is polled to a terminal state; "AWS accepted it" is never
//      reported as done.
//   3. A member the provider has not finished with is named by its provider
//      state — never "Healthy" — and carries no action until it settles.
//
// What v2 adds: the batch is watched by a MODULE-level watcher that feeds the
// global operation store, so a fleet change shows in the banner on every page
// and keeps being polled after the operator navigates away from this tab.
import {api, apiOnce, h, icon} from '../../core.js';
import {runAction, toast} from '../../components/actions.js';
import {openModal} from '../../components/overlays.js';
import {errorState, loadingState} from '../../components/views.js';
import {
  remove as removeOperation, upsert as upsertOperation,
} from '../../components/operations.js';
import {egressPanel} from './egress.js';

const CAPACITY_PATH = '/api/aws/capacity';
const STATUS_PATH = '/api/aws/capacity/status';
const PLAN_PATH = '/api/aws/capacity/plan';
const PLAN_APPLY_PATH = '/api/aws/capacity/plan/apply';

// Where the operation banner sends an operator who wants to watch this.
const OPERATION_HREF = '#/infrastructure';

const POLL_INTERVAL = 10000;
// Per STEP, matching v1's 90 minutes per operation — above the server's
// CACHE_RESIZE_TIMEOUT (5400s), so the watcher never abandons a batch the
// server still allows.
const POLL_LIMIT = 540;

const EXTERNAL_SUB = 'Infrastructure mode is external, so this portal shows the fleet but does not change it.';

// Never a dead control: when the server would refuse, the page says which
// thing is in the way instead of offering a control that fails. Same sentences
// as v1 — one blocked_reason reads identically in both portals.
const BLOCKED_COPY = {
  infrastructure_external: 'external infrastructure mode — capacity is applied by your infrastructure team\'s IaC',
  node_id_pinned: 'this fleet pins EDGE_NODE_ID, so a new node could never prove its own identity — remove the pin first',
  no_source_node: 'no healthy, running node is available to clone',
  last_healthy_target: 'this is the last healthy target — removing it would take the fleet out of service',
  no_database: 'no RDS database was found in this region',
  no_reader: 'this database has no reader to remove',
  no_cache_group: 'no ElastiCache replication group was found in this region',
  cluster_mode_unsupported: 'cluster-mode enabled — its replica count is a resharding decision, not a capacity change',
};

// The provider's phase, in words. Same map as v1.
const PHASE_COPY = {
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
  verifying: 're-reading AWS to prove the result',
  complete: 'done',
};

// The provider's own words for "not finished with this yet". A member in one
// of these states is neither healthy nor broken — it is mid-change, so the row
// names the state and withholds every action until the provider settles it.
//
// Nodes carry the load balancer's target-health state, where `initial` means
// registered but not health-checked yet. Databases and caches carry the AWS
// lifecycle status of the cluster or group.
const SETTLING_NODE = new Set(['initial']);
const SETTLING_RESOURCE = new Set([
  'creating', 'modifying', 'deleting', 'starting', 'stopping', 'rebooting',
  'renaming', 'upgrading', 'failing-over', 'snapshotting',
]);

const WAITING_NOTE = 'no actions until the provider confirms it';

function deltaText(value) {
  if (value === null || value === undefined) return '';
  const rounded = Math.round(Math.abs(value) * 100) / 100;
  return `${value < 0 ? '−' : '+'}$${rounded}/mo`;
}

function blockedText(action) {
  if (!action || action.offered) return '';
  return BLOCKED_COPY[action.blocked_reason]
    || String(action.blocked_reason || 'this action is not available').replaceAll('_', ' ');
}

function phaseLine(record) {
  if (!record) return 'working';
  if (record.state === 'failed') return record.message || 'failed';
  if (record.state === 'done') return record.message || 'done';
  const phases = record.phases || [];
  const index = phases.indexOf(record.phase);
  const step = index >= 0 ? `step ${index + 1} of ${phases.length} · ` : '';
  return `${step}${PHASE_COPY[record.phase] || record.phase || 'working'}`;
}

function wait(milliseconds) {
  return new Promise((resolve) => { setTimeout(resolve, milliseconds); });
}

// ── the batch watcher ───────────────────────────────────────────────────────
//
// Module-level on purpose. A fleet change outlives the page that started it:
// the operator applies it, then goes to look at something else. The watcher
// keeps polling, keeps the global operation store in step, and hands the live
// record to whichever Capacity page is mounted — or to none at all.

let watcher = null;
const listeners = new Set();

function announce(state) {
  for (const listener of [...listeners]) {
    try {
      listener(state);
    } catch (_error) {
      // One broken subscriber must not stop the others from being told.
    }
  }
}

/** The batch record currently being watched, or null. */
export function activeBatch() {
  return watcher ? watcher.record : null;
}

/**
 * Watch the mounted page's copy of the running batch. The listener is called
 * with {record, terminal, ok} on every poll. Returns an unsubscribe function.
 */
export function subscribeBatch(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function batchPhase(record) {
  const steps = record?.steps || [];
  const active = steps.find((step) => step.state === 'running');
  if (!active) return record?.message || 'working';
  const position = steps.length > 1
    ? `step ${(active.index ?? 0) + 1} of ${steps.length} · ` : '';
  return `${position}${phaseLine(active)}`;
}

// One line naming what is being changed — the SERVER's step descriptions, not
// a sentence assembled here.
function batchTitle(plan) {
  const steps = plan?.steps || [];
  if (steps.length === 1) return steps[0].description || 'Changing the fleet';
  return `${steps.length} fleet changes`;
}

function publish(record) {
  upsertOperation({
    id: watcher.id, title: watcher.title, phase: batchPhase(record),
    startedAt: watcher.startedAt, href: OPERATION_HREF,
  });
}

function finishWatch(current, ok) {
  if (watcher !== current) return;
  removeOperation(current.id);
  watcher = null;
  announce({record: current.record, terminal: true, ok});
}

async function pump(current) {
  const limit = POLL_LIMIT * Math.max(1, (current.record.steps || []).length);
  for (let count = 0; count < limit; count += 1) {
    await wait(POLL_INTERVAL);
    if (watcher !== current) return;
    let live;
    try {
      // No abort signal: this poll belongs to the operation, not to whichever
      // page happens to be on screen.
      live = await api(`${STATUS_PATH}?batch=${encodeURIComponent(current.id)}`);
    } catch (error) {
      if (watcher !== current) return;
      current.record = {...current.record,
        pollNote: `progress unavailable: ${error.message}`};
      announce({record: current.record, terminal: false, ok: false});
      continue;
    }
    if (watcher !== current) return;
    current.record = live;
    publish(live);
    if (live.state !== 'running') { finishWatch(current, live.state === 'done'); return; }
    announce({record: live, terminal: false, ok: false});
  }
  finishWatch(current, false);
}

function watchBatch(batch, title) {
  watcher = {id: String(batch.id), title, startedAt: Date.now(), record: batch};
  publish(batch);
  pump(watcher);
  return watcher.record;
}

// ---------------------------------------------------------------------------

export function capacityTab(ctx, signal = null, actions = null) {
  const root = h('div', {class: 'fleet-page'}, loadingState('Loading the fleet…'));

  let report = null;
  // Desired state, staged locally until Apply.
  let want = null;
  // While a batch is running: the server's batch record — replaces the bar body.
  let running = activeBatch();
  // The server-written plan for the current staged steps, and its request
  // lifecycle. planKey is the JSON of the staged steps the plan answers for —
  // a cheap way to re-request only when the staging actually changed.
  let serverPlan = null;
  let planPending = false;
  let planError = null;
  let planTimer = null;
  let planKey = '';

  const managed = () => report?.mode !== 'external';
  const offer = (name) => report?.actions?.[name] || {offered: false, blocked_reason: null};
  const instances = () => report?.nodes?.instances || [];
  const routing = () => report?.reader_routing || {};

  function stopped() {
    return signal?.aborted || !root.isConnected;
  }

  // ── members the provider has not finished with ──────────────────────────

  // The step of the running batch that names this resource, if any. It is the
  // provider's own report of what is happening to that member right now.
  function batchStep(identifier) {
    if (!running || running.state !== 'running') return null;
    return (running.steps || []).find((step) => step.resource === identifier
      && ['running', 'pending'].includes(step.state)) || null;
  }

  function settlingNode(row) {
    return SETTLING_NODE.has(String(row.state || '')) || Boolean(batchStep(row.id));
  }

  function settlingResource(row) {
    return SETTLING_RESOURCE.has(String(row.status || ''))
      || Boolean(batchStep(row.identifier));
  }

  // ── vocabulary ─────────────────────────────────────────────────────────

  function stateBadge({tone, label, spinner = false}) {
    const modifier = {ok: 'success', warn: 'warning', pend: 'accent', danger: 'danger'}[tone] || '';
    return h('span', {class: `badge ${modifier}`.trim()},
      spinner ? h('span', {class: 'spin', 'aria-hidden': 'true'}) : null,
      h('span', {text: label}));
  }

  function memberRow({state, name, chips = [], note = '', side = null, modifier = ''}) {
    return h('div', {class: `member ${modifier}`.trim()},
      stateBadge(state),
      h('span', {class: 'name', text: name}),
      ...chips.filter(Boolean).map((text) => h('span', {class: 'chip', text})),
      note ? h('span', {class: 'member-note', text: note}) : null,
      side ? h('span', {class: 'member-action'}, side) : null);
  }

  // The self-reported reader-routing state, as a chip. Per-node on purpose:
  // the report answers from the process that served it, and a node that has
  // not restarted since the config line was added still runs without routing.
  function routingChip(kind) {
    const state = routing()[kind] || {};
    if (kind === 'database') {
      if (state.skip_reason) {
        return h('span', {class: 'badge warning', title: state.skip_reason},
          'Reader config skipped');
      }
      if (state.active && state.matches_reader_endpoint === false) {
        return h('span', {class: 'badge warning',
          title: `${state.host} is not this cluster's reader endpoint`},
        'Reader host mismatch');
      }
      return h('span', {class: `badge ${state.active ? 'accent' : ''}`.trim(),
        title: state.active ? `reads route to ${state.host} (this node)` : ''},
      state.active ? 'Reader routing: on' : 'Reader routing: off');
    }
    return h('span', {class: `badge ${state.active ? 'accent' : ''}`.trim()},
      state.active ? 'Reader reads: on' : 'Reader reads: off');
  }

  function resetWant() {
    want = {
      addNodes: 0,
      removeNodes: new Set(),
      caches: new Map((report?.caches || []).map(
        (row) => [row.identifier, Number(row.replica_count || 0)])),
      dbAdd: new Map((report?.databases || []).map((row) => [row.identifier, 0])),
      dbRemove: new Set(),
      // Staged sizes, keyed by row identifier -> curated size key. Absent
      // means unchanged; the reader map stages ONE size the plan fans out
      // into one resize step per differing reader.
      cacheSizes: new Map(),
      dbWriterSizes: new Map(),
      dbReaderSizes: new Map(),
    };
  }

  // ── the staged steps ────────────────────────────────────────────────────

  // Raw submissions only — no wording, no ordering, no prices: those are the
  // server's. A staged node removal is TWO steps (drain, then terminate); one
  // staged reader size fans out into one resize per differing reader, skipping
  // any reader this same batch removes.
  function stagedSteps() {
    const steps = [];
    for (let index = 0; index < want.addNodes; index += 1) {
      steps.push({action: 'add_node'});
    }
    for (const [identifier, addCount] of want.dbAdd) {
      for (let index = 0; index < addCount; index += 1) {
        steps.push({action: 'add_reader', resource: identifier});
      }
    }
    for (const row of report?.caches || []) {
      const current = Number(row.replica_count || 0);
      const wanted = want.caches.get(row.identifier);
      if (wanted !== undefined && wanted !== current) {
        steps.push({action: 'set_cache_replicas', resource: row.identifier,
          count: wanted, apply_immediately: true});
      }
      const size = want.cacheSizes.get(row.identifier);
      const rung = size
        && (report?.sizes?.cache || []).find((entry) => entry.size === size);
      if (rung && rung.type !== row.node_type) {
        steps.push({action: 'resize_cache', resource: row.identifier,
          size, apply_immediately: true});
      }
    }
    for (const row of report?.databases || []) {
      const writerSize = want.dbWriterSizes.get(row.identifier);
      const writerTarget = row.kind === 'aurora' ? row.writer : row.identifier;
      if (writerSize && writerTarget) {
        const rung = (report?.sizes?.database || []).find(
          (entry) => entry.size === writerSize);
        const current = row.writer_instance_class || row.instance_class;
        if (rung && rung.type !== current) {
          steps.push({action: 'resize_database', resource: writerTarget,
            size: writerSize, apply_immediately: true});
        }
      }
      const readerSize = want.dbReaderSizes.get(row.identifier);
      const readerRung = readerSize
        && (report?.sizes?.database || []).find((entry) => entry.size === readerSize);
      if (readerRung) {
        for (const reader of row.readers || []) {
          if (want.dbRemove.has(reader)) continue;
          const current = (row.reader_instance_classes || {})[reader];
          if (current === readerRung.type) continue;
          steps.push({action: 'resize_database', resource: reader,
            size: readerSize, apply_immediately: true});
        }
      }
    }
    for (const reader of want.dbRemove) {
      steps.push({action: 'remove_reader', resource: reader});
    }
    for (const id of want.removeNodes) {
      steps.push({action: 'drain_node', resource: id});
      steps.push({action: 'terminate_node', resource: id});
    }
    return steps;
  }

  // ── the server plan ─────────────────────────────────────────────────────

  // Debounced: called from render(), it re-requests only when the staged
  // steps actually changed, ~400ms after the last tweak.
  function syncPlan() {
    if (running) return;
    const steps = stagedSteps();
    const key = JSON.stringify(steps);
    if (key === planKey) return;
    planKey = key;
    clearTimeout(planTimer);
    serverPlan = null;
    planError = null;
    if (!steps.length) { planPending = false; return; }
    planPending = true;
    planTimer = setTimeout(() => requestPlan(steps, key), 400);
  }

  async function requestPlan(steps, key) {
    let plan;
    try {
      plan = await apiOnce(PLAN_PATH, {method: 'POST', signal,
        body: JSON.stringify({steps})});
    } catch (error) {
      if (error?.name === 'AbortError' || key !== planKey || stopped()) return;
      planPending = false;
      planError = error.message || 'the server refused this plan';
      render();
      return;
    }
    if (key !== planKey || stopped()) return;
    serverPlan = plan;
    planPending = false;
    render();
  }

  // ── the batch ───────────────────────────────────────────────────────────

  async function finishPlan(ok) {
    if (ok) toast('Fleet updated');
    running = null;
    if (stopped()) return;
    await load(true);
    resetWant();
    render();
  }

  function totalLine(plan) {
    const total = plan.total_monthly_delta_usd;
    let text = total ? `Monthly cost change: ≈ ${deltaText(total)}`
      : 'No monthly cost change';
    if (plan.estimate_complete === false) {
      text += ' (some steps have no listed price)';
    }
    return text;
  }

  function confirmApply(plan) {
    return new Promise((resolve) => {
      let settled = false;
      const settle = (value) => { if (settled) return; settled = true; close(); resolve(value); };
      const steps = plan.steps || [];
      const content = h('div', {},
        h('p', {class: 'modal-copy',
          text: 'These run one at a time, in this order. Each step is re-checked '
            + 'against the live fleet the moment it runs; if one fails, the steps '
            + 'after it are not attempted.'}),
        h('ul', {class: 'fleet-confirm-list'},
          ...steps.map((step) => h('li', {},
            h('span', {class: `fleet-dot ${step.kind}`}),
            h('span', {text: step.description})))),
        h('p', {class: 'modal-copy', text: totalLine(plan)}),
        h('div', {class: 'form-actions'},
          h('button', {class: 'button ghost', type: 'button',
            onclick: () => settle(false)}, 'Cancel'),
          h('button', {class: 'button danger', type: 'button',
            onclick: () => settle(true)},
          `Apply ${steps.length} change${steps.length === 1 ? '' : 's'}`)));
      const close = openModal({
        title: 'Apply these changes?', content, danger: true,
        onClose: () => { if (!settled) { settled = true; resolve(false); } },
      });
    });
  }

  async function applyPlan(plan) {
    if (!(await confirmApply(plan))) return;
    let batch;
    try {
      batch = await apiOnce(PLAN_APPLY_PATH, {method: 'POST', signal,
        body: JSON.stringify({plan_id: plan.id})});
    } catch (error) {
      if (error?.name === 'AbortError' || error?.code === 'fresh_auth_required') return;
      // plan_not_found / plan_stale and friends: show the server's sentence,
      // re-request a fresh plan, re-render — NEVER silently re-plan-and-apply.
      toast(error.message || 'The plan could not be applied.');
      planKey = '';
      render();
      return;
    }
    // From here the operation belongs to the store, not to this page: the
    // banner reports it everywhere, and the watcher outlives this render.
    running = watchBatch(batch, batchTitle(plan));
    render();
  }

  // ── controls ────────────────────────────────────────────────────────────

  function stepper({value, canDown, canUp, downTitle, upTitle, onDown, onUp, tone}) {
    return h('div', {class: 'fleet-stepper'},
      h('button', {type: 'button', 'aria-label': downTitle || 'fewer',
        title: downTitle || '', disabled: canDown ? null : true,
        onclick: onDown}, '−'),
      h('span', {class: `fleet-stepper-num ${tone || ''}`, text: String(value)}),
      h('button', {type: 'button', 'aria-label': upTitle || 'more',
        title: upTitle || '', disabled: canUp ? null : true,
        onclick: onUp}, '+'));
  }

  // A disabled size dropdown: honest about what runs today and what is
  // coming. One option — the current size — and a note. Still used by the
  // node panel (app-node resize is a rolling replace, its own future item)
  // and as the blocked shape of the live selects below.
  function sizePlaceholder(label, current, note) {
    return h('div', {class: 'fleet-control'},
      h('span', {class: 'fleet-label', text: label}),
      h('select', {disabled: true, title: note},
        h('option', {text: current || 'current size'})),
      h('span', {class: 'fleet-floor', text: note}));
  }

  // A live size select, fed entirely by report.sizes — never hardcoded. The
  // first option is the current type (selected = no change; picking it back
  // unstages), then every other curated rung with its price.
  function sizeSelect({label, ladder, currentType, currentText, staged, note, onChange}) {
    const rungs = ladder || [];
    const currentRung = rungs.find((rung) => rung.type === currentType);
    const currentLabel = currentText
      || (currentRung
        ? `${currentRung.label} (current) — ${currentType}`
        : `Current — ${currentType || 'unknown'}`);
    return h('div', {class: 'fleet-control'},
      h('span', {class: 'fleet-label', text: label}),
      h('select', {onchange: (event) => onChange(event.target.value || null)},
        h('option', {value: '', text: currentLabel,
          selected: staged ? null : true}),
        ...rungs.filter((rung) => rung.type !== currentType).map((rung) =>
          h('option', {value: rung.size,
            text: `${rung.label} — ${rung.type}`
              + (rung.monthly_usd ? ` · ≈$${rung.monthly_usd}/mo per node` : ''),
            selected: staged === rung.size ? true : null}))),
      note ? h('span', {class: 'fleet-floor', text: note}) : null);
  }

  // ── panels ──────────────────────────────────────────────────────────────

  function summaryStrip() {
    const nodes = instances();
    const healthy = nodes.filter((row) => row.healthy).length;
    const settling = nodes.filter(settlingNode).length;
    const cache = (report?.caches || [])[0];
    const database = (report?.databases || [])[0];
    const readers = (database?.readers || []).length;
    const tile = (key, value, qualifier, meta) => h('div', {},
      h('span', {class: 'stat-key', text: key}),
      h('strong', {}, value,
        qualifier ? h('span', {class: 'stat-qualifier', text: ` ${qualifier}`}) : null),
      h('small', {text: meta}));
    return h('div', {class: 'stat-strip'},
      tile('App nodes', String(nodes.length),
        nodes[0]?.instance_type ? `× ${nodes[0].instance_type}` : '',
        nodes.length
          ? `${healthy} of ${nodes.length} healthy${settling ? ` · ${settling} still joining` : ''}`
          : 'none registered'),
      tile('Redis', cache ? `1+${cache.replica_count || 0}` : '—', '',
        cache
          ? `primary + ${cache.replica_count || 0} replica${cache.replica_count === 1 ? '' : 's'} · ${cache.status}`
          : 'no replication group found'),
      tile('Database', database ? `1+${readers}` : '—', '',
        database
          ? `writer + ${readers} reader${readers === 1 ? '' : 's'} · ${database.status}`
          : 'no database found'));
  }

  function resourcePanel({title, purpose, chip, controls, callout, members, changed}) {
    return h('section', {class: `panel fleet-panel ${changed ? 'changed' : ''}`.trim()},
      h('div', {class: 'panel-head'},
        h('div', {}, h('h2', {text: title}), h('p', {text: purpose})),
        chip || null),
      controls || null,
      callout ? h('div', {class: 'fleet-callout'}, callout) : null,
      members && members.length ? h('div', {class: 'fleet-members'}, ...members) : null);
  }

  function nodePanel() {
    const rows = instances();
    const add = offer('add_node');
    const drain = offer('drain_node');
    const healthyLeft = rows.filter(
      (row) => row.healthy && !want.removeNodes.has(row.id)).length;
    const total = rows.length - want.removeNodes.size + want.addNodes;
    const type = rows[0]?.instance_type || '';

    // A node the balancer has not finished checking, or one this batch is
    // already changing, carries no control: acting on it would be acting on a
    // state nobody has confirmed yet.
    const removable = (row) => managed() && drain.offered && !row.self
      && !want.removeNodes.has(row.id) && !settlingNode(row)
      && (!row.healthy || healthyLeft > 1);

    const bumpDown = () => {
      if (want.addNodes > 0) { want.addNodes -= 1; render(); return; }
      const candidates = rows.filter(removable);
      const pick = candidates[candidates.length - 1];
      if (pick) { want.removeNodes.add(pick.id); render(); }
    };
    const bumpUp = () => {
      if (want.removeNodes.size) {
        const last = [...want.removeNodes].pop();
        want.removeNodes.delete(last);
      } else if (add.offered) {
        want.addNodes += 1;
      }
      render();
    };

    const nodeRow = (row) => {
      const leaving = want.removeNodes.has(row.id);
      const step = batchStep(row.id);
      const settling = settlingNode(row);
      const labels = [
        row.self ? 'this node' : '', row.primary ? 'certbot primary' : '',
      ].filter(Boolean).join(' · ');
      const state = leaving ? {tone: 'warn', label: 'Will drain'}
        : step ? {tone: 'pend', label: step.kind === 'remove' ? 'Removing' : 'Changing', spinner: true}
          : row.healthy ? {tone: 'ok', label: 'Healthy'}
            : settling ? {tone: 'pend', label: row.state || 'joining', spinner: true}
              : {tone: 'warn', label: row.state || 'unknown'};
      const note = step ? phaseLine(step)
        : labels || (settling ? 'registered, waiting on the balancer\'s first health check' : '');
      const side = leaving
        ? h('button', {class: 'fleet-link undo', type: 'button',
          onclick: () => { want.removeNodes.delete(row.id); render(); }}, 'Keep it')
        : removable(row)
          ? h('button', {class: 'fleet-link', type: 'button',
            onclick: () => { want.removeNodes.add(row.id); render(); }}, 'Remove')
          : h('span', {class: 'fleet-floor',
            text: row.self ? 'answering this request'
              : settling || step ? WAITING_NOTE : ''});
      return memberRow({
        state, name: row.name || row.id, chips: [row.instance_type],
        note, side, modifier: leaving ? 'leaving' : settling || step ? 'pending' : '',
      });
    };

    const ghostRow = () => memberRow({
      state: {tone: 'pend', label: 'Planned'}, name: 'new node',
      chips: [type], modifier: 'ghost',
      side: h('button', {class: 'fleet-link undo', type: 'button',
        onclick: () => { want.addNodes -= 1; render(); }}, 'Cancel'),
    });

    return resourcePanel({
      title: 'App nodes',
      purpose: 'The machines that serve web requests and run background jobs.',
      changed: want.addNodes || want.removeNodes.size,
      controls: managed() ? h('div', {class: 'fleet-controls'},
        h('div', {class: 'fleet-control'},
          h('span', {class: 'fleet-label', text: 'How many'}),
          stepper({
            value: total,
            canDown: want.addNodes > 0 || rows.some(removable),
            canUp: add.offered || want.removeNodes.size > 0,
            downTitle: 'one fewer node', upTitle: 'one more node',
            onDown: bumpDown, onUp: bumpUp,
            tone: total > rows.length ? 'up' : total < rows.length ? 'down' : '',
          }),
          h('span', {class: 'fleet-floor',
            text: add.offered
              ? 'A new node proves it runs the fleet\'s code before it serves. 20–40 min.'
              : blockedText(add)})),
        sizePlaceholder('Size of each', type,
          'Changing size is a rolling replace — not offered here yet.')) : null,
      members: [
        ...rows.map(nodeRow),
        ...Array.from({length: want.addNodes}, ghostRow),
      ],
    });
  }

  function cachePanel() {
    const rows = report?.caches || [];
    const change = offer('set_cache_replicas');
    if (!rows.length) {
      return resourcePanel({
        title: 'Redis',
        purpose: blockedText(change) || 'No ElastiCache replication group was found.',
      });
    }
    const changed = rows.some((row) =>
      want.caches.get(row.identifier) !== Number(row.replica_count || 0)
      || want.cacheSizes.has(row.identifier));
    const groups = rows.map((row) => {
      const current = Number(row.replica_count || 0);
      const min = Number(row.min_replicas || 0);
      const wanted = want.caches.get(row.identifier) ?? current;
      const settling = settlingResource(row);
      const offered = managed() && change.offered && !row.blocked_reason && !settling;
      const blocked = row.blocked_reason
        ? blockedText({offered: false, blocked_reason: row.blocked_reason})
        : settling ? `${row.status} — ${WAITING_NOTE}` : blockedText(change);
      const resize = offer('resize_cache');
      const resizable = managed() && resize.offered && !row.blocked_reason && !settling;
      // The interruption case is the SERVER's statement (resize_impact),
      // surfaced before apply — the page never re-derives the policy.
      const impactNote = row.resize_impact === 'rolling'
        ? 'Resize rolls replicas first, then a brief failover — one short interruption.'
        : 'No replica: the cache is down while its node is replaced.';
      const memberState = settling
        ? {tone: 'pend', label: row.status || 'changing', spinner: true}
        : row.status === 'available'
          ? {tone: 'ok', label: 'Healthy'} : {tone: 'warn', label: row.status || 'unknown'};
      return {
        controls: offered ? h('div', {class: 'fleet-controls'},
          h('div', {class: 'fleet-control'},
            h('span', {class: 'fleet-label', text: `Replicas · ${row.identifier}`}),
            stepper({
              value: wanted, canDown: wanted > min, canUp: wanted < current + 5,
              downTitle: 'one fewer replica', upTitle: 'one more replica',
              onDown: () => { want.caches.set(row.identifier, wanted - 1); render(); },
              onUp: () => { want.caches.set(row.identifier, wanted + 1); render(); },
              tone: wanted > current ? 'up' : wanted < current ? 'down' : '',
            }),
            h('span', {class: 'fleet-floor',
              text: row.automatic_failover_on
                ? `Failover is on, so at least ${Math.max(min, 1)} replica must remain.`
                : 'Failover is off — zero replicas leaves nothing to fail over to.'})),
          resizable
            ? sizeSelect({
              label: 'Size of each', ladder: report?.sizes?.cache,
              currentType: row.node_type,
              staged: want.cacheSizes.get(row.identifier) || null,
              note: impactNote,
              onChange: (size) => {
                if (size) want.cacheSizes.set(row.identifier, size);
                else want.cacheSizes.delete(row.identifier);
                render();
              }})
            : sizePlaceholder('Size of each', row.node_type || '',
              blockedText(resize) || 'Resizing is not available.'))
          : h('p', {class: 'fleet-blocked', text: blocked}),
        members: [
          ...(row.members || []).map((member) => memberRow({
            state: memberState, name: member.id || member,
            chips: [member.node_type || row.node_type, member.role || ''],
          })),
          ...Array.from({length: Math.max(0, wanted - current)}, () => memberRow({
            state: {tone: 'pend', label: 'Planned'}, name: 'new replica',
            modifier: 'ghost',
            side: h('button', {class: 'fleet-link undo', type: 'button',
              onclick: () => { want.caches.set(row.identifier, wanted - 1); render(); }},
            'Cancel'),
          })),
        ],
      };
    });
    return resourcePanel({
      title: 'Redis',
      purpose: 'Caching, sessions, live updates and the job queue.',
      chip: routingChip('redis'),
      changed,
      controls: h('div', {}, ...groups.map((group) => group.controls)),
      callout: h('div', {class: 'callout'}, icon('activity'),
        h('p', {text: routing().redis?.active
          ? 'Replicas are failover cover first: if the primary dies, one takes '
            + 'over in about a minute. Dashboards and metrics also read from '
            + 'them — but they never make writes faster.'
          : 'Replicas are failover cover: if the primary dies, one takes over '
            + 'in about a minute. Reader reads are off on this node, so they '
            + 'do not serve any traffic until then.'})),
      members: groups.flatMap((group) => group.members),
    });
  }

  function databasePanel() {
    const rows = report?.databases || [];
    const add = offer('add_reader');
    const remove = offer('remove_reader');
    if (!rows.length) {
      return resourcePanel({
        title: 'Database',
        purpose: blockedText(add) || 'No database was found.',
      });
    }
    const resize = offer('resize_database');

    // The writer and the readers carry independent sizes — that asymmetry
    // (big writer, smaller readers) is where the money is. One reader size
    // is staged per row; the plan fans it out into one resize per reader.
    function writerSizeControl(row) {
      const currentType = row.writer_instance_class || row.instance_class;
      const target = row.kind === 'aurora' ? row.writer : row.identifier;
      if (!resize.offered) {
        return sizePlaceholder('Writer size',
          currentType || (row.kind === 'aurora' ? 'Aurora' : ''),
          blockedText(resize) || 'Resizing is not available.');
      }
      if (!target) {
        return sizePlaceholder('Writer size', currentType || 'Aurora',
          'This cluster reports no writer instance to resize.');
      }
      return sizeSelect({
        label: 'Writer size', ladder: report?.sizes?.database,
        currentType,
        staged: want.dbWriterSizes.get(row.identifier) || null,
        note: '~minutes offline while the writer changes class.',
        onChange: (size) => {
          if (size) want.dbWriterSizes.set(row.identifier, size);
          else want.dbWriterSizes.delete(row.identifier);
          render();
        }});
    }

    function readerSizeControl(row) {
      const readers = row.readers || [];
      const classes = readers.map(
        (reader) => (row.reader_instance_classes || {})[reader]).filter(Boolean);
      const shared = classes.length && classes.every((cls) => cls === classes[0])
        ? classes[0] : null;
      if (!resize.offered) {
        return sizePlaceholder('Reader size', shared || '',
          blockedText(resize) || 'Resizing is not available.');
      }
      if (!readers.length) {
        return sizePlaceholder('Reader size', '', 'No readers to size.');
      }
      return sizeSelect({
        label: 'Reader size', ladder: report?.sizes?.database,
        currentType: shared,
        currentText: classes.length && !shared ? 'Mixed sizes' : null,
        staged: want.dbReaderSizes.get(row.identifier) || null,
        note: 'Applies to every reader; reads keep flowing on the others.',
        onChange: (size) => {
          if (size) want.dbReaderSizes.set(row.identifier, size);
          else want.dbReaderSizes.delete(row.identifier);
          render();
        }});
    }

    const changed = rows.some((row) => (want.dbAdd.get(row.identifier) || 0) > 0
      || want.dbWriterSizes.has(row.identifier)
      || want.dbReaderSizes.has(row.identifier))
      || want.dbRemove.size > 0;

    const groups = rows.map((row) => {
      const readers = row.readers || [];
      const adding = want.dbAdd.get(row.identifier) || 0;
      const removingHere = readers.filter((reader) => want.dbRemove.has(reader));
      const count = readers.length - removingHere.length + adding;
      // The cluster's own status is the only lifecycle state the report
      // carries about its members. While RDS says it is mid-change, no member
      // of it is called Healthy and none of them offers an action.
      const settling = settlingResource(row);
      const memberState = settling
        ? {tone: 'pend', label: row.status || 'changing', spinner: true}
        : row.status === 'available'
          ? {tone: 'ok', label: 'Healthy'} : {tone: 'warn', label: row.status || 'unknown'};

      const readerRow = (reader) => {
        const leaving = want.dbRemove.has(reader);
        const step = batchStep(reader);
        const state = leaving ? {tone: 'warn', label: 'Will remove'}
          : step ? {tone: 'pend', label: step.kind === 'remove' ? 'Removing' : 'Changing', spinner: true}
            : memberState;
        const side = leaving
          ? h('button', {class: 'fleet-link undo', type: 'button',
            onclick: () => { want.dbRemove.delete(reader); render(); }}, 'Keep it')
          : remove.offered && !settling && !step
            ? h('button', {class: 'fleet-link', type: 'button',
              onclick: () => { want.dbRemove.add(reader); render(); }}, 'Remove')
            : settling || step
              ? h('span', {class: 'fleet-floor', text: WAITING_NOTE}) : null;
        return memberRow({
          state, name: reader,
          chips: [(row.reader_instance_classes || {})[reader], 'reader'],
          note: step ? phaseLine(step) : '',
          side,
          modifier: leaving ? 'leaving' : settling || step ? 'pending' : '',
        });
      };

      return {
        controls: managed() ? h('div', {class: 'fleet-controls'},
          h('div', {class: 'fleet-control'},
            h('span', {class: 'fleet-label', text: `Read replicas · ${row.identifier}`}),
            stepper({
              value: count,
              canDown: adding > 0 || (remove.offered && !settling && readers.some(
                (reader) => !want.dbRemove.has(reader))),
              canUp: add.offered || removingHere.length > 0,
              downTitle: 'one fewer reader', upTitle: 'one more reader',
              onDown: () => {
                if (adding > 0) { want.dbAdd.set(row.identifier, adding - 1); }
                else {
                  const pick = [...readers].reverse().find(
                    (reader) => !want.dbRemove.has(reader));
                  if (pick) want.dbRemove.add(pick);
                }
                render();
              },
              onUp: () => {
                if (removingHere.length) want.dbRemove.delete(removingHere[removingHere.length - 1]);
                else want.dbAdd.set(row.identifier, adding + 1);
                render();
              },
              tone: count > readers.length ? 'up' : count < readers.length ? 'down' : '',
            }),
            h('span', {class: 'fleet-floor',
              text: add.offered
                ? (count === 0 ? 'With none, every read stays on the writer.' : '')
                : blockedText(add)})),
          writerSizeControl(row),
          readerSizeControl(row)) : null,
        members: [
          memberRow({
            state: memberState, name: row.writer || row.identifier,
            chips: [row.writer_instance_class || row.instance_class, 'writer'],
            modifier: settling ? 'pending' : '',
          }),
          ...readers.map(readerRow),
          ...Array.from({length: adding}, () => memberRow({
            state: {tone: 'pend', label: 'Planned'}, name: 'new reader',
            modifier: 'ghost',
            side: h('button', {class: 'fleet-link undo', type: 'button',
              onclick: () => { want.dbAdd.set(row.identifier, adding - 1); render(); }},
            'Cancel'),
          })),
        ],
      };
    });

    return resourcePanel({
      title: 'Database',
      purpose: 'Postgres. Every account, message and record lives here.',
      chip: routingChip('database'),
      changed,
      controls: h('div', {}, ...groups.map((group) => group.controls)),
      callout: h('div', {class: 'callout'}, icon('activity'),
        h('p', {text: routing().database?.active
          ? 'Reader routing is on: safe reads spread across the replicas, and '
            + 'anything just written is read back from the writer. If saving is '
            + 'slow, the answer is a bigger writer, not more readers.'
          : 'Reader routing is off on this node: every query goes to the '
            + 'writer, so a reader is standby capacity until it is configured. '
            + 'If saving is slow, the answer is a bigger writer, not more readers.'})),
      members: groups.flatMap((group) => group.members),
    });
  }

  function warningsPanel() {
    const warnings = report?.warnings || [];
    if (!warnings.length) return null;
    return resourcePanel({
      title: 'Access',
      purpose: 'Parts of AWS this page could not read — unknown, not empty.',
      members: warnings.map((warning) => memberRow({
        state: {tone: 'warn', label: 'Unavailable'},
        name: warning.iam_action || 'AWS',
        note: warning.message || '',
      })),
    });
  }

  function techDetails() {
    return h('details', {class: 'disclosure fleet-tech'},
      h('summary', {text: 'What this page is actually doing'}),
      h('div', {class: 'fleet-tech-body'},
        h('p', {text: 'Every control maps to one server-side capacity action; nothing here '
          + 'talks to AWS directly. The staged changes are sent to the server, which '
          + 'writes the plan you see — its wording, its order (adds before removes, a '
          + 'terminate right behind its drain), and its cost delta. Confirming applies '
          + 'that exact plan by id; the server runs the steps as one batch, each '
          + 're-checked against the live fleet the moment it runs, and a failed step '
          + 'stops everything after it. A control the server would refuse is disabled '
          + 'with the reason.'}),
        h('p', {text: 'A running batch is polled to a terminal state whether or not this '
          + 'page is open: it appears in the operation banner on every screen, and '
          + 'coming back here picks up the same progress.'}),
        h('p', {text: 'Coming, and deliberately absent until its server side ships: '
          + 'automatic overnight failback of the writer after a database failover.'})));
  }

  // ── the apply bar ───────────────────────────────────────────────────────

  function runningBar() {
    const steps = running.steps || [];
    return h('div', {class: 'fleet-bar show'},
      h('div', {class: 'fleet-progress'},
        ...steps.map((step) => {
          const state = step.state === 'done' ? 'done'
            : step.state === 'failed' ? 'failed'
              : step.state === 'running' ? 'active' : 'pending';
          return h('div', {class: `fleet-pstep ${state}`},
            state === 'active' ? h('span', {class: 'pending-spinner'})
              : state === 'done' ? h('span', {class: 'fleet-tick', text: '✓'})
                : state === 'failed' ? h('span', {class: 'fleet-tick', text: '✕'})
                  : h('span', {class: 'fleet-tick', text: ''}),
            h('span', {text: step.description || step.action}),
            state === 'active'
              ? h('span', {class: 'fleet-phase',
                text: running.pollNote || phaseLine(step)}) : null,
            state === 'failed' ? h('span', {class: 'fleet-phase danger-text',
              text: step.message || 'failed'}) : null,
            step.state === 'not_attempted'
              ? h('span', {class: 'fleet-phase',
                text: 'not attempted — an earlier step failed'}) : null);
        }),
        running.stalled ? h('div', {class: 'fleet-phase danger-text',
          text: 'no progress reported — check the jobs runner'}) : null,
        running.state === 'failed' ? h('div', {class: 'form-actions'},
          h('button', {class: 'button', type: 'button',
            onclick: () => finishPlan(false)}, 'Close')) : null));
  }

  function applyBar() {
    if (running) return runningBar();
    const staged = stagedSteps();
    if (!staged.length) return h('div', {class: 'fleet-bar'});
    const plan = serverPlan;
    const body = plan
      ? h('ul', {},
        ...(plan.steps || []).map((step) => h('li', {},
          h('span', {class: `fleet-dot ${step.kind}`}),
          h('span', {}, step.description,
            step.monthly_delta_usd !== null && step.monthly_delta_usd !== undefined
              ? h('span', {class: 'fleet-bar-note',
                text: ` · ${deltaText(step.monthly_delta_usd)}`}) : null,
            ...(step.warnings || []).map((warning) =>
              h('span', {class: 'fleet-bar-note', text: ` — ${warning}`}))))))
      : h('p', {class: 'fleet-bar-note',
        text: planError ? `The server refused this plan: ${planError}` : 'pricing…'});
    return h('div', {class: 'fleet-bar show'},
      h('div', {class: 'fleet-bar-in'},
        h('div', {class: 'fleet-bar-list'},
          h('h4', {text: 'You\'re about to'}),
          body,
          plan ? h('p', {class: 'fleet-bar-note',
            text: `${totalLine(plan)} · ${plan.order_note || ''}`}) : null),
        h('div', {class: 'fleet-bar-actions'},
          h('button', {class: 'button ghost', type: 'button',
            onclick: () => { resetWant(); render(); }}, 'Discard'),
          // responsiveness-exempt: confirmApply awaits a human confirming the
          // server's plan; the watcher renders after every poll once it starts.
          h('button', {class: 'button primary', type: 'button',
            disabled: plan ? null : true,
            onclick: () => { if (serverPlan) applyPlan(serverPlan); }},
          `Apply ${(plan?.steps || staged).length} change${(plan?.steps || staged).length === 1 ? '' : 's'}`))));
  }

  // ── page ────────────────────────────────────────────────────────────────

  function render() {
    if (!want) return;
    syncPlan();
    root.replaceChildren(...[
      managed() ? null : h('div', {class: 'callout warning'},
        icon('alert'), h('p', {text: EXTERNAL_SUB})),
      summaryStrip(),
      nodePanel(),
      // Per-node egress, so it follows the nodes. Its switch is fleet-wide and
      // holds its own server-side claim, so it never joins the staged plan —
      // it applies itself and publishes its own operation. See egress.js.
      egressPanel({report, managed, offer, signal, reload: () => load(true)}),
      cachePanel(),
      databasePanel(),
      warningsPanel(),
      techDetails(),
      applyBar(),
    ].filter(Boolean));
  }

  async function load(refresh = false) {
    if (!refresh) root.replaceChildren(loadingState('Loading the fleet…'));
    try {
      report = await api(`${CAPACITY_PATH}${refresh ? '?refresh=1' : ''}`, {signal});
      if (!want) resetWant();
      render();
    } catch (error) {
      if (error?.name === 'AbortError') return;
      root.replaceChildren(errorState(error, () => load()));
    }
  }

  // The page adopts whatever the watcher already holds, so returning to this
  // tab mid-batch shows the same progress the banner is reporting.
  const unsubscribe = subscribeBatch((state) => {
    running = state.terminal ? null : state.record;
    if (state.terminal) { finishPlan(state.ok); return; }
    if (!stopped()) render();
  });

  // The Refresh button lives in the page header, outside the body this page
  // repaints, so it survives its own reload — the same arrangement v1 uses.
  actions?.replaceChildren(h('button', {
    class: 'button ghost compact', type: 'button',
    onclick: (event) => runAction(event.currentTarget, () => load(true),
      {announceLabel: 'Refreshing the fleet…'}),
  }, icon('refresh'), 'Refresh'));

  root.dispose = () => { clearTimeout(planTimer); unsubscribe(); };
  return load().then(() => root);
}

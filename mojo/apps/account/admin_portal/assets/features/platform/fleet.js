// Fleet Scaling — the whole fleet on one page: app nodes, Redis, database.
//
// The interaction model is stage-then-apply: every stepper edits a local
// desired state, the staged steps are sent to POST /capacity/plan, and the
// bottom bar renders the SERVER's plan — its wording, its ordering, its cost
// delta. Nothing here invents a description or a price. Confirming applies
// the plan by its id (POST /capacity/plan/apply); the server then runs the
// steps as one batch, each step re-validated against the live fleet the
// moment it runs, and this page polls one batch status to a terminal state.
//
// Rules carried over from capacity.js:
//   1. Every control the server would refuse is absent or disabled with the
//      reason (report.actions[..].offered / blocked_reason) — nothing here
//      re-derives an offer.
//   2. Progress is polled to a terminal state; "AWS accepted it" is never
//      reported as done.
import {api, apiOnce, h, icon} from '../../core.js';
import {toast} from '../../components/actions.js';
import {openModal} from '../../components/overlays.js';
import {errorState, loadingState} from '../../components/views.js';
import {BLOCKED_COPY, PHASE_COPY} from './capacity.js';

const CAPACITY_PATH = '/api/aws/capacity';
const STATUS_PATH = '/api/aws/capacity/status';
const PLAN_PATH = '/api/aws/capacity/plan';
const PLAN_APPLY_PATH = '/api/aws/capacity/plan/apply';

const POLL_INTERVAL = 10000;
// Per STEP, matching capacity.js's 90 minutes per operation — above the
// server's CACHE_RESIZE_TIMEOUT (5400s), so the page never abandons a batch
// the server still allows.
const POLL_LIMIT = 540;

const EXTERNAL_SUB = 'Infrastructure mode is external, so this portal shows the fleet but does not change it.';

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

// ---------------------------------------------------------------------------

export async function fleetPage(ctx, signal = null) {
  const root = h('div', {class: 'fleet-page'}, loadingState('Loading the fleet…'));

  let report = null;
  // Desired state, staged locally until Apply.
  let want = null;
  // While a batch is running: the server's batch record — replaces the bar body.
  let running = null;
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

  // The self-reported reader-routing state, as a chip. Per-node on purpose:
  // the report answers from the process that served it, and a node that has
  // not restarted since the config line was added still runs without routing.
  // The instance type on the row itself — the operator's cheapest answer to
  // "what am I actually paying for here". Mono, because it is an identifier.
  // Rendered only where the report supplies one.
  function typeTag(value) {
    return value ? h('span', {class: 'fleet-tag fleet-type mono', text: value}) : null;
  }

  function routingChip(kind) {
    const state = routing()[kind] || {};
    if (kind === 'database') {
      if (state.skip_reason) {
        return h('span', {class: 'fleet-chip warn', title: state.skip_reason},
          'Reader config skipped');
      }
      if (state.active && state.matches_reader_endpoint === false) {
        return h('span', {class: 'fleet-chip warn',
          title: `${state.host} is not this cluster's reader endpoint`},
        'Reader host mismatch');
      }
      return h('span', {class: `fleet-chip ${state.active ? 'on' : ''}`,
        title: state.active ? `reads route to ${state.host} (this node)` : ''},
      state.active ? 'Reader routing: on' : 'Reader routing: off');
    }
    return h('span', {class: `fleet-chip ${state.active ? 'on' : ''}`},
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

  function wait(milliseconds) {
    return new Promise((resolve) => {
      const timeout = setTimeout(resolve, milliseconds);
      signal?.addEventListener('abort',
        () => { clearTimeout(timeout); resolve(); }, {once: true});
    });
  }

  function stopped() {
    return signal?.aborted || !root.isConnected;
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
        && (report?.sizes?.cache || []).find((r) => r.size === size);
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
          (r) => r.size === writerSize);
        const current = row.writer_instance_class || row.instance_class;
        if (rung && rung.type !== current) {
          steps.push({action: 'resize_database', resource: writerTarget,
            size: writerSize, apply_immediately: true});
        }
      }
      const readerSize = want.dbReaderSizes.get(row.identifier);
      const readerRung = readerSize
        && (report?.sizes?.database || []).find((r) => r.size === readerSize);
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

  async function runBatch(batch) {
    running = batch;
    render();
    const params = new URLSearchParams({batch: batch.id});
    const limit = POLL_LIMIT * Math.max(1, (batch.steps || []).length);
    for (let count = 0; count < limit && !stopped(); count += 1) {
      await wait(POLL_INTERVAL);
      if (stopped()) return;
      let live;
      try {
        live = await api(`${STATUS_PATH}?${params.toString()}`, {signal});
      } catch (error) {
        if (error?.name === 'AbortError') return;
        running.pollNote = `progress unavailable: ${error.message}`;
        render();
        continue;
      }
      running = live;
      render();
      if (live.state !== 'running') return finishPlan(live.state === 'done');
    }
    if (!stopped()) return finishPlan(false);
  }

  async function finishPlan(ok) {
    if (ok) toast('Fleet updated');
    await load(true);
    running = null;
    resetWant();
    render();
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

  function totalLine(plan) {
    const total = plan.total_monthly_delta_usd;
    let text = total ? `Monthly cost change: ≈ ${deltaText(total)}`
      : 'No monthly cost change';
    if (plan.estimate_complete === false) {
      text += ' (some steps have no listed price)';
    }
    return text;
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
    runBatch(batch);
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
  // node card (app-node resize is a rolling replace, its own future item)
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

  // ── cards ───────────────────────────────────────────────────────────────

  function summaryStrip() {
    const nodes = instances();
    const healthy = nodes.filter((row) => row.healthy).length;
    const cache = (report?.caches || [])[0];
    const database = (report?.databases || [])[0];
    const tile = (label, value, small, meta) => h('div', {class: 'fleet-tile'},
      h('span', {class: 'fleet-label', text: label}),
      h('span', {class: 'fleet-tile-value'}, h('b', {text: value}),
        small ? h('small', {text: ` ${small}`}) : null),
      h('span', {class: 'fleet-tile-meta', text: meta}));
    return h('div', {class: 'fleet-strip'},
      tile('App nodes', String(nodes.length),
        nodes[0]?.instance_type ? `× ${nodes[0].instance_type}` : '',
        nodes.length ? `${healthy} of ${nodes.length} healthy` : 'none registered'),
      tile('Redis', cache ? `1+${cache.replica_count || 0}` : '—',
        '',
        cache
          ? `primary + ${cache.replica_count || 0} replica${cache.replica_count === 1 ? '' : 's'} · ${cache.status}`
          : 'no replication group found'),
      tile('Database', database ? `1+${(database.readers || []).length}` : '—',
        '',
        database
          ? `writer + ${(database.readers || []).length} reader${(database.readers || []).length === 1 ? '' : 's'} · ${database.status}`
          : 'no database found'));
  }

  function nodeCard() {
    const rows = instances();
    const add = offer('add_node');
    const drain = offer('drain_node');
    const healthyLeft = rows.filter(
      (row) => row.healthy && !want.removeNodes.has(row.id)).length;
    const total = rows.length - want.removeNodes.size + want.addNodes;
    const type = rows[0]?.instance_type || '';

    const removable = (row) => managed() && drain.offered && !row.self
      && !want.removeNodes.has(row.id) && (!row.healthy || healthyLeft > 1);

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
      const labels = [
        row.self ? 'this node' : '', row.primary ? 'certbot primary' : '',
        row.state || '',
      ].filter(Boolean).join(' · ');
      return h('div', {class: `fleet-row ${leaving ? 'leaving' : ''}`},
        h('span', {class: `fleet-pill ${leaving ? 'warn' : row.healthy ? 'ok' : 'warn'}`},
          h('span', {class: 'fleet-pill-dot'}),
          leaving ? 'Will drain' : row.healthy ? 'Healthy' : (row.state || 'unknown')),
        h('span', {class: 'fleet-name mono', text: row.name || row.id}),
        typeTag(row.instance_type),
        labels ? h('span', {class: 'fleet-tag', text: labels}) : null,
        h('span', {class: 'fleet-row-side'},
          leaving
            ? h('button', {class: 'fleet-link undo', type: 'button',
              onclick: () => { want.removeNodes.delete(row.id); render(); }}, 'Keep it')
            : removable(row)
              ? h('button', {class: 'fleet-link', type: 'button',
                onclick: () => { want.removeNodes.add(row.id); render(); }}, 'Remove')
              : h('span', {class: 'fleet-floor',
                text: row.self ? 'answering this request' : ''})));
    };

    const ghostRow = (index) => h('div', {class: 'fleet-row ghost'},
      h('span', {class: 'fleet-pill pend'}, h('span', {class: 'fleet-pill-dot'}), 'Planned'),
      h('span', {class: 'fleet-name mono', text: 'new node'}),
      type ? h('span', {class: 'fleet-tag', text: type}) : null,
      h('span', {class: 'fleet-row-side'},
        h('button', {class: 'fleet-link undo', type: 'button',
          onclick: () => { want.addNodes -= 1; render(); }}, 'Cancel')));

    return h('div', {class: `fleet-card ${want.addNodes || want.removeNodes.size ? 'changed' : ''}`},
      h('div', {class: 'fleet-card-head'},
        h('div', {class: 'fleet-card-title'},
          h('h3', {text: 'App nodes'}),
          h('p', {text: 'The machines that serve web requests and run background jobs.'}))),
      managed() ? h('div', {class: 'fleet-controls'},
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
      h('div', {class: 'fleet-rows'},
        ...rows.map(nodeRow),
        ...Array.from({length: want.addNodes}, (unused, index) => ghostRow(index))));
  }

  function cacheCard() {
    const rows = report?.caches || [];
    const change = offer('set_cache_replicas');
    if (!rows.length) {
      return h('div', {class: 'fleet-card'},
        h('div', {class: 'fleet-card-head'},
          h('div', {class: 'fleet-card-title'},
            h('h3', {text: 'Redis'}),
            h('p', {text: blockedText(change) || 'No ElastiCache replication group was found.'}))));
    }
    return h('div', {class: `fleet-card ${rows.some((row) =>
      want.caches.get(row.identifier) !== Number(row.replica_count || 0)
      || want.cacheSizes.has(row.identifier)) ? 'changed' : ''}`},
    h('div', {class: 'fleet-card-head'},
      h('div', {class: 'fleet-card-title'},
        h('h3', {text: 'Redis'}),
        h('p', {text: 'Caching, sessions, live updates and the job queue.'})),
      routingChip('redis')),
    ...rows.map((row) => {
      const current = Number(row.replica_count || 0);
      const min = Number(row.min_replicas || 0);
      const wanted = want.caches.get(row.identifier) ?? current;
      const offered = managed() && change.offered && !row.blocked_reason;
      const blocked = row.blocked_reason
        ? blockedText({offered: false, blocked_reason: row.blocked_reason})
        : blockedText(change);
      const resize = offer('resize_cache');
      const resizable = managed() && resize.offered && !row.blocked_reason;
      // The interruption case is the SERVER's statement (resize_impact),
      // surfaced before apply — the page never re-derives the policy.
      const impactNote = row.resize_impact === 'rolling'
        ? 'Resize rolls replicas first, then a brief failover — one short interruption.'
        : 'No replica: the cache is down while its node is replaced.';
      return h('div', {class: 'fleet-group'},
        offered ? h('div', {class: 'fleet-controls'},
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
          : h('p', {class: 'fleet-note warn-text', text: blocked}),
        h('div', {class: 'fleet-note'},
          icon('activity'),
          h('span', {text: routing().redis?.active
            ? 'Replicas are failover cover first: if the primary dies, one takes '
              + 'over in about a minute. Dashboards and metrics also read from '
              + 'them — but they never make writes faster.'
            : 'Replicas are failover cover: if the primary dies, one takes over '
              + 'in about a minute. Reader reads are off on this node, so they '
              + 'do not serve any traffic until then.'})),
        h('div', {class: 'fleet-rows'},
          ...(row.members || []).map((member) => h('div', {class: 'fleet-row'},
            h('span', {class: `fleet-pill ${row.status === 'available' ? 'ok' : 'warn'}`},
              h('span', {class: 'fleet-pill-dot'}),
              row.status === 'available' ? 'Healthy' : row.status),
            h('span', {class: 'fleet-name mono', text: member.id || member}),
            typeTag(member.node_type || row.node_type),
            h('span', {class: 'fleet-tag', text: member.role || ''}))),
          ...Array.from({length: Math.max(0, wanted - current)}, () =>
            h('div', {class: 'fleet-row ghost'},
              h('span', {class: 'fleet-pill pend'},
                h('span', {class: 'fleet-pill-dot'}), 'Planned'),
              h('span', {class: 'fleet-name mono', text: 'new replica'}),
              h('span', {class: 'fleet-row-side'},
                h('button', {class: 'fleet-link undo', type: 'button',
                  onclick: () => { want.caches.set(row.identifier, wanted - 1); render(); }},
                'Cancel'))))));
    }));
  }

  function databaseCard() {
    const rows = report?.databases || [];
    const add = offer('add_reader');
    const remove = offer('remove_reader');
    if (!rows.length) {
      return h('div', {class: 'fleet-card'},
        h('div', {class: 'fleet-card-head'},
          h('div', {class: 'fleet-card-title'},
            h('h3', {text: 'Database'}),
            h('p', {text: blockedText(add) || 'No database was found.'}))));
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
    return h('div', {class: `fleet-card ${changed ? 'changed' : ''}`},
      h('div', {class: 'fleet-card-head'},
        h('div', {class: 'fleet-card-title'},
          h('h3', {text: 'Database'}),
          h('p', {text: 'Postgres. Every account, message and record lives here.'})),
        routingChip('database')),
      ...rows.map((row) => {
        const readers = row.readers || [];
        const adding = want.dbAdd.get(row.identifier) || 0;
        const removingHere = readers.filter((reader) => want.dbRemove.has(reader));
        const count = readers.length - removingHere.length + adding;
        return h('div', {class: 'fleet-group'},
          managed() ? h('div', {class: 'fleet-controls'},
            h('div', {class: 'fleet-control'},
              h('span', {class: 'fleet-label', text: `Read replicas · ${row.identifier}`}),
              stepper({
                value: count,
                canDown: adding > 0 || (remove.offered && readers.some(
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
          h('div', {class: 'fleet-note'},
            icon('activity'),
            h('span', {text: routing().database?.active
              ? 'Reader routing is on: safe reads spread across the replicas, and '
                + 'anything just written is read back from the writer. If saving is '
                + 'slow, the answer is a bigger writer, not more readers.'
              : 'Reader routing is off on this node: every query goes to the '
                + 'writer, so a reader is standby capacity until it is configured. '
                + 'If saving is slow, the answer is a bigger writer, not more readers.'})),
          h('div', {class: 'fleet-rows'},
            h('div', {class: 'fleet-row'},
              h('span', {class: `fleet-pill ${row.status === 'available' ? 'ok' : 'warn'}`},
                h('span', {class: 'fleet-pill-dot'}),
                row.status === 'available' ? 'Healthy' : row.status),
              h('span', {class: 'fleet-name mono', text: row.writer || row.identifier}),
              typeTag(row.writer_instance_class || row.instance_class),
              h('span', {class: 'fleet-tag', text: 'writer'})),
            ...readers.map((reader) => {
              const leaving = want.dbRemove.has(reader);
              return h('div', {class: `fleet-row ${leaving ? 'leaving' : ''}`},
                h('span', {class: `fleet-pill ${leaving ? 'warn' : 'ok'}`},
                  h('span', {class: 'fleet-pill-dot'}),
                  leaving ? 'Will remove' : 'Healthy'),
                h('span', {class: 'fleet-name mono', text: reader}),
                typeTag((row.reader_instance_classes || {})[reader]),
                h('span', {class: 'fleet-tag', text: 'reader'}),
                h('span', {class: 'fleet-row-side'},
                  leaving
                    ? h('button', {class: 'fleet-link undo', type: 'button',
                      onclick: () => { want.dbRemove.delete(reader); render(); }}, 'Keep it')
                    : remove.offered
                      ? h('button', {class: 'fleet-link', type: 'button',
                        onclick: () => { want.dbRemove.add(reader); render(); }}, 'Remove')
                      : null));
            }),
            ...Array.from({length: adding}, () => h('div', {class: 'fleet-row ghost'},
              h('span', {class: 'fleet-pill pend'},
                h('span', {class: 'fleet-pill-dot'}), 'Planned'),
              h('span', {class: 'fleet-name mono', text: 'new reader'}),
              h('span', {class: 'fleet-row-side'},
                h('button', {class: 'fleet-link undo', type: 'button',
                  onclick: () => { want.dbAdd.set(row.identifier, adding - 1); render(); }},
                'Cancel'))))));
      }));
  }

  function warningsCard() {
    const warnings = report?.warnings || [];
    if (!warnings.length) return null;
    return h('div', {class: 'fleet-card'},
      h('div', {class: 'fleet-card-head'},
        h('div', {class: 'fleet-card-title'},
          h('h3', {text: 'Access'}),
          h('p', {text: 'Parts of AWS this page could not read — unknown, not empty.'}))),
      h('div', {class: 'fleet-rows'},
        ...warnings.map((warning) => h('div', {class: 'fleet-row'},
          h('span', {class: 'fleet-pill warn'},
            h('span', {class: 'fleet-pill-dot'}), 'Unavailable'),
          h('span', {class: 'fleet-name mono', text: warning.iam_action || 'AWS'}),
          h('span', {class: 'fleet-tag', text: warning.message || ''})))));
  }

  function techDetails() {
    return h('details', {class: 'fleet-tech'},
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
          h('button', {class: 'button primary', type: 'button',
            disabled: plan ? null : true,
            onclick: () => { if (serverPlan) applyPlan(serverPlan); }},
          `Apply ${(plan?.steps || staged).length} change${(plan?.steps || staged).length === 1 ? '' : 's'}`))));
  }

  // ── page ────────────────────────────────────────────────────────────────

  function render() {
    syncPlan();
    root.replaceChildren(...[
      managed() ? null : h('div', {class: 'callout warning'},
        icon('alert'), h('p', {text: EXTERNAL_SUB})),
      summaryStrip(),
      nodeCard(),
      cacheCard(),
      databaseCard(),
      warningsCard(),
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

  await load();
  return root;
}

// Fleet Scaling — the whole fleet on one page: app nodes, Redis, database.
//
// The interaction model is stage-then-apply: every stepper edits a local
// desired state, the bottom bar lists what would change in plain words, and
// Apply runs the steps one at a time through the EXISTING capacity actions —
// each one still validated server-side at execution time, exactly as if the
// operator had clicked them individually in the capacity panel.
//
// Rules carried over from capacity.js:
//   1. Every control the server would refuse is absent or disabled with the
//      reason (report.actions[..].offered / blocked_reason) — nothing here
//      re-derives an offer.
//   2. Progress is polled to a terminal state; "AWS accepted it" is never
//      reported as done.
//
// Deliberate placeholders, disabled until their server sides ship:
//   - the server-written plan (plan/apply batch API) — today the step list is
//     assembled here from the same offers the server published.
import {api, apiOnce, h, icon} from '../../core.js';
import {toast} from '../../components/actions.js';
import {openModal} from '../../components/overlays.js';
import {errorState, loadingState} from '../../components/views.js';
import {BLOCKED_COPY, PHASE_COPY} from './capacity.js';

const CAPACITY_PATH = '/api/aws/capacity';
const STATUS_PATH = '/api/aws/capacity/status';
const APPLY_PATH = '/api/aws/capacity/apply';

const POLL_INTERVAL = 10000;
// 90 minutes per operation, matching capacity.js — above the server's
// CACHE_RESIZE_TIMEOUT (5400s), so the page never abandons an operation the
// server still allows.
const POLL_LIMIT = 540;

const EXTERNAL_SUB = 'Infrastructure mode is external, so this portal shows the fleet but does not change it.';

// Rough monthly on-demand estimates, keyed by instance type. Display-only
// ("≈"), and only where the report names a type; the authoritative number
// arrives with the server-written plan when the batch API ships.
const EST_MONTHLY = {
  't3.small': 15, 't3.medium': 30, 't3.large': 60, 't3.xlarge': 121,
  'm6i.large': 70,
};

function estimate(type) {
  return EST_MONTHLY[type] || null;
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
  // While a plan is running: {steps, index, phase} — replaces the bar body.
  let running = null;

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

  // ── the plan ────────────────────────────────────────────────────────────

  // Step kind drives only the dot colour: up-ladder reads as 'add', down as
  // 'remove', and an unknown direction (current type not on the ladder)
  // defaults to 'add'.
  function sizeDirection(ladder, currentType, rung) {
    const from = (ladder || []).findIndex((r) => r.type === currentType);
    const to = (ladder || []).indexOf(rung);
    if (from < 0 || to < 0) return 'add';
    return to < from ? 'remove' : 'add';
  }

  // Adds before removes, always — the same ordering the batch API will
  // enforce server-side. Each step carries the submissions it will make.
  function plan() {
    const steps = [];
    for (let index = 0; index < want.addNodes; index += 1) {
      steps.push({
        key: `add-node-${index}`, kind: 'add',
        label: 'Add an app node',
        note: 'builds, deploys and proves itself before serving · 20–40 min',
        submits: [{action: 'add_node', confirm_resource: 'add_node'}],
      });
    }
    for (const [identifier, addCount] of want.dbAdd) {
      for (let index = 0; index < addCount; index += 1) {
        steps.push({
          key: `add-reader-${identifier}-${index}`, kind: 'add',
          label: `Add a read replica to ${identifier}`,
          note: '~10 min to come online',
          submits: [{action: 'add_reader', resource: identifier,
            confirm_resource: identifier}],
        });
      }
    }
    for (const row of report?.caches || []) {
      const current = Number(row.replica_count || 0);
      const wanted = want.caches.get(row.identifier);
      if (wanted === undefined || wanted === current) continue;
      steps.push({
        key: `cache-${row.identifier}`, kind: wanted > current ? 'add' : 'remove',
        label: `Change ${row.identifier} replicas ${current} → ${wanted}`,
        note: 'applies immediately — ElastiCache has no maintenance-window option here',
        submits: [{action: 'set_cache_replicas', resource: row.identifier,
          confirm_resource: row.identifier, count: wanted, apply_immediately: true}],
      });
    }
    for (const row of report?.caches || []) {
      const size = want.cacheSizes.get(row.identifier);
      if (!size) continue;
      const rung = (report?.sizes?.cache || []).find((r) => r.size === size);
      if (!rung || rung.type === row.node_type) continue;
      steps.push({
        key: `resize-cache-${row.identifier}`,
        kind: sizeDirection(report?.sizes?.cache, row.node_type, rung),
        label: `Resize ${row.identifier} to ${rung.type}`,
        note: row.resize_impact === 'rolling'
          ? 'rolls replicas first, then a brief failover — one short interruption'
          : 'no replica: the cache is down while its node is replaced',
        submits: [{action: 'resize_cache', resource: row.identifier,
          confirm_resource: row.identifier, size, apply_immediately: true}],
      });
    }
    for (const row of report?.databases || []) {
      const writerSize = want.dbWriterSizes.get(row.identifier);
      const writerTarget = row.kind === 'aurora' ? row.writer : row.identifier;
      if (writerSize && writerTarget) {
        const rung = (report?.sizes?.database || []).find((r) => r.size === writerSize);
        const current = row.writer_instance_class || row.instance_class;
        if (rung && rung.type !== current) {
          steps.push({
            key: `resize-writer-${row.identifier}`,
            kind: sizeDirection(report?.sizes?.database, current, rung),
            label: `Resize writer ${writerTarget} to ${rung.type}`,
            note: '~minutes offline while the writer changes class',
            submits: [{action: 'resize_database', resource: writerTarget,
              confirm_resource: writerTarget, size: writerSize,
              apply_immediately: true}],
          });
        }
      }
      // ONE staged reader size, fanned out into one step per reader whose
      // class differs — skipping any reader this same plan removes: resizing
      // a doomed reader is never what the operator meant.
      const readerSize = want.dbReaderSizes.get(row.identifier);
      const readerRung = readerSize
        && (report?.sizes?.database || []).find((r) => r.size === readerSize);
      if (readerRung) {
        for (const reader of row.readers || []) {
          if (want.dbRemove.has(reader)) continue;
          const current = (row.reader_instance_classes || {})[reader];
          if (current === readerRung.type) continue;
          steps.push({
            key: `resize-reader-${reader}`,
            kind: sizeDirection(report?.sizes?.database, current, readerRung),
            label: `Resize reader ${reader} to ${readerRung.type}`,
            note: 'reads keep flowing; this reader pauses while it changes class',
            submits: [{action: 'resize_database', resource: reader,
              confirm_resource: reader, size: readerSize,
              apply_immediately: true}],
          });
        }
      }
    }
    for (const reader of want.dbRemove) {
      steps.push({
        key: `remove-reader-${reader}`, kind: 'remove',
        label: `Remove read replica ${reader}`,
        note: 'deleted with no final snapshot — it is a copy of the primary',
        submits: [{action: 'remove_reader', resource: reader,
          confirm_resource: reader}],
      });
    }
    for (const id of want.removeNodes) {
      const row = instances().find((node) => node.id === id);
      steps.push({
        key: `remove-node-${id}`, kind: 'remove',
        label: `Drain and shut down ${row?.name || id}`,
        note: 'traffic moves off it first; the instance is then terminated',
        submits: [
          {action: 'drain_node', resource: id, confirm_resource: id},
          {action: 'terminate_node', resource: id, confirm_resource: id},
        ],
      });
    }
    return steps;
  }

  // ── the runner ──────────────────────────────────────────────────────────

  async function follow(operation, onPhase) {
    const params = new URLSearchParams({operation});
    for (let count = 0; count < POLL_LIMIT && !stopped(); count += 1) {
      await wait(POLL_INTERVAL);
      if (stopped()) return {state: 'aborted'};
      let live;
      try {
        live = await api(`${STATUS_PATH}?${params.toString()}`, {signal});
      } catch (error) {
        if (error?.name === 'AbortError') return {state: 'aborted'};
        onPhase(`progress unavailable: ${error.message}`);
        continue;
      }
      if (live.state === 'running') { onPhase(phaseLine(live)); continue; }
      return live;
    }
    return {state: 'failed', message: 'still running after 90 minutes — check the AWS console'};
  }

  async function runPlan(steps) {
    running = {steps, index: 0, phase: '', failed: null, done: []};
    render();
    for (let index = 0; index < steps.length; index += 1) {
      if (stopped()) return;
      running.index = index;
      const step = steps[index];
      for (const payload of step.submits) {
        running.phase = 'requesting…';
        render();
        let started;
        try {
          started = await apiOnce(APPLY_PATH, {method: 'POST', signal,
            body: JSON.stringify(payload)});
        } catch (error) {
          if (error?.code === 'fresh_auth_required') { running = null; render(); return; }
          running.failed = {index, message: error.message};
          render();
          return finishPlan(false);
        }
        const final = await follow(started.id, (text) => {
          running.phase = text;
          render();
        });
        if (final.state === 'aborted') return;
        if (final.state !== 'done') {
          running.failed = {index, message: phaseLine(final)};
          render();
          return finishPlan(false);
        }
      }
      running.done.push(index);
      render();
    }
    return finishPlan(true);
  }

  async function finishPlan(ok) {
    if (ok) toast('Fleet updated');
    await load(true);
    running = null;
    resetWant();
    render();
  }

  function confirmApply(steps) {
    return new Promise((resolve) => {
      let settled = false;
      const settle = (value) => { if (settled) return; settled = true; close(); resolve(value); };
      const content = h('div', {},
        h('p', {class: 'modal-copy',
          text: 'These run one at a time, in this order. Each step is re-checked '
            + 'against the live fleet the moment it runs; if one fails, the steps '
            + 'after it are not attempted.'}),
        h('ul', {class: 'fleet-confirm-list'},
          ...steps.map((step) => h('li', {},
            h('span', {class: `fleet-dot ${step.kind}`}),
            h('span', {text: step.label})))),
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
    const each = estimate(type);
    const cost = each ? `≈ $${each * total}` : '';

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
          h('p', {text: 'The machines that serve web requests and run background jobs.'})),
        cost ? h('span', {class: 'fleet-cost', text: `${cost} / mo`}) : null),
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
          + 'talks to AWS directly. Apply runs the steps in order (adds before removes), '
          + 'and every step re-checks its guards against the live fleet the moment it '
          + 'runs — a control the server would refuse is disabled with the reason.'}),
        h('p', {text: 'Coming, and deliberately absent until their server sides ship: '
          + 'a server-written plan with exact costs and timings, and automatic '
          + 'overnight failback of the writer after a database failover.'})));
  }

  // ── the apply bar ───────────────────────────────────────────────────────

  function applyBar(steps) {
    if (running) {
      return h('div', {class: 'fleet-bar show'},
        h('div', {class: 'fleet-progress'},
          ...running.steps.map((step, index) => {
            const state = running.done.includes(index) ? 'done'
              : running.failed && index === running.failed.index ? 'failed'
                : running.failed || index > running.index ? 'pending'
                  : index === running.index ? 'active' : 'pending';
            return h('div', {class: `fleet-pstep ${state}`},
              state === 'active' ? h('span', {class: 'pending-spinner'})
                : state === 'done' ? h('span', {class: 'fleet-tick', text: '✓'})
                  : state === 'failed' ? h('span', {class: 'fleet-tick', text: '✕'})
                    : h('span', {class: 'fleet-tick', text: ''}),
              h('span', {text: step.label}),
              state === 'active' ? h('span', {class: 'fleet-phase', text: running.phase}) : null,
              state === 'failed' ? h('span', {class: 'fleet-phase danger-text',
                text: running.failed.message}) : null);
          }),
          running.failed ? h('div', {class: 'form-actions'},
            h('button', {class: 'button', type: 'button',
              onclick: () => finishPlan(false)}, 'Close')) : null));
    }
    if (!steps.length) return h('div', {class: 'fleet-bar'});
    return h('div', {class: 'fleet-bar show'},
      h('div', {class: 'fleet-bar-in'},
        h('div', {class: 'fleet-bar-list'},
          h('h4', {text: 'You\'re about to'}),
          h('ul', {},
            ...steps.map((step) => h('li', {},
              h('span', {class: `fleet-dot ${step.kind}`}),
              h('span', {}, step.label,
                step.note ? h('span', {class: 'fleet-bar-note', text: ` — ${step.note}`}) : null))))),
        h('div', {class: 'fleet-bar-actions'},
          h('button', {class: 'button ghost', type: 'button',
            onclick: () => { resetWant(); render(); }}, 'Discard'),
          h('button', {class: 'button primary', type: 'button',
            onclick: async () => {
              if (await confirmApply(steps)) runPlan(steps);
            }},
          `Apply ${steps.length} change${steps.length === 1 ? '' : 's'}`))));
  }

  // ── page ────────────────────────────────────────────────────────────────

  function render() {
    const steps = running ? running.steps : plan();
    root.replaceChildren(...[
      managed() ? null : h('div', {class: 'callout warning'},
        icon('alert'), h('p', {text: EXTERNAL_SUB})),
      summaryStrip(),
      nodeCard(),
      cacheCard(),
      databaseCard(),
      warningsCard(),
      techDetails(),
      applyBar(steps),
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

// Infrastructure ▸ Capacity ▸ Outbound addresses — the fleet's stable egress.
//
// v1 kept these two operator actions inside the Dashboard's EC2 drill-in
// (platform/capacity.js egressRows). They are per-node egress, so here they
// sit with the rest of the fleet controls, in the same anatomy as App nodes: a
// purpose line, a state chip, ONE control with the consequence under it, and
// one member row per live address.
//
// Rules carried over from v1, none of them relaxed:
//   1. A failed read is UNKNOWN — never "off", never an empty allowlist.
//   2. The switch is offered only where the SERVER offers the action
//      (report.actions[..].offered); nothing here re-derives an offer, and a
//      refusal is shown in v1's words for that blocked_reason.
//   3. Disable keeps v1's typed echo. Detaching an allowlisted address is the
//      one change a provider notices before the operator does.
//   4. Progress is polled to a terminal state, and the capacity report is
//      re-read afterwards, so the roster is the provider's truth rather than
//      this page's optimism.
//
// The stable-IPs switch is fleet-wide and holds its own server-side claim, so
// it is NOT part of the staged plan the rest of the tab builds — the server
// refuses it inside a batch. It runs through the single-action apply, and its
// operation is published to the global banner exactly like a batch is, so it
// keeps being watched after the operator navigates away.
import {api, apiOnce, h, icon} from '../../core.js';
import {copyButton, runAction, toast} from '../../components/actions.js';
import {openModal} from '../../components/overlays.js';
import {routeHref} from '../../components/routes.js';
import {
  remove as removeOperation, upsert as upsertOperation,
} from '../../components/operations.js';

const STATUS_PATH = '/api/aws/capacity/status';
const APPLY_PATH = '/api/aws/capacity/apply';

const ENABLE = 'enable_stable_ips';
const DISABLE = 'disable_stable_ips';

// Where the operation banner sends an operator who wants to watch this.
const OPERATION_HREF = routeHref('infrastructure', {tab: 'capacity'});

const POLL_INTERVAL = 10000;
// 90 minutes at one poll every ten seconds, the same ceiling v1 used.
const POLL_LIMIT = 540;

// v1's sentences, unchanged: one blocked_reason reads identically in both
// portals. Only the egress reasons live here — the fleet ones are capacity.js.
const BLOCKED_COPY = {
  infrastructure_external: 'external infrastructure mode — capacity is applied by your infrastructure team\'s IaC',
  already_enabled: 'on — every node holds its stable address',
  not_enabled: 'stable outbound IPs are off and nothing is attached',
  no_fleet_nodes: 'no node is registered behind a load balancer',
  fleet_unavailable: 'the serving tier could not be read, so the fleet is unknown — not empty',
  addresses_unavailable: 'the region\'s Elastic IPs could not be read',
  policy_unavailable: 'the stable-IPs policy could not be read — its state is unknown, not off',
};

// The provider's phase, in words. v1's map, narrowed to the egress ladders:
// enable runs planning → associating → verifying, disable detaching →
// verifying.
const PHASE_COPY = {
  planning: 'planning which node gets which address',
  associating: 'attaching stable addresses',
  detaching: 'detaching stable addresses',
  addressing: 'attaching the fleet\'s stable outbound address',
  verifying: 're-reading AWS to prove the result',
  complete: 'done',
};

// The consequence that sits under the switch — the short form of what the
// confirmation then states in full.
const TURN_ON_CONSEQUENCE = 'Every registered node gets a permanent Elastic IP '
  + 'for its outbound calls. Attaching briefly cuts in-flight OUTBOUND '
  + 'connections; inbound service through the balancer is unaffected.';
const TURN_OFF_CONSEQUENCE = 'Providers that allowlisted these addresses may '
  + 'start refusing this fleet\'s calls the moment they detach. The '
  + 'reservations are kept, so re-enabling restores the same addresses.';

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

function monthlyPerAddress(egress) {
  const value = Number(egress.monthly_usd_per_address);
  return Number.isFinite(value) && value > 0 ? value.toFixed(2) : '3.60';
}

// ── the vocabulary ──────────────────────────────────────────────────────────
//
// Deliberate local copies of capacity.js's three view helpers rather than an
// import: capacity.js imports THIS module to place the panel, and a cycle
// between the two would be a worse trade than thirty lines of pure markup.
// The class names are the shared ones, so the two panels stay identical.

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

function resourcePanel({title, purpose, chip, controls, callout, members}) {
  return h('section', {class: 'panel fleet-panel'},
    h('div', {class: 'panel-head'},
      h('div', {}, h('h2', {text: title}), h('p', {text: purpose})),
      chip || null),
    controls || null,
    callout ? h('div', {class: 'fleet-callout'}, callout) : null,
    members && members.length ? h('div', {class: 'fleet-members'}, ...members) : null);
}

// ── the operation watcher ───────────────────────────────────────────────────
//
// Module-level, for the same reason capacity.js's batch watcher is: the change
// outlives the page that started it. The switch is fleet-wide and the server
// holds one claim for it, so there is at most one of these at a time.

// {id, action, title, startedAt, record, note} while something is running.
let watcher = null;
// The last terminal FAILURE, kept on screen until the operator acts again — a
// toast can be missed, and a refusal is the thing they most need to read.
let outcome = null;
// {host, paint, reload} of the panel currently on screen, or null. Newest
// wins; an older panel has already been dropped by the tab's own re-render.
// The operator may also have navigated away entirely, which is what `host`
// answers — a detached panel is neither repainted nor asked to reload.
let mounted = null;

function onScreen() {
  if (mounted && !mounted.host.isConnected) mounted = null;
  return mounted;
}

function repaint() {
  onScreen()?.paint();
}

function operationTitle(action) {
  return action === DISABLE
    ? 'Releasing stable outbound addresses'
    : 'Assigning stable outbound addresses';
}

function publish(current) {
  upsertOperation({
    id: current.id, title: current.title, phase: phaseLine(current.record),
    startedAt: current.startedAt, href: OPERATION_HREF,
  });
}

function finish(current, ok) {
  if (watcher !== current) return;
  removeOperation(current.id);
  watcher = null;
  const message = phaseLine(current.record);
  outcome = ok ? null : {message};
  toast(ok ? `${current.title} — ${message}` : message,
    {tone: ok ? 'success' : 'danger'});
  // Provider truth, not ours: re-read the report so the roster shows what AWS
  // actually holds now. The re-read repaints this panel on its way through.
  // Nobody is looking at Capacity? Nothing to re-read — the next mount loads
  // the report fresh anyway.
  onScreen()?.reload();
}

async function pump(current) {
  const params = new URLSearchParams({operation: current.id});
  for (let count = 0; count < POLL_LIMIT; count += 1) {
    await wait(POLL_INTERVAL);
    if (watcher !== current) return;
    let live;
    try {
      // No abort signal: this poll belongs to the operation, not to whichever
      // page happens to be on screen.
      live = await api(`${STATUS_PATH}?${params.toString()}`);
    } catch (error) {
      if (watcher !== current) return;
      current.note = `progress unavailable: ${error.message}`;
      repaint();
      continue;
    }
    if (watcher !== current) return;
    current.record = live;
    current.note = '';
    publish(current);
    if (live.state !== 'running') { finish(current, live.state === 'done'); return; }
    repaint();
  }
  if (watcher !== current) return;
  // Still running as far as anyone here knows, so the banner keeps saying so —
  // this stops polling, it does not decide the operation ended.
  current.note = 'still running after 90 minutes — check the AWS console';
  current.stopped = true;
  upsertOperation({
    id: current.id, title: current.title, phase: current.note,
    startedAt: current.startedAt, href: OPERATION_HREF,
  });
  repaint();
}

// ── confirmation ────────────────────────────────────────────────────────────
//
// v2's consequence dialog — a warning callout stating the consequence, the
// notes an operator would otherwise assume wrongly, and the action's own verb.
// Disable adds v1's typed echo on top; enable does not, matching the rest of
// this tab, where the plan itself is what the operator confirms.

function confirmEgress({title, subtitle, consequence, notes = [], verb,
  expect = '', danger = false}) {
  return new Promise((resolve) => {
    let settled = false;
    const settle = (value) => { if (settled) return; settled = true; close(); resolve(value); };
    const typed = expect ? h('input', {autocomplete: 'off', placeholder: expect}) : null;
    const go = h('button', {
      class: `button ${danger ? 'danger' : 'primary'}`, type: 'button',
      disabled: expect ? true : null,
      onclick: () => settle(true),
    }, verb);
    if (typed) {
      const validate = () => { go.disabled = typed.value.trim() !== expect; };
      typed.addEventListener('input', validate);
    }
    const content = h('div', {},
      h('div', {class: 'callout warning'}, icon('alert'), h('p', {text: consequence})),
      ...notes.filter(Boolean).map((text) => h('p', {class: 'modal-copy', text})),
      typed ? h('label', {class: 'field'},
        h('span', {text: `Type ${expect} to confirm`}), typed) : null,
      h('div', {class: 'form-actions'},
        h('button', {class: 'button ghost', type: 'button',
          onclick: () => settle(false)}, 'Cancel'),
        go));
    const close = openModal({
      title, subtitle, content, danger: true,
      onClose: () => { if (!settled) { settled = true; resolve(false); } },
    });
  });
}

// ── the panel ───────────────────────────────────────────────────────────────

/**
 * The Outbound addresses panel.
 *
 * `report` is the capacity report the tab already read; `offer(name)` and
 * `managed()` are the tab's own accessors, so the two panels can never
 * disagree about what the server is offering. `reload()` re-reads the report.
 */
export function egressPanel({report, managed, offer, reload, signal = null}) {
  const host = h('div', {class: 'egress-host'});

  const egress = () => report?.egress || {};
  const nodeName = (id) => (report?.nodes?.instances || []).find(
    (row) => row.id === id)?.name || id;

  async function submit(action) {
    if (watcher) return;
    let started;
    try {
      started = await apiOnce(APPLY_PATH, {method: 'POST', signal,
        body: JSON.stringify({action, confirm_resource: action})});
    } catch (error) {
      // 440: the shared client already prompted for fresh auth and already
      // retried once. Nothing to say here — the operator either signed in and
      // it went through, or they dismissed it.
      if (error?.name === 'AbortError' || error?.code === 'fresh_auth_required') return;
      outcome = {message: error.message || 'The change was refused.'};
      toast(outcome.message, {tone: 'danger'});
      repaint();
      return;
    }
    watcher = {
      id: String(started.id), action, title: operationTitle(action),
      startedAt: Date.now(), record: started, note: '', stopped: false,
    };
    outcome = null;
    publish(watcher);
    repaint();
    pump(watcher);
  }

  async function turnOn() {
    const state = egress();
    const reserved = (state.reserved || []).map((row) => row.public_ip).filter(Boolean);
    const toAllocate = Number(state.to_allocate || 0);
    const pending = (state.pending_nodes || []).length;
    const ok = await confirmEgress({
      title: 'Turn on stable outbound IPs?',
      subtitle: pending ? `${pending} node(s) need an address` : 'already converged',
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
    if (ok) await submit(ENABLE);
  }

  async function turnOff() {
    const state = egress();
    const list = (state.addresses || []).join(', ');
    const ok = await confirmEgress({
      title: 'Turn off stable outbound IPs?',
      subtitle: list,
      verb: 'Disable stable IPs',
      expect: DISABLE,
      danger: true,
      consequence:
        'Providers that allowlisted these addresses may start refusing this '
        + 'fleet\'s calls the moment they detach. Each node is left on a NEW '
        + 'auto-assigned address after a gap of up to a few minutes — or with '
        + 'no public address at all if its network interface was not launched '
        + 'with auto-assign — and outbound provider calls fail during that gap.',
      notes: [
        'The addresses are KEPT reserved, so re-enabling restores the exact '
        + 'same allowlisted addresses. From that moment each reservation bills '
        + `~$${monthlyPerAddress(state)}/month on top of the node's `
        + 'auto-assigned address.',
        'Releasing the reserved addresses is deliberately not part of this '
        + 'switch — that stays an AWS console action.',
      ],
    });
    if (ok) await submit(DISABLE);
  }

  // ── pieces ──────────────────────────────────────────────────────────────

  function allowlist(addresses, {pending = 0} = {}) {
    if (!addresses.length) return null;
    const text = addresses.join(', ');
    return h('div', {class: 'egress-allowlist'},
      h('span', {class: 'fleet-label', text: 'Give providers'}),
      h('code', {class: 'mono egress-ips', text}),
      copyButton(text, {label: 'Copy'}),
      pending
        ? h('span', {class: 'egress-warn',
          text: `${pending} node(s) still without a stable address`})
        : null);
  }

  // The last refusal or failure, in the server's words. A toast can be missed,
  // and this is the sentence that says why nothing changed — so it stays until
  // the operator either tries again or dismisses it.
  function outcomeLine() {
    if (!outcome) return null;
    return h('div', {class: 'egress-outcome'},
      h('p', {class: 'fleet-blocked', text: outcome.message}),
      h('button', {class: 'fleet-link undo', type: 'button',
        onclick: () => { outcome = null; repaint(); }}, 'Dismiss'));
  }

  // While the switch is running, it IS the progress: the control is gone and
  // the phase the server last reported stands in its place.
  function progressControls() {
    const failed = watcher.record?.state === 'failed';
    return h('div', {class: 'fleet-controls'},
      h('div', {class: 'fleet-control'},
        h('span', {class: 'fleet-label', text: 'Stable outbound IPs'}),
        stateBadge({tone: failed ? 'danger' : 'pend',
          label: failed ? 'Failed' : 'Working',
          spinner: !failed && !watcher.stopped}),
        h('span', {class: 'fleet-floor',
          text: `${watcher.title} · ${watcher.note || phaseLine(watcher.record)}`})));
  }

  function switchControls() {
    const state = egress();
    const enable = offer(ENABLE);
    const disable = offer(DISABLE);
    const on = Boolean(state.enabled);
    // The switch flips the CURRENT state, so the offer that gates it is the
    // one for the direction it would move in.
    const action = on ? disable : enable;
    const run = on ? turnOff : turnOn;
    const control = h('button', {
      class: `egress-switch ${on ? 'on' : ''}`.trim(), type: 'button',
      role: 'switch', 'aria-checked': on ? 'true' : 'false',
      'aria-label': 'Stable outbound IPs',
      disabled: action.offered ? null : true,
      onclick: (event) => runAction(event.currentTarget, () => run()),
    },
    h('span', {class: 'egress-track'}, h('span', {class: 'egress-knob'})),
    h('span', {class: 'egress-state', text: on ? 'On' : 'Off'}));
    // Enable stays offered while the fleet is on but not converged — that
    // re-run IS v1's retry path after a partial failure, and the switch is
    // already on, so it needs a control of its own.
    const converge = on && enable.offered
      ? h('button', {class: 'fleet-link undo', type: 'button',
        onclick: (event) => runAction(event.currentTarget, () => turnOn())},
      'Finish enabling')
      : null;
    return h('div', {class: 'fleet-controls'},
      h('div', {class: 'fleet-control'},
        h('span', {class: 'fleet-label', text: 'Stable outbound IPs'}),
        h('div', {class: 'egress-switch-row'}, control, converge),
        h('span', {class: 'fleet-floor',
          text: action.offered
            ? (on ? TURN_OFF_CONSEQUENCE : TURN_ON_CONSEQUENCE)
            : blockedText(action)})));
  }

  function roster() {
    const state = egress();
    const rows = [];
    for (const row of state.attached || []) {
      rows.push(memberRow({
        state: {tone: 'ok', label: 'Attached'},
        name: row.public_ip || row.allocation_id || row.instance,
        chips: [nodeName(row.instance), row.managed ? '' : 'not tagged'],
        note: row.managed ? ''
          : 'held by this node but not tagged for this portal — reported, never detached from here',
      }));
    }
    for (const row of state.reserved || []) {
      rows.push(memberRow({
        state: {label: 'Reserved'},
        name: row.public_ip || row.allocation_id || 'address',
        chips: ['unattached'],
        note: 'kept for re-enable — reattached to the next node that needs one',
      }));
    }
    for (const id of state.pending_nodes || []) {
      rows.push(memberRow({
        state: {tone: 'warn', label: 'Pending'},
        name: nodeName(id),
        chips: ['no stable address'],
        note: 'its outbound calls still leave from an auto-assigned address',
        modifier: 'pending',
      }));
    }
    return rows;
  }

  function build() {
    const state = egress();
    const running = Boolean(watcher);

    // A failed read is unknown, never an empty allowlist and never "off".
    if (!state.available || state.policy_available === false) {
      const reason = state.fleet_available === false ? BLOCKED_COPY.fleet_unavailable
        : state.addresses_available === false ? BLOCKED_COPY.addresses_unavailable
          : BLOCKED_COPY.policy_unavailable;
      return resourcePanel({
        title: 'Outbound addresses',
        purpose: 'The addresses this fleet\'s outbound calls come from — the '
          + 'ones you give providers to allowlist.',
        chip: stateBadge({tone: 'warn', label: 'Unknown'}),
        callout: h('div', {class: 'callout warning'}, icon('alert'),
          h('p', {text: `${reason}. Until it can be read, this is unknown — `
            + 'not off, and not an empty allowlist.'})),
      });
    }

    // Balancer-less install: no fleet for the switch to manage, but a node
    // holding an address is still the answer an operator came for. Read-only.
    const fallback = state.fallback_attached || [];
    const fleetless = !(state.attached || []).length
      && !(state.pending_nodes || []).length;
    if (fleetless && fallback.length) {
      const ips = [...new Set(fallback.map((row) => row.public_ip).filter(Boolean))];
      return resourcePanel({
        title: 'Outbound addresses',
        purpose: 'The addresses this installation\'s outbound calls come from '
          + '— the ones you give providers to allowlist.',
        chip: stateBadge({tone: 'ok', label: 'External'}),
        controls: allowlist(ips),
        callout: h('div', {class: 'callout'}, icon('activity'),
          h('p', {text: 'Managed outside this portal — no load balancer here, '
            + 'so there is nothing for this control to do.'})),
        members: fallback.map((row) => memberRow({
          state: {label: 'Attached'},
          name: row.public_ip || row.allocation_id || row.instance,
          chips: [row.instance_name || row.instance],
          note: 'stable address · attach or detach in the AWS console',
        })),
      });
    }

    const pending = (state.pending_nodes || []).length;
    const addresses = state.addresses || [];
    const chip = running
      ? stateBadge({tone: 'pend', label: 'Working', spinner: true})
      : state.enabled
        ? stateBadge({tone: pending ? 'warn' : 'ok',
          label: pending ? 'On (incomplete)' : 'On'})
        : stateBadge({label: 'Off'});

    return resourcePanel({
      title: 'Outbound addresses',
      purpose: 'The addresses this fleet\'s outbound calls come from — the '
        + 'ones you give providers to allowlist.',
      chip,
      controls: h('div', {},
        allowlist(addresses, {pending}),
        running ? progressControls()
          : managed() ? switchControls()
            : h('p', {class: 'fleet-blocked',
              text: BLOCKED_COPY.infrastructure_external}),
        outcomeLine()),
      members: roster(),
    });
  }

  const paint = () => { host.replaceChildren(build()); };
  mounted = {host, paint, reload};
  paint();
  return host;
}

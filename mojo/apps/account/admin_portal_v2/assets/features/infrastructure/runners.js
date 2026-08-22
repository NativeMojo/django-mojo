// Infrastructure ▸ Capacity ▸ Edge runners — the heartbeat roster.
//
// v1 kept this roster inside the Dashboard's EC2 drill-in
// (dashboard/inspectors.js openFleetInspector). It is evidence about the very
// nodes this tab lists, so here it sits with them, behind the same "▸"
// disclosure the technical notes use: the read is paid only when an operator
// actually asks for it, never on a Capacity load.
//
// Rules carried over from v1, none of them relaxed:
//   1. A runner proves itself with a heartbeat TIMESTAMP. Nothing here
//      synthesizes an up/down flag from how old that timestamp is.
//   2. An envelope with no data is UNAVAILABLE — never "no runners have
//      heartbeated". The section's own reason is printed instead.
//   3. The roster is offered only to a caller the platform-view tier would
//      accept; the same capability v1 gated the drill-in on.
//   4. A failed read renders as a failure with a Retry, in the slot alone —
//      the fleet panels above it were already proven.
import {api, formatDate, h} from '../../core.js';
import {loadInto} from '../../components/actions.js';

const FLEET_PATH = '/api/account/admin/platform?sections=fleet';

// ── the vocabulary ──────────────────────────────────────────────────────────
//
// Deliberate local copies of capacity.js's two view helpers rather than an
// import, for the reason egress.js states: capacity.js imports THIS module to
// place the panel, and a cycle between the two would be a worse trade than
// twenty lines of pure markup. The class names are the shared ones, so the
// rows are identical to the fleet's own.

function stateBadge({tone, label}) {
  const modifier = {ok: 'success', warn: 'warning', pend: 'accent', danger: 'danger'}[tone] || '';
  return h('span', {class: `badge ${modifier}`.trim()}, h('span', {text: label}));
}

function memberRow({state, name, chips = [], note = ''}) {
  return h('div', {class: 'member'},
    stateBadge(state),
    h('span', {class: 'name mono', text: name}),
    ...chips.filter(Boolean).map((text) => h('span', {class: 'chip', text})),
    note ? h('span', {class: 'member-note', text: note}) : null);
}

// v1's plainReason: the collector's own word for why a section carries no
// evidence, with the underscores taken out.
function plainReason(section) {
  const reason = section?.reason || section?.status || '';
  return String(reason).replaceAll('_', ' ');
}

// One row per runner. The badge says what was reported, not what this page
// concluded: a runner with a heartbeat time is "Reported", a runner registered
// without one is a gap in the evidence and says so.
function runnerRow(row) {
  const beat = row.last_heartbeat ? formatDate(row.last_heartbeat) : '';
  return memberRow({
    state: beat ? {label: 'Reported'} : {tone: 'warn', label: 'No timestamp'},
    name: row.runner || 'unknown',
    chips: (row.channels || []).slice(0, 6),
    note: beat
      ? `last heartbeat ${beat}`
      : 'registered on this channel, but reported no heartbeat time',
  });
}

function rosterView(section) {
  const data = section?.data || {};
  // An unauthorized/timeout envelope carries no data — absent evidence is
  // "unavailable", never "no runners have heartbeated". v1's two lines.
  if (!Object.keys(data).length) {
    return [
      h('p', {class: 'fleet-runners-line', text: 'Edge runners unavailable'}),
      h('p', {class: 'fleet-runners-line', text: plainReason(section) || 'no evidence'}),
    ];
  }
  const runners = data.runners || [];
  const channel = data.channel || 'edge';
  return [
    h('p', {class: 'fleet-runners-line',
      text: `Channel ${channel} · ${runners.length} runner${runners.length === 1 ? '' : 's'} reporting`
        + (section?.observed_at ? ` · observed ${formatDate(section.observed_at)}` : '')}),
    runners.length
      ? h('div', {class: 'fleet-members'}, ...runners.map(runnerRow))
      // v1's sentence for a section that answered with an empty roster.
      : h('p', {class: 'fleet-runners-line',
        text: 'No edge runner has sent a heartbeat on this channel.'}),
    data.truncated
      ? h('p', {class: 'fleet-runners-line',
        text: 'The provider truncated this list — more runners exist than are shown.'})
      : null,
  ].filter(Boolean);
}

/**
 * The collapsed roster, or null for a caller who may not read platform
 * evidence — v1 offered this drill-in to nobody else either.
 *
 * The node is created once and re-used across the tab's re-renders, so the
 * disclosure keeps whatever the operator opened and whatever it loaded.
 */
export function runnersPanel(ctx, signal = null) {
  if (ctx?.features?.platform?.capabilities?.view !== true) return null;
  const body = h('div', {class: 'fleet-runners-body'});
  const root = h('details', {class: 'disclosure fleet-runners'},
    h('summary', {text: 'Edge runners'}),
    body);

  function load() {
    return loadInto(body, async (current) => {
      const report = await api(FLEET_PATH, {signal});
      if (current()) body.replaceChildren(...rosterView(report?.sections?.fleet));
    }, {message: 'Loading edge runners…', retry: () => load()});
  }

  // Every open is a fresh read: heartbeats age, and a roster kept from ten
  // minutes ago would be read as current. loadInto's generation counter drops
  // whichever read a newer open superseded.
  root.addEventListener('toggle', () => { if (root.open) load(); });
  return root;
}

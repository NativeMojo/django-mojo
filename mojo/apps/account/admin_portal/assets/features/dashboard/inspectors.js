// Drill-ins for the Dashboard rows. A row is one sentence; everything an
// operator would previously have opened the Platform health grid for lives
// one deliberate click behind a `Details` link, in a centered modal. Two of
// these drill-ins fetch a narrowed `?sections=` slice on open — that work is
// paid only when someone actually asks for it, never on every Dashboard load.
import {api, formatDate, h} from '../../core.js';
import {loadInto, runAction} from '../../components/actions.js';
import {openModal} from '../../components/overlays.js';
import {capacityPanel} from '../platform/capacity.js';

const FLEET_PATH = '/api/account/admin/platform?sections=fleet';
const SECURITY_PATH = '/api/account/admin/platform?sections=security';

// statusRow renders its action as a .row-link anchor; wire the handler onto it
// (same pattern as the Deployments lane).
export function wireAction(row, run) {
  const link = row.querySelector('.row-link');
  if (link) {
    link.addEventListener('click', (event) => {
      event.preventDefault();
      runAction(event.currentTarget, () => run(), {announceLabel: `Opening ${link.textContent.trim()}…`});
    });
  }
  return row;
}

export function detailLink(label, run) {
  const link = h('a', {class: 'row-link', href: '#'}, label);
  link.addEventListener('click', (event) => {
    event.preventDefault();
    runAction(event.currentTarget, () => run(), {announceLabel: `Opening ${label}…`});
  });
  return link;
}

// The exact collector payload, kept behind a disclosure. Nothing here is
// summarized, so a reader who needs the raw evidence still gets it.
export function technicalDetails(data) {
  return h('details', {class: 'dash-technical'},
    h('summary', {text: 'Technical details'}),
    h('pre', {class: 'evidence-json', text: JSON.stringify(data ?? {}, null, 2)}));
}

// rows are [label, value] or [label, value, mono]. A fact with no value is
// absent rather than blank — an empty row reads as evidence of nothing.
export function factList(rows) {
  const entries = (rows || []).filter((row) =>
    Array.isArray(row) && row[1] != null && row[1] !== '');
  if (!entries.length) return null;
  return h('dl', {class: 'details'}, ...entries.map(([label, value, mono]) =>
    h('div', {},
      h('dt', {text: label}),
      h('dd', {class: mono ? 'mono' : null, text: String(value)}))));
}

function observedLine(source) {
  return source?.observed_at
    ? h('p', {class: 'muted small', text: `Observed ${formatDate(source.observed_at)}`})
    : null;
}

function plainReason(source) {
  const reason = source?.reason || source?.status || '';
  return String(reason).replaceAll('_', ' ');
}

export function openSourceInspector({title, source, facts = [], extra = null, footer = null, wide = false}) {
  return openModal({title, wide, content: h('div', {class: 'dash-inspector'},
    observedLine(source), factList(facts), extra, footer,
    technicalDetails(source?.data))});
}

function instanceList(instances) {
  if (!instances.length) {
    return h('p', {class: 'muted small',
      text: 'No instance-level evidence — this reading is a running-instance count, not target-group health.'});
  }
  return h('ul', {class: 'dash-runners'}, ...instances.map((row) => h('li', {},
    h('span', {class: 'mono', text: row.id || 'unknown'}),
    h('span', {class: 'muted',
      text: ` ${row.name || row.id || ''} · ${row.healthy ? 'healthy' : 'not healthy'}`}))));
}

function runnerList(rows) {
  if (!rows.length) {
    return h('p', {class: 'muted small',
      text: 'No edge runner has sent a heartbeat on this channel.'});
  }
  return h('ul', {class: 'dash-runners'}, ...rows.map((row) => h('li', {},
    h('span', {class: 'mono', text: row.runner || 'unknown'}),
    h('span', {class: 'muted', text:
      ` ${(row.channels || []).join(', ') || 'no channels'} · last heartbeat ${formatDate(row.last_heartbeat)}`}))));
}

// The capacity panel is the one place in the Dashboard that CHANGES anything,
// so it loads last, only for callers whose grants the endpoint would also
// accept, and only when the drill-in is actually open. A caller without the
// capability sees this inspector exactly as it was before capacity existed.
// Only this block fails: the fleet evidence above it was already proven, and a
// capacity read that will not answer must not blank the drill-in. loadInto
// paints the loading state into the slot alone and renders the failure there
// with a retry — the older hand-rolled version had no retry at all.
function mountCapacity(ctx, slot) {
  if (ctx?.features?.platform?.capabilities?.capacity !== true) return undefined;
  return loadInto(slot, async (current) => {
    const panel = await capacityPanel(ctx);
    if (current()) slot.replaceChildren(panel);
  }, {message: 'Loading capacity…'});
}

// The EC2 row's own evidence renders immediately; the edge runner roster is a
// second, permissioned read that only platform viewers may make. The caller
// gates the drill-in too — this guard is the one that has to hold.
export function openFleetInspector(ctx, computeSource) {
  const data = computeSource?.data || {};
  const slot = h('div', {class: 'dash-slot'});
  const capacity = h('div', {class: 'dash-slot'});
  const inspector = openSourceInspector({
    title: 'EC2 · instances and edge runners', wide: true,
    source: computeSource,
    facts: [
      ['Evidence', data.source === 'target_group'
        ? 'Load balancer target group' : 'Running EC2 instances'],
      ['Instances', `${data.up || 0} of ${data.total || 0} healthy`],
    ],
    extra: h('div', {class: 'dash-block'},
      instanceList(data.instances || []), slot, capacity),
  });
  if (ctx?.features?.platform?.capabilities?.view !== true) {
    mountCapacity(ctx, capacity);
    return inspector;
  }
  // Only this block fails: the instance evidence above it was already proven,
  // so the loading state and the failure both go into the slot, not over the
  // inspector.
  loadInto(slot, async (current) => {
    const report = await api(FLEET_PATH);
    if (!current()) return;
    const section = report?.sections?.fleet || {};
    if (!Object.keys(section.data || {}).length) {
      // An unauthorized/timeout envelope carries no data — absent evidence is
      // "unavailable", never "no runners have heartbeated".
      slot.replaceChildren(
        h('p', {class: 'muted small', text: 'Edge runners unavailable'}),
        h('p', {class: 'muted small', text: plainReason(section) || 'no evidence'}));
      return;
    }
    slot.replaceChildren(
      h('h3', {class: 'dash-subhead', text: `Edge runners · ${section.data?.channel || 'edge'}`}),
      runnerList(section.data?.runners || []));
  }, {message: 'Loading edge runners…'}).finally(() => { mountCapacity(ctx, capacity); });
  return inspector;
}

function sanityChecks(sanitySource) {
  const checks = sanitySource?.data?.checks;
  if (!Array.isArray(checks) || !checks.length) {
    return h('div', {class: 'dash-block'},
      h('p', {class: 'muted small', text: 'Sanity checks unavailable'}),
      h('p', {class: 'muted small', text: plainReason(sanitySource) || 'no evidence'}));
  }
  return h('div', {class: 'dash-block'},
    h('h3', {class: 'dash-subhead', text: 'Node checks'}),
    h('ul', {class: 'dash-runners'}, ...checks.map((row) => h('li', {},
      h('span', {class: `status-dot ${row.ok ? 'success' : 'warning'}`}),
      h('span', {text: row.name || 'check'}),
      h('span', {class: 'muted', text: row.ok ? ' passed' : ' failed'})))));
}

export function openApiInspector(apiSource, sanitySource) {
  const data = apiSource?.data || {};
  return openModal({title: 'Public API', content: h('div', {class: 'dash-inspector'},
    observedLine(apiSource),
    factList([
      ['Public origin', data.public_origin],
      ['The public address reports', data.probe?.version, true],
      ['This node reports', data.node_version, true],
    ]),
    sanityChecks(sanitySource),
    // Both envelopes: when the checks are missing, their envelope is the only
    // thing that says why.
    technicalDetails({api: apiSource || {}, sanity: sanitySource || {}}))});
}

function controlList(posture) {
  const controls = posture?.controls || {};
  const names = Object.keys(controls);
  if (!names.length) {
    return h('p', {class: 'muted small', text: 'No secure-posture evidence was returned.'});
  }
  return h('ul', {class: 'dash-runners'}, ...names.map((name) => h('li', {},
    h('span', {class: `status-dot ${controls[name] ? 'success' : 'warning'}`}),
    h('span', {text: name.replaceAll('_', ' ')}),
    h('span', {class: 'muted', text: controls[name] ? ' enabled' : ' disabled'}))));
}

function securityView(section) {
  const data = section?.data || {};
  // An envelope that carries no evidence says so. Rendering "No heartbeat
  // recorded" from an empty payload would report absent evidence as a finding.
  if (!Object.keys(data).length) {
    return [
      h('p', {class: 'muted small', text: 'Security posture unavailable'}),
      h('p', {class: 'muted small', text: plainReason(section) || 'no evidence'}),
    ];
  }
  const beat = data.cron_heartbeat || {};
  const delivery = data.monitoring_delivery || {};
  const disabled = data.secure_posture?.disabled || [];
  return [
    observedLine(section),
    factList([
      ['Open incidents', data.open_incidents?.count],
      ['Cron heartbeat', beat.present
        ? `${beat.state || 'unknown'} · ${beat.age_seconds == null ? 'age unknown' : `${beat.age_seconds}s ago`}`
        : 'No heartbeat recorded'],
      ['Monitoring delivery', delivery.present
        ? `${delivery.status || 'unknown'} · ${formatDate(delivery.observed_at)}`
        : 'No delivery proof recorded'],
      ['Disabled controls', disabled.length
        ? disabled.map((value) => value.replaceAll('_', ' ')).join(', ') : 'None'],
    ]),
    h('div', {class: 'dash-block'},
      h('h3', {class: 'dash-subhead', text: 'Secure posture'}),
      controlList(data.secure_posture)),
    technicalDetails(data),
  ].filter(Boolean);
}

// Security evidence belongs to its own permission tier, so it is never part
// of the Dashboard payload. The caller offers this drill-in only to callers
// the platform-security tier would accept.
export function openSecurityInspector(ctx) {
  const slot = h('div', {class: 'dash-block'});
  const inspector = openModal({title: 'Security posture',
    content: h('div', {class: 'dash-inspector'}, slot)});
  if (ctx?.features?.platform?.capabilities?.security !== true) {
    slot.replaceChildren(h('p', {class: 'muted small',
      text: 'Your role cannot read platform security evidence.'}));
    return inspector;
  }
  loadInto(slot, async (current) => {
    const report = await api(SECURITY_PATH);
    if (current()) slot.replaceChildren(...securityView(report?.sections?.security));
  }, {message: 'Loading security posture…'});
  return inspector;
}

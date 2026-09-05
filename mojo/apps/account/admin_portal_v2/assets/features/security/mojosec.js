import {badge, formatDate, h, statusTone, TableView} from '../../core.js';
import {runAction} from '../../components/actions.js';
import {openModal} from '../../components/overlays.js';
import {readSecurityDetail, sectionRows} from './api.js';

function envelopeNotice(envelope, emptyCopy) {
  if (envelope.status === 'unavailable' || envelope.status === 'failed') {
    return h('div', {class: 'security-state unavailable', role: 'status'},
      h('strong', {text: 'Unavailable'}),
      h('p', {text: 'The server could not produce authoritative security evidence. No healthy or zero state is inferred.'}));
  }
  if (envelope.status === 'partial' || envelope.status === 'stale') {
    return h('div', {class: 'security-state warning', role: 'status'},
      h('strong', {text: envelope.status === 'stale' ? 'Evidence is stale' : 'Evidence is partial'}),
      h('p', {text: `This view is bound to the server cutoff ${formatDate(envelope.cutoff)}.`}));
  }
  if (Array.isArray(envelope.data) && envelope.data.length === 0) {
    return h('div', {class: 'security-state empty', role: 'status'},
      h('strong', {text: emptyCopy}), h('p', {text: `Observed through ${formatDate(envelope.cutoff)}.`}));
  }
  return null;
}

function metric(label, value, accuracy) {
  const shown = value == null ? 'Unavailable' : String(value);
  return h('article', {class: 'security-metric'},
    h('span', {text: label}), h('strong', {text: shown}),
    badge(accuracy || 'unavailable', accuracy === 'exact' ? 'success'
      : accuracy === 'sampled' ? 'warning' : 'neutral'));
}

export function renderOverview({report}) {
  const envelope = report.sections.overview;
  const unavailable = envelopeNotice(envelope, 'No current security findings');
  if (unavailable && ['unavailable', 'failed'].includes(envelope.status)) return unavailable;
  const data = envelope.data && typeof envelope.data === 'object' ? envelope.data : {};
  const current = data.current && typeof data.current === 'object' ? data.current : {};
  const definitions = data.metric_definitions && typeof data.metric_definitions === 'object'
    ? data.metric_definitions : {};
  const accuracy = (name) => definitions[name]?.accuracy || 'unavailable';
  const transitions = data.recommendation_transitions && typeof data.recommendation_transitions === 'object'
    ? Object.entries(data.recommendation_transitions) : [];
  return h('div', {class: 'security-stack'}, unavailable,
    h('div', {class: 'security-metrics'},
      metric('Open incidents', current.open_incidents, accuracy('open_incidents')),
      metric('Active rule sets', current.active_rule_sets, accuracy('active_rule_sets')),
      metric('Pending recommendations', current.pending_recommendations, accuracy('pending_recommendations')),
      metric('Resolution rate', null, accuracy('resolution_rate'))),
    h('section', {class: 'panel security-panel'},
      h('header', {}, h('h2', {text: 'Recommendation transitions'}),
        h('p', {text: `Exact append-only transitions from ${formatDate(envelope.window.start)} through ${formatDate(envelope.cutoff)}.`})),
      transitions.length ? h('dl', {class: 'security-definition-list'},
        ...transitions.flatMap(([name, value]) => [h('dt', {text: name}), h('dd', {text: String(value)})]))
        : h('p', {class: 'muted', text: 'No transitions in this window.'})));
}

async function caseDetail(row) {
  const content = h('div', {class: 'security-stack'}, h('p', {text: 'Loading complete evidence…'}));
  openModal({title: `Case ${row.id}`, subtitle: row.family || row.sensor_kind || '',
    content, wide: true});
  try {
    row = await readSecurityDetail('cases', row.id);
  } catch (error) {
    content.replaceChildren(h('p', {class: 'form-message', text: error.message}));
    return;
  }
  const fields = [
    ['State', row.state], ['Urgency', row.urgency], ['Sensor', row.sensor_kind],
    ['Family', row.family], ['Resource', row.resource_id],
    ['Occurrences', row.occurrence_count], ['Receipts', row.receipt_count],
    ['Projected events', row.projected_event_count], ['Distinct', row.distinct_count],
    ['Samples', row.sample_count], ['Overflow', row.overflow_count],
    ['First seen', formatDate(row.first_seen)], ['Last seen', formatDate(row.last_seen)],
  ];
  content.replaceChildren(
      h('dl', {class: 'security-definition-list'}, ...fields.flatMap(([label, value]) => [
        h('dt', {text: label}), h('dd', {text: String(value ?? 'Unavailable')}),
      ])),
      h('section', {class: 'security-samples'},
        h('h3', {text: 'Complete retained evidence'}),
        h('pre', {class: 'security-evidence', text: JSON.stringify(row, null, 2)})));
}

export function renderCases({report, loadPage}) {
  const envelope = report.sections.cases;
  const notice = envelopeNotice(envelope, 'No cases in this window');
  if (notice && ['unavailable', 'failed'].includes(envelope.status)) return notice;
  const all = sectionRows(envelope);
  let page = 0; const size = 10;
  const body = h('div', {});
  const search = h('input', {type: 'search', placeholder: 'Filter cases', 'aria-label': 'Filter cases'});
  const paint = () => {
    const needle = search.value.trim().toLowerCase();
    const filtered = all.filter((row) => [row.family, row.sensor_kind, row.resource_id, row.state]
      .some((value) => String(value || '').toLowerCase().includes(needle)));
    const pages = Math.max(1, Math.ceil(filtered.length / size)); page = Math.min(page, pages - 1);
    const rows = filtered.slice(page * size, (page + 1) * size);
    const previous = h('button', {class: 'button ghost compact', type: 'button', disabled: page === 0 || null}, 'Previous');
    const next = h('button', {class: 'button ghost compact', type: 'button', disabled: page + 1 >= pages || null}, 'Next');
    previous.addEventListener('click', () => { page -= 1; paint(); });
    next.addEventListener('click', () => { page += 1; paint(); });
    body.replaceChildren(
      new TableView({rows, empty: 'No cases match this filter.', onSelect: caseDetail, columns: [
        {label: 'Case', render: (row) => row.family || `Case ${row.id}`},
        {label: 'State', render: (row) => badge(row.state || 'unknown', statusTone(row.state))},
        {label: 'Urgency', render: (row) => badge(row.urgency || 'unknown', statusTone(row.urgency))},
        {label: 'Occurrences', key: 'occurrence_count'},
        {label: 'Last seen', render: (row) => formatDate(row.last_seen)},
      ]}).render(),
      h('footer', {class: 'security-pager'}, h('span', {text: `${filtered.length} cases · page ${page + 1} of ${pages}`}), previous, next));
  };
  search.addEventListener('input', () => { page = 0; paint(); }); paint();
  const more = envelope.next_cursor
    ? h('button', {class: 'button ghost compact', type: 'button'}, 'Load more cases') : null;
  more?.addEventListener('click', () => runAction(
    more, () => loadPage('cases'), {pendingLabel: 'Loading…'}));
  return h('div', {class: 'security-stack'}, notice,
    h('div', {class: 'security-toolbar'}, search,
      h('span', {class: 'muted', text: `Sampled through ${formatDate(envelope.cutoff)}`}),
      more), body);
}

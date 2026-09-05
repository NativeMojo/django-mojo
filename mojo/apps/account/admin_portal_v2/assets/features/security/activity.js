import {badge, formatDate, h, statusTone, TableView} from '../../core.js';
import {openModal} from '../../components/overlays.js';
import {readSecurityDetail, sectionRows} from './api.js';

const TABS = ['incidents', 'events'];

async function showRecord(kind, row) {
  const content = h('div', {class: 'security-stack'}, h('p', {text: 'Loading complete evidence…'}));
  openModal({title: `${kind === 'incidents' ? 'Incident' : 'Event'} ${row.id}`,
    subtitle: row.category || '', content, wide: true});
  try {
    row = await readSecurityDetail(kind, row.id);
  } catch (error) {
    content.replaceChildren(h('p', {class: 'form-message', text: error.message}));
    return;
  }
  const names = kind === 'incidents'
    ? ['id', 'created', 'priority', 'state', 'status', 'scope', 'category', 'group_id',
      'rule_set_id', 'source_ip', 'hostname', 'model_name', 'model_id', 'title', 'details', 'metadata']
    : ['id', 'created', 'level', 'scope', 'category', 'country_code', 'group_id',
      'incident_id', 'source_ip', 'hostname', 'uid', 'model_name', 'model_id', 'title', 'details', 'metadata'];
  content.replaceChildren(h('dl', {class: 'security-definition-list'},
    ...names.filter((name) => row[name] != null).flatMap((name) => [
      h('dt', {text: name.replaceAll('_', ' ')}),
      h('dd', {class: ['details', 'metadata'].includes(name) ? 'security-evidence' : '',
        text: name === 'created' ? formatDate(row[name])
          : typeof row[name] === 'object' ? JSON.stringify(row[name], null, 2) : String(row[name])}),
    ])));
}

function table(kind, envelope) {
  if (['unavailable', 'failed'].includes(envelope.status)) {
    return h('div', {class: 'security-state unavailable', role: 'status'},
      h('strong', {text: `${kind === 'incidents' ? 'Incidents' : 'Events'} unavailable`}),
      h('p', {text: 'No empty or healthy state is inferred from unavailable evidence.'}));
  }
  const rows = sectionRows(envelope);
  const columns = kind === 'incidents' ? [
    {label: 'Incident', render: (row) => row.category || `Incident ${row.id}`},
    {label: 'Status', render: (row) => badge(row.status || row.state || 'unknown', statusTone(row.status || row.state))},
    {label: 'Priority', key: 'priority'},
    {label: 'Created', render: (row) => formatDate(row.created)},
  ] : [
    {label: 'Event', render: (row) => row.category || `Event ${row.id}`},
    {label: 'Level', render: (row) => badge(String(row.level ?? 'unknown'), Number(row.level) >= 8 ? 'danger' : 'neutral')},
    {label: 'Scope', key: 'scope'},
    {label: 'Created', render: (row) => formatDate(row.created)},
  ];
  return new TableView({rows, columns,
    empty: `No ${kind} were observed in this server window.`,
    onSelect: (row) => showRecord(kind, row)}).render();
}

export function renderIncidentEvents({report}) {
  let active = TABS[0]; const body = h('div', {});
  const buttons = TABS.map((kind) => {
    const button = h('button', {class: `button ghost compact${kind === active ? ' active' : ''}`, type: 'button'}, kind === 'incidents' ? 'Incidents' : 'Events');
    button.addEventListener('click', () => {
      active = kind; buttons.forEach((item) => item.classList.toggle('active', item === button));
      body.replaceChildren(table(active, report.sections[active]));
    });
    return button;
  });
  body.replaceChildren(table(active, report.sections[active]));
  return h('div', {class: 'security-stack'},
    h('div', {class: 'security-toolbar', role: 'tablist', 'aria-label': 'Security records'}, ...buttons), body);
}

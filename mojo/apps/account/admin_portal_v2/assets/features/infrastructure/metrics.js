// Infrastructure ▸ Metrics — CloudWatch time series for the EC2, RDS and
// ElastiCache resources this installation can see. Ported from v1's Metrics
// page; the chart is the same dependency-free SVG, and every offered option is
// still one the server would actually answer.
//
// One route-state change from v1: `tab` names the Infrastructure section here,
// so the charted resource type rides `kind` instead. Everything else — the
// metric, the focused resource — keeps its v1 key, and the whole selection is
// still linkable.
import {api, h, icon} from '../../core.js';
import {runAction} from '../../components/actions.js';
import {decodeRouteState, routeHref} from '../../components/routes.js';
import {rowSection, statusRow} from '../../components/rows.js';
import {degradedState, errorState, loadingState, permissionDeniedState} from '../../components/views.js';
import {lineChart} from './chart.js';

const RESOURCES_PATH = '/api/aws/cloudwatch/resources';
const FETCH_PATH = '/api/aws/cloudwatch/fetch';

// Mirrors CATEGORY_METRIC in mojo/helpers/aws/cloudwatch.py: an (account,
// category) pair the helper does not support is a 400, so it is never offered.
const CATEGORIES = {
  ec2: [
    {value: 'cpu', label: 'CPU utilization', unit: '%'},
    {value: 'net_in', label: 'Network in', unit: 'bytes'},
    {value: 'net_out', label: 'Network out', unit: 'bytes'},
    {value: 'disk_read', label: 'Disk read operations', unit: ''},
    {value: 'disk_write', label: 'Disk write operations', unit: ''},
    {value: 'status_check', label: 'Failed status checks', unit: ''},
    {value: 'memory', label: 'Memory used (requires CloudWatch Agent)', unit: '%'},
    {value: 'disk', label: 'Root disk used (requires CloudWatch Agent)', unit: '%'},
  ],
  rds: [
    {value: 'cpu', label: 'CPU utilization', unit: '%'},
    {value: 'conns', label: 'Database connections', unit: ''},
    {value: 'free_storage', label: 'Free storage', unit: 'bytes'},
    {value: 'free_memory', label: 'Freeable memory', unit: 'bytes'},
    {value: 'read_iops', label: 'Read IOPS', unit: ''},
    {value: 'write_iops', label: 'Write IOPS', unit: ''},
    {value: 'read_latency', label: 'Read latency', unit: 's'},
    {value: 'write_latency', label: 'Write latency', unit: 's'},
    {value: 'net_in', label: 'Network receive throughput', unit: 'bytes'},
    {value: 'net_out', label: 'Network transmit throughput', unit: 'bytes'},
  ],
  redis: [
    {value: 'cpu', label: 'CPU utilization', unit: '%'},
    {value: 'conns', label: 'Current connections', unit: ''},
    {value: 'cache_memory', label: 'Memory used for cache', unit: 'bytes'},
    {value: 'cache_hits', label: 'Cache hits', unit: ''},
    {value: 'cache_misses', label: 'Cache misses', unit: ''},
    {value: 'replication_lag', label: 'Replication lag', unit: 's'},
    {value: 'net_in', label: 'Network bytes in', unit: 'bytes'},
    {value: 'net_out', label: 'Network bytes out', unit: 'bytes'},
  ],
};

const ACCOUNTS = [
  {value: 'ec2', label: 'EC2 instances'},
  {value: 'rds', label: 'RDS databases'},
  {value: 'redis', label: 'ElastiCache clusters'},
];

// CloudWatch bills and thins by period, so the offered granularities are the
// ones that actually produce a readable series for the span.
const RANGES = {
  '1h': {label: 'Last hour', hours: 1, granularity: ['minutes'], preferred: 'minutes'},
  '6h': {label: 'Last 6 hours', hours: 6, granularity: ['minutes'], preferred: 'minutes'},
  '24h': {label: 'Last 24 hours', hours: 24, granularity: ['hours'], preferred: 'hours'},
  '48h': {label: 'Last 48 hours', hours: 48, granularity: ['hours'], preferred: 'hours'},
  '7d': {label: 'Last 7 days', hours: 168, granularity: ['hours', 'days'], preferred: 'days'},
  '30d': {label: 'Last 30 days', hours: 720, granularity: ['days', 'hours'], preferred: 'days'},
};

const GRANULARITY_LABELS = {minutes: 'Per minute', hours: 'Hourly', days: 'Daily'};

const STATS = [
  {value: 'avg', label: 'Average'},
  {value: 'max', label: 'Maximum'},
  {value: 'min', label: 'Minimum'},
  {value: 'sum', label: 'Sum'},
];

const DEGRADED_COPY = {
  credentials_unavailable: 'No AWS credentials are configured for this installation.',
  denied: 'The configured AWS identity was refused — check its IAM policy or whether its credentials are current.',
  network_unavailable: 'AWS did not answer. Try again shortly.',
  service_error: 'AWS returned an error; details are in the server log.',
};

const SERVICE_LABELS = {ec2: 'EC2', rds: 'RDS', redis: 'ElastiCache'};

const DEFAULTS = {account: 'ec2', category: 'cpu', range: '24h',
  granularity: 'hours', stat: 'avg'};

function degradedCopy(reason) {
  return DEGRADED_COPY[reason] || DEGRADED_COPY.service_error;
}

function categoriesFor(account) {
  return CATEGORIES[account] || CATEGORIES.ec2;
}

function categoryEntry(account, category) {
  const options = categoriesFor(account);
  return options.find((entry) => entry.value === category) || options[0];
}

function resourceTone(row) {
  const state = String(row.state || row.status || '').toLowerCase();
  if (['running', 'available'].includes(state)) return 'ok';
  if (['stopping', 'stopped', 'rebooting', 'modifying', 'creating',
    'deleting', 'pending'].includes(state)) return 'warn';
  if (!state) return 'muted';
  return 'danger';
}

// Deliberately NOT wrapped in runAction. Every one of these selects reloads the
// chart, and loadSeries already supersedes correctly — it aborts the previous
// request and drops a stale answer. A re-entry guard here would do the opposite:
// the second change would return the first's promise, the chart would keep the
// old selection, and the control and the screen would disagree. The affordance
// is loadSeries' own skeleton, painted into chartSlot before the await, which
// carries role="status" so it is announced.
function selectControl(label, options, value, onChange) {
  const select = h('select', {'aria-label': label, onchange: (event) => onChange(event.target.value)},
    ...options.map((option) => h('option', {value: option.value, text: option.label})));
  select.value = value;
  return h('label', {class: 'toolbar-select'}, h('span', {class: 'sr-only', text: label}), select);
}

export async function metricsTab(ctx, signal = null, actions = null) {
  // The lane, not the raw grant: the bootstrap decides what this feature may
  // show, and every other Infrastructure tab reads its capabilities the same
  // way.
  if (!ctx.features?.platform?.capabilities?.metrics) {
    return permissionDeniedState(
      'Infrastructure metrics require the manage_aws permission.');
  }

  const route = decodeRouteState();
  const state = {
    account: CATEGORIES[route.state.kind] ? route.state.kind : DEFAULTS.account,
    category: DEFAULTS.category,
    range: DEFAULTS.range,
    granularity: DEFAULTS.granularity,
    stat: DEFAULTS.stat,
    focus: route.state.focus || '',
  };
  state.category = categoryEntry(state.account, route.state.category).value;

  const selected = new Map();
  const resources = {};
  let controller = null;
  let chart = null;

  const chartSlot = h('div', {class: 'metrics-chart-slot'});
  const controls = h('div', {class: 'metrics-controls'});
  const listSlot = h('div', {class: 'metrics-resources'});
  const body = h('div', {class: 'metrics-body'}, loadingState('Loading resources…'));
  const root = h('div', {class: 'metrics-page'}, body);

  function disposeChart() {
    chart?.dispose?.();
    chart = null;
  }

  function rememberRoute() {
    history.replaceState({}, '', routeHref('infrastructure', {
      tab: 'metrics', kind: state.account, category: state.category,
      focus: state.focus || '',
    }));
  }

  function selectionFor(account) {
    if (!selected.has(account)) selected.set(account, new Set());
    return selected.get(account);
  }

  function chosenSlugs() {
    const rows = resources[state.account] || [];
    const picks = selectionFor(state.account);
    return rows.filter((row) => picks.has(row.slug)).map((row) => row.slug);
  }

  function resourceDetail(row) {
    return [row.instance_type, row.engine, row.node_type, row.instance_class]
      .filter(Boolean).join(' · ');
  }

  function resourceRow(row) {
    const picks = selectionFor(state.account);
    const box = h('input', {
      type: 'checkbox', class: 'metrics-pick', 'aria-label': `Chart ${row.slug}`,
      checked: picks.has(row.slug),
      // Unguarded for the same reason as selectControl: ticking a second box
      // while the first read is in flight has to start a new read, and
      // loadSeries aborts the old one. chartSlot's skeleton is the affordance.
      onchange: (event) => {
        if (event.target.checked) picks.add(row.slug); else picks.delete(row.slug);
        state.focus = picks.size === 1 ? [...picks][0] : '';
        rememberRoute();
        return loadSeries();
      },
    });
    return statusRow({
      tone: resourceTone(row), name: row.slug || row.id,
      valueNode: box, detail: resourceDetail(row),
    });
  }

  function renderResources() {
    const sections = ACCOUNTS.map(({value, label}) => {
      const rows = resources[value] || [];
      if (!rows.length) return null;
      // Only the charted resource type carries checkboxes. The others stay
      // visible but quiet — switching Resource type is what activates them.
      return rowSection(label, rows.map((row) => value === state.account
        ? resourceRow(row)
        : statusRow({tone: resourceTone(row), name: row.slug || row.id,
          detail: resourceDetail(row)})));
    }).filter(Boolean);
    listSlot.replaceChildren(...(sections.length
      ? sections
      : [h('p', {class: 'metrics-note', text: 'No EC2, RDS, or ElastiCache resources are visible to these credentials.'})]));
  }

  function renderControls() {
    const range = RANGES[state.range] || RANGES[DEFAULTS.range];
    if (!range.granularity.includes(state.granularity)) state.granularity = range.preferred;
    controls.replaceChildren(
      selectControl('Resource type', ACCOUNTS, state.account, (value) => {
        state.account = value;
        state.category = categoryEntry(value, state.category).value;
        state.focus = '';
        rememberRoute();
        renderControls();
        renderResources();
        loadSeries();
      }),
      selectControl('Metric', categoriesFor(state.account), state.category, (value) => {
        state.category = value;
        rememberRoute();
        loadSeries();
      }),
      selectControl('Time range',
        Object.entries(RANGES).map(([value, entry]) => ({value, label: entry.label})),
        state.range, (value) => {
          state.range = value;
          state.granularity = RANGES[value].preferred;
          renderControls();
          loadSeries();
        }),
      selectControl('Granularity',
        range.granularity.map((value) => ({value, label: GRANULARITY_LABELS[value]})),
        state.granularity, (value) => { state.granularity = value; loadSeries(); }),
      selectControl('Statistic', STATS, state.stat, (value) => {
        state.stat = value;
        loadSeries();
      }));
  }

  async function loadSeries() {
    disposeChart();
    chartSlot.replaceChildren(loadingState('Loading metric…'));
    const range = RANGES[state.range] || RANGES[DEFAULTS.range];
    const end = new Date();
    const start = new Date(end.getTime() - range.hours * 3600000);
    const params = new URLSearchParams({
      account: state.account, category: state.category,
      dt_start: start.toISOString(), dt_end: end.toISOString(),
      granularity: state.granularity, stat: state.stat,
    });
    const slugs = chosenSlugs();
    if (slugs.length) params.set('slugs', slugs.join(','));
    controller?.abort();
    controller = new AbortController();
    const local = controller;
    try {
      const payload = await api(`${FETCH_PATH}?${params.toString()}`, {signal: local.signal});
      if (local !== controller) return;
      if (payload.available === false) {
        chartSlot.replaceChildren(degradedState(
          'Metrics are unavailable', degradedCopy(payload.reason)));
        return;
      }
      const entry = categoryEntry(state.account, state.category);
      const series = Object.entries(payload.data || {})
        .map(([name, values]) => ({name, values: Array.isArray(values) ? values : []}));
      if (!series.length) {
        chartSlot.replaceChildren(h('p', {class: 'metrics-note',
          text: 'No resources are selected for this metric.'}));
        return;
      }
      chart = lineChart({
        labels: payload.labels || [], series, unit: entry.unit,
        stat: (STATS.find((item) => item.value === state.stat) || STATS[0]).label,
        timeRange: {start, end},
        ariaLabel: `${entry.label} for ${series.length} resource${series.length === 1 ? '' : 's'}`,
      });
      chartSlot.replaceChildren(chart.node);
    } catch (error) {
      if (local !== controller || error?.name === 'AbortError') return;
      chartSlot.replaceChildren(errorState(error, () => loadSeries()));
    }
  }

  async function loadResources() {
    disposeChart();
    body.replaceChildren(loadingState('Loading resources…'));
    try {
      const payload = await api(RESOURCES_PATH, {signal});
      if (payload.available === false) {
        body.replaceChildren(degradedState(
          'AWS is not reporting', degradedCopy(payload.reason)));
        return;
      }
      ACCOUNTS.forEach(({value}) => { resources[value] = payload[value] || []; });
      const focus = state.focus;
      if (focus && (resources[state.account] || []).some((row) => row.slug === focus)) {
        selectionFor(state.account).add(focus);
      }
      const missing = Object.keys(payload.degraded || {});
      renderControls();
      renderResources();
      body.replaceChildren(...[
        controls,
        // A partial outage stays quiet: the services that answered still
        // render, and the ones that did not are named in one muted line.
        missing.length
          ? h('p', {class: 'metrics-note',
            text: `Unavailable right now: ${missing.map((name) => SERVICE_LABELS[name] || name).join(', ')}.`})
          : null,
        chartSlot,
        listSlot,
      ].filter(Boolean));
      await loadSeries();
    } catch (error) {
      if (error?.name === 'AbortError') return;
      body.replaceChildren(errorState(error, () => loadResources()));
    }
  }

  // The header sits outside `body`, so unlike everything else on this tab the
  // Refresh button survives its own reload and can carry the pending state.
  actions?.replaceChildren(h('button', {
    class: 'button ghost compact', type: 'button',
    onclick: (event) => runAction(event.currentTarget, () => loadResources(),
      {announceLabel: 'Refreshing resources…'}),
  }, icon('refresh'), 'Refresh'));

  await loadResources();
  root.dispose = () => { disposeChart(); controller?.abort(); };
  return root;
}

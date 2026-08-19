import {h} from '../../core.js';

// A small, dependency-free line chart. Every string on this page is written
// with textContent — series names come from EC2 Name tags, which an operator
// with tag rights controls, so nothing here may ever be parsed as markup.

const SVG_NS = 'http://www.w3.org/2000/svg';
const WIDTH = 720;
const HEIGHT = 260;
const PAD = {top: 14, right: 14, bottom: 30, left: 62};
const PLOT_W = WIDTH - PAD.left - PAD.right;
const PLOT_H = HEIGHT - PAD.top - PAD.bottom;
const GRIDLINES = 4;
const PALETTE = 6;
const TICKS = 6;
const HOUR = 3600000;

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs).forEach(([key, value]) => {
    if (value != null) node.setAttribute(key, String(value));
  });
  return node;
}

function svgText(value, attrs = {}) {
  const node = svgEl('text', attrs);
  node.textContent = value;
  return node;
}

// Round the axis top up to something a human reads as a boundary.
export function niceMax(value) {
  if (!Number.isFinite(value) || value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const scaled = value / magnitude;
  const step = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 2.5 ? 2.5
    : scaled <= 5 ? 5 : 10;
  return step * magnitude;
}

// "100%" beside "75.0%" reads as two precisions on one axis, so a fixed
// rounding is applied and then its trailing zeros are dropped.
function trim(text) {
  return text.includes('.') ? text.replace(/0+$/, '').replace(/\.$/, '') : text;
}

export function formatValue(value, unit = '') {
  if (!Number.isFinite(value)) return '—';
  const absolute = Math.abs(value);
  if (unit === 'bytes') {
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let scaled = absolute; let index = 0;
    while (scaled >= 1024 && index < units.length - 1) { scaled /= 1024; index += 1; }
    const signed = value < 0 ? -scaled : scaled;
    return `${trim(signed.toFixed(scaled >= 100 || !index ? 0 : 1))} ${units[index]}`;
  }
  let text;
  if (absolute >= 1000000) text = `${trim((value / 1000000).toFixed(1))}M`;
  else if (absolute >= 1000) text = `${trim((value / 1000).toFixed(1))}k`;
  else if (absolute >= 10 || absolute === 0) text = trim(value.toFixed(absolute >= 100 ? 0 : 1));
  else if (absolute >= 0.01) text = trim(value.toFixed(2));
  else text = value.toExponential(1);
  return unit ? `${text}${unit === '%' ? '' : ' '}${unit}` : text;
}

// The server's own labels are period-shaped ("14:00", "2026-08-12") and lose
// the day as soon as the range crosses midnight. The axis is drawn from the
// requested range instead, so a 7-day view reads as dates and an hour view
// reads as clock time.
export function axisFormatter(spanMs) {
  if (spanMs <= 6 * HOUR) {
    return (date) => date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  }
  if (spanMs <= 48 * HOUR) {
    return (date) => `${date.toLocaleDateString([], {month: 'short', day: 'numeric'})} `
      + date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  }
  return (date) => date.toLocaleDateString([], {month: 'short', day: 'numeric'});
}

function timeBounds(timeRange) {
  const start = timeRange?.start ? new Date(timeRange.start) : null;
  const end = timeRange?.end ? new Date(timeRange.end) : null;
  if (!start || !end || Number.isNaN(start.valueOf()) || Number.isNaN(end.valueOf())) return null;
  return end - start > 0 ? {start, span: end - start} : null;
}

function axisTicks(bounds, count) {
  if (!bounds) return [];
  const format = axisFormatter(bounds.span);
  return Array.from({length: count}, (_, index) => {
    const fraction = count === 1 ? 0 : index / (count - 1);
    return {fraction, label: format(new Date(bounds.start.getTime() + fraction * bounds.span))};
  });
}

// The hover readout is read against the axis, so it is derived from the same
// client-side range rather than the server's period label — a bucket label
// like "14:00" says nothing about which day it belongs to.
function readoutClock(bounds, buckets) {
  if (!bounds || buckets <= 1) return null;
  const interval = bounds.span / (buckets - 1);
  // A sub-day bucket needs the day named as well as the clock time; a daily
  // bucket has no meaningful time of day to show.
  const withTime = interval < 24 * HOUR;
  return (index) => {
    const at = new Date(bounds.start.getTime() + index * interval);
    const day = at.toLocaleDateString([], {month: 'short', day: 'numeric'});
    return withTime
      ? `${day} ${at.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}`
      : day;
  };
}

function pointX(index, length) {
  if (length <= 1) return PAD.left + PLOT_W / 2;
  return PAD.left + (index / (length - 1)) * PLOT_W;
}

function pointY(value, max) {
  const clamped = Math.max(0, Math.min(max, Number.isFinite(value) ? value : 0));
  return PAD.top + PLOT_H - (clamped / max) * PLOT_H;
}

export function lineChart({labels = [], series = [], unit = '', stat = '',
  timeRange = null, ariaLabel = 'Metric chart'} = {}) {
  const lines = (series || []).filter((entry) => Array.isArray(entry.values));
  const length = lines.reduce((most, entry) => Math.max(most, entry.values.length),
    labels.length || 0);
  const observed = lines.flatMap((entry) => entry.values)
    .filter((value) => Number.isFinite(value));
  const peak = observed.length ? Math.max(...observed) : 0;
  const allZero = !(peak > 0);
  const max = niceMax(peak);

  const svg = svgEl('svg', {
    viewBox: `0 0 ${WIDTH} ${HEIGHT}`, class: 'metrics-chart-svg',
    role: 'img', 'aria-label': ariaLabel,
  });

  for (let step = 0; step <= GRIDLINES; step += 1) {
    const value = (max / GRIDLINES) * (GRIDLINES - step);
    const y = PAD.top + (PLOT_H / GRIDLINES) * step;
    svg.append(svgEl('line', {
      class: 'metrics-gridline', x1: PAD.left, x2: PAD.left + PLOT_W, y1: y, y2: y,
    }));
    svg.append(svgText(formatValue(value, unit), {
      class: 'metrics-axis-label', x: PAD.left - 8, y: y + 4, 'text-anchor': 'end',
    }));
  }

  const bounds = timeBounds(timeRange);
  axisTicks(bounds, TICKS).forEach((tick, index, all) => {
    const x = PAD.left + tick.fraction * PLOT_W;
    svg.append(svgText(tick.label, {
      class: 'metrics-axis-label', x, y: HEIGHT - 10,
      'text-anchor': index === 0 ? 'start' : index === all.length - 1 ? 'end' : 'middle',
    }));
  });

  const guide = svgEl('line', {
    class: 'metrics-guide', x1: 0, x2: 0, y1: PAD.top, y2: PAD.top + PLOT_H,
    opacity: '0',
  });
  svg.append(guide);

  lines.forEach((entry, index) => {
    const values = entry.values;
    const points = Array.from({length: Math.max(length, 1)}, (_, position) =>
      `${pointX(position, Math.max(length, 1)).toFixed(2)},`
      + `${pointY(values[position] ?? 0, max).toFixed(2)}`).join(' ');
    svg.append(svgEl('polyline', {
      class: 'metrics-line', fill: 'none', points,
      stroke: `var(--chart-${(index % PALETTE) + 1})`,
    }));
  });

  const clock = readoutClock(bounds, length);
  const readout = h('div', {class: 'metrics-readout', role: 'status'});
  const legend = h('div', {class: 'metrics-legend'}, ...lines.map((entry, index) => h(
    'span', {class: 'metrics-legend-item'},
    h('span', {class: 'metrics-swatch', style: `background:var(--chart-${(index % PALETTE) + 1})`}),
    h('span', {class: 'metrics-legend-name mono', text: entry.name}),
    h('span', {
      class: 'metrics-legend-value',
      text: formatValue(entry.values[entry.values.length - 1], unit),
    }))));

  function indexFor(event) {
    const box = svg.getBoundingClientRect();
    if (!box.width || length <= 0) return -1;
    const relative = ((event.clientX - box.left) / box.width) * WIDTH;
    const fraction = (relative - PAD.left) / PLOT_W;
    if (fraction < -0.05 || fraction > 1.05) return -1;
    return Math.max(0, Math.min(length - 1, Math.round(fraction * (length - 1))));
  }

  function onMove(event) {
    const index = indexFor(event);
    if (index < 0) return onLeave();
    guide.setAttribute('opacity', '1');
    const x = pointX(index, length);
    guide.setAttribute('x1', String(x));
    guide.setAttribute('x2', String(x));
    readout.replaceChildren(
      h('span', {class: 'metrics-readout-at', text: clock ? clock(index) : (labels[index] ?? '')}),
      ...lines.map((entry, position) => h('span', {class: 'metrics-readout-item'},
        h('span', {
          class: 'metrics-swatch',
          style: `background:var(--chart-${(position % PALETTE) + 1})`,
        }),
        h('span', {class: 'mono', text: entry.name}),
        h('span', {text: formatValue(entry.values[index], unit)}))));
  }

  function onLeave() {
    guide.setAttribute('opacity', '0');
    readout.replaceChildren();
  }

  svg.addEventListener('pointermove', onMove);
  svg.addEventListener('pointerleave', onLeave);

  const node = h('div', {class: 'metrics-chart'},
    h('div', {class: 'metrics-chart-frame'}, svg),
    allZero
      ? h('p', {class: 'metrics-chart-note', text: 'No non-zero datapoints in this range'})
      : null,
    readout,
    legend,
    stat ? h('p', {class: 'metrics-chart-note', text: `Statistic: ${stat}`}) : null);

  return {
    node,
    dispose() {
      svg.removeEventListener('pointermove', onMove);
      svg.removeEventListener('pointerleave', onLeave);
    },
  };
}

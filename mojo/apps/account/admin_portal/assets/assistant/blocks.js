// Block renderers for the Assistant panel.
//
// Every renderer validates FIRST and returns null on a malformed block. The
// caller draws one muted line naming the type it could not read, so a bad
// block is visible rather than silently dropped -- an operator who is told
// nothing cannot tell "the assistant said nothing" from "the panel ate it".
//
// Bounds are not decoration. Blocks are model output: a table with 40,000 rows
// or a chart with 900 series is a hang, not a rendering problem.
//
// No innerHTML anywhere in this module.

import {h} from '../core.js';
import {renderMarkdown} from './markdown.js';
import {planTracker} from './plan.js';

const MAX_COLUMNS = 12;
const MAX_ROWS = 200;
const MAX_LABELS = 60;
const MAX_SERIES = 8;
const MAX_STATS = 12;
const MAX_LIST = 60;
const MAX_REFS = 40;
const COLOR_RE = /^#[0-9a-f]{3,8}$/i;
const ALERT_LEVELS = new Set(['info', 'success', 'warning', 'error']);
const CHART_TYPES = new Set(['line', 'bar', 'pie', 'area']);
const PALETTE = ['var(--chart-1)', 'var(--chart-2)', 'var(--chart-3)',
  'var(--chart-4)', 'var(--chart-5)', 'var(--chart-6)'];

function text(value) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'object') {
    try { return JSON.stringify(value); } catch (_) { return String(value); }
  }
  return String(value);
}

function strings(list, cap) {
  return list.slice(0, cap).map((entry) => text(entry));
}

function titled(title, ...children) {
  return h('div', {class: 'assistant-block'},
    title ? h('h5', {text: text(title)}) : null, ...children);
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  Object.entries(attrs).forEach(([key, value]) => {
    if (value != null) node.setAttribute(key, String(value));
  });
  return node;
}

function color(entry, index, colors) {
  const own = typeof entry?.color === 'string' && COLOR_RE.test(entry.color) ? entry.color : null;
  // A colour lands in an SVG attribute, so anything that is not a plain hex
  // literal is dropped in favour of the theme palette rather than escaped.
  const declared = Array.isArray(colors) && typeof colors[index] === 'string'
    && COLOR_RE.test(colors[index]) ? colors[index] : null;
  return own || declared || PALETTE[index % PALETTE.length];
}

function numeric(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

// --- table -----------------------------------------------------------------

function renderTable(block) {
  const columns = Array.isArray(block.columns) ? block.columns : null;
  const rows = Array.isArray(block.rows) ? block.rows : null;
  if (!columns || !columns.length || !rows) return null;
  const heads = strings(columns, MAX_COLUMNS);
  const body = rows.slice(0, MAX_ROWS).filter((row) => Array.isArray(row));
  const table = h('table', {},
    h('thead', {}, h('tr', {}, ...heads.map((label) => h('th', {scope: 'col', text: label})))),
    h('tbody', {}, ...body.map((row) => h('tr', {},
      ...heads.map((_, index) => h('td', {text: text(row[index])}))))));
  return titled(block.title,
    h('div', {class: 'table-wrap', tabindex: '0', role: 'region',
      'aria-label': `${text(block.title) || 'Result'} table`}, table),
    rows.length > MAX_ROWS
      ? h('div', {class: 'assistant-note',
        text: `Showing the first ${MAX_ROWS} of ${rows.length} rows.`}) : null);
}

// --- chart -----------------------------------------------------------------

function renderChart(block) {
  if (!CHART_TYPES.has(block.chart_type)) return null;
  const labels = Array.isArray(block.labels) ? block.labels : null;
  if (!labels || !labels.length || labels.length > MAX_LABELS) return null;
  const series = Array.isArray(block.series) ? block.series : null;
  if (!series || !series.length) return null;
  const usable = series.slice(0, MAX_SERIES).filter((entry) => entry && typeof entry === 'object'
    && typeof entry.name === 'string' && entry.name
    && Array.isArray(entry.values) && entry.values.length === labels.length);
  if (!usable.length) return null;

  const names = strings(labels, MAX_LABELS);
  const width = 320;
  const height = 150;
  const pad = {left: 8, right: 8, top: 8, bottom: 16};
  const canvas = svg('svg', {
    class: 'assistant-chart', viewBox: `0 0 ${width} ${height}`,
    role: 'img', 'aria-label': `${text(block.title) || 'Chart'}`,
  });
  const plotWidth = width - pad.left - pad.right;
  const plotHeight = height - pad.top - pad.bottom;

  if (block.chart_type === 'pie') {
    const values = usable[0].values.map(numeric).map((value) => (value && value > 0 ? value : 0));
    const total = values.reduce((sum, value) => sum + value, 0);
    if (!total) return null;
    let angle = -Math.PI / 2;
    const cx = width / 2;
    const cy = pad.top + plotHeight / 2;
    const radius = Math.min(plotHeight, plotWidth) / 2 - 2;
    values.forEach((value, index) => {
      if (!value) return;
      const sweep = (value / total) * Math.PI * 2;
      const x1 = cx + radius * Math.cos(angle);
      const y1 = cy + radius * Math.sin(angle);
      angle += sweep;
      const x2 = cx + radius * Math.cos(angle);
      const y2 = cy + radius * Math.sin(angle);
      canvas.append(svg('path', {
        d: `M ${cx} ${cy} L ${x1.toFixed(2)} ${y1.toFixed(2)} `
          + `A ${radius} ${radius} 0 ${sweep > Math.PI ? 1 : 0} 1 `
          + `${x2.toFixed(2)} ${y2.toFixed(2)} Z`,
        fill: color({}, index, block.colors),
      }));
    });
    return titled(block.title, canvas, h('div', {class: 'assistant-legend'},
      ...names.map((label, index) => h('span', {},
        svgSwatch(color({}, index, block.colors)), label))));
  }

  const all = usable.flatMap((entry) => entry.values.map(numeric)).filter((value) => value !== null);
  const max = all.length ? Math.max(...all, 0) : 0;
  const min = all.length ? Math.min(...all, 0) : 0;
  const span = max - min || 1;
  const y = (value) => pad.top + plotHeight - ((value - min) / span) * plotHeight;
  const x = (index) => pad.left + (names.length === 1 ? plotWidth / 2
    : (index / (names.length - 1)) * plotWidth);

  canvas.append(svg('line', {
    x1: pad.left, x2: width - pad.right, y1: y(min < 0 ? 0 : min), y2: y(min < 0 ? 0 : min),
    stroke: 'var(--line)', 'stroke-width': 1,
  }));

  if (block.chart_type === 'bar') {
    const groupWidth = plotWidth / names.length;
    const barWidth = Math.max(1, (groupWidth * 0.7) / usable.length);
    usable.forEach((entry, seriesIndex) => {
      entry.values.forEach((raw, index) => {
        const value = numeric(raw);
        if (value === null) return;
        const base = y(min < 0 ? 0 : min);
        const top = y(value);
        canvas.append(svg('rect', {
          x: pad.left + index * groupWidth + groupWidth * 0.15 + seriesIndex * barWidth,
          y: Math.min(base, top), width: barWidth, height: Math.max(1, Math.abs(base - top)),
          fill: color(entry, seriesIndex, block.colors), rx: 1,
        }));
      });
    });
  } else {
    usable.forEach((entry, seriesIndex) => {
      const stroke = color(entry, seriesIndex, block.colors);
      // A non-finite value is a GAP, never a zero: plotting a missing sample as
      // zero invents a crash in the data.
      let run = [];
      const flush = () => {
        if (run.length > 1) {
          canvas.append(svg('polyline', {
            points: run.join(' '), fill: 'none', stroke, 'stroke-width': 1.6,
            'stroke-linejoin': 'round', 'stroke-linecap': 'round',
          }));
        } else if (run.length === 1) {
          const [px, py] = run[0].split(',');
          canvas.append(svg('circle', {cx: px, cy: py, r: 1.8, fill: stroke}));
        }
        run = [];
      };
      entry.values.forEach((raw, index) => {
        const value = numeric(raw);
        if (value === null) { flush(); return; }
        run.push(`${x(index).toFixed(2)},${y(value).toFixed(2)}`);
      });
      flush();
    });
  }

  return titled(block.title, canvas, h('div', {class: 'assistant-legend'},
    ...usable.map((entry, index) => h('span', {},
      svgSwatch(color(entry, index, block.colors)), text(entry.name)))));
}

function svgSwatch(fill) {
  const node = h('i');
  node.style.background = fill;
  return node;
}

// --- the small ones --------------------------------------------------------

function pairs(items, cap) {
  return items.slice(0, cap).filter((item) => item && typeof item === 'object'
    && typeof item.label === 'string');
}

function renderStat(block) {
  const items = Array.isArray(block.items) ? pairs(block.items, MAX_STATS) : [];
  if (!items.length) return null;
  return titled(block.title, h('div', {class: 'assistant-stats'},
    ...items.map((item) => h('div', {},
      h('span', {text: item.label}), h('strong', {text: text(item.value)})))));
}

function renderList(block) {
  const items = Array.isArray(block.items) ? pairs(block.items, MAX_LIST) : [];
  if (!items.length) return null;
  return titled(block.title, h('dl', {class: 'assistant-kv'},
    ...items.flatMap((item) => [h('dt', {text: item.label}),
      h('dd', {text: text(item.value)})])));
}

function renderAlert(block) {
  if (!ALERT_LEVELS.has(block.level) || !block.message) return null;
  const severe = block.level === 'warning' || block.level === 'error';
  return h('div', {class: `assistant-alert is-${block.level}`,
    role: severe ? 'alert' : 'status'},
  block.title ? h('strong', {text: text(block.title)}) : null,
  h('span', {text: text(block.message)}));
}

function safeDownload(value) {
  // Absolute https only, and no embedded credentials. export_data legitimately
  // returns either an installation shortlink or a storage-backend URL, so a
  // same-origin rule would break real installations -- the destination host is
  // shown beside the filename instead.
  try {
    const url = new URL(String(value));
    if (url.protocol !== 'https:' || url.username || url.password) return null;
    return url;
  } catch (_) { return null; }
}

function renderFile(block) {
  if (!block.filename || !block.url) return null;
  const url = safeDownload(block.url);
  const facts = ['size', 'format', 'row_count', 'expires_in']
    .filter((key) => block[key] !== undefined && block[key] !== null)
    .map((key) => `${key.replace('_', ' ')}: ${text(block[key])}`);
  return h('div', {class: 'assistant-block'}, h('div', {class: 'assistant-file'},
    h('strong', {text: text(block.filename)}),
    url ? h('a', {href: url.href, target: '_blank', rel: 'noopener noreferrer',
      referrerpolicy: 'no-referrer', text: `Download from ${url.hostname}`})
      : h('span', {text: `Download link (copy it by hand): ${text(block.url)}`}),
    facts.length ? h('span', {text: facts.join(' · ')}) : null,
    url ? null : h('span', {text: 'The link was not an absolute https address, so it is not clickable here.'})));
}

function renderContext(block) {
  const refs = Array.isArray(block.references) ? block.references : [];
  const usable = refs.slice(0, MAX_REFS).filter((ref) => ref && typeof ref === 'object'
    && typeof ref.model_name === 'string' && ref.model_name && ref.pk !== undefined);
  if (!usable.length) return null;
  // Inert chips. There is no route mapping in v1: guessing an Admin route from
  // a model string produces links that 404.
  return h('div', {class: 'assistant-block'}, h('div', {class: 'assistant-chips'},
    ...usable.map((ref) => h('span', {class: 'assistant-chip'},
      h('strong', {text: text(ref.label) || `${text(ref.model_name)} #${text(ref.pk)}`}),
      h('span', {text: `${text(ref.app_name)}.${text(ref.model_name)} #${text(ref.pk)}`})))));
}

const RENDERERS = {
  table: renderTable,
  chart: renderChart,
  stat: renderStat,
  list: renderList,
  alert: renderAlert,
  file: renderFile,
  context: renderContext,
  // Accepted defensively with the plan tracker's own schema. Nothing produces
  // it today; the live tracker is fed by the WS plan events.
  progress: (block) => planTracker(block),
};

export function renderBlock(block, ctx = {}) {
  if (!block || typeof block !== 'object') return null;
  const renderer = RENDERERS[block.type];
  if (!renderer) return null;
  try {
    return renderer(block, ctx);
  } catch (_) {
    // One bad block must never take the thread down with it.
    return null;
  }
}

export function unreadableBlock(block) {
  const type = block && typeof block === 'object' ? text(block.type) : '';
  return h('div', {class: 'assistant-unreadable',
    text: `The assistant sent a ${type || 'data'} block this panel could not read.`});
}

export function renderBlocks(list, ctx = {}) {
  const blocks = Array.isArray(list) ? list : [];
  return blocks.map((block) => renderBlock(block, ctx) || unreadableBlock(block));
}

export {renderMarkdown};

// Field maps drawn from the CFD mesh and field JSON (assay2chip/cfd/README.md).
//
// The mesh gives every cell an x_mm/y_mm, so a map is just one coloured patch per cell
// plus one coloured line per duct edge. Drawing it here rather than relying on
// cfd/figures/*.svg means the maps still appear while figures.py is being written, and
// the colour scale can be shared across chips.
//
// Viridis is used because it stays readable in greyscale and for colour-blind readers.

import { svg, el, fmtNum, badge } from './dom.js';
import { simBadge, SIM_TITLE } from './labels.js';

const VIRIDIS = [
  [68, 1, 84], [59, 82, 139], [33, 145, 140], [94, 201, 98], [253, 231, 37],
];

function ramp(t) {
  const x = Math.max(0, Math.min(1, Number.isFinite(t) ? t : 0)) * (VIRIDIS.length - 1);
  const i = Math.min(VIRIDIS.length - 2, Math.floor(x));
  const f = x - i;
  const a = VIRIDIS[i];
  const b = VIRIDIS[i + 1];
  const c = [0, 1, 2].map((k) => Math.round(a[k] + (b[k] - a[k]) * f));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

export const METRICS = {
  pressure: { label: 'pressure', units: 'kPa', where: 'node', scale: 1 / 1000, keys: ['p_pa', 'p', 'pressure_pa'] },
  speed: { label: 'speed', units: 'mm/s', where: 'node', scale: 1, keys: ['u_mm_s', 'u', 'speed_mm_s', 'velocity_mm_s'] },
  shear: { label: 'wall shear', units: 'dyn/cm^2', where: 'either', scale: 1, keys: ['shear_dyn_cm2', 'tau_dyn_cm2', 'shear'] },
  residence: { label: 'residence time', units: 's', where: 'node', scale: 1, keys: ['residence_s', 'residence_time_s', 't_res_s', 'residence'] },
  re: { label: 'Reynolds number', units: '', where: 'edge', scale: 1, keys: ['re', 'reynolds'] },
};

export const METRIC_ORDER = ['pressure', 'speed', 'shear', 'residence'];

function container(field, keys) {
  if (!field || typeof field !== 'object') return null;
  for (const k of keys) if (field[k] !== undefined && field[k] !== null) return field[k];
  return null;
}

function magnitude(v) {
  if (Array.isArray(v)) return Math.hypot(...v.map(Number).filter(Number.isFinite));
  if (v && typeof v === 'object') {
    const parts = ['x', 'y', 'z'].map((k) => Number(v[k])).filter(Number.isFinite);
    if (parts.length) return Math.hypot(...parts);
    if (Number.isFinite(Number(v.magnitude))) return Number(v.magnitude);
    return NaN;
  }
  return Number(v);
}

/** Read a per-id value whether the container is keyed by id or indexed by position. */
function valueFor(cont, id, index) {
  if (!cont) return NaN;
  if (Array.isArray(cont)) return magnitude(cont[index]);
  if (typeof cont === 'object') {
    if (cont[id] !== undefined) return magnitude(cont[id]);
    if (cont[String(id)] !== undefined) return magnitude(cont[String(id)]);
    return NaN;
  }
  return NaN;
}

/**
 * Pull one metric out of the field (and optional transport) for a mesh.
 * Returns {where:'node'|'edge', values:Map, min, max, units, label, missing}.
 */
export function metricValues(mesh, field, metricName, extra) {
  const def = METRICS[metricName];
  if (!def) return { missing: true, reason: `unknown metric ${metricName}` };
  const nodes = Array.isArray(mesh && mesh.nodes) ? mesh.nodes : [];
  const edges = Array.isArray(mesh && mesh.edges) ? mesh.edges : [];
  const sources = [field, extra].filter(Boolean);

  let cont = null;
  for (const s of sources) { cont = container(s, def.keys); if (cont) break; }
  if (!cont) return { missing: true, reason: `${def.label} is not in the field file` };

  const tryWhere = def.where === 'either' ? ['edge', 'node'] : [def.where];
  for (const where of tryWhere) {
    const items = where === 'node' ? nodes : edges;
    const values = new Map();
    let min = Infinity;
    let max = -Infinity;
    items.forEach((it, i) => {
      const v = valueFor(cont, it.id, i) * def.scale;
      if (!Number.isFinite(v)) return;
      values.set(String(it.id), v);
      if (v < min) min = v;
      if (v > max) max = v;
    });
    if (values.size) return { where, values, min, max, units: def.units, label: def.label, missing: false };
  }
  return { missing: true, reason: `${def.label} has no values that match this mesh` };
}

function bounds(mesh) {
  let x0 = Infinity;
  let y0 = Infinity;
  let x1 = -Infinity;
  let y1 = -Infinity;
  for (const n of mesh.nodes || []) {
    const x = Number(n.x_mm);
    const y = Number(n.y_mm);
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
    x0 = Math.min(x0, x); x1 = Math.max(x1, x);
    y0 = Math.min(y0, y); y1 = Math.max(y1, y);
  }
  if (!Number.isFinite(x0)) return { x0: 0, y0: 0, x1: 30, y1: 20 };
  return { x0, y0, x1, y1 };
}

/**
 * Draw one field map.
 * @param {object} mesh  cfd/<chip>.mesh.json
 * @param {object} metric result of metricValues
 * @param {object} opts  {highlight:Set<feature_id>, footprint:{x,y}}
 */
export function fieldMap(mesh, metric, opts = {}) {
  const b = bounds(mesh);
  const fp = opts.footprint;
  const x0 = fp ? 0 : b.x0 - 1;
  const y0 = fp ? 0 : b.y0 - 1;
  const x1 = fp ? Number(fp.x) : b.x1 + 1;
  const y1 = fp ? Number(fp.y) : b.y1 + 1;
  const w = Math.max(1e-6, x1 - x0);
  const h = Math.max(1e-6, y1 - y0);
  const flip = (y) => y1 + y0 - y;   // spec y is up, SVG y is down
  const grid = Number((mesh.meta && mesh.meta.grid_mm) || 0.5) || 0.5;
  const span = metric.max - metric.min || 1;
  const t = (v) => (v - metric.min) / span;
  const highlight = opts.highlight instanceof Set ? opts.highlight : new Set();

  const root = svg('svg', {
    class: 'hm',
    viewBox: `${x0 - 0.5} ${y0 - 0.5} ${w + 1} ${h + 1}`,
    preserveAspectRatio: 'xMidYMid meet',
    role: 'img',
    'aria-label': `${metric.label} map, simulated, ${fmtNum(metric.min, 3)} to ${fmtNum(metric.max, 3)} ${metric.units}`,
  });

  if (fp) {
    root.appendChild(svg('rect', { x: 0, y: 0, width: Number(fp.x), height: Number(fp.y), fill: '#0a1120', stroke: 'rgba(148,178,255,0.4)', 'stroke-width': 0.08 }));
  }

  // edges first
  const gE = svg('g');
  const nodeById = new Map((mesh.nodes || []).map((n) => [String(n.id), n]));
  (mesh.edges || []).forEach((e) => {
    const a = nodeById.get(String(e.a));
    const c = nodeById.get(String(e.b));
    if (!a || !c) return;
    const isEdgeMetric = metric.where === 'edge';
    const v = isEdgeMetric ? metric.values.get(String(e.id)) : undefined;
    const stroke = isEdgeMetric
      ? (Number.isFinite(v) ? ramp(t(v)) : 'rgba(148,178,255,0.2)')
      : 'rgba(148,178,255,0.22)';
    const line = svg('line', {
      x1: Number(a.x_mm), y1: flip(Number(a.y_mm)), x2: Number(c.x_mm), y2: flip(Number(c.y_mm)),
      stroke, 'stroke-width': Math.max(Number(e.width_mm) || grid * 0.6, grid * 0.3), 'stroke-linecap': 'round',
    });
    if (isEdgeMetric && Number.isFinite(v)) {
      line.appendChild(svg('title', {}, `${e.id}${e.feature_id ? ` (${e.feature_id})` : ''}: ${fmtNum(v, 4)} ${metric.units} - SIMULATED`));
    }
    gE.appendChild(line);
  });
  root.appendChild(gE);

  // cells
  if (metric.where === 'node') {
    const gN = svg('g');
    for (const n of mesh.nodes || []) {
      const v = metric.values.get(String(n.id));
      if (!Number.isFinite(v)) continue;
      const x = Number(n.x_mm);
      const y = Number(n.y_mm);
      if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
      const side = Number(n.area_mm2) > 0 ? Math.sqrt(Number(n.area_mm2)) : grid;
      const kind = String(n.kind || 'cell');
      const node = kind === 'port'
        ? svg('circle', { cx: x, cy: flip(y), r: side * 0.6, fill: ramp(t(v)), stroke: '#e2e8f0', 'stroke-width': 0.05 })
        : svg('rect', { x: x - side / 2, y: flip(y) - side / 2, width: side, height: side, fill: ramp(t(v)), 'shape-rendering': 'crispEdges' });
      node.appendChild(svg('title', {}, `${n.id}${n.feature_id ? ` (${n.feature_id})` : ''}: ${fmtNum(v, 4)} ${metric.units} - SIMULATED`));
      gN.appendChild(node);
    }
    root.appendChild(gN);
  }

  // Features a finding points at get one dashed outline round the whole feature, not a
  // stroke on every cell - otherwise the outline hides the field it is pointing at.
  if (highlight.size) {
    const boxes = featureBoxes(mesh, highlight);
    const gH = svg('g', { class: 'hm-highlight' });
    for (const [feature, bx] of boxes) {
      const pad = grid * 0.7;
      gH.appendChild(svg('rect', {
        x: bx.x0 - pad, y: flip(bx.y1) - pad, width: bx.x1 - bx.x0 + pad * 2, height: bx.y1 - bx.y0 + pad * 2,
        fill: 'none', stroke: '#f87171', 'stroke-width': Math.max(0.1, grid * 0.14),
        'stroke-dasharray': `${grid * 0.8} ${grid * 0.5}`, rx: grid * 0.3,
      }, svg('title', {}, `${feature}: a CFD finding points here`)));
    }
    root.appendChild(gH);
  }

  return root;
}

/** Bounding box in mm of every highlighted feature. */
function featureBoxes(mesh, highlight) {
  const boxes = new Map();
  const add = (feature, x, y) => {
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    const b = boxes.get(feature) || { x0: Infinity, y0: Infinity, x1: -Infinity, y1: -Infinity };
    b.x0 = Math.min(b.x0, x); b.x1 = Math.max(b.x1, x);
    b.y0 = Math.min(b.y0, y); b.y1 = Math.max(b.y1, y);
    boxes.set(feature, b);
  };
  for (const n of mesh.nodes || []) {
    const f = String(n.feature_id ?? '');
    if (highlight.has(f) || highlight.has(String(n.id))) add(f || String(n.id), Number(n.x_mm), Number(n.y_mm));
  }
  const byId = new Map((mesh.nodes || []).map((n) => [String(n.id), n]));
  for (const e of mesh.edges || []) {
    const f = String(e.feature_id ?? '');
    if (!highlight.has(f) && !highlight.has(String(e.id))) continue;
    for (const end of [byId.get(String(e.a)), byId.get(String(e.b))]) {
      if (end) add(f || String(e.id), Number(end.x_mm), Number(end.y_mm));
    }
  }
  return boxes;
}

/** The colour ramp with min/max labels, the unit, and the SIMULATED label. */
export function fieldLegend(metric) {
  const stops = VIRIDIS.map((_, i) => ramp(i / (VIRIDIS.length - 1))).join(', ');
  return el('div', { class: 'hm-legend' },
    el('span', { class: 'num' }, `${fmtNum(metric.min, 3)}`),
    el('span', { class: 'hm-ramp', style: { background: `linear-gradient(90deg, ${stops})` }, title: 'viridis, low to high' }),
    el('span', { class: 'num' }, `${fmtNum(metric.max, 3)}`),
    el('span', {}, metric.units || 'dimensionless'),
    simBadge());
}

/** A whole map panel: title, map, legend, and the SIMULATED label. */
export function fieldPanel(title, mesh, metric, opts = {}) {
  if (!metric || metric.missing) {
    return el('div', { class: 'panel' },
      el('h3', {}, title, ' ', badge('no data', 'todo', metric && metric.reason)),
      el('p', { class: 'muted small' }, metric && metric.reason ? metric.reason : 'the field file does not carry this metric yet'));
  }
  const flagged = opts.highlight instanceof Set && opts.highlight.size;
  return el('div', { class: 'panel' },
    el('h3', {}, title, ' ', simBadge()),
    fieldMap(mesh, metric, opts),
    fieldLegend(metric),
    flagged
      ? el('p', { class: 'hm-legend' },
        el('span', {
          style: {
            display: 'inline-block', width: '18px', height: '0', borderTop: '2px dashed #f87171', marginRight: '6px',
          },
        }),
        `dashed outline: a finding points at this feature (${opts.highlight.size})`)
      : null,
    el('p', { class: 'hint', title: SIM_TITLE },
      `Depth-averaged Hele-Shaw solve. ${(mesh.nodes || []).length} cells, ${(mesh.edges || []).length} edges, `
      + `grid ${fmtNum((mesh.meta && mesh.meta.grid_mm) || 0, 3)} mm. Hele-Shaw has no recirculation, so a trap here `
      + 'means a low-velocity or long-residence zone, not a closed streamline.'));
}

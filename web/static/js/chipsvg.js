// Plan view of a chip spec as SVG, drawn in millimetres with a viewBox so it scales to
// any width. One fluidic layer at one etch depth, so plan view IS the design: a crossing
// here is a short circuit, which makes this the most useful single picture of a chip.

import { svg, el, fmtNum, pick } from './dom.js';

const CHAMBER_COLOURS = {
  exchange: '#2dd4bf',
  culture: '#4ade80',
  mix: '#7dd3fc',
  filter: '#a78bfa',
  split: '#fbbf24',
  pool: '#f472b6',
  collect: '#94a3b8',
  trap: '#fb923c',
  incubate: '#f59e0b',
  reservoir: '#60a5fa',
  waste: '#64748b',
};
const CHAMBER_FALLBACK = '#5b7bb5';

const PORT_COLOURS = { inlet: '#5eead4', outlet: '#f472b6', vent: '#e2e8f0', waste: '#94a3b8' };

function chamberColour(type) {
  const t = String(type || '').toLowerCase();
  if (CHAMBER_COLOURS[t]) return CHAMBER_COLOURS[t];
  for (const [k, v] of Object.entries(CHAMBER_COLOURS)) if (t.includes(k)) return v;
  return CHAMBER_FALLBACK;
}

function xy(v) {
  if (Array.isArray(v)) return [Number(v[0]), Number(v[1])];
  if (v && typeof v === 'object') return [Number(pick(v, 'x', 'x_mm')), Number(pick(v, 'y', 'y_mm'))];
  return [NaN, NaN];
}

function footprintOf(spec) {
  const fp = spec && (spec.footprint || spec.envelope);
  const x = Number(pick(fp || {}, 'x', 'x_mm') ?? (Array.isArray(fp) ? fp[0] : NaN));
  const y = Number(pick(fp || {}, 'y', 'y_mm') ?? (Array.isArray(fp) ? fp[1] : NaN));
  return { x: Number.isFinite(x) ? x : 30, y: Number.isFinite(y) ? y : 20, known: Number.isFinite(x) && Number.isFinite(y) };
}

/** Positions of everything that a channel can attach to, by id. */
function anchorMap(spec) {
  const map = new Map();
  for (const c of spec.chambers || []) {
    const [x, y] = xy(c.center || c.position);
    if (Number.isFinite(x)) map.set(String(c.id), [x, y]);
  }
  for (const p of spec.ports || []) {
    const [x, y] = xy(p.position || p.center);
    if (Number.isFinite(x)) map.set(String(p.id), [x, y]);
  }
  for (const f of spec.features || []) {
    const [x, y] = xy(f.center || f.position);
    if (Number.isFinite(x)) map.set(String(f.id), [x, y]);
  }
  return map;
}

/**
 * Build the plan view.
 * @param {object} spec chip spec JSON
 * @param {object} opts {onSelect(kind, obj), labels:boolean}
 */
export function chipPlan(spec, opts = {}) {
  const fp = footprintOf(spec);
  const pad = Math.max(1.5, Math.min(fp.x, fp.y) * 0.07);
  const W = fp.x + pad * 2;
  const H = fp.y + pad * 2;
  const flip = (y) => fp.y - y;           // spec y is up, SVG y is down
  const anchors = anchorMap(spec);
  const seen = new Set();

  const root = svg('svg', {
    class: 'chip-plan',
    viewBox: `${-pad} ${-pad} ${W} ${H}`,
    role: 'img',
    'aria-label': `Plan view of ${String(spec.id || 'chip')}, ${fmtNum(fp.x, 5)} by ${fmtNum(fp.y, 5)} millimetres`,
    preserveAspectRatio: 'xMidYMid meet',
  });

  // substrate outline
  root.appendChild(svg('rect', { class: 'outline', x: 0, y: 0, width: fp.x, height: fp.y, rx: 0.6 }));

  // 5 mm grid, so a reader can judge real size without a scale bar
  const grid = svg('g', { class: 'ruler', 'aria-hidden': 'true' });
  for (let gx = 5; gx < fp.x; gx += 5) grid.appendChild(svg('line', { x1: gx, y1: 0, x2: gx, y2: fp.y, 'stroke-dasharray': '0.3 0.5' }));
  for (let gy = 5; gy < fp.y; gy += 5) grid.appendChild(svg('line', { x1: 0, y1: flip(gy), x2: fp.x, y2: flip(gy), 'stroke-dasharray': '0.3 0.5' }));
  root.appendChild(grid);

  // channels under the chambers
  const gCh = svg('g', { class: 'channels' });
  for (const ch of spec.channels || []) {
    const pts = channelPoints(ch, anchors);
    if (pts.length < 2) continue;
    const w = Number(ch.width_mm ?? ch.width ?? 0.2) || 0.2;
    const carries = String(ch.carries || '').toLowerCase();
    const colour = carries === 'capsules' ? '#fb923c' : carries === 'oil' ? '#fbbf24' : '#7dd3fc';
    const d = pts.map(([x, y], i) => `${i ? 'L' : 'M'}${fmtNum(x, 6)} ${fmtNum(flip(y), 6)}`).join(' ');
    const path = svg('path', {
      class: 'channel', d,
      stroke: colour, 'stroke-width': Math.max(w, 0.12), 'stroke-opacity': 0.92,
      tabindex: opts.onSelect ? '0' : null,
      role: opts.onSelect ? 'button' : null,
    });
    const label = [String(ch.id || ''), `${fmtNum(w, 3)} mm wide`,
      ch.depth_mm ? `${fmtNum(ch.depth_mm, 3)} mm deep` : null,
      ch.from && ch.to ? `${ch.from} -> ${ch.to}` : null,
      carries ? `carries ${carries}` : null].filter(Boolean).join(' - ');
    path.appendChild(svg('title', {}, label));
    if (opts.onSelect) wire(path, () => opts.onSelect('channel', ch));
    gCh.appendChild(path);
    seen.add(String(ch.block || 'channel'));
  }
  root.appendChild(gCh);

  // chambers
  const gCb = svg('g', { class: 'chambers' });
  const gTx = svg('g', { class: 'labels' });
  for (const c of spec.chambers || []) {
    const [cx, cy] = xy(c.center || c.position);
    if (!Number.isFinite(cx) || !Number.isFinite(cy)) continue;
    const size = Array.isArray(c.size) ? c.size : [c.size_x_mm, c.size_y_mm];
    const w = Number(size && size[0]) || 1;
    const h = Number(size && size[1]) || 1;
    const colour = chamberColour(c.type || c.block);
    const shape = String(c.shape || '').toLowerCase();
    const common = {
      class: 'chamber', fill: colour, 'fill-opacity': 0.26, stroke: colour,
      tabindex: opts.onSelect ? '0' : null, role: opts.onSelect ? 'button' : null,
    };
    const node = shape === 'circle' || shape === 'round'
      ? svg('circle', { ...common, cx, cy: flip(cy), r: Math.max(w, h) / 2 })
      : svg('rect', { ...common, x: cx - w / 2, y: flip(cy) - h / 2, width: w, height: h, rx: Math.min(0.4, w / 6) });
    const bits = [String(c.id || ''), String(c.type || c.block || '')];
    if (Number.isFinite(Number(c.volume_ul))) bits.push(`${fmtNum(c.volume_ul, 4)} uL`);
    bits.push(`${fmtNum(w, 4)} x ${fmtNum(h, 4)} mm`);
    if (c.retention && c.retention.opening_um) bits.push(`weir ${fmtNum(c.retention.opening_um, 3)} um`);
    if (c.filter && c.filter.gap_um) bits.push(`filter gap ${fmtNum(c.filter.gap_um, 3)} um`);
    node.appendChild(svg('title', {}, bits.filter(Boolean).join(' - ')));
    if (opts.onSelect) wire(node, () => opts.onSelect('chamber', c));
    gCb.appendChild(node);
    seen.add(String(c.type || c.block || 'chamber'));

    if (opts.labels !== false && w >= 2.2 && h >= 1.1) {
      gTx.appendChild(svg('text', {
        class: 'glabel', x: cx, y: flip(cy) + 0.28, 'text-anchor': 'middle',
        'font-size': Math.min(0.95, Math.max(0.42, w / Math.max(4, String(c.id || '').length))),
        'aria-hidden': 'true',
      }, String(c.id || '')));
    }
  }
  root.appendChild(gCb);

  // ports last, on top
  const gP = svg('g', { class: 'ports' });
  for (const p of spec.ports || []) {
    const [px, py] = xy(p.position || p.center);
    if (!Number.isFinite(px) || !Number.isFinite(py)) continue;
    const type = String(p.type || '').toLowerCase();
    const colour = PORT_COLOURS[type] || '#e2e8f0';
    const r = Number(p.width_um) ? Math.max(0.4, Number(p.width_um) / 2000) : 0.4;
    const node = svg('circle', {
      class: 'port', cx: px, cy: flip(py), r,
      fill: '#04070e', stroke: colour,
      tabindex: opts.onSelect ? '0' : null, role: opts.onSelect ? 'button' : null,
    });
    node.appendChild(svg('title', {}, [String(p.id || ''), type, p.role ? `role ${p.role}` : null,
      `at ${fmtNum(px, 4)}, ${fmtNum(py, 4)} mm`].filter(Boolean).join(' - ')));
    if (opts.onSelect) wire(node, () => opts.onSelect('port', p));
    gP.appendChild(node);
  }
  root.appendChild(gP);
  root.appendChild(gTx);

  if (!fp.known) {
    root.appendChild(svg('text', { class: 'glabel', x: 0.4, y: 1.2, 'font-size': 1, fill: '#fbbf24' },
      'footprint missing from the spec - drawn on a 30 x 20 mm guess'));
  }

  return { svg: root, legend: legendFor(spec), footprint: fp };
}

function channelPoints(ch, anchors) {
  if (Array.isArray(ch.path) && ch.path.length >= 2) {
    return ch.path.map(xy).filter(([x, y]) => Number.isFinite(x) && Number.isFinite(y));
  }
  const a = anchors.get(String(ch.from));
  const b = anchors.get(String(ch.to));
  if (a && b) return [a, b];
  return [];
}

function wire(node, fn) {
  node.addEventListener('click', fn);
  node.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fn(); }
  });
  node.style.cursor = 'pointer';
}

function legendFor(spec) {
  const types = new Set();
  for (const c of spec.chambers || []) types.add(String(c.type || c.block || 'chamber').toLowerCase());
  const carries = new Set();
  for (const c of spec.channels || []) carries.add(String(c.carries || 'liquid').toLowerCase());

  const host = el('div', { class: 'chip-legend' });
  for (const t of [...types].sort()) {
    host.appendChild(el('span', {}, el('i', { style: { background: chamberColour(t) } }), t));
  }
  for (const c of [...carries].sort()) {
    const colour = c === 'capsules' ? '#fb923c' : c === 'oil' ? '#fbbf24' : '#7dd3fc';
    host.appendChild(el('span', {}, el('i', { style: { background: colour, borderRadius: '1px', height: '3px' } }), `channel: ${c}`));
  }
  host.appendChild(el('span', {}, el('i', { style: { background: 'transparent', border: '1px solid #5eead4', borderRadius: '50%' } }), 'port'));
  host.appendChild(el('span', { class: 'muted' }, 'grid 5 mm'));
  return host;
}

/** A tiny thumbnail, no labels, for lists. */
export function chipThumb(spec) {
  const { svg: node } = chipPlan(spec, { labels: false });
  node.style.maxHeight = '150px';
  return node;
}

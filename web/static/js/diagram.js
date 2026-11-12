// The assay as one drawing: Module 1's three lanes (isolated, connected, harvested), the
// chain of chips and bench steps the sample passes through, what travels between them
// (cells, then capsules holding one cell each, then a DNA library), the bulk-omics branch,
// and the single-cell readouts that come off it (with an optional side-sampling footnote). Every stage is a focusable node.
//
// All text is set with text nodes. Colours come from css/theme.css through classes.

import { svg } from './dom.js';

const W = 1200;
const H = 610;
const LINE_Y = 214;
const BLOCK = { x: 24, y: 86, w: 370, h: 258 };
const LANES = [
  { key: 'n', label: 'neurons', y: LINE_Y - 50 },
  { key: 'g', label: 'microglia', y: LINE_Y },
  { key: 'a', label: 'astrocytes', y: LINE_Y + 50 },
];

/** Short names for the nodes; the full name is in the detail panel. */
export const SHORT = {
  module1: 'Culture',
  cellprep: 'Cell prep',
  bench_2b: 'Spin + strain',
  module2: 'Encapsulate',
  bench_3b: 'Break + cure',
  module3: 'Lyse + copy RNA',
  barcoding: 'Barcode',
  module4: 'Split + PCR',
  module5: 'Release + clean',
  spri_qc: 'Size + check',
  library_out: 'Library',
  sequencing: 'Sequence',
  module6_bulk_omics: 'Optional bulk',
};

export function shortName(stage) {
  if (SHORT[stage.id]) return SHORT[stage.id];
  const n = String(stage.name || stage.id).replace(/^Module \d+:\s*/i, '').replace(/\s*\(.*$/, '');
  return n.length > 16 ? `${n.slice(0, 15)}…` : n;
}

/** printed | chip | kit | bench | todo */
export function stageKind(stage, printedIds) {
  if (stage.chip) return stage.chip.printed || printedIds.has(stage.chip.chip_id) ? 'printed' : 'chip';
  const st = String(stage.status || '').toLowerCase();
  if (st === 'roadmap' || /not.?implemented/.test(st)) return 'todo';
  if (/kit|bought/i.test(`${stage.kind} ${stage.where}`)) return 'kit';
  return 'bench';
}

export const KIND_WORDS = {
  printed: 'printed chip',
  chip: 'chip, design only',
  kit: 'bought kit',
  bench: 'at the bench',
  todo: 'not built yet',
};

function text(x, y, str, cls, extra = {}) {
  return svg('text', { x, y, class: cls, ...extra }, str);
}

function isBranch(s) {
  return !!s.branch_from || /^module6/.test(s.id);
}

/**
 * @returns {{svg: SVGElement, select(id): void, focus(id): void}}
 */
export function chainDiagram(doc, printedIds, onSelect) {
  const root = svg('svg', {
    class: 'chain-svg', viewBox: `0 0 ${W} ${H}`, role: 'group',
    'aria-label': 'The assay chain. Each stage is a button that opens its detail below.',
  });
  const nodes = new Map();
  const order = [];

  const main = doc.stages.filter((s) => !isBranch(s));
  const branch = doc.stages.filter(isBranch);
  const first = main[0];
  const rest = main.slice(1);

  // positions of the small nodes
  const x0 = 470;
  const x1 = 1160;
  const step = rest.length > 1 ? (x1 - x0) / (rest.length - 1) : 0;
  const pos = new Map(rest.map((s, i) => [s.id, x0 + i * step]));

  /* ------------------------------------------------------------ zone brackets */
  const zones = [
    { from: 'cellprep', to: 'bench_3b', lines: ['Free the cells,', 'one cell per capsule'] },
    { from: 'module3', to: 'module5', lines: ['Inside every capsule:', 'open, copy, label the RNA'] },
    { from: 'spri_qc', to: 'sequencing', lines: ['Off the chip:', 'check and read'] },
  ];
  const gz = svg('g', { class: 'zones', 'aria-hidden': 'true' });
  gz.appendChild(text(BLOCK.x, 30, 'Grow three cell types,', 'zone-label'));
  gz.appendChild(text(BLOCK.x, 46, 'then let them talk', 'zone-label'));
  gz.appendChild(svg('path', { class: 'zone-rule', d: `M${BLOCK.x} 56 H${BLOCK.x + BLOCK.w}` }));
  for (const z of zones) {
    const a = pos.get(z.from);
    const b = pos.get(z.to);
    if (a === undefined || b === undefined) continue;
    const l = a - 26;
    const r = b + 26;
    gz.appendChild(svg('path', { class: 'zone-rule', d: `M${l} 62 V56 H${r} V62` }));
    z.lines.forEach((line, k) => gz.appendChild(text((l + r) / 2, 30 + k * 16, line, 'zone-label', { 'text-anchor': 'middle' })));
  }
  root.appendChild(gz);

  /* ------------------------------------------------------------ the line */
  const gl = svg('g', { class: 'flow', 'aria-hidden': 'true' });
  const segX = [[BLOCK.x + BLOCK.w, x0], ...rest.slice(0, -1).map((s) => [pos.get(s.id), pos.get(s.id) + step])];
  const segFrom = [first, ...rest.slice(0, -1)];
  segX.forEach(([a, b], i) => {
    const from = segFrom[i];
    const toId = (rest[i] || {}).id;
    const h = (from.leaves || []).find((x) => x.to && x.to.stage === toId) || (from.leaves || [])[0];
    const carries = String((h && h.carries) || '').toLowerCase();
    gl.appendChild(segment(a + (i ? 18 : 0), b - 18, carries));
  });
  root.appendChild(gl);

  /* ------------------------------------------------------------ module 1 block */
  if (first) {
    const kind = stageKind(first, printedIds);
    const g = nodeGroup(first, kind);
    g.classList.add('culture');
    g.appendChild(svg('rect', {
      class: 'block', x: BLOCK.x, y: BLOCK.y, width: BLOCK.w, height: BLOCK.h, rx: 20,
    }));
    g.appendChild(text(BLOCK.x + 22, BLOCK.y + 30, 'MODULE 1', 'mono-cap'));
    g.appendChild(text(BLOCK.x + 92, BLOCK.y + 30, shortName(first), 'block-title'));
    if (kind === 'printed') {
      g.appendChild(svg('circle', { class: 'printed-dot', cx: BLOCK.x + BLOCK.w - 84, cy: BLOCK.y + 26, r: 4 }));
      g.appendChild(text(BLOCK.x + BLOCK.w - 74, BLOCK.y + 30, 'PRINTED', 'mono-cap printed-word'));
    }
    const px = [BLOCK.x + 108, BLOCK.x + 196, BLOCK.x + 284];
    const pw = 70;
    for (const lane of LANES) {
      g.appendChild(text(BLOCK.x + 22, lane.y + 4, lane.label, 'lane-label'));
    }
    // phase 1: sealed lanes
    for (const lane of LANES) {
      g.appendChild(svg('rect', { class: `lane lane-${lane.key}`, x: px[0], y: lane.y - 13, width: pw, height: 26, rx: 6 }));
      cells(g, px[0] + 10, lane.y, 4, lane.key);
    }
    // phase 2: openings between lanes, something crossing
    for (const lane of LANES) {
      g.appendChild(svg('rect', { class: `lane lane-${lane.key}`, x: px[1], y: lane.y - 13, width: pw, height: 26, rx: 6 }));
      cells(g, px[1] + 10, lane.y, 4, lane.key);
    }
    for (let i = 0; i < 2; i += 1) {
      const ya = LANES[i].y + 13;
      const yb = LANES[i + 1].y - 13;
      for (const dx of [20, 48]) {
        g.appendChild(svg('rect', { class: 'bridge', x: px[1] + dx - 4, y: ya, width: 8, height: yb - ya }));
        g.appendChild(svg('circle', { class: 'signal', cx: px[1] + dx, cy: (ya + yb) / 2 + (dx === 20 ? -3 : 3), r: 2.2 }));
      }
    }
    // phase 3: lanes drain together to the junction
    const jx = BLOCK.x + BLOCK.w - 12;
    for (const lane of LANES) {
      g.appendChild(svg('path', {
        class: `lane-drain lane-${lane.key}`,
        d: `M${px[2]} ${lane.y} H${px[2] + 30} C${px[2] + 56} ${lane.y} ${px[2] + 56} ${LINE_Y} ${jx} ${LINE_Y}`,
      }));
      cells(g, px[2] + 4, lane.y, 2, lane.key);
    }
    g.appendChild(svg('circle', { class: 'junction', cx: jx, cy: LINE_Y, r: 5 }));
    ['isolated', 'connected', 'harvested'].forEach((w, i) => {
      g.appendChild(text(px[i] + pw / 2, LINE_Y + 92, `${i + 1}  ${w}`, 'phase-label', { 'text-anchor': 'middle' }));
    });
    g.appendChild(text(BLOCK.x + 22, BLOCK.y + BLOCK.h - 14, 'schematic: on the chip the three lanes meet in a Y', 'tiny-note'));
    root.appendChild(g);
    nodes.set(first.id, g);
    order.push(first.id);
  }

  /* ------------------------------------------------------------ small nodes */
  rest.forEach((s, i) => {
    const x = pos.get(s.id);
    const kind = stageKind(s, printedIds);
    const above = (rest.length - 1 - i) % 2 === 0;
    const g = nodeGroup(s, kind);
    g.appendChild(svg('rect', { class: 'hit', x: x - 40, y: above ? LINE_Y - 72 : LINE_Y - 24, width: 80, height: 100, rx: 10 }));
    g.appendChild(shape(x, LINE_Y, kind));
    const ly = above ? LINE_Y - 50 : LINE_Y + 46;
    g.appendChild(text(x, ly, shortName(s), 'node-label', { 'text-anchor': 'middle' }));
    g.appendChild(text(x, ly + 15, KIND_WORDS[kind], `node-kind kind-${kind}`, { 'text-anchor': 'middle' }));
    root.appendChild(g);
    nodes.set(s.id, g);
    order.push(s.id);
  });

  /* ------------------------------------------------------------ branch */
  const m6 = branch[0];
  const bx = BLOCK.x + 186;
  const by = 418;
  if (m6) {
    const kind = stageKind(m6, printedIds);
    root.appendChild(svg('path', { class: 'flow-line branch-line', d: `M${bx} ${BLOCK.y + BLOCK.h} V${by - 18}`, 'aria-hidden': 'true' }));
    root.appendChild(text(bx + 10, BLOCK.y + BLOCK.h + 26, 'some cells, before capsules', 'tiny-note', { 'aria-hidden': 'true' }));
    const g = nodeGroup(m6, kind);
    g.classList.add('optional');
    g.appendChild(svg('rect', { class: 'hit', x: bx - 24, y: by - 24, width: 180, height: 48, rx: 10 }));
    g.appendChild(shape(bx, by, kind));
    g.appendChild(text(bx + 28, by - 1, shortName(m6), 'node-label'));
    g.appendChild(text(bx + 28, by + 14, `Module 6 · ${KIND_WORDS[kind]}`, `node-kind kind-${kind}`));
    root.appendChild(g);
    nodes.set(m6.id, g);
    order.push(m6.id);
  }

  /* ------------------------------------------------------------ readouts */
  const ry = 520;
  const gr = svg('g', { class: 'readouts' });
  const seqX = pos.get('sequencing') ?? x1;
  const pills = [
    { x: seqX - 520, w: 250, label: 'Single-cell regulatory', sub: 'TIP-seq, cell by cell', from: [seqX, LINE_Y + 18] },
    { x: seqX - 250, w: 270, label: 'Single-cell transcriptome', sub: 'genes on, cell by cell', from: [seqX, LINE_Y + 18] },
  ];
  const gapR = seqX - 520;
  gr.appendChild(svg('path', { class: 'zone-rule', d: `M${BLOCK.x} ${ry + 28} H${gapR - 24}`, 'aria-hidden': 'true' }));
  gr.appendChild(text(BLOCK.x, ry + 20, 'SINGLE-CELL READOUTS', 'mono-cap readouts-cap', {}));
  gr.appendChild(svg('path', { class: 'readout-line', d: `M${seqX} ${LINE_Y + 18} V${ry - 22} H${pills[0].x + 125} V${ry}`, 'aria-hidden': 'true' }));
  gr.appendChild(svg('path', { class: 'readout-line', d: `M${seqX} ${ry - 22} H${pills[1].x + 135} V${ry}`, 'aria-hidden': 'true' }));
  for (const p of pills) {
    const cx = p.x + p.w / 2;
    const pg = svg('g', { class: 'readout' });
    pg.appendChild(svg('rect', { x: p.x, y: ry, width: p.w, height: 56, rx: 28 }));
    pg.appendChild(text(cx, ry + 25, p.label, 'readout-label', { 'text-anchor': 'middle' }));
    pg.appendChild(text(cx, ry + 42, p.sub, 'readout-sub', { 'text-anchor': 'middle' }));
    gr.appendChild(pg);
  }
  gr.appendChild(text(BLOCK.x, ry + 48, 'Optional, sampled separately: protein and metabolite', 'tiny-note', {}));
  gr.appendChild(text(BLOCK.x, ry + 64, 'samples from the same chambers, e.g. mass spec later.', 'tiny-note', {}));
  root.appendChild(gr);

  /* ------------------------------------------------------------ behaviour */
  function nodeGroup(s, kind) {
    const g = svg('g', {
      class: `node kind-${kind}`, tabindex: '0', role: 'button',
      'aria-label': `${s.name || s.id}: ${KIND_WORDS[kind]}. Open its detail.`,
      dataset: { stage: s.id },
    });
    g.appendChild(svg('title', {}, `${s.name || s.id} (${KIND_WORDS[kind]})`));
    g.addEventListener('click', () => onSelect(s.id));
    g.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(s.id); return; }
      if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
        e.preventDefault();
        const i = order.indexOf(s.id);
        const next = order[(i + (e.key === 'ArrowRight' ? 1 : order.length - 1)) % order.length];
        nodes.get(next)?.focus();
      }
    });
    return g;
  }

  return {
    svg: root,
    order,
    select(id) {
      for (const [k, g] of nodes) g.classList.toggle('selected', k === id);
      for (const [k, g] of nodes) g.setAttribute('aria-pressed', String(k === id));
    },
    focus(id) { nodes.get(id)?.focus(); },
  };
}

function cells(g, x, y, n, lane) {
  for (let i = 0; i < n; i += 1) {
    g.appendChild(svg('circle', { class: `cell cell-${lane}`, cx: x + i * 14 + (i % 2 ? 2 : 0), cy: y + (i % 2 ? 4 : -4), r: 3.4 }));
  }
}

function shape(x, y, kind) {
  const g = svg('g', { class: 'shape' });
  if (kind === 'printed' || kind === 'chip') {
    g.appendChild(svg('rect', { class: 'chip-box', x: x - 17, y: y - 17, width: 34, height: 34, rx: 8 }));
    g.appendChild(svg('path', { class: 'chip-glyph', d: `M${x - 8} ${y} H${x + 8} M${x - 8} ${y - 6} V${y + 6} M${x + 8} ${y - 6} V${y + 6}` }));
  } else {
    g.appendChild(svg('circle', { class: 'bench-ring', cx: x, cy: y, r: 15 }));
    if (kind === 'todo') {
      g.appendChild(svg('path', { class: 'todo-glyph', d: `M${x - 5} ${y} H${x + 5}` }));
    } else {
      g.appendChild(svg('path', { class: 'tube-glyph', d: `M${x - 3.5} ${y - 7} V${y + 4} a3.5 3.5 0 0 0 7 0 V${y - 7}` }));
    }
  }
  return g;
}

/** One stretch of the line, decorated with what travels along it. */
function segment(a, b, carries) {
  const g = svg('g', {});
  if (b <= a) return g;
  const cls = carries === 'library' ? 'flow-line library' : 'flow-line';
  g.appendChild(svg('path', { class: cls, d: `M${a} ${LINE_Y} H${b}` }));
  const len = b - a;
  if (carries === 'cells') {
    const n = Math.max(2, Math.floor(len / 14));
    for (let i = 1; i < n; i += 1) {
      g.appendChild(svg('circle', { class: 'travel-cell', cx: a + (len * i) / n, cy: LINE_Y, r: 2.4 }));
    }
  } else if (carries === 'capsules') {
    const n = Math.max(2, Math.floor(len / 22));
    for (let i = 1; i < n; i += 1) {
      const cx = a + (len * i) / n;
      g.appendChild(svg('circle', { class: 'capsule', cx, cy: LINE_Y, r: 6 }));
      g.appendChild(svg('circle', { class: 'capsule-cell', cx: cx + 0.8, cy: LINE_Y - 0.6, r: 2 }));
    }
  }
  return g;
}

/** Tiny inline glyphs for the HTML legend under the diagram. */
export function legendGlyph(kind) {
  const s = svg('svg', { class: 'legend-glyph', viewBox: '0 0 44 40', 'aria-hidden': 'true' });
  const g = svg('g', { class: `node kind-${kind}` });
  if (kind === 'cells' || kind === 'capsules' || kind === 'library') {
    g.appendChild(svg('path', { class: kind === 'library' ? 'flow-line library' : 'flow-line', d: 'M2 20 H42' }));
    if (kind === 'cells') for (const x of [12, 22, 32]) g.appendChild(svg('circle', { class: 'travel-cell', cx: x, cy: 20, r: 2.4 }));
    if (kind === 'capsules') {
      for (const x of [13, 31]) {
        g.appendChild(svg('circle', { class: 'capsule', cx: x, cy: 20, r: 6 }));
        g.appendChild(svg('circle', { class: 'capsule-cell', cx: x + 0.8, cy: 19.4, r: 2 }));
      }
    }
  } else {
    g.appendChild(shape(22, 20, kind));
  }
  s.appendChild(g);
  return s;
}

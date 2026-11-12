// A 3D-looking view of a chip drawn straight from its spec, as inline SVG. Needs no STL,
// no WebGL and no file route, so it always works. The plan is tilted with an oblique
// projection (an affine map, so the plan geometry is reused as-is) and every recess is
// cut into the plate to its real depth, exaggerated so a 0.3 mm etch is visible at all.

import { svg, fmtNum } from './dom.js';
import { xy, footprintOf, anchorMap, channelPoints, chamberColour } from './chipsvg.js';

const SKEW_X = 0.5;   // how far depth (spec y) leans to the right
const SQUASH = 0.42;  // how much depth is foreshortened
const PLATE_MM = 2;   // drawn plate thickness

let clipSeq = 0;

/**
 * @param {object} spec chip spec JSON
 * @param {object} opts {exaggerate: depth multiplier}
 */
export function chipIso(spec, opts = {}) {
  const fp = footprintOf(spec || {});
  const X = fp.x;
  const Y = fp.y;
  const S = opts.exaggerate || 8;
  const top = Y * SQUASH + 2;
  const W = X + Y * SKEW_X + 4;
  const H = top + PLATE_MM + 0.3 * S + 4;

  // spec (x, y up) -> screen. Affine, so it can be a transform on a group.
  const P = (x, y) => [x + y * SKEW_X + 2, top - y * SQUASH];
  const matrix = `matrix(1 0 ${SKEW_X} ${-SQUASH} 2 ${top})`;
  const poly = (pts) => pts.map(([a, b]) => `${fmtNum(a, 6)},${fmtNum(b, 6)}`).join(' ');

  const root = svg('svg', {
    class: 'chip-iso', viewBox: `0 0 ${fmtNum(W, 6)} ${fmtNum(H, 6)}`, role: 'img',
    'aria-label': `Tilted view of ${String(spec.id || 'the chip')}, ${fmtNum(X, 4)} by ${fmtNum(Y, 4)} mm, depth exaggerated ${S} times`,
    preserveAspectRatio: 'xMidYMid meet',
  });
  const defs = svg('defs');
  root.appendChild(defs);

  // plate: right side, front side, then the top face
  const [a0, a1, a2] = [P(0, 0), P(X, 0), P(X, Y)];
  root.appendChild(svg('polygon', {
    class: 'iso-side', points: poly([a1, a2, [a2[0], a2[1] + PLATE_MM], [a1[0], a1[1] + PLATE_MM]]),
  }));
  root.appendChild(svg('polygon', {
    class: 'iso-front', points: poly([a0, a1, [a1[0], a1[1] + PLATE_MM], [a0[0], a0[1] + PLATE_MM]]),
  }));
  root.appendChild(svg('polygon', { class: 'iso-top', points: poly([P(0, 0), P(X, 0), P(X, Y), P(0, Y)]) }));

  const anchors = anchorMap(spec);

  // channels: the dark slot, then its floor shifted down by the etch depth
  for (const ch of spec.channels || []) {
    const pts = channelPoints(ch, anchors);
    if (pts.length < 2) continue;
    const w = Number(ch.width_mm ?? 0.2) || 0.2;
    const d = Number(ch.depth_mm ?? 0.15) || 0.15;
    const dpath = pts.map(([x, y], i) => `${i ? 'L' : 'M'}${fmtNum(x, 6)} ${fmtNum(y, 6)}`).join(' ');
    const g = svg('g', { transform: matrix });
    g.appendChild(svg('path', { class: 'iso-slot', d: dpath, 'stroke-width': w * 1.15 }));
    root.appendChild(g);
    const f = svg('g', { transform: `translate(0 ${fmtNum(d * S * 0.6, 4)})` });
    const fg = svg('g', { transform: matrix });
    fg.appendChild(svg('path', { class: 'iso-chan', d: dpath, 'stroke-width': w * 0.7 }));
    f.appendChild(fg);
    root.appendChild(f);
  }

  // chambers: an opening, its far walls, and the floor at depth, clipped to the opening
  for (const c of spec.chambers || []) {
    const [cx, cy] = xy(c.center || c.position);
    if (!Number.isFinite(cx) || !Number.isFinite(cy)) continue;
    const size = Array.isArray(c.size) ? c.size : [c.size_x_mm, c.size_y_mm, c.depth_mm];
    const w = Number(size && size[0]) || 1;
    const h = Number(size && size[1]) || 1;
    const d = Number(size && size[2]) || 0.3;
    clipSeq += 1;
    const id = `isoclip-${clipSeq}`;
    const rect = { x: cx - w / 2, y: cy - h / 2, width: w, height: h };
    const clip = svg('clipPath', { id, clipPathUnits: 'userSpaceOnUse' });
    clip.appendChild(svg('rect', { ...rect, transform: matrix }));
    defs.appendChild(clip);

    const colour = chamberColour(c.type || c.block);
    const g = svg('g', { 'clip-path': `url(#${id})` });
    const wall = svg('g', { transform: matrix });
    wall.appendChild(svg('rect', { ...rect, class: 'iso-wall' }));
    g.appendChild(wall);
    const floor = svg('g', { transform: `translate(0 ${fmtNum(d * S, 4)})` });
    const fg = svg('g', { transform: matrix });
    fg.appendChild(svg('rect', { ...rect, class: 'iso-floor', style: { fill: colour } }));
    floor.appendChild(fg);
    g.appendChild(floor);
    const rim = svg('g', { transform: matrix });
    rim.appendChild(svg('rect', { ...rect, class: 'iso-rim', style: { stroke: colour } }));
    g.appendChild(rim);
    const title = svg('title', {}, `${String(c.id || '')} - ${fmtNum(w, 3)} x ${fmtNum(h, 3)} mm, ${fmtNum(d, 3)} mm deep`);
    g.appendChild(title);
    root.appendChild(g);
  }

  // ports: through-holes, drawn as dark ellipses
  for (const p of spec.ports || []) {
    const [px, py] = xy(p.position || p.center);
    if (!Number.isFinite(px)) continue;
    const r = Number(p.width_um) ? Math.max(0.4, Number(p.width_um) / 2000) : 0.4;
    const g = svg('g', { transform: matrix });
    g.appendChild(svg('circle', { class: 'iso-port', cx: px, cy: py, r }));
    g.appendChild(svg('title', {}, `${String(p.id || '')} - ${String(p.type || 'port')}`));
    root.appendChild(g);
  }

  return root;
}

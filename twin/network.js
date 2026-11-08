// Hydraulic network solver for the twin: the same model as operator/network.py (which is the
// reference; the Playwright check compares the two on every Module 1 v1 mode).
//   nodes  chambers + ports;  edges  channels, R = 12 mu L / (w h^3 (1 - 0.63 h/w)), plus each
//   chamber's slab from the entry edge to its centre;  ports  FEED (uL/min), VENT (0 Pa), CAP.
// Also: the medium tracer, dye arrival times, the one-line result and the safety rules of
// operator/module1_operator.py, so editing a pump rate in the twin re-solves on the spot.

export const MU_37C = 0.8e-3;
const FIRST_TINT = 2 / 3;
export const SHEAR_CEILING = 0.01;

// Port bore and lid, as operator/network.py reads them: the bore is contracts/blocks.json's
// fabrication floor (min_port_mm), the same source cad/make_train_cad.py draws the DXF from.
// configure() sets them before a Network is built; the exports are live bindings.
export let PORT_HOLE_MM = 0.8;
let LID_MM = 3;
export let PORT_HOLE_UL = PORT_HOLE_MM * PORT_HOLE_MM * LID_MM;   // the bore through the lid
export function configure({ portHoleMm, lidMm } = {}) {
  if (portHoleMm > 0) PORT_HOLE_MM = portHoleMm;
  if (lidMm > 0) LID_MM = lidMm;
  PORT_HOLE_UL = PORT_HOLE_MM * PORT_HOLE_MM * LID_MM;
}

// Largest feed that keeps every culture chamber under the shear ceiling: operator/network.py's
// _default_feed_ul_min, derived from the chip so a rescale can't leave it stale.
export function defaultFeed(chip, { mu = MU_37C, ceiling = SHEAR_CEILING, margin = 0.85 } = {}) {
  const flows = chip.chambers.filter((c) => c.type !== 'pool').map((c) => {
    const w = c.size[1] * 1e-3;
    const h = c.size[2] * 1e-3;
    return ((ceiling / 10) * w * h * h) / (6 * mu) * 60 * 1e9;   // m3/s -> uL/min
  });
  return flows.length ? Math.round(Math.min(...flows) * margin * 1000) / 1000 : 2;
}

export const MODES = {
  grow: { label: 'Separate growth', feed: ['p_in', 'p_g_in', 'p_a_in'], vent: ['p_mid'] },
  int_n: { label: 'Interact N → G+A', feed: ['p_in'], vent: ['p_g_out', 'p_a_out'] },
  int_g: { label: 'Interact G → N+A', feed: ['p_g_in'], vent: ['p_in', 'p_a_out'] },
  int_a: { label: 'Interact A → N+G', feed: ['p_a_in'], vent: ['p_in', 'p_g_out'] },
  harvest: { label: 'Harvest', feed: ['p_in'], vent: ['p_mid'] },
  seed: { label: 'Seed (fallback)', feed: ['p_in'], vent: ['p_g_out', 'p_a_out'] },
  collect: { label: 'Collect (protocol back-flush)', feed: ['p_in', 'p_g_out', 'p_a_out'], vent: ['p_mid'] },
  idle: { label: 'Idle (all capped)', feed: [], vent: [] },
};
export const DESIGN_MODES = ['grow', 'int_n', 'int_g', 'int_a', 'harvest', 'seed'];

// ------------------------------------------------------------------ geometry

// Channel centrelines from a cad/make_train_cad.py DXF: 4 LINEs per channel rectangle.
export function parseDxfChannels(text) {
  const t = text.split('\n').map((s) => s.trim());
  const lines = [];
  let cur = null;
  let layer = null;
  for (let i = 0; i + 1 < t.length; i += 2) {
    const code = t[i];
    const val = t[i + 1];
    if (code === '0') {
      if (cur && layer === 'CHANNELS') lines.push([[cur['10'], cur['20']], [cur['11'], cur['21']]]);
      cur = val === 'LINE' ? {} : null;
      layer = null;
    } else if (cur && code === '8') layer = val;
    else if (cur && ['10', '20', '11', '21'].includes(code)) cur[code] = Number(val);
  }
  const rects = [];
  for (let k = 0; k + 3 < lines.length; k += 4) {
    const [c0, c1, c2, c3] = [lines[k][0], lines[k + 1][0], lines[k + 2][0], lines[k + 3][0]];
    const a = [(c0[0] + c3[0]) / 2, (c0[1] + c3[1]) / 2];
    const b = [(c1[0] + c2[0]) / 2, (c1[1] + c2[1]) / 2];
    rects.push({ a, b, width_mm: Math.hypot(c0[0] - c3[0], c0[1] - c3[1]), length_mm: Math.hypot(b[0] - a[0], b[1] - a[1]) });
  }
  return rects;
}

function clipToBox(p0, p1, center, size) {
  const [cx, cy] = center;
  const [sx, sy] = size;
  const x0 = cx - sx / 2, x1 = cx + sx / 2, y0 = cy - sy / 2, y1 = cy + sy / 2;
  const dx = p1[0] - p0[0], dy = p1[1] - p0[1];
  let best = 0;
  for (const [lo, hi, start, delta] of [[x0, x1, p0[0], dx], [y0, y1, p0[1], dy]]) {
    if (Math.abs(delta) < 1e-9) continue;
    for (const bound of [lo, hi]) {
      const tt = (bound - start) / delta;
      if (tt >= 0 && tt <= 1) {
        const px = p0[0] + dx * tt, py = p0[1] + dy * tt;
        if (px >= x0 - 1e-6 && px <= x1 + 1e-6 && py >= y0 - 1e-6 && py <= y1 + 1e-6) best = Math.max(best, tt);
      }
    }
  }
  return [p0[0] + dx * best, p0[1] + dy * best];
}

const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);
function segDist(p, a, b) {
  const dx = b[0] - a[0], dy = b[1] - a[1];
  const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy || 1)));
  return dist(p, [a[0] + t * dx, a[1] + t * dy]);
}

// id -> {a, b, length_mm, width_mm, depth_mm, source}
export function channelGeometry(chip, dxfRects = null) {
  const boxes = Object.fromEntries(chip.chambers.map((c) => [c.id, [c.center, c.size]]));
  for (const p of chip.ports ?? []) boxes[p.id] = [p.position, [PORT_HOLE_MM, PORT_HOLE_MM, 0]];
  const out = {};
  for (const ch of chip.channels) {
    const [ca, sa] = boxes[ch.from];
    const [cb, sb] = boxes[ch.to];
    const a = clipToBox(ca, cb, ca, sa);
    const b = clipToBox(cb, ca, cb, sb);
    const g = { a, b, length_mm: dist(a, b), width_mm: ch.width_mm, depth_mm: ch.depth_mm, source: 'spec (straight, trimmed)' };
    if (dxfRects?.length) {
      const score = (r) => Math.min(dist(r.a, a) + dist(r.b, b), dist(r.a, b) + dist(r.b, a));
      const best = dxfRects.reduce((m, r) => (score(r) < score(m) ? r : m));
      if (score(best) < 0.05) {
        const fwd = dist(best.a, a) < dist(best.a, b);
        Object.assign(g, { a: fwd ? best.a : best.b, b: fwd ? best.b : best.a, length_mm: best.length_mm, width_mm: best.width_mm, source: 'DXF' });
      } else {
        // An as-built file (tools/check_asbuilt.py) needn't trim where our generator does (it may run on
        // into a port pad): take the outline whose centreline lies along this channel's line.
        const off = (r) => Math.max(segDist(r.a, ca, cb), segDist(r.b, ca, cb));
        const along = dxfRects.filter((r) => off(r) < Math.max(0.3, 2 * ch.width_mm) && r.length_mm > 0.3 * dist(a, b));
        const near = along.length ? along.reduce((m, r) => (off(r) < off(m) ? r : m)) : null;
        if (near) {
          const fwd = dist(near.a, ca) < dist(near.b, ca);
          Object.assign(g, { a: fwd ? near.a : near.b, b: fwd ? near.b : near.a, length_mm: near.length_mm, width_mm: near.width_mm, source: 'DXF (as built)' });
        }
      }
    }
    out[ch.id] = g;
  }
  return out;
}

// ------------------------------------------------------------------ physics

export function rectResistance(lengthMm, widthMm, depthMm, mu = MU_37C) {
  const w = Math.max(widthMm, depthMm) / 1000;
  const h = Math.min(widthMm, depthMm) / 1000;
  return (12 * mu * (lengthMm / 1000)) / (w * h ** 3 * (1 - (0.63 * h) / w));
}

export function shear(flowUlMin, widthMm, depthMm, mu = MU_37C) {
  const q = (Math.abs(flowUlMin) * 1e-9) / 60;
  return ((6 * mu * q) / ((widthMm / 1000) * (depthMm / 1000) ** 2)) * 10;
}

function chamberLeg(c, entry) {
  const [cx] = c.center;
  const [sx, sy] = c.size;
  const onX = Math.abs(Math.abs(entry[0] - cx) - sx / 2) < 1e-3;
  return onX ? [sx / 2, sy] : [sy / 2, sx];
}

export class Network {
  constructor(chip, dxfRects = null, mu = MU_37C) {
    this.chip = chip;
    this.mu = mu;
    this.geometry = channelGeometry(chip, dxfRects);
    this.chambers = Object.fromEntries(chip.chambers.map((c) => [c.id, c]));
    this.nodes = [...chip.chambers.map((c) => c.id), ...(chip.ports ?? []).map((p) => p.id)];
    this.culture = chip.chambers.filter((c) => !['pool', 'junction', 'split', 'inlet', 'outlet'].includes(c.type)).map((c) => c.id);
    this.edges = chip.channels.map((ch) => {
      const g = this.geometry[ch.id];
      let R = rectResistance(g.length_mm, g.width_mm, g.depth_mm, mu);
      for (const [end, pt] of [[ch.from, g.a], [ch.to, g.b]]) {
        const c = this.chambers[end];
        if (c) { const [leg, w] = chamberLeg(c, pt); R += rectResistance(leg, w, c.size[2], mu); }
      }
      return { id: ch.id, from: ch.from, to: ch.to, R, ...g };
    });
  }

  // ports: id -> 'VENT' | 'CAP' | ['FEED', uL/min]; unlisted = CAP
  solve(ports) {
    const state = {};
    for (const p of this.chip.ports ?? []) {
      const v = ports[p.id] ?? 'CAP';
      state[p.id] = Array.isArray(v) ? [v[0], v[0] === 'FEED' ? Number(v[1]) : 0] : [v, 0];
    }
    const idx = Object.fromEntries(this.nodes.map((n, i) => [n, i]));
    const parent = this.nodes.map((_, i) => i);
    const find = (i) => { while (parent[i] !== i) { parent[i] = parent[parent[i]]; i = parent[i]; } return i; };
    for (const e of this.edges) parent[find(idx[e.from])] = find(idx[e.to]);
    const comps = {};
    for (const n of this.nodes) (comps[find(idx[n])] ??= []).push(n);
    const fixed = {};
    for (const [n, [k]] of Object.entries(state)) if (k === 'VENT') fixed[n] = 0;
    for (const members of Object.values(comps)) {
      const feeds = members.filter((n) => state[n]?.[0] === 'FEED' && state[n][1] > 0);
      const vents = members.filter((n) => n in fixed);
      if (feeds.length && !vents.length) throw new Error(`${feeds.join(', ')} fed but nothing is vented downstream: the liquid has nowhere to go`);
      if (!vents.length) for (const n of members) fixed[n] = 0;
    }
    const unknown = this.nodes.filter((n) => !(n in fixed));
    const u = Object.fromEntries(unknown.map((n, i) => [n, i]));
    const m = unknown.length;
    const A = Array.from({ length: m }, () => new Array(m).fill(0));
    const rhs = new Array(m).fill(0);
    for (const n of unknown) if (state[n]?.[0] === 'FEED') rhs[u[n]] += (state[n][1] * 1e-9) / 60;
    for (const e of this.edges) {
      const g = 1 / e.R;
      for (const [x, y] of [[e.from, e.to], [e.to, e.from]]) {
        if (!(x in u)) continue;
        A[u[x]][u[x]] += g;
        if (y in u) A[u[x]][u[y]] -= g;
        else rhs[u[x]] += g * fixed[y];
      }
    }
    const pressure = { ...fixed };
    gauss(A, rhs).forEach((v, i) => { pressure[unknown[i]] = v; });
    const flows = {};
    for (const e of this.edges) {
      const q = ((pressure[e.from] - pressure[e.to]) / e.R) * 60 * 1e9;
      flows[e.id] = Math.abs(q) < 1e-9 ? 0 : q;
    }
    return this.#report(state, pressure, flows);
  }

  #report(state, pressure, flows) {
    const inflow = Object.fromEntries(this.nodes.map((n) => [n, 0]));
    for (const e of this.edges) { const q = flows[e.id]; inflow[q > 0 ? e.to : e.from] += Math.abs(q); }
    const ports = {};
    for (const [pid, [k, q]] of Object.entries(state)) {
      let net = 0;
      for (const e of this.edges) if (e.from === pid) net += flows[e.id]; else if (e.to === pid) net -= flows[e.id];
      ports[pid] = { state: k, set_ul_min: q, into_chip_ul_min: net };
    }
    const channels = {};
    for (const e of this.edges) {
      const q = flows[e.id];
      channels[e.id] = { flow_ul_min: q, from: q >= 0 ? e.from : e.to, to: q >= 0 ? e.to : e.from,
        shear_dyn_cm2: shear(q, e.width_mm, e.depth_mm, this.mu), length_mm: e.length_mm,
        volume_ul: e.length_mm * e.width_mm * e.depth_mm, velocity_mm_s: Math.abs(q) / 60 / (e.width_mm * e.depth_mm) };
    }
    const chambers = {};
    for (const [cid, c] of Object.entries(this.chambers)) {
      chambers[cid] = { throughput_ul_min: inflow[cid], shear_dyn_cm2: shear(inflow[cid], c.size[1], c.size[2], this.mu), volume_ul: c.volume_ul };
    }
    const routing = this.#routing(flows, state);
    return { ports, channels, chambers, pressure_pa: pressure,
      max_pressure_mbar: Math.max(...Object.values(pressure)) / 100, routing };
  }

  #routing(flows, state) {
    const into = Object.fromEntries(this.nodes.map((n) => [n, []]));
    for (const e of this.edges) {
      const q = flows[e.id];
      if (q > 0) into[e.to].push([e.from, q]); else if (q < 0) into[e.from].push([e.to, -q]);
    }
    const memo = {};
    const outTags = (n, stack) => {
      if (memo[n]) return memo[n];
      if (stack.includes(n)) throw new Error('circular flow');
      const tags = this.culture.includes(n) || state[n]?.[0] === 'FEED' ? { [n]: 1 } : inTags(n, [...stack, n]);
      memo[n] = tags;
      return tags;
    };
    const inTags = (n, stack = []) => {
      let total = into[n].reduce((a, [, q]) => a + q, 0);
      const mix = {};
      if (state[n]?.[0] === 'FEED') { mix[n] = state[n][1]; total += state[n][1]; }
      for (const [src, q] of into[n]) for (const [tag, f] of Object.entries(outTags(src, stack))) mix[tag] = (mix[tag] ?? 0) + q * f;
      return total ? Object.fromEntries(Object.entries(mix).map(([k, v]) => [k, v / total])) : {};
    };
    const inflowMix = {};
    for (const n of this.nodes) { const mx = inTags(n); if (Object.keys(mx).length) inflowMix[n] = mx; }
    const foreign = {};
    for (const c of this.culture) {
      const f = Object.fromEntries(Object.entries(inflowMix[c] ?? {}).filter(([t]) => this.culture.includes(t) && t !== c));
      if (Object.keys(f).length) foreign[c] = f;
    }
    return { inflow_mix: inflowMix, foreign_medium: foreign };
  }
}

function gauss(A, b) {
  const n = b.length;
  const M = A.map((row, i) => [...row, b[i]]);
  for (let col = 0; col < n; col++) {
    let piv = col;
    for (let r = col + 1; r < n; r++) if (Math.abs(M[r][col]) > Math.abs(M[piv][col])) piv = r;
    if (Math.abs(M[piv][col]) < 1e-30) throw new Error('singular network');
    [M[col], M[piv]] = [M[piv], M[col]];
    for (let r = 0; r < n; r++) {
      if (r === col || !M[r][col]) continue;
      const f = M[r][col] / M[col][col];
      for (let k = col; k <= n; k++) M[r][k] -= f * M[col][k];
    }
  }
  return M.map((row, i) => row[n] / row[i]);
}

// ------------------------------------------------------------------ modes, results, safety, dye

export function modeStates(mode, flow) {
  const m = MODES[mode];
  const states = {};
  const flows = {};
  for (const p of ['p_in', 'p_g_in', 'p_a_in', 'p_mid', 'p_g_out', 'p_a_out']) {
    states[p] = m.feed.includes(p) ? 'FEED' : m.vent.includes(p) ? 'VENT' : 'CAP';
    if (states[p] === 'FEED') flows[p] = typeof flow === 'object' ? flow[p] : flow;
  }
  return { states, flows };
}

export function solverPorts(states, flows) {
  return Object.fromEntries(Object.entries(states).map(([p, s]) => [p, s === 'FEED' ? ['FEED', flows[p] ?? 0] : s]));
}

const pct = (x) => `${Math.round(x * 100)}%`;
const foreignText = (f) => Object.entries(f).map(([c, srcs]) => `${c} receives ${Object.entries(srcs).map(([s, v]) => `${pct(v)} ${s} medium`).join(', ')}`).join('; ');

export function keyResult(mode, sol) {
  const ch = sol.chambers;
  const foreign = sol.routing.foreign_medium;
  const label = MODES[mode]?.label ?? 'Custom';
  const ventOut = Object.values(sol.ports).filter((p) => p.state === 'VENT').reduce((a, p) => a - p.into_chip_ul_min, 0);
  const culture = ['n_cx', 'g_cx', 'a_cx'];
  if (mode === 'grow') return Object.keys(foreign).length ? `${label}: NOT isolated: ${foreignText(foreign)}` : `${label}: no medium enters another chamber; ${ventOut.toFixed(1)} µL/min leaves p_mid`;
  if (mode in { int_n: 1, int_g: 1, int_a: 1 }) {
    const src = { int_n: 'n_cx', int_g: 'g_cx', int_a: 'a_cx' }[mode];
    const others = culture.filter((c) => c !== src);
    const tot = others.reduce((a, c) => a + ch[c].throughput_ul_min, 0) || 1;
    return `${label}: ${src} medium splits at j_mid: ${others.map((c) => `${pct(ch[c].throughput_ul_min / tot)} to ${c}`).join(' / ')}`;
  }
  if (mode === 'harvest') {
    const dead = culture.filter((c) => ch[c].throughput_ul_min < 1e-9);
    return dead.length ? `${label}: only ${culture.filter((c) => !dead.includes(c)).join(', ')} is swept to p_mid; ${dead.join(', ')} see no flow (the protocol's collect mode back-flushes them)` : `${label}: all three chambers sweep to p_mid`;
  }
  if (mode === 'collect') return `${label}: all three chambers sweep into j_mid and pool out of p_mid (${ventOut.toFixed(1)} µL/min)`;
  if (mode === 'seed') return `${label}: n_cx feeds g_cx and a_cx through j_mid; most cells settle in n_cx first`;
  if (mode === 'idle') return 'Idle: every port capped, no flow; the chambers are joined by open channels, so only diffusion crosses';
  return Object.keys(foreign).length ? `Custom: ${foreignText(foreign)}` : 'Custom: no chamber receives another chamber\'s medium';
}

// The same rules as operator/module1_operator.py check().
// cells: false for the dye demo (no cells, so chamber shear can't harm anything: a warning, not a block).
export function safetyCheck(net, mode, states, flows, { cells = true } = {}) {
  const blocked = [];
  const warnings = [];
  let sol;
  try { sol = net.solve(solverPorts(states, flows)); } catch (e) { return { status: 'blocked', reasons: [e.message], warnings: [], sol: null }; }
  const foreign = sol.routing.foreign_medium;
  if (mode === 'grow' && Object.keys(foreign).length) blocked.push(`separate growth must keep the media apart, but ${foreignText(foreign)}`);
  for (const c of ['n_cx', 'g_cx', 'a_cx']) {
    const tau = sol.chambers[c].shear_dyn_cm2;
    if (tau > SHEAR_CEILING) {
      const msg = `${c} wall shear ${tau.toFixed(4)} dyn/cm² > ${SHEAR_CEILING} (throughput ${sol.chambers[c].throughput_ul_min.toFixed(1)} µL/min)`;
      if (cells) blocked.push(msg); else warnings.push(`${msg}: fine for the dye demo (no cells), too high with cells`);
    }
  }
  for (const [port, s] of Object.entries(states)) {
    if (s !== 'FEED') continue;
    const q = flows[port] ?? 0;
    const alt = { ...states, [port]: q <= 0 ? 'VENT' : 'CAP' };
    try {
      const other = net.solve(solverPorts(alt, flows));
      if (q <= 0) {
        const back = other.ports[port].into_chip_ul_min;
        if (back < -1e-6) warnings.push(`${port} is FEED at 0 µL/min: with its pump stopped the line is open, and ${(-back).toFixed(2)} µL/min flows back into it`);
      } else {
        const pb = other.pressure_pa[port];
        const pf = sol.pressure_pa[port];
        if (pf > 0 && pb >= 0.7 * pf) warnings.push(`${port}: the rest of the chip pushes back at ${(pb / 100).toFixed(4)} mbar, ${pct(pb / pf)} of its own; if its pump stalls or it is pressure-fed, flow reverses into it`);
      }
    } catch { /* no other solution to compare */ }
  }
  if (MODES[mode]) {
    const want = MODES[mode].vent;
    const extra = Object.entries(states).filter(([p, s]) => s === 'VENT' && !want.includes(p)).map(([p]) => p);
    if (extra.length && want.length) warnings.push(`${extra.join(', ')} also vented: ${MODES[mode].label.toLowerCase()} should vent only ${want.join(', ')}`);
  }
  return { status: blocked.length ? 'blocked' : warnings.length ? 'warning' : 'approved', reasons: blocked, warnings, sol };
}

// Seconds for each fed port's dye to reach j_mid and each vented port (plug flow; first tint ~2/3).
// chambers: also each culture chamber it fills, from the dye entering (first_s) to the chamber
// swept once at plug flow (mean_s). Used by the bench demo (twin/bench.js); the default output is
// the same as operator/network.py's.
export function dyeTimes(net, sol, states, flows, { chambers = false } = {}) {
  const edges = [];
  for (const e of net.edges) {
    const c = sol.channels[e.id];
    if (Math.abs(c.flow_ul_min) < 1e-9) continue;
    edges.push([c.from, c.to, (c.volume_ul / Math.abs(c.flow_ul_min)) * 60]);
  }
  const out = {};
  const targets = ['j_mid', ...(chambers ? net.culture : []), ...Object.entries(states).filter(([, s]) => s === 'VENT').map(([p]) => p)];
  for (const [src, s] of Object.entries(states)) {
    if (s !== 'FEED' || !(flows[src] > 0)) continue;
    const best = { [src]: (PORT_HOLE_UL / flows[src]) * 60 };
    const enter = {};
    const queue = [src];
    while (queue.length) {
      queue.sort((a, b) => best[a] - best[b]);
      const n = queue.shift();
      for (const [a, b, dt] of edges) {
        if (a !== n) continue;
        const ch = net.chambers[b];
        const extra = ch && b !== 'j_mid' && sol.chambers[b].throughput_ul_min > 0 ? (ch.volume_ul / sol.chambers[b].throughput_ul_min) * 60 : 0;
        const tb = best[n] + dt + extra;
        if (tb < (best[b] ?? Infinity)) { best[b] = tb; enter[b] = best[n] + dt; queue.push(b); }
      }
    }
    out[src] = Object.fromEntries(targets.filter((tg) => tg in best && tg !== src).map((tg) => [tg, net.culture.includes(tg)
      ? { mean_s: best[tg], first_s: enter[tg], chamber: true }
      : { mean_s: best[tg], first_s: best[tg] * FIRST_TINT }]));
  }
  return out;
}

// Small-molecule diffusion when the pumps stop (design doc: ~8.8 mm in 12 h).
export const DIFFUSIVITY = (8.8e-3) ** 2 / (2 * 12 * 3600);
export function diffusionMm(seconds) { return Math.sqrt(2 * DIFFUSIVITY * Math.max(0, seconds)) * 1000; }

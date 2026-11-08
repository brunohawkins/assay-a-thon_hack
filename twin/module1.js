// Module 1 v1 (MJ's valve-free culture chip, chip/module1_v1.json): its flow twin.
//
// The chip is drawn from its spec with channel paths taken from the embossing DXF
// (cad/exports/module1_v1/module1_v1.dxf) and its STLs where present, at real scale. The
// liquid is driven by twin/network.js (the same model as operator/network.py): pick a mode
// (or set each port to FEED / VENT / CAP), the network is solved, and dye is advected along
// the solved flows. Junctions mix by flow fraction; when every pump stops, colours spread by
// diffusion (design doc: ~8.8 mm in 12 h). Physical demo mode runs in real time with a
// stopwatch and the bench syringe rates, and predicts when the dye reaches j_mid and each
// outlet. During protocol playback each step switches the mode itself.
import * as THREE from 'three';
import { CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';
import {
  Network, parseDxfChannels, MODES, DESIGN_MODES, modeStates, solverPorts, keyResult, safetyCheck, dyeTimes,
  diffusionMm, shearCeiling, defaultFeed,
} from './network.js';
import { contracts } from './contracts.js';

export const CHIP_ID = 'module1_v1';
const PORTS = ['p_in', 'p_g_in', 'p_a_in', 'p_mid', 'p_g_out', 'p_a_out'];
const INLET_OF = { n_cx: 'p_in', g_cx: 'p_g_in', a_cx: 'p_a_in' };
// Physical demo colours: n = red, g = yellow, a = blue. A port feeds its chamber's colour.
const DYE_RGB = [[0.94, 0.27, 0.27], [0.98, 0.8, 0.08], [0.23, 0.51, 0.96]];
const DYE_OF_PORT = { p_in: 0, p_g_in: 1, p_g_out: 1, p_a_in: 2, p_a_out: 2 };
const DYE_NAMES = ['red', 'yellow', 'blue'];
const CELLS = 14;                      // cells per channel in the advection grid
const D_MM2_S = (8.8 ** 2) / (2 * 12 * 3600); // mm^2/s from the design doc's 8.8 mm in 12 h
// uL/min. NOT the old hard-coded 11 from build_module1.py, which went stale the moment the
// chip was rescaled: prepare() derives it from THIS chip with network.js defaultFeed(), the
// same rule as operator/network.py's DEFAULT_FEED_UL_MIN, off the contract shear ceiling.
let PROTOCOL_FEED = 2.4;
const SPEEDS = [1, 10, 100, 1000, 10000];
const CHAMBER_INFO = { n_cx: 'neuron chamber', g_cx: 'glia (microglia) chamber', a_cx: 'astrocyte chamber', j_mid: 'junction (not a culture chamber)' };

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmtT = (s) => (s >= 3600 ? `${(s / 3600).toFixed(1)} h` : s >= 60 ? `${Math.floor(s / 60)} min ${String(Math.round(s % 60)).padStart(2, '0')} s` : `${s.toFixed(0)} s`);
const mmss = (s) => { const t = Math.round(s); return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}`; };

function dyeColor(c, out = new THREE.Color()) {
  // subtractive mix: white minus each dye's absorbance, weighted by its concentration
  let r = 1, g = 1, b = 1;
  for (let k = 0; k < 3; k++) {
    r -= c[k] * (1 - DYE_RGB[k][0]);
    g -= c[k] * (1 - DYE_RGB[k][1]);
    b -= c[k] * (1 - DYE_RGB[k][2]);
  }
  return out.setRGB(Math.max(0, r), Math.max(0, g), Math.max(0, b), THREE.SRGBColorSpace);
}
const amount = (c) => Math.min(1, c[0] + c[1] + c[2]);

// Protocol step -> mode and port states (build_module1.py: one lane fed at a time in growth).
export function stepMode(ev) {
  const n = ev.narration ?? '';
  if (/^Idle/.test(n)) return { mode: 'idle', ...modeStates('idle', 0) };
  if (/^Mode 3, neurons/.test(n)) return { mode: 'int_n', ...modeStates('int_n', ev.args?.flow_ul_min ?? PROTOCOL_FEED) };
  if (/^Mode 3, glia/.test(n)) return { mode: 'int_g', ...modeStates('int_g', ev.args?.flow_ul_min ?? PROTOCOL_FEED) };
  if (/^Mode 3, astrocytes/.test(n)) return { mode: 'int_a', ...modeStates('int_a', ev.args?.flow_ul_min ?? PROTOCOL_FEED) };
  if (/^Collect/.test(n)) return { mode: 'collect', ...modeStates('collect', PROTOCOL_FEED) };
  if (/^Mode 1/.test(n)) return { mode: 'seed', ...modeStates('seed', PROTOCOL_FEED) };
  const target = ev.args?.target;
  const inlet = INLET_OF[target];
  const states = Object.fromEntries(PORTS.map((p) => [p, 'CAP']));
  states.p_mid = 'VENT';
  const flows = {};
  if (inlet) { states[inlet] = 'FEED'; flows[inlet] = ev.args?.flow_ul_min ?? PROTOCOL_FEED; }
  return { mode: 'grow', states, flows };
}

export function createModule1View({ ctx, ui, overlays, root, operatorKinds = ['llm', 'scripted'] }) {
  let chip = null;
  let net = null;
  let attached = false;
  let state = null;        // { mode, states, flows }
  let sol = null;
  let verdict = null;
  let sim = null;          // advection grid
  let speed = 100;
  let demo = false;
  let follow = true;       // follow the protocol while it plays
  let stopwatch = { running: false, t: 0 };
  let simT = 0;
  let idleT = 0;
  let decisions = [];
  let measured = {};
  let flagged = null;      // presenter: { ids, t } parts pulsing red (a refused request)
  let asbuilt = null;      // cad/exports/module1_v1/asbuilt.json, when the as-built CAD differs (tools/check_asbuilt.py)
  // Dye-demo syringe rate. contracts/ carries no parameter for it yet, so contracts.js hands
  // back the operator's value and lists it in `missing`; the panel names where it came from.
  const bench = Object.fromEntries(PORTS.map((p) => [p, contracts().benchFlowUlMin]));
  const group = new THREE.Group();
  const objs = { arrows: null, cells: null, portIcons: {}, m2: null, disc: null, discLabel: null };
  const panel = $('m1-panel');
  const tooltip = $('m1-tooltip');
  const raycaster = new THREE.Raycaster();
  const mouse = new THREE.Vector2();
  let hoverTargets = [];

  try { measured = JSON.parse(localStorage.getItem('m1v1-measured') ?? '{}'); } catch { measured = {}; }

  // The chip's own top layer, so "the N mm lid" is never a second copy of a number.
  const lidMm = () => net?.chip.layers?.at(-1)?.thickness_mm ?? '?';

  // ---------------------------------------------------------------- loading

  async function prepare(spec) {
    // The port bore is not fetched or configured here: twin/contracts.js has already read it
    // from contracts/blocks.json, the same file operator/network.py and the CAD generator read,
    // and each Network carries it (net.portHoleMm / net.portHoleUl) with this chip's own lid.
    PROTOCOL_FEED = defaultFeed(spec);
    // What was printed wins: if tools/check_asbuilt.py found the as-built CAD differs from the spec,
    // draw and solve on its normalised outline. The listing is read first, so no 404 when there is none.
    asbuilt = null;
    let dxfName = `${CHIP_ID}.dxf`;
    try {
      const listing = await (await fetch(`${root}cad/exports/${CHIP_ID}/`, { cache: 'no-store' })).text();
      if (listing.includes('asbuilt.json')) {
        asbuilt = await (await fetch(`${root}cad/exports/${CHIP_ID}/asbuilt.json`, { cache: 'no-store' })).json();
        if (asbuilt?.dxf) dxfName = asbuilt.dxf;
      }
    } catch { asbuilt = null; }
    const res = await fetch(`${root}cad/exports/${CHIP_ID}/${dxfName}`, { cache: 'no-store' });
    const rects = res.ok ? parseDxfChannels(await res.text()) : [];
    net = new Network(spec, rects);
    // Draw-only additions (the spec file itself is not changed): channel paths from the DXF,
    // the port holes at the contract bore, and a module block so ports and cells draw as on
    // module chips.
    const view = structuredClone(spec);
    for (const ch of view.channels) {
      const g = net.geometry[ch.id];
      ch.path = [g.a, g.b];
    }
    for (const p of view.ports) p.width_um = net.portHoleMm * 1000;
    view.module = { id: 'm1v1', name: 'Module 1 v1', status: 'designed', printed: true };
    return { view, dxf: rects.length };
  }

  function attach(c) {
    chip = c;
    chip.flowOwned = true;
    // The port icons carry the port names; drop the chip's own port labels so they don't double up.
    const portIds = new Set(c.spec.ports.map((p) => p.id));
    c.labels = c.labels.filter((l) => {
      if (!portIds.has(l.element.textContent)) return true;
      l.removeFromParent();
      l.element.remove();
      return false;
    });
    // STLs are loaded by main.js, from the folder this chip OWNS (it awaits loadChipCad before
    // attaching, so they are already in place here). The chamber STLs are drawn over the
    // liquid, so keep them nearly clear and unlit: that is what makes the dye colours read true.
    for (const ch of c.spec.chambers) {
      const stl = c.parts.get(ch.id)?.stl;
      if (stl) { stl.material.opacity = 0.07; stl.material.emissiveIntensity = 0; }
    }
    buildOverlays();
    attached = true;
    panel.hidden = false;
    document.body.classList.add('has-m1');
    renderPanel();
    if (!state) setMode('grow', PROTOCOL_FEED, { source: 'start' });
    ctx.renderer.domElement.addEventListener('pointermove', onHover);
    ctx.renderer.domElement.addEventListener('pointerleave', hideTip);
    loadDecisions();
  }

  function detach() {
    if (!attached) return;
    setFlag(null);
    attached = false;
    panel.hidden = true;
    tooltip.hidden = true;
    document.body.classList.remove('has-m1');
    overlays.setStopwatch?.(null);
    ctx.renderer.domElement.removeEventListener('pointermove', onHover);
    ctx.renderer.domElement.removeEventListener('pointerleave', hideTip);
    group.removeFromParent();
    group.traverse((o) => { o.geometry?.dispose?.(); o.material?.dispose?.(); if (o.isCSS2DObject) o.element.remove(); });
    group.clear();
    chip = null;
  }

  // The operator's decisions: an LLM run if one was recorded, else the scripted (offline) one.
  // The folder listing is read first, so a missing file causes no 404.
  async function loadDecisions() {
    let listing = '';
    try { listing = await (await fetch(`${root}runs/module1_v1/`, { cache: 'no-store' })).text(); } catch { return; }
    for (const kind of operatorKinds) {
      if (!listing.includes(`operator_${kind}.json`)) continue;
      try {
        const res = await fetch(`${root}runs/module1_v1/operator_${kind}.json`, { cache: 'no-store' });
        if (res.ok) { const d = await res.json(); decisions = d.decisions.map((x) => ({ ...x, operator: kind })); renderPanel(); return; }
      } catch { /* none */ }
    }
  }

  // ---------------------------------------------------------------- 3D overlays

  function buildOverlays() {
    group.clear();
    chip.group.add(group);
    const top = chip.top;
    const k = Math.min(1, chip.footprint.x / 140); // overlays were drawn for the 140 mm board; scale with the chip
    const zc = chip.floorZ0 + 0.16;
    // Advection cells along every channel: one instance per cell, coloured by its dye.
    const nCh = net.edges.length;
    const cellGeom = new THREE.BoxGeometry(1, 1, 0.22);
    const cellMat = new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.95, depthWrite: false });
    objs.cells = new THREE.InstancedMesh(cellGeom, cellMat, nCh * CELLS);
    objs.cells.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(nCh * CELLS * 3), 3);
    const m = new THREE.Matrix4();
    const q = new THREE.Quaternion();
    net.edges.forEach((e, k) => {
      const a = new THREE.Vector3(e.a[0], e.a[1], zc);
      const b = new THREE.Vector3(e.b[0], e.b[1], zc);
      q.setFromUnitVectors(new THREE.Vector3(1, 0, 0), b.clone().sub(a).normalize());
      const len = a.distanceTo(b) / CELLS;
      for (let i = 0; i < CELLS; i++) {
        const pos = a.clone().lerp(b, (i + 0.5) / CELLS);
        m.compose(pos, q, new THREE.Vector3(len * 1.02, e.width_mm * 0.92, 1));
        objs.cells.setMatrixAt(k * CELLS + i, m);
      }
    });
    objs.cells.renderOrder = 7;
    group.add(objs.cells);

    // Flow arrows: one cone per channel at its middle, above the lid.
    const coneGeom = new THREE.ConeGeometry(0.9 * k, 2.4 * k, 18).rotateZ(-Math.PI / 2); // points along +x
    objs.arrows = net.edges.map((e) => {
      const mesh = new THREE.Mesh(coneGeom, new THREE.MeshStandardMaterial({ color: 0xffffff, emissive: 0x5eead4, emissiveIntensity: 0.5, transparent: true, opacity: 0.9 }));
      mesh.position.set((e.a[0] + e.b[0]) / 2, (e.a[1] + e.b[1]) / 2, top + 0.9 * k);
      mesh.renderOrder = 8;
      group.add(mesh);
      return mesh;
    });

    // Port icons (syringe = FEED, open = VENT, plug = CAP).
    for (const p of net.chip.ports) {
      const el = document.createElement('div');
      el.className = 'port-icon';
      const obj = new CSS2DObject(el);
      obj.center.set(0.5, 1.15);
      obj.position.set(p.position[0], p.position[1], top + 1.2 * k);
      group.add(obj);
      objs.portIcons[p.id] = obj;
    }

    // Next-module box: where harvest flow from p_mid goes (protocols/pipeline_mvp.json: cell prep).
    const pm = net.chip.ports.find((p) => p.id === 'p_mid').position;
    // Off the board, in front of it (the board's y = 0 edge faces the camera).
    const boxPos = new THREE.Vector3(pm[0], -16 * k, top + 1 * k);
    const tubeCurve = new THREE.CatmullRomCurve3([new THREE.Vector3(pm[0], pm[1], top), new THREE.Vector3(pm[0], pm[1], top + 5 * k),
      new THREE.Vector3(pm[0] + 3 * k, 4 * k, top + 5 * k), boxPos.clone().setY(boxPos.y + 6 * k)]);
    const tubeMat = new THREE.MeshStandardMaterial({ color: 0xcbd5e1, emissive: 0x334155, emissiveIntensity: 0.4, transparent: true, opacity: 0.7 });
    const tube = new THREE.Mesh(new THREE.TubeGeometry(tubeCurve, 80, Math.max(0.45 * k, net.portHoleMm / 2), 10, false), tubeMat);
    group.add(tube);
    const boxMat = new THREE.MeshStandardMaterial({ color: 0x1e293b, emissive: 0x2dd4bf, emissiveIntensity: 0.12, transparent: true, opacity: 0.75 });
    const box = new THREE.Mesh(new THREE.BoxGeometry(26 * k, 10 * k, 4 * k), boxMat);
    box.position.copy(boxPos);
    group.add(box);
    const lab = document.createElement('div');
    lab.className = 'part-label m2-box-label';
    lab.textContent = 'on to cell prep (cell_prep_v1)';
    const labObj = new CSS2DObject(lab);
    labObj.center.set(0.5, 0);
    labObj.position.copy(boxPos).setY(boxPos.y - 6 * k);
    group.add(labObj);
    objs.m2 = { tubeMat, boxMat };

    // Diffusion disc around j_mid (shown while every port is capped).
    const j = net.chip.chambers.find((c) => c.id === 'j_mid');
    objs.disc = new THREE.Mesh(new THREE.CircleGeometry(1, 64), new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.32, depthWrite: false }));
    objs.disc.position.set(j.center[0], j.center[1], zc + 0.12);
    objs.disc.visible = false;
    group.add(objs.disc);
    const dl = document.createElement('div');
    dl.className = 'part-label diffusion-label';
    objs.discLabel = new CSS2DObject(dl);
    objs.discLabel.center.set(0.5, 0);
    objs.discLabel.position.set(j.center[0], j.center[1] - 5 * k, top + 0.4 * k);
    objs.discLabel.visible = false;
    group.add(objs.discLabel);

    // Hover: chambers, channels and ports carry what they are.
    hoverTargets = [];
    for (const part of chip.parts.values()) {
      const s = part.spec;
      part.box.userData.info = part.kind === 'chamber'
        ? `<b>${s.id}</b> · ${CHAMBER_INFO[s.id] ?? s.type}<br>${s.size[0]} × ${s.size[1]} × ${s.size[2]} mm · ${s.volume_ul} µL${s.mesh_pore_um ? ` · ${s.mesh_pore_um} µm mesh (bought membrane?)` : ' · no mesh'}`
        : `<b>${s.id}</b> · port: ${net.portHoleMm} mm hole through the ${lidMm()} mm lid (${net.portHoleUl.toFixed(2)} µL)<br>at (${s.position[0]}, ${s.position[1]}) mm`;
      part.box.userData.port = part.kind === 'port' ? s.id : null;
      hoverTargets.push(part.box);
    }
    for (const ch of chip.channels) {
      const g = net.geometry[ch.id];
      for (const seg of ch.group.children) {
        seg.userData.info = `<b>${ch.id}</b> · channel ${ch.from} → ${ch.to}<br>${g.width_mm.toFixed(2)} × ${g.depth_mm} mm · ${g.length_mm.toFixed(2)} mm long (${g.source}) · ${(g.length_mm * g.width_mm * g.depth_mm).toFixed(2)} µL`;
        seg.userData.channel = ch.id;
        hoverTargets.push(seg);
      }
    }
    sim = newSim();
    snapSteady();
  }

  function onHover(e) {
    if (!attached) return;
    const r = ctx.renderer.domElement.getBoundingClientRect();
    mouse.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    raycaster.setFromCamera(mouse, ctx.camera);
    const hit = raycaster.intersectObjects(hoverTargets, false)[0];
    if (!hit) { hideTip(); return; }
    const d = hit.object.userData;
    let extra = '';
    if (d.channel && sol) {
      const c = sol.channels[d.channel];
      extra = `<br>now: ${Math.abs(c.flow_ul_min).toFixed(2)} µL/min ${c.flow_ul_min ? `${c.from} → ${c.to}` : '(no flow)'} · shear ${c.shear_dyn_cm2.toFixed(3)} dyn/cm²`;
    } else if (d.port && state) {
      extra = `<br>now: ${state.states[d.port]}${state.states[d.port] === 'FEED' ? ` ${state.flows[d.port]} µL/min` : ''}`;
    }
    tooltip.innerHTML = d.info + extra;
    tooltip.style.left = `${e.clientX + 14}px`;
    tooltip.style.top = `${e.clientY + 12}px`;
    tooltip.hidden = false;
  }
  function hideTip() { tooltip.hidden = true; }

  // ---------------------------------------------------------------- dye simulation

  function newSim() {
    const node = {};
    for (const c of net.chip.chambers) node[c.id] = { c: [0, 0, 0], v: c.volume_ul };
    for (const p of net.chip.ports) node[p.id] = { c: [0, 0, 0], v: net.portHoleUl };
    const cells = net.edges.map(() => Array.from({ length: CELLS }, () => [0, 0, 0]));
    return { node, cells };
  }

  function step(dt) {
    if (!sim || !sol) return;
    const flows = net.edges.map((e) => sol.channels[e.id].flow_ul_min / 60);   // uL/s
    let cfl = 0;
    net.edges.forEach((e, k) => {
      const vc = (e.length_mm * e.width_mm * e.depth_mm) / CELLS;
      cfl = Math.max(cfl, (Math.abs(flows[k]) * dt) / vc, (D_MM2_S * dt) / (e.length_mm / CELLS) ** 2 * 2);
    });
    for (const [id, n] of Object.entries(sim.node)) {
      const qin = net.edges.reduce((a, e, k) => a + ((e.to === id && flows[k] > 0) || (e.from === id && flows[k] < 0) ? Math.abs(flows[k]) : 0), 0);
      cfl = Math.max(cfl, (qin * dt) / n.v);
    }
    const nSub = Math.max(1, Math.ceil(cfl / 0.45));
    if (nSub > 20000) { snapSteady(); return; } // that long at this flow: everything has long reached steady state
    const h = dt / nSub;
    for (let s = 0; s < nSub; s++) substep(h, flows);
  }

  function substep(h, flows) {
    const feeds = state.states;
    for (const [pid, st] of Object.entries(feeds)) {
      if (st === 'FEED' && (state.flows[pid] ?? 0) > 0) {
        const k = DYE_OF_PORT[pid];
        sim.node[pid].c = [0, 0, 0];
        if (k !== undefined) sim.node[pid].c[k] = 1;
      }
    }
    const nodeIn = Object.fromEntries(Object.keys(sim.node).map((id) => [id, [0, 0, 0, 0]])); // sum Q*c, sum Q
    net.edges.forEach((e, k) => {
      const q = flows[k];
      const cells = sim.cells[k];
      const vc = (e.length_mm * e.width_mm * e.depth_mm) / CELLS;
      // advection (upwind)
      if (Math.abs(q) > 1e-12) {
        const fwd = q > 0;
        const up = sim.node[fwd ? e.from : e.to].c;
        const f = (Math.abs(q) * h) / vc;
        const order = fwd ? [...cells.keys()] : [...cells.keys()].reverse();
        const prev = order.map((i) => cells[i].slice());
        order.forEach((i, j) => {
          const inc = j === 0 ? up : prev[j - 1];
          for (let d = 0; d < 3; d++) cells[i][d] += f * (inc[d] - prev[j][d]);
        });
        const outC = cells[order[order.length - 1]];
        const dn = nodeIn[fwd ? e.to : e.from];
        for (let d = 0; d < 3; d++) dn[d] += Math.abs(q) * outC[d];
        dn[3] += Math.abs(q);
      }
      // diffusion along the channel and into the parts at its ends
      const dx = e.length_mm / CELLS;
      const kd = (D_MM2_S * h) / (dx * dx);
      const ends = [sim.node[e.from], sim.node[e.to]];
      const old = cells.map((c) => c.slice());
      for (let i = 0; i < CELLS; i++) {
        const left = i === 0 ? ends[0].c : old[i - 1];
        const right = i === CELLS - 1 ? ends[1].c : old[i + 1];
        for (let d = 0; d < 3; d++) cells[i][d] += kd * (left[d] - 2 * old[i][d] + right[d]);
      }
      for (const [end, i] of [[ends[0], 0], [ends[1], CELLS - 1]]) {
        if (feeds[e.from] === 'FEED' && end === ends[0]) continue;
        for (let d = 0; d < 3; d++) end.c[d] += (kd * (old[i][d] - end.c[d]) * vc) / end.v;
      }
    });
    for (const [id, n] of Object.entries(sim.node)) {
      if (feeds[id] === 'FEED') continue;
      const [a, b, c, q] = nodeIn[id];
      if (q > 0) {
        const k = (q * h) / n.v;
        n.c[0] += k * (a / q - n.c[0]);
        n.c[1] += k * (b / q - n.c[1]);
        n.c[2] += k * (c / q - n.c[2]);
      }
    }
  }

  // Jump to the steady colours of the current state (after a seek, or at the start).
  function snapSteady() {
    if (!sim || !sol) return;
    const portDye = (pid) => { const c = [0, 0, 0]; if (DYE_OF_PORT[pid] !== undefined) c[DYE_OF_PORT[pid]] = 1; return c; };
    const mixOf = (node) => {
      const mix = sol.routing.inflow_mix[node];
      if (!mix) return null;
      const c = [0, 0, 0];
      for (const [tag, f] of Object.entries(mix)) {
        const src = tag in sim.node && net.culture.includes(tag) ? (sol.routing.inflow_mix[tag] ? resolve(tag) : [0, 0, 0]) : portDye(tag);
        for (let d = 0; d < 3; d++) c[d] += f * src[d];
      }
      return c;
    };
    const memo = {};
    function resolve(n) { if (!memo[n]) memo[n] = mixOf(n) ?? [0, 0, 0]; return memo[n]; }
    for (const id of Object.keys(sim.node)) { const c = mixOf(id); if (c) sim.node[id].c = c; }
    net.edges.forEach((e, k) => {
      const ch = sol.channels[e.id];
      if (!ch.flow_ul_min) return;
      const up = state.states[ch.from] === 'FEED' ? portDye(ch.from) : net.culture.includes(ch.from) ? resolve(ch.from) : sim.node[ch.from].c;
      sim.cells[k] = sim.cells[k].map(() => up.slice());
    });
  }

  // ---------------------------------------------------------------- modes and ports

  function setMode(mode, flow, { source = 'panel', states: st, flows: fl } = {}) {
    const ms = st ? { states: st, flows: fl } : modeStates(mode, flow);
    apply({ mode, ...ms }, source);
  }

  function apply(next, source = 'panel') {
    const v = net ? safetyCheck(net, next.mode, next.states, next.flows, { cells: !demo }) : null;
    verdict = v ? { ...v, source } : null;
    if (v && v.status === 'blocked' && source !== 'protocol') { renderPanel(); return false; }
    state = next;
    sol = v?.sol ?? null;
    if (next.mode === 'idle' || !sol) idleT = 0;
    if (demo && source !== 'protocol') { stopwatch = { running: true, t: 0 }; }
    renderPanel();
    return true;
  }

  // ---------------------------------------------------------------- frame

  function update(dt, playerPlaying) {
    if (!attached || !sol) return;
    const simDt = following(playerPlaying) ? 0 : dt * speed;
    if (simDt > 0) advance(simDt);
    if (stopwatch.running) stopwatch.t += dt * (demo ? 1 : speed);
    draw();
    if (flagged) pulseFlagged(dt);
    overlays.setStopwatch?.(demo ? { t: stopwatch.t, running: stopwatch.running } : null);
  }

  function advance(simDt) {
    step(simDt);
    simT += simDt;
    if (!Object.values(sol.channels).some((c) => c.flow_ul_min)) idleT += simDt;
  }

  const following = (playing) => follow && playing;

  // Presenter: parts named in a refused request pulse red until cleared.
  function pulseFlagged(dt) {
    flagged.t += dt;
    const k = 0.5 + 0.5 * Math.sin(flagged.t * 5);
    for (const id of flagged.ids) {
      const part = chip?.parts.get(id);
      if (!part?.glow) continue;
      part.glow.visible = true;
      part.glowMaterial.color.setRGB(0.97, 0.2, 0.2);
      part.glowMaterial.opacity = 0.25 + 0.4 * k;
    }
  }
  function setFlag(ids) {
    for (const id of flagged?.ids ?? []) { const part = chip?.parts.get(id); if (part?.glow) part.glow.visible = false; }
    flagged = ids?.length ? { ids, t: 0 } : null;
  }

  // Protocol playback: the player says which step and how far into it.
  let lastEv = -1;
  function onEvent(i, ev, playing) {
    if (!attached || !ev || !follow) return;
    if (i === lastEv) return;
    lastEv = i;
    const sm = stepMode(ev);
    apply(sm, 'protocol');
    if (!playing) snapSteady();
  }
  let lastProto = null;
  function onTick(info, playing) {
    if (!attached || !follow || !playing) { lastProto = info.protocolT; return; }
    if (lastProto !== null && info.protocolT > lastProto) advance(Math.min(info.protocolT - lastProto, 7 * 86400));
    lastProto = info.protocolT;
  }
  function onSeek() { if (attached) snapSteady(); }

  function draw() {
    const col = new THREE.Color();
    net.edges.forEach((e, k) => {
      for (let i = 0; i < CELLS; i++) {
        const c = sim.cells[k][i];
        const a = amount(c);
        dyeColor(c, col);
        col.lerp(new THREE.Color(0x9fb4d1), 1 - a);
        objs.cells.setColorAt(k * CELLS + i, col);
      }
      const ch = sol.channels[e.id];
      const arrow = objs.arrows[k];
      const qa = Math.abs(ch.flow_ul_min);
      arrow.visible = qa > 1e-6;
      if (arrow.visible) {
        const dir = new THREE.Vector3(e.b[0] - e.a[0], e.b[1] - e.a[1], 0).normalize().multiplyScalar(ch.flow_ul_min > 0 ? 1 : -1);
        arrow.quaternion.setFromUnitVectors(new THREE.Vector3(1, 0, 0), dir);
        arrow.scale.setScalar(0.45 + 0.55 * Math.min(1, Math.log10(1 + qa) / Math.log10(1 + 60)));
        const mid = sim.cells[k][Math.floor(CELLS / 2)];
        arrow.material.color.copy(dyeColor(mid, col).lerp(new THREE.Color(0xffffff), 1 - amount(mid)));
      }
    });
    objs.cells.instanceColor.needsUpdate = true;
    for (const c of net.chip.chambers) {
      const part = chip.chamber(c.id);
      if (!part) continue;
      const conc = sim.node[c.id].c;
      const a = amount(conc);
      part.fluid.visible = true;
      part.fluid.scale.z = Math.max(part.size.z * 0.96, 0.0001);
      dyeColor(conc, part.fluidMaterial.color).lerp(new THREE.Color(0x9fb4d1), 1 - a);
      part.fluidMaterial.emissive.copy(part.fluidMaterial.color);
      // A low glow keeps the dye saturated: brighter, the tone mapping washes yellow out to white.
      part.fluidMaterial.emissiveIntensity = 0.18;
      part.fluidMaterial.opacity = 0.3 + 0.62 * a;
    }
    // port icons
    for (const pid of PORTS) {
      const el = objs.portIcons[pid].element;
      const st = state.states[pid];
      const html = `${ICONS[st]}<span>${pid} · ${st}${st === 'FEED' ? ` ${state.flows[pid]} µL/min` : ''}</span>`;
      if (el.dataset.html !== html) { el.innerHTML = html; el.dataset.html = html; el.className = `port-icon ${st.toLowerCase()}`; }
    }
    // Module 2 box: lit while flow leaves p_mid
    const out = sol.ports.p_mid?.into_chip_ul_min ?? 0;
    const pm = sim.node.p_mid.c;
    objs.m2.tubeMat.emissive.copy(dyeColor(pm, col));
    objs.m2.tubeMat.emissiveIntensity = out < -1e-6 ? 0.9 : 0.2;
    objs.m2.boxMat.emissiveIntensity = out < -1e-6 ? 0.35 : 0.12;
    // diffusion disc while nothing flows
    const still = !Object.values(sol.channels).some((c) => c.flow_ul_min);
    objs.disc.visible = still && idleT > 60;
    objs.discLabel.visible = objs.disc.visible;
    if (objs.disc.visible) {
      const r = diffusionMm(idleT);
      objs.disc.scale.setScalar(Math.max(r, 0.5));
      objs.disc.material.color.copy(dyeColor(sim.node.j_mid.c, col));
      objs.discLabel.element.textContent = `diffusion: ~${r.toFixed(1)} mm after ${fmtT(idleT)} (doc: 8.8 mm in 12 h)`;
    }
  }

  const ICONS = {
    FEED: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 21l4-4M6 14l4 4M8 12l8-8 4 4-8 8zM15 3l6 6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    VENT: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="7" fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 2v5" stroke="currentColor" stroke-width="2"/></svg>',
    CAP: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="9" width="12" height="9" rx="2" fill="currentColor"/><rect x="9" y="4" width="6" height="6" rx="1" fill="none" stroke="currentColor" stroke-width="2"/></svg>',
  };

  // ---------------------------------------------------------------- panel

  function renderPanel() {
    if (!panel || !net) return;
    const C = contracts();
    const s = state ?? modeStates('grow', PROTOCOL_FEED);
    const times = sol ? dyeTimes(net, sol, s.states, s.flows) : {};
    const modeBtn = (m) => `<button data-m1mode="${m}" aria-pressed="${state?.mode === m}">${esc(MODES[m].label)}</button>`;
    const v = verdict;
    const portRows = PORTS.map((p) => {
      const st = s.states[p];
      const into = sol?.ports[p]?.into_chip_ul_min ?? 0;
      return `<tr><td>${p}</td><td><select data-port="${p}">${['FEED', 'VENT', 'CAP'].map((x) => `<option${x === st ? ' selected' : ''}>${x}</option>`).join('')}</select></td>
        <td>${st === 'FEED' ? `<input data-rate="${p}" type="number" min="0" step="0.5" value="${s.flows[p] ?? 0}">` : ''}</td>
        <td class="num">${Math.abs(into) > 1e-9 ? `${into > 0 ? 'in' : 'out'} ${Math.abs(into).toFixed(2)}` : '–'}</td></tr>`;
    }).join('');
    const timeRows = Object.entries(times).flatMap(([src, tg]) => Object.entries(tg).map(([t, x]) => {
      const key = `${state?.mode}|${src}|${t}`;
      const m = measured[key];
      const diff = m != null ? ` <span class="${Math.abs(m - x.mean_s) <= 0.35 * x.mean_s ? 'ok' : 'off'}">(${m - x.mean_s >= 0 ? '+' : ''}${Math.round(m - x.mean_s)} s)</span>` : '';
      return `<tr><td><span class="dot" style="background:${dotCss(src)}"></span>${src} → ${t}</td><td class="num">${mmss(x.first_s)}–${mmss(x.mean_s)}</td>
        <td><input data-measured="${key}" placeholder="m:ss" value="${m != null ? mmss(m) : ''}" aria-label="measured time ${src} to ${t}">${diff}</td></tr>`;
    })).join('');
    const chRows = sol ? net.edges.map((e) => {
      const c = sol.channels[e.id];
      return `<tr><td>${e.id}</td><td class="num">${Math.abs(c.flow_ul_min).toFixed(2)}</td><td>${c.flow_ul_min ? `${c.from}→${c.to}` : '–'}</td><td class="num">${c.shear_dyn_cm2.toFixed(3)}</td></tr>`;
    }).join('') : '';
    const ceiling = shearCeiling();
    const chamberRows = sol ? ['n_cx', 'g_cx', 'a_cx', 'j_mid'].map((c) => `<tr><td>${c}</td><td class="num">${sol.chambers[c].throughput_ul_min.toFixed(2)}</td><td></td>
      <td class="num ${c !== 'j_mid' && sol.chambers[c].shear_dyn_cm2 > ceiling ? 'off' : ''}">${sol.chambers[c].shear_dyn_cm2.toFixed(4)}</td></tr>`).join('') : '';
    const decisionRows = decisions.map((d, k) => `<li class="dec ${d.status}" data-decision="${k}" tabindex="0">
        <span class="tag ${d.status}">${d.status}</span> <b>${esc(MODES[d.intent]?.label ?? 'custom')}</b> · ${esc(d.reason)}
        <div class="meta">${esc(d.summary)}</div>${[...(d.reasons ?? []).map((r) => `<div class="blk">✕ ${esc(r)}</div>`), ...(d.warnings ?? []).map((w) => `<div class="wrn">! ${esc(w)}</div>`)].join('')}</li>`).join('');
    panel.innerHTML = `
      <div class="cp-head"><h2>Module 1 v1 · flow</h2>
        <p class="meta">MJ's chip (resin print + hot emboss) · real scale · channels from ${asbuilt ? `the AS-BUILT file ${esc(asbuilt.source_dxf)} (${asbuilt.differences.length} differences from the spec)` : 'module1_v1.dxf'} · same model as operator/network.py (parity checked by hand, not by a test)</p>
        <p class="meta">µ = ${C.mu_pa_s} Pa s (fluid.viscosity_37c_pa_s: culture medium at 37 °C, not the ${C.muWater20cPaS} Pa s water-at-20 °C value) · shear ceiling ${ceiling} dyn/cm² · port bore ${net.portHoleMm} mm — all from contracts/</p>
        ${net.estimated.length ? `<p class="wrn">! no DXF rectangle matched ${esc(net.estimated.join(', '))}: those lengths are straight-line estimates, not cut geometry</p>` : ''}
        ${C.missing.map((m) => `<p class="wrn">! ${esc(m.wanted)} does not exist: ${esc(String(m.name))} = ${m.value} comes from ${esc(m.from)}</p>`).join('')}</div>
      <section><h3>Mode</h3><div class="m1-modes">${DESIGN_MODES.map(modeBtn).join('')}</div>
        <div class="m1-modes sub">${['collect', 'idle'].map(modeBtn).join('')}</div>
        <p class="m1-result">${sol ? esc(keyResult(state?.mode, sol)) : ''}</p>
        ${v ? `<p class="m1-verdict ${v.status}"><span class="tag ${v.status}">${v.status}</span> ${v.status === 'blocked' ? 'not applied' : 'safety checked'}${v.source === 'protocol' ? ' (protocol step)' : ''}</p>
          ${[...v.reasons.map((r) => `<p class="blk">✕ ${esc(r)}</p>`), ...v.warnings.map((w) => `<p class="wrn">! ${esc(w)}</p>`)].join('')}` : ''}
      </section>
      <section><h3>Ports <small>FEED = syringe · VENT = open · CAP = plug</small></h3>
        <table class="m1-ports"><thead><tr><th>port</th><th>state</th><th>µL/min</th><th>flow</th></tr></thead><tbody>${portRows}</tbody></table>
        <p class="meta">max pressure ${sol ? (sol.max_pressure_mbar).toFixed(3) : '–'} mbar</p>
      </section>
      <section><h3>Time</h3>
        <div class="m1-speed">${SPEEDS.map((x) => `<button data-speed-m1="${x}" aria-pressed="${!demo && speed === x}">${x}×</button>`).join('')}</div>
        <label class="m1-toggle"><input type="checkbox" data-demo ${demo ? 'checked' : ''}> Physical demo: real time, bench rates, stopwatch</label>
        <label class="m1-toggle"><input type="checkbox" data-follow ${follow ? 'checked' : ''}> Follow the protocol while it plays</label>
        ${demo ? `<div class="m1-bench">Bench rate for every syringe: <input data-bench type="number" min="1" step="1" value="${bench.p_in}"> µL/min
          <button data-sw>${stopwatch.running ? 'Stop' : 'Start'} stopwatch</button> <button data-swreset>Reset</button></div>` : ''}
      </section>
      <section><h3>Dye arrival, predicted <small>first tint – plug flow, at these rates</small></h3>
        <table class="m1-times"><thead><tr><th>dye</th><th>predicted</th><th>measured</th></tr></thead><tbody>${timeRows || '<tr><td colspan="3">no flow</td></tr>'}</tbody></table>
        <p class="meta">Includes the ${net.portHoleMm} × ${net.portHoleMm} × ${lidMm()} mm inlet hole (${net.portHoleUl.toFixed(2)} µL); tubing to the chip not included.</p>
      </section>
      <details><summary>Solver: every channel</summary>
        <table class="m1-solver"><thead><tr><th></th><th>µL/min</th><th>direction</th><th>shear dyn/cm²</th></tr></thead><tbody>${chRows}${chamberRows}</tbody></table>
      </details>
      <section><h3>AI operator <small>${decisions[0]?.operator === 'llm' ? 'Claude' : 'scripted (offline)'} · ports and rates only</small></h3>
        <ol class="m1-decisions">${decisionRows || '<li class="meta">python operator/module1_operator.py writes runs/module1_v1/operator_scripted.json</li>'}</ol>
      </section>`;
  }

  function dotCss(port) {
    const k = DYE_OF_PORT[port];
    return k === undefined ? '#9fb4d1' : `rgb(${DYE_RGB[k].map((x) => Math.round(x * 255)).join(',')})`;
  }

  panel?.addEventListener('click', (e) => {
    const t = e.target.closest('button, li[data-decision]');
    if (!t) return;
    if (t.dataset.m1mode) { follow = false; handlers.onManual?.(); setMode(t.dataset.m1mode, demo ? bench.p_in : PROTOCOL_FEED); }
    else if (t.dataset.speedM1) { speed = Number(t.dataset.speedM1); demo = false; renderPanel(); }
    else if (t.hasAttribute('data-sw')) { stopwatch.running = !stopwatch.running; renderPanel(); }
    else if (t.hasAttribute('data-swreset')) { stopwatch = { running: false, t: 0 }; renderPanel(); }
    else if (t.dataset.decision !== undefined) {
      const d = decisions[Number(t.dataset.decision)];
      follow = false;
      handlers.onManual?.();
      verdict = null;
      apply({ mode: d.intent, states: d.states, flows: d.flows }, `operator: ${d.operator}`);
    }
  });
  panel?.addEventListener('change', (e) => {
    const t = e.target;
    if (t.dataset.port) {
      const next = structuredClone(state);
      next.states[t.dataset.port] = t.value;
      if (t.value === 'FEED') next.flows[t.dataset.port] = next.flows[t.dataset.port] ?? (demo ? bench.p_in : PROTOCOL_FEED);
      else delete next.flows[t.dataset.port];
      next.mode = Object.keys(MODES).find((m) => JSON.stringify(modeStates(m, 1).states) === JSON.stringify(next.states)) ?? 'custom';
      follow = false;
      handlers.onManual?.();
      apply(next, 'panel');
    } else if (t.dataset.rate) {
      const next = structuredClone(state);
      next.flows[t.dataset.rate] = Math.max(0, Number(t.value));
      follow = false;
      apply(next, 'panel');
    } else if (t.hasAttribute('data-demo')) {
      demo = t.checked;
      if (demo) {
        speed = 1;
        follow = false;
        handlers.onManual?.();
        setMode(state?.mode in MODES && state.mode !== 'idle' ? state.mode : 'grow', bench.p_in);
        stopwatch = { running: false, t: 0 };
      }
      renderPanel();
    } else if (t.hasAttribute('data-follow')) {
      follow = t.checked;
      renderPanel();
    } else if (t.hasAttribute('data-bench')) {
      const r = Math.max(1, Number(t.value));
      for (const p of PORTS) bench[p] = r;
      setMode(state?.mode in MODES ? state.mode : 'grow', r);
    } else if (t.dataset.measured) {
      const m = /^(\d+):(\d{1,2})$/.exec(t.value.trim());
      if (m) measured[t.dataset.measured] = Number(m[1]) * 60 + Number(m[2]);
      else if (!t.value.trim()) delete measured[t.dataset.measured];
      try { localStorage.setItem('m1v1-measured', JSON.stringify(measured)); } catch { /* private window */ }
      renderPanel();
    }
  });

  const handlers = {};
  return {
    prepare, attach, detach, update, onEvent, onTick, onSeek,
    isChip: (spec) => spec?.id === CHIP_ID,
    setHandlers(h) { Object.assign(handlers, h); },
    get attached() { return attached; },
    // for the smoke test
    get state() { return state; },
    get solution() { return sol; },
    get verdict() { return verdict; },
    get net() { return net; },
    setMode: (m, flow = PROTOCOL_FEED) => { follow = false; setMode(m, flow); },
    snapSteady, advance: (s) => advance(s), setDemo(on) { demo = on; if (on) speed = 1; renderPanel(); },
    chamberColor: (id) => sim?.node[id]?.c.slice(),
    // presenter view (twin/presenter.js)
    reset() { if (net) { sim = newSim(); simT = 0; idleT = 0; } },   // clear medium everywhere: dye starts at the feeds
    setSpeed(x) { speed = x; demo = false; renderPanel(); },
    flag: setFlag,
    check: (mode, states, flows) => safetyCheck(net, mode, states, flows, { cells: true }),
    get decisions() { return decisions; },
    get defaultFeed() { return PROTOCOL_FEED; },
    get asbuilt() { return asbuilt; },
    dyeCss: (k) => `rgb(${DYE_RGB[k].map((x) => Math.round(x * 255)).join(',')})`,
  };
}

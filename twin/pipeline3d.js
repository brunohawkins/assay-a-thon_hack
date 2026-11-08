// Pipeline view in 3D: every chip of the pipeline at real scale (the 30 x 20 mm build envelope),
// laid out as a two-row snake, with 1/16 inch tubes from each hand-off port to the next chip's.
// As the pipeline plays, the sample moves port to port through each chip's channels and along the
// tubes (cells, then capsules after encapsulation, then library), and the chambers it passes light
// up. Statuses come from protocols/pipeline_status.md (twin/status.js); a stage with no chip is a
// ghost outline labelled "next"; off-chip steps are labelled on the tube where they happen.
// Click a chip to open its own view. Same playback interface as the old card view.
import * as THREE from 'three';
import { CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';
import { Chip } from './chip.js';
import { applyBlocks } from './blocks.js';
import { statusView } from './status.js';

const SLOT_W = 30;
const SLOT_H = 20;
const GAP_X = 24;
const GAP_Y = 34;
const DOTS = 7;
const TUBE_R = 0.8;    // 1/16 inch tubing: 1.6 mm outer diameter
const DOT_R = 0.55;    // markers, not to scale (a capsule is 0.07-0.26 mm)
const KIND_COLOR = { cells: 0xe5534b, capsules: 0x1b9e8f, library: 0x8250df };
const CAPSULE_CORE = 0xf08a7e;   // a capsule: the cell-pink core inside its teal shell
const TUBE_COLOR = 0x8c959f;

// Short stage names for the labels in the scene; the sidebar lists the full ones.
// Keyed by chip id, or by the pipeline's module id for a stage with no chip.
export const SHORT = { module1_v1: 'Culture', cell_prep_v1: 'Cell prep', spc_demo_v1: 'Encapsulation', module3_v1_translated: 'Lysis, wash, RT',
  module4_v1: 'Index PCR', module5_v1: 'Release, cleanup', sctip_spc_v1: 'Barcoding in oil', bench: 'Permeabilise, tagment' };
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const clamp01 = (x) => Math.min(1, Math.max(0, x));
const ease = (x) => { x = clamp01(x); return x < 0.5 ? 4 * x * x * x : 1 - (-2 * x + 2) ** 3 / 2; };
const firstSentence = (s) => (String(s ?? '').match(/^.*?[.!?](\s|$)/)?.[0] ?? String(s ?? '')).trim();

function label(html, cls) {
  const el = document.createElement('div');
  el.className = `p3-label ${cls ?? ''}`;
  el.innerHTML = html;
  const obj = new CSS2DObject(el);
  obj.center.set(0.5, 1);
  return obj;
}

// A point along a polyline of Vector3s, at fraction f of its length.
function along(points, f) {
  if (!points.length) return null;
  if (points.length === 1) return points[0].clone();
  const seg = [];
  let len = 0;
  for (let i = 1; i < points.length; i++) { const d = points[i].distanceTo(points[i - 1]); seg.push(d); len += d; }
  let d = clamp01(f) * len;
  for (let i = 0; i < seg.length; i++) {
    if (d <= seg[i] || i === seg.length - 1) return points[i].clone().lerp(points[i + 1], seg[i] ? d / seg[i] : 0);
    d -= seg[i];
  }
  return points.at(-1).clone();
}

export function createPipeline3D(root, ctx, handlers = {}) {
  const world = new THREE.Group();
  world.name = 'pipeline3d';
  world.rotation.x = -Math.PI / 2; // chip-plane coordinates, z up, like a chip
  world.visible = false;
  ctx.scene.add(world);

  let slots = [];
  let tubes = [];
  let phases = [];
  let starts = [];
  let total = 1;
  let t = 0;
  let playing = false;
  let speed = 1;
  let stopAt = null;
  let current = -2;
  let visible = false;
  let data = null;
  let size = { x: SLOT_W, y: SLOT_H };
  let loadToken = 0;

  // The sample: a core (cells / library) and a shell (capsules).
  const dotGeom = new THREE.SphereGeometry(DOT_R, 18, 12);
  const shellGeom = new THREE.SphereGeometry(DOT_R * 1.45, 18, 12);
  const dots = Array.from({ length: DOTS }, () => {
    const core = new THREE.Mesh(dotGeom, new THREE.MeshStandardMaterial({ color: KIND_COLOR.cells, emissive: KIND_COLOR.cells, emissiveIntensity: 0.6 }));
    const shell = new THREE.Mesh(shellGeom, new THREE.MeshStandardMaterial({ color: KIND_COLOR.capsules, emissive: KIND_COLOR.capsules,
      emissiveIntensity: 0.4, transparent: true, opacity: 0.45, depthWrite: false }));
    core.add(shell);
    core.renderOrder = 20;
    world.add(core);
    return { core, shell };
  });

  // ---------------------------------------------------------------- building

  function clear() {
    for (const s of slots) {
      if (s.chip) { world.remove(s.chip.group); s.chip.dispose(); }
      for (const o of s.objects ?? []) { world.remove(o); o.traverse?.((x) => { x.geometry?.dispose?.(); x.material?.dispose?.(); if (x.isCSS2DObject) x.element.remove(); }); }
    }
    for (const tb of tubes) for (const o of tb.objects) { world.remove(o); o.traverse?.((x) => { x.geometry?.dispose?.(); x.material?.dispose?.(); if (x.isCSS2DObject) x.element.remove(); }); }
    slots = [];
    tubes = [];
  }

  // Two rows (a snake: the second row runs back), however many stages there are.
  let perRow = 4;
  function place(i, n) {
    const rows = Math.ceil(n / perRow);
    const row = Math.floor(i / perRow);
    let col = i % perRow;
    if (row % 2) col = perRow - 1 - col;
    return { x: col * (SLOT_W + GAP_X), y: (rows - 1 - row) * (SLOT_H + GAP_Y) };
  }

  const portOf = (end, list) => {
    const ids = (end?.ports ?? [end?.port]).filter((p) => p && p !== 'TBC');
    return ids.length ? ids : null;
  };

  async function load(d) {
    const token = ++loadToken;
    data = d;
    clear();
    const { pipe, entries, status } = d;
    const n = entries.length + 1; // + the end of the line (off-chip, library out)
    perRow = Math.max(3, Math.ceil(n / 2));
    const cols = Math.min(perRow, n);
    const rows = Math.ceil(n / perRow);
    size = { x: cols * SLOT_W + (cols - 1) * GAP_X, y: rows * SLOT_H + (rows - 1) * GAP_Y };
    world.position.set(-size.x / 2, 0, size.y / 2);

    // Chips, ghosts and the end vial.
    const built = await Promise.all(entries.map(async (e, k) => {
      const at = place(k, n);
      const slot = { k, entry: e, kind: e.spec ? 'chip' : 'ghost', x: at.x, y: at.y, objects: [] };
      if (e.spec) {
        const chip = new Chip(e.spec);
        chip.group.rotation.set(0, 0, 0);
        chip.group.position.set(at.x + (SLOT_W - e.spec.footprint.x) / 2, at.y + (SLOT_H - e.spec.footprint.y) / 2, 0);
        chip.setLabelsVisible(false);
        chip.group.userData.slot = k;
        await applyBlocks(chip);
        slot.chip = chip;
      }
      return slot;
    }));
    if (token !== loadToken) { for (const s of built) s.chip?.dispose(); return; } // a newer load won
    slots = built;
    for (const s of slots) if (s.chip) world.add(s.chip.group);
    const endAt = place(entries.length, n);
    slots.push({ k: entries.length, kind: 'end', x: endAt.x, y: endAt.y, objects: [], entry: null });

    // Ports in and out of each chip, from the hand-offs (TBC: the chip's first inlet / outlet).
    const into = (m) => pipe.handoffs.find((h) => h.to.module === m);
    const outOf = (m) => pipe.handoffs.find((h) => h.from.module === m);
    const local = (s, id) => {
      const part = s.chip?.parts.get(id);
      return part ? part.center.clone().add(s.chip.group.position) : null;
    };
    for (const s of slots) {
      if (s.kind === 'end') {
        s.inPos = new THREE.Vector3(s.x + SLOT_W / 2, s.y + SLOT_H / 2, 13);
        s.path = [s.inPos.clone()];
        continue;
      }
      const e = s.entry;
      const inH = into(e.module);
      const outH = outOf(e.module);
      s.inH = inH;
      s.outH = outH;
      if (s.chip) {
        const ports = s.chip.spec.ports;
        const firstOf = (type) => ports.find((p) => p.type === type)?.id ?? null;
        s.inPorts = portOf(inH?.to) ?? [firstOf('inlet')].filter(Boolean);
        s.outPort = portOf(outH?.from)?.[0] ?? firstOf('outlet');
        s.inPort = s.inPorts[0] ?? null;
        const top = s.chip.top;
        s.top = top;
        s.inPos = s.inPort ? local(s, s.inPort).setZ(top) : new THREE.Vector3(s.x, s.y + SLOT_H / 2, top);
        s.outPos = s.outPort ? local(s, s.outPort).setZ(top) : new THREE.Vector3(s.x + SLOT_W, s.y + SLOT_H / 2, top);
        // Inside the chip: along its channels from the inlet port to the outlet port.
        const route = s.inPort && s.outPort ? s.chip.route(s.inPort, s.outPort) : [];
        if (route.length) {
          s.path = s.chip.pathPoints(s.inPort, s.outPort).map((p) => p.add(s.chip.group.position));
          let at = s.inPort;
          s.chambers = [];
          for (const step of route) {
            at = step.forward ? step.channel.to : step.channel.from;
            if (s.chip.chamber(at)) s.chambers.push(at);
          }
        } else {
          const mid = new THREE.Vector3(s.x + SLOT_W / 2, s.y + SLOT_H / 2, top * 0.5);
          s.path = [s.inPos.clone().setZ(top * 0.5), mid, s.outPos.clone().setZ(top * 0.5)];
          s.chambers = s.chip.spec.chambers.map((c) => c.id);
        }
      } else {
        // Ghost: an outline the size of the envelope, labelled "next" (a bench step: amber, "bench").
        const tint = e.status === 'bench' ? 0xbf8700 : 0xcf222e;
        const box = new THREE.BoxGeometry(SLOT_W, SLOT_H, 4);
        const edges = new THREE.LineSegments(new THREE.EdgesGeometry(box), new THREE.LineDashedMaterial({ color: tint, dashSize: 1.2, gapSize: 0.8, transparent: true, opacity: 0.7 }));
        edges.computeLineDistances();
        const face = new THREE.Mesh(box, new THREE.MeshBasicMaterial({ color: tint, transparent: true, opacity: 0.035, depthWrite: false }));
        for (const o of [edges, face]) { o.position.set(s.x + SLOT_W / 2, s.y + SLOT_H / 2, 2); world.add(o); s.objects.push(o); }
        s.top = 4;
        s.inPos = new THREE.Vector3(s.x, s.y + SLOT_H / 2, 4);
        s.outPos = new THREE.Vector3(s.x + SLOT_W, s.y + SLOT_H / 2, 4);
        s.path = [s.inPos.clone().setZ(2), new THREE.Vector3(s.x + SLOT_W / 2, s.y + SLOT_H / 2, 2), s.outPos.clone().setZ(2)];
        s.chambers = [];
      }
    }
    // The end of the line: a tube rack vial for the off-chip steps.
    const end = slots.at(-1);
    const vial = new THREE.Mesh(new THREE.CylinderGeometry(3.2, 3.2, 12, 32, 1, true).rotateX(Math.PI / 2),
      new THREE.MeshStandardMaterial({ color: 0xaab4be, transparent: true, opacity: 0.4, roughness: 0.15, side: THREE.DoubleSide, depthWrite: false }));
    vial.position.set(end.x + SLOT_W / 2, end.y + SLOT_H / 2, 6);
    const liquid = new THREE.Mesh(new THREE.CylinderGeometry(2.9, 2.9, 3, 32).rotateX(Math.PI / 2),
      new THREE.MeshStandardMaterial({ color: KIND_COLOR.library, emissive: KIND_COLOR.library, emissiveIntensity: 0.2, transparent: true, opacity: 0.5 }));
    liquid.position.set(end.x + SLOT_W / 2, end.y + SLOT_H / 2, 1.5);
    liquid.visible = false;
    end.liquid = liquid;
    world.add(vial, liquid);
    end.objects.push(vial, liquid);

    // Tubes between consecutive slots.
    for (let k = 0; k + 1 < slots.length; k++) {
      const a = slots[k];
      const b = slots[k + 1];
      const h = a.outH;
      const kind = h?.carries ?? 'cells';
      const ends = b.kind === 'chip' && b.inPorts?.length > 1 ? b.inPorts.map((p) => local(b, p).setZ(b.top)) : [b.inPos];
      const objects = [];
      const curves = ends.map((to) => {
        const from = a.outPos;
        const lift = 7;
        const mid = from.clone().lerp(to, 0.5).setZ(Math.max(from.z, to.z) + lift + 3);
        return new THREE.CatmullRomCurve3([from.clone(), from.clone().setZ(from.z + lift), mid, to.clone().setZ(to.z + lift), to.clone()]);
      });
      curves.forEach((c, i) => {
        const mesh = new THREE.Mesh(new THREE.TubeGeometry(c, 80, i ? TUBE_R * 0.7 : TUBE_R, 10, false),
          new THREE.MeshStandardMaterial({ color: TUBE_COLOR, emissive: KIND_COLOR[kind], emissiveIntensity: 0, transparent: true, opacity: 0.5, roughness: 0.35, depthWrite: false }));
        mesh.renderOrder = 9;
        world.add(mesh);
        objects.push(mesh);
      });
      // Off-chip steps that happen in this tube (pipeline_status.md: OFF-CHIP rows after this chip).
      // An end-of-line step (SPRI, QC) only labels the tube into the end, whatever the chip order.
      const off = (status?.offChip ?? []).filter((o) => o.after && o.after === a.entry?.spec?.id && (o.before !== null || b.kind === 'end'));
      if (off.length && b.kind !== 'end') {
        const lab = label('off-chip step', 'p3-tube');
        lab.element.title = off.map((o) => o.stage).join('; ');
        lab.center.set(0.5, -0.9);
        lab.position.copy(curves[0].getPoint(0.5));
        world.add(lab);
        objects.push(lab);
      }
      tubes.push({ k, kind, curve: curves[0], points: curves[0].getPoints(60), objects, handoff: h });
    }

    // Labels: number, a short stage name and a status dot (the sidebar has the full names and
    // words); a stage with no chip says so. The back row's labels sit behind their chips, the front
    // row's in front, so the two rows never cover each other.
    slots.forEach((s, k) => {
      let html;
      if (s.kind === 'end') {
        html = '<b>Library out</b><span class="p3-id">off-chip, in tubes</span>';
      } else {
        const e = s.entry;
        const st = s.chip ? status?.byChip.get(s.chip.spec.id) : null;
        const badge = s.kind === 'ghost' ? (e.status === 'bench' ? 'BENCH' : 'NEXT') : st?.badge ?? (e.status ?? '').toUpperCase();
        const v = statusView(badge, s.kind === 'ghost' ? '' : st?.detail ?? '');
        const name = SHORT[s.chip?.spec.id] ?? SHORT[e.module] ?? String(e.name ?? '').split(/[:(]/)[0].trim();
        const dot = `<i class="sdot ${v.cls}" title="${esc(v.text)}"></i>`;
        html = `<span class="p3-head">${dot}<span class="p3-num">${k + 1}</span><b>${esc(name)}</b></span>${s.kind === 'ghost' ? `<span class="p3-id">${esc(v.word.toLowerCase())}</span>` : ''}`;
      }
      const obj = label(html, s.kind);
      const front = s.y === 0 && slots.some((o) => o.y > 0);
      obj.center.set(0.5, front ? 0 : 1);
      obj.position.set(s.x + SLOT_W / 2, front ? s.y - 2 : s.y + SLOT_H + 2, 0);
      world.add(obj);
      s.objects.push(obj);
      s.label = obj.element;
      if (s.chip) {
        s.label.dataset.open = s.entry.view ?? '';
        s.label.style.pointerEvents = s.entry.view ? 'auto' : 'none';
        s.label.addEventListener('click', () => s.entry.view && handlers.onOpenModule?.(s.entry.view, s.entry.file));
      }
    });
    world.visible = visible;
    buildPhases();
    render();
    if (visible) frame();
  }

  function buildPhases() {
    phases = [];
    slots.forEach((s, k) => {
      if (s.kind === 'end') return;
      const e = s.entry;
      const st = s.chip ? data.status?.byChip.get(s.chip.spec.id) : null;
      const what = s.kind === 'ghost' ? `${e.name}: ${e.status === 'bench' ? 'a bench step' : 'not on a chip yet'}. ${firstSentence(e.note ?? e.status_note ?? '')}`
        : `${st?.stage ?? e.name} (${s.chip.spec.id}), ${statusView(st?.badge ?? e.status, st?.detail ?? '').text.toLowerCase()}. ${firstSentence(e.note ?? s.chip.spec.description ?? '')}`;
      phases.push({ id: `pl_${e.module}`, kind: 'module', k, play: s.kind === 'ghost' ? 2.5 : 4.5, proto: e.duration_s ?? 600,
        narration: what, args: s.chip ? { chip: s.chip.spec.id, status: st?.status ?? e.status } : { status: 'next' } });
      const tb = tubes[k];
      if (!tb) return;
      const to = slots[k + 1];
      const h = tb.handoff;
      const toName = to.kind === 'end' ? 'the off-chip steps' : to.chip ? to.chip.spec.id : to.entry?.name;
      const off = (data.status?.offChip ?? []).filter((o) => o.after && o.after === s.chip?.spec.id && o.before !== null && to.kind !== 'end').map((o) => o.stage.toLowerCase());
      phases.push({ id: `pl_${h?.id ?? `${e.module}_to_next`}`, kind: 'handoff', k, play: 2.6, proto: 120,
        narration: `${(h?.carries ?? 'sample').replace(/^./, (c) => c.toUpperCase())} go from ${s.chip ? `${s.chip.spec.id} ${s.outPort ?? ''}` : e.name} to ${toName}${to.kind === 'chip' && to.inPort ? ` ${to.inPort}` : ''} through 1/16 inch tubing${off.length ? `; off-chip on the way: ${off.join(', ')}` : ''}.`,
        args: { carries: h?.carries ?? 'cells', from: h?.from?.port ?? '', to: (h?.to?.ports ?? [h?.to?.port ?? h?.to?.roadmap ?? '']).join(', ') } });
    });
    starts = [];
    let acc = 0;
    let proto = 0;
    phases.forEach((ph, i) => {
      starts.push(acc);
      acc += ph.play;
      ph.event = { id: ph.id, command: ph.kind === 'module' ? 'module' : 'hand-off', narration: ph.narration, args: ph.args, duration_s: ph.proto, t_s: proto, protocol_step: i + 1 };
      proto += ph.proto;
    });
    total = acc || 1;
    t = Math.min(t, total);
    current = -2;
  }

  // ---------------------------------------------------------------- playback

  function indexAt(x) {
    let i = 0;
    while (i + 1 < starts.length && starts[i + 1] <= x + 1e-9) i++;
    return i;
  }

  function setPlaying(on) {
    if (on && t >= total - 1e-6) t = 0;
    playing = on;
    if (!on) stopAt = null;
    handlers.onPlayState?.(on);
  }

  function update(dt) {
    if (!visible || !phases.length) return;
    if (playing) {
      t += dt * speed;
      if (stopAt !== null && t >= stopAt) { t = stopAt; setPlaying(false); }
      if (t >= total) { t = total; setPlaying(false); }
    }
    render();
  }

  const carriedIn = (s) => s.inH?.carries ?? 'cells';
  const carriedOut = (s) => s.outH?.carries ?? carriedIn(s);

  function render() {
    if (!phases.length || !slots.length) return;
    const done = t >= total - 1e-6;
    const i = done ? phases.length - 1 : indexAt(t);
    const p = done ? 1 : clamp01((t - starts[i]) / phases[i].play);
    const ph = phases[i];
    if (i !== current) {
      current = i;
      handlers.onPhase?.(i, ph.event);
    }
    handlers.onTick?.({ t, total, protocolT: ph.event.t_s + p * ph.event.duration_s, index: i, progress: p });

    // Labels and tubes: the working chip (or the tube in use) stands out; earlier ones are done.
    slots.forEach((s, k) => {
      s.label?.classList.toggle('active', (ph.kind === 'module' && ph.k === k) || (ph.kind === 'handoff' && (ph.k === k || ph.k + 1 === k)));
      s.label?.classList.toggle('done', k < ph.k || (ph.kind === 'handoff' && k === ph.k));
    });
    tubes.forEach((tb, k) => {
      const on = ph.kind === 'handoff' && ph.k === k;
      for (const o of tb.objects) if (o.material?.emissive) { o.material.emissiveIntensity = on ? 0.75 : k < ph.k ? 0.2 : 0; o.material.opacity = on ? 0.75 : 0.5; }
    });
    const end = slots.at(-1);
    if (end?.liquid) end.liquid.visible = done || (ph.kind === 'handoff' && ph.k === slots.length - 2 && p > 0.6);

    // Chambers on the sample's path glow as it passes.
    for (const s of slots) {
      if (!s.chip) continue;
      const here = ph.kind === 'module' && ph.k === s.k;
      s.chambers?.forEach((id, j) => {
        const part = s.chip.chamber(id);
        if (!part?.glow) return;
        const at = here ? p * s.chambers.length - j : -1;
        part.glow.visible = here && at > -0.2;
        if (part.glow.visible) {
          part.glowMaterial.color.setHex(KIND_COLOR[p < 0.5 ? carriedIn(s) : carriedOut(s)]);
          part.glowMaterial.opacity = 0.18 + 0.3 * clamp01(1 - Math.abs(at - 0.5));
        }
      });
    }

    // The sample.
    dots.forEach((d, j) => {
      const f = ease(clamp01((p - (j / DOTS) * 0.3) / 0.7));
      let pos = null;
      let kind = 'cells';
      if (ph.kind === 'module') {
        const s = slots[ph.k];
        pos = along(s.path, f);
        kind = f < 0.5 ? carriedIn(s) : carriedOut(s);
        if (s.kind === 'ghost') kind = carriedIn(s);
      } else {
        const tb = tubes[ph.k];
        pos = tb ? along(tb.points, f) : null;
        kind = tb?.kind ?? 'cells';
      }
      d.core.visible = Boolean(pos);
      if (!pos) return;
      d.core.position.copy(pos);
      d.core.material.color.setHex(kind === 'capsules' ? CAPSULE_CORE : KIND_COLOR[kind]);
      d.core.material.emissive.setHex(kind === 'capsules' ? CAPSULE_CORE : KIND_COLOR[kind]);
      d.shell.visible = kind === 'capsules';
      d.core.scale.setScalar(kind === 'library' ? 0.7 : 1);
    });
  }

  // ---------------------------------------------------------------- picking

  const ray = new THREE.Raycaster();
  const ndc = new THREE.Vector2();
  let down = null;
  const canvas = ctx.renderer.domElement;
  canvas.addEventListener('pointerdown', (e) => { down = [e.clientX, e.clientY]; });
  canvas.addEventListener('pointerup', (e) => {
    if (!visible || !down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 5) return;
    const r = canvas.getBoundingClientRect();
    ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ndc, ctx.camera);
    const hit = ray.intersectObjects(slots.filter((s) => s.chip).map((s) => s.chip.group), true)[0];
    let o = hit?.object;
    while (o && o.userData.slot === undefined) o = o.parent;
    const s = o ? slots[o.userData.slot] : null;
    if (s?.entry?.view) handlers.onOpenModule?.(s.entry.view, s.entry.file);
  });
  canvas.addEventListener('pointermove', (e) => {
    if (!visible) return;
    const r = canvas.getBoundingClientRect();
    ndc.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ndc, ctx.camera);
    const hit = ray.intersectObjects(slots.filter((s) => s.chip).map((s) => s.chip.group), true).length > 0;
    canvas.style.cursor = hit ? 'pointer' : '';
  });

  // A wider frame than a single chip's: in perspective the near row spreads beyond the far one.
  // A steeper view than a single chip's, so the two rows read as a plan of the line.
  function frame() { ctx.frameChip({ x: size.x * 1.08, y: size.y * 1.3, targetY: size.y * 0.02, dir: [-0.03, 0.78, 0.62] }); }

  return {
    load,
    frame,
    show() { visible = true; world.visible = true; root.hidden = false; current = -2; frame(); render(); },
    hide() { visible = false; world.visible = false; root.hidden = true; canvas.style.cursor = ''; setPlaying(false); },
    get visible() { return visible; },
    get playing() { return playing; },
    get t() { return t; },
    get total() { return total; },
    get currentIndex() { return current; },
    get data() { return data; },
    get slots() { return slots; },
    get size() { return size; },
    toggle() { setPlaying(!playing); },
    setPlaying,
    step() {
      if (t >= total - 1e-6) t = 0;
      const i = indexAt(t);
      stopAt = i + 1 < starts.length ? starts[i + 1] : total;
      playing = true;
      handlers.onPlayState?.(true);
    },
    seek(x) { t = Math.min(Math.max(x, 0), total); stopAt = null; render(); },
    seekToPhase(i) { this.seek(starts[Math.max(0, Math.min(i, starts.length - 1))] ?? 0); },
    phaseIndex(id) { return phases.findIndex((ph) => ph.id === id); },
    phaseSlot(i) { const ph = phases[i]; return ph ? { k: ph.k, kind: ph.kind } : null; },
    // Play phases i0..i1 (inclusive) and stop inside i1.
    playRange(i0, i1) {
      t = starts[i0] ?? 0;
      stopAt = (starts[i1] ?? total) + (phases[i1]?.play ?? 0) - 1e-3;
      playing = true;
      current = -2;
      handlers.onPlayState?.(true);
      render();
    },
    setSpeed(x) { speed = x; },
    update,
    timeline() { return phases.map((ph, i) => ({ ev: ph.event, start: starts[i], duration: ph.play })); },
    runLike() {
      return { kind: 'pipeline', protocol_id: data?.pipe?.id ?? 'pipeline', chip_id: 'all modules', placeholder: true, operator: 'planner',
        total_duration_s: phases.reduce((a, ph) => a + ph.proto, 0) };
    },
    refresh() { current = -2; render(); },
  };
}

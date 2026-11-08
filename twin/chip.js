// Builds the chip from chip_spec.json. Everything inside `chip.group` uses chip
// coordinates: millimetres, origin at the bottom-left corner of the footprint,
// x right, y up the chip, z up out of the chip (same as the CAD exports).
// Real scale: capsules, channels and ports are drawn at their spec sizes.
import * as THREE from 'three';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';
import { CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';
import { contracts } from './contracts.js';

// Mid-tone colours that read on the light bench (twin/style.css).
const TYPE_COLORS = {
  inlet: 0x218bff,
  exchange: 0x1b9e8f,
  split: 0x8250df,
  pool: 0x0969da,
  outlet: 0xbf3989,
  well: 0xbf8700,
  culture: 0x8250df,
  junction: 0x0969da,
  bubble_trap: 0x1b7c83,
  filter: 0xbf8700,
  mixer: 0x2da44e,
  cell_wash: 0x218bff,
  qc_window: 0xbf3989,
};
const PORT_COLORS = { inlet: 0x218bff, outlet: 0xbf3989, control: 0x6e7781 };
const ROLE_COLORS = {
  waste: 0xe16f24, backflush: 0x2da44e, release: 0xbf3989, load: 0x218bff, reagent: 0x218bff,
  cells: 0xe5534b, medium: 0x1b9e8f, factor: 0xbf8700, harvest: 0xbf3989, control: 0x6e7781, oil: 0xbf8700, transfer: 0xbf3989,
};
const FEATURE_COLORS = { gas_window: 0x218bff, heat_zone: 0xe16f24, cold_zone: 0x0969da, curing_window: 0x8250df, optical_window: 0xbf3989 };

// Draw order for see-through parts (lower draws first).
// A channel at rest (twin/player.js puts it back to this after a pulse).
export const CHANNEL = { color: 0x6fa8dc, emissive: 0x0969da, emissiveIntensity: 0.12, opacity: 0.6 };
export const ORDER = { capsule: 2, fluid: 3, channel: 4, chamber: 5, glow: 6, layer: 10, edges: 11 };

export class Chip {
  constructor(spec) {
    this.spec = spec;
    this.id = spec.id;
    this.footprint = spec.footprint;
    this.height = spec.layers.reduce((z, l) => z + l.thickness_mm, 0);
    this.top = this.height;
    // Size unit for labels, glows and textures: 1 for a chip that fills the demo envelope
    // (contracts/blocks.json, 30 x 20 mm). It used to divide by 25 for a 75 x 25 mm slide,
    // which no chip here is, so every chip came out at a flat 0.8 and the unit tracked nothing.
    this.u = spec.footprint.y / contracts().envelopeY;
    this.module = spec.module ?? null;
    this.fluidZ = spec.chambers.length
      ? spec.chambers.reduce((z, c) => z + c.center[2], 0) / spec.chambers.length
      : this.height / 2;
    this.floorZ0 = spec.chambers.length ? Math.min(...spec.chambers.map((c) => c.center[2] - c.size[2] / 2)) : 0;
    // Module chips without capsules (1 culture, 1b cell prep) move CELLS: drawn as small
    // dots, not to scale. Capsules: real size when the spec says so; a visible default otherwise.
    this.cells = Boolean(spec.module && !spec.capsule);
    this.flowThrough = spec.module?.id === 'm1b'; // cells follow the stream from stage to stage
    this.capsuleRadius = this.cells ? 0.17 * this.u : spec.capsule?.diameter_um ? spec.capsule.diameter_um / 2000 : 0.55;
    this.capsuleCount = this.cells ? 72 : Math.min(Math.round(spec.capsule?.count ?? 24), 12000);
    this.realCapsules = Boolean(spec.capsule?.diameter_um);
    this.spawnHeight = Math.max(1.2, spec.footprint.y * 0.36);

    this.group = new THREE.Group();
    this.group.name = `chip:${spec.id}`;
    this.group.rotation.x = -Math.PI / 2; // chip z-up -> three.js y-up
    this.group.position.set(-spec.footprint.x / 2, 0, spec.footprint.y / 2);

    this.parts = new Map(); // id -> chamber or port record
    this.channels = [];
    this.labels = [];
    this.features = new Map();
    this.stages = new Map();
    // On-chip valves were dropped from the design (they need a second layer) and no chip
    // declares any, so the twin no longer draws them. If one ever does, say so instead of
    // inventing a control layer to hang it in.
    this.undrawn = (spec.valves ?? []).length
      ? [`${spec.valves.length} on-chip valve${spec.valves.length > 1 ? 's' : ''} (${spec.valves.map((v) => v.id).join(', ')}) are not drawn: on-chip valves were removed from the design, which is one fluidic layer`]
      : [];

    this.#buildLayers();
    for (const c of spec.chambers) this.#buildChamber(c);
    for (const p of spec.ports) this.#buildPort(p);
    for (const ch of spec.channels) this.#buildChannel(ch);
    for (const f of spec.features ?? []) this.#buildFeature(f);
    for (const st of spec.stages ?? []) this.#buildStage(st);
  }

  // ---------------------------------------------------------------- building

  #buildLayers() {
    let z = 0;
    this.spec.layers.forEach((layer, i) => {
      const { x, y } = this.footprint;
      const geom = new THREE.BoxGeometry(x, y, layer.thickness_mm);
      const mat = new THREE.MeshPhysicalMaterial({
        color: 0xdfe6ee,
        transparent: true,
        opacity: 0.05 + 0.02 * (i % 2),
        roughness: 0.06,
        metalness: 0,
        clearcoat: 1,
        clearcoatRoughness: 0.08,
        depthWrite: false,
      });
      const mesh = new THREE.Mesh(geom, mat);
      mesh.position.set(x / 2, y / 2, z + layer.thickness_mm / 2);
      mesh.renderOrder = ORDER.layer;
      mesh.name = `layer:${layer.id}`;
      this.group.add(mesh);
      z += layer.thickness_mm;
    });
    // One outline for the whole chip (per-layer outlines stack up into a blur of lines).
    const { x, y } = this.footprint;
    const outline = new THREE.LineSegments(
      new THREE.EdgesGeometry(new THREE.BoxGeometry(x, y, z)),
      new THREE.LineBasicMaterial({ color: 0x57606a, transparent: true, opacity: 0.5, depthWrite: false }),
    );
    outline.position.set(x / 2, y / 2, z / 2);
    outline.renderOrder = ORDER.edges;
    this.group.add(outline);
  }

  #buildChamber(c) {
    const color = TYPE_COLORS[c.type] ?? 0x9ca3af;
    const center = new THREE.Vector3(...c.center);
    const size = new THREE.Vector3(...c.size);
    const round = c.shape === 'cylinder';
    const serpentine = c.mixer?.kind === 'serpentine';
    const geom = round
      ? new THREE.CylinderGeometry(size.x / 2, size.x / 2, size.z, 48).rotateX(Math.PI / 2)
      : new THREE.BoxGeometry(size.x, size.y, size.z);

    const material = new THREE.MeshPhysicalMaterial({
      color,
      emissive: color,
      emissiveIntensity: 0.08,
      transparent: true,
      opacity: serpentine ? 0.05 : c.type === 'junction' ? 0.5 : 0.16,
      roughness: 0.2,
      depthWrite: false,
    });
    const box = new THREE.Mesh(geom, material);
    box.position.copy(center);
    box.renderOrder = ORDER.chamber;
    box.name = `chamber:${c.id}`;
    this.group.add(box);

    const edgeMaterial = new THREE.LineBasicMaterial({ color, transparent: true, opacity: serpentine ? 0.25 : 0.9, depthWrite: false });
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geom, 20), edgeMaterial);
    edges.position.copy(center);
    edges.renderOrder = ORDER.edges;
    this.group.add(edges);

    const floorZ = center.z - size.z / 2;
    const hasMesh = c.retention?.type === 'mesh' || Boolean(c.mesh_pore_um);

    // A serpentine mixer: draw the folded channel inside its box.
    let serp = null;
    if (serpentine) {
      const turns = c.mixer.turns ?? 8;
      const w = Math.max(c.mixer.channel_width_mm ?? 0.15, 0.08);
      const x0 = center.x - size.x / 2;
      const y0 = center.y - size.y / 2 + w;
      const y1 = center.y + size.y / 2 - w;
      const pts = [new THREE.Vector3(x0, center.y, center.z)];
      for (let k = 0; k <= turns; k++) {
        const x = x0 + ((k + 0.5) / (turns + 1)) * size.x;
        pts.push(new THREE.Vector3(x, k % 2 ? y0 : y1, center.z), new THREE.Vector3(x, k % 2 ? y1 : y0, center.z));
      }
      pts.push(new THREE.Vector3(center.x + size.x / 2, center.y, center.z));
      const serpMaterial = new THREE.MeshStandardMaterial({ color, emissive: color, emissiveIntensity: 0.35, transparent: true, opacity: 0.6, depthWrite: false });
      serp = new THREE.Group();
      for (let k = 0; k < pts.length - 1; k++) {
        const a = pts[k];
        const b = pts[k + 1];
        const len = a.distanceTo(b);
        if (len < 1e-6) continue;
        const seg = new THREE.Mesh(new THREE.BoxGeometry(len + w, w, size.z), serpMaterial);
        seg.position.copy(a).add(b).multiplyScalar(0.5);
        seg.quaternion.setFromUnitVectors(new THREE.Vector3(1, 0, 0), b.clone().sub(a).normalize());
        seg.renderOrder = ORDER.channel;
        serp.add(seg);
      }
      serp.userData.material = serpMaterial;
      this.group.add(serp);
    }

    // Pillar filter: a dotted floor.
    if (c.filter) {
      const tex = dotTexture();
      tex.repeat.set(size.x / (0.5 * this.u), size.y / (0.5 * this.u));
      const floor = new THREE.Mesh(
        new THREE.PlaneGeometry(size.x * 0.98, size.y * 0.98),
        new THREE.MeshBasicMaterial({ map: tex, transparent: true, opacity: 0.8, depthWrite: false, color }),
      );
      floor.position.set(center.x, center.y, center.z - size.z / 2 + Math.min(0.02, size.z * 0.1));
      floor.renderOrder = ORDER.fluid - 1;
      this.group.add(floor);
    }

    // Mesh floor that holds the capsules.
    if (hasMesh || c.retention) {
      const tex = meshTexture();
      const cell = 0.7 * this.u;
      tex.repeat.set(size.x / cell, size.y / cell);
      const floor = new THREE.Mesh(
        new THREE.PlaneGeometry(size.x * 0.98, size.y * 0.98),
        new THREE.MeshBasicMaterial({ map: tex, transparent: true, opacity: 0.55, depthWrite: false, color }),
      );
      floor.position.set(center.x, center.y, floorZ + Math.min(0.03, size.z * 0.05));
      floor.renderOrder = ORDER.fluid - 1;
      this.group.add(floor);
    }

    // Liquid: a unit box (or cylinder), bottom at z=0, scaled to the fill level.
    const fluidGeom = round ? new THREE.CylinderGeometry(0.5, 0.5, 1, 40).rotateX(Math.PI / 2) : new THREE.BoxGeometry(1, 1, 1);
    fluidGeom.translate(0, 0, 0.5);
    const fluidMaterial = new THREE.MeshStandardMaterial({
      color: 0xffffff,
      emissive: 0xffffff,
      emissiveIntensity: 0.45,
      envMapIntensity: 0.25,
      transparent: true,
      opacity: 0.6,
      roughness: 0.55,
      depthWrite: false,
    });
    const fluid = new THREE.Mesh(fluidGeom, fluidMaterial);
    fluid.position.set(center.x, center.y, floorZ + Math.min(0.02, size.z * 0.03));
    fluid.scale.set(size.x * 0.96, size.y * 0.96, 0.001);
    fluid.visible = false;
    fluid.renderOrder = ORDER.fluid;
    this.group.add(fluid);

    // Warm/cool glow for hold_temp.
    const pad = 1.6 * this.u;
    const glowMaterial = new THREE.MeshBasicMaterial({
      color: 0xffffff,
      transparent: true,
      opacity: 0,
      blending: THREE.NormalBlending,
      depthWrite: false,
    });
    const glow = new THREE.Mesh(round
      ? new THREE.CylinderGeometry((size.x + pad) / 2, (size.x + pad) / 2, size.z + pad * 0.75, 40).rotateX(Math.PI / 2)
      : new THREE.BoxGeometry(size.x + pad, size.y + pad, size.z + pad * 0.75), glowMaterial);
    glow.position.copy(center);
    glow.visible = false;
    glow.renderOrder = ORDER.glow;
    this.group.add(glow);

    if (c.type !== 'junction') {
      const label = makeLabel(c.id, 'part-label', [0, 0.5]);
      label.position.set(center.x + size.x / 2 + 0.6 * this.u, center.y, this.top + 0.3 * this.u);
      this.group.add(label);
      this.labels.push(label);
    }

    const tempLabel = makeLabel('', 'temp-label');
    tempLabel.position.set(center.x, center.y, this.top + 3.5 * this.u);
    tempLabel.visible = false;
    this.group.add(tempLabel);

    this.parts.set(c.id, {
      kind: 'chamber',
      id: c.id,
      type: c.type,
      spec: c,
      color,
      center,
      size,
      floorZ,
      heightZ: (c.height_um ? c.height_um / 1000 : size.z),
      hasMesh,
      round,
      serp,
      box,
      edges,
      material,
      edgeMaterial,
      fluid,
      fluidMaterial,
      glow,
      glowMaterial,
      tempLabel,
    });
  }

  #buildPort(p) {
    const color = ROLE_COLORS[p.role] ?? PORT_COLORS[p.type] ?? 0x9ca3af;
    const [x, y, pz] = p.position;
    const r = (p.width_um ? p.width_um / 2000 : 1.1) * (p.type === 'control' ? 0.7 : 1);
    const bottom = pz < this.top / 2;
    // Top ports: a tube from the fluid layer up through the lid. Bottom ports: a tube
    // from under the chip up to the drain plane below the chambers.
    const minFloor = Math.min(...this.spec.chambers.map((c) => c.center[2] - c.size[2] / 2));
    const z0 = bottom ? pz - 0.3 * this.u : this.fluidZ - 0.6 * this.u;
    const z1 = bottom ? minFloor : this.top + 0.5 * this.u;
    const tube = new THREE.Mesh(
      new THREE.CylinderGeometry(r, r, Math.max(z1 - z0, 0.01), 32, 1, true).rotateX(Math.PI / 2),
      new THREE.MeshPhysicalMaterial({
        color,
        emissive: color,
        emissiveIntensity: 0.15,
        transparent: true,
        opacity: 0.35,
        roughness: 0.2,
        side: THREE.DoubleSide,
        depthWrite: false,
      }),
    );
    tube.position.set(x, y, (z0 + z1) / 2);
    tube.renderOrder = ORDER.chamber;
    this.group.add(tube);

    const ringZ = bottom ? z0 : z1;
    const ringMaterial = new THREE.MeshStandardMaterial({ color, emissive: color, emissiveIntensity: 0.5, roughness: 0.3 });
    const ring = new THREE.Mesh(new THREE.TorusGeometry(r * 1.23, r * 0.18, 12, 40), ringMaterial);
    ring.position.set(x, y, ringZ);
    this.group.add(ring);

    const glowMaterial = new THREE.MeshBasicMaterial({
      color: 0xff5fd2,
      transparent: true,
      opacity: 0,
      blending: THREE.NormalBlending,
      depthWrite: false,
    });
    const glow = new THREE.Mesh(new THREE.SphereGeometry(r * 2.4, 24, 16), glowMaterial);
    glow.position.set(x, y, ringZ);
    glow.visible = false;
    glow.renderOrder = ORDER.glow;
    this.group.add(glow);

    if (p.type !== 'control') {
      const text = p.role && !this.module ? `${p.id} · ${p.role}` : p.id;
      const label = makeLabel(text, 'part-label', bottom ? [0.5, 0] : [0.5, 1]);
      label.element.title = p.role ? `${p.id} (${p.role})` : p.id;
      label.position.set(x, y + (bottom ? -1.6 : 1.6) * this.u, bottom ? z0 : z1 + 0.7 * this.u);
      this.group.add(label);
      this.labels.push(label);
    }

    this.parts.set(p.id, {
      kind: 'port',
      id: p.id,
      type: p.type,
      role: p.role ?? null,
      capsulePassable: p.capsule_passable,
      bottom,
      spec: p,
      color,
      center: new THREE.Vector3(x, y, bottom ? pz : this.top),
      spawn: new THREE.Vector3(x, y, bottom ? pz - this.spawnHeight : this.top + this.spawnHeight),
      box: tube,
      ring,
      ringMaterial,
      glow,
      glowMaterial,
    });
  }

  #buildChannel(ch) {
    const from = this.parts.get(ch.from);
    const to = this.parts.get(ch.to);
    if (!from || !to) return;
    const points = ch.path ? this.#pathPoints(ch, from, to) : routeChannel(from, to, ch, this);
    const material = new THREE.MeshStandardMaterial({
      color: CHANNEL.color,
      emissive: CHANNEL.emissive,
      emissiveIntensity: CHANNEL.emissiveIntensity,
      transparent: true,
      opacity: CHANNEL.opacity,
      roughness: 0.3,
      depthWrite: false,
    });
    const group = new THREE.Group();
    const w = ch.width_mm;
    const d = ch.depth_mm;
    for (let i = 0; i < points.length - 1; i++) {
      const a = points[i];
      const b = points[i + 1];
      const len = a.distanceTo(b);
      if (len < 1e-6) continue;
      const seg = new THREE.Mesh(new THREE.BoxGeometry(len + w, w, d), material);
      seg.position.copy(a).add(b).multiplyScalar(0.5);
      seg.quaternion.setFromUnitVectors(new THREE.Vector3(1, 0, 0), b.clone().sub(a).normalize());
      seg.renderOrder = ORDER.channel;
      group.add(seg);
    }
    this.group.add(group);
    this.channels.push({ id: ch.id, from: ch.from, to: ch.to, points, material, group, carries: ch.carries ?? null, spec: ch });
  }

  // Explicit centreline from the spec, in the channel plane (or the drain plane under a
  // mesh for bottom ports), dropping vertically into bottom ports.
  #pathPoints(ch, from, to) {
    const bottom = (from.kind === 'port' && from.bottom) || (to.kind === 'port' && to.bottom);
    const z = bottom ? this.floorZ0 - ch.depth_mm / 2 - 0.002 : this.floorZ0 + ch.depth_mm / 2;
    const pts = ch.path.map(([x, y]) => new THREE.Vector3(x, y, z));
    if (from.kind === 'port' && from.bottom) pts.unshift(new THREE.Vector3(...from.spec.position));
    if (to.kind === 'port' && to.bottom) pts.push(new THREE.Vector3(...to.spec.position));
    return pts;
  }

  #buildFeature(f) {
    const color = FEATURE_COLORS[f.type] ?? 0x9ca3af;
    const [cx, cy, cz] = f.center;
    const [sx, sy] = f.size;
    const material = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: f.type === 'heat_zone' ? 0.1 : 0.07, depthWrite: false, side: THREE.DoubleSide });
    const plane = new THREE.Mesh(new THREE.PlaneGeometry(sx, sy), material);
    plane.position.set(cx, cy, f.type === 'heat_zone' ? 0.01 : Math.max(cz, this.top + 0.02));
    plane.renderOrder = ORDER.layer - 1;
    this.group.add(plane);
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.PlaneGeometry(sx, sy)),
      new THREE.LineDashedMaterial({ color, dashSize: 0.8 * this.u, gapSize: 0.5 * this.u, transparent: true, opacity: 0.6 }));
    edges.position.copy(plane.position);
    edges.computeLineDistances();
    this.group.add(edges);
    if (f.type === 'heat_zone') { // windows are clear from their outline; the heater gets a label
      const label = makeLabel(`${f.id} · ${f.temp_c} °C`, 'part-label feature-label', [0, 1]);
      label.position.set(cx - sx / 2 + 0.3 * this.u, cy + sy / 2 - 0.3 * this.u, 0);
      this.group.add(label);
      this.labels.push(label);
    }
    this.features.set(f.id, { id: f.id, type: f.type, spec: f, plane, material, baseOpacity: material.opacity, color });
  }

  // An off-chip stage (Module 2's cold delay): a coil of tubing over a cold block, between two ports.
  #buildStage(st) {
    const a = this.spec.ports.find((p) => p.id === st.from_port);
    const b = this.spec.ports.find((p) => p.id === st.to_port);
    if (!a || !b) return;
    const pa = new THREE.Vector3(...a.position);
    const pb = new THREE.Vector3(...b.position);
    const mid = pa.clone().add(pb).multiplyScalar(0.5);
    const lift = 7 * this.u;
    const loops = 5;
    const pts = [pa.clone(), pa.clone().setZ(this.top + lift * 0.6)];
    for (let k = 0; k <= loops * 12; k++) {
      const t = k / (loops * 12);
      const ang = t * loops * Math.PI * 2;
      const r = 1.6 * this.u;
      pts.push(new THREE.Vector3(mid.x + r * Math.cos(ang) + (t - 0.5) * 6 * this.u, mid.y + 4.5 * this.u + r * Math.sin(ang), this.top + lift + Math.sin(ang) * 0.4));
    }
    pts.push(pb.clone().setZ(this.top + lift * 0.6), pb.clone());
    const curve = new THREE.CatmullRomCurve3(pts);
    const material = new THREE.MeshStandardMaterial({ color: 0x6fa8dc, emissive: 0x0969da, emissiveIntensity: 0.12, transparent: true, opacity: 0.6, depthWrite: false });
    const tube = new THREE.Mesh(new THREE.TubeGeometry(curve, 400, 0.18 * this.u, 10, false), material);
    tube.renderOrder = ORDER.channel;
    this.group.add(tube);
    const blockMat = new THREE.MeshStandardMaterial({ color: 0x9ab8d6, emissive: 0x0969da, emissiveIntensity: 0.08, transparent: true, opacity: 0.7 });
    const block = new THREE.Mesh(new THREE.BoxGeometry(12 * this.u, 5 * this.u, 0.6 * this.u), blockMat);
    block.position.set(mid.x, mid.y + 4.5 * this.u, this.top + lift - 1.1 * this.u);
    this.group.add(block);
    const label = makeLabel(`${st.id} · ${st.temp_c} °C · off-chip tubing on a cold block`, 'part-label stage-label', [0.5, 1]);
    label.position.set(mid.x, mid.y + 7.6 * this.u, this.top + lift + 0.6 * this.u);
    this.group.add(label);
    this.labels.push(label);
    this.stages.set(st.id, { id: st.id, spec: st, tube, material, block, blockMat, curve });
  }

  // ---------------------------------------------------------------- queries

  chamber(id) {
    const p = this.parts.get(id);
    return p && p.kind === 'chamber' ? p : null;
  }

  channel(id) {
    return this.channels.find((c) => c.id === id) ?? null;
  }

  // Channels on the shortest route from one part to another (follows channel
  // direction first, then ignores it if there's no directed route).
  route(fromId, toId) {
    return bfs(this.channels, fromId, toId, true) ?? bfs(this.channels, fromId, toId, false) ?? [];
  }

  // Points a capsule follows from part A to part B, through channel centrelines.
  pathPoints(fromId, toId) {
    const steps = this.route(fromId, toId);
    const pts = [];
    let at = fromId;
    for (const { channel, forward } of steps) {
      const part = this.parts.get(at);
      pts.push(part.center.clone());
      const cp = forward ? channel.points : [...channel.points].reverse();
      pts.push(...cp.map((p) => p.clone()));
      at = forward ? channel.to : channel.from;
    }
    const end = this.parts.get(toId);
    if (end) pts.push(end.center.clone());
    return pts;
  }

  // The nearest port connected to a chamber, filtered by type ('inlet' | 'outlet'),
  // role ('waste', 'backflush', ...) and/or whether capsules can pass it.
  findPort(chamberId, { type, role, capsules } = {}) {
    let best = null;
    for (const part of this.parts.values()) {
      if (part.kind !== 'port') continue;
      if (type && part.type !== type) continue;
      if (role && part.role !== role) continue;
      if (capsules === true && part.capsulePassable === false) continue;
      if (capsules === false && part.capsulePassable !== false) continue;
      // A plain lookup never picks a line capsules can't use (waste, back-flush).
      if (!role && capsules === undefined && part.capsulePassable === false) continue;
      const r = part.type === 'inlet' ? this.route(part.id, chamberId) : this.route(chamberId, part.id);
      if (r.length && (!best || r.length < best.len)) best = { id: part.id, len: r.length };
    }
    return best?.id ?? null;
  }

  channelsBetween(fromId, toId) {
    return this.route(fromId, toId).map((s) => s.channel);
  }

  // ---------------------------------------------------------------- looks

  setLabelsVisible(on) {
    for (const l of this.labels) l.visible = on;
  }

  // Where this chip's STLs live. cad/make_train_cad.py gives the FLAT cad/exports/ folder to
  // exactly ONE chip and writes every other chip into cad/exports/<chip_id>/; both record
  // their owner in a hidden .chip_id. Part ids repeat across chips (inlet_1 and outlet_1 are
  // on ipsc_to_mgl_chip_v1 AND placeholder_chip_v1; in_oil on sctip_spc_v1 and spc_demo_v1;
  // out_waste on module5_v1 and sctip_spc_v1), so loading the flat folder for every chip
  // showed another chip's parts at another chip's coordinates and called it a success.
  // The owner file decides, and nothing is guessed.
  // The .stl names a folder actually holds, from the `python -m http.server` listing, or null
  // when there is no listing to read (a static host): asking for a part that is not there is
  // then the only way to find out, and the 404 is expected.
  static async listStls(dir, root) {
    const html = await Chip.#text(`${root}${dir}`);
    return html === null ? null : Chip.#stlNames(html);
  }

  static async #text(url) {
    try {
      const res = await fetch(url, { cache: 'no-store' });
      return res.ok ? await res.text() : null;
    } catch {
      return null;
    }
  }

  static #stlNames(html) {
    return [...html.matchAll(/href="([^"?#]+\.stl)"/gi)].map((m) => decodeURIComponent(m[1]).split('/').pop().replace(/\.stl$/i, ''));
  }

  static async stlSource(chipId, root) {
    const text = (url) => Chip.#text(`${root}${url}`);
    const stls = (html) => Chip.#stlNames(html);
    // The listing from `python -m http.server` says which subfolders exist, so the owner's own
    // chip never asks for a folder that is not there (display.js reads cad/display/ the same way).
    const top = await text('cad/exports/');
    const hasOwnFolder = top === null || top.includes(`href="${encodeURIComponent(chipId)}/"`) || top.includes(`href="${chipId}/"`);
    if (hasOwnFolder) {
      const own = `cad/exports/${chipId}/`;
      const marker = await text(`${own}.chip_id`);
      if (marker?.trim() === chipId) {
        const listing = await text(own);
        return { dir: own, owner: chipId, names: listing === null ? null : stls(listing) };
      }
    }
    const flatOwner = (await text('cad/exports/.chip_id'))?.trim() ?? null;
    if (flatOwner === chipId) {
      return { dir: 'cad/exports/', owner: chipId, names: top === null ? null : stls(top) };
    }
    return { dir: null, owner: flatOwner, names: null,
      why: `No cad/exports/${chipId}/ folder claims ${chipId}`
        + (flatOwner ? `, and the flat cad/exports/ folder belongs to ${flatOwner}` : '')
        + `, so every part is a box from the spec. Run python cad/make_train_cad.py --chip chip/${chipId}.json.` };
  }

  // What make_train_cad.py writes for a part, in chip coordinates: a chamber is its own box;
  // a port is a bore-square hole down through the lid from its position.
  #expectedBox(part) {
    if (part.kind === 'chamber') return { center: part.center.clone(), size: part.size.clone() };
    const bore = contracts().portHoleMm;
    const lid = Number(this.spec.layers.at(-1)?.thickness_mm) || 0;
    const [x, y, z] = part.spec.position;
    return { center: new THREE.Vector3(x, y, z - lid / 2), size: new THREE.Vector3(bore, bore, lid) };
  }

  // Swap in <dir>/<part id>.stl for any chamber or port that has one, from the directory that
  // belongs to THIS chip. Every file is checked against the box the spec says it should be:
  // an STL at the wrong coordinates is another chip's part and is refused, not displayed.
  // Returns a report; main.js shows it, so a miss is never silent.
  async loadStls({ dir, names = null } = {}, root = '') {
    const report = { dir, loaded: [], missing: [], mismatched: [], failed: [], blocks: [], listed: Boolean(names) };
    const want = [...this.parts.keys()];
    if (!dir) {
      report.missing = want;
      return report;
    }
    const base = `${root}${dir}`;
    // `names` is the directory listing from `python -m http.server`, which tells us what exists
    // without 404s. Without one (a static host), ask for each part by name instead.
    const ids = names ?? want;
    report.unrelated = names ? ids.filter((id) => !this.parts.has(id)) : [];
    const loader = new STLLoader();
    await Promise.all(want.map(async (id) => {
      const part = this.parts.get(id);
      if (!ids.includes(id)) { report.missing.push(id); return; }
      // A Flowmatter block shape (twin/blocks.js) wins over a plain exported box: the spec
      // named a library part, so that part's real geometry is already drawn here.
      if (part.block) { report.blocks.push(id); return; }
      let geom;
      try {
        geom = await loader.loadAsync(`${base}${encodeURIComponent(id)}.stl`);
      } catch {
        report.failed.push(id);
        return;
      }
      geom.computeBoundingBox();
      const got = geom.boundingBox;
      const expect = this.#expectedBox(part);
      const center = got.getCenter(new THREE.Vector3());
      const size = got.getSize(new THREE.Vector3());
      const tol = Math.max(0.05, 0.02 * Math.max(size.x, size.y, size.z));
      if (center.distanceTo(expect.center) > tol || size.distanceTo(expect.size) > tol) {
        report.mismatched.push({ id, at: center.toArray().map((v) => +v.toFixed(3)),
          expected: expect.center.toArray().map((v) => +v.toFixed(3)) });
        geom.dispose();
        return;
      }
      geom.computeVertexNormals();
      const mat = (part.material ?? part.box.material).clone();
      mat.opacity = Math.max(mat.opacity, 0.3);
      const mesh = new THREE.Mesh(geom, mat);
      mesh.renderOrder = ORDER.chamber;
      mesh.name = `stl:${id}`;
      mesh.add(outlineOf(geom, part.color));
      this.group.add(mesh); // STL is already in chip coordinates
      part.box.visible = false;
      if (part.edges) part.edges.visible = false;
      part.stl = mesh;
      report.loaded.push(id);
    }));
    return report;
  }

  dispose() {
    this.group.traverse((o) => {
      o.geometry?.dispose?.();
      if (o.material) [].concat(o.material).forEach((m) => { m.map?.dispose?.(); m.dispose(); });
      if (o.isCSS2DObject) o.element.remove();
    });
  }
}

// ------------------------------------------------------------------ helpers

// The crease lines of a shape (an STL or a Flowmatter block), so it reads on a light background.
export function outlineOf(geom, color) {
  const lines = new THREE.LineSegments(new THREE.EdgesGeometry(geom, 30),
    new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.75, depthWrite: false }));
  lines.renderOrder = ORDER.edges;
  return lines;
}

function bfs(channels, fromId, toId, directed) {
  if (fromId === toId) return [];
  const prev = new Map([[fromId, null]]);
  const queue = [fromId];
  while (queue.length) {
    const at = queue.shift();
    for (const channel of channels) {
      const moves = [];
      if (channel.from === at) moves.push([channel.to, true]);
      if (!directed && channel.to === at) moves.push([channel.from, false]);
      for (const [next, forward] of moves) {
        if (prev.has(next)) continue;
        prev.set(next, { at, channel, forward });
        if (next === toId) {
          const steps = [];
          let cur = toId;
          while (prev.get(cur)) {
            const p = prev.get(cur);
            steps.unshift({ channel: p.channel, forward: p.forward });
            cur = p.at;
          }
          return steps;
        }
        queue.push(next);
      }
    }
  }
  return null;
}

// Channel centreline (same rule as cad/export_display_model.py):
//  - chamber to chamber: right-angle route between facing edges, in the fluid plane
//  - port over or under a chamber: straight up/down through the lid or floor
//  - other ports: leave the chamber through the edge facing the port; ports under the
//    chip run in a drain plane just below the chamber floor (under the mesh)
function routeChannel(from, to, spec, chip) {
  if (from.kind === 'chamber' && to.kind === 'chamber') {
    const z = chip.fluidZ;
    const a = from.center;
    const b = to.center;
    const dir = Math.sign(b.x - a.x) || 1;
    const p0 = new THREE.Vector3(a.x + dir * (from.size.x / 2), a.y, z);
    const p3 = new THREE.Vector3(b.x - dir * (to.size.x / 2), b.y, z);
    if (Math.abs(a.y - b.y) < 0.01) return [p0, p3];
    const mid = (p0.x + p3.x) / 2;
    return [p0, new THREE.Vector3(mid, a.y, z), new THREE.Vector3(mid, b.y, z), p3];
  }
  if (from.kind === 'port' && to.kind === 'port') return [from.center.clone(), to.center.clone()];
  const ch = from.kind === 'chamber' ? from : to;
  const port = ch === from ? to : from;
  const pts = portChamberRoute(ch, port, spec);
  return ch === from ? pts : pts.reverse();
}

function portChamberRoute(ch, port, spec) {
  const { x: cx, y: cy } = ch.center;
  const hx = ch.size.x / 2;
  const hy = ch.size.y / 2;
  const [px, py, pz] = port.spec.position;
  const V = (x, y, z) => new THREE.Vector3(x, y, z);
  const zc = port.bottom ? ch.floorZ - spec.depth_mm / 2 - 0.002 : ch.center.z;
  if (Math.abs(px - cx) <= hx && Math.abs(py - cy) <= hy) {
    return port.bottom ? [V(px, py, ch.floorZ), V(px, py, pz)] : [V(px, py, ch.center.z + ch.size.z / 2), V(px, py, pz)];
  }
  const alongX = Math.abs(px - cx) / hx >= Math.abs(py - cy) / hy;
  const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
  const ex = alongX ? cx + Math.sign(px - cx) * hx : clamp(px, cx - hx, cx + hx);
  const ey = alongX ? clamp(py, cy - hy, cy + hy) : cy + Math.sign(py - cy) * hy;
  const pts = [];
  if (port.bottom) {
    // start under the floor, partway in from the edge, so the drain sits below the mesh
    pts.push(V(ex + (cx - ex) * 0.4, ey + (cy - ey) * 0.4, zc));
  }
  pts.push(V(ex, ey, zc));
  const midX = alongX ? px : ex;
  const midY = alongX ? ey : py;
  if (Math.abs(midX - px) > 1e-6 || Math.abs(midY - py) > 1e-6) pts.push(V(midX, midY, zc));
  pts.push(V(px, py, zc));
  if (port.bottom) pts.push(V(px, py, pz));
  return pts;
}

// anchor: which point of the label sits on its 3D position ([0, 0.5] = left middle).
function makeLabel(text, className, anchor = [0.5, 1]) {
  const el = document.createElement('div');
  el.className = className;
  el.textContent = text;
  const obj = new CSS2DObject(el);
  obj.center.set(anchor[0], anchor[1]);
  return obj;
}

let dotCanvas = null;
function dotTexture() {
  if (!dotCanvas) {
    dotCanvas = document.createElement('canvas');
    dotCanvas.width = dotCanvas.height = 32;
    const g = dotCanvas.getContext('2d');
    g.fillStyle = 'rgba(255,255,255,0.95)';
    g.beginPath();
    g.arc(16, 16, 6, 0, Math.PI * 2);
    g.fill();
  }
  const tex = new THREE.CanvasTexture(dotCanvas);
  tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

let meshCanvas = null;
function meshTexture() {
  if (!meshCanvas) {
    meshCanvas = document.createElement('canvas');
    meshCanvas.width = meshCanvas.height = 32;
    const g = meshCanvas.getContext('2d');
    g.clearRect(0, 0, 32, 32);
    g.strokeStyle = 'rgba(255,255,255,0.9)';
    g.lineWidth = 2;
    g.strokeRect(0, 0, 32, 32);
  }
  const tex = new THREE.CanvasTexture(meshCanvas);
  tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

// Builds the chip from chip_spec.json. Everything inside `chip.group` uses chip
// coordinates: millimetres, origin at the bottom-left corner of the footprint,
// x right, y up the slide, z up out of the slide (same as the CAD exports).
// Real scale: capsules, channels and ports are drawn at their spec sizes.
import * as THREE from 'three';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';
import { CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';

const TYPE_COLORS = {
  inlet: 0x7dd3fc,
  exchange: 0x5eead4,
  split: 0xa78bfa,
  pool: 0x60a5fa,
  outlet: 0xf9a8d4,
  well: 0xfcd34d,
};
const PORT_COLORS = { inlet: 0x7dd3fc, outlet: 0xff5fd2 };
const ROLE_COLORS = { waste: 0xfb923c, backflush: 0x34d399, release: 0xff5fd2, load: 0x7dd3fc, reagent: 0x7dd3fc };

// Draw order for see-through parts (lower draws first).
export const ORDER = { capsule: 2, fluid: 3, channel: 4, chamber: 5, glow: 6, layer: 10, edges: 11 };

export class Chip {
  constructor(spec) {
    this.spec = spec;
    this.id = spec.id;
    this.footprint = spec.footprint;
    this.height = spec.layers.reduce((z, l) => z + l.thickness_mm, 0);
    this.top = this.height;
    this.u = spec.footprint.y / 25; // size unit: 1 for a 75 x 25 mm slide
    this.fluidZ = spec.chambers.length
      ? spec.chambers.reduce((z, c) => z + c.center[2], 0) / spec.chambers.length
      : this.height / 2;
    // Capsules: real size when the spec says so; a visible default otherwise.
    this.capsuleRadius = spec.capsule?.diameter_um ? spec.capsule.diameter_um / 2000 : 0.55;
    this.capsuleCount = Math.min(Math.round(spec.capsule?.count ?? 24), 12000);
    this.realCapsules = Boolean(spec.capsule?.diameter_um);
    this.spawnHeight = Math.max(1.2, spec.footprint.y * 0.36);

    this.group = new THREE.Group();
    this.group.name = `chip:${spec.id}`;
    this.group.rotation.x = -Math.PI / 2; // chip z-up -> three.js y-up
    this.group.position.set(-spec.footprint.x / 2, 0, spec.footprint.y / 2);

    this.parts = new Map(); // id -> chamber or port record
    this.channels = [];
    this.labels = [];

    this.#buildLayers();
    for (const c of spec.chambers) this.#buildChamber(c);
    for (const p of spec.ports) this.#buildPort(p);
    for (const ch of spec.channels) this.#buildChannel(ch);
  }

  // ---------------------------------------------------------------- building

  #buildLayers() {
    let z = 0;
    this.spec.layers.forEach((layer, i) => {
      const { x, y } = this.footprint;
      const geom = new THREE.BoxGeometry(x, y, layer.thickness_mm);
      const mat = new THREE.MeshPhysicalMaterial({
        color: 0xdbeafe,
        transparent: true,
        opacity: 0.07 + 0.03 * (i % 2),
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
      const edges = new THREE.LineSegments(
        new THREE.EdgesGeometry(geom),
        new THREE.LineBasicMaterial({ color: 0xbcd7ff, transparent: true, opacity: 0.28, depthWrite: false }),
      );
      edges.position.copy(mesh.position);
      edges.renderOrder = ORDER.edges;
      this.group.add(edges);
      z += layer.thickness_mm;
    });
  }

  #buildChamber(c) {
    const color = TYPE_COLORS[c.type] ?? 0x9ca3af;
    const center = new THREE.Vector3(...c.center);
    const size = new THREE.Vector3(...c.size);
    const geom = new THREE.BoxGeometry(size.x, size.y, size.z);

    const material = new THREE.MeshPhysicalMaterial({
      color,
      emissive: color,
      emissiveIntensity: 0.08,
      transparent: true,
      opacity: 0.16,
      roughness: 0.2,
      depthWrite: false,
    });
    const box = new THREE.Mesh(geom, material);
    box.position.copy(center);
    box.renderOrder = ORDER.chamber;
    box.name = `chamber:${c.id}`;
    this.group.add(box);

    const edgeMaterial = new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.9, depthWrite: false });
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geom), edgeMaterial);
    edges.position.copy(center);
    edges.renderOrder = ORDER.edges;
    this.group.add(edges);

    const floorZ = center.z - size.z / 2;
    const hasMesh = c.retention?.type === 'mesh' || Boolean(c.mesh_pore_um);

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

    // Liquid: a unit box, bottom at z=0, scaled to the fill level.
    const fluidGeom = new THREE.BoxGeometry(1, 1, 1);
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
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    const glow = new THREE.Mesh(new THREE.BoxGeometry(size.x + pad, size.y + pad, size.z + pad * 0.75), glowMaterial);
    glow.position.copy(center);
    glow.visible = false;
    glow.renderOrder = ORDER.glow;
    this.group.add(glow);

    const label = makeLabel(c.id, 'part-label', [0, 0.5]);
    label.position.set(center.x + size.x / 2 + 0.6 * this.u, center.y, this.top + 0.3 * this.u);
    this.group.add(label);
    this.labels.push(label);

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
    const r = p.width_um ? p.width_um / 2000 : 1.1;
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
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    const glow = new THREE.Mesh(new THREE.SphereGeometry(r * 2.4, 24, 16), glowMaterial);
    glow.position.set(x, y, ringZ);
    glow.visible = false;
    glow.renderOrder = ORDER.glow;
    this.group.add(glow);

    const label = makeLabel(p.role ? `${p.id} · ${p.role}` : p.id, 'part-label', bottom ? [0.5, 0] : [0.5, 1]);
    label.position.set(x, y + (bottom ? -1.6 : 1.6) * this.u, bottom ? z0 : z1 + 0.7 * this.u);
    this.group.add(label);
    this.labels.push(label);

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
    const points = routeChannel(from, to, ch, this);
    const material = new THREE.MeshStandardMaterial({
      color: 0x93c5fd,
      emissive: 0x3b82f6,
      emissiveIntensity: 0.35,
      transparent: true,
      opacity: 0.5,
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
    this.channels.push({ id: ch.id, from: ch.from, to: ch.to, points, material, group });
  }

  // ---------------------------------------------------------------- queries

  chamber(id) {
    const p = this.parts.get(id);
    return p && p.kind === 'chamber' ? p : null;
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

  // Kept for older callers: nearest port of a type that capsules/product can use.
  nearestPort(type, chamberId) {
    return this.findPort(chamberId, { type });
  }

  channelsBetween(fromId, toId) {
    return this.route(fromId, toId).map((s) => s.channel);
  }

  // ---------------------------------------------------------------- looks

  setLabelsVisible(on) {
    for (const l of this.labels) l.visible = on;
  }

  // Swap in cad/exports/<id>.stl for any chamber or port that has one.
  // Uses the directory listing from `python -m http.server`; anything missing
  // silently keeps the box.
  async loadStls(baseUrl) {
    let html = '';
    try {
      const res = await fetch(baseUrl, { cache: 'no-store' });
      if (!res.ok) return [];
      html = await res.text();
    } catch {
      return [];
    }
    const names = [...html.matchAll(/href="([^"?#]+\.stl)"/gi)].map((m) => decodeURIComponent(m[1]).split('/').pop());
    const loader = new STLLoader();
    const loaded = [];
    await Promise.all(
      names.map(async (name) => {
        const id = name.replace(/\.stl$/i, '');
        const part = this.parts.get(id);
        if (!part) return;
        try {
          const geom = await loader.loadAsync(baseUrl + encodeURIComponent(name));
          geom.computeVertexNormals();
          const mat = (part.material ?? part.box.material).clone();
          mat.opacity = Math.max(mat.opacity, 0.3);
          const mesh = new THREE.Mesh(geom, mat);
          mesh.renderOrder = ORDER.chamber;
          mesh.name = `stl:${id}`;
          this.group.add(mesh); // STL is already in chip coordinates
          part.box.visible = false;
          if (part.edges) part.edges.visible = false;
          part.stl = mesh;
          loaded.push(id);
        } catch (err) {
          console.warn(`Could not load ${name}; keeping the box.`, err);
        }
      }),
    );
    return loaded;
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

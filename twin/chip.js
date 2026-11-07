// Builds the chip from chip_spec.json. Everything inside `chip.group` uses chip
// coordinates: millimetres, origin at the bottom-left corner of the footprint,
// x right, y up the slide, z up out of the slide (same as the CAD exports).
import * as THREE from 'three';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';
import { CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';

const TYPE_COLORS = {
  inlet: 0x7dd3fc,
  exchange: 0x5eead4,
  split: 0xa78bfa,
  pool: 0x60a5fa,
  outlet: 0xf9a8d4,
};
const PORT_COLORS = { inlet: 0x7dd3fc, outlet: 0xff5fd2 };

// Draw order for see-through parts (lower draws first).
export const ORDER = { capsule: 2, fluid: 3, channel: 4, chamber: 5, glow: 6, layer: 10, edges: 11 };

export class Chip {
  constructor(spec) {
    this.spec = spec;
    this.id = spec.id;
    this.footprint = spec.footprint;
    this.height = spec.layers.reduce((z, l) => z + l.thickness_mm, 0);
    this.top = this.height;
    this.fluidZ = spec.chambers.length
      ? spec.chambers.reduce((z, c) => z + c.center[2], 0) / spec.chambers.length
      : this.height / 2;

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

    // Mesh floor that holds the capsules.
    if (c.mesh_pore_um) {
      const tex = meshTexture();
      tex.repeat.set(size.x / 0.7, size.y / 0.7);
      const floor = new THREE.Mesh(
        new THREE.PlaneGeometry(size.x * 0.98, size.y * 0.98),
        new THREE.MeshBasicMaterial({ map: tex, transparent: true, opacity: 0.55, depthWrite: false, color }),
      );
      floor.position.set(center.x, center.y, floorZ + 0.03);
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
    fluid.position.set(center.x, center.y, floorZ + 0.02);
    fluid.scale.set(size.x * 0.96, size.y * 0.96, 0.001);
    fluid.visible = false;
    fluid.renderOrder = ORDER.fluid;
    this.group.add(fluid);

    // Warm/cool glow for hold_temp.
    const glowMaterial = new THREE.MeshBasicMaterial({
      color: 0xffffff,
      transparent: true,
      opacity: 0,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    const glow = new THREE.Mesh(new THREE.BoxGeometry(size.x + 1.6, size.y + 1.6, size.z + 1.2), glowMaterial);
    glow.position.copy(center);
    glow.visible = false;
    glow.renderOrder = ORDER.glow;
    this.group.add(glow);

    const label = makeLabel(c.id, 'part-label', [0, 0.5]);
    label.position.set(center.x + size.x / 2 + 0.6, center.y, this.top + 0.3);
    this.group.add(label);
    this.labels.push(label);

    const tempLabel = makeLabel('', 'temp-label');
    tempLabel.position.set(center.x, center.y, this.top + 3.5);
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
    const color = PORT_COLORS[p.type] ?? 0x9ca3af;
    const [x, y] = p.position;
    const z0 = this.fluidZ - 0.6;
    const z1 = this.top + 0.5;
    const tube = new THREE.Mesh(
      new THREE.CylinderGeometry(1.1, 1.1, z1 - z0, 32, 1, true).rotateX(Math.PI / 2),
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

    const ringMaterial = new THREE.MeshStandardMaterial({ color, emissive: color, emissiveIntensity: 0.5, roughness: 0.3 });
    const ring = new THREE.Mesh(new THREE.TorusGeometry(1.35, 0.2, 12, 40), ringMaterial);
    ring.position.set(x, y, z1);
    this.group.add(ring);

    const glowMaterial = new THREE.MeshBasicMaterial({
      color: 0xff5fd2,
      transparent: true,
      opacity: 0,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    const glow = new THREE.Mesh(new THREE.SphereGeometry(2.6, 24, 16), glowMaterial);
    glow.position.set(x, y, z1);
    glow.visible = false;
    glow.renderOrder = ORDER.glow;
    this.group.add(glow);

    const label = makeLabel(p.id, 'part-label', [0.5, 1]);
    label.position.set(x, y + 1.6, z1 + 1.2);
    this.group.add(label);
    this.labels.push(label);

    this.parts.set(p.id, {
      kind: 'port',
      id: p.id,
      type: p.type,
      spec: p,
      color,
      center: new THREE.Vector3(x, y, this.fluidZ),
      spawn: new THREE.Vector3(x, y, this.top + 9),
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
    const points = routeChannel(from, to, this.fluidZ);
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
    const w = Math.max(ch.width_mm, 0.35);
    const d = Math.max(ch.depth_mm, 0.35);
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

  // The nearest port of a type ('inlet' | 'outlet') connected to a chamber.
  nearestPort(type, chamberId) {
    let best = null;
    for (const part of this.parts.values()) {
      if (part.kind !== 'port' || part.type !== type) continue;
      const r = type === 'inlet' ? this.route(part.id, chamberId) : this.route(chamberId, part.id);
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

// Channel centreline: leave the source on the side facing the target, then a
// right-angle route (horizontal, vertical, horizontal) like a real manifold.
function routeChannel(from, to, z) {
  const a = from.center;
  const b = to.center;
  const dir = Math.sign(b.x - a.x) || 1;
  const ax = from.kind === 'chamber' ? a.x + dir * (from.size.x / 2) : a.x;
  const bx = to.kind === 'chamber' ? b.x - dir * (to.size.x / 2) : b.x;
  const p0 = new THREE.Vector3(ax, a.y, z);
  const p3 = new THREE.Vector3(bx, b.y, z);
  if (Math.abs(a.y - b.y) < 0.01) return [p0, p3];
  const mid = (ax + bx) / 2;
  return [p0, new THREE.Vector3(mid, a.y, z), new THREE.Vector3(mid, b.y, z), p3];
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

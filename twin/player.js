// Plays a run file: animates each command on the chip.
//
// The player precomputes the chip state (where every capsule is, what liquid is
// in each chamber) before every event. Rendering any moment = draw the state
// before the current event, then animate that event at its progress. That makes
// seeking, stepping and switching runs instant and glitch-free.
//
// Capsules are instanced meshes, so a real-scale chip can show all 10,000 of them.
import * as THREE from 'three';
import { ORDER } from './chip.js';

export const LIBRARY_COLOR = '#ff5fd2';
const CARRIER = 'carrier';

// ------------------------------------------------------------------ colours

function hashHue(name) {
  let h = 0x811c9dc5; // FNV-1a
  for (let i = 0; i < name.length; i++) {
    h ^= name.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return ((h >>> 0) * 0.618033988749895) % 1; // golden-ratio spread
}

// One colour per reagent, the same everywhere (scene, timeline, narration).
export function reagentCss(name) {
  if (!name || name === CARRIER) return '#9fb4d1';
  // Comma syntax: three.js Color.setStyle only parses hsl(h, s%, l%).
  return `hsl(${Math.round(hashHue(name) * 360)}, 72%, 58%)`;
}

const colorCache = new Map();
export function reagentColor(name) {
  const key = name || CARRIER;
  if (!colorCache.has(key)) colorCache.set(key, new THREE.Color().setStyle(reagentCss(key), THREE.SRGBColorSpace));
  return colorCache.get(key);
}

export function tempCss(t) {
  if (t >= 30) return `hsl(${Math.round(38 - Math.min(1, (t - 30) / 60) * 34)}, 95%, 60%)`;
  if (t <= 15) return 'hsl(205, 95%, 65%)';
  return 'hsl(210, 30%, 90%)';
}

// How long each command takes on screen at 1x, in seconds.
export function playDuration(ev) {
  const lg = Math.log10(1 + (ev.duration_s ?? 0));
  switch (ev.command) {
    case 'load': return 3.2;
    case 'exchange': return ev.args.direction === 'backflush' ? 3.6 : 1.0 + 1.5 * (ev.args.washes ?? 1) + 0.25 * lg;
    case 'hold_temp': return 2.2 + 0.6 * lg;
    case 'split':
    case 'pool': return 3.6;
    case 'release': return 4.6;
    default: return 2;
  }
}

// ------------------------------------------------------------------ easing

const clamp01 = (x) => Math.min(1, Math.max(0, x));
const ease = (x) => { x = clamp01(x); return x < 0.5 ? 4 * x * x * x : 1 - (-2 * x + 2) ** 3 / 2; };
const span = (p, a, b) => clamp01((p - a) / (b - a));
const envelope = (p, edge = 0.15) => Math.min(span(p, 0, edge), 1 - span(p, 1 - edge, 1));

// Distance-parameterised polyline.
class Polyline {
  constructor(points) {
    this.points = points.length ? points : [new THREE.Vector3()];
    this.cum = [0];
    for (let i = 1; i < this.points.length; i++) {
      this.cum.push(this.cum[i - 1] + this.points[i].distanceTo(this.points[i - 1]));
    }
    this.length = this.cum[this.cum.length - 1];
  }
  get start() { return this.points[0]; }
  get end() { return this.points[this.points.length - 1]; }
  at(t, out = new THREE.Vector3()) {
    if (this.length === 0) return out.copy(this.points[0]);
    const d = clamp01(t) * this.length;
    let i = 1;
    while (i < this.cum.length - 1 && this.cum[i] < d) i++;
    const seg = this.cum[i] - this.cum[i - 1] || 1;
    return out.copy(this.points[i - 1]).lerp(this.points[i], (d - this.cum[i - 1]) / seg);
  }
}

// ------------------------------------------------------------------ state model

function emptyState() {
  return { capsules: [], fluid: {}, library: {}, nextId: 0 };
}

function cloneState(s) {
  return {
    capsules: s.capsules.map((c) => ({ ...c })),
    fluid: Object.fromEntries(Object.entries(s.fluid).map(([k, v]) => [k, { ...v }])),
    library: { ...s.library },
    nextId: s.nextId,
  };
}

function applyEvent(prev, ev, chip) {
  const s = cloneState(prev);
  const a = ev.args;
  const inChamber = (id) => s.capsules.filter((c) => c.alive && c.chamber === id);
  switch (ev.command) {
    case 'load':
      for (let i = 0; i < chip.capsuleCount; i++) {
        s.capsules.push({ id: s.nextId++, chamber: a.target, alive: true, suspended: false });
      }
      s.fluid[a.target] = { reagent: CARRIER, level: 0.55 };
      break;
    case 'exchange':
      s.fluid[a.target] = { reagent: a.reagent, level: 0.8 };
      // Back-flush lifts capsules off the mesh; a normal wash settles them back onto it.
      inChamber(a.target).forEach((c) => { c.suspended = a.direction === 'backflush'; });
      break;
    case 'split': {
      const from = s.fluid[a.source];
      inChamber(a.source).forEach((c, i) => { c.chamber = a.targets[i % a.targets.length]; c.suspended = false; });
      for (const t of a.targets) s.fluid[t] = { reagent: from?.reagent ?? CARRIER, level: 0.6 };
      delete s.fluid[a.source];
      break;
    }
    case 'pool': {
      const first = a.sources.map((id) => s.fluid[id]).find(Boolean);
      for (const src of a.sources) {
        inChamber(src).forEach((c) => { c.chamber = a.target; c.suspended = false; });
        delete s.fluid[src];
      }
      s.fluid[a.target] = { reagent: first?.reagent ?? CARRIER, level: 0.7 };
      break;
    }
    case 'release': {
      inChamber(a.target).forEach((c) => { c.alive = false; });
      s.fluid[a.target] = { reagent: a.reagent, level: 0.35 };
      const recovery = capsuleOutlet(chip, a.target);
      const out = recovery ?? chip.findPort(a.target, { type: 'outlet' });
      if (out && !recovery) s.library[out] = true;
      break;
    }
    default:
      break;
  }
  return s;
}

// An outlet the spec says capsules can pass (e.g. a release outlet), or null.
function capsuleOutlet(chip, chamberId) {
  const id = chip.findPort(chamberId, { type: 'outlet', capsules: true });
  return id && chip.parts.get(id).capsulePassable === true ? id : null;
}

// Resting spot for each capsule: a jittered grid on the chamber's mesh floor, or a
// random spot in the chamber volume while suspended (after a back-flush).
function assignHomes(state, chip) {
  const r = chip.capsuleRadius;
  const homes = new Map();
  const byChamber = new Map();
  for (const c of state.capsules) {
    if (!c.alive) continue;
    if (!byChamber.has(c.chamber)) byChamber.set(c.chamber, []);
    byChamber.get(c.chamber).push(c);
  }
  for (const [cid, caps] of byChamber) {
    const ch = chip.chamber(cid);
    if (!ch) continue;
    const margin = Math.min(0.3, Math.min(ch.size.x, ch.size.y) * 0.02);
    const usableX = Math.max(ch.size.x - 2 * r - margin, 0.01);
    const usableY = Math.max(ch.size.y - 2 * r - margin, 0.01);
    const settled = caps.filter((c) => !c.suspended);
    const n = settled.length;
    const cols = Math.max(1, Math.round(Math.sqrt((n * ch.size.x) / ch.size.y)));
    const rows = Math.max(1, Math.ceil(n / cols));
    settled.forEach((c, i) => {
      const col = i % cols;
      const row = Math.floor(i / cols);
      const jx = (rand(c.id * 2 + 1) - 0.5) * 0.35 * (usableX / cols);
      const jy = (rand(c.id * 2 + 2) - 0.5) * 0.35 * (usableY / rows);
      homes.set(c.id, {
        chamber: cid,
        pos: new THREE.Vector3(
          ch.center.x - usableX / 2 + ((col + 0.5) / cols) * usableX + jx,
          ch.center.y - usableY / 2 + ((row + 0.5) / rows) * usableY + jy,
          ch.floorZ + r * 1.05,
        ),
      });
    });
    const headroom = Math.max(ch.size.z - 2.1 * r, 0);
    for (const c of caps.filter((x) => x.suspended)) {
      homes.set(c.id, {
        chamber: cid,
        suspended: true,
        pos: new THREE.Vector3(
          ch.center.x - usableX / 2 + rand(c.id * 3 + 7) * usableX,
          ch.center.y - usableY / 2 + rand(c.id * 3 + 8) * usableY,
          ch.floorZ + r * 1.05 + (0.35 + 0.65 * rand(c.id * 3 + 9)) * headroom,
        ),
      });
    }
  }
  return homes;
}

function rand(seed) {
  const x = Math.sin(seed * 12.9898) * 43758.5453;
  return x - Math.floor(x);
}

// ------------------------------------------------------------------ player

const _m = new THREE.Matrix4();
const _q = new THREE.Quaternion();
const _s = new THREE.Vector3();
const _p = new THREE.Vector3();
const _a = new THREE.Vector3();

export class Player {
  constructor(ctx, handlers = {}) {
    this.ctx = ctx;
    this.handlers = handlers; // onEvent(i, ev), onTick(info), onPlayState(playing)
    this.chip = null;
    this.run = null;
    this.events = [];
    this.playing = false;
    this.speed = 1;
    this.t = 0;
    this.stopAt = null;
    this.fade = 1;
    this.fadeTarget = 1;
    this.currentIndex = -1;
    this.cap = null; // instanced capsule meshes
    this.blobs = [];
    this.paths = new Map();
    ctx.onFrame((dt) => this.update(dt));
  }

  setChip(chip) {
    this.#disposeCapsules();
    for (const b of this.blobs) b.removeFromParent();
    this.blobs = [];
    this.chip = chip;
    const blobGeometry = new THREE.SphereGeometry(0.55 * chip.u, 16, 12);
    for (let i = 0; i < 3; i++) {
      const blob = new THREE.Mesh(
        blobGeometry,
        new THREE.MeshBasicMaterial({ color: LIBRARY_COLOR, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false }),
      );
      blob.visible = false;
      blob.renderOrder = ORDER.glow;
      chip.group.add(blob);
      this.blobs.push(blob);
    }
  }

  load(run, { autoplay = false } = {}) {
    this.run = run;
    this.events = run.events;
    this.paths.clear();
    // Timeline: when each event starts, in playback seconds at 1x.
    this.starts = [];
    let t = 0;
    for (const ev of this.events) {
      this.starts.push(t);
      t += playDuration(ev);
    }
    this.total = t;
    // Chip state before each event (states[n] = final).
    this.states = [emptyState()];
    for (const ev of this.events) this.states.push(applyEvent(this.states[this.states.length - 1], ev, this.chip));
    this.homes = this.states.map((s) => assignHomes(s, this.chip));
    this.#ensureCapsules(this.states[this.states.length - 1].nextId);
    this.t = 0;
    this.stopAt = null;
    this.currentIndex = -1;
    this.fadeTarget = 1;
    this.render();
    this.setPlaying(autoplay && this.events.length > 0);
  }

  // Fade capsules and liquid out (used before switching runs). Resolves when done.
  fadeOut() {
    this.fadeTarget = 0;
    if (this.fade === 0) return Promise.resolve();
    return new Promise((resolve) => {
      const done = () => { this._fadeResolve = null; resolve(); };
      this._fadeResolve = done;
      setTimeout(() => { if (this._fadeResolve === done) { this.fade = 0; done(); } }, 600); // never wait on a slow frame rate
    });
  }

  timeline() {
    return this.events.map((ev, i) => ({ ev, start: this.starts[i], duration: playDuration(ev) }));
  }

  // Redraw and re-announce the current event (e.g. after the UI rebuilt).
  refresh() {
    this.currentIndex = -2;
    this.render();
  }

  setPlaying(on) {
    if (on && this.t >= this.total - 1e-6) this.t = 0; // replay from the start
    this.playing = on;
    if (!on) this.stopAt = null;
    this.handlers.onPlayState?.(on);
  }

  toggle() { this.setPlaying(!this.playing); }

  setSpeed(x) { this.speed = x; }

  // Play to the end of the current command (or the next one, if paused at a
  // boundary), then pause.
  step() {
    if (!this.events.length) return;
    if (this.t >= this.total - 1e-6) this.t = 0;
    const i = this.#indexAt(this.t);
    this.stopAt = i + 1 < this.events.length ? this.starts[i + 1] : this.total;
    this.playing = true;
    this.handlers.onPlayState?.(true);
  }

  seek(t) {
    this.t = Math.min(Math.max(t, 0), this.total);
    this.stopAt = null;
    this.render();
  }

  seekToEvent(i) {
    this.seek(this.starts[Math.max(0, Math.min(i, this.events.length - 1))] ?? 0);
  }

  update(dt) {
    if (this.fade !== this.fadeTarget) {
      const k = this.fadeTarget > this.fade ? dt / 0.35 : -dt / 0.22;
      this.fade = this.fadeTarget > this.fade ? Math.min(this.fadeTarget, this.fade + k) : Math.max(this.fadeTarget, this.fade + k);
      if (this.fade === 0 && this._fadeResolve) { this._fadeResolve(); this._fadeResolve = null; }
    }
    if (!this.run) return;
    if (this.playing) {
      this.t += dt * this.speed;
      if (this.stopAt !== null && this.t >= this.stopAt) {
        this.t = this.stopAt;
        this.setPlaying(false);
      }
      if (this.t >= this.total) {
        this.t = this.total;
        this.setPlaying(false);
      }
    }
    this.render();
  }

  #indexAt(t) {
    let i = 0;
    while (i + 1 < this.starts.length && this.starts[i + 1] <= t + 1e-9) i++;
    return i;
  }

  // ---------------------------------------------------------------- capsules

  #disposeCapsules() {
    if (!this.cap) return;
    for (const m of [this.cap.shell, this.cap.core]) {
      if (!m) continue;
      m.removeFromParent();
      m.geometry.dispose();
      m.material.dispose();
    }
    this.cap = null;
  }

  #ensureCapsules(n) {
    if (this.cap && this.cap.capacity >= n) return;
    this.#disposeCapsules();
    const r = this.chip.capsuleRadius;
    const many = n > 400;
    const capacity = Math.max(n, 1);
    const shellMat = many
      ? new THREE.MeshStandardMaterial({ color: 0xf1e4cf, emissive: 0x3a2a18, roughness: 0.45, transparent: true, opacity: 0.95 })
      : new THREE.MeshPhysicalMaterial({ color: 0xdff3ff, transparent: true, opacity: 0.5, roughness: 0.1, clearcoat: 1, depthWrite: false });
    const shell = new THREE.InstancedMesh(many ? new THREE.IcosahedronGeometry(r, 1) : new THREE.SphereGeometry(r, 20, 14), shellMat, capacity);
    shell.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    shell.frustumCulled = false;
    shell.renderOrder = ORDER.capsule;
    this.chip.group.add(shell);
    let core = null;
    if (!many) {
      const coreMat = new THREE.MeshStandardMaterial({ color: 0xffb38a, emissive: 0x7a3010, emissiveIntensity: 0.35, roughness: 0.5, transparent: true });
      core = new THREE.InstancedMesh(new THREE.SphereGeometry(r * 0.42, 12, 10), coreMat, capacity);
      core.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
      core.frustumCulled = false;
      this.chip.group.add(core);
    }
    this.cap = {
      shell, core, capacity, shellOpacity: shellMat.opacity,
      pos: new Float32Array(capacity * 3), scale: new Float32Array(capacity),
    };
  }

  #setCap(id, v, scale = 1) {
    const c = this.cap;
    c.pos[id * 3] = v.x;
    c.pos[id * 3 + 1] = v.y;
    c.pos[id * 3 + 2] = v.z;
    c.scale[id] = scale;
  }

  #flushCapsules() {
    const c = this.cap;
    if (!c) return;
    for (let id = 0; id < c.capacity; id++) {
      _p.set(c.pos[id * 3], c.pos[id * 3 + 1], c.pos[id * 3 + 2]);
      _m.compose(_p, _q, _s.setScalar(c.scale[id] || 1e-6));
      c.shell.setMatrixAt(id, _m);
      c.core?.setMatrixAt(id, _m);
    }
    c.shell.instanceMatrix.needsUpdate = true;
    if (c.core) c.core.instanceMatrix.needsUpdate = true;
    c.shell.material.opacity = c.shellOpacity * this.fade;
    if (c.core) c.core.material.opacity = this.fade;
    c.shell.visible = this.fade > 0.001;
    if (c.core) c.core.visible = this.fade > 0.001;
  }

  #path(key, build) {
    if (!this.paths.has(key)) this.paths.set(key, new Polyline(build()));
    return this.paths.get(key);
  }

  // ---------------------------------------------------------------- drawing

  render() {
    if (!this.run || !this.chip || !this.cap) return;
    const n = this.events.length;
    const done = n === 0 || this.t >= this.total - 1e-6;
    const i = done ? n : this.#indexAt(this.t);
    const p = done ? 1 : clamp01((this.t - this.starts[i]) / playDuration(this.events[i]));

    this.#drawState(i);
    if (!done) this.#animate(i, p);
    this.#flushCapsules();

    const shown = done ? n - 1 : i;
    if (shown !== this.currentIndex) {
      this.currentIndex = shown;
      this.handlers.onEvent?.(shown, this.events[shown]);
    }
    const ev = this.events[Math.max(shown, 0)];
    const protoT = ev ? ev.t_s + (done ? (ev.duration_s ?? 0) : p * (ev.duration_s ?? 0)) : 0;
    this.handlers.onTick?.({ t: this.t, total: this.total, protocolT: protoT, index: shown, progress: p });
  }

  #drawState(i) {
    const state = this.states[i];
    const homes = this.homes[i];
    for (let id = 0; id < this.cap.capacity; id++) {
      const home = homes.get(id);
      if (home) this.#setCap(id, home.pos, 1);
      else this.cap.scale[id] = 0;
    }
    for (const part of this.chip.parts.values()) {
      if (part.kind === 'chamber') {
        const fl = state.fluid[part.id];
        this.#setFluid(part, fl?.reagent, fl?.level ?? 0);
        part.glow.visible = false;
        part.tempLabel.visible = false;
        part.material.emissive.setHex(part.color);
        part.material.emissiveIntensity = 0.08;
      } else {
        const lib = Boolean(state.library[part.id]);
        part.glow.visible = lib && this.fade > 0.001;
        part.glowMaterial.color.set(LIBRARY_COLOR);
        part.glowMaterial.opacity = 0.35 * this.fade;
      }
    }
    for (const ch of this.chip.channels) {
      ch.material.emissive.setHex(0x3b82f6);
      ch.material.emissiveIntensity = 0.35;
      ch.material.opacity = 0.5;
    }
    for (const b of this.blobs) b.visible = false;
  }

  #setFluid(part, reagent, level, color = null) {
    const lv = clamp01(level);
    part.fluid.visible = lv > 0.005 && this.fade > 0.001;
    part.fluid.scale.z = Math.max(lv * part.size.z * 0.96, 0.0001);
    const c = color ?? reagentColor(reagent);
    part.fluidMaterial.color.copy(c);
    part.fluidMaterial.emissive.copy(c);
    part.fluidMaterial.opacity = 0.55 * this.fade;
  }

  #pulseChannels(channels, color, strength) {
    for (const ch of channels) {
      ch.material.emissive.copy(color);
      ch.material.emissiveIntensity = 0.35 + 1.4 * strength;
      ch.material.opacity = 0.5 + 0.4 * strength;
    }
  }

  // Capsules that stay in the same chamber but move (settling, lifting) glide there.
  #settle(i, p, wobble = 0) {
    const before = this.homes[i];
    const after = this.homes[i + 1];
    const e = ease(p);
    const r = this.chip.capsuleRadius;
    for (const [id, b] of before) {
      const a = after.get(id);
      if (!a || a.chamber !== b.chamber || a.pos.equals(b.pos)) continue;
      _a.copy(b.pos).lerp(a.pos, e);
      if (wobble) {
        const w = wobble * r * Math.sin(Math.PI * e);
        _a.x += Math.sin(p * 9 + id) * w;
        _a.y += Math.cos(p * 7 + id * 1.3) * w;
      }
      this.#setCap(id, _a, 1);
    }
  }

  #animate(i, p) {
    const ev = this.events[i];
    const a = ev.args;
    const before = this.states[i];
    const after = this.states[i + 1];
    const chip = this.chip;

    switch (ev.command) {
      case 'load': {
        const part = chip.chamber(a.target);
        const inlet = chip.findPort(a.target, { type: 'inlet', capsules: true }) ?? chip.findPort(a.target, { type: 'inlet' });
        const path = this.#path(`load|${i}`, () => (inlet
          ? [chip.parts.get(inlet).spawn.clone(), ...chip.pathPoints(inlet, a.target)]
          : [part.center.clone().setZ(chip.top + chip.spawnHeight), part.center.clone()]));
        this.#settle(i, p);
        this.#arrive(i, p, path);
        const fl = before.fluid[a.target];
        this.#setFluid(part, CARRIER, THREE.MathUtils.lerp(fl?.level ?? 0, after.fluid[a.target].level, ease(span(p, 0.1, 0.8))));
        if (inlet) this.#pulseChannels(chip.channelsBetween(inlet, a.target), reagentColor(CARRIER), envelope(p));
        break;
      }
      case 'exchange': {
        const part = chip.chamber(a.target);
        const newColor = reagentColor(a.reagent);
        const old = before.fluid[a.target];
        if (a.direction === 'backflush') {
          // Liquid comes up through the mesh from below and lifts the capsules.
          const level = THREE.MathUtils.lerp((old?.level ?? 0) * 0.3, after.fluid[a.target].level, ease(span(p, 0, 0.55)));
          const color = old ? reagentColor(old.reagent).clone().lerp(newColor, ease(span(p, 0, 0.5))) : newColor;
          this.#setFluid(part, a.reagent, level, color);
          this.#settle(i, span(p, 0.12, 0.9), 1.6);
          const bf = chip.findPort(a.target, { type: 'inlet', role: 'backflush' }) ?? chip.findPort(a.target, { type: 'inlet', capsules: false });
          if (bf) this.#pulseChannels(chip.channelsBetween(bf, a.target), newColor, envelope(span(p, 0, 0.7), 0.2));
          break;
        }
        const washes = Math.max(1, a.washes ?? 1);
        const cycle = Math.min(Math.floor(p * washes), washes - 1);
        const cp = p * washes - cycle;
        const last = cycle === washes - 1;
        let level;
        if (cp < 0.4) level = THREE.MathUtils.lerp(cycle === 0 ? (old?.level ?? 0) * 0.5 : 0.08, 1, ease(cp / 0.4));
        else if (cp < 0.6) level = 1;
        else level = THREE.MathUtils.lerp(1, last ? after.fluid[a.target].level : 0.08, ease((cp - 0.6) / 0.4));
        const color = cycle === 0 && old ? reagentColor(old.reagent).clone().lerp(newColor, ease(cp / 0.4)) : newColor;
        this.#setFluid(part, a.reagent, level, color);
        this.#settle(i, p);
        // Capsules sway in the flow but stay on the mesh.
        const r = chip.capsuleRadius;
        for (const [id, home] of this.homes[i + 1]) {
          if (home.chamber !== a.target || home.suspended) continue;
          const sway = Math.sin(p * washes * Math.PI * 4 + id) * 0.2 * r * (level > 0.3 ? 1 : 0.3);
          if (this.homes[i].get(id)?.suspended) continue; // already gliding down
          _a.set(home.pos.x + sway, home.pos.y, home.pos.z + Math.abs(sway) * 0.3);
          this.#setCap(id, _a, 1);
        }
        const inlet = chip.findPort(a.target, { type: 'inlet', capsules: true }) ?? chip.findPort(a.target, { type: 'inlet' });
        const feed = inlet ? chip.channelsBetween(inlet, a.target) : [];
        this.#pulseChannels(feed, newColor, cp < 0.45 ? Math.sin((cp / 0.45) * Math.PI) : 0);
        const waste = chip.findPort(a.target, { type: 'outlet', role: 'waste' }) ?? chip.findPort(a.target, { type: 'outlet', capsules: false });
        if (waste) this.#pulseChannels(chip.channelsBetween(a.target, waste), color, cp > 0.6 ? Math.sin(((cp - 0.6) / 0.4) * Math.PI) : 0);
        break;
      }
      case 'hold_temp': {
        const part = chip.chamber(a.target);
        const env = envelope(p);
        const color = new THREE.Color().setStyle(tempCss(a.temp_c), THREE.SRGBColorSpace);
        part.glow.visible = true;
        part.glowMaterial.color.copy(color);
        part.glowMaterial.opacity = 0.32 * env * (0.85 + 0.15 * Math.sin(p * Math.PI * 6));
        part.material.emissive.copy(color);
        part.material.emissiveIntensity = 0.08 + 0.9 * env;
        part.tempLabel.visible = env > 0.02;
        part.tempLabel.element.textContent = `${a.temp_c} °C · ${formatDuration(a.duration_s)}`;
        part.tempLabel.element.style.opacity = String(env);
        part.tempLabel.element.style.borderColor = tempCss(a.temp_c);
        break;
      }
      case 'split':
      case 'pool': {
        this.#travel(i, p);
        const sources = ev.command === 'split' ? [a.source] : a.sources;
        const targets = ev.command === 'split' ? a.targets : [a.target];
        for (const s of sources) {
          const fl = before.fluid[s];
          if (fl) this.#setFluid(chip.chamber(s), fl.reagent, fl.level * (1 - ease(span(p, 0, 0.6))));
        }
        for (const t of targets) {
          const fl = after.fluid[t];
          const prev = before.fluid[t]?.level ?? 0;
          if (fl) this.#setFluid(chip.chamber(t), fl.reagent, THREE.MathUtils.lerp(prev, fl.level, ease(span(p, 0.3, 1))));
        }
        const used = new Set();
        for (const s of sources) for (const t of targets) chip.channelsBetween(s, t).forEach((c) => used.add(c));
        const firstFluid = sources.map((s) => before.fluid[s]).find(Boolean);
        this.#pulseChannels([...used], reagentColor(firstFluid?.reagent), envelope(p, 0.2));
        break;
      }
      case 'release': {
        const part = chip.chamber(a.target);
        const old = before.fluid[a.target];
        const recovery = capsuleOutlet(chip, a.target);
        if (recovery) {
          // The washed capsules themselves leave through the release outlet.
          const port = chip.parts.get(recovery);
          const path = this.#path(`rel|${i}`, () => [...chip.pathPoints(a.target, recovery), port.spawn.clone()]);
          this.#leave(i, p, path);
          this.#setFluid(part, a.reagent, THREE.MathUtils.lerp(Math.max(old?.level ?? 0, 0.8), after.fluid[a.target].level, ease(span(p, 0.4, 1))),
            reagentColor(a.reagent));
          this.#pulseChannels(chip.channelsBetween(a.target, recovery), reagentColor(a.reagent), envelope(p, 0.2));
          port.glow.visible = true;
          port.glowMaterial.color.copy(reagentColor(a.reagent));
          port.glowMaterial.opacity = 0.3 * envelope(span(p, 0.3, 1), 0.3) * this.fade;
          break;
        }
        const fade = 1 - ease(span(p, 0.05, 0.5));
        for (const [id, home] of this.homes[i]) {
          if (home.chamber !== a.target) continue;
          this.#setCap(id, home.pos, Math.max(fade, 0) * (0.4 + 0.6 * fade));
        }
        const lib = new THREE.Color().setStyle(LIBRARY_COLOR, THREE.SRGBColorSpace);
        const fluidColor = (old ? reagentColor(old.reagent).clone() : reagentColor(a.reagent).clone())
          .lerp(reagentColor(a.reagent), ease(span(p, 0, 0.3)))
          .lerp(lib, 0.6 * envelope(span(p, 0.2, 0.9), 0.3));
        this.#setFluid(part, a.reagent,
          THREE.MathUtils.lerp(Math.max(old?.level ?? 0, 0.8), after.fluid[a.target].level, ease(span(p, 0.5, 1))), fluidColor);
        // The library flows out to the outlet.
        const outlet = chip.findPort(a.target, { type: 'outlet' });
        if (outlet) {
          const path = this.#path(`lib|${i}`, () => [...chip.pathPoints(a.target, outlet), chip.parts.get(outlet).center.clone()]);
          this.blobs.forEach((blob, k) => {
            const lp = span(p, 0.3 + k * 0.08, 0.85 + k * 0.05);
            blob.visible = lp > 0 && lp < 1;
            path.at(ease(lp), blob.position);
            blob.material.opacity = 0.9 * this.fade;
          });
          this.#pulseChannels(chip.channelsBetween(a.target, outlet), lib, envelope(span(p, 0.25, 1), 0.2));
          const port = chip.parts.get(outlet);
          port.glow.visible = true;
          port.glowMaterial.color.set(LIBRARY_COLOR);
          port.glowMaterial.opacity = 0.35 * ease(span(p, 0.75, 1)) * this.fade;
        }
        break;
      }
      default:
        break;
    }
  }

  // New capsules (load) stream down `path` and fan out to their homes.
  #arrive(i, p, path) {
    const homesBefore = this.homes[i];
    const movers = [];
    for (const [id, home] of this.homes[i + 1]) if (!homesBefore.has(id)) movers.push([id, home]);
    const m = movers.length;
    movers.forEach(([id, home], k) => {
      const delay = m > 1 ? (0.45 * k) / (m - 1) : 0;
      const local = span(p, delay, delay + 0.5);
      if (local <= 0) { this.cap.scale[id] = 0; return; }
      const e = ease(local);
      path.at(e, _a);
      _a.addScaledVector(_p.copy(home.pos).sub(path.end), e * e);
      this.#setCap(id, _a, 1);
    });
  }

  // Capsules that change chamber (split, pool) travel along the channels.
  #travel(i, p) {
    const homesBefore = this.homes[i];
    const movers = [];
    for (const [id, after] of this.homes[i + 1]) {
      const before = homesBefore.get(id);
      if (before && before.chamber !== after.chamber) movers.push([id, before, after]);
    }
    const m = movers.length;
    movers.forEach(([id, before, after], k) => {
      const path = this.#path(`mv|${before.chamber}|${after.chamber}`, () => this.chip.pathPoints(before.chamber, after.chamber));
      const delay = m > 1 ? (0.4 * k) / (m - 1) : 0;
      const e = ease(span(p, delay, delay + 0.55));
      path.at(e, _a);
      _a.addScaledVector(_p.copy(before.pos).sub(path.start), 1 - e);
      _a.addScaledVector(_p.copy(after.pos).sub(path.end), e);
      this.#setCap(id, _a, 1);
    });
  }

  // Capsules leaving the chip (release through a capsule outlet).
  #leave(i, p, path) {
    const leaving = [];
    for (const [id, before] of this.homes[i]) if (!this.homes[i + 1].has(id)) leaving.push([id, before]);
    const m = leaving.length;
    leaving.forEach(([id, before], k) => {
      const delay = m > 1 ? (0.45 * k) / (m - 1) : 0;
      const local = span(p, delay, delay + 0.5);
      if (local >= 1) { this.cap.scale[id] = 0; return; }
      const e = ease(local);
      path.at(e, _a);
      _a.addScaledVector(_p.copy(before.pos).sub(path.start), 1 - e);
      this.#setCap(id, _a, 1);
    });
  }
}

export function formatDuration(s) {
  if (s == null) return '';
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
}

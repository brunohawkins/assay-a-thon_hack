// A small STL viewer written for this tool: raw WebGL, no library, nothing from the
// network. Orbit with drag or the arrow keys, zoom with the wheel or +/-, pan with
// shift-drag, reset with 0 or Home.
//
// S11 writes one STL per feature into cad/exports/<chip_id>/, so the viewer takes several
// buffers and colours them separately - that is what makes the 3D view readable.

/* ------------------------------------------------------------------ parsing */

/** Parse a binary or ASCII STL into {positions, normals, triangles, bbox}. */
export function parseStl(buffer) {
  const view = new DataView(buffer);
  if (buffer.byteLength >= 84) {
    const tris = view.getUint32(80, true);
    if (tris > 0 && 84 + tris * 50 === buffer.byteLength) return parseBinary(view, tris);
  }
  const head = new TextDecoder().decode(new Uint8Array(buffer, 0, Math.min(512, buffer.byteLength)));
  if (/^\s*solid/i.test(head)) return parseAscii(new TextDecoder().decode(new Uint8Array(buffer)));
  if (buffer.byteLength >= 84) return parseBinary(view, view.getUint32(80, true));
  throw new Error('not an STL file');
}

function parseBinary(view, tris) {
  const positions = new Float32Array(tris * 9);
  const normals = new Float32Array(tris * 9);
  let o = 84;
  for (let t = 0; t < tris; t += 1) {
    const nx = view.getFloat32(o, true), ny = view.getFloat32(o + 4, true), nz = view.getFloat32(o + 8, true);
    o += 12;
    for (let v = 0; v < 3; v += 1) {
      const i = t * 9 + v * 3;
      positions[i] = view.getFloat32(o, true);
      positions[i + 1] = view.getFloat32(o + 4, true);
      positions[i + 2] = view.getFloat32(o + 8, true);
      normals[i] = nx; normals[i + 1] = ny; normals[i + 2] = nz;
      o += 12;
    }
    o += 2; // attribute byte count
  }
  return finish(positions, normals, tris);
}

function parseAscii(text) {
  const pos = [];
  const nrm = [];
  let n = [0, 0, 1];
  const re = /(facet\s+normal|vertex)\s+(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)/g;
  let m;
  while ((m = re.exec(text))) {
    const v = [parseFloat(m[2]), parseFloat(m[3]), parseFloat(m[4])];
    if (m[1][0] === 'f') n = v;
    else { pos.push(v[0], v[1], v[2]); nrm.push(n[0], n[1], n[2]); }
  }
  return finish(new Float32Array(pos), new Float32Array(nrm), pos.length / 9);
}

function finish(positions, normals, tris) {
  const bbox = { min: [Infinity, Infinity, Infinity], max: [-Infinity, -Infinity, -Infinity] };
  for (let i = 0; i < positions.length; i += 3) {
    for (let a = 0; a < 3; a += 1) {
      const v = positions[i + a];
      if (v < bbox.min[a]) bbox.min[a] = v;
      if (v > bbox.max[a]) bbox.max[a] = v;
    }
  }
  // Replace zero-length facet normals with a computed one.
  for (let i = 0; i < normals.length; i += 9) {
    if (normals[i] || normals[i + 1] || normals[i + 2]) continue;
    const ax = positions[i + 3] - positions[i], ay = positions[i + 4] - positions[i + 1], az = positions[i + 5] - positions[i + 2];
    const bx = positions[i + 6] - positions[i], by = positions[i + 7] - positions[i + 1], bz = positions[i + 8] - positions[i + 2];
    let nx = ay * bz - az * by, ny = az * bx - ax * bz, nz = ax * by - ay * bx;
    const len = Math.hypot(nx, ny, nz) || 1;
    nx /= len; ny /= len; nz /= len;
    for (let v = 0; v < 3; v += 1) { normals[i + v * 3] = nx; normals[i + v * 3 + 1] = ny; normals[i + v * 3 + 2] = nz; }
  }
  return { positions, normals, triangles: tris, bbox };
}

/* ------------------------------------------------------------------ matrices */

function mul(a, b) {
  const o = new Float32Array(16);
  for (let r = 0; r < 4; r += 1) for (let c = 0; c < 4; c += 1) {
    o[c * 4 + r] = a[r] * b[c * 4] + a[4 + r] * b[c * 4 + 1] + a[8 + r] * b[c * 4 + 2] + a[12 + r] * b[c * 4 + 3];
  }
  return o;
}

function perspective(fovy, aspect, near, far) {
  const f = 1 / Math.tan(fovy / 2);
  const nf = 1 / (near - far);
  return new Float32Array([f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) * nf, -1, 0, 0, 2 * far * near * nf, 0]);
}

function lookAt(eye, target, up) {
  const z = norm([eye[0] - target[0], eye[1] - target[1], eye[2] - target[2]]);
  const x = norm(cross(up, z));
  const y = cross(z, x);
  return new Float32Array([
    x[0], y[0], z[0], 0,
    x[1], y[1], z[1], 0,
    x[2], y[2], z[2], 0,
    -dot(x, eye), -dot(y, eye), -dot(z, eye), 1,
  ]);
}

function translation(t) {
  return new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, t[0], t[1], t[2], 1]);
}

const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
function norm(v) { const l = Math.hypot(v[0], v[1], v[2]) || 1; return [v[0] / l, v[1] / l, v[2] / l]; }

function mat3of(m) {
  return new Float32Array([m[0], m[1], m[2], m[4], m[5], m[6], m[8], m[9], m[10]]);
}

/* ------------------------------------------------------------------ shaders */

const VS = `
attribute vec3 aPos;
attribute vec3 aNrm;
uniform mat4 uMVP;
uniform mat3 uNM;
varying vec3 vN;
void main() {
  vN = uNM * aNrm;
  gl_Position = uMVP * vec4(aPos, 1.0);
}`;

const FS = `
precision mediump float;
varying vec3 vN;
uniform vec3 uColor;
void main() {
  vec3 n = normalize(vN);
  if (!gl_FrontFacing) n = -n;
  float key = max(dot(n, normalize(vec3(0.35, 0.55, 0.75))), 0.0);
  float fill = max(dot(n, normalize(vec3(-0.6, -0.2, 0.3))), 0.0) * 0.35;
  float rim = pow(1.0 - abs(n.z), 3.0) * 0.25;
  vec3 c = uColor * (0.22 + 0.72 * key + fill) + vec3(0.35, 0.55, 0.75) * rim;
  gl_FragColor = vec4(c, 1.0);
}`;

const LINE_VS = `
attribute vec3 aPos;
uniform mat4 uMVP;
void main() { gl_Position = uMVP * vec4(aPos, 1.0); }`;

const LINE_FS = `
precision mediump float;
uniform vec3 uColor;
void main() { gl_FragColor = vec4(uColor, 0.55); }`;

function compile(gl, vsSrc, fsSrc) {
  const make = (type, src) => {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s) || 'shader failed');
    return s;
  };
  const p = gl.createProgram();
  gl.attachShader(p, make(gl.VERTEX_SHADER, vsSrc));
  gl.attachShader(p, make(gl.FRAGMENT_SHADER, fsSrc));
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p) || 'link failed');
  return p;
}

/* ------------------------------------------------------------------ viewer */

export const PALETTE = [
  [0.37, 0.92, 0.83], [0.49, 0.83, 0.99], [0.65, 0.55, 0.98], [0.98, 0.75, 0.29],
  [0.95, 0.45, 0.68], [0.55, 0.89, 0.52], [0.98, 0.58, 0.24], [0.69, 0.78, 0.93],
];

export class StlViewer {
  constructor(canvas) {
    this.canvas = canvas;
    this.meshes = [];
    this.theta = -0.9;
    this.phi = 0.95;
    this.radius = 2.6;
    this.pan = [0, 0];
    this.center = [0, 0, 0];
    this.scale = 1;
    this.showBox = true;
    this.disposed = false;
    this._dirty = true;

    const opts = { antialias: true, alpha: false, preserveDrawingBuffer: false, powerPreference: 'low-power' };
    this.gl = canvas.getContext('webgl', opts) || canvas.getContext('experimental-webgl', opts);
    if (!this.gl) throw new Error('WebGL is not available in this browser');
    const gl = this.gl;
    this.prog = compile(gl, VS, FS);
    this.lineProg = compile(gl, LINE_VS, LINE_FS);
    this.loc = {
      aPos: gl.getAttribLocation(this.prog, 'aPos'),
      aNrm: gl.getAttribLocation(this.prog, 'aNrm'),
      uMVP: gl.getUniformLocation(this.prog, 'uMVP'),
      uNM: gl.getUniformLocation(this.prog, 'uNM'),
      uColor: gl.getUniformLocation(this.prog, 'uColor'),
    };
    this.lineLoc = {
      aPos: gl.getAttribLocation(this.lineProg, 'aPos'),
      uMVP: gl.getUniformLocation(this.lineProg, 'uMVP'),
      uColor: gl.getUniformLocation(this.lineProg, 'uColor'),
    };
    gl.enable(gl.DEPTH_TEST);
    gl.clearColor(0.016, 0.027, 0.055, 1);

    this._bindInput();
    this._loop = () => {
      if (this.disposed) return;
      if (this._dirty) { this._dirty = false; this.draw(); }
      this._raf = requestAnimationFrame(this._loop);
    };
    this._raf = requestAnimationFrame(this._loop);

    if (typeof ResizeObserver !== 'undefined') {
      this._ro = new ResizeObserver(() => this.invalidate());
      this._ro.observe(canvas);
    } else {
      this._onResize = () => this.invalidate();
      addEventListener('resize', this._onResize);
    }
  }

  invalidate() { this._dirty = true; }

  /** meshes: [{name, buffer, color?}] - replaces everything on screen. */
  setMeshes(items) {
    const gl = this.gl;
    for (const m of this.meshes) { gl.deleteBuffer(m.pos); gl.deleteBuffer(m.nrm); }
    this.meshes = [];
    const bbox = { min: [Infinity, Infinity, Infinity], max: [-Infinity, -Infinity, -Infinity] };
    let triangles = 0;
    items.forEach((item, i) => {
      let geo;
      try { geo = parseStl(item.buffer); }
      catch (e) { console.warn('STL parse failed for', item.name, e); return; }
      if (!geo.triangles) return;
      triangles += geo.triangles;
      for (let a = 0; a < 3; a += 1) {
        bbox.min[a] = Math.min(bbox.min[a], geo.bbox.min[a]);
        bbox.max[a] = Math.max(bbox.max[a], geo.bbox.max[a]);
      }
      const pos = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, pos);
      gl.bufferData(gl.ARRAY_BUFFER, geo.positions, gl.STATIC_DRAW);
      const nrm = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, nrm);
      gl.bufferData(gl.ARRAY_BUFFER, geo.normals, gl.STATIC_DRAW);
      this.meshes.push({
        name: item.name, pos, nrm, count: geo.positions.length / 3,
        color: item.color || PALETTE[i % PALETTE.length], visible: true, triangles: geo.triangles,
        bbox: geo.bbox,
      });
    });

    this.bbox = Number.isFinite(bbox.min[0]) ? bbox : { min: [0, 0, 0], max: [1, 1, 1] };
    this.triangles = triangles;
    this.center = [0, 1, 2].map((a) => (this.bbox.min[a] + this.bbox.max[a]) / 2);
    const span = Math.max(...[0, 1, 2].map((a) => this.bbox.max[a] - this.bbox.min[a]), 1e-6);
    this.scale = 2 / span;
    this._buildBox();
    this.reset();
    return { triangles, bbox: this.bbox, meshes: this.meshes.length };
  }

  _buildBox() {
    const gl = this.gl;
    if (this.boxBuf) gl.deleteBuffer(this.boxBuf);
    const { min, max } = this.bbox;
    const c = [[min[0], min[1], min[2]], [max[0], min[1], min[2]], [max[0], max[1], min[2]], [min[0], max[1], min[2]],
      [min[0], min[1], max[2]], [max[0], min[1], max[2]], [max[0], max[1], max[2]], [min[0], max[1], max[2]]];
    const edges = [[0, 1], [1, 2], [2, 3], [3, 0], [4, 5], [5, 6], [6, 7], [7, 4], [0, 4], [1, 5], [2, 6], [3, 7]];
    const data = new Float32Array(edges.length * 6);
    edges.forEach(([a, b], i) => {
      data.set(c[a], i * 6);
      data.set(c[b], i * 6 + 3);
    });
    this.boxBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, this.boxBuf);
    gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
    this.boxCount = edges.length * 2;
  }

  setVisible(name, on) {
    const m = this.meshes.find((x) => x.name === name);
    if (m) { m.visible = on; this.invalidate(); }
  }

  reset() {
    this.theta = -0.9;
    this.phi = 0.95;
    this.radius = 2.6;
    this.pan = [0, 0];
    this.invalidate();
  }

  _bindInput() {
    const c = this.canvas;
    c.tabIndex = 0;
    let dragging = false;
    let panning = false;
    let lx = 0;
    let ly = 0;

    const down = (e) => {
      dragging = true;
      panning = e.shiftKey || e.button === 1;
      lx = e.clientX; ly = e.clientY;
      c.setPointerCapture?.(e.pointerId);
      e.preventDefault();
    };
    const move = (e) => {
      if (!dragging) return;
      const dx = e.clientX - lx;
      const dy = e.clientY - ly;
      lx = e.clientX; ly = e.clientY;
      if (panning) {
        this.pan[0] -= dx * 0.004 * this.radius;
        this.pan[1] += dy * 0.004 * this.radius;
      } else {
        this.theta -= dx * 0.008;
        this.phi = Math.max(0.02, Math.min(Math.PI - 0.02, this.phi - dy * 0.008));
      }
      this.invalidate();
    };
    const up = (e) => {
      dragging = false; panning = false;
      c.releasePointerCapture?.(e.pointerId);
    };
    c.addEventListener('pointerdown', down);
    c.addEventListener('pointermove', move);
    c.addEventListener('pointerup', up);
    c.addEventListener('pointercancel', up);
    c.addEventListener('wheel', (e) => {
      e.preventDefault();
      this.radius = Math.max(0.4, Math.min(40, this.radius * Math.exp(e.deltaY * 0.0012)));
      this.invalidate();
    }, { passive: false });
    c.addEventListener('keydown', (e) => {
      const step = e.shiftKey ? 0.25 : 0.08;
      const keys = {
        ArrowLeft: () => { this.theta -= step; },
        ArrowRight: () => { this.theta += step; },
        ArrowUp: () => { this.phi = Math.max(0.02, this.phi - step); },
        ArrowDown: () => { this.phi = Math.min(Math.PI - 0.02, this.phi + step); },
        '+': () => { this.radius = Math.max(0.4, this.radius / 1.15); },
        '=': () => { this.radius = Math.max(0.4, this.radius / 1.15); },
        '-': () => { this.radius = Math.min(40, this.radius * 1.15); },
        _: () => { this.radius = Math.min(40, this.radius * 1.15); },
        0: () => this.reset(),
        Home: () => this.reset(),
      };
      const fn = keys[e.key];
      if (!fn) return;
      e.preventDefault();
      fn();
      this.invalidate();
    });
    this._cleanup = () => {
      c.removeEventListener('pointerdown', down);
      c.removeEventListener('pointermove', move);
      c.removeEventListener('pointerup', up);
    };
  }

  draw() {
    const gl = this.gl;
    const c = this.canvas;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const w = Math.max(1, Math.round(c.clientWidth * dpr));
    const h = Math.max(1, Math.round(c.clientHeight * dpr));
    if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
    gl.viewport(0, 0, w, h);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    if (!this.meshes.length) return;

    const eye = [
      this.radius * Math.sin(this.phi) * Math.cos(this.theta),
      this.radius * Math.cos(this.phi),
      this.radius * Math.sin(this.phi) * Math.sin(this.theta),
    ];
    const target = [this.pan[0], this.pan[1], 0];
    const view = lookAt([eye[0] + target[0], eye[1] + target[1], eye[2]], target, [0, 1, 0]);
    const proj = perspective(0.9, w / h, 0.01, 200);
    // model: centre the part on the origin, then normalise its size, then lie flat (z up -> y up)
    const s = this.scale;
    const model = new Float32Array([
      s, 0, 0, 0,
      0, 0, s, 0,
      0, -s, 0, 0,
      -this.center[0] * s, this.center[2] * s, this.center[1] * s, 1,
    ]);
    const mv = mul(view, model);
    const mvp = mul(proj, mv);
    const nm = mat3of(mv);

    gl.useProgram(this.prog);
    gl.uniformMatrix4fv(this.loc.uMVP, false, mvp);
    gl.uniformMatrix3fv(this.loc.uNM, false, nm);
    gl.enableVertexAttribArray(this.loc.aPos);
    gl.enableVertexAttribArray(this.loc.aNrm);
    for (const m of this.meshes) {
      if (!m.visible) continue;
      gl.uniform3fv(this.loc.uColor, new Float32Array(m.color));
      gl.bindBuffer(gl.ARRAY_BUFFER, m.pos);
      gl.vertexAttribPointer(this.loc.aPos, 3, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, m.nrm);
      gl.vertexAttribPointer(this.loc.aNrm, 3, gl.FLOAT, false, 0, 0);
      gl.drawArrays(gl.TRIANGLES, 0, m.count);
    }

    if (this.showBox && this.boxBuf) {
      gl.useProgram(this.lineProg);
      gl.uniformMatrix4fv(this.lineLoc.uMVP, false, mvp);
      gl.uniform3fv(this.lineLoc.uColor, new Float32Array([0.58, 0.7, 1.0]));
      gl.enableVertexAttribArray(this.lineLoc.aPos);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.boxBuf);
      gl.vertexAttribPointer(this.lineLoc.aPos, 3, gl.FLOAT, false, 0, 0);
      gl.drawArrays(gl.LINES, 0, this.boxCount);
    }
  }

  dispose() {
    this.disposed = true;
    cancelAnimationFrame(this._raf);
    this._ro?.disconnect();
    if (this._onResize) removeEventListener('resize', this._onResize);
    this._cleanup?.();
    const gl = this.gl;
    for (const m of this.meshes) { gl.deleteBuffer(m.pos); gl.deleteBuffer(m.nrm); }
    if (this.boxBuf) gl.deleteBuffer(this.boxBuf);
    this.meshes = [];
    gl.getExtension('WEBGL_lose_context')?.loseContext();
  }
}

export function bboxSize(bbox) {
  if (!bbox) return null;
  return [0, 1, 2].map((a) => bbox.max[a] - bbox.min[a]);
}

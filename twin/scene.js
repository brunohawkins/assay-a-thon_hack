// Renderer, camera, OrbitControls, lighting, and the render loop.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CSS2DRenderer } from 'three/addons/renderers/CSS2DRenderer.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

const FOV_3D = 38;
const FOV_PLAN = 5;    // degrees: near enough to a drawing that the chip's depth hardly shows

export function createScene(container) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setClearColor(0x000000, 0); // the page's CSS gradient shows through
  // Neutral tone mapping keeps the dye colours true (ACES pushes yellow towards white).
  renderer.toneMapping = THREE.NeutralToneMapping;
  renderer.toneMappingExposure = 1.0;
  container.appendChild(renderer.domElement);

  // HTML labels (part ids, temperatures) drawn over the canvas.
  const labelRenderer = new CSS2DRenderer();
  labelRenderer.domElement.className = 'label-layer';
  container.appendChild(labelRenderer.domElement);

  const scene = new THREE.Scene();
  const pmrem = new THREE.PMREMGenerator(renderer);
  const room = new RoomEnvironment();
  scene.environment = pmrem.fromScene(room, 0.04).texture;
  scene.environmentIntensity = 0.4;
  room.dispose();
  pmrem.dispose();

  const camera = new THREE.PerspectiveCamera(FOV_3D, 1, 0.5, 1500);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.minDistance = 25;
  controls.maxDistance = 260;
  controls.maxPolarAngle = Math.PI * 0.49;

  // Plan (2D): the same scene and animations seen straight from above, through a long lens so it
  // reads as a drawing. No orbiting: a drag pans, the wheel zooms.
  let plan = false;
  function applyPlanControls() {
    controls.enableRotate = !plan;
    controls.mouseButtons.LEFT = plan ? THREE.MOUSE.PAN : THREE.MOUSE.ROTATE;
    controls.touches.ONE = plan ? THREE.TOUCH.PAN : THREE.TOUCH.ROTATE;
    camera.fov = plan ? FOV_PLAN : FOV_3D;
    camera.updateProjectionMatrix();
  }

  // Even daylight on a bench: a bright sky/ground fill, a key light and a soft fill opposite.
  // Kept moderate: brighter, the tone mapping bleaches the dye colours towards white.
  scene.add(new THREE.HemisphereLight(0xffffff, 0xc9d1d9, 0.95));
  const key = new THREE.DirectionalLight(0xffffff, 1.3);
  key.position.set(40, 80, 50);
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xffffff, 0.45);
  fill.position.set(-60, 30, -60);
  scene.add(fill);

  const floor = makeFloor();
  floor.traverse((o) => { o.userData.noFit = true; });   // the bench under the chip is not part of what is framed
  scene.add(floor);

  const callbacks = [];
  const clock = new THREE.Clock();
  let footprint = null;
  let userMoved = false;
  controls.addEventListener('start', () => { userMoved = true; });

  // The part of the screen not covered by the header, narration panel and
  // controls. The chip is centred and fitted in there.
  function freeRect(w, h) {
    // Hidden elements (display: none, e.g. in the presenter view) have an all-zero box: skip them.
    const box = (sel) => { const r = document.querySelector(sel)?.getBoundingClientRect(); return r && r.width > 0 && r.height > 0 ? r : null; };
    const top = box('.topbar')?.bottom ?? 0;
    const panel = box('.narration');
    const controlsBox = box('.controls') ?? box('.pr-caption'); // the presenter's caption sits where the controls were
    // Any visible left-hand panel (the control-loop panel, Module 1 v1's flow panel, the home page).
    const left = [...document.querySelectorAll('.control, .pr-card, .home-panel')].map((e) => e.getBoundingClientRect()).find((r) => r.width > 0);
    let leftEdge = 0;
    if (left && left.width > 0 && left.left < w / 2) leftEdge = left.right;
    let right = w;
    let bottom = controlsBox ? controlsBox.top : h;
    if (panel && panel.width > 0) {
      if (panel.left > w / 2) right = panel.left; // side panel (wide screens)
      else bottom = Math.min(bottom, panel.top); // stacked above the controls (phones)
    }
    return { left: leftEdge + 8, top: top + 8, right: right - 8, bottom: bottom - 8 };
  }

  // Shift the projection centre into the free rect with a view offset.
  function applyViewOffset(w, h) {
    const r = freeRect(w, h);
    const dx = w / 2 - (r.left + r.right) / 2;
    const dy = h / 2 - (r.top + r.bottom) / 2;
    const fullW = w + 2 * Math.abs(dx);
    const fullH = h + 2 * Math.abs(dy);
    camera.aspect = fullW / fullH;
    camera.setViewOffset(fullW, fullH, dx > 0 ? 2 * dx : 0, dy > 0 ? 2 * dy : 0, w, h);
    camera.updateProjectionMatrix();
    return { fullH, freeW: Math.max(r.right - r.left, 50), freeH: Math.max(r.bottom - r.top, 50) };
  }

  function fit() {
    if (!footprint) return;
    const w = container.clientWidth || window.innerWidth;
    const h = container.clientHeight || window.innerHeight;
    const { fullH, freeW, freeH } = applyViewOffset(w, h);
    const t = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
    if (plan) { fitPlan(t, fullH, freeW, freeH); return; }
    const dir = new THREE.Vector3(...(footprint.dir ?? [-0.06, 0.6, 0.86])).normalize();
    const halfX = (footprint.x / 2) * 1.12;
    // Apparent height: the tilted footprint plus room above it for capsules arriving.
    const halfV = ((footprint.y * dir.y + 0.88 * footprint.y * Math.hypot(dir.x, dir.z)) / 2) * 1.1;
    const dist = Math.max((halfX * fullH) / (t * freeW), (halfV * fullH) / (t * freeH), 1);
    // Zoom limits and clip planes scale with the chip, so a 20 mm chip can be
    // inspected down to single 70 um capsules and a 200 mm display model still fits.
    controls.maxDistance = dist * 3;
    controls.minDistance = dist * 0.02;
    camera.near = Math.max(0.002, dist * 0.001);
    camera.far = dist * 40;
    camera.updateProjectionMatrix();
    controls.target.set(0, footprint.targetY ?? footprint.y * 0.12, 0);
    camera.position.copy(dir.multiplyScalar(dist)).add(controls.target);
    controls.update();
  }

  // Plan: fit what is actually on screen seen from above (the chip and anything drawn beside
  // it, such as Module 1's next-module box, or the whole pipeline line), not just the footprint.
  const _box = new THREE.Box3();
  const _part = new THREE.Box3();
  function visibleBounds() {
    _box.makeEmpty();
    scene.traverseVisible((o) => {
      if (o.userData.noFit || !(o.isMesh || o.isLine || o.isPoints) || !o.geometry) return;
      if (o.isInstancedMesh) { if (!o.count) return; o.computeBoundingBox(); _part.copy(o.boundingBox); }
      else { if (!o.geometry.boundingBox) o.geometry.computeBoundingBox(); _part.copy(o.geometry.boundingBox); }
      if (!_part.isEmpty()) _box.union(_part.applyMatrix4(o.matrixWorld));
    });
    return _box;
  }
  function fitPlan(t, fullH, freeW, freeH) {
    const b = visibleBounds();
    const empty = b.isEmpty();
    const cx = empty ? 0 : (b.min.x + b.max.x) / 2;
    const cz = empty ? 0 : (b.min.z + b.max.z) / 2;
    // Room around it for the labels drawn over the scene (the footprint if nothing is drawn yet).
    const halfX = (empty ? footprint.x : b.max.x - b.min.x) / 2 * 1.14;
    const halfV = (empty ? footprint.y : b.max.z - b.min.z) / 2 * 1.2;
    const dist = Math.max((halfX * fullH) / (t * freeW), (halfV * fullH) / (t * freeH), 1);
    controls.maxDistance = dist * 3;
    controls.minDistance = dist * 0.02;
    camera.near = Math.max(0.002, dist * 0.001);
    camera.far = dist * 40;
    camera.updateProjectionMatrix();
    // Labels hang above their anchors: shift the view down a little to leave room on top.
    controls.target.set(cx, 0, cz - halfV * 0.04);
    camera.position.set(controls.target.x, dist, controls.target.z + dist * 1e-4);
    controls.update();
  }

  // Scale bar: a round length that spans ~70-170 px at the orbit target.
  const NICE = [0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500];
  const bar = { el: null, line: null, label: null, mode: 'real', scale: 1, lastText: '' };
  const _v1 = new THREE.Vector3();
  const _v2 = new THREE.Vector3();
  const _right = new THREE.Vector3();
  const fmtLen = (mm) => (mm < 1 ? `${Math.round(mm * 1000)} µm` : `${+mm.toFixed(2)} mm`);
  function updateScaleBar() {
    if (!bar.el) return;
    const w = container.clientWidth || window.innerWidth;
    _right.setFromMatrixColumn(camera.matrixWorld, 0);
    _v1.copy(controls.target).project(camera);
    _v2.copy(controls.target).addScaledVector(_right, 1).project(camera);
    const pxPerUnit = (Math.abs(_v2.x - _v1.x) * w) / 2;
    if (!isFinite(pxPerUnit) || pxPerUnit <= 0) return;
    let len = NICE[0];
    for (const v of NICE) if (v * pxPerUnit <= 170) len = v;
    bar.line.style.width = `${Math.round(len * pxPerUnit)}px`;
    const text = bar.mode === 'display'
      ? `${fmtLen(len)} on the model = ${fmtLen(len / bar.scale)} real`
      : `${fmtLen(len)} (real scale)`;
    if (text !== bar.lastText) {
      bar.label.textContent = text;
      bar.lastText = text;
    }
  }

  function resize() {
    const w = container.clientWidth || window.innerWidth;
    const h = container.clientHeight || window.innerHeight;
    renderer.setSize(w, h, false);
    renderer.domElement.style.width = '100%';
    renderer.domElement.style.height = '100%';
    labelRenderer.setSize(w, h);
    if (footprint && !userMoved) fit();
    else applyViewOffset(w, h);
  }
  new ResizeObserver(resize).observe(container);
  window.addEventListener('resize', resize);
  resize();

  renderer.setAnimationLoop(() => {
    const dt = Math.min(clock.getDelta(), 0.1);
    for (const cb of callbacks) cb(dt);
    controls.update();
    renderer.render(scene, camera);
    labelRenderer.render(scene, camera);
    updateScaleBar();
  });

  return {
    renderer,
    scene,
    camera,
    controls,
    onFrame(cb) { callbacks.push(cb); },
    // Point the camera at a chip of this footprint (mm), fitted to the free screen area.
    frameChip(fp) {
      footprint = fp;
      userMoved = false;
      fit();
    },
    // Plan (2D, from above, no orbiting) or the 3D view. Refits whatever is framed.
    setPlan(on) {
      if (plan === Boolean(on)) return;
      plan = Boolean(on);
      applyPlanControls();
      userMoved = false;
      if (footprint) fit();
    },
    get plan() { return plan; },
    attachScaleBar(el) {
      bar.el = el;
      bar.line = el.querySelector('.bar');
      bar.label = el.querySelector('.text');
    },
    // 'real': world units are real mm. 'display': world units are mm on the printed model.
    setScaleMode(mode, scale = 1) {
      bar.mode = mode;
      bar.scale = scale;
      bar.lastText = '';
    },
    setLabelsHidden(hidden) {
      labelRenderer.domElement.classList.toggle('hidden', hidden);
    },
  };
}

// A pale bench: a soft contact shadow under the chip and a faint grid, like a cutting mat.
function makeFloor() {
  const group = new THREE.Group();
  const size = 512;
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  const g = canvas.getContext('2d');
  const grad = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  grad.addColorStop(0, 'rgba(31,35,40,0.10)');
  grad.addColorStop(0.35, 'rgba(31,35,40,0.04)');
  grad.addColorStop(1, 'rgba(31,35,40,0)');
  g.fillStyle = grad;
  g.fillRect(0, 0, size, size);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  const shadow = new THREE.Mesh(
    new THREE.PlaneGeometry(120, 120),
    new THREE.MeshBasicMaterial({ map: tex, transparent: true, depthWrite: false }),
  );
  shadow.rotation.x = -Math.PI / 2;
  shadow.position.y = -0.4;
  group.add(shadow);

  const grid = makeGrid();
  grid.position.y = -0.45;
  group.add(grid);
  return group;
}

// 5 mm lines, every 10 mm a little stronger, fading out from ~100 mm so there is no hard edge.
function makeGrid() {
  const half = 240;
  const pos = [];
  const col = [];
  const c = new THREE.Color(0x8c959f);
  const fade = (x, z) => {
    const t = Math.min(1, Math.max(0, (Math.hypot(x, z) - 100) / 120));
    return 1 - t * t * (3 - 2 * t);
  };
  const add = (x, z, a) => { pos.push(x, 0, z); col.push(c.r, c.g, c.b, a * fade(x, z)); };
  for (let i = -half; i <= half; i += 5) {
    const a = i % 10 === 0 ? 0.34 : 0.17;
    for (let j = -half; j < half; j += 5) {
      add(j, i, a); add(j + 5, i, a);   // along x
      add(i, j, a); add(i, j + 5, a);   // along z
    }
  }
  const geom = new THREE.BufferGeometry();
  geom.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  geom.setAttribute('color', new THREE.Float32BufferAttribute(col, 4));
  return new THREE.LineSegments(geom, new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, depthWrite: false }));
}

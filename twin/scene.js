// Renderer, camera, OrbitControls, lighting, and the render loop.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CSS2DRenderer } from 'three/addons/renderers/CSS2DRenderer.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

export function createScene(container) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setClearColor(0x000000, 0); // the page's CSS gradient shows through
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  container.appendChild(renderer.domElement);

  // HTML labels (part ids, temperatures) drawn over the canvas.
  const labelRenderer = new CSS2DRenderer();
  labelRenderer.domElement.className = 'label-layer';
  container.appendChild(labelRenderer.domElement);

  const scene = new THREE.Scene();
  const pmrem = new THREE.PMREMGenerator(renderer);
  const room = new RoomEnvironment();
  scene.environment = pmrem.fromScene(room, 0.04).texture;
  scene.environmentIntensity = 0.55;
  room.dispose();
  pmrem.dispose();

  const camera = new THREE.PerspectiveCamera(38, 1, 0.5, 1500);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.minDistance = 25;
  controls.maxDistance = 260;
  controls.maxPolarAngle = Math.PI * 0.49;

  // Soft lighting: sky/ground fill, a key light and a cool rim light.
  scene.add(new THREE.HemisphereLight(0xcfe3ff, 0x0a0d14, 0.9));
  const key = new THREE.DirectionalLight(0xffffff, 2.2);
  key.position.set(40, 80, 50);
  scene.add(key);
  const rim = new THREE.DirectionalLight(0x7aa2ff, 1.4);
  rim.position.set(-60, 30, -60);
  scene.add(rim);

  scene.add(makeFloor());

  const callbacks = [];
  const clock = new THREE.Clock();
  let footprint = null;
  let userMoved = false;
  controls.addEventListener('start', () => { userMoved = true; });

  // The part of the screen not covered by the header, narration panel and
  // controls. The chip is centred and fitted in there.
  function freeRect(w, h) {
    const box = (sel) => document.querySelector(sel)?.getBoundingClientRect();
    const top = box('.topbar')?.bottom ?? 0;
    const panel = box('.narration');
    const controlsBox = box('.controls');
    let right = w;
    let bottom = controlsBox ? controlsBox.top : h;
    if (panel && panel.width > 0) {
      if (panel.left > w / 2) right = panel.left; // side panel (wide screens)
      else bottom = Math.min(bottom, panel.top); // stacked above the controls (phones)
    }
    return { left: 0, top: top + 8, right: right - 8, bottom: bottom - 8 };
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
    const dir = new THREE.Vector3(-0.06, 0.6, 0.86).normalize();
    const halfX = (footprint.x / 2) * 1.12;
    // Apparent height: the tilted footprint plus room above it for capsules arriving.
    const halfV = ((footprint.y * dir.y + 22 * Math.hypot(dir.x, dir.z)) / 2) * 1.1;
    const dist = Math.max((halfX * fullH) / (t * freeW), (halfV * fullH) / (t * freeH), 40);
    controls.maxDistance = Math.max(260, dist * 1.8); // don't let the zoom limit undo the fit
    controls.target.set(0, 3, 0);
    camera.position.copy(dir.multiplyScalar(dist)).add(controls.target);
    controls.update();
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
  };
}

// A dark floor with a faint grid and a soft pool of light under the chip.
function makeFloor() {
  const group = new THREE.Group();
  const size = 512;
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  const g = canvas.getContext('2d');
  const grad = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  grad.addColorStop(0, 'rgba(90,150,255,0.22)');
  grad.addColorStop(0.45, 'rgba(60,110,220,0.07)');
  grad.addColorStop(1, 'rgba(0,0,0,0)');
  g.fillStyle = grad;
  g.fillRect(0, 0, size, size);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  const glow = new THREE.Mesh(
    new THREE.PlaneGeometry(220, 220),
    new THREE.MeshBasicMaterial({ map: tex, transparent: true, depthWrite: false }),
  );
  glow.rotation.x = -Math.PI / 2;
  glow.position.y = -0.4;
  group.add(glow);

  const grid = new THREE.GridHelper(200, 40, 0x223355, 0x152036);
  grid.material.transparent = true;
  grid.material.opacity = 0.35;
  grid.material.depthWrite = false;
  grid.position.y = -0.45;
  group.add(grid);
  return group;
}

// Entry point: loads runs/index.json, builds the chip, plays the selected run.
// The twin only ever reads run files, so a recorded run always works offline.
import { createScene } from './scene.js';
import { Chip } from './chip.js';
import { Player } from './player.js';
import { createUI } from './ui.js';
import { findDisplayModel, buildDisplayModel } from './display.js';
import { createControlPanel } from './control.js';

const ROOT = '../'; // serve the repo root: python -m http.server, then open /twin/

const ctx = createScene(document.getElementById('viewport'));
let chip = null;
let chipPath = null;
let runs = [];
let busy = false;
let view = 'real';
let displayManifest = null;
let displayGroup = null;

const ui = createUI({
  onSelectRun: (file) => selectRun(file),
  onPlayPause: () => player.toggle(),
  onStep: () => player.step(),
  onSpeed: (x) => player.setSpeed(x),
  onSeek: (t) => player.seek(t),
  onLabels: (on) => chip?.setLabelsVisible(on),
  onView: (mode) => setView(mode),
});

const control = createControlPanel({
  onLoopToggle: (file) => { ui.selectRun(file); selectRun(file); },
});

// Side panels start just below the topbar, however many rows it wraps to, so the
// SIMULATED HARDWARE badge is never hidden behind a panel.
const topbar = document.querySelector('.topbar');
function placePanels() {
  const bottom = topbar.getBoundingClientRect().bottom;
  document.documentElement.style.setProperty('--panels-top', `${Math.max(84, Math.ceil(bottom + 12))}px`);
}
new ResizeObserver(placePanels).observe(topbar);
placePanels();

const player = new Player(ctx, {
  onEvent: (i, ev) => ui.setEvent(i, ev),
  onTick: (info) => { ui.setTick(info); control.update(info.protocolT); },
  onPlayState: (on) => ui.setPlaying(on),
});

ctx.attachScaleBar(document.getElementById('scalebar'));

// For debugging in the browser console, and for the headless smoke test.
window.__twin = {
  player,
  ctx,
  get chip() { return chip; },
  get view() { return view; },
  get displayManifest() { return displayManifest; },
  get displayGroup() { return displayGroup; },
  ready: false,
};

async function fetchJson(path) {
  const res = await fetch(ROOT + path, { cache: 'no-store' });
  if (!res.ok) throw new Error(`Could not load ${path} (HTTP ${res.status}).`);
  return res.json();
}

async function ensureChip(path) {
  if (chip && chipPath === path) return;
  const spec = await fetchJson(path);
  await setView('real');
  if (displayGroup) {
    displayGroup.traverse((o) => { o.geometry?.dispose?.(); o.material?.dispose?.(); });
    displayGroup = null;
  }
  if (chip) {
    ctx.scene.remove(chip.group);
    chip.dispose();
  }
  chip = new Chip(spec);
  chipPath = path;
  chip.setLabelsVisible(ui.labelsOn());
  ctx.scene.add(chip.group);
  ctx.frameChip(spec.footprint);
  player.setChip(chip);
  chip.loadStls(`${ROOT}cad/exports/`).then((ids) => {
    if (ids.length) ui.toast(`Using CAD for ${ids.join(', ')}`);
  });
  displayManifest = await findDisplayModel(spec.id, ROOT);
  ui.setDisplayAvailable(displayManifest, spec.id);
}

// Real scale: the working chip, in real mm. Display model: the printed show piece.
async function setView(mode) {
  if (mode === view) return;
  if (mode === 'display') {
    if (!displayManifest) return;
    if (!displayGroup) displayGroup = await buildDisplayModel(displayManifest, ROOT);
    chip.group.visible = false;
    ctx.scene.add(displayGroup);
    ctx.setLabelsHidden(true);
    const [x, y, z] = displayManifest.overall_mm;
    ctx.frameChip({ x, y, targetY: z * 0.4 });
    ctx.setScaleMode('display', displayManifest.display_scale);
    ui.toast(`Display model: ${displayManifest.display_scale}× the real chip. Print ${displayManifest.print_file}`);
  } else {
    if (displayGroup) ctx.scene.remove(displayGroup);
    if (chip) {
      chip.group.visible = true;
      ctx.frameChip(chip.footprint);
    }
    ctx.setLabelsHidden(false);
    ctx.setScaleMode('real');
  }
  view = mode;
  ui.setView(mode);
}

let pendingFile = null;

async function selectRun(file) {
  if (busy) { pendingFile = file; return; } // the latest request wins once the current switch finishes
  busy = true;
  try {
    const entry = runs.find((r) => r.file === file) ?? runs[0];
    const run = await fetchJson(`runs/${entry.file}`);
    run.__file = entry.file;
    const path = run.chip_spec ?? entry.chip_spec;
    if (!path) throw new Error(`${entry.file} does not say which chip spec it used (chip_spec).`);
    const sameChip = chip && path === chipPath;
    const autoplay = player.playing || !player.run;
    if (sameChip && player.run) await player.fadeOut(); // smooth swap on the same chip
    await ensureChip(path);
    const hadControl = document.body.classList.contains('has-control');
    control.setRun(run, runs);
    if (hadControl !== document.body.classList.contains('has-control') && view === 'real') ctx.frameChip(chip.footprint);
    player.load(run, { autoplay });
    ui.setRun(run, player.timeline());
    ui.setSizing(run.sizing ?? null);
    player.refresh(); // re-send the current event now the timeline exists
    history.replaceState(null, '', `#run=${encodeURIComponent(entry.file)}`);
  } finally {
    busy = false;
    if (pendingFile) {
      const next = pendingFile;
      pendingFile = null;
      if (next !== player.run?.__file) selectRun(next);
    }
  }
}

async function start() {
  try {
    const index = await fetchJson('runs/index.json');
    runs = index.runs ?? [];
    if (!runs.length) throw new Error('runs/index.json lists no runs. Generate some: python operator/run.py --all');
    const wanted = decodeURIComponent(new URLSearchParams(location.hash.slice(1)).get('run') ?? '');
    const first = runs.find((r) => r.file === wanted) ?? runs.find((r) => r.protocol_id === 'placeholder_scrnaseq') ?? runs[0];
    ui.setRuns(runs, first.file);
    await selectRun(first.file);
    window.__twin.ready = true;
  } catch (err) {
    console.warn(err);
    ui.error(
      `${err.message} Start a server from the repo root (python -m http.server 8000) ` +
        'and open http://localhost:8000/twin/',
    );
  }
}

start();

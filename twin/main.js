// Entry point: loads runs/index.json, shows the pipeline view or a module view, plays runs.
// The twin only ever reads run files, so a recorded run always works offline.
import { createScene } from './scene.js';
import { Chip } from './chip.js';
import { Player } from './player.js';
import { createUI } from './ui.js';
import { findDisplayModel, buildDisplayModel } from './display.js';
import { createControlPanel } from './control.js';
import { createPipeline } from './pipeline.js';
import { createOverlays } from './overlays.js';
import { createModule1View, CHIP_ID as M1V1 } from './module1.js';

const ROOT = '../'; // serve the repo root: python -m http.server, then open /twin/

const ctx = createScene(document.getElementById('viewport'));
let chip = null;
let chipPath = null;
let chipSpec = null;
let runs = [];
let busy = false;
let view = 'real';
let displayManifest = null;
let displayGroup = null;
let module = null;          // 'pipeline' | 'm1' | 'm1b' | 'm2' | 'm3' | 'other'
let pipelineLoaded = false;
let cultureRun = false;

// Which run a module view opens first.
const PREFERRED = {
  m1: (r) => r.variant === 'control',
  m1b: (r) => r.scenario === 'm1b_cellprep_demo',
  m2: () => true,
  m3: (r) => !r.scenario,
  m1v1: () => true,
};
// Module 1 v1 (MJ's chip) has no module block in its spec: its runs are told apart by chip id.
const moduleOf = (r) => r.module ?? (r.chip_id === M1V1 ? 'm1v1' : null);

const inPipeline = () => module === 'pipeline';
const active = () => (inPipeline() ? pipeline : player);

const ui = createUI({
  onSelectRun: (file) => selectRun(file),
  onPlayPause: () => active().toggle(),
  onStep: () => active().step(),
  onSpeed: (x) => { player.setSpeed(x); pipeline.setSpeed(x); },
  onSeek: (t) => { active().seek(t); if (!inPipeline()) m1.onSeek(); },
  onLabels: (on) => chip?.setLabelsVisible(on),
  onView: (mode) => setView(mode),
  onModule: (m) => showModule(m),
});

const control = createControlPanel({
  onLoopToggle: (file) => { ui.selectRun(file); selectRun(file); },
});

const overlays = createOverlays();
const m1 = createModule1View({ ctx, ui, overlays, root: ROOT });
m1.setHandlers({ onManual: () => player.setPlaying(false) });

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
  onEvent: (i, ev) => { if (!inPipeline()) { ui.setEvent(i, ev); m1.onEvent(i, ev, player.playing); } },
  onTick: (info) => {
    if (inPipeline()) return;
    ui.setTick(info);
    control.update(info.protocolT);
    overlays.setDay(cultureRun ? info.protocolT : null, player.run?.total_duration_s ?? 0);
    m1.onTick(info, player.playing);
  },
  onPlayState: (on) => { if (!inPipeline()) ui.setPlaying(on); },
  onOverlay: (o) => overlays.setNozzle(inPipeline() ? null : o.nozzle),
});

const pipeline = createPipeline(document.getElementById('pipeline'), {
  onOpenModule: (m, file) => showModule(m, file),
  onPhase: (i, ev) => ui.setEvent(i, ev),
  onTick: (info) => ui.setTick(info),
  onPlayState: (on) => ui.setPlaying(on),
});
ctx.onFrame((dt) => pipeline.update(dt));
ctx.onFrame((dt) => { if (!inPipeline()) m1.update(dt, player.playing); });

ctx.attachScaleBar(document.getElementById('scalebar'));

// For debugging in the browser console, and for the headless smoke test.
window.__twin = {
  player,
  pipeline,
  ctx,
  overlays,
  m1,
  showModule: (m, file) => showModule(m, file),
  get chip() { return chip; },
  get view() { return view; },
  get module() { return module; },
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
  let spec = await fetchJson(path);
  let drawSpec = spec;
  if (m1.isChip(spec)) drawSpec = (await m1.prepare(spec)).view; // channel paths from the DXF
  await setView('real');
  if (displayGroup) {
    displayGroup.traverse((o) => { o.geometry?.dispose?.(); o.material?.dispose?.(); });
    displayGroup = null;
  }
  m1.detach();
  if (chip) {
    ctx.scene.remove(chip.group);
    chip.dispose();
  }
  chip = new Chip(drawSpec);
  chipSpec = spec;
  chipPath = path;
  chip.setLabelsVisible(ui.labelsOn());
  ctx.scene.add(chip.group);
  ctx.frameChip(spec.footprint);
  player.setChip(chip);
  ui.setChipInfo(spec);
  if (m1.isChip(spec)) {
    m1.attach(chip); // loads its own STLs from cad/exports/module1_v1/
    ctx.frameChip(spec.footprint); // re-fit now the flow panel takes the left side
  }
  else chip.loadStls(`${ROOT}cad/exports/`).then((ids) => {
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

function runsFor(m) {
  return runs.filter((r) => (m === 'other' ? !moduleOf(r) : moduleOf(r) === m));
}

// Pipeline view or one module's view.
async function showModule(m, file = null) {
  module = m;
  ui.setTab(m);
  if (m === 'pipeline') {
    player.setPlaying(false);
    overlays.setDay(null);
    overlays.setNozzle(null);
    document.body.classList.remove('has-control');
    await ensurePipeline();
    pipeline.show();
    ui.setRun(pipeline.runLike(), pipeline.timeline());
    ui.setSizing(null);
    pipeline.refresh();
    ui.setPlaying(pipeline.playing);
    history.replaceState(null, '', '#view=pipeline');
    return;
  }
  pipeline.hide();
  const list = runsFor(m);
  if (!list.length) return;
  const pick = (file && list.find((r) => r.file === file)) ?? list.find(PREFERRED[m] ?? (() => true)) ?? list[0];
  ui.setRuns(list, pick.file);
  await selectRun(pick.file);
}

async function ensurePipeline() {
  if (pipelineLoaded) return;
  const pipe = await fetchJson('protocols/pipeline_mvp.json');
  const specs = {};
  await Promise.all(pipe.modules.map(async (m) => { specs[m.module] = await fetchJson(m.chip); }));
  const find = (m, pred = () => true) => runs.find((r) => r.module === m && pred(r))?.file ?? null;
  const files = {
    m1_control: find('m1', (r) => r.variant === 'control'),
    m1_interaction: find('m1', (r) => r.variant === 'interaction'),
    m1b: find('m1b', PREFERRED.m1b) ?? find('m1b'),
    m2: find('m2'),
    m3: find('m3', PREFERRED.m3) ?? find('m3'),
    m1v1: runs.find((r) => r.chip_id === M1V1)?.file ?? null,
  };
  const v1 = runs.find((r) => r.chip_id === M1V1);
  if (v1?.chip_spec) specs.m1v1 = await fetchJson(v1.chip_spec);
  const loaded = {};
  await Promise.all(Object.entries(files).map(async ([k, f]) => { if (f) loaded[k] = await fetchJson(`runs/${f}`); }));
  pipeline.load({ pipe, specs, runs: loaded, files });
  pipelineLoaded = true;
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
    cultureRun = run.events.some((e) => e.command === 'culture') || run.total_duration_s > 3 * 86400;
    player.load(run, { autoplay });
    ui.setRun(run, player.timeline());
    ui.setSizing(m1.isChip(chip.spec) ? null : run.sizing ?? null); // Module 1 v1 holds cells: capsule sizing doesn't apply
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
    const params = new URLSearchParams(location.hash.slice(1));
    const wanted = decodeURIComponent(params.get('run') ?? '');
    const entry = runs.find((r) => r.file === wanted);
    if (entry) await showModule(moduleOf(entry) ?? 'other', entry.file);
    else await showModule('pipeline');
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

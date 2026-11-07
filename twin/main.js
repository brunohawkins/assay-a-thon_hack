// Entry point: loads runs/index.json, shows the pipeline view or a module view, plays runs.
// The twin only ever reads run files, so a recorded run always works offline.
import { loadContracts } from './contracts.js';
import { createScene } from './scene.js';
import { Chip } from './chip.js';
import { Player } from './player.js';
import { createUI } from './ui.js';
import { findDisplayModel, buildDisplayModel } from './display.js';
import { createControlPanel } from './control.js';
import { createPipeline } from './pipeline.js';
import { createOverlays } from './overlays.js';
import { createModule1View, CHIP_ID as M1V1 } from './module1.js';
import { createLibraryView, LIB_CHIPS } from './library_modules.js';

const ROOT = '../'; // serve the repo root: python -m http.server, then open /twin/

// Contracts first. Viscosity, the port bore, the shear ceiling and the build envelope all come
// from contracts/parameters.json and contracts/blocks.json, and network.js, chip.js and
// module1.js read them the moment they are constructed. The twin will not run on stand-in
// physics: the last time it carried its own copies they drifted 25% and 2.5x from the
// contracts and from operator/network.py, and nothing said so.
try {
  await loadContracts(ROOT);
} catch (err) {
  document.getElementById('error-text').textContent =
    `${err.message} The twin reads its physics from contracts/parameters.json and `
    + 'contracts/blocks.json and will not fall back to numbers of its own. Start a server from '
    + 'the repo root (python -m http.server 8000) and open http://localhost:8000/twin/';
  document.getElementById('error').hidden = false;
  throw err;
}

const ctx = createScene(document.getElementById('viewport'));
let chip = null;
let chipPath = null;
let chipSpec = null;
let runs = [];
let busy = false;
let view = 'real';
let displayManifest = null;
let displayGroup = null;
let module = null;          // 'pipeline' | 'm1v1' | 'm2' | 'm3v1' | 'm4' | 'm5' | 'old' (or m1, m1b, m3) | 'other'
let pipelineLoaded = false;
let cultureRun = false;

// Which run a module view opens first.
const PREFERRED = {
  m1: (r) => r.variant === 'control',
  m1b: (r) => r.scenario === 'm1b_cellprep_demo',
  m2: () => true,
  m3: (r) => !r.scenario,
  m1v1: () => true,
  old: (r) => r.module === 'm3' && !r.scenario,
};
// The current modules' specs (MJ's, Szonja's translated) have no module block: their runs are
// told apart by chip id. A chip/module2*.json from MJ lands in the Module 2 tab.
const CHIP_VIEW = { [M1V1]: 'm1v1', module3_v1_translated: 'm3v1', ...LIB_CHIPS };
const moduleOf = (r) => r.module ?? CHIP_VIEW[r.chip_id] ?? (/^module2/.test(r.chip_id ?? '') ? 'm2' : null);
// The superseded drafts (Module 1 brief, 1b, 3 wash) share one "Old drafts" tab.
const OLD = new Set(['m1', 'm1b', 'm3']);
const tabOf = (m) => (OLD.has(m) ? 'old' : m);

// Module 3 (Szonja) has no protocol file: the twin plays her workflow block straight from
// chip/modules/module3_step1_3.json, mapped onto the generic commands. Nothing is written.
const M3_SOURCE = 'chip/modules/module3_step1_3.json';
const M3_ENTRY = {
  file: 'module3_v1_translated.workflow', synthetic: 'module3', chip_id: 'module3_v1_translated',
  chip_spec: 'chip/module3_v1_translated.json', protocol_id: 'module3_step1_3',
  protocol_name: "Module 3: load, lysis, wash (Szonja's workflow, translated chip)", operator: 'planner',
};

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
const lib = createLibraryView({ panel: document.getElementById('lib-panel') });

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
  onEvent: (i, ev) => { if (!inPipeline()) { ui.setEvent(i, ev); m1.onEvent(i, ev, player.playing); lib.onEvent(i); } },
  onTick: (info) => {
    if (inPipeline()) return;
    ui.setTick(info);
    control.update(info.protocolT);
    overlays.setDay(cultureRun ? info.protocolT : null, player.run?.total_duration_s ?? 0);
    m1.onTick(info, player.playing);
    lib.update(); // runs right after the player draws the frame
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
  lib,
  get pipelineData() { return pipelineData; },
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
  lib.detach();
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
  ui.setChipInfo(spec, pipelineData?.entries.find((e) => e.spec?.id === spec.id) ?? null);
  if (m1.isChip(spec)) {
    m1.attach(chip);
    ctx.frameChip(spec.footprint); // re-fit now the flow panel takes the left side
  }
  // Every chip loads the folder that BELONGS to it (cad/exports/<chip_id>/, or the flat
  // cad/exports/ only when its .chip_id names this chip). Part ids repeat across chips, so
  // loading the flat folder for all of them drew another chip's geometry at another chip's
  // coordinates and toasted "Using CAD for inlet_1, outlet_1" as if it had worked.
  // Awaited, so the chip on screen and the CAD badge describing it can never be two
  // different chips: selectRun's `busy` guard then makes the whole swap atomic.
  await loadChipCad(spec);
  displayManifest = await findDisplayModel(spec.id, ROOT);
  ui.setDisplayAvailable(displayManifest, spec.id, spec);
}

async function loadChipCad(spec) {
  const target = chip;
  const src = await Chip.stlSource(spec.id, ROOT);
  const report = await target.loadStls(src, ROOT);
  if (chip !== target) return; // the user switched chips while this was in flight
  ui.setCadStatus({ ...report, chipId: spec.id, why: src.why, undrawn: target.undrawn });
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
  if (m === 'other') return runs.filter((r) => !moduleOf(r));
  if (m === 'old') return runs.filter((r) => OLD.has(moduleOf(r)));
  return runs.filter((r) => moduleOf(r) === m);
}

// Pipeline view or one module's view.
async function showModule(m, file = null) {
  module = m;
  ui.setTab(tabOf(m));
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

// The chip a pipeline module uses now: a module with "prefer" (Module 2's draft) switches to
// the newest file matching its glob as soon as one exists. Mirrors schema_io.pipeline_chip;
// reads the folder listing, so without one (a static host) it stays on the draft.
async function pipelineChip(m) {
  if (m.prefer) {
    const cut = m.prefer.glob.lastIndexOf('/') + 1;
    const dir = m.prefer.glob.slice(0, cut);
    const pat = new RegExp(`^${m.prefer.glob.slice(cut).replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '[^/]*').replace(/\?/g, '.')}$`);
    let names = [];
    try {
      const html = await (await fetch(ROOT + dir, { cache: 'no-store' })).text();
      names = [...html.matchAll(/href="([^"?#/]+)"/g)].map((x) => decodeURIComponent(x[1]));
    } catch { /* no listing: keep the draft */ }
    const hits = names.filter((n) => n.endsWith('.json') && pat.test(n)).sort();
    if (hits.length) {
      return { path: dir + hits.at(-1), status: m.prefer.status ?? m.status,
        note: m.prefer.status_note ?? `switched automatically from ${m.chip}` };
    }
  }
  return { path: m.chip ?? null, status: m.status, note: m.status_note ?? null };
}

let pipelineData = null;

// protocols/pipeline_mvp.json with each module's chip spec and the run its card opens.
async function loadPipelineData() {
  if (pipelineData) return pipelineData;
  const pipe = await fetchJson('protocols/pipeline_mvp.json');
  const entries = await Promise.all(pipe.modules.map(async (m) => {
    const { path, status, note } = await pipelineChip(m);
    const spec = path ? await fetchJson(path) : null;
    const view = spec ? moduleOf({ chip_id: spec.id, module: spec.module?.id }) : null;
    const list = spec ? runs.filter((r) => r.chip_id === spec.id) : [];
    const pick = list.find(PREFERRED[view] ?? (() => true)) ?? list[0];
    return { ...m, chipPath: path, status, statusNote: note, switched: path !== (m.chip ?? null), spec,
      view: pick ? view : null, file: pick?.file ?? null, duration_s: pick?.total_duration_s ?? null };
  }));
  const m1e = entries.find((e) => e.module === 'module1');
  pipelineData = { pipe, entries, days: Math.round((m1e?.duration_s ?? 0) / 86400) };
  return pipelineData;
}

async function ensurePipeline() {
  if (pipelineLoaded) return;
  pipeline.load(await loadPipelineData());
  pipelineLoaded = true;
}

// Szonja's workflow as a run: flows become load / exchange / back-flush; the no-flow steps
// (settle, incubate) join the step before them. Ends with the hand-off out of to_split_tree,
// which now feeds Module 4: split-and-pool barcoding is dropped, so there is no split tree.
async function module3Run(entry) {
  const src = await fetchJson(M3_SOURCE);
  const events = [];
  let t = 0;
  for (const s of src.workflow) {
    const open = s.valves_open ?? [];
    const dur = s.time_min ? s.time_min * 60 : s.volume_ul && s.flow_ul_min ? (s.volume_ul / s.flow_ul_min) * 60 : 60;
    const text = `Step ${s.step} (${s.substep}): ${s.operation}${s.volume_ul ? `, ${s.volume_ul} µL` : ''}${s.flow_ul_min ? ` at ${s.flow_ul_min} µL/min` : ''}.`
      + `${s.note ? ` ${s.note}.` : ''}${s.check ? ` Check: ${s.check}.` : ''}`;
    let ev = null;
    if (s.inlet === 'capsule_in') ev = { command: 'load', args: { target: 'main_trap', volume_ul: s.volume_ul ?? 0 } };
    else if (open.includes('v_backflush')) ev = { command: 'exchange', args: { target: 'main_trap', reagent: 'wash_buffer', volume_ul: s.volume_ul ?? 0, washes: 1, direction: 'backflush' } };
    else if (s.inlet) {
      ev = { command: 'exchange', args: { target: 'main_trap', reagent: s.inlet, volume_ul: s.volume_ul ?? 0, washes: 1,
        flow_ul_min: s.flow_ul_min, waste: open.includes('v_lysis_waste') ? 'lysis_waste' : 'waste' } };
    }
    if (!ev && events.length) { // no flow: fold into the previous step
      const prev = events.at(-1);
      prev.narration += ` Then ${text}`;
      prev.duration_s += dur;
      t += dur;
      continue;
    }
    if (!ev) continue;
    events.push({ ...ev, t_s: t, duration_s: dur, narration: `${text} Valves open: ${open.join(', ') || 'none'}.`, protocol_step: s.step });
    t += dur;
  }
  events.push({ command: 'release', args: { target: 'main_trap', reagent: 'wash_buffer' }, t_s: t, duration_s: 60, protocol_step: events.length + 1,
    narration: 'Hand-off: the resuspended capsules leave through to_split_tree, which now feeds Module 4 (sublibrary split + index PCR). The port keeps its name from the dropped split-and-pool design; there is no split tree.' });
  return { protocol_id: entry.protocol_id, protocol_name: entry.protocol_name, chip_id: entry.chip_id, chip_spec: entry.chip_spec,
    operator: 'planner', placeholder: true, synthetic: true, total_duration_s: t + 60, events };
}

let pendingFile = null;

async function selectRun(file) {
  if (busy) { pendingFile = file; return; } // the latest request wins once the current switch finishes
  busy = true;
  try {
    const entry = runs.find((r) => r.file === file) ?? runs[0];
    const run = entry.synthetic ? await module3Run(entry) : await fetchJson(`runs/${entry.file}`);
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
    if (lib.isChip(chip.spec)) { lib.attach(chip, run); ctx.frameChip(chip.footprint); } else lib.detach();
    ui.setRun(run, player.timeline());
    // Capsule-trap sizing only fits the generated capsule chips (not Module 1 v1's cells, nor MJ's
    // and Szonja's chips, whose own design notes size them).
    ui.setSizing(m1.isChip(chip.spec) || lib.isChip(chip.spec) || run.synthetic ? null : run.sizing ?? null);
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
    runs.push(M3_ENTRY);
    try { await loadPipelineData(); } catch (err) { console.warn(err); } // badges only: module views work without it
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

// Entry point: loads runs/index.json, shows the pipeline view or a module view, plays runs.
// The twin only ever reads run files, so a recorded run always works offline.
import { loadContracts } from './contracts.js';
import { createScene } from './scene.js';
import { Chip } from './chip.js';
import { Player } from './player.js';
import { createUI } from './ui.js';
import { findDisplayModel, buildDisplayModel } from './display.js';
import { createControlPanel } from './control.js';
import { createPipeline3D } from './pipeline3d.js';
import { loadStatus } from './status.js';
import { createOverlays } from './overlays.js';
import { createModule1View, CHIP_ID as M1V1 } from './module1.js';
import { createLibraryView, LIB_CHIPS } from './library_modules.js';
import { createPresenter } from './presenter.js';
import { applyBlocks } from './blocks.js';
import { createBench } from './bench.js';
import { overviewHtml, chipCardHtml, homeStagesHtml } from './sidebar.js';

const ROOT = '../'; // serve the repo root: python -m http.server, then open /twin/
// twin/?present: the presenter view (twin/presenter.js), also what `python tools/demo.py` opens.
const PRESENT = new URLSearchParams(location.search).has('present');
if (PRESENT) document.body.classList.add('presenter');
const setUrl = (hash) => { if (!PRESENT) history.replaceState(null, '', hash); }; // the presenter keeps its own #step=N
// Engineering mode adds the run picker, solver tables, control loop, sizing panel and old drafts.
// Off by default so the demo view stays clean; remembered per browser. Never on in the presenter.
const ENG_KEY = 'twin-engineering';
let eng = false;
if (!PRESENT) { try { eng = localStorage.getItem(ENG_KEY) === '1'; } catch { eng = false; } }
document.body.classList.toggle('eng', eng);

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
let module = null;          // 'home' | 'pipeline' | 'bench' | 'm1v1' | 'cp' | 'enc' | 'm3v1' | 'm4' | 'm5' | 'old' (or m1, m1b, m2, m3) | 'other'
let statusData = null;      // protocols/pipeline_status.md (twin/status.js)
let pipelineLoaded = false;
let cultureRun = false;

// Which run a module view opens first.
const PREFERRED = {
  m1: (r) => r.variant === 'control',
  m1b: (r) => r.scenario === 'm1b_cellprep_demo',
  m2: () => true,
  m3: (r) => !r.scenario,
  m1v1: () => true,
  m3v1: (r) => !r.synthetic,          // MJ's protocols/module3_v1.json run first; Szonja's workflow is the other
  old: (r) => r.module === 'm3' && !r.scenario,
};
// The current modules' specs (MJ's 30 x 20 mm chips) have no module block: their runs are told
// apart by chip id. A chip that protocols/pipeline_status.md marks DUPLICATE-RETIRE goes to "Old drafts".
// Cell prep and the spc_demo encapsulation chip have tabs of their own now; sctip_spc_v1, which is
// the barcoding stage (4b in protocols/pipeline_status.md), has no tab, so it opens in the Other
// tab - otherwise its pipeline chip is unclickable and reads as undesigned, which is how the
// single-cell step went missing in the first place.
const CHIP_VIEW = { [M1V1]: 'm1v1', cell_prep_v1: 'cp', spc_demo_v1: 'enc', module3_v1_translated: 'm3v1', ...LIB_CHIPS };
const OTHER_CHIPS = new Set(['sctip_spc_v1']);
const moduleOf = (r) => r.module ?? (statusData?.retired.has(r.chip_id) ? 'retired' : CHIP_VIEW[r.chip_id]
  ?? (/^module2/.test(r.chip_id ?? '') ? 'enc' : OTHER_CHIPS.has(r.chip_id) ? 'other' : null));
// The superseded generated drafts (Module 1 brief, 1b, Module 2 draft, 3 wash) and the retired
// duplicates share one "Old drafts" tab.
const OLD = new Set(['m1', 'm1b', 'm2', 'm3', 'retired']);
const tabOf = (m) => (OLD.has(m) ? 'old' : m);

// Module 3 (Szonja) has no protocol file: the twin plays her workflow block straight from
// chip/modules/module3_step1_3.json, mapped onto the generic commands. Nothing is written.
const M3_SOURCE = 'chip/modules/module3_step1_3.json';
const M3_ENTRY = {
  file: 'module3_v1_translated.workflow', synthetic: 'module3', chip_id: 'module3_v1_translated',
  chip_spec: 'chip/module3_v1_translated.json', protocol_id: 'module3_step1_3',
  protocol_name: "Module 3: load, lysis, wash (Szonja's workflow, translated chip)", operator: 'planner',
};

// Yuvraj's prompt-to-chip result, cached by tools/cache_prompt_demo.py from his branch (not
// merged): the generated chip spec and its placed Flowmatter part STLs. The presenter's first step.
const PROMPT_DIR = 'docs/demo/prompt_to_chip/';
const PROMPT_ENTRY = {
  file: 'capsule_prep_v1.prompt-demo', synthetic: 'prompt', chip_id: 'capsule_prep_v1',
  chip_spec: `${PROMPT_DIR}chip_spec.json`, stl_dir: `${PROMPT_DIR}stl/`, protocol_id: 'prompt_to_chip',
  protocol_name: "Prompt to chip: cell prep and encapsulation (Yuvraj's library parts, cached)", operator: 'planner',
};

// The home page shows the pipeline scene beside its text.
const inPipeline = () => module === 'pipeline' || module === 'home';
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
// The presenter uses the scripted operator only (a recorded LLM run is skipped): no API involved.
const m1 = createModule1View({ ctx, ui, overlays, root: ROOT, operatorKinds: PRESENT ? ['scripted'] : ['llm', 'scripted'] });
m1.setHandlers({ onManual: () => player.setPlaying(false) });
const lib = createLibraryView({ panel: document.getElementById('lib-panel') });
// The sidebar's context block: the chip card, or the bench demo's controls.
const sideContext = document.getElementById('side-context');
const bench = createBench({ el: sideContext, m1, onOpenChip: () => showModule('m1v1') });
sideContext.addEventListener('click', (e) => { if (e.target.closest('[data-open-bench]')) showModule('bench'); });

// The sidebar and panels start just below the header, however many rows it wraps to.
const topbar = document.querySelector('.topbar');
function placePanels() {
  document.documentElement.style.setProperty('--header-bottom', `${Math.ceil(topbar.getBoundingClientRect().bottom)}px`);
}
new ResizeObserver(placePanels).observe(topbar);
placePanels();

const engToggle = document.getElementById('eng-toggle');
engToggle.checked = eng;
engToggle.addEventListener('change', () => setEng(engToggle.checked));
function setEng(on) {
  eng = on;
  engToggle.checked = on;
  document.body.classList.toggle('eng', on);
  try { localStorage.setItem(ENG_KEY, on ? '1' : '0'); } catch { /* private window */ }
  if (!on && (module === 'old' || module === 'other')) { showModule('pipeline'); return; }
  reframe();
}
// Fit the view to the free screen area again (a panel appeared or went away).
function reframe() {
  if (inPipeline()) pipeline.frame();
  else if (chip && view === 'real') ctx.frameChip(chip.footprint);
}

const player = new Player(ctx, {
  onEvent: (i, ev) => { if (!inPipeline()) { ui.setEvent(i, ev); m1.onEvent(i, ev, player.playing); lib.onEvent(i); } },
  onTick: (info) => {
    if (inPipeline()) return;
    ui.setTick(info);
    control.update(info.protocolT);
    overlays.setDay(cultureRun && module !== 'bench' ? info.protocolT : null, player.run?.total_duration_s ?? 0);
    m1.onTick(info, player.playing);
    lib.update(); // runs right after the player draws the frame
  },
  onPlayState: (on) => { if (!inPipeline()) ui.setPlaying(on); },
  onOverlay: (o) => overlays.setNozzle(inPipeline() ? null : o.nozzle),
});

const pipeline = createPipeline3D(document.getElementById('pipeline'), ctx, {
  onOpenModule: (m, file) => showModule(m, file),
  onPhase: (i, ev) => { ui.setEvent(i, ev); markStage(i); },
  onTick: (info) => ui.setTick(info),
  onPlayState: (on) => { ui.setPlaying(on); if (!on && module === 'home') loopHome(); },
});
// On the home page the pipeline plays on a loop, with a pause between runs.
let homeTimer = null;
function loopHome() {
  clearTimeout(homeTimer);
  homeTimer = setTimeout(() => { if (module === 'home' && !pipeline.playing) pipeline.setPlaying(true); }, 2500);
}
const homeEl = document.getElementById('home');
const homeStages = document.getElementById('home-stages');
homeEl.addEventListener('click', (e) => {
  const go = e.target.closest('[data-goto]');
  if (go) { showModule(go.dataset.goto); return; }
  const li = e.target.closest('li[data-open]');
  if (li) showModule(li.dataset.open, li.dataset.file || null);
});
homeEl.addEventListener('keydown', (e) => {
  const li = e.target.closest?.('li[data-open]');
  if (li && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); e.stopPropagation(); li.click(); }
});
ctx.onFrame((dt) => pipeline.update(dt));
ctx.onFrame(() => bench.update());   // before Module 1 draws, so the dye is on time
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
  bench,
  get pipelineData() { return pipelineData; },
  setEng: (on) => setEng(on),
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

async function ensureChip(path, stlDir = null) {
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
  await applyBlocks(chip); // chambers drawn from their Flowmatter block STLs where the library has them
  chipSpec = spec;
  chipPath = path;
  chip.setLabelsVisible(ui.labelsOn());
  ctx.scene.add(chip.group);
  ctx.frameChip(spec.footprint);
  player.setChip(chip);
  ui.setChipInfo(spec, pipelineData?.entries.find((e) => e.spec?.id === spec.id) ?? null, statusData?.byChip.get(spec.id) ?? null);
  // Every chip loads the folder that BELONGS to it (cad/exports/<chip_id>/, or the flat
  // cad/exports/ only when its .chip_id names this chip). Part ids repeat across chips, so
  // loading the flat folder for all of them drew another chip's geometry at another chip's
  // coordinates and toasted "Using CAD for inlet_1, outlet_1" as if it had worked.
  // Awaited, so the chip on screen and the CAD badge describing it can never be two
  // different chips: selectRun's `busy` guard then makes the whole swap atomic. It also runs
  // BEFORE m1.attach, so the Module 1 view can dim the chamber STLs it finds (dye colours).
  await loadChipCad(spec, stlDir);
  if (m1.isChip(spec)) {
    m1.attach(chip);
    ctx.frameChip(spec.footprint); // re-fit now the flow panel takes the left side
  }
  displayManifest = await findDisplayModel(spec.id, ROOT);
  ui.setDisplayAvailable(displayManifest, spec.id, spec);
}

// stlDir: a caller that already knows where this chip's parts are overrides the lookup.
// Yuvraj's prompt-to-chip result is cached outside cad/exports/ (docs/demo/prompt_to_chip/stl/),
// so it has no owner marker to find; everything else is asked for by chip id.
async function loadChipCad(spec, stlDir = null) {
  const target = chip;
  // The listing is read for an explicit folder too: the cached prompt-to-chip result holds a
  // chamber STL per library part and no port STLs at all, so asking for each part by name
  // would log a 404 for every port of it.
  const src = stlDir
    ? { dir: stlDir, owner: spec.id, names: await Chip.listStls(stlDir, ROOT) }
    : await Chip.stlSource(spec.id, ROOT);
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
  if (m === 'other') return runs.filter((r) => { const v = moduleOf(r); return !v || v === 'other'; });
  if (m === 'old') return runs.filter((r) => OLD.has(moduleOf(r)));
  return runs.filter((r) => moduleOf(r) === m);
}

// Pipeline view or one module's view.
async function showModule(m, file = null) {
  const was = module;
  const wasPipeline = was === 'pipeline' || was === 'home';
  module = m;
  ui.setTab(tabOf(m));
  homeEl.hidden = m !== 'home';
  if (was === 'bench' && m !== 'bench') bench.detach();
  if (m === 'pipeline' || m === 'home') {
    sideContext.replaceChildren();
    player.setPlaying(false);
    overlays.setDay(null);
    overlays.setNozzle(null);
    document.body.classList.remove('has-control');
    if (chip) chip.group.visible = false;          // the pipeline scene has its own chips
    if (displayGroup) displayGroup.visible = false;
    await ensurePipeline();
    pipeline.show();
    ui.setRun(pipeline.runLike(), pipeline.timeline());
    ui.setSizing(null);
    pipeline.refresh();
    if (m === 'home') {
      homeStages.innerHTML = homeStagesHtml({ slots: pipeline.slots, status: statusData });
      if (!pipeline.playing) pipeline.setPlaying(true);
      setUrl(location.pathname + location.search);
    } else {
      if (was === 'home') pipeline.setPlaying(false);
      setUrl('#view=pipeline');
    }
    ui.setPlaying(pipeline.playing);
    return;
  }
  pipeline.hide();
  if (chip) chip.group.visible = view !== 'display';
  if (displayGroup) displayGroup.visible = view === 'display';
  if (m === 'bench') {
    // Module 1 in real time beside the physical food-dye run (twin/bench.js).
    const list = runsFor('m1v1');
    if (!list.length) return;
    const pick = list.find(PREFERRED.m1v1) ?? list[0];
    ui.setRuns(list, pick.file);
    player.setPlaying(false);
    while (busy) await new Promise((r) => setTimeout(r, 30));
    if (module !== 'bench') return;
    await selectRun(pick.file);
    if (module !== 'bench') return;
    player.setPlaying(false);
    overlays.setDay(null);
    await setView('real');
    bench.attach();
    ctx.frameChip(chip.footprint);
    setUrl('#view=bench');
    return;
  }
  const list = runsFor(m);
  if (!list.length) return;
  const pick = (file && list.find((r) => r.file === file)) ?? list.find(PREFERRED[m] ?? (() => true)) ?? list[0];
  ui.setRuns(list, pick.file);
  await selectRun(pick.file);
  if (wasPipeline && chip && view === 'real') ctx.frameChip(chip.footprint); // back from the pipeline scene
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
const pipelineCache = new Map();
// The assay variants the pipeline view can switch between (tools/choose_assay.py picks the default).
const VARIANTS = [
  { variant: 'capseq', file: 'protocols/pipeline_capseq.json', label: 'CapSeq · RNA' },
  { variant: 'tipseq', file: 'protocols/pipeline_tipseq.json', label: 'TIP-seq · chromatin DNA' },
];
let pipelineFile = 'protocols/pipeline_mvp.json';
let chosenVariant = null;   // the variant protocols/pipeline_mvp.json copies

// A pipeline file with each module's chip spec and the run its chip opens.
async function loadPipelineData(file = pipelineFile) {
  if (pipelineCache.has(file)) return (pipelineData = pipelineCache.get(file));
  const pipe = await fetchJson(file);
  if (file === 'protocols/pipeline_mvp.json') chosenVariant = pipe.variant ?? null;
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
  pipelineData = { pipe, entries, status: statusData, file, days: Math.round((m1e?.duration_s ?? 0) / 86400) };
  pipelineCache.set(file, pipelineData);
  return pipelineData;
}

async function ensurePipeline() {
  if (pipelineLoaded) return;
  await pipeline.load(await loadPipelineData());
  renderPipelineBar();
  pipelineLoaded = true;
}

// The sidebar on the overview: the CapSeq / TIP-seq switch and every stage in order.
const pipelineBar = document.getElementById('pipeline');
function renderPipelineBar() {
  const current = pipelineData?.pipe?.variant ?? chosenVariant;
  // The legend that used to live here, including what "bought hardware · our chemistry" means,
  // is part of the stage list now: twin/sidebar.js overviewHtml().
  pipelineBar.innerHTML = overviewHtml({ variants: VARIANTS, current, chosen: chosenVariant, slots: pipeline.slots,
    pipe: pipelineData?.pipe, status: statusData });
  markStage(pipeline.currentIndex);
}
// The stage the pipeline is playing stands out in the list.
function markStage(i) {
  const s = pipeline.phaseSlot(i);
  pipelineBar.querySelectorAll('.stages li[data-slot]').forEach((li) => li.classList.toggle('now', Boolean(s) && Number(li.dataset.slot) === s.k));
}
pipelineBar.addEventListener('keydown', (e) => {
  const li = e.target.closest?.('li[data-open]');
  if (li && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); e.stopPropagation(); li.click(); }
});
pipelineBar.addEventListener('click', async (e) => {
  const li = e.target.closest('li[data-open]');
  if (li) { showModule(li.dataset.open, li.dataset.file || null); return; }
  const b = e.target.closest('[data-variant]');
  if (!b) return;
  const v = VARIANTS.find((x) => x.variant === b.dataset.variant);
  if (!v || pipelineData?.pipe?.variant === v.variant) return;
  pipelineFile = v.file;
  pipeline.setPlaying(false);
  await pipeline.load(await loadPipelineData(v.file));
  renderPipelineBar();
  ui.setRun(pipeline.runLike(), pipeline.timeline());
  pipeline.seek(0);
  if (inPipeline()) pipeline.frame();
});

// Szonja's workflow as a run: flows become load / exchange / back-flush; the no-flow steps
// (settle, incubate) join the step before them. Ends with the hand-off out of to_split_tree,
// which now feeds the BARCODING stage and not Module 4: plate-based split-and-pool is dropped,
// so there is no split tree and the port name is only a leftover of it.

// The generated chip as a one-step run: medium into its first part, so the twin draws it live.
function promptRun(entry) {
  return { protocol_id: entry.protocol_id, protocol_name: entry.protocol_name, chip_id: entry.chip_id, chip_spec: entry.chip_spec,
    operator: 'planner', placeholder: true, synthetic: true, total_duration_s: 60,
    events: [{ command: 'exchange', args: { target: 'culture_1', reagent: 'medium', volume_ul: 100, washes: 1 }, t_s: 0, duration_s: 60,
      protocol_step: 1, narration: "Generated by Yuvraj's prompt-to-chip tool from Flowmatter library parts (cached result). Medium into the first part." }] };
}

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

// The sidebar on a chip view: what the chip is, its status and what it does.
function renderChipCard() {
  if (!chip || !chipSpec || module === 'bench' || module === 'pipeline') return;
  const entries = pipelineData?.entries ?? [];
  const k = entries.findIndex((e) => e.spec?.id === chipSpec.id);
  const variant = VARIANTS.find((v) => v.variant === pipelineData?.pipe?.variant)?.label ?? '';
  sideContext.innerHTML = chipCardHtml({
    spec: chipSpec, entry: k >= 0 ? entries[k] : null, md: statusData?.byChip.get(chipSpec.id) ?? null,
    stage: k >= 0 ? { k, n: entries.length, variant } : null, retired: Boolean(statusData?.retired.has(chipSpec.id)),
  });
}

let pendingFile = null;

async function selectRun(file) {
  if (busy) { pendingFile = file; return; } // the latest request wins once the current switch finishes
  busy = true;
  try {
    const entry = runs.find((r) => r.file === file) ?? runs[0];
    const run = entry.synthetic === 'module3' ? await module3Run(entry)
      : entry.synthetic === 'prompt' ? promptRun(entry) : await fetchJson(`runs/${entry.file}`);
    run.__file = entry.file;
    const path = run.chip_spec ?? entry.chip_spec;
    if (!path) throw new Error(`${entry.file} does not say which chip spec it used (chip_spec).`);
    const sameChip = chip && path === chipPath;
    const autoplay = player.playing || !player.run;
    if (sameChip && player.run) await player.fadeOut(); // smooth swap on the same chip
    await ensureChip(path, entry.stl_dir ?? null);
    const hadControl = document.body.classList.contains('has-control');
    control.setRun(run, runs);
    if (hadControl !== document.body.classList.contains('has-control') && view === 'real') ctx.frameChip(chip.footprint);
    cultureRun = run.events.some((e) => e.command === 'culture') || run.total_duration_s > 3 * 86400;
    player.load(run, { autoplay });
    if (lib.isChip(chip.spec)) { lib.attach(chip, run); ctx.frameChip(chip.footprint); } else lib.detach();
    ui.setRun(run, player.timeline());
    // Capsule-trap sizing only fits the generated capsule chips (not Module 1 v1's cells, nor MJ's
    // and Szonja's chips, whose own design notes size them).
    ui.setSizing(chip.spec.module && !run.synthetic ? run.sizing ?? null : null);
    player.refresh(); // re-send the current event now the timeline exists
    renderChipCard();
    setUrl(`#run=${encodeURIComponent(entry.file)}`);
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
    runs.push(M3_ENTRY, PROMPT_ENTRY);
    statusData = await loadStatus(ROOT);
    try { await loadPipelineData(); } catch (err) { console.warn(err); } // badges only: module views work without it
    if (PRESENT) {
      const presenter = createPresenter({
        showModule: (m, file) => showModule(m, file),
        pipeline, player, m1, lib, ctx,
        get chip() { return chip; },
        promptFile: PROMPT_ENTRY.file,
        promptDir: ROOT + PROMPT_DIR,
        frame: () => { if (chip && !inPipeline()) ctx.frameChip(chip.footprint); },
        preload: () => loadPipelineData(),
      });
      window.__presenter = presenter;
      window.__twin.ready = true;
      await presenter.start();
      return;
    }
    const params = new URLSearchParams(location.hash.slice(1));
    const wanted = decodeURIComponent(params.get('run') ?? '');
    const entry = runs.find((r) => r.file === wanted);
    const tab = entry ? tabOf(moduleOf(entry) ?? 'other') : null;
    if (entry && (tab === 'old' || tab === 'other') && !eng) setEng(true); // a link to an old run is an engineer's
    if (entry) await showModule(moduleOf(entry) ?? 'other', entry.file);
    else if (params.get('view') === 'bench') await showModule('bench');
    else if (params.get('view') === 'pipeline') await showModule('pipeline');
    else await showModule('home');
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

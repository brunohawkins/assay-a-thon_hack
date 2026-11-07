// Entry point: loads runs/index.json, builds the chip, plays the selected run.
// The twin only ever reads run files, so a recorded run always works offline.
import { createScene } from './scene.js';
import { Chip } from './chip.js';
import { Player } from './player.js';
import { createUI } from './ui.js';

const ROOT = '../'; // serve the repo root: python -m http.server, then open /twin/

const ctx = createScene(document.getElementById('viewport'));
let chip = null;
let chipPath = null;
let runs = [];
let busy = false;

const ui = createUI({
  onSelectRun: (file) => selectRun(file),
  onPlayPause: () => player.toggle(),
  onStep: () => player.step(),
  onSpeed: (x) => player.setSpeed(x),
  onSeek: (t) => player.seek(t),
  onLabels: (on) => chip?.setLabelsVisible(on),
});

const player = new Player(ctx, {
  onEvent: (i, ev) => ui.setEvent(i, ev),
  onTick: (info) => ui.setTick(info),
  onPlayState: (on) => ui.setPlaying(on),
});

// For debugging in the browser console, and for the headless smoke test.
window.__twin = { player, get chip() { return chip; }, ready: false };

async function fetchJson(path) {
  const res = await fetch(ROOT + path, { cache: 'no-store' });
  if (!res.ok) throw new Error(`Could not load ${path} (HTTP ${res.status}).`);
  return res.json();
}

async function ensureChip(path) {
  if (chip && chipPath === path) return;
  const spec = await fetchJson(path);
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
}

async function selectRun(file) {
  if (busy) return;
  busy = true;
  try {
    const entry = runs.find((r) => r.file === file) ?? runs[0];
    const run = await fetchJson(`runs/${entry.file}`);
    const path = run.chip_spec ?? entry.chip_spec;
    if (!path) throw new Error(`${entry.file} does not say which chip spec it used (chip_spec).`);
    const sameChip = chip && path === chipPath;
    const autoplay = player.playing || !player.run;
    if (sameChip && player.run) await player.fadeOut(); // smooth swap on the same chip
    await ensureChip(path);
    player.load(run, { autoplay });
    ui.setRun(run, player.timeline());
    player.refresh(); // re-send the current event now the timeline exists
    history.replaceState(null, '', `#run=${encodeURIComponent(entry.file)}`);
  } finally {
    busy = false;
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

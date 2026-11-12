// Loading and normalising THIS project's assay description.
//
// docs/modules.json is one entry per stage of the real pipeline (iPSCs -> three neural
// lineages -> single-cell transcriptome, plus the bulk multi-omic arm). docs/videos/
// manifest.json is the bench-recording slot for each printed chip.
//
// Neither is inside the /file allowlist yet, so several routes are tried and the caller
// is told exactly which one the backend still needs.

import { api, ApiError } from './api.js';
import { pick } from './dom.js';

/** Routes that could serve a repo file under docs/. The first that answers JSON wins. */
export function docsCandidates(path, runId) {
  const out = [`/${path}`, `/api/${path}`];
  if (runId) out.push(`/api/runs/${encodeURIComponent(runId)}/file?path=${encodeURIComponent(path)}`);
  return out;
}

export const NEEDED_ROUTE =
  'GET /docs/<path> (or docs/ added to the /file allowlist) for docs/modules.json, '
  + 'docs/videos/manifest.json and the render files they point at';

async function tryJson(url) {
  let res;
  try { res = await fetch(url, { headers: { Accept: 'application/json' } }); }
  catch (cause) { throw new ApiError('cannot reach the backend', { offline: true, path: url, detail: String(cause.message) }); }
  if (!res.ok) return null;
  const text = await res.text();
  if (!/^\s*[[{]/.test(text)) return null;
  try { return JSON.parse(text); } catch (_) { return null; }
}

/** Load a docs/ file, returning {data, url} or {missing:true, tried}. */
export async function loadDocsJson(path, runId) {
  const tried = docsCandidates(path, runId);
  for (const url of tried) {
    const data = await tryJson(url);
    if (data) return { data, url };
  }
  return { missing: true, tried };
}

/**
 * Which run can serve a path.
 * assay2chip/work/<id>/... belongs to run <id>; chip/, protocols/ and cad/figures/ are
 * open to every run, so any id will do.
 */
export function runIdForPath(path, fallback) {
  const m = /^assay2chip\/work\/([^/]+)\//.exec(String(path || ''));
  return m ? m[1] : fallback;
}

/** A URL for one artifact path, or null when no run can serve it. */
export function artifactUrl(path, fallbackRunId) {
  if (!path) return null;
  const runId = runIdForPath(path, fallbackRunId);
  if (!runId) return null;
  return api.fileUrl(runId, path);
}

function normPort(p) {
  return {
    id: String(pick(p, 'id') ?? ''),
    type: String(pick(p, 'type') ?? ''),
    role: pick(p, 'role') ?? null,
    fed: Number(pick(p, 'fed_in_steps') ?? NaN),
    vented: Number(pick(p, 'vented_in_steps') ?? NaN),
  };
}

function normStep(s) {
  return {
    step: pick(s, 'step'),
    operation: String(pick(s, 'operation') ?? ''),
    target: asList(s.target),
    reagent: asList(s.reagent),
    volume_ul: pick(s, 'volume_ul'),
    washes: pick(s, 'washes'),
    flow_ul_min: pick(s, 'flow_ul_min'),
    duration_s: pick(s, 'duration_s'),
    temp_c: pick(s, 'temp_c'),
    exchange_kind: pick(s, 'exchange_kind') ?? null,
    feed: asList(s.feed),
    vent: asList(s.vent),
  };
}

const asList = (v) => (Array.isArray(v) ? v.map(String) : v ? [String(v)] : []);

function normVideo(v) {
  if (!v || typeof v !== 'object') return null;
  return {
    module: String(pick(v, 'module') ?? ''),
    chip_id: String(pick(v, 'chip_id') ?? ''),
    title: String(pick(v, 'title') ?? ''),
    shot: String(pick(v, 'shot') ?? ''),
    shot_source: String(pick(v, 'shot_source') ?? ''),
    url: pick(v, 'url') || null,
    release_tag: pick(v, 'release_tag') || null,
    recorded: !!v.recorded,
    duration_s: pick(v, 'duration_s') ?? null,
  };
}

function normChip(c) {
  if (!c || typeof c !== 'object') return null;
  const art = c.artifacts && typeof c.artifacts === 'object' ? c.artifacts : {};
  const cfd = art.cfd && typeof art.cfd === 'object' ? art.cfd : {};
  return {
    chip_id: String(pick(c, 'chip_id') ?? ''),
    chip_path: String(pick(c, 'chip') ?? ''),
    chip_name: String(pick(c, 'chip_name') ?? ''),
    scale: String(pick(c, 'scale') ?? ''),
    depth_mm: pick(c, 'fluidic_depth_mm'),
    printed: !!c.printed_for_demo,
    slot: String(pick(c, 'slot') ?? ''),
    placeholder: !!c.placeholder,
    protocol: String(pick(c, 'protocol') ?? ''),
    steps: (Array.isArray(c.function) ? c.function : []).map(normStep),
    ports: (Array.isArray(c.ports) ? c.ports : []).map(normPort),
    chambers: Array.isArray(c.chambers) ? c.chambers : [],
    thermal_zones: Array.isArray(c.thermal_zones) ? c.thermal_zones : [],
    off_chip: asList(c.off_chip),
    known_limits: (Array.isArray(c.known_limits) ? c.known_limits : []).map((k) => ({
      source: String(pick(k, 'source') ?? ''), text: String(pick(k, 'text') ?? String(k)),
    })),
    full_spec: c.full_spec && typeof c.full_spec === 'object' ? normChip(c.full_spec)
      : (typeof c.full_spec === 'string' ? { chip_path: c.full_spec, chip_id: '', steps: [], ports: [] } : null),
    plan_svgs: asList(art.plan_svgs),
    renders_3d: asList(art.renders_3d),
    cfd: {
      figures: asList(cfd.figures),
      steps_simulated: Array.isArray(cfd.steps_simulated) ? cfd.steps_simulated : [],
      report: pick(cfd, 'report') ?? null,
      field: pick(cfd, 'field') ?? null,
      mesh: pick(cfd, 'mesh') ?? null,
    },
    openfoam: asList(art.openfoam || art.renders_openfoam),
    animation: pick(art, 'animation', 'pipeline_animation') ?? null,
    flow_runs: asList(art.flow_runs),
    demo: c.demo ?? null,
    video: normVideo(c.video),
  };
}

function normHandoff(h) {
  if (!h || typeof h !== 'object') return null;
  return {
    id: String(pick(h, 'id') ?? ''),
    carries: String(pick(h, 'carries') ?? ''),
    from: h.from && typeof h.from === 'object' ? h.from : null,
    to: h.to && typeof h.to === 'object' ? h.to : null,
    after_step: pick(h, 'after_step') ?? null,
    volume_ul: pick(h, 'volume_ul') ?? null,
    particle_um: pick(h, 'particle_um') ?? null,
    connector: String(pick(h, 'connector') ?? ''),
    automated: h.automated,
    note: String(pick(h, 'note') ?? ''),
  };
}

export function normStage(s, i) {
  const o = s && typeof s === 'object' ? s : {};
  const chip = normChip(o.module);
  return {
    order: Number(pick(o, 'order') ?? i),
    id: String(pick(o, 'stage', 'id') ?? `stage-${i}`),
    name: String(pick(o, 'name') ?? ''),
    kind: String(pick(o, 'kind') ?? (chip ? 'CHIP' : 'OFF-CHIP')).toUpperCase(),
    status: String(pick(o, 'status') ?? ''),
    status_note: pick(o, 'status_note') ?? null,
    owner: String(pick(o, 'owner') ?? ''),
    where: String(pick(o, 'where') ?? ''),
    note: String(pick(o, 'note') ?? ''),
    protocol: String(pick(o, 'protocol') ?? ''),
    source: pick(o, 'source') ?? null,
    branch_from: pick(o, 'branch_from') ?? null,
    arrives: (Array.isArray(o.arrives) ? o.arrives : []).map(normHandoff).filter(Boolean),
    leaves: (Array.isArray(o.leaves) ? o.leaves : []).map(normHandoff).filter(Boolean),
    chip,
  };
}

/** docs/modules.json -> the shape the view renders. */
export function normModulesDoc(raw) {
  const d = raw && typeof raw === 'object' ? raw : {};
  const stages = (Array.isArray(d.stages) ? d.stages : []).map(normStage);
  stages.sort((a, b) => a.order - b.order);
  return {
    generated_by: String(pick(d, 'generated_by') ?? ''),
    pipeline: String(pick(d, 'pipeline') ?? ''),
    pipeline_id: String(pick(d, 'pipeline_id') ?? ''),
    variant: String(pick(d, 'variant') ?? ''),
    inputs: d.inputs && typeof d.inputs === 'object' ? d.inputs : {},
    printed: (Array.isArray(d.printed_for_demo) ? d.printed_for_demo : []).map((p) => ({
      slot: String(pick(p, 'slot') ?? ''),
      chip_id: String(pick(p, 'chip_id') ?? ''),
      chip_path: String(pick(p, 'chip') ?? ''),
    })),
    stages,
    not_in_pipeline: (Array.isArray(d.not_in_pipeline) ? d.not_in_pipeline : []).map(normStage),
    raw: d,
  };
}

/** docs/videos/manifest.json -> a map from chip id to its video slot. */
export function normVideoManifest(raw) {
  const list = Array.isArray(raw) ? raw : (raw && Array.isArray(raw.videos) ? raw.videos : []);
  const byChip = new Map();
  const byModule = new Map();
  for (const v of list.map(normVideo).filter(Boolean)) {
    if (v.chip_id) byChip.set(v.chip_id, v);
    if (v.module) byModule.set(v.module, v);
  }
  return {
    about: raw && raw.about ? String(raw.about) : '',
    videos: list.map(normVideo).filter(Boolean),
    byChip,
    byModule,
  };
}

/** Only play media the manifest actually points at: http(s), or a file under this site's own media/. */
export function playableUrl(url) {
  const s = String(url || '').trim();
  if (!s) return null;
  if (/^media\/[\w./-]+$/.test(s) && !s.includes('..')) return s;
  if (!/^https?:\/\//i.test(s)) return null;
  return s;
}

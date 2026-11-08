// Normalising whatever shape the backend sends into the one shape the views use.
//
// web/README.md fixes the field names but not whether per-stage status arrives as an
// array or as an object keyed by stage id, so both are accepted. Same for the SSE
// payload key that names the stage.

import { pick, at } from './dom.js';

/** Fallback stage list from assay2chip/README.md, used when /api/stages is not ready. */
export const FALLBACK_STAGES = [
  { id: 's0', name: 'ingest', description: 'paper / protocol in - text, tables and provenance' },
  { id: 's1', name: 'extract', description: 'ordered wet-lab steps, with confidence and open questions' },
  { id: 's2', name: 'normalise', description: 'canonical unit operations' },
  { id: 's3', name: 'triage', description: 'on chip, off chip or missing part' },
  { id: 's4', name: 'partition', description: 'which operations go on which chip' },
  { id: 's5', name: 'group', description: 'a design file per chip' },
  { id: 's6', name: 'build', description: 'parts sized from physics and placed' },
  { id: 's7', name: 'route', description: 'planar channels, no crossings' },
  { id: 's8', name: 'protocol', description: 'flows from the built geometry' },
  { id: 's9', name: 'verify', description: 'validate, check_chip_spec, figures - must be 0 errors' },
  { id: 's10', name: 'cfd', description: 'end-to-end flow simulation and automatic fixes' },
  { id: 's11', name: 'fabricate', description: 'STL, mould, closed DXF and renders' },
  { id: 's12', name: 'report', description: 'problems, fixes and artefacts' },
];

const STAGE_RE = /^s(\d+)/i;

export function stageOrder(id) {
  const m = STAGE_RE.exec(String(id || ''));
  return m ? Number(m[1]) : 999;
}

/** /api/stages -> [{id, name, description}] */
export function normaliseStages(raw) {
  let list = [];
  if (Array.isArray(raw)) list = raw;
  else if (raw && Array.isArray(raw.stages)) list = raw.stages;
  else if (raw && typeof raw === 'object') {
    list = Object.entries(raw).map(([id, v]) => (typeof v === 'string' ? { id, description: v } : { id, ...v }));
  }
  const out = list.map((s) => {
    if (typeof s === 'string') {
      const m = /^(s\d+)[\s_:-]*(.*)$/i.exec(s);
      return m ? { id: m[1].toLowerCase(), name: m[2] || '', description: '' } : { id: s, name: '', description: '' };
    }
    const id = String(pick(s, 'id', 'stage', 'key', 'slug') ?? '');
    return {
      id,
      name: String(pick(s, 'name', 'title', 'label') ?? '').replace(/^s\d+[\s_:-]*/i, ''),
      description: String(pick(s, 'description', 'desc', 'summary', 'one_liner') ?? ''),
    };
  }).filter((s) => s.id);
  out.sort((a, b) => stageOrder(a.id) - stageOrder(b.id));
  return out.length ? out : FALLBACK_STAGES;
}

const VALID_STATUS = new Set(['pending', 'queued', 'running', 'done', 'failed', 'skipped', 'not_implemented', 'unknown']);

function normStatus(v) {
  const s = String(v || 'pending').toLowerCase().replace(/[\s-]+/g, '_');
  if (VALID_STATUS.has(s)) return s;
  if (s === 'ok' || s === 'complete' || s === 'completed' || s === 'success') return 'done';
  if (s === 'error' || s === 'fail') return 'failed';
  if (s === 'notimplemented' || s === 'todo' || s === 'unimplemented') return 'not_implemented';
  if (s === 'active' || s === 'in_progress') return 'running';
  return 'pending';
}

function normOutputs(v) {
  if (!v) return [];
  const arr = Array.isArray(v) ? v : [v];
  return arr.map((o) => {
    if (typeof o === 'string') return { path: o };
    if (o && typeof o === 'object') return { path: String(pick(o, 'path', 'file', 'name') ?? ''), bytes: o.bytes, kind: o.kind };
    return { path: String(o) };
  }).filter((o) => o.path);
}

/** One stage entry of a run manifest. */
export function normaliseStageEntry(id, raw) {
  const o = raw && typeof raw === 'object' ? raw : { status: raw };
  return {
    id: String(id),
    status: normStatus(pick(o, 'status', 'state')),
    started: pick(o, 'started', 'start', 'started_at') ?? null,
    seconds: Number(pick(o, 'seconds', 'duration_s', 'elapsed') ?? NaN),
    outputs: normOutputs(pick(o, 'outputs', 'files', 'artifacts')),
    error: pick(o, 'error', 'err', 'message') ?? null,
    note: pick(o, 'note', 'detail') ?? null,
  };
}

/** GET /api/runs/<id> -> {run_id, title, created, status, options, llm, scale, stages:Map} */
export function normaliseManifest(raw, runId) {
  const m = raw && typeof raw === 'object' ? raw : {};
  const stages = new Map();
  const src = pick(m, 'stages', 'stage_status', 'pipeline') ?? {};
  if (Array.isArray(src)) {
    for (const s of src) {
      const id = String(pick(s, 'id', 'stage', 'key') ?? '');
      if (id) stages.set(id, normaliseStageEntry(id, s));
    }
  } else if (src && typeof src === 'object') {
    for (const [id, v] of Object.entries(src)) stages.set(id, normaliseStageEntry(id, v));
  }
  const options = m.options && typeof m.options === 'object' ? m.options : {};
  return {
    run_id: String(pick(m, 'run_id', 'id') ?? runId ?? ''),
    title: String(pick(m, 'title', 'name') ?? '') || String(runId ?? ''),
    created: pick(m, 'created', 'created_at', 'started') ?? null,
    status: normStatus(pick(m, 'status', 'state')),
    error: pick(m, 'error') ?? null,
    // The backend explains a queued run in its own words: runs are serialised because
    // S5-S11 write into the shared chip/ and protocols/ trees.
    message: String(pick(m, 'message') ?? ''),
    queued_behind: Number(pick(m, 'queued_behind') ?? NaN),
    missing_contract_keys: Array.isArray(m.missing_contract_keys) ? m.missing_contract_keys.map(String) : [],
    // False when the run was executed outside the web server: there is then no
    // per-stage record, and "pending" would be a lie about what happened.
    has_manifest: at(m, 'manifest', 'exists') !== false,
    kind: String(pick(m, 'kind', 'job_kind') ?? ''),
    llm: String(pick(m, 'llm') ?? options.llm ?? ''),
    scale: String(pick(m, 'scale') ?? options.scale ?? ''),
    source: m.source && typeof m.source === 'object' ? m.source : null,
    chips: Array.isArray(m.chips) ? m.chips : [],
    findings_by_severity: m.findings_by_severity && typeof m.findings_by_severity === 'object' ? m.findings_by_severity : null,
    options,
    stages,
    raw: m,
  };
}

/** GET /api/runs -> [{run_id, title, created, status, chips, findings_by_severity}] */
export function normaliseRunList(raw) {
  const list = Array.isArray(raw) ? raw : (raw && Array.isArray(raw.runs) ? raw.runs : []);
  return list.map((r) => {
    if (typeof r === 'string') return { run_id: r, title: r, created: null, status: 'pending', chips: [], findings_by_severity: null };
    return {
      run_id: String(pick(r, 'run_id', 'id') ?? ''),
      title: String(pick(r, 'title', 'name') ?? '') || String(pick(r, 'run_id', 'id') ?? ''),
      created: pick(r, 'created', 'created_at') ?? null,
      status: normStatus(pick(r, 'status', 'state')),
      chips: Array.isArray(r.chips) ? r.chips : (Number.isFinite(Number(r.chips)) ? [] : []),
      chip_count: Array.isArray(r.chips) ? r.chips.length : Number(r.chips ?? NaN),
      findings_by_severity: r.findings_by_severity && typeof r.findings_by_severity === 'object' ? r.findings_by_severity : null,
      llm: String(pick(r, 'llm') ?? ''),
      kind: String(pick(r, 'kind') ?? ''),
    };
  }).filter((r) => r.run_id);
}

/** GET /api/runs/<id>/artifacts -> [{path, kind, stage, bytes}] */
export function normaliseArtifacts(raw) {
  const list = Array.isArray(raw) ? raw : (raw && Array.isArray(raw.artifacts) ? raw.artifacts : []);
  return list.map((a) => {
    const path = String(typeof a === 'string' ? a : (pick(a, 'path', 'file', 'name') ?? ''));
    const ext = (path.split('.').pop() || '').toLowerCase();
    return {
      path,
      kind: String((typeof a === 'object' && a.kind) || ext || ''),
      stage: String((typeof a === 'object' && pick(a, 'stage', 'from_stage')) || ''),
      bytes: typeof a === 'object' ? Number(a.bytes ?? NaN) : NaN,
    };
  }).filter((a) => a.path);
}

/** One normalised CFD finding (assay2chip/cfd/README.md). */
export function normaliseFinding(raw, i = 0) {
  const f = raw && typeof raw === 'object' ? raw : {};
  const sev = String(pick(f, 'severity', 'sev') ?? 'low').toLowerCase();
  return {
    id: String(pick(f, 'id', 'finding_id') ?? `f${i}`),
    cls: String(pick(f, 'class', 'cls', 'kind', 'type') ?? 'unknown'),
    severity: ['blocker', 'high', 'medium', 'low'].includes(sev) ? sev : 'low',
    chip_id: String(pick(f, 'chip_id', 'chip') ?? ''),
    feature_id: String(pick(f, 'feature_id', 'feature') ?? ''),
    step: pick(f, 'step') ?? null,
    metric: String(pick(f, 'metric') ?? ''),
    value: pick(f, 'value') ?? null,
    limit: pick(f, 'limit') ?? null,
    units: String(pick(f, 'units', 'unit') ?? ''),
    message: String(pick(f, 'message', 'msg', 'text') ?? ''),
    fix_hint: String(pick(f, 'fix_hint', 'hint') ?? ''),
  };
}

export function normaliseFindings(raw) {
  const list = Array.isArray(raw) ? raw : (raw && Array.isArray(raw.findings) ? raw.findings : []);
  return list.map(normaliseFinding);
}

/** One normalised fix (fixes.json). */
export function normaliseFix(raw, i = 0) {
  const f = raw && typeof raw === 'object' ? raw : {};
  const pred = f.predicted && typeof f.predicted === 'object' ? f.predicted : {};
  return {
    id: String(pick(f, 'id') ?? `fix${i}`),
    finding_id: String(pick(f, 'finding_id') ?? ''),
    action: String(pick(f, 'action') ?? ''),
    target: String(pick(f, 'target') ?? ''),
    params: f.params && typeof f.params === 'object' ? f.params : null,
    metric: String(pick(pred, 'metric') ?? ''),
    before: pick(pred, 'before') ?? null,
    after: pick(pred, 'after') ?? null,
    units: String(pick(pred, 'units', 'unit') ?? pick(f, 'units') ?? ''),
    auto_applicable: !!f.auto_applicable,
    applied: !!f.applied,
    verified_after: pick(f, 'verified_after') ?? null,
  };
}

export function normaliseFixes(raw) {
  const list = Array.isArray(raw) ? raw : (raw && Array.isArray(raw.fixes) ? raw.fixes : []);
  return list.map(normaliseFix);
}

/** Count findings per severity. */
export function countSeverity(findings) {
  const out = { blocker: 0, high: 0, medium: 0, low: 0 };
  for (const f of findings) if (out[f.severity] !== undefined) out[f.severity] += 1;
  return out;
}

/** The verdict sentence for a CFD run: blockers decide it. */
export function verdict(findings) {
  const c = countSeverity(findings);
  if (c.blocker > 0) return { word: 'will not run', kind: 'bad', counts: c };
  if (c.high > 0) return { word: 'runs, with high-severity problems', kind: 'warn', counts: c };
  if (c.medium + c.low > 0) return { word: 'runs, with minor notes', kind: 'ok', counts: c };
  return { word: 'no findings', kind: 'ok', counts: c };
}

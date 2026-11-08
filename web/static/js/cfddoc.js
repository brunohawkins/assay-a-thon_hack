// Reading S10's output.
//
// The backend writes ONE consolidated document, cfd/cfd.json, holding the per-module
// status, the per-chip steps / figures / findings / fixes, the coupled handoffs and the
// solver's own validation. Older and partial runs may instead leave separate
// findings.json / fixes.json / pipeline.field.json, so both are accepted and normalised
// into the same shape here.

import { pick, at } from './dom.js';
import { normaliseFindings, normaliseFixes } from './model.js';

/** Per-module status table: this is where "NOT YET IMPLEMENTED" lives for CFD. */
export function normaliseModules(raw) {
  const src = raw && typeof raw === 'object' ? raw : {};
  return Object.entries(src).map(([name, v]) => {
    const o = v && typeof v === 'object' ? v : { status: v };
    const status = String(pick(o, 'status', 'state') ?? 'unknown').toLowerCase();
    const errors = Array.isArray(o.errors) ? o.errors.map(String) : [];
    return {
      name,
      status,
      detail: String(pick(o, 'detail', 'note') ?? ''),
      entry_point: String(pick(o, 'entry_point', 'entry') ?? ''),
      calls: Number(pick(o, 'calls') ?? NaN),
      errors,
      // "ok but never called" is not the same as "ran": say which.
      unused: /never needed|never called|not needed/i.test(String(o.detail || '')) || Number(o.calls) === 0,
      broken: status === 'error' || status === 'failed' || errors.length > 0,
      missing: status === 'missing' || status === 'not_implemented' || status === 'unimplemented',
    };
  }).sort((a, b) => a.name.localeCompare(b.name));
}

/** One protocol step of a chip, as the solver scoped it. */
function normaliseStep(s, i) {
  const o = s && typeof s === 'object' ? s : {};
  return {
    step: pick(o, 'step', 'index') ?? i + 1,
    key: String(pick(o, 'scenario_id', 'id') ?? `step-${i}`),
    label: String(pick(o, 'label', 'name') ?? `step ${pick(o, 'step') ?? i + 1}`),
    operation: String(pick(o, 'operation', 'kind') ?? ''),
    targets: Array.isArray(o.targets) ? o.targets.map(String) : [],
    flow_ul_min: pick(o, 'flow_ul_min'),
    temp_c: pick(o, 'temp_c'),
    cites: String(pick(o, 'cites', 'source') ?? ''),
    // Carried through because a value the solver inferred is not a value the protocol stated.
    inferred: !!o.inferred,
    ambiguous: !!o.ambiguous,
    viscosity_pa_s: pick(o, 'viscosity_pa_s'),
  };
}

/** Figures are flat paths named <chip>_s<step>_<metric>.svg. */
export function indexFigures(paths) {
  const byStep = new Map();
  for (const p of paths || []) {
    const file = String(p).split('/').pop();
    const m = /^(.*)_s([\w.]+)_(pressure|speed|shear|residence|velocity|re)\.svg$/i.exec(file);
    const step = m ? m[2] : 'all';
    const metric = m ? m[3].toLowerCase() : 'other';
    if (!byStep.has(step)) byStep.set(step, new Map());
    byStep.get(step).set(metric === 'velocity' ? 'speed' : metric, String(p));
  }
  return byStep;
}

function normaliseChip(c, i) {
  const o = c && typeof c === 'object' ? c : {};
  const fp = o.footprint_mm || o.footprint;
  const steps = (Array.isArray(o.steps) ? o.steps : []).map(normaliseStep);
  const mesh = o.mesh && typeof o.mesh === 'object' ? o.mesh : {};
  return {
    chip_id: String(pick(o, 'chip_id', 'id') ?? `chip ${i + 1}`),
    module: pick(o, 'module') ?? null,
    chip_path: String(pick(o, 'chip_path', 'path') ?? ''),
    protocol_path: String(pick(o, 'protocol_path') ?? ''),
    scale: String(pick(o, 'scale') ?? ''),
    footprint: Array.isArray(fp) ? { x: Number(fp[0]), y: Number(fp[1]) } : (fp || null),
    steps,
    figures: Array.isArray(o.figures) ? o.figures.map(String) : [],
    figuresByStep: indexFigures(o.figures),
    findings: normaliseFindings(o.findings),
    fixes: normaliseFixes(o.fixes),
    notes: Array.isArray(o.notes) ? o.notes.map(String) : [],
    mesh: {
      status: String(pick(mesh, 'status') ?? ''),
      nodes: Number(pick(mesh, 'nodes') ?? NaN),
      edges: Number(pick(mesh, 'edges') ?? NaN),
      grid_mm: Number(pick(mesh, 'grid_mm') ?? NaN),
      mu_pa_s: Number(pick(mesh, 'mu_pa_s') ?? NaN),
    },
  };
}

/** The solver's own benchmark results. A solver nobody has checked is worse than none. */
export function normaliseValidation(raw) {
  const v = raw && typeof raw === 'object' ? raw : null;
  if (!v) return null;
  const counts = v.counts && typeof v.counts === 'object' ? v.counts : {};
  return {
    status: String(pick(v, 'status') ?? 'unknown').toLowerCase(),
    trusted: v.trusted === undefined ? null : !!v.trusted,
    detail: String(pick(v, 'detail') ?? ''),
    pass: Number(counts.pass ?? NaN),
    fail: Number(counts.fail ?? NaN),
    skipped: Number(counts.skipped ?? NaN),
    recommended_grid_mm: pick(v, 'recommended_grid_mm'),
    solver_available: at(v, 'solver', 'available'),
    solver_reason: String(at(v, 'solver', 'reason') || ''),
    checks: (Array.isArray(v.checks) ? v.checks : []).map((c) => ({
      name: String(pick(c, 'name') ?? ''),
      ok: c.ok === undefined ? null : !!c.ok,
      status: String(pick(c, 'status') ?? '').toLowerCase(),
      expected: pick(c, 'expected'),
      got: pick(c, 'got'),
      detail: String(pick(c, 'detail') ?? ''),
    })),
    findings: normaliseFindings(v.validation_findings),
  };
}

function normaliseHandoffs(raw) {
  return (Array.isArray(raw) ? raw : []).map((h) => ({
    from: String(pick(h, 'from', 'from_chip') ?? ''),
    to: String(pick(h, 'to', 'to_chip') ?? ''),
    from_port: String(pick(h, 'from_port') ?? ''),
    to_port: String(pick(h, 'to_port') ?? ''),
    carries: String(pick(h, 'carries') ?? ''),
    volume_ul: pick(h, 'volume_ul'),
    dead_volume_ul: pick(h, 'dead_volume_ul', 'dead_volume'),
    pressure_kpa: pick(h, 'pressure_kpa', 'pressure_budget_kpa', 'dp_kpa'),
    automated: h.automated,
    how: String(pick(h, 'how') ?? ''),
    risks: Array.isArray(h.risks) ? h.risks.map(String) : [],
  }));
}

/** cfd/cfd.json -> the shape the CFD view renders. */
export function normaliseCfdDoc(raw) {
  const d = raw && typeof raw === 'object' ? raw : {};
  const chips = (Array.isArray(d.chips) ? d.chips : []).map(normaliseChip);
  const topFindings = normaliseFindings(d.findings);
  const topFixes = normaliseFixes(d.fixes);
  // Fall back to the per-chip lists when the top level is empty, and vice versa.
  const findings = topFindings.length ? topFindings : chips.flatMap((c) => c.findings);
  const fixes = topFixes.length ? topFixes : chips.flatMap((c) => c.fixes);
  return {
    run: String(pick(d, 'run', 'id') ?? ''),
    title: String(pick(d, 'title', 'name') ?? ''),
    mode: String(pick(d, 'mode') ?? ''),
    generated_at: pick(d, 'generated_at') ?? null,
    modules: normaliseModules(d.modules),
    chips,
    handoffs: normaliseHandoffs(d.handoffs),
    coupling: d.coupling && typeof d.coupling === 'object' ? d.coupling : null,
    findings,
    fixes,
    validation: normaliseValidation(d.validation),
    notes: Array.isArray(d.notes) ? d.notes.map(String) : [],
    raw: d,
  };
}

/** An empty document, so the view can render the same way when nothing loaded. */
export function emptyCfdDoc() {
  return {
    run: '', title: '', mode: '', generated_at: null, modules: [], chips: [], handoffs: [],
    coupling: null, findings: [], fixes: [], validation: null, notes: [], raw: null,
  };
}

/** Build a document out of the older separate files, so partial runs still render. */
export function cfdDocFromParts({ findings, fixes, pipeline }) {
  const doc = emptyCfdDoc();
  doc.findings = findings ? normaliseFindings(findings) : [];
  doc.fixes = fixes ? normaliseFixes(fixes) : [];
  if (pipeline && typeof pipeline === 'object') {
    doc.handoffs = normaliseHandoffs(pipeline.handoffs);
    doc.chips = (Array.isArray(pipeline.chips) ? pipeline.chips : []).map((c, i) =>
      normaliseChip(typeof c === 'string' ? { chip_id: c } : c, i));
    doc.raw = pipeline;
  }
  return doc;
}

/** Where a run's CFD files live, from its artifact list. */
export function indexCfdFiles(artifacts) {
  const out = { doc: null, findings: null, fixes: null, pipeline: null, figures: [], meshes: new Map() };
  for (const a of artifacts) {
    const m = /(?:^|\/)cfd\/(.+)$/.exec(a.path);
    if (!m) continue;
    const name = m[1];
    if (/^cfd\.json$/i.test(name)) { out.doc = a.path; continue; }
    if (/^findings\.json$/i.test(name)) { out.findings = a.path; continue; }
    if (/^fixes\.json$/i.test(name)) { out.fixes = a.path; continue; }
    if (/^pipeline\.field\.json$/i.test(name)) { out.pipeline = a.path; continue; }
    if (/^figures\//i.test(name) && /\.svg$/i.test(name)) { out.figures.push(a.path); continue; }
    const g = /^(.+?)\.mesh\.json$/i.exec(name);
    if (g) out.meshes.set(g[1], a.path);
  }
  return out;
}

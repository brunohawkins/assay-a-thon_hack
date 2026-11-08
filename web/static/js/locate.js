// Finding a run's files.
//
// web/README.md gives /api/runs/<id>/artifacts and /file?path=, but not the exact path
// strings a stage reports, so nothing here hard-codes one. We ask the artifact list
// first, then fall back to the conventional locations from assay2chip/README.md.

import { api, ApiError } from './api.js';
import { normaliseArtifacts } from './model.js';

export async function loadArtifacts(runId) {
  try {
    return normaliseArtifacts(await api.artifacts(runId));
  } catch (err) {
    if (err instanceof ApiError && (err.notReady || err.missing)) return [];
    throw err;
  }
}

const basename = (p) => String(p).split('/').pop();

/** Every artifact whose file name matches. */
export function byName(artifacts, name) {
  return artifacts.filter((a) => basename(a.path) === name);
}

/** Every artifact whose path matches a regular expression. */
export function byPattern(artifacts, re) {
  return artifacts.filter((a) => re.test(a.path));
}

/**
 * Candidate paths for one of the pipeline's named stage files.
 * The artifact list wins, then the conventional work-dir path. A bare file name is last:
 * /file takes repo-relative paths and answers 403 for anything else, so trying the bare
 * name first just puts a pointless rejection in the network log.
 */
export function candidates(runId, name, artifacts = []) {
  const hits = byName(artifacts, name).map((a) => a.path);
  return [...new Set([...hits, `assay2chip/work/${runId}/${name}`, name])];
}

/**
 * Load the first candidate that exists.
 * Returns {data, path} or {missing:true, tried:[...], error} - never throws for a 404,
 * because a stage that has not run yet is a normal state, not an error.
 */
export async function loadStageJson(runId, name, artifacts = [], extra = []) {
  const tried = [...candidates(runId, name, artifacts), ...extra];
  let lastErr = null;
  for (const path of tried) {
    try {
      const data = await api.fileJson(runId, path);
      return { data, path };
    } catch (err) {
      lastErr = err;
      if (err instanceof ApiError && err.looksOffline) return { missing: true, tried, error: err };
      continue; // 404 and 403 both just mean "not this path"
    }
  }
  return { missing: true, tried, error: lastErr };
}

/** Load several named stage files at once. */
export async function loadStageJsons(runId, names, artifacts = []) {
  const out = {};
  await Promise.all(names.map(async (name) => {
    out[name] = await loadStageJson(runId, name, artifacts);
  }));
  return out;
}

/** Group cad/exports artifacts by chip id. */
export function exportsByChip(artifacts) {
  const map = new Map();
  for (const a of artifacts) {
    const m = /(?:^|\/)cad\/exports\/([^/]+)\/(.+)$/.exec(a.path);
    if (!m) continue;
    if (!map.has(m[1])) map.set(m[1], []);
    map.get(m[1]).push(a);
  }
  return map;
}

/** Figures for one chip from cad/figures (layout, features, stack) plus S11 renders. */
export function figuresFor(artifacts, chipId) {
  if (!chipId) return [];
  const safe = String(chipId).replace(/[^\w.-]/g, '');
  const re = new RegExp(`${safe}[^/]*\\.(svg|png|jpe?g)$`, 'i');
  return artifacts.filter((a) => re.test(a.path) && /(figure|render|export|print|cad)/i.test(a.path));
}

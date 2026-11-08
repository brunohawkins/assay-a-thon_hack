// Honesty labels. The repo's rule is that a number which reads as fact but is not one is
// the failure mode to avoid, so these are deliberately hard to leave off: every helper
// that renders a simulated quantity attaches the label itself.

import { el, badge, fmtNum } from './dom.js';

export const SIM_TITLE =
  'Simulated: a depth-averaged Hele-Shaw network solve, not a measurement from a physical chip.';
export const DEMO_TITLE =
  'Demo scale: 30 x 20 mm fabrication demo, not the ANSI/SLAS 127.76 x 85.48 mm design target.';
export const STUB_TITLE =
  'LLM stub: extraction ran offline with the deterministic fallback, not a language model.';
export const TODO_TITLE =
  'Not yet implemented: this stage of the pipeline does not exist yet, so there is no output.';

export const simBadge = () => badge('SIMULATED', 'sim', SIM_TITLE);
export const demoBadge = () => badge('DEMO SCALE', 'demo', DEMO_TITLE);
export const stubBadge = () => badge('LLM STUB', 'stub', STUB_TITLE);
export const todoBadge = () => badge('NOT YET IMPLEMENTED', 'todo', TODO_TITLE);

/**
 * A simulated quantity: value, unit and the SIMULATED badge, as one inline group.
 * Use this for anything the CFD solver produced.
 */
export function simNum(value, units, { sig = 3, label = true } = {}) {
  const shown = value === null || value === undefined || value === ''
    ? '-'
    : (typeof value === 'number' ? fmtNum(value, sig) : String(value));
  return el('span', { class: 'sim-num' },
    el('span', { class: 'num' }, units ? `${shown} ${units}` : shown),
    label && shown !== '-' ? simBadge() : null);
}

/** DEMO SCALE when the footprint is the 30 x 20 mm demo envelope or scale says so. */
export function isDemoScale(chipLike) {
  if (!chipLike || typeof chipLike !== 'object') return false;
  const scale = String(chipLike.scale || '').toLowerCase();
  if (scale === 'demo') return true;
  if (scale === 'target') return false;
  const fp = chipLike.footprint || chipLike.envelope;
  const x = Number(fp && (fp.x ?? fp[0] ?? fp.x_mm));
  const y = Number(fp && (fp.y ?? fp[1] ?? fp.y_mm));
  if (!Number.isFinite(x) || !Number.isFinite(y)) return false;
  return x <= 40 && y <= 30;
}

/** Footprint as "30 x 20 mm", plus DEMO SCALE when it applies. */
export function footprintLabel(chipLike) {
  const fp = (chipLike && (chipLike.footprint || chipLike.envelope)) || null;
  const x = fp ? Number(fp.x ?? fp[0] ?? fp.x_mm) : NaN;
  const y = fp ? Number(fp.y ?? fp[1] ?? fp.y_mm) : NaN;
  const txt = Number.isFinite(x) && Number.isFinite(y) ? `${fmtNum(x, 5)} x ${fmtNum(y, 5)} mm` : 'footprint unknown';
  return el('span', { class: 'row', style: { gap: '6px' } },
    el('span', { class: 'num' }, txt),
    isDemoScale(chipLike) ? demoBadge() : null);
}

/** PLACEHOLDER / DRAFT flags that the repo's own chip specs carry. */
export function specFlags(spec) {
  const out = [];
  if (!spec || typeof spec !== 'object') return out;
  if (spec.placeholder) out.push(badge('PLACEHOLDER DATA', 'warn', 'The spec marks itself placeholder.'));
  if (spec.translated_from) out.push(badge('TRANSLATED', 'info', `Translated from ${String(spec.translated_from)}`));
  const status = String(spec.status || '').toLowerCase();
  if (status === 'draft') out.push(badge('DRAFT', 'warn'));
  if (status === 'superseded') out.push(badge('SUPERSEDED', 'todo'));
  return out;
}

export const SEVERITIES = ['blocker', 'high', 'medium', 'low'];

export function severityBadge(sev) {
  const s = SEVERITIES.includes(String(sev)) ? String(sev) : 'low';
  return el('span', { class: `sev sev-${s}` }, s);
}

export const STATUS_WORDS = {
  pending: 'pending',
  queued: 'queued',
  running: 'running',
  done: 'done',
  failed: 'failed',
  skipped: 'skipped',
  not_implemented: 'not yet implemented',
};

export function statusBadge(status) {
  const s = String(status || 'pending');
  if (s === 'done') return badge('done', 'ok');
  if (s === 'running') return badge('running', 'live');
  if (s === 'failed') return badge('failed', 'bad');
  if (s === 'unknown') return badge('not recorded', '', 'No stage record for this run.');
  if (s === 'queued') return badge('queued', 'info', 'Waiting its turn: one run at a time, because S5-S11 write into the shared chip/ and protocols/ trees.');
  if (s === 'skipped') return badge('skipped');
  if (s === 'not_implemented') return todoBadge();
  return badge('pending');
}

// Shell: health gate, nav, hash routing, the run picker, and the global honesty badges.

import { api, ApiError, explain, apiBase, setApiBase } from './api.js';
import { el, clear, setChildren, badge, toast, errorState, fmtTime } from './dom.js';
import { normaliseStages, normaliseRunList, FALLBACK_STAGES } from './model.js';
import { stubBadge, STUB_TITLE } from './labels.js';
import { keepOnly } from './live.js';

import * as newRun from './views/newrun.js';
import * as assayView from './views/assay.js';
import * as runView from './views/run.js';
import * as stepsView from './views/steps.js';
import * as chipsView from './views/chips.js';
import * as cfdView from './views/cfd.js';
import * as reportView from './views/report.js';

const VIEWS = {
  // Two jobs: run the general pipeline on any assay, and present THIS project's assay.
  assay: { label: 'Our assay', mod: assayView, needsRun: false },
  new: { label: 'New run', mod: newRun, needsRun: false },
  run: { label: 'Run', mod: runView, needsRun: true },
  steps: { label: 'Steps', mod: stepsView, needsRun: true },
  chips: { label: 'Chips', mod: chipsView, needsRun: false },
  cfd: { label: 'CFD', mod: cfdView, needsRun: true },
  report: { label: 'Report', mod: reportView, needsRun: true },
};

export const state = {
  health: null,
  healthError: null,
  stages: FALLBACK_STAGES,
  runs: [],
  runId: null,
  view: 'new',
  param: null,
};

const nodes = {
  nav: document.getElementById('nav'),
  main: document.getElementById('main'),
  banner: document.getElementById('banner'),
  badges: document.getElementById('badges'),
  select: document.getElementById('run-select'),
};

let unmount = null;
let healthTimer = 0;

/* ------------------------------------------------------------------ routing */

export function navigate(view, param) {
  const hash = param ? `#/${view}/${encodeURIComponent(param)}` : `#/${view}`;
  if (location.hash === hash) render();
  else location.hash = hash;
}

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [view, ...rest] = raw.split('/');
  const param = rest.length ? decodeURIComponent(rest.join('/')) : null;
  return { view: VIEWS[view] ? view : 'new', param };
}

/** The context every view receives. */
function context() {
  return {
    state,
    navigate,
    refreshRuns,
    selectRun,
    runId: state.runId,
    param: state.param,
    // /file is only ever reached through a run, but chip/, protocols/ and cad/figures/
    // are allowlisted for EVERY run - so any run id opens them. cad/exports/<chip> still
    // needs the run that built that chip.
    fileRunId: state.runId || (state.runs[0] && state.runs[0].run_id) || null,
  };
}

/* ------------------------------------------------------------------ chrome */

function renderNav() {
  const nav = clear(nodes.nav);
  for (const [key, v] of Object.entries(VIEWS)) {
    const disabled = v.needsRun && !state.runId;
    const b = el('button', {
      type: 'button',
      disabled: disabled || null,
      title: disabled ? 'Pick or start a run first' : null,
      onclick: () => navigate(key, v.needsRun ? state.runId : null),
    }, v.label);
    if (state.view === key) b.setAttribute('aria-current', 'page');
    nav.appendChild(b);
  }
}

function renderBadges() {
  const host = clear(nodes.badges);
  const h = state.health;
  if (!h) {
    host.appendChild(badge('backend unknown', 'bad', state.healthError ? explain(state.healthError) : ''));
    return;
  }
  host.appendChild(badge('backend up', 'ok', h.version ? `version ${h.version}` : ''));
  if (String(h.llm) === 'stub') host.appendChild(stubBadge());
  else if (String(h.llm) === 'real') host.appendChild(badge('LLM live', 'info', 'Extraction can call a language model.'));
  if (h.version) host.appendChild(badge(String(h.version), '', 'backend version'));
}

function renderRunSelect() {
  const sel = clear(nodes.select);
  sel.appendChild(el('option', { value: '' }, state.runs.length ? 'select a run...' : 'no runs yet'));
  for (const r of state.runs) {
    const bits = [r.run_id];
    if (r.title && r.title !== r.run_id) bits.push(`- ${r.title}`);
    if (r.status && r.status !== 'done') bits.push(`(${r.status})`);
    sel.appendChild(el('option', { value: r.run_id }, bits.join(' ').slice(0, 110)));
  }
  sel.value = state.runId || '';
  sel.disabled = !state.runs.length;
}

nodes.select.addEventListener('change', (e) => {
  const id = e.target.value;
  if (!id) { state.runId = null; renderNav(); return; }
  selectRun(id, VIEWS[state.view] && VIEWS[state.view].needsRun ? state.view : 'run');
});

export function selectRun(id, view = 'run') {
  state.runId = id;
  keepOnly(id);
  renderRunSelect();
  navigate(view, id);
}

function showBanner(kind, ...children) {
  const b = nodes.banner;
  setChildren(b, ...children);
  b.className = `banner ${kind}`;
  b.hidden = false;
}

function hideBanner() { nodes.banner.hidden = true; }

/* ------------------------------------------------------------------ health */

async function checkHealth() {
  try {
    const h = await api.health();
    state.health = h && typeof h === 'object' ? h : {};
    state.healthError = null;
    if (Array.isArray(state.health.stages) && state.health.stages.length) {
      state.stages = normaliseStages(state.health.stages);
    }
    hideBanner();
    stopHealthPolling();
    return true;
  } catch (err) {
    state.health = null;
    state.healthError = err;
    renderOfflineBanner(err);
    startHealthPolling();
    return false;
  }
}

function renderOfflineBanner(err) {
  const offline = err instanceof ApiError && err.looksOffline;
  const base = apiBase();
  showBanner('bad',
    el('strong', offline ? 'Backend not running.' : 'Backend error.'),
    offline
      ? el('span', {}, 'Start it with ', el('code', 'python web/server.py'), ' (it listens on 127.0.0.1:8765). ')
      : el('span', {}, `${explain(err)} `),
    base ? el('span', { class: 'small' }, `Asking ${base}. `) : null,
    el('button', { type: 'button', class: 'ghost', style: { marginLeft: '8px' }, onclick: () => boot() }, 'Retry'),
    el('button', {
      type: 'button', class: 'ghost', style: { marginLeft: '6px' },
      onclick: () => {
        const next = prompt('Backend base URL (blank = same origin as this page)', base);
        if (next === null) return;
        setApiBase(next.trim());
        boot();
      },
    }, 'Change backend URL'));
}

function startHealthPolling() {
  if (healthTimer) return;
  healthTimer = setInterval(async () => {
    const ok = await checkHealth();
    if (ok) { renderBadges(); await refreshRuns(); render(); }
  }, 5000);
}

function stopHealthPolling() {
  if (healthTimer) { clearInterval(healthTimer); healthTimer = 0; }
}

/* ------------------------------------------------------------------ data */

export async function refreshRuns() {
  try {
    state.runs = normaliseRunList(await api.runs());
  } catch (err) {
    state.runs = [];
    if (!(err instanceof ApiError && (err.looksOffline || err.notReady))) console.warn('run list:', explain(err));
  }
  if (state.runId && !state.runs.some((r) => r.run_id === state.runId)) {
    // keep an id that came straight from POST /api/runs even if the list lags behind
    state.runs.unshift({ run_id: state.runId, title: state.runId, created: null, status: 'running', chips: [] });
  }
  renderRunSelect();
  return state.runs;
}

async function loadStages() {
  try {
    state.stages = normaliseStages(await api.stages());
  } catch (err) {
    state.stages = FALLBACK_STAGES;
    if (err instanceof ApiError && err.notReady) {
      console.info('stage list not implemented yet; using the list from assay2chip/README.md');
    }
  }
}

/* ------------------------------------------------------------------ render */

function offlineView(host) {
  setChildren(host, errorState('The backend is not running',
    el('p', {}, 'This page is only the front end. Everything it shows - runs, chips, CFD findings - comes from the local pipeline server.'),
    el('h4', 'Start it'),
    el('pre', { class: 'code' }, 'python web/server.py\n# then open http://127.0.0.1:8765'),
    el('p', { class: 'small muted' }, state.healthError ? explain(state.healthError) : ''),
    el('p', { class: 'small muted' }, 'Nothing is loaded from the network: no CDN, no web font, no analytics. If the server is on another port, use the "Change backend URL" button above.')));
}

function render() {
  const { view, param } = parseHash();
  state.view = view;
  state.param = param;

  const def = VIEWS[view];
  if (param && def.needsRun) state.runId = param;
  if (def.needsRun && !state.runId) {
    // nothing to show: fall back to the new-run view
    state.view = 'new';
  }
  renderNav();
  renderRunSelect();

  if (unmount) { try { unmount(); } catch (e) { console.error(e); } unmount = null; }
  const host = clear(nodes.main);

  if (!state.health) { offlineView(host); return; }

  const active = VIEWS[state.view];
  try {
    const res = active.mod.mount(host, context());
    if (typeof res === 'function') unmount = res;
    else if (res && typeof res.then === 'function') res.then((u) => { if (typeof u === 'function') unmount = u; }).catch(viewFailed);
  } catch (err) {
    viewFailed(err);
  }

  function viewFailed(err) {
    console.error(err);
    setChildren(host, errorState('This view failed to render',
      el('p', explain(err)),
      el('p', { class: 'small muted' }, 'The backend is being built in parallel, so a view can arrive before its data does. The console has the details.')));
  }
}

/* ------------------------------------------------------------------ boot */

async function boot() {
  const ok = await checkHealth();
  renderBadges();
  if (ok) {
    await Promise.all([loadStages(), refreshRuns()]);
    renderBadges();
  }
  if (!state.runId) {
    const { param, view } = parseHash();
    if (param && VIEWS[view] && VIEWS[view].needsRun) state.runId = param;
    else if (state.runs.length) state.runId = state.runs[0].run_id;
  }
  render();
}

addEventListener('hashchange', render);

addEventListener('error', (e) => {
  if (e.message) console.error('uncaught:', e.message);
});

window.addEventListener('unhandledrejection', (e) => {
  const err = e.reason;
  if (err instanceof ApiError && (err.looksOffline || err.notReady)) {
    toast(explain(err), true);
    e.preventDefault();
  }
});

boot();

export { fmtTime };

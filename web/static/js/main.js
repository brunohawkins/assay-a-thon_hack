// Shell: health gate, the three sections, hash routing and the global status pill.
//
// Three views: #/assay (landing), #/run[/<id>] (start a run, watch it), #/results/<id>.
// The old seven routes still resolve, into the section that now holds them.

import { api, ApiError, explain, apiBase, setApiBase } from './api.js';
import { el, clear, setChildren, badge, toast, errorState, fmtTime } from './dom.js';
import { normaliseStages, normaliseRunList, FALLBACK_STAGES } from './model.js';
import { stubBadge } from './labels.js';
import { keepOnly } from './live.js';

import * as homeView from './views/home.js';
import * as runItView from './views/runit.js';
import * as resultsView from './views/results.js';

const VIEWS = {
  assay: { label: 'The assay', mod: homeView, sub: 'what it is' },
  run: { label: 'Run it', mod: runItView, sub: 'start or watch' },
  results: { label: 'Results', mod: resultsView, sub: 'what it found' },
};

/** Old routes, and the section that now holds what they showed. */
const LEGACY = { new: 'run', steps: 'results', chips: 'results', cfd: 'results', report: 'results' };

export const state = {
  health: null,
  healthError: null,
  stages: FALLBACK_STAGES,
  runs: [],
  runId: null,
  view: 'assay',
  param: null,
};

const nodes = {
  nav: document.getElementById('nav'),
  main: document.getElementById('main'),
  banner: document.getElementById('banner'),
  status: document.getElementById('status'),
};

let unmount = null;
let healthTimer = 0;

/* ------------------------------------------------------------------ routing */

function resolve(view, param) {
  if (VIEWS[view]) return { view, param };
  if (LEGACY[view]) {
    // #/chips/<chip> carried a chip id, not a run id
    const p = view === 'chips' ? state.runId : param;
    return { view: LEGACY[view], param: p || state.runId };
  }
  return { view: 'assay', param: null };
}

export function navigate(view, param) {
  const r = resolve(view, param);
  const hash = r.param ? `#/${r.view}/${encodeURIComponent(r.param)}` : `#/${r.view}`;
  if (location.hash === hash) render();
  else location.hash = hash;
}

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  const [view, ...rest] = raw.split('/');
  const param = rest.length ? decodeURIComponent(rest.join('/')) : null;
  return resolve(view || 'assay', param);
}

function context() {
  return {
    state,
    navigate,
    refreshRuns,
    selectRun,
    runId: state.runId,
    param: state.param,
    // chip/, protocols/ and cad/figures/ are readable through ANY run's /file route.
    fileRunId: state.runId || (state.runs[0] && state.runs[0].run_id) || null,
  };
}

export function selectRun(id, view = 'run') {
  state.runId = id;
  keepOnly(id);
  navigate(view, id);
}

/** The run most worth showing when none is chosen: one with findings or a report. */
function bestRun() {
  const has = (r) => r.findings_by_severity && Object.values(r.findings_by_severity).some((n) => Number(n) > 0);
  return (state.runs.find((r) => r.status === 'done' && has(r)) || state.runs.find(has)
    || state.runs.find((r) => r.status === 'done') || state.runs[0] || {}).run_id || null;
}

/* ------------------------------------------------------------------ chrome */

function renderNav() {
  const nav = clear(nodes.nav);
  Object.entries(VIEWS).forEach(([key, v], i) => {
    const param = key === 'results' ? (state.runId || bestRun()) : null;
    const href = param ? `#/${key}/${encodeURIComponent(param)}` : `#/${key}`;
    const a = el('a', { href, class: 'nav-item' },
      el('span', { class: 'nav-n', 'aria-hidden': 'true' }, String(i + 1).padStart(2, '0')),
      el('span', { class: 'nav-l' }, v.label));
    if (state.view === key) a.setAttribute('aria-current', 'page');
    nav.appendChild(a);
  });
}

function renderStatus() {
  const host = clear(nodes.status);
  const h = state.health;
  if (!h) {
    host.appendChild(badge('server not reachable', 'bad', state.healthError ? explain(state.healthError) : ''));
    return;
  }
  host.appendChild(el('span', { class: 'live-dot', title: h.version ? `local server, version ${h.version}` : 'local server' }, 'server up'));
  if (String(h.llm) === 'stub') host.appendChild(stubBadge());
  else if (String(h.llm) === 'real') host.appendChild(badge('LLM live', 'info', 'Protocols are read by a language model.'));
}

function showBanner(kind, ...children) {
  setChildren(nodes.banner, ...children);
  nodes.banner.className = `banner ${kind}`;
  nodes.banner.hidden = false;
}

function hideBanner() { nodes.banner.hidden = true; }

/* ------------------------------------------------------------------ health */

async function checkHealth() {
  try {
    const h = await api.health();
    state.health = h && typeof h === 'object' ? h : {};
    state.healthError = null;
    if (Array.isArray(state.health.stages) && state.health.stages.length) state.stages = normaliseStages(state.health.stages);
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
    el('strong', {}, offline ? 'The local server is not running.' : 'The local server answered with an error.'),
    el('span', {}, offline ? ' Retrying every few seconds. ' : ` ${explain(err)} `),
    el('button', { type: 'button', class: 'ghost', onclick: () => boot() }, 'Retry now'),
    el('button', {
      type: 'button', class: 'ghost',
      onclick: () => {
        const next = prompt('Server address (blank = this page\'s own address)', base);
        if (next === null) return;
        setApiBase(next.trim());
        boot();
      },
    }, 'Change server address'));
}

function startHealthPolling() {
  if (healthTimer) return;
  healthTimer = setInterval(async () => {
    if (await checkHealth()) { await Promise.all([loadStages(), refreshRuns()]); renderStatus(); render(); }
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
    state.runs.unshift({ run_id: state.runId, title: state.runId, created: null, status: 'running', chips: [] });
  }
  return state.runs;
}

async function loadStages() {
  try { state.stages = normaliseStages(await api.stages()); } catch (_) { /* keep the health list or the fallback */ }
}

/* ------------------------------------------------------------------ render */

function offlineView(host) {
  setChildren(host,
    el('section', { class: 'hero' },
      el('p', { class: 'eyebrow' }, 'Not connected'),
      el('h1', { class: 'display' }, 'The local server is not running, so there is nothing to show yet.'),
      el('p', { class: 'lede' }, 'This page is only the front end. The assay description, the runs, the chips and the simulation results all come from a small server on this computer.')),
    el('section', { class: 'section' },
      el('header', { class: 'sec-head' }, el('h2', {}, 'Start it'), el('p', { class: 'sub' }, 'From the repository folder, in a terminal:')),
      el('pre', { class: 'code' }, 'python web/server.py\n# then open http://127.0.0.1:8765'),
      state.healthError && !(state.healthError instanceof ApiError && state.healthError.looksOffline)
        ? el('p', { class: 'cap' }, explain(state.healthError)) : null,
      el('p', { class: 'cap' }, 'This page will connect by itself as soon as the server answers.')));
}

function render() {
  const { view, param } = parseHash();
  state.view = view;
  state.param = param;
  if (view === 'results') {
    state.runId = param || state.runId || bestRun();
    state.param = state.runId;
  } else if (view === 'run' && param) {
    state.runId = param;
  }
  if (unmount) { try { unmount(); } catch (e) { console.error(e); } unmount = null; }
  if (state.runId) keepOnly(state.runId);
  renderNav();
  document.title = `${VIEWS[view].label} · Capsule processor`;

  const host = clear(nodes.main);
  host.dataset.view = view;

  if (!state.health) { offlineView(host); return; }

  try {
    const res = VIEWS[view].mod.mount(host, context());
    if (typeof res === 'function') unmount = res;
    else if (res && typeof res.then === 'function') res.then((u) => { if (typeof u === 'function') unmount = u; }).catch(failed);
  } catch (err) { failed(err); }
  window.scrollTo(0, 0);

  function failed(err) {
    console.error(err);
    setChildren(host, errorState('This page failed to draw', el('p', {}, explain(err)),
      el('p', { class: 'small' }, 'The details are in the browser console.')));
  }
}

/* ------------------------------------------------------------------ boot */

async function boot() {
  const ok = await checkHealth();
  if (ok) await Promise.all([loadStages(), refreshRuns()]);
  renderStatus();
  render();
}

addEventListener('hashchange', render);

window.addEventListener('unhandledrejection', (e) => {
  const err = e.reason;
  if (err instanceof ApiError && (err.looksOffline || err.notReady)) {
    toast(explain(err), true);
    e.preventDefault();
  }
});

boot();

export { fmtTime };

// The API client. Shapes follow web/README.md, which is authoritative.
//
// Every call can fail in three interesting ways and the UI distinguishes them:
//   - offline:   the backend is not running at all (fetch rejects, or we got HTML back)
//   - notReady:  503, the backend is up but this piece is not built yet
//   - missing:   404, this run/chip/file does not exist

const DEFAULT_BASE = '';

/** Dev convenience: ?api=http://127.0.0.1:8765 when the static files are served separately. */
function initialBase() {
  try {
    const q = new URLSearchParams(location.search).get('api');
    if (q) { sessionStorage.setItem('a2c.api', q); return q.replace(/\/$/, ''); }
    const saved = sessionStorage.getItem('a2c.api');
    if (saved) return saved.replace(/\/$/, '');
  } catch (_) { /* private mode */ }
  return DEFAULT_BASE;
}

let base = initialBase();

export function apiBase() { return base; }
export function setApiBase(b) {
  base = (b || '').replace(/\/$/, '');
  try { b ? sessionStorage.setItem('a2c.api', base) : sessionStorage.removeItem('a2c.api'); } catch (_) {}
}

export class ApiError extends Error {
  constructor(message, { status = 0, detail = '', offline = false, nonJson = false, path = '' } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.offline = offline;
    // A real backend answers errors as {"error","detail"} JSON with a real status
    // (web/README.md). A non-JSON error body on an /api/ path means something else is
    // serving this origin - typically `python -m http.server` with no backend at all.
    this.nonJson = nonJson;
    this.path = path;
  }
  get notReady() { return this.status === 503 || this.status === 501; }
  get missing() { return this.status === 404 && !this.nonJson; }
  /** True when nothing is answering the API, however it failed. */
  get looksOffline() { return this.offline || this.nonJson; }
}

export function url(path, query) {
  const u = `${base}${path}`;
  if (!query) return u;
  const qs = new URLSearchParams(query).toString();
  return qs ? `${u}?${qs}` : u;
}

async function parseError(res, path) {
  let detail = '';
  let message = `${res.status} ${res.statusText || ''}`.trim();
  const type = res.headers.get('content-type') || '';
  let nonJson = false;
  try {
    if (type.includes('json')) {
      const body = await res.json();
      if (body && typeof body === 'object') {
        message = String(body.error || message);
        detail = String(body.detail || '');
      }
    } else {
      nonJson = true;
      const t = (await res.text()).slice(0, 300).trim();
      if (t && !/^<!doctype|^<html/i.test(t)) detail = t;
      else detail = 'the error body was not JSON, so this origin is probably not the pipeline backend';
    }
  } catch (_) { /* keep the status line */ }
  return new ApiError(message, { status: res.status, detail, nonJson, path });
}

async function req(path, { method = 'GET', body, query, raw = false } = {}) {
  const target = url(path, query);
  let res;
  try {
    const init = { method, headers: { Accept: 'application/json' } };
    if (body instanceof FormData) init.body = body;
    else if (body !== undefined) {
      init.body = JSON.stringify(body);
      init.headers['Content-Type'] = 'application/json';
    }
    res = await fetch(target, init);
  } catch (cause) {
    throw new ApiError('cannot reach the backend', { offline: true, path, detail: String(cause && cause.message || cause) });
  }
  if (!res.ok) throw await parseError(res, path);
  if (raw) return res;
  const type = res.headers.get('content-type') || '';
  if (!type.includes('json')) {
    // A static file server answers /api/health with index.html or a 404 page; that is
    // "no backend", not "bad JSON".
    const t = await res.text();
    if (/^\s*[[{]/.test(t)) { try { return JSON.parse(t); } catch (_) {} }
    throw new ApiError('that URL is not the pipeline backend', {
      offline: true, path,
      detail: 'the response was not JSON - a plain static file server is answering /api/',
    });
  }
  return res.json();
}

export const api = {
  health: () => req('/api/health'),
  stages: () => req('/api/stages'),
  runs: () => req('/api/runs'),

  /** POST /api/runs - a File goes as multipart `file`, anything else as JSON. */
  createRun(input) {
    if (input instanceof File) {
      const fd = new FormData();
      fd.append('file', input, input.name);
      return req('/api/runs', { method: 'POST', body: fd });
    }
    if (input && input.file instanceof File) {
      const fd = new FormData();
      fd.append('file', input.file, input.file.name);
      if (input.id) fd.append('id', input.id);
      if (input.options) fd.append('options', JSON.stringify(input.options));
      return req('/api/runs', { method: 'POST', body: fd });
    }
    return req('/api/runs', { method: 'POST', body: input });
  },

  run: (id) => req(`/api/runs/${encodeURIComponent(id)}`),
  artifacts: (id) => req(`/api/runs/${encodeURIComponent(id)}/artifacts`),
  report: (id) => req(`/api/runs/${encodeURIComponent(id)}/report`),
  rerun: (id, from) => req(`/api/runs/${encodeURIComponent(id)}/rerun`, { method: 'POST', body: { from } }),

  chips: () => req('/api/chips'),
  chip: (id) => req(`/api/chips/${encodeURIComponent(id)}`),
  cfd: (body) => req('/api/cfd', { method: 'POST', body }),

  eventsUrl: (id) => url(`/api/runs/${encodeURIComponent(id)}/events`),

  /**
   * A URL for one file of a run. The README scopes /file to a run, so a run id is
   * required; repo chips browsed outside a run have no documented file endpoint
   * (see fileUrlLoose).
   */
  fileUrl: (runId, path) => url(`/api/runs/${encodeURIComponent(runId)}/file`, { path }),

  /**
   * Repo-level paths (chip/, protocols/, cad/figures/) are readable through ANY run's
   * /file route - the sandbox allowlists those trees for every run. cad/exports/<chip>/
   * is only readable through a run that built that chip. There is no repo-level file
   * route in the API, so with no run at all this returns null and the caller must say so
   * rather than render a broken link.
   */
  fileUrlLoose: (runId, path) => (runId ? api.fileUrl(runId, path) : null),

  /** Paths every run may read, per the /file allowlist in web/README.md. */
  isRepoWidePath: (path) => /^(chip|protocols|cad\/figures)\//.test(String(path)),

  /** Fetch a JSON artifact of a run. */
  async fileJson(runId, path) {
    const res = await req(`/api/runs/${encodeURIComponent(runId)}/file`, { query: { path }, raw: true });
    const t = await res.text();
    try { return JSON.parse(t); }
    catch (cause) { throw new ApiError(`${path} is not valid JSON`, { status: 200, path, detail: String(cause.message) }); }
  },

  /** Fetch a text artifact of a run. */
  async fileText(runId, path) {
    const res = await req(`/api/runs/${encodeURIComponent(runId)}/file`, { query: { path }, raw: true });
    return res.text();
  },

  /** Fetch any repo/run path as JSON, run-scoped when possible. */
  async anyJson(runId, path) {
    const target = api.fileUrlLoose(runId, path);
    if (!target) throw new ApiError('no run to read files through', { status: 0, path, detail: NO_RUN_DETAIL });
    let res;
    try { res = await fetch(target, { headers: { Accept: 'application/json' } }); }
    catch (cause) { throw new ApiError('cannot reach the backend', { offline: true, path, detail: String(cause.message) }); }
    if (!res.ok) throw await parseError(res, path);
    const t = await res.text();
    try { return JSON.parse(t); }
    catch (cause) { throw new ApiError(`${path} is not valid JSON`, { path, detail: String(cause.message) }); }
  },

  /** Fetch any repo/run path as an ArrayBuffer (STL). */
  async anyBuffer(runId, path) {
    const target = api.fileUrlLoose(runId, path);
    if (!target) throw new ApiError('no run to read files through', { status: 0, path, detail: NO_RUN_DETAIL });
    let res;
    try { res = await fetch(target); }
    catch (cause) { throw new ApiError('cannot reach the backend', { offline: true, path, detail: String(cause.message) }); }
    if (!res.ok) throw await parseError(res, path);
    return res.arrayBuffer();
  },
};

const NO_RUN_DETAIL =
  'the API serves files only through /api/runs/<id>/file, so a run has to be selected';

/** Download through fetch so a 404 shows a message instead of navigating to an error page. */
export async function download(runId, path) {
  const target = api.fileUrlLoose(runId, path);
  if (!target) throw new ApiError('no run to download through', { status: 0, path, detail: NO_RUN_DETAIL });
  const res = await fetch(target);
  if (!res.ok) throw await parseError(res, path);
  const blob = await res.blob();
  const name = String(path).split('/').pop() || 'download';
  const href = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = href;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(href), 20000);
}

/** One line of explanation for any ApiError, in plain language. */
export function explain(err) {
  if (!(err instanceof ApiError)) return String(err && err.message || err);
  if (err.looksOffline) return 'The backend is not running. Start it with: python web/server.py';
  if (err.notReady) return `Not built yet: the backend answered ${err.status} for ${err.path}.`;
  if (err.status === 403) {
    return `Outside this run's sandbox: ${err.path}. /file serves the run's own files plus `
      + 'cad/figures/, chip/, protocols/ and cad/exports/ for the chips this run built.';
  }
  if (err.missing) return `Not found: ${err.path}`;
  return `${err.message}${err.detail ? ` - ${err.detail}` : ''}`;
}

// Live run state, driven by GET /api/runs/<id>/events (Server-Sent Events).
//
// The timeline is NOT re-fetched on every event. We fetch the manifest once, then each
// `stage` event patches that one stage in place and only that row re-renders. `log`
// appends to a capped ring buffer, `finding` feeds the severity counters and the CFD
// table, `done` closes the stream and refreshes the manifest once so the final seconds
// and output lists are authoritative.
//
// EventSource retries for ever by default, which looks like a hang when the backend has
// no /events route yet. So after a few failures without a single message we stop and fall
// back to polling the manifest, and say which mode we are in.

import { api, ApiError } from './api.js';
import { normaliseManifest, normaliseFinding, normaliseStageEntry } from './model.js';

const MAX_LOG = 4000;
const MAX_SSE_FAILURES = 3;
const MAX_MANIFEST_ERRORS = 3;
const POLL_MS = 2500;

const registry = new Map();

export class RunLive {
  constructor(runId) {
    this.runId = runId;
    this.manifest = null;
    this.logs = [];
    this.findings = [];
    this.mode = 'idle';       // idle | connecting | live | polling | closed | error
    this.error = null;
    this.finished = false;
    this.listeners = new Map();
    this.refs = 0;
    this._es = null;
    this._failures = 0;
    this._gotMessage = false;
    this._poll = 0;
    this._stopped = false;
  }

  on(event, fn) {
    if (!this.listeners.has(event)) this.listeners.set(event, new Set());
    this.listeners.get(event).add(fn);
    return () => this.listeners.get(event).delete(fn);
  }

  emit(event, payload) {
    const set = this.listeners.get(event);
    if (set) for (const fn of [...set]) { try { fn(payload); } catch (e) { console.error(e); } }
    const any = this.listeners.get('*');
    if (any) for (const fn of [...any]) { try { fn({ event, payload }); } catch (e) { console.error(e); } }
  }

  setMode(mode, error = null) {
    if (this.mode === mode && this.error === error) return;
    this.mode = mode;
    this.error = error;
    this.emit('mode', { mode, error });
  }

  async start() {
    if (this._started) return this;
    this._started = true;
    await this.refreshManifest();
    if (this.finished) { this.setMode('closed'); return this; }
    this.openStream();
    return this;
  }

  async refreshManifest() {
    try {
      const raw = await api.run(this.runId);
      this._manifestErrors = 0;
      this.manifest = normaliseManifest(raw, this.runId);
      this.finished = ['done', 'failed', 'skipped'].includes(this.manifest.status)
        && ![...this.manifest.stages.values()].some((s) => s.status === 'running');
      this.emit('manifest', this.manifest);
    } catch (err) {
      this.error = err;
      this._manifestErrors = (this._manifestErrors || 0) + 1;
      // Do not sit in a loop hammering a backend that is 503-ing or gone.
      if (this._manifestErrors >= MAX_MANIFEST_ERRORS) this.stopPolling();
      this.setMode('error', err);
      this.emit('manifest', null);
    }
    return this.manifest;
  }

  openStream() {
    if (this._stopped || this._es) return;
    if (typeof EventSource === 'undefined') { this.startPolling(); return; }
    this.setMode('connecting');
    let es;
    try { es = new EventSource(api.eventsUrl(this.runId)); }
    catch (_) { this.startPolling(); return; }
    this._es = es;

    es.addEventListener('open', () => { this._failures = 0; this.setMode('live'); });

    const handler = (name) => (ev) => {
      this._gotMessage = true;
      this._failures = 0;
      this.setMode('live');
      this.handle(name, parsePayload(ev.data));
    };
    for (const name of ['stage', 'log', 'finding', 'done']) es.addEventListener(name, handler(name));
    // Some servers send unnamed `data:` lines; treat those as logs with an optional type.
    es.addEventListener('message', (ev) => {
      this._gotMessage = true;
      const p = parsePayload(ev.data);
      const t = p && typeof p === 'object' && typeof p.event === 'string' ? p.event : 'log';
      this.setMode('live');
      this.handle(t, p);
    });

    es.addEventListener('error', () => {
      if (this._stopped) return;
      this._failures += 1;
      // CLOSED means the browser gave up; CONNECTING means it is retrying by itself.
      if (es.readyState === EventSource.CLOSED || (this._failures >= MAX_SSE_FAILURES && !this._gotMessage)) {
        this.closeStream();
        this.startPolling();
      } else {
        this.setMode('connecting');
      }
    });
  }

  handle(name, payload) {
    if (name === 'stage') this.applyStage(payload);
    else if (name === 'log') this.applyLog(payload);
    else if (name === 'finding') this.applyFinding(payload);
    else if (name === 'done') this.applyDone(payload);
    else this.applyLog(payload);
  }

  applyStage(p) {
    if (!p || typeof p !== 'object') return;
    const id = String(p.stage ?? p.id ?? p.name ?? '');
    if (!id || !this.manifest) return;
    const prev = this.manifest.stages.get(id) || normaliseStageEntry(id, {});
    const merged = normaliseStageEntry(id, {
      status: p.status ?? prev.status,
      started: p.started ?? prev.started,
      seconds: p.seconds ?? (Number.isFinite(prev.seconds) ? prev.seconds : undefined),
      outputs: p.outputs ?? prev.outputs,
      error: p.error ?? null,
      note: p.note ?? prev.note,
    });
    this.manifest.stages.set(id, merged);
    this.emit('stage', merged);
  }

  applyLog(p) {
    const line = {
      t: (p && (p.t ?? p.time ?? p.ts)) ?? null,
      stage: String((p && (p.stage ?? p.id)) ?? ''),
      level: String((p && (p.level ?? p.lvl)) ?? 'info').toLowerCase(),
      text: p === null || p === undefined ? ''
        : (typeof p === 'string' ? p : String(p.message ?? p.text ?? p.line ?? p.msg ?? JSON.stringify(p))),
    };
    this.logs.push(line);
    if (this.logs.length > MAX_LOG) this.logs.splice(0, this.logs.length - MAX_LOG);
    this.emit('log', line);
  }

  applyFinding(p) {
    const f = normaliseFinding(p, this.findings.length);
    this.findings.push(f);
    this.emit('finding', f);
  }

  applyDone(p) {
    this.finished = true;
    this.closeStream();
    this.stopPolling();
    this.setMode('closed');
    this.refreshManifest().then(() => this.emit('done', p || {}));
  }

  startPolling() {
    if (this._stopped || this._poll) return;
    this.setMode('polling');
    const tick = async () => {
      if (this._stopped) return;
      await this.refreshManifest();
      if (this.finished) { this.stopPolling(); this.setMode('closed'); this.emit('done', {}); return; }
      if (this.mode !== 'error') this.setMode('polling');
    };
    this._poll = setInterval(tick, POLL_MS);
    tick();
  }

  stopPolling() {
    if (this._poll) { clearInterval(this._poll); this._poll = 0; }
  }

  closeStream() {
    if (this._es) { try { this._es.close(); } catch (_) {} this._es = null; }
  }

  stop() {
    this._stopped = true;
    this.closeStream();
    this.stopPolling();
    this.listeners.clear();
    registry.delete(this.runId);
  }
}

function parsePayload(data) {
  if (data === undefined || data === null) return null;
  const s = String(data);
  const t = s.trim();
  if (t.startsWith('{') || t.startsWith('[')) { try { return JSON.parse(t); } catch (_) {} }
  return { message: s };
}

/** One live stream per run, shared between views so switching tabs does not restart it. */
export function liveFor(runId) {
  if (!registry.has(runId)) registry.set(runId, new RunLive(runId));
  const l = registry.get(runId);
  l.start();
  return l;
}

/** Drop every stream except the run we are looking at. */
export function keepOnly(runId) {
  for (const [id, l] of [...registry]) if (id !== runId) l.stop();
}

export function modeLabel(mode) {
  if (mode === 'live') return { text: 'live', kind: 'live', title: 'Connected to the run\'s Server-Sent Events stream.' };
  if (mode === 'connecting') return { text: 'connecting', kind: 'info', title: 'Opening the event stream.' };
  if (mode === 'polling') return { text: 'polling', kind: 'warn', title: 'No event stream; re-reading the manifest every few seconds instead.' };
  if (mode === 'closed') return { text: 'finished', kind: 'ok', title: 'The run has finished and the stream is closed.' };
  if (mode === 'error') return { text: 'no updates', kind: 'bad', title: 'The backend could not be reached.' };
  return { text: mode, kind: '', title: '' };
}

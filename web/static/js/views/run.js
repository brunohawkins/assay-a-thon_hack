// View 2: Run. The stage timeline with live status and logs from the SSE stream; click a
// stage to see its output file pretty-printed.
//
// The timeline is built once. After that, each `stage` event patches one row in place and
// each `log` event appends one line - no full re-render, so a long run stays smooth and
// the scroll position is kept.

import { api, ApiError, explain, download } from '../api.js';
import { el, clear, setChildren, badge, toast, fmtSeconds, fmtTime, fmtBytes, errorState, emptyState } from '../dom.js';
import { statusBadge, todoBadge, stubBadge, demoBadge, simBadge, severityBadge, TODO_TITLE } from '../labels.js';
import { liveFor, modeLabel } from '../live.js';
import { stageOrder, normaliseArtifacts } from '../model.js';
import { renderJson } from '../jsonview.js';
import { markdownBlock } from '../markdown.js';

export function mount(host, ctx) {
  const runId = ctx.runId;
  const live = liveFor(runId);
  const offs = [];
  const rows = new Map();
  let selected = null;
  let artifacts = [];

  /* ---------------------------------------------------------------- chrome */

  const titleLine = el('span', { class: 'sub' }, runId);
  const modeBadge = el('span');
  const counters = el('div', { class: 'counters' });
  const head = el('div', { class: 'view-head' },
    el('h2', 'Run'),
    titleLine,
    el('span', { class: 'spacer' }),
    modeBadge,
    el('button', {
      type: 'button', class: 'ghost',
      onclick: () => { live.refreshManifest(); loadArtifacts(); toast('Re-read the manifest'); },
    }, 'Refresh'));

  const timeline = el('ol', { class: 'timeline', 'aria-label': 'Pipeline stages' });
  const detail = el('section', { class: 'panel', 'aria-live': 'polite' },
    el('p', { class: 'muted' }, 'Pick a stage to see what it wrote.'));

  const logPre = el('pre', { class: 'log', tabindex: '0', 'aria-label': 'Run log' });
  const logFilter = el('select', {
    'aria-label': 'Filter the log by stage',
    onchange: () => redrawLogs(),
  }, el('option', { value: '' }, 'all stages'));
  const logPanel = el('section', { class: 'panel' },
    el('div', { class: 'row' }, el('h3', { style: { margin: '0' } }, 'Log'), el('span', { class: 'spacer' }), logFilter),
    logPre,
    el('p', { class: 'hint' }, 'Streamed from /api/runs/' + runId + '/events. Newest at the bottom.'));

  const summary = el('section', { class: 'panel' });

  setChildren(host, head,
    el('div', { class: 'cols c-timeline' },
      el('div', { class: 'cols' }, el('section', { class: 'panel' }, el('h3', { style: { marginTop: 0 } }, 'Stages'), timeline), summary),
      el('div', { class: 'cols' }, detail, logPanel)));

  /* ---------------------------------------------------------------- timeline */

  function stageList() {
    const known = new Map((ctx.state.stages || []).map((s) => [s.id, s]));
    const ids = new Set([...known.keys()]);
    if (live.manifest) for (const id of live.manifest.stages.keys()) ids.add(id);
    // A CFD job has its own stage names (mesh, solve, couple, ...), so only fall back to
    // the s0..s12 list when the manifest does not name its own stages.
    const manifestIds = live.manifest ? [...live.manifest.stages.keys()] : [];
    const useManifestOnly = manifestIds.length > 0 && manifestIds.every((id) => !known.has(id));
    const list = [...(useManifestOnly ? manifestIds : ids)].map((id) => ({
      id,
      name: (known.get(id) || {}).name || '',
      description: (known.get(id) || {}).description || '',
    }));
    list.sort((a, b) => stageOrder(a.id) - stageOrder(b.id) || a.id.localeCompare(b.id));
    return list;
  }

  function buildTimeline() {
    clear(timeline);
    rows.clear();
    const list = stageList();
    if (!list.length) {
      timeline.appendChild(el('li', {}, el('p', { class: 'muted' }, 'The backend did not report any stages for this run.')));
      return;
    }
    if (unrecorded()) {
      timeline.appendChild(el('li', {}, el('p', { class: 'small muted', style: { padding: '0 10px' } },
        'The pipeline\'s own stage list, shown for reference. This run has no stage record, '
        + 'so none of these statuses were observed.')));
    }
    clear(logFilter).appendChild(el('option', { value: '' }, 'all stages'));
    for (const s of list) {
      logFilter.appendChild(el('option', { value: s.id }, s.id));
      const dot = el('span', { class: 'tl-dot', 'aria-hidden': 'true' });
      const right = el('span', { class: 'tl-right' });
      const name = el('span', { class: 'tl-name' }, s.name || s.id);
      const btn = el('button', {
        type: 'button',
        onclick: () => select(s.id),
      },
      dot,
      el('span', { class: 'tl-main' },
        el('span', { class: 'tl-id' }, s.id), ' ', name,
        s.description ? el('span', { class: 'tl-desc', style: { display: 'block' } }, s.description) : null),
      right);
      const li = el('li', { dataset: { status: 'pending', stage: s.id } }, btn);
      timeline.appendChild(li);
      rows.set(s.id, { li, btn, right });
      paintRow(s.id);
    }
    // arrow-key navigation inside the timeline
    timeline.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
      const all = [...timeline.querySelectorAll('button')];
      const i = all.indexOf(document.activeElement);
      if (i === -1) return;
      e.preventDefault();
      const next = all[Math.min(all.length - 1, Math.max(0, i + (e.key === 'ArrowDown' ? 1 : -1)))];
      next.focus();
    });
  }

  function paintRow(id) {
    const row = rows.get(id);
    if (!row) return;
    const entry = live.manifest && live.manifest.stages.get(id);
    const status = entry ? entry.status : 'pending';
    row.li.dataset.status = status;
    const right = clear(row.right);
    right.appendChild(statusBadge(status));
    if (entry && Number.isFinite(entry.seconds)) right.appendChild(el('span', { class: 'tl-secs' }, fmtSeconds(entry.seconds)));
    if (entry && entry.outputs.length) {
      right.appendChild(el('span', { class: 'tl-secs' }, `${entry.outputs.length} file${entry.outputs.length === 1 ? '' : 's'}`));
    }
    row.btn.setAttribute('aria-current', String(selected === id));
    row.btn.setAttribute('aria-label',
      `${id} ${entry && entry.status === 'not_implemented' ? 'not yet implemented' : status}`);
  }

  /* ---------------------------------------------------------------- summary */

  function paintSummary() {
    const m = live.manifest;
    const body = clear(summary);
    body.appendChild(el('h3', { style: { marginTop: 0 } }, 'This run'));
    if (!m) {
      body.appendChild(el('p', { class: 'muted' }, live.error ? explain(live.error) : 'Loading the manifest...'));
      return;
    }
    setChildren(titleLine, m.title && m.title !== runId ? `${runId} - ${m.title}` : runId);

    const flags = el('div', { class: 'row', style: { gap: '6px', marginBottom: '10px' } }, statusBadge(m.status));
    if (String(m.llm) === 'stub') flags.appendChild(stubBadge());
    if (String(m.scale) === 'demo') flags.appendChild(demoBadge());
    if (m.kind === 'cfd' || /cfd/i.test(String(m.title))) flags.appendChild(simBadge());
    body.appendChild(flags);

    const kv = el('dl', { class: 'kv' });
    const add = (k, v) => { if (v !== null && v !== undefined && v !== '') { kv.appendChild(el('dt', k)); kv.appendChild(el('dd', {}, v)); } };
    add('run id', runId);
    add('created', fmtTime(m.created));
    if (m.source) {
      add('source', String(m.source.kind || ''));
      add('ref', String(m.source.ref || ''));
      if (m.source.sha256) add('sha256', String(m.source.sha256).slice(0, 16) + '...');
    }
    add('scale', m.scale || null);
    add('llm', m.llm || null);
    if (m.chips.length) add('chips', m.chips.map((c) => (typeof c === 'string' ? c : c.chip_id || c.id)).join(', '));
    body.appendChild(kv);

    // Without a run_manifest.json every stage reads "pending", which would be a lie
    // about a run that plainly produced files. A run that has not started yet has no
    // manifest for a perfectly good reason, so that case is left alone.
    if (unrecorded()) {
      body.appendChild(el('p', { class: 'small', style: { marginTop: '10px', color: '#fde68a' } },
        'No stage record for this run: it was not started through this server, so the timeline cannot say '
        + 'which stages ran. Its files are still listed below and in the other views.'));
    }

    // A queued run shows thirteen pending stages and nothing else; say why.
    if (m.status === 'queued' || m.message) {
      body.appendChild(el('p', {
        class: 'small',
        style: { marginTop: '10px', color: m.status === 'queued' ? '#7dd3fc' : 'var(--muted)' },
      },
      m.message || (Number.isFinite(m.queued_behind) && m.queued_behind
        ? `Queued behind ${m.queued_behind} job${m.queued_behind === 1 ? '' : 's'}.`
        : 'Queued.')));
    }

    if (m.missing_contract_keys && m.missing_contract_keys.length) {
      body.appendChild(el('div', { class: 'unresolved', style: { marginTop: '10px' } },
        el('h3', `${m.missing_contract_keys.length} contract values this run needed and did not find`),
        el('ul', { class: 'small', style: { margin: 0, paddingLeft: '18px' } },
          ...m.missing_contract_keys.slice(0, 20).map((k) => el('li', { class: 'mono' }, k)))));
    }

    if (m.error) {
      body.appendChild(el('div', { class: 'err', style: { marginTop: '10px' } },
        el('h3', 'The run reported an error'), el('p', { class: 'mono' }, String(m.error))));
    }

    paintCounters();
    body.appendChild(counters);

    const links = el('div', { class: 'row', style: { marginTop: '12px' } },
      el('button', { type: 'button', onclick: () => ctx.navigate('steps', runId) }, 'Steps'),
      el('button', { type: 'button', onclick: () => ctx.navigate('cfd', runId) }, 'CFD'),
      el('button', { type: 'button', onclick: () => ctx.navigate('report', runId) }, 'Report'),
      el('button', { type: 'button', onclick: () => ctx.navigate('chips', null) }, 'Chips'));
    body.appendChild(links);

    const failed = [...m.stages.values()].find((s) => s.status === 'failed');
    if (failed) {
      body.appendChild(el('p', { style: { marginTop: '10px' } },
        el('button', {
          type: 'button',
          onclick: () => rerun(failed.id),
        }, `Re-run from ${failed.id}`)));
    }
  }

  function paintCounters() {
    const m = live.manifest;
    const sev = (m && m.findings_by_severity) || null;
    const counts = { blocker: 0, high: 0, medium: 0, low: 0 };
    for (const f of live.findings) if (counts[f.severity] !== undefined) counts[f.severity] += 1;
    if (sev) for (const k of Object.keys(counts)) counts[k] = Math.max(counts[k], Number(sev[k]) || 0);
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    const body = clear(counters);
    if (!total) return;
    body.appendChild(el('span', { class: 'small muted', style: { width: '100%' } }, 'CFD findings so far'));
    for (const k of ['blocker', 'high', 'medium', 'low']) {
      if (!counts[k]) continue;
      body.appendChild(el('button', {
        type: 'button', class: 'ct',
        onclick: () => ctx.navigate('cfd', runId),
        title: `Show the ${k} findings`,
      }, el('b', String(counts[k])), severityBadge(k)));
    }
  }

  async function rerun(from) {
    try {
      const res = await api.rerun(runId, from);
      const id = res && (res.run_id || res.id);
      toast(id && id !== runId ? `Re-run started as ${id}` : `Re-running from ${from}`);
      if (id && id !== runId) { await ctx.refreshRuns(); ctx.selectRun(id, 'run'); }
      else live.refreshManifest();
    } catch (err) { toast(explain(err), true); }
  }

  /* ---------------------------------------------------------------- detail */

  async function select(id) {
    const prev = selected;
    selected = id;
    if (prev) paintRow(prev);
    paintRow(id);
    const entry = live.manifest && live.manifest.stages.get(id);
    const known = (ctx.state.stages || []).find((s) => s.id === id) || {};
    const body = clear(detail);
    body.appendChild(el('div', { class: 'row' },
      el('h3', { style: { margin: 0 } }, `${id}${known.name ? ` ${known.name}` : ''}`),
      el('span', { class: 'spacer' }),
      statusBadge(entry ? entry.status : 'pending')));
    if (known.description) body.appendChild(el('p', { class: 'muted small' }, known.description));

    if (!entry) {
      body.appendChild(el('p', { class: 'muted' }, 'The manifest has no entry for this stage yet.'));
      return;
    }
    if (entry.status === 'not_implemented') {
      body.appendChild(el('p', {}, todoBadge()));
      body.appendChild(el('p', { class: 'muted' }, TODO_TITLE + ' Nothing was written, so there is no file to show.'));
      return;
    }
    if (entry.error) {
      body.appendChild(el('div', { class: 'err' }, el('h3', 'Stage error'), el('p', { class: 'mono' }, String(entry.error))));
    }
    const meta = el('dl', { class: 'kv' });
    if (entry.started) { meta.appendChild(el('dt', 'started')); meta.appendChild(el('dd', fmtTime(entry.started))); }
    if (Number.isFinite(entry.seconds)) { meta.appendChild(el('dt', 'took')); meta.appendChild(el('dd', fmtSeconds(entry.seconds))); }
    if (meta.childElementCount) body.appendChild(meta);

    const paths = entry.outputs.map((o) => o.path);
    const extra = artifacts.filter((a) => a.stage === id && !paths.includes(a.path)).map((a) => a.path);
    const all = [...paths, ...extra];
    if (!all.length) {
      body.appendChild(el('p', { class: 'muted' }, entry.status === 'pending'
        ? 'Not run yet.'
        : 'This stage reported no output files.'));
      return;
    }

    const fileHost = el('div', { style: { marginTop: '10px' } });
    const picker = el('div', { class: 'row', style: { marginBottom: '8px' } });
    all.forEach((p, i) => {
      const b = el('button', {
        type: 'button', class: 'sel', 'aria-pressed': String(i === 0),
        onclick: () => {
          for (const other of picker.querySelectorAll('button')) other.setAttribute('aria-pressed', 'false');
          b.setAttribute('aria-pressed', 'true');
          showFile(fileHost, p);
        },
      }, shortPath(p));
      picker.appendChild(b);
    });
    body.appendChild(picker);
    body.appendChild(fileHost);
    showFile(fileHost, all[0]);
  }

  async function showFile(hostEl, path) {
    setChildren(hostEl, el('p', { class: 'loading' }, `Loading ${shortPath(path)}`));
    const ext = (path.split('.').pop() || '').toLowerCase();
    const bar = el('div', { class: 'row', style: { marginBottom: '8px' } },
      el('code', { class: 'small' }, path),
      el('span', { class: 'spacer' }),
      el('button', {
        type: 'button', class: 'ghost',
        onclick: async () => {
          try { await download(runId, path); }
          catch (err) { toast(explain(err), true); }
        },
      }, 'Download'));

    try {
      if (ext === 'svg' || ext === 'png' || ext === 'jpg' || ext === 'jpeg') {
        setChildren(hostEl, bar, el('div', { class: 'figs' },
          el('figure', {},
            el('img', { src: api.fileUrl(runId, path), alt: `figure from ${path}`, loading: 'lazy' }),
            el('figcaption', path))));
        return;
      }
      if (ext === 'json') {
        const data = await api.fileJson(runId, path);
        const view = el('div');
        renderJson(view, data);
        setChildren(hostEl, bar, view);
        return;
      }
      if (ext === 'md' || ext === 'markdown') {
        const text = await api.fileText(runId, path);
        setChildren(hostEl, bar, markdownBlock(text));
        return;
      }
      if (ext === 'stl' || ext === 'dxf') {
        setChildren(hostEl, bar, el('p', { class: 'muted' },
          `${ext.toUpperCase()} geometry. Open the Chips view for the 3D viewer, or download it.`));
        return;
      }
      const text = await api.fileText(runId, path);
      setChildren(hostEl, bar, el('pre', { class: 'code' }, text.slice(0, 400000)));
    } catch (err) {
      setChildren(hostEl, bar, fileError(err, path));
    }
  }

  function fileError(err, path) {
    if (err instanceof ApiError && err.missing) {
      return emptyState('That file is not there yet',
        el('p', {}, el('code', path), ' is listed in the manifest but the backend returned 404.'),
        el('p', { class: 'small muted' }, 'The /file route is sandboxed to the paths a run declares, so this usually means the stage has not finished writing.'));
    }
    if (err instanceof ApiError && err.notReady) {
      return emptyState('Not built yet', el('p', explain(err)));
    }
    return errorState('Could not read that file', el('p', explain(err)));
  }

  /* ---------------------------------------------------------------- logs */

  function logLine(line) {
    const stage = line.stage ? el('span', { class: 'lg-stage' }, `${line.stage} `) : null;
    const t = line.t ? el('span', { class: 'lg-t' }, `${shortTime(line.t)} `) : null;
    const level = ['error', 'warn', 'warning'].includes(line.level) ? ` level-${line.level === 'warning' ? 'warn' : line.level}` : '';
    return el('span', { class: `log-line${level}` }, t, stage, line.text, '\n');
  }

  function pinned() {
    return logPre.scrollTop + logPre.clientHeight >= logPre.scrollHeight - 24;
  }

  function redrawLogs() {
    const want = logFilter.value;
    const lines = want ? live.logs.filter((l) => l.stage === want) : live.logs;
    clear(logPre);
    if (!lines.length) {
      logPre.appendChild(el('span', { class: 'muted' }, live.mode === 'polling'
        ? 'No log stream: the backend has no events route yet, so the manifest is polled instead.\n'
        : 'No log lines yet.\n'));
      return;
    }
    for (const l of lines.slice(-1200)) logPre.appendChild(logLine(l));
    logPre.scrollTop = logPre.scrollHeight;
  }

  /* ---------------------------------------------------------------- artifacts */

  async function loadArtifacts() {
    try {
      artifacts = normaliseArtifacts(await api.artifacts(runId));
    } catch (err) {
      artifacts = [];
      if (!(err instanceof ApiError && (err.notReady || err.missing))) console.warn('artifacts:', explain(err));
    }
    if (selected) select(selected);
  }

  /* ---------------------------------------------------------------- live wiring */

  function paintMode() {
    const m = modeLabel(live.mode);
    setChildren(modeBadge, badge(m.text, m.kind, m.title));
  }

  offs.push(live.on('manifest', () => {
    buildTimeline();
    paintSummary();
    if (!selected) {
      const pick = interestingStage();
      if (pick) select(pick.id);
    } else {
      select(selected);
    }
    redrawLogs();
  }));

  offs.push(live.on('stage', (entry) => {
    if (!rows.has(entry.id)) { buildTimeline(); paintSummary(); }
    else paintRow(entry.id);
    if (entry.status === 'running' && !userPicked) select(entry.id);
    if (selected === entry.id) select(entry.id);
  }));

  let userPicked = false;
  timeline.addEventListener('click', () => { userPicked = true; });

  offs.push(live.on('log', (line) => {
    const want = logFilter.value;
    if (want && line.stage !== want) return;
    const atBottom = pinned();
    if (logPre.firstChild && logPre.firstChild.classList && logPre.firstChild.classList.contains('muted')) clear(logPre);
    logPre.appendChild(logLine(line));
    while (logPre.childElementCount > 1400) logPre.removeChild(logPre.firstChild);
    if (atBottom) logPre.scrollTop = logPre.scrollHeight;
  }));

  offs.push(live.on('finding', () => paintCounters()));
  offs.push(live.on('mode', paintMode));
  offs.push(live.on('done', () => { paintSummary(); loadArtifacts(); toast('Run finished'); ctx.refreshRuns(); }));

  /**
   * True when this run has no stage record AND is not simply waiting to start: the
   * statuses in the timeline were never observed and must not be read as fact.
   */
  function unrecorded() {
    const m = live.manifest;
    if (!m || m.has_manifest) return false;
    return !['queued', 'running'].includes(m.status);
  }

  /** What to open by default: whatever a reader most needs to see. */
  function interestingStage() {
    const all = [...(live.manifest ? live.manifest.stages.values() : [])];
    return all.find((s) => s.status === 'running')
      || all.find((s) => s.status === 'failed')
      || all.filter((s) => s.status === 'done').pop()
      || null;
  }

  paintMode();
  buildTimeline();
  paintSummary();
  redrawLogs();
  if (live.manifest && !selected) {
    const pick = interestingStage();
    if (pick) select(pick.id);
  }
  loadArtifacts();

  return () => { for (const off of offs) off(); };
}

function shortPath(p) {
  const parts = String(p).split('/');
  return parts.length > 2 ? `${parts[parts.length - 2]}/${parts[parts.length - 1]}` : String(p);
}

function shortTime(t) {
  const s = fmtTime(t);
  const m = /(\d{1,2}:\d{2}:\d{2})/.exec(s);
  return m ? m[1] : String(t).slice(0, 12);
}

export { fmtBytes };

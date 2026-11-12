// View 2: RUN IT. One obvious action (drop a protocol), the options folded away, and a
// live horizontal track of the stages while a run is going.
//
// API calls and the SSE contract are unchanged: POST /api/runs, POST /api/cfd, then the
// shared RunLive stream from live.js (GET /api/runs/<id>/events, manifest polling as the
// fallback).

import { api, ApiError, explain } from '../api.js';
import { el, clear, setChildren, badge, toast, fmtSeconds, fmtTime, errorState } from '../dom.js';
import { stubBadge, demoBadge, simBadge, STUB_TITLE, TODO_TITLE } from '../labels.js';
import { liveFor, modeLabel } from '../live.js';
import { stageOrder } from '../model.js';
import { term } from '../glossary.js';

const MVP_PIPELINE = 'protocols/pipeline_mvp.json';
const ACCEPT = '.pdf,.md,.markdown,.txt,.text,application/pdf,text/markdown,text/plain';

const STATUS_WORD = {
  pending: 'waiting', queued: 'queued', running: 'running now', done: 'done', failed: 'failed',
  skipped: 'skipped', not_implemented: 'not built yet', unknown: 'not recorded',
};

export function mount(host, ctx) {
  const runId = ctx.param || null;
  const offs = [];

  const top = el('section', { class: 'hero hero-tight' },
    el('p', { class: 'eyebrow' }, 'Run it'),
    el('h1', { class: 'display' }, 'Turn a written lab protocol into chips, and check they flow.'),
    el('p', { class: 'lede' },
      'Drop a protocol and the software works out which steps can run on a chip, designs and routes the chips, '
      + 'simulates the liquid moving through them and writes a report. It runs on this machine; nothing is uploaded anywhere else.'));

  const liveHost = el('section', { class: 'section', id: 'live' });
  const startHost = el('section', { class: 'section' });
  const recentHost = el('section', { class: 'section' });

  setChildren(host, top, runId ? liveHost : null, startHost, recentHost);

  buildStart(startHost);
  buildRecent(recentHost);
  if (runId) offs.push(...buildLive(liveHost, runId));

  return () => { for (const off of offs) off(); };

  /* ---------------------------------------------------------------- start */

  function buildStart(section) {
    const form = {
      file: null, text: '', ref: '', id: '', from: '', to: '',
      scale: 'auto',
      llm: ctx.state.health && String(ctx.state.health.llm) === 'stub' ? 'stub' : 'auto',
    };
    const status = el('div', { 'aria-live': 'polite' });
    const fileName = el('span', { class: 'drop-file' });
    const submit = el('button', { type: 'submit', class: 'primary big' }, 'Design chips for this protocol');

    const fileInput = el('input', {
      type: 'file', accept: ACCEPT, id: 'ri-file', class: 'visually-hidden',
      onchange: (e) => setFile(e.target.files && e.target.files[0]),
    });
    const drop = el('div', {
      class: 'drop', tabindex: '0', role: 'button', 'aria-describedby': 'ri-drop-help',
      onclick: () => fileInput.click(),
      onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); } },
    },
    dropArt(),
    el('span', { class: 'drop-t' }, 'Drop a protocol here'),
    el('span', { class: 'drop-s' }, 'A PDF, markdown or plain-text file, or click to choose one'),
    fileName);
    for (const ev of ['dragenter', 'dragover']) drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); });
    for (const ev of ['dragleave', 'drop']) drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); });
    drop.addEventListener('drop', (e) => {
      const dt = e.dataTransfer;
      if (!dt) return;
      if (dt.files && dt.files[0]) { setFile(dt.files[0]); return; }
      const t = dt.getData('text/plain');
      if (t) { refInput.value = t.trim(); form.ref = t.trim(); pasteFold.open = true; update(); }
    });

    const refInput = el('input', {
      type: 'text', id: 'ri-ref', placeholder: '10.1038/...  or  https://...',
      oninput: (e) => { form.ref = e.target.value.trim(); if (form.ref) setFile(null, true); update(); },
    });
    const textArea = el('textarea', {
      id: 'ri-text', rows: '6', placeholder: 'Paste the protocol, one step per line or as prose.',
      oninput: (e) => { form.text = e.target.value; if (form.text.trim()) setFile(null, true); update(); },
    });

    function setFile(f, keepText) {
      form.file = f || null;
      setChildren(fileName, f ? `${f.name} · ${Math.max(1, Math.round(f.size / 1024))} kB` : '');
      drop.classList.toggle('has-file', !!f);
      if (f && !keepText) { form.text = ''; textArea.value = ''; form.ref = ''; refInput.value = ''; }
      update();
    }
    const hasSource = () => !!(form.file || form.text.trim() || form.ref);
    function update() { submit.disabled = !hasSource(); }
    update();

    const scaleNote = el('p', { class: 'cap' });
    const llmNote = el('p', { class: 'cap' });
    const scaleHint = (v) => {
      if (v === 'demo') return [demoBadge(), ' Every chip comes out 30 × 20 mm, the size we can print now, and is labelled so wherever it appears.'];
      if (v === 'target') return 'Chips are laid out on the full microplate footprint, 127.76 × 85.48 mm.';
      return ['The software picks per chip, and says which it used. ', term('demo', 'What is demo scale?')];
    };
    const llmHint = (v) => {
      const bits = [];
      if (v === 'stub') bits.push(stubBadge(), ' The protocol is read with simple fixed rules instead of a language model, so the extracted steps are placeholders.');
      else if (v === 'real') bits.push('A language model reads the protocol. If none is configured the run stops rather than guessing.');
      else bits.push('Uses a language model when one is configured, otherwise the offline rules.');
      if (ctx.state.health && String(ctx.state.health.llm) === 'stub' && v !== 'stub') {
        bits.push(' ', badge('no model configured', 'warn', STUB_TITLE), ' so this will fall back to the offline rules.');
      }
      return bits;
    };
    setChildren(scaleNote, scaleHint(form.scale));
    setChildren(llmNote, llmHint(form.llm));

    const pasteFold = el('details', { class: 'fold' },
      el('summary', {}, el('span', { class: 'fs-text' }, el('span', {}, 'Paste text or a DOI instead'),
        el('span', { class: 'fs-s' }, 'For a protocol that is not in a file.'))),
      el('label', { class: 'field', for: 'ri-ref' }, el('span', {}, 'DOI or web address'), refInput),
      el('label', { class: 'field', for: 'ri-text' }, el('span', {}, 'Protocol text'), textArea));

    const optFold = el('details', { class: 'fold' },
      el('summary', {}, el('span', { class: 'fs-text' }, el('span', {}, 'Options'),
        el('span', { class: 'fs-s' }, 'Chip size, how the protocol is read, and which stages to run. The defaults are fine.'))),
      el('div', { class: 'opt-grid' },
        el('div', {},
          el('p', { class: 'opt-t' }, 'Chip size'),
          segment('Chip size', ['auto', 'demo', 'target'], form.scale, (v) => { form.scale = v; setChildren(scaleNote, scaleHint(v)); },
            { auto: 'let the software choose', demo: '30 × 20 mm', target: 'full microplate' }),
          scaleNote),
        el('div', {},
          el('p', { class: 'opt-t' }, 'Reading the protocol'),
          segment('Reading the protocol', ['auto', 'real', 'stub'], form.llm, (v) => { form.llm = v; setChildren(llmNote, llmHint(v)); },
            { auto: 'automatic', real: 'language model', stub: 'offline rules' }),
          llmNote),
        el('div', {},
          el('p', { class: 'opt-t' }, 'Run name and stage range'),
          el('div', { class: 'row' },
            el('label', { class: 'field' }, el('span', {}, 'Name (optional)'),
              el('input', { type: 'text', placeholder: 'my_assay', oninput: (e) => { form.id = e.target.value.trim(); } })),
            el('label', { class: 'field narrow' }, el('span', {}, 'From'),
              el('input', { type: 'text', placeholder: 's0', oninput: (e) => { form.from = e.target.value.trim(); } })),
            el('label', { class: 'field narrow' }, el('span', {}, 'To'),
              el('input', { type: 'text', placeholder: 's12', oninput: (e) => { form.to = e.target.value.trim(); } }))),
          el('p', { class: 'cap' }, 'Stages are s0 to s12. Leave both blank to run everything.'))));

    const formEl = el('form', {
      class: 'start-form',
      onsubmit: async (e) => {
        e.preventDefault();
        if (!hasSource()) return;
        submit.disabled = true;
        setChildren(status, el('p', { class: 'loading' }, 'Starting the run...'));
        const options = { llm: form.llm, scale: form.scale };
        if (form.from) options.from = form.from;
        if (form.to) options.to = form.to;
        try {
          let res;
          if (form.file) res = await api.createRun({ file: form.file, id: form.id || undefined, options });
          else {
            const body = { options };
            if (form.id) body.id = form.id;
            if (form.ref) body[/^(https?:)?\/\//i.test(form.ref) ? 'url' : 'doi'] = form.ref;
            else body.text = form.text;
            res = await api.createRun(body);
          }
          const id = res && (res.run_id || res.id || res.job_id);
          if (!id) throw new ApiError('the backend accepted the run but returned no run_id', { detail: JSON.stringify(res).slice(0, 200) });
          toast(`Run ${id} started`);
          await ctx.refreshRuns();
          ctx.selectRun(id, 'run');
        } catch (err) {
          submit.disabled = false;
          setChildren(status, failure('Could not start the run', err));
        }
      },
    },
    drop, fileInput,
    el('p', { class: 'cap', id: 'ri-drop-help' },
      'The file is saved under assay2chip/work/<run>/upload/ on this machine. The server only listens to this computer.'),
    el('div', { class: 'start-actions' }, submit, status),
    pasteFold, optFold);

    /* the CFD shortcut */
    const cfdStatus = el('div', { 'aria-live': 'polite' });
    const cfdButton = el('button', {
      type: 'button', class: 'secondary',
      onclick: async () => {
        cfdButton.disabled = true;
        setChildren(cfdStatus, el('p', { class: 'loading' }, 'Starting the flow simulation...'));
        try {
          const res = await api.cfd({ pipeline: MVP_PIPELINE });
          const id = res && (res.job_id || res.run_id || res.id);
          if (!id) throw new ApiError('the backend accepted the job but returned no job_id', { detail: JSON.stringify(res).slice(0, 200) });
          toast(`Flow simulation ${id} started`);
          await ctx.refreshRuns();
          ctx.selectRun(id, 'run');
        } catch (err) {
          cfdButton.disabled = false;
          setChildren(cfdStatus, failure('Could not start the flow simulation', err));
        }
      },
    }, 'Simulate the existing chain');

    setChildren(section,
      el('div', { class: 'start-grid' },
        el('div', { class: 'card card-primary' },
          el('p', { class: 'eyebrow' }, 'Design from a protocol'),
          el('h2', {}, 'Start from your protocol'),
          el('p', { class: 'sub' }, 'About a minute for a short protocol. You can watch every stage as it happens.'),
          formEl),
        el('div', { class: 'card' },
          el('p', { class: 'eyebrow' }, 'Check what exists'),
          el('h2', {}, 'Already have chips?'),
          el('p', { class: 'sub' }, 'Re-check the six-chip chain shown on The assay page without redesigning anything.'),
          el('p', {}, 'This meshes every chip, solves the flow through each protocol step, joins the chips through their tubing and lists anything that would go wrong, such as too much ',
            term('shear'), ', a long ', term('residence'), ', ', term('bubble', 'bubbles'), ' or ', term('leak', 'leaks'), '.'),
          el('p', { class: 'row' }, cfdButton, simBadge()),
          cfdStatus,
          el('p', { class: 'cap' }, 'No chip file is changed: a proposed fix is applied to a copy. Every number it produces is a prediction from the flow model.'))));
  }

  /* ---------------------------------------------------------------- live track */

  function buildLive(section, id) {
    const live = liveFor(id);
    const offs2 = [];
    let picked = null;

    const modeHost = el('span', {});
    const headline = el('div', { class: 'live-headline' });
    const track = el('ol', { class: 'track', 'aria-label': 'Stages of this run' });
    const trackWrap = el('div', { class: 'track-wrap' }, track);
    const stageInfo = el('div', { class: 'stage-info', 'aria-live': 'polite' });
    const logPre = el('pre', { class: 'log', tabindex: '0', 'aria-label': 'Run log' });
    const logFold = el('details', { class: 'fold' },
      el('summary', {}, el('span', { class: 'fs-text' }, el('span', {}, 'Show the raw log'),
        el('span', { class: 'fs-s' }, 'Everything the pipeline printed, newest at the bottom.'))),
      logPre);
    const after = el('div', { class: 'live-after' });

    setChildren(section,
      el('div', { class: 'card card-live' },
        el('div', { class: 'live-top' },
          el('div', {},
            el('p', { class: 'eyebrow' }, 'This run'),
            el('h2', { class: 'mono-title' }, id)),
          el('span', { class: 'spacer' }), modeHost),
        el('p', { class: 'sub' }, 'Each circle is one stage of the pipeline, left to right. Click one for what it does and what it wrote.'),
        headline, trackWrap, stageInfo, after, logFold));

    function stageList() {
      const known = new Map((ctx.state.stages || []).map((s) => [s.id, s]));
      const mids = live.manifest ? [...live.manifest.stages.keys()] : [];
      const own = mids.length > 0 && mids.every((x) => !known.has(x));
      const ids = own ? mids : [...new Set([...known.keys(), ...mids])];
      return ids.map((sid) => ({ id: sid, name: (known.get(sid) || {}).name || '', description: (known.get(sid) || {}).description || '' }))
        .sort((a, b) => stageOrder(a.id) - stageOrder(b.id) || a.id.localeCompare(b.id));
    }
    const entryOf = (sid) => (live.manifest && live.manifest.stages.get(sid)) || null;
    const unrecorded = () => live.manifest && !live.manifest.has_manifest && !['queued', 'running'].includes(live.manifest.status);

    function paint() {
      const list = stageList();
      const m = live.manifest;
      clear(track);
      for (const s of list) {
        const e = entryOf(s.id);
        const st = unrecorded() ? 'unknown' : (e ? e.status : 'pending');
        const b = el('button', {
          type: 'button', class: 'track-btn',
          'aria-label': `${s.id} ${s.name}: ${STATUS_WORD[st] || st}`,
          'aria-pressed': String(picked === s.id),
          onclick: () => { picked = s.id; paint(); },
        },
        el('span', { class: 'track-dot', 'aria-hidden': 'true' }),
        el('span', { class: 'track-name' }, s.name || s.id),
        el('span', { class: 'track-st' }, st === 'pending' ? s.id : (STATUS_WORD[st] || st)));
        track.appendChild(el('li', { dataset: { status: st } }, b));
      }
      const done = list.filter((s) => (entryOf(s.id) || {}).status === 'done').length;
      const running = list.find((s) => (entryOf(s.id) || {}).status === 'running');
      const failed = list.find((s) => (entryOf(s.id) || {}).status === 'failed');
      const flags = el('span', { class: 'flags' },
        m && String(m.llm) === 'stub' ? stubBadge() : null,
        m && String(m.scale) === 'demo' ? demoBadge() : null,
        m && (m.kind === 'cfd' || /cfd/i.test(String(m.title))) ? simBadge() : null);

      let big;
      let small = '';
      if (!m) big = live.error ? 'Cannot read this run' : 'Connecting...';
      else if (unrecorded()) { big = 'No stage record for this run'; small = 'It was not started from this page, so which stages ran is unknown. Its files are still on the Results page.'; }
      else if (m.status === 'queued') { big = 'Waiting its turn'; small = m.message || 'Runs go one at a time, because several stages write into the shared chip and protocol folders.'; }
      else if (failed) { big = `Stopped at ${failed.id} ${(list.find((x) => x.id === failed.id) || {}).name || ''}`; small = 'Click the red stage for the error.'; }
      else if (running) { const rs = list.find((x) => x.id === running.id) || {}; big = `Now: ${rs.name || running.id}`; small = rs.description || ''; }
      else if (m.status === 'done' || live.finished) big = 'Finished';
      else big = 'Starting';
      setChildren(headline,
        el('div', { class: 'lh-main' },
          el('span', { class: 'lh-big' }, big),
          list.length ? el('span', { class: 'lh-count' }, `${done} of ${list.length} stages done`) : null,
          flags),
        small ? el('p', { class: 'sub' }, small) : null,
        el('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': String(list.length || 1), 'aria-valuenow': String(done) },
          el('i', { style: { width: `${list.length ? (100 * done) / list.length : 0}%` } })));

      const finished = m && (m.status === 'done' || m.status === 'failed' || live.finished) && !unrecorded();
      setChildren(after, finished || unrecorded()
        ? el('div', { class: 'row' },
          el('a', { class: 'button primary', href: `#/results/${encodeURIComponent(id)}` }, 'See the results →'),
          failed ? el('button', { type: 'button', class: 'secondary', onclick: () => rerun(failed.id) }, `Try again from ${failed.id}`) : null)
        : null);
      paintInfo(list);
      const ml = modeLabel(live.mode);
      setChildren(modeHost, m && m.status === 'failed' && live.mode === 'closed'
        ? badge('stopped', 'bad', 'The run ended with a failed stage.')
        : badge(ml.text, ml.kind, ml.title));
    }

    function paintInfo(list) {
      const sid = picked || (list.find((s) => ['running', 'failed'].includes((entryOf(s.id) || {}).status)) || {}).id;
      if (!sid) { clear(stageInfo); return; }
      const s = list.find((x) => x.id === sid) || { id: sid };
      const e = entryOf(sid);
      const st = e ? e.status : 'pending';
      setChildren(stageInfo,
        el('div', { class: 'si-head' },
          el('span', { class: 'mono' }, sid), el('strong', {}, s.name || ''),
          el('span', { class: `si-st st-${st}` }, STATUS_WORD[st] || st)),
        s.description ? el('p', {}, s.description) : null,
        st === 'not_implemented' ? el('p', { class: 'cap' }, TODO_TITLE) : null,
        e && e.error ? el('pre', { class: 'code err-code' }, String(e.error)) : null,
        e && (Number.isFinite(e.seconds) || e.outputs.length)
          ? el('p', { class: 'cap' },
            Number.isFinite(e.seconds) ? `Took ${fmtSeconds(e.seconds)}. ` : '',
            e.outputs.length ? `Wrote ${e.outputs.length} file${e.outputs.length === 1 ? '' : 's'}: ${e.outputs.slice(0, 4).map((o) => o.path.split('/').pop()).join(', ')}${e.outputs.length > 4 ? '...' : ''}` : '')
          : null);
    }

    function logLine(line) {
      return el('span', { class: `log-line${line.level === 'error' ? ' level-error' : /warn/.test(line.level) ? ' level-warn' : ''}` },
        line.stage ? el('span', { class: 'lg-stage' }, `${line.stage} `) : null, line.text, '\n');
    }
    function redrawLog() {
      clear(logPre);
      if (!live.logs.length) { logPre.appendChild(el('span', { class: 'muted' }, 'No log lines yet.\n')); return; }
      for (const l of live.logs.slice(-1200)) logPre.appendChild(logLine(l));
      logPre.scrollTop = logPre.scrollHeight;
    }

    async function rerun(from) {
      try {
        const res = await api.rerun(id, from);
        const nid = res && (res.run_id || res.id);
        toast(nid && nid !== id ? `Re-run started as ${nid}` : `Re-running from ${from}`);
        if (nid && nid !== id) { await ctx.refreshRuns(); ctx.selectRun(nid, 'run'); } else live.refreshManifest();
      } catch (err) { toast(explain(err), true); }
    }

    offs2.push(live.on('manifest', () => { paint(); redrawLog(); }));
    offs2.push(live.on('stage', () => paint()));
    offs2.push(live.on('mode', () => paint()));
    offs2.push(live.on('log', (line) => {
      const atBottom = logPre.scrollTop + logPre.clientHeight >= logPre.scrollHeight - 24;
      if (logPre.firstChild && logPre.firstChild.classList && logPre.firstChild.classList.contains('muted')) clear(logPre);
      logPre.appendChild(logLine(line));
      while (logPre.childElementCount > 1400) logPre.removeChild(logPre.firstChild);
      if (atBottom) logPre.scrollTop = logPre.scrollHeight;
    }));
    offs2.push(live.on('done', () => { paint(); ctx.refreshRuns(); toast('Run finished'); }));
    paint();
    redrawLog();
    return offs2;
  }

  /* ---------------------------------------------------------------- recent */

  function buildRecent(section) {
    const runs = (ctx.state.runs || []).slice(0, 8);
    if (!runs.length) {
      setChildren(section, el('header', { class: 'sec-head' }, el('h2', {}, 'Earlier runs'),
        el('p', { class: 'sub' }, 'None yet. Start one above.')));
      return;
    }
    setChildren(section,
      el('header', { class: 'sec-head' }, el('h2', {}, 'Earlier runs'),
        el('p', { class: 'sub' }, 'Pick one up where it left off, or open what it produced.')),
      el('ul', { class: 'run-list' }, ...runs.map((r) => {
        const sev = r.findings_by_severity || {};
        const total = ['blocker', 'high', 'medium', 'low'].reduce((a, k) => a + (Number(sev[k]) || 0), 0);
        return el('li', { class: 'run-row' },
          el('span', { class: `run-dot st-${r.status}`, title: STATUS_WORD[r.status] || r.status }),
          el('span', { class: 'run-id mono' }, r.run_id),
          el('span', { class: 'run-when' }, fmtTime(r.created)),
          el('span', { class: 'run-sev' }, total ? sevBar(sev) : el('span', { class: 'muted' }, STATUS_WORD[r.status] || r.status)),
          el('span', { class: 'run-acts' },
            el('a', { href: `#/run/${encodeURIComponent(r.run_id)}` }, 'Progress'),
            el('a', { href: `#/results/${encodeURIComponent(r.run_id)}` }, 'Results')));
      })));
  }
}

function sevBar(sev) {
  const keys = ['blocker', 'high', 'medium', 'low'];
  const total = keys.reduce((a, k) => a + (Number(sev[k]) || 0), 0);
  return el('span', { class: 'mini-sev', title: keys.map((k) => `${Number(sev[k]) || 0} ${k}`).join(', ') },
    el('span', { class: 'mini-bar' }, ...keys.filter((k) => Number(sev[k]) > 0)
      .map((k) => el('i', { class: `sev-fill-${k}`, style: { flexGrow: String(Number(sev[k])) } }))),
    el('span', { class: 'mini-n' }, `${total} findings`, Number(sev.blocker) ? `, ${sev.blocker} blocking` : ''));
}

export { sevBar };

function dropArt() {
  const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  s.setAttribute('viewBox', '0 0 120 60');
  s.setAttribute('class', 'drop-art');
  s.setAttribute('aria-hidden', 'true');
  // a page turning into a chip: lines of text, an arrow, a chip with three chambers
  const parts = [
    ['rect', { x: 6, y: 6, width: 34, height: 46, rx: 4, class: 'da-page' }],
    ['path', { d: 'M13 18h20M13 25h20M13 32h14M13 39h18', class: 'da-lines' }],
    ['path', { d: 'M50 29h18m-6-6 6 6-6 6', class: 'da-arrow' }],
    ['rect', { x: 78, y: 12, width: 36, height: 34, rx: 6, class: 'da-chip' }],
    ['rect', { x: 84, y: 18, width: 10, height: 8, rx: 2, class: 'da-cx' }],
    ['rect', { x: 98, y: 18, width: 10, height: 8, rx: 2, class: 'da-cx' }],
    ['rect', { x: 91, y: 32, width: 10, height: 8, rx: 2, class: 'da-cx' }],
  ];
  for (const [tag, attrs] of parts) {
    const n = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
    s.appendChild(n);
  }
  return s;
}

function segment(label, values, current, onChange, words = {}) {
  const group = el('div', { class: 'segment', role: 'radiogroup', 'aria-label': label });
  const buttons = values.map((v) => el('button', {
    type: 'button', role: 'radio', 'aria-checked': String(v === current), 'aria-pressed': String(v === current),
    onclick: () => {
      for (const b of buttons) {
        const on = b.dataset.v === v;
        b.setAttribute('aria-pressed', String(on));
        b.setAttribute('aria-checked', String(on));
      }
      onChange(v);
    },
    dataset: { v },
  }, words[v] || v));
  for (const b of buttons) group.appendChild(b);
  return group;
}

function failure(title, err) {
  const notReady = err instanceof ApiError && err.notReady;
  return errorState(notReady ? 'That part of the backend is not ready' : title,
    el('p', {}, explain(err)),
    notReady ? el('p', { class: 'small' }, 'The server answered 503: the route exists but is not wired up yet. Nothing is wrong with your input.') : null);
}

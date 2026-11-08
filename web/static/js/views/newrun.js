// View 1: New run. Drop a PDF / markdown / text, or paste a DOI or URL, pick the scale
// and the LLM mode. Also the "Run CFD on the existing MVP pipeline" button, which is the
// first thing most people will press.

import { api, ApiError, explain } from '../api.js';
import { el, clear, setChildren, badge, toast, fmtTime, errorState } from '../dom.js';
import { stubBadge, demoBadge, STUB_TITLE, simBadge } from '../labels.js';
import { statusBadge } from '../labels.js';

const MVP_PIPELINE = 'protocols/pipeline_mvp.json';
const ACCEPT = '.pdf,.md,.markdown,.txt,.text,application/pdf,text/markdown,text/plain';

export function mount(host, ctx) {
  const form = {
    file: null,
    text: '',
    ref: '',
    scale: 'auto',
    llm: ctx.state.health && String(ctx.state.health.llm) === 'stub' ? 'stub' : 'auto',
    id: '',
    from: '',
    to: '',
  };

  const status = el('div', { 'aria-live': 'polite' });
  const fileLabel = el('p', { class: 'file-name' });
  const submit = el('button', { type: 'submit', class: 'primary' }, 'Start the run');

  /* ---------------------------------------------------------------- source */

  const fileInput = el('input', {
    type: 'file', accept: ACCEPT, id: 'nr-file',
    style: { position: 'absolute', width: '1px', height: '1px', opacity: '0' },
    onchange: (e) => setFile(e.target.files && e.target.files[0]),
  });

  const drop = el('div', {
    class: 'drop', tabindex: '0', role: 'button',
    'aria-describedby': 'nr-drop-help',
    onclick: () => fileInput.click(),
    onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); } },
  },
  el('strong', 'Drop a paper or protocol here'),
  el('span', 'PDF, markdown or plain text - or click to choose a file'),
  fileLabel);

  for (const ev of ['dragenter', 'dragover']) {
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); });
  }
  for (const ev of ['dragleave', 'drop']) {
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); });
  }
  drop.addEventListener('drop', (e) => {
    const dt = e.dataTransfer;
    if (!dt) return;
    if (dt.files && dt.files[0]) { setFile(dt.files[0]); return; }
    const text = dt.getData('text/plain');
    if (text) { refInput.value = text.trim(); form.ref = text.trim(); refInput.focus(); }
  });

  function setFile(f) {
    form.file = f || null;
    setChildren(fileLabel, f ? `${f.name} (${Math.max(1, Math.round(f.size / 1024))} kB)` : '');
    if (f) { form.text = ''; textArea.value = ''; form.ref = ''; refInput.value = ''; }
    updateSubmit();
  }

  const refInput = el('input', {
    type: 'text', id: 'nr-ref', placeholder: '10.1038/s41586-021-00000-0  or  https://example.org/protocol',
    style: { width: '100%' },
    oninput: (e) => { form.ref = e.target.value.trim(); if (form.ref) setFile(null); updateSubmit(); },
  });

  const textArea = el('textarea', {
    id: 'nr-text', rows: '7', placeholder: 'Or paste the protocol text here, one step per line or as prose.',
    oninput: (e) => { form.text = e.target.value; if (form.text.trim()) setFile(null); updateSubmit(); },
  });

  /* ---------------------------------------------------------------- options */

  const scaleSeg = segment('Scale', ['auto', 'demo', 'target'], form.scale, (v) => {
    form.scale = v;
    setChildren(scaleNote, scaleHint(v));
  }, {
    auto: 'Let the pipeline choose: demo if the design does not fit the target footprint.',
    demo: '30 x 20 mm fabrication demo. Every chip is labelled DEMO SCALE.',
    target: 'ANSI/SLAS microplate footprint, 127.76 x 85.48 mm.',
  });
  const scaleNote = el('p', { class: 'hint' }, scaleHint(form.scale));

  const llmSeg = segment('LLM mode', ['auto', 'real', 'stub'], form.llm, (v) => {
    form.llm = v;
    setChildren(llmNote, llmHint(v));
  }, {
    auto: 'Use a model if one is configured, otherwise the deterministic fallback.',
    real: 'Require a language model for extraction (S1) and grouping (S5).',
    stub: 'Force the offline deterministic fallback. Output is labelled LLM STUB.',
  });
  const llmNote = el('p', { class: 'hint' }, llmHint(form.llm));

  function scaleHint(v) {
    if (v === 'demo') return [demoBadge(), ' Every chip comes out 30 x 20 mm and is labelled DEMO SCALE wherever it appears.'];
    if (v === 'target') return 'Chips are laid out on the 127.76 x 85.48 mm microplate footprint. The ceiling is 290 x 190 mm.';
    return 'The pipeline picks demo or target per chip and says which it used.';
  }

  function llmHint(v) {
    const backendStub = ctx.state.health && String(ctx.state.health.llm) === 'stub';
    const bits = [];
    if (v === 'stub') bits.push(stubBadge(), ' Extraction runs offline. Steps, confidences and groupings come from rules, not a model.');
    else if (v === 'real') bits.push('S1 and S5 call a language model. If none is configured the run fails rather than quietly guessing.');
    else bits.push('A model is used when available, and the run records which path it took.');
    if (backendStub && v !== 'stub') {
      bits.push(' ', badge('backend reports llm: stub', 'warn', STUB_TITLE),
        ' so this run will almost certainly fall back to the offline path.');
    }
    return bits;
  }

  const idInput = el('input', { type: 'text', placeholder: 'my_assay', oninput: (e) => { form.id = e.target.value.trim(); } });
  const fromInput = el('input', { type: 'text', placeholder: 's0', oninput: (e) => { form.from = e.target.value.trim(); } });
  const toInput = el('input', { type: 'text', placeholder: 's12', oninput: (e) => { form.to = e.target.value.trim(); } });

  /* ---------------------------------------------------------------- submit */

  function hasSource() { return !!(form.file || form.text.trim() || form.ref); }
  function updateSubmit() { submit.disabled = !hasSource(); }
  updateSubmit();

  const formEl = el('form', {
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
  el('fieldset', {},
    el('legend', 'Source'),
    drop, fileInput,
    el('label', { class: 'field', for: 'nr-ref', style: { marginTop: '12px' } }, el('span', 'DOI or URL'), refInput),
    el('label', { class: 'field', for: 'nr-text' }, el('span', 'Pasted text'), textArea),
    el('p', { class: 'hint', id: 'nr-drop-help' },
      'The file is uploaded to assay2chip/work/<id>/upload/ on this machine and nowhere else. '
      + 'The server binds to 127.0.0.1, so an unpublished protocol does not leave the host.')),

  el('fieldset', {}, el('legend', 'Scale'), scaleSeg, scaleNote),
  el('fieldset', {}, el('legend', 'Extraction'), llmSeg, llmNote),

  el('details', { class: 'disclose' },
    el('summary', 'Advanced: run id and stage range'),
    el('label', { class: 'field' }, el('span', 'Run id (optional)'), idInput),
    el('div', { class: 'row' },
      el('label', { class: 'field' }, el('span', 'From stage'), fromInput),
      el('label', { class: 'field' }, el('span', 'To stage'), toInput)),
    el('p', { class: 'hint' }, 'Stage ids are s0 to s12. Leave both blank to run the whole chain.')),

  el('div', { class: 'row' }, submit, status));

  /* ---------------------------------------------------------------- CFD shortcut */

  const cfdStatus = el('div', { 'aria-live': 'polite' });
  const cfdButton = el('button', {
    type: 'button', class: 'primary',
    onclick: async () => {
      cfdButton.disabled = true;
      setChildren(cfdStatus, el('p', { class: 'loading' }, 'Meshing and solving the MVP chain...'));
      try {
        const res = await api.cfd({ pipeline: MVP_PIPELINE });
        const id = res && (res.job_id || res.run_id || res.id);
        if (!id) throw new ApiError('the backend accepted the CFD job but returned no job_id', { detail: JSON.stringify(res).slice(0, 200) });
        toast(`CFD job ${id} started`);
        await ctx.refreshRuns();
        ctx.selectRun(id, 'run');
      } catch (err) {
        cfdButton.disabled = false;
        setChildren(cfdStatus, failure('Could not start the CFD job', err));
      }
    },
  }, 'Run CFD on the existing MVP pipeline');

  const cfdPanel = el('section', { class: 'panel' },
    el('h2', {}, 'Already-built chips: check the whole chain flows ', simBadge()),
    el('p', {}, 'The hand-designed MVP chain - module 1 culture, cell prep, encapsulation, '
      + 'lysis/wash/RT, sublibrary PCR, release - is seven chips joined by tubing. This meshes every '
      + 'one, solves the depth-averaged network, couples them through the handoffs and reports leaks, '
      + 'traps, shear and dead ends with fixes.'),
    el('p', { class: 'row' }, cfdButton, el('code', MVP_PIPELINE)),
    cfdStatus,
    el('p', { class: 'hint' }, 'No chip is modified: in standalone mode a fix is a proposal plus a patched copy '
      + 'in the work directory. Every number it produces is simulated, from a Hele-Shaw network solve, not a measurement.'));

  /* ---------------------------------------------------------------- recent runs */

  const recent = el('section', { class: 'panel' }, el('h2', 'Recent runs'), el('p', { class: 'loading' }, 'Loading...'));
  renderRecent();

  function renderRecent() {
    const runs = ctx.state.runs || [];
    const body = clear(recent);
    body.appendChild(el('h2', 'Recent runs'));
    if (!runs.length) {
      body.appendChild(el('p', { class: 'muted' }, 'No runs yet. Start one above, or press the CFD button to check the chips that already exist.'));
      return;
    }
    const rows = runs.slice(0, 25).map((r) => {
      const sev = r.findings_by_severity || {};
      return el('tr', {},
        el('td', {}, el('button', {
          type: 'button', class: 'ghost', style: { padding: '2px 6px' },
          onclick: () => ctx.selectRun(r.run_id, 'run'),
        }, r.run_id)),
        el('td', {}, r.title && r.title !== r.run_id ? r.title : el('span', { class: 'muted' }, '-')),
        el('td', {}, statusBadge(r.status)),
        el('td', { class: 'num' }, Number.isFinite(r.chip_count) ? String(r.chip_count) : (r.chips ? String(r.chips.length) : '-')),
        el('td', {}, severitySummary(sev)),
        el('td', { class: 'small muted' }, fmtTime(r.created)));
    });
    body.appendChild(el('div', { class: 'scroll-x' },
      el('table', { class: 'data' },
        el('thead', {}, el('tr', {},
          el('th', 'Run'), el('th', 'Title'), el('th', 'Status'), el('th', { class: 'num' }, 'Chips'),
          el('th', 'Findings'), el('th', 'Created'))),
        el('tbody', {}, ...rows))));
  }

  setChildren(host,
    el('div', { class: 'view-head' },
      el('h2', 'New run'),
      el('span', { class: 'sub' }, 'a paper or protocol in, printable chips and a flow verdict out')),
    el('div', { class: 'cols c-2' },
      el('section', { class: 'panel' }, formEl),
      el('div', { class: 'cols' }, cfdPanel, recent)));

  return () => {};
}

function severitySummary(sev) {
  const order = [['blocker', 'bad'], ['high', 'warn'], ['medium', 'info'], ['low', '']];
  const bits = order
    .filter(([k]) => Number(sev[k]) > 0)
    .map(([k, kind]) => badge(`${sev[k]} ${k}`, kind));
  return bits.length ? el('span', { class: 'row', style: { gap: '4px' } }, ...bits) : el('span', { class: 'muted' }, '-');
}

function segment(label, values, current, onChange, titles = {}) {
  const group = el('div', { class: 'segment', role: 'radiogroup', 'aria-label': label });
  const buttons = values.map((v) => el('button', {
    type: 'button', role: 'radio', 'aria-checked': String(v === current),
    'aria-pressed': String(v === current), title: titles[v] || null,
    onclick: () => {
      for (const b of buttons) {
        const on = b === btnFor(v);
        b.setAttribute('aria-pressed', String(on));
        b.setAttribute('aria-checked', String(on));
      }
      onChange(v);
    },
  }, v));
  const btnFor = (v) => buttons[values.indexOf(v)];
  for (const b of buttons) group.appendChild(b);
  return group;
}

function failure(title, err) {
  const api503 = err instanceof ApiError && err.notReady;
  return errorState(api503 ? 'Not built yet' : title,
    el('p', explain(err)),
    api503
      ? el('p', { class: 'small' }, 'The backend answered 503: this endpoint exists in web/README.md but is not wired up yet. Nothing is wrong with your input.')
      : null,
    err instanceof ApiError && err.detail ? el('p', { class: 'small mono' }, err.detail) : null);
}

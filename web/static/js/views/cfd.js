// View 5: CFD. Per chip and per protocol step: the pressure / speed / shear /
// residence-time maps; the findings table filterable by severity; every fix with its
// before and after numbers; and the pipeline overview of the whole chain.
//
// Every number here is simulated and labelled so. The solve is depth-averaged Hele-Shaw,
// which has no recirculation, so a "trap" is a low-velocity or long-residence zone, never
// a closed streamline.
//
// Two things on this page exist to stop a number being trusted more than it deserves:
// the per-module status (a module that never ran produced nothing) and the solver's own
// validation (if the benchmarks did not pass, the findings are not trustworthy).

import { api, ApiError, explain } from '../api.js';
import { el, clear, setChildren, badge, fmtNum, fmtTime, emptyState, errorState, loading } from '../dom.js';
import { simBadge, simNum, severityBadge, demoBadge, todoBadge, SEVERITIES, SIM_TITLE } from '../labels.js';
import { countSeverity, verdict } from '../model.js';
import { loadArtifacts } from '../locate.js';
import { liveFor } from '../live.js';
import { indexCfdFiles, normaliseCfdDoc, cfdDocFromParts, emptyCfdDoc, indexFigures } from '../cfddoc.js';
import { renderJson } from '../jsonview.js';

const METRICS = [
  ['pressure', 'Pressure'],
  ['speed', 'Speed'],
  ['shear', 'Wall shear'],
  ['residence', 'Residence time'],
];

export function mount(host, ctx) {
  const runId = ctx.runId;
  const live = liveFor(runId);
  const offs = [];
  let cancelled = false;

  const state = {
    doc: emptyCfdDoc(),
    loaded: false,
    sourcePath: null,
    chipIndex: 0,
    stepKey: null,
    filterSev: '',
    filterClass: '',
    filterChip: '',
  };

  const head = el('div', { class: 'view-head' },
    el('h2', 'CFD'),
    el('span', { class: 'sub' }, runId),
    simBadge(),
    el('span', { class: 'spacer' }),
    el('button', { type: 'button', class: 'ghost', onclick: () => reload() }, 'Reload'));

  const verdictPanel = el('section', { class: 'panel' });
  const validationPanel = el('section', { class: 'panel' });
  const mapsPanel = el('section', {});
  const findingsPanel = el('section', { class: 'panel' });
  const fixesPanel = el('section', { class: 'panel' });
  const chainPanel = el('section', { class: 'panel' });
  const modulesPanel = el('section', { class: 'panel' });

  const notePanel = el('section', { class: 'panel' },
    el('h4', 'How to read these numbers'),
    el('ul', { class: 'small muted', style: { margin: 0, paddingLeft: '20px' } },
      el('li', 'Every value here is simulated. Shallow rectangular ducts at Re well under 1 make the Hele-Shaw resistance network the correct physics, not a shortcut - but it is still a model.'),
      el('li', 'Hele-Shaw flow has no recirculation eddies, so a trap means a low-velocity or long-residence-time zone, found from the residence-time and velocity fields.'),
      el('li', 'Any edge above Re 1 is outside the model and is reported as an "unmodelled" finding rather than given a number to trust.'),
      el('li', 'Shear ceilings are scoped: 0.01 dyn/cm^2 for adherent cells only, 0.3 for a pre-lysis rinse, and capsule chambers are bounded by escape pressure instead.')));

  setChildren(host, head, verdictPanel, validationPanel, mapsPanel,
    el('div', { class: 'cols' }, findingsPanel, fixesPanel, chainPanel, modulesPanel, notePanel));

  paintAll();
  setChildren(mapsPanel, loading('Looking for the CFD output'));
  reload();

  offs.push(live.on('finding', () => { mergeLive(); paintVerdict(); paintFindings(); }));
  offs.push(live.on('done', () => reload()));
  offs.push(live.on('mode', () => paintVerdict()));

  function mergeLive() {
    const have = new Set(state.doc.findings.map((f) => f.id));
    for (const f of live.findings) if (!have.has(f.id)) { state.doc.findings.push(f); state.loaded = true; }
  }

  /* ---------------------------------------------------------------- loading */

  async function reload() {
    let artifacts = [];
    try { artifacts = await loadArtifacts(runId); }
    catch (err) {
      if (cancelled) return;
      setChildren(mapsPanel, errorState('Could not list this run\'s files', el('p', explain(err))));
      return;
    }
    if (cancelled) return;

    const files = indexCfdFiles(artifacts);
    let doc = null;
    if (files.doc) {
      const raw = await maybeJson(files.doc);
      if (raw) { doc = normaliseCfdDoc(raw); state.sourcePath = files.doc; }
    }
    if (!doc) {
      const [findings, fixes, pipeline] = await Promise.all([
        maybeJson(files.findings), maybeJson(files.fixes), maybeJson(files.pipeline),
      ]);
      if (findings || fixes || pipeline) {
        doc = cfdDocFromParts({ findings, fixes, pipeline });
        state.sourcePath = files.findings || files.fixes || files.pipeline;
      }
    }
    if (cancelled) return;

    // Figures can exist on disk before cfd.json lists them.
    if (doc && files.figures.length) {
      for (const c of doc.chips) {
        if (c.figures.length) continue;
        const mine = files.figures.filter((p) => p.split('/').pop().startsWith(`${c.chip_id}_`));
        if (mine.length) { c.figures = mine; c.figuresByStep = indexFigures(mine); }
      }
    }

    state.loaded = !!doc;
    state.doc = doc || emptyCfdDoc();
    mergeLive();
    if (state.doc.chips.length) {
      state.chipIndex = Math.min(state.chipIndex, state.doc.chips.length - 1);
      const chip = state.doc.chips[state.chipIndex];
      if (!chip.steps.some((s) => s.key === state.stepKey)) state.stepKey = chip.steps[0] ? chip.steps[0].key : null;
    }
    paintAll();
  }

  async function maybeJson(path) {
    if (!path) return null;
    try { return await api.fileJson(runId, path); }
    catch (_) { return null; }
  }

  function paintAll() {
    paintVerdict();
    paintValidation();
    paintMaps();
    paintFindings();
    paintFixes();
    paintChain();
    paintModules();
  }

  /* ---------------------------------------------------------------- verdict */

  function paintVerdict() {
    const findings = state.doc.findings;
    const v = verdict(findings);
    const c = v.counts;
    const body = clear(verdictPanel);
    const running = live.manifest && live.manifest.status === 'running';
    const val = state.doc.validation;
    // "no findings" is a real result; "nothing to read" is not. Never conflate them.
    const unknown = !state.loaded && !findings.length;
    const untrusted = val && (val.trusted === false || val.status === 'fail');
    const word = unknown ? (running ? 'still solving' : 'not known - no analysis to read') : v.word;
    const colour = unknown ? '#9aa8c2' : v.kind === 'bad' ? '#fca5a5' : v.kind === 'warn' ? '#fde68a' : '#86efac';

    body.appendChild(el('div', { class: 'row' },
      el('h2', { style: { margin: 0 } }, 'Verdict: '),
      el('strong', { style: { fontSize: '18px', color: colour } }, word),
      unknown ? null : simBadge(),
      untrusted ? badge('SOLVER NOT VALIDATED', 'bad', 'The solver\'s own benchmarks did not pass, so these findings are not trustworthy.') : null,
      el('span', { class: 'spacer' }),
      running ? badge('run in progress', 'live') : null,
      state.doc.mode ? badge(`mode: ${state.doc.mode}`, '', 'chip = one chip standalone, pipeline = the whole chain') : null));

    if (state.doc.title) body.appendChild(el('p', { class: 'muted', style: { marginTop: '6px' } }, state.doc.title));

    if (unknown) {
      body.appendChild(el('p', { class: 'muted', style: { marginTop: '8px' } },
        running
          ? 'S10 is still running. Findings appear here as they stream in.'
          : 'No CFD output is readable for this run and nothing streamed, so this page cannot say whether the chain flows. That is not the same as a clean result.'));
      body.appendChild(el('p', {}, el('button', { type: 'button', onclick: () => ctx.navigate('run', runId) }, 'Open the stage timeline')));
      return;
    }

    body.appendChild(el('div', { class: 'counters', style: { marginTop: '10px' } },
      ...SEVERITIES.map((s) => el('button', {
        type: 'button', class: 'ct',
        'aria-pressed': String(state.filterSev === s),
        onclick: () => { state.filterSev = state.filterSev === s ? '' : s; paintFindings(); paintVerdict(); },
        title: `Filter the table to ${s} findings`,
      }, el('b', String(c[s])), severityBadge(s)))));

    if (c.blocker) {
      body.appendChild(el('p', { style: { marginTop: '10px', color: '#fecaca' } },
        `${c.blocker} blocker${c.blocker === 1 ? '' : 's'}: the protocol would not run on this chip set as drawn. `
        + 'Each one below carries the two numbers that disagree.'));
    }
    for (const n of state.doc.notes) body.appendChild(el('p', { class: 'small muted' }, n));
    if (state.doc.generated_at) {
      body.appendChild(el('p', { class: 'small muted' }, `Solved ${fmtTime(state.doc.generated_at)}`,
        state.sourcePath ? el('span', { class: 'mono' }, ` - ${state.sourcePath}`) : null));
    }
  }

  /* ---------------------------------------------------------------- validation */

  function paintValidation() {
    const v = state.doc.validation;
    const body = clear(validationPanel);
    body.appendChild(el('div', { class: 'row' },
      el('h3', { style: { margin: 0 } }, 'Solver validation'),
      v ? badge(v.status, v.status === 'pass' ? 'ok' : 'bad') : todoBadge(),
      v && v.trusted === true ? badge('findings trustworthy', 'ok') : null,
      v && v.trusted === false ? badge('findings NOT trustworthy', 'bad') : null));
    if (!v) {
      body.appendChild(el('p', { class: 'muted', style: { marginTop: '8px' } },
        'This run carries no validation block. A solver nobody has checked is worse than no solver, '
        + 'so treat the numbers above as unverified until assay2chip/cfd/validate.py reports here.'));
      return;
    }
    body.appendChild(el('p', { class: 'small muted', style: { marginTop: '8px' } },
      `${Number.isFinite(v.pass) ? v.pass : '?'} passed, ${Number.isFinite(v.fail) ? v.fail : '?'} failed`
      + `${Number.isFinite(v.skipped) && v.skipped ? `, ${v.skipped} skipped` : ''}`
      + `${v.recommended_grid_mm ? ` - recommended grid ${fmtNum(v.recommended_grid_mm, 3)} mm` : ''}`));
    if (v.solver_available === false) {
      body.appendChild(el('p', { style: { color: '#fde68a' } }, `Solver unavailable: ${v.solver_reason || 'no reason given'}`));
    }
    if (v.checks.length) {
      body.appendChild(el('details', { class: 'disclose', style: { marginTop: '8px' } },
        el('summary', `${v.checks.length} benchmark checks`),
        el('div', { class: 'scroll-x' }, el('table', { class: 'data' },
          el('thead', {}, el('tr', {}, el('th', 'Check'), el('th', { class: 'num' }, 'Tolerance'), el('th', { class: 'num' }, 'Measured'), el('th', 'Result'))),
          el('tbody', {}, ...v.checks.map((c) => el('tr', {},
            el('td', { class: 'small' }, c.name, c.detail ? el('div', { class: 'muted' }, c.detail) : null),
            el('td', { class: 'num' }, c.expected === null || c.expected === undefined ? '-' : fmtNum(c.expected, 3)),
            el('td', { class: 'num' }, c.got === null || c.got === undefined ? '-' : fmtNum(c.got, 3)),
            el('td', {}, c.ok ? badge('pass', 'ok') : badge(c.status || 'fail', 'bad')))))))));
    }
    if (v.findings.length) {
      body.appendChild(el('details', { class: 'disclose' },
        el('summary', `${v.findings.length} findings about the model itself`),
        el('ul', { class: 'small', style: { paddingLeft: '20px' } },
          ...v.findings.map((f) => el('li', {}, severityBadge(f.severity), ' ', f.message || f.id)))));
    }
  }

  /* ---------------------------------------------------------------- maps */

  function paintMaps() {
    const chips = state.doc.chips;
    if (!chips.length) {
      setChildren(mapsPanel, emptyState('No field maps yet',
        state.loaded
          ? el('p', 'The CFD document has no chips in it, so there are no per-step maps to show.')
          : el('p', 'S10 writes cfd/cfd.json and cfd/figures/*.svg. Neither is in this run\'s artifact list yet.'),
        el('p', {}, el('button', { type: 'button', onclick: () => ctx.navigate('run', runId) }, 'Open the stage timeline'))));
      return;
    }

    const chip = chips[Math.min(state.chipIndex, chips.length - 1)];
    const chipBar = el('div', { class: 'segment', role: 'tablist', 'aria-label': 'Chip' },
      ...chips.map((c, i) => el('button', {
        type: 'button', role: 'tab', 'aria-selected': String(i === state.chipIndex), 'aria-pressed': String(i === state.chipIndex),
        onclick: () => {
          state.chipIndex = i;
          state.stepKey = chips[i].steps[0] ? chips[i].steps[0].key : null;
          paintMaps();
        },
      }, c.chip_id)));

    const step = chip.steps.find((s) => s.key === state.stepKey) || chip.steps[0] || null;
    const stepBar = chip.steps.length
      ? el('div', { class: 'segment', role: 'tablist', 'aria-label': 'Protocol step', style: { flexWrap: 'wrap' } },
        ...chip.steps.map((s) => el('button', {
          type: 'button', role: 'tab',
          'aria-selected': String(s.key === (step && step.key)), 'aria-pressed': String(s.key === (step && step.key)),
          title: s.label,
          onclick: () => { state.stepKey = s.key; paintMaps(); },
        }, `step ${s.step}`)))
      : el('span', { class: 'small muted' }, 'the solver recorded no protocol steps for this chip');

    const head2 = el('section', { class: 'panel' },
      el('div', { class: 'row' },
        el('h3', { style: { margin: 0 } }, 'Field maps'), simBadge(),
        chip.scale === 'demo' ? demoBadge() : null,
        el('span', { class: 'spacer' }), chipBar),
      el('div', { class: 'row', style: { marginTop: '10px' } },
        el('span', { class: 'small muted' }, 'Protocol step'), stepBar),
      step ? stepDetail(step) : null,
      meshLine(chip));

    const figs = step ? (chip.figuresByStep.get(String(step.step)) || chip.figuresByStep.get('all')) : null;
    const panels = METRICS.map(([key, title]) => mapPanel(title, figs ? figs.get(key) : null, chip, step));
    setChildren(mapsPanel, head2, el('div', { class: 'cols c-2', style: { marginTop: '16px' } }, ...panels));
  }

  function stepDetail(step) {
    const bits = el('dl', { class: 'kv', style: { marginTop: '10px' } });
    const add = (k, v) => { if (v !== null && v !== undefined && v !== '') { bits.appendChild(el('dt', k)); bits.appendChild(el('dd', {}, v)); } };
    add('step', String(step.label));
    add('operation', step.operation);
    add('targets', step.targets.join(', '));
    add('flow', step.flow_ul_min !== null && step.flow_ul_min !== undefined ? simNum(step.flow_ul_min, 'uL/min') : null);
    add('temperature', step.temp_c !== null && step.temp_c !== undefined ? `${fmtNum(step.temp_c, 4)} C` : null);
    add('viscosity', step.viscosity_pa_s ? `${fmtNum(step.viscosity_pa_s, 3)} Pa s` : null);
    add('from', step.cites);
    const flags = el('div', { class: 'row', style: { gap: '6px', marginTop: '8px' } });
    if (step.inferred) {
      flags.appendChild(badge('INFERRED STEP', 'warn',
        'The solver worked this scenario out from the geometry; the protocol did not state it outright.'));
    }
    if (step.ambiguous) flags.appendChild(badge('AMBIGUOUS', 'bad', 'The protocol could be read more than one way here.'));
    return el('div', {}, bits, flags.childElementCount ? flags : null);
  }

  function meshLine(chip) {
    const m = chip.mesh;
    if (!Number.isFinite(m.nodes)) return null;
    return el('p', { class: 'hint', style: { marginTop: '8px' } },
      `Mesh: ${m.nodes.toLocaleString()} cells, ${Number.isFinite(m.edges) ? m.edges.toLocaleString() : '?'} edges`
      + `${Number.isFinite(m.grid_mm) ? `, grid ${fmtNum(m.grid_mm, 3)} mm` : ''}`
      + `${Number.isFinite(m.mu_pa_s) ? `, mu ${fmtNum(m.mu_pa_s, 3)} Pa s` : ''}.`
      + (m.status && m.status !== 'ok' ? ` Mesh status: ${m.status}.` : ''));
  }

  function mapPanel(title, path, chip, step) {
    if (!path) {
      return el('div', { class: 'panel' },
        el('h3', {}, title, ' ', badge('no figure', 'todo')),
        el('p', { class: 'muted small' },
          step
            ? `assay2chip/cfd/figures.py has not written a ${title.toLowerCase()} figure for ${chip.chip_id} step ${step.step}.`
            : 'No step selected.'));
    }
    const src = api.fileUrlLoose(runId, path);
    const img = el('img', {
      src, alt: `${title} map for ${chip.chip_id}${step ? ` step ${step.step}` : ''} - simulated`,
      loading: 'lazy',
    });
    // An SVG loaded through <img> cannot run script, so an untrusted figure stays inert.
    img.addEventListener('error', () => {
      const fig = img.parentElement;
      if (fig) setChildren(fig, el('p', { class: 'muted small' }, `could not load ${path}`));
    });
    const panel = el('div', { class: 'panel' },
      el('h3', {}, title, ' ', simBadge()),
      el('div', { class: 'figs' }, el('figure', {}, img, el('figcaption', path.split('/').pop()))),
      el('p', { class: 'hint' },
        'Depth-averaged Hele-Shaw solve. Hele-Shaw has no recirculation, so a trap here means a '
        + 'low-velocity or long-residence zone, not a closed streamline.'));
    checkFigure(panel, path, title);
    return panel;
  }

  /**
   * figures.py writes a placeholder SVG when it cannot draw a map. Rendered as an image
   * that is an unreadable white box, so read the file and replace it with the reason.
   * The SVG is parsed and only its TEXT is used - no node from it enters this document.
   */
  async function checkFigure(panel, path, title) {
    let text;
    try { text = await api.fileText(runId, path); }
    catch (_) { return; }
    if (cancelled || !/figure (failed|unavailable)|could not/i.test(text)) return;
    let message = '';
    try {
      const doc = new DOMParser().parseFromString(text, 'image/svg+xml');
      message = [...doc.querySelectorAll('text')].map((n) => n.textContent.trim())
        .filter(Boolean).slice(1).join(' ').slice(0, 400);
    } catch (_) { /* fall through to the generic wording */ }
    const h3 = panel.querySelector('h3');
    if (h3) setChildren(h3, title, ' ', badge('FIGURE FAILED', 'bad', 'figures.py could not draw this map.'));
    const figs = panel.querySelector('.figs');
    if (figs) {
      setChildren(figs, el('p', { class: 'small', style: { color: '#fecaca' } },
        message || `assay2chip/cfd/figures.py could not draw the ${title.toLowerCase()} map.`),
      el('p', { class: 'small muted mono' }, path.split('/').pop()));
    }
  }

  /* ---------------------------------------------------------------- findings */

  function paintFindings() {
    const findings = state.doc.findings;
    const body = clear(findingsPanel);
    body.appendChild(el('div', { class: 'row' },
      el('h3', { style: { margin: 0 } }, `Findings (${findings.length})`), simBadge()));

    if (!findings.length) {
      body.appendChild(el('p', { class: 'muted', style: { marginTop: '10px' } },
        live.manifest && live.manifest.status === 'running'
          ? 'None yet. Findings stream in as each chip is analysed.'
          : state.loaded
            ? 'The analysis ran and found nothing at any severity.'
            : 'No findings file and none streamed. Either the analysis has not run or it could not be read.'));
      return;
    }

    const classes = [...new Set(findings.map((f) => f.cls))].sort();
    const chips = [...new Set(findings.map((f) => f.chip_id).filter(Boolean))].sort();
    const counts = countSeverity(findings);

    body.appendChild(el('div', { class: 'row', style: { margin: '10px 0' } },
      el('div', { class: 'segment', role: 'radiogroup', 'aria-label': 'Severity' },
        ...['', ...SEVERITIES].map((s) => el('button', {
          type: 'button', role: 'radio', 'aria-checked': String(state.filterSev === s), 'aria-pressed': String(state.filterSev === s),
          onclick: () => { state.filterSev = s; paintFindings(); paintVerdict(); },
        }, s ? `${s} (${counts[s]})` : `all (${findings.length})`))),
      el('label', { class: 'row', style: { gap: '6px' } },
        el('span', { class: 'small muted' }, 'Class'),
        el('select', { onchange: (e) => { state.filterClass = e.target.value; paintFindings(); } },
          el('option', { value: '' }, 'any'),
          ...classes.map((c) => el('option', { value: c, selected: c === state.filterClass || null }, c)))),
      chips.length > 1
        ? el('label', { class: 'row', style: { gap: '6px' } },
          el('span', { class: 'small muted' }, 'Chip'),
          el('select', { onchange: (e) => { state.filterChip = e.target.value; paintFindings(); } },
            el('option', { value: '' }, 'any'),
            ...chips.map((c) => el('option', { value: c, selected: c === state.filterChip || null }, c))))
        : null));

    const shown = findings.filter((f) =>
      (!state.filterSev || f.severity === state.filterSev)
      && (!state.filterClass || f.cls === state.filterClass)
      && (!state.filterChip || f.chip_id === state.filterChip));
    const order = { blocker: 0, high: 1, medium: 2, low: 3 };
    shown.sort((a, b) => order[a.severity] - order[b.severity]);

    const fixById = new Map(state.doc.fixes.map((x) => [x.finding_id, x]));
    body.appendChild(el('div', { class: 'scroll-x' }, el('table', { class: 'data' },
      el('caption', { title: SIM_TITLE }, 'Each row gives the two numbers that disagree. All simulated.'),
      el('thead', {}, el('tr', {},
        el('th', 'Severity'), el('th', 'Class'), el('th', 'Chip / feature'), el('th', 'Step'),
        el('th', 'Metric'), el('th', { class: 'num' }, 'Value'), el('th', { class: 'num' }, 'Limit'),
        el('th', 'What it means'), el('th', 'Fix'))),
      el('tbody', {}, ...(shown.length ? shown.map((f) => findingRow(f, fixById.get(f.id))) : [
        el('tr', {}, el('td', { colspan: '9', class: 'muted' }, 'Nothing matches that filter.')),
      ])))));
  }

  function findingRow(f, fix) {
    return el('tr', { dataset: { sev: f.severity } },
      el('td', {}, severityBadge(f.severity)),
      el('td', { class: 'mono small' }, f.cls),
      el('td', { class: 'small' },
        f.chip_id ? el('div', { class: 'mono' }, f.chip_id) : null,
        f.feature_id ? el('div', { class: 'muted mono' }, f.feature_id) : null),
      el('td', { class: 'num small' }, f.step === null || f.step === undefined ? '-' : String(f.step)),
      el('td', { class: 'small mono', style: { maxWidth: '22ch', overflowWrap: 'anywhere' } }, f.metric || '-'),
      el('td', { class: 'num' }, simNum(f.value, f.units)),
      el('td', { class: 'num' }, f.limit === null || f.limit === undefined
        ? el('span', { class: 'muted' }, 'unknown')
        : el('span', { class: 'num' }, `${typeof f.limit === 'number' ? fmtNum(f.limit, 3) : String(f.limit)}${f.units ? ` ${f.units}` : ''}`)),
      el('td', { style: { maxWidth: '38ch' } }, f.message || ''),
      el('td', { class: 'small' },
        fix
          ? el('span', {},
            fix.applied ? badge('applied', 'ok') : fix.auto_applicable ? badge('auto-applicable', 'info') : badge('needs a human', 'warn'),
            el('div', { class: 'muted' }, fix.action))
          : el('span', { class: 'muted' }, f.fix_hint || 'none proposed')));
  }

  /* ---------------------------------------------------------------- fixes */

  function paintFixes() {
    const fixes = state.doc.fixes;
    const body = clear(fixesPanel);
    body.appendChild(el('div', { class: 'row' },
      el('h3', { style: { margin: 0 } }, `Fixes (${fixes.length})`), simBadge()));
    if (!fixes.length) {
      body.appendChild(el('p', { class: 'muted', style: { marginTop: '10px' } },
        'No fixes proposed. A fix is only applied automatically when it keeps every fabrication floor, '
        + 'introduces no channel crossing, and the re-run shows the finding gone with no new blocker.'));
      return;
    }
    const applied = fixes.filter((x) => x.applied).length;
    const manual = fixes.filter((x) => !x.auto_applicable).length;
    body.appendChild(el('p', { class: 'small muted', style: { marginTop: '6px' } },
      `${applied} applied automatically to a copy, ${manual} left for a human. `
      + 'Before and after are both predictions from the solver, never measurements. '
      + 'A hand-designed repo chip is never modified in place.'));

    const byFinding = new Map(state.doc.findings.map((f) => [f.id, f]));
    body.appendChild(el('div', { class: 'scroll-x', style: { marginTop: '10px' } }, el('table', { class: 'data' },
      el('thead', {}, el('tr', {},
        el('th', 'Finding'), el('th', 'Action'), el('th', 'Target'), el('th', 'Metric'),
        el('th', { class: 'num' }, 'Before'), el('th', { class: 'num' }, 'After'),
        el('th', 'Applied'), el('th', { class: 'num' }, 'Verified'))),
      el('tbody', {}, ...fixes.map((x) => {
        const f = byFinding.get(x.finding_id);
        return el('tr', {},
          el('td', { class: 'small' },
            f ? severityBadge(f.severity) : null,
            el('div', { class: 'mono muted', style: { overflowWrap: 'anywhere', maxWidth: '18ch' } }, x.finding_id || '-')),
          el('td', { class: 'small' }, x.action || '-'),
          el('td', { class: 'small mono' }, x.target || '-'),
          el('td', { class: 'small mono', style: { maxWidth: '20ch', overflowWrap: 'anywhere' } }, x.metric || '-'),
          el('td', { class: 'num' }, simNum(x.before, x.units)),
          el('td', { class: 'num' }, simNum(x.after, x.units)),
          el('td', {}, x.applied ? badge('applied', 'ok', 'applied to a copy in the work directory')
            : x.auto_applicable ? badge('could be', 'info') : badge('no', 'warn', 'Reported for a human instead.')),
          el('td', { class: 'num' }, x.verified_after === null || x.verified_after === undefined
            ? el('span', { class: 'muted' }, 'not re-run')
            : simNum(x.verified_after, x.units)));
      })))));
  }

  /* ---------------------------------------------------------------- chain */

  function paintChain() {
    const body = clear(chainPanel);
    body.appendChild(el('div', { class: 'row' },
      el('h3', { style: { margin: 0 } }, 'Pipeline overview'), simBadge()));

    const chips = state.doc.chips;
    const handoffs = state.doc.handoffs;

    if (!chips.length) {
      body.appendChild(el('p', { class: 'muted', style: { marginTop: '10px' } },
        'No chips in the CFD document, so there is no chain to draw.'));
      return;
    }

    const chain = el('div', { class: 'chain', style: { marginTop: '10px' } });
    chips.forEach((c, i) => {
      const counts = countSeverity(c.findings.length ? c.findings : state.doc.findings.filter((f) => f.chip_id === c.chip_id));
      chain.appendChild(el('div', { class: 'node' },
        el('h5', {}, String(i + 1), '. ', c.module ? String(c.module) : c.chip_id),
        el('span', { class: 'id' }, c.chip_id),
        c.scale === 'demo' ? el('div', { style: { marginTop: '4px' } }, demoBadge()) : null,
        c.footprint ? el('div', { class: 'small muted', style: { marginTop: '4px' } },
          `${fmtNum(c.footprint.x, 5)} x ${fmtNum(c.footprint.y, 5)} mm`) : null,
        el('div', { class: 'row', style: { gap: '4px', marginTop: '6px' } },
          ...SEVERITIES.filter((s) => counts[s]).map((s) => el('span', {}, el('b', { class: 'num' }, String(counts[s])), ' ', severityBadge(s))),
          Object.values(counts).every((n) => !n) ? badge('clean', 'ok') : null),
        c.steps.length ? el('div', { class: 'small muted', style: { marginTop: '4px' } },
          `${c.steps.length} protocol step${c.steps.length === 1 ? '' : 's'} solved`) : null));
      const h = handoffs.find((x) => x.from === c.chip_id);
      if (h && i < chips.length - 1) {
        chain.appendChild(el('div', { class: `link${h.automated === false ? ' manual' : ''}` },
          el('div', { class: 'arrow' }, '\u2192'),
          el('div', {}, h.carries || 'tubing'),
          h.dead_volume_ul !== undefined && h.dead_volume_ul !== null ? el('div', {}, 'dead ', simNum(h.dead_volume_ul, 'uL')) : null,
          h.automated === false ? el('div', {}, badge('needs a human', 'bad')) : null));
      }
    });
    body.appendChild(chain);

    if (handoffs.length) {
      const manual = handoffs.filter((h) => h.automated === false).length;
      body.appendChild(el('p', { class: 'small', style: { marginTop: '10px' } },
        `${handoffs.length} handoff${handoffs.length === 1 ? '' : 's'}, `,
        manual
          ? el('strong', { style: { color: '#fca5a5' } }, `${manual} that a machine cannot do`)
          : el('strong', { style: { color: '#86efac' } }, 'all automated'),
        '. The stated goal is no human intervention.'));
      body.appendChild(el('div', { class: 'scroll-x', style: { marginTop: '8px' } }, el('table', { class: 'data' },
        el('caption', 'Tubing between chips: dead volume and the pressure it costs. Simulated.'),
        el('thead', {}, el('tr', {},
          el('th', 'From'), el('th', 'To'), el('th', 'Carries'),
          el('th', { class: 'num' }, 'Volume'), el('th', { class: 'num' }, 'Dead volume'), el('th', { class: 'num' }, 'Pressure'))),
        el('tbody', {}, ...handoffs.map((h) => el('tr', {},
          el('td', { class: 'mono small' }, `${h.from}${h.from_port ? `.${h.from_port}` : ''}`),
          el('td', { class: 'mono small' }, `${h.to}${h.to_port ? `.${h.to_port}` : ''}`),
          el('td', { class: 'small' }, h.carries),
          el('td', { class: 'num' }, h.volume_ul === undefined || h.volume_ul === null ? '-' : simNum(h.volume_ul, 'uL')),
          el('td', { class: 'num' }, h.dead_volume_ul === undefined || h.dead_volume_ul === null ? '-' : simNum(h.dead_volume_ul, 'uL')),
          el('td', { class: 'num' }, h.pressure_kpa === undefined || h.pressure_kpa === null ? '-' : simNum(h.pressure_kpa, 'kPa'))))))));
    } else if (chips.length > 1) {
      body.appendChild(el('p', { class: 'muted small', style: { marginTop: '10px' } },
        'The chips were solved individually; no handoffs were coupled, so the pressure a chip sees '
        + 'from the one upstream of it is not in these numbers.'));
    }

    if (state.doc.raw) {
      body.appendChild(el('details', { class: 'disclose', style: { marginTop: '10px' } },
        el('summary', 'Raw CFD document'), renderJson(el('div'), state.doc.raw)));
    }
  }

  /* ---------------------------------------------------------------- modules */

  function paintModules() {
    const mods = state.doc.modules;
    const body = clear(modulesPanel);
    body.appendChild(el('h3', { style: { marginTop: 0 } }, 'CFD modules'));
    if (!mods.length) {
      body.appendChild(el('p', { class: 'muted' },
        'The CFD document reports no per-module status, so which parts of the solver actually ran is unknown.'));
      return;
    }
    const broken = mods.filter((m) => m.broken || m.missing).length;
    body.appendChild(el('p', { class: 'small muted' },
      broken
        ? `${broken} of ${mods.length} modules did not do their job on this run, so the findings below them are incomplete.`
        : `All ${mods.length} modules reported ok. A module marked "not needed" contributed nothing to these numbers.`));
    body.appendChild(el('div', { class: 'scroll-x' }, el('table', { class: 'data' },
      el('thead', {}, el('tr', {}, el('th', 'Module'), el('th', 'Status'), el('th', { class: 'num' }, 'Calls'), el('th', 'Detail'))),
      el('tbody', {}, ...mods.map((m) => el('tr', { dataset: { sev: m.broken || m.missing ? 'high' : '' } },
        el('td', { class: 'mono small' }, m.name),
        el('td', {}, m.missing ? todoBadge()
          : m.broken ? badge('errors', 'bad')
            : m.unused ? badge('not needed', '', 'Reported ok but never called on this run, so it produced nothing.')
              : badge('ran', 'ok')),
        el('td', { class: 'num' }, Number.isFinite(m.calls) ? String(m.calls) : '-'),
        el('td', { class: 'small muted' }, m.detail,
          m.errors.length ? el('ul', { style: { margin: '4px 0 0', paddingLeft: '18px', color: '#fca5a5' } },
            ...m.errors.slice(0, 5).map((e) => el('li', e))) : null)))))));
  }

  return () => { cancelled = true; for (const off of offs) off(); };
}

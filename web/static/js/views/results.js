// View 3: RESULTS. One run at a time: the verdict, the chips it made (plan and tilted
// views), the flow maps, the findings ranked by severity with their fixes, the report
// and the video slot per printed module. The old detailed views stay reachable, folded
// away at the bottom, so nothing they showed is lost.

import { api, ApiError, explain, download } from '../api.js';
import { el, svg, clear, setChildren, badge, toast, fmtNum, fmtTime, loading, emptyState } from '../dom.js';
import {
  simBadge, demoBadge, stubBadge, todoBadge, printedBadge, isDemoScale, SEVERITIES, SIM_TITLE,
} from '../labels.js';
import { normaliseManifest, countSeverity, verdict } from '../model.js';
import { loadArtifacts, exportsByChip } from '../locate.js';
import { indexCfdFiles, normaliseCfdDoc, cfdDocFromParts, emptyCfdDoc, indexFigures } from '../cfddoc.js';
import { loadDocsJson, normModulesDoc, normVideoManifest, playableUrl } from '../ourassay.js';
import { chipPlan } from '../chipsvg.js';
import { chipIso } from '../chipiso.js';
import { StlViewer, PALETTE } from '../stl.js';
import { markdownBlock } from '../markdown.js';
import { term, classTerm } from '../glossary.js';
import { sevBar } from './runit.js';

import * as runView from './run.js';
import * as stepsView from './steps.js';
import * as chipsView from './chips.js';
import * as cfdView from './cfd.js';
import * as reportView from './report.js';

const SEV_WORD = { blocker: 'Blocker', high: 'High', medium: 'Medium', low: 'Low' };
const SEV_MEANS = {
  blocker: 'would stop the protocol working',
  high: 'likely to spoil the result',
  medium: 'worth fixing',
  low: 'minor',
};

const MAPS = [
  ['pressure', 'Pressure', 'How hard the pumps must push. A sudden jump marks a narrow restriction.'],
  ['speed', 'Speed', 'How fast the liquid moves. Slow, dark corners are where old liquid lingers.'],
  ['shear', 'Wall shear', 'The drag on the floor where cells sit, against the ceiling for this step.'],
  ['residence', 'Residence time', 'How long liquid stays in each spot. Long times mean a wash does not reach there.'],
];

const FIX_WORDS = {
  move_outlet: 'Move the outlet',
  add_flow_distributor: 'Add a flow distributor at the inlet',
  add_pillar_row: 'Add a row of pillars to spread the flow',
  change_aspect_ratio: 'Change the chamber\'s width-to-depth ratio',
  add_vent: 'Add an air vent',
  taper_expansion: 'Taper the widening so the liquid front does not stick',
  raise_flow: 'Increase the flow rate',
  shorten_line: 'Shorten the tubing',
  vertical_mounting: 'Mount the chip on its side',
  set_flow: 'Change the flow rate',
  widen_chamber: 'Widen the chamber',
};

const TITLE_BY_CLASS = {
  shear: (f) => `Too much shear on ${f}`,
  trap: (f) => `Liquid lingers in ${f}`,
  bubble: (f) => `Air could be trapped in ${f}`,
  sediment: (f) => `Particles could settle in ${f}`,
  leak: (f) => `Liquid could escape at ${f}`,
  clog: (f) => `Could clog at ${f}`,
};

export function mount(host, ctx) {
  let cancelled = false;
  const unmounts = [];
  const viewers = new Set();
  const runId = ctx.runId;

  const picker = runPicker(ctx, runId);
  const top = el('section', { class: 'hero hero-tight' },
    el('p', { class: 'eyebrow' }, 'Results'),
    el('h1', { class: 'display' }, 'What one run produced, and what would go wrong.'),
    el('p', { class: 'lede' }, 'The chips it designed, the flow simulation through them, every problem it found ranked by how serious it is with a proposed fix, its report, and the bench recordings.'),
    picker);

  if (!runId) {
    setChildren(host, top, emptyState('No run to show yet',
      el('p', {}, 'Start one on the Run it page, or run the flow simulation on the existing chain.'),
      el('p', {}, el('a', { class: 'button primary', href: '#/run' }, 'Go to Run it'))));
    return () => {};
  }

  const meta = el('div', { class: 'run-meta' });
  const onPage = el('nav', { class: 'on-page', 'aria-label': 'On this page' },
    ...[['verdict', 'Verdict'], ['chips', 'Chips'], ['maps', 'Flow maps'], ['findings', 'Findings'], ['report', 'Report'], ['videos', 'Recordings'], ['more', 'Everything else']]
      .map(([id, w]) => el('a', {
        href: `#res-${id}`,
        onclick: (e) => { e.preventDefault(); document.getElementById(`res-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' }); },
      }, w)));

  const S = {
    verdict: section('verdict', 'Will it flow?', 'The flow simulation\'s overall answer, and where the problems are.'),
    chips: section('chips', 'The chips it made', 'Each chip drawn from its design file. One flat layer at one depth, so the view from above is the whole design.'),
    maps: section('maps', 'Flow simulation maps', 'What the liquid does inside one chip during one protocol step, coloured from low to high.'),
    findings: section('findings', 'Findings, most serious first', 'Every place the simulation says the chip and the protocol disagree, with the two numbers and a proposed fix.'),
    report: section('report', 'Report', 'The run\'s own written summary: problems, fixes and the files it made.'),
    videos: section('videos', 'Bench recordings', 'One slot per printed chip: a real recording of dyed water run through it, once filmed.'),
    more: section('more', 'Everything else', 'The full detail behind this page, for when you need the exact files and numbers.'),
  };
  for (const s of Object.values(S)) s.body.appendChild(loading('Loading'));

  setChildren(host, top, meta, onPage, ...Object.values(S).map((s) => s.el));

  const data = { manifest: null, artifacts: [], cfd: emptyCfdDoc(), cfdLoaded: false, modules: null, videos: null, chips: [] };
  const ui = { chipIndex: 0, stepKey: null, sev: '', chip: '', showAll: false };

  (async () => {
    const [man, arts, mods, vids] = await Promise.all([
      api.run(runId).then((r) => normaliseManifest(r, runId)).catch((e) => e),
      loadArtifacts(runId).catch(() => []),
      loadDocsJson('docs/modules.json', ctx.fileRunId).catch(() => ({ missing: true })),
      loadDocsJson('docs/videos/manifest.json', ctx.fileRunId).catch(() => ({ missing: true })),
    ]);
    if (cancelled) return;
    if (man instanceof Error) {
      for (const s of Object.values(S)) clear(s.body);
      setChildren(S.verdict.body, emptyState('This run could not be read', el('p', {}, explain(man))));
      return;
    }
    data.manifest = man;
    data.artifacts = arts || [];
    try { data.modules = mods.missing ? null : normModulesDoc(mods.data); } catch (_) { data.modules = null; }
    try { data.videos = vids.missing ? null : normVideoManifest(vids.data); } catch (_) { data.videos = null; }
    await loadCfd();
    if (cancelled) return;
    data.chips = chipIds();
    paintMeta();
    paintVerdict();
    paintChips();
    paintMaps();
    paintFindings();
    paintReport();
    paintVideos();
    paintMore();
  })().catch((err) => {
    console.error(err);
    if (!cancelled) setChildren(S.verdict.body, emptyState('Something went wrong reading this run', el('p', {}, explain(err))));
  });

  return () => {
    cancelled = true;
    for (const u of unmounts) { try { u(); } catch (_) {} }
    for (const v of viewers) { try { v.dispose(); } catch (_) {} }
  };

  /* ---------------------------------------------------------------- data */

  async function loadCfd() {
    const files = indexCfdFiles(data.artifacts);
    const json = async (p) => { if (!p) return null; try { return await api.fileJson(runId, p); } catch (_) { return null; } };
    let doc = null;
    if (files.doc) { const raw = await json(files.doc); if (raw) doc = normaliseCfdDoc(raw); }
    if (!doc) {
      const [findings, fixes, pipeline] = await Promise.all([json(files.findings), json(files.fixes), json(files.pipeline)]);
      if (findings || fixes || pipeline) doc = cfdDocFromParts({ findings, fixes, pipeline });
    }
    if (doc && files.figures.length) {
      for (const c of doc.chips) {
        if (c.figures.length) continue;
        const mine = files.figures.filter((p) => p.split('/').pop().startsWith(`${c.chip_id}_`));
        if (mine.length) { c.figures = mine; c.figuresByStep = indexFigures(mine); }
      }
    }
    data.cfd = doc || emptyCfdDoc();
    data.cfdLoaded = !!doc;
  }

  function chipIds() {
    const ids = [];
    const add = (x) => { const id = typeof x === 'string' ? x : x && (x.chip_id || x.id); if (id && !ids.includes(String(id))) ids.push(String(id)); };
    for (const c of data.manifest.chips) add(c);
    for (const c of data.cfd.chips) add(c.chip_id);
    for (const a of data.artifacts) { const m = /^chip\/([^/]+)\.json$/.exec(a.path); if (m) add(m[1]); }
    return ids;
  }

  function printedIds() { return new Set(data.modules ? data.modules.printed.map((p) => p.chip_id) : []); }

  /* ---------------------------------------------------------------- meta */

  function paintMeta() {
    const m = data.manifest;
    setChildren(meta,
      el('div', { class: 'flags' },
        badge(statusWord(m.status), m.status === 'done' ? 'ok' : m.status === 'failed' ? 'bad' : ''),
        String(m.llm) === 'stub' ? stubBadge() : null,
        String(m.scale) === 'demo' || data.cfd.chips.some((c) => c.scale === 'demo') ? demoBadge() : null,
        data.cfdLoaded ? simBadge() : null),
      el('span', { class: 'cap' }, m.created ? `Started ${fmtTime(m.created)}` : ''),
      m.source && m.source.ref ? el('span', { class: 'cap' }, `from ${String(m.source.ref).slice(0, 80)}`) : null,
      el('span', { class: 'spacer' }),
      el('a', { href: `#/run/${encodeURIComponent(runId)}` }, 'Stage progress'));
  }

  /* ---------------------------------------------------------------- verdict */

  function paintVerdict() {
    const body = clear(S.verdict.body);
    const f = data.cfd.findings;
    if (!data.cfdLoaded && !f.length) {
      body.appendChild(el('div', { class: 'verdict unknown' },
        el('p', { class: 'v-word' }, 'Not known'),
        el('p', {}, 'This run has no flow simulation output, so this page cannot say whether the chips would work. That is not the same as a clean result.'),
        el('p', {}, el('a', { href: `#/run/${encodeURIComponent(runId)}` }, 'See which stages ran'))));
      return;
    }
    const v = verdict(f);
    const c = v.counts;
    const val = data.cfd.validation;
    const untrusted = val && (val.trusted === false || val.status === 'fail');
    const word = c.blocker ? 'No, not as drawn' : c.high ? 'Mostly, with serious problems' : f.length ? 'Yes, with minor notes' : 'Yes, nothing found';
    body.appendChild(el('div', { class: `verdict ${v.kind}` },
      el('div', { class: 'v-left' },
        el('p', { class: 'v-word' }, word),
        el('p', {}, c.blocker
          ? [`${c.blocker} `, term('blocker', c.blocker === 1 ? 'blocker' : 'blockers'), ' would stop the protocol working on these chips. Each comes with the two numbers that disagree and at least one fix.']
          : f.length ? `${f.length} findings, none of them blocking.` : 'The simulation found nothing at any severity.'),
        el('div', { class: 'flags' }, simBadge(), data.cfd.mode ? badge(data.cfd.mode === 'pipeline' ? 'whole chain' : data.cfd.mode, '', 'pipeline = every chip joined by its tubing; chip = one chip alone') : null)),
      el('div', { class: 'v-right' }, severityBars(c))));

    if (untrusted) {
      body.appendChild(el('div', { class: 'warn-box' },
        el('strong', {}, 'The flow model failed one of its own checks. '),
        el('span', {}, 'Before trusting any number it gives, the solver runs itself against known answers. '
          + `${Number.isFinite(val.pass) ? val.pass : '?'} passed and ${Number.isFinite(val.fail) ? val.fail : '?'} failed, so every finding below is marked untrusted until that is fixed.`),
        val.detail ? el('p', { class: 'cap' }, val.detail) : null));
    }

    // where the problems are: one row per chip, one square per finding
    const chips = [...new Set(f.map((x) => x.chip_id).filter(Boolean))];
    if (chips.length) {
      const order = { blocker: 0, high: 1, medium: 2, low: 3 };
      body.appendChild(el('div', { class: 'dist' },
        el('p', { class: 'eyebrow' }, 'Where the problems are'),
        el('p', { class: 'cap' }, 'One square per finding, coloured by severity. Click a chip to list only its findings.'),
        el('ul', { class: 'dist-rows' }, ...chips.map((id) => {
          const mine = f.filter((x) => x.chip_id === id).sort((a, b) => order[a.severity] - order[b.severity]);
          const cc = countSeverity(mine);
          return el('li', {},
            el('button', {
              type: 'button', class: 'dist-row',
              'aria-label': `${id}: ${mine.length} findings, ${cc.blocker} blocking. Show them.`,
              onclick: () => { ui.chip = id; ui.sev = ''; paintFindings(); S.findings.el.scrollIntoView({ behavior: 'smooth', block: 'start' }); },
            },
            el('span', { class: 'dist-name mono' }, id),
            el('span', { class: 'dist-sq', 'aria-hidden': 'true' }, ...mine.map((x) => el('i', { class: `sq sev-fill-${x.severity}`, title: `${x.severity}: ${x.cls} at ${x.feature_id}` }))),
            el('span', { class: 'dist-n' }, cc.blocker ? `${cc.blocker} of ${mine.length} blocking` : `${mine.length} found`)));
        }))));
    }

    const fixes = data.cfd.fixes;
    if (fixes.length) {
      const applied = fixes.filter((x) => x.applied).length;
      const auto = fixes.filter((x) => x.auto_applicable && !x.applied).length;
      body.appendChild(el('p', { class: 'fix-sum' },
        el('strong', {}, `${fixes.length} fixes proposed. `),
        `${applied} applied to a copy of the chip and re-checked, ${auto} more could be applied automatically, `
        + `${fixes.length - applied - auto} need a person to decide. The original chip files are never changed.`));
    }
  }

  function severityBars(c) {
    const total = SEVERITIES.reduce((a, k) => a + c[k], 0) || 1;
    const max = Math.max(1, ...SEVERITIES.map((k) => c[k]));
    return el('ul', { class: 'sev-bars', 'aria-label': 'Findings by severity' },
      ...SEVERITIES.map((k) => el('li', {},
        el('button', {
          type: 'button', class: 'sev-bar-btn', 'aria-label': `${c[k]} ${k} findings. Show them.`,
          onclick: () => { ui.sev = k; ui.chip = ''; paintFindings(); S.findings.el.scrollIntoView({ behavior: 'smooth', block: 'start' }); },
        },
        el('span', { class: 'sb-name' }, SEV_WORD[k], el('span', { class: 'sb-means' }, SEV_MEANS[k])),
        el('span', { class: 'sb-track' }, el('i', { class: `sev-fill-${k}`, style: { width: `${(100 * c[k]) / max}%` } })),
        el('span', { class: 'sb-n' }, String(c[k]), el('span', { class: 'sb-pct' }, ` ${Math.round((100 * c[k]) / total)}%`))))));
  }

  /* ---------------------------------------------------------------- chips */

  function paintChips() {
    const body = clear(S.chips.body);
    if (!data.chips.length) {
      body.appendChild(emptyState('No chips in this run',
        el('p', {}, data.manifest.status === 'failed' ? 'The run stopped before any chip was designed.' : 'No chip has been designed or simulated by this run yet.')));
      return;
    }
    const printed = printedIds();
    const exportsMap = exportsByChip(data.artifacts);
    if (!exportsMap.size) {
      body.appendChild(el('p', { class: 'cap chips-note' }, 'This run did not export printable 3D files, so each chip\'s tilted view is drawn from its design file instead. Depth is exaggerated so the 0.3 mm channels can be seen.'));
    }
    const grid = el('div', { class: 'chip-grid' });
    body.appendChild(grid);
    data.chips.forEach((id, i) => {
      const cfdChip = data.cfd.chips.find((c) => c.chip_id === id);
      const mine = data.cfd.findings.filter((x) => x.chip_id === id);
      const card = el('article', { class: 'chip-card' });
      const fig = el('div', { class: 'chip-fig' }, el('p', { class: 'loading' }, 'Drawing...'));
      const flags = el('div', { class: 'flags' });
      const name = el('p', { class: 'cap' });
      const actions = el('div', { class: 'chip-acts' });
      const extra = el('div', {});
      setChildren(card,
        el('header', {}, el('h3', { class: 'mono-title' }, id), name),
        flags, fig, actions, extra,
        mine.length ? el('p', { class: 'chip-sev' }, sevBar(countSeverity(mine))) : null);
      grid.appendChild(card);

      if (printed.has(id)) flags.appendChild(printedBadge());

      api.chip(id).then((spec) => {
        if (cancelled) return;
        const merged = { ...spec, scale: spec.scale || (cfdChip && cfdChip.scale) };
        if (isDemoScale(merged)) flags.appendChild(demoBadge());
        const fp = spec.footprint || {};
        const fx = Number(fp.x ?? fp.x_mm);
        const fy = Number(fp.y ?? fp.y_mm);
        if (Number.isFinite(fx) && Number.isFinite(fy)) flags.appendChild(el('span', { class: 'cap mono' }, `${fmtNum(fx, 5)} × ${fmtNum(fy, 5)} mm`));
        setChildren(name, String(spec.name || ''));
        const plan = chipPlan(spec, {});
        let iso = null;
        setChildren(fig, plan.svg);
        const seg = el('div', { class: 'segment', role: 'group', 'aria-label': `View of ${id}` });
        const mk = (label, fn, on) => {
          const b = el('button', { type: 'button', 'aria-pressed': String(!!on), onclick: () => { for (const o of seg.children) o.setAttribute('aria-pressed', String(o === b)); fn(); } }, label);
          seg.appendChild(b);
        };
        mk('Plan', () => { disposeIn(fig); setChildren(fig, plan.svg); }, true);
        mk('Tilted', () => { disposeIn(fig); iso = iso || chipIso(spec); setChildren(fig, iso); });
        const stls = (exportsMap.get(id) || []).filter((a) => /\.stl$/i.test(a.path));
        if (stls.length) mk('3D model', () => show3d(fig, stls));
        actions.appendChild(seg);
        if (cfdChip && cfdChip.figures.length) {
          actions.appendChild(el('button', {
            type: 'button', class: 'ghost',
            onclick: () => { ui.chipIndex = data.cfd.chips.indexOf(cfdChip); ui.stepKey = null; paintMaps(); S.maps.el.scrollIntoView({ behavior: 'smooth', block: 'start' }); },
          }, 'Flow maps →'));
        }
        const files = [...(exportsMap.get(id) || [])];
        if (files.length) {
          extra.appendChild(el('details', { class: 'fold small-fold' },
            el('summary', {}, `Files to print (${files.length})`),
            el('div', { class: 'dl-list' }, ...files.map((a) => el('button', {
              type: 'button', class: 'ghost',
              onclick: async () => { try { await download(runId, a.path); } catch (err) { toast(explain(err), true); } },
            }, el('span', { class: 'kind' }, (a.path.split('.').pop() || '').toUpperCase()), ' ', a.path.split('/').pop())))));
        }
      }).catch((err) => {
        if (cancelled) return;
        setChildren(fig, el('p', { class: 'muted small' }, err instanceof ApiError && err.missing ? 'The design file for this chip is not in the repo any more.' : explain(err)));
      });
      if (i === 0) card.classList.add('first');
    });
  }

  function disposeIn(node) {
    for (const v of [...viewers]) if (node.contains(v.canvas)) { try { v.dispose(); } catch (_) {} viewers.delete(v); }
  }

  async function show3d(fig, stls) {
    const canvas = el('canvas', { class: 'stl', role: 'img', 'aria-label': '3D model of the chip; drag to turn it' });
    setChildren(fig, canvas, el('p', { class: 'cap' }, 'Drag to turn, scroll to zoom.'));
    let viewer;
    try { viewer = new StlViewer(canvas); viewers.add(viewer); } catch (err) {
      setChildren(fig, el('p', { class: 'muted small' }, `This browser cannot show 3D: ${explain(err)}`));
      return;
    }
    const loaded = [];
    for (const a of stls.slice(0, 40)) {
      try { loaded.push({ name: a.path.split('/').pop(), buffer: await api.anyBuffer(runId, a.path) }); } catch (_) { /* skip */ }
    }
    if (cancelled || viewer.disposed) return;
    if (!loaded.length) { setChildren(fig, el('p', { class: 'muted small' }, 'The 3D files could not be read.')); return; }
    viewer.setMeshes(loaded.map((m, i) => ({ ...m, color: PALETTE[i % PALETTE.length] })));
  }

  /* ---------------------------------------------------------------- maps */

  function paintMaps() {
    const body = clear(S.maps.body);
    const chips = data.cfd.chips.filter((c) => c.figures.length || c.steps.length);
    if (!chips.length) {
      body.appendChild(emptyState('No flow maps in this run',
        el('p', {}, data.cfdLoaded ? 'The simulation ran but drew no maps.' : 'This run did not include the flow simulation.')));
      return;
    }
    ui.chipIndex = Math.min(Math.max(0, chips.indexOf(data.cfd.chips[ui.chipIndex])), chips.length - 1);
    const chip = chips[ui.chipIndex] || chips[0];
    const step = chip.steps.find((s) => s.key === ui.stepKey) || chip.steps[0] || null;

    const chipBar = el('div', { class: 'segment wrap', role: 'group', 'aria-label': 'Chip' },
      ...chips.map((c, i) => el('button', {
        type: 'button', 'aria-pressed': String(c === chip),
        onclick: () => { ui.chipIndex = data.cfd.chips.indexOf(c); ui.stepKey = null; paintMaps(); },
      }, c.chip_id)));
    const stepBar = chip.steps.length
      ? el('div', { class: 'segment wrap', role: 'group', 'aria-label': 'Protocol step' },
        ...chip.steps.map((s, i) => {
          const twin = chip.steps.slice(0, i).filter((o) => String(o.step) === String(s.step)).length;
          return el('button', {
            type: 'button', 'aria-pressed': String(s === step), title: s.label,
            onclick: () => { ui.stepKey = s.key; paintMaps(); },
          }, `step ${s.step}${twin ? ` (${twin + 1})` : ''}`);
        }))
      : el('span', { class: 'cap' }, 'No protocol steps recorded for this chip.');

    body.appendChild(el('div', { class: 'map-controls' },
      el('div', {}, el('p', { class: 'eyebrow' }, 'Chip'), chipBar),
      el('div', {}, el('p', { class: 'eyebrow' }, 'Protocol step'), stepBar)));

    if (step) {
      body.appendChild(el('p', { class: 'step-line' },
        el('strong', {}, `Step ${step.step}: `),
        step.operation ? `${step.operation}` : '',
        step.targets.length ? ` in ${step.targets.join(', ')}` : '',
        step.flow_ul_min !== null && step.flow_ul_min !== undefined ? ` at ${fmtNum(step.flow_ul_min, 3)} µL/min` : '',
        step.temp_c !== null && step.temp_c !== undefined ? `, ${fmtNum(step.temp_c, 3)} °C` : '',
        step.inferred ? el('span', {}, ' ', badge('INFERRED', 'warn', 'The simulation worked this scenario out from the geometry; the protocol did not state it outright.')) : null));
    }
    body.appendChild(el('p', { class: 'cap' }, 'From the fast flat-sheet model: see ', term('rom'), '. ',
      'Each map is ', term('simulated'), ', not measured.'));

    const figs = step ? (chip.figuresByStep.get(String(step.step)) || chip.figuresByStep.get('all')) : null;
    body.appendChild(el('div', { class: 'map-grid' }, ...MAPS.map(([key, title, sub]) => {
      const path = figs ? figs.get(key) : null;
      const fig = el('figure', { class: 'map' },
        el('figcaption', {}, el('span', { class: 'map-t' }, title, ' ', simBadge()), el('span', { class: 'cap' }, sub)));
      if (!path) { fig.appendChild(el('div', { class: 'map-missing' }, 'No map drawn for this step.')); return fig; }
      const img = el('img', { src: api.fileUrl(runId, path), alt: `${title} map for ${chip.chip_id}, step ${step ? step.step : ''}, simulated`, loading: 'lazy' });
      img.addEventListener('error', () => { img.replaceWith(el('div', { class: 'map-missing' }, 'This map could not be loaded.')); });
      fig.insertBefore(el('a', { href: api.fileUrl(runId, path), target: '_blank', rel: 'noopener', class: 'map-img', title: 'Open full size' }, img), fig.firstChild);
      return fig;
    })));
  }

  /* ---------------------------------------------------------------- findings */

  function paintFindings() {
    const body = clear(S.findings.body);
    const all = data.cfd.findings;
    if (!all.length) {
      body.appendChild(emptyState(data.cfdLoaded ? 'Nothing found' : 'No findings to show',
        el('p', {}, data.cfdLoaded ? 'The simulation ran and found nothing at any severity.' : 'This run has no flow simulation output.')));
      return;
    }
    const counts = countSeverity(all);
    const chips = [...new Set(all.map((x) => x.chip_id).filter(Boolean))];
    const sevSeg = el('div', { class: 'segment wrap', role: 'group', 'aria-label': 'Severity' },
      ...['', ...SEVERITIES].map((k) => el('button', {
        type: 'button', 'aria-pressed': String(ui.sev === k),
        onclick: () => { ui.sev = k; ui.showAll = false; paintFindings(); },
      }, k ? `${SEV_WORD[k]} ${counts[k]}` : `All ${all.length}`)));
    const chipSel = el('select', {
      'aria-label': 'Chip',
      onchange: (e) => { ui.chip = e.target.value; ui.showAll = false; paintFindings(); },
    }, el('option', { value: '' }, 'every chip'), ...chips.map((c) => el('option', { value: c, selected: c === ui.chip || null }, c)));
    body.appendChild(el('div', { class: 'find-filters' }, sevSeg, el('label', { class: 'row' }, el('span', { class: 'cap' }, 'Chip'), chipSel)));

    const order = { blocker: 0, high: 1, medium: 2, low: 3 };
    const ratio = (f) => { const v = Number(f.value); const l = Number(f.limit); return v > 0 && l > 0 ? Math.max(v / l, l / v) : 0; };
    const shown = all.filter((f) => (!ui.sev || f.severity === ui.sev) && (!ui.chip || f.chip_id === ui.chip))
      .sort((a, b) => order[a.severity] - order[b.severity] || ratio(b) - ratio(a));
    const LIMIT = 10;
    const list = el('ol', { class: 'findings' }, ...(ui.showAll ? shown : shown.slice(0, LIMIT)).map(findingCard));
    body.appendChild(list);
    if (!shown.length) body.appendChild(el('p', { class: 'muted' }, 'Nothing matches that filter.'));
    if (shown.length > LIMIT) {
      body.appendChild(el('button', {
        type: 'button', class: 'secondary',
        onclick: () => { ui.showAll = !ui.showAll; paintFindings(); },
      }, ui.showAll ? `Show the first ${LIMIT} only` : `Show all ${shown.length}`));
    }
  }

  function findingCard(f) {
    const fixes = data.cfd.fixes.filter((x) => x.finding_id === f.id);
    fixes.sort((a, b) => Number(b.applied) - Number(a.applied) || Number(b.auto_applicable) - Number(a.auto_applicable));
    const best = fixes[0];
    const make = TITLE_BY_CLASS[f.cls];
    const where = f.feature_id || f.chip_id || 'the chip';
    const title = make ? make(where) : `${f.cls} at ${where}`;
    const ct = classTerm(f.cls);
    const untrusted = /^UNTRUSTED/i.test(f.message);
    const msg = f.message.replace(/^UNTRUSTED \([^)]*\)\s*-\s*/i, '');

    return el('li', { class: `finding f-${f.severity}` },
      el('div', { class: 'f-side' },
        el('span', { class: `sev-chip sev-fill-${f.severity}` }, SEV_WORD[f.severity]),
        ct ? term(ct) : el('span', { class: 'cap' }, f.cls)),
      el('div', { class: 'f-main' },
        el('h4', { class: 'f-title' }, title),
        el('p', { class: 'f-where' }, el('span', { class: 'mono' }, [f.chip_id, f.step !== null && f.step !== undefined ? `step ${f.step}` : null].filter(Boolean).join(' · ')),
          untrusted ? badge('UNTRUSTED', 'bad', 'The flow model failed one of its own checks, so this number is not yet trustworthy. See the box at the top of the page.') : null),
        gauge(f),
        msg ? el('details', { class: 'fold small-fold' }, el('summary', {}, 'The simulation\'s own words'), el('p', { class: 'small' }, msg)) : null,
        best ? fixLine(best, fixes.length - 1) : (f.fix_hint ? el('p', { class: 'fix' }, el('strong', {}, 'Suggested: '), f.fix_hint) : el('p', { class: 'cap' }, 'No fix proposed.'))));
  }

  /** Value against limit on a shared log scale, so 50x over is visibly worse than 2x. */
  function gauge(f) {
    const v = Number(f.value);
    const l = Number(f.limit);
    const u = f.units ? ` ${f.units}` : '';
    if (!(v > 0) || !(l > 0)) {
      return el('p', { class: 'gauge-text' }, el('span', { class: 'num' }, `${f.value ?? '-'}${u}`), ' ', simBadge(),
        f.limit !== null && f.limit !== undefined ? ` against ${f.limit}${u}` : '');
    }
    const lo = Math.log10(Math.min(v, l) / 3);
    const hi = Math.log10(Math.max(v, l) * 3);
    const at = (x) => ((Math.log10(x) - lo) / (hi - lo)) * 100;
    const r = v / l;
    const words = r >= 1 ? `${fmtNum(r, 2)}× the limit` : `${fmtNum(1 / r, 2)}× below the limit`;
    const W = 300;
    const g = svg('svg', { class: 'gauge', viewBox: `0 0 ${W} 34`, role: 'img', 'aria-label': `simulated ${fmtNum(v, 3)}${u} against a limit of ${fmtNum(l, 3)}${u}, ${words}` },
      svg('line', { x1: 0, y1: 20, x2: W, y2: 20, class: 'g-track' }),
      svg('line', { x1: (at(Math.min(v, l)) * W) / 100, y1: 20, x2: (at(Math.max(v, l)) * W) / 100, y2: 20, class: `g-gap sev-stroke-${f.severity}` }),
      svg('line', { x1: (at(l) * W) / 100, y1: 10, x2: (at(l) * W) / 100, y2: 30, class: 'g-limit' }),
      svg('text', { x: (at(l) * W) / 100, y: 8, class: 'g-lab', 'text-anchor': 'middle' }, 'limit'),
      svg('circle', { cx: (at(v) * W) / 100, cy: 20, r: 5, class: `g-val sev-fill-${f.severity}` }));
    return el('div', { class: 'gauge-row' }, g,
      el('p', { class: 'gauge-text' },
        el('span', { class: 'num' }, `${fmtNum(v, 3)}${u}`), ' ', simBadge(), ' against ',
        el('span', { class: 'num' }, `${fmtNum(l, 3)}${u}`), el('strong', {}, ` · ${words}`),
        f.metric ? el('span', { class: 'cap' }, ` ${f.metric.replace(/_/g, ' ')}`) : null));
  }

  function fixLine(x, others) {
    const what = FIX_WORDS[x.action] || x.action.replace(/_/g, ' ');
    const state = x.applied ? badge('applied to a copy, re-checked', 'ok')
      : x.auto_applicable ? badge('can be applied automatically', 'info') : badge('needs a person', 'warn');
    const ba = x.before !== null && x.before !== undefined && x.after !== null && x.after !== undefined
      && fmtNum(x.before, 3) !== fmtNum(x.after, 3)
      ? el('span', { class: 'num' }, ` ${fmtNum(x.before, 3)} → ${fmtNum(x.after, 3)}${x.units ? ` ${x.units}` : ''} `)
      : null;
    return el('p', { class: 'fix' },
      el('strong', {}, 'Fix: '), what, x.target ? el('span', { class: 'mono' }, ` (${x.target})`) : null,
      ba, ba ? simBadge() : null, ' ', state,
      others > 0 ? el('span', { class: 'cap' }, ` · ${others} other option${others === 1 ? '' : 's'} in the full tables below`) : null);
  }

  /* ---------------------------------------------------------------- report */

  async function paintReport() {
    const body = clear(S.report.body);
    const hit = data.artifacts.find((a) => /(^|\/)report\.md$/i.test(a.path) && !/\/cfd\//.test(a.path))
      || data.artifacts.find((a) => /(^|\/)report\.md$/i.test(a.path));
    let md = null;
    if (hit) { try { md = await api.fileText(runId, hit.path); } catch (_) { md = null; } }
    if (!md && data.manifest.raw && data.manifest.raw.has_report) {
      try { const r = await api.report(runId); md = typeof r === 'string' ? r : (r && (r.markdown || r.md)) || null; } catch (_) { md = null; }
    }
    if (cancelled) return;
    if (!md) {
      body.appendChild(emptyState('No report yet', el('p', {}, 'The last stage writes the report. This run has not got that far, or was a simulation-only job.')));
      return;
    }
    const block = markdownBlock(md);
    const wrap = el('div', { class: 'report-wrap collapsed' }, block);
    const toggle = el('button', {
      type: 'button', class: 'secondary',
      onclick: () => { const c = wrap.classList.toggle('collapsed'); setChildren(toggle, c ? 'Read the whole report' : 'Show less'); },
    }, 'Read the whole report');
    body.appendChild(el('div', { class: 'flags' },
      /\bsimulat/i.test(md) ? simBadge() : null, /\bstub\b/i.test(md) ? stubBadge() : null,
      /30\s*x\s*20\s*mm|demo scale/i.test(md) ? demoBadge() : null, /not (yet )?implemented/i.test(md) ? todoBadge() : null));
    body.appendChild(wrap);
    body.appendChild(el('div', { class: 'row' }, toggle,
      el('button', { type: 'button', class: 'ghost', onclick: async () => { try { await download(runId, hit ? hit.path : 'report.md'); } catch (err) { toast(explain(err), true); } } }, 'Download')));
  }

  /* ---------------------------------------------------------------- videos */

  function paintVideos() {
    const body = clear(S.videos.body);
    const vids = data.videos ? data.videos.videos : [];
    if (!vids.length) {
      body.appendChild(emptyState('No recording slots', el('p', {}, 'docs/videos/manifest.json is not available right now.')));
      return;
    }
    body.appendChild(el('div', { class: 'video-grid' }, ...vids.map((v) => {
      const inRun = data.chips.includes(v.chip_id);
      const url = playableUrl(v.url);
      const media = url && v.recorded !== false
        ? el('video', { controls: true, preload: 'metadata', playsinline: true, src: url, class: 'video' })
        : el('div', { class: 'video-slot empty-slot' }, el('span', { class: 'play', 'aria-hidden': 'true' }),
          el('div', {}, el('strong', {}, 'Not recorded yet'), v.shot ? el('p', { class: 'cap' }, `Planned: ${v.shot}`) : null));
      return el('article', { class: 'video-card' },
        el('header', {}, el('p', { class: 'eyebrow' }, v.module || 'module'), el('h3', { class: 'mono-title' }, v.chip_id)),
        el('div', { class: 'flags' }, printedBadge(), inRun ? badge('in this run', 'info') : null),
        media);
    })));
  }

  /* ---------------------------------------------------------------- more */

  function paintMore() {
    const body = clear(S.more.body);
    const sub = { ...ctx, runId, param: null };
    const items = [
      ['The stage-by-stage record', 'Every pipeline stage with its timing, the files it wrote (opened in place) and the full log.', runView],
      ['Where each protocol step ended up', 'Every step read from the protocol and whether it landed on a chip, off-chip, or needs a part that does not exist.', stepsView],
      ['Full flow-simulation tables', 'Every finding and every fix as a table, the solver\'s own checks, the tubing between chips and which parts of the solver ran.', cfdView],
      ['Every chip in the repo', 'All chip designs, not only this run\'s: plan, 3D, fabrication limits and downloads.', chipsView],
      ['The report, full page', 'The report with a table of contents.', reportView],
    ];
    for (const [title, line, mod] of items) {
      const inner = el('div', { class: 'legacy' });
      const d = el('details', { class: 'fold' },
        el('summary', {}, el('span', { class: 'fs-text' }, el('span', { class: 'fs-t' }, title), el('span', { class: 'fs-s' }, line))), inner);
      let done = false;
      d.addEventListener('toggle', () => {
        if (!d.open || done) return;
        done = true;
        try {
          const res = mod.mount(inner, sub);
          if (typeof res === 'function') unmounts.push(res);
          else if (res && typeof res.then === 'function') res.then((u) => { if (typeof u === 'function') unmounts.push(u); });
        } catch (err) { setChildren(inner, el('p', { class: 'muted' }, explain(err))); }
      });
      body.appendChild(d);
    }
  }
}

/* ------------------------------------------------------------------ helpers */

function section(id, title, sub) {
  const body = el('div', { class: 'sec-body' });
  const node = el('section', { class: 'section', id: `res-${id}` },
    el('header', { class: 'sec-head' }, el('h2', {}, title), el('p', { class: 'sub' }, sub)), body);
  return { el: node, body };
}

function statusWord(s) {
  return { done: 'finished', failed: 'failed', running: 'running', queued: 'queued', unknown: 'no stage record', pending: 'not started' }[s] || s;
}

function runPicker(ctx, runId) {
  const runs = ctx.state.runs || [];
  const sel = el('select', {
    id: 'run-select', 'aria-label': 'Choose a run',
    onchange: (e) => { if (e.target.value) ctx.selectRun(e.target.value, 'results'); },
  });
  if (!runs.length) sel.appendChild(el('option', { value: '' }, 'no runs yet'));
  for (const r of runs) {
    const sev = r.findings_by_severity || {};
    const n = ['blocker', 'high', 'medium', 'low'].reduce((a, k) => a + (Number(sev[k]) || 0), 0);
    sel.appendChild(el('option', { value: r.run_id, selected: r.run_id === runId || null },
      `${r.run_id}${n ? `  ·  ${n} findings` : ''}${r.status && !['done', 'unknown'].includes(r.status) ? `  ·  ${r.status}` : ''}`));
  }
  sel.disabled = !runs.length;
  return el('label', { class: 'run-picker' }, el('span', { class: 'eyebrow' }, 'Showing run'), sel);
}

export { SIM_TITLE };

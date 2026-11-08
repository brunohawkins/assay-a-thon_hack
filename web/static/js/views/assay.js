// "Our assay": THIS project's pipeline, not the general one.
//
// iPSCs -> three neural lineages -> single-cell transcriptome, plus the bulk multi-omic
// arm. One card per stage in chain order, showing exactly what that module does.
//
// The six printed modules are FROZEN. This section presents them and never proposes a
// change: no fix buttons, no "rerun", no edit. Where a full-spec target design exists it
// is shown beside the demo chip, labelled FULL SPEC.

import { api, ApiError, explain, download } from '../api.js';
import { el, clear, setChildren, badge, safeLink, toast, fmtNum, fmtSeconds, emptyState, errorState, loading } from '../dom.js';
import { simBadge, demoBadge, todoBadge, DEMO_TITLE } from '../labels.js';
import { chipPlan } from '../chipsvg.js';
import { StlViewer, PALETTE, bboxSize } from '../stl.js';
import {
  loadDocsJson, normModulesDoc, normVideoManifest, artifactUrl, runIdForPath, playableUrl, NEEDED_ROUTE,
} from '../ourassay.js';

const CFD_METRICS = ['pressure', 'speed', 'shear', 'residence'];
const TWIN_URL = '/twin/index.html';

export function mount(host, ctx) {
  let cancelled = false;
  const viewers = new Set();
  const state = { doc: null, videos: null, openId: null, tabs: new Map() };

  setChildren(host,
    el('div', { class: 'view-head' },
      el('h2', 'Our assay'),
      el('span', { class: 'sub' }, 'iPSCs to three neural lineages, single-cell transcriptome and the bulk multi-omic arm')),
    loading('Reading docs/modules.json'));

  (async () => {
    const fallbackRun = ctx.fileRunId;
    const [mods, vids] = await Promise.all([
      loadDocsJson('docs/modules.json', fallbackRun),
      loadDocsJson('docs/videos/manifest.json', fallbackRun),
    ]);
    if (cancelled) return;
    if (mods.missing) { renderNoData(host, mods); return; }
    state.doc = normModulesDoc(mods.data);
    state.videos = vids.missing ? null : normVideoManifest(vids.data);
    render(host, ctx, state, viewers, () => cancelled);
  })().catch((err) => {
    if (!cancelled) setChildren(host, errorState('Could not load the assay description', el('p', explain(err))));
  });

  return () => {
    cancelled = true;
    for (const v of viewers) { try { v.dispose(); } catch (_) {} }
    viewers.clear();
  };
}

function renderNoData(host, res) {
  setChildren(host,
    el('div', { class: 'view-head' }, el('h2', 'Our assay'), todoBadge()),
    emptyState('docs/modules.json is not being served',
      el('p', 'The file exists in the repo, but the API has no route that reaches it: /file allowlists '
        + 'the run\'s own files plus cad/figures/, chip/ and protocols/ only.'),
      el('h4', 'Route this section needs'),
      el('pre', { class: 'code' }, NEEDED_ROUTE),
      el('p', { class: 'small muted' }, `Tried: ${(res.tried || []).join('  ')}`)));
}

/* ------------------------------------------------------------------ render */

function render(host, ctx, state, viewers, isCancelled) {
  const doc = state.doc;
  const printedIds = new Set(doc.printed.map((p) => p.chip_id));
  const chipStages = doc.stages.filter((s) => s.chip);
  const printedCount = chipStages.filter((s) => printedIds.has(s.chip.chip_id) || s.chip.printed).length;

  const head = el('div', { class: 'view-head' },
    el('h2', 'Our assay'),
    el('span', { class: 'sub' }, doc.pipeline || 'this project\'s pipeline'),
    el('span', { class: 'spacer' }),
    badge('FROZEN', 'info', 'The printed modules are fixed for the demo. This section presents them and proposes no changes.'),
    safeLink(TWIN_URL, 'Open the digital twin'));

  const overview = el('section', { class: 'panel' },
    el('h3', { style: { marginTop: 0 } }, 'The chain'),
    el('p', { class: 'small muted' },
      `${doc.stages.length} stages, ${chipStages.length} on a chip, ${printedCount} printed for the demo.`
      + (doc.variant ? ` Variant: ${doc.variant}.` : '')),
    chainStrip(doc, printedIds, (id) => jumpTo(id)),
    el('p', { class: 'hint' }, 'Every number on a card is read from the module\'s own chip spec and protocol. '
      + 'Flow numbers are simulated; bench recordings are the only measured thing here.'));

  const cards = el('div', { class: 'cols' },
    ...doc.stages.map((s) => stageCard(s, { ctx, state, viewers, isCancelled, printedIds })));

  const extras = doc.not_in_pipeline.length
    ? el('section', { class: 'panel' },
      el('h3', { style: { marginTop: 0 } }, 'Built, but not in this chain'),
      el('p', { class: 'small muted' }, 'These modules exist and are printed or designed, but pipeline_mvp.json does not include them.'),
      el('div', { class: 'cols' }, ...doc.not_in_pipeline.map((s) => stageCard(s, { ctx, state, viewers, isCancelled, printedIds }))))
    : null;

  const provenance = el('section', { class: 'panel' },
    el('h4', 'Where this page comes from'),
    el('dl', { class: 'kv' },
      el('dt', 'modules'), el('dd', 'docs/modules.json'),
      el('dt', 'videos'), el('dd', state.videos ? 'docs/videos/manifest.json' : el('span', { class: 'muted' }, 'not served yet')),
      el('dt', 'generated by'), el('dd', doc.generated_by || 'unknown'),
      el('dt', 'pipeline'), el('dd', doc.pipeline_id || '-')),
    el('p', { class: 'hint' }, 'Generated from the chip specs and protocols, so it cannot drift from them. '
      + 'Nothing here is editable: the printed modules are frozen.'));

  setChildren(host, head, overview, cards, extras, provenance);

  function jumpTo(id) {
    const node = host.querySelector(`[data-stage-card="${CSS.escape(id)}"]`);
    if (!node) return;
    node.open = true;
    node.scrollIntoView({ block: 'start', behavior: 'smooth' });
    node.querySelector('summary')?.focus();
  }
}

/** The chain as a row of clickable chips, in order. */
function chainStrip(doc, printedIds, onJump) {
  const strip = el('div', { class: 'chain', style: { marginTop: '10px' } });
  doc.stages.forEach((s, i) => {
    const printed = s.chip && (s.chip.printed || printedIds.has(s.chip.chip_id));
    strip.appendChild(el('button', {
      type: 'button', class: 'node', style: { textAlign: 'left', cursor: 'pointer' },
      onclick: () => onJump(s.id),
    },
    el('h5', {}, String(i + 1), '. ', s.name || s.id),
    s.chip ? el('span', { class: 'id' }, s.chip.chip_id) : el('span', { class: 'id' }, s.kind),
    el('div', { class: 'row', style: { gap: '4px', marginTop: '5px' } },
      printed ? badge('PRINTED', 'ok', 'Printed for the demo.') : null,
      s.chip && !printed ? badge('design only', '') : null,
      !s.chip ? offChipBadge(s.kind) : null)));
    if (i < doc.stages.length - 1) {
      const h = s.leaves[0];
      strip.appendChild(el('div', { class: 'link' },
        el('div', { class: 'arrow' }, '\u2192'),
        h ? el('div', {}, h.carries || 'transfer') : null,
        h && h.volume_ul ? el('div', {}, `${fmtNum(h.volume_ul, 4)} uL`) : null));
    }
  });
  return strip;
}

function offChipBadge(kind) {
  const k = String(kind || 'OFF-CHIP').toUpperCase();
  if (/KIT|BOUGHT/.test(k)) return badge('BOUGHT KIT', 'warn', 'Done with a bought kit, not on a chip.');
  return badge('OFF-CHIP', 'warn', 'Done at the bench, not on a chip.');
}

/* ------------------------------------------------------------------ one card */

function stageCard(stage, env) {
  const { printedIds } = env;
  const chip = stage.chip;
  const printed = chip && (chip.printed || printedIds.has(chip.chip_id));

  const title = el('summary', { style: { listStyle: 'none' } },
    el('div', { class: 'row' },
      el('strong', { style: { fontSize: '15px' } }, stage.name || stage.id),
      chip ? el('span', { class: 'mono small muted' }, chip.chip_id) : null,
      el('span', { class: 'spacer' }),
      printed ? badge('PRINTED FOR THE DEMO', 'ok') : null,
      chip && !printed ? badge('not printed', '', 'Designed but not in the printed demo set.') : null,
      !chip ? offChipBadge(stage.kind) : null,
      chip && /demo/i.test(chip.scale) ? demoBadge() : null,
      chip && chip.placeholder ? badge('PLACEHOLDER DATA', 'warn') : null,
      stage.status ? badge(stage.status, stage.status === 'final' ? 'ok' : '') : null));

  const body = el('div', { style: { marginTop: '12px' } });
  const card = el('details', {
    class: 'panel', dataset: { stageCard: stage.id },
    open: null,
  }, title, body);

  let built = false;
  const build = () => {
    if (built) return;
    built = true;
    setChildren(body, ...cardBody(stage, chip, printed, env));
  };
  card.addEventListener('toggle', () => { if (card.open) build(); });
  if (card.open) build();
  return card;
}

function cardBody(stage, chip, printed, env) {
  const out = [];

  if (stage.note) out.push(el('p', { class: 'small muted' }, stage.note));

  out.push(flowSummary(stage));

  if (!chip) {
    out.push(el('div', { class: 'empty', style: { textAlign: 'left' } },
      el('h3', {}, offChipBadge(stage.kind), ' ', stage.name || stage.id),
      el('p', stage.where || 'This step is done off chip.'),
      stage.status_note ? el('p', { class: 'small muted' }, String(stage.status_note)) : null,
      stage.protocol ? el('p', { class: 'small mono muted' }, stage.protocol) : null,
      el('p', { class: 'hint' }, 'No chip, so no plan view, no geometry and no flow simulation. '
        + 'It is in the chain because the sample passes through it.')));
    return out;
  }

  out.push(chipFacts(chip, printed));
  out.push(functionTable(chip));
  out.push(portsTable(chip));
  out.push(figuresBlock(chip, env));
  out.push(threeDBlock(chip, env));
  out.push(cfdBlock(chip, env));
  out.push(demoBlock(chip, stage));
  out.push(videoBlock(chip, env));
  out.push(limitsBlock(chip));
  out.push(fullSpecBlock(chip, env));
  out.push(el('p', { class: 'row', style: { marginTop: '12px' } },
    safeLink(TWIN_URL, 'Open this module in the digital twin'),
    el('span', { class: 'small muted' }, 'the twin animates the same chip spec')));
  return out.filter(Boolean);
}

function chipFacts(chip, printed) {
  const kv = el('dl', { class: 'kv' });
  const add = (k, v) => { if (v !== null && v !== undefined && v !== '') { kv.appendChild(el('dt', k)); kv.appendChild(el('dd', {}, v)); } };
  add('chip', chip.chip_name || chip.chip_id);
  add('spec', chip.chip_path);
  add('protocol', chip.protocol);
  add('scale', el('span', { class: 'row', style: { gap: '6px' } },
    el('span', {}, chip.scale || 'unstated'),
    /demo/i.test(chip.scale) ? demoBadge() : null));
  add('etch depth', chip.depth_mm ? `${fmtNum(chip.depth_mm, 3)} mm, one fluidic layer` : null);
  add('chambers', chip.chambers.length
    ? chip.chambers.map((c) => `${c.id} (${c.block || c.type}${c.volume_ul ? `, ${fmtNum(c.volume_ul, 4)} uL` : ''})`).join('; ')
    : null);
  add('thermal', chip.thermal_zones.length
    ? chip.thermal_zones.map((z) => `${z.id}: ${fmtNum(z.min_c, 3)}-${fmtNum(z.max_c, 3)} C`).join('; ')
    : null);
  add('printed', printed ? 'yes, in the demo set' : 'no');
  return el('div', {}, el('h4', 'What it is'), kv);
}

/** The exact function: every step with its operation, chamber, reagent and numbers. */
function functionTable(chip) {
  if (!chip.steps.length) {
    return el('div', {}, el('h4', 'Exact function'),
      el('p', { class: 'muted small' }, 'No protocol steps in docs/modules.json for this chip.'));
  }
  const rows = chip.steps.map((s) => el('tr', {},
    el('td', { class: 'num' }, s.step === null || s.step === undefined ? '-' : String(s.step)),
    el('td', { class: 'small' }, el('span', { class: 'mono' }, s.operation || '-'),
      s.exchange_kind ? el('div', { class: 'muted' }, s.exchange_kind) : null),
    el('td', { class: 'small mono' }, s.target.join(', ') || '-'),
    el('td', { class: 'small' }, s.reagent.join(', ') || el('span', { class: 'muted' }, '-')),
    el('td', { class: 'num' }, s.volume_ul === null || s.volume_ul === undefined ? '-' : `${fmtNum(s.volume_ul, 4)} uL`,
      s.washes ? el('div', { class: 'small muted' }, `x${s.washes}`) : null),
    el('td', { class: 'num' }, s.flow_ul_min === null || s.flow_ul_min === undefined ? '-' : `${fmtNum(s.flow_ul_min, 4)} uL/min`),
    el('td', { class: 'num' }, s.duration_s === null || s.duration_s === undefined ? '-' : fmtSeconds(s.duration_s)),
    el('td', { class: 'num' }, s.temp_c === null || s.temp_c === undefined ? '-' : `${fmtNum(s.temp_c, 4)} C`),
    el('td', { class: 'small mono muted' },
      s.feed.length ? el('div', {}, `in: ${s.feed.join(', ')}`) : null,
      s.vent.length ? el('div', {}, `out: ${s.vent.join(', ')}`) : null)));

  const total = chip.steps.reduce((a, s) => a + (Number(s.duration_s) || 0), 0);
  const tbody = el('tbody');
  const WINDOW = 14;
  const long = rows.length > WINDOW;
  let expanded = false;
  const fill = () => setChildren(tbody, ...(expanded ? rows : rows.slice(0, WINDOW)));
  fill();

  const toggle = long
    ? el('button', {
      type: 'button', style: { marginTop: '8px' },
      onclick: () => {
        expanded = !expanded;
        fill();
        setChildren(toggle, expanded ? `Show only the first ${WINDOW} steps` : `Show all ${rows.length} steps`);
        toggle.setAttribute('aria-expanded', String(expanded));
      },
      'aria-expanded': 'false',
    }, `Show all ${rows.length} steps`)
    : null;

  const opCounts = new Map();
  for (const s of chip.steps) opCounts.set(s.operation, (opCounts.get(s.operation) || 0) + 1);
  const shape = [...opCounts].sort((a, b) => b[1] - a[1]).map(([k, n]) => `${n} x ${k}`).join(', ');

  return el('div', {},
    el('h4', `Exact function - ${chip.steps.length} step${chip.steps.length === 1 ? '' : 's'}`),
    shape ? el('p', { class: 'small muted' }, shape,
      total ? ` - total protocol time ${fmtSeconds(total)}` : '') : null,
    el('div', { class: 'scroll-x' }, el('table', { class: 'data' },
      el('caption', `Straight from ${chip.protocol || 'the protocol'}; nothing here is re-derived.`),
      el('thead', {}, el('tr', {},
        el('th', { class: 'num' }, '#'), el('th', 'Operation'), el('th', 'Chamber'), el('th', 'Reagent'),
        el('th', { class: 'num' }, 'Volume'), el('th', { class: 'num' }, 'Flow'),
        el('th', { class: 'num' }, 'Time'), el('th', { class: 'num' }, 'Temp'), el('th', 'Ports'))),
      tbody)),
    long ? el('p', { class: 'small muted', style: { margin: '6px 0 0' } },
      `Showing the first ${WINDOW} of ${rows.length}.`) : null,
    toggle);
}

function portsTable(chip) {
  if (!chip.ports.length) return null;
  return el('div', {},
    el('h4', `Ports (${chip.ports.length})`),
    el('div', { class: 'scroll-x' }, el('table', { class: 'data' },
      el('thead', {}, el('tr', {}, el('th', 'Port'), el('th', 'Type'), el('th', 'Role'),
        el('th', { class: 'num' }, 'Fed in'), el('th', { class: 'num' }, 'Vented in'))),
      el('tbody', {}, ...chip.ports.map((p) => el('tr', {},
        el('td', { class: 'mono small' }, p.id),
        el('td', { class: 'small' }, p.type),
        el('td', { class: 'small muted' }, p.role || '-'),
        el('td', { class: 'num' }, Number.isFinite(p.fed) ? `${p.fed} steps` : '-'),
        el('td', { class: 'num' }, Number.isFinite(p.vented) ? `${p.vented} steps` : '-')))))),
    chip.off_chip.length
      ? el('details', { class: 'disclose', style: { marginTop: '8px' } },
        el('summary', `${chip.off_chip.length} thing${chip.off_chip.length === 1 ? '' : 's'} that stay off chip`),
        el('ul', { class: 'small', style: { paddingLeft: '20px', margin: 0 } },
          ...chip.off_chip.map((t) => el('li', t))))
      : null);
}

/** What arrives and what leaves. */
function flowSummary(stage) {
  const side = (list, label, empty) => el('div', { class: 'node', style: { flex: '1 1 260px' } },
    el('h5', label),
    list.length
      ? el('ul', { class: 'small', style: { margin: '4px 0 0', paddingLeft: '18px' } },
        ...list.map((h) => el('li', {},
          el('strong', h.carries || 'material'),
          h.volume_ul ? ` ${fmtNum(h.volume_ul, 4)} uL` : '',
          h.particle_um ? ` (${fmtNum(h.particle_um, 4)} um particles)` : '',
          h.from && label === 'Arrives' ? el('div', { class: 'muted mono' }, `from ${h.from.stage || '?'} ${(h.from.ports || []).join(', ')}`) : null,
          h.to && label === 'Leaves' ? el('div', { class: 'muted mono' }, `to ${h.to.stage || '?'} ${(h.to.ports || []).join(', ')}`) : null,
          h.connector && h.connector !== 'match' ? el('div', { style: { color: '#fde68a' } }, `connector: ${h.connector}`) : null,
          h.automated === false ? el('div', {}, badge('needs a human', 'bad')) : null,
          h.note ? el('div', { class: 'muted' }, h.note) : null)))
      : el('p', { class: 'muted small', style: { margin: '4px 0 0' } }, empty));
  return el('div', {},
    el('h4', 'What arrives, what leaves'),
    el('div', { class: 'chain' },
      side(stage.arrives, 'Arrives', 'Nothing: this is where the chain starts.'),
      side(stage.leaves, 'Leaves', 'Nothing: this is where the chain ends.')));
}

/* ------------------------------------------------------------------ media */

function figureGrid(paths, env, emptyText) {
  if (!paths.length) return el('p', { class: 'muted small' }, emptyText);
  return el('div', { class: 'figs' }, ...paths.map((p) => {
    const src = artifactUrl(p, env.ctx.fileRunId);
    const fig = el('figure', {});
    if (!src) {
      setChildren(fig, el('p', { class: 'small muted' }, `${p} - no run can serve this path`));
      return fig;
    }
    const img = el('img', { src, alt: p.split('/').pop(), loading: 'lazy' });
    img.addEventListener('error', () => setChildren(fig,
      el('p', { class: 'small muted' }, `could not load ${p}`),
      el('p', { class: 'hint' }, `served through run ${runIdForPath(p, env.ctx.fileRunId)}`)));
    setChildren(fig, img, el('figcaption', p.split('/').pop()));
    return fig;
  }));
}

function figuresBlock(chip, env) {
  return el('div', {},
    el('h4', 'Plan view'),
    figureGrid(chip.plan_svgs, env,
      'No plan SVG listed. cad/make_figures.py writes layout, features and stack views.'));
}

function threeDBlock(chip, env) {
  const host = el('div', {});
  const paths = chip.renders_3d;
  if (paths.length) {
    setChildren(host, el('h4', '3D render'), figureGrid(paths, env, ''));
    return host;
  }
  // No pre-made render: offer the live STL viewer on the chip's exported geometry.
  const canvas = el('canvas', { class: 'stl', role: 'img', 'aria-label': `3D view of ${chip.chip_id}`, style: { height: '320px' } });
  const status = el('p', { class: 'stl-help' }, 'Looking for exported geometry...');
  const parts = el('div', { class: 'row', style: { gap: '6px', marginTop: '6px' } });
  setChildren(host, el('h4', '3D view'),
    el('div', { class: 'stl-wrap' }, canvas),
    el('p', { class: 'stl-help' }, 'Drag to orbit, wheel to zoom, shift-drag to pan. Arrow keys orbit, + and - zoom, 0 resets.'),
    status, parts);

  let viewer;
  try { viewer = new StlViewer(canvas); } catch (err) {
    setChildren(host, el('h4', '3D view'), el('p', { class: 'muted small' }, `No WebGL: ${explain(err)}`));
    return host;
  }
  env.viewers.add(viewer);

  (async () => {
    const dir = `cad/exports/${chip.chip_id}`;
    const ids = chip.chambers.map((c) => c.id).concat(chip.ports.map((p) => p.id)).filter(Boolean);
    const tries = [...new Set([`${dir}/${chip.chip_id}.stl`, ...ids.map((i) => `${dir}/${i}.stl`)])];
    const loaded = [];
    let forbidden = false;
    for (const path of tries) {
      if (env.isCancelled()) return;
      try { loaded.push({ name: path.split('/').pop(), buffer: await api.anyBuffer(env.ctx.fileRunId, path) }); }
      catch (err) {
        // 403 means this run may not read cad/exports/<chip> at all, so every other
        // path would be refused too: stop rather than fire a dozen doomed requests.
        if (err instanceof ApiError && (err.status === 403 || err.status === 0)) { forbidden = true; break; }
      }
      if (loaded.length >= 40) break;
    }
    if (forbidden && !loaded.length) {
      setChildren(status, el('span', { class: 'muted' },
        `cad/exports/${chip.chip_id}/ is outside the sandbox of run ${env.ctx.fileRunId || '(none)'}: `
        + '/file opens a chip\'s exports only to the run that built it. The plan view above is the same geometry in 2D.'));
      canvas.remove();
      return;
    }
    if (env.isCancelled() || viewer.disposed) return;
    if (!loaded.length) {
      setChildren(status, el('span', { class: 'muted' },
        `No exported geometry reachable: cad/exports/${chip.chip_id}/ is only readable through a run that `
        + 'built this chip, and no such run is selected. The plan view above is the same geometry in 2D.'));
      canvas.remove();
      return;
    }
    const info = viewer.setMeshes(loaded.map((m, i) => ({ ...m, color: PALETTE[i % PALETTE.length] })));
    const size = bboxSize(info.bbox);
    setChildren(status, `${loaded.length} part${loaded.length === 1 ? '' : 's'}, ${info.triangles.toLocaleString()} triangles`
      + (size ? `, ${fmtNum(size[0], 4)} x ${fmtNum(size[1], 4)} x ${fmtNum(size[2], 4)} mm.` : '.'));
    loaded.forEach((m, i) => {
      const colour = PALETTE[i % PALETTE.length].map((v) => Math.round(v * 255));
      const b = el('button', {
        type: 'button', class: 'sel', 'aria-pressed': 'true',
        onclick: () => {
          const on = b.getAttribute('aria-pressed') !== 'true';
          b.setAttribute('aria-pressed', String(on));
          viewer.setVisible(m.name, on);
        },
      }, el('i', {
        style: {
          display: 'inline-block', width: '9px', height: '9px', borderRadius: '2px',
          background: `rgb(${colour.join(',')})`, marginRight: '6px',
        },
      }), m.name);
      parts.appendChild(b);
    });
  })();

  return host;
}

/** The reduced-order CFD maps, plus OpenFOAM renders and the chain animation when they exist. */
function cfdBlock(chip, env) {
  const host = el('div', {});
  const figs = chip.cfd.figures;
  const steps = [...new Set(figs.map((p) => {
    const m = /_s([\w.]+)_(?:pressure|speed|shear|residence)\.svg$/i.exec(p.split('/').pop());
    return m ? m[1] : null;
  }).filter(Boolean))];

  const header = el('div', { class: 'row' }, el('h4', { style: { margin: 0 } }, 'Flow simulation'), simBadge());
  if (!figs.length) {
    setChildren(host, header,
      el('p', { class: 'muted small' }, 'No reduced-order CFD maps for this chip yet. S10 writes them per protocol step.'),
      openfoamBlock(chip, env));
    return host;
  }

  const grid = el('div', {});
  const bar = el('div', { class: 'segment', role: 'tablist', 'aria-label': 'Simulated step', style: { flexWrap: 'wrap' } });
  const show = (step) => {
    for (const b of bar.children) b.setAttribute('aria-pressed', String(b.dataset.step === step));
    const mine = CFD_METRICS.map((metric) => figs.find((p) => new RegExp(`_s${step}_${metric}\\.svg$`, 'i').test(p)))
      .filter(Boolean);
    setChildren(grid, figureGrid(mine, env, 'No maps for this step.'));
  };
  for (const step of steps) {
    bar.appendChild(el('button', {
      type: 'button', role: 'tab', dataset: { step }, 'aria-pressed': String(step === steps[0]),
      onclick: () => show(step),
    }, `step ${step}`));
  }

  setChildren(host, header,
    el('p', { class: 'small muted' },
      `Depth-averaged Hele-Shaw solve, ${steps.length} protocol step${steps.length === 1 ? '' : 's'} simulated`
      + `${chip.cfd.steps_simulated.length ? ` (${chip.cfd.steps_simulated.join(', ')})` : ''}. `
      + 'Hele-Shaw has no recirculation, so a trap here is a low-velocity or long-residence zone.'),
    el('div', { class: 'row', style: { margin: '8px 0' } }, el('span', { class: 'small muted' }, 'Step'), bar),
    grid,
    openfoamBlock(chip, env));
  show(steps[0]);
  return host;
}

function openfoamBlock(chip, env) {
  const host = el('div', { style: { marginTop: '10px' } });
  const have = chip.openfoam.length;
  const anim = chip.animation;
  setChildren(host,
    el('h4', '3D CFD and the end-to-end animation'),
    have
      ? figureGrid(chip.openfoam, env, '')
      : el('p', { class: 'muted small' }, badge('not yet run', 'todo'),
        ' The OpenFOAM 3D solve is not in docs/modules.json for this chip. The maps above are the '
        + 'reduced-order Hele-Shaw solve, which is the correct physics at Re well under 1 but is still 2D.'),
    anim ? mediaSlot(anim, env, 'Pipeline animation') : null);
  return host;
}

/** A <video> for a local render path or a release URL; never a third-party player. */
function mediaSlot(path, env, label) {
  const src = /^https?:\/\//i.test(String(path)) ? playableUrl(path) : artifactUrl(path, env.ctx.fileRunId);
  if (!src) return el('p', { class: 'muted small' }, `${label}: ${path} cannot be served yet.`);
  const v = el('video', {
    controls: true, preload: 'metadata', playsinline: true,
    style: { width: '100%', borderRadius: '10px', background: '#04070e', border: '1px solid var(--panel-border)' },
    src,
  });
  v.addEventListener('error', () => setChildren(v.parentElement, el('p', { class: 'muted small' }, `${label}: could not play ${path}`)));
  return el('div', { style: { marginTop: '8px' } }, el('h4', label), v);
}

function demoBlock(chip, stage) {
  const d = chip.demo;
  const host = el('div', {}, el('h4', 'Demo procedure'));
  if (!d) {
    host.appendChild(el('p', { class: 'muted small' },
      'No demo procedure recorded for this chip in docs/modules.json yet.'
      + (stage.protocol ? ` The full protocol is ${stage.protocol}.` : '')));
    return host;
  }
  if (typeof d === 'string') { host.appendChild(el('p', d)); return host; }
  const steps = Array.isArray(d.steps) ? d.steps : Array.isArray(d) ? d : null;
  if (steps) {
    host.appendChild(el('ol', { style: { paddingLeft: '20px' } },
      ...steps.map((s) => el('li', typeof s === 'string' ? s : String(s.text || s.step || JSON.stringify(s))))));
  }
  if (d.note) host.appendChild(el('p', { class: 'small muted' }, String(d.note)));
  if (!steps && !d.note) host.appendChild(el('p', { class: 'small mono muted' }, JSON.stringify(d).slice(0, 400)));
  return host;
}

/** The video slot: plays the team's recording, or says it is not uploaded. */
function videoBlock(chip, env) {
  const v = chip.video
    || (env.state.videos && (env.state.videos.byChip.get(chip.chip_id) || env.state.videos.byModule.get(chip.slot)))
    || null;
  const host = el('div', {}, el('div', { class: 'row' }, el('h4', { style: { margin: 0 } }, 'Bench recording')));

  if (!v) {
    host.appendChild(el('p', { class: 'muted small' }, 'No video slot for this chip in docs/videos/manifest.json.'));
    return host;
  }
  const url = playableUrl(v.url);
  if (url && v.recorded !== false) {
    const video = el('video', {
      controls: true, preload: 'metadata', playsinline: true,
      style: { width: '100%', maxHeight: '420px', borderRadius: '10px', background: '#000', border: '1px solid var(--panel-border)' },
      src: url,
    });
    const wrap = el('div', { style: { marginTop: '8px' } }, video);
    video.addEventListener('error', () => setChildren(wrap,
      el('p', { class: 'err' }, `Could not play the recording at ${url}`)));
    host.appendChild(el('p', { class: 'small' }, v.title || '', ' ', badge('measured, not simulated', 'ok',
      'This is a recording of the real chip, the only non-simulated thing on this card.')));
    host.appendChild(wrap);
    if (v.release_tag) host.appendChild(el('p', { class: 'small muted' }, `GitHub release ${v.release_tag}`));
  } else {
    host.appendChild(el('div', { class: 'empty', style: { textAlign: 'left', marginTop: '8px' } },
      el('h3', 'Recording not yet uploaded'),
      v.title ? el('p', { class: 'small' }, v.title) : null,
      v.shot ? el('p', { class: 'small muted' }, el('strong', 'Planned shot: '), v.shot) : null,
      v.shot_source ? el('p', { class: 'hint' }, `shot list ${v.shot_source}`) : null,
      el('p', { class: 'hint' }, 'The slot fills itself once the manifest entry has a URL - '
        + 'a GitHub Release asset, played by the browser, with no third-party player.')));
  }
  return host;
}

function limitsBlock(chip) {
  if (!chip.known_limits.length) return null;
  return el('details', { class: 'disclose', style: { marginTop: '10px' } },
    el('summary', `${chip.known_limits.length} known limit${chip.known_limits.length === 1 ? '' : 's'} of this module`),
    el('ul', { class: 'small', style: { paddingLeft: '20px', margin: 0 } },
      ...chip.known_limits.map((k) => el('li', {},
        el('span', {}, k.text),
        k.source ? el('div', { class: 'muted mono', style: { fontSize: '11px' } }, k.source) : null))));
}

/** The full-spec target design, beside the demo chip. */
function fullSpecBlock(chip, env) {
  const fs = chip.full_spec;
  const host = el('div', { style: { marginTop: '10px' } });
  if (!fs) {
    setChildren(host, el('h4', 'Full-spec target design'),
      el('p', { class: 'muted small' },
        'No full-spec target design for this module yet. The printed chip is the demo-scale version: ',
        demoBadge(), ' ', DEMO_TITLE));
    return host;
  }
  const facts = el('dl', { class: 'kv' });
  const add = (k, v) => { if (v) { facts.appendChild(el('dt', k)); facts.appendChild(el('dd', {}, v)); } };
  add('spec', fs.chip_path);
  add('chip id', fs.chip_id);
  add('scale', fs.scale);
  add('steps', fs.steps && fs.steps.length ? `${fs.steps.length} protocol steps` : null);
  setChildren(host,
    el('div', { class: 'row' }, el('h4', { style: { margin: 0 } }, 'Full-spec target design'),
      badge('FULL SPEC', 'info', 'The real device, not the printed demo coupon.')),
    el('div', { class: 'cols c-2', style: { marginTop: '8px' } },
      el('div', {}, el('h5', {}, 'Printed demo ', demoBadge()), planFor(chip, env)),
      el('div', {}, el('h5', {}, 'Full spec ', badge('FULL SPEC', 'info')), facts,
        fs.plan_svgs && fs.plan_svgs.length ? figureGrid(fs.plan_svgs, env, '') : planFor(fs, env))));
  return host;
}

/** Plan SVG from the artifacts if there is one, otherwise drawn from the live chip spec. */
function planFor(chip, env) {
  if (chip.plan_svgs && chip.plan_svgs.length) return figureGrid(chip.plan_svgs.slice(0, 1), env, '');
  const host = el('div', {}, el('p', { class: 'loading' }, 'Drawing the plan view...'));
  if (!chip.chip_id) { setChildren(host, el('p', { class: 'muted small' }, 'No chip id to draw.')); return host; }
  api.chip(chip.chip_id)
    .then((spec) => {
      if (env.isCancelled()) return;
      const { svg, legend } = chipPlan(spec, {});
      setChildren(host, svg, legend);
    })
    .catch((err) => {
      if (env.isCancelled()) return;
      setChildren(host, el('p', { class: 'muted small' },
        err instanceof ApiError && err.missing
          ? `${chip.chip_id} is not in /api/chips.`
          : explain(err)));
    });
  return host;
}

export { toast, download, clear };

// View 1: THE ASSAY. What this is and why, told as a drawing.
//
// Reads docs/modules.json (one entry per stage of the real chain) and docs/videos/
// manifest.json (the bench-recording slot per printed chip). The printed modules are
// frozen for the demo: this page presents them and proposes no change.

import { api, explain } from '../api.js';
import { el, svg, setChildren, badge, fmtNum, fmtSeconds, errorState, loading } from '../dom.js';
import { demoBadge, todoBadge, printedBadge, simBadge, stubBadge, SIM_TITLE, DEMO_TITLE, PRINTED_TITLE, STUB_TITLE, TODO_TITLE } from '../labels.js';
import { chipPlan } from '../chipsvg.js';
import { chipIso } from '../chipiso.js';
import { chainDiagram, legendGlyph, stageKind, shortName, KIND_WORDS } from '../diagram.js';
import { term, glossaryList } from '../glossary.js';
import { loadDocsJson, normModulesDoc, normVideoManifest, playableUrl, NEEDED_ROUTE } from '../ourassay.js';
import { cardBody } from './assay.js';

/** What each stage does, in plain words. Falls back to the catalogue's own text. */
const PLAIN = {
  module1: 'Grows the three cell types, each made from stem cells, in three separate lanes. The lanes are kept sealed while each type matures, then connected so the cells can signal to each other through the shared liquid, and finally all three are flushed out together through the middle junction.',
  cellprep: 'Loosens the cells from the chamber floor with an enzyme, stops the enzyme again, and gives them a small chamber where they can be counted.',
  bench_2b: 'At the bench: spin the cells down, pass them through a fine strainer to remove clumps, and resuspend them. Done in a tube because a strainer this fine cannot be printed.',
  module2: 'Where each cell is wrapped in its own capsule. The printed board shows the flow geometry only; the real 70 µm capsules are made on a bought droplet kit, because the nozzle needed is far smaller than our printers can make.',
  bench_3b: 'At the bench: break the oil emulsion the capsules were made in, wash the oil away and harden the capsule shells. The written protocol covers only the hardening, so part of this step is still a gap.',
  module3: 'Holds the capsules still while liquids are changed around them: the cell inside is broken open, washed, and its RNA is copied into DNA, which stays inside the capsule. Then the capsules are released.',
  barcoding: 'Gives every capsule its own barcode. Each capsule goes into its own oil droplet, where a single barcode molecule is copied many times and joined to that cell\'s DNA. The chemistry is ours; the droplet hardware is bought.',
  module4: 'Splits the capsules across four chambers, filled one at a time, and runs a PCR in each that adds a second, sublibrary label used to sort the data later.',
  module5: 'Lets the DNA out of the capsules and cleans it up with magnetic beads, leaving a library ready for checking.',
  spri_qc: 'Off the chip: keep only fragments of the right size, then check how much there is and its quality. No protocol has been written for this yet.',
  library_out: 'The finished, labelled library in a tube, ready to sequence.',
  sequencing: 'Read on a commercial sequencer. Every read carries its cell\'s barcode, so it can be traced back to one cell.',
  module6_bulk_omics: 'Optional side branch, not part of the single-cell path: some cells from the same chambers can be extracted separately with a cold solvent, to keep protein and small-molecule samples for mass spec or similar work later. Nothing here is built or run yet.',
};

export function mount(host, ctx) {
  let cancelled = false;
  const viewers = new Set();
  const specCache = new Map();

  setChildren(host, hero(), demoSection(), numbersStrip(), el('section', { class: 'section' }, loading('Reading the chain from docs/modules.json')));

  (async () => {
    const [mods, vids] = await Promise.all([
      loadDocsJson('docs/modules.json', ctx.fileRunId),
      loadDocsJson('docs/videos/manifest.json', ctx.fileRunId),
    ]);
    if (cancelled) return;
    const chainHost = el('section', { class: 'section', id: 'chain' });
    setChildren(host, hero(), demoSection(), numbersStrip(), chainHost, labelsSection(), glossarySection(), footer(ctx));
    if (mods.missing) {
      setChildren(chainHost, sectionHead('The chain, stage by stage', 'The drawing of the assay could not be loaded.'),
        el('div', { class: 'notice' },
          el('p', {}, 'docs/modules.json is not reachable. It is regenerated from the chip specs, so it may simply be '
            + 'mid-rebuild; reload in a moment.'),
          el('p', { class: 'small muted' }, `This section needs: ${NEEDED_ROUTE}.`)));
      return;
    }
    let doc;
    try { doc = normModulesDoc(mods.data); } catch (err) {
      setChildren(chainHost, errorState('docs/modules.json could not be read', el('p', explain(err))));
      return;
    }
    if (!doc.stages.length) {
      setChildren(chainHost, sectionHead('The chain, stage by stage', 'docs/modules.json has no stages in it yet.'),
        el('p', { class: 'muted' }, 'It is generated by tools/module_catalog.py; it may be mid-rebuild.'));
      return;
    }
    const videos = vids.missing ? null : normVideoManifest(vids.data);
    renderChain(chainHost, doc, videos);
  })().catch((err) => {
    if (!cancelled) setChildren(host, hero(), errorState('Could not load the assay', el('p', explain(err))));
  });

  function renderChain(sectionEl, doc, videos) {
    const printedIds = new Set(doc.printed.map((p) => p.chip_id));
    const detail = el('div', { class: 'stage-detail', 'aria-live': 'polite', id: 'stage-detail' });
    let current = null;

    const diagram = chainDiagram(doc, printedIds, (id) => open(id, true));
    const all = doc.stages;
    const printedCount = all.filter((s) => stageKind(s, printedIds) === 'printed').length;
    const chipCount = all.filter((s) => s.chip).length;

    setChildren(sectionEl,
      sectionHead('The chain, stage by stage',
        'Follow one sample from left to right. Click a stage, or tab to it and press Enter, to see what it does.'),
      el('p', { class: 'chain-sum' },
        `${all.length} stages: ${chipCount} on a chip, ${printedCount} of them `,
        term('printed', 'printed for the demo'), ', and the rest done ', term('offchip', 'off-chip'), '.'),
      el('div', { class: 'diagram-wrap' }, diagram.svg),
      el('p', { class: 'cap scroll-hint' }, 'The drawing is wider than this window: scroll it sideways to follow the whole chain.'),
      el('ul', { class: 'legend', 'aria-label': 'Legend' },
        ...[
          ['printed', 'a chip we printed'],
          ['chip', 'a chip, designed but not printed'],
          ['bench', 'done at the bench or on a bought kit'],
          ['todo', 'not built yet'],
          ['cells', 'loose cells travelling'],
          ['capsules', 'capsules, one cell in each'],
          ['library', 'DNA library'],
        ].map(([k, w]) => el('li', {}, legendGlyph(k), el('span', {}, w)))),
      el('p', { class: 'aside' }, 'Each ', term('capsule'), ' is the reason this is single-cell: one cell per capsule, and later one ',
        term('barcode'), ' per capsule. The single-cell ', term('readout', 'readouts'), ' at the bottom come off the end of the chain; the small footnote under them is optional side sampling.'),
      detail);

    const want = ctx.param && all.some((s) => s.id === ctx.param) ? ctx.param : all[0].id;
    open(want, false);

    function open(id, fromUser) {
      const s = all.find((x) => x.id === id);
      if (!s) return;
      current = id;
      diagram.select(id);
      if (fromUser) {
        try { history.replaceState(null, '', `#/assay/${encodeURIComponent(id)}`); } catch (_) {}
      }
      for (const v of viewers) { try { v.dispose(); } catch (_) {} }
      viewers.clear();
      setChildren(detail, ...stageDetail(s, doc, videos, printedIds, diagram.order, open));
      if (fromUser) detail.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
    return () => current;
  }

  /* ---------------------------------------------------------------- one stage */

  function stageDetail(s, doc, videos, printedIds, order, open) {
    const kind = stageKind(s, printedIds);
    const chip = s.chip;
    const idx = order.indexOf(s.id);
    const prev = order[(idx + order.length - 1) % order.length];
    const next = order[(idx + 1) % order.length];
    const byId = (id) => doc.stages.find((x) => x.id === id);
    const mainIds = doc.stages.filter((x) => !x.branch_from).map((x) => x.id);
    const lastMain = s.branch_from ? s.id : mainIds[mainIds.length - 1];

    const flags = el('div', { class: 'flags' },
      kind === 'printed' ? printedBadge() : null,
      kind === 'chip' ? badge('DESIGN ONLY', '', 'A chip design that was not printed for the demo.') : null,
      kind === 'bench' || kind === 'kit' ? badge('OFF-CHIP', 'off', 'Done at the bench or on a bought instrument.') : null,
      kind === 'todo' ? todoBadge() : null,
      chip && /demo/i.test(chip.scale) ? demoBadge() : null,
      chip && chip.placeholder ? badge('PLACEHOLDER DATA', 'warn') : null,
      /hole/i.test(s.kind) ? badge('GAP IN THE PROTOCOL', 'warn', 'Part of this step has no written protocol yet.') : null,
      s.status && !/off-chip|roadmap/i.test(s.status) ? badge(s.status, s.status === 'final' ? 'ok' : '') : null);

    const plain = PLAIN[s.id] || s.note || (chip && chip.chip_name) || s.where || '';
    const head = el('header', { class: 'sd-head' },
      el('p', { class: 'eyebrow' }, `Stage ${idx + 1} of ${order.length} · ${KIND_WORDS[kind]}`),
      el('h3', { class: 'sd-title' }, s.name || s.id),
      flags,
      plain ? el('p', { class: 'sd-lede' }, plain) : null);

    const out = [head];

    // what arrives -> this -> what leaves
    out.push(el('div', { class: 'sd-flow' },
      flowSide('Arrives', s.arrives, 'from', idx === 0 ? 'Nothing: the chain starts here.'
        : `Whatever leaves ${shortName(byId(prev))}, carried by hand. The catalogue records no tube or volume for it.`, byId),
      el('div', { class: 'sd-flow-me', 'aria-hidden': 'true' }, el('span', {}, shortName(s))),
      flowSide('Leaves', s.leaves, 'to', s.id === lastMain ? 'Nothing: the chain ends here.'
        : `On to ${shortName(byId(next))}, by hand. The catalogue records no tube or volume for it.`, byId)));

    if (chip) {
      const steps = chip.steps.length;
      const total = chip.steps.reduce((a, x) => a + (Number(x.duration_s) || 0), 0);
      out.push(el('div', { class: 'tiles' },
        tile(String(steps), steps === 1 ? 'protocol step' : 'protocol steps'),
        tile(total ? fmtDuration(total) : '-', 'total protocol time'),
        tile(String(chip.chambers.length), chip.chambers.length === 1 ? 'chamber' : 'chambers'),
        tile(String(chip.ports.length), 'ports'),
        tile(chip.depth_mm ? `${fmtNum(chip.depth_mm, 3)} mm` : '-', 'etch depth, one layer')));

      out.push(el('div', { class: 'sd-grid' },
        el('div', { class: 'sd-figure' }, figureBlock(chip)),
        el('div', {}, portsBlock(chip), videoBlock(chip, videos))));

      if (chip.known_limits.length) {
        out.push(el('details', { class: 'fold' },
          el('summary', {}, el('span', { class: 'fs-text' }, el('span', {}, `What this chip cannot do (${chip.known_limits.length})`),
            el('span', { class: 'fs-s' }, 'Recorded by its designers, so a demo chip is never mistaken for the real thing.'))),
          el('ul', { class: 'limits' }, ...chip.known_limits.map((k) => el('li', {}, k.text)))));
      }
      const deep = el('div', {});
      const fold = el('details', { class: 'fold' },
        el('summary', {}, el('span', { class: 'fs-text' }, el('span', {}, 'Full engineering detail'),
          el('span', { class: 'fs-s' }, 'Every protocol step with its volumes and timings, the flow maps and the 3D simulation, straight from the chip spec.'))),
        deep);
      let built = false;
      fold.addEventListener('toggle', () => {
        if (!fold.open || built) return;
        built = true;
        const env = { ctx, state: { videos }, viewers, isCancelled: () => cancelled, printedIds };
        setChildren(deep, ...cardBody(s, chip, kind === 'printed', env).filter(Boolean));
      });
      out.push(fold);
    } else {
      out.push(el('div', { class: 'offchip-card' },
        el('div', { class: 'oc-icon', 'aria-hidden': 'true' }, legendGlyph(kind === 'todo' ? 'todo' : 'bench')),
        el('div', {},
          el('h4', {}, kind === 'todo' ? 'Not built yet' : 'Why this is off-chip'),
          el('p', {}, kind === 'todo'
            ? 'This stage is on the roadmap: it is in the chain because the sample has to pass through it, but nothing for it exists in the repo yet.'
            : 'It needs a feature smaller than our printers can make, or a step that is simpler and more reliable in a tube. There is no chip, so no plan view and no flow simulation.'),
          s.where ? el('p', { class: 'small muted' }, `Where: ${s.where}`) : null,
          s.protocol ? el('p', { class: 'small muted' }, `Protocol: ${s.protocol}`) : null)));
    }

    out.push(el('nav', { class: 'sd-nav', 'aria-label': 'Step through the chain' },
      el('button', { type: 'button', class: 'ghost', onclick: () => { open(prev, true); } }, '← ', shortName(byId(prev))),
      el('span', { class: 'spacer' }),
      el('button', { type: 'button', class: 'ghost', onclick: () => { open(next, true); } }, shortName(byId(next)), ' →')));
    return out;
  }

  function flowSide(label, list, dir, empty, byId) {
    return el('div', { class: `sd-flow-side ${dir}` },
      el('p', { class: 'eyebrow' }, label),
      list.length
        ? el('ul', {}, ...list.map((h) => {
          const other = h[dir] && h[dir].stage ? byId(h[dir].stage) : null;
          return el('li', {},
            el('strong', {}, h.carries || 'material'),
            el('span', { class: 'muted' }, ` ${dir} `, other ? shortName(other) : (h[dir] && h[dir].stage) || 'elsewhere'),
            h.particle_um ? el('span', { class: 'muted' }, ` · ${fmtNum(h.particle_um, 3)} µm`) : null,
            h.automated === false ? el('span', {}, ' ', badge('needs a person', 'bad')) : null,
            h.note ? el('span', { class: 'note' }, h.note) : null);
        }))
        : el('p', { class: 'muted' }, empty));
  }

  function tile(value, label) {
    return el('div', { class: 'tile' }, el('span', { class: 'tile-v' }, value), el('span', { class: 'tile-l' }, label));
  }

  function figureBlock(chip) {
    const host = el('div', {}, el('p', { class: 'loading' }, 'Drawing the chip...'));
    const draw = (spec) => {
      const plan = chipPlan(spec, {});
      const iso = chipIso(spec);
      const stage = el('div', { class: 'fig-stage' }, plan.svg);
      const seg = el('div', { class: 'segment', role: 'group', 'aria-label': 'Chip view' });
      const views = [['Plan', () => setChildren(stage, plan.svg)], ['Tilted', () => setChildren(stage, iso)]];
      for (const [name, fn] of views) {
        const b = el('button', {
          type: 'button', 'aria-pressed': String(name === 'Plan'),
          onclick: () => { for (const o of seg.children) o.setAttribute('aria-pressed', String(o === b)); fn(); },
        }, name);
        seg.appendChild(b);
      }
      setChildren(host,
        el('div', { class: 'fig-head' }, el('p', { class: 'eyebrow' }, 'The chip, from above'), seg),
        stage, plan.legend,
        el('p', { class: 'cap' }, 'Drawn live from ', el('span', { class: 'mono' }, chip.chip_path || chip.chip_id),
          '. One flat layer at one depth, so the view from above is the whole design. The tilted view exaggerates depth eight times.'));
    };
    if (specCache.has(chip.chip_id)) { draw(specCache.get(chip.chip_id)); return host; }
    api.chip(chip.chip_id).then((spec) => {
      if (cancelled) return;
      specCache.set(chip.chip_id, spec);
      draw(spec);
    }).catch((err) => {
      if (!cancelled) setChildren(host, el('p', { class: 'muted small' }, `Could not draw ${chip.chip_id}: ${explain(err)}`));
    });
    return host;
  }

  function portsBlock(chip) {
    if (!chip.ports.length) return null;
    const ins = chip.ports.filter((p) => /inlet/i.test(p.type));
    const outs = chip.ports.filter((p) => !/inlet/i.test(p.type));
    const pill = (p) => el('li', { class: 'port-pill', title: `${p.id} - ${p.type}${p.role ? `, ${p.role}` : ''}` },
      el('span', { class: 'mono' }, p.id), p.role ? el('span', { class: 'muted' }, ` ${p.role}`) : null);
    return el('div', { class: 'ports-block' },
      el('p', { class: 'eyebrow' }, 'Ports'),
      el('p', { class: 'cap' }, 'Holes drilled through the lid where tubing connects. Liquid goes in at the left, out at the right.'),
      el('div', { class: 'ports-row' },
        el('ul', { class: 'ports-col' }, ...ins.map(pill)),
        el('div', { class: 'ports-chip', 'aria-hidden': 'true' }, el('span', {}, 'chip')),
        el('ul', { class: 'ports-col' }, ...outs.map(pill))));
  }

  function videoBlock(chip, videos) {
    const v = chip.video || (videos && (videos.byChip.get(chip.chip_id) || videos.byModule.get(chip.slot))) || null;
    if (!v) return null;
    const url = playableUrl(v.url);
    const head = el('p', { class: 'eyebrow' }, 'Bench recording');
    if (url && v.recorded !== false) {
      const video = el('video', { controls: true, preload: 'metadata', playsinline: true, src: url, class: 'video' });
      const wrap = el('div', { class: 'video-slot' }, video,
        el('p', { class: 'cap' }, 'A recording of the real printed chip: the only measured thing on this page.'));
      video.addEventListener('error', () => setChildren(wrap, el('p', { class: 'muted small' }, 'The recording could not be played.')));
      return el('div', { class: 'video-block' }, head, wrap);
    }
    return el('div', { class: 'video-block' }, head,
      el('div', { class: 'video-slot empty-slot' },
        el('span', { class: 'play', 'aria-hidden': 'true' }),
        el('div', {},
          el('strong', {}, 'Recording not yet uploaded'),
          v.shot ? el('p', { class: 'cap' }, `Planned shot: ${v.shot}`) : null)));
  }

  return () => {
    cancelled = true;
    for (const v of viewers) { try { v.dispose(); } catch (_) {} }
  };
}

/* ------------------------------------------------------------------ static parts */

function fmtDuration(s) {
  const v = Number(s);
  if (!Number.isFinite(v)) return '-';
  if (v < 120) return fmtSeconds(v);
  if (v < 7200) return `${Math.round(v / 60)} min`;
  if (v < 172800) return `${Math.round(v / 3600)} h`;
  return `${Math.round(v / 86400)} days`;
}

function hero() {
  const step = (n, h, t) => el('li', { class: 'claim-step' },
    el('span', { class: 'cs-n', 'aria-hidden': 'true' }, n), el('strong', {}, h), el('span', {}, t));
  return el('section', { class: 'hero hero-tight' },
    el('p', { class: 'eyebrow' }, 'The assay'),
    el('h1', { class: 'display' },
      'Perturb-seq screens what a gene does to a cell. This is a way to screen what a cell does to a cell.'),
    el('p', { class: 'lede' },
      'There is no equivalent for cell-to-cell effects. Co-culture loses track of which cell came from where, '
      + 'and you cannot control who touches whom, or when. This platform grows three cell types apart, '
      + 'so each has a clean baseline, lets them talk on cue in a chosen direction, then reads every cell individually.'),
    el('ol', { class: 'claim-steps', 'aria-label': 'How it works' },
      step('1', 'Grow apart', 'Neurons, microglia and astrocytes in separate sealed chambers: a clean baseline for each.'),
      step('2', 'Talk on cue', 'Open the connections when you choose, in the direction you choose.'),
      step('3', 'Read every cell', 'One cell per capsule, its own barcode, plus a label for the chamber it came from.')),
    el('p', { class: 'claim-line' },
      el('strong', {}, 'The capability: '),
      'single-cell libraries from co-cultured cells, with the cellular context preserved and controlled. '
      + 'Nobody has demonstrated this yet; no cells have run in these chips so far.'),
    el('p', { class: 'cap hero-foot' },
      'Optional, and not the focus: the same chambers can also be sampled separately for protein and metabolite work later, for example mass spec.'));
}

const STILLS = [
  ['media/chip-filled-1.jpg', 'Photo: the printed chip held in a hand, filled, pink and yellow dye in neighbouring chambers.',
    'A hand holding the printed chip over lined paper, filled with dye: pink in the upper and left chambers, yellow in the one beside it',
    'Printed chip, food dye. Pink and yellow held apart in neighbouring chambers.'],
  ['media/chip-filled-2.jpg', 'Photo: the printed chip held between two hands, dye-filled channels and chambers visible.',
    'Two hands holding the clear printed chip up to the camera, its channels and chambers filled with pink dye',
    'Printed chip, food dye. The channel network filled from the inlet.'],
  ['media/printed-chip-dye-two-reagents.jpg', 'Photo: pink food dye in one chamber, yellow in the chamber next to it.',
    'The printed Module 1 chip holding pink food dye in one chamber and yellow in the chamber next to it, the two kept apart',
    'Printed Module 1 chip, food dye. Two reagents in neighbouring chambers, walls holding.'],
  ['media/printed-chip-dye-filled-network.jpg', 'Photo: the printed Module 1 chip flooded with pink food dye.',
    'The printed Module 1 chip flooded with pink food dye: channels, chambers and ports filled from one inlet',
    'Printed Module 1 chip, food dye. Channels, chambers and ports filled from one inlet.'],
  ['media/chip-master-1.jpg', 'Photo: a set of printed masters (parts) lying on a laptop.',
    'Six clear printed parts with chamber and channel outlines lying on a laptop keyboard',
    'Printed masters and other printed parts. Hardware only; nothing is filled.'],
  ['media/chip-master-2.jpg', 'Photo: printed masters in front of a laptop showing the digital twin.',
    'Printed parts on a laptop in front of the digital twin on screen',
    'Printed parts beside the digital twin on screen. The screen is the simulation; the plastic is real.'],
];

const SIM_STILLS = [
  ['media/simulated-flow-module1.jpg', 'Module 1: simulated flow field and streamlines from its feed ports.'],
  ['media/simulated-flow-module3.jpg', 'Module 3: capsule trap during the lysis-buffer exchange, flagged for the 3D solve.'],
  ['media/simulated-flow-module4.jpg', 'Module 4: the sublibrary split, flagged where the reduced-order model runs out of validity.'],
];

function demoVideo(src, poster, label) {
  const v = el('video', {
    class: 'demo-video', controls: true, muted: true, playsinline: true, preload: 'metadata', poster,
    'aria-label': label,
  }, el('source', { src, type: 'video/mp4' }));
  v.muted = true;
  return v;
}

function demoSection() {
  const proves = [
    'A printed chip exists: these are photographs of physical objects, held in a hand.',
    'Dye entered at one inlet and reached the channels and chambers.',
    'Pink and yellow dye sit in neighbouring chambers, which the team reads as the walls holding. Keeping neighbours apart until you choose is the baseline everything else depends on.',
  ];
  const doesNot = [
    'Any cell biology. No cells have been in these chips.',
    'Cell-to-cell signalling, capsule formation, or single-cell libraries: those are the goal, not a result.',
    'Anything about the other chips in the chain.',
    'Any thermal step, such as heating or PCR cycling.',
    'That the real reagents are compatible with the printed material.',
  ];

  const sim = el('figure', { class: 'demo-fig twin' },
    el('div', { class: 'demo-tag' },
      badge('SIMULATED FLOW', 'warn', 'A computer simulation of the chips, not a recording of hardware.'),
      badge('reduced-order (2.5D)', '', 'The fast depth-averaged solver, not the full 3D OpenFOAM solve.')),
    demoVideo('media/simulated-flow-pipeline-reduced-order-2p5d.mp4', 'media/simulated-flow-whole-chain.jpg',
      'Simulated flow through the whole chain of chips, reduced-order 2.5D, 89 seconds'),
    el('figcaption', { class: 'cap' },
      el('strong', {}, 'Simulated flow, whole chain, 89 s. '),
      'All eight chips on a bench in chain order with the nine handoff tubes, each chip\'s flow field and streamlines from its feed ports. '
      + 'Every chip here comes from the fast reduced-order (2.5D) solver; nothing here is a 3D solve and nothing here is hardware.'));

  const bench = el('div', { class: 'photo-grid' },
    ...STILLS.map(([src, cap, alt, short]) => el('figure', { class: 'photo' },
      el('a', { href: src, target: '_blank', rel: 'noopener' },
        el('img', { src, alt, loading: 'lazy' }),
        badge('REAL HARDWARE', 'ok', 'A photograph of a physical printed object.')),
      el('figcaption', { class: 'cap' }, short))));

  return el('section', { class: 'section demo', id: 'demo', 'aria-labelledby': 'demo-h' },
    el('header', { class: 'sec-head' },
      el('h2', { id: 'demo-h' }, 'What exists today'),
      el('p', { class: 'sub' }, 'Real printed chips, photographed. They hold food dye, not cells: this shows the hardware keeps neighbouring chambers apart, and nothing yet about biology.')),
    el('div', { class: 'demo-tag' }, badge('REAL HARDWARE', 'ok', 'Photographs of physical printed chips and parts.'),
      badge('PHOTOS, NOT VIDEO', ''), badge('printed chip, food dye, not cells', '')),
    bench,
    el('p', { class: 'cap' }, 'The team\'s own photographs, from their presentation. There is no bench video of the dye running yet.'),
    el('div', { class: 'demo-claims' },
      el('div', { class: 'claim yes' },
        el('h3', {}, 'What the photos show'),
        el('ul', {}, ...proves.map((t) => el('li', {}, t)))),
      el('div', { class: 'claim no' },
        el('h3', {}, 'What they do not show'),
        el('ul', {}, ...doesNot.map((t) => el('li', {}, t))))),
    el('div', { class: 'demo-more' },
      el('p', { class: 'eyebrow' }, 'Simulation, for context'),
      sim,
      el('div', { class: 'sim-strip' },
        ...SIM_STILLS.map(([src, cap]) => el('figure', { class: 'still tall' },
          el('a', { href: src, target: '_blank', rel: 'noopener' }, el('img', { src, alt: cap, loading: 'lazy' })),
          el('figcaption', { class: 'cap' }, cap)))),
      el('figure', { class: 'demo-fig twin' },
        el('div', { class: 'demo-tag' }, badge('DIGITAL TWIN - SIMULATION, NOT LIVE HARDWARE', 'warn', 'A screen recording of the browser digital twin. No hardware appears in it.')),
        demoVideo('media/capsule-processor-demo.mp4', 'media/capsule-processor-poster.jpg',
          'Digital-twin walkthrough of the capsule processor, 2 minutes 7 seconds'),
        el('figcaption', { class: 'cap' },
          el('strong', {}, 'Digital twin walkthrough, 2 min 7 s. '),
          'A screen recording of the browser digital twin, titled \u201cCellular Perturb-seq on a chip\u201d. '
          + 'Its own footer reads \u201cSimulation: the digital twin, not live hardware\u201d. No physical chip appears in it.'))));
}

function sectionHead(title, sub) {
  return el('header', { class: 'sec-head' }, el('h2', {}, title), el('p', { class: 'sub' }, sub));
}

/** The numbers that matter, each drawn so the size can be seen. */
function numbersStrip() {
  return el('section', { class: 'section numbers', 'aria-label': 'Scale of the assay' },
    sectionHead('How much one run holds', 'Approximate design figures for the full-size device. Not measurements.'),
    el('div', { class: 'num-grid' },
      numCard('≈ 2,000', 'cells per condition', 'Each dot is one cell.', cellField()),
      numCard('≈ 10', 'conditions per well', 'Each square is one condition of about 2,000 cells, in one well.', wellPicture()),
      numCard('≈ 80', 'conditions per commercial chip', 'Each square is one condition. The bright ten are about one well\'s worth.', chipGrid())));
}

function numCard(value, label, caption, picture) {
  return el('figure', { class: 'num-card' },
    el('div', { class: 'num-pic' }, picture),
    el('figcaption', {},
      el('span', { class: 'num-v' }, value),
      el('span', { class: 'num-l' }, label),
      el('span', { class: 'cap' }, caption)));
}

function cellField() {
  // 2,000 dots as one path: 50 columns x 40 rows, slightly jittered so it reads as cells.
  const cols = 50;
  const rows = 40;
  let d = '';
  let seed = 7;
  const rnd = () => { seed = (seed * 9301 + 49297) % 233280; return seed / 233280; };
  for (let r = 0; r < rows; r += 1) {
    for (let c = 0; c < cols; c += 1) {
      const x = 4 + c * 4.8 + (rnd() - 0.5) * 1.6;
      const y = 4 + r * 4.8 + (rnd() - 0.5) * 1.6;
      d += `M${x.toFixed(1)} ${y.toFixed(1)}h0.01`;
    }
  }
  return svg('svg', { viewBox: '0 0 248 200', class: 'pic', role: 'img', 'aria-label': '2,000 dots, one per cell' },
    svg('path', { d, class: 'dots' }));
}

function wellPicture() {
  const g = svg('svg', { viewBox: '0 0 248 200', class: 'pic', role: 'img', 'aria-label': 'One well holding ten conditions' });
  g.appendChild(svg('circle', { cx: 124, cy: 100, r: 92, class: 'well' }));
  const pos = [];
  for (let r = 0; r < 2; r += 1) for (let c = 0; c < 5; c += 1) pos.push([54 + c * 30, 70 + r * 36]);
  pos.forEach(([x, y], i) => g.appendChild(svg('rect', { x, y, width: 22, height: 26, rx: 5, class: i === 0 ? 'cond hot' : 'cond' })));
  g.appendChild(svg('text', { x: 124, y: 160, class: 'pic-label', 'text-anchor': 'middle' }, 'one well'));
  return g;
}

function chipGrid() {
  const g = svg('svg', { viewBox: '0 0 248 200', class: 'pic', role: 'img', 'aria-label': 'Eighty conditions on one chip' });
  g.appendChild(svg('rect', { x: 6, y: 14, width: 236, height: 172, rx: 14, class: 'well' }));
  for (let i = 0; i < 80; i += 1) {
    const c = i % 10;
    const r = Math.floor(i / 10);
    g.appendChild(svg('rect', { x: 20 + c * 21.6, y: 28 + r * 18.4, width: 15, height: 12, rx: 3, class: i < 10 ? 'cond hot' : 'cond' }));
  }
  return g;
}

function labelsSection() {
  const row = (b, text) => el('li', {}, el('span', { class: 'lab' }, b), el('span', {}, text));
  return el('section', { class: 'section' },
    sectionHead('How to read the labels', 'Wherever a number could be mistaken for a fact it is not, it carries one of these.'),
    el('ul', { class: 'label-list' },
      row(simBadge(), `${SIM_TITLE} Shown in amber.`),
      row(demoBadge(), DEMO_TITLE),
      row(printedBadge(), PRINTED_TITLE),
      row(stubBadge(), STUB_TITLE),
      row(todoBadge(), TODO_TITLE)));
}

function glossarySection() {
  return el('section', { class: 'section', id: 'glossary' },
    sectionHead('Words used on this site', 'Underlined words anywhere on the site show these definitions when you hover or focus them.'),
    glossaryList());
}

function footer(ctx) {
  return el('section', { class: 'section next-steps' },
    el('a', { class: 'next-card', href: '#/run' },
      el('span', { class: 'eyebrow' }, 'Next'),
      el('span', { class: 'next-t' }, 'Run it'),
      el('span', { class: 'cap' }, 'Give the software a protocol, or re-check these chips with the flow simulation.')),
    el('a', { class: 'next-card', href: ctx.state.runId ? `#/results/${encodeURIComponent(ctx.state.runId)}` : '#/results' },
      el('span', { class: 'eyebrow' }, 'Or'),
      el('span', { class: 'next-t' }, 'See results'),
      el('span', { class: 'cap' }, 'The chips a run made, what the simulation found, and how to fix it.')),
    el('a', { class: 'next-card', href: '/twin/index.html' },
      el('span', { class: 'eyebrow' }, 'Elsewhere'),
      el('span', { class: 'next-t' }, 'Digital twin'),
      el('span', { class: 'cap' }, 'The same chips in 3D, animated through their protocol.')));
}

// View 3: Steps. Every assay step and where it ended up - which chip, off chip, or a
// missing part - with the unresolved questions from extraction at the top, because a
// guessed number that reads as fact is the failure this repo is fighting.

import { explain } from '../api.js';
import { el, clear, setChildren, badge, fmtNum, emptyState, errorState, loading } from '../dom.js';
import { stubBadge, demoBadge, todoBadge } from '../labels.js';
import { loadArtifacts, loadStageJson } from '../locate.js';

const PLACEMENTS = ['on_chip', 'off_chip', 'missing_part'];
const PLACE_WORDS = { on_chip: 'on chip', off_chip: 'off chip', missing_part: 'missing part', unknown: 'not triaged' };

export function mount(host, ctx) {
  const runId = ctx.runId;
  setChildren(host, el('div', { class: 'view-head' }, el('h2', 'Steps'), el('span', { class: 'sub' }, runId)), loading('Reading the extraction'));
  let cancelled = false;

  (async () => {
    let artifacts = [];
    try { artifacts = await loadArtifacts(runId); }
    catch (err) { if (!cancelled) setChildren(host, errorState('Could not list this run\'s files', el('p', explain(err)))); return; }

    const [assay, unitops, partition] = await Promise.all([
      loadStageJson(runId, 'assay.json', artifacts),
      loadStageJson(runId, 'unitops.json', artifacts),
      loadStageJson(runId, 'partition.json', artifacts),
    ]);
    if (cancelled) return;
    render(host, ctx, { runId, assay, unitops, partition });
  })();

  return () => { cancelled = true; };
}

function render(host, ctx, { runId, assay, unitops, partition }) {
  const steps = Array.isArray(assay.data && assay.data.steps) ? assay.data.steps : [];
  const ops = Array.isArray(unitops.data && unitops.data.ops) ? unitops.data.ops : [];
  const chips = Array.isArray(partition.data && partition.data.chips) ? partition.data.chips : [];
  const handoffs = Array.isArray(partition.data && partition.data.handoffs) ? partition.data.handoffs : [];
  const cuts = Array.isArray(partition.data && partition.data.cuts) ? partition.data.cuts : [];
  const unresolved = Array.isArray(assay.data && assay.data.unresolved) ? assay.data.unresolved : [];

  const head = el('div', { class: 'view-head' },
    el('h2', 'Steps'),
    el('span', { class: 'sub' }, runId),
    el('span', { class: 'spacer' }),
    assay.missing ? todoBadge() : null);

  if (!steps.length && !ops.length) {
    setChildren(host, head, emptyState('Extraction has not produced steps yet',
      el('p', 'S1 writes assay.json and S2/S3 write unitops.json. Neither is readable for this run yet.'),
      el('p', { class: 'small muted' }, `Tried: ${[...(assay.tried || []), ...(unitops.tried || [])].join(', ')}`),
      el('p', {}, el('button', { type: 'button', onclick: () => ctx.navigate('run', runId) }, 'Open the stage timeline'))));
    return;
  }

  /* -------------------------------------------------------- joins */

  // op -> chip
  const chipOf = new Map();
  for (const c of chips) {
    for (const o of (Array.isArray(c.ops) ? c.ops : [])) chipOf.set(String(o), c);
  }
  // step -> ops
  const opsOfStep = new Map();
  for (const op of ops) {
    const from = Array.isArray(op.from_steps) ? op.from_steps : (op.from_steps ? [op.from_steps] : []);
    for (const s of from) {
      const k = String(s);
      if (!opsOfStep.has(k)) opsOfStep.set(k, []);
      opsOfStep.get(k).push(op);
    }
  }

  const rows = [];
  for (const s of steps) {
    const key = String(s.step ?? s.index ?? '');
    const linked = opsOfStep.get(key) || [];
    if (!linked.length) rows.push({ step: s, op: null, chip: null, where: 'unknown' });
    else for (const op of linked) {
      const where = String((op.placement && op.placement.where) || 'unknown');
      rows.push({ step: s, op, chip: chipOf.get(String(op.op)) || null, where: PLACEMENTS.includes(where) ? where : 'unknown' });
    }
  }
  // ops that no step claims (normalisation can merge or add)
  for (const op of ops) {
    const from = Array.isArray(op.from_steps) ? op.from_steps : [];
    const claimed = from.some((s) => steps.some((st) => String(st.step) === String(s)));
    if (claimed) continue;
    const where = String((op.placement && op.placement.where) || 'unknown');
    rows.push({ step: null, op, chip: chipOf.get(String(op.op)) || null, where: PLACEMENTS.includes(where) ? where : 'unknown' });
  }

  const counts = { on_chip: 0, off_chip: 0, missing_part: 0, unknown: 0 };
  for (const r of rows) counts[r.where] += 1;
  const manual = handoffs.filter((h) => !h.automated);

  /* -------------------------------------------------------- unresolved */

  const needs = ops
    .filter((o) => o.placement && o.placement.where === 'missing_part' && o.placement.need)
    .map((o) => ({ step: `op ${o.op}`, question: `needs a part that does not exist: ${o.placement.need}` }));
  const lowConf = steps
    .filter((s) => Number.isFinite(Number(s.confidence)) && Number(s.confidence) < 0.5)
    .map((s) => ({ step: s.step, question: `low extraction confidence (${fmtNum(s.confidence, 2)})${s.notes ? `: ${s.notes}` : ''}` }));
  const allOpen = [...unresolved, ...needs, ...lowConf];

  const openPanel = allOpen.length
    ? el('section', { class: 'unresolved' },
      el('h3', {}, `Unresolved: ${allOpen.length} question${allOpen.length === 1 ? '' : 's'} the source never answered`),
      el('p', { class: 'small' }, 'These are gaps, not guesses. Nothing downstream invented a value for them - a stage that needed one used a contract default and flagged it.'),
      el('ol', {}, ...allOpen.map((u) => el('li', {},
        u.step !== undefined && u.step !== null ? el('strong', {}, `${typeof u.step === 'number' ? `step ${u.step}` : String(u.step)}: `) : null,
        el('span', { class: 'q' }, String(u.question || u.text || u.note || ''))))))
    : el('section', { class: 'panel' },
      el('h3', { style: { marginTop: 0 } }, 'Unresolved questions'),
      el('p', { class: 'muted' }, assay.missing
        ? 'assay.json is not readable yet, so the open questions are unknown.'
        : 'None recorded. Extraction reported no gaps for this source.'));

  /* -------------------------------------------------------- summary */

  const summary = el('section', { class: 'panel' },
    el('h3', { style: { marginTop: 0 } }, 'Where the assay ended up'),
    el('div', { class: 'counters' },
      counter(steps.length, 'assay steps'),
      counter(ops.length, 'unit operations'),
      ...PLACEMENTS.map((p) => counter(counts[p], PLACE_WORDS[p], p)),
      counts.unknown ? counter(counts.unknown, PLACE_WORDS.unknown, 'unknown') : null,
      counter(chips.length, 'chips')),
    handoffs.length
      ? el('p', { style: { marginTop: '10px' } },
        `${handoffs.length} handoff${handoffs.length === 1 ? '' : 's'} between chips, `,
        manual.length
          ? el('strong', { style: { color: '#fca5a5' } }, `${manual.length} of which a machine cannot do`)
          : el('strong', { style: { color: '#86efac' } }, 'all automated'),
        '. The stated goal is no human intervention, so a pipetting step counts against it.')
      : null,
    cuts.length
      ? el('details', { class: 'disclose', style: { marginTop: '10px' } },
        el('summary', `Why the assay was cut into ${chips.length} chips (${cuts.length} cut${cuts.length === 1 ? '' : 's'})`),
        el('ul', {}, ...cuts.map((c) => el('li', {},
          el('span', { class: 'mono' }, `between ops ${Array.isArray(c.between) ? c.between.join(' and ') : String(c.between)}: `),
          String(c.why || '')))))
      : null);

  /* -------------------------------------------------------- table */

  const filterState = { where: '', q: '' };
  const tbody = el('tbody');

  function paintRows() {
    clear(tbody);
    const shown = rows.filter((r) => {
      if (filterState.where && r.where !== filterState.where) return false;
      if (!filterState.q) return true;
      const hay = [
        r.step && r.step.text, r.step && r.step.action, r.step && r.step.target,
        r.op && r.op.kind, r.op && r.op.placement && r.op.placement.part,
        r.op && r.op.placement && r.op.placement.why, r.chip && r.chip.chip_id,
      ].filter(Boolean).join(' ').toLowerCase();
      return hay.includes(filterState.q.toLowerCase());
    });
    if (!shown.length) {
      tbody.appendChild(el('tr', {}, el('td', { colspan: '7', class: 'muted' }, 'Nothing matches that filter.')));
      return;
    }
    for (const r of shown) tbody.appendChild(stepRow(r, ctx));
  }

  const table = el('div', { class: 'scroll-x' },
    el('table', { class: 'data' },
      el('thead', {}, el('tr', {},
        el('th', 'Step'), el('th', 'What the source says'), el('th', 'Action'),
        el('th', 'Unit op'), el('th', 'Placement'), el('th', 'Chip'), el('th', 'Why'))),
      tbody));

  const filters = el('div', { class: 'row', style: { marginBottom: '10px' } },
    el('div', { class: 'segment', role: 'radiogroup', 'aria-label': 'Filter by placement' },
      ...['', ...PLACEMENTS, 'unknown'].map((p) => {
        const b = el('button', {
          type: 'button', role: 'radio', 'aria-checked': String(filterState.where === p), 'aria-pressed': String(filterState.where === p),
          onclick: () => {
            filterState.where = p;
            for (const o of b.parentElement.children) { o.setAttribute('aria-pressed', 'false'); o.setAttribute('aria-checked', 'false'); }
            b.setAttribute('aria-pressed', 'true');
            b.setAttribute('aria-checked', 'true');
            paintRows();
          },
        }, p ? `${PLACE_WORDS[p]} (${counts[p]})` : `all (${rows.length})`);
        return b;
      })),
    el('label', { class: 'row', style: { gap: '6px' } },
      el('span', { class: 'small muted' }, 'Search'),
      el('input', { type: 'text', placeholder: 'reagent, action, chip...', oninput: (e) => { filterState.q = e.target.value; paintRows(); } })));

  paintRows();

  /* -------------------------------------------------------- handoffs */

  const handoffPanel = handoffs.length
    ? el('section', { class: 'panel' },
      el('h3', { style: { marginTop: 0 } }, 'Handoffs between chips'),
      el('div', { class: 'scroll-x' }, el('table', { class: 'data' },
        el('thead', {}, el('tr', {},
          el('th', 'From'), el('th', 'To'), el('th', 'Carries'), el('th', { class: 'num' }, 'Volume'),
          el('th', { class: 'num' }, 'Particle'), el('th', 'Automated'), el('th', 'How / risks'))),
        el('tbody', {}, ...handoffs.map((h) => el('tr', {},
          el('td', { class: 'mono' }, `${h.from || '?'}${h.from_port ? `.${h.from_port}` : ''}`),
          el('td', { class: 'mono' }, `${h.to || '?'}${h.to_port ? `.${h.to_port}` : ''}`),
          el('td', {}, String(h.carries || '')),
          el('td', { class: 'num' }, h.volume_ul !== undefined ? `${fmtNum(h.volume_ul, 4)} uL` : '-'),
          el('td', { class: 'num' }, h.particle_um !== undefined ? `${fmtNum(h.particle_um, 4)} um` : '-'),
          el('td', {}, h.automated ? badge('machine', 'ok') : badge('needs a human', 'bad', 'Pipetting counts as human intervention.')),
          el('td', { class: 'small' },
            String(h.how || ''),
            Array.isArray(h.risks) && h.risks.length
              ? el('ul', { style: { margin: '4px 0 0', paddingLeft: '18px' } }, ...h.risks.map((r) => el('li', String(r))))
              : null)))))))
    : null;

  const sourcePanel = el('section', { class: 'panel' },
    el('h4', 'Where this came from'),
    el('dl', { class: 'kv' },
      el('dt', 'assay.json'), el('dd', {}, assay.path || missingNote(assay)),
      el('dt', 'unitops.json'), el('dd', {}, unitops.path || missingNote(unitops)),
      el('dt', 'partition.json'), el('dd', {}, partition.path || missingNote(partition))),
    el('p', { class: 'hint' }, 'Step text is quoted verbatim from the source and shown as plain text, never as markup.'));

  setChildren(host, head, openPanel, summary,
    el('section', { class: 'panel' }, el('h3', { style: { marginTop: 0 } }, 'Step by step'), filters, table),
    handoffPanel, sourcePanel);
}

function stepRow(r, ctx) {
  const s = r.step;
  const op = r.op;
  const place = r.where;
  const chip = r.chip;
  const conf = s && Number(s.confidence);

  return el('tr', {},
    el('td', { class: 'num' }, s ? String(s.step ?? '') : el('span', { class: 'muted', title: 'added or merged during normalisation' }, '-')),
    el('td', { style: { maxWidth: '34ch' } },
      s ? el('span', {}, String(s.text || '')) : el('span', { class: 'muted' }, 'no source sentence'),
      s && s.quote_page ? el('span', { class: 'small muted' }, ` (p${s.quote_page})`) : null,
      Number.isFinite(conf) ? el('div', { class: 'small muted', style: { marginTop: '3px' } },
        'confidence ', confBar(conf), ` ${fmtNum(conf, 2)}`) : null,
      s && s.notes ? el('div', { class: 'small', style: { color: '#fde68a', marginTop: '3px' } }, String(s.notes)) : null),
    el('td', { class: 'small' },
      s ? String(s.action || '') : '',
      s && s.temp_c !== undefined && s.temp_c !== null ? el('div', { class: 'muted' }, `${fmtNum(s.temp_c, 4)} C`) : null,
      s && s.duration_s ? el('div', { class: 'muted' }, `${fmtNum(s.duration_s, 4)} s`) : null,
      s && Array.isArray(s.reagents) && s.reagents.length
        ? el('div', { class: 'muted' }, s.reagents.map((g) => String(g.name || '')).filter(Boolean).join(', '))
        : null),
    el('td', { class: 'small' },
      op ? el('span', { class: 'mono' }, `op ${op.op}`) : el('span', { class: 'muted' }, 'not normalised'),
      op ? el('div', {}, String(op.kind || '')) : null,
      op && op.particles && op.particles.kind && op.particles.kind !== 'none'
        ? el('div', { class: 'muted' }, `${op.particles.kind}${op.particles.diameter_um ? ` ${fmtNum(op.particles.diameter_um, 3)} um` : ''}`)
        : null,
      op && op.shear_class && op.shear_class !== 'none'
        ? el('div', { class: 'muted', title: 'picks which shear ceiling applies' }, String(op.shear_class))
        : null),
    el('td', {}, el('span', { class: `place place-${place}` }, PLACE_WORDS[place]),
      op && op.placement && op.placement.part ? el('div', { class: 'small mono muted' }, String(op.placement.part)) : null,
      op && op.placement && op.placement.need ? el('div', { class: 'small', style: { color: '#fca5a5' } }, `needs: ${String(op.placement.need)}`) : null),
    el('td', {}, chip
      ? el('span', {},
        el('button', {
          type: 'button', class: 'ghost', style: { padding: '1px 6px' },
          onclick: () => ctx.navigate('chips', String(chip.chip_id)),
        }, String(chip.chip_id)),
        String(chip.scale) === 'demo' ? el('div', { style: { marginTop: '3px' } }, demoBadge()) : null)
      : el('span', { class: 'muted' }, place === 'on_chip' ? 'not assigned yet' : '-')),
    el('td', { class: 'small muted', style: { maxWidth: '28ch' } },
      op && op.placement ? String(op.placement.why || '') : '',
      chip && chip.reason ? el('div', { style: { marginTop: '3px' } }, String(chip.reason)) : null));
}

function confBar(v) {
  const pct = Math.max(0, Math.min(1, Number(v))) * 100;
  const cls = v < 0.4 ? 'conf-bar lo' : v < 0.7 ? 'conf-bar mid' : 'conf-bar';
  return el('span', { class: cls, role: 'img', 'aria-label': `confidence ${fmtNum(v, 2)}` }, el('i', { style: { width: `${pct}%` } }));
}

function counter(n, label, placement) {
  return el('span', { class: 'ct' },
    el('b', String(n)),
    placement ? el('span', { class: `place place-${placement}` }, label) : el('span', { class: 'small muted' }, label));
}

function missingNote(res) {
  return el('span', { class: 'muted small' }, `not readable (tried ${(res.tried || []).length} path${(res.tried || []).length === 1 ? '' : 's'})`);
}

export { stubBadge };

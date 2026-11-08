// The sidebar's content: on the overview, the assay switch and every stage in order; on a chip,
// what it is, its status and what it does. Plain HTML strings; main.js wires up the clicks.
//
// Every word about a stage comes from protocols/pipeline_mvp.json, the chip spec,
// protocols/pipeline_status.md or the generated dye-demo plan (docs/demo_plan.json), and every
// time from the stage's run in runs/: no prose table of our own, so nothing here can drift from
// the design (MJ's rule for the twin).
import { statusHtml, statusView, shortStage, notPrintable, calm, joinAfter, lineTime } from './status.js';
import { caveat } from './pipeline3d.js';
import { formatDuration } from './player.js';

const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const firstSentence = (s) => (String(s ?? '').match(/^.*?[.!?](\s|$)/)?.[0] ?? String(s ?? '')).trim();
const tidy = (s) => String(s ?? '').replace(/\s*\(off-chip\)\s*$/i, '');
const lowerFirst = (s) => String(s ?? '').replace(/^./, (c) => c.toLowerCase());

// Each stage's time on this line (twin/main.js: its run's protocol time, less skipped steps).
const timeOf = (s) => (s?.kind !== 'end' && s?.entry?.duration_s != null ? s.entry.duration_s : null);
const ranges = (xs) => {
  const out = [];
  for (const x of [...xs].sort((a, b) => a - b)) {
    const r = out.at(-1);
    if (r && x === r[1] + 1) r[1] = x; else out.push([x, x]);
  }
  return out.map(([a, b]) => (a === b ? `${a}` : `${a}–${b}`)).join(', ');
};
const timeTitle = (s) => (s?.entry?.skipped?.length
  ? `Protocol time as the twin runs it, without steps ${ranges(s.entry.skipped)}, which this line skips`
  : 'Protocol time as the twin runs it');
const timeHtml = (s) => (timeOf(s) != null ? `<span class="dur" title="${esc(timeTitle(s))}">${esc(formatDuration(timeOf(s)))}</span>` : '');
// The total, in words: "88.7 days from cells to library: stage 1 takes 87.6 days, stages 2–7 take 25.8 h."
export function lineTimeText(slots) {
  const t = lineTime(slots);
  if (!t) return '';
  const split = t.first != null && t.stages > 1 && t.rest > 0
    ? `: stage 1 takes ${formatDuration(t.first)}, stage${t.stages > 2 ? `s 2–${t.stages}` : ' 2'} ${t.stages > 2 ? 'take' : 'takes'} ${formatDuration(t.rest)}` : '';
  return `${formatDuration(t.total)} from cells to library${split}.${t.missing ? ` ${t.missing} stage${t.missing > 1 ? 's have' : ' has'} no time yet.` : ''}`;
}

// Roadmap entries that branch off a chip earlier in the line (Module 6 off culture) rather than
// follow the last chip: the pipeline file lists them in its roadmap because it can express only
// one chain (protocols/pipeline_status.md), so they are told apart by their hand-offs.
export function branches(pipe) {
  const mods = pipe?.modules ?? [];
  const last = mods.at(-1)?.module;
  const road = new Map((pipe?.roadmap ?? []).map((r) => [r.id, r]));
  return (pipe?.handoffs ?? [])
    .filter((h) => h.to?.roadmap && h.from?.module && h.from.module !== last && road.has(h.to.roadmap))
    .map((h) => ({ from: h.from.module, carries: h.carries, ...road.get(h.to.roadmap) }));
}

// What follows the last chip: the roadmap without its branches.
function endName(pipe, slots, status) {
  const off = new Set(branches(pipe).map((b) => b.id));
  const road = (pipe?.roadmap ?? []).filter((r) => !off.has(r.id)).map((r) => tidy(r.name));
  const tail = (status?.offChip ?? []).filter((o) => o.after && o.after === slots.at(-2)?.chip?.spec.id).map((o) => o.stage);
  return (road.length ? road : tail).join(' → ') || 'Library out';
}

// One line saying what a stage does: the pipeline entry's own note (or the chip's description),
// with the caveat that belongs to it.
export function stageSummary(e, spec) {
  const head = firstSentence(e?.note ?? spec?.description ?? '');
  const extra = e ? caveat(e) : '';
  return calm([head, extra].filter(Boolean).join(' '));
}

// The chips printed for the demo: built_for_demo on a pipeline module, or on a roadmap entry
// (the Module 6 branch), whose chip is named in its note. These are frozen (AGENTS.md).
export function printedChips(pipe) {
  const out = new Set();
  for (const m of pipe?.modules ?? []) if (m.built_for_demo && m.chip) out.add(m.chip.split('/').pop().replace(/\.json$/, ''));
  for (const r of pipe?.roadmap ?? []) {
    const id = r.built_for_demo ? /chip\/([\w-]+)\.json/.exec(r.note ?? '')?.[1] : null;
    if (id) out.add(id);
  }
  return out;
}

export function overviewHtml({ variants, current, chosen, slots, pipe, status, plans = null }) {
  const printed = printedChips(pipe);
  const items = [];
  const branched = branches(pipe);
  slots.forEach((s, k) => {
    if (s.kind === 'end') {
      // What happens after the last chip, from the hand-off into the end and its off-chip steps.
      const j = joinAfter(slots, k - 1, status);
      const sub = j?.lead ? `${j.lead}${j.hole ? ' Protocol not written yet.' : ''}` : 'In tubes, after the last chip';
      items.push(`<li class="end" data-slot="${k}" data-join="${k - 1}"><span class="n">${k + 1}</span><span class="name">${esc(endName(pipe, slots, status))}</span>
        ${statusHtml('OFF-CHIP')}<span class="sub">${esc(sub)}</span></li>`);
      return;
    }
    const e = s.entry;
    if (s.chip) {
      const st = status?.byChip.get(s.chip.spec.id);
      const open = e.view ? ` data-open="${esc(e.view)}" data-file="${esc(e.file ?? '')}" tabindex="0" role="button"` : '';
      const v = statusView(st?.badge ?? e.status, st?.detail ?? '');
      const tags = [notPrintable(e) ? 'bought hardware · our chemistry' : '', printed.has(s.chip.spec.id) ? 'printed for the demo' : '',
        plans?.has(s.chip.spec.id) ? 'dye demo' : ''].filter(Boolean);
      const detail = v.text !== v.word ? v.text.slice(v.word.length + 3) : '';
      const time = timeHtml(s);
      items.push(`<li data-slot="${k}"${open} title="${esc(st?.stage ?? e.name)}"><span class="n">${k + 1}</span><span class="name">${esc(shortStage(st?.stage ?? e.name))}</span>
        ${statusHtml(st?.badge ?? e.status)}<span class="sub">${detail ? `${esc(detail)}<br>` : ''}${time ? `${time} · ` : ''}<code>${esc(s.chip.spec.id)}</code>${tags.map((t) => ` · ${esc(t)}`).join('')}</span></li>`);
    } else {
      items.push(`<li class="ghost" data-slot="${k}"><span class="n">${k + 1}</span><span class="name">${esc(e.name)}</span>
        ${statusHtml(e.status === 'bench' ? 'BENCH' : 'NEXT')}<span class="sub">${esc(firstSentence(e.status_note ?? e.note ?? ''))}</span></li>`);
    }
    for (const b of branched.filter((x) => x.from === e.module)) {
      items.push(`<li class="offchip branch"><span class="n">⑂</span><span class="name">Branch: ${esc(tidy(b.name))}${b.built_for_demo ? ' · printed for the demo' : ''}</span></li>`);
    }
    // The join to the next chip: off-chip steps, or what moves by tubing. The join into the end of
    // the line is told on the end row instead.
    const j = slots[k + 1]?.kind === 'end' ? null : joinAfter(slots, k, status);
    if (j) {
      const tip = [j.lead, ...j.more].join(' ');
      items.push(`<li class="handoff${j.steps.length ? ' offchip' : ''}" data-join="${k}"${tip ? ` title="${esc(tip)}"` : ''}><span class="n">↳</span>`
        + `<span class="name">${esc(j.title)}${j.hole ? '<span class="hole"> · protocol not written yet</span>' : ''}</span>`
        + `${j.lead ? `<span class="sub">${esc(j.lead)}</span>` : ''}</li>`);
    }
  });
  const total = lineTimeText(slots);
  const alt = current !== chosen ? pipe : null;
  return `
    ${variants.length > 1 ? `<h2 class="side-title">Assay</h2>
    <div class="segmented" role="group" aria-label="Assay">${variants.map((v) => `<button type="button" data-variant="${v.variant}"
      aria-pressed="${v.variant === current}">${esc(v.label)}<small>${v.variant === chosen ? 'the plan' : 'alternative'}</small></button>`).join('')}</div>` : ''}
    ${alt ? `<p class="note">${esc(firstSentence(alt.description ?? 'An alternative to the chosen assay.'))}</p>` : ''}
    <p class="p3-legend">Every chip at real scale, 30 × 20 mm, joined by 1/16 inch tubing. Press Play to follow the sample down the line; click a stage to open its chip.</p>
    <ol class="stages">${items.join('')}</ol>
    ${total ? `<p class="line-time"><b>Time.</b> ${esc(total)} The off-chip steps have no times yet. Each time is the chip's protocol as the twin runs it, not a measurement.</p>` : ''}
    <p class="meta">“Designed” means a chip spec and a protocol exist, not that anything was printed or run (protocols/pipeline_status.md).
      “Bought hardware · our chemistry”: the stage's features are below the in-plane floor, so it runs on a bought kit and its chip spec is a demo layout.</p>`;
}

// The home page's strip: every stage in order, short names, a status dot and how long it takes;
// the off-chip steps between chips as steps of their own; chips open on click.
export function homeStagesHtml({ slots, status }) {
  const arrow = (j) => `<li class="arrow" aria-hidden="true"${j?.lead ? ` title="${esc(j.lead)}"` : ''}>→</li>`;
  const out = [];
  slots.forEach((s, k) => {
    if (s.kind === 'end') {
      out.push(`<li class="stage end" title="Off-chip, in tubes"><span class="n">${k + 1}</span>Library out</li>`);
      const total = lineTimeText(slots);
      const t = lineTime(slots);
      if (t) out.push(`<li class="line-time" title="${esc(`${total} The off-chip steps have no times yet.`)}">${esc(formatDuration(t.total))} in all</li>`);
      return;
    }
    const e = s.entry;
    const st = s.chip ? status?.byChip.get(s.chip.spec.id) : null;
    const v = statusView(s.chip ? st?.badge ?? e.status : e.status === 'bench' ? 'BENCH' : 'NEXT', s.chip ? st?.detail ?? '' : '');
    const name = shortStage(st?.stage ?? e.name);
    const open = s.chip && e.view ? ` data-open="${esc(e.view)}" data-file="${esc(e.file ?? '')}" tabindex="0" role="button"` : '';
    const bought = notPrintable(e) ? ' · bought kit' : '';
    out.push(`<li class="stage${s.chip ? '' : ' ghost'}"${open} title="${esc(`${st?.stage ?? e.name}: ${v.text}`)}"><i class="sdot ${v.cls}"></i><span class="n">${k + 1}</span>${esc(name)}${esc(bought)}${timeHtml(s)}</li>`);
    // The join to the next stage: its off-chip steps, each a step of its own.
    const j = joinAfter(slots, k, status);
    for (const o of j?.steps ?? []) {
      const tip = [j.lead, ...j.more, j.hole ? 'Protocol not written yet.' : ''].filter(Boolean).join(' ');
      out.push(arrow(null), `<li class="offchip${j.hole ? ' hole' : ''}" title="${esc(tip)}"><span class="k">Off-chip:</span>${esc(lowerFirst(o.stage))}</li>`);
    }
    out.push(arrow(j));
  });
  return out.join('');
}

// One chip: its stage name, status, what it does (from the pipeline file), and its dye demo.
export function chipCardHtml({ spec, entry = null, md = null, stage = null, retired = false, plan = null, cad = null, printed = null }) {
  const m = spec.module;
  const title = md?.stage ?? entry?.name ?? spec.name ?? spec.id;
  const badge = retired ? 'RETIRED' : md?.badge ?? entry?.status ?? (m?.status === 'superseded' ? 'SUPERSEDED' : m?.status === 'draft' ? 'DRAFT' : null);
  const status = badge ? statusHtml(badge, retired ? '' : md?.detail ?? entry?.statusNote ?? '') : '';
  const summary = stageSummary(entry, spec);
  const n = (xs) => (xs ?? []).length;
  const rows = [];
  if (stage) rows.push(['Pipeline', `Stage ${stage.k + 1} of ${stage.n}, ${esc(stage.variant)}`]);
  rows.push(['Size', `${spec.footprint.x} × ${spec.footprint.y} mm`]);
  rows.push(['Parts', `${n(spec.chambers)} chambers · ${n(spec.ports)} ports`]);
  if (entry && notPrintable(entry)) rows.push(['Hardware', 'Bought kit, our chemistry: the chip spec is a demo layout']);
  if (printed !== null) rows.push(['Printed', printed ? 'Yes, for the demo; its geometry is frozen' : 'No, twin only']);
  rows.push(['Dye demo', plan ? `Feed <code>${esc(plan.feed)}</code>, vent <code>${esc(plan.vent)}</code>, ${plan.flow_ul_min} µL/min` : 'None for this chip']);
  if (cad) rows.push(['CAD', esc(cad)]);
  const superseded = m?.status === 'superseded' || retired
    ? `<p class="note">Kept for reference: ${esc(m?.note ?? `replaced by ${m?.superseded_by ?? 'a current chip'}`)}</p>` : '';
  return `
    <section class="side-section chip-card">
      <div class="chip-head"><div><h2>${esc(title)}</h2><span class="id">${esc(spec.id)}</span></div></div>
      ${status ? `<div style="margin-top:8px">${status}</div>` : ''}
      ${summary ? `<p class="lead" style="margin-top:12px">${esc(summary)}</p>` : ''}
      <dl class="facts">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>
      ${plan ? `<p style="margin-top:14px"><button type="button" data-open-bench="${esc(spec.id)}">Open its dye demo</button></p>` : ''}
      ${superseded}
    </section>`;
}

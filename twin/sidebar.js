// The sidebar's content: on the overview, the assay switch and every stage in order; on a chip,
// what it is, its status and what it does. Plain HTML strings; main.js wires up the clicks.
import { statusHtml, statusView } from './status.js';
import { SHORT } from './pipeline3d.js';

const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const firstSentence = (s) => (String(s ?? '').match(/^.*?[.!?](\s|$)/)?.[0] ?? String(s ?? '')).trim();

// What each chip of the current pipeline does, in a sentence or two (from its spec and design note).
export const CHIP_SUMMARY = {
  module1_v1: 'Grows neurons, glia and astrocytes in three chambers that meet at one junction. The flow decides whether the cultures stay apart or share medium; harvest flushes all three out through p_mid.',
  cell_prep_v1: 'Detaches the harvested cells, quenches the detachment and counts the cells before encapsulation.',
  spc_demo_v1: 'Makes the capsules: the cell stream and the shell stream meet oil at a nozzle and pinch off into droplets.',
  module3_v1_translated: 'Holds the capsules in a trap while liquids are exchanged around them: lysis, washes, reverse transcription, then release.',
  module4_v1: 'Splits the capsules into four sublibraries; each gets its own index and is thermocycled in place.',
  module5_v1: 'Opens the capsules, captures the DNA on magnetic beads, then washes and elutes the finished library, one sublibrary at a time.',
  sctip_spc_v1: 'Packs the capsules, adds the amplification mix and a single-copy barcode, and drops one capsule per droplet for off-chip thermocycling.',
};
// The chip the physical food-dye demo runs on.
export const BENCH_CHIP = 'module1_v1';

// Off-chip steps that happen between slot k and the next one (as the scene labels its tubes).
function offChipAfter(slots, k, status) {
  const a = slots[k];
  const b = slots[k + 1];
  if (!a?.chip || !b || b.kind === 'end') return [];
  return (status?.offChip ?? []).filter((o) => o.after === a.chip.spec.id && o.before !== null);
}

const endName = (pipe, slots, status) => {
  const road = (pipe?.roadmap ?? []).map((r) => r.name.replace(/\s*\(off-chip\)\s*$/i, ''));
  const off = (status?.offChip ?? []).filter((o) => o.after && o.after === slots.at(-2)?.chip?.spec.id).map((o) => o.stage);
  return (road.length ? road : off).join(' → ') || 'Library out';
};

export function overviewHtml({ variants, current, chosen, slots, pipe, status }) {
  const items = [];
  slots.forEach((s, k) => {
    if (s.kind === 'end') {
      items.push(`<li class="end" data-slot="${k}"><span class="n">${k + 1}</span><span class="name">${esc(endName(pipe, slots, status))}</span>
        ${statusHtml('OFF-CHIP')}<span class="sub">In tubes, after the last chip</span></li>`);
      return;
    }
    const e = s.entry;
    if (s.chip) {
      const st = status?.byChip.get(s.chip.spec.id);
      const open = e.view ? ` data-open="${esc(e.view)}" data-file="${esc(e.file ?? '')}" tabindex="0" role="button"` : '';
      const bench = s.chip.spec.id === BENCH_CHIP ? ' · food-dye demo' : '';
      items.push(`<li data-slot="${k}"${open}><span class="n">${k + 1}</span><span class="name">${esc(st?.stage ?? e.name)}</span>
        ${statusHtml(st?.badge ?? e.status, st?.detail ?? '')}<span class="sub"><code>${esc(s.chip.spec.id)}</code>${bench}</span></li>`);
    } else {
      items.push(`<li class="ghost" data-slot="${k}"><span class="n">${k + 1}</span><span class="name">${esc(e.name)}</span>
        ${statusHtml(e.status === 'bench' ? 'BENCH' : 'NEXT')}<span class="sub">${esc(firstSentence(e.status_note ?? e.note ?? ''))}</span></li>`);
    }
    for (const o of offChipAfter(slots, k, status)) {
      const hole = /HOLE/i.test(o.status) ? ' (no protocol yet)' : '';
      items.push(`<li class="offchip"><span class="n">↳</span><span class="name">Off-chip: ${esc(o.stage.toLowerCase())}${hole}</span></li>`);
    }
  });
  return `
    <h2 class="side-title">Assay</h2>
    <div class="segmented" role="group" aria-label="Assay">${variants.map((v) => `<button type="button" data-variant="${v.variant}"
      aria-pressed="${v.variant === current}">${esc(v.label)}${v.variant === chosen ? '<small>default</small>' : ''}</button>`).join('')}</div>
    <p class="p3-legend">Every chip at real scale, 30 × 20 mm, joined by 1/16 inch tubing. Press Play to follow the sample down the line; click a stage to open its chip.</p>
    <ol class="stages">${items.join('')}</ol>
    <p class="meta">“Designed” means a chip spec and a protocol exist (protocols/pipeline_status.md), not that the chip is printed.
      Module 1 is the one being made, for the food-dye demo; the others exist in the twin only.</p>`;
}

// The home page's strip: every stage in order, short names and a status dot; chips open on click.
export function homeStagesHtml({ slots, status }) {
  return slots.map((s, k) => {
    if (s.kind === 'end') return `<li class="stage end" title="Off-chip, in tubes"><span class="n">${k + 1}</span>Library out</li>`;
    const e = s.entry;
    const st = s.chip ? status?.byChip.get(s.chip.spec.id) : null;
    const v = statusView(s.chip ? st?.badge ?? e.status : e.status === 'bench' ? 'BENCH' : 'NEXT', s.chip ? st?.detail ?? '' : '');
    const name = SHORT[s.chip?.spec.id] ?? SHORT[e.module] ?? String(e.name ?? '').split(/[:(]/)[0].trim();
    const open = s.chip && e.view ? ` data-open="${esc(e.view)}" data-file="${esc(e.file ?? '')}" tabindex="0" role="button"` : '';
    return `<li class="stage${s.chip ? '' : ' ghost'}"${open} title="${esc(`${st?.stage ?? e.name}: ${v.text}`)}"><i class="sdot ${v.cls}"></i><span class="n">${k + 1}</span>${esc(name)}</li>`;
  }).join('<li class="arrow" aria-hidden="true">→</li>');
}

// One chip: its stage name, status, size and what it does.
export function chipCardHtml({ spec, entry = null, md = null, stage = null, retired = false }) {
  const m = spec.module;
  const title = md?.stage ?? entry?.name ?? spec.name ?? spec.id;
  const badge = retired ? 'RETIRED' : md?.badge ?? entry?.status ?? (m?.status === 'superseded' ? 'SUPERSEDED' : m?.status === 'draft' ? 'DRAFT' : null);
  const status = badge ? statusHtml(badge, retired ? '' : md?.detail ?? entry?.statusNote ?? '') : '';
  const summary = CHIP_SUMMARY[spec.id] ?? firstSentence(spec.description ?? '');
  const n = (xs) => (xs ?? []).length;
  const parts = [`${n(spec.chambers)} chambers`, `${n(spec.ports)} ports`];
  if (n(spec.valves)) parts.push(`${n(spec.valves)} valves`);
  const rows = [];
  if (stage) rows.push(['Pipeline', `Stage ${stage.k + 1} of ${stage.n}, ${esc(stage.variant)}`]);
  rows.push(['Size', `${spec.footprint.x} × ${spec.footprint.y} mm`]);
  rows.push(['Parts', parts.join(' · ')]);
  rows.push(['Bench', spec.id === BENCH_CHIP ? 'Food-dye demo' : 'Twin only']);
  const superseded = m?.status === 'superseded' || retired
    ? `<p class="note">Kept for reference: ${esc(m?.note ?? `replaced by ${m?.superseded_by ?? 'a current chip'}`)}</p>` : '';
  return `
    <section class="side-section chip-card">
      <div class="chip-head"><div><h2>${esc(title)}</h2><span class="id">${esc(spec.id)}</span></div></div>
      ${status ? `<div style="margin-top:8px">${status}</div>` : ''}
      ${summary ? `<p class="lead" style="margin-top:12px">${esc(summary)}</p>` : ''}
      <dl class="facts">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>
      ${spec.id === BENCH_CHIP ? '<p style="margin-top:14px"><button type="button" data-open-bench>Open the bench demo</button></p>' : ''}
      ${superseded}
    </section>`;
}

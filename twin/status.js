// Statuses from protocols/pipeline_status.md (MJ's table: Stage | Chip | Protocol | Status), so the
// twin shows what that file says rather than its own copy. Read once; nothing here edits it.
//
//   byChip   chip id -> { stage, status, badge, detail, extras: [{stage, status}] }
//            (the row whose status says BUILT / wins comes first; other rows for the same chip,
//            e.g. "RT ONLY, runs in the capsule trap" for Module 3, become extras)
//   offChip  [{ stage, where, status, after, before }]: OFF-CHIP rows, with the chips either side
//   retired  Set of whole chips the file retires (its retired table, or rows all saying DUPLICATE-RETIRE)

// The status file is Markdown, so a cell may be emphasised (**Single-cell barcoding**). The
// twin renders these as text in a 3D label, where the asterisks would show up literally.
const plain = (s) => s.replace(/\*\*(.+?)\*\*/g, '$1').replace(/(^|\s)\*(\S[^*]*?)\*(?=\s|$|[.,;:])/g, '$1$2');
const cells = (line) => line.trim().replace(/^\||\|$/g, '').split('|').map((c) => plain(c.trim()));
const code = (s) => /`([^`]+)`/.exec(s)?.[1] ?? null;
const badgeOf = (status) => status.split(/[,(]/)[0].trim().toUpperCase();

export function parseStatus(md) {
  const rows = [];
  const retiredRows = [];
  let header = null;
  let table = 0;
  for (const line of md.split('\n')) {
    if (!line.trim().startsWith('|')) { header = null; continue; }   // a new table starts with its own header
    const c = cells(line);
    if (c.every((x) => /^:?-+:?$/.test(x))) continue;     // the |---| separator
    if (!header) { header = c.map((x) => x.toLowerCase()); table++; continue; }
    const get = (name) => c[header.indexOf(name)] ?? '';
    if (header.includes('item')) {        // the "Retired and duplicates" table: Item | Replaced by | ...
      retiredRows.push({ item: get('item'), chip: code(get('item')), whole: /^`[^`]+`$/.test(get('item')), by: code(get('replaced by')) });
      continue;
    }
    rows.push({ stage: get('stage'), chipCell: get('chip'), chip: code(get('chip')), protocol: get('protocol'), status: get('status'), table });
  }
  const byChip = new Map();
  const offChip = [];
  const all = new Map();
  let last = null;
  for (const r of rows) {
    if (r.chip) {
      if (!all.has(r.chip)) all.set(r.chip, []);
      all.get(r.chip).push(r);
      if (!/DUPLICATE/i.test(r.status)) last = r.chip;
    } else if (/OFF-CHIP/i.test(r.status)) {
      offChip.push({ stage: r.stage, where: r.chipCell, status: r.status, after: last, before: null });
    }
  }
  // The chip that comes next after each off-chip step in the same table (null: it is at the end of
  // the line). The next table (the Module 6 branch) is not the next stage of the chain.
  for (const o of offChip) {
    const i = rows.findIndex((r) => r.stage === o.stage && r.chipCell === o.where);
    o.before = rows.slice(i + 1).find((r) => r.table === rows[i].table && r.chip && !/DUPLICATE/i.test(r.status))?.chip ?? null;
  }
  // Retired: a whole chip in the retired table (not "`module1_v1` dissociation", which retires
  // steps), or a chip whose every row says DUPLICATE (the table's earlier layout).
  const retired = new Set(retiredRows.filter((r) => r.chip && r.whole).map((r) => r.chip));
  for (const [chip, rs] of all) {
    const live = rs.filter((r) => !/DUPLICATE/i.test(r.status));
    if (!live.length) { retired.add(chip); }
    const primary = live.find((r) => /BUILT|wins/i.test(r.status)) ?? live[0] ?? rs[0];
    byChip.set(chip, {
      stage: primary.stage, status: primary.status, badge: badgeOf(primary.status),
      detail: primary.status.slice(primary.status.split(/[,(]/)[0].length).replace(/^[,\s]+/, ''),
      extras: live.filter((r) => r !== primary).map((r) => ({ stage: r.stage, status: r.status, badge: badgeOf(r.status) })),
    });
  }
  return { rows, byChip, offChip, retired, retiredRows };
}

export async function loadStatus(root) {
  try {
    const res = await fetch(`${root}protocols/pipeline_status.md`, { cache: 'no-store' });
    return res.ok ? parseStatus(await res.text()) : null;
  } catch {
    return null;
  }
}

// How a status reads on screen: a coloured dot and a plain word (twin/style.css .status).
// The keys are the words pipeline_status.md and the pipeline files use. BUILT there means a spec
// and a protocol exist, not that anything was printed or run, so it reads "Designed".
const SHOWN = {
  BUILT: ['st-final', 'Designed', 'pipeline_status.md: BUILT. A chip spec and a protocol exist; that does not mean printed or run.'],
  FINAL: ['st-final', 'Designed', 'Pipeline file: final design (a chip spec and a protocol exist).'],
  TRANSLATED: ['st-translated', 'Translated', 'Pipeline file: translated from another team member\'s design.'],
  'RT ONLY': ['st-translated', 'RT only', 'pipeline_status.md: RT ONLY.'],
  DRAFT: ['st-draft', 'Draft', 'A draft design.'],
  MISSING: ['st-missing', 'Not designed yet', 'No chip for this stage yet.'],
  NEXT: ['st-missing', 'Not designed yet', 'No chip for this stage yet.'],
  BENCH: ['st-bench', 'Bench step', 'Done at the bench, in tubes: no chip.'],
  'OFF-CHIP': ['st-bench', 'Off-chip', 'pipeline_status.md: OFF-CHIP, done by hand in tubes.'],
  'DUPLICATE-RETIRE': ['superseded', 'Retired', 'pipeline_status.md: DUPLICATE-RETIRE.'],
  'DESIGNED + PROTOCOL': ['st-bought', 'Designed', 'pipeline_status.md: DESIGNED + PROTOCOL. Our chemistry; the hardware is bought, not printed.'],
  RETIRED: ['superseded', 'Retired', 'Retired: replaced by a current chip.'],
  SUPERSEDED: ['superseded', 'Superseded', 'Replaced by a newer design; kept for reference.'],
};

export function statusView(badge, detail = '') {
  const key = String(badge ?? '').trim().toUpperCase();
  const [cls, word, title] = SHOWN[key] ?? ['st-draft', key ? key[0] + key.slice(1).toLowerCase() : 'Unknown', ''];
  const d = calm(String(detail ?? '').trim().replace(/^\((.*)\)$/, '$1'));
  return { cls, word, text: d ? `${word} · ${d}` : word, title };
}

const escHtml = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
// MJ's notes use capitals for emphasis ("THE STAGE THAT MAKES THIS A SINGLE-CELL ASSAY"). On screen,
// a run of capitalised words reads as shouting, so it is shown in lower case; acronyms (RT, PCR,
// QC...) keep their capitals. The words are MJ's; only their case changes.
const ACRONYMS = new Set(['RT', 'PCR', 'QC', 'SPRI', 'DNA', 'RNA', 'CDNA', 'UMI', 'COC', 'STL', 'DXF', 'MVP', 'CAD', 'LLM', 'AI', 'UV', 'ID', 'IN', 'OUT', 'TBC', 'NB']);
export function calm(text) {
  return String(text ?? '').replace(/\b[A-Z][A-Z0-9-]*(?:[ ,]+[A-Z][A-Z0-9-]*)+\b/g, (run, offset, whole) => {
    const words = run.split(/([ ,]+)/);
    const real = words.filter((w, i) => i % 2 === 0);
    if (real.filter((w) => !ACRONYMS.has(w) && w.length > 1).length < 2) return run;
    let out = words.map((w, i) => (i % 2 === 0 && !ACRONYMS.has(w) ? w.toLowerCase() : w)).join('');
    const before = whole.slice(0, offset).trimEnd();
    if (!before || /[.!?:]$/.test(before)) out = out[0].toUpperCase() + out.slice(1);
    return out;
  });
}

// The label for a stage: the status file's own stage name, up to any colon
// ("Single-cell barcoding: one clonal barcode per capsule ..." reads "Single-cell barcoding").
export const shortStage = (name) => String(name ?? '').split(':')[0].trim();

// A stage the pipeline file marks unprintable: its features are under the in-plane floor, so the
// chemistry is ours and the hardware is bought (MJ: the chip spec is then a demo layout, not a part).
export const notPrintable = (entry) => /NOT PRINTABLE/i.test(entry?.build?.method ?? '');

export function statusHtml(badge, detail = '') {
  const v = statusView(badge, detail);
  return `<span class="status ${v.cls}" title="${escHtml(v.title)}">${escHtml(v.text)}</span>`;
}

// ---------------------------------------------------------------- time and hand-offs

// Sentences, split only where a full stop is followed by a space ("0.2 mm" stays whole).
export const sentences = (s) => String(s ?? '').split(/(?<=[.!?])\s+/).map((x) => x.trim()).filter(Boolean);

// Protocol steps that a pipeline entry's own note says this assay skips: stage 4b's note on the
// CapSeq line says "steps 17-19 (...) are the chromatin/transposase VARIANT tail and are skipped
// here". Read clause by clause, so "Steps 1-16 and 20-21 ... are the shared stage; steps 17-19 ...
// are skipped" skips 17-19 only. A note that says nothing of the kind skips nothing.
export function skippedSteps(entry) {
  const out = new Set();
  for (const clause of String(entry?.note ?? '').split(/[.;](?:\s|$)/)) {
    if (!/\bskipped\b|\bnot run\b/i.test(clause)) continue;
    for (const m of clause.matchAll(/\bsteps?\s+(\d+)\s*(?:-|–|to)\s*(\d+)/gi)) {
      for (let i = Number(m[1]); i <= Number(m[2]); i++) out.add(i);
    }
  }
  return out;
}

// How long a stage takes on this assay's path: its run's protocol time (runs/, the twin's
// simulation of the chip's protocol, not a measurement), less the steps the pipeline skips.
export function stageSeconds(run, skip = new Set()) {
  if (!run) return null;
  if (!skip.size) return run.total_duration_s ?? null;
  return (run.events ?? []).filter((e) => !skip.has(e.protocol_step)).reduce((a, e) => a + (Number(e.duration_s) || 0), 0);
}

// What a protocol feeds into its chip, by the protocol's own generic reagent names (CONFIDENTIALITY.md:
// placeholder names like pcr_mix are fine; no recipes, enzymes or sequences are read here) and the
// ports they enter by: { list: ['pooled capsule suspension', 'index primer 1–4', 'PCR mix', ...],
// ports: 'in_s1–in_s4' }. Numbered reagents (index_primer_1 ... _4) are folded into one entry.
const UPPER = new Set(['PCR', 'RT', 'DNA', 'RNA', 'QC', 'SPRI']);
const numberRanges = (xs) => {
  const out = [];
  for (const x of [...xs].sort((a, b) => a - b)) {
    const r = out.at(-1);
    if (r && x === r[1] + 1) r[1] = x; else out.push([x, x]);
  }
  return out.map(([a, b]) => (a === b ? `${a}` : `${a}–${b}`)).join(', ');
};
export function reagentsIn(protocol) {
  const names = new Map();
  const ports = new Map();
  for (const s of protocol?.steps ?? []) {
    if (!s.reagent) continue;
    const m = /^(.*?)_(\d+)$/.exec(s.reagent);
    const base = (m ? m[1] : s.reagent).split('_').map((w) => (UPPER.has(w.toUpperCase()) ? w.toUpperCase() : w)).join(' ');
    if (!names.has(base)) names.set(base, new Set());
    if (m) names.get(base).add(Number(m[2]));
    for (const p of s.ports?.feed ?? []) {
      const q = /^(.*?)(\d+)$/.exec(p);
      const key = q ? q[1] : p;
      if (!ports.has(key)) ports.set(key, new Set());
      if (q) ports.get(key).add(Number(q[2]));
    }
  }
  if (!names.size) return null;
  const portText = [...ports].map(([k, nums]) => {
    if (!nums.size) return k;
    const sorted = [...nums].sort((a, b) => a - b);
    const run = sorted.every((x, i) => !i || x === sorted[i - 1] + 1);
    return run && sorted.length > 1 ? `${k}${sorted[0]}–${k}${sorted.at(-1)}` : sorted.map((x) => `${k}${x}`).join(', ');
  }).join(', ');
  return { list: [...names].map(([b, nums]) => (nums.size ? `${b} ${numberRanges(nums)}` : b)), ports: portText };
}

// Every stage's time on the line and the total. `slots` are the pipeline scene's (twin/pipeline3d.js),
// each stage's entry.duration_s set by twin/main.js; a stage with no run has no time and is counted
// as missing, never guessed. Null when no stage has a time.
export function lineTime(slots) {
  const times = (slots ?? []).filter((s) => s.kind !== 'end').map((s) => s.entry?.duration_s ?? null);
  const known = times.filter((x) => x != null);
  if (!known.length) return null;
  const total = known.reduce((a, x) => a + x, 0);
  return { total, first: times[0], rest: times[0] != null ? total - times[0] : null, missing: times.length - known.length, stages: times.length };
}

// The off-chip steps (pipeline_status.md's OFF-CHIP rows) on the join out of chip `from`: into
// the next chip, or, for the join into the end of the line, the steps that come after the last
// chip (shown there whatever the chip order, so the TIP-seq line keeps them too).
export function offChipSteps(status, from, toEnd = false) {
  const all = status?.offChip ?? [];
  return toEnd ? all.filter((o) => o.before === null) : all.filter((o) => o.after && o.after === from && o.before !== null);
}

// "HOLE" in pipeline_status.md: nobody has written that step's protocol yet.
export const noProtocol = (o) => /\bHOLE\b/i.test(o?.status ?? '');

const lowerFirst = (s) => String(s ?? '').replace(/^./, (c) => c.toLowerCase());
const upperFirst = (s) => String(s ?? '').replace(/^./, (c) => c.toUpperCase());

// What happens between two chips, in the pipeline file's and pipeline_status.md's own words:
//   steps   the off-chip steps on this join (pipeline_status.md)
//   title   "Off-chip: break emulsion, wash oil, cure", or "Capsules by tubing" when there are none
//   hole    an off-chip step here has no protocol yet
//   lead    the hand-off note's first sentence: what physically moves, and how
//   more    the hand-off's sentences on what is off-chip, undocumented or mismatched, and any
//           connector warning (an adapter onto bought hardware)
export function joinView(h, steps = []) {
  const carries = h?.carries ?? 'sample';
  const lead = calm(sentences(h?.note)[0] ?? '');
  const notes = [h?.note, h?.from?.note, h?.to?.note].flatMap(sentences).map(calm);
  const more = notes.filter((s) => s !== lead && /undocumented|does not match|off-chip/i.test(s));
  const warn = sentences(h?.connector?.warning)[0];
  if (warn) more.push(calm(warn));
  return {
    steps,
    carries,
    hole: steps.some(noProtocol),
    title: steps.length ? `Off-chip: ${steps.map((o) => lowerFirst(o.stage)).join('; ')}` : `${upperFirst(carries)} by tubing`,
    lead,
    more: [...new Set(more)].slice(0, 3),
  };
}

// The join from slot k of the pipeline scene to the next slot: the hand-off it draws as a tube
// (slot.outH, from the pipeline file) and the off-chip steps on it. Null after the end.
export function joinAfter(slots, k, status) {
  const a = slots?.[k];
  const b = slots?.[k + 1];
  if (!a || !b || a.kind === 'end') return null;
  const toEnd = b.kind === 'end';
  const steps = a.chip ? offChipSteps(status, a.chip.spec.id, toEnd) : toEnd ? offChipSteps(status, null, true) : [];
  return joinView(a.outH, steps);
}

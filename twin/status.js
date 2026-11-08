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
  for (const line of md.split('\n')) {
    if (!line.trim().startsWith('|')) { header = null; continue; }   // a new table starts with its own header
    const c = cells(line);
    if (c.every((x) => /^:?-+:?$/.test(x))) continue;     // the |---| separator
    if (!header) { header = c.map((x) => x.toLowerCase()); continue; }
    const get = (name) => c[header.indexOf(name)] ?? '';
    if (header.includes('item')) {        // the "Retired and duplicates" table: Item | Replaced by | ...
      retiredRows.push({ item: get('item'), chip: code(get('item')), whole: /^`[^`]+`$/.test(get('item')), by: code(get('replaced by')) });
      continue;
    }
    rows.push({ stage: get('stage'), chipCell: get('chip'), chip: code(get('chip')), protocol: get('protocol'), status: get('status') });
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
  // The chip that comes next after each off-chip step (null: it is at the end of the line).
  for (const o of offChip) {
    const i = rows.findIndex((r) => r.stage === o.stage && r.chipCell === o.where);
    o.before = rows.slice(i + 1).find((r) => r.chip && !/DUPLICATE/i.test(r.status))?.chip ?? null;
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

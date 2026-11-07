// Statuses from protocols/pipeline_status.md (MJ's table: Stage | Chip | Protocol | Status), so the
// twin shows what that file says rather than its own copy. Read once; nothing here edits it.
//
//   byChip   chip id -> { stage, status, badge, detail, extras: [{stage, status}] }
//            (the row whose status says BUILT / wins comes first; other rows for the same chip,
//            e.g. "RT ONLY, runs in the capsule trap" for Module 3, become extras)
//   offChip  [{ stage, where, status, after, before }]: OFF-CHIP rows, with the chips either side
//   retired  Set of chip ids whose every row says DUPLICATE-RETIRE

const cells = (line) => line.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
const code = (s) => /`([^`]+)`/.exec(s)?.[1] ?? null;
const badgeOf = (status) => status.split(/[,(]/)[0].trim().toUpperCase();

export function parseStatus(md) {
  const rows = [];
  let header = null;
  for (const line of md.split('\n')) {
    if (!line.trim().startsWith('|')) { header = null; continue; }   // a new table starts with its own header
    const c = cells(line);
    if (c.every((x) => /^:?-+:?$/.test(x))) continue;     // the |---| separator
    if (!header) { header = c.map((x) => x.toLowerCase()); continue; }
    const get = (name) => c[header.indexOf(name)] ?? '';
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
  const retired = new Set();
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
  return { rows, byChip, offChip, retired };
}

export async function loadStatus(root) {
  try {
    const res = await fetch(`${root}protocols/pipeline_status.md`, { cache: 'no-store' });
    return res.ok ? parseStatus(await res.text()) : null;
  } catch {
    return null;
  }
}

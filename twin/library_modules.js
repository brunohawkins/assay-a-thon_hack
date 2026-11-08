// Modules 4 and 5 (MJ's chips, chip/module4_v1.json and chip/module5_v1.json). The generic
// player already animates their protocols (load, exchange, hold_temp, release); this adds
// what protocols/module5_design.md says in words, kept simple:
//   Module 4: the 16 fill pulses from the off-chip rotary valve in palindromic order
//             (ABCD DCBA), then the thermocycle, which heats the WHOLE chip, so all four
//             chambers glow together, with "cycle n of 12".
//   Module 5: one sublibrary per pass (pass k of 4), the magnet under bead_cx on or off,
//             and the release -> dilute -> capture -> wash -> elute -> out sequence.
import * as THREE from 'three';

export const LIB_CHIPS = { module4_v1: 'm4', module5_v1: 'm5' };
const SUBS = ['sub_1', 'sub_2', 'sub_3', 'sub_4'];
const LETTER = { sub_1: 'A', sub_2: 'B', sub_3: 'C', sub_4: 'D' };
const STAGES = {
  m4: [['fill', 'Fill: rotary valve, 16 pulses, ABCD DCBA'], ['primer', 'Index primer, one per chamber'],
    ['mix', 'PCR mix'], ['cycle', 'Thermocycle: whole chip']],
  m5: [['in', 'Sublibrary in (in_lib)'], ['beads', 'Beads in, magnet ON'], ['release', 'Capsule release'],
    ['dilute', 'Dilute, then capture on the beads'], ['wash', 'Wash on the pellet'], ['elute', 'Elute'], ['out', 'Library out (out_lib)']],
};
const TITLE = { m4: 'Module 4 · sublibrary split + index PCR', m5: 'Module 5 · release + cleanup' };

const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function stageOf(kind, ev) {
  const n = ev?.narration ?? '';
  if (kind === 'm4') {
    if (ev.command === 'load') return 'fill';
    if (ev.command === 'hold_temp') return 'cycle';
    return /primer/i.test(n) ? 'primer' : 'mix';
  }
  if (ev.command === 'load') return ev.args.target === 'bead_cx' ? 'beads' : 'in';
  if (ev.command === 'hold_temp') return 'release';
  if (ev.command === 'release') return ev.args.target === 'react_cx' ? 'release' : 'out';
  if (/dilut/i.test(n)) return 'dilute';
  if (/elute/i.test(n)) return 'elute';
  return 'wash';
}

export function createLibraryView({ panel }) {
  let chip = null;
  let kind = null;
  let events = [];
  let cur = null;
  let magnet = null;

  const isChip = (spec) => Boolean(LIB_CHIPS[spec?.id]);

  function attach(c, run) {
    detach();
    chip = c;
    kind = LIB_CHIPS[c.spec.id];
    events = run?.events ?? [];
    panel.hidden = false;
    if (kind === 'm5') {
      const bead = c.spec.chambers.find((x) => x.id === 'bead_cx');
      if (bead) {
        const mat = new THREE.MeshStandardMaterial({ color: 0x475569, metalness: 0.6, roughness: 0.4, emissive: 0x000000 });
        magnet = new THREE.Mesh(new THREE.BoxGeometry(bead.size[0] + 1, bead.size[1] + 1, 0.8), mat);
        magnet.position.set(bead.center[0], bead.center[1], -0.6);
        magnet.name = 'magnet';
        c.group.add(magnet);
      }
    }
    render(0);
  }

  function detach() {
    if (magnet) {
      magnet.parent?.remove(magnet);
      magnet.geometry.dispose();
      magnet.material.dispose();
      magnet = null;
    }
    chip = null;
    kind = null;
    cur = null;
    panel.hidden = true;
    panel.replaceChildren();
  }

  function render(i) {
    if (!kind) return;
    cur = events[i] ?? null;
    const upto = events.slice(0, i + 1);
    const stage = cur ? stageOf(kind, cur) : null;
    const lines = [];
    if (kind === 'm4') {
      const fills = events.filter((e) => e.command === 'load');
      const done = upto.filter((e) => e.command === 'load').length;
      const order = fills.map((e, k) => `<span class="${k < done - 1 ? 'done' : k === done - 1 && stage === 'fill' ? 'now' : ''}">${LETTER[e.args.target] ?? '?'}</span>`).join('');
      lines.push(`<p class="lib-detail">${stage === 'fill' ? `Pulse ${done} of ${fills.length} → ${esc(cur.args.target)}` : `${fills.length} fill pulses`}</p>`);
      lines.push(`<p class="lib-order" title="Fill order from the rotary valve: palindromic, so every chamber gets the same mix of early and late capsules">${order}</p>`);
      const cycles = Math.max(0, ...events.map((e) => Number(/cycle (\d+)/.exec(e.narration ?? '')?.[1] ?? 0)));
      if (stage === 'cycle') {
        const c = /cycle (\d+) (\w+)/.exec(cur.narration ?? '');
        const what = c ? `cycle ${c[1]} of ${cycles} · ${c[2]}` : (/(initial denature|final extension|cool down)/.exec(cur.narration ?? '')?.[1] ?? 'hold');
        lines.push(`<p class="lib-detail hot">${esc(what)} · ${cur.args.temp_c} °C, all four chambers</p>`);
      } else {
        lines.push(`<p class="lib-detail">${cycles} cycles once all four are filled</p>`);
      }
    } else {
      const passes = events.filter((e) => e.command === 'load' && e.args.target === 'react_cx').length;
      const pass = upto.filter((e) => e.command === 'load' && e.args.target === 'react_cx').length;
      const said = upto.map((e) => /magnet (ON|OFF|on|off)/.exec(e.narration ?? '')?.[1]).filter(Boolean).at(-1);
      const on = said ? said.toUpperCase() === 'ON' : stage === 'beads';
      if (magnet) {
        magnet.material.emissive.set(on ? 0x2563eb : 0x000000);
        magnet.material.emissiveIntensity = on ? 0.9 : 0;
      }
      const bench = /bench/i.test(cur?.narration ?? '');
      lines.push(`<p class="lib-detail">${bench ? 'Hand-off to the bench' : `Pass ${Math.max(1, pass)} of ${passes} · sublibrary sub_${Math.max(1, pass)}`}</p>`);
      lines.push(`<p class="lib-detail"><span class="dot" style="background:${on ? '#60a5fa' : '#475569'}"></span>magnet under bead_cx ${on ? 'ON' : 'off'}</p>`);
    }
    panel.innerHTML = `
      <h2>${esc(TITLE[kind])}</h2>
      <p class="lib-sub">Design-note steps (protocols/module5_design.md); geometry from ${esc(chip.spec.id)}.json</p>
      <ol class="lib-stages">${STAGES[kind].map(([k, name]) => `<li class="${k === stage ? 'now' : ''}">${esc(name)}</li>`).join('')}</ol>
      ${lines.join('')}`;
  }

  // Called after the player draws each frame (its onTick): Module 4's thermocycle heats the whole chip, but the
  // protocol names one chamber per step, so copy that chamber's glow to the other three.
  function update() {
    if (kind !== 'm4' || cur?.command !== 'hold_temp' || !chip) return;
    const src = chip.parts.get(cur.args.target);
    if (!src?.glow) return;
    for (const id of SUBS) {
      const part = chip.parts.get(id);
      if (!part || part === src) continue;
      part.glow.visible = src.glow.visible;
      part.glowMaterial.color.copy(src.glowMaterial.color);
      part.glowMaterial.opacity = src.glowMaterial.opacity;
      part.material.emissive.copy(src.material.emissive);
      part.material.emissiveIntensity = src.material.emissiveIntensity;
    }
  }

  return {
    isChip,
    attach,
    detach,
    update,
    onEvent(i) { if (kind) render(i); },
    get kind() { return kind; },
  };
}

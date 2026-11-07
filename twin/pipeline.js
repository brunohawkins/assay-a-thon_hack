// Pipeline view: the whole MVP on one screen, from protocols/pipeline_mvp.json and the module
// runs. Module 1 (control + interaction side by side) -> Module 1b (automated cell prep,
// simulated) -> Module 2 -> Module 3 -> roadmap. Cells and capsules move card to card as dots,
// following the pipeline's hand-offs; the bottom timeline is the pipeline's phases.
import { fateCss, formatDuration } from './player.js';

const LANES = [
  { n: 1, name: 'neuron', id: 'm1_neuron_1' },
  { n: 2, name: 'astrocyte', id: 'm1_astrocyte_1' },
  { n: 3, name: 'microglia', id: 'm1_microglia_1' },
];
const M1B_STAGES = [['m1b_merge_1', 'merge'], ['m1b_mix_1', 'quench'], ['m1b_cellwash_1', 'wash'], ['m1b_filter_1', 'filter'],
  ['m1b_qc_window_1', 'QC count'], ['m1b_resuspend_1', 'resuspend']];
const M2_STAGES = ['co-flow', 'nozzle', 'phase separation', 'cold delay (off-chip, 4 °C)', 'trap: oil out', 'UV cure', 'washes'];
const M3_STAGES = ['load', 'wash ×3', 'back-flush', 'release'];
const DOTS = 6;

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const clamp01 = (x) => Math.min(1, Math.max(0, x));
const ease = (x) => { x = clamp01(x); return x < 0.5 ? 4 * x * x * x : 1 - (-2 * x + 2) ** 3 / 2; };

function badges(spec) {
  const m = spec?.module;
  if (!m) return '';
  const out = [m.printed === false
    ? '<span class="badge sim-only">Designed · simulated · not printed this weekend</span>'
    : '<span class="badge printed">Printed</span>'];
  if (m.status === 'draft') out.push('<span class="badge draft">Draft design</span>');
  return out.join('');
}

export function createPipeline(root, handlers = {}) {
  let phases = [];
  let starts = [];
  let total = 1;
  let t = 0;
  let playing = false;
  let speed = 1;
  let stopAt = null;
  let current = -2;
  let visible = false;
  let el = {};
  let data = null;

  function load(d) {
    data = d;
    const { specs, runs } = d;
    const m1Run = runs.m1v1 ?? runs.m1_control;
    const m1Days = Math.round((m1Run?.total_duration_s ?? 0) / 86400);
    const s1b = runs.m1b?.summary ?? {};
    const qc = s1b.counted ? `${s1b.counted.cells_per_ul.toLocaleString()} cells/µL · ${(s1b.counted.viability * 100).toFixed(0)}% viable` : '–';
    const p2 = specs.m2?.generator?.params ?? {};
    const capVolNl = (4 / 3) * Math.PI * ((p2.capsule_diameter_um ?? 70) / 2) ** 3 / 1e6;
    const perHour = ((p2.core_flow_ul_h ?? 100) + (p2.shell_flow_ul_h ?? 250)) / capVolNl * 1000;
    const trap3 = specs.m3?.chambers?.find((c) => c.id === 'm3_trap_1');

    phases = [
      { id: 'pl_m1', module: 'm1', play: 6, proto: m1Run?.total_duration_s ?? 1,
        narration: specs.m1v1
          ? `Module 1 v1 (MJ's chip): three lineages grow apart in separate-growth mode, then each chamber's medium crosses to the other two in turn (${m1Days} days, compressed). No valves: routing is FEED / VENT / CAP.`
          : `Module 1: both chips seed, split and differentiate three cell types (${m1Days} days, compressed). Only the interaction chip opens its pair valves; the control stays isolated.` },
      { id: 'pl_m1_out', module: 'm1', play: 2.5, proto: 300,
        narration: specs.m1v1
          ? 'Harvest: the three chambers are swept into j_mid and the pooled, barcoded cells leave through p_mid.'
          : 'Harvest: cells leave m1_out_1/2/3 of both chips (labelled per chamber) for Module 1b\'s three inlets.' },
      { id: 'pl_m1b', module: 'm1b', play: 6.5, proto: runs.m1b?.total_duration_s ?? 1,
        narration: `Module 1b (automated, simulated): merge, quench, wash, filter, count, resuspend. Counter: ${qc}; the operator dilutes ${s1b.dilution ?? '–'}× so Module 2 gets ${s1b.mean_cells_per_capsule ?? '–'} cells per capsule; safety: ${s1b.verdict?.result ?? '–'}.` },
      { id: 'pl_m1b_out', module: 'm1b', play: 2, proto: 60,
        narration: 'Cells in dextran buffer go from m1b_to_module2_1 into Module 2\'s core inlet.' },
      { id: 'pl_m2', module: 'm2', play: 7, proto: runs.m2?.total_duration_s ?? 1,
        narration: `Module 2: core and shell co-flow into the nozzle, oil pinches off ~${Math.round(perHour / 3600)} droplets a second, each phase-separates into a core-shell capsule; cold delay off-chip at 4 °C; then the trap: oil out, UV cure, washes.` },
      { id: 'pl_m2_out', module: 'm2', play: 2, proto: 60,
        narration: 'Cured capsules leave through the 100 µm aggregate filter for Module 3.' },
      { id: 'pl_m3', module: 'm3', play: 4.5, proto: runs.m3?.total_duration_s ?? 1,
        narration: `Module 3: ${trap3 ? `${trap3.size[0]} × ${trap3.size[1]} mm trap, ` : ''}three washes down through the mesh, back-flush, release.` },
      { id: 'pl_roadmap', module: null, play: 2, proto: 1,
        narration: 'Next, on the roadmap: library prep and sequencing.' },
    ];
    starts = [];
    let acc = 0;
    let proto = 0;
    phases.forEach((ph, i) => {
      starts.push(acc);
      acc += ph.play;
      ph.event = { command: ph.id, narration: ph.narration, args: {}, duration_s: ph.proto, t_s: proto, protocol_step: i + 1 };
      proto += ph.proto;
    });
    total = acc;
    t = 0;
    current = -2;

    const lanes = (variant) => LANES.map((l) => `<li class="pl-lane" data-lane="${l.n}" data-chip="${variant}">
        <span class="sw" data-fate="${esc(fateCss(l.id))}"></span>${l.name}<span class="pl-anchor out" data-anchor="m1_${variant}_${l.n}"></span></li>`).join('');
    const m1card = (variant, file) => `<article class="pl-card pl-m1card" data-open="m1" data-file="${esc(file ?? '')}" tabindex="0">
        <header><b>${variant === 'control' ? 'Control' : 'Interaction'}</b><span class="pl-day" data-day="${variant}">day 0</span></header>
        <ul class="pl-lanes">${lanes(variant)}</ul>
        <p class="pl-valves" data-valves="${variant}"><span class="vdot"></span>pair valves closed</p>
      </article>`;
    root.innerHTML = `
      <div class="pl-row">
        <section class="pl-col pl-m1" aria-label="Module 1">
          <h3>Module 1 · Culture</h3><div class="pl-badges">${badges(specs.m1)}</div>
          ${specs.m1v1 ? `<article class="pl-card pl-v1" data-open="m1v1" data-file="${esc(d.files.m1v1 ?? '')}" tabindex="0">
            <header><b>Module 1 v1 · MJ's chip</b><span class="badge printed">being laser-cut</span></header>
            <ul class="pl-lanes">${LANES.map((l) => `<li class="pl-lane"><span class="sw" data-fate="${esc(fateCss(l.id))}"></span>${l.name}</li>`).join('')}</ul>
            <p class="pl-note">${esc(specs.m1v1.chambers.map((c) => c.id).join(' · '))} · no valves: FEED / VENT / CAP at 6 ports
              <span class="pl-day" data-day="v1">day 0</span></p>
            <p class="pl-out">harvest → p_mid<span class="pl-anchor out" data-anchor="m1v1_out"></span></p>
          </article>
          <p class="pl-note">Earlier brief design (valves; control + interaction chips):</p>` : ''}
          <div class="pl-pair">${m1card('control', d.files.m1_control)}${m1card('interaction', d.files.m1_interaction)}</div>
          ${specs.m1v1 ? '' : '<p class="pl-note">One chip, printed twice: control and interaction.</p>'}
        </section>
        <div class="pl-arrow" aria-hidden="true"></div>
        <section class="pl-col pl-m1b" aria-label="Module 1b">
          <h3>Module 1b · Cell prep</h3><div class="pl-badges">${badges(specs.m1b)}</div>
          <article class="pl-card" data-open="m1b" data-file="${esc(d.files.m1b ?? '')}" tabindex="0">
            <span class="pl-anchor in" data-anchor="m1b_in"></span>
            <ol class="pl-stages">${M1B_STAGES.map(([id, name], k) => `<li data-stage="m1b:${k}" title="${id}">${name}<span class="pl-anchor" data-anchor="m1b_${k}"></span></li>`).join('')}</ol>
            <p class="pl-qc" data-qc>counter: waiting</p>
            <p class="pl-verdict" data-verdict></p>
          </article>
        </section>
        <div class="pl-arrow" aria-hidden="true"></div>
        <section class="pl-col pl-m2" aria-label="Module 2">
          <h3>Module 2 · Encapsulation</h3><div class="pl-badges">${badges(specs.m2)}</div>
          <p class="pl-mj">Module 2 (encapsulation) – in progress by MJ</p>
          <article class="pl-card" data-open="m2" data-file="${esc(d.files.m2 ?? '')}" tabindex="0">
            <span class="pl-anchor in" data-anchor="m2_in"></span>
            <ol class="pl-stages">${M2_STAGES.map((name, k) => `<li data-stage="m2:${k}">${name}<span class="pl-anchor" data-anchor="m2_${k}"></span></li>`).join('')}</ol>
            <p class="pl-note">${Number(p2.capsule_count ?? 0).toLocaleString()} capsules · ${Math.round(perHour / 3600)} Hz · nozzle ${p2.nozzle_width_um ?? '–'} µm</p>
          </article>
        </section>
        <div class="pl-arrow" aria-hidden="true"></div>
        <section class="pl-col pl-m3" aria-label="Module 3">
          <h3>Module 3 · Capsule wash</h3><div class="pl-badges">${badges(specs.m3)}</div>
          <article class="pl-card" data-open="m3" data-file="${esc(d.files.m3 ?? '')}" tabindex="0">
            <span class="pl-anchor in" data-anchor="m3_in"></span>
            <ol class="pl-stages">${M3_STAGES.map((name, k) => `<li data-stage="m3:${k}">${name}<span class="pl-anchor" data-anchor="m3_${k}"></span></li>`).join('')}</ol>
            <p class="pl-note">${trap3 ? `${trap3.size[0]} × ${trap3.size[1]} mm trap · ${trap3.retention?.opening_um} µm mesh` : ''}</p>
          </article>
        </section>
        <div class="pl-arrow" aria-hidden="true"></div>
        <section class="pl-col pl-road" aria-label="Roadmap">
          <h3>Roadmap</h3>
          ${(d.pipe.roadmap ?? []).map((r, k) => `<article class="pl-card pl-roadmap" title="${esc(r.note ?? '')}">${esc(r.name)}<span class="badge">roadmap</span>${k === 0 ? '<span class="pl-anchor in" data-anchor="road_in"></span>' : ''}</article>`).join('')}
        </section>
      </div>
      <p class="pl-caption" data-caption></p>
      <div class="pl-dots" aria-hidden="true">${Array.from({ length: DOTS }, (_, k) => `<span class="pl-dot" data-dot="${k}"></span>`).join('')}</div>`;
    el = {
      dots: [...root.querySelectorAll('.pl-dot')],
      caption: root.querySelector('[data-caption]'),
      qc: root.querySelector('[data-qc]'),
      verdict: root.querySelector('[data-verdict]'),
      cols: Object.fromEntries(['m1', 'm1b', 'm2', 'm3', 'road'].map((m) => [m, root.querySelector(`.pl-${m}`)])),
    };
    root.querySelectorAll('[data-open]').forEach((card) => {
      const open = () => handlers.onOpenModule?.(card.dataset.open, card.dataset.file || null);
      card.addEventListener('click', open);
      card.addEventListener('keydown', (e) => { if (e.key === 'Enter') open(); });
    });
    render();
  }

  // ---------------------------------------------------------------- playback

  function indexAt(x) {
    let i = 0;
    while (i + 1 < starts.length && starts[i + 1] <= x + 1e-9) i++;
    return i;
  }

  function setPlaying(on) {
    if (on && t >= total - 1e-6) t = 0;
    playing = on;
    if (!on) stopAt = null;
    handlers.onPlayState?.(on);
  }

  function update(dt) {
    if (!visible || !phases.length) return;
    if (playing) {
      t += dt * speed;
      if (stopAt !== null && t >= stopAt) { t = stopAt; setPlaying(false); }
      if (t >= total) { t = total; setPlaying(false); }
    }
    render();
  }

  // ---------------------------------------------------------------- drawing

  function anchor(name) {
    const a = root.querySelector(`[data-anchor="${name}"]`);
    if (!a) return null;
    const r = a.getBoundingClientRect();
    const o = root.getBoundingClientRect();
    return { x: r.left + r.width / 2 - o.left + root.scrollLeft, y: r.top + r.height / 2 - o.top + root.scrollTop };
  }

  function along(points, f) {
    const pts = points.filter(Boolean);
    if (!pts.length) return null;
    if (pts.length === 1) return pts[0];
    const seg = [];
    let len = 0;
    for (let i = 1; i < pts.length; i++) { const d = Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y); seg.push(d); len += d; }
    let d = clamp01(f) * len;
    for (let i = 0; i < seg.length; i++) {
      if (d <= seg[i] || i === seg.length - 1) {
        const k = seg[i] ? d / seg[i] : 0;
        return { x: pts[i].x + (pts[i + 1].x - pts[i].x) * k, y: pts[i].y + (pts[i + 1].y - pts[i].y) * k };
      }
      d -= seg[i];
    }
    return pts[pts.length - 1];
  }

  function highlight(prefix, k) {
    root.querySelectorAll(`[data-stage^="${prefix}:"]`).forEach((li) => {
      const n = Number(li.dataset.stage.split(':')[1]);
      li.classList.toggle('active', n === k);
      li.classList.toggle('done', k !== null && n < k);
    });
  }

  function render() {
    if (!phases.length) return;
    const done = t >= total - 1e-6;
    const i = done ? phases.length - 1 : indexAt(t);
    const p = done ? 1 : clamp01((t - starts[i]) / phases[i].play);
    const ph = phases[i];
    if (i !== current) {
      current = i;
      handlers.onPhase?.(i, ph.event);
      el.caption.textContent = ph.narration;
    }
    const protoT = ph.event.t_s + p * ph.event.duration_s;
    handlers.onTick?.({ t, total, protocolT: protoT, index: i, progress: p });

    // Columns: active / done.
    const order = ['m1', 'm1', 'm1b', 'm1b', 'm2', 'm2', 'm3', 'road'];
    const rank = { m1: 0, m1b: 1, m2: 2, m3: 3, road: 4 };
    Object.entries(el.cols).forEach(([m, col]) => {
      col.classList.toggle('active', order[i] === m || (i === 7 && m === 'road'));
      col.classList.toggle('done', rank[m] < rank[order[i]]);
    });

    // Module 1: day counter, lanes take on their cell type's colour, pair valves.
    const days = Math.round((phases[0].proto) / 86400);
    const dayNow = i === 0 ? Math.floor(p * days) : days;
    const fateK = i === 0 ? ease(p) : 1;
    root.querySelectorAll('[data-day]').forEach((d) => { d.textContent = `day ${dayNow} of ${days}`; });
    root.querySelectorAll('.pl-lane .sw').forEach((sw) => {
      sw.style.background = `color-mix(in srgb, ${sw.dataset.fate} ${Math.round(fateK * 100)}%, #64748b)`;
    });
    const open = i > 0 || dayNow >= Math.round(days * 14 / 17);
    root.querySelectorAll('[data-valves]').forEach((v) => {
      const isOpen = v.dataset.valves === 'interaction' && open && i <= 1;
      v.classList.toggle('open', isOpen);
      v.lastChild.textContent = v.dataset.valves === 'control' ? 'pair valves closed (blocked in control)'
        : isOpen ? 'pair valves open: signalling' : open ? 'pair valves closed again' : 'pair valves closed';
    });

    // Module 1b / 2 / 3 stage highlights and readouts.
    const s1b = data.runs.m1b?.summary ?? {};
    const k1b = i === 2 ? Math.min(M1B_STAGES.length - 1, Math.floor(p * M1B_STAGES.length)) : i > 2 ? M1B_STAGES.length : null;
    highlight('m1b', k1b);
    const counted = k1b !== null && k1b >= 4 && s1b.counted;
    el.qc.textContent = counted ? `counter: ${s1b.counted.cells_per_ul.toLocaleString()} cells/µL · ${(s1b.counted.viability * 100).toFixed(0)}% viable` : 'counter: waiting';
    el.verdict.textContent = k1b !== null && k1b >= 5 ? `diluted ${s1b.dilution}× → ${s1b.mean_cells_per_capsule} cells/capsule · safety ${s1b.verdict?.result ?? '–'}` : '';
    el.verdict.classList.toggle('ok', s1b.verdict?.result === 'approved');
    const k2 = i === 4 ? Math.min(M2_STAGES.length - 1, Math.floor(p * M2_STAGES.length)) : i > 4 ? M2_STAGES.length : null;
    highlight('m2', k2);
    const k3 = i === 6 ? Math.min(M3_STAGES.length - 1, Math.floor(p * M3_STAGES.length)) : i > 6 ? M3_STAGES.length : null;
    highlight('m3', k3);

    // The dots: cells, then capsules, following the hand-offs.
    const v1out = data.specs.m1v1 ? anchor('m1v1_out') : null;
    const outs = v1out ? Array.from({ length: DOTS }, () => v1out)
      : ['control', 'interaction'].flatMap((v) => LANES.map((l) => anchor(`m1_${v}_${l.n}`)));
    const m1b = M1B_STAGES.map((_, k) => anchor(`m1b_${k}`));
    const m2 = M2_STAGES.map((_, k) => anchor(`m2_${k}`));
    const m3 = M3_STAGES.map((_, k) => anchor(`m3_${k}`));
    el.dots.forEach((dot, k) => {
      const stagger = k / DOTS * 0.35;
      const f = clamp01((p - stagger) / 0.65);
      let pos = null;
      let capsule = i >= 4 && (i > 4 || p > 1.5 / M2_STAGES.length);
      if (i === 1) pos = along([outs[k], anchor('m1b_in'), m1b[0]], ease(f));
      else if (i === 2) pos = along([m1b[0], ...m1b.slice(1)], ease(f));
      else if (i === 3) pos = along([m1b[m1b.length - 1], anchor('m2_in'), m2[0]], ease(f));
      else if (i === 4) pos = along(m2, ease(f));
      else if (i === 5) pos = along([m2[m2.length - 1], anchor('m3_in'), m3[0]], ease(f));
      else if (i === 6) pos = along(m3, ease(f));
      else if (i === 7) pos = along([m3[m3.length - 1], anchor('road_in')], ease(f));
      dot.hidden = !pos;
      if (pos) {
        dot.style.transform = `translate(${pos.x}px, ${pos.y}px)`;
        dot.classList.toggle('capsule', capsule);
      }
    });
  }

  return {
    load,
    show() { visible = true; root.hidden = false; current = -2; render(); },
    hide() { visible = false; root.hidden = true; setPlaying(false); },
    get visible() { return visible; },
    get playing() { return playing; },
    get t() { return t; },
    get total() { return total; },
    get currentIndex() { return current; },
    toggle() { setPlaying(!playing); },
    setPlaying,
    step() {
      if (t >= total - 1e-6) t = 0;
      const i = indexAt(t);
      stopAt = i + 1 < starts.length ? starts[i + 1] : total;
      playing = true;
      handlers.onPlayState?.(true);
    },
    seek(x) { t = Math.min(Math.max(x, 0), total); stopAt = null; render(); },
    seekToPhase(i) { this.seek(starts[Math.max(0, Math.min(i, starts.length - 1))] ?? 0); },
    setSpeed(x) { speed = x; },
    update,
    timeline() { return phases.map((ph, i) => ({ ev: ph.event, start: starts[i], duration: ph.play })); },
    runLike() {
      return { kind: 'pipeline', protocol_id: 'pipeline_mvp', chip_id: 'all modules', placeholder: true, operator: 'planner',
        total_duration_s: phases.reduce((a, ph) => a + ph.proto, 0) };
    },
    refresh() { current = -2; render(); },
  };
}

export { formatDuration };

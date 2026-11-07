// Pipeline view: every current module on one screen, left to right, from
// protocols/pipeline_mvp.json. Each module shows its status badge (FINAL, DRAFT, TRANSLATED,
// MISSING), whether it is built for Sunday's dye demo or exists in the twin only, and its
// outgoing hand-off (ports, TBC, connector warnings). Cells, then capsules, then library
// move card to card as dots; the bottom timeline is the pipeline's phases. Click a module
// to open its own view.
import { formatDuration } from './player.js';

// What each module does, as short stage names for the card (keyed by pipeline module id).
const STAGES = {
  module1: ['seed', 'grow apart', 'interaction modes', 'collect → j_mid'],
  module2: ['co-flow', 'nozzle', 'phase separation', 'cold delay (off-chip)', 'trap: oil out', 'UV cure', 'washes'],
  module3: ['load (100 µm pre-filter)', 'lysis ×2', 'wash ×3', 'back-flush'],
  barcoding: ['split', 'barcode', 'pool'],
  module4: ['fill ABCD DCBA', 'index primer', 'PCR mix', 'thermocycle'],
  module5: ['collagenase release', 'dilute', 'bead capture', 'elute → out_lib'],
};
const STATUS = { final: 'FINAL', draft: 'DRAFT', translated: 'TRANSLATED', missing: 'MISSING' };
const CARRIES = { cells: 'cells', capsules: 'capsules', library: 'library' };
const DOTS = 6;

const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const clamp01 = (x) => Math.min(1, Math.max(0, x));
const ease = (x) => { x = clamp01(x); return x < 0.5 ? 4 * x * x * x : 1 - (-2 * x + 2) ** 3 / 2; };
const portText = (end) => (end.ports ?? [end.port]).map((p) => (p === 'TBC' ? '<b class="tbc">TBC</b>' : esc(p))).join(', ');

function summary(e, days) {
  const s = e.spec;
  switch (e.module) {
    case 'module1':
      return `Three lineages grow apart, then each chamber's medium crosses to the other two in turn (${days} days, compressed). No valves: every route is FEED / VENT / CAP at the ports. This is the chip being laser-cut for Sunday's dye demo.`;
    case 'module2':
      return e.switched
        ? `MJ's Module 2 (${esc(e.chipPath)}), picked up automatically.`
        : 'Core and shell co-flow into the nozzle and oil pinches off droplets that phase-separate into core-shell capsules; cold delay off-chip, then oil out, UV cure and washes. A stand-in: MJ is redesigning this module.';
    case 'module3': {
      const trap = s?.chambers?.find((c) => c.id === 'main_trap');
      return `Szonja's ${trap ? `${trap.size[0]} × ${trap.size[1]} mm ` : ''}trap holds the capsules behind a ${trap?.retention?.opening_um ?? '–'} µm retention gap: two lysis exchanges to their own waste, three washes, then a back-flush lifts the bed. Translated into the chip-spec format; layout inferred.`;
    }
    case 'barcoding':
      return 'Split-and-pool barcoding (library steps 4–12) is not designed yet: Module 3 points to barcoding_geometry.json, which is not in the repo.';
    case 'module4':
      return 'An off-chip rotary valve splits the pooled capsules four ways, one chamber at a time in palindromic order (ABCD DCBA); each chamber gets its own index primer and the whole chip is thermocycled.';
    case 'module5':
      return 'One sublibrary per pass: collagenase at 37 °C dissolves the capsules, the eluate is diluted, the DNA is captured on magnetic beads, then eluted through out_lib.';
    default:
      return e.name;
  }
}

function facts(e) {
  const s = e.spec;
  if (!s) return 'no chip yet';
  const n = (k) => (s[k] ?? []).length;
  return `${s.footprint.x} × ${s.footprint.y} mm · ${n('chambers')} chambers · ${n('ports')} ports${n('valves') ? ` · ${n('valves')} valves` : ''}`;
}

function connectorText(c) {
  if (!c) return '';
  if (c.status === 'match') return `<p class="pl-conn ok" title="Same port style on both chips">connector match</p>`;
  return `<p class="pl-conn ${c.status}" title="${esc(c.warning)}">⚠ connector ${c.status === 'mismatch' ? 'mismatch' : 'unknown'}: ${esc(c.from)} → ${esc(c.to)}</p>`;
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

  // d = { pipe, entries: [{ ...pipeline module, chipPath, status, statusNote, switched, spec, view, file }], days }
  function load(d) {
    data = d;
    const { pipe, entries } = d;
    const col = Object.fromEntries(entries.map((e, k) => [e.module, k]));
    const outOf = (k) => pipe.handoffs.find((h) => h.from.module === entries[k].module);
    const days = d.days ?? 0;
    const stagesOf = (e) => (e.switched ? ['in', 'encapsulate', 'out'] : STAGES[e.module] ?? ['in', 'process', 'out']);

    phases = [];
    entries.forEach((e, k) => {
      phases.push({ id: `pl_${e.module}`, kind: 'module', k, play: e.status === 'missing' ? 3 : 5.5,
        proto: e.module === 'module1' ? days * 86400 : (e.duration_s ?? 600),
        narration: `${e.name} · ${STATUS[e.status]}${e.statusNote ? ` (${e.statusNote})` : ''}. ${summary(e, days)}` });
      const h = outOf(k);
      if (!h) return;
      const to = h.to.module !== undefined ? entries[col[h.to.module]] : null;
      const toName = to ? to.name : (pipe.roadmap ?? []).find((r) => r.id === h.to.roadmap)?.name ?? h.to.roadmap;
      const tbc = [h.from, h.to].some((x) => (x.ports ?? [x.port]).includes('TBC'));
      const conn = h.connector
        ? h.connector.status === 'match' ? ' Same port style on both chips.' : ` Connector ${h.connector.status}: ${h.connector.warning}`
        : '';
      phases.push({ id: `pl_${h.id}`, kind: 'handoff', k, to: to ? col[h.to.module] : null, h, play: 2.4, proto: 120,
        narration: `Hand-off: ${e.name} ${(h.from.ports ?? [h.from.port]).join(', ')} → ${toName}${to ? ` ${(h.to.ports ?? [h.to.port]).join(', ')}` : ''} (${CARRIES[h.carries]}).${tbc ? ' TBC: port not named yet.' : ''}${conn}` });
    });
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

    const card = (e, k) => {
      const h = outOf(k);
      const stages = stagesOf(e);
      const demo = e.built_for_demo;
      return `
        <section class="pl-col${e.status === 'missing' ? ' pl-missing' : ''}" data-col="${k}" aria-label="${esc(e.name)}">
          <h3>${esc(e.name)}</h3>
          <div class="pl-badges">
            <span class="badge st-${e.status}" title="${esc(e.statusNote ?? '')}">${STATUS[e.status]}</span>
            <span class="badge ${demo ? 'printed' : 'sim-only'}">${demo ? 'Built for Sunday’s dye demo' : 'Twin only'}</span>
          </div>
          ${e.statusNote ? `<p class="pl-mj">${esc(e.statusNote)}</p>` : ''}
          <article class="pl-card" ${e.view ? `data-open="${esc(e.view)}" data-file="${esc(e.file ?? '')}" tabindex="0" title="Open ${esc(e.name)}"` : 'title="Not designed yet: nothing to open"'}>
            <span class="pl-anchor in" data-anchor="in_${k}"></span>
            ${e.module === 'module1' ? '<header><b>MJ’s chip</b><span class="pl-day" data-day>day 0</span></header>' : ''}
            <ol class="pl-stages">${stages.map((s, j) => `<li data-stage="${k}:${j}">${esc(s)}<span class="pl-anchor" data-anchor="s_${k}_${j}"></span></li>`).join('')}</ol>
            <p class="pl-note">${esc(facts(e))}</p>
            ${h ? `<p class="pl-out">out: ${portText(h.from)} → ${h.to.module ? portText(h.to) : esc(h.to.roadmap)}<span class="pl-anchor out" data-anchor="out_${k}"></span></p>` : ''}
            ${h ? connectorText(h.connector) : ''}
          </article>
          <p class="pl-meta">${esc(e.owner ?? '')}${e.build ? ` · ${esc(e.build.method)}` : ''}</p>
        </section>
        <div class="pl-arrow" aria-hidden="true"></div>`;
    };
    root.innerHTML = `
      <p class="pl-legend">
        <span class="badge st-final">FINAL</span><span class="badge st-draft">DRAFT</span><span class="badge st-translated">TRANSLATED</span><span class="badge st-missing">MISSING</span>
        <span class="pl-legend-gap"></span>
        <span class="badge printed">Built for Sunday’s dye demo</span><span class="badge sim-only">Twin only</span>
        <span class="pl-legend-gap"></span><span class="pl-legend-note">⚠ = the dye demo needs an adapter between the two chips' ports · <b class="tbc">TBC</b> = port not named yet</span>
      </p>
      <div class="pl-row">
        ${entries.map(card).join('')}
        <section class="pl-col pl-road" data-col="${entries.length}" aria-label="Library out">
          <h3>Library out</h3>
          ${(pipe.roadmap ?? []).map((r, k) => `<article class="pl-card pl-roadmap" title="${esc(r.note ?? '')}">${esc(r.name)}<span class="badge">${k === 0 ? 'output' : 'next'}</span>${k === 0 ? '<span class="pl-anchor in" data-anchor="road_in"></span>' : ''}</article>`).join('')}
        </section>
      </div>
      <p class="pl-caption" data-caption></p>
      <div class="pl-dots" aria-hidden="true">${Array.from({ length: DOTS }, (_, k) => `<span class="pl-dot" data-dot="${k}"></span>`).join('')}</div>`;
    el = {
      dots: [...root.querySelectorAll('.pl-dot')],
      caption: root.querySelector('[data-caption]'),
      cols: [...root.querySelectorAll('.pl-col')],
      stages: entries.map((e) => stagesOf(e).length),
    };
    root.querySelectorAll('[data-open]').forEach((c) => {
      const open = () => handlers.onOpenModule?.(c.dataset.open, c.dataset.file || null);
      c.addEventListener('click', open);
      c.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') open(); });
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

  // Dot position of an anchor, in the view's own (unscaled) pixels: the presenter view scales
  // the whole pipeline up with a CSS transform, and the dots move inside that transform.
  function anchor(name) {
    const a = root.querySelector(`[data-anchor="${name}"]`);
    if (!a) return null;
    const r = a.getBoundingClientRect();
    const o = root.getBoundingClientRect();
    const s = root.offsetWidth ? o.width / root.offsetWidth : 1;
    return { x: (r.left + r.width / 2 - o.left) / s + root.scrollLeft, y: (r.top + r.height / 2 - o.top) / s + root.scrollTop };
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

  // What the dots are while module k works: what came in, then (past halfway) what goes out.
  function carried(k, p) {
    const { pipe, entries } = data;
    const inH = pipe.handoffs.find((h) => h.to.module === entries[k]?.module);
    const outH = pipe.handoffs.find((h) => h.from.module === entries[k]?.module);
    return (p < 0.5 ? inH?.carries : outH?.carries) ?? inH?.carries ?? outH?.carries ?? 'cells';
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
    handlers.onTick?.({ t, total, protocolT: ph.event.t_s + p * ph.event.duration_s, index: i, progress: p });

    // Columns: the working module (or the hand-off's destination) is active; earlier ones are done.
    const activeCol = ph.kind === 'module' ? ph.k : ph.to ?? el.cols.length - 1;
    el.cols.forEach((c, k) => {
      c.classList.toggle('active', k === activeCol || (ph.kind === 'handoff' && k === ph.k));
      c.classList.toggle('done', k < (ph.kind === 'module' ? ph.k : ph.k + 1) && !(ph.kind === 'handoff' && k === ph.k));
    });

    // Stage highlights.
    el.stages.forEach((n, k) => {
      const at = ph.kind === 'module' && ph.k === k ? Math.min(n - 1, Math.floor(p * n)) : k < (ph.kind === 'module' ? ph.k : ph.k + 1) ? n : null;
      root.querySelectorAll(`[data-stage^="${k}:"]`).forEach((li) => {
        const j = Number(li.dataset.stage.split(':')[1]);
        li.classList.toggle('active', j === at);
        li.classList.toggle('done', at !== null && j < at);
      });
    });

    // Module 1's day counter.
    const days = data.days ?? 0;
    const m1 = data.entries.findIndex((e) => e.module === 'module1');
    const dayNow = i === 0 && m1 === 0 ? Math.floor(p * days) : days;
    root.querySelectorAll('[data-day]').forEach((x) => { x.textContent = `day ${dayNow} of ${days}`; });

    // Dots.
    el.dots.forEach((dot, k) => {
      const stagger = (k / DOTS) * 0.35;
      const f = ease(clamp01((p - stagger) / 0.65));
      let pos = null;
      let kind = 'cells';
      if (ph.kind === 'module') {
        const pts = Array.from({ length: el.stages[ph.k] }, (_, j) => anchor(`s_${ph.k}_${j}`));
        pos = along([anchor(`in_${ph.k}`), ...pts], f);
        kind = carried(ph.k, f);
      } else {
        const last = anchor(`s_${ph.k}_${el.stages[ph.k] - 1}`);
        const dest = ph.to !== null ? [anchor(`in_${ph.to}`), anchor(`s_${ph.to}_0`)] : [anchor('road_in')];
        pos = along([last, anchor(`out_${ph.k}`), ...dest], f);
        kind = ph.h.carries;
      }
      dot.hidden = !pos;
      if (pos) {
        dot.style.transform = `translate(${pos.x}px, ${pos.y}px)`;
        dot.classList.toggle('capsule', kind === 'capsules');
        dot.classList.toggle('library', kind === 'library');
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
    get data() { return data; },
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
    phaseIndex(id) { return phases.findIndex((ph) => ph.id === id); },
    // Play phases i0..i1 (inclusive) and stop: the presenter's hand-off from Module 1 to Module 2.
    playRange(i0, i1) {
      t = starts[i0] ?? 0;
      stopAt = (starts[i1] ?? total) + (phases[i1]?.play ?? 0) - 1e-3; // end inside phase i1, not on the next one's first frame
      playing = true;
      current = -2;
      handlers.onPlayState?.(true);
      render();
    },
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

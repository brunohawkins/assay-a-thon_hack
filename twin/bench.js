// Bench demo: Module 1 in real time, next to the food-dye run on the bench.
//
// The physical demo pushes red, yellow and blue food dye (neuron, glia and astrocyte chambers)
// through the Module 1 chip with syringe pumps. This view runs the same thing on the twin: pick
// the mode, set the bench up as the port table says, start the pumps and the timer together, and
// the twin's dye moves in real time. Predicted arrival times come from the chip's network model
// (twin/network.js, the same model as operator/network.py); "Mark" records when the dye arrives on
// the bench, so the two can be compared. The twin never talks to the pumps.
import { MODES, modeStates, dyeTimes, keyResult } from './network.js';
import { DYE_OF_PORT, DYE_NAMES } from './module1.js';

const MODE_LIST = [
  ['grow', 'Separate growth', 'Each inlet feeds its own chamber; all three leave through the junction outlet.'],
  ['int_n', 'Neurons to glia and astrocytes', 'Only red is pumped. It crosses the junction into the other two chambers.'],
  ['int_g', 'Glia to neurons and astrocytes', 'Only yellow is pumped, out through the neuron inlet and the astrocyte outlet.'],
  ['int_a', 'Astrocytes to neurons and glia', 'Only blue is pumped, out through the neuron inlet and the glia outlet.'],
  ['collect', 'Harvest', 'Syringes on the neuron inlet and both outlets flush all three chambers out through p_mid.'],
];
const PORTS = [
  ['p_in', 'neuron inlet'], ['p_g_in', 'glia inlet'], ['p_a_in', 'astrocyte inlet'],
  ['p_mid', 'junction outlet'], ['p_g_out', 'glia outlet'], ['p_a_out', 'astrocyte outlet'],
];
const PLACE = { j_mid: 'junction', ...Object.fromEntries(PORTS) };
const CHAMBER = { n_cx: 'neuron', g_cx: 'glia', a_cx: 'astrocyte' };
const DEFAULT_RATE = 10;   // µL/min: arrivals of about 15 s to 80 s, easy to time by hand
const STORE = 'twin-bench';

const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const clock = (s) => `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, '0')}`;
const mmss = (s) => { const t = Math.round(s); return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}`; };
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
const uniq = (xs) => [...new Set(xs)];
const list = (xs) => (xs.length < 2 ? xs.join('') : `${xs.slice(0, -1).join(', ')} and ${xs.at(-1)}`);
const dyesOf = (g) => uniq(g.items.map((r) => DYE_OF_PORT[r.src])).sort();

export function createBench({ el, m1, onOpenChip }) {
  let attached = false;
  let mode = 'grow';
  let rate = DEFAULT_RATE;
  let tubeUl = 0;
  let running = false;
  let t0 = 0;           // performance.now() when the timer last started, minus what had elapsed
  let elapsed = 0;      // seconds on the timer
  let simT = 0;         // seconds the simulation has advanced since the timer was reset
  let marks = {};       // row key -> seconds on the timer when the dye arrived on the bench
  let rows = [];
  let lastText = '';

  try {
    const saved = JSON.parse(localStorage.getItem(STORE) ?? '{}');
    if (saved.rate > 0) rate = saved.rate;
    if (saved.tubeUl >= 0) tubeUl = saved.tubeUl;
    if (MODES[saved.mode]) mode = saved.mode;
  } catch { /* private window: defaults */ }
  const save = () => { try { localStorage.setItem(STORE, JSON.stringify({ rate, tubeUl, mode })); } catch { /* ignore */ } };

  // ---------------------------------------------------------------- state

  function applyMode() {
    const { states, flows } = modeStates(mode, rate);
    m1.apply({ mode, states, flows }, 'bench');
    stop(true);
  }

  function stop(clearTime) {
    running = false;
    if (clearTime) { elapsed = 0; simT = 0; marks = {}; }
    render();
  }

  function start() {
    if (running) return;
    running = true;
    t0 = performance.now() - elapsed * 1000;
    render();
  }

  function pause() {
    if (!running) return;
    elapsed = (performance.now() - t0) / 1000;
    running = false;
    render();
  }

  // A fresh chip full of water: the dye starts at the syringes.
  function reset() {
    m1.reset();
    stop(true);
  }

  // What to watch for, in order: each dye filling a chamber, reaching the junction, reaching an
  // open port. Arrivals of the same kind within 1.5 s of each other are one event (in separate
  // growth all three chambers fill together), and the junction and the outlet right after it are
  // one row.
  function arrivals() {
    const sol = m1.solution;
    const st = m1.state;
    if (!sol || !st || !m1.net) return [];
    const extra = rate > 0 ? (tubeUl / rate) * 60 : 0;
    const raw = [];
    for (const [src, targets] of Object.entries(dyeTimes(m1.net, sol, st.states, st.flows, { chambers: true }))) {
      for (const [tg, x] of Object.entries(targets)) {
        raw.push({ src, tg, kind: x.chamber ? 'chamber' : tg === 'j_mid' ? 'junction' : 'outlet', first: x.first_s + extra, mean: x.mean_s + extra });
      }
    }
    raw.sort((a, b) => a.mean - b.mean);
    const groups = [];
    for (const r of raw) {
      const g = groups.find((x) => x.kind === r.kind && Math.abs(x.mean - r.mean) <= 1.5);
      if (g) { g.items.push(r); g.first = Math.min(g.first, r.first); g.mean = Math.max(g.mean, r.mean); }
      else groups.push({ kind: r.kind, items: [r], first: r.first, mean: r.mean, also: null });
    }
    for (const j of groups.filter((x) => x.kind === 'junction')) {
      const o = groups.find((x) => x.kind === 'outlet' && Math.abs(x.mean - j.mean) <= 1.5 && String(dyesOf(x)) === String(dyesOf(j)));
      if (o) { j.also = o; j.mean = Math.max(j.mean, o.mean); groups.splice(groups.indexOf(o), 1); }
    }
    return groups.map((g) => ({ ...g, key: `${g.kind}:${g.items.map((r) => `${r.src}>${r.tg}`).sort().join(',')}`, label: labelOf(g) }))
      .sort((a, b) => a.first - b.first || a.mean - b.mean);
  }

  function labelOf(g) {
    const dyes = dyesOf(g).map((k) => DYE_NAMES[k]);
    const who = cap(list(dyes));
    const many = dyes.length > 1;
    if (g.kind === 'chamber') {
      const places = uniq(Object.keys(CHAMBER).filter((c) => g.items.some((r) => r.tg === c)).map((c) => CHAMBER[c]));
      return `${who} ${many ? 'fill' : 'fills'} the ${list(places)} chamber${places.length > 1 ? 's' : ''}`;
    }
    const order = ['j_mid', ...PORTS.map(([id]) => id)];
    const at = (items) => list(uniq([...items].sort((a, b) => order.indexOf(a.tg) - order.indexOf(b.tg)).map((r) => PLACE[r.tg] ?? r.tg)));
    if (g.kind === 'junction') {
      return `${who} ${many ? 'reach' : 'reaches'} the junction${g.also ? ` and ${many ? 'leave' : 'leaves'} through the ${at(g.also.items)}` : ''}`;
    }
    return `${who} ${many ? 'reach' : 'reaches'} the ${at(g.items)}`;
  }
  // Each prediction is a window: from the dye first showing there (a chamber: the dye entering it;
  // elsewhere: the first tint) to plug-flow arrival (a chamber: swept once). A mark is "first seen".
  const verdict = (r, m) => {
    if (m >= r.first * 0.9 && m <= r.mean * 1.1) return { ok: true, text: 'in window' };
    const d = m < r.first ? m - r.first : m - r.mean;
    return { ok: false, text: `${d < 0 ? '−' : '+'}${Math.abs(Math.round(d))} s` };
  };

  // ---------------------------------------------------------------- render

  function portRow([id, name]) {
    const st = m1.state?.states[id] ?? 'CAP';
    const k = DYE_OF_PORT[id];
    const set = st === 'FEED'
      ? `<span class="port-state feed"><i style="background:${m1.dyeCss(k)}"></i>Syringe, ${DYE_NAMES[k]} dye</span>`
      : st === 'VENT' ? '<span class="port-state open"><i></i>Open, to waste</span>' : '<span class="port-state plug"><i></i>Plugged</span>';
    return `<tr><td><code>${id}</code></td><td class="meta-cell">${esc(name)}</td><td>${set}</td></tr>`;
  }

  function arrivalRow(r) {
    const m = marks[r.key];
    let bench;
    if (m != null) {
      const v = verdict(r, m);
      bench = `${mmss(m)} <span class="delta ${v.ok ? 'ok' : 'off'}" title="${v.ok ? 'Inside the predicted window' : 'Outside the predicted window, by this much'}">${v.text}</span>`;
    } else {
      bench = `<button type="button" data-mark="${esc(r.key)}"${running ? '' : ' disabled'}>Mark</button>`;
    }
    const dots = dyesOf(r).map((k) => `<span class="dot" style="background:${m1.dyeCss(k)}"></span>`).join('');
    const when = `<td class="num" title="${r.kind === 'chamber' ? `The dye enters at ${mmss(r.first)}; the chamber is swept once (plug flow) by ${mmss(r.mean)}`
      : `First tint ${mmss(r.first)}; plug-flow arrival ${mmss(r.mean)}`}">${mmss(r.first)}–${mmss(r.mean)}</td>`;
    return `<tr data-row="${esc(r.key)}"><td><span class="dots">${dots}</span>${esc(r.label)}</td>${when}<td class="num">${bench}</td></tr>`;
  }

  function render() {
    if (!attached) return;
    rows = arrivals();
    const sol = m1.solution;
    const v = m1.verdict;
    const blocked = v?.status === 'blocked';
    // During a run the timer and what to watch for come first; the set-up is done before.
    const desc = MODE_LIST.find(([id]) => id === mode)?.[2] ?? '';
    el.innerHTML = `
      <section class="side-section">
        <div class="chip-head"><div><h2>Bench demo</h2><span class="id">module1_v1 · food dye · real time</span></div></div>
        <p class="lead" style="margin-top:8px">Run the dye through the chip on the bench and the twin alongside, on the same clock.</p>
        <div class="timer" style="margin-top:14px"><span class="t" aria-live="off">${clock(elapsed)}</span>
          <div class="buttons"><button type="button" class="primary" data-act="${running ? 'pause' : 'start'}">${running ? 'Pause' : elapsed > 0 ? 'Resume' : 'Start'}</button>
          <button type="button" data-act="reset" title="Stop, clear the dye and set the timer to zero">Reset</button></div></div>
      </section>
      <section class="side-section">
        <h3>Watch for</h3>
        <table class="arrivals"><thead><tr><th>Event</th><th class="num">Predicted</th><th class="num">Bench</th></tr></thead>
          <tbody>${rows.map(arrivalRow).join('') || '<tr><td colspan="3" class="pending">No flow in this mode.</td></tr>'}</tbody></table>
        <p class="meta">Each window runs from the dye first showing there to plug-flow arrival (the chip's network model).
          Press Mark when you first see it on the bench.</p>
      </section>
      <section class="side-section">
        <h3>Mode</h3>
        <div class="bench-modes" role="group" aria-label="Mode">${MODE_LIST.map(([id, name]) => `<button type="button" data-mode="${id}" aria-pressed="${id === mode}">${esc(name)}</button>`).join('')}</div>
        <p class="lead" style="margin-top:10px">${esc(desc)}</p>
        ${blocked ? `<p class="note warn"><b>Not applied.</b> ${esc(v.reasons.join('; '))}</p>` : ''}
        ${sol && !blocked ? `<p class="meta">${esc(keyResult(mode, sol))}</p>` : ''}
      </section>
      <section class="side-section">
        <h3>Bench set-up</h3>
        <div class="rate-row"><label for="bench-rate">Pump rate, each syringe</label>
          <input id="bench-rate" type="number" min="0.5" max="500" step="0.5" value="${rate}"><span class="unit">µL/min</span></div>
        <table class="ports" style="margin-top:10px"><thead><tr><th>Port</th><th></th><th>On the bench</th></tr></thead>
          <tbody>${PORTS.map(portRow).join('')}</tbody></table>
        <div class="rate-row" style="margin-top:12px"><label for="bench-tube">Tubing to the chip</label>
          <input id="bench-tube" type="number" min="0" step="1" value="${tubeUl}"><span class="unit">µL</span></div>
        <p class="meta">Prime the chip with water first, so it starts clear. Start the timer as the dye enters the chip, or enter the
          tubing's volume and start it with the pumps. Changing the mode keeps the dye in the chip; Reset clears it.</p>
        <p style="margin-top:14px"><button type="button" data-act="open">Open Module 1 in detail</button></p>
      </section>`;
    lastText = '';
    tick();
  }

  // Timer text and which arrivals are due: cheap, every frame.
  function tick() {
    const t = el.querySelector('.timer .t');
    if (!t) return;
    const text = clock(elapsed);
    if (text !== lastText) { t.textContent = text; lastText = text; }
    for (const r of rows) {
      const tr = el.querySelector(`tr[data-row="${CSS.escape(r.key)}"]`);
      tr?.classList.toggle('due', elapsed >= r.first * 0.9 && marks[r.key] == null);
    }
  }

  el.addEventListener('click', (e) => {
    if (!attached) return;
    const b = e.target.closest('button');
    if (!b || b.disabled) return;
    if (b.dataset.mode && b.dataset.mode !== mode) { mode = b.dataset.mode; save(); applyMode(); }
    else if (b.dataset.act === 'start') start();
    else if (b.dataset.act === 'pause') pause();
    else if (b.dataset.act === 'reset') reset();
    else if (b.dataset.act === 'open') onOpenChip?.();
    else if (b.dataset.mark) { marks[b.dataset.mark] = running ? (performance.now() - t0) / 1000 : elapsed; render(); }
  });
  el.addEventListener('change', (e) => {
    if (!attached) return;
    if (e.target.id === 'bench-rate') {
      const x = Number(e.target.value);
      if (x > 0) { rate = x; save(); applyMode(); } else e.target.value = rate;
    } else if (e.target.id === 'bench-tube') {
      const x = Number(e.target.value);
      tubeUl = x >= 0 ? x : 0;
      save();
      render();
    }
  });

  return {
    attach() {
      attached = true;
      m1.setBench(true);
      m1.reset();
      elapsed = 0;
      simT = 0;
      marks = {};
      running = false;
      applyMode();
    },
    detach() {
      if (!attached) return;
      attached = false;
      running = false;
      m1.setBench(false);
      el.replaceChildren();
    },
    // Every frame, before Module 1 draws: advance the dye by the wall-clock time on the timer.
    update() {
      if (!attached) return;
      if (running) {
        elapsed = (performance.now() - t0) / 1000;
        const d = Math.min(elapsed - simT, 5);
        if (d > 0) { m1.advance(d); simT += d; }
      }
      tick();
    },
    get attached() { return attached; },
    // for the smoke test
    get mode() { return mode; },
    get rate() { return rate; },
    get running() { return running; },
    get elapsed() { return elapsed; },
    get rows() { return rows; },
    get marks() { return marks; },
    setMode(m) { if (MODES[m]) { mode = m; applyMode(); } },
    start, pause, reset,
  };
}

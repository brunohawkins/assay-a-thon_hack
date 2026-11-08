// Bench demo: the twin beside the one-pump food-dye run on the bench.
//
// What is on the bench (docs/demo.md): printed and hot-embossed chips at 30 x 20 mm, ONE syringe
// pump, food dye in water. For every demo-scale chip, tools/demo_plan.py derives one feed, one
// vent, the ports to plug, the pump rate, and the order the dye reaches each part with its
// predicted times; docs/demo_plan.json is the same plans as JSON (tests/test_demo_plan_json.py
// keeps it current). Nothing here is typed in: this view reads that file and runs the twin
// alongside, on one clock that starts when the dye reaches the feed port. Module 1 runs through
// its flow model (twin/network.js, the same model as operator/network.py); every other chip
// colours its parts along the predicted route. The twin never talks to the pump.
import * as THREE from 'three';
import { CHANNEL } from './chip.js';
import { calm } from './status.js';

const M1 = 'module1_v1';
const DYE = new THREE.Color().setRGB(0.94, 0.27, 0.27, THREE.SRGBColorSpace);   // Module 1's red, for every chip
const DYE_CSS = 'rgb(240, 69, 69)';
const FIRST_SHOW = 2 / 3;      // docs/demo.md: the fast centre of the stream shows about a third sooner

const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const clock = (s) => `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, '0')}`;
const mmss = (s) => { const t = Math.round(s); return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, '0')}`; };
const list = (xs) => (xs.length < 2 ? xs.join('') : `${xs.slice(0, -1).join(', ')} and ${xs.at(-1)}`);
const clamp01 = (x) => Math.min(1, Math.max(0, x));

export function createBench({ el, m1, root, getChip, nameOf = (id) => id, order = () => [], onPick }) {
  let plans = new Map();     // chip id -> plan (docs/demo_plan.json)
  let loadError = null;
  let plan = null;
  let chip = null;
  let attached = false;
  let running = false;
  let t0 = 0;
  let elapsed = 0;
  let simT = 0;
  let marks = {};
  let saved = [];            // materials changed on a non-Module-1 chip, restored on detach
  let lastText = '';

  async function load() {
    try {
      const res = await fetch(`${root}docs/demo_plan.json`, { cache: 'no-store' });
      if (!res.ok) throw new Error(`docs/demo_plan.json: HTTP ${res.status}`);
      plans = new Map((await res.json()).filter((p) => p.flow_ul_min).map((p) => [p.chip_id, p]));
    } catch (err) {
      loadError = `${err.message}. Generate it with: python tools/demo_plan.py --json > docs/demo_plan.json`;
      plans = new Map();
    }
  }

  // Chips in pipeline order first, then the rest.
  function chipsInOrder() {
    const want = order();
    const ids = [...plans.keys()];
    return [...want.filter((id) => plans.has(id)), ...ids.filter((id) => !want.includes(id)).sort()];
  }

  // ---------------------------------------------------------------- the chip in 3D

  const portState = (id) => (id === plan.feed ? 'FEED' : id === plan.vent ? 'VENT' : 'CAP');

  function prepareChip() {
    if (plan.chip_id === M1) {
      const states = Object.fromEntries(chip.spec.ports.map((p) => [p.id, portState(p.id)]));
      m1.setBench(true);
      m1.apply({ mode: 'custom', states, flows: { [plan.feed]: plan.flow_ul_min } }, 'bench');
      m1.reset();
      return;
    }
    // Every other chip: the protocol player stands aside (flowOwned) and the dye is drawn here.
    chip.flowOwned = true;
    const keep = (mat, props) => saved.push([mat, Object.fromEntries(props.map((k) => [k, mat[k]?.clone ? mat[k].clone() : mat[k]]))]);
    for (const part of chip.parts.values()) {
      if (part.kind === 'chamber') {
        part.fluid.visible = false;
        part.glow.visible = false;
        for (const m of [part.block?.mesh, part.stl]) {
          if (!m) continue;
          keep(m.material, ['opacity', 'emissiveIntensity']);
          m.material.opacity = 0.08;
          m.material.emissiveIntensity = 0;
        }
        keep(part.fluidMaterial, ['color', 'emissive', 'emissiveIntensity', 'opacity', 'envMapIntensity']);
      } else if (part.ringMaterial) {
        keep(part.ringMaterial, ['color', 'emissive']);
        const st = portState(part.id);
        const c = st === 'FEED' ? DYE : new THREE.Color(st === 'VENT' ? 0x8c959f : 0x424a53);
        part.ringMaterial.color.copy(c);
        part.ringMaterial.emissive.copy(c);
      }
    }
    for (const ch of chip.channels) keep(ch.material, ['color', 'emissive', 'emissiveIntensity', 'opacity']);
    drawRoute(0);
  }

  function releaseChip() {
    if (!plan || !chip) return;
    if (plan.chip_id === M1) { m1.setBench(false); return; }
    for (const [mat, props] of saved) for (const [k, v] of Object.entries(props)) { if (v?.copy) mat[k].copy(v); else mat[k] = v; }
    saved = [];
    for (const part of chip.parts.values()) if (part.kind === 'chamber') part.fluid.visible = false;
    chip.flowOwned = false;
  }

  // Colour every part on the route as far as the dye has got at time t (plug flow, as predicted).
  function drawRoute(t) {
    for (const step of plan.sequence) {
      const f = step.fill_s > 0 ? clamp01((t - step.starts_s) / step.fill_s) : t >= step.starts_s ? 1 : 0;
      if (step.kind === 'chamber') {
        const part = chip.parts.get(step.id);
        if (!part) continue;
        part.fluid.visible = f > 0;
        if (f > 0) {
          part.fluid.scale.z = Math.max(f * part.size.z * 0.96, 0.0001);
          part.fluidMaterial.color.setRGB(0, 0, 0);
          part.fluidMaterial.emissive.copy(DYE);
          part.fluidMaterial.emissiveIntensity = 0.95;
          part.fluidMaterial.envMapIntensity = 0;
          part.fluidMaterial.opacity = 0.35 + 0.55 * f;
        }
      } else if (step.kind === 'channel') {
        const ch = chip.channels.find((c) => c.id === step.id);
        if (!ch) continue;
        if (f > 0) {
          ch.material.color.copy(DYE);
          ch.material.emissive.copy(DYE);
          ch.material.emissiveIntensity = 0.4;
          ch.material.opacity = 0.6 + 0.3 * f;
        } else {
          ch.material.color.setHex(CHANNEL.color);
          ch.material.emissive.setHex(CHANNEL.emissive);
          ch.material.emissiveIntensity = CHANNEL.emissiveIntensity;
          ch.material.opacity = CHANNEL.opacity;
        }
      }
    }
  }

  // ---------------------------------------------------------------- timer

  function start() { if (running) return; running = true; t0 = performance.now() - elapsed * 1000; render(); }
  function pause() { if (!running) return; elapsed = (performance.now() - t0) / 1000; running = false; render(); }
  function reset() {
    running = false;
    elapsed = 0;
    simT = 0;
    marks = {};
    if (plan?.chip_id === M1) m1.reset(); else if (plan) drawRoute(0);
    render();
  }

  // A mark is "first seen". The window runs from the fast centre of the stream showing (about a
  // third before the plug-flow arrival, docs/demo.md) to the part being full.
  const windowOf = (s) => [s.starts_s * FIRST_SHOW, s.ends_s];
  const verdict = (s, m) => {
    const [a, b] = windowOf(s);
    if (m >= a * 0.9 && m <= b * 1.1) return { ok: true, text: 'in window' };
    const d = m < a ? m - a : m - b;
    return { ok: false, text: `${d < 0 ? '−' : '+'}${Math.abs(Math.round(d))} s` };
  };

  // ---------------------------------------------------------------- sidebar

  function stepRow(s, i) {
    const key = `${s.kind}:${s.id}`;
    const [a] = windowOf(s);
    const m = marks[key];
    let bench = '';
    if (s.filmable) {
      if (m != null) {
        const v = verdict(s, m);
        bench = `${mmss(m)} <span class="delta ${v.ok ? 'ok' : 'off'}">${v.text}</span>`;
      } else bench = `<button type="button" data-mark="${esc(key)}"${running ? '' : ' disabled'}>Mark</button>`;
    }
    const what = s.kind === 'port' ? `${s.id} hole` : s.id;
    const title = s.kind === 'chamber'
      ? `Dye enters at ${mmss(s.starts_s)} (the fast centre about ${mmss(a)}); full by ${mmss(s.ends_s)}. ${s.volume_ul} µL.`
      : `${mmss(s.starts_s)} to ${mmss(s.ends_s)}. ${s.volume_ul} µL.`;
    return `<tr data-row="${esc(key)}" data-i="${i}" class="${s.filmable ? 'film' : 'fast'}" title="${esc(title)}">
      <td><code>${esc(what)}</code> <span class="kind">${esc(s.kind)}</span></td>
      <td class="num">${mmss(s.starts_s)}–${mmss(s.ends_s)}</td><td class="num">${bench}</td></tr>`;
  }

  function render() {
    if (!attached) return;
    if (!plan) {
      el.innerHTML = `<section class="side-section"><h2 class="side-title">Bench demo</h2><p class="note warn">${esc(loadError ?? 'No dye-demo plan for this chip.')}</p></section>`;
      return;
    }
    const p = plan;
    const ports = chip?.spec.ports.map((x) => x.id) ?? [];
    const plugs = ports.filter((id) => portState(id) === 'CAP');
    const limit = p.pressure_limit_kpa;
    el.innerHTML = `
      <section class="side-section">
        <div class="chip-head"><div><h2>Dye demo</h2><span class="id">one syringe pump · food dye in water · real time</span></div></div>
        <label class="field" style="margin-top:12px"><span>Chip</span>
          <select id="bench-chip">${chipsInOrder().map((id) => `<option value="${esc(id)}"${id === p.chip_id ? ' selected' : ''}>${esc(nameOf(id))} · ${esc(id)}</option>`).join('')}</select></label>
        <div class="timer" style="margin-top:14px"><span class="t" aria-live="off">${clock(elapsed)}</span>
          <div class="buttons"><button type="button" class="primary" data-act="${running ? 'pause' : 'start'}">${running ? 'Pause' : elapsed > 0 ? 'Resume' : 'Start'}</button>
          <button type="button" data-act="reset" title="Stop, clear the dye and set the timer to zero">Reset</button></div></div>
        <p class="meta">Start the timer as the dye reaches <code>${esc(p.feed)}</code>. The twin runs alongside on the same clock.</p>
      </section>
      <section class="side-section">
        <h3>Set up</h3>
        <table class="ports">
          <tr><td>Pump</td><td><span class="port-state feed"><i style="background:${DYE_CSS}"></i><span><code>${esc(p.feed)}</code> at ${p.flow_ul_min} µL/min</span></span></td></tr>
          <tr><td>Vent</td><td><span class="port-state open"><i></i><span><code>${esc(p.vent)}</code> open to waste</span></span></td></tr>
          <tr><td>Plug</td><td><span class="port-state plug"><i></i><span>${plugs.length ? plugs.map((x) => `<code>${esc(x)}</code>`).join(', ') : 'nothing'}</span></span></td></tr>
        </table>
        <p class="meta">Drive pressure ${p.pressure_kpa.toFixed(3)} kPa, ${((100 * p.pressure_kpa) / limit).toFixed(1)}% of the ${limit} kPa bond limit (an assumption until a burst test).
          ${p.swept_ul} µL in all; out of <code>${esc(p.vent)}</code> at ${mmss(p.total_s)}.</p>
      </section>
      <section class="side-section">
        <h3>Watch for</h3>
        <table class="arrivals"><thead><tr><th>Part</th><th class="num">Predicted</th><th class="num">Bench</th></tr></thead>
          <tbody>${p.sequence.map(stepRow).join('')}</tbody></table>
        <p class="meta">In order of arrival. Bold parts colour slowly enough to see and film; the rest go by too fast. Press Mark when you first see dye in one.</p>
      </section>
      <section class="side-section">
        <h3>Then</h3>
        <ul class="plain-list">
          ${p.alternatives.map((a) => `<li>Move the vent to <code>${esc(a.vent)}</code>: the dye goes on into ${list(a.extra_chambers.map((c) => `<code>${esc(c)}</code>`))} (+${Math.round(a.reach_s)} s).</li>`).join('')}
          ${p.other_inlets.length ? `<li>Repeat from ${list(p.other_inlets.map((x) => `<code>${esc(x)}</code>`))}, one at a time, for the rest of the chip.</li>` : ''}
          <li>Clear-water wash-out of <code>${esc(p.headline)}</code>: about ${Math.round(p.washout_s)} s.</li>
        </ul>
        ${p.cannot.length ? `<h3>What this run cannot show</h3><ul class="plain-list faint">${p.cannot.map((c) => `<li>${esc(calm(c))}</li>`).join('')}</ul>` : ''}
        <p class="meta">Every number is derived from the chip spec and contracts/ by tools/demo_plan.py (docs/demo.md); none is typed in.</p>
        <p style="margin-top:12px"><button type="button" data-act="open">Open this chip's own view</button></p>
      </section>`;
    lastText = '';
    tick();
  }

  function tick() {
    const t = el.querySelector('.timer .t');
    if (!t || !plan) return;
    const text = clock(elapsed);
    if (text !== lastText) { t.textContent = text; lastText = text; }
    plan.sequence.forEach((s, i) => {
      const tr = el.querySelector(`tr[data-i="${i}"]`);
      const key = `${s.kind}:${s.id}`;
      tr?.classList.toggle('due', s.filmable && elapsed >= windowOf(s)[0] * 0.9 && elapsed <= s.ends_s * 1.2 && marks[key] == null);
      tr?.classList.toggle('past', elapsed > s.ends_s);
    });
  }

  el.addEventListener('click', (e) => {
    if (!attached) return;
    const b = e.target.closest('button');
    if (!b || b.disabled) return;
    if (b.dataset.act === 'start') start();
    else if (b.dataset.act === 'pause') pause();
    else if (b.dataset.act === 'reset') reset();
    else if (b.dataset.act === 'open') onPick?.(plan.chip_id, { open: true });
    else if (b.dataset.mark) { marks[b.dataset.mark] = (performance.now() - t0) / 1000; render(); }
  });
  el.addEventListener('change', (e) => {
    if (attached && e.target.id === 'bench-chip' && e.target.value !== plan?.chip_id) onPick?.(e.target.value);
  });

  return {
    load,
    get loaded() { return plans.size > 0; },
    planFor: (id) => plans.get(id) ?? null,
    get planned() { return new Set(plans.keys()); },
    // The chip a bench demo opens on: the asked one, else Module 1, else the first in the line.
    pick(id) { return plans.has(id) ? id : plans.has(M1) ? M1 : chipsInOrder()[0] ?? null; },
    // main.js has loaded this chip (and attached Module 1's view if it is Module 1) first.
    attach(id) {
      plan = plans.get(id) ?? null;
      chip = getChip();
      attached = true;
      running = false;
      elapsed = 0;
      simT = 0;
      marks = {};
      if (plan && chip) prepareChip();
      render();
    },
    detach() {
      if (!attached) return;
      releaseChip();
      attached = false;
      running = false;
      plan = null;
      chip = null;
      el.replaceChildren();
    },
    // Every frame, before Module 1 draws: advance the dye by the wall-clock time on the timer.
    update() {
      if (!attached || !plan) return;
      if (running) {
        elapsed = (performance.now() - t0) / 1000;
        if (plan.chip_id === M1) {
          const d = Math.min(elapsed - simT, 5);
          if (d > 0) { m1.advance(d); simT += d; }
        }
      }
      if (plan.chip_id !== M1 && chip) drawRoute(elapsed);
      tick();
    },
    get attached() { return attached; },
    // for the smoke test
    get plan() { return plan; },
    get running() { return running; },
    get elapsed() { return elapsed; },
    get marks() { return marks; },
    start, pause, reset,
  };
}

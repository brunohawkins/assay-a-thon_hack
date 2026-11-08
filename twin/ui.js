// Protocol picker, play/pause/step, speed, narration panel and timeline.
import { reagentCss, tempCss, LIBRARY_COLOR, formatDuration } from './player.js';

const COMMAND_COLORS = {
  load: '#7dd3fc',
  split: '#a78bfa',
  pool: '#60a5fa',
  release: LIBRARY_COLOR,
  valve: '#f87171',
  collect: '#fde68a',
  harvest: LIBRARY_COLOR,
  encapsulate: '#2dd4bf',
  cure: '#a78bfa',
  transfer: '#fda4af',
  count: '#f0abfc',
  pl_m1: '#c4b5fd', pl_m1_out: '#fda4af', pl_m1b: '#86efac', pl_m1b_out: '#fda4af',
  pl_m2: '#2dd4bf', pl_m2_out: '#5eead4', pl_m3: '#7dd3fc', pl_roadmap: '#64748b',
};
const MODULE_NAMES = { pipeline: 'Pipeline', m1: 'Module 1 · Culture', m1b: 'Module 1b · Cell prep', m2: 'Module 2 · Encapsulation', m3: 'Module 3 · Capsule wash', other: 'Other runs' };

// "core_flow_ul_h" -> ["core flow", "µL/h"]
const UNIT_SUFFIXES = [['_per_ul', 'per µL'], ['_ul_h', 'µL/h'], ['_ul_min', 'µL/min'], ['_ul', 'µL'], ['_um', 'µm'], ['_mm', 'mm'], ['_nm', 'nm'],
  ['_deg', '°'], ['_temp_c', '°C'], ['_c', '°C'], ['_min_low', 'min (low)'], ['_min_high', 'min (high)'], ['_s', 's']];
function paramLabel(key) {
  for (const [suf, unit] of UNIT_SUFFIXES) if (key.endsWith(suf)) return [key.slice(0, -suf.length).replaceAll('_', ' '), unit];
  return [key.replaceAll('_', ' '), ''];
}

export function commandColor(ev) {
  if (ev.command === 'exchange' || ev.command === 'mix') return reagentCss(ev.args.reagent);
  if (ev.command === 'culture') return reagentCss(ev.args.reagents?.[0]);
  if (ev.command === 'hold_temp') return tempCss(ev.args.temp_c);
  return COMMAND_COLORS[ev.command] ?? '#9ca3af';
}

const $ = (id) => document.getElementById(id);

export function createUI(actions) {
  const el = {
    select: $('run-select'),
    play: $('play'),
    step: $('step'),
    speeds: [...document.querySelectorAll('[data-speed]')],
    labels: $('labels-toggle'),
    timeline: $('timeline'),
    playhead: $('playhead'),
    clock: $('clock'),
    stepLabel: $('step-label'),
    commandChip: $('command-chip'),
    narration: $('narration'),
    details: $('details'),
    runMeta: $('run-meta'),
    placeholder: $('placeholder-badge'),
    operator: $('operator-badge'),
    toast: $('toast'),
    error: $('error'),
    errorText: $('error-text'),
    views: [...document.querySelectorAll('[data-view]')],
    sizing: $('sizing'),
    sizingBody: $('sizing-body'),
    params: $('params'),
    paramsBody: $('params-body'),
    paramsMeta: $('params-meta'),
    tabs: [...document.querySelectorAll('[data-module]')],
    printBadge: $('print-badge'),
    draftBadge: $('draft-badge'),
  };
  let segments = [];
  let total = 1;
  let events = [];

  el.select.addEventListener('change', () => actions.onSelectRun(el.select.value));
  el.play.addEventListener('click', () => actions.onPlayPause());
  el.step.addEventListener('click', () => actions.onStep());
  el.speeds.forEach((b) => b.addEventListener('click', () => setSpeed(Number(b.dataset.speed))));
  el.labels.addEventListener('click', () => {
    const on = el.labels.getAttribute('aria-pressed') !== 'true';
    el.labels.setAttribute('aria-pressed', String(on));
    actions.onLabels(on);
  });
  el.tabs.forEach((b) => b.addEventListener('click', () => {
    if (b.getAttribute('aria-pressed') !== 'true') actions.onModule?.(b.dataset.module);
  }));
  el.views.forEach((b) => b.addEventListener('click', () => {
    if (b.disabled || b.getAttribute('aria-pressed') === 'true') return;
    actions.onView(b.dataset.view);
  }));
  el.timeline.addEventListener('click', (e) => {
    const r = el.timeline.getBoundingClientRect();
    actions.onSeek(((e.clientX - r.left) / r.width) * total);
  });
  window.addEventListener('keydown', (e) => {
    if (e.target instanceof HTMLSelectElement || e.metaKey || e.ctrlKey) return;
    if (e.code === 'Space') { e.preventDefault(); actions.onPlayPause(); }
    else if (e.code === 'ArrowRight') { e.preventDefault(); actions.onStep(); }
    else if (e.key === '1') setSpeed(1);
    else if (e.key === '2') setSpeed(5);
    else if (e.key === '3') setSpeed(20);
    else if (e.key === 'l' || e.key === 'L') el.labels.click();
  });

  function setSpeed(x) {
    el.speeds.forEach((b) => b.setAttribute('aria-pressed', String(Number(b.dataset.speed) === x)));
    actions.onSpeed(x);
  }

  let toastTimer = null;

  return {
    setRuns(runs, selected) {
      el.select.replaceChildren(
        ...runs.map((r) => {
          const o = document.createElement('option');
          o.value = r.file;
          const scn = r.scenario ? ` — ${r.scenario.replace(/^m3_wash_|^m1b_cellprep_/, '').replace(/_/g, ' ')}` : '';
          const variant = r.variant ? ` — ${r.variant} chip` : '';
          const op = r.operator === 'llm' ? ' · LLM operator' : r.operator === 'scripted' ? ' · scripted replay' : '';
          o.textContent = `${r.protocol_name}${variant}${scn}${op}`;
          return o;
        }),
      );
      if (selected) el.select.value = selected;
    },

    setRun(run, timeline) {
      events = timeline.map((s) => s.ev);
      total = timeline.length ? timeline[timeline.length - 1].start + timeline[timeline.length - 1].duration : 1;
      el.timeline.querySelectorAll('.seg').forEach((s) => s.remove());
      segments = timeline.map(({ ev, duration }, i) => {
        const seg = document.createElement('div');
        seg.className = 'seg';
        seg.style.flexGrow = String(duration);
        seg.style.setProperty('--seg', commandColor(ev));
        seg.title = `Step ${ev.protocol_step ?? i + 1} · ${ev.command}`;
        el.timeline.insertBefore(seg, el.playhead);
        return seg;
      });
      el.placeholder.hidden = !run.placeholder;
      el.operator.textContent = run.operator === 'llm' ? `LLM operator${run.model ? ` · ${run.model}` : ''}` : 'planner';
      el.operator.dataset.kind = run.operator;
      el.runMeta.textContent = run.kind === 'pipeline'
        ? `pipeline_mvp · ${events.length} phases · ${formatDuration(run.total_duration_s)} of protocol time`
        : `${run.protocol_id} on ${run.chip_id}${run.run_parameters?.variant ? ` (${run.run_parameters.variant})` : ''} · ${events.length} commands · ${formatDuration(run.total_duration_s)} of protocol time`;
    },

    setEvent(i, ev) {
      segments.forEach((s, k) => s.classList.toggle('active', k === i));
      if (!ev) {
        el.stepLabel.textContent = 'Ready';
        el.commandChip.hidden = true;
        el.narration.textContent = 'Press Play to run the protocol on the chip.';
        el.details.replaceChildren();
        return;
      }
      const stepNo = ev.protocol_step ?? i + 1;
      const lastStep = events.reduce((m, e, k) => Math.max(m, e.protocol_step ?? k + 1), 0);
      el.stepLabel.textContent = `Step ${stepNo} of ${lastStep}`;
      el.commandChip.hidden = false;
      el.commandChip.textContent = ev.command;
      el.commandChip.style.setProperty('--chip', commandColor(ev));
      el.narration.textContent = ev.narration || '';
      el.narration.classList.remove('enter');
      void el.narration.offsetWidth; // restart the fade-in
      el.narration.classList.add('enter');
      el.details.replaceChildren(...details(ev));
    },

    setTick({ t, total: tot, protocolT }) {
      el.playhead.style.left = `${(tot ? t / tot : 0) * 100}%`;
      el.clock.textContent = formatClock(protocolT);
    },

    setPlaying(on) {
      el.play.setAttribute('aria-pressed', String(on));
      el.play.querySelector('.label').textContent = on ? 'Pause' : 'Play';
    },

    labelsOn() { return el.labels.getAttribute('aria-pressed') === 'true'; },

    selectRun(file) { el.select.value = file; },

    setTab(module) {
      el.tabs.forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.module === module)));
      document.body.dataset.module = module;
    },

    // Badges and the parameters panel from the chip spec's module and generator blocks.
    // info: this chip's entry in protocols/pipeline_mvp.json, if it is one of the current modules.
    // md: this chip's row in protocols/pipeline_status.md (twin/status.js), which wins for the badge.
    setChipInfo(spec, info = null, md = null) {
      const m = spec?.module;
      const STATUS = { final: 'FINAL', draft: 'DRAFT', translated: 'TRANSLATED', missing: 'MISSING' };
      const MD_CLASS = { BUILT: 'st-final', 'RT ONLY': 'st-translated', 'DUPLICATE-RETIRE': 'superseded', 'OFF-CHIP': 'st-draft' };
      if (md) {
        el.printBadge.hidden = false;
        el.printBadge.textContent = info?.built_for_demo ? 'Built for the dye demo' : 'Twin only';
        el.printBadge.className = `badge ${info?.built_for_demo ? 'printed' : 'sim-only'}`;
        el.draftBadge.hidden = false;
        el.draftBadge.textContent = `${md.badge}${md.detail ? ` · ${md.detail}` : ''}`;
        el.draftBadge.className = `badge ${MD_CLASS[md.badge] ?? 'st-draft'}`;
        el.draftBadge.title = `protocols/pipeline_status.md: ${md.stage}: ${md.status}`;
      } else if (info) {
        el.printBadge.hidden = false;
        el.printBadge.textContent = info.built_for_demo ? 'Built for Sunday’s dye demo' : 'Twin only';
        el.printBadge.className = `badge ${info.built_for_demo ? 'printed' : 'sim-only'}`;
        el.draftBadge.hidden = false;
        el.draftBadge.textContent = `${STATUS[info.status] ?? info.status}${info.statusNote ? ` · ${info.statusNote}` : ''}`;
        el.draftBadge.className = `badge st-${info.status}`;
      } else if (m?.status === 'superseded') {
        el.printBadge.hidden = true;
        el.draftBadge.hidden = false;
        el.draftBadge.textContent = 'Superseded · reference only';
        el.draftBadge.className = 'badge superseded';
        el.draftBadge.title = m.note ?? `Replaced by ${m.superseded_by}`;
      } else {
        el.printBadge.hidden = !m;
        el.draftBadge.hidden = m?.status !== 'draft';
        el.draftBadge.textContent = 'Draft design';
        el.draftBadge.className = 'badge draft';
        if (m) {
          el.printBadge.textContent = m.printed === false ? 'Designed · simulated · not printed this weekend' : 'Printed';
          el.printBadge.className = `badge ${m.printed === false ? 'sim-only' : 'printed'}`;
        }
      }
      const gen = spec?.generator;
      el.params.hidden = !gen;
      if (!gen) return;
      const tag = { draft: ' (draft design)', superseded: ' (superseded)' }[m?.status] ?? '';
      el.paramsMeta.textContent = `${MODULE_NAMES[m?.id] ?? spec.id}${tag} · generated by ${gen.script}`;
      el.paramsBody.replaceChildren(...Object.entries(gen.params).flatMap(([k, v]) => {
        const [name, unit] = paramLabel(k);
        const dt = document.createElement('dt');
        dt.textContent = name;
        dt.title = k;
        const dd = document.createElement('dd');
        dd.textContent = typeof v === 'boolean' ? (v ? 'yes' : 'no') : `${typeof v === 'number' ? v.toLocaleString(undefined, { maximumFractionDigits: 3 }) : v}${unit ? ` ${unit}` : ''}`;
        return [dt, dd];
      }));
    },

    setView(mode) {
      el.views.forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.view === mode)));
    },

    // Enable the "Display model" view only when one exists for this chip.
    setDisplayAvailable(manifest, chipId) {
      const btn = el.views.find((b) => b.dataset.view === 'display');
      btn.disabled = !manifest;
      btn.title = manifest
        ? `Printed display model, ${manifest.display_scale}× scale (${manifest.overall_mm.join(' × ')} mm)`
        : `No display model for ${chipId} yet: python cad/export_display_model.py <chip spec>`;
    },

    setSizing(s) {
      el.sizing.hidden = !s;
      if (!s) return;
      const num = (v, d = 1) => Number(v).toLocaleString(undefined, { maximumFractionDigits: d });
      const rows = [];
      const add = (k, v, warn = false) => rows.push([k, v, warn]);
      add('Capsules', s.capsule_count != null ? num(s.capsule_count, 0) : `— (${s.capsule_count_note})`);
      add('Cells expected', `~${num(s.cells_expected, 0)}`);
      add('Capsule size', `${num(s.capsule_diameter_um)} µm (swollen ${num(s.swollen_diameter_um)} µm)`);
      add('Trap area', `${num(s.trap_area_mm2, 2)} mm² (monolayer needs ${num(s.monolayer_area_mm2_needed, 2)})`,
        s.trap_area_mm2 < s.monolayer_area_mm2_needed - 1e-6);
      add('Trap volume', `${num(s.trap_volume_ul, 2)} µL (packed capsules ${num(s.packed_volume_ul_needed, 2)})`,
        s.trap_volume_ul < s.packed_volume_ul_needed);
      if (s.channel_width_um) {
        const [lo, hi] = s.channel_width_um;
        const [rlo, rhi] = s.channel_width_um_recommended;
        add('Channel widths', `${num(lo)}${hi !== lo ? `–${num(hi)}` : ''} µm (capsule channels ${num(rlo)}–${num(rhi)})`, lo < rlo);
      }
      add('Trap gap', `${num(s.trap_gap_um_recommended[0])}–${num(s.trap_gap_um_recommended[1])} µm recommended`);
      if (s.barcode_collision_fraction != null) add('Barcode collisions', `${(s.barcode_collision_fraction * 100).toFixed(1)}%`, s.barcode_collision_fraction > 0.05);
      el.sizingBody.replaceChildren(...rows.flatMap(([k, v, warn]) => {
        const dt = document.createElement('dt');
        dt.textContent = k;
        const dd = document.createElement('dd');
        dd.textContent = v;
        if (warn) dd.classList.add('warn');
        return [dt, dd];
      }));
    },

    toast(msg) {
      el.toast.textContent = msg;
      el.toast.classList.add('show');
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => el.toast.classList.remove('show'), 3500);
    },

    error(msg) {
      el.errorText.textContent = msg;
      el.error.hidden = false;
    },
  };
}

function details(ev) {
  const a = ev.args;
  const rows = [];
  const add = (k, v, swatch) => {
    if (v === undefined || v === null || v === '') return;
    const dt = document.createElement('dt');
    dt.textContent = k;
    const dd = document.createElement('dd');
    if (swatch) {
      const s = document.createElement('span');
      s.className = 'swatch';
      s.style.background = swatch;
      dd.append(s);
    }
    dd.append(String(v));
    rows.push(dt, dd);
  };
  if (a.source) add('From', a.source);
  if (a.sources) add('From', a.sources.join(', '));
  if (a.target) add(a.source || a.sources ? 'To' : 'Chamber', a.target);
  if (a.targets) add('To', a.targets.join(', '));
  if (a.valves) add('Valves', `${a.valves.map((v) => v.replace(/^m\d+[a-z]?_valve_/, '')).join(', ')} → ${a.state}`);
  if (a.reagent) add('Reagent', a.reagent.replaceAll('_', ' '), reagentCss(a.reagent));
  if (a.reagents) add('Media', a.reagents.map((r) => r.replaceAll('_', ' ')).join(', '), reagentCss(a.reagents[0]));
  if (a.factors) add('Factors', a.factors.map((r) => r.replaceAll('_', ' ')).join(', '));
  if (a.exchange_interval_s) add('Medium change', `every ${formatDuration(a.exchange_interval_s)} (stop-flow between)`);
  if (a.core_flow_ul_h != null) add('Flows', `core ${a.core_flow_ul_h} · shell ${a.shell_flow_ul_h} · oil ${a.oil_flow_ul_h} µL/h`);
  if (a.capsules != null) add('Capsules', Number(a.capsules).toLocaleString());
  if (a.wavelength_nm != null) add('Light', `${a.wavelength_nm} nm`);
  if (a.dilution != null) add('Dilution', `${a.dilution}× (set by the operator from the count)`);
  if (a.waste) add('Drains to', a.waste);
  if (ev.measured?.cells_per_ul != null) add('Counted', `${ev.measured.cells_per_ul.toLocaleString()} cells/µL · ${(ev.measured.viability * 100).toFixed(1)}% viable`);
  if (a.volume_ul != null) add('Volume', `${a.volume_ul} µL${a.washes ? ` × ${a.washes}` : ''}`);
  if (a.flow_ul_min != null) add('Flow', `${a.flow_ul_min} µL/min`);
  if (a.temp_c != null) add('Temp', `${a.temp_c} °C`, tempCss(a.temp_c));
  add('Takes', formatDuration(ev.duration_s));
  return rows;
}

function formatClock(s) {
  s = Math.max(0, Math.round(s || 0));
  if (s >= 86400) return `${Math.floor(s / 86400)} d ${String(Math.floor((s % 86400) / 3600)).padStart(2, '0')} h`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = String(m).padStart(2, '0');
  const ss = String(sec).padStart(2, '0');
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

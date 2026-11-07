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
};
// Chip id -> what that chip is, for the parameters panel. The chain is
// protocols/pipeline_mvp.json / protocols/pipeline_status.md: module1_v1 (1), cell_prep_v1
// (2), spc_demo_v1 (3), module3_v1_translated (4), sctip_spc_v1 (4b, barcoding),
// module4_v1 (5), module5_v1 (6). The m1/m1b/m2/m3 names are the RETIRED drafts in cad/,
// kept because their runs still play in "Old drafts" and labelled retired so they cannot be
// read as current modules. sctip_spc_v1 is NOT retired: filing it as a duplicate of module
// 3's trap is what lost the pipeline its single-cell step. Only its stage A is retired.
const CHIP_NAMES = {
  module1_v1: 'Module 1 · culture + lane label',
  cell_prep_v1: 'Cell prep · detach, quench, count',
  spc_demo_v1: 'Module 2 · encapsulation',
  module3_v1_translated: 'Module 3 · load, lysis, wash, RT',
  sctip_spc_v1: 'Barcoding (4b) · one clonal barcode per capsule, in its own droplet',
  module4_v1: 'Module 4 · sublibrary split + index PCR',
  module5_v1: 'Module 5 · release + cleanup',
  module6_v1: 'Module 6 · three-lineage co-culture',
  ipsc_to_mgl_chip_v1: 'iPSC-to-microglia train (separate experiment, not in the chain)',
  placeholder_chip_v1: 'Placeholder chip',
  m1_culture_v1: 'Module 1 culture (retired draft)',
  m1b_cellprep_v1: 'Module 1b cell prep (retired draft)',
  m2_encapsulation_v1: 'Module 2 encapsulation (retired draft)',
  m3_wash_v1: 'Module 3 capsule wash (retired draft)',
};

// "core_flow_ul_h" -> ["core flow", "µL/h"]
const UNIT_SUFFIXES = [['_per_ul', 'per µL'], ['_ul_h', 'µL/h'], ['_ul_min', 'µL/min'], ['_ul', 'µL'], ['_um', 'µm'], ['_mm', 'mm'], ['_nm', 'nm'],
  ['_deg', '°'], ['_temp_c', '°C'], ['_c', '°C'], ['_min_low', 'min (low)'], ['_min_high', 'min (high)'], ['_s', 's']];
function paramLabel(key) {
  for (const [suf, unit] of UNIT_SUFFIXES) if (key.endsWith(suf)) return [key.slice(0, -suf.length).replaceAll('_', ' '), unit];
  return [key.replaceAll('_', ' '), ''];
}

export function commandColor(ev) {
  // Pipeline phases carry their own colour: their ids come from pipeline_mvp.json, so a table
  // here keyed by module name goes stale the moment the chain changes - which is how pl_m1,
  // pl_m1b, pl_m2 and pl_m3 outlived their modules and left the whole timeline grey.
  if (ev.color) return ev.color;
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
    hardware: $('hardware-badge'),
    cad: $('cad-badge'),
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

  // Was this run recorded against real hardware? Three states, because the flag is genuinely
  // missing from most run files and the badge used to be a hard-coded label that said
  // "SIMULATED HARDWARE" whatever it was looking at - including a real-hardware run.
  function setHardware(run) {
    const flag = run?.simulated_hardware;
    if (run?.kind === 'pipeline') {
      el.hardware.textContent = 'NO RUN: PIPELINE OVERVIEW';
      el.hardware.className = 'badge sim-unknown';
      el.hardware.title = 'The pipeline view is a drawing of protocols/pipeline_mvp.json, not a recorded run, so there is no hardware to report. Open a module to see its run.';
    } else if (flag === true) {
      el.hardware.textContent = 'SIMULATED HARDWARE';
      el.hardware.className = 'badge sim';
      el.hardware.title = 'This run says simulated_hardware: true. No physical pump, valves or sensors; closed-loop runs drive a virtual chip.';
    } else if (flag === false) {
      el.hardware.textContent = 'HARDWARE RUN';
      el.hardware.className = 'badge sim-real';
      el.hardware.title = 'This run says simulated_hardware: false, so it claims real hardware.';
    } else {
      el.hardware.textContent = 'HARDWARE NOT RECORDED';
      el.hardware.className = 'badge sim-unknown';
      el.hardware.title = `${run?.__file ?? 'This run'} carries no simulated_hardware flag, so the twin cannot say whether it was real or simulated. Neither answer is assumed.`;
    }
  }

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
      setHardware(run);
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
    setChipInfo(spec, info = null) {
      const m = spec?.module;
      const STATUS = { final: 'FINAL', draft: 'DRAFT', translated: 'TRANSLATED', missing: 'MISSING' };
      if (info) {
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
      el.paramsMeta.textContent = `${info?.name ?? CHIP_NAMES[spec.id] ?? spec.id}${tag} · generated by ${gen.script}`;
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

    // Which STLs this chip actually got, and from where. Loading another chip's parts used to
    // be invisible: the loader console.warn'd and left the wrong geometry on screen, and a
    // chip with no STLs at all looked exactly like one that had them.
    // r = { chipId, dir, loaded, missing, mismatched, failed, listed, unrelated, why, undrawn }
    setCadStatus(r) {
      const bad = [];
      if (!r.dir) bad.push(r.why ?? `No CAD folder belongs to ${r.chipId}, so every part is a box from the spec.`);
      // Say what was measured, not what caused it: the same mismatch is another chip's part
      // when x or y is wrong, and a stale export of this chip's own part when only z is.
      for (const m of r.mismatched ?? []) {
        const flat = m.at[0] === m.expected[0] && m.at[1] === m.expected[1];
        bad.push(`${m.id}.stl sits at (${m.at.join(', ')}) mm, but ${r.chipId} puts ${m.id} at (${m.expected.join(', ')}) mm, so it is not drawn. `
          + (flat
            ? 'Same x and y, wrong depth: a stale export of this chip, from before its port moved through the other face. Re-run cad/make_train_cad.py.'
            : "Wrong position in plane: that file is another chip's part, or this chip's layout has moved since it was exported."));
      }
      if (r.listed && r.failed?.length) bad.push(`${r.failed.join(', ')}: listed in ${r.dir} but could not be read.`);
      const detail = [
        r.dir ? `CAD from ${r.dir}` : 'no CAD folder for this chip',
        r.loaded?.length ? `${r.loaded.length} part${r.loaded.length > 1 ? 's' : ''} drawn from STL: ${r.loaded.join(', ')}` : 'no part drawn from STL',
        r.missing?.length ? `no STL for ${r.missing.join(', ')} (box drawn from the spec instead)` : null,
        r.mismatched?.length ? `REFUSED, wrong coordinates for this chip (box drawn from the spec instead): ${r.mismatched.map((m) => `${m.id} at (${m.at.join(', ')}) not (${m.expected.join(', ')})`).join('; ')}` : null,
        r.failed?.length ? `could not be read: ${r.failed.join(', ')}${r.listed ? '' : ' (no directory listing: these may simply not exist)'}` : null,
        r.unrelated?.length ? `${r.unrelated.join(', ')} in that folder belong to no part of this chip` : null,
        ...(r.undrawn ?? []),
        r.why ?? null,
      ].filter(Boolean).join('\n');
      const total = (r.loaded?.length ?? 0) + (r.missing?.length ?? 0) + (r.mismatched?.length ?? 0) + (r.failed?.length ?? 0);
      el.cad.hidden = false;
      el.cad.title = detail;
      if (bad.length) {
        el.cad.textContent = r.mismatched?.length ? `CAD problem: ${r.mismatched.length} part(s) at the wrong coordinates` : 'CAD: none for this chip';
        el.cad.className = 'badge warn';
        this.toast(bad[0]);
      } else if (!r.loaded?.length) {
        el.cad.textContent = 'CAD: none, boxes from the spec';
        el.cad.className = 'badge sim-only';
      } else {
        el.cad.textContent = `CAD: ${r.loaded.length}/${total} parts`;
        el.cad.className = r.loaded.length === total ? 'badge printed' : 'badge sim-only';
      }
      if (r.undrawn?.length) this.toast(r.undrawn[0]);
    },

    // Enable the "Display model" view only when one exists for this chip.
    // The manifest carries no hash or mtime of the spec it was generated from, so a stale
    // model cannot be ruled out. What CAN be checked is the footprint it recorded against the
    // spec on screen; either way the provenance gap is stated rather than hidden.
    setDisplayAvailable(manifest, chipId, spec = null) {
      const btn = el.views.find((b) => b.dataset.view === 'display');
      btn.disabled = !manifest;
      if (!manifest) {
        btn.title = `No display model for ${chipId} yet: python cad/export_display_model.py <chip spec>`;
        return;
      }
      const [fx, fy] = manifest.real_footprint_mm ?? [];
      const stale = spec && Number.isFinite(fx)
        && (Math.abs(fx - spec.footprint.x) > 1e-6 || Math.abs(fy - spec.footprint.y) > 1e-6);
      btn.title = [
        `Printed display model, ${manifest.display_scale}× scale (${manifest.overall_mm.join(' × ')} mm)`,
        stale
          ? `STALE: built for a ${fx} × ${fy} mm chip, but ${chipId} is now ${spec.footprint.x} × ${spec.footprint.y} mm. Re-run python cad/export_display_model.py ${manifest.chip_spec ?? ''}`
          : `Generated from ${manifest.chip_spec ?? 'an unrecorded spec'}. The manifest records no spec hash or date, so the twin cannot prove it is up to date: re-run cad/export_display_model.py if the spec has changed.`,
      ].join('\n');
      if (stale) {
        el.cad.hidden = false;
        el.cad.textContent = 'Display model stale';
        el.cad.className = 'badge warn';
        el.cad.title = btn.title;
        this.toast(`Display model for ${chipId} was built for a ${fx} × ${fy} mm chip, not today's ${spec.footprint.x} × ${spec.footprint.y} mm.`);
      }
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

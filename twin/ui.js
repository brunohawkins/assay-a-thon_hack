// Protocol picker, play/pause/step, speed, narration panel and timeline.
import { reagentCss, tempCss, LIBRARY_COLOR, formatDuration } from './player.js';

const COMMAND_COLORS = {
  load: '#7dd3fc',
  split: '#a78bfa',
  pool: '#60a5fa',
  release: LIBRARY_COLOR,
};

export function commandColor(ev) {
  if (ev.command === 'exchange') return reagentCss(ev.args.reagent);
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
          const scn = r.scenario ? ` — ${r.scenario.replace(/^wash_mvp_/, '').replace(/_/g, ' ')}` : '';
          const op = r.operator === 'llm' ? ' · LLM operator' : r.operator === 'scripted' ? ' · scripted replay' : '';
          o.textContent = `${r.protocol_name}${scn}${op}`;
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
      el.runMeta.textContent = `${run.protocol_id} on ${run.chip_id} · ${events.length} commands · ${formatDuration(run.total_duration_s)} of protocol time`;
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
  if (a.reagent) add('Reagent', a.reagent.replaceAll('_', ' '), reagentCss(a.reagent));
  if (a.volume_ul != null) add('Volume', `${a.volume_ul} µL${a.washes ? ` × ${a.washes}` : ''}`);
  if (a.flow_ul_min != null) add('Flow', `${a.flow_ul_min} µL/min`);
  if (a.temp_c != null) add('Temp', `${a.temp_c} °C`, tempCss(a.temp_c));
  add('Takes', formatDuration(ev.duration_s));
  return rows;
}

function formatClock(s) {
  s = Math.max(0, Math.round(s || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = String(m).padStart(2, '0');
  const ss = String(sec).padStart(2, '0');
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

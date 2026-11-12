// Control-loop panel for closed-loop runs: believed vs true, prediction vs measurement,
// the optimizer's decision table, the event log, and the alert banner. Everything is
// driven by the run file and the current protocol time, so it stays in sync with the
// 3D replay when you play, step or scrub.

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const num = (v, d = 1) => (v == null || Number.isNaN(v) ? '–' : Number(v).toLocaleString(undefined, { maximumFractionDigits: d }));

// Drawn on the light chart background (twin/style.css .chart).
const COLORS = {
  predicted: '#0969da',
  measured: '#24292f',
  truth: '#1a7f37',
  limit: '#cf222e',
  alert: '#bf8700',
  update: '#8250df',
  cursor: 'rgba(31,35,40,0.45)',
  grid: 'rgba(31,35,40,0.08)',
  title: '#424a53',
  tick: '#6e7781',
};

export function createControlPanel({ onLoopToggle }) {
  const el = {
    panel: $('control'),
    loop: $('loop-toggle'),
    state: $('cp-state'),
    gaugeFill: $('cp-gauge-fill'),
    gaugeTrue: $('cp-gauge-true'),
    gaugeAlert: $('cp-gauge-alert'),
    gaugeLabel: $('cp-gauge-label'),
    pChart: $('cp-pressure'),
    aChart: $('cp-absorbance'),
    decision: $('cp-decision'),
    decisionTitle: $('cp-decision-title'),
    log: $('cp-log'),
    banner: $('alert-banner'),
    loopMeta: $('cp-loop-meta'),
  };
  let run = null;
  let series = null;
  let limit = 5;
  let alertAt = 4.25;
  let lastKey = '';
  let counterpart = null;

  el.loop.querySelectorAll('button').forEach((b) => b.addEventListener('click', () => {
    if (b.disabled || b.getAttribute('aria-pressed') === 'true' || !counterpart) return;
    onLoopToggle(counterpart);
  }));

  function buildSeries(r) {
    const s = { t: [], pPred: [], pMeas: [], pTrue: [], aPred: [], aMeas: [], aTrue: [], slices: [], updates: [], alerts: [] };
    for (const ev of r.events) {
      for (const sl of ev.slices ?? []) {
        s.slices.push({ ...sl, ev });
        s.t.push(sl.t_s);
        s.pPred.push(sl.predicted?.pressure_kpa ?? null);
        s.pMeas.push(sl.measured?.pressure_kpa ?? null);
        s.pTrue.push(sl.true?.pressure_kpa ?? null);
        s.aPred.push(sl.predicted?.outlet_absorbance ?? null);
        s.aMeas.push(sl.measured?.outlet_absorbance ?? null);
        s.aTrue.push(sl.true?.outlet_absorbance ?? null);
      }
      for (const u of ev.model_updates ?? []) s.updates.push(u);
      for (const a of ev.alerts ?? []) s.alerts.push(a);
    }
    return s;
  }

  function setRun(r, runs) {
    run = r;
    const hasControl = Boolean(r && r.events?.some((e) => e.slices?.length));
    el.panel.hidden = !hasControl;
    document.body.classList.toggle('has-control', hasControl);
    el.banner.hidden = true;
    lastKey = '';
    if (!hasControl) {
      el.loop.hidden = true;
      series = null;
      return;
    }
    series = buildSeries(r);
    const alertEv = series.alerts.find((a) => a.evidence?.limit_kpa);
    limit = alertEv?.evidence?.limit_kpa ?? 5;
    alertAt = 0.85 * limit;
    const sensors = Object.entries(r.scenario?.sensors ?? {}).filter(([, on]) => on).map(([k]) => k);
    el.loopMeta.textContent = `${r.loop === 'closed' ? 'Closed loop' : 'Open loop'} · operator ${r.operator} · sensors: ${sensors.length ? sensors.join(', ') : 'none'}`
      + (r.scenario?.faults?.length ? ` · faults: ${r.scenario.faults.map((f) => f.type).join(', ')}` : '');
    // Open/closed counterpart: same protocol, chip and faults, other loop.
    const faults = JSON.stringify((r.scenario?.faults ?? []).map((f) => f.type).sort());
    counterpart = (runs ?? []).find((x) => x.protocol_id === r.protocol_id && x.chip_id === r.chip_id && x.operator === 'planner'
      && x.loop && x.loop !== r.loop && JSON.stringify([...(x.faults ?? [])].sort()) === faults)?.file ?? null;
    el.loop.hidden = false;
    el.loop.querySelectorAll('button').forEach((b) => {
      b.setAttribute('aria-pressed', String(b.dataset.loop === r.loop));
      b.disabled = b.dataset.loop !== r.loop && !counterpart;
      b.title = b.disabled ? 'No matching run for this scenario' : `Replay the same protocol ${b.dataset.loop} loop`;
    });
  }

  function latest(arr, t) {
    let out = null;
    for (const x of arr) if (x.t_s <= t + 1e-6) out = x;
    return out;
  }

  function update(protoT) {
    if (!series) return;
    const key = `${Math.round(protoT * 4)}`;
    if (key === lastKey) return;
    lastKey = key;
    const sl = latest(series.slices, protoT);
    renderState(sl);
    drawChart(el.pChart, protoT, {
      title: 'Pressure at the mesh (kPa)', pred: series.pPred, meas: series.pMeas, truth: series.pTrue,
      lines: [{ y: limit, color: COLORS.limit, label: 'escape' }, { y: alertAt, color: COLORS.alert, label: 'alert' }],
      yMin: Math.min(-0.5, ...series.pPred.filter((v) => v != null)), yMax: limit * 1.15,
    });
    drawChart(el.aChart, protoT, {
      title: 'Outlet absorbance (new reagent fraction)', pred: series.aPred, meas: series.aMeas, truth: series.aTrue,
      lines: [], yMin: 0, yMax: 1.05,
    });
    renderDecision(protoT);
    renderLog(protoT);
    renderBanner(protoT);
  }

  function renderState(sl) {
    const b = sl?.believed ?? {};
    const t = sl?.true_state ?? {};
    const rows = [
      ['Exchange %', b.exchange_pct, t.exchange_pct, 1],
      ['Inside capsules %', b.capsule_exchange_pct, t.capsule_exchange_pct, 1],
      ['Capsules on mesh', b.capsules_on_mesh, t.capsules_on_mesh, 0],
      ['Suspended', b.capsules_suspended, t.capsules_suspended, 0],
      ['Collected', b.capsules_collected, t.capsules_collected, 0],
      ['Lost to waste', b.capsules_lost, t.capsules_lost, 0],
      ['Pressure (kPa)', b.pressure_kpa, t.pressure_kpa, 2],
      ['Waste (µL)', b.waste_ul, t.waste_ul, 1],
    ];
    el.state.replaceChildren(...rows.map(([k, bv, tv, d]) => {
      const tr = document.createElement('tr');
      const off = bv != null && tv != null && Math.abs(bv - tv) > Math.max(1, 0.05 * Math.abs(tv));
      tr.innerHTML = `<th>${k}</th><td>${num(bv, d)}</td><td class="${off ? 'off' : ''}">${num(tv, d)}</td>`;
      return tr;
    }));
    const span = limit * 1.2;
    const pb = Math.max(0, b.pressure_kpa ?? 0);
    const pt = Math.max(0, t.pressure_kpa ?? 0);
    el.gaugeFill.style.width = `${Math.min(100, (pb / span) * 100)}%`;
    el.gaugeFill.classList.toggle('hot', pb >= alertAt);
    el.gaugeTrue.style.left = `${Math.min(100, (pt / span) * 100)}%`;
    el.gaugeAlert.style.left = `${(alertAt / span) * 100}%`;
    el.gaugeLabel.textContent = `believed ${num(b.pressure_kpa, 2)} · true ${num(t.pressure_kpa, 2)} · alert ${num(alertAt, 2)} · escape ${num(limit, 1)} kPa`;
  }

  function drawChart(canvas, protoT, cfg) {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (!w || !h) return;
    if (canvas.width !== Math.round(w * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); }
    const g = canvas.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    const pad = { l: 34, r: 8, t: 16, b: 16 };
    const tMax = Math.max(run.total_duration_s || 1, 1);
    const X = (t) => pad.l + (t / tMax) * (w - pad.l - pad.r);
    const Y = (v) => pad.t + (1 - (v - cfg.yMin) / (cfg.yMax - cfg.yMin)) * (h - pad.t - pad.b);
    g.font = '10px ui-monospace, Menlo, monospace';
    g.fillStyle = COLORS.title;
    g.fillText(cfg.title, pad.l, 11);
    // grid + y labels
    g.strokeStyle = COLORS.grid;
    g.lineWidth = 1;
    const ticks = 4;
    for (let i = 0; i <= ticks; i++) {
      const v = cfg.yMin + ((cfg.yMax - cfg.yMin) * i) / ticks;
      g.beginPath(); g.moveTo(pad.l, Y(v)); g.lineTo(w - pad.r, Y(v)); g.stroke();
      g.fillStyle = COLORS.tick;
      g.fillText(v.toFixed(Math.abs(cfg.yMax) < 2 ? 1 : 0), 2, Y(v) + 3);
    }
    for (const line of cfg.lines) {
      g.strokeStyle = line.color; g.setLineDash([4, 3]);
      g.beginPath(); g.moveTo(pad.l, Y(line.y)); g.lineTo(w - pad.r, Y(line.y)); g.stroke();
      g.setLineDash([]); g.fillStyle = line.color; g.fillText(line.label, w - pad.r - 34, Y(line.y) - 2);
    }
    // model updates and alerts (revealed as the run plays)
    for (const u of series.updates) {
      if (u.t_s > protoT) continue;
      g.strokeStyle = COLORS.update; g.setLineDash([2, 3]);
      g.beginPath(); g.moveTo(X(u.t_s), pad.t); g.lineTo(X(u.t_s), h - pad.b); g.stroke(); g.setLineDash([]);
    }
    for (const a of series.alerts) {
      if (a.t_s > protoT) continue;
      g.fillStyle = COLORS.limit;
      g.beginPath(); g.moveTo(X(a.t_s), pad.t); g.lineTo(X(a.t_s) - 4, pad.t - 6); g.lineTo(X(a.t_s) + 4, pad.t - 6); g.fill();
    }
    const plot = (vals, color, style) => {
      g.strokeStyle = color; g.fillStyle = color;
      g.setLineDash(style === 'dash' ? [5, 3] : style === 'dot' ? [1, 3] : []);
      g.lineWidth = style === 'dot' ? 1.2 : 1.6;
      let started = false;
      g.beginPath();
      for (let i = 0; i < series.t.length; i++) {
        const v = vals[i];
        if (series.t[i] > protoT + 1e-6) break;
        if (v == null) { started = false; continue; }
        if (style === 'points') { g.fillRect(X(series.t[i]) - 1.5, Y(v) - 1.5, 3, 3); continue; }
        if (!started) { g.moveTo(X(series.t[i]), Y(v)); started = true; } else g.lineTo(X(series.t[i]), Y(v));
      }
      if (style !== 'points') g.stroke();
      g.setLineDash([]);
    };
    plot(cfg.truth, COLORS.truth, 'dot');
    plot(cfg.pred, COLORS.predicted, 'dash');
    plot(cfg.meas, COLORS.measured, 'points');
    g.strokeStyle = COLORS.cursor;
    g.beginPath(); g.moveTo(X(protoT), pad.t); g.lineTo(X(protoT), h - pad.b); g.stroke();
  }

  function renderDecision(protoT) {
    const plans = run.log.filter((x) => (x.kind === 'plan' || x.kind === 'replan') && x.data?.plan && x.t_s <= protoT + 1e-6);
    const entry = plans[plans.length - 1];
    if (!entry) {
      el.decisionTitle.textContent = 'Optimizer decision';
      el.decision.innerHTML = '<p class="cp-empty">No plan yet.</p>';
      return;
    }
    const plan = entry.data.plan;
    el.decisionTitle.textContent = `Optimizer: step ${plan.step} · ${plan.goal?.kind ?? ''}${entry.kind === 'replan' ? ' (re-plan)' : ''}`;
    const rows = plan.candidates.map((c) => {
      const what = c.command === 'hold_temp' ? `${c.ramp_c_per_s} °C/s`
        : `${c.washes ? `${c.washes}×` : ''}${num(c.volume_ul, 1)} µL @ ${num(c.flow_ul_min, 0)}`;
      const cls = c.status === 'chosen' ? 'chosen' : c.status === 'rejected' ? 'rejected' : '';
      return `<tr class="${cls}"><td>${esc(what)}</td><td>${num(c.time_s, 0)} s</td><td>${num(c.final_pct, 1)}</td>`
        + `<td>${num(c.peak_kpa, 2)}</td><td>${c.status === 'chosen' ? '✔ chosen' : esc(c.reason ?? '')}</td></tr>`;
    }).join('');
    el.decision.innerHTML = `<table><thead><tr><th>Option</th><th>Time</th><th>Exch %</th><th>Peak kPa</th><th>Verdict</th></tr></thead>`
      + `<tbody>${rows}</tbody></table>`;
  }

  function renderLog(protoT) {
    const items = run.log.filter((x) => x.t_s <= protoT + 1e-6).slice(-40).reverse();
    el.log.replaceChildren(...items.map((x) => {
      const li = document.createElement('li');
      let tag = x.kind;
      let cls = x.kind;
      if (x.kind === 'safety') {
        tag = x.data?.safety?.result ?? 'safety';
        cls = `safety-${tag}`;
      }
      const expl = x.data?.explanation ? `<div class="why">${esc(x.data.explanation)}</div>` : '';
      li.innerHTML = `<span class="t">${num(x.t_s, 0)} s</span><span class="tag ${cls}">${esc(tag.replace('_', ' '))}</span>`
        + `<span class="msg">${esc(x.message)}</span>${expl}`;
      return li;
    }));
  }

  function renderBanner(protoT) {
    const alerts = run.log.filter((x) => x.kind === 'alert' && x.t_s <= protoT + 1e-6);
    const last = alerts[alerts.length - 1];
    if (!last || protoT - last.t_s > 40) { el.banner.hidden = true; return; }
    const a = last.data.alert;
    const after = run.log.filter((x) => x.t_s >= last.t_s && x.t_s <= protoT + 1e-6 && x !== last
      && ['plan', 'safety', 'replan', 'pause', 'stop', 'model_update'].includes(x.kind));
    const steps = after.slice(0, 4).map((x) => {
      if (x.kind === 'plan') return x.message.startsWith('recovery') ? 'back-flush proposed' : 'plan';
      if (x.kind === 'safety') return `safety ${x.data?.safety?.result}`;
      if (x.kind === 'replan') return 're-planned';
      if (x.kind === 'model_update') return x.message.includes('distrusting') ? 'sensor distrusted' : 'model updated';
      return x.kind === 'pause' ? 'paused for a human' : 'stopped';
    });
    const resolved = after.some((x) => x.kind === 'replan' || (x.kind === 'safety' && after.indexOf(x) > 0)
      || x.message.includes('distrusting'));
    el.banner.hidden = false;
    el.banner.classList.toggle('resolved', resolved);
    const ev = Object.entries(a.evidence ?? {}).filter(([k]) => !['readings', 'predicted'].includes(k))
      .map(([k, v]) => `${k.replace(/_/g, ' ')} ${typeof v === 'number' ? num(v, 2) : v}`).join(' · ');
    el.banner.innerHTML = `<strong>${esc(a.name.replace(/_/g, ' '))}</strong> <span>${esc(a.message)}</span>`
      + (ev ? `<small>${esc(ev)}</small>` : '')
      + (steps.length ? `<em>→ ${esc(steps.join(' → '))}${resolved ? ' → recovering' : ''}</em>` : '');
  }

  return { setRun, update };
}

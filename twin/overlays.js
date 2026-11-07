// 2D overlays on top of the 3D chip view:
//  - the day counter (Module 1's culture runs over days; the timeline is compressed)
//  - the nozzle close-up (Module 2): droplets are 70 um on a 75 mm chip, a pixel at real
//    scale, so the co-flow, the pinch-off and the ring forming in each droplet are drawn here.
// Everything is drawn from the playback progress, so seeking and stepping stay exact.

const $ = (id) => document.getElementById(id);

export function createOverlays() {
  const day = $('day-counter');
  const inset = $('nozzle-inset');
  const canvas = inset.querySelector('canvas');
  const caption = inset.querySelector('.meta');
  const g = canvas.getContext('2d');
  let lastDay = null;

  function setDay(protoT, totalT) {
    if (protoT == null) {
      day.hidden = true;
      lastDay = null;
      return;
    }
    const d = Math.floor(protoT / 86400);
    const key = `${d}/${Math.round(totalT / 86400)}`;
    if (key === lastDay) return;
    lastDay = key;
    day.hidden = false;
    day.innerHTML = `<span class="d">Day ${d}</span><span class="of">of ${Math.round(totalT / 86400)} · compressed</span>`;
  }

  function setNozzle(info) {
    if (!info) {
      inset.hidden = true;
      return;
    }
    inset.hidden = false;
    inset.style.opacity = String(info.fade ?? 1);
    const a = info.args;
    const text = a ? `Core ${a.core_flow_ul_h} · shell ${a.shell_flow_ul_h} · oil ${a.oil_flow_ul_h} µL/h · ${Number(a.capsules).toLocaleString()} capsules. Not to scale with the chip.` : '';
    if (caption.textContent !== text) caption.textContent = text;
    draw(g, canvas, info.p, info.args);
  }

  return { setDay, setNozzle };
}

// ------------------------------------------------------------------ drawing

const C = { core: '#fda4af', shell: '#2dd4bf', oil: '#fbbf24', wall: 'rgba(188, 215, 255, 0.55)', text: '#cbd5e1', cell: '#7f1d1d' };

function rand(seed) {
  const x = Math.sin(seed * 12.9898) * 43758.5453;
  return x - Math.floor(x);
}

function draw(g, canvas, p, args) {
  const W = canvas.width;
  const H = canvas.height;
  const cy = H * 0.52;
  const yj = W * 0.24;           // Y-junction
  const nz = W * 0.46;           // nozzle
  const ch = H * 0.16;           // co-flow channel half-height
  const nh = H * 0.05;           // nozzle half-gap
  g.clearRect(0, 0, W, H);
  g.lineWidth = 2;
  g.lineCap = 'round';

  // Inlet arms: core from the upper left, shell from the lower left.
  const arm = (y0, color) => {
    g.strokeStyle = color;
    g.lineWidth = ch * 0.9;
    g.globalAlpha = 0.8;
    g.beginPath();
    g.moveTo(W * 0.04, y0);
    g.lineTo(yj, cy + (y0 < cy ? -ch / 2 : ch / 2));
    g.stroke();
    g.globalAlpha = 1;
  };
  arm(cy - H * 0.33, C.core);
  arm(cy + H * 0.33, C.shell);

  // Co-flow: core and shell side by side, not mixing.
  g.fillStyle = C.core;
  g.fillRect(yj, cy - ch, nz - yj, ch);
  g.fillStyle = C.shell;
  g.fillRect(yj, cy, nz - yj, ch);
  // Oil from both sides at the nozzle.
  g.fillStyle = C.oil;
  g.globalAlpha = 0.85;
  g.fillRect(nz - W * 0.025, 0.06 * H, W * 0.05, cy - nh - 0.06 * H);
  g.fillRect(nz - W * 0.025, cy + nh, W * 0.05, H * 0.94 - cy - nh);
  // Oil carries the droplets down the phase-separation channel.
  g.fillRect(nz + W * 0.025, cy - ch * 1.25, W * 0.94 - nz - W * 0.025, ch * 2.5);
  g.globalAlpha = 1;
  g.strokeStyle = C.wall;
  g.lineWidth = 1.5;
  g.strokeRect(yj, cy - ch, nz - yj, 2 * ch);
  g.strokeRect(nz + W * 0.025, cy - ch * 1.25, W * 0.94 - nz - W * 0.025, ch * 2.5);

  // Droplets: one pinches off every 1/rate of the playback; each moves right and its
  // GelMA shell separates from the dextran core into a ring as it goes.
  const n = 4;                                // droplets in the channel at once
  const R = ch * 0.78;
  const x0 = nz + W * 0.03;
  const x1 = W * 0.95 + R;
  const phase = p * n * 3;
  for (let k = Math.floor(phase) - n; k <= Math.floor(phase); k++) {
    const age = (phase - k) / n;              // 0 at the nozzle -> 1 at the far end
    if (age < 0 || age > 1) continue;
    const grow = Math.min(1, age * 9);        // the pinch-off: the droplet grows out of the nozzle
    const x = x0 + age * (x1 - x0);
    const r = R * (0.35 + 0.65 * grow);
    const sep = Math.min(1, Math.max(0, (age - 0.08) / 0.55)); // phase separation progress
    g.save();
    g.beginPath();
    g.arc(x, cy, r, 0, Math.PI * 2);
    g.clip();
    if (sep < 1) {                            // still side by side: core on top, shell below
      g.globalAlpha = 1 - sep;
      g.fillStyle = C.core;
      g.fillRect(x - r, cy - r, 2 * r, r);
      g.fillStyle = C.shell;
      g.fillRect(x - r, cy, 2 * r, r);
    }
    g.globalAlpha = sep;
    g.fillStyle = C.shell;                    // the ring: GelMA shell outside...
    g.fillRect(x - r, cy - r, 2 * r, 2 * r);
    g.fillStyle = C.core;                     // ...dextran core inside
    g.beginPath();
    g.arc(x, cy, r * 0.62, 0, Math.PI * 2);
    g.fill();
    g.globalAlpha = 1;
    g.restore();
    g.strokeStyle = 'rgba(255,255,255,0.75)';
    g.lineWidth = 1.2;
    g.beginPath();
    g.arc(x, cy, r, 0, Math.PI * 2);
    g.stroke();
    if (rand(k * 7.13) < 0.12) {              // ~0.1 cells per capsule: most are empty
      g.fillStyle = C.cell;
      g.beginPath();
      g.arc(x + r * 0.12, cy - r * 0.1, Math.max(2, r * 0.2), 0, Math.PI * 2);
      g.fill();
    }
  }

  // Labels.
  g.fillStyle = C.text;
  g.font = `${Math.round(H * 0.07)}px ui-sans-serif, system-ui, sans-serif`;
  g.fillText('core: cells in dextran', W * 0.02, cy - H * 0.38);
  g.fillText('shell: GelMA', W * 0.02, cy + H * 0.45);
  g.fillText('oil', nz + W * 0.035, H * 0.12);
  g.fillText('Y-junction', yj - W * 0.05, cy + ch + H * 0.11);
  g.fillText('nozzle', nz + W * 0.035, H * 0.96);
  g.font = `${Math.round(H * 0.06)}px ui-sans-serif, system-ui, sans-serif`;
  g.fillText('phase separation: core + shell ring', nz + W * 0.06, cy - ch * 1.25 - H * 0.04);
}

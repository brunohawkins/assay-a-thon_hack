// Presenter view: twin/?present opens straight into a guided sequence for a projector, with big
// Next / Back buttons (a clicker's PageDown / PageUp, the arrow keys and Space work too).
//
// Made to survive a live demo:
//   - every step sets the twin up from scratch, so Back, Next, skipping and fast clicking never
//     leave it half-way; a newer click cancels the step that is still setting up;
//   - nothing goes to the network: three.js is vendored (twin/vendor/), the AI operator is the
//     scripted one, and every file comes from this repo;
//   - debug panels are hidden, the "simulation" labelling stays on screen, and the twin's own
//     keyboard shortcuts are switched off;
//   - an error in one step is shown quietly and the buttons keep working.
// #step=N in the URL opens step N; tools/record_presenter.py reads `seconds` to pace the video.
import { modeStates } from './network.js';
import { notPrintable, shortStage } from './status.js';

let FLOW = 2.4; // µL/min: Module 1's feed, derived from the chip by the twin (operator/network.py DEFAULT_FEED_UL_MIN)
const NAMES = { n_cx: 'neuron', g_cx: 'glia', a_cx: 'astrocyte' };
const CHIP_OF = { m1v1: 'module1_v1', m4: 'module4_v1', m5: 'module5_v1', prompt: 'capsule_prep_v1' };
const PROMPT = 'Turn steps 1–15 of the cell prep and encapsulation table into a chip, built from Flowmatter library parts.';
const STATE_WORD = { CAP: 'capped', VENT: 'open', FEED: 'fed' };

// The refusal step's request: the scripted operator's blocked decision (runs/module1_v1/operator_scripted.json):
// during separate growth, cap p_mid and vent p_g_out instead.
const unsafe = () => ({
  ask: 'Cap p_mid and vent p_g_out, to flush the glia chamber faster.',
  mode: 'grow',
  states: { p_in: 'FEED', p_g_in: 'FEED', p_a_in: 'FEED', p_mid: 'CAP', p_g_out: 'VENT', p_a_out: 'CAP' },
  flows: { p_in: FLOW, p_g_in: FLOW, p_a_in: FLOW },
});

const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const list = (xs) => (xs.length < 2 ? xs.join('') : `${xs.slice(0, -1).join(', ')} and ${xs.at(-1)}`);
const CANCELLED = Symbol('cancelled');

// One sentence, from the safety check's own result: which chamber would receive whose medium.
export function refusalSentence(request, verdict) {
  const foreign = verdict.sol?.routing?.foreign_medium ?? {};
  const victims = Object.entries(foreign).map(([c, src]) => `the ${NAMES[c] ?? c} chamber would get `
    + list(Object.entries(src).map(([k, x]) => `${Math.round(x * 100)}% ${NAMES[k] ?? k} medium`)));
  if (victims.length) return `No: ${list(victims)}, mixing cultures that separate growth keeps apart.`;
  return `No: ${verdict.reasons?.[0] ?? 'the safety check blocks it'}.`;
}

function makeSteps(api) {
  const { pipeline, player, m1 } = api;

  // Module 1 in a given mode at FLOW, optionally starting from another mode's steady colours.
  async function module1(s, mode, { from = null, speed = 60 } = {}) {
    await s.show('m1v1');
    FLOW = m1.defaultFeed ?? FLOW;
    player.setPlaying(false);
    m1.flag(null);
    m1.setMode(from ?? mode, FLOW);
    m1.reset();
    if (from) { m1.snapSteady(); m1.setMode(mode, FLOW); }
    m1.setSpeed(speed);
    api.frame();
  }
  const stepRange = (xs) => (xs.length > 1 && xs.at(-1) - xs[0] === xs.length - 1 ? `${xs[0]}–${xs.at(-1)}` : xs.join(', '));
  const typeOut = async (s, text) => {
    for (let i = 1; i <= text.length; i += 2) { s.caption(`“${text.slice(0, i)}”`); await s.wait(22); }
    s.caption(`“${text}”`);
  };
  const chipsIn = () => (pipeline.data?.entries ?? []).filter((e) => e.spec).length;

  return [
    {
      id: 'prompt', seconds: 13,
      title: 'From a prompt to a chip',
      caption: 'Typed prompt → library parts → chip spec → checked → drawn.',
      async enter(s) {
        const [design, check] = await Promise.all(['design.json', 'check.json'].map(async (f) => (await fetch(api.promptDir + f, { cache: 'no-store' })).json()));
        s.check();
        await s.show('prompt');
        api.chip.group.visible = false;            // it appears once the check has passed
        api.chip.setLabelsVisible(false);
        player.setPlaying(false);
        await typeOut(s, PROMPT);
        await s.wait(700);
        const verdict = check.passes ? `our checker passes it (${check.errors.length} errors)` : `our checker finds ${check.errors.length + check.schema_errors.length} errors`;
        s.caption(`${design.modules.length} Flowmatter parts (steps ${stepRange(design.modules.flatMap((m) => m.steps).sort((a, b) => a - b).filter((x, i, a) => a.indexOf(x) === i))}) → chip spec → ${verdict}. Cached run of Yuvraj’s tool.`);
        await s.wait(500);
        api.chip.group.visible = true;
        api.frame();
        player.seek(0);
        player.setPlaying(true);
      },
    },
    {
      id: 'pipeline', seconds: 9,
      title: 'The whole pipeline, at real scale',
      caption: '',
      async enter(s) {
        await s.show('pipeline');
        const entries = pipeline.data?.entries ?? [];
        const ghosts = entries.some((e) => !e.spec);
        const bought = entries.filter(notPrintable).map((e) => shortStage(pipeline.data?.status?.byChip.get(e.spec?.id)?.stage ?? e.name));
        s.caption(`${chipsIn()} stages at real scale, 30 × 20 mm each: cells go in, capsules in the middle, a library out.`
          + `${bought.length ? ` ${bought.join(', ')} runs our chemistry on bought hardware.` : ''}${ghosts ? ' Dashed: not designed yet.' : ''}`);
        pipeline.setSpeed(1.6);
        pipeline.seek(0);
        pipeline.setPlaying(true);
      },
    },
    {
      id: 'grow', seconds: 10,
      title: 'Module 1 · separate growth',
      caption: 'Each lineage gets its own medium (red neurons, yellow glia, blue astrocytes); all of it leaves through p_mid.',
      async enter(s) { await module1(s, 'grow', { speed: 20 }); },
    },
    {
      id: 'interact', seconds: 10,
      title: 'Module 1 · interaction',
      caption: 'Same chip, no valves: only the neuron inlet is fed, and its red medium flows into the other two chambers.',
      async enter(s) { await module1(s, 'int_n', { from: 'grow', speed: 30 }); },
    },
    {
      id: 'refuse', seconds: 11,
      title: 'The AI operator refuses an unsafe request',
      caption: '',
      async enter(s) {
        await module1(s, 'grow', { from: 'grow', speed: 10 });
        const UNSAFE = unsafe();
        await typeOut(s, UNSAFE.ask);
        await s.wait(800);
        const v = m1.check(UNSAFE.mode, UNSAFE.states, UNSAFE.flows);
        if (v.status === 'blocked') {
          s.caption(`AI operator: “${refusalSentence(UNSAFE, v)}” Safety check: blocked, ports unchanged.`, 'blocked');
          const base = modeStates(UNSAFE.mode, FLOW).states;
          m1.flag([...Object.keys(v.sol?.routing?.foreign_medium ?? {}), ...Object.keys(UNSAFE.states).filter((p) => base[p] !== UNSAFE.states[p])]);
        } else {
          s.caption(`The safety check says ${v.status}: ${[...(v.reasons ?? []), ...(v.warnings ?? [])].join('; ') || 'no objection'}.`);
        }
      },
    },
    {
      id: 'harvest', seconds: 14,
      title: 'Harvest, and on down the line',
      caption: 'All three chambers flush out through p_mid, on to cell prep.',
      async enter(s) {
        await module1(s, 'collect', { from: 'grow', speed: 20 });
        await s.wait(5500);
        s.caption('In the pipeline the cells move on: cell prep, then encapsulation into capsules.');
        await s.show('pipeline');
        const tl = pipeline.timeline();
        const i0 = tl.findIndex((x) => /^pl_module1_to_/.test(x.ev.id ?? x.ev.command));
        if (i0 >= 0) { pipeline.setSpeed(1.6); pipeline.playRange(i0, Math.min(i0 + 3, tl.length - 1)); }
      },
    },
    {
      id: 'm4', seconds: 12,
      title: 'Module 4 · split four ways, index PCR',
      caption: 'A rotary valve fills the four chambers A B C D D C B A; then the whole chip is thermocycled.',
      async enter(s) {
        await s.show('m4');
        const tl = player.timeline();
        const end = tl.findIndex((x) => /cycle 3 extend/.test(x.ev.narration ?? ''));
        api.frame();
        player.seek(0);
        player.setSpeed(10);
        player.setPlaying(true);
        player.stopAt = end >= 0 ? tl[end].start + tl[end].duration : null;
      },
    },
    {
      id: 'm5', seconds: 9,
      title: 'Module 5 · release and clean-up',
      caption: 'Collagenase frees the DNA at 37 °C; magnetic beads catch it; one sublibrary per pass.',
      async enter(s) {
        await s.show('m5');
        const tl = player.timeline();
        const next = tl.findIndex((x) => /pass 2/.test(x.ev.narration ?? ''));
        api.frame();
        player.seek(0);
        player.setSpeed(3);
        player.setPlaying(true);
        player.stopAt = next > 0 ? tl[next].start : null;
      },
    },
  ];
}

export function createPresenter(api, { extraSteps = [] } = {}) {
  const steps = [...extraSteps.map((f) => f(api)), ...makeSteps(api)];
  let index = -1;
  let pending = null;   // the step the latest click asked for
  let running = false;
  let ready = false;

  // ---------------------------------------------------------------- DOM
  const root = document.createElement('div');
  root.className = 'pr-root';
  root.innerHTML = `
    <section class="pr-caption panel" aria-live="polite">
      <div class="pr-main">
        <p class="pr-head"><span class="pr-count"></span><span class="pr-sim">Simulation: the digital twin, not live hardware</span></p>
        <h2 class="pr-title"></h2>
        <p class="pr-text"></p>
      </div>
      <nav class="pr-nav" aria-label="Presenter">
        <button class="pr-back" type="button">← Back</button>
        <button class="pr-next primary" type="button">Next →</button>
        <ol class="pr-dots">${steps.map((st, i) => `<li data-i="${i}" title="${esc(st.title)}"></li>`).join('')}</ol>
      </nav>
    </section>
    <div class="pr-loading">Loading the twin…</div>`;
  document.body.append(root);
  const el = {
    caption: root.querySelector('.pr-caption'), count: root.querySelector('.pr-count'),
    title: root.querySelector('.pr-title'), text: root.querySelector('.pr-text'),
    back: root.querySelector('.pr-back'), next: root.querySelector('.pr-next'), dots: [...root.querySelectorAll('.pr-dots li')],
    loading: root.querySelector('.pr-loading'),
  };
  new ResizeObserver(() => {
    document.documentElement.style.setProperty('--pr-caption-h', `${Math.ceil(el.caption.getBoundingClientRect().height)}px`);
    api.frame();
  }).observe(el.caption);

  el.back.addEventListener('click', () => go(index - 1));
  el.next.addEventListener('click', () => go(index + 1));
  root.querySelector('.pr-dots').addEventListener('click', (e) => { const li = e.target.closest('li'); if (li) go(Number(li.dataset.i)); });

  // The twin's own shortcuts (Space = play, L = labels, 1-3 = speed) are off here: only
  // navigation keys work, so a stray key press on stage can't change anything.
  window.addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey || /^F\d+$/.test(e.key)) return; // browser shortcuts, F11 full screen
    e.stopImmediatePropagation();
    const k = e.key;
    if (['ArrowRight', 'PageDown', ' ', 'Enter', 'ArrowDown'].includes(k)) { e.preventDefault(); go(index + 1); }
    else if (['ArrowLeft', 'PageUp', 'Backspace', 'ArrowUp'].includes(k)) { e.preventDefault(); go(index - 1); }
    else if (k === 'Home') { e.preventDefault(); go(0); }
    else if (k === 'End') { e.preventDefault(); go(steps.length - 1); }
    else if (k === 'f' || k === 'F') { e.preventDefault(); toggleFullscreen(); }
    else e.preventDefault();
  }, true);

  // The cursor hides after a moment without movement (projector).
  let idleTimer = null;
  window.addEventListener('pointermove', () => {
    document.body.classList.remove('pr-idle');
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => document.body.classList.add('pr-idle'), 2500);
  });

  function toggleFullscreen() {
    try {
      if (document.fullscreenElement) document.exitFullscreen();
      else document.documentElement.requestFullscreen?.();
    } catch { /* not allowed: ignore */ }
  }

  // ---------------------------------------------------------------- steps

  function render(i) {
    const st = steps[i];
    el.count.textContent = `${i + 1} / ${steps.length}`;
    el.title.textContent = st.title;
    el.text.textContent = st.caption ?? '';
    el.text.className = 'pr-text';
    el.back.disabled = i === 0;
    el.next.disabled = i === steps.length - 1;
    el.dots.forEach((d, k) => { d.classList.toggle('done', k < i); d.classList.toggle('now', k === i); });
  }

  // The context a step's enter() gets. Every wait checks whether a newer click arrived; if so it
  // throws CANCELLED and the newer step takes over from scratch.
  function context() {
    const check = () => { if (pending !== null) throw CANCELLED; };
    const wait = async (ms) => {
      const end = performance.now() + ms;
      while (performance.now() < end) {
        check();
        await new Promise((r) => setTimeout(r, Math.min(50, end - performance.now())));
      }
      check();
    };
    const until = async (cond, ms = 20000) => {
      const end = performance.now() + ms;
      while (!cond()) {
        if (performance.now() > end) throw new Error('the view did not load in time');
        await wait(40);
      }
    };
    return {
      wait,
      until,
      check,
      async show(m) {
        if (m === 'prompt') await api.showModule('other', api.promptFile);
        else await api.showModule(m);
        check();
        if (m === 'pipeline') await until(() => api.pipeline.visible && api.pipeline.total > 1);
        else if (m === 'prompt') await until(() => api.chip?.spec?.id === CHIP_OF.prompt && api.player.run?.chip_id === CHIP_OF.prompt);
        else if (CHIP_OF[m]) {
          await until(() => api.chip?.spec?.id === CHIP_OF[m] && api.player.run?.chip_id === CHIP_OF[m] && (m !== 'm1v1' || api.m1.attached));
        } else await until(() => api.player.run);
        check();
      },
      caption(text, tone = '') { el.text.textContent = text; el.text.className = `pr-text ${tone}`; },
    };
  }

  async function go(i) {
    if (!ready) return;
    const target = Math.max(0, Math.min(steps.length - 1, i));
    if (target === index && !running) return;
    pending = target;
    if (running) return; // the running step notices `pending` at its next wait and stops
    running = true;
    document.body.classList.add('pr-busy');
    while (pending !== null) {
      const k = pending;
      pending = null;
      index = k;
      render(k);
      try {
        api.m1.flag?.(null);
        await steps[k].enter(context());
        history.replaceState(null, '', `${location.pathname}${location.search}#step=${k + 1}`);
      } catch (e) {
        if (e !== CANCELLED) {
          console.warn('presenter step', steps[k].id, e);
          el.text.textContent = `This step could not load (${e.message ?? e}). Next and Back still work.`;
        }
      }
    }
    running = false;
    document.body.classList.remove('pr-busy');
  }

  async function start() {
    const m = /step=(\d+)/.exec(location.hash);
    try { await api.preload?.(); } catch (e) { console.warn('presenter preload', e); }
    ready = true;
    el.loading.hidden = true;
    await go(m ? Number(m[1]) - 1 : 0);
  }

  return {
    start,
    go,
    next: () => go(index + 1),
    back: () => go(index - 1),
    get steps() { return steps.map(({ id, title, seconds }) => ({ id, title, seconds })); },
    get caption() { return el.text.textContent; },
    get index() { return index; },
    get busy() { return running; },
    get ready() { return ready; },
  };
}

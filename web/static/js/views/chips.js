// View 4: Chips. Plan-view SVG, a 3D view of the placed STLs, the fabrication manifest
// and the download links for STL, mould, DXF and the Fusion script.

import { api, ApiError, explain, download } from '../api.js';
import { el, clear, setChildren, badge, toast, fmtNum, fmtBytes, emptyState, errorState, loading, pick } from '../dom.js';
import { demoBadge, isDemoScale, footprintLabel, specFlags, simBadge } from '../labels.js';
import { chipPlan } from '../chipsvg.js';
import { StlViewer, PALETTE, bboxSize } from '../stl.js';
import { renderJson } from '../jsonview.js';
import { loadArtifacts, exportsByChip, figuresFor } from '../locate.js';

const FAB_FLOORS = {
  feature_mm: 0.2,
  wall_mm: 0.4,
  port_mm: 0.8,
  port_pitch_mm: 5,
  depth_mm: 0.3,
};

export function mount(host, ctx) {
  let viewer = null;
  let cancelled = false;
  const state = { chips: [], chipId: ctx.param || null, spec: null, artifacts: [], tab: 'plan' };

  const sidebar = el('section', { class: 'panel' }, el('h3', { style: { marginTop: 0 } }, 'Chips'), loading('Listing chips'));
  const detail = el('div', {});
  setChildren(host,
    el('div', { class: 'view-head' },
      el('h2', 'Chips'),
      el('span', { class: 'sub' }, 'one fluidic layer, one etch depth - the plan view is the design'),
      el('span', { class: 'spacer' }),
      ctx.fileRunId
        ? el('span', { class: 'small muted', title: 'Files are served through /api/runs/<id>/file; chip/, protocols/ and cad/figures/ are open to every run.' },
          `files read through run ${ctx.fileRunId}`)
        : el('span', { class: 'small' }, badge('no run: files unavailable', 'warn', 'The API serves files only through a run, so figures, STLs and downloads need at least one run to exist.'))),
    el('div', { class: 'cols c-side' }, sidebar, detail));

  (async () => {
    const [chipsRes, arts] = await Promise.all([
      api.chips().then((r) => (Array.isArray(r) ? r : (r && r.chips) || [])).catch((e) => e),
      ctx.runId ? loadArtifacts(ctx.runId).catch(() => []) : Promise.resolve([]),
    ]);
    if (cancelled) return;
    state.artifacts = arts || [];

    if (chipsRes instanceof Error) {
      setChildren(sidebar, el('h3', { style: { marginTop: 0 } }, 'Chips'), chipListError(chipsRes));
      setChildren(detail, chipListError(chipsRes));
      return;
    }
    state.chips = chipsRes.map(normChip);
    paintSidebar();
    if (!state.chips.length) {
      setChildren(detail, emptyState('No chips yet',
        el('p', 'GET /api/chips returned an empty list. Either no chip has been built yet or the backend has not indexed chip/*.json.')));
      return;
    }
    const wanted = state.chipId && state.chips.find((c) => c.id === state.chipId);
    openChip((wanted || state.chips[0]).id);
  })();

  function normChip(c) {
    if (typeof c === 'string') return { id: c, path: '', footprint: null, scale: '', figures: [] };
    return {
      id: String(pick(c, 'id', 'chip_id') ?? ''),
      path: String(pick(c, 'path', 'file') ?? ''),
      footprint: c.footprint || null,
      scale: String(c.scale || ''),
      figures: Array.isArray(c.figures) ? c.figures.map(String) : [],
      name: String(pick(c, 'name', 'title') ?? ''),
    };
  }

  function paintSidebar() {
    const body = clear(sidebar);
    body.appendChild(el('h3', { style: { marginTop: 0 } }, `Chips (${state.chips.length})`));
    const list = el('div', { class: 'cols', style: { gap: '4px' } });
    for (const c of state.chips) {
      const b = el('button', {
        type: 'button', class: 'sel', style: { width: '100%', textAlign: 'left' },
        'aria-pressed': String(c.id === state.chipId),
        onclick: () => openChip(c.id),
      },
      el('span', {}, el('span', { class: 'mono' }, c.id),
        isDemoScale(c) ? el('span', {}, ' ', demoBadge()) : null,
        c.name ? el('span', { class: 'small muted', style: { display: 'block' } }, c.name) : null));
      list.appendChild(b);
    }
    body.appendChild(list);
    body.appendChild(el('p', { class: 'hint' }, 'From GET /api/chips: every chip in the repo, plus any this run built.'));
  }

  async function openChip(id) {
    state.chipId = id;
    paintSidebar();
    disposeViewer();
    setChildren(detail, loading(`Loading ${id}`));
    try {
      state.spec = await api.chip(id);
    } catch (err) {
      setChildren(detail, err instanceof ApiError && err.notReady
        ? emptyState('Not built yet', el('p', explain(err)))
        : errorState(`Could not load ${id}`, el('p', explain(err))));
      return;
    }
    if (cancelled) return;
    paintDetail();
  }

  /* ---------------------------------------------------------------- detail */

  function paintDetail() {
    const spec = state.spec || {};
    const rec = state.chips.find((c) => c.id === state.chipId) || { id: state.chipId };
    const merged = { ...rec, ...spec, footprint: spec.footprint || rec.footprint, scale: spec.scale || rec.scale };

    const tabs = ['plan', '3d', 'fabrication', 'downloads'];
    const labels = { plan: 'Plan view', '3d': '3D view', fabrication: 'Fabrication', downloads: 'Downloads' };
    const panel = el('div', { id: 'chip-panel', role: 'tabpanel', 'aria-label': labels[state.tab] });
    const bar = el('div', { class: 'segment', role: 'tablist', 'aria-label': 'Chip views' });
    for (const t of tabs) {
      const b = el('button', {
        type: 'button', role: 'tab', 'aria-selected': String(state.tab === t), 'aria-pressed': String(state.tab === t),
        'aria-controls': 'chip-panel',
        onclick: () => {
          state.tab = t;
          for (const o of bar.children) { o.setAttribute('aria-selected', 'false'); o.setAttribute('aria-pressed', 'false'); }
          b.setAttribute('aria-selected', 'true');
          b.setAttribute('aria-pressed', 'true');
          panel.setAttribute('aria-label', labels[t]);
          paintTab(panel, merged);
        },
      }, labels[t]);
      bar.appendChild(b);
    }

    setChildren(detail,
      el('section', { class: 'panel' },
        el('div', { class: 'row' },
          el('h2', { style: { margin: 0 } }, String(merged.id || '')),
          ...specFlags(spec),
          el('span', { class: 'spacer' }),
          footprintLabel(merged)),
        merged.name ? el('p', { class: 'muted', style: { marginTop: '6px' } }, String(merged.name)) : null,
        spec.description ? el('details', { class: 'disclose', style: { marginTop: '8px' } },
          el('summary', 'Description from the spec'),
          el('p', { class: 'small' }, String(spec.description))) : null,
        el('div', { class: 'row', style: { marginTop: '10px' } }, bar)),
      panel);

    paintTab(panel, merged);
  }

  function paintTab(panel, merged) {
    disposeViewer();
    if (state.tab === 'plan') return paintPlan(panel, merged);
    if (state.tab === '3d') return paint3d(panel, merged);
    if (state.tab === 'fabrication') return paintFab(panel, merged);
    return paintDownloads(panel, merged);
  }

  /* ---------------------------------------------------------------- plan */

  function paintPlan(panel, merged) {
    const spec = state.spec || {};
    const info = el('div', { class: 'panel' }, el('p', { class: 'muted small' }, 'Click a chamber, channel or port for its numbers.'));
    const { svg: plan, legend } = chipPlan(spec, {
      onSelect: (kind, obj) => {
        setChildren(info,
          el('h4', `${kind}: ${String(obj.id || '')}`),
          renderJson(el('div'), obj));
      },
    });
    const figs = figureList(merged);
    setChildren(panel,
      el('div', { class: 'cols c-2' },
        el('section', { class: 'panel' },
          el('h3', { style: { marginTop: 0 } }, 'Plan view', isDemoScale(merged) ? el('span', {}, ' ', demoBadge()) : null),
          plan, legend,
          el('p', { class: 'hint' }, 'Drawn straight from the chip spec in millimetres. No channel may cross another: '
            + 'one etch depth means a crossing is a short circuit.')),
        el('div', { class: 'cols' }, info, figs)));
  }

  function figureList(merged) {
    const fromRec = (merged.figures || []).map((p) => ({ path: String(p), bytes: NaN }));
    const fromArts = figuresFor(state.artifacts, merged.id);
    const all = [...fromRec, ...fromArts].filter((f, i, arr) => arr.findIndex((o) => o.path === f.path) === i);
    if (!all.length) {
      return el('section', { class: 'panel' },
        el('h3', { style: { marginTop: 0 } }, 'Figures'),
        el('p', { class: 'muted' }, 'No figures listed for this chip. S9 writes layout / features / stack SVGs and S11 writes the renders.'));
    }
    return el('section', { class: 'panel' },
      el('h3', { style: { marginTop: 0 } }, `Figures (${all.length})`),
      el('div', { class: 'figs' }, ...all.map((f) => el('figure', {},
        el('img', {
          src: api.fileUrlLoose(ctx.fileRunId, f.path) || '',
          alt: `figure ${f.path}`,
          loading: 'lazy',
          onerror: (e) => {
            const fig = e.target.parentElement;
            setChildren(fig, el('p', { class: 'small muted' }, `could not load ${f.path}`),
              ctx.fileRunId ? null : el('p', { class: 'hint' }, 'No run exists yet, and the API serves files only through /api/runs/<id>/file.'));
          },
        }),
        el('figcaption', f.path)))));
  }

  /* ---------------------------------------------------------------- 3d */

  function paint3d(panel, merged) {
    const canvas = el('canvas', { class: 'stl', 'aria-label': `3D view of ${merged.id}`, role: 'img' });
    const status = el('p', { class: 'stl-help' }, 'Loading geometry...');
    const parts = el('div', { class: 'row', style: { gap: '6px', marginTop: '8px' } });
    const dims = el('p', { class: 'small muted' });

    setChildren(panel, el('section', { class: 'panel' },
      el('h3', { style: { marginTop: 0 } }, '3D view', isDemoScale(merged) ? el('span', {}, ' ', demoBadge()) : null),
      el('div', { class: 'stl-wrap' }, canvas),
      el('p', { class: 'stl-help' }, 'Drag to orbit, wheel to zoom, shift-drag to pan. '
        + 'With the canvas focused: arrow keys orbit, + and - zoom, 0 resets.'),
      status, dims, parts,
      el('p', { class: 'hint' }, 'A small WebGL viewer written for this page - no 3D library, nothing fetched from the network. '
        + 'The wireframe box is the real bounding box of the placed parts.')));

    try { viewer = new StlViewer(canvas); }
    catch (err) {
      setChildren(panel, emptyState('No WebGL in this browser',
        el('p', explain(err)),
        el('p', 'The plan view and the S11 renders show the same geometry.'),
        el('p', {}, el('button', { type: 'button', onclick: () => { state.tab = 'plan'; paintDetail(); } }, 'Open the plan view'))));
      return;
    }
    loadStls(merged, status, parts, dims);
  }

  async function loadStls(merged, status, parts, dims) {
    const paths = stlCandidates(merged);
    if (!paths.length) {
      setChildren(status, 'No STL paths to try: S11 writes cad/exports/<chip_id>/ and this chip has no spec features to derive names from.');
      return;
    }
    const loaded = [];
    let tried = 0;
    for (const path of paths) {
      tried += 1;
      setChildren(status, `Loading geometry... ${tried}/${paths.length}`);
      try {
        const buf = await api.anyBuffer(ctx.fileRunId, path);
        if (cancelled || !viewer || viewer.disposed) return;
        loaded.push({ name: path.split('/').pop(), buffer: buf, path });
      } catch (_) { /* a missing per-feature STL is normal */ }
      if (loaded.length >= 48) break;
    }
    if (cancelled || !viewer || viewer.disposed) return;
    if (!loaded.length) {
      setChildren(status, el('span', { class: 'muted' },
        `None of the ${paths.length} expected STL paths exist yet. S11 (fabricate) writes them into cad/exports/${merged.id}/.`));
      parts.appendChild(el('p', { class: 'hint' }, ctx.fileRunId
        ? `cad/exports/ is only readable through a run that built this chip. Run ${ctx.fileRunId} did not build ${merged.id}, `
          + 'so start a run that produces it (or open the run that did) to see its geometry.'
        : 'No run exists yet, and the API serves files only through /api/runs/<id>/file.'));
      return;
    }
    const info = viewer.setMeshes(loaded.map((m, i) => ({ ...m, color: PALETTE[i % PALETTE.length] })));
    const size = bboxSize(info.bbox);
    setChildren(status, `${loaded.length} part${loaded.length === 1 ? '' : 's'}, ${info.triangles.toLocaleString()} triangles.`);
    setChildren(dims, size
      ? `Bounding box ${fmtNum(size[0], 4)} x ${fmtNum(size[1], 4)} x ${fmtNum(size[2], 4)} mm.`
      : '');
    clear(parts);
    loaded.forEach((m, i) => {
      const colour = PALETTE[i % PALETTE.length].map((v) => Math.round(v * 255));
      const b = el('button', {
        type: 'button', class: 'sel', 'aria-pressed': 'true',
        onclick: () => {
          const on = b.getAttribute('aria-pressed') !== 'true';
          b.setAttribute('aria-pressed', String(on));
          viewer.setVisible(m.name, on);
        },
      },
      el('i', {
        style: {
          display: 'inline-block', width: '9px', height: '9px', borderRadius: '2px',
          background: `rgb(${colour.join(',')})`, marginRight: '6px',
        },
      }),
      m.name);
      parts.appendChild(b);
    });
  }

  /** cad/exports/<chip>/<feature>.stl, from the artifact list when we have one. */
  function stlCandidates(merged) {
    const fromArts = [...exportsByChip(state.artifacts).get(merged.id) || []]
      .filter((a) => /\.stl$/i.test(a.path)).map((a) => a.path);
    if (fromArts.length) return fromArts;
    const spec = state.spec || {};
    const ids = [
      ...(spec.chambers || []).map((c) => c.id),
      ...(spec.ports || []).map((p) => p.id),
      ...(spec.features || []).map((f) => f.id),
    ].filter(Boolean).map(String);
    const dir = `cad/exports/${merged.id}`;
    return [...new Set([`${dir}/${merged.id}.stl`, ...ids.map((i) => `${dir}/${i}.stl`)])];
  }

  /* ---------------------------------------------------------------- fabrication */

  function paintFab(panel, merged) {
    const spec = state.spec || {};
    const checks = fabChecks(spec);
    const layers = Array.isArray(spec.layers) ? spec.layers : [];

    const checkRows = checks.map((c) => el('tr', { dataset: { sev: c.ok ? '' : 'high' } },
      el('td', c.name),
      el('td', { class: 'num' }, c.value),
      el('td', { class: 'num' }, c.limit),
      el('td', {}, c.ok === null ? badge('unknown', 'todo', 'the spec does not carry this value')
        : c.ok ? badge('ok', 'ok') : badge('fails the floor', 'bad')),
      el('td', { class: 'small muted' }, c.note || '')));

    const fab = el('div', {});
    setChildren(panel,
      el('div', { class: 'cols c-2' },
        el('section', { class: 'panel' },
          el('h3', { style: { marginTop: 0 } }, 'Fabrication envelope', isDemoScale(merged) ? el('span', {}, ' ', demoBadge()) : null),
          el('div', { class: 'scroll-x' }, el('table', { class: 'data' },
            el('caption', 'Floors come from contracts/blocks.json. Checked here against the spec as a reading aid - '
              + 'tools/check_chip_spec.py is the authority.'),
            el('thead', {}, el('tr', {}, el('th', 'Check'), el('th', { class: 'num' }, 'Spec'), el('th', { class: 'num' }, 'Floor'), el('th', 'Verdict'), el('th', 'Note'))),
            el('tbody', {}, ...checkRows))),
          layers.length ? el('div', {},
            el('h4', 'Layer stack'),
            el('div', { class: 'scroll-x' }, el('table', { class: 'data' },
              el('thead', {}, el('tr', {}, el('th', 'Layer'), el('th', 'Material'), el('th', { class: 'num' }, 'Thickness'))),
              el('tbody', {}, ...layers.map((l) => el('tr', {},
                el('td', { class: 'mono' }, String(l.id || '')),
                el('td', String(l.material || '')),
                el('td', { class: 'num' }, l.thickness_mm !== undefined ? `${fmtNum(l.thickness_mm, 4)} mm` : '-'))))))) : null),
        el('div', { class: 'cols' },
          el('section', { class: 'panel' }, el('h3', { style: { marginTop: 0 } }, 'Spec'), renderJson(el('div'), spec)),
          fab)));

    loadFabManifest(fab, merged);
  }

  async function loadFabManifest(hostEl, merged) {
    setChildren(hostEl, el('section', { class: 'panel' }, el('h3', { style: { marginTop: 0 } }, 'Fabrication manifest'), loading('Looking for cad/fabrication.json')));
    const tries = [`cad/exports/${merged.id}/manifest.json`, `cad/exports/${merged.id}/fabrication.json`, 'cad/fabrication.json'];
    for (const path of tries) {
      try {
        const data = await api.anyJson(ctx.fileRunId, path);
        if (cancelled) return;
        setChildren(hostEl, el('section', { class: 'panel' },
          el('h3', { style: { marginTop: 0 } }, 'Fabrication manifest'),
          el('p', { class: 'small mono muted' }, path),
          renderJson(el('div'), data)));
        return;
      } catch (_) { /* try the next */ }
    }
    if (cancelled) return;
    setChildren(hostEl, el('section', { class: 'panel' },
      el('h3', { style: { marginTop: 0 } }, 'Fabrication manifest'),
      el('p', { class: 'muted' }, 'No manifest found yet.'),
      el('p', { class: 'small muted' }, `Tried: ${tries.join(', ')}`),
      el('p', { class: 'hint' }, 'cad/fabrication.json is outside the /file allowlist, and a per-chip manifest is '
        + 'only readable through the run that exported the chip. The envelope checks on the left come straight from the spec.')));
  }

  function fabChecks(spec) {
    const fp = spec.footprint || {};
    const depths = [
      ...(spec.channels || []).map((c) => Number(c.depth_mm)),
      ...(spec.chambers || []).map((c) => Number(Array.isArray(c.size) ? c.size[2] : NaN)),
    ].filter(Number.isFinite);
    const widths = (spec.channels || []).map((c) => Number(c.width_mm)).filter(Number.isFinite);
    const ports = spec.ports || [];
    const minPitch = portPitch(ports);
    const out = [];

    const fx = Number(fp.x);
    const fy = Number(fp.y);
    out.push({
      name: 'footprint', value: Number.isFinite(fx) ? `${fmtNum(fx, 5)} x ${fmtNum(fy, 5)} mm` : '-',
      limit: '290 x 190 mm ceiling',
      ok: Number.isFinite(fx) ? (fx <= 290 && fy <= 190) : null,
      note: Number.isFinite(fx) && fx <= 30 && fy <= 20 ? 'the 30 x 20 mm demo envelope' : 'design target is 127.76 x 85.48 mm',
    });
    out.push({
      name: 'max depth', value: depths.length ? `${fmtNum(Math.max(...depths), 3)} mm` : '-',
      limit: `${FAB_FLOORS.depth_mm} mm`, ok: depths.length ? Math.max(...depths) <= FAB_FLOORS.depth_mm + 1e-9 : null,
      note: 'one fluidic layer at one etch depth',
    });
    out.push({
      name: 'min in-plane feature', value: widths.length ? `${fmtNum(Math.min(...widths), 3)} mm` : '-',
      limit: `${FAB_FLOORS.feature_mm} mm`, ok: widths.length ? Math.min(...widths) >= FAB_FLOORS.feature_mm - 1e-9 : null,
      note: 'narrowest channel width in the spec',
    });
    out.push({
      name: 'ports', value: String(ports.length), limit: '<= 6 per chip',
      ok: ports.length ? ports.length <= 6 : null, note: '0.8 mm drilled holes',
    });
    out.push({
      name: 'min port pitch', value: Number.isFinite(minPitch) ? `${fmtNum(minPitch, 3)} mm` : '-',
      limit: `${FAB_FLOORS.port_pitch_mm} mm`, ok: Number.isFinite(minPitch) ? minPitch >= FAB_FLOORS.port_pitch_mm - 1e-6 : null,
      note: 'closest pair of ports in plan view',
    });
    out.push({
      name: 'channels', value: String((spec.channels || []).length), limit: 'no crossings',
      ok: null, note: 'crossing check belongs to tools/check_chip_spec.py, not to this page',
    });
    return out;
  }

  function portPitch(ports) {
    let min = Infinity;
    for (let i = 0; i < ports.length; i += 1) {
      for (let j = i + 1; j < ports.length; j += 1) {
        const a = ports[i].position || [];
        const b = ports[j].position || [];
        const d = Math.hypot(Number(a[0]) - Number(b[0]), Number(a[1]) - Number(b[1]));
        if (Number.isFinite(d)) min = Math.min(min, d);
      }
    }
    return Number.isFinite(min) ? min : NaN;
  }

  /* ---------------------------------------------------------------- downloads */

  function paintDownloads(panel, merged) {
    const fromArts = [...(exportsByChip(state.artifacts).get(merged.id) || [])];
    const groups = [
      { label: 'STL (printable part)', re: /\.stl$/i, kind: 'stl' },
      { label: 'Mould', re: /(mould|mold)[^/]*$/i, kind: 'mould' },
      { label: 'DXF (closed outline)', re: /\.dxf$/i, kind: 'dxf' },
      { label: 'Fusion script', re: /\.(py|js)$/i, kind: 'fusion' },
    ];

    const derived = [
      { label: 'STL (printable part)', path: `cad/exports/${merged.id}/${merged.id}.stl` },
      { label: 'Mould', path: `cad/exports/${merged.id}/${merged.id}_mould.stl` },
      { label: 'DXF (closed outline)', path: `cad/exports/${merged.id}/${merged.id}_closed.dxf` },
      { label: 'DXF', path: `cad/exports/${merged.id}/${merged.id}.dxf` },
      { label: 'Fusion script', path: `chip/fusion/${merged.id}.py` },
      { label: 'Chip spec (JSON)', path: merged.path || `chip/${merged.id}.json` },
    ];

    const sections = [];
    if (fromArts.length) {
      for (const g of groups) {
        const hits = fromArts.filter((a) => g.re.test(a.path));
        if (!hits.length) continue;
        sections.push(el('div', {},
          el('h4', g.label),
          el('div', { class: 'dl-list' }, ...hits.map((a) => dlButton(a.path, a.bytes)))));
      }
      const rest = fromArts.filter((a) => !groups.some((g) => g.re.test(a.path)));
      if (rest.length) {
        sections.push(el('div', {}, el('h4', 'Other exports'), el('div', { class: 'dl-list' }, ...rest.map((a) => dlButton(a.path, a.bytes)))));
      }
    }

    setChildren(panel, el('section', { class: 'panel' },
      el('h3', { style: { marginTop: 0 } }, 'Downloads', isDemoScale(merged) ? el('span', {}, ' ', demoBadge()) : null),
      sections.length
        ? el('div', {}, ...sections)
        : el('div', {},
          el('p', { class: 'muted' }, 'This run has not reported any exports for this chip, so these are the conventional '
            + 'S11 output paths. A download that 404s means the file has not been written yet.'),
          el('div', { class: 'dl-list' }, ...derived.map((d) => dlButton(d.path, NaN, d.label)))),
      el('p', { class: 'hint' }, 'Files are fetched through the backend\'s sandboxed /file route and saved by the browser; '
        + 'nothing is uploaded anywhere.')));
  }

  function dlButton(path, bytes, label) {
    const ext = (String(path).split('.').pop() || '').toLowerCase();
    return el('button', {
      type: 'button',
      title: path,
      onclick: async () => {
        try { await download(ctx.fileRunId, path); toast(`Saved ${String(path).split('/').pop()}`); }
        catch (err) {
          toast(err instanceof ApiError && err.missing ? `${path} does not exist yet` : explain(err), true);
        }
      },
    },
    el('span', { class: 'kind' }, ext),
    el('span', {}, label || String(path).split('/').pop()),
    Number.isFinite(bytes) ? el('span', { class: 'bytes' }, fmtBytes(bytes)) : null);
  }

  /* ---------------------------------------------------------------- teardown */

  function disposeViewer() {
    if (viewer) { try { viewer.dispose(); } catch (_) {} viewer = null; }
  }

  function chipListError(err) {
    if (err instanceof ApiError && err.notReady) {
      return emptyState('Not built yet', el('p', explain(err)),
        el('p', { class: 'small muted' }, 'GET /api/chips is in web/README.md but the backend has not wired it up.'));
    }
    return errorState('Could not list the chips', el('p', explain(err)));
  }

  return () => { cancelled = true; disposeViewer(); };
}

export { simBadge };

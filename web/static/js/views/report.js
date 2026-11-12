// View 6: Report. report.md rendered with the small safe markdown renderer in
// markdown.js - escaped text nodes only, no innerHTML, and images become links so an
// ingested paper cannot make this page fetch anything.

import { api, ApiError, explain, download } from '../api.js';
import { el, clear, setChildren, badge, toast, emptyState, errorState, loading } from '../dom.js';
import { simBadge, stubBadge, demoBadge, todoBadge } from '../labels.js';
import { markdownBlock } from '../markdown.js';
import { renderJson } from '../jsonview.js';
import { loadArtifacts, loadStageJson } from '../locate.js';

export function mount(host, ctx) {
  const runId = ctx.runId;
  let cancelled = false;
  setChildren(host,
    el('div', { class: 'view-head' }, el('h2', 'Report'), el('span', { class: 'sub' }, runId)),
    loading('Loading the report'));

  (async () => {
    let md = null;
    let json = null;
    let err = null;
    const summary = (ctx.state.runs || []).find((r) => r.run_id === runId);
    try {
      // The run list says when there is no report; asking anyway only logs a 404.
      if (summary && summary.has_report === false) throw new ApiError('no report yet', { status: 404 });
      const res = await api.report(runId);
      md = typeof res === 'string' ? res : (res && (res.markdown || res.md)) || null;
      json = res && typeof res === 'object' ? (res.json || null) : null;
    } catch (e) { err = e; }

    if (cancelled) return;

    // Fall back to the file route if /report is not wired up yet.
    if (!md && !(summary && summary.has_report === false)) {
      const artifacts = await loadArtifacts(runId).catch(() => []);
      // /file takes repo-relative paths, so the full path goes first: a bare name is a
      // guaranteed 403 and only adds noise to the network log.
      const hit = artifacts.find((a) => /report\.md$/i.test(a.path));
      const tries = [...new Set([hit && hit.path, `assay2chip/work/${runId}/report.md`].filter(Boolean))];
      for (const path of tries) {
        try { md = await api.fileText(runId, path); break; }
        catch (_) { /* next */ }
      }
      if (!json) {
        const j = await loadStageJson(runId, 'report.json', artifacts);
        if (!j.missing) json = j.data;
      }
    }
    if (cancelled) return;
    render(host, ctx, { runId, md, json, err });
  })();

  return () => { cancelled = true; };
}

function render(host, ctx, { runId, md, json, err }) {
  const head = el('div', { class: 'view-head' },
    el('h2', 'Report'),
    el('span', { class: 'sub' }, runId),
    el('span', { class: 'spacer' }),
    md ? el('button', {
      type: 'button', class: 'ghost',
      onclick: async () => {
        try { await download(runId, 'report.md'); }
        catch (_) {
          const blob = new Blob([md], { type: 'text/markdown' });
          const url = URL.createObjectURL(blob);
          const a = el('a', { href: url, download: `${runId}_report.md` });
          document.body.appendChild(a); a.click(); a.remove();
          setTimeout(() => URL.revokeObjectURL(url), 10000);
          toast('Saved the copy this page is showing');
        }
      },
    }, 'Download report.md') : null);

  if (!md) {
    const notReady = err instanceof ApiError && err.notReady;
    setChildren(host, head,
      notReady
        ? emptyState('Not built yet', el('p', {}, todoBadge()), el('p', explain(err)))
        : emptyState('No report yet',
          el('p', 'S12 writes report.md at the end of a run. GET /api/runs/' + runId + '/report returned nothing and report.md is not readable.'),
          err ? el('p', { class: 'small muted' }, explain(err)) : null,
          el('p', {}, el('button', { type: 'button', onclick: () => ctx.navigate('run', runId) }, 'Open the stage timeline'))),
      json ? el('section', { class: 'panel' }, el('h3', { style: { marginTop: 0 } }, 'report.json'), renderJson(el('div'), json)) : null);
    return;
  }

  const flags = el('div', { class: 'row', style: { gap: '6px', marginBottom: '12px' } });
  if (/\bsimulat/i.test(md)) flags.appendChild(simBadge());
  if (/\b(llm[ _-]?stub|stub)\b/i.test(md)) flags.appendChild(stubBadge());
  if (/30\s*x\s*20\s*mm|demo scale/i.test(md)) flags.appendChild(demoBadge());
  if (/not yet implemented|not implemented/i.test(md)) flags.appendChild(todoBadge());

  const body = markdownBlock(md);
  const toc = buildToc(body);

  setChildren(host, head,
    flags.childElementCount
      ? el('p', { class: 'small muted' }, 'The report itself says: ', flags)
      : null,
    el('div', { class: 'cols c-side' },
      el('section', { class: 'panel' },
        el('h3', { style: { marginTop: 0 } }, 'Contents'),
        toc.childElementCount ? toc : el('p', { class: 'muted small' }, 'No headings.'),
        el('p', { class: 'hint' }, 'Rendered by a small in-page markdown renderer: every string is inserted as text, '
          + 'raw HTML is shown literally, and an image reference becomes a link rather than a fetch.')),
      el('section', { class: 'panel' }, body)),
    json ? el('details', { class: 'disclose', style: { marginTop: '16px' } },
      el('summary', 'report.json'), renderJson(el('div'), json)) : null);
}

function buildToc(body) {
  const toc = el('nav', { 'aria-label': 'Report contents' });
  const list = el('ul', { style: { margin: 0, paddingLeft: '18px' } });
  let n = 0;
  for (const h of body.querySelectorAll('h1, h2, h3')) {
    n += 1;
    const id = `rp-${n}`;
    h.id = id;
    const level = Number(h.tagName.slice(1));
    list.appendChild(el('li', { style: { marginLeft: `${(level - 1) * 10}px`, fontSize: level > 2 ? '12px' : '13px' } },
      el('a', {
        href: `#${id}`,
        onclick: (e) => { e.preventDefault(); h.scrollIntoView({ block: 'start', behavior: 'smooth' }); h.tabIndex = -1; h.focus(); },
      }, h.textContent)));
  }
  if (n) toc.appendChild(list);
  return toc;
}

export { badge, clear, errorState };

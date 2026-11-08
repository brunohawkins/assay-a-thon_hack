// A small, deliberately limited markdown renderer that builds DOM nodes.
//
// Rules this follows because report.md can quote an ingested paper:
//   - no innerHTML anywhere; every piece of text is a text node
//   - raw HTML in the source is NOT interpreted, it is shown as literal text
//   - links are only rendered as links when the scheme is http(s), mailto or relative
//   - images are rendered as a labelled link, never an <img>, so nothing is fetched
//     from the network and an off-repo URL cannot phone home
//
// Supported: headings, paragraphs, fenced and indented code, blockquotes, hr,
// unordered/ordered lists with one level of nesting, GFM pipe tables, and the inline
// set below.

import { el, safeLink } from './dom.js';

const ESCAPABLE = '\\`*_{}[]()#+-.!|~>';

/* ----------------------------------------------------------------- inline */

function pushText(out, s) {
  if (s) out.push(document.createTextNode(s));
}

// Longest mark first, so *** beats ** beats *.
const MARKS = [['***', 'strongem'], ['**', 'strong'], ['__', 'strong'], ['~~', 'del'], ['*', 'em'], ['_', 'em']];

// `_` inside a word (snake_case, file_name_v1) is not emphasis.
function underscoreOk(s, open, close, mark) {
  if (mark[0] !== '_') return true;
  const before = open === 0 ? ' ' : s[open - 1];
  const after = s[close + mark.length] ?? ' ';
  return !/\w/.test(before) && !/\w/.test(after);
}

/** Inline markdown -> array of nodes. Always advances, so it cannot loop. */
export function renderInline(src) {
  const out = [];
  const s = String(src ?? '');
  let buf = '';
  let i = 0;
  const flush = () => { pushText(out, buf); buf = ''; };

  while (i < s.length) {
    const c = s[i];

    // backslash escape
    if (c === '\\' && i + 1 < s.length && ESCAPABLE.includes(s[i + 1])) {
      buf += s[i + 1]; i += 2; continue;
    }

    // code span: `code` or ``code``
    if (c === '`') {
      let ticks = 0;
      while (s[i + ticks] === '`') ticks += 1;
      const fence = '`'.repeat(ticks);
      const end = s.indexOf(fence, i + ticks);
      if (end !== -1) {
        flush();
        out.push(el('code', s.slice(i + ticks, end).trim()));
        i = end + ticks;
        continue;
      }
      buf += fence; i += ticks; continue;
    }

    // image: ![alt](src) - rendered as a link, never an <img>, so nothing is fetched
    if (c === '!' && s[i + 1] === '[') {
      const m = matchLink(s, i + 1);
      if (m) {
        flush();
        out.push(el('span', { class: 'small muted' }, 'figure: '), safeLink(m.href, m.label || 'image'));
        i = m.end; continue;
      }
    }

    // link: [label](href)
    if (c === '[') {
      const m = matchLink(s, i);
      if (m) {
        flush();
        const a = safeLink(m.href, m.label || m.href);
        if (a.tagName === 'A' && m.label) {
          a.textContent = '';
          for (const n of renderInline(m.label)) a.appendChild(n);
        }
        out.push(a);
        i = m.end; continue;
      }
    }

    // autolink: <https://...>
    if (c === '<') {
      const m = /^<((?:https?:\/\/|mailto:)[^>\s]+)>/.exec(s.slice(i));
      if (m) { flush(); out.push(safeLink(m[1], m[1])); i += m[0].length; continue; }
    }

    // strong / em / strike
    let matched = false;
    for (const [mark, tag] of MARKS) {
      if (!s.startsWith(mark, i)) continue;
      const from = i + mark.length;
      const close = s.indexOf(mark, from);
      if (close === -1 || close === from) continue;
      if (!underscoreOk(s, i, close, mark)) continue;
      const inner = s.slice(from, close);
      if (!inner.trim()) continue;
      flush();
      const kids = renderInline(inner);
      out.push(tag === 'strongem' ? el('strong', {}, el('em', {}, ...kids)) : el(tag, {}, ...kids));
      i = close + mark.length;
      matched = true;
      break;
    }
    if (matched) continue;

    buf += c;
    i += 1;
  }
  flush();
  return out;
}

function matchLink(s, start) {
  if (s[start] !== '[') return null;
  let depth = 0;
  let i = start;
  for (; i < s.length; i += 1) {
    if (s[i] === '\\') { i += 1; continue; }
    if (s[i] === '[') depth += 1;
    else if (s[i] === ']') { depth -= 1; if (depth === 0) break; }
  }
  if (depth !== 0 || s[i + 1] !== '(') return null;
  const label = s.slice(start + 1, i);
  let j = i + 2;
  let paren = 1;
  for (; j < s.length; j += 1) {
    if (s[j] === '(') paren += 1;
    else if (s[j] === ')') { paren -= 1; if (paren === 0) break; }
  }
  if (paren !== 0) return null;
  let href = s.slice(i + 2, j).trim();
  const titled = /^(\S+)\s+["'(].*$/.exec(href);
  if (titled) href = titled[1];
  return { label, href, end: j + 1 };
}

/* ----------------------------------------------------------------- blocks */

const H_RE = /^(#{1,6})\s+(.*)$/;
const HR_RE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
const UL_RE = /^(\s*)([-*+])\s+(.*)$/;
const OL_RE = /^(\s*)(\d+)[.)]\s+(.*)$/;
const FENCE_RE = /^\s{0,3}(```+|~~~+)\s*(\S*)/;
const QUOTE_RE = /^\s{0,3}>\s?(.*)$/;
const TABLE_SEP_RE = /^\s*\|?[\s:|-]+\|[\s:|-]*$/;

function splitRow(line) {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1);
  const cells = [];
  let cur = '';
  for (let i = 0; i < s.length; i += 1) {
    if (s[i] === '\\' && s[i + 1] === '|') { cur += '|'; i += 1; continue; }
    if (s[i] === '|') { cells.push(cur); cur = ''; continue; }
    cur += s[i];
  }
  cells.push(cur);
  return cells.map((c) => c.trim());
}

function alignsOf(sep) {
  return splitRow(sep).map((c) => {
    const l = c.startsWith(':');
    const r = c.endsWith(':');
    if (l && r) return 'center';
    if (r) return 'right';
    if (l) return 'left';
    return null;
  });
}

/** Markdown -> a DocumentFragment of block elements. */
export function renderMarkdown(src) {
  const lines = String(src ?? '').replace(/\r\n?/g, '\n').split('\n');
  const root = document.createDocumentFragment();
  let i = 0;

  const flushPara = (buf) => {
    if (!buf.length) return;
    const joined = buf.join('\n');
    root.appendChild(el('p', {}, ...renderInline(joined)));
    buf.length = 0;
  };
  const para = [];

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) { flushPara(para); i += 1; continue; }

    const fence = FENCE_RE.exec(line);
    if (fence) {
      flushPara(para);
      const close = fence[1][0].repeat(3);
      const body = [];
      i += 1;
      while (i < lines.length && !lines[i].trimStart().startsWith(close)) { body.push(lines[i]); i += 1; }
      i += 1;
      const code = el('code', body.join('\n'));
      if (fence[2]) code.dataset.lang = fence[2];
      root.appendChild(el('pre', {}, code));
      continue;
    }

    const h = H_RE.exec(line);
    if (h) {
      flushPara(para);
      const level = Math.min(6, h[1].length);
      root.appendChild(el(`h${level}`, {}, ...renderInline(h[2].replace(/\s+#+\s*$/, ''))));
      i += 1; continue;
    }

    if (HR_RE.test(line)) { flushPara(para); root.appendChild(el('hr')); i += 1; continue; }

    if (QUOTE_RE.test(line)) {
      flushPara(para);
      const body = [];
      while (i < lines.length && (QUOTE_RE.test(lines[i]) || (lines[i].trim() && body.length))) {
        const m = QUOTE_RE.exec(lines[i]);
        if (!m) break;
        body.push(m[1]);
        i += 1;
      }
      const bq = el('blockquote');
      bq.appendChild(renderMarkdown(body.join('\n')));
      root.appendChild(bq);
      continue;
    }

    // GFM table: a header row followed by a separator row
    if (line.includes('|') && i + 1 < lines.length && TABLE_SEP_RE.test(lines[i + 1])) {
      flushPara(para);
      const header = splitRow(line);
      const aligns = alignsOf(lines[i + 1]);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].trim() && lines[i].includes('|')) { rows.push(splitRow(lines[i])); i += 1; }
      const thead = el('thead', {}, el('tr', {}, ...header.map((c, k) => {
        const th = el('th', {}, ...renderInline(c));
        if (aligns[k]) th.style.textAlign = aligns[k];
        return th;
      })));
      const tbody = el('tbody', {}, ...rows.map((r) => el('tr', {}, ...header.map((_, k) => {
        const td = el('td', {}, ...renderInline(r[k] ?? ''));
        if (aligns[k]) td.style.textAlign = aligns[k];
        return td;
      }))));
      root.appendChild(el('table', {}, thead, tbody));
      continue;
    }

    if (UL_RE.test(line) || OL_RE.test(line)) {
      flushPara(para);
      const { node, next } = parseList(lines, i);
      root.appendChild(node);
      i = next;
      continue;
    }

    // indented code block (4 spaces), only when not continuing a paragraph
    if (/^ {4}\S/.test(line) && !para.length) {
      flushPara(para);
      const body = [];
      while (i < lines.length && (/^ {4}/.test(lines[i]) || !lines[i].trim())) { body.push(lines[i].slice(4)); i += 1; }
      while (body.length && !body[body.length - 1].trim()) body.pop();
      root.appendChild(el('pre', {}, el('code', body.join('\n'))));
      continue;
    }

    para.push(line);
    i += 1;
  }
  flushPara(para);
  return root;
}

function parseList(lines, start) {
  const first = UL_RE.exec(lines[start]) || OL_RE.exec(lines[start]);
  const ordered = !UL_RE.test(lines[start]);
  const baseIndent = first[1].length;
  const list = el(ordered ? 'ol' : 'ul');
  if (ordered && Number(first[2]) !== 1) list.setAttribute('start', String(Number(first[2])));

  let i = start;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      // a blank line ends the list unless the next line is still an item
      const nxt = lines[i + 1] || '';
      if (!(UL_RE.test(nxt) || OL_RE.test(nxt)) || (UL_RE.exec(nxt) || OL_RE.exec(nxt))[1].length < baseIndent) break;
      i += 1; continue;
    }
    const m = UL_RE.exec(line) || OL_RE.exec(line);
    if (!m) {
      // lazy continuation of the previous item
      if (list.lastElementChild) {
        list.lastElementChild.appendChild(document.createTextNode(' '));
        for (const n of renderInline(line.trim())) list.lastElementChild.appendChild(n);
        i += 1; continue;
      }
      break;
    }
    const indent = m[1].length;
    if (indent < baseIndent) break;
    if (indent > baseIndent) {
      const sub = parseList(lines, i);
      if (list.lastElementChild) list.lastElementChild.appendChild(sub.node);
      else list.appendChild(el('li', {}, sub.node));
      i = sub.next;
      continue;
    }
    const li = el('li', {}, ...renderInline(m[3]));
    list.appendChild(li);
    i += 1;
  }
  return { node: list, next: i };
}

/** Render into a fresh container with the .md class. */
export function markdownBlock(src) {
  const host = el('div', { class: 'md' });
  host.appendChild(renderMarkdown(src));
  return host;
}

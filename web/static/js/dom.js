// Safe DOM helpers. Nothing in here ever assigns innerHTML: every string that could
// have come from an ingested paper goes in as a text node.

const SVG_NS = 'http://www.w3.org/2000/svg';

function applyProps(node, props) {
  for (const [k, v] of Object.entries(props)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class' || k === 'className') node.setAttribute('class', String(v));
    else if (k === 'text') node.appendChild(document.createTextNode(String(v)));
    else if (k === 'dataset') for (const [dk, dv] of Object.entries(v)) {
      if (dv !== null && dv !== undefined) node.dataset[dk] = String(dv);
    }
    else if (k === 'style' && typeof v === 'object') Object.assign(node.style, v);
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, '');
    else node.setAttribute(k, String(v));
  }
}

function append(node, children) {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    if (Array.isArray(c)) append(node, c);
    else if (c instanceof Node) node.appendChild(c);
    else node.appendChild(document.createTextNode(String(c)));
  }
}

/** el('div', {class:'x'}, [children]) - props optional, children may be nodes or strings. */
export function el(tag, props, ...children) {
  const node = document.createElement(tag);
  if (props instanceof Node || Array.isArray(props) || typeof props === 'string' || typeof props === 'number') {
    children.unshift(props);
  } else if (props) applyProps(node, props);
  append(node, children);
  return node;
}

/** Same, in the SVG namespace. */
export function svg(tag, props, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  if (props) applyProps(node, props);
  append(node, children);
  return node;
}

export function frag(...children) {
  const f = document.createDocumentFragment();
  append(f, children);
  return f;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function setChildren(node, ...children) {
  clear(node);
  append(node, children);
  return node;
}

/** A link that is only rendered as a link when the href scheme is safe. */
export function safeLink(href, label) {
  const s = String(href || '');
  const ok = /^(https?:|mailto:|\/|\.\/|#|[\w.-]+\/)/i.test(s) && !/^\s*javascript:/i.test(s);
  if (!ok) return el('span', { class: 'mono', title: 'link with an unsafe scheme, shown as text' }, label || s);
  const a = el('a', { href: s, text: label || s });
  if (/^https?:/i.test(s)) { a.target = '_blank'; a.rel = 'noopener noreferrer nofollow'; }
  return a;
}

export function badge(label, kind, title) {
  return el('span', { class: kind ? `badge ${kind}` : 'badge', title: title || null }, label);
}

/** Pick a nested value without throwing on a missing branch. */
export function at(obj, ...keys) {
  let cur = obj;
  for (const k of keys) {
    if (cur === null || typeof cur !== 'object') return undefined;
    cur = cur[k];
  }
  return cur;
}

/** First defined property out of several candidate names. */
export function pick(obj, ...names) {
  if (!obj || typeof obj !== 'object') return undefined;
  for (const n of names) if (obj[n] !== undefined && obj[n] !== null) return obj[n];
  return undefined;
}

export function fmtBytes(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '';
  if (v < 1024) return `${v} B`;
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} kB`;
  return `${(v / 1048576).toFixed(1)} MB`;
}

export function fmtSeconds(s) {
  const v = Number(s);
  if (!Number.isFinite(v)) return '';
  if (v < 10) return `${v.toFixed(2)} s`;
  if (v < 120) return `${v.toFixed(1)} s`;
  return `${Math.floor(v / 60)}m ${Math.round(v % 60)}s`;
}

/** Significant-figure formatting that never fakes precision we do not have. */
export function fmtNum(v, sig = 3) {
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v ?? '-');
  if (n === 0) return '0';
  const a = Math.abs(n);
  if (a >= 1e5 || a < 1e-3) return n.toExponential(Math.max(0, sig - 1));
  return String(Number(n.toPrecision(sig)));
}

export function fmtTime(t) {
  if (t === null || t === undefined || t === '') return '';
  const d = typeof t === 'number' ? new Date(t * (t > 1e11 ? 1 : 1000)) : new Date(t);
  if (Number.isNaN(d.getTime())) return String(t);
  return d.toLocaleString();
}

let toastTimer = 0;
export function toast(message, bad = false) {
  const node = document.getElementById('toast');
  if (!node) return;
  setChildren(node, String(message));
  node.classList.toggle('bad', !!bad);
  node.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove('show'), 5200);
}

export function loading(what = 'Loading') {
  return el('p', { class: 'loading' }, `${what}...`);
}

export function emptyState(title, ...body) {
  return el('div', { class: 'empty' }, el('h3', title), ...body.map((b) => (b instanceof Node ? b : el('p', b))));
}

export function errorState(title, ...body) {
  return el('div', { class: 'err' }, el('h3', title), ...body.map((b) => (b instanceof Node ? b : el('p', b))));
}

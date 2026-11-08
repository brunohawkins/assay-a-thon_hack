// A collapsible JSON tree. Every key and value is a text node, so a string lifted out of
// an ingested paper cannot inject markup.

import { el, clear } from './dom.js';

const BIG = 60;          // collapse arrays/objects larger than this by default
const DEPTH_OPEN = 2;    // auto-expand to this depth

function scalar(v) {
  if (v === null) return el('span', { class: 'jnull' }, 'null');
  switch (typeof v) {
    case 'string': return el('span', { class: 'js' }, JSON.stringify(v));
    case 'number': return el('span', { class: 'jn' }, String(v));
    case 'boolean': return el('span', { class: 'jb' }, String(v));
    default: return el('span', { class: 'jnull' }, String(v));
  }
}

function isLeaf(v) { return v === null || typeof v !== 'object'; }

function summaryOf(v) {
  if (Array.isArray(v)) return `[${v.length} item${v.length === 1 ? '' : 's'}]`;
  const n = Object.keys(v).length;
  return `{${n} key${n === 1 ? '' : 's'}}`;
}

function node(key, value, depth) {
  const label = key === null
    ? null
    : el('span', {}, el('span', { class: 'jk' }, String(key)), el('span', { class: 'jpunc' }, ': '));

  if (isLeaf(value)) {
    return el('li', {}, label, scalar(value));
  }

  const entries = Array.isArray(value)
    ? value.map((v, i) => [i, v])
    : Object.entries(value);

  const open = depth < DEPTH_OPEN && entries.length <= BIG;
  const ul = el('ul');
  const details = el('details', open ? { open: true } : {},
    el('summary', {}, label, el('span', { class: 'jcount' }, summaryOf(value))),
    ul);

  let built = false;
  const build = () => {
    if (built) return;
    built = true;
    for (const [k, v] of entries) ul.appendChild(node(k, v, depth + 1));
  };
  if (open) build();
  else details.addEventListener('toggle', build, { once: true });

  return el('li', {}, details);
}

/** Render any parsed JSON into a host element. */
export function renderJson(host, value) {
  clear(host);
  host.classList.add('jsonview');
  const ul = el('ul');
  if (isLeaf(value)) ul.appendChild(el('li', {}, scalar(value)));
  else for (const [k, v] of (Array.isArray(value) ? value.map((x, i) => [i, x]) : Object.entries(value))) {
    ul.appendChild(node(k, v, 0));
  }
  host.appendChild(ul);
  return host;
}

export function jsonView(value) {
  return renderJson(el('div'), value);
}

/** Plain pretty-printed text, for small payloads and for copy/paste. */
export function jsonPre(value) {
  let text;
  try { text = JSON.stringify(value, null, 2); }
  catch (_) { text = String(value); }
  return el('pre', { class: 'code' }, text);
}

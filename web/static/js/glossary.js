// Plain-language definitions for every word on this site that a first-time visitor
// would have to look up. term() wraps a word so hovering or focusing it shows the
// definition; glossaryList() prints them all. Text only, never HTML.

import { el } from './dom.js';

export const GLOSSARY = {
  capsule: {
    word: 'capsule',
    def: 'A hollow gel bead about 70 µm across, a little narrower than a hair, holding one cell. '
      + 'Its shell lets reagents in but keeps the cell\'s RNA inside, so liquids can be swapped around it '
      + 'and every cell stays separate.',
  },
  lane: {
    word: 'lane',
    def: 'One of the three culture channels on Module 1, one per cell type: neurons, microglia and astrocytes. '
      + 'The lanes start out sealed from each other, are then connected so the cells can signal, and are '
      + 'harvested together at the end.',
  },
  weir: {
    word: 'weir',
    def: 'A low step under the lid that leaves a gap narrower than a capsule. Liquid flows over it and capsules '
      + 'stay behind. We use height for this because our printers cannot make anything narrower than 0.2 mm '
      + 'side to side, but can control depth finely.',
  },
  shear: {
    word: 'shear',
    def: 'The drag that moving liquid puts on whatever it flows past, including cells on the floor. Too much and '
      + 'cells are damaged or washed off, so every step has a ceiling. Measured in dyn/cm².',
  },
  residence: {
    word: 'residence time',
    def: 'How long liquid stays in one spot before it is flushed out. A corner with a long residence time keeps '
      + 'old liquid in it, which means a wash or a reagent change did not fully happen there.',
  },
  sweep: {
    word: 'sweep',
    def: 'Changing the liquid in a full chamber by pushing fresh liquid through it, usually a few chamber volumes, '
      + 'so the old liquid is carried out. Different from filling an empty chamber.',
  },
  blocker: {
    word: 'blocker',
    def: 'A problem severe enough that the protocol would not work on the chip as drawn, for example a pressure '
      + 'the chip cannot hold, or shear far above what the cells survive. High, medium and low are progressively '
      + 'milder.',
  },
  demo: {
    word: 'demo scale',
    def: 'The 30 × 20 mm chips we can print this weekend: a scaled-down test of the layout. The real device targets '
      + 'a standard microplate footprint, 127.76 × 85.48 mm. Numbers from a demo chip are not numbers for the real one.',
  },
  rom: {
    word: 'reduced-order vs 3D',
    def: 'The fast flow simulation treats each shallow channel as a flat sheet (a "Hele-Shaw" model). That is the '
      + 'right physics for slow flow in flat channels and takes seconds. The 3D simulation (OpenFOAM) resolves the '
      + 'full depth and takes far longer; it is used to check the fast one where the flat-sheet idea is doubtful.',
  },
  offchip: {
    word: 'off-chip',
    def: 'Done at the bench, in a tube, or on a bought instrument, not on a chip we print. Usually because the '
      + 'feature needed is smaller than our printers can make.',
  },
  printed: {
    word: 'printed for the demo',
    def: 'One of the six chips physically resin-printed for this weekend\'s demonstration. The others exist as designs only.',
  },
  simulated: {
    word: 'simulated',
    def: 'Worked out by the flow model from the chip geometry, not measured on a real chip. No pumps or cameras are '
      + 'connected this weekend, so every flow number here is a prediction.',
  },
  stub: {
    word: 'LLM stub',
    def: 'The step that reads a protocol and pulls out its steps normally uses a language model. In stub mode it runs '
      + 'offline with simple fixed rules instead, so what it extracts is a placeholder, not a reading of the text.',
  },
  handoff: {
    word: 'handoff',
    def: 'Moving the sample from one chip to the next, usually through tubing. Each one costs some volume left in the '
      + 'tube (dead volume) and some pressure.',
  },
  readout: {
    word: 'readout',
    def: 'A measurement the assay produces. The main ones are single-cell: which genes are switched on (transcriptome) and the regulatory state (TIP-seq), each read cell by cell. Protein and small-molecule samples can optionally be taken separately from the same chambers, for example for mass spec later.',
  },
  barcode: {
    word: 'barcode',
    def: 'A short unique DNA sequence attached to everything from one cell. After sequencing, reads with the same barcode '
      + 'came from the same cell, which is what makes the data single-cell.',
  },
  trap: {
    word: 'trap (dead zone)',
    def: 'A region where liquid barely moves, so it is not exchanged when the rest of the chamber is. The flat-sheet '
      + 'model cannot show swirling eddies, so a trap here means slow flow or long residence time.',
  },
  bubble: {
    word: 'bubble',
    def: 'Air caught when a chamber fills, typically where a channel opens suddenly into something wider and the '
      + 'liquid front sticks at the edge while the corners fill last.',
  },
  sediment: {
    word: 'sediment',
    def: 'Cells or capsules sinking to the floor faster than the flow carries them, so they pile up instead of moving on.',
  },
  leak: {
    word: 'leak',
    def: 'Liquid going somewhere it should not, for example out of a port meant to be closed, or into a neighbouring lane.',
  },
  clog: {
    word: 'clog',
    def: 'A channel narrow enough that cells or capsules could jam in it.',
  },
};

/** Map a finding class to a glossary key when one exists. */
export function classTerm(cls) {
  const c = String(cls || '').toLowerCase();
  return GLOSSARY[c] ? c : null;
}

let tipSeq = 0;

/**
 * A word with its definition on hover and keyboard focus. The definition is a real
 * element linked by aria-describedby, so screen readers get it too.
 */
export function term(key, label) {
  const g = GLOSSARY[key];
  if (!g) return el('span', {}, label || key);
  tipSeq += 1;
  const id = `tip-${tipSeq}`;
  return el('span', { class: 'term' },
    el('button', { type: 'button', class: 'term-word', 'aria-describedby': id }, label || g.word),
    el('span', { class: 'term-tip', role: 'tooltip', id },
      el('strong', {}, g.word), ' ', g.def));
}

/** The whole glossary as a definition list. */
export function glossaryList(keys = Object.keys(GLOSSARY)) {
  return el('dl', { class: 'glossary' },
    ...keys.filter((k) => GLOSSARY[k]).map((k) => el('div', { class: 'g-item' },
      el('dt', { id: `g-${k}` }, GLOSSARY[k].word),
      el('dd', {}, GLOSSARY[k].def))));
}

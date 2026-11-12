// Flowmatter block shapes in the twin. A chamber whose spec names a block that Yuvraj's library has
// ("block": "culture_chamber", "y_junction", "serpentine_channel", ...) is drawn from that block's
// STL (cad/library/, listed in chip/components/catalogue.json by his paper-to-chip tool) instead of
// a plain box, so the chip looks like the Flowmatter design and the printed part.
//
// The STL is fitted to the chamber's box (x, y, depth) and turned (4 rotations x mirror) so its
// arms point at the chamber's channels, preferring the turn that stretches it least. Channels keep
// the twin's own geometry: a spec channel is a straight from -> to segment (that is also what the
// DXF and the flow model use), so a bend block can't be placed on it honestly. Ports have no
// block field and keep theirs too. Anything without a matching block falls back unchanged.
import * as THREE from 'three';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';
import { outlineOf } from './chip.js';

const ROOT = '../';                                        // the repo root, as main.js serves it
const CATALOGUE = `${ROOT}chip/components/catalogue.json`;
const loader = new STLLoader();
const geometries = new Map(); // part -> Promise<BufferGeometry>
let catalogue = null;         // Promise<Map part -> catalogue entry>

function loadCatalogue() {
  catalogue ??= fetch(CATALOGUE, { cache: 'no-store' })
    .then((r) => (r.ok ? r.json() : { parts: [] }))
    .then((c) => new Map(c.parts.map((p) => [p.part, p])))
    .catch(() => new Map());
  return catalogue;
}

function loadGeometry(entry) {
  if (!geometries.has(entry.part)) geometries.set(entry.part, loader.loadAsync(`${ROOT}${entry.stl}`));
  return geometries.get(entry.part);
}

// Unit directions (in the chamber's half-extent units) from its centre to each connected channel.
function channelDirections(chip, c) {
  const at = (id) => chip.spec.chambers.find((x) => x.id === id)?.center ?? chip.spec.ports.find((x) => x.id === id)?.position;
  const out = [];
  for (const ch of chip.spec.channels) {
    if (ch.from !== c.id && ch.to !== c.id) continue;
    const other = ch.path?.length ? (ch.from === c.id ? ch.path[1] ?? ch.path.at(-1) : ch.path.at(-2) ?? ch.path[0]) : at(ch.from === c.id ? ch.to : ch.from);
    if (!other) continue;
    const dx = (other[0] - c.center[0]) / (c.size[0] / 2);
    const dy = (other[1] - c.center[1]) / (c.size[1] / 2);
    const n = Math.hypot(dx, dy);
    if (n > 1e-9) out.push([dx / n, dy / n]);
  }
  return out;
}

// The best of 8 turns: arms toward the channels, least stretch. Returns { rot (quarter turns), mirror }.
function bestTurn(pos, size, c, dirs) {
  const [sx, sy] = size;
  const [cx, cy] = c.size;
  let best = { rot: 0, mirror: 1, score: -Infinity };
  for (let rot = 0; rot < 4; rot++) {
    for (const mirror of [1, -1]) {
      const swap = rot % 2 === 1;
      const [fx, fy] = swap ? [sy, sx] : [sx, sy];       // footprint after turning
      const stretch = Math.abs(Math.log((cx / cy) / (fx / fy)));
      let reach = 0;
      if (dirs.length) {
        for (const [dx, dy] of dirs) {
          let m = -Infinity;
          for (let i = 0; i < pos.length; i += 9) {          // one vertex per triangle is plenty
            let x = (pos[i] - sx / 2) * mirror;
            let y = pos[i + 1] - sy / 2;
            for (let r = 0; r < rot; r++) [x, y] = [-y, x];
            m = Math.max(m, (x / (fx / 2)) * dx + (y / (fy / 2)) * dy);
          }
          reach += m;
        }
        reach /= dirs.length;
      }
      const score = reach - 0.6 * stretch;
      if (score > best.score + 1e-6) best = { rot, mirror, score };
    }
  }
  return best;
}

// Draw the chip's chambers from their blocks where the library has them. Resolves to the ids drawn.
export async function applyBlocks(chip) {
  const wanted = chip.spec.chambers.filter((c) => c.block);
  if (!wanted.length) return [];
  const cat = await loadCatalogue();
  const drawn = [];
  await Promise.all(wanted.map(async (c) => {
    const entry = cat.get(c.block);
    const part = chip.parts.get(c.id);
    if (!entry || !part) return;
    let src;
    try { src = await loadGeometry(entry); } catch { return; }
    const geom = src.clone();
    const pos = geom.attributes.position.array;
    const [sx, sy, sz] = entry.size_mm;
    const turn = bestTurn(pos, entry.size_mm, c, channelDirections(chip, c));
    const [fx, fy] = turn.rot % 2 ? [sy, sx] : [sx, sy];
    const m = new THREE.Matrix4()
      .makeTranslation(c.center[0], c.center[1], c.center[2] - c.size[2] / 2)
      .multiply(new THREE.Matrix4().makeScale(c.size[0] / fx, c.size[1] / fy, c.size[2] / sz))
      .multiply(new THREE.Matrix4().makeRotationZ((turn.rot * Math.PI) / 2))
      .multiply(new THREE.Matrix4().makeScale(turn.mirror, 1, 1))
      .multiply(new THREE.Matrix4().makeTranslation(-sx / 2, -sy / 2, 0));
    geom.applyMatrix4(m);
    geom.computeVertexNormals();
    const mat = (part.material ?? part.box.material).clone();
    mat.opacity = Math.max(mat.opacity, 0.4);
    mat.side = THREE.DoubleSide;
    const mesh = new THREE.Mesh(geom, mat);
    mesh.renderOrder = part.box.renderOrder;
    mesh.name = `block:${c.id}:${c.block}`;
    mesh.add(outlineOf(geom, part.color));
    chip.group.add(mesh);
    part.box.visible = false;
    if (part.edges) part.edges.visible = false;
    part.block = { name: c.block, mesh };
    drawn.push(c.id);
  }));
  return drawn;
}

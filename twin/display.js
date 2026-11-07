// The printed DISPLAY MODEL of a chip (cad/display/<chip_id>/), for the
// "Display model" view. Geometry is the real chip x display_scale, in model mm.
import * as THREE from 'three';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';

const KIND_COLORS = {
  base: 0x8fa7c8,
  chamber: 0x5eead4,
  channel: 0x93c5fd,
  port_top: 0xff5fd2,
  port_bottom: 0xfb923c,
  label: 0x334155,
  label_text: 0xf8fafc,
};

// Look for cad/display/<chip_id>/manifest.json. Uses the folder listing from
// `python -m http.server`, so chips without a display model cause no 404s.
export async function findDisplayModel(chipId, root) {
  try {
    const listing = await fetch(`${root}cad/display/`, { cache: 'no-store' });
    if (!listing.ok) return null;
    const html = await listing.text();
    if (!html.includes(`href="${encodeURIComponent(chipId)}/"`)) return null;
    const res = await fetch(`${root}cad/display/${chipId}/manifest.json`, { cache: 'no-store' });
    return res.ok ? await res.json() : null;
  } catch {
    return null;
  }
}

export async function buildDisplayModel(manifest, root) {
  const group = new THREE.Group();
  group.name = `display:${manifest.chip_id}`;
  group.rotation.x = -Math.PI / 2; // model z-up -> three.js y-up
  const [ox, oy] = manifest.min_mm;
  const [sx, sy] = manifest.overall_mm;
  group.position.set(-(ox + sx / 2), 0, oy + sy / 2);
  const loader = new STLLoader();
  await Promise.all(
    manifest.parts.map(async (part) => {
      const geom = await loader.loadAsync(`${root}cad/display/${manifest.chip_id}/${part.file}`);
      geom.computeVertexNormals();
      const color = KIND_COLORS[part.kind] ?? 0xcccccc;
      const mesh = new THREE.Mesh(geom, new THREE.MeshStandardMaterial({
        color, roughness: 0.55, metalness: 0.05, emissive: color, emissiveIntensity: part.kind === 'base' ? 0.02 : 0.12,
      }));
      mesh.name = `display:${part.id}`;
      group.add(mesh);
    }),
  );
  return group;
}

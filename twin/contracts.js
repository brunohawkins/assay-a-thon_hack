// The shared numbers, read from contracts/ at load time - never copied into the twin.
//
// Mirrors operator/network.py's _parameter() / _port_hole_mm(): one source of truth per
// value, and no fallback to a literal. A literal copied in here is exactly how the twin's
// PORT_HOLE_MM stayed at 2.0 mm after the CAD generator started deriving 0.8 mm from
// contracts/blocks.json: every port-connected channel stopped matching its own DXF and the
// geometry fell back to a straight-line estimate without saying a word. Failing loudly on a
// missing contract is the point.
//
// A value the contracts do NOT carry (the dye-demo bench rate) is still named here, but with
// its real source recorded and listed in `missing`, so the UI can admit where it came from
// instead of passing it off as a contract number.

let loaded = null;

// name -> the contract it is read from, for the UI and for error messages.
//
// mu_pa_s is fluid.viscosity_37c_pa_s (0.8 mPa s), NOT fluid.viscosity_pa_s (1.0 mPa s).
// The contract keeps the two apart on purpose: 1.0 mPa s is water at ~20 C, the bench dye
// demo; 0.8 mPa s is culture medium at 37 C, which is what every chip in this twin runs.
// The twin's old MU_37C = 0.8e-3 literal was the right physics with no provenance, so
// reading the 37 C key leaves every number where it was and gives it a source.
export const SOURCES = {
  mu_pa_s: 'contracts/parameters.json fluid.viscosity_37c_pa_s',
  muWater20cPaS: 'contracts/parameters.json fluid.viscosity_pa_s',
  shearCeiling: 'contracts/parameters.json shear.culture_ceiling_dyn_cm2',
  portHoleMm: 'contracts/blocks.json fabrication_floors.min_port_mm',
  envelopeX: 'contracts/blocks.json envelope.max_x_mm',
  envelopeY: 'contracts/blocks.json envelope.max_y_mm',
  weirCrestUm: 'contracts/blocks.json retention.weir_geometry.crest_thickness_um',
  benchFlowUlMin: 'contracts/parameters.json dye_demo.bench_flow_ul_min',
  lanes: 'contracts/parameters.json barcoding.lanes',
};

// No contract parameter exists for these yet: the value is the Python operator's, and the
// twin says so rather than presenting it as a contract number.
const UNCONTRACTED = {
  benchFlowUlMin: { value: 50, from: 'operator/module1_operator.py BENCH_FLOW_UL_MIN' },
};

function number(obj, path, where) {
  let at = obj;
  for (const key of path) {
    if (at == null || !(key in at)) throw new Error(`${where} has no ${path.join('.')}`);
    at = at[key];
  }
  const v = Number(at);
  if (!Number.isFinite(v)) throw new Error(`${where} ${path.join('.')} is not a number (${at})`);
  return v;
}

// A positive number, or null when the entry is missing or empty (for values that are not physics).
function optional(params, name) {
  const v = params[name]?.value;
  return v != null && Number(v) > 0 ? Number(v) : null;
}

async function json(url) {
  const res = await fetch(url, { cache: 'no-store' });
  if (!res.ok) throw new Error(`Could not read ${url} (HTTP ${res.status}).`);
  return res.json();
}

// Reads contracts/parameters.json and contracts/blocks.json. Throws if either is missing or
// is missing a value the twin needs: the twin must not run on stand-in physics.
export async function loadContracts(root) {
  if (loaded) return loaded;
  const [params, blocks] = await Promise.all([
    json(`${root}contracts/parameters.json`),
    json(`${root}contracts/blocks.json`),
  ]);
  const p = params.parameters ?? {};
  const param = (name) => number(p, [name, 'value'], 'contracts/parameters.json');
  const values = {
    mu_pa_s: param('fluid.viscosity_37c_pa_s'),
    muWater20cPaS: param('fluid.viscosity_pa_s'),
    shearCeiling: param('shear.culture_ceiling_dyn_cm2'),
    portHoleMm: number(blocks, ['fabrication_floors', 'min_port_mm'], 'contracts/blocks.json'),
    envelopeX: number(blocks, ['envelope', 'max_x_mm'], 'contracts/blocks.json'),
    envelopeY: number(blocks, ['envelope', 'max_y_mm'], 'contracts/blocks.json'),
    // A weir's in-plane crest thickness when a chip does not give its own (operator/network.py
    // _weir_crest_default_um reads the same entry).
    weirCrestUm: number(blocks, ['retention', 'weir_geometry', 'crest_thickness_um'], 'contracts/blocks.json'),
    // How many separately labelled cultures one Module 1 run carries (one lane label per lineage).
    // Not physics: only the presenter's scale step reads it, so a missing entry leaves it null
    // (and that step leaves its "today" row out) instead of stopping the twin.
    lanes: optional(p, 'barcoding.lanes'),
  };
  const missing = [];
  for (const [name, { value, from }] of Object.entries(UNCONTRACTED)) {
    const key = SOURCES[name].split(' ').pop();
    if (p[key] && Number.isFinite(Number(p[key].value))) values[name] = Number(p[key].value);
    else {
      values[name] = value;
      missing.push({ name, value, wanted: SOURCES[name], from });
    }
  }
  loaded = { ...values, missing };
  return loaded;
}

export function contracts() {
  if (!loaded) throw new Error('loadContracts() has not finished: no contract values yet.');
  return loaded;
}

// Port bore x bore through the lid, in µL. The bore is the contract's; the lid thickness is
// this chip's own top layer, so it is never a second copy of "3 mm".
export function portHoleUl(spec) {
  const bore = contracts().portHoleMm;
  const lid = Number(spec?.layers?.at(-1)?.thickness_mm);
  return bore * bore * (Number.isFinite(lid) ? lid : 0);
}

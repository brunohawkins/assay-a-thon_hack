"""Design checks for chip specs, at REAL scale.

    python tools/check_chip_spec.py                     # every spec in chip/ and cad/*_spec.json
    python tools/check_chip_spec.py cad/m3_wash_spec.json --protocol protocols/m3_wash.json

tools/validate.py checks that files follow the contracts; this checks that the design
works for capsules. Capsule diameter, swell factor and nozzle width come from the spec
when it states them, else from contracts/parameters.json. Protocols are the ones with a
run on the chip (runs/), plus any given with --protocol.

The build envelope and the port rule come from the chip's optional top-level "scale"
(contracts/schemas/chip_spec.schema.json), read out of contracts/blocks.json:
  demo (the default, so every chip that does not say otherwise)  -> envelope, 30 x 20 mm,
      and fabrication_floors.max_ports_per_chip (6), which blocks.json says plainly is a
      demo-envelope limit rather than a process floor;
  target -> target_envelope, 127.76 x 85.48 mm (ANSI/SLAS), where the port limit lifts and
      becomes how many ports fit that area at fabrication_floors.min_port_pitch_mm.
Nothing else changes with scale: one fluidic layer, one etch depth, 0.3 mm max depth, the
in-plane floors and the block library apply identically, so a demo chip is checked exactly
as strictly as it always was.

Errors (exit code 1):
  - trap gap or retention opening >= 0.4 x capsule diameter
  - capsule-carrying channel, or capsule-passable outlet, narrower than 1.5 x capsule diameter
    (a channel's "carries" picks the rule: capsules/droplets 1.5 x capsule diameter; cells the
    cell-outlet minimum, dissociation.outlet_min_width_um; liquid, oil, signals none)
  - chamber lower than the swollen capsule diameter where culture happens (capsule chips; Modules 1 and 1b hold cells, not capsules)
  - cell filter gap not smaller than the encapsulation nozzle (cell_filter.gap_um, and every
    filter chamber whose purpose is cells), including the nozzle of a chip DOWNSTREAM across
    a pipeline hand-off (Module 1b's filter vs Module 2's nozzle); a pass prints an OK line
  - thermal zone (or protocol hold_temp) above the chip material's max working temperature
  - two channels that CROSS in plan view. We print ONE fluidic layer, so a crossing is not a
    bridge, it is a short circuit between whatever those two lines serve. Every channel is
    checked: the ones that declare a "path" along it, the rest along the straight clipped
    centreline cad/make_train_cad.py actually cuts (imported from cad/make_figures.py, so
    the drawing, the cut path and this check cannot disagree). This used to be a warning that
    only looked at channels with an explicit path, which is 8 of 16 specs, so on the other
    half the rule returned an empty list and passed trivially.
  - port bore below fabrication_floors.min_port_mm: tubing cannot be connected to it
  - more than one layer at the fluidic depth, or any on-chip valve: both need a second layer
  - port overlap: a port's hole (a square of its width_um, else contracts/blocks.json's
    min_port_mm bore) must open onto the cut channel or chamber under it by at least 1 mm2,
    or by a quarter of the hole's own area when the hole is smaller than 2 x 2 mm (a 0.8 mm
    bore holds 0.64 mm2 in all, so 1 mm2 can't be met; a quarter is what 1 mm2 is of the
    2 mm holes the rule was written for). The cut area is the chip's exported DXF
    (cad/exports/<chip>/<chip>.dxf, CHANNELS and CHAMBERS) when there is one, else the spec.
Warnings (do not fail CI, so only judgement calls live here):
  - fabricated feature smaller than the fabrication minimum in cad/fabrication.json
    (fabrication limits never block the design; while they're TODO, one summary warning).
    A WEIR's opening_um is excluded: it is a gap in Z between the crest and the lid, and the
    in-plane floor limits width only (see the schema's retention description), so comparing
    it with the in-plane minimum was a false positive on every weir chip.
  - barcode collision rate above 5%, over the RANDOM per-cell barcode pool (4^random_bases);
    lanes and sublibraries are demultiplexing metadata and are never the barcode space
  - reagent whose compatibility with the chip material is TODO or incompatible
  - unknown material, channel shallower than a capsule, outlet without a width
  - ports closer than min_port_pitch_mm (set by the connector footprint, not the process)
  - a mesh pore below the in-plane floor (legal as a bought insert, not if we must make it)
TODO lines: fabrication limits still unknown; parts marked fabricable: false (by design).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))
sys.path.insert(0, str(ROOT / "cad"))

import commands  # noqa: E402
import schema_io  # noqa: E402
import sizing  # noqa: E402

# Channel geometry comes from the drawing tool, which gets it from the CAD tool: one
# computation of "where does this channel actually run", not three. make_figures.segments_of
# clips each channel to the boxes at its ends exactly as cad/make_train_cad.py cuts it.
try:
    import make_figures as figures  # noqa: E402
except Exception:                   # pragma: no cover - cad/ missing or unimportable
    figures = None

TRAP_MAX_FACTOR = 0.4
CHANNEL_MIN_FACTOR = 1.5
PORT_OVERLAP_MM2 = 1.0          # the rule as given, for a 2 x 2 mm hole
PORT_OVERLAP_SMALL_FRACTION = 0.25   # the same rule scaled to holes under 4 mm2
CULTURE_WORDS = ("culture", "media", "medium")


class Findings:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.todo: list[str] = []      # fabrication / compatibility TODO warnings
        self.ok: list[str] = []        # rules worth confirming out loud (e.g. across chips)

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def param(params: dict, key: str) -> float:
    return params["parameters"][key]["value"]


def resolve_materials(chip: dict, materials: dict) -> tuple[list[tuple[str, dict]], list[str]]:
    """Materials of the chip's layers: ([(id, material)], [unknown names])."""
    found, unknown = [], []
    for layer in chip.get("layers", []):
        name = str(layer.get("material", "")).strip().lower()
        hit = next(((mid, m) for mid, m in materials.items()
                    if name == mid or name in [a.lower() for a in m.get("aliases", [])]), None)
        if hit and hit[0] not in [f[0] for f in found]:
            found.append(hit)
        elif not hit and name not in unknown:
            unknown.append(name)
    return found, unknown


def paired_protocols(chip: dict, extra: list[Path]) -> list[tuple[Path, dict]]:
    """Protocols with a run on this chip, plus explicit ones."""
    by_id = {}
    for path in sorted((ROOT / "protocols").glob("*.json")):
        try:
            by_id[schema_io.load_json(path).get("id")] = path
        except ValueError:
            continue
    paths = []
    for run_path in sorted((ROOT / "runs").glob("*.json")):
        if run_path.name == "index.json":
            continue
        try:
            run = schema_io.load_json(run_path)
        except ValueError:
            continue
        if run.get("chip_id") == chip.get("id") and run.get("protocol_id") in by_id:
            paths.append(by_id[run["protocol_id"]])
    paths += [Path(p) for p in extra]
    out, seen = [], set()
    for p in paths:
        if p.resolve() in seen:
            continue
        seen.add(p.resolve())
        out.append((p, schema_io.load_json(p)))
    return out


def downstream_nozzles(chip: dict) -> list[tuple[float, str, str]]:
    """Encapsulation nozzles fed by this chip through pipeline hand-offs: [(nozzle_um, chip_id, handoff_id)].

    Deduplicated, because protocols/ now holds several pipeline files and pipeline_mvp.json is a
    COPY of the chosen assay variant (tools/choose_assay.py): the same hand-off is read twice and
    would otherwise be reported twice, which is not a second constraint.
    """
    out = []
    seen = set()
    my_ports = {p["id"] for p in chip.get("ports", [])}
    for _, pipe in schema_io.pipelines():
        chips = {}
        for m in pipe.get("modules", []):
            chip_path = schema_io.pipeline_chip(m)[0]
            if not chip_path:
                continue  # a missing module (no chip yet)
            try:
                chips[m["module"]] = schema_io.load_json(ROOT / chip_path)
            except (ValueError, FileNotFoundError):
                continue
        for h in pipe.get("handoffs", []):
            src, dst = h.get("from", {}), h.get("to", {})
            down = chips.get(dst.get("module"))
            if src.get("port") in my_ports and chips.get(src.get("module"), {}).get("id") == chip.get("id") and down:
                nozzle = (down.get("encapsulation") or {}).get("nozzle_um")
                if nozzle and (nozzle, down["id"], h["id"]) not in seen:
                    seen.add((nozzle, down["id"], h["id"]))
                    out.append((nozzle, down["id"], h["id"]))
    return out


def check(chip: dict, protocols: list[tuple[Path, dict]], params: dict, materials: dict, fab: dict,
          downstream: list[tuple[float, str, str]] | None = None, blocks: dict | None = None) -> Findings:
    f = Findings()
    caps = chip.get("capsule", {})
    d = caps.get("diameter_um", param(params, "capsule.diameter_um"))
    swollen = d * caps.get("swell_factor", param(params, "capsule.swell_factor"))
    ports = {p["id"]: p for p in chip.get("ports", [])}
    chambers = {c["id"]: c for c in chip.get("chambers", [])}

    # --- retention: trap gap / mesh / pillars / weir ------------------------------------
    limit = TRAP_MAX_FACTOR * d
    if "gap_um" in chip.get("trap", {}) and chip["trap"]["gap_um"] >= limit:
        f.error(f"trap.gap_um {chip['trap']['gap_um']:g} um >= 0.4 x capsule diameter ({limit:g} um): capsules can slip through")
    for c in chambers.values():
        ret = c.get("retention")
        if ret and ret["opening_um"] >= limit:
            f.error(f"chamber '{c['id']}': {ret['type']} opening {ret['opening_um']:g} um >= 0.4 x capsule diameter "
                    f"({limit:g} um): capsules can slip through")
        elif not ret and c.get("mesh_pore_um") and c["mesh_pore_um"] >= limit:
            f.error(f"chamber '{c['id']}': mesh_pore_um {c['mesh_pore_um']:g} >= 0.4 x capsule diameter ({limit:g} um)")

    # --- capsule-carrying channels and outlets ------------------------------------------
    min_w = CHANNEL_MIN_FACTOR * d
    cell_min = param(params, "dissociation.outlet_min_width_um")
    for ch in chip.get("channels", []):
        carries = ch.get("carries", "capsules")
        if carries == "cells":
            if ch["width_mm"] * 1000 < cell_min:
                f.error(f"channel '{ch['id']}' carries cells but is {ch['width_mm'] * 1000:g} um wide, < {cell_min:g} um "
                        f"(wide enough for single cells and small clumps)")
            continue
        if carries in ("liquid", "oil", "signals") or ch.get("capsule_passable") is False:
            continue  # no particles to fit, or deliberately too small (e.g. a microgroove barrier)
        ends = [ports.get(ch["from"]), ports.get(ch["to"])]
        if any(p is not None and p.get("capsule_passable") is False for p in ends):
            continue  # behind a trap (e.g. waste below a mesh): no capsules here
        width = ch["width_mm"] * 1000
        if width < min_w:
            f.error(f"channel '{ch['id']}' carries capsules but is {width:g} um wide, < 1.5 x capsule diameter ({min_w:g} um)")
        if ch["depth_mm"] * 1000 < d:
            f.warn(f"channel '{ch['id']}' is {ch['depth_mm'] * 1000:g} um deep, shallower than a {d:g} um capsule")
    for p in ports.values():
        if p.get("type") == "outlet" and p.get("capsule_passable") is True:
            if "width_um" not in p:
                f.warn(f"outlet '{p['id']}' passes capsules but has no width_um, so it can't be checked")
            elif p["width_um"] < min_w:
                f.error(f"outlet '{p['id']}' passes capsules but is {p['width_um']:g} um wide, < 1.5 x capsule diameter ({min_w:g} um)")

    # --- culture chambers tall enough for swollen capsules ------------------------------
    culture = {}
    for z in chip.get("thermal_zones", []):
        if str(z.get("purpose", "")).lower() == "culture":
            for cid in z.get("chambers", []):
                culture[cid] = f"thermal zone '{z['id']}'"
    for path, proto in protocols:
        for s in proto.get("steps", []):
            names = [str(x).lower() for x in [s.get("reagent")] + list(s.get("reagents") or []) if x]
            hit = next((n for n in names if any(w in n for w in CULTURE_WORDS)), None)
            if hit:
                for t in commands.step_targets(s):
                    culture.setdefault(t, f"{rel(path)} step {s['step']} ({hit})")
    holds_capsules = "capsule" in chip or not chip.get("module")   # module chips say so; older chips are all capsule chips
    for cid, why in culture.items() if holds_capsules else []:
        c = chambers.get(cid)
        if not c:
            continue
        height = c.get("height_um", c["size"][2] * 1000)
        if height < swollen:
            f.error(f"chamber '{cid}' is {height:g} um high but culture happens there ({why}); "
                    f"swollen capsules are {swollen:g} um")

    # --- cell filter vs nozzle ----------------------------------------------------------
    nozzle = chip.get("encapsulation", {}).get("nozzle_um", param(params, "encapsulation.nozzle_um"))
    gap = chip.get("cell_filter", {}).get("gap_um")
    if gap is not None and gap >= nozzle:
        f.error(f"cell_filter.gap_um {gap:g} >= encapsulation nozzle {nozzle:g} um: clumps can reach the nozzle")
    cell_filters = [(c["id"], c["filter"]["gap_um"]) for c in chambers.values() if (c.get("filter") or {}).get("purpose") == "cells"]
    for fid, fgap in cell_filters:
        if fgap >= nozzle:
            f.error(f"filter '{fid}' gap {fgap:g} um >= encapsulation nozzle {nozzle:g} um: cell clumps "
                    f"that pass it can block the nozzle")
    for d_nozzle, d_chip, handoff in downstream or []:
        for fid, fgap in cell_filters or ([("cell_filter", gap)] if gap is not None else []):
            if fgap >= d_nozzle:
                f.error(f"filter '{fid}' gap {fgap:g} um >= the {d_nozzle:g} um nozzle of {d_chip} downstream "
                        f"(hand-off {handoff}): cell clumps that pass it can block that nozzle")
            else:
                f.ok.append(f"filter '{fid}' gap {fgap:g} um < the {d_nozzle:g} um nozzle of {d_chip} downstream "
                            f"(hand-off {handoff})")

    # --- temperature vs material --------------------------------------------------------
    mats, unknown = resolve_materials(chip, materials)
    for name in unknown:
        f.warn(f"material '{name}' is not in contracts/materials.json: temperature and chemical compatibility can't be checked")
    if mats:
        mat_id, mat = min(mats, key=lambda m: m[1]["max_working_temp_c"])
        tmax = mat["max_working_temp_c"]
        for z in chip.get("thermal_zones", []):
            if z["max_temp_c"] > tmax:
                f.error(f"thermal zone '{z['id']}' reaches {z['max_temp_c']:g} C, above {mat['name']}'s max working "
                        f"temperature ({tmax:g} C)")
        for path, proto in protocols:
            for s in proto.get("steps", []):
                if s.get("operation") == "hold_temp" and s.get("temp_c") is not None and s["temp_c"] > tmax:
                    f.error(f"{rel(path)} step {s['step']} holds '{s['target']}' at {s['temp_c']:g} C, above "
                            f"{mat['name']}'s max working temperature ({tmax:g} C)")

    # --- fabrication minimums (warnings only) -------------------------------------------
    made_here = [ch for ch in chip.get("channels", []) if ch.get("fabricable", True)]
    not_here = [ch for ch in chip.get("channels", []) if not ch.get("fabricable", True)]
    if not_here:
        f.todo.append("not fabricable here (by design): " + ", ".join(
            f"{ch['id']} ({ch['width_mm'] * 1000:g} x {ch['depth_mm'] * 1000:g} um)" for ch in not_here))
    # IN-PLANE ONLY. The fabrication minimum is a lateral limit: it is what the printer can
    # resolve in x-y and what the embossing tool can replicate as a standing feature. DEPTH
    # is set in z by the mould relief or the layer stack and is free below it - that is the
    # whole reason a depth weir can retain a 70 um capsule when no printable in-plane gap
    # can. Checking depth against the lateral floor produced ~90 false warnings.
    features = [(f"channel '{ch['id']}' width", ch["width_mm"] * 1000) for ch in made_here]
    features += [(f"port '{p['id']}' width", p["width_um"]) for p in ports.values() if "width_um" in p]
    # chamber height is z, like channel depth: not a lateral feature, so not checked here.
    # NOR IS A WEIR'S GAP. It is the clearance in Z between the crest and the lid (see the
    # schema's retention description and blocks.json retention.weir_geometry): the in-plane
    # floor limits width only, which is the whole reason a 25 um weir gap is buildable when
    # no printable in-plane gap can retain a 70 um capsule. Comparing it with the in-plane
    # minimum warned on every weir chip in the repo for a feature that is legal by design.
    # A mesh or pillar opening IS in plane, so those stay.
    features += [(f"chamber '{c['id']}' {c['retention']['type']} opening", c["retention"]["opening_um"])
                 for c in chambers.values()
                 if c.get("retention", {}).get("source") == "fabricated"
                 and c["retention"].get("type") != "weir"]
    features += [(f"chamber '{c['id']}' weir crest thickness", c["retention"]["crest_thickness_um"])
                 for c in chambers.values() if _num((c.get("retention") or {}).get("crest_thickness_um"))]
    known = [v for v in (fab.get("laser_kerf_um"), fab.get("min_embossed_feature_um")) if isinstance(v, (int, float))]
    if features and not known:
        smallest = min(features, key=lambda x: x[1])
        f.todo.append(f"fabrication TODO: {len(features)} fabricated features can't be checked because "
                      f"cad/fabrication.json has no laser_kerf_um or min_embossed_feature_um yet "
                      f"(smallest: {smallest[0]} = {smallest[1]:g} um)")
    elif known:
        fab_min = max(known)
        for label, value in features:
            if value < fab_min:
                f.warn(f"{label} is {value:g} um, below the fabrication minimum ({fab_min:g} um); fine for the design, "
                       f"but we can't make it ourselves")

    # --- barcode collisions -------------------------------------------------------------
    bc = chip.get("barcoding")
    if bc:
        inputs = {"capsule_diameter_um": d}
        if "count" in caps:
            inputs["capsule_count"] = caps["count"]
        # Split-and-pool is dropped. A chip may still carry the old rounds/chambers_per_round
        # keys, but they no longer describe anything, so they are ignored rather than passed on.
        #
        # THE BARCODE SPACE IS THE RANDOM POOL, NOT lanes x sublibraries. Each capsule gets one
        # barcode of barcoding.random_bases random bases (4^16 = about 4.3e9), amplified clonally
        # inside that capsule's own oil droplet; one capsule holds one cell, so the assay IS
        # single-cell. lanes and sublibraries say which lineage and which sublibrary a read came
        # from - demultiplexing metadata - and counting them as the barcode space (about twelve
        # groups) is the arithmetic that made this repo wrongly call the data lane-level.
        # Both random-barcode fields were missing from the chip schema, so no chip could supply
        # them and this rule could never fire; they are optional now and fall back to
        # contracts/parameters.json through operator/sizing.py.
        for key, inp in (("lanes", "barcoding_lanes"), ("sublibraries", "sublibraries"),
                         ("random_bases", "barcode_random_bases"), ("loading_mean", "barcode_loading_mean")):
            if key in bc:
                inputs[inp] = bc[key]
        r = sizing.compute(inputs)
        eff = {**sizing.DEFAULTS, **inputs}       # what sizing actually used, after its defaults
        if r["barcode_collision_fraction"] > sizing.COLLISION_WARN:
            f.warn(f"barcode collisions: about {r['barcode_collision_fraction']:.1%} of the ~{r['cells']:,.0f} cells "
                   f"in {r['capsule_count']:,.0f} capsules share their random barcode with another cell, against a "
                   f"pool of {r['barcode_pool']:,} ({eff['barcode_random_bases']:g} random bases). Shorten the run "
                   f"or lengthen the barcode; lanes x sublibraries is demultiplexing, not resolution")
        if r.get("barcode_absent_fraction", 0) > 0.5:
            f.warn(f"barcode loading: at a Poisson mean of {eff['barcode_loading_mean']:g} barcodes per droplet, "
                   f"{r['barcode_absent_fraction']:.0%} of droplets get none and those cells are lost")

    # --- channels that cross in plan view (every channel, path or not) -------------------
    for a, b, (x, y) in crossings(chip):
        f.error(f"channels '{a}' and '{b}' cross at ({x:g}, {y:g}): we print ONE fluidic layer, so that is not "
                f"a bridge but a short circuit between what those two lines serve; reroute one of them")

    # --- reagent compatibility ----------------------------------------------------------
    reagents = sorted({r for _, proto in protocols for s in proto.get("steps", [])
                       for r in [s.get("reagent")] + list(s.get("reagents") or []) + list(s.get("factors") or []) if r})
    for mat_id, mat in mats:
        compat = mat.get("chemical_compatibility", {})
        todo = [r for r in reagents if compat.get(r, "TODO") == "TODO"]
        bad = [r for r in reagents if compat.get(r) == "incompatible"]
        if todo:
            f.todo.append(f"compatibility TODO: {mat['name']} with {', '.join(todo)}")
        if bad:
            f.warn(f"{mat['name']} is incompatible with {', '.join(bad)}")
    if blocks:
        check_blocks(chip, blocks, f)
    check_port_overlap(chip, f, blocks)
    return f


# --- port overlap: does each lid hole open onto the cut area under it? ---------------------

def _dxf_outlines(chip_id: str) -> tuple[list[list[tuple[float, float]]], str] | tuple[None, None]:
    """Cut outlines (CHANNELS and CHAMBERS layers) of the chip's exported DXF, if there is one.
    cad/make_train_cad.py writes each outline as 4 consecutive LINE entities."""
    for path in (ROOT / "cad" / "exports" / chip_id / f"{chip_id}.dxf", ROOT / "cad" / "exports" / f"{chip_id}.dxf"):
        if not path.is_file():
            continue
        tokens = path.read_text(errors="replace").split("\n")
        starts, cur, layer = [], None, None
        for code, val in zip(tokens[0::2], tokens[1::2]):
            code, val = code.strip(), val.strip()
            if code == "0":
                if cur is not None and layer in ("CHANNELS", "CHAMBERS") and "10" in cur and "20" in cur:
                    starts.append((cur["10"], cur["20"]))
                cur, layer = ({} if val == "LINE" else None), None
            elif cur is not None and code == "8":
                layer = val
            elif cur is not None and code in ("10", "20"):
                cur[code] = float(val)
        polys = [starts[i:i + 4] for i in range(0, len(starts) - 3, 4)]
        return polys, path.relative_to(ROOT).as_posix()
    return None, None


def _spec_outlines(chip: dict) -> list[list[tuple[float, float]]]:
    """Chambers as boxes, channels as rectangles along their path (else straight between centres)."""
    at = {c["id"]: c["center"][:2] for c in chip.get("chambers", [])} | {p["id"]: p["position"][:2] for p in chip.get("ports", [])}
    polys = [[(c["center"][0] + sx * c["size"][0] / 2, c["center"][1] + sy * c["size"][1] / 2)
              for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))] for c in chip.get("chambers", [])]
    for ch in chip.get("channels", []):
        pts = ch.get("path") or [at.get(ch["from"]), at.get(ch["to"])]
        if any(p is None for p in pts):
            continue
        for a, b in zip(pts, pts[1:]):
            dx, dy = b[0] - a[0], b[1] - a[1]
            n = math.hypot(dx, dy) or 1.0
            px, py = -dy / n * ch["width_mm"] / 2, dx / n * ch["width_mm"] / 2
            polys.append([(a[0] + px, a[1] + py), (b[0] + px, b[1] + py), (b[0] - px, b[1] - py), (a[0] - px, a[1] - py)])
    return polys


def _inside(x: float, y: float, poly) -> bool:
    hit = False
    for i in range(len(poly)):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % len(poly)]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            hit = not hit
    return hit


def port_overlaps(chip: dict, blocks: dict | None = None, n: int = 50, polys: list | None = None,
                  source: str | None = None) -> list[dict]:
    """For every fluid port: its hole side, the cut area under it (mm2), what that must be, and the source.
    polys/source: measure against these outlines instead (tools/check_asbuilt.py passes an as-built file's)."""
    if blocks is None:
        try:
            blocks = json.loads((ROOT / "contracts" / "blocks.json").read_text())
        except (OSError, ValueError):
            blocks = {}
    floor = (blocks.get("fabrication_floors") or {}).get("min_port_mm", 2.0)
    if polys is None:
        polys, source = _dxf_outlines(chip.get("id", ""))
        if not polys:
            polys, source = _spec_outlines(chip), "the spec (no exported DXF)"
    out = []
    for p in chip.get("ports", []):
        if p.get("type") == "control":
            continue
        side = (p.get("width_um") or floor * 1000) / 1000
        x0, y0 = p["position"][0] - side / 2, p["position"][1] - side / 2
        near = [q for q in polys if min(v[0] for v in q) <= x0 + side and max(v[0] for v in q) >= x0
                and min(v[1] for v in q) <= y0 + side and max(v[1] for v in q) >= y0]
        hits = sum(any(_inside(x0 + (i + 0.5) * side / n, y0 + (j + 0.5) * side / n, q) for q in near)
                   for i in range(n) for j in range(n)) if near else 0
        area = side * side
        need = PORT_OVERLAP_MM2 if area >= 4.0 else PORT_OVERLAP_SMALL_FRACTION * area
        out.append({"port": p["id"], "side_mm": side, "overlap_mm2": hits / (n * n) * area, "need_mm2": need, "source": source,
                    "tolerance_mm2": area / n})  # one row of samples
    return out


def check_port_overlap(chip: dict, f: "Findings", blocks: dict | None = None) -> None:
    for r in port_overlaps(chip, blocks):
        msg = (f"port overlap: '{r['port']}' {r['side_mm']:g} mm hole opens onto {r['overlap_mm2']:.2f} mm2 of cut channel/chamber "
               f"(needs >= {r['need_mm2']:.2f} mm2; geometry from {r['source']})")
        if r["overlap_mm2"] + r["tolerance_mm2"] < r["need_mm2"]:
            f.error(msg + ": run the channel across the hole or add a port pad under it")
        else:
            f.ok.append(msg)


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def scale_of(chip: dict) -> str:
    """The chip's build scale: "demo" (the schema default) or "target"."""
    return str(chip.get("scale") or "demo")


def envelope_for(chip: dict, blocks: dict) -> tuple[dict, str]:
    """({max_x_mm, max_y_mm, max_depth_mm, layers}, label) for this chip's scale.

    Both envelopes live in contracts/blocks.json: `envelope` (demo, 30 x 20 mm) and
    `target_envelope` (the ANSI/SLAS 127.76 x 85.48 mm plate), which until now no code read.
    Only the x/y limits differ: depth and the layer count are the same at both scales.
    """
    demo = blocks.get("envelope", {})
    if scale_of(chip) != "target":
        return demo, f"demo build envelope ({demo.get('max_x_mm', '?')} x {demo.get('max_y_mm', '?')} mm)"
    tgt = blocks.get("target_envelope", {})
    env = {"max_x_mm": tgt.get("x_mm"), "max_y_mm": tgt.get("y_mm"),
           "max_depth_mm": tgt.get("depth_mm", demo.get("max_depth_mm")),
           "layers": tgt.get("layers", demo.get("layers"))}
    return env, f"target envelope ({tgt.get('standard', 'ANSI/SLAS')}, {env['max_x_mm']} x {env['max_y_mm']} mm)"


def max_ports_for(chip: dict, blocks: dict, env: dict) -> tuple[float | None, str]:
    """(limit, why) on port count for this chip's scale.

    DEMO: fabrication_floors.max_ports_per_chip (6), unchanged - blocks.json says in two
    places that this is what 30 x 20 mm fits at the 5 mm pitch, a DEMO ENVELOPE limit and
    not a process floor.
    TARGET: the limit lifts and becomes geometric - how many 5 mm-pitch port positions the
    board area holds - because nothing about printing or embossing caps port count. The
    pairwise pitch check below is what actually keeps ports apart at both scales.
    """
    floors = blocks.get("fabrication_floors", {})
    cap = floors.get("max_ports_per_chip")
    if scale_of(chip) != "target":
        return cap, "max ports per chip (a demo-envelope limit at the 5 mm port pitch)"
    pitch = floors.get("min_port_pitch_mm")
    if not (_num(pitch) and pitch > 0 and _num(env.get("max_x_mm")) and _num(env.get("max_y_mm"))):
        return None, "no port limit at target scale"
    grid = int(env["max_x_mm"] // pitch) * int(env["max_y_mm"] // pitch)
    return grid, (f"port positions an {env['max_x_mm']:g} x {env['max_y_mm']:g} mm board holds on a "
                  f"{pitch:g} mm grid (the 6-port demo limit does not apply at target scale)")


def check_blocks(chip: dict, blocks: dict, f: Findings) -> None:
    """Fabrication envelope, floors and CAD block library from contracts/blocks.json."""
    env, env_label = envelope_for(chip, blocks)
    floors = blocks.get("fabrication_floors", {})
    library = set(blocks.get("basic", {})) | set(blocks.get("advanced", {}))
    chambers = [c for c in chip.get("chambers", []) if isinstance(c, dict)]
    channels = [c for c in chip.get("channels", []) if isinstance(c, dict)]
    ports = [p for p in chip.get("ports", []) if isinstance(p, dict)]

    fp = chip.get("footprint") or {}
    for axis, key in (("x", "max_x_mm"), ("y", "max_y_mm")):
        if _num(fp.get(axis)) and _num(env.get(key)) and fp[axis] > env[key]:
            f.error(f"footprint {axis} {fp[axis]:g} mm > {env_label} {key} {env[key]:g} mm")

    max_depth = env.get("max_depth_mm")
    chamber_depths = []
    for c in chambers:
        size = c.get("size")
        if isinstance(size, list) and len(size) > 2 and _num(size[2]):
            chamber_depths.append(size[2])
            if _num(max_depth) and size[2] > max_depth:
                f.error(f"chamber '{c.get('id')}' is {size[2]:g} mm deep, > max depth {max_depth:g} mm")
    for ch in channels:
        if _num(max_depth) and _num(ch.get("depth_mm")) and ch["depth_mm"] > max_depth:
            f.error(f"channel '{ch.get('id')}' is {ch['depth_mm']:g} mm deep, > max depth {max_depth:g} mm")

    for kind, items in (("chamber", chambers), ("channel", channels)):
        for item in items:
            if "block" not in item:
                f.warn(f"{kind} '{item.get('id')}' has no \"block\" field: we cannot fabricate what we cannot name")
            elif item["block"] not in library:
                f.error(f"{kind} '{item.get('id')}' declares block '{item['block']}', which is not in the CAD block "
                        f"library (contracts/blocks.json basic/advanced)")

    max_ports, why_ports = max_ports_for(chip, blocks, env)
    if _num(max_ports) and len(ports) > max_ports:
        f.error(f"{len(ports)} ports > max {max_ports:g}: {why_ports}")

    # Port BORE is a process floor at every scale: we drill min_port_mm holes and tubing has
    # to go into them. Promoted from nothing at all - no rule here read min_port_mm, so the
    # only thing that ever looked at a port width was the fabrication-TODO comparison, which
    # never fails CI.
    min_port = floors.get("min_port_mm")
    if _num(min_port):
        for p in ports:
            if _num(p.get("width_um")) and p["width_um"] < min_port * 1000 - 1e-9:
                f.error(f"port '{p.get('id')}' bore is {p['width_um']:g} um, below the "
                        f"{min_port * 1000:g} um minimum port we can drill and connect tubing to")

    pitch = floors.get("min_port_pitch_mm")
    if _num(pitch):
        placed = [p for p in ports if isinstance(p.get("position"), list) and len(p["position"]) >= 2
                  and _num(p["position"][0]) and _num(p["position"][1])]
        for i, a in enumerate(placed):
            for b in placed[i + 1:]:
                dist = ((a["position"][0] - b["position"][0]) ** 2 + (a["position"][1] - b["position"][1]) ** 2) ** 0.5
                if dist < pitch - 1e-9:
                    f.warn(f"ports '{a.get('id')}' and '{b.get('id')}' are {dist:.2f} mm apart, closer than the "
                           f"{pitch:g} mm minimum port pitch")

    min_open = floors.get("min_open_channel_mm")
    if _num(min_open):
        for ch in channels:
            if _num(ch.get("width_mm")) and ch["width_mm"] < min_open:
                f.error(f"channel '{ch.get('id')}' is {ch['width_mm']:g} mm wide, below the {min_open:g} mm "
                        f"minimum open channel")

    # ONE FLUIDIC LAYER is the hardest rule in contracts/blocks.json (its whole "excluded"
    # section exists because of it), so a second one is an error, not a warning.
    fluidic = [la for la in chip.get("layers", []) if isinstance(la, dict) and _num(la.get("thickness_mm"))
               and any(abs(la["thickness_mm"] - d) < 1e-9 for d in chamber_depths)]
    layers_allowed = env.get("layers", 1)
    if _num(layers_allowed) and len(fluidic) > layers_allowed:
        f.error(f"{len(fluidic)} layers ({', '.join(str(la.get('id')) for la in fluidic)}) match a chamber depth, "
                f"but we print {layers_allowed:g} fluidic layer: a second one is a second process we do not have")
    if chip.get("valves"):
        f.error(f"{len(chip['valves'])} on-chip valve(s) ({', '.join(str(v.get('id')) for v in chip['valves'])}): "
                f"a pneumatic valve needs a control layer above the fluidic one, and we print one layer "
                f"(contracts/blocks.json excluded.multilayer)")

    if _num(min_open):
        for c in chambers:
            pore = c.get("mesh_pore_um")
            if _num(pore) and pore < min_open * 1000:
                f.warn(f"chamber '{c.get('id')}' declares mesh_pore_um {pore:g}, below the {min_open * 1000:g} um we can "
                       f"print: replace it with pillar_array, trap_chamber, pillar_funnel_trap or dld_array")


def _segments(path: list[list[float]]) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    return [((a[0], a[1]), (b[0], b[1])) for a, b in zip(path[:-1], path[1:])]


def channel_segments(chip: dict) -> dict[str, list]:
    """Every channel's centreline as segments, by channel id.

    A channel with an explicit "path" is taken along it. A channel without one is taken
    along the STRAIGHT RUN BETWEEN ITS TWO ENDS, clipped to the boxes at each end - which
    is what cad/make_train_cad.py cuts and what cad/make_figures.py draws. That geometry is
    imported from make_figures rather than recomputed here: a third copy of "where does
    this channel run" is how the drawing, the cut path and the checks drift apart.

    Without the fallback this check saw only the 8 of 16 specs that declare paths, and
    returned an empty list - a silent pass - on the other half.
    """
    out: dict[str, list] = {}
    for ch in chip.get("channels", []):
        if ch.get("path"):
            out[ch["id"]] = _segments(ch["path"])
    if figures is None:
        return out
    try:
        drawn = figures.segments_of(chip)
    except Exception:       # a spec too incomplete to draw (e.g. no layers): paths only
        return out
    for ch, p0, p1, _groove in drawn:
        out.setdefault(ch["id"], [(tuple(p0), tuple(p1))])
    return out


def _intersect(p1, p2, p3, p4) -> tuple[float, float] | None:
    """Proper intersection point of segments p1-p2 and p3-p4 (not at their ends), or None."""
    d = (p2[0] - p1[0]) * (p4[1] - p3[1]) - (p2[1] - p1[1]) * (p4[0] - p3[0])
    if abs(d) < 1e-12:
        return None
    t = ((p3[0] - p1[0]) * (p4[1] - p3[1]) - (p3[1] - p1[1]) * (p4[0] - p3[0])) / d
    u = ((p3[0] - p1[0]) * (p2[1] - p1[1]) - (p3[1] - p1[1]) * (p2[0] - p1[0])) / d
    eps = 1e-6
    if eps < t < 1 - eps and eps < u < 1 - eps:
        return (round(p1[0] + t * (p2[0] - p1[0]), 3), round(p1[1] + t * (p2[1] - p1[1]), 3))
    return None


def crossings(chip: dict) -> list[tuple[str, str, tuple[float, float]]]:
    """Pairs of channels with no shared end whose centrelines cross in plan view.

    EVERY channel is checked, along its "path" where it has one and along the straight
    clipped run it is cut as where it has not (channel_segments). Channels that share an
    end genuinely meet there and are skipped.
    """
    segs = channel_segments(chip)
    chans = [ch for ch in chip.get("channels", []) if ch["id"] in segs]
    out = []
    for i, a in enumerate(chans):
        for b in chans[i + 1:]:
            if {a["from"], a["to"]} & {b["from"], b["to"]}:
                continue
            hit = next((pt for s1 in segs[a["id"]] for s2 in segs[b["id"]] if (pt := _intersect(*s1, *s2))), None)
            if hit:
                out.append((a["id"], b["id"], hit))
    return out


def spec_paths(args: list[str]) -> list[Path]:
    if args:
        return [Path(a) for a in args]
    return sorted((ROOT / "chip").glob("*.json")) + sorted((ROOT / "cad").glob("*_spec.json"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Real-scale design checks for chip specs.")
    ap.add_argument("specs", nargs="*", help="chip spec files (default: chip/*.json and cad/*_spec.json)")
    ap.add_argument("--protocol", action="append", default=[], help="also check reagents/temperatures of this protocol")
    args = ap.parse_args(argv)

    params = schema_io.load_json(ROOT / "contracts" / "parameters.json")
    materials = schema_io.load_json(ROOT / "contracts" / "materials.json")["materials"]
    fab = schema_io.load_json(ROOT / "cad" / "fabrication.json")
    blocks_path = ROOT / "contracts" / "blocks.json"
    try:
        blocks = schema_io.load_json(blocks_path) if blocks_path.exists() else None
    except ValueError:
        blocks = None
    total_errors = 0
    for path in spec_paths(args.specs):
        chip = schema_io.load_json(path)
        errs = schema_io.schema_errors("chip_spec", chip)
        if errs:
            print(f"{rel(path)}: not a valid chip spec ({len(errs)} schema errors); run tools/validate.py")
            total_errors += 1
            continue
        protocols = paired_protocols(chip, args.protocol)
        f = check(chip, protocols, params, materials, fab, downstream_nozzles(chip), blocks)
        names = ", ".join(p.stem for p, _ in protocols) or "none"
        print(f"{rel(path)}  (chip {chip['id']}; protocols: {names})")
        for o in f.ok:
            print(f"  OK     {o}")
        for e in f.errors:
            print(f"  ERROR  {e}")
        for w in f.warnings:
            print(f"  WARN   {w}")
        for t in f.todo:
            print(f"  TODO   {t}")
        status = "FAILED" if f.errors else "OK"
        print(f"  {status}: {len(f.errors)} error(s), {len(f.warnings)} warning(s), {len(f.todo)} TODO warning(s)\n")
        total_errors += len(f.errors)
    return 1 if total_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

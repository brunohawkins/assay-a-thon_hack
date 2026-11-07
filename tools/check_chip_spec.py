"""Design checks for chip specs, at REAL scale.

    python tools/check_chip_spec.py                     # every spec in chip/ and cad/*_spec.json
    python tools/check_chip_spec.py cad/m3_wash_spec.json --protocol protocols/m3_wash.json

tools/validate.py checks that files follow the contracts; this checks that the design
works for capsules. Capsule diameter, swell factor and nozzle width come from the spec
when it states them, else from contracts/parameters.json. Protocols are the ones with a
run on the chip (runs/), plus any given with --protocol.

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
Warnings:
  - fabricated feature smaller than the fabrication minimum in cad/fabrication.json
    (fabrication limits never block the design; while they're TODO, one summary warning)
  - barcode collision rate above 5%
  - reagent whose compatibility with the chip material is TODO or incompatible
  - unknown material, channel shallower than a capsule, outlet without a width
  - two channels with explicit paths that cross in the same layer (they would join)
TODO lines: fabrication limits still unknown; parts marked fabricable: false (by design).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))

import commands  # noqa: E402
import schema_io  # noqa: E402
import sizing  # noqa: E402

TRAP_MAX_FACTOR = 0.4
CHANNEL_MIN_FACTOR = 1.5
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
    """Encapsulation nozzles fed by this chip through pipeline hand-offs: [(nozzle_um, chip_id, handoff_id)]."""
    out = []
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
                if nozzle:
                    out.append((nozzle, down["id"], h["id"]))
    return out


def check(chip: dict, protocols: list[tuple[Path, dict]], params: dict, materials: dict, fab: dict,
          downstream: list[tuple[float, str, str]] | None = None) -> Findings:
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
    features = [(f"channel '{ch['id']}' width", ch["width_mm"] * 1000) for ch in made_here]
    features += [(f"channel '{ch['id']}' depth", ch["depth_mm"] * 1000) for ch in made_here]
    features += [(f"port '{p['id']}' width", p["width_um"]) for p in ports.values() if "width_um" in p]
    features += [(f"chamber '{c['id']}' height", c["height_um"]) for c in chambers.values() if "height_um" in c]
    features += [(f"chamber '{c['id']}' {c['retention']['type']} opening", c["retention"]["opening_um"])
                 for c in chambers.values() if c.get("retention", {}).get("source") == "fabricated"]
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
        if "chambers_per_round" in bc:
            inputs["split_chambers_per_round"] = bc["chambers_per_round"]
        if "rounds" in bc:
            inputs["barcoding_rounds"] = bc["rounds"]
        if "sublibraries" in bc:
            inputs["sublibraries"] = bc["sublibraries"]
        r = sizing.compute(inputs)
        if r["barcode_collision_fraction"] > sizing.COLLISION_WARN:
            f.warn(f"barcode collisions: about {r['barcode_collision_fraction']:.0%} of the ~{r['cells']:,.0f} cells "
                   f"in {r['capsule_count']:,.0f} capsules share a barcode "
                   f"({r['barcode_combinations_with_sublibraries']:,} combinations)")

    # --- channels that cross (explicit paths only) ---------------------------------------
    for a, b, (x, y) in crossings(chip):
        f.warn(f"channels '{a}' and '{b}' cross at ({x:g}, {y:g}): in one layer they would join; "
               f"needs a bridge (second layer) or a reroute")

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
    return f


def _segments(path: list[list[float]]) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    return [((a[0], a[1]), (b[0], b[1])) for a, b in zip(path[:-1], path[1:])]


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
    """Pairs of channels (both with an explicit path, no shared end) whose centrelines cross."""
    chans = [ch for ch in chip.get("channels", []) if ch.get("path")]
    out = []
    for i, a in enumerate(chans):
        for b in chans[i + 1:]:
            if {a["from"], a["to"]} & {b["from"], b["to"]}:
                continue
            hit = next((pt for s1 in _segments(a["path"]) for s2 in _segments(b["path"]) if (pt := _intersect(*s1, *s2))), None)
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
    total_errors = 0
    for path in spec_paths(args.specs):
        chip = schema_io.load_json(path)
        errs = schema_io.schema_errors("chip_spec", chip)
        if errs:
            print(f"{rel(path)}: not a valid chip spec ({len(errs)} schema errors); run tools/validate.py")
            total_errors += 1
            continue
        protocols = paired_protocols(chip, args.protocol)
        f = check(chip, protocols, params, materials, fab, downstream_nozzles(chip))
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

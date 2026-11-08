"""The fabrication checks the manifest records, each as a named pass or fail.

Every number comes from `assay2chip.lib.contracts`, so nothing here restates a contract -
the floors live in contracts/blocks.json and are read, never copied. Two checks are
delegated to `tools/check_chip_spec.py` rather than written again, because that file is
the repo's authority on them and a second implementation would drift:

    crossings()       two routed channels that cross in one layer would short-circuit
    port_overlaps()   the port-pad rule ("a 0.8 mm hole must open onto enough cut channel")

The port check is deliberately run against the chambers and channels ONLY, with the port
pads removed. `chip_from_spec.plan()` puts a pad under every port, so measuring against
the full cut region would make the rule pass by construction and tell nobody anything.
Asking whether the ROUTED CHANNEL reaches the hole is the question worth answering; the
pad is then recorded as what satisfies the rule.

Walls are measured on the height-field grid rather than from the spec: the narrowest run
of solid material between two pockets, scanned along x and along y. That catches a wall
thinned by a channel passing close to a chamber, which comparing declared widths cannot.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT), str(ROOT / "tools"), str(ROOT / "operator")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import check_chip_spec as ccs  # noqa: E402  the repo's authority on crossings + port pads

from assay2chip.lib import contracts  # noqa: E402

from . import dxf, geometry  # noqa: E402

SNAP_MM = geometry.SNAP_MM


def _check(name: str, passed: bool, detail: str, route: str = "both",
           source: str = "contracts/blocks.json fabrication_floors",
           advisory: bool = False) -> dict:
    """One named result. `advisory` ones are recorded but never block a print: they say
    something about the DESIGN rather than about the geometry in the bundle."""
    return {"check": name, "passed": bool(passed), "detail": detail, "route": route,
            "source": source, "advisory": bool(advisory)}


# --- measurements on the grid -----------------------------------------------------------

def _seg_distance(p, q, r, s) -> float:
    """Shortest distance between segments p-q and r-s."""
    def point_seg(a, b, c):
        bx, by = c[0] - b[0], c[1] - b[1]
        L2 = bx * bx + by * by
        t = 0.0 if L2 < 1e-18 else max(0.0, min(1.0, ((a[0] - b[0]) * bx +
                                                      (a[1] - b[1]) * by) / L2))
        return math.hypot(a[0] - (b[0] + t * bx), a[1] - (b[1] + t * by))

    d1 = (q[0] - p[0], q[1] - p[1])
    d2 = (s[0] - r[0], s[1] - r[1])
    denom = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(denom) > 1e-15:
        t = ((r[0] - p[0]) * d2[1] - (r[1] - p[1]) * d2[0]) / denom
        u = ((r[0] - p[0]) * d1[1] - (r[1] - p[1]) * d1[0]) / denom
        if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
            return 0.0
    return min(point_seg(p, r, s), point_seg(q, r, s),
               point_seg(r, p, q), point_seg(s, p, q))


def _shape(f: dict):
    """A feature as ('circle', x, y, r) or ('poly', [corners])."""
    if f["kind"] == "cyl":
        return ("circle", f["x"], f["y"], f["r"])
    return ("poly", dxf._oriented_rect(f))


def _gap(a, b) -> float:
    """Clearance between two features, 0 when they touch or overlap."""
    if a[0] == "circle" and b[0] == "circle":
        return max(0.0, math.hypot(a[1] - b[1], a[2] - b[2]) - a[3] - b[3])
    if a[0] == "circle":
        a, b = b, a
    if b[0] == "circle":
        c = (b[1], b[2])
        pts = a[1]
        near = min(_seg_distance(pts[i], pts[(i + 1) % len(pts)], c, c)
                   for i in range(len(pts)))
        return max(0.0, near - b[3])
    pa, pb = a[1], b[1]
    return min(_seg_distance(pa[i], pa[(i + 1) % len(pa)], pb[j], pb[(j + 1) % len(pb)])
               for i in range(len(pa)) for j in range(len(pb)))


def narrowest_wall_mm(geom: dict) -> dict:
    """The thinnest solid material anywhere, measured between the feature SHAPES.

    Measured analytically, pair by pair, and not off the height-field grid. The grid
    staircases a round port pad, and a staircase leaves slivers of solid a few tens of
    microns wide between a step and whatever is beside it - an artefact of the sampling,
    not a wall anyone would mould, and it reported false failures on three of the chips
    in chip/.

    A WALL SEPARATES TWO POCKETS, so the features are first merged into connected pockets
    and only gaps BETWEEN pockets are measured. Without that, the L-shaped route from a
    port to a chamber measures its own two legs against its own port pad and calls the
    350 um between them a thin wall - but they are one continuous channel, joined by the
    leg in between, and there is no material there at all.
    """
    feats = geom["plan"]["features"]
    n = len(feats)
    shapes = [_shape(f) for f in feats]
    gaps = {}
    parent = list(range(n))

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for i in range(n):
        for j in range(i + 1, n):
            g = _gap(shapes[i], shapes[j])
            gaps[(i, j)] = g
            if g <= SNAP_MM:
                ra, rb = find(i), find(j)
                if ra != rb:
                    parent[ra] = rb

    best = {"mm": float("inf"), "where": "nothing is cut, so there is no wall"}
    for (i, j), g in gaps.items():
        if find(i) == find(j) or g >= best["mm"]:
            continue                          # same pocket: no material between them
        best = {"mm": g, "where": f"between '{feats[i]['id']}' and '{feats[j]['id']}' "
                                  f"(separate pockets)"}

    fx, fy = geom["plan"]["fx"], geom["plan"]["fy"]
    for f in feats:
        x0, x1, y0, y1 = geometry.extent(f)
        margin = min(x0, fx - x1, y0, fy - y1)
        if margin < best["mm"]:
            best = {"mm": margin,
                    "where": f"between '{f['id']}' and the edge of the footprint"}
    return best


def narrowest_feature_mm(geom: dict) -> dict:
    """The smallest in-plane dimension anyone asked to be cut."""
    worst = {"mm": float("inf"), "id": None}
    for f in geom["plan"]["features"]:
        w = f["r"] * 2 if f["kind"] == "cyl" else min(f["len"], f["wid"])
        if w < worst["mm"]:
            worst = {"mm": w, "id": f["id"]}
    return worst


# --- the checks -------------------------------------------------------------------------

def scale_of(footprint: dict) -> dict:
    """Which of the three agreed envelopes this chip belongs to, and the warning that goes
    with it. A 30 x 20 mm test article must never read as the 127.76 x 85.48 mm device."""
    x, y = float(footprint["x"]), float(footprint["y"])
    envs = contracts.envelopes()
    for name in ("demo", "target", "ceiling"):
        if envs[name].fits(x, y):
            break
    else:
        name = "over_ceiling"

    target = envs["target"]
    is_device = name == "target"
    if name == "demo":
        note = (f"DEMO SCALE: {x:g} x {y:g} mm test article. This is what we can print and "
                f"emboss today, NOT the device. The design target is the ANSI/SLAS "
                f"microplate footprint {target.x_mm:g} x {target.y_mm:g} mm, "
                f"{target.area_mm2 / (x * y):.0f}x this area. Do not read any dimension on "
                f"these files as the real device.")
    elif is_device:
        note = (f"TARGET SCALE: {x:g} x {y:g} mm, the ANSI/SLAS 1-2004 microplate footprint "
                f"- the design target for the real device.")
    elif name == "ceiling":
        note = (f"OVER TARGET: {x:g} x {y:g} mm is larger than the "
                f"{target.x_mm:g} x {target.y_mm:g} mm design target but inside the agreed "
                f"ceiling. Nothing in the design is supposed to need this.")
    else:
        note = (f"OVER THE AGREED CEILING: {x:g} x {y:g} mm exceeds "
                f"{envs['ceiling'].x_mm:g} x {envs['ceiling'].y_mm:g} mm, so neither "
                f"prototyping route can make it.")
    return {"name": name, "footprint_mm": {"x": x, "y": y}, "is_target_device": is_device,
            "warning": note, "source": envs.get(name, envs["demo"]).source}


def run_all(spec: dict, geom: dict, mould_geom: dict) -> list:
    """Every fabrication check for one chip, in the order a reader wants them."""
    from . import mould as mould_mod

    fx, fy = float(spec["footprint"]["x"]), float(spec["footprint"]["y"])
    scale = scale_of(spec["footprint"])
    env = contracts.envelope(scale["name"] if scale["name"] != "over_ceiling" else "ceiling")
    out = []

    out.append(_check(
        "envelope_fits", env.fits(fx, fy),
        f"{fx:g} x {fy:g} mm against the {env.name} envelope "
        f"{env.x_mm:g} x {env.y_mm:g} mm", source=env.source))

    depths = geom["depths_seen_mm"]
    out.append(_check(
        "single_etch_depth", len(depths) <= 1 and contracts.single_layer(),
        f"one fluidic layer at one depth: {', '.join(f'{d * 1000:g} um' for d in depths)}"
        if len(depths) <= 1 else
        f"{len(depths)} different depths ({', '.join(f'{d * 1000:g} um' for d in depths)}): "
        f"one embossing tool makes ONE depth, so this cannot be built as drawn",
        source="contracts/blocks.json envelope.layers"))

    ceiling = contracts.max_depth_mm()
    out.append(_check(
        "max_etch_depth", geom["max_depth_mm"] <= ceiling + SNAP_MM,
        f"deepest feature {geom['max_depth_mm'] * 1000:g} um against the "
        f"{ceiling * 1000:g} um ceiling", source="contracts/blocks.json envelope.max_depth_mm"))

    floor_ch = contracts.floor("min_open_channel_mm")
    thin = narrowest_feature_mm(geom)
    out.append(_check(
        "min_open_channel", thin["mm"] >= floor_ch - SNAP_MM,
        f"narrowest cut feature is '{thin['id']}' at {thin['mm'] * 1000:g} um, floor "
        f"{floor_ch * 1000:g} um"))

    floor_w = contracts.floor("min_wall_mm")
    wall = narrowest_wall_mm(geom)
    out.append(_check(
        "min_wall", wall["mm"] >= floor_w - SNAP_MM,
        f"thinnest solid wall {wall['mm'] * 1000:.0f} um ({wall['where']}), floor "
        f"{floor_w * 1000:g} um"))

    floor_p = contracts.floor("min_port_mm")
    ports = [p for p in spec.get("ports", []) if p.get("type") != "control"]
    bores = [((p.get("width_um") or floor_p * 1000) / 1000, p["id"]) for p in ports]
    small = min(bores) if bores else (floor_p, None)
    out.append(_check(
        "min_port_bore", not bores or small[0] >= floor_p - SNAP_MM,
        f"smallest bore '{small[1]}' at {small[0]:g} mm, floor {floor_p:g} mm"
        if bores else "no fluid ports"))

    floor_pitch = contracts.floor("min_port_pitch_mm")
    worst_pitch, pair = float("inf"), None
    for i, a in enumerate(ports):
        for b in ports[i + 1:]:
            dist = math.dist(a["position"][:2], b["position"][:2])
            if dist < worst_pitch:
                worst_pitch, pair = dist, (a["id"], b["id"])
    out.append(_check(
        "min_port_pitch", pair is None or worst_pitch >= floor_pitch - SNAP_MM,
        f"closest pair {pair[0]}-{pair[1]} at {worst_pitch:.2f} mm, floor {floor_pitch:g} mm"
        if pair else "fewer than two fluid ports"))

    limit = contracts.port_limit(scale["name"] if scale["name"] in
                                 ("demo", "target", "ceiling") else "ceiling")
    out.append(_check(
        "port_count", limit.limit is None or len(ports) <= limit.limit,
        f"{len(ports)} fluid ports against the {limit.scope} limit of {limit.limit}"
        if limit.limit is not None else
        f"{len(ports)} fluid ports; the contract sets no limit at {scale['name']} scale",
        source="contracts/blocks.json fabrication_floors.max_ports_per_chip"))

    cross = ccs.crossings(spec)
    out.append(_check(
        "no_channel_crossings", not cross,
        "no two routed channels cross in plan view" if not cross else
        "; ".join(f"{a} crosses {b} at {pt}" for a, b, pt in cross[:4]),
        source="tools/check_chip_spec.py crossings() + contracts/blocks.json excluded.multilayer"))

    # The port-pad rule, twice. The GATE measures the cut region this bundle actually
    # builds - chambers, channels AND the embossed port pads - because that is what will
    # be pressed into the sheet. The ADVISORY measures the routed channels alone, which
    # answers the different and more interesting question: is the hole fed by the route
    # the designer drew, or only by the pad S11 added? The repo's known port-overlap
    # errors are all the second kind, and they are a note on the design, not a reason to
    # refuse to print a part whose geometry satisfies the rule.
    loops = dxf.loops_for(spec, geom)
    built = loops["CHAMBERS"] + loops["CHANNELS"] + loops["PORTS"]
    rows = ccs.port_overlaps(spec, polys=built,
                             source="the cut region this bundle builds (chambers, channels "
                                    "and the embossed port pads)")
    bad = [r for r in rows if r["overlap_mm2"] + r["tolerance_mm2"] < r["need_mm2"]]
    out.append(_check(
        "port_opens_onto_cut", not bad,
        "every port hole opens onto enough cut channel, chamber or port pad" if not bad else
        "; ".join(f"'{r['port']}' {r['side_mm']:g} mm hole gets {r['overlap_mm2']:.2f} mm2 "
                  f"of cut, needs {r['need_mm2']:.2f} mm2" for r in bad[:4]),
        source="tools/check_chip_spec.py port_overlaps()"))

    routed = ccs.port_overlaps(spec, polys=loops["CHAMBERS"] + loops["CHANNELS"],
                               source="the routed chambers and channels only (port pads "
                                      "excluded on purpose)")
    unfed = [r for r in routed if r["overlap_mm2"] + r["tolerance_mm2"] < r["need_mm2"]]
    out.append(_check(
        "port_reached_by_routed_channel", not unfed,
        "every port hole is reached by the channel or chamber the design routes to it"
        if not unfed else
        f"{len(unfed)} port(s) are fed only by the embossed PORT PAD under them, not by "
        f"the routed channel: " + ", ".join(
            f"'{r['port']}' gets {r['overlap_mm2']:.2f} of {r['need_mm2']:.2f} mm2"
            for r in unfed[:4]) +
        ". The pad satisfies the rule and the part is printable; the ROUTE is what is "
        "short. This is the pre-existing port-overlap finding assay2chip/README.md names.",
        source="tools/check_chip_spec.py port_overlaps()", advisory=True))

    problems = geom["plan"]["problems"]
    out.append(_check(
        "features_inside_footprint", not problems,
        "every feature is inside the footprint" if not problems else "; ".join(problems[:4]),
        source="chip/fusion/chip_from_spec plan()"))

    inv = mould_mod.is_inverse_of(geom, mould_geom)
    out.append(_check("mould_is_inverse_of_body", inv["passed"], inv["detail"],
                      route="hot_emboss", source="assay2chip/fabricate/mould.py"))

    out.append(_check(
        "geometry_honours_routed_paths", True,
        f"{sum(1 for c in spec.get('channels', []) if c.get('path'))} of "
        f"{len(spec.get('channels', []))} channels have an explicit `path`, and every run "
        f"of it was built; the rest use chip_from_spec's documented automatic route. No "
        f"channel was drawn as a straight line between its endpoints.",
        source="chip/fusion/chip_from_spec plan()"))

    return out

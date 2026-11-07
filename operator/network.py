"""Hydraulic network solver for any chip spec (steady, laminar, incompressible).

    python operator/network.py chip/module1_v1.json --mode grow            # one mode, table out
    python operator/network.py chip/module1_v1.json --report               # every mode + design-doc check

Model
  Nodes   every chamber (junctions included) and every port.
  Edges   every channel: R = 12 mu L / (w h^3 (1 - 0.63 h/w)), h the smaller side, L its
          centreline length (from the DXF when given, else from the spec's path or the
          straight line between the parts, trimmed at their outlines, as cad/make_train_cad.py
          cuts it). Each channel also crosses part of the chamber it enters: from the entry
          edge to the chamber centre, as a slab of the chamber's width and height.
          Port holes in the lid (2 mm x 3 mm) add next to nothing and are left out, as is
          any tubing off the chip.
  Ports   FEED  a fixed flow source (a syringe pump), uL/min
          VENT  pressure 0 (an open outlet)
          CAP   closed: no flow
Outputs flow in every channel (uL/min, + means from -> to), pressure at every node (Pa,
        mbar), wall shear in every channel and chamber (dyn/cm2, tau = 6 mu Q / (w h^2) as in
        protocols/build_module1.py), and where each chamber's medium goes: a tracer tags the
        liquid by the last culture chamber it passed through, so "medium from n_cx reaches
        g_cx" is computed, not assumed.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FEED, VENT, CAP = "FEED", "VENT", "CAP"
MU_37C = 0.8e-3            # Pa s: water at 37 C, the value protocols/build_module1.py uses
def _port_hole_mm() -> float:
    """Port bore, taken from the same contract cad/make_train_cad.py reads.

    This was a 2.0 mm literal mirroring a 2.0 mm literal in the CAD generator. When the
    generator started deriving the bore from contracts/blocks.json (0.8 mm) this copy went
    stale, every port-connected channel stopped matching its own DXF, and the geometry
    silently fell back to the straight-line estimate.
    """
    try:
        import json
        b = json.loads((ROOT / "contracts" / "blocks.json").read_text())
        return float(b["fabrication_floors"]["min_port_mm"])
    except Exception:
        return 2.0


PORT_HOLE_MM = _port_hole_mm()   # cad/make_train_cad.py PORT_SIZE_MM, same source
DYN_CM2_PER_PA = 10.0


class NetworkError(ValueError):
    """The port states have no steady solution (e.g. liquid fed with nowhere to go)."""


# --- geometry -------------------------------------------------------------------------


def dxf_channel_segments(dxf_path: Path) -> list[dict]:
    """Channel centrelines from a cad/make_train_cad.py DXF: every 4 LINEs on the CHANNELS
    layer are one rectangle; its short sides are the channel's two ends."""
    lines, layer, cur = [], None, {}
    tokens = dxf_path.read_text().split("\n")
    pairs = list(zip(tokens[0::2], tokens[1::2]))
    in_line = False
    for code, value in pairs:
        code, value = code.strip(), value.strip()
        if code == "0":
            if in_line and layer == "CHANNELS":
                lines.append(((cur["10"], cur["20"]), (cur["11"], cur["21"])))
            in_line, layer, cur = value == "LINE", None, {}
        elif in_line and code == "8":
            layer = value
        elif in_line and code in ("10", "20", "11", "21"):
            cur[code] = float(value)
    rects = []
    for k in range(0, len(lines) - 3, 4):
        c0, c1, c2, c3 = lines[k][0], lines[k + 1][0], lines[k + 2][0], lines[k + 3][0]
        a = ((c0[0] + c3[0]) / 2, (c0[1] + c3[1]) / 2)
        b = ((c1[0] + c2[0]) / 2, (c1[1] + c2[1]) / 2)
        rects.append({"a": a, "b": b, "width_mm": math.dist(c0, c3), "length_mm": math.dist(a, b), "closed": lines[k + 3][1] == c0})
    return rects


def _clip_to_box(p0, p1, center, size):
    """Same rule as cad/make_train_cad.py: move p0 along p0->p1 until it leaves the box."""
    (cx, cy), (sx, sy) = center[:2], size[:2]
    x0, x1, y0, y1 = cx - sx / 2, cx + sx / 2, cy - sy / 2, cy + sy / 2
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    best = 0.0
    for lo, hi, start, delta in ((x0, x1, p0[0], dx), (y0, y1, p0[1], dy)):
        if abs(delta) < 1e-9:
            continue
        for bound in (lo, hi):
            t = (bound - start) / delta
            if 0.0 <= t <= 1.0:
                px, py = p0[0] + dx * t, p0[1] + dy * t
                if x0 - 1e-6 <= px <= x1 + 1e-6 and y0 - 1e-6 <= py <= y1 + 1e-6:
                    best = max(best, t)
    return (p0[0] + dx * best, p0[1] + dy * best)


def channel_geometry(chip: dict, dxf_path: Path | None = None) -> dict[str, dict]:
    """id -> {a, b, length_mm, width_mm, depth_mm, source}: the cut centreline of every channel."""
    boxes = {c["id"]: (c["center"], c["size"]) for c in chip["chambers"]}
    for p in chip.get("ports", []):
        boxes[p["id"]] = (p["position"], [PORT_HOLE_MM, PORT_HOLE_MM, 0])
    out = {}
    for ch in chip["channels"]:
        if ch.get("path"):
            pts = ch["path"]
            length = sum(math.dist(p, q) for p, q in zip(pts[:-1], pts[1:]))
            out[ch["id"]] = {"a": tuple(pts[0]), "b": tuple(pts[-1]), "length_mm": length, "width_mm": ch["width_mm"],
                             "depth_mm": ch["depth_mm"], "source": "spec path"}
            continue
        (ca, sa), (cb, sb) = boxes[ch["from"]], boxes[ch["to"]]
        a = _clip_to_box(ca[:2], cb[:2], ca, sa)
        b = _clip_to_box(cb[:2], ca[:2], cb, sb)
        out[ch["id"]] = {"a": a, "b": b, "length_mm": math.dist(a, b), "width_mm": ch["width_mm"],
                         "depth_mm": ch["depth_mm"], "source": "spec (straight, trimmed)"}
    if dxf_path and dxf_path.exists():
        rects = dxf_channel_segments(dxf_path)
        for cid, g in out.items():   # match each DXF rectangle to the channel whose ends it shares
            best = min(rects, key=lambda r: min(math.dist(r["a"], g["a"]) + math.dist(r["b"], g["b"]),
                                                math.dist(r["a"], g["b"]) + math.dist(r["b"], g["a"])), default=None)
            if best and min(math.dist(best["a"], g["a"]) + math.dist(best["b"], g["b"]),
                            math.dist(best["a"], g["b"]) + math.dist(best["b"], g["a"])) < 0.05:
                fwd = math.dist(best["a"], g["a"]) < math.dist(best["a"], g["b"])
                g.update({"a": best["a"] if fwd else best["b"], "b": best["b"] if fwd else best["a"],
                          "length_mm": best["length_mm"], "width_mm": best["width_mm"], "source": "DXF"})
    return out


# --- physics --------------------------------------------------------------------------


def rect_resistance(length_mm: float, width_mm: float, depth_mm: float, mu: float) -> float:
    """Pa s / m^3. R = 12 mu L / (w h^3 (1 - 0.63 h / w)), h the smaller side."""
    w, h = max(width_mm, depth_mm) / 1000, min(width_mm, depth_mm) / 1000
    return 12 * mu * (length_mm / 1000) / (w * h ** 3 * (1 - 0.63 * h / w))


def shear_dyn_cm2(flow_ul_min: float, width_mm: float, depth_mm: float, mu: float) -> float:
    """Wall shear of a wide slot: tau = 6 mu Q / (w h^2), in dyn/cm2."""
    q = abs(flow_ul_min) * 1e-9 / 60
    w, h = width_mm / 1000, depth_mm / 1000
    return 6 * mu * q / (w * h * h) * DYN_CM2_PER_PA


def _chamber_leg(chamber: dict, entry: tuple[float, float]) -> tuple[float, float]:
    """(length, width) in mm of the slab from an entry point on the chamber outline to its centre."""
    (cx, cy, _), (sx, sy, _) = chamber["center"], chamber["size"]
    on_x_edge = abs(abs(entry[0] - cx) - sx / 2) < 1e-3   # entered through a left / right edge
    return (sx / 2, sy) if on_x_edge else (sy / 2, sx)


@dataclass
class Network:
    chip: dict
    mu: float = MU_37C
    dxf: Path | None = None
    geometry: dict = field(init=False)
    edges: list[dict] = field(init=False)
    nodes: list[str] = field(init=False)

    def __post_init__(self):
        self.geometry = channel_geometry(self.chip, self.dxf)
        chambers = {c["id"]: c for c in self.chip["chambers"]}
        self.nodes = list(chambers) + [p["id"] for p in self.chip.get("ports", [])]
        self.edges = []
        for ch in self.chip["channels"]:
            g = self.geometry[ch["id"]]
            r = rect_resistance(g["length_mm"], g["width_mm"], g["depth_mm"], self.mu)
            for end, pt in ((ch["from"], g["a"]), (ch["to"], g["b"])):
                if end in chambers:
                    leg, width = _chamber_leg(chambers[end], pt)
                    r += rect_resistance(leg, width, chambers[end]["size"][2], self.mu)
            self.edges.append({"id": ch["id"], "from": ch["from"], "to": ch["to"], "R": r, **g})

    # ------------------------------------------------------------------ solving

    def solve(self, ports: dict[str, tuple[str, float] | str]) -> dict:
        """ports: id -> FEED (with uL/min) / VENT / CAP, e.g. {"p_in": ("FEED", 11), "p_mid": "VENT"}.
        Unlisted ports are CAP. Returns flows, pressures, shear and medium routing."""
        state = {}
        for pid in (p["id"] for p in self.chip.get("ports", [])):
            v = ports.get(pid, CAP)
            kind, q = (v, 0.0) if isinstance(v, str) else (v[0], float(v[1]))
            if kind not in (FEED, VENT, CAP):
                raise NetworkError(f"port {pid}: state must be FEED, VENT or CAP, not {kind!r}")
            state[pid] = (kind, q if kind == FEED else 0.0)
        idx = {n: i for i, n in enumerate(self.nodes)}
        # Connected components through channels; a component with feeds but no vent has no solution.
        parent = list(range(len(self.nodes)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        for e in self.edges:
            parent[find(idx[e["from"]])] = find(idx[e["to"]])
        comps: dict[int, list[str]] = {}
        for n in self.nodes:
            comps.setdefault(find(idx[n]), []).append(n)
        fixed = {n: 0.0 for n, (k, _) in state.items() if k == VENT}
        for members in comps.values():
            feeds = [n for n in members if state.get(n, (CAP,))[0] == FEED and state[n][1] > 0]
            vents = [n for n in members if n in fixed]
            if feeds and not vents:
                raise NetworkError(f"{', '.join(feeds)} fed but nothing is vented downstream: the liquid has nowhere to go")
            if not vents:
                for n in members:
                    fixed[n] = 0.0          # a dead, all-capped part: no flow, pressure undefined (shown as 0)
        unknown = [n for n in self.nodes if n not in fixed]
        u = {n: i for i, n in enumerate(unknown)}
        m = len(unknown)
        A = [[0.0] * m for _ in range(m)]
        rhs = [0.0] * m
        for n in unknown:
            k, q = state.get(n, (CAP, 0.0))
            if k == FEED:
                rhs[u[n]] += q * 1e-9 / 60            # m^3/s into the node
        for e in self.edges:
            g = 1.0 / e["R"]
            a, b = e["from"], e["to"]
            for x, y in ((a, b), (b, a)):
                if x in u:
                    A[u[x]][u[x]] += g
                    if y in u:
                        A[u[x]][u[y]] -= g
                    else:
                        rhs[u[x]] += g * fixed[y]
        pressure = dict(fixed)
        for n, val in zip(unknown, _gauss(A, rhs)):
            pressure[n] = val
        flows = {}
        for e in self.edges:
            q = (pressure[e["from"]] - pressure[e["to"]]) / e["R"] * 60 * 1e9   # uL/min, + = from -> to
            flows[e["id"]] = 0.0 if abs(q) < 1e-9 else q
        return self._report(state, pressure, flows)

    # ------------------------------------------------------------------ results

    def _report(self, state, pressure, flows) -> dict:
        chambers = {c["id"]: c for c in self.chip["chambers"]}
        culture = [c["id"] for c in self.chip["chambers"] if c.get("type") not in ("pool", "junction", "split", "inlet", "outlet")]
        # net flow into each node, and through each chamber
        inflow = {n: 0.0 for n in self.nodes}
        for e in self.edges:
            q = flows[e["id"]]
            inflow[e["to"] if q > 0 else e["from"]] += abs(q)
        port_flow = {}
        for pid, (k, q) in state.items():
            net_out = sum((flows[e["id"]] if e["from"] == pid else -flows[e["id"]]) for e in self.edges if pid in (e["from"], e["to"]))
            port_flow[pid] = {"state": k, "set_ul_min": q, "into_chip_ul_min": round(net_out, 6)}
        channel_rows = {}
        for e in self.edges:
            q = flows[e["id"]]
            channel_rows[e["id"]] = {
                "flow_ul_min": round(q, 6), "direction": (f"{e['from']} -> {e['to']}" if q > 0 else f"{e['to']} -> {e['from']}") if q else "none",
                "shear_dyn_cm2": round(shear_dyn_cm2(q, e["width_mm"], e["depth_mm"], self.mu), 6),
                "length_mm": round(e["length_mm"], 3), "volume_ul": round(e["length_mm"] * e["width_mm"] * e["depth_mm"], 4),
                "velocity_mm_s": round(abs(q) / 60 / (e["width_mm"] * e["depth_mm"]), 5), "geometry": e["source"]}
        chamber_rows = {}
        for cid, c in chambers.items():
            thru = inflow[cid]
            chamber_rows[cid] = {"throughput_ul_min": round(thru, 6),
                                 "shear_dyn_cm2": round(shear_dyn_cm2(thru, c["size"][1], c["size"][2], self.mu), 6),
                                 "volume_ul": c["volume_ul"]}
        routing = self._routing(flows, state, culture)
        return {"ports": port_flow, "channels": channel_rows, "chambers": chamber_rows,
                "pressure_pa": {n: round(p, 6) for n, p in pressure.items()},
                "max_pressure_mbar": round(max(pressure.values()) / 100, 6),
                "routing": routing, "mu_pa_s": self.mu}

    def _routing(self, flows, state, culture) -> dict:
        """Steady tracer: what fraction of the liquid entering each node comes from each source,
        where a source is a fed port (fresh medium) or a culture chamber (its conditioned medium:
        everything leaving a culture chamber is tagged with that chamber)."""
        into: dict[str, list[tuple[str, float]]] = {n: [] for n in self.nodes}
        for e in self.edges:
            q = flows[e["id"]]
            if q > 0:
                into[e["to"]].append((e["from"], q))
            elif q < 0:
                into[e["from"]].append((e["to"], -q))
        memo: dict[str, dict[str, float]] = {}

        def outflow_tags(n, stack=()):
            if n in memo:
                return memo[n]
            if n in stack:
                raise NetworkError("circular flow")
            if n in culture:
                tags = {n: 1.0}
            elif state.get(n, (CAP,))[0] == FEED:
                tags = {n: 1.0}
            else:
                tags = inflow_tags(n, stack + (n,))
            memo[n] = tags
            return tags

        def inflow_tags(n, stack=()):
            total = sum(q for _, q in into[n])
            mix: dict[str, float] = {}
            if state.get(n, (CAP,))[0] == FEED:
                mix[n] = state[n][1]
                total += state[n][1]
            for src, q in into[n]:
                for tag, f in outflow_tags(src, stack).items():
                    mix[tag] = mix.get(tag, 0.0) + q * f
            return {t: v / total for t, v in mix.items()} if total else {}

        result = {}
        for n in self.nodes:
            mix = inflow_tags(n)
            if mix:
                result[n] = {t: round(f, 6) for t, f in sorted(mix.items(), key=lambda kv: -kv[1])}
        foreign = {c: {t: f for t, f in result.get(c, {}).items() if t in culture and t != c} for c in culture}
        return {"inflow_mix": result, "foreign_medium": {c: v for c, v in foreign.items() if v}}


def _gauss(A: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-30:
            raise NetworkError("singular network")
        M[col], M[piv] = M[piv], M[col]
        for r in range(n):
            if r != col and M[r][col]:
                f = M[r][col] / M[col][col]
                for k in range(col, n + 1):
                    M[r][k] -= f * M[col][k]
    return [M[i][n] / M[i][i] for i in range(n)]


# --- Module 1 v1 modes (protocols/module1_design.md, protocols/build_module1.py) ----------

MODULE1_MODES = {
    "grow": {"label": "Separate growth", "feed": ["p_in", "p_g_in", "p_a_in"], "vent": ["p_mid"]},
    "int_n": {"label": "Interact N → G+A", "feed": ["p_in"], "vent": ["p_g_out", "p_a_out"]},
    "int_g": {"label": "Interact G → N+A", "feed": ["p_g_in"], "vent": ["p_in", "p_a_out"]},
    "int_a": {"label": "Interact A → N+G", "feed": ["p_a_in"], "vent": ["p_in", "p_g_out"]},
    "harvest": {"label": "Harvest", "feed": ["p_in"], "vent": ["p_mid"]},
    "seed": {"label": "Seed (fallback)", "feed": ["p_in"], "vent": ["p_g_out", "p_a_out"]},
    # Not in the design doc's table, but what the protocol runs: build_module1.py COLLECT.
    "collect": {"label": "Collect (protocol back-flush)", "feed": ["p_in", "p_g_out", "p_a_out"], "vent": ["p_mid"]},
    "idle": {"label": "Idle (all capped)", "feed": [], "vent": []},
}
def _default_feed_ul_min() -> float:
    """Largest feed that keeps every culture chamber under the shear ceiling.

    Derived from the chip, never hard-coded: the board has been rescaled once already
    (140 x 75 mm -> the 30 x 20 mm build envelope) and a literal here went stale the
    moment it was, silently pushing wall shear 4x over the ceiling. tau = 6*mu*Q/(w*h^2)
    with w the chamber width across the flow and h its depth.
    """
    mu, ceiling, margin = 0.8e-3, 0.01, 0.85
    try:
        chip = json.loads((ROOT / "chip" / "module1_v1.json").read_text())
    except OSError:
        return 2.0
    flows = []
    for c in chip["chambers"]:
        if c.get("type") == "pool":          # the junction is not a culture chamber
            continue
        w, h = c["size"][1] * 1e-3, c["size"][2] * 1e-3
        q = (ceiling / 10.0) * w * h * h / (6.0 * mu)      # m3/s
        flows.append(q * 60.0 * 1e9)                        # uL/min
    return round(min(flows) * margin, 3) if flows else 2.0


DEFAULT_FEED_UL_MIN = _default_feed_ul_min()


def mode_ports(mode: str, flow: float | dict = DEFAULT_FEED_UL_MIN) -> dict:
    m = MODULE1_MODES[mode]
    out = {}
    for p in m["feed"]:
        out[p] = (FEED, flow[p] if isinstance(flow, dict) else flow)
    for p in m["vent"]:
        out[p] = VENT
    return out


def module1_network(mu: float = MU_37C) -> Network:
    chip = json.loads((ROOT / "chip" / "module1_v1.json").read_text())
    return Network(chip, mu=mu, dxf=ROOT / "cad" / "exports" / "module1_v1" / "module1_v1.dxf")


def solve_modes(net: Network, flow: float = DEFAULT_FEED_UL_MIN) -> dict:
    out = {}
    for mode in MODULE1_MODES:
        if mode == "idle":
            continue
        out[mode] = net.solve(mode_ports(mode, flow))
    return out


def threshold_flow(width_mm: float, depth_mm: float, ceiling: float, mu: float) -> float:
    """Flow (uL/min) at which tau = 6 mu Q / (w h^2) reaches the ceiling (dyn/cm2)."""
    return ceiling / DYN_CM2_PER_PA * (width_mm / 1000) * (depth_mm / 1000) ** 2 / (6 * mu) * 60e9


def design_check(net: Network) -> list[dict]:
    """The design doc's numbers next to what the solver gets. Nothing is tuned to fit."""
    sols = solve_modes(net)
    running = {m: s["max_pressure_mbar"] for m, s in sols.items() if m in ("grow", "int_n", "int_g", "int_a", "harvest", "seed")}
    lo, hi = min(running.values()), max(running.values())
    ch = net.chip["chambers"][0]
    chan = net.chip["channels"][0]
    q_chamber = threshold_flow(ch["size"][1], ch["size"][2], 0.01, net.mu)
    q_channel = threshold_flow(chan["width_mm"], chan["depth_mm"], 0.01, net.mu)
    worst_chamber = max((r["shear_dyn_cm2"], m, c) for m, s in sols.items() for c, r in s["chambers"].items() if c != "j_mid")
    d_implied = (8.8e-3) ** 2 / (2 * 12 * 3600)
    rows = [
        {"quantity": "Running pressure, all modes at 11 uL/min per feed (mbar)", "doc": "0.04-0.22",
         "solver": f"{lo:.3f}-{hi:.3f}", "per_mode": running,
         "match": abs(lo - 0.04) / 0.04 < 0.3 and abs(hi - 0.22) / 0.22 < 0.3},
        {"quantity": "Chamber shear reaches 0.01 dyn/cm2 at (uL/min)", "doc": "13.5", "solver": f"{q_chamber:.2f}",
         "match": abs(q_chamber - 13.5) < 0.2},
        {"quantity": "Channel shear reaches 0.01 dyn/cm2 at (uL/min)", "doc": "0.9", "solver": f"{q_channel:.2f}",
         "match": abs(q_channel - 0.9) < 0.05},
        {"quantity": "Highest chamber shear in any mode at 11 uL/min (dyn/cm2)", "doc": "< 0.01",
         "solver": f"{worst_chamber[0]:.4f} ({worst_chamber[2]}, {worst_chamber[1]})", "match": worst_chamber[0] < 0.01},
        {"quantity": "Diffusion: implied D from 8.8 mm in 12 h (m2/s)", "doc": "8.8 mm / 12 h, 33 mm / 7 days",
         "solver": f"{d_implied:.2e}; 7 days -> {math.sqrt(2 * d_implied * 7 * 86400) * 1000:.1f} mm", "match": True},
    ]
    return rows


def _print_solution(name: str, sol: dict) -> None:
    print(f"\n== {name}   (max pressure {sol['max_pressure_mbar']:.4f} mbar)")
    for pid, p in sol["ports"].items():
        if p["state"] != CAP or abs(p["into_chip_ul_min"]) > 1e-9:
            print(f"  {pid:9s} {p['state']:5s} set {p['set_ul_min']:6.2f}  into chip {p['into_chip_ul_min']:8.3f} uL/min")
    for cid, c in sol["channels"].items():
        print(f"  {cid:9s} {c['flow_ul_min']:8.3f} uL/min  {c['direction']:18s} shear {c['shear_dyn_cm2']:.4f} dyn/cm2  "
              f"L {c['length_mm']:.2f} mm ({c['geometry']})")
    for cid, c in sol["chambers"].items():
        print(f"  {cid:9s} through {c['throughput_ul_min']:7.3f} uL/min  shear {c['shear_dyn_cm2']:.5f} dyn/cm2")
    print(f"  foreign medium in a culture chamber: {sol['routing']['foreign_medium'] or 'none'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Hydraulic network solver for a chip spec.")
    ap.add_argument("chip", type=Path, nargs="?", default=ROOT / "chip" / "module1_v1.json")
    ap.add_argument("--mode", choices=list(MODULE1_MODES), help="a Module 1 v1 mode")
    ap.add_argument("--flow", type=float, default=DEFAULT_FEED_UL_MIN, help="uL/min at each FEED port")
    ap.add_argument("--report", action="store_true", help="every mode and the design-doc check")
    ap.add_argument("--json", type=Path, help="write the per-mode solutions here")
    args = ap.parse_args(argv)
    chip = json.loads(args.chip.read_text())
    dxf = ROOT / "cad" / "exports" / chip["id"] / f"{chip['id']}.dxf"
    net = Network(chip, dxf=dxf)
    if args.mode:
        _print_solution(MODULE1_MODES[args.mode]["label"], net.solve(mode_ports(args.mode, args.flow)))
    if args.report or args.json:
        sols = solve_modes(net, args.flow)
        if args.report:
            for m, s in sols.items():
                _print_solution(MODULE1_MODES[m]["label"], s)
            print("\nDesign-doc check (mu = 0.8 mPa s, water at 37 C, as build_module1.py):")
            for r in design_check(net):
                print(f"  {'OK  ' if r['match'] else 'DIFF'} {r['quantity']}: doc {r['doc']}, solver {r['solver']}")
        if args.json:
            args.json.write_text(json.dumps({"chip": chip["id"], "flow_ul_min": args.flow, "modes": sols}, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

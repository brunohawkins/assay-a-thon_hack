"""Module 1 v1 operator (MJ's valve-free culture chip).

The chip has no valves, so the operator's whole command set is the six ports' states and
the pump rates:

    set_ports(intent, states={port: FEED|VENT|CAP}, flows={port: uL/min}, reason)

Every change is checked first with the network solver (operator/network.py):
  BLOCK  a culture chamber would receive another chamber's medium during separate growth
  BLOCK  wall shear in a culture chamber above 0.01 dyn/cm2
  BLOCK  liquid fed with nowhere to go (nothing vented)
  WARN   a FEED line the rest of the chip pushes back on: with a stalled pump, or if it were
         pressure-fed, flow would reverse into it (protocols/module1_design.md: "a fed port is
         a pressure source")
  WARN   more ports vented than the mode intends (e.g. only p_mid in separate growth)

    python operator/module1_operator.py                       # scripted (offline) decisions
    python operator/module1_operator.py --operator llm        # Claude decides (ANTHROPIC_API_KEY); else scripted
    python operator/module1_operator.py --times --flow 50     # predicted dye times for the bench demo

Writes runs/module1_v1/operator_<scripted|llm>.json for the twin.
"""

from __future__ import annotations

import argparse
import heapq
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import network as nw

ROOT = nw.ROOT
OUT_DIR = ROOT / "runs" / "module1_v1"
PORTS = ["p_in", "p_g_in", "p_a_in", "p_mid", "p_g_out", "p_a_out"]
CULTURE = ["n_cx", "g_cx", "a_cx"]
SHEAR_CEILING = 0.01           # dyn/cm2, culture chambers
BACKPRESSURE_WARN = 0.7        # warn when the rest of the chip pushes back with >= 70% of a feed's own pressure
PORT_HOLE_UL = nw.PORT_HOLE_MM * nw.PORT_HOLE_MM * 3.0  # the port bore (contracts/blocks.json) through the 3 mm lid
FIRST_TINT = 2.0 / 3.0         # a slot's centre moves at 1.5x the mean: first colour at ~2/3 of the mean transit
BENCH_FLOW_UL_MIN = 50.0       # default syringe rate for the dye demo (no cells: shear doesn't matter)
NAMES = {"n_cx": "N", "g_cx": "G", "a_cx": "A"}


def states_of(mode: str, flow: float | dict = nw.DEFAULT_FEED_UL_MIN) -> tuple[dict, dict]:
    m = nw.MODULE1_MODES[mode]
    states = {p: ("FEED" if p in m["feed"] else "VENT" if p in m["vent"] else "CAP") for p in PORTS}
    flows = {p: (flow[p] if isinstance(flow, dict) else flow) for p in m["feed"]}
    return states, flows


def solver_ports(states: dict, flows: dict) -> dict:
    return {p: (("FEED", flows.get(p, 0.0)) if s == "FEED" else s) for p, s in states.items()}


# --- the key result of a mode, in one line --------------------------------------------------


def key_result(intent: str | None, sol: dict) -> str:
    ch = sol["chambers"]
    foreign = sol["routing"]["foreign_medium"]
    label = nw.MODULE1_MODES.get(intent, {}).get("label", "Custom")
    vent_out = {p: -v["into_chip_ul_min"] for p, v in sol["ports"].items() if v["state"] == "VENT"}
    if intent == "grow":
        return (f"{label}: no medium enters another chamber; {sum(vent_out.values()):.1f} uL/min leaves p_mid"
                if not foreign else f"{label}: NOT isolated: {_foreign_text(foreign)}")
    if intent in ("int_n", "int_g", "int_a"):
        src = {"int_n": "n_cx", "int_g": "g_cx", "int_a": "a_cx"}[intent]
        others = [c for c in CULTURE if c != src]
        tot = sum(ch[c]["throughput_ul_min"] for c in others) or 1
        split = " / ".join(f"{ch[c]['throughput_ul_min'] / tot:.0%} to {c}" for c in others)
        return f"{label}: {src} medium splits at j_mid: {split}"
    if intent == "harvest":
        swept = [c for c in CULTURE if ch[c]["throughput_ul_min"] > 1e-9]
        dead = [c for c in CULTURE if c not in swept]
        return (f"{label}: only {', '.join(swept)} is swept to p_mid; {', '.join(dead)} see no flow (the protocol's "
                f"collect mode back-flushes them)" if dead else f"{label}: all three chambers sweep into j_mid and out of p_mid")
    if intent == "collect":
        return f"{label}: all three chambers sweep into j_mid and pool out of p_mid ({sum(vent_out.values()):.1f} uL/min)"
    if intent == "seed":
        return f"{label}: n_cx feeds g_cx and a_cx through j_mid ({ch['g_cx']['throughput_ul_min'] / max(ch['n_cx']['throughput_ul_min'], 1e-9):.0%} each); most cells settle in n_cx first"
    if intent == "idle":
        return "Idle: every port capped, no flow; the chambers are joined by open channels, so only diffusion crosses"
    return f"{label}: " + (_foreign_text(foreign) if foreign else "no chamber receives another chamber's medium")


def _foreign_text(foreign: dict) -> str:
    return "; ".join(f"{c} receives {', '.join(f'{f:.0%} {s} medium' for s, f in srcs.items())}" for c, srcs in foreign.items())


# --- safety -----------------------------------------------------------------------------------


def check(net: nw.Network, intent: str | None, states: dict, flows: dict) -> dict:
    """Solve, then apply the rules. Returns verdict (approved / warning / blocked) with reasons."""
    blocked, warnings = [], []
    try:
        sol = net.solve(solver_ports(states, flows))
    except nw.NetworkError as e:
        return {"status": "blocked", "reasons": [str(e)], "warnings": [], "solution": None, "summary": f"blocked: {e}"}
    foreign = sol["routing"]["foreign_medium"]
    if intent == "grow" and foreign:
        blocked.append(f"separate growth must keep the media apart, but {_foreign_text(foreign)}")
    for c in CULTURE:
        tau = sol["chambers"][c]["shear_dyn_cm2"]
        if tau > SHEAR_CEILING:
            blocked.append(f"{c} wall shear {tau:.4f} dyn/cm2 > {SHEAR_CEILING} (throughput {sol['chambers'][c]['throughput_ul_min']:.1f} uL/min; "
                           f"the ceiling is reached at {nw.threshold_flow(12, 0.3, SHEAR_CEILING, net.mu):.1f})")
    p = sol["pressure_pa"]
    for port, s in states.items():
        if s != "FEED":
            continue
        q = flows.get(port, 0.0)
        alt = dict(states)
        if q <= 0:
            alt[port] = "VENT"     # a stopped pump on an open line behaves like a vent
            try:
                back = net.solve(solver_ports(alt, flows))["ports"][port]["into_chip_ul_min"]
            except nw.NetworkError:
                back = 0.0
            if back < -1e-6:
                warnings.append(f"{port} is FEED at 0 uL/min: with its pump stopped the line is open, and "
                                f"{-back:.2f} uL/min flows back into it")
            continue
        alt[port] = "CAP"
        try:
            pb = net.solve(solver_ports(alt, flows))["pressure_pa"][port]
        except nw.NetworkError:
            continue
        if p[port] > 0 and pb >= BACKPRESSURE_WARN * p[port]:
            warnings.append(f"{port}: the rest of the chip pushes back at {pb / 100:.4f} mbar, {pb / p[port]:.0%} of its own "
                            f"{p[port] / 100:.4f} mbar; if its pump stalls or it is pressure-fed, flow reverses into it")
    if intent in nw.MODULE1_MODES:
        want = set(nw.MODULE1_MODES[intent]["vent"])
        extra = sorted(port for port, s in states.items() if s == "VENT" and port not in want)
        if extra and want:
            warnings.append(f"{', '.join(extra)} also vented: {nw.MODULE1_MODES[intent]['label'].lower()} should vent only "
                            f"{', '.join(sorted(want))}")
    status = "blocked" if blocked else "warning" if warnings else "approved"
    return {"status": status, "reasons": blocked, "warnings": warnings, "solution": sol,
            "summary": key_result(intent, sol)}


# --- dye times ---------------------------------------------------------------------------------


def dye_times(net: nw.Network, states: dict, flows: dict) -> dict:
    """Seconds for each fed port's dye to reach j_mid and each vented port, at these rates.
    Plug flow through every part ('mean'), and the first tint at ~2/3 of it ('first')."""
    sol = net.solve(solver_ports(states, flows))
    chambers = {c["id"]: c for c in net.chip["chambers"]}
    edges = []
    for e in net.edges:
        q = sol["channels"][e["id"]]["flow_ul_min"]
        if abs(q) < 1e-9:
            continue
        a, b = (e["from"], e["to"]) if q > 0 else (e["to"], e["from"])
        edges.append((a, b, sol["channels"][e["id"]]["volume_ul"] / abs(q) * 60))
    through = {c: sol["chambers"][c]["throughput_ul_min"] for c in chambers}
    out = {}
    targets = ["j_mid"] + [p for p, s in states.items() if s == "VENT"]
    for src, s in states.items():
        if s != "FEED" or flows.get(src, 0) <= 0:
            continue
        start = PORT_HOLE_UL / flows[src] * 60           # through the inlet hole first
        best = {src: start}
        heap = [(start, src)]
        while heap:
            t, n = heapq.heappop(heap)
            if t > best.get(n, float("inf")):
                continue
            for a, b, dt in edges:
                if a != n:
                    continue
                tb = t + dt + (chambers[b]["volume_ul"] / through[b] * 60 if b in chambers and b != "j_mid" and through[b] > 0 else 0)
                if tb < best.get(b, float("inf")):
                    best[b] = tb
                    heapq.heappush(heap, (tb, b))
        out[src] = {t: {"mean_s": round(best[t], 1), "first_s": round(best[t] * FIRST_TINT, 1)} for t in targets if t in best}
    return out


# --- operators -----------------------------------------------------------------------------------

SCRIPT = [
    ("grow", {"p_in": "FEED", "p_mid": "VENT"}, "Seed and feed n_cx on its own: p_in fed, p_mid vented, everything else capped."),
    ("grow", None, "Separate growth: all three inlets fed with their own media, only p_mid vented."),
    ("grow", {"p_in": "FEED", "p_g_in": "FEED", "p_a_in": "FEED", "p_mid": "VENT", "p_g_out": "VENT"},
     "Try venting p_g_out as well, to refresh g_cx faster."),
    ("grow", {"p_in": "FEED", "p_g_in": "FEED", "p_a_in": "FEED", "p_g_out": "VENT"},
     "Vent p_g_out instead of p_mid (a plug left in p_mid by mistake)."),
    ("grow", None, "Back to separate growth: p_mid vented, p_g_out capped."),
    ("int_n", None, "Interaction: send n_cx's conditioned medium to g_cx and a_cx."),
    ("int_g", None, "Interaction: send g_cx's conditioned medium to n_cx and a_cx."),
    ("int_a", None, "Interaction: send a_cx's conditioned medium to n_cx and g_cx."),
    ("harvest", None, "Harvest as the design doc tabulates it: feed p_in, vent p_mid."),
    ("collect", None, "Collect as the protocol does it: back-flush g_cx and a_cx from their outlets too, so all three pool."),
    ("idle", None, "Stop: all ports capped."),
]


def decide(net, intent, states, flows, reason, source):
    v = check(net, intent, states, flows)
    sol = v.pop("solution")
    rec = {"intent": intent, "states": states, "flows": flows, "reason": reason, "operator": source, **v}
    if sol:
        rec["predicted"] = {
            "channels": {k: {"flow_ul_min": c["flow_ul_min"], "direction": c["direction"]} for k, c in sol["channels"].items()},
            "chambers": {k: {"throughput_ul_min": c["throughput_ul_min"], "shear_dyn_cm2": c["shear_dyn_cm2"]} for k, c in sol["chambers"].items()},
            "max_pressure_mbar": sol["max_pressure_mbar"]}
    return rec


def run_scripted(net, flow: float) -> list[dict]:
    out = []
    applied = None
    for intent, ports, reason in SCRIPT:
        if ports is None:
            states, flows = states_of(intent, flow)
        else:
            states = {p: ports.get(p, "CAP") for p in PORTS}
            flows = {p: flow for p, s in states.items() if s == "FEED"}
        rec = decide(net, intent, states, flows, reason, "scripted")
        rec["applied"] = rec["status"] != "blocked"
        if rec["applied"]:
            applied = rec
        else:
            rec["kept"] = applied["intent"] if applied else None
        out.append(rec)
    return out


LLM_SYSTEM = """You operate Module 1 v1, a valve-free three-chamber culture chip (n_cx neurons, g_cx glia,
a_cx astrocytes, junction j_mid). Your only command is set_ports: each of the six ports p_in, p_g_in,
p_a_in, p_mid, p_g_out, p_a_out is FEED (syringe pump, uL/min), VENT (open) or CAP (closed). A safety
checker solves the flows before anything changes and blocks changes that mix media during separate
growth or exceed 0.01 dyn/cm2 in a chamber. Walk the chip through: separate growth, the three
interaction modes (N->G+A, G->N+A, A->N+G), then collect all three chambers to p_mid, then idle.
Explain each decision in one sentence."""


def run_llm(net, flow: float, api_key: str, max_calls: int = 20) -> list[dict]:
    import llm_operator as llm

    tool = {"name": "set_ports", "description": "Set every port's state and the feed rates. Checked by the safety solver first.",
            "input_schema": {"type": "object", "additionalProperties": False, "required": ["intent", "states", "flows", "reason"],
                             "properties": {
                                 "intent": {"type": "string", "enum": list(nw.MODULE1_MODES) + ["custom"]},
                                 "states": {"type": "object", "additionalProperties": False, "required": PORTS,
                                            "properties": {p: {"type": "string", "enum": ["FEED", "VENT", "CAP"]} for p in PORTS}},
                                 "flows": {"type": "object", "additionalProperties": {"type": "number"},
                                           "description": "uL/min for each FEED port"},
                                 "reason": {"type": "string"}}}}
    messages = [{"role": "user", "content": f"Default feed rate {flow} uL/min. Start."}]
    out = []
    for _ in range(max_calls):
        resp = llm.call_messages_api({"model": llm.MODEL, "max_tokens": 4000, "system": LLM_SYSTEM, "tools": [tool],
                                      "messages": messages}, api_key)
        messages.append({"role": "assistant", "content": resp["content"]})
        calls = [b for b in resp["content"] if b.get("type") == "tool_use"]
        if not calls:
            break
        results = []
        for c in calls:
            inp = c["input"]
            rec = decide(net, None if inp["intent"] == "custom" else inp["intent"], inp["states"], inp.get("flows", {}),
                         inp["reason"], f"llm ({llm.MODEL})")
            rec["applied"] = rec["status"] != "blocked"
            out.append(rec)
            results.append({"type": "tool_result", "tool_use_id": c["id"], "content": json.dumps(
                {k: rec[k] for k in ("status", "reasons", "warnings", "summary")})})
        messages.append({"role": "user", "content": results})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Module 1 v1 operator: port states and pump rates only.")
    ap.add_argument("--operator", choices=["scripted", "llm"], default="scripted")
    ap.add_argument("--flow", type=float, default=nw.DEFAULT_FEED_UL_MIN, help="uL/min per FEED port")
    ap.add_argument("--times", action="store_true", help="print predicted dye times per mode at --flow")
    args = ap.parse_args(argv)
    net = nw.module1_network()
    if args.times:
        for mode in ("grow", "int_n", "int_g", "int_a", "harvest", "seed", "collect"):
            states, flows = states_of(mode, args.flow)
            print(f"{nw.MODULE1_MODES[mode]['label']} at {args.flow:g} uL/min per syringe:")
            for src, t in dye_times(net, states, flows).items():
                print(f"  dye from {src}: " + ", ".join(f"{k} {v['first_s'] / 60:.1f}-{v['mean_s'] / 60:.1f} min" for k, v in t.items()))
        return 0
    kind = args.operator
    if kind == "llm" and not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        print("ANTHROPIC_API_KEY is not set: using the scripted (offline) operator.")
        kind = "scripted"
    decisions = run_scripted(net, args.flow) if kind == "scripted" else run_llm(net, args.flow, os.environ["ANTHROPIC_API_KEY"])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"operator_{kind}.json"
    out.write_text(json.dumps({"chip": "module1_v1", "operator": kind, "flow_ul_min": args.flow,
                               "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               "note": "scripted = a fixed decision list (offline demo), not model output" if kind == "scripted" else "",
                               "decisions": decisions}, indent=1) + "\n")
    for d in decisions:
        print(f"[{d['status']:8s}] {d['intent'] or 'custom':8s} {d['reason']}\n           -> {d['summary']}"
              + "".join(f"\n           BLOCK {r}" for r in d["reasons"]) + "".join(f"\n           WARN  {w}" for w in d["warnings"]))
    print(f"wrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

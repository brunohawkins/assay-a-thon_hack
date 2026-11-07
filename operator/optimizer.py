"""Optimizer: picks the parameters for one protocol step by simulating candidates.

    plan_step(step, believed, model, chip) -> plan dict

For the step's goal (e.g. exchange to >= 99%, lift >= 95% of capsules, recover >= 98%):
  1. generate candidate parameter sets (flow rate, volume per wash, washes, ramp rate)
  2. simulate each with the process model from the CURRENT believed state
  3. discard any the safety checker blocks, that miss the goal, or whose peak pressure
     is above control.pressure_margin of the escape pressure
  4. choose by objective: least time, then least reagent, then lowest peak pressure
Returns the chosen plan and the full comparison table (with why each loser lost).
"""

from __future__ import annotations

import math

import safety
import schema_io
from model import ProcessModel
from state import ChipState

OBJECTIVE = "least time, then least reagent, then lowest peak pressure; peak pressure must stay below the planning margin"


def _param(key: str) -> float:
    params, _ = safety._contracts()
    return float(params["parameters"][key]["value"])


def goal_for_step(step: dict, chip: dict) -> dict:
    """What a protocol step is trying to achieve, in numbers the model can check."""
    op = step["operation"]
    g = {"step": step["step"], "operation": op, "target": step["target"], "reagent": step.get("reagent"),
         "volume_ul": step.get("volume_ul"), "note": step.get("note", "")}
    if op == "exchange" and step.get("direction") == "backflush":
        g.update(kind="backflush", fraction=_param("backflush.target_fraction"))
    elif op == "exchange":
        g.update(kind="exchange", fraction=_param("exchange.target_fraction"),
                 interior_fraction=_param("exchange.interior_target_fraction"))
    elif op == "release":
        g.update(kind="release", fraction=_param("release.target_fraction"))
    elif op == "load":
        g.update(kind="load", capsules=float(chip.get("capsule", {}).get("count", 24)))
    elif op == "hold_temp":
        g.update(kind="hold", temp_c=step["temp_c"], duration_s=step["duration_s"])
    else:
        g.update(kind="move")
    return g


def _slices(command: str, args: dict, n: int) -> list[dict]:
    """Split a plan-level command into n equal time slices."""
    total = args["volume_ul"] * args.get("washes", 1)
    per = total / n
    base = {k: v for k, v in args.items() if k not in ("washes", "soak_s")}
    return [{**base, "volume_ul": per} for _ in range(n)]


def simulate(command: str, args: dict, believed: ChipState, model: ProcessModel, slices_per_wash: int) -> dict:
    """Run a plan through the model. Returns predicted metrics and the end state."""
    s = believed.copy()
    target = args["target"]
    peak, lost, reservoir_short = 0.0, 0.0, False
    if command in ("exchange", "load", "release"):
        n = max(1, slices_per_wash * int(args.get("washes", 1)))
        for sl in _slices(command, args, n):
            if command == "load":
                sl["capsules"] = args.get("capsules", 0) / n
            s, out = model.predict(s, command, sl)
            peak = max(peak, out["pressure_kpa"]) if args.get("direction") != "backflush" else min(peak, out["pressure_kpa"])
            lost += out["capsules_out_waste"]
            reservoir_short |= out["reservoir_short"]
        if args.get("soak_s"):
            s, _ = model.predict(s, "wait", {"duration_s": args["soak_s"]})
    elif command == "hold_temp":
        ramp_time = abs(args["temp_c"] - s.chambers[target].temp_c) / args.get("ramp_c_per_s", 1.0)
        s, _ = model.predict(s, "hold_temp", {**args, "duration_s": ramp_time + args["duration_s"]})
    else:
        s, _ = model.predict(s, command, args)
    ch = s.chambers[target] if target in s.chambers else None
    return {"state": s, "time_s": s.clock_s - believed.clock_s, "peak_kpa": peak, "capsules_lost": lost,
            "final_fraction": ch.concentration if ch else None,
            "interior_fraction": ch.capsule_concentration if ch else None,
            "reservoir_short": reservoir_short}


def _soak_needed(c: float, ci: float, target: float, tau: float) -> float:
    """Seconds of no-flow soak for the capsule interior to reach target (inf if impossible)."""
    if ci >= target:
        return 0.0
    if c <= target:
        return math.inf
    return tau * math.log((c - ci) / (c - target))


def candidates_for(goal: dict, believed: ChipState, model: ProcessModel) -> list[tuple[str, dict]]:
    t = goal["target"]
    vc = model.params.chamber_volume_ul.get(t, 10.0)
    v = float(goal.get("volume_ul") or 2 * vc)
    if goal["kind"] == "exchange":
        return [("exchange", {"target": t, "reagent": goal["reagent"], "volume_ul": v, "washes": w, "flow_ul_min": q})
                for q in (10, 20, 40, 80) for w in (1, 2, 3, 4)]
    if goal["kind"] == "backflush":
        return [("exchange", {"target": t, "reagent": goal["reagent"], "volume_ul": round(m * v, 1), "washes": 1,
                              "flow_ul_min": q, "direction": "backflush"})
                for q in (15, 30, 45, 60) for m in (0.5, 1.0)]
    if goal["kind"] == "release":
        return [("release", {"target": t, "reagent": goal["reagent"], "volume_ul": round(m * vc, 1), "flow_ul_min": q})
                for q in (10, 20, 40) for m in (2, 4, 6)]
    if goal["kind"] == "load":
        return [("load", {"target": t, "volume_ul": v, "flow_ul_min": q, "capsules": goal["capsules"]}) for q in (10, 20, 40)]
    if goal["kind"] == "hold":
        return [("hold_temp", {"target": t, "temp_c": goal["temp_c"], "duration_s": goal["duration_s"], "ramp_c_per_s": r})
                for r in (0.5, 1.0, 2.0)]
    return [(goal["operation"], {k: goal[k] for k in ("target",) if goal.get(k)})]


def plan_step(goal: dict, believed: ChipState, model: ProcessModel, chip: dict) -> dict:
    margin = _param("control.pressure_margin") * model.params.escape_pressure_kpa
    spw = int(_param("control.slices_per_wash"))
    rows = []
    for i, (command, args) in enumerate(candidates_for(goal, believed, model)):
        sim = simulate(command, args, believed, model, spw)
        row = {"id": i, "command": command, "flow_ul_min": args.get("flow_ul_min"), "volume_ul": args.get("volume_ul"),
               "washes": args.get("washes"), "ramp_c_per_s": args.get("ramp_c_per_s"), "args": args}
        reason = None
        if goal["kind"] == "exchange":
            soak = _soak_needed(sim["final_fraction"], sim["interior_fraction"], goal["interior_fraction"], model.params.diffusion_time_s)
            if math.isfinite(soak) and soak > 0:
                args["soak_s"] = round(soak, 1)
                sim = simulate(command, args, believed, model, spw)
            if sim["final_fraction"] < goal["fraction"]:
                reason = f"predicted exchange {100 * sim['final_fraction']:.1f}% < goal {100 * goal['fraction']:.0f}%"
        elif goal["kind"] == "backflush":
            ch = sim["state"].chambers[goal["target"]]
            lifted = ch.capsules_suspended / max(ch.capsules, 1e-9)
            row["lifted_pct"] = round(100 * lifted, 1)
            if lifted < goal["fraction"]:
                reason = f"lifts {100 * lifted:.0f}% of capsules < goal {100 * goal['fraction']:.0f}%"
        elif goal["kind"] == "release":
            before = believed.chambers[goal["target"]].capsules_suspended
            rec = (sim["state"].capsules_collected - believed.capsules_collected) / before if before > 0 else 1.0
            row["recovered_pct"] = round(100 * rec, 1)
            if rec < goal["fraction"]:
                reason = f"recovers {100 * rec:.0f}% of suspended capsules < goal {100 * goal['fraction']:.0f}%"
        verdict = safety.check(command, args, believed, chip, model)
        row["safety"] = verdict.to_dict()
        if verdict.status == "blocked":
            reason = "safety: " + verdict.reasons[0]
        elif reason is None and goal["kind"] in ("exchange", "load") and sim["peak_kpa"] > margin:
            reason = f"peak {sim['peak_kpa']:.2f} kPa above the {margin:.1f} kPa planning margin"
        row.update(time_s=round(sim["time_s"], 1),
                   reagent_ul=round(args.get("volume_ul", 0) * args.get("washes", 1), 1) if command != "hold_temp" else 0.0,
                   final_pct=None if sim["final_fraction"] is None else round(100 * sim["final_fraction"], 2),
                   interior_pct=None if sim["interior_fraction"] is None else round(100 * sim["interior_fraction"], 2),
                   peak_kpa=round(sim["peak_kpa"], 3), soak_s=args.get("soak_s", 0.0),
                   status="rejected" if reason else "ok", reason=reason)
        rows.append(row)

    ok = [r for r in rows if r["status"] == "ok"]
    chosen = min(ok, key=lambda r: (r["time_s"], r["reagent_ul"], abs(r["peak_kpa"]))) if ok else None
    if chosen:
        chosen["status"] = "chosen"
        for r in ok:
            if r is not chosen:
                r["reason"] = "slower" if r["time_s"] > chosen["time_s"] else "uses more reagent"
    return {
        "step": goal["step"], "goal": {k: v for k, v in goal.items() if k != "note"}, "objective": OBJECTIVE,
        "command": chosen["command"] if chosen else None, "args": dict(chosen["args"]) if chosen else None,
        "predicted": {k: chosen[k] for k in ("time_s", "reagent_ul", "final_pct", "interior_pct", "peak_kpa")} if chosen else None,
        "candidates": [{k: v for k, v in r.items() if k != "args"} for r in rows],
        "feasible": bool(chosen),
    }


if __name__ == "__main__":
    import sys
    from model import ModelParams
    chip = schema_io.load_json(schema_io.ROOT / "cad" / "wash_mvp_spec.json")
    params, _ = safety._contracts()
    model = ProcessModel(ModelParams.from_chip(chip, params))
    s = ChipState.from_chip_spec(chip, params, reagents=["capsule_suspension", "wash_buffer", "resuspension_buffer"])
    s.chambers["trap_1"].capsules_on_mesh = 10000
    step = {"step": 2, "operation": "exchange", "target": "trap_1", "reagent": "wash_buffer", "volume_ul": 20}
    plan = plan_step(goal_for_step(step, chip), s, model, chip)
    print("chosen:", plan["args"], plan["predicted"])
    for r in plan["candidates"]:
        print(f"  {r['flow_ul_min']:>4} uL/min x{r['washes']}  {r['time_s']:>6.1f}s  {r['final_pct']:>6.2f}%  {r['peak_kpa']:.2f} kPa  "
              f"{r['status']:8s} {r['reason'] or ''}")
    sys.exit(0)

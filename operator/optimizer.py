"""Optimizer: picks the parameters for one protocol step by simulating candidates.

    plan_step(step, believed, model, chip) -> plan dict

For the step's goal (e.g. sweep the chamber to >= 99%, fill it once, lift >= 95% of the
capsules, recover >= 98%):
  1. generate candidate parameter sets (flow rate, volume per wash, washes, ramp rate)
  2. simulate each with the process model from the CURRENT believed state
  3. discard any the safety checker blocks, that miss the goal, or whose peak pressure
     is above control.pressure_margin of the escape pressure
  4. choose by objective: least time, then least reagent, then lowest peak pressure
Returns the chosen plan and the full comparison table (with why each loser lost).

WHAT AN EXCHANGE IS FOR decides its goal, and it is a data field, not prose: a step's
`exchange_kind` (contracts/schemas/protocol.schema.json) is "sweep", "fill" or "partial".
  sweep    clear the chamber: must reach exchange.target_fraction (the strict default, so
           a step that declares nothing is held to this).
  fill     first filling of an empty or air-filled chamber: the goal is that the stated
           volume (at most one chamber volume) goes in. Judging a fill against a 99%
           displacement asks it to clear out something that was never there.
  partial  a deliberate fractional change (a routine medium half-change): the goal is the
           replaced fraction the step's own volume implies. It must NOT inherit the sweep
           target - leaving conditioned medium behind is the point of the step.
"""

from __future__ import annotations

import math

import planner
import safety
import schema_io
from model import ProcessModel
from state import ChipState

OBJECTIVE = "least time, then least reagent, then lowest peak pressure; peak pressure must stay below the planning margin"

# Flows offered for an exchange, in uL/min. Safety and the planning margin cut them down.
EXCHANGE_FLOWS = (10, 20, 40, 80)


def _param(key: str) -> float:
    params, _ = safety._contracts()
    return float(params["parameters"][key]["value"])


def chip_chamber_volume_ul(chip: dict, chamber_id: str | None) -> float | None:
    """The chamber's volume as the SPEC states it: the design intent a protocol's volumes
    were written against, which is what a fill or a partial change is measured in. (The
    model's own chamber_volume_ul drifts as the closed loop refits it.)"""
    for c in chip.get("chambers", []):
        if c["id"] == chamber_id:
            return float(c["volume_ul"])
    return None


def replaced_fraction(volume_ul: float, chamber_volume_ul: float) -> float:
    """Fraction of a well-mixed chamber's liquid replaced by pushing volume_ul through it."""
    if chamber_volume_ul <= 0:
        return 0.0
    return 1 - math.exp(-volume_ul / chamber_volume_ul)


def goal_for_step(step: dict, chip: dict) -> dict:
    """What a protocol step is trying to achieve, in numbers the model can check.

    Every operation the pipeline uses gets a goal. This used to index step["target"]
    unconditionally and drop `targets`/`sources`, so culture, split, pool, collect and
    harvest - which name their chambers in `targets`/`sources` - raised KeyError or
    ValueError and could not be planned at all. The step -> (command, args) mapping is
    planner.step_to_command, so the closed loop plans exactly the command the simple
    planner would have sent, including the chip-derived split/pool fan-out.
    """
    op = step["operation"]
    # An exchange with several targets is one command per target (planner.step_to_commands);
    # the goal is written for the first and `fan_out` names them all, so the controller can
    # run the same plan through each chamber in turn instead of planning a target of None.
    fan_out = [c[1]["target"] for c in planner.step_to_commands(step, chip)] if (
        op == "exchange" and step.get("targets")) else []
    command, args = planner.step_to_command({**step, "targets": None, "target": fan_out[0]} if fan_out else step, chip)
    target = args.get("target") or args.get("source") or (list(args.get("targets") or []) or [None])[0]
    g = {"step": step["step"], "operation": op, "command": command, "args": dict(args),
         "target": target, "targets": list(args.get("targets") or []),
         "sources": list(args.get("sources") or []), "fan_out": fan_out,
         "reagent": step.get("reagent"), "volume_ul": step.get("volume_ul"), "note": step.get("note", "")}
    if op == "exchange" and step.get("direction") == "backflush":
        g.update(kind="backflush", fraction=_param("backflush.target_fraction"))
    elif op == "exchange":
        # ABSENT means sweep: the strict default, so an unclassified exchange cannot slip
        # through on a weaker goal than the one it would have been held to.
        exchange_kind = step.get("exchange_kind") or "sweep"
        vc = chip_chamber_volume_ul(chip, target)
        g.update(kind="exchange", exchange_kind=exchange_kind, chamber_volume_ul=vc,
                 washes=int(step.get("washes") or 1))
        if exchange_kind == "sweep":
            g.update(fraction=_param("exchange.target_fraction"),
                     interior_fraction=_param("exchange.interior_target_fraction"))
        elif exchange_kind == "fill":
            # A fill is bounded ABOVE by the chamber's volume and has no lower bound: it
            # ends with the chamber full of the new liquid.
            stated = float(step.get("volume_ul") or vc or 0.0)
            g["fill_volume_ul"] = min(stated, vc) if vc else stated
            g["expected_fraction"] = replaced_fraction(g["fill_volume_ul"], vc) if vc else None
        else:   # partial
            total = float(step.get("volume_ul") or 0.0) * int(step.get("washes") or 1)
            g["expected_fraction"] = replaced_fraction(total, vc) if vc else None
    elif op == "release":
        g.update(kind="release", fraction=_param("release.target_fraction"))
    elif op == "load":
        g.update(kind="load", capsules=float(chip.get("capsule", {}).get("count", 24)))
    elif op == "hold_temp":
        g.update(kind="hold", temp_c=step["temp_c"], duration_s=step["duration_s"])
    else:
        # culture, split, pool, collect, harvest, mix, transfer, count, encapsulate, cure:
        # the step states its own parameters, so there is nothing for the optimizer to
        # search. It still simulates and gets a safety verdict like any other plan.
        g.update(kind="as_written")
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
    target = args.get("target") or args.get("source") or (list(args.get("targets") or []) or [None])[0]
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
    delivered = float(args.get("volume_ul") or 0.0) * int(args.get("washes", 1) or 1) if command in (
        "exchange", "load", "release") else 0.0
    return {"state": s, "time_s": s.clock_s - believed.clock_s, "peak_kpa": peak, "capsules_lost": lost,
            "final_fraction": ch.concentration if ch else None,
            "interior_fraction": ch.capsule_concentration if ch else None,
            "volume_ul": delivered, "reservoir_short": reservoir_short}


def _soak_needed(c: float, ci: float, target: float, tau: float) -> float:
    """Seconds of no-flow soak for the capsule interior to reach target (inf if impossible)."""
    if ci >= target:
        return 0.0
    if c <= target:
        return math.inf
    return tau * math.log((c - ci) / (c - target))


def exchange_flows(goal: dict) -> list[float]:
    """Flows to try for an exchange: the fixed ladder plus the flow the step itself states.

    The step's own flow matters because it is where a chip's own limits ended up: a culture
    chamber's protocol flow is derived from shear.culture_ceiling_dyn_cm2, and every flow on
    the ladder is many times over that ceiling, so without this the safety checker rightly
    blocked all of them and a perfectly good culture step had no feasible plan at all.
    """
    stated = (goal.get("args") or {}).get("flow_ul_min")
    flows = set(EXCHANGE_FLOWS)
    if stated:
        flows.add(float(stated))
    return sorted(flows)


def candidates_for(goal: dict, believed: ChipState, model: ProcessModel) -> list[tuple[str, dict]]:
    t = goal.get("target")
    vc = model.params.chamber_volume_ul.get(t, 10.0)
    v = float(goal.get("volume_ul") or 2 * vc)
    if goal["kind"] == "exchange":
        kind = goal.get("exchange_kind", "sweep")
        flows = exchange_flows(goal)
        if kind == "fill":
            # One pass of the stated volume (never more than the chamber holds); the only
            # choice left is how fast.
            fill = float(goal.get("fill_volume_ul") or min(v, vc))
            return [("exchange", {"target": t, "reagent": goal["reagent"], "volume_ul": fill, "washes": 1,
                                  "flow_ul_min": q}) for q in flows]
        if kind == "partial":
            # The replaced fraction is deliberate, so neither the volume nor the number of
            # washes is the optimizer's to change: more would wash out the conditioned
            # medium the step is keeping.
            washes = max(1, int(goal.get("washes") or 1))
            return [("exchange", {"target": t, "reagent": goal["reagent"], "volume_ul": v, "washes": washes,
                                  "flow_ul_min": q}) for q in flows]
        return [("exchange", {"target": t, "reagent": goal["reagent"], "volume_ul": v, "washes": w, "flow_ul_min": q})
                for q in flows for w in (1, 2, 3, 4)]
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
    # Everything else runs exactly as the protocol wrote it, with its own targets, sources,
    # reagents, volumes and flows. The fallback used to rebuild the args from the single key
    # "target", which threw away `targets` and `sources` and made a culture, split, pool,
    # collect or harvest step unplannable (and then uncheckable).
    return [(goal.get("command") or goal["operation"], dict(goal.get("args") or {}))]


def plan_step(goal: dict, believed: ChipState, model: ProcessModel, chip: dict,
              run_params: dict | None = None, lyse_ahead: bool = False) -> dict:
    """Plan one step. `run_params` (the run's variant, from the chip's run_parameters) and
    `lyse_ahead` are passed straight to the safety checker: without the first, no rule that
    depends on which variant is running can ever fire here."""
    margin = _param("control.pressure_margin") * model.params.escape_pressure_kpa
    spw = int(_param("control.slices_per_wash"))
    rows = []
    for i, (command, args) in enumerate(candidates_for(goal, believed, model)):
        try:
            sim = simulate(command, args, believed, model, spw)
        except (ValueError, KeyError) as e:
            # No model rule for this command, or it needs something the step did not give.
            # Say so as a rejected candidate: a plan that cannot be simulated must come back
            # as "not feasible, and here is why", never as a crash in the operator loop.
            rows.append({"id": i, "command": command, "flow_ul_min": args.get("flow_ul_min"),
                         "volume_ul": args.get("volume_ul"), "washes": args.get("washes"), "ramp_c_per_s": None,
                         "args": args, "safety": {"result": "blocked", "reasons": []}, "time_s": 0.0,
                         "reagent_ul": 0.0, "final_pct": None, "interior_pct": None, "peak_kpa": 0.0, "soak_s": 0.0,
                         "status": "rejected", "reason": f"cannot simulate {command}: {e}"})
            continue
        row = {"id": i, "command": command, "flow_ul_min": args.get("flow_ul_min"), "volume_ul": args.get("volume_ul"),
               "washes": args.get("washes"), "ramp_c_per_s": args.get("ramp_c_per_s"), "args": args}
        reason = None
        if goal["kind"] == "exchange" and goal.get("exchange_kind", "sweep") == "sweep":
            soak = _soak_needed(sim["final_fraction"], sim["interior_fraction"], goal["interior_fraction"],
                                model.params.diffusion_time_s)
            if math.isfinite(soak) and soak > 0:
                # Round the soak UP: a soak rounded down is a tenth of a second short of the
                # interior target, which the check below would (rightly) reject.
                args["soak_s"] = math.ceil(soak * 10) / 10
                sim = simulate(command, args, believed, model, spw)
            if sim["final_fraction"] < goal["fraction"]:
                reason = f"predicted exchange {100 * sim['final_fraction']:.1f}% < goal {100 * goal['fraction']:.0f}%"
            elif sim["interior_fraction"] is not None and sim["interior_fraction"] < goal["interior_fraction"] - 1e-6:
                # The interior goal has to be MET, not merely attempted. When the soak came
                # back infinite the soak was dropped and the plan was still reported
                # feasible, which quietly abandoned exchange.interior_target_fraction: the
                # capsule interior tends to the chamber's own concentration, so if that is
                # below the target no soak of any length can close the gap.
                reason = ((f"capsule interior cannot reach {100 * goal['interior_fraction']:.0f}%: the chamber liquid "
                           f"only reaches {100 * sim['final_fraction']:.1f}%, which is the most the interior can tend "
                           f"to, so no soak closes the gap (needs more volume)") if not math.isfinite(soak) else
                          (f"capsule interior {100 * sim['interior_fraction']:.1f}% < goal "
                           f"{100 * goal['interior_fraction']:.0f}% even after a {args.get('soak_s', 0):.0f} s soak"))
        elif goal["kind"] == "exchange":
            # fill or partial: the step's own volume IS the goal (see the module docstring
            # and contracts/schemas/protocol.schema.json exchange_kind), so what has to be
            # true is that the volume actually went in.
            want = float(goal.get("fill_volume_ul") or (float(goal.get("volume_ul") or 0.0)
                                                        * int(goal.get("washes") or 1)))
            if sim["reservoir_short"]:
                reason = f"reservoir cannot deliver the {want:.1f} uL this {goal['exchange_kind']} needs"
            elif want > 0 and sim["volume_ul"] < want - 1e-6:
                reason = f"delivers {sim['volume_ul']:.1f} uL of the {want:.1f} uL the step {goal['exchange_kind']}s"
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
        verdict = safety.check(command, args, believed, chip, model, run_params=run_params, lyse_ahead=lyse_ahead)
        row["safety"] = verdict.to_dict()
        if verdict.status == "blocked":
            reason = "safety: " + verdict.reasons[0]
        elif reason is None and goal["kind"] in ("exchange", "load") and abs(sim["peak_kpa"]) > margin:
            reason = f"peak {sim['peak_kpa']:.2f} kPa above the {margin:.1f} kPa planning margin"
        row.update(time_s=round(sim["time_s"], 1),
                   reagent_ul=round((args.get("volume_ul") or 0) * (args.get("washes") or 1), 1) if command != "hold_temp" else 0.0,
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
        "run_parameters": dict(run_params or {}),
    }


if __name__ == "__main__":
    import sys
    from model import ModelParams
    chip = schema_io.load_json(schema_io.ROOT / "cad" / "m3_wash_spec.json")
    params, _ = safety._contracts()
    model = ProcessModel(ModelParams.from_chip(chip, params))
    s = ChipState.from_chip_spec(chip, params, reagents=["capsule_suspension", "wash_buffer", "resuspension_buffer"])
    s.chambers["m3_trap_1"].capsules_on_mesh = 10000
    step = {"step": 2, "operation": "exchange", "target": "m3_trap_1", "reagent": "wash_buffer", "volume_ul": 20}
    plan = plan_step(goal_for_step(step, chip), s, model, chip)
    print("chosen:", plan["args"], plan["predicted"])
    for r in plan["candidates"]:
        print(f"  {r['flow_ul_min']:>4} uL/min x{r['washes']}  {r['time_s']:>6.1f}s  {r['final_pct']:>6.2f}%  {r['peak_kpa']:.2f} kPa  "
              f"{r['status']:8s} {r['reason'] or ''}")
    sys.exit(0)

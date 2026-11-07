"""Operators that drive the controller. All three use the same primitives and produce
the same run format (the LLM operator is in llm_operator.py).

  planner   deterministic: the optimizer for every step, plus a fixed recovery table
              pressure_high        -> back_flush, then re-plan the rest of the step
              reagent_low          -> pause_for_human
              capsule_count_drop   -> stop and report
              sensor_disagreement  -> distrust that sensor, fall back to the model, re-plan
  scripted  replays a saved transcript of operator tool calls (e.g. from an LLM run),
            so a demo works offline
"""

from __future__ import annotations

from datetime import datetime, timezone

MAX_RECOVERIES_PER_STEP = 4


def dispatch(ctl, tool: str, args: dict, explanation: str = "") -> dict:
    """Run one operator tool against the controller. Shared by the LLM and scripted operators."""
    if tool == "plan_step":
        plan = ctl.plan_step(int(args["step"]), explanation)
        return summarise_plan(plan)
    if tool == "execute_plan":
        return ctl.execute_plan(int(args["step"]), explanation)
    if tool == "back_flush":
        return ctl.back_flush(args["chamber"], explanation)
    if tool == "get_state":
        ctl.record_call("get_state", {}, explanation)
        return ctl.get_state()
    if tool == "get_sensors":
        ctl.record_call("get_sensors", {}, explanation)
        return {"trusted": sorted(ctl.trusted), "readings": ctl.get_sensors()}
    if tool == "get_alerts":
        ctl.record_call("get_alerts", {}, explanation)
        return {"active": ctl.get_alerts()}
    if tool == "pause_for_human":
        return ctl.pause_for_human(args.get("reason") or explanation)
    if tool == "distrust_sensor":
        return ctl.distrust_sensor(args["sensor"], explanation)
    raise ValueError(f"unknown tool {tool!r}")


def summarise_plan(plan: dict) -> dict:
    """What an operator needs from a plan (the full table stays in the run log)."""
    rejected = [r for r in plan["candidates"] if r["status"] == "rejected"]
    return {"step": plan["step"], "feasible": plan["feasible"], "command": plan["command"], "args": plan["args"],
            "predicted": plan["predicted"], "candidates": len(plan["candidates"]), "rejected": len(rejected),
            "rejection_examples": sorted({r["reason"] for r in rejected})[:4]}


def run_planner(ctl) -> None:
    steps = sorted(ctl.steps)
    for i, n in enumerate(steps):
        if ctl.status != "running":
            break
        ctl.current_step = n
        if ctl.monitor:
            short = ctl.monitor.check_supplies(ctl, [ctl.steps[k] for k in steps[i:]])
            if short:
                ctl.pause_for_human(f"{short[0]['message']}; refill before continuing")
                break
        step = ctl.steps[n]
        ctl.plan_step(n, f"Plan step {n} ({step['operation']}): pick the fastest safe settings that meet its goal.")
        outcome = ctl.execute_plan(n, step.get("note", ""))
        tries = 0
        while outcome["status"] != "done" and ctl.status == "running":
            tries += 1
            if tries > MAX_RECOVERIES_PER_STEP:
                ctl.pause_for_human(f"step {n} still failing after {MAX_RECOVERIES_PER_STEP} recoveries")
                break
            if outcome["status"] in ("blocked", "no_safe_plan"):
                ctl.pause_for_human(f"step {n}: {outcome.get('reasons', ['no safe plan meets the goal'])[0]}")
                break
            alert = (outcome.get("alerts") or [{}])[0]
            name = alert.get("name")
            if name == "pressure_high":
                ctl.back_flush(step["target"], "Pressure is near the escape limit, so back-flush to lift the capsules "
                                               "and clear the mesh before continuing.")
                ctl.plan_step(n, "Re-plan the rest of the step with the refitted mesh resistance.")
                outcome = ctl.execute_plan(n, step.get("note", "") + " (after recovery)")
            elif name == "sensor_disagreement":
                sensor = alert["evidence"].get("sensor", "pressure_kpa")
                ctl.distrust_sensor(sensor, f"The {sensor} sensor disagrees with the model; ignore it and fall back "
                                            f"to the model for that quantity.")
                ctl.plan_step(n, "Re-plan the rest of the step without the distrusted sensor.")
                outcome = ctl.execute_plan(n, step.get("note", "") + " (sensor distrusted)")
            elif name == "reagent_low":
                ctl.pause_for_human(f"{alert['message']}; refill the reservoir")
            elif name == "capsule_count_drop":
                ctl.stop(f"{alert['message']}; stopping to protect the remaining capsules")
            else:
                ctl.pause_for_human(f"step {n} interrupted: {alert.get('message', outcome['status'])}")
    if ctl.status == "running":
        ctl.status = "done"


def transcript_from(ctl, source: str, model: str | None = None) -> dict:
    return {"source": source, "model": model, "protocol_id": ctl.protocol["id"], "chip_id": ctl.chip["id"],
            "scenario": ctl.scenario.get("name"), "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "calls": list(ctl.transcript)}


def run_scripted(ctl, transcript: dict) -> None:
    for call in transcript["calls"]:
        if ctl.status != "running":
            break
        dispatch(ctl, call["tool"], call.get("input", {}), call.get("explanation", ""))
    if ctl.status == "running":
        ctl.status = "done"

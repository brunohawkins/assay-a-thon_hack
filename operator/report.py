"""Run records and the Markdown audit report for closed-loop runs.

    build_run(controller, header, scenario_path) -> run dict (contracts/schemas/run.schema.json)
    write_report(run, path)                       -> runs/<run>_report.md

The report lists steps, timings, alerts, recoveries, model updates, final capsule
recovery, reagent used, final exchange % and belief-vs-truth drift. Values marked TRUE
come from the virtual chip (SIMULATED HARDWARE); real hardware could never report them.
"""

from __future__ import annotations

from pathlib import Path


def build_run(ctl, header: dict, scenario: dict, scenario_path: str | None) -> dict:
    caps = ctl.adapter.capabilities()
    run = dict(header)
    run.update({
        "simulated_hardware": bool(caps.get("simulated")),
        "adapter": ctl.adapter.name,
        "loop": ctl.loop,
        "status": ctl.status,
        "scenario": {**{k: v for k, v in scenario.items() if k != "description"},
                     "file": scenario_path, "description": scenario.get("description", "")},
        "events": ctl.events,
        "log": ctl.log,
        "summary": ctl.summary(),
    })
    run["total_duration_s"] = round(ctl.believed.clock_s, 3)
    return run


def _fmt(v, unit=""):
    if v is None:
        return "–"
    if isinstance(v, float):
        v = round(v, 2)
    return f"{v}{unit}"


def render_report(run: dict) -> str:
    s = run.get("summary", {})
    scn = run.get("scenario", {})
    sim = run.get("simulated_hardware")
    sensors = [k for k, on in (scn.get("sensors") or {}).items() if on]
    faults = scn.get("faults") or []
    L = []
    L.append(f"# Run report: {run.get('protocol_name', run['protocol_id'])} on {run['chip_id']}")
    L.append("")
    if sim:
        L.append("> **SIMULATED HARDWARE.** This run used the virtual chip: no physical pump, valves or sensors. "
                 "Values marked TRUE come from the simulation and could never be measured directly on real hardware.")
        L.append("")
    L.append(f"- Scenario: `{scn.get('name', '–')}`: {scn.get('description', '')}")
    L.append(f"- Operator: **{run['operator']}** · Adapter: `{run.get('adapter')}` · Loop: **{run.get('loop')}**")
    L.append(f"- Sensors: {', '.join(sensors) if sensors else 'none (open loop)'}")
    L.append(f"- Faults injected: {', '.join(f['type'] + ' ' + str({k: v for k, v in f.items() if k != 'type'}) for f in faults) if faults else 'none'}")
    L.append(f"- Status: **{run.get('status')}**{(' (' + s.get('status_reason') + ')') if s.get('status_reason') else ''}"
             f" · Protocol time: {_fmt(run.get('total_duration_s'), ' s')}")
    L.append(f"- Created: {run.get('created_at')}")
    L.append("")

    b, t = s.get("believed") or {}, s.get("true") or {}
    L.append("## Outcome")
    L.append("")
    L.append("| | Believed (model + sensors) | TRUE (simulation) |")
    L.append("|---|---|---|")
    fx = s.get("final_exchange_pct") or {}
    L.append(f"| Final exchange % (step {fx.get('step', '–')}) | {_fmt(fx.get('believed'), '%')} | {_fmt(fx.get('true'), '%')} |")
    L.append(f"| Capsule recovery | {_fmt(b.get('capsule_recovery_pct'), '%')} | {_fmt(t.get('capsule_recovery_pct'), '%')} |")
    L.append(f"| Capsules collected | {_fmt(b.get('capsules_collected'))} | {_fmt(t.get('capsules_collected'))} |")
    L.append(f"| Capsules lost to waste | {_fmt(b.get('capsules_lost'))} | {_fmt(t.get('capsules_lost'))} |")
    L.append(f"| Capsules left on the mesh | {_fmt(b.get('capsules_on_mesh'))} | {_fmt(t.get('capsules_on_mesh'))} |")
    for reagent in sorted(set((b.get("reagent_used_ul") or {})) | set((t.get("reagent_used_ul") or {}))):
        L.append(f"| Reagent used: {reagent} | {_fmt((b.get('reagent_used_ul') or {}).get(reagent), ' uL')} | "
                 f"{_fmt((t.get('reagent_used_ul') or {}).get(reagent), ' uL')} |")
    L.append(f"| Waste | {_fmt(b.get('waste_ul'), ' uL')} | {_fmt(t.get('waste_ul'), ' uL')} |")
    L.append("")
    L.append(f"Alerts: **{s.get('alerts', 0)}** ({', '.join(s.get('alert_names', [])) or 'none'}) · "
             f"Recoveries: **{s.get('recoveries', 0)}** · Model updates: **{s.get('model_updates', 0)}**")
    L.append("")

    d = s.get("drift")
    if d:
        L.append("## Belief vs truth drift")
        L.append("")
        L.append("How far the operator's belief was from the simulated truth at the end of the run.")
        L.append("")
        for key, label in (("mesh_resistance_error_pct", "Mesh resistance (model vs true)"),
                           ("chamber_volume_ul_error_pct", "Chamber volume (model vs true)")):
            if key in d:
                L.append(f"- {label}: {d[key]['start']:+.1f}% at the start -> {d[key]['end']:+.1f}% at the end")
        for key, label, unit in (("final_exchange_pct_points", "Final exchange %", " points"),
                                 ("capsules_collected", "Capsules collected", " capsules"),
                                 ("capsules_lost", "Capsules lost", " capsules"),
                                 ("waste_ul", "Waste volume", " uL")):
            if key in d:
                L.append(f"- {label}: off by {d[key]}{unit}")
        L.append("")

    L.append("## Steps")
    L.append("")
    L.append("| Start | Step | Command as executed | Duration | Safety | Operator's explanation |")
    L.append("|---|---|---|---|---|---|")
    for e in run["events"]:
        a = e["args"]
        pa = e.get("plan_args", {})
        if e["command"] == "exchange":
            what = (f"{'back-flush' if a.get('direction') == 'backflush' else 'exchange'} {a['washes']} x {a['volume_ul']:g} uL "
                    f"{a['reagent']} at {a['flow_ul_min']:g} uL/min" + (f" + {pa['soak_s']:.0f} s soak" if pa.get("soak_s") else ""))
        elif e["command"] in ("load", "release"):
            what = f"{e['command']} {pa.get('volume_ul', a.get('volume_ul', 0)):g} uL at {pa.get('flow_ul_min', 0):g} uL/min"
        else:
            what = f"{e['command']} {a}"
        tag = (e.get("safety") or {}).get("result", "–") + (" · recovery" if e.get("recovery") else "")
        L.append(f"| {e['t_s']:.1f} s | {e.get('protocol_step', '–')} | {what} | {e['duration_s']:.1f} s | {tag} | "
                 f"{(e.get('explanation') or '').replace('|', '/')} |")
    L.append("")

    alerts = [x for x in run.get("log", []) if x["kind"] == "alert"]
    L.append("## Alerts")
    L.append("")
    if not alerts:
        L.append("None." + ("" if sensors else " (No sensors were on, so nothing could be detected.)"))
    for x in alerts:
        a = x["data"]["alert"]
        L.append(f"- **t = {a['t_s']:.1f} s, step {a.get('step')}: `{a['name']}`** ({a['severity']}): {a['message']}. "
                 f"Evidence: `{a['evidence']}`")
    L.append("")

    rec = [x for x in run.get("log", []) if x["kind"] in ("pause", "stop") or
           (x["kind"] == "plan" and x["message"].startswith("recovery")) or
           (x["kind"] == "model_update" and "distrusting" in x["message"])]
    L.append("## Recoveries and operator interventions")
    L.append("")
    if not rec:
        L.append("None.")
    for x in rec:
        L.append(f"- t = {x['t_s']:.1f} s: {x['message']}")
    L.append("")

    ups = [x for x in run.get("log", []) if x["kind"] == "model_update" and "param" in (x.get("data") or {})]
    L.append("## Model updates")
    L.append("")
    if not ups:
        L.append("None" + ("." if sensors else " (open loop: nothing to correct the model with)."))
    else:
        L.append("| t | Parameter | Old -> new | Why |")
        L.append("|---|---|---|---|")
        for x in ups:
            u = x["data"]
            L.append(f"| {u['t_s']:.1f} s | `{u['param']}` | {u['old']:.4g} -> {u['new']:.4g} {u['unit']} | {u['reason']} |")
    L.append("")

    L.append("## Optimizer decisions")
    L.append("")
    for x in run.get("log", []):
        if x["kind"] not in ("plan", "replan") or "plan" not in (x.get("data") or {}):
            continue
        plan = x["data"]["plan"]
        rejected = [r for r in plan["candidates"] if r["status"] == "rejected"]
        reasons = {}
        for r in rejected:
            key = r["reason"].split(":")[0] if r["reason"].startswith("safety") else r["reason"].split(" ")[0] + " …"
            reasons[key] = reasons.get(key, 0) + 1
        L.append(f"- t = {x['t_s']:.1f} s, step {plan['step']} ({x['kind']}): {x['message']}. "
                 f"{len(plan['candidates'])} candidates, {len(rejected)} rejected"
                 + (f" ({', '.join(f'{n} x {k}' for k, n in reasons.items())})" if reasons else "") + ".")
    L.append("")
    L.append("## Full operator log")
    L.append("")
    for x in run.get("log", []):
        L.append(f"- `{x['t_s']:7.1f} s` **{x['kind']}**: {x['message']}")
    L.append("")
    return "\n".join(L)


def write_report(run: dict, path: Path) -> None:
    path.write_text(render_report(run), encoding="utf-8")

"""Module 1b operator: runs the cell-prep protocol like any module, with one closed loop.

    python operator/run.py --scenario scenarios/m1b_cellprep_demo.json

At m1b_qc_window_1 a SIMULATED counter reports the cell concentration and viability
(the scenario sets what is really there; the counter adds seeded noise). The operator
then sets the dilution at m1b_resuspend_1 itself, so the cells give ~0.1 cells per capsule
at Module 2's flows (sizing.py), and logs why. Nothing counts the cells again after the
mixer, so what goes to Module 2 is an ASSUMPTION - counted / dilution - and everything
that reports or judges it says so. Before the cells leave the chip through its transfer
outlet (found from the chip spec, not from a hard-coded port id), the safety checker
(safety.check_cell_prep) approves or blocks them:
  - viability below 90% (10x)                  -> blocked: the operator stops and reports
  - still off target after the dilution        -> blocked with the fix, e.g.
    ("too dilute - concentrate 1.6x", or more dilution than the mixer can do)
Everything lands in the run file and its report.
"""

from __future__ import annotations

import random
from pathlib import Path

import commands
import planner
import safety
import schema_io
from adapters.sim import SimAdapter

ROOT = schema_io.ROOT

# How the concentration that leaves for Module 2 is arrived at. It is NOT measured: the
# counter reads before the mixer, and the dilution is what the operator ASKED the mixer for,
# so the number the safety checker judges is counted / dilution - an assumption. Said out
# loud here, in the verdict, in the summary and in the report, because an approval that
# rests on an assumed number has to say so.
ASSUMED_BASIS = ("ASSUMED, not measured: counted at the QC window (before the mixer) divided by the dilution the "
                 "operator set. There is no counter after the mixer, so the dilution is commanded, not verified")


class SimulatedCounter:
    """SIMULATED HARDWARE: the cell counter at the QC window."""

    def __init__(self, spec: dict, seed: int):
        self.rng = random.Random(seed)
        self.cells_per_ul = float(spec["cells_per_ul"])
        self.viability = float(spec["viability"])
        self.cv = float(spec.get("cv", 0.05))

    def read(self) -> dict:
        conc = self.cells_per_ul * (1 + self.rng.gauss(0, self.cv))
        via = min(1.0, max(0.0, self.viability + self.rng.gauss(0, 0.005)))
        return {"cells_per_ul": round(conc), "viability": round(via, 3)}


def leaves_the_chip(chip: dict, target: str) -> bool:
    """True when `target` is an outlet port that sends the cells on to the next module.

    Keyed off the chip's own structure - an outlet port that is not the waste - and not off
    the id "m1b_to_module2_1". Keyed on the literal id, renaming that port in the spec
    would have silently stopped the only safety gate between these cells and Module 2,
    while the run still reported itself as done.
    """
    port = next((p for p in chip.get("ports", []) if p["id"] == target), None)
    if port is None:
        return False
    return port.get("type") == "outlet" and port.get("role") != "waste"


def choose_dilution(count: dict, target: dict) -> tuple[float, str]:
    """Dilution the operator sets at the resuspension mixer, and why."""
    want = target["target_cells_per_ul"]
    raw = count["cells_per_ul"] / want
    if raw < 1.0:
        return 1.0, (f"counted {count['cells_per_ul']:,} cells/uL, below the ~{want:,.0f} cells/uL Module 2 needs for "
                     f"{target['target_mean']:g} cells per capsule; dilution can't fix that, so no extra dilution "
                     f"(minimum buffer only)")
    dilution = round(min(raw, target["max_dilution"]), 2)
    why = (f"counted {count['cells_per_ul']:,} cells/uL at {count['viability']:.0%} viability; Module 2 makes "
           f"{target['capsules_per_hour']:,.0f} capsules/h from {target['core_flow_ul_h']:g} uL/h of core stream, so "
           f"{target['target_mean']:g} cells per capsule needs ~{want:,.0f} cells/uL: diluting {dilution:g}x with dextran buffer")
    if raw > target["max_dilution"]:
        why += f" (the most the mixer can do; {raw:.1f}x was needed)"
    return dilution, why


def run(protocol: dict, chip: dict, chip_path: Path, scenario: dict, scenario_path: Path) -> dict:
    m2_chip = schema_io.load_json(ROOT / scenario["module2_chip"])
    target = safety.cell_prep_target(m2_chip)
    counter = SimulatedCounter(scenario["counter"], scenario.get("seed", 0))
    sample_ul = float(scenario.get("sample_volume_ul", 50))
    sim = SimAdapter(chip)
    count: dict | None = None
    dilution = 1.0
    status, verdict = "done", None
    for step in protocol["steps"]:
        for command, args in planner.step_to_commands(step, chip):
            if command == "mix" and step.get("dilution") == "auto":
                if count is None:
                    raise planner.PlanError(f"step {step['step']}: dilution 'auto' needs a count first")
                dilution, why = choose_dilution(count, target)
                args = {**args, "dilution": dilution, "volume_ul": round(sample_ul * (dilution - 1), 1) or 0.1}
                sim.note("decision", f"m1b_resuspend_1 dilution set to {dilution:g}x: {why}", step["step"])
            if command == "transfer" and leaves_the_chip(chip, args["target"]):
                assumed_final = count["cells_per_ul"] / dilution
                verdict = safety.check_cell_prep(assumed_final, count["viability"], m2_chip)
                verdict.reasons.append(f"this verdict rests on {assumed_final:,.0f} cells/uL, which is {ASSUMED_BASIS}")
                sim.note("safety" if verdict.ok else "blocked",
                         f"cells to Module 2 through {args['target']}: {verdict.status}: "
                         f"{'; '.join(verdict.reasons)}", step["step"])
                if not verdict.ok:
                    status = "stopped"
                    sim.note("stop", "stopped before Module 2: the cells stay in m1b_resuspend_1 for a person to fix "
                                     f"({verdict.reasons[0]})", step["step"])
                    break
            sim.send(command, commands.validate(command, args, chip)["args"], narration=step["note"],
                     protocol_step=step["step"], duration_s=step["duration_s"])
            if command == "count":
                count = counter.read()
                sim.events[-1]["measured"] = count
                sim.note("measurement", f"simulated counter at {args['target']}: {count['cells_per_ul']:,} cells/uL, "
                                        f"{count['viability']:.1%} viable", step["step"])
        if status != "done":
            break

    header = planner.run_header(protocol, chip, chip_path, "planner")
    run_ = sim.to_run(header)
    assumed_final = count["cells_per_ul"] / dilution if count else None
    run_.update({
        "simulated_hardware": True, "adapter": "simulated_counter", "loop": "closed", "status": status,
        "scenario": {**{k: v for k, v in scenario.items() if k != "description"}, "file": planner.repo_relative(scenario_path),
                     "description": scenario.get("description", "")},
        "summary": {
            "status": status, "counted": count, "counted_where": "m1b_qc_window_1, by a SIMULATED counter",
            "dilution": dilution, "dilution_basis": "set by the operator; commanded, not verified",
            "assumed_final_cells_per_ul": round(assumed_final) if assumed_final else None,
            "assumed_mean_cells_per_capsule": (round(safety.mean_cells_per_capsule(assumed_final, target), 4)
                                               if assumed_final else None),
            "assumption_basis": ASSUMED_BASIS,
            # Same assumed numbers under their original names, which operator/run.py's
            # console line reads. The assumed_* keys above are the ones to quote.
            "final_cells_per_ul": round(assumed_final) if assumed_final else None,
            "mean_cells_per_capsule": (round(safety.mean_cells_per_capsule(assumed_final, target), 4)
                                       if assumed_final else None),
            "target_mean": target["target_mean"], "target_cells_per_ul": round(target["target_cells_per_ul"]),
            "verdict": verdict.to_dict() if verdict else None,
            "verdict_rests_on": "an assumed concentration (see assumption_basis)",
        },
    })
    return run_


def render_report(run_: dict) -> str:
    s = run_["summary"]
    c = s.get("counted") or {}
    L = [f"# Run report: {run_['protocol_name']} on {run_['chip_id']}", "",
         "> **SIMULATED HARDWARE.** The cell counter at the QC window is simulated; no chip was printed for Module 1b.", "",
         f"- Scenario: `{run_['scenario']['name']}`: {run_['scenario'].get('description', '')}",
         f"- Status: **{run_['status']}**", f"- Created: {run_['created_at']}", "",
         "## Outcome", "",
         "| | |", "|---|---|",
         f"| MEASURED at the QC window (simulated counter) | {c.get('cells_per_ul', '–'):,} cells/uL, "
         f"{c.get('viability', 0):.1%} viable |" if c else "| Counted | – |",
         f"| Dilution set by the operator (commanded, not verified) | {s['dilution']:g}x |",
         f"| ASSUMED sent to Module 2 | {s['assumed_final_cells_per_ul']:,} cells/uL -> "
         f"{s['assumed_mean_cells_per_capsule']:.3f} cells per capsule "
         f"(target {s['target_mean']:g}, i.e. ~{s['target_cells_per_ul']:,} cells/uL) |"
         if s.get("assumed_final_cells_per_ul") else "| Sent | – |",
         f"| How that number was arrived at | {s.get('assumption_basis', '–')} |",
         f"| Safety checker (on the ASSUMED number) | **{(s.get('verdict') or {}).get('result', '–')}**: "
         f"{'; '.join((s.get('verdict') or {}).get('reasons', []))} |",
         "", "## Steps", "", "| Start | Step | Command | Duration | What happened |", "|---|---|---|---|---|"]
    for e in run_["events"]:
        a = e["args"]
        what = ", ".join(f"{k}={v}" for k, v in a.items())
        if e.get("measured"):
            what += (f" -> MEASURED (simulated counter) {e['measured']['cells_per_ul']:,} cells/uL, "
                     f"{e['measured']['viability']:.1%} viable")
        L.append(f"| {e['t_s']:.0f} s | {e['protocol_step']} | {e['command']} | {e['duration_s']:.0f} s | {what} |")
    L += ["", "## Operator log (measurements, decisions, safety)", ""]
    L += [f"- `{x['t_s']:7.0f} s` **{x['kind']}** (step {x.get('step')}): {x['message']}" for x in run_.get("log", [])]
    return "\n".join(L) + "\n"

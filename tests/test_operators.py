"""The operators, in two parts.

SHIPPED (class ShippedModule3): the controller, optimizer and safety checker against the
chip and protocol we actually ship as Module 3 - chip/module3_v1_translated.json and
protocols/module3_v1.json. Nothing exercised them before: every operator test ran on
cad/m3_wash_spec.json, which that file's own module block marks `superseded`.

REFERENCE ONLY (classes below it): the operator MECHANICS - fault detection, recovery,
transcript replay and the LLM tool loop - still run on the superseded wash MVP, because
those scenarios (clog growth per uL, capsule counts, sensor sets) were written for that
chip and they test operator CODE, not a shipped artefact. Each such class says so, and
test_the_mvp_chip_is_still_marked_superseded keeps the label honest.

No test here rewrites a protocol's volumes. The shipped volumes are asserted as they are.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))

import llm_operator  # noqa: E402
import optimizer  # noqa: E402
import safety  # noqa: E402
import schema_io  # noqa: E402
from adapters.virtual_chip import VirtualChip  # noqa: E402
from controller import Controller  # noqa: E402
from faults import Monitor, build_faults  # noqa: E402
from model import ModelParams, ProcessModel  # noqa: E402
from operators import run_planner, run_scripted, transcript_from  # noqa: E402
from state import ChipState  # noqa: E402

PARAMS, _ = safety._contracts()
CLOSED = {"pressure_kpa": True, "outlet_absorbance": True}

SHIPPED_CHIP = schema_io.load_json(ROOT / "chip" / "module3_v1_translated.json")
SHIPPED_PROTO = schema_io.load_json(ROOT / "protocols" / "module3_v1.json")

MVP_CHIP = schema_io.load_json(ROOT / "cad" / "m3_wash_spec.json")
MVP_PROTO = schema_io.load_json(ROOT / "protocols" / "m3_wash.json")


def reagents_of(proto):
    return sorted({s["reagent"] for s in proto["steps"] if s.get("reagent")} | {"capsule_suspension"})


def controller(chip, proto, scenario, operator="planner"):
    reagents = reagents_of(proto)
    virtual = VirtualChip(chip, PARAMS, scenario, reagents=reagents, faults=build_faults(scenario))
    return Controller(proto, chip, virtual, scenario, operator=operator, monitor=Monitor(PARAMS))


class ShippedModule3(unittest.TestCase):
    """The operator stack on the chip and protocol in protocols/pipeline_mvp.json."""

    def scenario(self, **extra):
        # Reservoir volumes are a bench decision, so they are sized from the protocol's own
        # demand; the protocol's volumes themselves are untouched. (That the demand exceeds
        # contracts' reservoir.default_ul is asserted in tests/test_design_rules.py.)
        need = {}
        for s in SHIPPED_PROTO["steps"]:
            r = s.get("reagent") or ("capsule_suspension" if s["operation"] == "load" else None)
            if r and s.get("volume_ul"):
                need[r] = need.get(r, 0.0) + float(s["volume_ul"]) * int(s.get("washes") or 1)
        return {"seed": 4, "sensors": CLOSED, "reservoirs_ul": {r: 2 * v for r, v in need.items()}, **extra}

    def test_the_pipeline_still_points_at_this_chip_and_protocol(self):
        (_, pipe), = schema_io.pipelines()
        entry = next(m for m in pipe["modules"] if m["module"] == "module3")
        self.assertEqual(schema_io.pipeline_chip(entry)[0], "chip/module3_v1_translated.json")
        self.assertEqual(entry["protocol"], "protocols/module3_v1.json")

    def test_the_planner_runs_the_shipped_protocol_to_the_end(self):
        c = controller(SHIPPED_CHIP, SHIPPED_PROTO, self.scenario())
        run_planner(c)
        self.assertEqual(c.status, "done", msg=f"{c.status}: {c.status_reason}")
        self.assertEqual(c.summary()["true"]["capsules_lost"], 0)

    def test_every_step_of_the_shipped_protocol_has_a_feasible_plan(self):
        """The optimizer must be able to meet each step's goal with the volumes the protocol
        ships. A step whose volume_ul is one chamber volume cannot reach
        exchange.target_fraction at the wash counts the optimizer will consider."""
        model = ProcessModel(ModelParams.from_chip(SHIPPED_CHIP, PARAMS))
        trap = next(c["id"] for c in SHIPPED_CHIP["chambers"] if c.get("retention"))
        reagents = reagents_of(SHIPPED_PROTO)
        bed = model.params.monolayer_capacity[trap]

        def state_for(step):
            """A full capsule bed, as the protocol's load leaves it - but a release runs after
            the back-flush has lifted the bed, so it is planned from the lifted state."""
            s = ChipState.from_chip_spec(SHIPPED_CHIP, PARAMS, reagents=reagents,
                                         reservoirs_ul={r: 1e6 for r in reagents})
            lifted = step["operation"] == "release"
            s.chambers[trap].capsules_on_mesh = 0 if lifted else bed
            s.chambers[trap].capsules_suspended = bed if lifted else 0
            return s

        for step in SHIPPED_PROTO["steps"]:
            if step["operation"] not in ("exchange", "release", "load"):
                continue
            with self.subTest(step=step["step"], operation=step["operation"], reagent=step.get("reagent")):
                plan = optimizer.plan_step(optimizer.goal_for_step(step, SHIPPED_CHIP), state_for(step),
                                           model, SHIPPED_CHIP)
                reasons = sorted({r["reason"] for r in plan["candidates"] if r["reason"]})
                self.assertTrue(plan["feasible"], f"no candidate meets the goal; best of "
                                                  f"{len(plan['candidates'])}: {reasons[-1] if reasons else '-'}")


class PlannerMechanics(unittest.TestCase):
    """REFERENCE ONLY: fault detection and recovery, on the superseded wash MVP.

    The scenarios below (clog growth per uL, reagent_low at 30 uL, 5% capsule loss per wash)
    were tuned to that chip's 14.7 uL trap. They test operator/controller.py and
    operator/faults.py, not an artefact we ship. The shipped chip is covered by
    ShippedModule3 above.
    """

    def controller(self, scenario, operator="planner"):
        return controller(MVP_CHIP, MVP_PROTO, scenario, operator)

    def test_the_mvp_chip_is_still_marked_superseded(self):
        self.assertEqual(MVP_CHIP["module"]["status"], "superseded")
        self.assertEqual(MVP_CHIP["module"]["superseded_by"], "chip/module3_v1_translated.json")
        chips = {m.get("chip") for m in schema_io.pipelines()[0][1]["modules"]}
        self.assertNotIn("cad/m3_wash_spec.json", chips, "a superseded chip must stay out of the pipeline")

    def test_clog_is_detected_and_recovered_without_losing_capsules(self):
        c = self.controller({"seed": 4, "sensors": CLOSED, "faults": [{"type": "clog", "start_wash": 2}]})
        run_planner(c)
        s = c.summary()
        self.assertEqual(s["status"], "done")
        self.assertIn("pressure_high", s["alert_names"])
        self.assertEqual(s["recoveries"], 1)
        self.assertEqual(s["true"]["capsules_lost"], 0)

    def test_open_loop_clog_loses_capsules_unnoticed(self):
        c = self.controller({"seed": 4, "sensors": {}, "faults": [{"type": "clog", "start_wash": 2}]})
        run_planner(c)
        s = c.summary()
        self.assertEqual(s["alert_names"], [])
        self.assertGreater(s["true"]["capsules_lost"], 500)
        self.assertEqual(s["believed"]["capsules_lost"], 0)

    def test_closed_loop_belief_converges(self):
        c = self.controller({"seed": 4, "sensors": CLOSED})
        run_planner(c)
        d = c.summary()["drift"]
        self.assertLess(abs(d["mesh_resistance_error_pct"]["end"]), abs(d["mesh_resistance_error_pct"]["start"]))

    def test_reagent_low_pauses_and_capsule_loss_stops(self):
        c = self.controller({"seed": 4, "sensors": {"pump_volume_ul": True},
                             "faults": [{"type": "reagent_low", "reagent": "wash_buffer", "start_ul": 30}]})
        run_planner(c)
        self.assertEqual(c.status, "paused_for_human")
        c = self.controller({"seed": 4, "sensors": {"capsules_counted": True},
                             "faults": [{"type": "capsule_loss", "fraction_per_wash": 0.05}]})
        run_planner(c)
        self.assertEqual(c.status, "stopped")


class Scripted(unittest.TestCase):
    """REFERENCE ONLY: transcript replay, on the superseded wash MVP (see PlannerMechanics)."""

    def test_replaying_a_transcript_reproduces_the_run(self):
        scn = {"seed": 4, "sensors": CLOSED, "faults": [{"type": "clog", "start_wash": 2}]}
        a = controller(MVP_CHIP, MVP_PROTO, scn)
        run_planner(a)
        b = controller(MVP_CHIP, MVP_PROTO, scn, operator="scripted")
        run_scripted(b, transcript_from(a, "planner"))
        self.assertEqual(json.dumps(a.summary(), sort_keys=True), json.dumps(b.summary(), sort_keys=True))


def fake_api(script):
    """Returns tool calls from `script` one turn at a time; checks the request shape."""
    turns = iter(script)
    seen = []

    def call(body, key):
        assert body["model"] == llm_operator.MODEL and body["tool_choice"] == {"type": "auto"}
        assert {t["name"] for t in body["tools"]} >= {"plan_step", "execute_plan", "back_flush", "get_state",
                                                       "get_sensors", "get_alerts", "pause_for_human"}
        assert all(t["strict"] for t in body["tools"])
        if len(body["messages"]) > 1:
            seen.append(body["messages"][-1]["content"])
        step = next(turns, None)
        if step is None:
            return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Done."}]}
        return {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": f"t{len(seen)}_{i}", "name": name, "input": {**inp, "reason": f"because {name}"}}
            for i, (name, inp) in enumerate(step)]}
    return call, seen


class LLMLoop(unittest.TestCase):
    """REFERENCE ONLY: the LLM tool loop, on the superseded wash MVP (see PlannerMechanics).

    The script below addresses the MVP protocol's four steps by number, including releasing
    at step 4 before the back-flush at step 3 to prove the safety checker blocks it.
    """

    def test_tools_safety_block_and_completion(self):
        c = controller(MVP_CHIP, MVP_PROTO, {"seed": 4, "sensors": CLOSED}, operator="llm")
        release = next(s["step"] for s in MVP_PROTO["steps"] if s["operation"] == "release")
        backflush = next(s["step"] for s in MVP_PROTO["steps"] if s.get("direction") == "backflush")
        self.assertLess(backflush, release, "the MVP protocol back-flushes before it releases")
        script = [[("get_state", {})], [("plan_step", {"step": 1})], [("execute_plan", {"step": 1})],
                  [("plan_step", {"step": 2}), ("execute_plan", {"step": 2})],
                  [("plan_step", {"step": release}), ("execute_plan", {"step": release})],   # too early -> blocked
                  [("plan_step", {"step": backflush}), ("execute_plan", {"step": backflush})],
                  [("plan_step", {"step": release}), ("execute_plan", {"step": release})]]
        call, seen = fake_api(script)
        llm_operator.run_llm(c, api_key="fake", call_api=call)
        blocked = [r for turn in seen for r in turn if "back-flush first" in r.get("content", "")]
        self.assertTrue(blocked, "the safety checker's reason should reach the model")
        self.assertEqual(c.status, "done")
        self.assertTrue(all(e["explanation"] for e in c.events))
        self.assertTrue(any(call["explanation"] == "because plan_step" for call in c.transcript))

    def test_tool_call_cap_stops_the_run(self):
        c = controller(MVP_CHIP, MVP_PROTO, {"seed": 4, "sensors": CLOSED}, operator="llm")
        call, _ = fake_api([[("get_state", {})]] * 10)
        llm_operator.run_llm(c, api_key="fake", call_api=call, max_tool_calls=3)
        self.assertEqual(c.status, "stopped")
        self.assertIn("cap", c.status_reason)


if __name__ == "__main__":
    unittest.main()

"""Operators on the simulated wash MVP: planner recovery, scripted replay, and the LLM
tool loop against a fake API (no key or network needed)."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))

import llm_operator  # noqa: E402
import safety  # noqa: E402
import schema_io  # noqa: E402
from adapters.virtual_chip import VirtualChip  # noqa: E402
from controller import Controller  # noqa: E402
from faults import Monitor, build_faults  # noqa: E402
from operators import run_planner, run_scripted, transcript_from  # noqa: E402

CHIP = schema_io.load_json(ROOT / "cad" / "wash_mvp_spec.json")
PROTO = schema_io.load_json(ROOT / "protocols" / "placeholder_capsule_wash_mvp.json")
PARAMS, _ = safety._contracts()
CLOSED = {"pressure_kpa": True, "outlet_absorbance": True}


def controller(scenario, operator="planner"):
    reagents = sorted({s["reagent"] for s in PROTO["steps"] if s.get("reagent")} | {"capsule_suspension"})
    chip = VirtualChip(CHIP, PARAMS, scenario, reagents=reagents, faults=build_faults(scenario))
    return Controller(PROTO, CHIP, chip, scenario, operator=operator, monitor=Monitor(PARAMS))


class Planner(unittest.TestCase):
    def test_clog_is_detected_and_recovered_without_losing_capsules(self):
        c = controller({"seed": 4, "sensors": CLOSED, "faults": [{"type": "clog", "start_wash": 2}]})
        run_planner(c)
        s = c.summary()
        self.assertEqual(s["status"], "done")
        self.assertIn("pressure_high", s["alert_names"])
        self.assertEqual(s["recoveries"], 1)
        self.assertEqual(s["true"]["capsules_lost"], 0)

    def test_open_loop_clog_loses_capsules_unnoticed(self):
        c = controller({"seed": 4, "sensors": {}, "faults": [{"type": "clog", "start_wash": 2}]})
        run_planner(c)
        s = c.summary()
        self.assertEqual(s["alert_names"], [])
        self.assertGreater(s["true"]["capsules_lost"], 500)
        self.assertEqual(s["believed"]["capsules_lost"], 0)

    def test_closed_loop_belief_converges(self):
        c = controller({"seed": 4, "sensors": CLOSED})
        run_planner(c)
        d = c.summary()["drift"]
        self.assertLess(abs(d["mesh_resistance_error_pct"]["end"]), abs(d["mesh_resistance_error_pct"]["start"]))

    def test_reagent_low_pauses_and_capsule_loss_stops(self):
        c = controller({"seed": 4, "sensors": {"pump_volume_ul": True},
                        "faults": [{"type": "reagent_low", "reagent": "wash_buffer", "start_ul": 30}]})
        run_planner(c)
        self.assertEqual(c.status, "paused_for_human")
        c = controller({"seed": 4, "sensors": {"capsules_counted": True},
                        "faults": [{"type": "capsule_loss", "fraction_per_wash": 0.05}]})
        run_planner(c)
        self.assertEqual(c.status, "stopped")


class Scripted(unittest.TestCase):
    def test_replaying_a_transcript_reproduces_the_run(self):
        scn = {"seed": 4, "sensors": CLOSED, "faults": [{"type": "clog", "start_wash": 2}]}
        a = controller(scn)
        run_planner(a)
        b = controller(scn, operator="scripted")
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
    def test_tools_safety_block_and_completion(self):
        c = controller({"seed": 4, "sensors": CLOSED}, operator="llm")
        script = [[("get_state", {})], [("plan_step", {"step": 1})], [("execute_plan", {"step": 1})],
                  [("plan_step", {"step": 2}), ("execute_plan", {"step": 2})],
                  [("plan_step", {"step": 4}), ("execute_plan", {"step": 4})],      # release too early -> blocked
                  [("plan_step", {"step": 3}), ("execute_plan", {"step": 3})],
                  [("plan_step", {"step": 4}), ("execute_plan", {"step": 4})]]
        call, seen = fake_api(script)
        llm_operator.run_llm(c, api_key="fake", call_api=call)
        blocked = [r for turn in seen for r in turn if "back-flush first" in r.get("content", "")]
        self.assertTrue(blocked, "the safety checker's reason should reach the model")
        self.assertEqual(c.status, "done")
        self.assertTrue(all(e["explanation"] for e in c.events))
        self.assertTrue(any(call["explanation"] == "because plan_step" for call in c.transcript))

    def test_tool_call_cap_stops_the_run(self):
        c = controller({"seed": 4, "sensors": CLOSED}, operator="llm")
        call, _ = fake_api([[("get_state", {})]] * 10)
        llm_operator.run_llm(c, api_key="fake", call_api=call, max_tool_calls=3)
        self.assertEqual(c.status, "stopped")
        self.assertIn("cap", c.status_reason)


if __name__ == "__main__":
    unittest.main()

"""Each rule in operator/safety.py, on the chip and protocol we SHIP as Module 3
(chip/module3_v1_translated.json + protocols/module3_v1.json), not on the superseded wash
MVP: cad/m3_wash_spec.json is marked superseded and is out of the pipeline, and the valve
rules live in tests/test_module1.py.

Every threshold the rules fire on is DERIVED here from the chip, the contracts and the
process model (escape pressure / the bed's resistance, the chamber's own volume, the layer
material's working temperature), because the shipped trap is 38.4 uL instead of the MVP's
7.35 uL and a hard-coded flow or temperature stops exercising the rule it was written for.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))

import safety  # noqa: E402
import schema_io  # noqa: E402
from model import ModelParams, ProcessModel  # noqa: E402
from state import ChipState  # noqa: E402

CHIP = schema_io.load_json(ROOT / "chip" / "module3_v1_translated.json")
PROTOCOL = schema_io.load_json(ROOT / "protocols" / "module3_v1.json")
PARAMS = schema_io.load_json(ROOT / "contracts" / "parameters.json")
MATERIALS = schema_io.load_json(ROOT / "contracts" / "materials.json")["materials"]

TRAP = next(c for c in CHIP["chambers"] if c.get("retention"))["id"]
PORT = next(p["id"] for p in CHIP["ports"] if p.get("role") == "waste")
MODEL = ProcessModel(ModelParams.from_chip(CHIP, PARAMS))
FULL_BED = MODEL.params.monolayer_capacity[TRAP]          # capsules that cover the retention once
ESCAPE_KPA = MODEL.params.escape_pressure_kpa
MARGIN = PARAMS["parameters"]["control.pressure_margin"]["value"]
# The resistance a full bed presents, so the two flows below are the rule's own thresholds
# rather than numbers that happened to straddle them on the old 7.35 uL trap.
BED_KPA_PER_UL_MIN = MODEL.params.mesh_clean_resistance[TRAP] + MODEL.params.mesh_resistance[TRAP]
ESCAPE_FLOW = ESCAPE_KPA / BED_KPA_PER_UL_MIN             # pushes capsules through
WARN_FLOW = (1 + MARGIN) / 2 * ESCAPE_FLOW                # halfway into the warning band

# The step the protocol actually ships, so the "normal" case is the shipped one.
WASH_STEP = next(s for s in PROTOCOL["steps"] if s["operation"] == "exchange" and s.get("direction") != "backflush")
WASH = {"target": TRAP, "reagent": WASH_STEP["reagent"], "volume_ul": WASH_STEP["volume_ul"],
        "washes": 1, "flow_ul_min": WASH_STEP["flow_ul_min"]}
WASH_VOLUME = WASH["volume_ul"] * WASH["washes"]
REAGENTS = sorted({s["reagent"] for s in PROTOCOL["steps"] if s.get("reagent")} | {"capsule_suspension"})


def _material_temps():
    """Working temperature of each of the chip's layer materials, read from contracts/materials.json."""
    names = {str(la.get("material", "")).lower() for la in CHIP["layers"]}
    return [m["max_working_temp_c"] for mid, m in MATERIALS.items()
            if names & ({mid} | {a.lower() for a in m.get("aliases", [])})]


MAX_TEMP_C = min(_material_temps())


def state(on_mesh=FULL_BED, suspended=0, **reservoirs):
    s = ChipState.from_chip_spec(CHIP, PARAMS, reagents=REAGENTS, reservoirs_ul=reservoirs)
    s.chambers[TRAP].capsules_on_mesh = on_mesh
    s.chambers[TRAP].capsules_suspended = suspended
    return s


class Rules(unittest.TestCase):
    def test_the_shipped_wash_step_is_approved(self):
        self.assertEqual(safety.check("exchange", WASH, state(), CHIP).status, "approved")

    def test_unknown_target_is_blocked(self):
        r = safety.check("exchange", {**WASH, "target": "trap_9"}, state(), CHIP)
        self.assertEqual(r.status, "blocked")
        self.assertIn("trap_9 not in chip", r.reasons[0])

    def test_port_as_target_is_blocked(self):
        r = safety.check("exchange", {**WASH, "target": PORT}, state(), CHIP)
        self.assertEqual(r.status, "blocked")
        self.assertIn("is a port", r.reasons[0])

    def test_pressure_that_pushes_capsules_through_is_blocked(self):
        flow = round(ESCAPE_FLOW * 1.2, 3)
        r = safety.check("exchange", {**WASH, "flow_ul_min": flow}, state(), CHIP)
        self.assertEqual(r.status, "blocked", f"{flow} uL/min on a full bed is {flow * BED_KPA_PER_UL_MIN:.2f} kPa")
        self.assertIn("pushed through", r.reasons[0])

    def test_pressure_near_the_limit_warns(self):
        r = safety.check("exchange", {**WASH, "flow_ul_min": round(WARN_FLOW, 3)}, state(), CHIP)
        self.assertEqual(r.status, "warning")
        self.assertIn("escape pressure", r.reasons[0])

    def test_the_shipped_flow_is_well_under_the_escape_flow(self):
        # The rule above only means something if the shipped protocol is not already at it.
        self.assertLess(WASH["flow_ul_min"], MARGIN * ESCAPE_FLOW)

    def test_backflush_is_judged_on_the_magnitude_of_its_pressure(self):
        # A back-flush pushes UP through the retention, so the pressure is negative, but its
        # magnitude lifts capsules out of the chamber just as readily. safety.py used to skip
        # the rule for a back-flush; it now judges the magnitude either way, so the same
        # over-escape flow that is blocked forwards is blocked backwards too.
        over = safety.check("exchange", {**WASH, "flow_ul_min": round(ESCAPE_FLOW * 1.2, 3),
                                         "direction": "backflush"}, state(), CHIP)
        self.assertEqual(over.status, "blocked")
        self.assertIn("lifting off the retention", over.reasons[0])

    def test_the_shipped_backflush_step_is_within_the_escape_pressure(self):
        step = next((s for s in PROTOCOL["steps"] if s.get("direction") == "backflush"), None)
        self.assertIsNotNone(step, "the shipped Module 3 protocol back-flushes the bed")
        r = safety.check("exchange", {"target": step["target"], "reagent": step["reagent"],
                                      "volume_ul": step["volume_ul"], "washes": step["washes"],
                                      "flow_ul_min": step["flow_ul_min"], "direction": "backflush"},
                         state(), CHIP)
        self.assertNotEqual(r.status, "blocked", msg=f"step {step['step']}: {r.reasons}")

    def test_waste_overflow_is_blocked(self):
        s = state()
        s.waste_ul = s.waste_capacity_ul - WASH_VOLUME / 2
        r = safety.check("exchange", WASH, s, CHIP)
        self.assertEqual(r.status, "blocked")
        self.assertIn("waste would overflow", r.reasons[0])

    def test_collection_overflow_is_blocked(self):
        s = state(on_mesh=0, suspended=FULL_BED)
        release = {"target": TRAP, "reagent": "resuspension_buffer", "volume_ul": 30}
        s.collection_ul = s.collection_capacity_ul - release["volume_ul"] / 2
        self.assertIn("collection would overflow", safety.check("release", release, s, CHIP).reasons[0])

    def test_dry_reservoir_is_blocked_and_low_one_warns(self):
        dry = safety.check("exchange", WASH, state(**{WASH["reagent"]: WASH_VOLUME * 0.5}), CHIP)
        self.assertIn("run dry", dry.reasons[0])
        low = safety.check("exchange", WASH, state(**{WASH["reagent"]: WASH_VOLUME * 1.05}), CHIP)
        self.assertEqual(low.status, "warning")
        self.assertIn("nearly empty", low.reasons[0])

    def test_temperature_above_material_limit_is_blocked(self):
        hot = {"target": TRAP, "temp_c": MAX_TEMP_C + 10, "duration_s": 60}
        r = safety.check("hold_temp", hot, state(), CHIP)
        self.assertEqual(r.status, "blocked")
        self.assertIn("max working temperature", r.reasons[0])
        ok = safety.check("hold_temp", {**hot, "temp_c": MAX_TEMP_C - 10}, state(), CHIP)
        self.assertEqual(ok.status, "approved")

    def test_every_hold_in_the_shipped_protocol_is_approved(self):
        holds = [s for s in PROTOCOL["steps"] if s["operation"] == "hold_temp"]
        self.assertTrue(holds, "the shipped Module 3 protocol holds temperature (lysis, RT)")
        for s in holds:
            r = safety.check("hold_temp", {"target": s["target"], "temp_c": s["temp_c"],
                                           "duration_s": s["duration_s"]}, state(), CHIP)
            self.assertEqual(r.status, "approved", msg=f"step {s['step']}: {r.reasons}")

    def test_release_with_capsules_on_mesh_needs_backflush(self):
        release = {"target": TRAP, "reagent": "resuspension_buffer", "volume_ul": 30}
        r = safety.check("release", release, state(), CHIP)
        self.assertEqual(r.status, "blocked")
        self.assertIn("back-flush first", r.reasons[0])
        ok = safety.check("release", release, state(on_mesh=0, suspended=FULL_BED), CHIP)
        self.assertEqual(ok.status, "approved")


class ShippedChip(unittest.TestCase):
    def test_the_superseded_wash_mvp_is_not_what_we_ship(self):
        # Why this file moved: the MVP spec says so itself.
        mvp = schema_io.load_json(ROOT / "cad" / "m3_wash_spec.json")
        self.assertEqual(mvp["module"]["status"], "superseded")
        self.assertEqual(mvp["module"]["superseded_by"], "chip/module3_v1_translated.json")

    def test_the_chip_under_test_is_the_pipelines_module_3(self):
        _, pipe = schema_io.chosen_pipeline()
        entry = next(m for m in pipe["modules"] if m["module"] == "module3")
        self.assertEqual(schema_io.pipeline_chip(entry)[0], "chip/module3_v1_translated.json")
        self.assertEqual(entry["protocol"], "protocols/module3_v1.json")


if __name__ == "__main__":
    unittest.main()

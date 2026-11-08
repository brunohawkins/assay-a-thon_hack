"""Checks assay2chip/cfd/scenario.py: protocol step -> CFD boundary conditions.

    python -m unittest tests.test_cfd_scenario -v

These run OFFLINE: everything reads the repo's own protocols, chips and contracts. No
solver, no network, no LLM. The real protocols are the fixtures on purpose - the mapping
rules only matter if they survive Module 1's 182 port-less steps, Module 6's -20 C quench
and Module 5's steps that claim to flow into a sealed chip.

The point of most of these tests is the PROVENANCE, not the arithmetic: a boundary
condition read out of a step's English, a viscosity the contracts do not cover and a split
feed are all legitimate, and all three are wrong if they travel downstream unlabelled.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.cfd import model  # noqa: E402
from assay2chip.cfd import scenario as S  # noqa: E402
from assay2chip.lib import contracts  # noqa: E402

PAIRS = {
    "module1_v1": "module1_v1",
    "cell_prep_v1": "cell_prep_v1",
    "module3_v1": "module3_v1_translated",
    "module4_v1": "module4_v1",
    "module5_v1": "module5_v1",
    "module6_v1": "module6_v1",
    "spc_demo_v1": "spc_demo_v1",
    "sctip_spc_v1": "sctip_spc_v1",
    "ipsc_to_mgl_v1": "ipsc_to_mgl_chip_v1",
}

_cache: dict[str, object] = {}


def run_for(protocol_id: str, worst_cases: bool = True) -> S.ProtocolScenarios:
    """Built once per protocol: the 182-step protocols are slow to rebuild per test."""
    key = f"{protocol_id}:{worst_cases}"
    if key not in _cache:
        _cache[key] = S.build_protocol(
            f"protocols/{protocol_id}.json",
            f"chip/{PAIRS[protocol_id]}.json",
            worst_cases=worst_cases,
        )
    return _cache[key]  # type: ignore[return-value]


def sweep() -> dict:
    if "sweep" not in _cache:
        _cache["sweep"] = S.build_all()
    return _cache["sweep"]  # type: ignore[return-value]


def pipeline(path: str = "protocols/pipeline_mvp.json") -> S.PipelineScenarios:
    if path not in _cache:
        _cache[path] = S.build_pipeline(path)
    return _cache[path]  # type: ignore[return-value]


def protocol_json(protocol_id: str) -> dict:
    return json.loads((ROOT / "protocols" / f"{protocol_id}.json").read_text())


def chip_json(chip_id: str) -> dict:
    return json.loads((ROOT / "chip" / f"{chip_id}.json").read_text())


def build_from_objects(protocol: dict, chip: dict) -> S.ProtocolScenarios:
    """Build a hand-made protocol/chip pair, for the cases the repo has no example of."""
    with tempfile.TemporaryDirectory() as directory:
        protocol_path = Path(directory) / "p.json"
        chip_path = Path(directory) / "c.json"
        protocol_path.write_text(json.dumps(protocol))
        chip_path.write_text(json.dumps(chip))
        return S.build_protocol(protocol_path, chip_path)


class Base(unittest.TestCase):
    def step(self, run: S.ProtocolScenarios, number: int) -> S.ScenarioRecord:
        found = [s for s in run.step_scenarios if s.step == number]
        self.assertTrue(found, f"no scenario for step {number} of {run.protocol_id}")
        return found[0]


# --- the three port rules ----------------------------------------------------

class PortRules(Base):
    def test_feed_becomes_a_flow_bc_at_the_steps_flow_rate(self):
        step = self.step(run_for("module3_v1"), 2)
        bc = step.ports["reagent_in"]
        self.assertEqual(bc.type, "flow")
        stated = protocol_json("module3_v1")["steps"][1]["flow_ul_min"]
        self.assertAlmostEqual(bc.q_ul_min, stated)

    def test_vent_becomes_zero_pressure(self):
        step = self.step(run_for("module3_v1"), 2)
        self.assertEqual(step.ports["lysis_waste"].type, "pressure")
        self.assertEqual(step.ports["lysis_waste"].p_pa, 0.0)

    def test_cap_becomes_closed(self):
        step = self.step(run_for("module3_v1"), 2)
        for port in ("capsule_in", "backflush_in", "to_split_tree"):
            with self.subTest(port=port):
                self.assertEqual(step.ports[port].type, "closed")

    def test_every_chip_port_gets_a_boundary_condition(self):
        for protocol_id, chip_id in PAIRS.items():
            ports = {p["id"] for p in chip_json(chip_id)["ports"]}
            for scenario in run_for(protocol_id).scenarios:
                with self.subTest(scenario=scenario.id):
                    self.assertEqual(set(scenario.ports), ports)

    def test_a_port_in_none_of_the_three_lists_is_closed_and_flagged(self):
        """Only a capped port is a true block - but an unstated port is not driven either."""
        chip = {
            "id": "t_chip",
            "ports": [{"id": "in_a", "type": "inlet"}, {"id": "out_a", "type": "outlet"},
                      {"id": "spare", "type": "outlet"}],
            "chambers": [{"id": "cx"}],
            "channels": [{"from": "in_a", "to": "cx"}, {"from": "cx", "to": "out_a"},
                         {"from": "cx", "to": "spare"}],
        }
        step = {"step": 1, "operation": "exchange", "target": "cx", "flow_ul_min": 1.0,
                "note": "wash", "ports": {"feed": ["in_a"], "vent": ["out_a"]}}
        plan = S.stated_ports(step, S.ChipGraph(chip))
        self.assertEqual(plan.cap, ["spare"])
        self.assertIn("port_not_stated", plan.flags)

    def test_step_waste_is_taken_as_the_drain(self):
        chip = {"id": "t", "ports": [{"id": "in_a", "type": "inlet"},
                                     {"id": "w", "type": "outlet"}],
                "chambers": [{"id": "cx"}],
                "channels": [{"from": "in_a", "to": "cx"}, {"from": "cx", "to": "w"}]}
        step = {"step": 1, "operation": "exchange", "target": "cx", "waste": "w",
                "flow_ul_min": 1.0, "note": "x", "ports": {"feed": ["in_a"]}}
        plan = S.stated_ports(step, S.ChipGraph(chip))
        self.assertEqual(plan.vent, ["w"])
        self.assertIn("vent_added_from_step_waste", plan.flags)


# --- several feeds, one flow -------------------------------------------------

class SplitFeeds(Base):
    def test_encapsulate_splits_by_named_stream_and_is_exact(self):
        step = self.step(run_for("spc_demo_v1"), 2)
        flows = protocol_json("spc_demo_v1")["steps"][1]["flows_ul_h"]
        self.assertFalse(step.ambiguous)
        self.assertAlmostEqual(step.ports["in_core"].q_ul_min, flows["core"] / 60)
        self.assertAlmostEqual(step.ports["in_shell"].q_ul_min, flows["shell"] / 60)
        self.assertAlmostEqual(step.ports["in_oil"].q_ul_min, flows["oil"] / 60)

    def test_encapsulate_maps_streams_by_synonym_when_ports_are_named_otherwise(self):
        """sctip feeds the core through in_load and the shell through in_reagent."""
        step = self.step(run_for("sctip_spc_v1"), 1)
        flows = protocol_json("sctip_spc_v1")["steps"][0]["flows_ul_h"]
        self.assertAlmostEqual(step.ports["in_load"].q_ul_min, flows["core"] / 60)
        self.assertAlmostEqual(step.ports["in_reagent"].q_ul_min, flows["shell"] / 60)
        self.assertAlmostEqual(step.ports["in_oil"].q_ul_min, flows["oil"] / 60)
        self.assertTrue(any("mapped_by_synonym" in f for f in step.flags))

    def test_several_feeds_one_rate_is_ambiguous_with_both_readings_recorded(self):
        """Module 6 step 4 cultures three lanes through three ports at one stated rate."""
        step = self.step(run_for("module6_v1"), 4)
        self.assertTrue(step.ambiguous)
        self.assertEqual({a["reading"] for a in step.alternatives},
                         {"per_feed_full", "equal_split"})
        self.assertEqual(len([p for p in step.ports.values() if p.type == "flow"]), 3)

    def test_parallel_lanes_at_a_known_one_lane_rate_read_as_per_feed(self):
        """3.78 uL/min is also the one-lane load rate, so it is per lane, not a total."""
        step = self.step(run_for("module6_v1"), 4)
        stated = protocol_json("module6_v1")["steps"][3]["flow_ul_min"]
        self.assertIn("multi_feed_per_feed_full", step.flags)
        for port in ("in_n", "in_g", "in_a"):
            with self.subTest(port=port):
                self.assertAlmostEqual(step.ports[port].q_ul_min, stated)

    def test_equal_split_is_the_fallback_and_says_so(self):
        step = {"step": 1, "operation": "exchange", "target": "cx", "flow_ul_min": 10.0,
                "note": "push both reagents in"}
        plan = S.resolve_flow(step, ["in_a", "in_b"], [], set())
        self.assertTrue(plan.ambiguous)
        self.assertEqual(plan.per_port, {"in_a": 5.0, "in_b": 5.0})
        self.assertIn("multi_feed_equal_split", plan.flags)
        self.assertIn("EQUALLY", plan.source)

    def test_an_ambiguous_split_is_never_silent(self):
        for protocol_id in PAIRS:
            for scenario in run_for(protocol_id).scenarios:
                if scenario.ambiguous:
                    with self.subTest(scenario=scenario.id):
                        self.assertTrue(scenario.alternatives)
                        self.assertTrue(any("multi_feed" in f for f in scenario.flags))


# --- no-flow steps are kept --------------------------------------------------

class NoFlowSteps(Base):
    def test_a_hold_with_everything_capped_is_kept(self):
        step = self.step(run_for("module3_v1"), 3)
        self.assertEqual(step.kind, "no_flow")
        self.assertTrue(all(p.type == "closed" for p in step.ports.values()))
        self.assertEqual(step.duration_s, 600)

    def test_module1_keeps_all_73_idle_holds(self):
        holds = [s for s in run_for("module1_v1").step_scenarios
                 if s.operation == "hold_temp"]
        self.assertEqual(len(holds), 73)
        self.assertTrue(all(s.kind == "no_flow" for s in holds))
        self.assertTrue(all(s.duration_s and s.duration_s > 0 for s in holds))

    def test_a_no_flow_step_leaves_its_vents_at_atmosphere(self):
        """Evaporation and bubble growth need the vented port still open."""
        step = self.step(run_for("module6_v1"), 7)
        self.assertEqual(step.kind, "no_flow")
        self.assertEqual(step.ports["out_n"].type, "pressure")
        self.assertEqual(step.ports["in_n"].type, "closed")

    def test_no_flow_scenarios_really_have_no_flow(self):
        for protocol_id in PAIRS:
            for scenario in run_for(protocol_id).scenarios:
                if scenario.kind == "no_flow":
                    with self.subTest(scenario=scenario.id):
                        self.assertEqual(scenario.q_in_ul_min, 0)
                        self.assertFalse(
                            any(bc.type == "flow" for bc in scenario.ports.values())
                        )

    def test_every_step_of_every_protocol_produces_a_scenario(self):
        for protocol_id in PAIRS:
            emitted = {s.step for s in run_for(protocol_id).step_scenarios}
            dropped = {s["step"] for s in protocol_json(protocol_id)["steps"]} - emitted
            with self.subTest(protocol=protocol_id):
                self.assertEqual(dropped, set())

    def test_the_culture_duration_survives_into_the_scenario(self):
        """The long dwells are the ones bubble and evaporation analysis needs."""
        step = self.step(run_for("module6_v1"), 4)
        self.assertEqual(step.duration_s, protocol_json("module6_v1")["steps"][3]["duration_s"])


# --- inference ---------------------------------------------------------------

class Inference(Base):
    def test_module1_has_no_ports_blocks_so_every_step_is_inferred(self):
        self.assertTrue(all(s.get("ports") is None
                            for s in protocol_json("module1_v1")["steps"]))
        for scenario in run_for("module1_v1").step_scenarios:
            with self.subTest(step=scenario.step):
                self.assertTrue(scenario.inferred)
                self.assertIn("ports_inferred", scenario.flags)
                self.assertTrue(scenario.inference)

    def test_inference_reads_the_note_for_feed_and_vent(self):
        """'half change ... through p_in, p_mid vented'."""
        step = self.step(run_for("module1_v1"), 6)
        self.assertEqual(step.ports["p_in"].type, "flow")
        self.assertEqual(step.ports["p_mid"].type, "pressure")
        self.assertEqual(step.ports["p_g_in"].type, "closed")
        self.assertTrue(any("p_in" in line and "feed" in line for line in step.inference))

    def test_inference_handles_the_interaction_window(self):
        """Step 153: feed p_in, p_mid CAPPED, both far outlets vented."""
        step = self.step(run_for("module1_v1"), 153)
        self.assertEqual(step.ports["p_in"].type, "flow")
        self.assertEqual(step.ports["p_mid"].type, "closed")
        self.assertEqual(step.ports["p_g_out"].type, "pressure")
        self.assertEqual(step.ports["p_a_out"].type, "pressure")

    def test_inference_drives_an_outlet_for_a_backflush(self):
        """'Back-flush g_cx from p_g_out': the driven port is an outlet."""
        step = self.step(run_for("module1_v1"), 179)
        self.assertEqual(step.ports["p_g_out"].type, "flow")
        self.assertTrue(any("back-flush" in line.lower() for line in step.inference))
        self.assertEqual(step.ports["p_g_in"].type, "closed")

    def test_inference_caps_everything_for_an_idle_hold(self):
        step = self.step(run_for("module1_v1"), 2)
        self.assertEqual(step.kind, "no_flow")
        self.assertTrue(all(p.type == "closed" for p in step.ports.values()))

    def test_pooling_drives_every_lane(self):
        step = self.step(run_for("module1_v1"), 181)
        flowing = sorted(p for p, bc in step.ports.items() if bc.type == "flow")
        self.assertEqual(flowing, ["p_a_in", "p_g_in", "p_in"])
        self.assertIn("pool_feeds_all_lanes", step.flags)

    def test_graph_inference_picks_the_nearest_outlet_as_the_vent(self):
        """No port named anywhere: fall back to the chip's channels."""
        chip = {"id": "t",
                "ports": [{"id": "in_a", "type": "inlet"}, {"id": "near", "type": "outlet"},
                          {"id": "far", "type": "outlet"}],
                "chambers": [{"id": "cx"}, {"id": "cy"}],
                "channels": [{"from": "in_a", "to": "cx"}, {"from": "cx", "to": "near"},
                             {"from": "cx", "to": "cy"}, {"from": "cy", "to": "far"}]}
        step = {"step": 1, "operation": "exchange", "target": "cx", "flow_ul_min": 1.0,
                "note": "wash the chamber"}
        plan = S.infer_ports(step, S.ChipGraph(chip), [])
        self.assertEqual(plan.feed, ["in_a"])
        self.assertEqual(plan.vent, ["near"])
        self.assertIn("far", plan.cap)

    def test_the_ipsc_chip_drains_an_exchange_to_its_waste_port(self):
        """waste_1 is 3 hops from m1_expand; outlet_1 is 6, so the wash goes to waste."""
        step = self.step(run_for("ipsc_to_mgl_v1"), 1)
        self.assertEqual(step.ports["inlet_1"].type, "flow")
        self.assertEqual(step.ports["waste_1"].type, "pressure")

    def test_a_stated_ports_block_is_never_marked_inferred(self):
        for protocol_id in ("module3_v1", "module4_v1", "module6_v1", "cell_prep_v1"):
            with self.subTest(protocol=protocol_id):
                self.assertFalse(any(s.inferred for s in run_for(protocol_id).step_scenarios))

    def test_several_candidate_inlets_are_flagged_not_hidden(self):
        step = self.step(run_for("module1_v1"), 175)
        self.assertIn("feed_inferred_from_several_candidates", step.flags)
        self.assertTrue(any("other inlets" in line for line in step.inference))


# --- viscosity ---------------------------------------------------------------

class Viscosity(Base):
    def test_37c_step_uses_the_37c_contract_value(self):
        step = self.step(run_for("module6_v1"), 4)
        self.assertEqual(step.temp_c, 37)
        self.assertEqual(step.fluid.contract_key, contracts.VISCOSITY_37C_KEY)
        self.assertAlmostEqual(step.fluid.viscosity_pa_s,
                               contracts.viscosity_37c_pa_s().pa_s)
        self.assertFalse(step.fluid.assumption)

    def test_a_cold_rinse_is_a_flagged_assumption_not_water(self):
        step = self.step(run_for("module6_v1"), 5)
        self.assertEqual(step.temp_c, 4)
        self.assertTrue(step.fluid.assumption)
        self.assertIsNone(step.fluid.contract_key)
        self.assertGreater(step.fluid.viscosity_pa_s,
                           contracts.viscosity_water_20c_pa_s().pa_s)
        self.assertGreater(step.fluid.viscosity_max_pa_s, step.fluid.viscosity_pa_s)
        self.assertIn("viscosity_assumed:cold_aqueous", step.flags)

    def test_a_cold_organic_quench_is_a_flagged_assumption(self):
        step = self.step(run_for("module6_v1"), 6)
        self.assertEqual(step.temp_c, -20)
        self.assertEqual(step.fluid.fluid_class, "cold_organic_solvent")
        self.assertTrue(step.fluid.assumption)
        self.assertIn("ASSUMPTION", step.fluid.source)

    def test_oil_and_the_viscous_core_buffer_are_assumptions(self):
        step = self.step(run_for("spc_demo_v1"), 2)
        self.assertEqual(step.port_fluids["in_oil"]["class"], "oil")
        self.assertEqual(step.port_fluids["in_core"]["class"], "viscous_polymer")
        self.assertTrue(step.port_fluids["in_core"]["assumption"])
        self.assertTrue(step.port_fluids["in_core"]["non_newtonian"])
        self.assertIn("multi_fluid_step", step.flags)

    def test_the_dominant_stream_is_the_scenarios_fluid(self):
        step = self.step(run_for("spc_demo_v1"), 2)
        self.assertEqual(step.fluid.fluid_class, "oil")  # the continuous phase

    def test_no_assumed_fluid_is_silently_water(self):
        water = contracts.viscosity_water_20c_pa_s().pa_s
        unresolved: list[dict] = []
        for fluid_class in S.ASSUMED_FLUIDS:
            with self.subTest(fluid=fluid_class):
                spec = S._assumed_fluid(fluid_class, "x", 4.0, unresolved)
                self.assertTrue(spec.assumption)
                self.assertNotAlmostEqual(spec.viscosity_pa_s, water)
                self.assertTrue(spec.why)
        self.assertEqual(len(unresolved), len(S.ASSUMED_FLUIDS))
        self.assertTrue(all(e["key"].startswith("fluid.") for e in unresolved))

    def test_room_temperature_on_a_real_assay_flags_the_dye_demo_scope(self):
        """The 22 C lysis dwell is not the dye demo, so the 20 C key is out of scope."""
        rt = [s for s in run_for("module3_v1").step_scenarios if s.temp_c == 22]
        self.assertTrue(rt)
        for scenario in rt:
            with self.subTest(step=scenario.step):
                self.assertTrue(
                    scenario.fluid.assumption
                    or any("out_of_scope" in f for f in scenario.flags)
                )

    def test_a_dwell_keeps_the_fluid_the_last_step_pushed_in(self):
        step = self.step(run_for("module3_v1"), 3)
        self.assertEqual(step.fluid.name, "lysis_buffer")
        self.assertEqual(step.fluid.fluid_class, "denaturant")
        self.assertTrue(any(f.startswith("fluid_carried_from_step") for f in step.flags))

    def test_pcr_temperatures_are_flagged_as_beyond_the_contract(self):
        run = run_for("module4_v1")
        hot = [s for s in run.step_scenarios if (s.temp_c or 0) > S.HOT_CONTRACT_MAX_C]
        self.assertTrue(hot)
        for scenario in hot:
            with self.subTest(step=scenario.step):
                self.assertTrue(any("out_of_scope" in f for f in scenario.flags))
        self.assertTrue(any(u["key"] == "fluid.viscosity_hot_pa_s" for u in run.unresolved))

    def test_temperature_is_read_from_the_note_when_temp_c_is_null(self):
        """'Lift the cells in n_cx ... at 4 C' - step 175 has temp_c null."""
        self.assertIsNone(protocol_json("module1_v1")["steps"][174]["temp_c"])
        step = self.step(run_for("module1_v1"), 175)
        self.assertEqual(step.temp_c, 4)
        self.assertIn("temp_read_from_note", step.flags)

    def test_temperature_is_inherited_and_the_inheritance_is_recorded(self):
        step = self.step(run_for("module1_v1"), 6)
        self.assertEqual(step.temp_c, 37)
        self.assertIn("inherited", step.temp_source)
        self.assertTrue(any(f.startswith("temp_inherited_from_step_") for f in step.flags))

    def test_a_zone_without_a_setpoint_is_recorded_as_a_range(self):
        chip = {"id": "t", "ports": [{"id": "in_a", "type": "inlet"}],
                "chambers": [{"id": "cx"}], "channels": [],
                "thermal_zones": [{"id": "z", "chambers": ["cx"], "min_temp_c": 4,
                                   "max_temp_c": 50}]}
        step = {"step": 1, "operation": "hold_temp", "target": "cx", "temp_c": None,
                "note": "held by its thermal zone under the chamber"}
        _, _, _, flags = S.resolve_temperature(step, chip, None, None)
        self.assertTrue(any(f.startswith("temp_zone_range_only") for f in flags))

    def test_an_unknown_temperature_is_recorded_as_unresolved(self):
        run = run_for("module4_v1")
        self.assertTrue(any(u["kind"] == "unknown_temperature" for u in run.unresolved))

    def test_every_non_assumed_viscosity_is_exactly_a_contract_value(self):
        """No hard-coded viscosity may reach a scenario."""
        allowed = {contracts.viscosity_37c_pa_s().pa_s,
                   contracts.viscosity_water_20c_pa_s().pa_s}
        for protocol_id in PAIRS:
            for scenario in run_for(protocol_id).scenarios:
                if not scenario.fluid.assumption:
                    with self.subTest(scenario=scenario.id):
                        self.assertIn(scenario.fluid.viscosity_pa_s, allowed)

    def test_every_assumed_viscosity_is_a_multiple_of_the_contract_water_value(self):
        water = contracts.viscosity_water_20c_pa_s().pa_s
        for protocol_id in PAIRS:
            for scenario in run_for(protocol_id).scenarios:
                fluid = scenario.fluid
                if fluid.assumption and fluid.contract_key is None:
                    with self.subTest(scenario=scenario.id):
                        self.assertAlmostEqual(
                            round(fluid.viscosity_pa_s / water, 6),
                            float(fluid.source.split()[1].rstrip("x")),
                            places=5,
                        )

    def test_cold_water_factor_is_monotonic_and_clamped(self):
        self.assertAlmostEqual(S._cold_water_factor(20.0), 1.0)
        self.assertGreater(S._cold_water_factor(4.0), S._cold_water_factor(15.0))
        self.assertGreater(S._cold_water_factor(15.0), 1.0)
        self.assertEqual(S._cold_water_factor(-40.0), S._cold_water_factor(0.0))

    def test_port_names_with_underscores_still_classify(self):
        self.assertEqual(S.classify_fluid(None, "encapsulate", "in_oil"), "oil")
        self.assertEqual(S.classify_fluid(None, "encapsulate", "in_core"), "viscous_polymer")
        self.assertEqual(S.classify_fluid("wash_buffer", "exchange", "in_buffer"), "aqueous")


# --- flow rates --------------------------------------------------------------

class FlowRates(Base):
    def test_a_rate_derived_from_volume_and_duration_is_flagged(self):
        step = self.step(run_for("module1_v1"), 3)
        stated = protocol_json("module1_v1")["steps"][2]
        self.assertAlmostEqual(step.q_in_ul_min,
                               stated["volume_ul"] / (stated["duration_s"] / 60))
        self.assertIn("flow_derived_from_volume_and_duration", step.flags)

    def test_an_inherited_rate_is_per_port_not_a_resplit_total(self):
        """Step 3 films the droplet train: each stream keeps its own rate."""
        step = self.step(run_for("spc_demo_v1"), 3)
        flows = protocol_json("spc_demo_v1")["steps"][1]["flows_ul_h"]
        self.assertFalse(step.ambiguous)
        self.assertAlmostEqual(step.ports["in_oil"].q_ul_min, flows["oil"] / 60)
        self.assertAlmostEqual(step.ports["in_core"].q_ul_min, flows["core"] / 60)
        self.assertIn("flow_inherited_per_port", step.flags)

    def test_zero_duration_is_a_valve_action_not_a_flow(self):
        step = self.step(run_for("module6_v1"), 9)
        self.assertEqual(step.kind, "no_flow")
        self.assertIn("no_flow_zero_duration", step.flags)

    def test_a_flowing_op_with_no_derivable_rate_is_reported_not_invented(self):
        step = {"step": 7, "operation": "transfer", "target": "cx", "flow_ul_min": None,
                "volume_ul": None, "duration_s": 100, "note": "move the cells across"}
        plan = S.resolve_flow(step, ["in_a"], [], set())
        self.assertTrue(plan.no_flow)
        self.assertIsNotNone(plan.unmapped)
        self.assertEqual(plan.unmapped[0], "no_flow_rate")

    def test_a_reconstructed_rate_is_reported_as_unresolved(self):
        run = run_for("cell_prep_v1")
        self.assertTrue(any(u["kind"] == "flow_rate_not_stated" for u in run.unresolved))

    def test_flow_is_positive_and_closed_ports_carry_none(self):
        for protocol_id in PAIRS:
            for scenario in run_for(protocol_id).scenarios:
                for bc in scenario.ports.values():
                    with self.subTest(scenario=scenario.id, port=bc.port):
                        if bc.type == "flow":
                            self.assertGreater(bc.q_ul_min, 0)
                        else:
                            self.assertIsNone(bc.q_ul_min)

    def test_washes_become_a_repeat_count_and_a_total_volume(self):
        step = self.step(run_for("module3_v1"), 4)
        stated = protocol_json("module3_v1")["steps"][3]
        self.assertEqual(step.repeats, stated["washes"])
        self.assertAlmostEqual(step.volume_total_ul,
                               stated["volume_ul"] * stated["washes"])

    def test_an_exchange_with_no_exchange_kind_defaults_to_sweep(self):
        step = self.step(run_for("module3_v1"), 4)
        self.assertEqual(step.exchange_kind, "sweep")
        self.assertIn("exchange_kind_absent_treated_as_sweep", step.flags)


# --- unsolvable steps are reported ------------------------------------------

class Unmappable(Base):
    def test_a_driven_feed_with_no_vent_is_reported(self):
        """An incompressible network with no pressure reference cannot be solved."""
        chip = {"id": "t", "ports": [{"id": "in_a", "type": "inlet"},
                                     {"id": "out", "type": "outlet"}],
                "chambers": [{"id": "cx"}],
                "channels": [{"from": "in_a", "to": "cx"}, {"from": "cx", "to": "out"}]}
        protocol = {"id": "t_proto", "name": "t", "source": "", "placeholder": True,
                    "steps": [{"step": 1, "operation": "load", "target": "cx",
                               "reagent": "buffer", "volume_ul": 1.0, "duration_s": 60,
                               "temp_c": 37, "washes": None, "flow_ul_min": 1.0,
                               "note": "fill it", "ports": {"feed": ["in_a"], "vent": [],
                                                            "cap": ["out"]}}]}
        run = build_from_objects(protocol, chip)
        self.assertEqual([u.reason for u in run.unmapped], ["no_pressure_reference"])
        self.assertEqual(run.step_scenarios[0].kind, "no_flow")
        self.assertFalse(run.step_scenarios[0].mappable)

    def test_module5_contradiction_between_note_and_ports_is_reported(self):
        """'collagenase into react_cx through in_prot, all ports capped' cannot be both."""
        run = run_for("module5_v1")
        contradicted = [u.step for u in run.unmapped
                        if u.reason == "note_contradicts_ports_block"]
        self.assertEqual(contradicted, [3, 11, 19, 27])
        for number in contradicted:
            with self.subTest(step=number):
                self.assertEqual(self.step(run, number).kind, "no_flow")

    def test_a_port_that_is_not_on_the_chip_is_reported(self):
        chip = {"id": "t", "ports": [{"id": "in_a", "type": "inlet"},
                                     {"id": "out", "type": "outlet"}],
                "chambers": [{"id": "cx"}], "channels": []}
        protocol = {"id": "t_proto", "name": "t", "source": "", "placeholder": True,
                    "steps": [{"step": 1, "operation": "exchange", "target": "cx",
                               "reagent": "buffer", "volume_ul": 1.0, "duration_s": 60,
                               "temp_c": 37, "washes": 1, "flow_ul_min": 1.0, "note": "x",
                               "ports": {"feed": ["in_ghost"], "vent": ["out"],
                                         "cap": []}}]}
        run = build_from_objects(protocol, chip)
        self.assertEqual([u.reason for u in run.unmapped], ["port_not_on_chip"])
        self.assertEqual(run.step_scenarios, [])

    def test_a_target_that_is_not_on_the_chip_is_reported(self):
        chip = {"id": "t", "ports": [{"id": "in_a", "type": "inlet"},
                                     {"id": "out", "type": "outlet"}],
                "chambers": [{"id": "cx"}],
                "channels": [{"from": "in_a", "to": "cx"}, {"from": "cx", "to": "out"}]}
        protocol = {"id": "t_proto", "name": "t", "source": "", "placeholder": True,
                    "steps": [{"step": 1, "operation": "exchange", "target": "nowhere",
                               "reagent": "buffer", "volume_ul": 1.0, "duration_s": 60,
                               "temp_c": 37, "washes": 1, "flow_ul_min": 1.0, "note": "x",
                               "ports": {"feed": ["in_a"], "vent": ["out"], "cap": []}}]}
        run = build_from_objects(protocol, chip)
        self.assertIn("target_not_on_chip", {u.reason for u in run.unmapped})

    def test_a_chamber_with_no_inlet_path_is_reported(self):
        chip = {"id": "t", "ports": [{"id": "in_a", "type": "inlet"},
                                     {"id": "out", "type": "outlet"}],
                "chambers": [{"id": "cx"}, {"id": "island"}],
                "channels": [{"from": "in_a", "to": "cx"}, {"from": "cx", "to": "out"}]}
        protocol = {"id": "t_proto", "name": "t", "source": "", "placeholder": True,
                    "steps": [{"step": 1, "operation": "exchange", "target": "island",
                               "reagent": "buffer", "volume_ul": 1.0, "duration_s": 60,
                               "temp_c": 37, "washes": 1, "flow_ul_min": 1.0,
                               "note": "wash the island"}]}
        run = build_from_objects(protocol, chip)
        self.assertIn("no_feed_path", {u.reason for u in run.unmapped})

    def test_every_unmapped_step_carries_a_reason_and_a_citation(self):
        for protocol_id in PAIRS:
            for unmapped in run_for(protocol_id).unmapped:
                with self.subTest(protocol=protocol_id, step=unmapped.step):
                    self.assertTrue(unmapped.reason)
                    self.assertTrue(unmapped.detail)
                    self.assertTrue(unmapped.cites.startswith("protocols/"))


# --- worst cases -------------------------------------------------------------

class WorstCases(Base):
    def test_each_chamber_gets_its_maximum_protocol_flow(self):
        run = run_for("module6_v1")
        worst = {s.targets[0]: s for s in run.worst_cases
                 if s.kind == "worst_case_chamber"}
        self.assertTrue({"cx_n", "cx_g", "cx_a"} <= set(worst))
        for chamber, scenario in worst.items():
            with self.subTest(chamber=chamber):
                seen = [s.q_in_ul_min for s in run.step_scenarios if chamber in s.targets]
                self.assertAlmostEqual(scenario.q_in_ul_min, max(seen))

    def test_each_feed_gets_a_scenario_driven_alone(self):
        run = run_for("module6_v1")
        alone = [s for s in run.worst_cases if s.kind == "worst_case_feed"]
        self.assertEqual({s.id.rsplit(":", 1)[-1] for s in alone},
                         {"in_n", "in_g", "in_a"})
        for scenario in alone:
            port = scenario.id.rsplit(":", 1)[-1]
            with self.subTest(port=port):
                flowing = [p for p, bc in scenario.ports.items() if bc.type == "flow"]
                self.assertEqual(flowing, [port])
                self.assertTrue(any(bc.type == "pressure" for bc in scenario.ports.values()),
                                "a worst case still needs a drain")

    def test_a_feed_driven_alone_runs_at_its_highest_rate(self):
        run = run_for("module6_v1")
        alone = {s.id.rsplit(":", 1)[-1]: s for s in run.worst_cases
                 if s.kind == "worst_case_feed"}
        highest = max(s.ports["in_n"].q_ul_min or 0 for s in run.step_scenarios)
        self.assertAlmostEqual(alone["in_n"].ports["in_n"].q_ul_min, highest)

    def test_worst_cases_cite_the_step_they_came_from(self):
        for scenario in run_for("module6_v1").worst_cases:
            with self.subTest(scenario=scenario.id):
                self.assertIn("worst case", scenario.cites)
                self.assertIn(f"step {scenario.step}", scenario.cites)
                self.assertIn("worst_case", scenario.flags)

    def test_every_chip_with_flow_gets_both_kinds_of_worst_case(self):
        for protocol_id in PAIRS:
            run = run_for(protocol_id)
            if not any(s.q_in_ul_min > 0 for s in run.step_scenarios):
                continue
            kinds = {s.kind for s in run.worst_cases}
            with self.subTest(protocol=protocol_id):
                self.assertEqual(kinds, {"worst_case_chamber", "worst_case_feed"})

    def test_worst_cases_can_be_turned_off(self):
        self.assertEqual(run_for("module3_v1", worst_cases=False).worst_cases, [])
        self.assertTrue(run_for("module3_v1").worst_cases)


# --- citation and the shape handed to the solver ----------------------------

class Citation(Base):
    def test_every_scenario_cites_its_protocol_step(self):
        for protocol_id in PAIRS:
            run = run_for(protocol_id)
            for scenario in run.scenarios:
                with self.subTest(scenario=scenario.id):
                    self.assertIn(run.protocol_path, scenario.cites)
                    self.assertEqual(scenario.protocol_id, run.protocol_id)
                    self.assertEqual(scenario.chip_id, run.chip_id)
                    self.assertIsNotNone(scenario.step)

    def test_the_scenario_handed_to_the_solver_is_a_model_scenario(self):
        record = self.step(run_for("module3_v1"), 2)
        built = record.scenario
        self.assertIsInstance(built, model.Scenario)
        self.assertIn("protocols/module3_v1.json step 2", str(built.step))
        self.assertEqual(built.chip_id, "module3_v1_translated")
        self.assertTrue(built.ports["reagent_in"].is_flow)
        self.assertTrue(built.ports["lysis_waste"].is_pressure)
        self.assertEqual(built.ports["lysis_waste"].p_pa, 0.0)
        self.assertTrue(built.ports["capsule_in"].is_closed)

    def test_the_solver_scenario_carries_the_resolved_viscosity(self):
        """solve.fluid_for() trusts mu_pa_s, so it must be set and the fluid named."""
        record = self.step(run_for("module3_v1"), 2)
        built = record.scenario
        self.assertEqual(built.fluid, record.fluid.name)
        self.assertAlmostEqual(built.mu_pa_s, record.fluid.viscosity_pa_s)
        self.assertAlmostEqual(built.flow_ul_min, record.q_in_ul_min)

    def test_the_solver_scenario_reports_si_flow_too(self):
        built = self.step(run_for("module3_v1"), 2).scenario
        self.assertAlmostEqual(built.ports["reagent_in"].q_m3_s, S.ul_min_to_m3_s(10.8))
        self.assertAlmostEqual(built.q_in_m3_s, S.ul_min_to_m3_s(10.8))

    def test_an_assumed_viscosity_travels_with_the_scenario(self):
        """A Field built from an assumption must be able to say so."""
        record = self.step(run_for("module6_v1"), 6)
        self.assertTrue(record.fluid.assumption)
        questions = " ".join(q["question"] for q in record.scenario.unresolved)
        self.assertIn("not in any contract", questions)
        self.assertIn(record.fluid.fluid_class, questions)

    def test_an_inferred_port_set_travels_with_the_scenario(self):
        record = self.step(run_for("module1_v1"), 6)
        questions = " ".join(q["question"] for q in record.scenario.unresolved)
        self.assertIn("no `ports` block", questions)

    def test_the_port_role_is_kept_in_the_port_state_note(self):
        built = self.step(run_for("module3_v1"), 2).scenario
        self.assertTrue(built.ports["reagent_in"].note.startswith("feed"))
        self.assertTrue(built.ports["lysis_waste"].note.startswith("vent"))

    def test_the_scenario_survives_a_json_round_trip(self):
        built = self.step(run_for("module3_v1"), 2).scenario
        again = model.Scenario.from_json(json.loads(json.dumps(built.to_json())))
        self.assertEqual(again.step, built.step)
        self.assertEqual(again.ports["reagent_in"].q_ul_min,
                         built.ports["reagent_in"].q_ul_min)
        self.assertAlmostEqual(again.mu_pa_s, built.mu_pa_s)

    def test_make_scenario_is_available_as_a_function(self):
        """run.py looks for scenario.make_scenario(record)."""
        record = self.step(run_for("module3_v1"), 2)
        self.assertIsInstance(S.make_scenario(record), model.Scenario)

    def test_units_helpers(self):
        self.assertAlmostEqual(S.ul_min_to_m3_s(60.0), 1e-9)
        self.assertAlmostEqual(S.ul_h_to_ul_min(120.0), 2.0)

    def test_compact_step_ranges(self):
        self.assertEqual(S._compact([1, 2, 3, 7, 9, 10]), "1-3, 7, 9-10")
        self.assertEqual(S._compact([]), "-")


# --- pipeline mode -----------------------------------------------------------

class PipelineMode(Base):
    def test_pipeline_mvp_enumerates_every_chip_in_chain_order(self):
        chain = pipeline()
        self.assertEqual([link.module for link in chain.chain],
                         ["module1", "cellprep", "module2", "module3", "barcoding",
                          "hairpin", "module4", "module5"])
        self.assertEqual([link.order for link in chain.chain], list(range(1, 9)))
        self.assertEqual(len(chain.runs), 6)

    def test_pipeline_keeps_the_placeholder_modules_as_gaps(self):
        chain = pipeline()
        gaps = {link.module: link.gap for link in chain.chain if link.gap}
        self.assertEqual(set(gaps), {"barcoding", "hairpin"})
        self.assertTrue(all("no chip" in gap for gap in gaps.values()))
        self.assertTrue(any(e["kind"] == "chain_gap" for e in chain.unresolved))

    def test_pipeline_marks_the_handoff_steps(self):
        chain = pipeline()
        by_module = {link.module: link for link in chain.chain}
        leaving = [s for s in by_module["module1"].run.step_scenarios if s.handoff_out]
        self.assertEqual([s.step for s in leaving], [182])
        self.assertEqual(leaving[0].handoff_out, "module1_to_cellprep")
        arriving = by_module["cellprep"].run.step_scenarios[0]
        self.assertEqual(arriving.handoff_in, "module1_to_cellprep")

    def test_the_handoff_after_step_matches_the_pipeline_file(self):
        chain = pipeline()
        stated = {h["id"]: h["after_step"] for h in chain.handoffs}
        by_module = {link.module: link for link in chain.chain}
        marked = [s.step for s in by_module["module2"].run.step_scenarios
                  if s.handoff_out == "module2_to_module3"]
        self.assertEqual(marked, [stated["module2_to_module3"]])

    def test_pipeline_reports_the_handoff_with_no_named_port(self):
        """Module 4 has no capsule outlet, so module4_to_module5 cannot be attached."""
        gaps = {h["id"]: h["gap"] for h in pipeline().handoffs if h["gap"]}
        self.assertIn("module4_to_module5", gaps)
        self.assertIn("no port", gaps["module4_to_module5"])

    def test_the_tipseq_pipeline_also_builds(self):
        chain = pipeline("protocols/pipeline_tipseq.json")
        self.assertEqual(chain.pipeline_id, "pipeline_tipseq")
        self.assertEqual([link.module for link in chain.chain][:2], ["module1", "bench"])
        self.assertTrue(any(link.module == "bench" and link.gap for link in chain.chain))

    def test_a_partition_file_is_accepted(self):
        partition = {
            "id": "demo_run",
            "chips": [
                {"chip_id": "module3_v1_translated", "ops": [1, 2], "scale": "demo",
                 "reason": "the trap"},
                {"chip_id": "not_built_yet", "ops": [3], "scale": "demo", "reason": "later"},
            ],
            "handoffs": [{"id": "h1", "from": {"module": "module3_v1_translated"},
                          "to": {"module": "not_built_yet"}}],
            "cuts": [], "unresolved": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "partition.json"
            path.write_text(json.dumps(partition))
            chain = S.build_pipeline(path)
        self.assertEqual([link.module for link in chain.chain],
                         ["module3_v1_translated", "not_built_yet"])
        self.assertTrue(chain.chain[1].gap)
        self.assertIn("not_built_yet", chain.chain[1].gap)

    def test_an_unknown_pipeline_shape_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nonsense.json"
            path.write_text(json.dumps({"id": "x"}))
            with self.assertRaises(ValueError):
                S.build_pipeline(path)


# --- the repo-wide sweep -----------------------------------------------------

class RepoSweep(Base):
    def test_every_protocol_that_pairs_with_a_chip_builds(self):
        """Nothing may fail to build. The set is a superset: other stages generate
        protocols into protocols/ (S1-S5 writes one per translated chip), and a new one
        appearing must be built, not break this test."""
        result = sweep()
        self.assertEqual(result["failed"], [])
        self.assertTrue(set(PAIRS) <= set(result["runs"]),
                        f"not built: {sorted(set(PAIRS) - set(result['runs']))}")
        for protocol_id, run in result["runs"].items():
            with self.subTest(protocol=protocol_id):
                self.assertTrue((ROOT / run.chip_path).exists())
                self.assertTrue(run.step_scenarios)

    def test_the_pairing_matches_the_table_this_test_file_uses(self):
        for protocol_id, chip_id in PAIRS.items():
            with self.subTest(protocol=protocol_id):
                found, why = S.chip_for_protocol(protocol_json(protocol_id))
                self.assertEqual(found, chip_id)
                self.assertTrue(why)

    def test_the_unpaired_protocols_are_the_retired_and_placeholder_ones(self):
        """The four retired module drafts (their chips live in cad/, not chip/) and the
        three placeholder assays have no chip, so there is nothing to simulate."""
        result = sweep()
        self.assertTrue(
            {"m1_culture", "m1b_cellprep", "m2_encapsulation", "m3_wash",
             "placeholder_culture_then_scrnaseq", "placeholder_scdnaseq",
             "placeholder_scrnaseq"}
            <= {entry["protocol_id"] for entry in result["unpaired"]}
        )
        self.assertTrue(all(entry["reason"] for entry in result["unpaired"]))

    def test_module3_pairs_with_the_translated_chip(self):
        chip_id, why = S.chip_for_protocol(protocol_json("module3_v1"))
        self.assertEqual(chip_id, "module3_v1_translated")
        self.assertIn("module3", why)

    def test_the_report_names_every_unmappable_step(self):
        result = sweep()
        text = S.report(result)
        self.assertIn("Unmappable steps:", text)
        for run in result["runs"].values():
            for unmapped in run.unmapped:
                with self.subTest(step=unmapped.step):
                    self.assertIn(f"step {unmapped.step} ({unmapped.operation})", text)

    def test_the_report_names_every_assumption(self):
        text = S.report(sweep())
        self.assertIn("Assumptions and contract gaps:", text)
        for fluid_class, spec in S.ASSUMED_FLUIDS.items():
            if any(fluid_class in f for run in sweep()["runs"].values()
                   for s in run.scenarios for f in s.flags):
                with self.subTest(fluid=fluid_class):
                    self.assertIn(spec["wanted_key"], text)

    def test_everything_is_json_serialisable(self):
        for run in sweep()["runs"].values():
            with self.subTest(protocol=run.protocol_id):
                json.dumps(run.to_dict())
        json.dumps(pipeline().to_dict())

    def test_the_counts_in_the_summary_add_up(self):
        for run in sweep()["runs"].values():
            counts = run.to_dict()["counts"]
            with self.subTest(protocol=run.protocol_id):
                self.assertEqual(
                    counts["steps"] + counts["no_flow"] + counts["worst_case"],
                    len(run.scenarios),
                )

    def test_the_cli_runs(self):
        self.assertEqual(S.main([]), 0)
        self.assertEqual(S.main(["--pipeline", "protocols/pipeline_mvp.json"]), 0)


if __name__ == "__main__":
    unittest.main()

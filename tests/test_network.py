"""operator/network.py: the hydraulic network solver, on Module 1 v1 (MJ's chip).

    python -m unittest discover -s tests -v
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))

import network as nw  # noqa: E402
import schema_io  # noqa: E402

NET = nw.module1_network()
PROTO_FLOW = max(s["flow_ul_min"] for s in schema_io.load_json(ROOT / "protocols" / "module1_v1.json")["steps"]
                 if s.get("flow_ul_min"))
# The ceiling is a contract value, read here from contracts/parameters.json rather than
# restated, so the assertions below cannot drift from the number the design is held to.
PARAMS = schema_io.load_json(ROOT / "contracts" / "parameters.json")["parameters"]
SHEAR_CEILING = PARAMS["shear.culture_ceiling_dyn_cm2"]["value"]
CULTURE = ["n_cx", "g_cx", "a_cx"]
INLET = {"n_cx": "p_in", "g_cx": "p_g_in", "a_cx": "p_a_in"}
SOURCE = {"int_n": "n_cx", "int_g": "g_cx", "int_a": "a_cx"}


class Geometry(unittest.TestCase):
    def test_every_channel_comes_from_the_dxf(self):
        rects = nw.dxf_channel_segments(ROOT / "cad" / "exports" / "module1_v1" / "module1_v1.dxf")
        self.assertEqual(len(rects), len(NET.chip["channels"]))
        widths = {round(c["width_mm"], 3) for c in NET.chip["channels"]}
        self.assertTrue(all(round(r["width_mm"], 3) in widths and r["closed"] for r in rects))
        self.assertEqual({g["source"] for g in NET.geometry.values()}, {"DXF"})

    def test_resistance_formula(self):
        # 12 mu L / (w h^3 (1 - 0.63 h/w)) for a 10 mm, 0.8 x 0.3 mm channel at mu = 0.8 mPa s
        r = nw.rect_resistance(10, 0.8, 0.3, 0.8e-3)
        expect = 12 * 0.8e-3 * 0.01 / (0.8e-3 * (0.3e-3) ** 3 * (1 - 0.63 * 0.3 / 0.8))
        self.assertAlmostEqual(r / expect, 1.0, places=12)


class Conservation(unittest.TestCase):
    def test_mass_is_conserved_in_every_mode_and_at_every_node(self):
        for mode in nw.MODULE1_MODES:
            if mode == "idle":
                continue
            sol = NET.solve(nw.mode_ports(mode, 7.5))
            self.assertAlmostEqual(sum(p["into_chip_ul_min"] for p in sol["ports"].values()), 0.0, places=6, msg=mode)
            for node in NET.nodes:
                if node in sol["ports"]:
                    continue
                net_in = sum(-sol["channels"][e["id"]]["flow_ul_min"] if e["from"] == node else sol["channels"][e["id"]]["flow_ul_min"]
                             for e in NET.edges if node in (e["from"], e["to"]))
                self.assertAlmostEqual(net_in, 0.0, places=6, msg=f"{mode} at {node}")

    def test_capped_ports_carry_no_flow(self):
        sol = NET.solve(nw.mode_ports("grow"))
        for pid in ("p_g_out", "p_a_out"):
            self.assertEqual(sol["ports"][pid]["into_chip_ul_min"], 0)

    def test_feeding_with_nothing_vented_is_refused(self):
        with self.assertRaises(nw.NetworkError):
            NET.solve({"p_in": ("FEED", 5)})


class Modes(unittest.TestCase):
    def test_separate_growth_keeps_every_chamber_on_its_own_medium(self):
        sol = NET.solve(nw.mode_ports("grow"))
        self.assertEqual(sol["routing"]["foreign_medium"], {})
        for c in CULTURE:
            self.assertEqual(sol["routing"]["inflow_mix"][c], {INLET[c]: 1.0})
        self.assertAlmostEqual(sol["ports"]["p_mid"]["into_chip_ul_min"], -3 * nw.DEFAULT_FEED_UL_MIN, places=6)

    def test_each_interaction_sends_the_source_medium_into_the_other_two(self):
        for mode, src in SOURCE.items():
            sol = NET.solve(nw.mode_ports(mode))
            others = [c for c in CULTURE if c != src]
            for c in others:
                self.assertEqual(sol["routing"]["inflow_mix"][c], {src: 1.0}, msg=f"{mode}: {c}")
            self.assertNotIn(src, sol["routing"]["foreign_medium"], msg=mode)
            through = [sol["chambers"][c]["throughput_ul_min"] for c in others]
            self.assertAlmostEqual(sum(through), nw.DEFAULT_FEED_UL_MIN, places=6)

    def test_n_to_g_and_a_splits_evenly_by_symmetry(self):
        sol = NET.solve(nw.mode_ports("int_n"))
        g, a = (sol["chambers"][c]["throughput_ul_min"] for c in ("g_cx", "a_cx"))
        # Symmetric to within the 1 um rounding of the scaled chamber coordinates, not to 9 decimals.
        self.assertAlmostEqual(g, a, delta=1e-3 * (g + a) / 2)

    def test_design_doc_harvest_only_sweeps_n_cx(self):
        # The doc's harvest (feed p_in, vent p_mid) leaves g_cx and a_cx without flow;
        # the protocol's collect mode back-flushes them too.
        doc = NET.solve(nw.mode_ports("harvest"))
        self.assertEqual((doc["chambers"]["g_cx"]["throughput_ul_min"], doc["chambers"]["a_cx"]["throughput_ul_min"]), (0, 0))
        col = NET.solve(nw.mode_ports("collect"))
        self.assertTrue(all(col["chambers"][c]["throughput_ul_min"] > 0 for c in CULTURE))


class DesignNumbers(unittest.TestCase):
    def test_the_solver_reads_its_ceiling_and_viscosity_from_the_contracts(self):
        self.assertEqual(nw.SHEAR_CEILING_DYN_CM2, SHEAR_CEILING)
        # The 37 C medium value, not fluid.viscosity_pa_s (water at 20 C, the dye-demo figure).
        self.assertEqual(nw.MU_37C, PARAMS["fluid.viscosity_37c_pa_s"]["value"])

    def test_the_threshold_flow_is_the_flow_at_which_shear_reaches_the_ceiling(self):
        # The old form of this test restated two uL/min figures from the design doc, which
        # were computed at a viscosity the contract no longer holds. The invariant is what
        # matters: threshold_flow inverts shear_dyn_cm2 at the ceiling, for real geometry.
        chamber = next(c for c in NET.chip["chambers"] if c["id"] == "n_cx")
        w_chamber, depth = chamber["size"][1], chamber["size"][2]
        w_channel = min(c["width_mm"] for c in NET.chip["channels"])
        for label, width in (("chamber n_cx", w_chamber), ("narrowest channel", w_channel)):
            q = nw.threshold_flow(width, depth, SHEAR_CEILING, NET.mu)
            self.assertAlmostEqual(nw.shear_dyn_cm2(q, width, depth, NET.mu), SHEAR_CEILING, places=12, msg=label)
            self.assertGreater(q, 0, label)
        # Shear scales as 1/width, so the narrow channel is always the binding one.
        self.assertLess(nw.threshold_flow(w_channel, depth, SHEAR_CEILING, NET.mu),
                        nw.threshold_flow(w_chamber, depth, SHEAR_CEILING, NET.mu))

    def test_chambers_stay_under_the_shear_ceiling_at_the_protocol_flow(self):
        for mode, sol in nw.solve_modes(NET, PROTO_FLOW).items():
            for c in CULTURE:
                self.assertLess(sol["chambers"][c]["shear_dyn_cm2"], SHEAR_CEILING, msg=f"{mode} {c}")

    def test_operator_default_feed_stays_under_the_shear_ceiling(self):
        for mode, sol in nw.solve_modes(NET).items():
            for c in CULTURE:
                self.assertLess(sol["chambers"][c]["shear_dyn_cm2"], SHEAR_CEILING,
                                msg=f"{mode} {c}: operator/network.py DEFAULT_FEED_UL_MIN={nw.DEFAULT_FEED_UL_MIN} is stale")

    def test_running_pressure_range_is_reported_not_tuned(self):
        rows = {r["quantity"]: r for r in nw.design_check(NET)}
        row = next(v for k, v in rows.items() if k.startswith("Running pressure"))
        lo, hi = (float(x) for x in row["solver"].split("-"))
        expect = [s["max_pressure_mbar"] for m, s in nw.solve_modes(NET).items() if m != "collect"]
        self.assertTrue(0 < lo <= hi)
        self.assertAlmostEqual(lo, min(expect), places=3)
        self.assertAlmostEqual(hi, max(expect), places=3)


class AnyChip(unittest.TestCase):
    def test_solver_runs_on_the_shipped_module_3_chip(self):
        # Was cad/m3_wash_spec.json, which is marked superseded and is out of the pipeline.
        chip = json.loads((ROOT / "chip" / "module3_v1_translated.json").read_text())
        load = next(p["id"] for p in chip["ports"] if p.get("role") == "load")
        waste = next(p["id"] for p in chip["ports"] if p.get("role") == "waste")
        out = next(c["id"] for c in chip["channels"] if c["to"] == waste)
        sol = nw.Network(chip).solve({load: ("FEED", 20), waste: "VENT"})
        self.assertAlmostEqual(sol["channels"][out]["flow_ul_min"], 20.0, places=6)
        self.assertAlmostEqual(sol["ports"][waste]["into_chip_ul_min"], -20.0, places=6)


if __name__ == "__main__":
    unittest.main()

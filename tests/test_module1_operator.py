"""operator/module1_operator.py: Module 1 v1's operator, its safety rules and dye times.

    python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))

import module1_operator as op  # noqa: E402
import network as nw  # noqa: E402
import schema_io  # noqa: E402

NET = nw.module1_network()
CULTURE = [c for c in NET.chip["chambers"] if c["id"] in op.CULTURE]
# Flow derived from the chip and its protocol, not hard-coded: the protocol's own exchange rate, which
# the protocol builder keeps under the shear ceiling for the chip's current chamber size.
PROTO_FLOW = max(s["flow_ul_min"] for s in schema_io.load_json(ROOT / "protocols" / "module1_v1.json")["steps"]
                 if s.get("flow_ul_min"))
CEILING_FLOW = min(nw.threshold_flow(c["size"][1], c["size"][2], op.SHEAR_CEILING, NET.mu) for c in CULTURE)


def custom(**states):
    st = {p: states.get(p, "CAP") for p in op.PORTS}
    return st, {p: PROTO_FLOW for p, s in st.items() if s == "FEED"}


class Safety(unittest.TestCase):
    def test_every_design_mode_passes_at_the_protocol_flow(self):
        for mode in ("grow", "int_n", "int_g", "int_a", "harvest", "seed", "collect"):
            v = op.check(NET, mode, *op.states_of(mode, PROTO_FLOW))
            self.assertEqual(v["status"], "approved", msg=f"{mode}: {v['reasons']} {v['warnings']}")

    def test_mixing_media_during_separate_growth_is_blocked(self):
        v = op.check(NET, "grow", *custom(p_in="FEED", p_g_in="FEED", p_a_in="FEED", p_g_out="VENT"))
        self.assertEqual(v["status"], "blocked")
        self.assertTrue(any("keep the media apart" in r and "g_cx receives" in r for r in v["reasons"]))

    def test_chamber_shear_over_the_ceiling_is_blocked(self):
        states, flows = op.states_of("grow", 1.5 * CEILING_FLOW)   # each chamber carries more than the ceiling flow
        v = op.check(NET, "grow", states, flows)
        self.assertEqual(v["status"], "blocked")
        self.assertTrue(any("wall shear" in r for r in v["reasons"]))

    def test_an_extra_vent_is_a_warning(self):
        v = op.check(NET, "grow", *custom(p_in="FEED", p_g_in="FEED", p_a_in="FEED", p_mid="VENT", p_g_out="VENT"))
        self.assertEqual(v["status"], "warning")
        self.assertTrue(any("p_g_out also vented" in w for w in v["warnings"]))

    def test_a_stalled_feed_line_warns_about_reverse_flow(self):
        states, flows = op.states_of("grow", PROTO_FLOW)
        flows["p_g_in"] = 0.0
        v = op.check(NET, "grow", states, flows)
        self.assertTrue(any("p_g_in is FEED at 0" in w and "flows back into it" in w for w in v["warnings"]))

    def test_a_weak_feed_line_warns_about_back_pressure(self):
        states, flows = op.states_of("grow", PROTO_FLOW)
        flows["p_g_in"] = 0.05 * PROTO_FLOW                # far below the other two
        v = op.check(NET, "grow", states, flows)
        self.assertTrue(any(w.startswith("p_g_in: the rest of the chip pushes back") for w in v["warnings"]))

    def test_nothing_vented_is_blocked(self):
        v = op.check(NET, "custom", *custom(p_in="FEED"))
        self.assertEqual(v["status"], "blocked")


class ProtocolFlow(unittest.TestCase):
    def test_protocol_flow_stays_under_the_chamber_shear_ceiling(self):
        self.assertLess(PROTO_FLOW, CEILING_FLOW)


class Operator(unittest.TestCase):
    def test_scripted_run_blocks_the_mistake_and_keeps_the_previous_state(self):
        d = op.run_scripted(NET, PROTO_FLOW)
        blocked = [x for x in d if x["status"] == "blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertFalse(blocked[0]["applied"])
        self.assertEqual(blocked[0]["kept"], "grow")
        self.assertTrue(all("predicted" in x for x in d))

    def test_dye_reaches_the_junction_before_the_outlets(self):
        t = op.dye_times(NET, *op.states_of("int_n", 50.0))["p_in"]
        self.assertLess(t["j_mid"]["mean_s"], t["p_g_out"]["mean_s"])
        self.assertAlmostEqual(t["p_g_out"]["mean_s"], t["p_a_out"]["mean_s"], places=6)   # symmetric
        self.assertAlmostEqual(t["j_mid"]["first_s"], t["j_mid"]["mean_s"] * 2 / 3, delta=0.1)


if __name__ == "__main__":
    unittest.main()

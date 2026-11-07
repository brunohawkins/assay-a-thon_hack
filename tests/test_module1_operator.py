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

NET = nw.module1_network()


def custom(**states):
    st = {p: states.get(p, "CAP") for p in op.PORTS}
    return st, {p: 11.0 for p, s in st.items() if s == "FEED"}


class Safety(unittest.TestCase):
    def test_every_design_mode_passes_at_the_protocol_flow(self):
        for mode in ("grow", "int_n", "int_g", "int_a", "harvest", "seed", "collect"):
            v = op.check(NET, mode, *op.states_of(mode))
            self.assertEqual(v["status"], "approved", msg=f"{mode}: {v['reasons']} {v['warnings']}")

    def test_mixing_media_during_separate_growth_is_blocked(self):
        v = op.check(NET, "grow", *custom(p_in="FEED", p_g_in="FEED", p_a_in="FEED", p_g_out="VENT"))
        self.assertEqual(v["status"], "blocked")
        self.assertTrue(any("keep the media apart" in r and "g_cx receives" in r for r in v["reasons"]))

    def test_chamber_shear_over_the_ceiling_is_blocked(self):
        states, flows = op.states_of("grow", 20.0)        # 20 uL/min through each chamber > 13.5
        v = op.check(NET, "grow", states, flows)
        self.assertEqual(v["status"], "blocked")
        self.assertTrue(any("wall shear" in r for r in v["reasons"]))

    def test_an_extra_vent_is_a_warning(self):
        v = op.check(NET, "grow", *custom(p_in="FEED", p_g_in="FEED", p_a_in="FEED", p_mid="VENT", p_g_out="VENT"))
        self.assertEqual(v["status"], "warning")
        self.assertTrue(any("p_g_out also vented" in w for w in v["warnings"]))

    def test_a_stalled_feed_line_warns_about_reverse_flow(self):
        states, flows = op.states_of("grow")
        flows["p_g_in"] = 0.0
        v = op.check(NET, "grow", states, flows)
        self.assertTrue(any("p_g_in is FEED at 0" in w and "flows back into it" in w for w in v["warnings"]))

    def test_a_weak_feed_line_warns_about_back_pressure(self):
        states, flows = op.states_of("grow")
        flows["p_g_in"] = 0.5                              # far below the other two
        v = op.check(NET, "grow", states, flows)
        self.assertTrue(any(w.startswith("p_g_in: the rest of the chip pushes back") for w in v["warnings"]))

    def test_nothing_vented_is_blocked(self):
        v = op.check(NET, "custom", *custom(p_in="FEED"))
        self.assertEqual(v["status"], "blocked")


class Operator(unittest.TestCase):
    def test_scripted_run_blocks_the_mistake_and_keeps_the_previous_state(self):
        d = op.run_scripted(NET, 11.0)
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

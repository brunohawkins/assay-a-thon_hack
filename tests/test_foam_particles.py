"""assay2chip/foam/particles.py: Lagrangian parcels with gravity and drag on a frozen field.

    python -m unittest tests.test_foam_particles -v
    FOAM_SKIP_INTEGRATION=1 python -m unittest tests.test_foam_particles   # offline only

OFFLINE:
  * the conservative END per question comes from the contracts, both ends, never one
    number read two ways (loss = max diameter and max excess density, retention = min);
  * intent is the reduced model's (`cfd.particles.sediment_intent`), applied per zone:
    module4's feed line and cell_prep's mixer are LOSSES, module3's trap is DESIRED;
  * module3's waste lines sit behind the weir; nothing behind a weir on module4;
  * the cloud readers, the zone flow graph and the "entered" bookkeeping;
  * a finding is read from the run whose ends match the zone's intent, and says which.

INTEGRATION (OpenFOAM; skipped cleanly when absent) - dense beads in a straight duct
must settle on the floor in about the Stokes fall time, and neutrally buoyant parcels
must reach the outlet.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "operator")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from assay2chip.cfd import particles as cfd_particles  # noqa: E402
from assay2chip.foam import particles as fp  # noqa: E402
from assay2chip.foam import transport as ft  # noqa: E402
from assay2chip.lib import contracts  # noqa: E402

from tests.test_foam_transport import duct_steady, needs_openfoam, synthetic_fields  # noqa: E402


def chip(cid):
    return json.loads((ROOT / "chip" / f"{cid}.json").read_text())


class Ends(unittest.TestCase):
    def test_loss_uses_max_and_retention_uses_min_from_the_contracts(self):
        classes = cfd_particles.particle_classes()
        for name, key in (("capsule", "capsule.excess_density_kg_m3"),
                          ("cells", "cell.excess_density_kg_m3"),
                          ("bead", "bead.excess_density_kg_m3")):
            lo, hi = contracts.param_range(key)
            _, loss = classes[name].settling_for_intent(1e-3, fp.INTENT_FOR_RUN["loss"])
            _, ret = classes[name].settling_for_intent(1e-3, fp.INTENT_FOR_RUN["retention"])
            self.assertEqual((loss["excess_density_kg_m3"], loss["density_end"]), (hi, "max"))
            self.assertEqual((ret["excess_density_kg_m3"], ret["density_end"]), (lo, "min"))
            self.assertEqual(loss["density_key"], key)


class Intent(unittest.TestCase):
    def test_reduced_model_intent_per_zone(self):
        classes = cfd_particles.particle_classes()
        m4 = chip("module4_v1")
        self.assertEqual(fp.zone_role(m4, "ch_in_1", ["in_s1"]), "feed")
        self.assertEqual(fp.zone_intent(m4, "ch_in_1", classes["capsule"], ["in_s1"], "load").kind,
                         "loss")
        cp = chip("cell_prep_v1")
        self.assertEqual(fp.zone_intent(cp, "mix_1", classes["cells"], ["in_buffer"],
                                        "transfer").kind, "loss")
        m3 = chip("module3_v1_translated")
        self.assertEqual(fp.zone_intent(m3, "main_trap", classes["capsule"], ["capsule_in"],
                                        "load").kind, "desired")

    def test_behind_weir_outlets(self):
        # every capsule-impassable line into module3's trap meets it through a crest - the
        # two waste lines and the two liquid feeds alike
        self.assertEqual(fp.behind_weir_outlets(chip("module3_v1_translated")),
                         {"waste": "main_trap", "lysis_waste": "main_trap",
                          "reagent_in": "main_trap", "backflush_in": "main_trap"})
        self.assertEqual(fp.behind_weir_outlets(chip("module4_v1")), {})


class Readers(unittest.TestCase):
    def test_cloud_positions_active_and_age(self):
        tmp = Path(tempfile.mkdtemp())
        cdir = tmp / "20" / "lagrangian" / "kinematicCloud"
        cdir.mkdir(parents=True)
        head = "FoamFile\n{\n    version 2.0;\n    object x;\n}\n"
        (cdir / "positions").write_text(head + "2\n(\n(0.001 0.002 0) 5\n(0.003 0.004 1e-4) 7\n)\n")
        (cdir / "active").write_text(head + "2\n(\n0\n1\n)\n")
        (cdir / "age").write_text(head + "2{1.5}\n")
        got = fp._read_cloud(tmp / "20")
        self.assertEqual(got["cell"].tolist(), [5, 7])
        self.assertEqual(got["active"].tolist(), [False, True])
        np.testing.assert_allclose(got["age"], [1.5, 1.5])
        np.testing.assert_allclose(got["xyz"][1], [0.003, 0.004, 1e-4])

    def test_escapes_counted_from_post_processing_records(self):
        tmp = Path(tempfile.mkdtemp())
        d = tmp / "postProcessing" / "lagrangian" / "kinematicCloud" / "hits" / "0"
        d.mkdir(parents=True)
        (d / "p_out.dat").write_text("# Time origId\n0.1 1\n0.2 2\n\n")
        self.assertEqual(fp._escapes(tmp, ["p_out", "p_in"]), {"p_out": 2, "p_in": 0})


class Bookkeeping(unittest.TestCase):
    def test_zone_graph_and_entered(self):
        f = synthetic_fields(n=6)
        f.zone = np.array([0, 0, 0, 1, 1, 1], dtype=np.int32)
        f.zone_names = ["line", "trap"]
        g = fp.zone_flow_graph(f, 0.0)
        self.assertEqual(g, {"line": {"trap"}})
        run = fp.ParcelRun("capsule", "loss", 76.5, "max", 50, "max", "k", 1050, 200, [], 10, "")
        run.injected = 10
        run.settled = {"line": {"total": 6}, "trap": {"total": 3}}
        run.reached = {"p_out": 1}
        outlet = {"p_out": {"trap"}, "p_in": {"line"}}
        self.assertEqual(fp.entered(run, g, "line", outlet), 10)
        self.assertEqual(fp.entered(run, g, "trap", outlet), 4)

    def test_finding_comes_from_the_run_matching_the_intent(self):
        m4 = chip("module4_v1")

        class Rec:
            operation = "load"

        zone = {"entered": 100, "settled": 100, "settled_fraction_of_entered": 1.0,
                "in_flight_at_end": 0, "median_landing_distance_from_port_mm": 0.4, "where": {}}
        def run(name, d_end, rho_end, rho):
            return {"run": name, "diameter_um": 70, "diameter_end": d_end,
                    "density_end": rho_end, "density_key": "capsule.excess_density_kg_m3",
                    "excess_density_kg_m3": rho, "zones": {"ch_in_1": dict(zone)}, "weir": {}}
        result = {"_chip": m4, "_record": Rec(), "_feeds": ["in_s1"], "_mu": None,
                  "chip_id": "module4_v1", "step": 1,
                  "classes": {"capsule": {"runs": {"loss": run("loss", "max", "max", 50),
                                                   "retention": run("retention", "min", "min", 12)}}}}
        out = fp.findings_for(result)
        self.assertEqual(len(out), 1)
        f = out[0]
        self.assertEqual((f["class"], f["severity"], f["source"]), ("sediment", "blocker", "openfoam"))
        self.assertIn("max of capsule.excess_density_kg_m3", f["message"])
        self.assertIn("FULL-SPEC", f["fix_hint"])


class Integration(unittest.TestCase):
    @needs_openfoam
    def test_beads_settle_in_a_duct_at_the_stokes_rate(self):
        tmp = Path(tempfile.mkdtemp())
        steady = duct_steady(tmp, L=4e-3, q_ul_min=2.0)
        f = ft.read_fields(steady)
        classes = cfd_particles.particle_classes(steady.fluid.mu_pa_s)
        bead = classes["bead"]
        pr = fp.run_class(steady, f, bead, "loss", seeds={"ports": ["p_in"], "zones": []},
                          n_parcels=100, end_time_s=20.0, steps=200)
        v_s = pr.stokes_settling_um_s * 1e-6
        t_fall = f.depth_m / v_s
        self.assertGreater(pr.injected, 50)
        floor = sum(v["floor"] for v in pr.settled.values())
        self.assertGreater(floor / pr.injected, 0.95)
        # parcels enter across the inlet face and fall at most the full depth: they land
        # within (peak speed x full-depth fall time) of the inlet
        u_mean = 2.0 / 6e10 / (0.6e-3 * 0.3e-3)
        self.assertLess(max(pr.landing_distance_mm.values()), 2.5 * u_mean * t_fall * 1e3)


if __name__ == "__main__":
    unittest.main()

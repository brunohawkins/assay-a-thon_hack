"""The generators as a design tool: flags from the parameter schema, the NL hook, display export.

    python -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))
sys.path.insert(0, str(ROOT / "cad"))
sys.path.insert(0, str(ROOT / "cad" / "generators"))
sys.path.insert(0, str(ROOT / "chip" / "nl_to_spec"))

import export_display_model as edm  # noqa: E402
import generators  # noqa: E402
import genlib  # noqa: E402
import schema_io  # noqa: E402


class Params(unittest.TestCase):
    def test_flags_come_from_the_schema(self):
        params, _ = genlib.parse_params("m2_encapsulation", ["--nozzle-width-um", "50", "--inlet-filters", "false"], "t")
        self.assertEqual(params["nozzle_width_um"], 50)
        self.assertIs(params["inlet_filters"], False)
        self.assertEqual(params["oil_flow_ul_h"], 700)  # default filled in

    def test_params_json_and_flags_combine(self):
        params, _ = genlib.parse_params("m3_wash", ["--params-json", '{"inlets": 2}', "--capsule-count", "20000"], "t")
        self.assertEqual((params["inlets"], params["capsule_count"]), (2, 20000))

    def test_every_generator_has_a_schema_with_defaults(self):
        for name in generators.GENERATORS:
            schema = genlib.schema_for(name)
            self.assertTrue(all("default" in p and "description" in p for p in schema["properties"].values()), name)


class NLHook(unittest.TestCase):
    def test_build_from_a_parameter_object(self):
        spec = generators.build("m3_wash", {"capsule_count": 40000})
        self.assertAlmostEqual(spec["chambers"][0]["size"][0], 14.0)

    def test_tool_definitions_cover_every_generator(self):
        tools = generators.tool_definitions()
        self.assertEqual({t["name"] for t in tools}, {f"design_{n}" for n in generators.GENERATORS})
        props = next(t for t in tools if t["name"] == "design_m2_encapsulation")["input_schema"]["properties"]
        self.assertNotIn("minimum", props["y_junction_angle_deg"])  # limits are enforced by build(), not the tool


class DisplayModel(unittest.TestCase):
    def test_every_part_file_is_module_prefixed(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(edm, "DISPLAY_DIR", Path(tmp)):
            for spec in ("m1_culture", "m1b_cellprep", "m2_encapsulation", "m3_wash"):
                m = edm.export(ROOT / "cad" / f"{spec}_spec.json")
                prefix = schema_io.load_json(ROOT / "cad" / f"{spec}_spec.json")["module"]["id"] + "_"
                self.assertTrue(all(p["file"].startswith(prefix) for p in m["parts"]), spec)
                self.assertTrue(m["print_file"].startswith(prefix))


if __name__ == "__main__":
    unittest.main()

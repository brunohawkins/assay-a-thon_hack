"""Design rules every SHIPPED artefact must obey, read from contracts/ and applied to the
chips on disk and the protocols paired with them.

Every rule in this file had no test, and most have no checker either:

  ONE ETCH DEPTH      one fluidic layer means one depth, and one floor plane.
  WEIR GAP            the LOWER bound (>= trap.gap_factor_low x diameter) and the crest
                      derivation (crest = that chip's etch depth - gap). The crest fields
                      are optional in the schema, so they are checked only WHERE PRESENT.
  FABRICATION FLOORS  min_wall_mm, min_port_mm and min_enclosed_printed_channel_mm from
                      contracts/blocks.json are read by no other code and no other test.
  PORT PITCH          tools/check_chip_spec.py only WARNS, and it exits 0 on warnings, so
                      nothing fails on it. Here it is an assertion.
  PROTOCOL VOLUMES    a LOAD must fit the chamber, and an exchange is held to the rule for
                      the job it declares in `exchange_kind` (sweep / partial / fill), with
                      sweep as the strict default when the field is absent. The whole
                      protocol must also fit the reagent reservoirs and the waste bottle.
  WALL SHEAR          against the ceiling that belongs to what the chamber HOLDS:
                      shear.culture_ceiling_dyn_cm2 for adherent cells, 
                      shear.prelysis_rinse_ceiling_dyn_cm2 for a rinse before a deliberate
                      lyse, and capsule.escape_pressure_kpa for capsules behind a retention
                      (a capsule is attached to nothing, so shear is the wrong bound).

Chips are discovered the way tools/check_chip_spec.py discovers them (chip/*.json and
cad/*_spec.json) and protocols are paired the way it pairs them (through runs/), so a new
chip or protocol is covered automatically. Numbers come from contracts/; none are restated
here.

    python -m unittest discover -s tests -v
"""

import copy
import math
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "cad"))

import check_chip_spec as ccs  # noqa: E402
import commands  # noqa: E402
import make_figures as mf  # noqa: E402  the repo's only planar-collision / clearance detector
import schema_io  # noqa: E402
from model import ModelParams, ProcessModel  # noqa: E402
from state import ChipState  # noqa: E402

PARAMS = schema_io.load_json(ROOT / "contracts" / "parameters.json")
BLOCKS = schema_io.load_json(ROOT / "contracts" / "blocks.json")
FLOORS = BLOCKS["fabrication_floors"]
WEIR = BLOCKS["retention"]["weir_geometry"]
SPECS = {path: schema_io.load_json(path) for path in ccs.spec_paths([])}
TOL = 1e-9


def param(key: str) -> float:
    return PARAMS["parameters"][key]["value"]


MU_PA_S = param("fluid.viscosity_37c_pa_s")   # 37 C medium, not water at 20 C
CULTURE_CEILING = param("shear.culture_ceiling_dyn_cm2")
RINSE_CEILING = param("shear.prelysis_rinse_ceiling_dyn_cm2")
ESCAPE_KPA = param("capsule.escape_pressure_kpa")
GAP_FACTOR_LOW = param("trap.gap_factor_low")
GAP_FACTOR_HIGH = param("trap.gap_factor_high")
DEFAULT_DIAMETER_UM = param("capsule.diameter_um")
RESERVOIR_UL = param("reservoir.default_ul")
WASTE_UL = param("waste.capacity_ul")
EXCHANGE_TARGET = param("exchange.target_fraction")
BACKFLUSH_TARGET = param("backflush.target_fraction")
# A well-mixed chamber keeps exp(-V) of its old liquid after V chamber volumes, so reaching
# exchange.target_fraction takes -ln(1 - target) chamber volumes in total. Derived, not stated.
SWEEP_VOLUMES = -math.log(1 - EXCHANGE_TARGET)

# A chamber volume is often rounded DOWN when it is written into a protocol (e.g. 3.1 uL
# into a 3.132 uL chamber), so "about one chamber volume" allows 2%. 0.5 chamber volumes
# is not about one chamber volume.
SWEEP_TOL = 0.02
# What an exchange is FOR, from contracts/schemas/protocol.schema.json. ABSENCE MEANS SWEEP:
# the strict case, so a step that forgets to declare itself is still held to the floor.
DEFAULT_EXCHANGE_KIND = "sweep"
EXCHANGE_KINDS = set(schema_io.load_json(ROOT / "contracts" / "schemas" / "protocol.schema.json")
                     ["$defs"]["step"]["properties"]["exchange_kind"]["enum"])
# A partial has to say what fraction it replaces: a percentage, a number of chamber volumes,
# or the word half.
FRACTION_IN_NOTE = re.compile(r"\d+(?:\.\d+)?\s*%|\d*\.?\d+\s*(?:of\s+(?:a|the)\s+)?chamber\s+volume|\bhalf\b", re.I)

LYSE_WORDS = ("lysis", "lyse", "quench", "solubilisation")
RINSE_WORDS = ("rinse",)
CAPSULE_WORDS = ("capsule", "emulsion", "droplet", "bead")


def capsule_diameter_um(chip: dict) -> float:
    return chip.get("capsule", {}).get("diameter_um", DEFAULT_DIAMETER_UM)


def depths_um(chip: dict) -> dict[str, float]:
    """Every etched feature's depth in um, by feature id."""
    out = {f"chamber '{c['id']}'": c["size"][2] * 1000 for c in chip.get("chambers", [])}
    out.update({f"channel '{c['id']}'": c["depth_mm"] * 1000 for c in chip.get("channels", [])})
    return out


def etch_depth_um(chip: dict) -> float:
    """The chip's one etch depth: the deepest feature (test_every_chamber_and_channel_shares_one_depth
    is what makes "the deepest" and "the one" the same number)."""
    return max(depths_um(chip).values())


def pair_protocols(chip: dict) -> list:
    return ccs.paired_protocols(chip, [])


def is_superseded(chip: dict) -> bool:
    """A retired design, by its own declaration. The only shape this test file reads."""
    return (chip.get("module") or {}).get("status") == "superseded"


def exchange_kind(step: dict) -> str:
    """What the step declares it is for. Absence is SWEEP, the strict default."""
    return step.get("exchange_kind") or DEFAULT_EXCHANGE_KIND


def first_lyse_step(steps: list) -> dict[str, int]:
    """The step number at which each chamber's contents are deliberately killed."""
    out: dict[str, int] = {}
    for s in steps:
        name = str(s.get("reagent") or "").lower()
        if any(w in name for w in LYSE_WORDS):
            for t in commands.step_targets(s):
                out.setdefault(t, s["step"])
    return out


def cultured_chambers(steps: list) -> set:
    """Chambers where cells are grown, read from the protocol the way check_chip_spec reads it."""
    out = set()
    for s in steps:
        names = [str(x).lower() for x in [s.get("reagent")] + list(s.get("reagents") or []) if x]
        if s.get("operation") == "culture" or any(w in n for n in names for w in ccs.CULTURE_WORDS):
            out |= set(commands.step_targets(s))
    return out


def wall_shear_dyn_cm2(flow_ul_min: float, width_mm: float, depth_mm: float) -> float:
    """tau = 6 mu Q / (w h^2) in a wide slit, in dyn/cm2 (1 Pa = 10 dyn/cm2), the same
    formula as operator/network.py and cad/generators/m3_wash.py, with the contract viscosity."""
    q = flow_ul_min * 1e-9 / 60
    return 10 * 6 * MU_PA_S * q / ((width_mm / 1000) * (depth_mm / 1000) ** 2)


def widest_section_mm(chamber: dict) -> float:
    """The wetted width of the chamber's cross-section, taking the FAVOURABLE orientation.

    A spec does not say which way the liquid crosses a chamber (module 1 feeds along its
    long axis, module 6 deliberately across its short one), so the width could be either
    in-plane side. Taking the longer side gives the LOWEST shear, so a failure below means
    no orientation of that chamber is under the ceiling.
    """
    return max(chamber["size"][0], chamber["size"][1])


class PerChip(unittest.TestCase):
    """One subtest per spec on disk, so a bad chip names itself (as tests/test_envelope.py)."""

    def for_every_chip(self, check_chip) -> None:
        self.assertTrue(SPECS, "no chip specs found")
        for path, chip in SPECS.items():
            with self.subTest(chip=str(path.relative_to(ROOT))):
                check_chip(chip)

    def for_every_protocol(self, check_pair) -> None:
        """Protocol rules run on SHIPPED chips only: a retired design's volumes describe a
        chip we are not building, so holding its protocol to the rules buys nothing.

        The decision is made from the spec's own `module.status`, never from an id or a file
        name, it is reported as a skip rather than passing quietly, and
        PipelineIsShipped.test_no_chip_in_the_pipeline_is_superseded stops the exemption
        becoming a way to dodge a rule.
        """
        pairs = 0
        for path, chip in SPECS.items():
            for proto_path, proto in pair_protocols(chip):
                pairs += 1
                with self.subTest(chip=str(path.relative_to(ROOT)), protocol=str(proto_path.relative_to(ROOT))):
                    if is_superseded(chip):
                        self.skipTest(f"{chip['id']} is module.status == 'superseded' "
                                      f"(superseded_by {chip['module'].get('superseded_by', 'unstated')}): "
                                      f"a retired reference design, not something we ship")
                    check_pair(chip, proto)
        self.assertTrue(pairs, "no chip/protocol pair found: the pairing through runs/ is broken")


class PipelineIsShipped(unittest.TestCase):
    """The guard on the superseded exemption in PerChip.for_every_protocol.

    Skipping a retired design is only safe while "retired" cannot be claimed for a chip we
    are actually building. So: every chip the pipeline names must NOT be superseded. Mark a
    pipeline chip superseded to dodge a volume or shear rule and this fails instead.
    """

    def pipeline_chips(self):
        (path, pipe), = schema_io.pipelines()
        out = []
        for m in pipe["modules"]:
            chip_path = schema_io.pipeline_chip(m)[0]
            if chip_path:
                out.append((m["module"], chip_path, schema_io.load_json(ROOT / chip_path)))
        return path, out

    def test_no_chip_in_the_pipeline_is_superseded(self):
        _, chips = self.pipeline_chips()
        self.assertTrue(chips, "the pipeline names no chip, so nothing is being checked")
        for module, chip_path, chip in chips:
            with self.subTest(module=module, chip=chip_path):
                self.assertFalse(is_superseded(chip),
                                 f"{chip_path} is in the pipeline as {module} but declares "
                                 f"module.status == 'superseded', which would exempt its protocol from "
                                 f"every rule in ProtocolVolumes and ShearAndEscapePressure")

    def test_every_skipped_chip_names_what_replaced_it(self):
        # A retired design has to point at its replacement, or the skip hides a gap instead
        # of recording a decision.
        retired = {path: chip for path, chip in SPECS.items() if is_superseded(chip)}
        for path, chip in retired.items():
            with self.subTest(chip=str(path.relative_to(ROOT))):
                successor = chip["module"].get("superseded_by")
                self.assertTrue(successor, "superseded, but nothing says what replaced it")
                self.assertTrue((ROOT / successor).is_file(), f"superseded_by {successor} does not exist")
                self.assertFalse(is_superseded(schema_io.load_json(ROOT / successor)),
                                 f"{successor} is superseded too: the chain does not end at a shipped chip")


class OneEtchDepth(PerChip):
    """One fluidic layer means ONE etch depth. Nothing in the repo checked this."""

    def test_every_chamber_and_channel_shares_one_depth(self):
        def one(chip):
            by_depth: dict[float, list] = {}
            for what, depth in depths_um(chip).items():
                by_depth.setdefault(round(depth, 6), []).append(what)
            self.assertEqual(len(by_depth), 1,
                             "a second etch depth is a second process we cannot run: "
                             + "; ".join(f"{d:g} um: {', '.join(ids)}" for d, ids in sorted(by_depth.items())))
        self.for_every_chip(one)

    def test_every_chamber_sits_on_one_floor_plane(self):
        def one(chip):
            floors: dict[float, list] = {}
            for c in chip.get("chambers", []):
                if len(c.get("center") or []) > 2:
                    floors.setdefault(round(c["center"][2] - c["size"][2] / 2, 4), []).append(c["id"])
            if not floors:
                return
            self.assertEqual(len(floors), 1,
                             "one etch into one layer gives one floor: "
                             + "; ".join(f"z={z:g} mm: {', '.join(ids)}" for z, ids in sorted(floors.items())))
        self.for_every_chip(one)

    def test_declared_chamber_height_matches_its_depth(self):
        def one(chip):
            for c in chip.get("chambers", []):
                if "height_um" in c:
                    self.assertAlmostEqual(c["height_um"], c["size"][2] * 1000, places=6, msg=f"chamber '{c['id']}'")
        self.for_every_chip(one)


class WeirGeometry(PerChip):
    """The retention gap's band and the crest that makes it, from contracts/blocks.json.

    check_chip_spec.py enforces the UPPER bound only (>= 0.4 x diameter is an error).
    The lower bound and `crest = etch depth - gap` were written down and never checked.
    """

    def retentions(self, chip):
        return [(c, c["retention"]) for c in chip.get("chambers", []) if c.get("retention")]

    def test_the_contract_still_states_the_weir_band(self):
        # Pins the factors the assertions below read, so the band cannot be widened quietly.
        self.assertLess(GAP_FACTOR_LOW, GAP_FACTOR_HIGH)
        self.assertEqual(WEIR["gap_um"], 25)
        self.assertTrue(GAP_FACTOR_LOW * DEFAULT_DIAMETER_UM <= WEIR["gap_um"] < GAP_FACTOR_HIGH * DEFAULT_DIAMETER_UM)

    def test_every_gap_is_at_or_above_the_lower_bound(self):
        def one(chip):
            d = capsule_diameter_um(chip)
            for c, ret in self.retentions(chip):
                self.assertGreaterEqual(ret["opening_um"], GAP_FACTOR_LOW * d - TOL,
                                        f"chamber '{c['id']}': {ret['type']} opening {ret['opening_um']:g} um is below "
                                        f"{GAP_FACTOR_LOW:g} x {d:g} um; below the band the gap closes on tolerance")
        self.for_every_chip(one)

    def test_every_gap_is_below_the_upper_bound(self):
        def one(chip):
            d = capsule_diameter_um(chip)
            for c, ret in self.retentions(chip):
                self.assertLess(ret["opening_um"], GAP_FACTOR_HIGH * d,
                                f"chamber '{c['id']}': {ret['type']} opening {ret['opening_um']:g} um passes a "
                                f"{d:g} um capsule")
        self.for_every_chip(one)

    def test_a_crest_where_one_is_declared_is_the_etch_depth_minus_the_gap(self):
        # crest_height_um / crest_thickness_um / retains_um are OPTIONAL schema fields, so
        # they are checked only where a chip carries them.
        def one(chip):
            depth = etch_depth_um(chip)
            for c, ret in self.retentions(chip):
                if "crest_height_um" in ret:
                    self.assertAlmostEqual(ret["crest_height_um"], depth - ret["opening_um"], places=6,
                                           msg=f"chamber '{c['id']}': a crest is this chip's etch depth "
                                               f"({depth:g} um) minus the gap, never a fixed number")
                if "crest_thickness_um" in ret:
                    self.assertGreaterEqual(ret["crest_thickness_um"], FLOORS["min_open_channel_mm"] * 1000 - TOL,
                                            f"chamber '{c['id']}': crest thickness is IN PLANE, so the in-plane floor applies")
        self.for_every_chip(one)

    def test_a_weir_retains_what_it_says_it_retains(self):
        def one(chip):
            for c, ret in self.retentions(chip):
                if "retains_um" in ret:
                    self.assertLess(ret["opening_um"], GAP_FACTOR_HIGH * ret["retains_um"],
                                    f"chamber '{c['id']}': a {ret['opening_um']:g} um gap does not retain a "
                                    f"{ret['retains_um']:g} um particle ({GAP_FACTOR_HIGH:g} x diameter = "
                                    f"{GAP_FACTOR_HIGH * ret['retains_um']:g} um)")
        self.for_every_chip(one)


class FabricationFloors(PerChip):
    """contracts/blocks.json fabrication_floors: min_wall_mm, min_port_mm and
    min_enclosed_printed_channel_mm are read by no other code and by no other test."""

    def clearance(self, chip):
        """(messages, tightest clearance) from the repo's own planar detector."""
        return mf.geometry_warnings(chip)

    def test_no_rib_is_thinner_than_the_minimum_wall(self):
        def one(chip):
            _, tightest = self.clearance(chip)
            gap, what = tightest
            if gap == float("inf"):
                return
            self.assertGreaterEqual(gap, FLOORS["min_wall_mm"] - TOL,
                                    f"{what} are {gap:.2f} mm apart, under the {FLOORS['min_wall_mm']:g} mm minimum wall")
        self.for_every_chip(one)

    def test_nothing_collides_in_the_one_layer(self):
        # cad/make_figures.py is the only planar-collision detector in the repo and the CI
        # job did not run it. This asserts the same thing from the suite.
        def one(chip):
            msgs, _ = self.clearance(chip)
            self.assertEqual([m for m in msgs if m.startswith("ERROR")], [])
        self.for_every_chip(one)

    def test_every_declared_port_width_is_at_least_the_minimum_port(self):
        def one(chip):
            for p in chip.get("ports", []):
                if p.get("width_um") is not None:
                    self.assertGreaterEqual(p["width_um"], FLOORS["min_port_mm"] * 1000 - TOL,
                                            f"port '{p['id']}' is {p['width_um']:g} um; we drill "
                                            f"{FLOORS['min_port_mm']:g} mm holes")
        self.for_every_chip(one)

    def test_every_port_pair_is_at_least_the_minimum_pitch_apart(self):
        # check_chip_spec.py only warns about this, and it exits 0 on warnings.
        def one(chip):
            placed = [p for p in chip.get("ports", []) if len(p.get("position") or []) >= 2]
            for i, a in enumerate(placed):
                for b in placed[i + 1:]:
                    dist = math.dist(a["position"][:2], b["position"][:2])
                    self.assertGreaterEqual(dist, FLOORS["min_port_pitch_mm"] - TOL,
                                            f"ports '{a['id']}' and '{b['id']}' are {dist:.2f} mm apart, under the "
                                            f"{FLOORS['min_port_pitch_mm']:g} mm pitch the connector needs")
        self.for_every_chip(one)

    def test_sub_floor_channels_are_printed_open_and_sealed_with_a_lid(self):
        """min_enclosed_printed_channel_mm is 0.5 mm: below it the contract says print OPEN
        and seal with film, so a chip whose channels are narrower MUST have a lid layer over
        the etched one. Nothing checked that the route the floor assumes is the route we use."""
        floor = FLOORS["min_enclosed_printed_channel_mm"]

        def one(chip):
            layers = chip.get("layers") or []
            narrow = [c["id"] for c in chip.get("channels", []) if c["width_mm"] < floor - TOL]
            if not narrow:
                return
            depth_mm = etch_depth_um(chip) / 1000
            etched = next((i for i, la in enumerate(layers) if abs(la.get("thickness_mm", 0) - depth_mm) < 1e-9), None)
            self.assertIsNotNone(etched, f"{len(narrow)} channels are under the {floor:g} mm enclosed-print floor but no "
                                         f"layer is the {depth_mm:g} mm etched one, so there is no open-and-seal route")
            self.assertGreater(len(layers) - 1, etched,
                               f"{len(narrow)} channels are under the {floor:g} mm enclosed-print floor "
                               f"({', '.join(narrow[:3])}), so they must be printed open and sealed, but "
                               f"'{layers[etched]['id']}' is the top layer: nothing seals them")
        self.for_every_chip(one)


class ProtocolVolumes(PerChip):
    """A LOAD fills a chamber. An exchange is held to the rule for the job it DECLARES.

    `exchange_kind` (contracts/schemas/protocol.schema.json) carries what used to live only
    in English inside `note`, where no rule could read it: a routine culture half-change and
    a wash that failed to clear the chamber are both "half a chamber volume" to a checker
    that cannot tell them apart. The field is optional and ABSENCE MEANS SWEEP - the strict
    case - so an unclassified exchange is still held to the floor.
    """

    def steps_with_volume(self, chip, proto, operation):
        chambers = {c["id"]: c for c in chip.get("chambers", [])}
        for s in proto.get("steps", []):
            if s.get("operation") != operation or s.get("volume_ul") is None:
                continue
            for t in commands.step_targets(s):
                c = chambers.get(t)
                if c and c.get("volume_ul"):
                    yield s, t, float(s["volume_ul"]), float(c["volume_ul"])

    def exchanges(self, chip, proto, kind):
        """Exchange steps of one declared kind, as (step, target, volume_ul, chamber volume)."""
        return [row for row in self.steps_with_volume(chip, proto, "exchange") if exchange_kind(row[0]) == kind]

    def test_no_load_overfills_its_chamber(self):
        def one(chip, proto):
            bad = [f"step {s['step']} loads {v:g} uL into '{t}' ({vc:g} uL): {v / vc:.2f} chamber volumes"
                   for s, t, v, vc in self.steps_with_volume(chip, proto, "load") if v > vc * (1 + SWEEP_TOL)]
            self.assertEqual(bad, [], f"{len(bad)} LOAD step(s) push more liquid in than the chamber holds")
        self.for_every_protocol(one)

    def test_every_sweep_moves_at_least_one_chamber_volume(self):
        # Below one chamber volume a sweep cannot clear the chamber however many times it is
        # repeated, so this floor applies to sweeps and to undeclared exchanges. A partial is
        # a deliberate fractional change and a fill is not clearing anything, so neither is
        # held to it - nothing is exempted by id or by protocol name.
        def one(chip, proto):
            bad = [f"step {s['step']} sweeps '{t}' with {v:g} uL of its {vc:g} uL ({v / vc:.2f} chamber volumes); "
                   f"exchange_kind is {s.get('exchange_kind', 'absent')!r} "
                   f"({'absent, which means sweep' if 'exchange_kind' not in s else 'sweep'})"
                   for s, t, v, vc in self.exchanges(chip, proto, "sweep") if v < vc * (1 - SWEEP_TOL)]
            self.assertEqual(bad[:6], [], f"{len(bad)} sweep step(s) are under one chamber volume")
        self.for_every_protocol(one)

    def test_every_sweep_reaches_the_exchange_target(self):
        # washes x volume must be at least -ln(1 - exchange.target_fraction) chamber volumes.
        # A backflush is excluded because its goal is a different contract value
        # (backflush.target_fraction: lifting the capsule bed off the weir, not clearing the
        # liquid); the one-chamber-volume floor above still applies to it.
        def one(chip, proto):
            bad = []
            for s, t, v, vc in self.exchanges(chip, proto, "sweep"):
                if s.get("direction") == "backflush":
                    continue
                total = v * int(s.get("washes") or 1)
                if total < SWEEP_VOLUMES * vc * (1 - SWEEP_TOL):
                    bad.append(f"step {s['step']} sweeps '{t}' with {s.get('washes') or 1} x {v:g} = {total:g} uL "
                               f"({total / vc:.2f} chamber volumes), short of the {SWEEP_VOLUMES:.2f} "
                               f"({SWEEP_VOLUMES * vc:.2f} uL) that reaches {EXCHANGE_TARGET:.0%}")
            self.assertEqual(bad[:6], [], f"{len(bad)} sweep step(s) cannot reach exchange.target_fraction")
        self.for_every_protocol(one)

    def test_no_fill_exceeds_the_chamber_volume(self):
        # A fill puts the reagent in, so it is bounded ABOVE by the chamber volume and has no
        # lower bound: it is not trying to get the old liquid out.
        def one(chip, proto):
            bad = [f"step {s['step']} fills '{t}' with {v:g} uL of its {vc:g} uL ({v / vc:.2f} chamber volumes)"
                   for s, t, v, vc in self.exchanges(chip, proto, "fill") if v > vc * (1 + SWEEP_TOL)]
            self.assertEqual(bad[:6], [], f"{len(bad)} fill step(s) put in more than the chamber holds")
        self.for_every_protocol(one)

    def test_every_partial_states_the_fraction_it_replaces(self):
        # A partial escapes the sweep floor BECAUSE it is deliberate, so it has to say what
        # it replaces; otherwise "partial" becomes the loophole the floor was there to close.
        def one(chip, proto):
            bad = [f"step {s['step']} ('{t}', {v:g} of {vc:g} uL = {v / vc:.2f} chamber volumes) is partial but its "
                   f"note names no fraction"
                   for s, t, v, vc in self.exchanges(chip, proto, "partial")
                   if not FRACTION_IN_NOTE.search(s.get("note") or "")]
            self.assertEqual(bad[:6], [], f"{len(bad)} partial step(s) do not say what fraction they replace")
        self.for_every_protocol(one)

    def test_exchange_kind_is_declared_only_on_exchange_steps(self):
        def one(chip, proto):
            for s in proto.get("steps", []):
                if "exchange_kind" in s:
                    self.assertEqual(s["operation"], "exchange", f"step {s['step']}")
                    self.assertIn(s["exchange_kind"], EXCHANGE_KINDS, f"step {s['step']}")
        self.for_every_protocol(one)

    def test_the_schema_rejects_exchange_kind_on_a_step_that_is_not_an_exchange(self):
        """The data assertion above only holds while nobody writes it; this is the schema
        constraint itself, so a hold_temp labelled "sweep" cannot be committed."""
        path, proto = next((p, pr) for chip in SPECS.values() for p, pr in pair_protocols(chip)
                           if any(s["operation"] != "exchange" for s in pr["steps"]))
        proto = copy.deepcopy(proto)
        victim = next(s for s in proto["steps"] if s["operation"] != "exchange")
        for kind in sorted(EXCHANGE_KINDS):
            victim["exchange_kind"] = kind
            self.assertTrue(schema_io.schema_errors("protocol", proto),
                            f"{path.name} step {victim['step']} is a {victim['operation']}, not an exchange, "
                            f"but the schema accepts exchange_kind={kind!r} on it")

    def test_every_protocol_fits_the_reservoirs_and_the_waste_bottle(self):
        # reservoir.default_ul and waste.capacity_ul are contract numbers the operator
        # enforces at run time (operator/safety.py rules 3 and 4), so a protocol that does
        # not fit them stops mid-run.
        def one(chip, proto):
            used: dict[str, float] = {}
            total = 0.0
            for s in proto.get("steps", []):
                if s.get("volume_ul") is None or s["operation"] not in ("load", "exchange", "release", "mix"):
                    continue
                vol = float(s["volume_ul"]) * int(s.get("washes") or 1)
                total += vol
                reagent = s.get("reagent") or ("capsule_suspension" if s["operation"] == "load" else None)
                if reagent:
                    used[reagent] = used.get(reagent, 0.0) + vol
            over = [f"'{r}' needs {v:.0f} uL of a {RESERVOIR_UL:g} uL reservoir" for r, v in sorted(used.items())
                    if v > RESERVOIR_UL]
            self.assertEqual(over, [])
            self.assertLessEqual(total, WASTE_UL, f"the protocol pushes {total:.0f} uL through a {WASTE_UL:g} uL waste bottle")
        self.for_every_protocol(one)


class ShearAndEscapePressure(PerChip):
    """The right ceiling for what the chamber HOLDS, all three read from contracts/parameters.json.

      adherent cells        shear.culture_ceiling_dyn_cm2 (detaching them is the risk)
      rinse before a lyse   shear.prelysis_rinse_ceiling_dyn_cm2 (the cells are about to die;
                            holding this rinse to the culture ceiling takes ~12 min and
                            perturbs the measurement far more than it protects it)
      capsules              no shear ceiling at all: a capsule is attached to nothing, and the
                            bound is capsule.escape_pressure_kpa across its retention
    """

    def test_the_three_ceilings_are_distinct_contract_values(self):
        self.assertLess(CULTURE_CEILING, RINSE_CEILING)
        self.assertGreater(ESCAPE_KPA, 0)
        self.assertGreater(BACKFLUSH_TARGET, 0)

    def test_cells_stay_under_the_ceiling_that_applies_to_their_step(self):
        def one(chip, proto):
            steps = proto.get("steps", [])
            lyse, cultured = first_lyse_step(steps), cultured_chambers(steps)
            chambers = {c["id"]: c for c in chip.get("chambers", [])}
            bad = []
            for s in steps:
                q = s.get("flow_ul_min")
                if not q or s["operation"] not in ("load", "exchange", "culture"):
                    continue
                name = str(s.get("reagent") or "").lower()
                for t in commands.step_targets(s):
                    c = chambers.get(t)
                    # after the lyse step the cells are dead on purpose: no ceiling applies
                    if not c or t not in cultured or (t in lyse and s["step"] >= lyse[t]):
                        continue
                    rinsing = any(w in name for w in RINSE_WORDS) and t in lyse
                    ceiling = RINSE_CEILING if rinsing else CULTURE_CEILING
                    tau = wall_shear_dyn_cm2(q, widest_section_mm(c), c["size"][2])
                    if tau > ceiling * (1 + 1e-9):
                        bad.append(f"step {s['step']} runs '{t}' at {q:g} uL/min: {tau:.4f} dyn/cm2 over the "
                                   f"{ceiling:g} ceiling")
            self.assertEqual(bad[:6], [], f"{len(bad)} step(s) over the shear ceiling for living cells")
        self.for_every_protocol(one)

    def test_capsules_behind_a_retention_stay_under_the_escape_pressure(self):
        def one(chip, proto):
            steps = proto.get("steps", [])
            cultured = cultured_chambers(steps)
            chambers = {c["id"]: c for c in chip.get("chambers", [])}
            model = ProcessModel(ModelParams.from_chip(chip, PARAMS))
            bad = []
            for s in steps:
                q = s.get("flow_ul_min")
                if not q or s["operation"] not in ("load", "exchange") or s.get("direction") == "backflush":
                    continue
                for t in commands.step_targets(s):
                    c = chambers.get(t)
                    if not c or t in cultured or not (c.get("retention") or c.get("mesh_pore_um")):
                        continue
                    state = ChipState.from_chip_spec(chip, PARAMS)
                    state.chambers[t].capsules_on_mesh = model.params.monolayer_capacity[t]   # a full bed
                    pressure = model.mesh_pressure(state, t, q)
                    if pressure >= ESCAPE_KPA:
                        bad.append(f"step {s['step']} at {q:g} uL/min puts {pressure:.2f} kPa across '{t}' "
                                   f"(escape {ESCAPE_KPA:g} kPa)")
            self.assertEqual(bad[:6], [], f"{len(bad)} step(s) would push capsules through the retention")
        self.for_every_protocol(one)


if __name__ == "__main__":
    unittest.main()

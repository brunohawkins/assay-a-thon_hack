"""The rules an audit of the hand-written protocols in this repo turned up, as code.

Each one of these was a real defect found by reading `protocols/*.json` against
`chip/*.json`, so each one is checked on every protocol S8 writes - and the same checker
runs on a hand-written protocol, which is the only honest way to claim the generator is
better than the file it replaces.

  1 TARGETS AND NUMBERING   every target is a chamber or port on the chip, and steps are
                            numbered 1, 2, 3 ... with no gaps or repeats.
  2 A LOAD FILLS            a load puts in at most the chamber's volume_ul.
  3 EXCHANGE KIND           every exchange declares what it is for, and meets that rule:
                            a sweep reaches exchange.target_fraction, a partial states its
                            fraction, a fill is bounded above by the chamber. A step with
                            no exchange_kind is held to the sweep rule, so absence is
                            reported rather than excused.
  4 THE RIGHT CEILING       the flow respects the ceiling scoped to what the chamber
                            holds, computed on the chamber's REAL cross-section.
  5 DURATION WITH MARGIN    duration_s >= volume x washes / flow, with margin left over.
  6 PORT STATE              every flow step says which ports are fed, vented and capped,
                            each port appears once, and every port on the chip is accounted
                            for - an unlisted port is an undefined state.
  7 THERMAL ZONE            every step with a temperature names a declared thermal zone
                            that can hold it.
  8 GENERIC NAMES           no concentrations, vendors, enzymes, compositions or sequences.
  9 DETERMINISTIC           no timestamps: the same inputs give the same bytes.

`problems()` returns one line per defect. Empty list means the protocol is sound.
"""

from __future__ import annotations

import re

from assay2chip.lib import contracts

from . import exchange, flows, reagents
from .geometry import Chip, GeometryError

# Operations that move liquid, and therefore need a port state and a flow.
FLOW_OPERATIONS = ("load", "exchange", "release", "collect", "harvest", "transfer", "mix",
                   "encapsulate", "culture", "split", "pool")

# How much slack over the bare transfer time counts as a stated margin. A duration equal
# to volume x washes / flow assumes the pump starts instantly and the line is already
# primed, which no real step does.
MIN_MARGIN = 1.05

TIMESTAMP = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|\b\d{4}-\d{2}-\d{2}T")


def infer_classes(chip: Chip) -> dict:
    """A shear class per chamber, from the chip spec alone.

    Only used when the caller has no `unitops.json` to hand (auditing somebody else's
    protocol). It is deliberately the STRICTEST reading the spec supports: a chamber that
    retains something is capsule handling, a culture zone holds adherent cells, and
    anything else is unbounded by shear.
    """
    out = {}
    for chamber_id, chamber in chip.chambers.items():
        zone = chip.thermal_zone(chamber_id)
        if chamber.get("retention") and chip.spec.get("capsule"):
            out[chamber_id] = "capsules"
        elif chamber.get("type") == "culture" or zone.get("purpose") == "culture":
            out[chamber_id] = "adherent_cells"
        else:
            out[chamber_id] = "none"
    return out


def problems(protocol: dict, chip_spec: dict, classes: dict = None) -> list:
    """Every rule this protocol breaks on this chip, as readable lines."""
    chip = Chip(chip_spec)
    classes = classes or infer_classes(chip)
    out = []
    steps = protocol.get("steps") or []

    for index, step in enumerate(steps, start=1):
        where = f"step {step.get('step', index)}"
        if step.get("step") != index:
            out.append(f"{where}: steps must be numbered 1, 2, 3 ... in order, but position "
                       f"{index} is numbered {step.get('step')!r}")
        targets = [t for t in [step.get("target")] + list(step.get("targets") or []) if t]
        named = targets + ([step["source"]] if step.get("source") else [])
        for target in named:
            if target not in chip.place_ids:
                out.append(f"{where}: target '{target}' is not a chamber or port on chip "
                           f"{chip.id}")
        chambers = [t for t in targets if t in chip.chambers]
        operation = step.get("operation")

        if operation == "load":
            for target in chambers:
                volume = step.get("volume_ul")
                if volume is not None and volume > chip.volume_ul(target) + 1e-6:
                    out.append(f"{where}: loads {volume:g} uL into '{target}', which holds "
                               f"{chip.volume_ul(target):g} uL")

        if operation == "exchange":
            if "exchange_kind" not in step:
                out.append(f"{where}: declares no exchange_kind, so it is held to the sweep "
                           f"rule (schema: absence means sweep) - say what it is for")
            for target in chambers:
                out += exchange.problems(step, chip.volume_ul(target))

        if operation in FLOW_OPERATIONS:
            out += _port_problems(step, chip, where)
            out += _flow_problems(step, chip, chambers, classes, where)
            out += _duration_problems(step, where)

        if step.get("temp_c") is not None:
            for target in chambers:
                reason = chip.zone_holds(target, float(step["temp_c"]))
                if reason:
                    out.append(f"{where}: {reason}")
                else:
                    zone = chip.thermal_zone(target)
                    if zone.get("id") and zone["id"] not in str(step.get("note", "")):
                        out.append(f"{where}: holds '{target}' at {step['temp_c']:g} C but its "
                                   f"note never names the thermal zone '{zone['id']}' that "
                                   f"does it")

    out += reagents.screen(protocol)
    for field, value in list(protocol.items()) + [
            (f"step {s.get('step')} note", s.get("note")) for s in steps]:
        if isinstance(value, str) and TIMESTAMP.search(value):
            out.append(f"{field}: holds a timestamp, so two runs of the generator would "
                       f"differ for no reason")
    return out


def _port_problems(step: dict, chip: Chip, where: str) -> list:
    ports = step.get("ports")
    if not ports:
        return [f"{where}: moves liquid but says nothing about ports: which one is driven, "
                f"which is the drain and which are capped is then undefined"]
    out = []
    lists = {name: list(ports.get(name) or []) for name in ("feed", "vent", "cap")}
    for name, ids in lists.items():
        for port_id in ids:
            if port_id not in chip.ports:
                out.append(f"{where}: ports.{name} names '{port_id}', which is not a port on "
                           f"chip {chip.id}")
    seen = [p for ids in lists.values() for p in ids]
    for port_id in sorted({p for p in seen if seen.count(p) > 1}):
        out.append(f"{where}: port '{port_id}' is in more than one of feed, vent and cap")
    if not lists["feed"]:
        out.append(f"{where}: no port is driven, so nothing moves")
    if not lists["vent"] and step.get("operation") != "encapsulate":
        out.append(f"{where}: no port is vented, so there is nowhere for the liquid to go")
    missing = [p for p in chip.port_ids() if p not in seen]
    if missing:
        out.append(f"{where}: ports {', '.join(missing)} are in none of feed, vent or cap, so "
                   f"their state is undefined (only a capped port is a true block)")
    return out


def _flow_problems(step: dict, chip: Chip, chambers: list, classes: dict, where: str) -> list:
    flow = step.get("flow_ul_min")
    if flow is None:
        if step.get("operation") in ("load", "exchange"):
            return [f"{where}: moves liquid with no flow_ul_min"]
        return []
    out = []
    mu = contracts.viscosity_for(step.get("temp_c"))
    for target in chambers:
        shear_class = classes.get(target, "none")
        ceiling = contracts.shear_ceiling_for(shear_class)
        if ceiling.ceiling_dyn_cm2 is None:
            continue
        try:
            section = chip.cross_section(target)
        except GeometryError as e:
            out.append(f"{where}: {e}")
            continue
        shear = flows.wall_shear_dyn_cm2(flow, section.width_mm, section.height_mm, mu.pa_s)
        if shear > ceiling.ceiling_dyn_cm2 * (1 + 1e-9):
            out.append(f"{where}: {flow:g} uL/min through '{target}' is {shear:.4f} dyn/cm2 on "
                       f"its real cross-section ({section.why}), over the "
                       f"{ceiling.ceiling_dyn_cm2:g} dyn/cm2 ceiling for {shear_class} "
                       f"({flows.ceiling_key(ceiling)})")
    return out


def _duration_problems(step: dict, where: str) -> list:
    volume, washes = step.get("volume_ul"), step.get("washes")
    flow, duration = step.get("flow_ul_min"), step.get("duration_s")
    if not volume or not flow or duration is None:
        return []
    repeats = max(1, len(step.get("targets") or []))
    needed = volume * (washes or 1) * repeats / flow * 60.0
    if duration + 1e-9 < needed:
        return [f"{where}: {washes or 1} x {volume:g} uL at {flow:g} uL/min needs {needed:.0f} s, "
                f"but duration_s is {duration:g}"]
    if duration < needed * MIN_MARGIN:
        return [f"{where}: duration_s {duration:g} is the bare transfer time ({needed:.0f} s) "
                f"with no margin for priming, dead volume or settling"]
    return []


def summary(protocol: dict, chip_spec: dict, classes: dict = None) -> str:
    """A short report, for --audit and for the stage log."""
    found = problems(protocol, chip_spec, classes)
    head = (f"{protocol.get('id')} on {chip_spec.get('id')}: {len(protocol.get('steps') or [])} "
            f"steps, {len(found)} problem(s)")
    return "\n".join([head] + [f"  - {line}" for line in found])

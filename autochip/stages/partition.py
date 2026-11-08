"""Stage 3. Protocol IR -> chip plan (contracts/schemas/chip_plan.schema.json).

run(ir, constraints, parts, agent, out_dir, feedback=None) -> chip plan
  Agent path: prompts/partition.md + the IR, the constraints, the usable parts and the schema. With
  `feedback` (constraint errors from the layout stage for the previous plan, read from
  out_dir/chip_plan.json) the agent is asked to re-split. The rules backend gets rules_plan().
rules_plan(ir, constraints, parts) -> chip plan from the deterministic grouping rules below.
validate(plan, ir, constraints, parts) -> [readable problems] (schema + meaning).
design_files(plan, ir) -> {chip_id: design dict} in the chip/designs format (chip/designs/README.md).

Grouping rules (Yuvraj's, chip/designs/README.md, extended):
  1 One module per place the material sits: steps on the same material in the same place share a chamber.
  2 A reagent joining a flowing stream: merge part, then mixer.
  3 Centrifuge washes: flush over a trap (capsules, beads) or a spiral (loose cells).
  4 Timed waits in flow: delay line sized by volume / flow.
  5 Split-and-pool: split part, one chamber per portion, pool part.
  6 Imaging / QC / steps needing equipment the lab lacks: off chip if manual steps are allowed, else
    on chip if a part exists, else missing_parts.
Chip boundaries: material leaves the chip for an off-chip step; oil would reach a culture chip;
temperature regimes clash; two pieces of equipment the chip must sit on (magnet vs thermocycler);
cells -> capsules -> library hand-offs (not with prefer=fewest_chips); port limit; footprint limit.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path

from autochip import constraints as C
from autochip import parts as P
from autochip.llm import schema_errors

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "contracts" / "schemas" / "chip_plan.schema.json"
TEAM_PARAMETERS = ROOT / "contracts" / "parameters.json"
FILTER_NOZZLE_RATIO = 0.75   # a cell filter's gap must stay below the encapsulation nozzle, or clumps block it
PROMPT = ROOT / "autochip" / "prompts" / "partition.md"

QC_ACTIONS = {"count", "image", "measure"}
# Bench operations the grouping rules turn into chip operations (centrifuge wash -> flush / spiral, strainer -> filter),
# so an off_chip hint from extract does not keep them off the chip.
CHIP_REPLACES = {"centrifuge", "wash_cells", "wash_particles", "resuspend", "filter"}
HOLD_ACTIONS = {"incubate", "break_emulsion", "cure", "lyse", "reverse_transcribe", "release", "elute", "fragment",
                "ligate", "add_reagent", "mix", "resuspend", "quench", "wash_cells", "wash_particles", "feed",
                "dissociate", "other", "bind_beads", "magnetic_separate", "purify", "centrifuge"}
MAGNET_ACTIONS = {"bind_beads", "magnetic_separate", "purify"}
PARTICLES = {"capsules", "droplets", "beads", "gel"}
MATERIAL_CLASS = {"cells": "cells", "capsules": "capsules", "droplets": "capsules", "gel": "capsules",
                  "nucleic_acid": "library", "library": "library", "beads": "library"}
SEATS = {"thermocycler", "peltier_stage", "magnet"}
NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "ten": 10, "twelve": 12}

# Part preference per job: parametric first (sized to the protocol), library stand-ins after.
PREFER = {
    "culture": ["culture_chamber_param", "chamber", "culture_chamber"],
    "hold": ["chamber", "culture_chamber"],
    "trap": ["trap_chamber", "culture_chamber"],
    "merge": ["merge", "t_junction", "y_junction", "y_junction_curved"],
    "mixer": ["mixer", "serpentine_channel", "snake_channel"],
    "mixer_viscous": ["mixer", "snake_channel", "serpentine_channel"],
    "cell_wash": ["spiral_separator", "spiral_channel", "trap_chamber", "culture_chamber"],
    "filter": ["pillar_filter"],
    "droplet": ["droplet_junction", "cross_junction"],
    "delay": ["delay_line", "serpentine_channel", "snake_channel", "spiral_channel"],
    "split": ["splitter", "flow_manifold", "flow_distributor", "fork_junction"],
    "pool": ["pool", "ring_junction"],
    "thermal": ["thermal_chamber", "culture_chamber"],
    "magnetic": ["magnetic_chamber", "culture_chamber"],
    "qc": ["optical_window"],
}
MODULE_NAME = {"culture": "culture", "hold": "chamber", "trap": "trap", "merge": "join", "mixer": "mix",
               "cell_wash": "cell_wash", "filter": "filter", "droplet": "droplets", "delay": "delay",
               "split": "split", "pool": "pool", "thermal": "pcr", "magnetic": "beads", "qc": "qc_window"}
MINOR_LABEL = {"merge": "mixing", "mixer": "mixing", "filter": "filter", "delay": "incubation", "pool": "pool", "qc": "qc"}


def _module_labels(m: dict, by_n: dict) -> list[str]:
    """What a module does, for naming its chip. [] for plumbing (merge, mixer, filter, delay, pool)."""
    job = m["_job"]
    acts = {by_n[n]["action"] for n in m["steps"] if n in by_n}
    fixed = {"culture": "cell_culture", "cell_wash": "cell_wash", "droplet": "encapsulation", "split": "split",
             "magnetic": "bead_cleanup", "thermal": "index_pcr"}
    if job in fixed:
        return [fixed[job]]
    if "amplify" in acts:
        return ["index_pcr"]
    if job in ("trap", "hold"):
        out = [lab for lab, hit in (("capsule_curing", acts & {"break_emulsion", "cure"}),
                                    ("lysis_rt", acts & {"lyse", "reverse_transcribe"}),
                                    ("library_prep", acts & {"fragment", "ligate"}),
                                    ("release", acts & {"release"})) if hit]
        if out:
            return out
        return ["wash"] if acts <= {"wash_cells", "wash_particles", "centrifuge", "resuspend", "collect", "transfer"} else ["processing"]
    return []


def _chip_label(mods: list[dict], by_n: dict) -> list[str]:
    if not any(m["steps"] for m in mods):
        return ["bench_only"]
    labels = [lab for m in mods for lab in _module_labels(m, by_n)]
    if not labels:
        labels = [MINOR_LABEL.get(m["_job"], m["_job"]) for m in mods]
    labels = list(dict.fromkeys(labels))
    if ("index_pcr" in labels or "cell_culture" in labels) and "split" in labels:
        labels.remove("split")   # the split feeds the PCR portions; 'index_pcr' says it
    return labels


def _chip_name(labels: list[str]) -> str:
    words = {"pcr": "PCR", "rt": "RT", "etc": "etc."}
    text = " + ".join(" ".join(words.get(w, w) for w in lab.split("_")) for lab in labels) + " chip"
    return text[0].upper() + text[1:]


def _section(ref: str | None) -> str | None:
    """'2.1 Cell preparation, sentence 3' -> 'cell preparation'; None for refs without a heading."""
    if not ref or "," not in ref:
        return None
    head = re.sub(r"^[\d.]+\s*", "", ref.split(",")[0].strip()).lower()
    if not head or re.match(r"^(step|page|p\.|table|row|sentence|line|prompt|fig)", head):
        return None
    return head


# ---------------------------------------------------------------------------------------- helpers
def _num(text: str) -> int | None:
    m = re.search(r"\b(\d+)\s*(?:equal\s+)?(?:portions|sublibraries|parts|branches|aliquots|wells|chambers)", text)
    if m:
        return int(m.group(1))
    for w, n in NUMBER_WORDS.items():
        if re.search(rf"\b{w}\b", text):
            return n
    return None


def _minutes(text: str) -> float | None:
    """'15-30 min' -> 1800 s (the longer end), '5 min' -> 300 s."""
    m = re.search(r"(\d+)\s*(?:-|–|to)?\s*(\d+)?\s*min", text)
    return 60.0 * max(int(m.group(1)), int(m.group(2) or 0)) if m else None


def _um(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:µm|μm|um|micron)", text)
    return float(m.group(1)) if m else None


def _regime(step: dict) -> str:
    cond = step.get("conditions") or {}
    t = step.get("temp_c")
    if cond.get("thermal_cycles") or step.get("action") == "amplify":
        return "cycle"
    if cond.get("cold") or (t is not None and t <= 10):
        return "cold"
    if t is not None and t >= 50:
        return "hot"
    if (t is not None and t >= 30) or step.get("action") in ("culture", "seed", "feed"):
        return "warm"
    return "ambient"


def _needs(step: dict) -> list[list[str]]:
    """Equipment the chip needs for this step on chip, as alternatives: [[a, b], [c]] = (a or b) and c."""
    cond = step.get("conditions") or {}
    out = []
    r = _regime(step)
    if r == "cycle":
        out.append(["thermocycler", "peltier_stage"])
    elif r == "cold":
        out.append(["cold_plate", "peltier_stage", "thermocycler"])
    elif r in ("warm", "hot"):
        out.append(["incubator_37c", "heater", "peltier_stage", "thermocycler"] if step.get("action") in ("culture", "seed", "feed")
                   else ["heater", "peltier_stage", "thermocycler", "incubator_37c"])
    if cond.get("magnet") or step.get("action") in ("magnetic_separate",):
        out.append(["magnet"])
    if cond.get("light_nm") or step.get("action") == "cure":
        nm = cond.get("light_nm") or 405
        out.append(["led_405nm", "uv_lamp"] if nm >= 380 else ["uv_lamp", "led_405nm"])
    return out


def _team_nozzle_um() -> float:
    """The team's encapsulation nozzle (contracts/parameters.json), which the chip checker also assumes."""
    try:
        return float(json.loads(TEAM_PARAMETERS.read_text())["parameters"]["encapsulation.nozzle_um"]["value"])
    except (OSError, KeyError, TypeError, ValueError):
        return 40.0


def _per_lane_distinct(step: dict) -> bool:
    """The step gives each parallel portion a different reagent (index PCR, barcodes)."""
    text = (step.get("text", "") + " " + (step.get("note") or "")).lower()
    return bool(re.search(r"\bdifferent\b|\bindex|\bbarcod|\bunique\b|\bdistinct\b", text))


def _material(step: dict) -> str | None:
    return step.get("input_material") or step.get("output_material")


def _reagent_info(ir: dict) -> dict:
    return {r["id"]: r for r in ir.get("reagents", [])}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_") or "x"


def _schema() -> dict:
    return json.loads(SCHEMA.read_text())


# ------------------------------------------------------------------------------------ rules plan
class _Planner:
    def __init__(self, ir: dict, c: dict, parts: dict):
        self.ir, self.c, self.parts = ir, c, parts
        self.av = P.available(parts)
        self.equip = set(C.get(c, "off_chip.equipment") or [])
        self.manual = bool(C.get(c, "off_chip.manual_steps_allowed", True))
        self.prefer = C.get(c, "objectives.prefer", "most_on_chip")
        notes = " ".join((c.get("notes") or []) + (C.get(c, "objectives.notes") or [])).lower()
        explicit = C.get(c, "fabrication.thermal_zoning_allowed")
        self.zoning = bool(explicit) if explicit is not None else ("thermal zon" in notes or "temperature zon" in notes)
        self.reagents = _reagent_info(ir)
        self.sizes = {m["kind"]: m.get("size_um") for m in ir.get("materials", []) if m.get("size_um")}
        self.counter: dict[str, int] = {}
        self.encapsulates = any(s["action"] in ("encapsulate", "emulsify") for s in ir["steps"])
        self.nozzle_um = self._nozzle()
        self.off: list[dict] = []        # {"step", "why", "equipment", "moves"}
        self.missing: list[dict] = []    # {"step", "need", "why", "workaround"}
        self.notes: list[str] = []       # plan-level findings for the rationale

    # -- parts
    def pick(self, job: str) -> str | None:
        for name in PREFER[job]:
            if name in self.av:
                return name
        return None

    def new_module(self, job: str, part: str, steps: list[int], does: str, **kw) -> dict:
        base = MODULE_NAME.get(job, job)
        self.counter[base] = self.counter.get(base, 0) + 1
        m = {"id": f"{base}_{self.counter[base]}", "part": part, "part_source": self.parts[part]["source"],
             "steps": list(steps), "inlets": [], "outlets": [], "does": does, "_job": job}
        m.update(kw)
        return m

    def add_inlets(self, m: dict, step: dict) -> None:
        for r in step.get("reagents", []) or []:
            name = _slug(r)
            if name not in m["inlets"]:
                m["inlets"].append(name)

    # -- where a step goes
    def equipment_gap(self, step: dict) -> list[str]:
        return [alts[0] for alts in _needs(step) if not self.equip & set(alts)]

    def send_off(self, step: dict, why: str, moves: bool) -> None:
        if self.manual:
            self.off.append({"step": step["n"], "why": why, "equipment": list(step.get("equipment") or []), "moves": moves})
        else:
            self.missing.append({"step": step["n"], "need": why.split(":")[0], "why": why + " (manual bench steps are not allowed)",
                                 "workaround": "allow manual steps, or add the equipment / part"})

    @staticmethod
    def _bead_step(s: dict) -> bool:
        return ("beads" in (s.get("input_material"), s.get("output_material")) or s["action"] in MAGNET_ACTIONS | {"elute"}
                or bool((s.get("conditions") or {}).get("magnet")))

    def populations(self, steps: list[dict], i: int) -> list[str] | None:
        """Populations cultured apart and later connected (co-culture, shared / conditioned medium), from the run of
        culture steps starting at steps[i]: ['neurons', 'microglia', 'astrocytes'], or None."""
        run = []
        for st in steps[i:]:
            if st["action"] not in ("culture", "seed", "feed"):
                break
            run.append(st)
        text = " ".join(st.get("text", "") + " " + (st.get("note") or "") for st in run).lower()
        if not re.search(r"\bseparate\b|\bapart\b|\beach in its own\b|\bindividual", text):
            return None
        if not re.search(r"\bconnect|\bshar(?:e|ed|es|ing)\b|\bconditioned\b|\bco-?cultur|\bside by side\b|\btogether\b", text):
            return None
        m = (re.search(r"\(([a-z][a-z-]+(?:,\s*(?:and\s+)?[a-z][a-z-]+)+)\)", text)           # (a, b, c)
             or re.search(r"\b([a-z][a-z-]+(?:, [a-z][a-z-]+)+,? and [a-z][a-z-]+)\b", text))  # a, b and c
        names = [n.strip() for n in re.split(r",\s*(?:and\s+)?|\s+and\s+", m.group(1)) if n.strip()] if m else []
        if len(names) < 2:
            n = _num(text)
            names = [f"population_{k + 1}" for k in range(n)] if n and 2 <= n <= 8 else []
        return names[:8] if len(names) >= 2 else None

    def add_pop_inlets(self, m: dict, step: dict) -> None:
        for r in step.get("reagents", []) or []:
            name = f"{_slug(r)}_{m['_pop']}"[:48]
            if _slug(r) not in m["_reagents"]:
                m["_reagents"].append(_slug(r))
            if name not in m["inlets"]:
                m["inlets"].append(name)

    def coculture_block(self, pops: list[str], s: dict, mat: str | None) -> list[dict] | None:
        """Team Module 1 pattern: one culture chamber per population, each with its own medium line, all joined at a
        central junction. Apart: each chamber is fed from its own line and vents through the junction. Sharing: one
        is fed and the others vent (their own outlet), so medium crosses between them; switched only by which ports
        are fed, vented or capped. The split part on inlet_1 stays capped while they grow."""
        sp, po, ch = self.pick("split"), self.pick("pool"), self.pick("culture")
        if not (sp and po and ch):
            return None
        self._pops = pops
        n = len(pops)
        grp = [self.new_module("split", sp, [], f"Feeds inlet_1 to the {n} culture chambers (capped while they grow apart; "
                                                "each population is seeded through its own line).",
                               params={"n_out": n} if self.parts[sp]["source"] == "parametric" else {})]
        for k, pop in enumerate(pops):
            slug = _slug(pop)[:20]
            m = self.new_module("culture", ch, [s["n"]], f"Grows the {pop} in their own chamber, fed from their own line"
                                + (" and vented through the central junction." if k == 0 else f"; vents through {slug}_vent_out when the medium is shared."),
                                holds=mat or "cells", params=self.vol_params(ch, s, 100), _cocult=True, _pop=slug, _reagents=[])
            m["id"] = f"culture_{slug}"
            m["outlets"] = [] if k == 0 else [f"{slug}_vent"]
            self.add_pop_inlets(m, s)
            grp.append(m)
        grp.append(self.new_module("pool", po, [], f"Central junction joining the {n} chambers: apart, each vents through it; "
                                                   "sharing, medium fed into one chamber crosses here into the others. After "
                                                   "detaching, the cells of all chambers leave through it.",
                                   params={"n_in": n} if self.parts[po]["source"] == "parametric" else {}))
        grp[-1]["id"] = "junction_mid"
        return grp

    def _nozzle(self) -> float:
        """Nozzle the droplet junction gets: the team value, but never below what the process can make."""
        floor = ((self.parts.get("droplet_junction") or {}).get("params", {}).get("nozzle_um", {}) or {}).get("min") or 0
        return max(_team_nozzle_um(), floor)

    def is_oil(self, r: str) -> bool:
        return bool(self.reagents.get(r, {}).get("immiscible")) or "oil" in r

    def size_um(self, kind: str | None) -> float:
        return self.sizes.get(kind) or {"capsules": 70, "droplets": 70, "gel": 70, "cells": 12, "beads": 1}.get(kind or "", 20)

    # -- pass 1: steps -> modules (blocks of modules that stay together)
    def modules(self) -> list[list[dict]]:
        steps = sorted(self.ir["steps"], key=lambda s: s["n"])
        blocks: list[list[dict]] = []
        held: dict | None = None          # module the material sits in
        split_group: list[dict] | None = None
        cocult: list[dict] | None = None      # split -> one culture chamber per population -> central junction
        last_flow_mod: dict | None = None
        last_flow = 10.0
        gaps = [s["n"] for s in steps if self.equipment_gap(s)]
        bench = {}
        for a_, b_ in zip(gaps, gaps[1:]):
            if b_ - a_ <= 4:
                for n in range(a_ + 1, b_):
                    bench[n] = f"done at the bench between steps {a_} and {b_}, while the material is off chip anyway"
        # A bead clean-up is one unit: if any of its steps lacks equipment (the magnet), the whole run of bead
        # steps is done at the bench like a kit, instead of beads going on and off the chip.
        run: list[dict] = []
        for s in steps + [None]:
            if s is not None and self._bead_step(s):
                run.append(s)
                continue
            missing = [x for x in run if self.equipment_gap(x)]
            if missing:
                need = ", ".join(sorted({e for x in missing for e in self.equipment_gap(x)}))
                for x in run:
                    if x["n"] not in gaps:
                        bench.setdefault(x["n"], f"bead clean-up done at the bench as a kit (step {missing[0]['n']} needs {need})")
            run = []
        if all(s["action"] in QC_ACTIONS | {"collect", "transfer", "store"} or s["n"] in bench or s["n"] in gaps for s in steps):
            # nothing would be left on a chip: keep the steps before the first one that lacks equipment on chip
            bench = {n: why for n, why in bench.items() if not (gaps and n < gaps[0])}
        for i, s in enumerate(steps):
            a = s["action"]
            mat = _material(s)
            if s["n"] in bench and a not in QC_ACTIONS:
                self.send_off(s, bench[s["n"]], moves=True)
                held = None
                continue
            text = s.get("text", "") + " " + (s.get("note") or "")
            cond = s.get("conditions") or {}
            if a == "cure" and not cond.get("light_nm") and (cond.get("cold") or re.search(r"\bgel", text.lower())):
                a = "incubate"   # gelation: a timed wait, not a light cure
            if s.get("flow_ul_min"):
                last_flow = float(s["flow_ul_min"])
            gap = self.equipment_gap(s)
            # QC, storage, explicit off-chip
            if a in QC_ACTIONS:
                part = self.pick("qc")
                if self.manual or not part:
                    self.send_off(s, "imaging / QC is done on the bench" if self.manual else "QC step: no optical window part", moves=False)
                    continue
                if held:
                    held["steps"].append(s["n"]); held["does"] += " Imaged through a clear lid for QC."
                else:
                    m = self.new_module("qc", part, [s["n"]], "Clear window to image / count on the microscope.")
                    blocks.append([m])
                continue
            if a == "store" or (s.get("on_chip_hint") == "off_chip" and self.manual and a not in CHIP_REPLACES):
                self.send_off(s, "kept at the bench (the source does this step off chip)", moves=held is None)
                continue
            if gap:
                self.send_off(s, f"needs {', '.join(gap)}, which the lab does not have here", moves=True)
                held = None if a not in ("incubate",) else held
                split_group = None
                continue
            if a in ("collect", "transfer"):
                target = held or last_flow_mod or (blocks[-1][-1] if blocks else None)
                prev_off = [o for o in self.off if o["moves"] and o["step"] == s["n"] - 1]
                if prev_off:
                    self.send_off(s, prev_off[0]["why"], moves=True)
                elif target:
                    target["steps"].append(s["n"])
                    if held is None:
                        target["does"] += f" The product leaves through the outlet (step {s['n']})."
                else:
                    self.send_off(s, "hand-off at the bench", moves=True)
                continue
            # split-and-pool
            if cocult is not None:
                if a in ("culture", "seed", "feed", "wash_cells", "incubate", "add_reagent", "dissociate", "other"):
                    for m in cocult[1:-1]:
                        m["steps"].append(s["n"])
                        self.add_pop_inlets(m, s)
                    if a == "dissociate":
                        cocult[-1]["steps"].append(s["n"])
                        cocult = None
                    continue
                cocult = None
            if split_group is not None:
                per_portion = a == "amplify" or re.search(r"\beach\b", text.lower())
                if a == "pool":
                    split_group[-1]["steps"].append(s["n"]); split_group = None; held = None
                    continue
                if per_portion:
                    distinct = _per_lane_distinct(s)
                    for k, m in enumerate(split_group[1:-1], 1):
                        m["steps"].append(s["n"])
                        if distinct and s.get("reagents"):
                            # each portion gets a different reagent (index / barcode): its own named line
                            m["inlets"].append(f"index_mix_{k}")
                            m["does"] += (f" Gets its own index mix through index_mix_{k}_in "
                                          f"({', '.join(s['reagents'])} with this lane's index), so every portion is indexed differently.")
                        else:
                            self.add_inlets(m, s)
                        if a == "amplify":
                            m["_job"] = "thermal"
                    continue
                split_group = None
            if a == "split":
                n = _num(text) or 4
                sp = self.pick("split")
                po = self.pick("pool")
                nxt = steps[i + 1] if i + 1 < len(steps) else {}
                job = "thermal" if nxt.get("action") == "amplify" else ("trap" if mat in PARTICLES else "hold")
                ch = self.pick(job)
                if not (sp and po and ch):
                    self.missing.append({"step": s["n"], "need": f"split into {n} branches + {n} chambers + pool",
                                         "why": "no split/pool/chamber part usable here", "workaround": "split by pipette"})
                    continue
                grp = [self.new_module("split", sp, [s["n"]], f"Splits the stream into {n} equal portions.",
                                       params={"n_out": n} if self.parts[sp]["source"] == "parametric" else {})]
                for k in range(n):
                    params = {"volume_ul": s.get("volume_ul") or 25} if self.parts[ch]["source"] == "parametric" else {}
                    grp.append(self.new_module(job, ch, [], f"Portion {k + 1} of {n}.", params=params, holds=mat))
                grp.append(self.new_module("pool", po, [], f"Gathers the {n} portions back to one stream (they carry different indexes, so they can be pooled; or collect each chamber on its own).",
                                           params={"n_in": n} if self.parts[po]["source"] == "parametric" else {}))
                blocks.append(grp)
                split_group = grp
                held = None
                last_flow_mod = grp[-1]
                continue

            # culture / feed in a chamber
            if a in ("culture", "seed", "feed"):
                if held and held["_job"] == "culture":
                    held["steps"].append(s["n"]); self.add_inlets(held, s)
                    continue
                pops = self.populations(steps, i)
                grp = self.coculture_block(pops, s, mat) if pops else None
                if grp:
                    blocks.append(grp); cocult = grp; held = None; last_flow_mod = grp[-1]
                    continue
                part = self.pick("culture")
                m = self.new_module("culture", part, [s["n"]], "Grows the cells in place; medium flows over them.",
                                    holds=mat or "cells", outlets=[], params=self.vol_params(part, s, 100))
                self.add_inlets(m, s)
                blocks.append([m]); held = m; last_flow_mod = m
                continue

            # filter in flow
            if a == "filter":
                g = _um(text) or (100 if mat in PARTICLES else 40)
                purpose = "capsule_aggregates" if mat in PARTICLES else "cells"
                capped = ""
                if purpose == "cells" and self.encapsulates:
                    # clumps that pass the filter must not block the encapsulation nozzle (checked by the team checker
                    # against its nozzle value, so use the smaller of the two)
                    cap = math.floor(FILTER_NOZZLE_RATIO * min(self.nozzle_um, _team_nozzle_um()))
                    if g > cap:
                        capped = (f" The source's {g:g} um strainer is capped at {cap:g} um "
                                  f"({FILTER_NOZZLE_RATIO:g} x the {min(self.nozzle_um, _team_nozzle_um()):g} um encapsulation nozzle) "
                                  "so clumps that pass it can't block the nozzle.")
                        g = float(cap)
                part = self.pick("filter")
                floor = (self.parts.get("pillar_filter", {}).get("params", {}).get("gap_um", {}) or {}).get("min")
                if not part or (floor and g < floor):
                    why = (self.parts["pillar_filter"]["why_unavailable"] if part is None and "pillar_filter" in self.parts
                           else f"{g:g} um gap is below the smallest gap the process makes ({floor:g} um)")
                    self.missing.append({"step": s["n"], "need": f"pillar filter, ~{g:g} um gap", "why": (why or "no filter part") + capped,
                                         "workaround": "strain off chip before loading (cell strainer at or below this size), "
                                                       "or use a bought mesh in a trap"})
                    continue
                m = self.new_module("filter", part, [s["n"]], f"Pillar filter, {g:g} um gaps: removes clumps / aggregates in flow." + capped,
                                    params={"gap_um": g, "purpose": purpose}, holds=None)
                blocks.append([m]); held = None; last_flow_mod = m
                continue

            # droplets / capsules
            if a in ("encapsulate", "emulsify"):
                part = self.pick("droplet")
                if not part:
                    self.missing.append({"step": s["n"], "need": "flow-focusing droplet junction", "why": "no droplet part usable",
                                         "workaround": "make droplets on a separate commercial chip"})
                    continue
                d = _um(text) or self.size_um(s.get("output_material"))
                params = {"nozzle_um": self.nozzle_um} if self.parts[part]["source"] == "parametric" else {}
                # The cell / core stream arrives on the main line. The junction's own lines: the shell (if the
                # protocol has one) and the oil. Never fold the oil into a shared line. A core reagent that was
                # not merged upstream joins the stream through a merge + mixer just before the junction.
                upstream = {r for blk in blocks for mm in blk for r in mm["inlets"]}
                rs = [r for r in dict.fromkeys(_slug(r) for r in (s.get("reagents") or [])) if r not in upstream]
                oils = [r for r in rs if self.is_oil(r)]
                aqueous = [r for r in rs if r not in oils]
                shell = next((r for r in aqueous if "shell" in r or "shell" in str(self.reagents.get(r, {}).get("role") or "")), None)
                if shell is None and len(aqueous) > 1:
                    shell = aqueous[-1]
                extra = [r for r in aqueous if r != shell]
                oil = oils[0] if oils else "oil"
                blk = []
                if extra:
                    mp, mx = self.pick("merge"), self.pick("mixer_viscous")
                    if mp and mx:
                        j = self.new_module("merge", mp, [s["n"]], f"{', '.join(extra)} joins the cell stream before the junction (centre stream).",
                                            params={"shape": "t"} if self.parts[mp]["source"] == "parametric" else {})
                        j["inlets"] = extra[:1]
                        blk += [j, self.new_module("mixer", mx, [s["n"]], "Mixes the cells into the core solution before the junction.",
                                                   params={"length_mm": 40, "viscous": True} if self.parts[mx]["source"] == "parametric" else {})]
                does = (f"Flow-focusing junction: the cell stream arrives on the main line (centre)"
                        + (f", {shell} comes in through {shell}_in" if shell else "")
                        + f" and {oil} through {oil}_in pinches off ~{d:g} um droplets"
                        + (f" at a {self.nozzle_um:g} um nozzle." if params else "."))
                m = self.new_module("droplet", part, [s["n"]], does, params=params, holds=None, _fixed=True)
                m["inlets"] = ([shell] if shell else []) + [oil]
                blk.append(m)
                blocks.append(blk); held = None; last_flow_mod = m
                continue

            # centrifuge / wash of loose cells in flow
            if a in ("centrifuge", "wash_cells") and held is None and mat not in PARTICLES:
                if last_flow_mod and last_flow_mod["_job"] == "cell_wash" and blocks and blocks[-1][-1] is last_flow_mod:
                    last_flow_mod["steps"].append(s["n"]); self.add_inlets(last_flow_mod, s)
                    continue
                part = self.pick("cell_wash")
                m = self.new_module("cell_wash", part, [s["n"]],
                                    "Moves the loose cells into clean buffer (replaces the centrifuge pellet and resuspension).",
                                    outlets=["waste"], holds=None)
                if self.parts[part]["role"] == "chamber":
                    m["mesh_pore_um"] = 5
                    m["holds"] = mat
                self.add_inlets(m, s)
                blocks.append([m]); held = m if m["holds"] else None; last_flow_mod = m
                continue

            # reagent into a flowing stream
            if a in ("quench", "add_reagent", "mix", "resuspend") and held is None:
                viscous = any(self.reagents.get(r, {}).get("viscous") for r in s.get("reagents", []) or [])
                mixer = self.pick("mixer_viscous" if viscous else "mixer")
                mixp = {"length_mm": 40, "viscous": True} if viscous else {"length_mm": 30}
                blk = []
                if s.get("reagents"):
                    mp = self.pick("merge")
                    if not mp or not mixer:
                        self.missing.append({"step": s["n"], "need": "merge + mixer", "why": "no merge/mixer part usable", "workaround": "mix off chip"})
                        continue
                    j = self.new_module("merge", mp, [s["n"]], f"{', '.join(s['reagents'])} joins the stream.",
                                        params={"shape": "t"} if self.parts[mp]["source"] == "parametric" else {})
                    self.add_inlets(j, s)
                    blk.append(j)
                blk.append(self.new_module("mixer", mixer, [s["n"]], "Folded channel mixes the stream" + (" (wide gentle turns for the viscous liquid)." if viscous else "."),
                                           params=mixp if self.parts[mixer]["source"] == "parametric" else {}))
                blocks.append(blk); last_flow_mod = blk[-1]
                continue

            # timed wait in flow
            if a == "incubate" and held is None:
                dur = s.get("duration_s") or _minutes(text) or 600
                flow = s.get("flow_ul_min") or last_flow
                need_ul = dur * flow / 60
                part = self.delay_part(need_ul)
                if not part:
                    self.missing.append({"step": s["n"], "need": f"delay line ~{need_ul:.0f} uL", "why": "no delay part usable", "workaround": "collect and wait off chip"})
                    continue
                params = {"residence_s": dur, "flow_ul_min": flow} if self.parts[part]["source"] == "parametric" else {}
                vol = need_ul if params else self.parts[part]["volume_ul"]
                m = self.new_module("delay", part, [s["n"]],
                                    f"Delay line: {vol:.0f} uL at {flow:g} uL/min gives {vol / flow:.0f} min in flow (needs {dur / 60:.0f} min)"
                                    + (" over the cold plate." if _regime(s) == "cold" else "."), params=params, holds=None)
                blocks.append([m]); last_flow_mod = m
                continue

            # everything that happens to material sitting in one place
            if held is not None and held["_job"] not in ("cell_wash",) and not (a in MAGNET_ACTIONS and held["_job"] != "magnetic"):
                held["steps"].append(s["n"]); self.add_inlets(held, s)
                if a == "dissociate":
                    held["does"] += " Then the cells are detached in the same chamber and leave in the flow."
                    held = None
                elif a == "release":
                    held["does"] += " The product is released and leaves in the flow."
                    held = None
                continue
            job = "magnetic" if (a in MAGNET_ACTIONS or (s.get("conditions") or {}).get("magnet")) else (
                "trap" if mat in PARTICLES else "hold")
            part = self.pick(job)
            if not part:
                self.missing.append({"step": s["n"], "need": f"{job} chamber", "why": "no chamber part usable", "workaround": "do it off chip"})
                continue
            does = {"magnetic": "Chamber over the magnet: beads are held while the liquid is exchanged around them.",
                    "trap": "Trap chamber: the particles stay on the mesh while liquids are flushed through.",
                    "hold": "Chamber where the liquid is held and reagents exchanged."}[job]
            m = self.new_module(job, part, [s["n"]], does, holds=mat, params=self.vol_params(part, s, 50))
            if job == "trap":
                pore = max(5, round(0.35 * self.size_um(mat)))
                m["mesh_pore_um"] = pore
                if self.parts[part]["source"] == "parametric":
                    m["params"].update({"retention": "mesh", "gap_um": pore})
            if job in ("trap", "magnetic", "hold"):
                m["outlets"] = ["waste"]
            self.add_inlets(m, s)
            blocks.append([m]); last_flow_mod = m
            held = m
            if a in ("dissociate", "release"):
                held = None
        max_ports = C.get(self.c, "ports.max_per_chip")
        by_n = {s["n"]: s for s in steps}
        for bi, blk in enumerate(list(blocks)):
            chambers = [m for m in blk if m.get("_cocult")]
            ports = 2 + sum(len(m["inlets"]) + len(m["outlets"]) for m in blk)
            if chambers and max_ports and ports > max_ports:
                compact = 2 + sum(len(m["inlets"]) + len(m["outlets"]) for m in blk if not m.get("_cocult")) + len(chambers)
                if compact <= max_ports:
                    for m in chambers:
                        later = m["inlets"][1:]
                        m["inlets"], m["outlets"] = m["inlets"][:1], []
                        m["does"] = (f"Grows the {m['_pop'].replace('_', ' ')} in their own chamber, fed from {m['inlets'][0]}_in. "
                                     "To fit the port limit there are no vent lines: when the medium is shared, the chambers "
                                     "not being fed vent through their own (unfed) medium port"
                                     + (f"; {', '.join(r.rsplit('_' + m['_pop'], 1)[0] for r in later)} comes in through the "
                                        "same line (swap the syringe)." if later else "."))
                    self.notes.append(f"co-culture of {', '.join(self._pops)} fitted into {compact} ports (limit {max_ports}) by "
                                      "venting through the unfed medium ports and sending later reagents down the medium lines")
                    continue
                one = chambers[0]
                one.update(id="culture_all", _cocult=False, outlets=["waste"],
                           steps=sorted({n for m in blk for n in m["steps"]}),
                           inlets=list(dict.fromkeys(r for m in chambers for r in m["_reagents"])))
                one["does"] = (f"Grows the {', '.join(self._pops)} in one chamber: one chamber per population around a "
                               f"central junction would need {compact} ports even without vent lines, more than the {max_ports} allowed.")
                self.notes.append(f"OPEN PROBLEM: the protocol cultures {', '.join(self._pops)} apart first, but one chamber per "
                                  f"population needs {compact} ports (limit {max_ports}), so they share one chamber here and are "
                                  "NOT kept apart")
                self.missing.append({"step": min(n for n in one["steps"]), "need": f"co-culture chip: {len(chambers)} culture chambers "
                                     f"around a central junction ({compact} ports)",
                                     "why": f"ports.max_per_chip is {max_ports}", "workaround": "grow the populations apart off chip "
                                     "or on separate chips, and combine them before dissociation", "_shadow": True})
                blocks[blocks.index(blk)] = [one]
        for blk in list(blocks):
            ports = 2 + sum(len(m["inlets"]) + len(m["outlets"]) for m in blk)
            if blk[0]["_job"] == "split" and max_ports and ports > max_ports:
                # Each portion needs its own reagent line and that won't fit one chip: split at the bench.
                blocks.remove(blk)
                for n in sorted({n for m in blk for n in m["steps"]}):
                    self.send_off(by_n[n], f"split-and-pool needs {ports} ports here (limit {max_ports}): split by pipette", moves=True)
                self.off.sort(key=lambda o: o["step"])
                self.missing.sort(key=lambda o: o["step"])
        for blk in blocks:
            for m in blk:
                if m["_job"] == "culture" and not m.get("_cocult"):
                    m["outlets"] = ["waste"]
                # Too many reagent lines for the part (or for a whole chip): later water-based reagents share
                # one line, swapped at the syringe between steps (no on-chip valves needed). Oil-phase
                # reagents always keep their own line.
                if m.get("_fixed"):
                    continue

                def room_for(mod):
                    r = self.parts[mod["part"]]["openings"] - 2 - len(mod["outlets"])
                    if max_ports:
                        r = min(r, max_ports - 2 - len(mod["outlets"]))
                    return max(r, 1)
                room = room_for(m)
                if len(m["inlets"]) > room:
                    oils = [r for r in m["inlets"] if self.is_oil(r)]
                    water = [r for r in m["inlets"] if r not in oils]
                    if len(oils) + 1 > room and m["outlets"]:
                        m["outlets"] = []
                        m["does"] += " Waste leaves through the main outlet (no port left for a waste line)."
                        room = room_for(m)
                    if len(oils) + 1 > room:
                        water, oils = m["inlets"], []
                        m["does"] += " Ports are short, so the oil-phase reagent shares the line too: flush it with buffer afterwards."
                    keep = max(room - 1 - len(oils), 0)
                    shared = water[keep:]
                    m["inlets"] = [r for r in m["inlets"] if r in oils or r in water[:keep]] + ["reagent_line"]
                    m["does"] += (f" {', '.join(shared)} come in one after another through reagent_line_in "
                                  "(swap the syringe between steps).")
        return blocks

    def vol_params(self, part: str, step: dict, default: float) -> dict:
        if self.parts[part]["source"] != "parametric":
            return {}
        return {"volume_ul": step.get("volume_ul") or default}

    def delay_part(self, need_ul: float) -> str | None:
        if "delay_line" in self.av:
            return "delay_line"
        lib = [p for p in ("serpentine_channel", "snake_channel", "spiral_channel") if p in self.av]
        if not lib:
            return None
        fit = [p for p in lib if (self.parts[p]["volume_ul"] or 0) >= need_ul]
        return min(fit, key=lambda p: self.parts[p]["volume_ul"]) if fit else max(lib, key=lambda p: self.parts[p]["volume_ul"])

    # -- pass 2: modules -> chips
    def block_info(self, blk: list[dict]) -> dict:
        by_n = {s["n"]: s for s in self.ir["steps"]}
        steps = [by_n[n] for m in blk for n in m["steps"] if n in by_n]
        regimes = {_regime(s) for s in steps} - {"ambient"}
        seats = set()
        for s in steps:
            for alts in _needs(s):
                if set(alts) & SEATS and alts[0] in SEATS:
                    have = [a for a in alts if a in self.equip]
                    if have and have[0] in SEATS:
                        seats.add(have[0])
        oil = any(self.reagents.get(r, {}).get("immiscible") or "oil" in r for s in steps for r in (s.get("reagents") or [])) or \
            any(s["action"] in ("encapsulate", "emulsify", "break_emulsion") for s in steps) or \
            any(m["_job"] == "delay" and steps and _material(steps[0]) == "droplets" for m in blk)
        culture = any(s["action"] in ("culture", "seed", "feed") for s in steps)
        first = min((s["n"] for s in steps), default=None)
        cls = None
        if steps:
            s0 = min(steps, key=lambda s: s["n"])
            mat = s0.get("output_material") if any(m["_job"] == "droplet" for m in blk) else s0.get("input_material")
            cls = MATERIAL_CLASS.get(mat or "")
        ports = sum(len(m.get("inlets", [])) + len(m.get("outlets", [])) for m in blk)
        area = 0.0
        for m in blk:
            fx, fy, _ = P.footprint_mm(self.parts[m["part"]], m.get("params"), self.c)
            area += fx * fy
        section = _section(min(steps, key=lambda s: s["n"]).get("source_ref")) if steps else None
        last_section = _section(max(steps, key=lambda s: s["n"]).get("source_ref")) if steps else None
        return {"regimes": regimes, "seats": seats, "oil": oil, "culture": culture, "first": first, "cls": cls, "section": section, "last_section": last_section,
                "ports": ports, "area": area, "steps": {s["n"] for s in steps}}

    def chips(self, blocks: list[list[dict]], soft: bool = True) -> list[dict]:
        max_ports = C.get(self.c, "ports.max_per_chip")
        x, y = C.max_chip_mm(self.c)
        margin = C.get(self.c, "envelope.edge_margin_mm") or 0
        pitch = C.get(self.c, "ports.min_pitch_mm") or 3
        cap_area = (x - 2 * margin) * (y - 2 * margin) if x and y else None
        moved = sorted(o["step"] for o in self.off if o["moves"])
        chips: list[dict] = []
        cur = None
        for blk in blocks:
            info = self.block_info(blk)
            why = None
            if cur is not None:
                last = max(cur["steps"]) if cur["steps"] else 0
                via = [n for n in moved if last < n < (info["first"] or 0)]
                regimes = cur["regimes"] | info["regimes"]
                clash = len(regimes) > 1 and not self.zoning and not (
                    "cycle" in regimes and not cur["culture"] and not info["culture"])
                ports = 2 + cur["ports"] + info["ports"]
                area = (cur["area"] + info["area"]) * 1.6 + ports * pitch * pitch
                if self.prefer == "one_chip_per_module":
                    why = "one chip per module (objectives.prefer)"
                elif via:
                    why = f"the material leaves the chip for off-chip step(s) {', '.join(map(str, via))}"
                elif (info["oil"] and cur["culture"]) or (info["culture"] and cur["oil"]):
                    why = "keeps the oil phase off the culture chip"
                elif clash:
                    why = f"temperature regimes clash ({' vs '.join(sorted(regimes))}) and there is no thermal zoning"
                elif len(cur["seats"] | info["seats"]) > 1:
                    why = f"the chip would have to sit on both {' and '.join(sorted(cur['seats'] | info['seats']))}"
                elif soft and self.prefer != "fewest_chips" and info["section"] and cur["section"] and info["section"] != cur["section"]:
                    why = f"natural hand-off: the protocol moves from '{cur['section']}' to '{info['section']}'"
                elif soft and self.prefer != "fewest_chips" and info["cls"] and cur["cls"] and info["cls"] != cur["cls"]:
                    why = f"natural hand-off: {cur['cls']} -> {info['cls']}"
                elif max_ports and ports > max_ports:
                    why = f"would need {ports} ports; the limit is {max_ports} per chip"
                elif cap_area and area > cap_area:
                    why = f"estimated footprint would exceed {x} x {y} mm"
            if cur is None or why:
                cur = {"modules": [], "steps": set(), "regimes": set(), "seats": set(), "oil": False, "culture": False,
                       "cls": None, "section": None, "ports": 0, "area": 0.0, "why": why}
                chips.append(cur)
            cur["modules"] += blk
            cur["steps"] |= info["steps"]
            cur["regimes"] |= info["regimes"]
            cur["seats"] |= info["seats"]
            cur["oil"] |= info["oil"]
            cur["culture"] |= info["culture"]
            cur["cls"] = info["cls"] or cur["cls"]
            cur["section"] = info["last_section"] or cur["section"]
            cur["ports"] += info["ports"]
            cur["area"] += info["area"]
        return chips


def rules_plan(ir: dict, constraints: dict, parts: dict) -> dict:
    """Deterministic chip plan from the grouping rules (used by the rules backend and as the agent's draft)."""
    pl = _Planner(ir, constraints, parts)
    blocks = pl.modules()
    if not blocks:
        part = pl.pick("hold") or pl.pick("trap") or next(iter(pl.av), None)
        if part is None:
            raise ValueError(f"no usable parts at all under constraints {constraints['id']}")
        blocks = [[pl.new_module("hold", part, [], "Placeholder: no step of this protocol can run on a chip under these "
                                 "constraints (see off_chip and missing_parts).")]]
    groups = pl.chips(blocks)
    max_chips = C.get(constraints, "envelope.max_chips")
    note = ""
    if max_chips and len(groups) > max_chips:
        groups = pl.chips(blocks, soft=False)
        if len(groups) > max_chips:
            note = f" Needs {len(groups)} chips but the constraints allow {max_chips}: the layout stage will flag it."
    by_n = {s["n"]: s for s in ir["steps"]}
    equip = set(C.get(constraints, "off_chip.equipment") or [])

    chips = []
    for i, g in enumerate(groups, 1):
        labels = _chip_label(g["modules"], by_n)
        if len("_".join(labels)) > 64:
            while len("_".join(labels + ["etc"])) > 64 and len(labels) > 1:
                labels = labels[:-1]
            labels = labels + ["etc"]
        label = "_".join(labels)
        cid = f"chip{i}_{label}"
        mods = []
        seen_in: dict[str, int] = {}
        seen_out: set[str] = set()
        for m in g["modules"]:
            m = {k: v for k, v in m.items() if not k.startswith("_")}
            ins = []
            for r in m.get("inlets", []):
                seen_in[r] = seen_in.get(r, 0) + 1
                ins.append(r if seen_in[r] == 1 else f"{r}_{seen_in[r]}")
            m["inlets"] = ins
            m["outlets"] = [f"{m['id']}_{o}" if o == "waste" else o for o in m.get("outlets", [])]
            for o in m["outlets"]:
                seen_out.add(o)
            if not m.get("params"):
                m.pop("params", None)
            mods.append(m)
        steps = sorted({n for m in mods for n in m["steps"]})
        temps = [by_n[n].get("temp_c") for n in steps if by_n[n].get("temp_c") is not None and _regime(by_n[n]) != "ambient"]
        eq = set()
        for n in steps:
            for alts in _needs(by_n[n]):
                have = [a for a in alts if a in equip]
                if have:
                    eq.add(have[0])
        pump = "syringe_pump" if "syringe_pump" in equip else ("pressure_controller" if "pressure_controller" in equip else None)
        if pump:
            eq.add(pump)
        first_mat = _material(by_n[steps[0]]) if steps else None
        chip = {"id": cid, "name": _chip_name(labels), "purpose": "; ".join(
            dict.fromkeys(m["does"].split(".")[0] for m in mods if m["steps"])) + ".",
            "steps": steps, "why_separate": g["why"] or "first chip in the flow",
            "temp_c": (max(temps, key=lambda t: abs(t - 22)) if temps and "cycle" not in g["regimes"] else None),
            "equipment": sorted(eq), "modules": mods,
            "inputs": [{"name": "inlet_1", "from": "external", "carries": first_mat or "sample"}] +
                      [{"name": f"{r}_in", "from": "external", "carries": r} for m in mods for r in m["inlets"]],
            "outputs": [{"name": "outlet_1", "to": "external"}] +
                       [{"name": f"{o}_out", "to": "waste"} for m in mods for o in m["outlets"]]}
        chips.append(chip)

    handoffs = []
    moved = sorted(o["step"] for o in pl.off if o["moves"])
    for a, b in zip(chips, chips[1:]):
        last, first = max(a["steps"] or [0]), min(b["steps"] or [0])
        via = [n for n in moved if last < n < first]
        carries = _material(by_n[first]) if first in by_n else "sample"
        handoffs.append({"from_chip": a["id"], "from_output": "outlet_1", "to_chip": b["id"], "to_input": "inlet_1",
                         "carries": carries or "sample", "method": "off_chip_step" if via else "tubing",
                         **({"via_steps": via} if via else {})})
        a["outputs"][0]["to"] = f"{b['id']}.inlet_1"
        b["inputs"][0]["from"] = f"{a['id']}.outlet_1"

    off_chip = []
    for o in pl.off:
        if off_chip and off_chip[-1]["why"] == o["why"] and off_chip[-1]["steps"][-1] == o["step"] - 1:
            off_chip[-1]["steps"].append(o["step"])
            off_chip[-1]["equipment"] = sorted(set(off_chip[-1]["equipment"]) | set(o["equipment"]))
        else:
            off_chip.append({"steps": [o["step"]], "why": o["why"], "equipment": sorted(set(o["equipment"]))})
    missing = [{"steps": [m["step"]], "need": m["need"], "why": m["why"], **({"workaround": m["workaround"]} if m.get("workaround") else {})}
               for m in pl.missing]

    coverage: dict[str, str] = {}
    for ch in chips:
        for m in ch["modules"]:
            for n in m["steps"]:
                # a step done by several modules (merge + mixer, core merge + junction) points at the one doing
                # its main job: the junction for encapsulation, else the first.
                main = by_n.get(n, {}).get("action") in ("encapsulate", "emulsify") and m["part"] in parts \
                    and parts[m["part"]]["role"] == "flow_focusing"
                if main or str(n) not in coverage:
                    coverage[str(n)] = f"{ch['id']}.{m['id']}"
    for o in off_chip:
        for n in o["steps"]:
            coverage[str(n)] = "off_chip"
    shadow = {m["step"] for m in pl.missing if m.get("_shadow")}   # on chip, but only partly: listed, coverage kept
    for m in missing:
        for n in m["steps"]:
            if n not in shadow:
                coverage[str(n)] = "missing"
    coverage = {k: coverage[k] for k in sorted(coverage, key=int)}

    reasons = [f"{c['id']}: {c['why_separate']}" for c in chips[1:]]
    plan = {"kind": "chip_plan", "id": _slug(f"{ir['id']}_{constraints['id']}")[:64], "name": f"{ir.get('name', ir['id'])} on {constraints['id']}",
            "protocol_id": ir["id"], "constraints_id": constraints["id"],
            "rationale": "Built by the rules fallback (no agent). " + (("New chip where " + "; ".join(reasons) + ".") if reasons else "Everything fits on one chip.") + note
                         + "".join(f" {n[0].upper() + n[1:]}." for n in pl.notes),
            "chips": chips, "handoffs": handoffs, "off_chip": off_chip, "missing_parts": missing, "coverage": coverage}
    return plan


# ------------------------------------------------------------------------------------- validate
def validate(plan: dict, ir: dict, constraints: dict, parts: dict) -> list[str]:
    errs = schema_errors(plan, _schema())
    if errs:
        return errs
    out = []
    step_ns = {s["n"] for s in ir["steps"]}
    by_n = {s["n"]: s for s in ir["steps"]}
    equip = set(C.get(constraints, "off_chip.equipment") or [])
    immiscible = {_slug(r["id"]) for r in ir.get("reagents", []) if r.get("immiscible")}
    if plan["protocol_id"] != ir["id"]:
        out.append(f"protocol_id must be '{ir['id']}'")
    if plan["constraints_id"] != constraints["id"]:
        out.append(f"constraints_id must be '{constraints['id']}'")
    max_chips = C.get(constraints, "envelope.max_chips")
    if max_chips and len(plan["chips"]) > max_chips:
        out.append(f"{len(plan['chips'])} chips but the constraints allow at most {max_chips} (envelope.max_chips)")
    max_ports = C.get(constraints, "ports.max_per_chip")
    chip_ids = [c["id"] for c in plan["chips"]]
    for dup in sorted({i for i in chip_ids if chip_ids.count(i) > 1}):
        out.append(f"chip id '{dup}' is used twice")
    where: dict[int, list[str]] = {}
    for ch in plan["chips"]:
        mids = [m["id"] for m in ch["modules"]]
        for dup in sorted({i for i in mids if mids.count(i) > 1}):
            out.append(f"chip {ch['id']}: module id '{dup}' is used twice")
        names = [n for m in ch["modules"] for n in m.get("inlets", [])]
        for dup in sorted({i for i in names if names.count(i) > 1}):
            out.append(f"chip {ch['id']}: inlet name '{dup}' is used by more than one module (inlet names must be unique per chip; use e.g. '{dup}_2')")
        onames = [n for m in ch["modules"] for n in m.get("outlets", [])]
        for dup in sorted({i for i in onames if onames.count(i) > 1}):
            out.append(f"chip {ch['id']}: outlet name '{dup}' is used by more than one module (outlet names must be unique per chip)")
        # parallel lanes that must each get a different reagent (index PCR, barcodes): one named line per lane
        lane_steps: dict[int, list[dict]] = {}
        for m in ch["modules"]:
            if parts.get(m["part"], {}).get("role") not in ("merge", "mixer", "split", "pool"):
                for n in m["steps"]:
                    lane_steps.setdefault(n, []).append(m)
        for n, lanes in sorted(lane_steps.items()):
            st = by_n.get(n)
            if len(lanes) < 2 or not st or not st.get("reagents") or not _per_lane_distinct(st):
                continue
            reagents = {_slug(r) for r in st["reagents"]}
            bare = [m["id"] for m in lanes if set(m.get("inlets", [])) & reagents]
            same = [m["id"] for m in lanes if any(re.sub(r"_\d+$", "", i) in reagents for i in m.get("inlets", []))]
            none = [m["id"] for m in lanes if not m.get("inlets")]
            if none or (bare and len(same) == len(lanes)):
                out.append(f"chip {ch['id']}: step {n} gives each portion a different reagent (index / barcode), but lanes "
                           f"{', '.join(none or same)} {'have no reagent line' if none else 'all get the same ' + ', '.join(sorted(reagents)) + ' line'}: "
                           f"give each lane its own named inlet, e.g. index_mix_1 ... index_mix_{len(lanes)}")
        n_ports = 2 + len(names) + len(onames)
        if max_ports and n_ports > max_ports:
            out.append(f"chip {ch['id']}: {n_ports} ports (inlet_1 + outlet_1 + {len(names)} inlets + {len(onames)} outlets) "
                       f"but the limit is {max_ports} per chip: move modules to another chip or share lines")
        for e in ch.get("equipment", []):
            if equip and e not in equip:
                out.append(f"chip {ch['id']}: equipment '{e}' is not in the lab's off_chip.equipment ({', '.join(sorted(equip))})")
        listed = set(ch["steps"])
        for m in ch["modules"]:
            label = f"chip {ch['id']} module {m['id']}"
            part = parts.get(m["part"])
            if part is None:
                out.append(f"{label}: no part '{m['part']}' (use a name from the parts list)")
            elif not part.get("available", True):
                out.append(f"{label}: part '{m['part']}' can't be used here: {part['why_unavailable']}")
            else:
                if m.get("part_source") and m["part_source"] != part["source"]:
                    out.append(f"{label}: part_source should be '{part['source']}' for '{m['part']}'")
                extra = len(m.get("inlets", [])) + len(m.get("outlets", []))
                if extra + 2 > part["openings"]:
                    out.append(f"{label}: '{m['part']}' takes {part['openings']} fluid connections but the module needs "
                               f"{extra + 2} (main in + out + {extra} extra lines); feed reagents through a merge part or a chamber instead")
                out += [f"{label}: {e}" for e in _param_errors(part, m.get("params") or {}, constraints)]
            enc = [by_n[n] for n in m["steps"] if n in by_n and by_n[n]["action"] in ("encapsulate", "emulsify")]
            if enc and part and part.get("role") == "flow_focusing":
                ins = m.get("inlets", [])
                rs = [_slug(r) for st in enc for r in (st.get("reagents") or [])]
                oil = [r for r in rs if r in immiscible or "oil" in r] or ["oil"]
                if not any(i in immiscible or "oil" in i for i in ins if i != "reagent_line"):
                    out.append(f"{label}: the encapsulation junction needs its own oil inlet named for the oil (e.g. '{oil[0]}'); "
                               f"it has {ins or 'none'}. Never put the oil on a shared line")
                shell = [r for r in rs if "shell" in r]
                before = [i for mm in ch["modules"][:ch["modules"].index(m) + 1] for i in mm.get("inlets", [])]
                if shell and shell[0] not in before:
                    out.append(f"{label}: the encapsulation junction needs its own shell inlet '{shell[0]}' (or a merge "
                               f"just before it on the same chip); the cell / core stream arrives on the main line")
            for n in m["steps"]:
                if n not in step_ns:
                    out.append(f"{label}: step {n} is not in the protocol")
                    continue
                where.setdefault(n, []).append(f"{ch['id']}.{m['id']}")
                if n not in listed:
                    out.append(f"chip {ch['id']}: step {n} is done by module {m['id']} but missing from the chip's steps list")
                for alts in _needs(by_n[n]):
                    if not equip & set(alts):
                        out.append(f"{label}: step {n} needs {' or '.join(alts)} on chip, which the lab does not have: put it off_chip or in missing_parts")
    for h in plan["handoffs"]:
        for k in ("from_chip", "to_chip"):
            if h[k] not in chip_ids:
                out.append(f"handoff {h['from_chip']} -> {h['to_chip']}: no chip '{h[k]}'")
        if h["from_chip"] == h["to_chip"]:
            out.append(f"handoff {h['from_chip']} -> {h['to_chip']}: a chip can't hand off to itself")
        for n in h.get("via_steps", []):
            if n not in step_ns:
                out.append(f"handoff {h['from_chip']} -> {h['to_chip']}: via step {n} is not in the protocol")
        if h["method"] == "off_chip_step" and not h.get("via_steps"):
            out.append(f"handoff {h['from_chip']} -> {h['to_chip']}: method off_chip_step needs via_steps")
    off = {n for o in plan["off_chip"] for n in o["steps"]}
    miss = {n for o in plan["missing_parts"] for n in o["steps"]}
    if off and not C.get(constraints, "off_chip.manual_steps_allowed", True):
        out.append(f"steps {sorted(off)} are off_chip but the constraints do not allow manual bench steps: put them on chip or in missing_parts")
    for n in sorted((off | miss) - step_ns):
        out.append(f"off_chip / missing_parts step {n} is not in the protocol")
    cov = plan["coverage"]
    for k, v in cov.items():
        if not k.isdigit() or int(k) not in step_ns:
            out.append(f"coverage: '{k}' is not a protocol step number")
            continue
        n = int(k)
        if v == "off_chip":
            if n not in off:
                out.append(f"coverage: step {n} says off_chip but no off_chip entry lists it")
        elif v == "missing":
            if n not in miss:
                out.append(f"coverage: step {n} says missing but no missing_parts entry lists it")
        elif v not in where.get(n, []):
            out.append(f"coverage: step {n} -> '{v}' but that module does not list step {n} "
                       f"(modules doing it: {', '.join(where.get(n, [])) or 'none'})")
    for n in sorted(step_ns):
        if str(n) not in cov:
            out.append(f"coverage: step {n} is not covered (give '<chip>.<module>', 'off_chip' or 'missing')")
        if n not in where and n not in off and n not in miss:
            out.append(f"step {n} is not done by any module, off_chip entry or missing_parts entry")
    return out


def _param_errors(part: dict, params: dict, c: dict) -> list[str]:
    if part["source"] == "library":
        bad = [k for k in params if k not in ("scale", "note")]
        return [f"library part '{part['name']}' takes no params ({', '.join(bad)}); only 'scale'"] if bad else []
    out = []
    spec = part.get("params", {})
    for k, v in params.items():
        if k not in spec:
            out.append(f"'{part['name']}' has no param '{k}' (params: {', '.join(spec)})")
            continue
        s = spec[k]
        if s.get("choices"):
            if v not in s["choices"]:
                out.append(f"param {k}='{v}' must be one of {s['choices']}")
            continue
        if isinstance(s.get("default"), bool) or k == "viscous":
            if not isinstance(v, bool):
                out.append(f"param {k} must be true or false")
            continue
        if isinstance(s.get("default"), str):
            continue
        if v is None:
            continue
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0:
            out.append(f"param {k}={v!r} must be a positive number ({s['unit']})")
            continue
        if s.get("min") is not None and v < s["min"]:
            out.append(f"param {k}={v:g} {s['unit']} is below what the process can make ({s['min']:g} {s['unit']}, fabrication.min_feature)")
        if k in ("n_out", "n_in") and (v != int(v) or v < 2 or v > 96):
            out.append(f"param {k} must be a whole number from 2 to 96")
        if k == "volume_ul" and v > 5000:
            out.append(f"param volume_ul={v:g} is too big for a chip chamber (max 5000 uL)")
        if k == "residence_s" and v > 6 * 3600:
            out.append(f"param residence_s={v:g} is over 6 h; hold the material in a chamber instead of a delay line")
    if part["name"] == "trap_chamber":
        ret = params.get("retention", spec["retention"]["default"])
        gap = params.get("gap_um", spec["gap_um"]["default"])
        floor = spec["gap_um"].get("min_printed")
        if ret in ("weir", "pillars") and floor and isinstance(gap, (int, float)) and gap < floor:
            out.append(f"{ret} gap {gap:g} um is below the smallest printed gap ({floor:g} um): use retention 'mesh' (a bought membrane) instead")
    if part["name"] == "delay_line":
        rs = params.get("residence_s", spec["residence_s"]["default"])
        fl = params.get("flow_ul_min", spec["flow_ul_min"]["default"])
        rng = C.get(c, "off_chip.flow_range_ul_min")
        if rng and isinstance(fl, (int, float)) and not (rng[0] <= fl <= rng[1]):
            out.append(f"flow_ul_min={fl:g} is outside the pumps' range {rng}")
        if isinstance(rs, (int, float)) and isinstance(fl, (int, float)) and rs * fl / 60 > 5000:
            out.append(f"delay line volume {rs * fl / 60:.0f} uL is too big for a chip: hold the material in a chamber instead")
    return out


# ------------------------------------------------------------------------------------ agent path
def _prompt(ir: dict, c: dict, parts: dict, draft: dict, feedback: list[str] | None, previous: dict | None) -> str:
    gone = P.why_unavailable(parts)
    lines = [
        "Split this protocol into chips and modules. Reply with one chip_plan JSON object.",
        "", "## Protocol IR", "```json", json.dumps(ir, indent=1), "```",
        "", "## Constraints", C.summary(c),
        f"- Manual bench steps: {'allowed' if C.get(c, 'off_chip.manual_steps_allowed', True) else 'NOT allowed'}",
        f"- Off-chip equipment the lab has (exact names): {', '.join(C.get(c, 'off_chip.equipment') or []) or 'none listed'}",
        "", "## Parts you may use (name [source, role -> chip type, openings])", P.parts_for_prompt(parts),
        "", "## Parts you may NOT use here, and why (say so in missing_parts if a step needs one)",
        "\n".join(f"- {k}: {v}" for k, v in sorted(gone.items())) or "- none",
        "", "## Output schema (chip_plan.schema.json)", "```json", json.dumps(_schema(), separators=(",", ":")), "```",
        "", "## Draft from the simple deterministic rules (valid, but naive; improve it, restructure freely)", "```json",
        json.dumps(draft, separators=(",", ":")), "```",
    ]
    if feedback:
        lines += ["", "## RE-SPLIT REQUEST",
                  "The layout stage built your previous plan and found these constraint errors. Re-split so they go away "
                  "(move modules to other chips, shrink parametric sizes, use fewer ports, send steps off chip or to missing_parts):",
                  "\n".join(f"- {f}" for f in feedback)]
        if previous:
            lines += ["", "Previous plan:", "```json", json.dumps(previous, separators=(",", ":")), "```"]
    lines += ["", f"Use protocol_id '{ir['id']}' and constraints_id '{c['id']}'. Every step 1..{max(s['n'] for s in ir['steps'])} "
              "must be in coverage. Reply with the JSON object only."]
    return "\n".join(lines)


def _normalise(plan: dict, parts: dict) -> dict:
    for ch in plan.get("chips", []):
        for m in ch.get("modules", []):
            if m.get("part") in parts and not m.get("part_source"):
                m["part_source"] = parts[m["part"]]["source"]
    plan["coverage"] = {str(k): v for k, v in sorted(plan.get("coverage", {}).items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0)}
    return plan


# ------------------------------------------------------------------------- rules re-split on feedback
SIZE_HINTS = ("larger than", "envelope.max_chip_mm", "mm wide, more than", "split it over two chips", "split its")
PORT_HINTS = ("ports, max", "ports.max_per_chip", "port count", "ports (inlet_1")


def _parse_feedback(feedback: list[str]) -> dict[str, list[str]]:
    """'chip <id>: <message>' lines -> {chip_id: [messages]}."""
    out: dict[str, list[str]] = {}
    for line in feedback:
        m = re.match(r"\s*chip\s+([a-z0-9_]+)\s*:\s*(.*)", line, re.S)
        if m:
            out.setdefault(m.group(1), []).append(m.group(2).strip())
    return out


def _job_of(m: dict, by_n: dict, parts: dict) -> str:
    """Planner job of a module in a finished plan (for naming chips after a re-split)."""
    part = parts.get(m["part"], {})
    role, name = part.get("role", ""), m["part"]
    acts = {by_n[n]["action"] for n in m["steps"] if n in by_n}
    if acts & {"culture", "seed", "feed"}:
        return "culture"
    if "amplify" in acts or name == "thermal_chamber":
        return "thermal"
    if acts & MAGNET_ACTIONS or name == "magnetic_chamber":
        return "magnetic"
    jobs = {"flow_focusing": "droplet", "spiral": "cell_wash", "split": "split", "pool": "pool", "merge": "merge",
            "mixer": "mixer", "delay": "delay", "filter": "filter", "qc": "qc"}
    if role in jobs:
        return jobs[role]
    return "trap" if m.get("holds") in PARTICLES else "hold"


def _units(mods: list[dict], parts: dict) -> list[list[int]]:
    """Module indices that must stay on one chip: modules sharing a step (merge + mixer, core merge + junction)
    and lane blocks (split part -> lanes -> pool part)."""
    units: list[list[int]] = []
    i = 0
    while i < len(mods):
        if parts.get(mods[i]["part"], {}).get("role") == "split":
            j = i
            while j + 1 < len(mods) and parts.get(mods[j]["part"], {}).get("role") != "pool":
                j += 1
            units.append(list(range(i, j + 1)))
            i = j + 1
            continue
        if units and set(mods[i]["steps"]) & {n for k in units[-1] for n in mods[k]["steps"]}:
            units[-1].append(i)
        else:
            units.append([i])
        i += 1
    return units


def _module_area(m: dict, parts: dict, c: dict) -> float:
    part = parts.get(m["part"])
    if not part:
        return 1.0
    fx, fy, _ = P.footprint_mm(part, m.get("params"), c)
    return fx * fy


def _best_cut(mods: list[dict], units: list[list[int]], by_n: dict, parts: dict, c: dict, ports_bad: bool) -> int | None:
    """Unit index k to cut before (left = units[:k]), or None if the chip is one unit."""
    if len(units) < 2:
        return None
    max_ports = C.get(c, "ports.max_per_chip")
    area = [sum(_module_area(mods[i], parts, c) for i in u) for u in units]
    ports = [sum(len(mods[i].get("inlets", [])) + len(mods[i].get("outlets", [])) for i in u) for u in units]

    def material(u: list[int], first: bool) -> str | None:
        steps = sorted(n for i in u for n in mods[i]["steps"] if n in by_n)
        if not steps:
            return None
        st = by_n[steps[0] if first else steps[-1]]
        return st.get("input_material") if first else (st.get("output_material") or st.get("input_material"))

    best, best_key, total = None, None, sum(area)
    for k in range(1, len(units)):
        left, right = sum(area[:k]), sum(area[k:])
        fits_ports = not (ports_bad and max_ports) or (2 + sum(ports[:k]) <= max_ports and 2 + sum(ports[k:]) <= max_ports)
        before, after = material(units[k - 1], first=False), material(units[k], first=True)
        changes = before != after or MATERIAL_CLASS.get(material(units[k - 1], True) or "") != MATERIAL_CLASS.get(after or "")
        balance = abs(left - right) / total if total else abs(k - len(units) / 2)
        key = (not fits_ports, balance - (0.25 if changes else 0.0), k)   # a material change wins unless very lopsided
        if best_key is None or key < best_key:
            best, best_key = k, key
    return best


def _chip_from(mods: list[dict], old: dict, ir: dict, c: dict, why: str) -> dict:
    by_n = {s["n"]: s for s in ir["steps"]}
    equip = set(C.get(c, "off_chip.equipment") or [])
    steps = sorted({n for m in mods for n in m["steps"]})
    temps = [by_n[n]["temp_c"] for n in steps if n in by_n and by_n[n].get("temp_c") is not None and _regime(by_n[n]) != "ambient"]
    cycles = any(_regime(by_n[n]) == "cycle" for n in steps if n in by_n)
    eq = {p for p in ("syringe_pump", "pressure_controller") if p in (old.get("equipment") or [])}
    for n in steps:
        for alts in _needs(by_n[n]) if n in by_n else []:
            have = [a for a in alts if a in equip]
            if have:
                eq.add(have[0])
    first_mat = _material(by_n[steps[0]]) if steps and steps[0] in by_n else None
    return {"id": old["id"], "name": old.get("name", old["id"]),
            "purpose": "; ".join(dict.fromkeys(m["does"].split(".")[0] for m in mods if m["steps"])) + ".",
            "steps": steps, "why_separate": why,
            "temp_c": (max(temps, key=lambda t: abs(t - 22)) if temps and not cycles else None),
            "equipment": sorted(eq), "modules": mods,
            "inputs": [{"name": "inlet_1", "from": "external", "carries": first_mat or "sample"}] +
                      [{"name": f"{r}_in", "from": "external", "carries": r} for m in mods for r in m.get("inlets", [])],
            "outputs": [{"name": "outlet_1", "to": "external"}] +
                       [{"name": f"{o}_out", "to": "waste"} for m in mods for o in m.get("outlets", [])]}


def resplit(plan: dict, feedback: list[str], ir: dict, constraints: dict, parts: dict) -> dict:
    """Deterministic answer to layout feedback (rules backend). Each chip reported too large or with too many
    ports is cut in two at a module boundary: modules sharing a step (merge + mixer) and lane blocks
    (split -> lanes -> pool) stay together, and cuts where the material changes are preferred. A chip that is
    one unit already is left as it is and reported in the rationale: it needs a smaller part, not another chip."""
    plan = copy.deepcopy(plan)
    by_n = {s["n"]: s for s in ir["steps"]}
    bad = _parse_feedback(feedback)
    max_chips = C.get(constraints, "envelope.max_chips")
    notes: list[str] = []
    pieces: list[tuple[dict, str, str]] = []    # (chip, old id, "whole" | "left" | "right")
    n_chips = len(plan["chips"])
    for ch in plan["chips"]:
        msgs = bad.get(ch["id"], [])
        size_bad = any(h in m for m in msgs for h in SIZE_HINTS)
        ports_bad = any(h in m for m in msgs for h in PORT_HINTS)
        if not (size_bad or ports_bad):
            if msgs:
                notes.append(f"{ch['id']} left as it is (the rules can't fix: {msgs[0][:100]})")
            pieces.append((ch, ch["id"], "whole"))
            continue
        mods = ch["modules"]
        k = _best_cut(mods, _units(mods, parts), by_n, parts, constraints, ports_bad)
        if k is None or (max_chips and n_chips + 1 > max_chips):
            notes.append(f"{ch['id']} can't be split further ({'one module' if len(mods) == 1 else 'one block that must stay together'}): "
                         "it needs a smaller part" if k is None else f"{ch['id']} not split: envelope.max_chips is {max_chips}")
            pieces.append((ch, ch["id"], "whole"))
            continue
        cut = _units(mods, parts)[k][0]
        reason = "too large for the chip envelope" if size_bad else "too many ports"
        pieces.append((_chip_from(mods[:cut], ch, ir, constraints, ch.get("why_separate") or "first chip in the flow"), ch["id"], "left"))
        pieces.append((_chip_from(mods[cut:], ch, ir, constraints,
                                  f"split off {ch['id']} after layout: the chip was {reason}; cut after module {mods[cut - 1]['id']}"),
                       ch["id"], "right"))
        n_chips += 1
        notes.append(f"{ch['id']} split in two after module {mods[cut - 1]['id']} ({reason})")

    # rename every chip from what it does, in flow order
    new_ids = []
    for i, (ch, _, _) in enumerate(pieces):
        labels = _chip_label([{**m, "_job": _job_of(m, by_n, parts)} for m in ch["modules"]], by_n)
        if len("_".join(labels)) > 64:
            while len("_".join(labels + ["etc"])) > 64 and len(labels) > 1:
                labels = labels[:-1]
            labels = labels + ["etc"]
        new_ids.append(f"chip{i + 1}_{'_'.join(labels)}")
        ch["name"] = _chip_name(labels)
    first_piece = {old: new_ids[i] for i, (_, old, part) in enumerate(pieces) if part in ("whole", "left")}
    last_piece = {old: new_ids[i] for i, (_, old, part) in enumerate(pieces) if part in ("whole", "right")}

    old_handoffs = {(h["from_chip"], h["to_chip"]): h for h in plan["handoffs"]}
    handoffs, used = [], set()
    for i in range(len(pieces) - 1):
        (a, a_old, _), (b, b_old, _) = pieces[i], pieces[i + 1]
        if a_old == b_old:   # the two halves of a split chip
            first = min(b["steps"] or [0])
            h = {"from_chip": a_old, "from_output": "outlet_1", "to_chip": b_old, "to_input": "inlet_1",
                 "carries": (_material(by_n[first]) if first in by_n else None) or "sample", "method": "tubing"}
        elif (a_old, b_old) in old_handoffs:
            h = copy.deepcopy(old_handoffs[(a_old, b_old)])
            used.add((a_old, b_old))
        else:
            continue
        h["from_chip"], h["to_chip"] = new_ids[i], new_ids[i + 1]
        handoffs.append(h)
    for key, h in old_handoffs.items():   # hand-offs between chips that were not neighbours (agent plans)
        if key not in used:
            h = copy.deepcopy(h)
            h["from_chip"], h["to_chip"] = last_piece.get(key[0], key[0]), first_piece.get(key[1], key[1])
            handoffs.append(h)

    coverage = dict(plan["coverage"])
    chips = []
    for i, (ch, old, _) in enumerate(pieces):
        ch["id"] = new_ids[i]
        for m in ch["modules"]:
            for n, where in coverage.items():
                if where == f"{old}.{m['id']}":
                    coverage[n] = f"{ch['id']}.{m['id']}"
        chips.append(ch)
    by_id = {c["id"]: c for c in chips}
    for h in handoffs:
        if h["from_chip"] in by_id and h["to_chip"] in by_id:
            by_id[h["from_chip"]]["outputs"][0]["to"] = f"{h['to_chip']}.{h.get('to_input', 'inlet_1')}"
            by_id[h["to_chip"]]["inputs"][0]["from"] = f"{h['from_chip']}.{h.get('from_output', 'outlet_1')}"
    plan["chips"], plan["handoffs"], plan["coverage"] = chips, handoffs, coverage
    plan["rationale"] = (plan.get("rationale") or "") + " Rules re-split after layout feedback: " + ("; ".join(notes) or "nothing to change") + "."
    return plan


def run(ir: dict, constraints: dict, parts: dict, agent, out_dir, feedback: list[str] | None = None) -> dict:
    draft = rules_plan(ir, constraints, parts)
    previous = None
    if feedback and out_dir:
        prev_path = Path(out_dir) / "chip_plan.json"
        if prev_path.exists():
            previous = json.loads(prev_path.read_text())
    if feedback and getattr(agent, "backend", None) == "rules":
        # no agent to ask: cut the reported chips deterministically
        plan = resplit(previous or draft, feedback, ir, constraints, parts)
        errs = validate(plan, ir, constraints, parts)
        if errs:
            kept = copy.deepcopy(previous or draft)
            kept["rationale"] = (kept.get("rationale") or "") + f" The rules re-split gave an invalid plan ({errs[0][:150]}); kept the previous plan."
            return kept
        return plan
    user = _prompt(ir, constraints, parts, draft, feedback, previous)
    key = None
    if feedback:
        key = "resplit_" + hashlib.sha1("\n".join(feedback).encode()).hexdigest()[:8]
    obj = agent.ask_json("partition", PROMPT.read_text(), user,
                         validate=lambda o: validate(_normalise(copy.deepcopy(o), parts), ir, constraints, parts), key=key)
    if obj is None:
        return draft
    return _normalise(obj, parts)


# -------------------------------------------------------------------------------- design files
def design_files(plan: dict, ir: dict, parts: dict | None = None) -> dict[str, dict]:
    """Each chip as a chip/designs/<id>.json design file that chip/components/build_chip.py can build.
    Parametric modules are written as their nearest library block (`part`), keeping the real part and
    its sizing in `autochip_part` / `params`. Parametric parts with no library stand-in (pillar filter,
    optical window, open well) go to the design's missing_parts: the autochip layout stage draws them."""
    if parts is None:
        parts = P.load_parts(C.load("unconstrained"))
    by_n = {s["n"]: s for s in ir["steps"]}
    chips = plan["chips"]
    firsts = [min(c["steps"]) if c["steps"] else 0 for c in chips]
    out = {}
    for i, ch in enumerate(chips):
        lo = 0 if i == 0 else firsts[i]
        hi = firsts[i + 1] if i + 1 < len(chips) else 10 ** 9
        mods, missing, stand_ins = [], [], []
        for m in ch["modules"]:
            part = parts.get(m["part"], {})
            src = m.get("part_source") or part.get("source", "library")
            d = {"id": m["id"], "part": m["part"], "steps": m["steps"], "does": m["does"]}
            if m.get("inlets"):
                d["inlets"] = m["inlets"]
            if m.get("outlets"):
                d["outlets"] = m["outlets"]
            if m.get("mesh_pore_um") is not None:
                d["mesh_pore_um"] = m["mesh_pore_um"]
            if src == "parametric":
                lib = part.get("library_equivalent")
                spec = {**{k: v["default"] for k, v in part.get("params", {}).items()}, **(m.get("params") or {})}
                if not lib:
                    missing.append({"steps": m["steps"], "need": f"{m['part']} ({', '.join(f'{k}={v}' for k, v in spec.items())})",
                                    "why": "parametric part with no library block; the autochip layout stage draws it",
                                    "module": m["id"], "inlets": m.get("inlets", [])})
                    continue
                d["part"] = lib
                d["autochip_part"] = m["part"]
                d["params"] = spec
                if m["part"] == "trap_chamber" and d.get("mesh_pore_um") is None:
                    d["mesh_pore_um"] = spec.get("gap_um")
                stand_ins.append(m["id"])
            elif m.get("params"):
                d["params"] = m["params"]
            mods.append(d)
        if not mods:
            # build_chip needs at least one module; keep the chip buildable with a plain channel.
            mods.append({"id": "pass_through", "part": "straight_channel", "steps": [], "does": "Placeholder: every module on this chip is parametric."})
        off = [o for o in plan.get("off_chip", []) if any(lo <= n < hi for n in o["steps"])]
        miss = [m for m in plan.get("missing_parts", []) if any(lo <= n < hi for n in m["steps"])]
        notes = []
        if stand_ins or missing:
            notes.append("Parametric parts: " + (f"modules {', '.join(stand_ins)} use the nearest library block as a stand-in "
                                                  "(real part and sizing in autochip_part / params). " if stand_ins else "")
                         + ("Modules listed in missing_parts with a 'module' field are parametric with no library block. " if missing else "")
                         + "build_chip.py builds the stand-ins at library size; the exact chip needs the autochip layout stage.")
        out[ch["id"]] = {
            "id": ch["id"],
            "name": ch["name"],
            "description": ch["purpose"] + (" " + " ".join(notes) if notes else ""),
            "source": f"autochip chip plan {plan['id']} (protocol {plan['protocol_id']}, constraints {plan['constraints_id']})",
            "placeholder": bool(ir.get("placeholder", True)),
            "needs_autochip_layout": bool(stand_ins or missing),
            "temp_c": ch.get("temp_c"),
            "equipment": ch.get("equipment", []),
            "modules": mods,
            "off_chip": [{"steps": o["steps"], "why": o["why"]} for o in off],
            "missing_parts": missing + [{"steps": m["steps"], "need": m["need"], "why": m["why"]} for m in miss],
            "steps_text": {str(n): by_n[n]["text"] for n in ch["steps"] if n in by_n},
        }
    return out

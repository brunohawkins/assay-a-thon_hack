"""Stage 5 (OWNER: agent D). Per chip: the run protocol (what to pump where, when), checked by the flow solver.

run(plan, ir, chips, constraints, out_dir) -> dict[chip_id, {"protocol": dict, "checks": [ {"severity", "message"} ], "files": {name: path}}]
Writes, per chip:
  out_dir/runs/<chip>.protocol.json   contracts/schemas/protocol.schema.json (what the operator and the twin read)
  out_dir/runs/<chip>.run.json        the executable program: per step which port FEEDs (rate, volume, syringe),
                                      which VENTs, all others CAP, plus temperature, light, magnet, wait, what to watch,
                                      and the operator/network.py flow check of those port states
  out_dir/runs/<chip>.run_sheet.md    the wet-lab run sheet for the chip (setup, step table, checks)
and for the whole protocol:
  out_dir/runs/pipeline_run_sheet.md  every IR step in order: on-chip steps, bench steps, hand-offs, missing parts
  out_dir/runs/pipeline_run.json      the same sequence as data (report.py renders it into report.html)
Optionally out_dir/runs/<chip>.twin_run.json: the operator's deterministic planner (operator/planner.py) run on the
protocol + chip, i.e. what the twin replays. Its outcome is reported as an info/warning check, never an error.

How port states are chosen (no on-chip valves: flow is switched by which syringe pushes and which port is capped):
  fill  (exchange, wash, lyse, RT, ...): FEED the module's own inlet for that reagent (else another of its inlets,
        else an upstream inlet, with a warning), VENT the module's own waste port, else the nearest outlet downstream.
  flow  (encapsulate, mix in a stream, filter, split, pool): FEED the carrier (the load inlet, which pushes the
        material from where it sits) plus the module's own reagent inlets; VENT beyond where the material goes next.
  load  FEED the load inlet with the sample; VENT downstream of the target.
  out   (collect, harvest, release, transfer): FEED the module's inlet (or the carrier); VENT the product outlet.
  hold  (incubate, cure, count, culture between exchanges): every port CAP.
Everything else is CAP, so liquid can't run back into upstream modules.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "schemas"

# --- tunables -------------------------------------------------------------------------------

# Default feed rate (uL/min) by what the target module holds (flows are placeholders unless the IR gives one).
DEFAULT_FLOW = {"cells": 10.0, "capsules": 20.0, "droplets": 5.0, "beads": 20.0, "gel": 5.0, "oil": 10.0}
DEFAULT_FLOW_OTHER = 20.0
# Wall-shear limits in the target chamber (dyn/cm2) by what it holds. Adherent / settled cells: ~1 dyn/cm2 is a
# common comfort limit; capsules and droplets are sturdier. Channel limit for cells moving through channels.
CHAMBER_SHEAR_LIMIT = {"cells": 1.0, "capsules": 5.0, "droplets": 5.0}
CHANNEL_SHEAR_LIMIT_CELLS = 10.0
FLUSH_VOLUMES = 3            # an exchange pushes 3 chamber volumes per wash (~95% replaced)
SYRINGE_MARGIN = 1.2         # load 20% more than the program uses
# Encapsulation co-flow default (uL/h): core, shell, oil.
ENCAP_FLOWS_UL_H = {"core": 120.0, "shell": 120.0, "oil": 600.0}
DEFAULT_CAPSULES = 100000
ROOM_C = 22.0

MATERIALS = ("cells", "capsules", "droplets", "beads", "nucleic_acid", "library", "lysate", "liquid", "oil", "gel")

# IR action -> (protocol operation, mode, default reagent, default hold seconds after the fill)
ACTIONS = {
    "culture": ("culture", "culture", "culture_medium", None),
    "seed": ("load", "load", "cell_suspension", None),
    "feed": ("exchange", "fill", "culture_medium", None),
    "dissociate": ("exchange", "fill", "dissociation_enzyme", 300),
    "quench": ("exchange", "fill", "quench_medium", None),
    "add_reagent": ("exchange", "fill", "reagent", None),
    "mix": ("mix", "fill", "mixing_buffer", None),
    "incubate": ("hold_temp", "hold", None, 600),
    "wash_cells": ("exchange", "fill", "wash_buffer", None),
    "wash_particles": ("exchange", "fill", "wash_buffer", None),
    "centrifuge": ("exchange", "fill", "wash_buffer", None),
    "resuspend": ("exchange", "fill", "resuspension_buffer", None),
    "filter": ("transfer", "flow", "carrier_buffer", None),
    "count": ("count", "hold", None, 60),
    "image": ("count", "hold", None, 60),
    "measure": ("count", "hold", None, 60),
    "encapsulate": ("encapsulate", "flow", "core_solution", None),
    "emulsify": ("encapsulate", "flow", "core_solution", None),
    "cure": ("cure", "hold", None, 30),
    "break_emulsion": ("exchange", "fill", "emulsion_breaker", None),
    "lyse": ("exchange", "fill", "lysis_buffer", 600),
    "reverse_transcribe": ("exchange", "fill", "rt_mix", 3600),
    "amplify": ("exchange", "fill", "pcr_mix", 1800),
    "fragment": ("exchange", "fill", "fragmentation_mix", 600),
    "ligate": ("exchange", "fill", "ligation_mix", 900),
    "bind_beads": ("exchange", "fill", "magnetic_beads", 300),
    "magnetic_separate": ("exchange", "fill", "wash_buffer", None),
    "elute": ("exchange", "fill", "elution_buffer", 120),
    "purify": ("exchange", "fill", "wash_buffer", None),
    "split": ("split", "flow", "carrier_buffer", None),
    "pool": ("pool", "flow", "carrier_buffer", None),
    "release": ("release", "out", "release_reagent", None),
    "collect": ("collect", "out", "collection_buffer", None),
    "transfer": ("transfer", "out", "carrier_buffer", None),
    "store": ("hold_temp", "hold", None, 600),
    "other": ("exchange", "fill", "reagent", None),
}
# Actions that add something to a stream: in a flow-through part (mixer, junction) they become a "mix" in flow.
STREAM_ADDS = ("quench", "add_reagent", "mix", "resuspend", "wash_cells", "wash_particles", "centrifuge")
SYRINGES_ML = (1, 3, 5, 10, 20, 30, 60)   # standard syringe sizes for the set-up table
SEPARATOR_WORDS = ("spiral", "separat", "inertial")
FLOW_CHAMBER_TYPES = ("mixer", "junction", "pool", "split", "filter")
FLOW_PART_WORDS = ("junction", "serpentine", "snake", "spiral", "mixer", "channel", "manifold", "distributor", "fork")
DEFAULT_TEMP = {"culture": 37.0, "dissociate": 37.0, "reverse_transcribe": 42.0, "amplify": 72.0, "store": 4.0}
SEVERITY_ORDER = ("error", "warning", "info")


# --- small helpers ------------------------------------------------------------------------------


def snake(s: str | None, fallback: str = "reagent") -> str:
    """Any name -> lowercase snake_case that starts with a letter (the contract's snake_id)."""
    t = re.sub(r"[^a-z0-9]+", "_", str(s or "").lower()).strip("_")
    t = re.sub(r"_+", "_", t)
    if not t:
        t = fallback
    if not t[0].isalpha():
        t = "r_" + t
    return t


def _tokens(s: str) -> set[str]:
    return {t for t in snake(s).split("_") if t and t not in ("in", "inlet", "port", "out", "outlet", "line")}


def fmt_time(s: float) -> str:
    s = int(round(s))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    if d:
        return f"{d} d {h:02d}:{m:02d}"
    return f"{h}:{m:02d}:{sec:02d}"


def fmt_dur(s: float | None) -> str:
    if s is None:
        return ""
    s = float(s)
    if s >= 86400:
        return f"{s / 86400:.3g} d"
    if s >= 3600:
        return f"{s / 3600:.3g} h"
    if s >= 60:
        return f"{s / 60:.3g} min"
    return f"{s:.0f} s"


def _rel(path: Path, base: Path) -> str:
    try:
        return Path(path).resolve().relative_to(Path(base).resolve()).as_posix()
    except ValueError:
        return str(path)


def _network_module():
    """operator/network.py, loaded by path so the repo's operator/ folder never shadows the stdlib module."""
    if "autochip_operator_network" in sys.modules:
        return sys.modules["autochip_operator_network"]
    spec = importlib.util.spec_from_file_location("autochip_operator_network", ROOT / "operator" / "network.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["autochip_operator_network"] = mod
    spec.loader.exec_module(mod)
    return mod


def _validator(name: str):
    import jsonschema
    return jsonschema.Draft202012Validator(json.loads((SCHEMAS / f"{name}.schema.json").read_text()))


def _cget(c: dict, path: str, default=None):
    cur = c or {}
    for k in path.split("."):
        if not isinstance(cur, dict) or cur.get(k) is None:
            return default
        cur = cur[k]
    return cur


# --- chip graph ---------------------------------------------------------------------------------


class ChipGraph:
    """The chip spec as a directed graph (channels point from -> to, i.e. the designed flow order)."""

    def __init__(self, spec: dict, chip_plan: dict):
        self.spec = spec
        self.chambers = {c["id"]: c for c in spec.get("chambers", [])}
        self.ports = {p["id"]: p for p in spec.get("ports", [])}
        self.succ: dict[str, list[str]] = {}
        self.pred: dict[str, list[str]] = {}
        for ch in spec.get("channels", []):
            self.succ.setdefault(ch["from"], []).append(ch["to"])
            self.pred.setdefault(ch["to"], []).append(ch["from"])
        self.modules = {m["id"]: m for m in chip_plan.get("modules", [])}
        self.module_order = [m["id"] for m in chip_plan.get("modules", [])]
        self.module_chambers = {mid: self._chambers_of(mid) for mid in self.module_order}
        reagent_ports = {f"{snake(n)}_in" for m in chip_plan.get("modules", []) for n in m.get("inlets", []) or []}
        waste_ports = {f"{snake(n)}_out" for m in chip_plan.get("modules", []) for n in m.get("outlets", []) or []}
        self.inlets = [p for p, d in self.ports.items() if d.get("type") == "inlet"]
        self.outlets = [p for p, d in self.ports.items() if d.get("type") == "outlet"]
        main = [p for p in self.inlets if self.ports[p].get("role") in ("load", "cells", "transfer")
                or re.fullmatch(r"inlet_\d+", p)]
        if not main:
            main = [p for p in self.inlets if p not in reagent_ports]
        self.load_inlets = sorted(main, key=lambda p: (not re.fullmatch(r"inlet_\d+", p), p))
        self.waste_outlets = [p for p in self.outlets if self.ports[p].get("role") == "waste" or "waste" in p
                              or self.ports[p].get("capsule_passable") is False or p in waste_ports]
        prod = [p for p in self.outlets if p not in self.waste_outlets]
        self.product_outlets = sorted(prod, key=lambda p: (not (re.fullmatch(r"outlet_\d+", p)
                                                                or self.ports[p].get("role") in ("harvest", "release", "transfer")), p))

    def _chambers_of(self, mid: str) -> list[str]:
        if mid in self.chambers:
            return [mid]
        return [c for c in self.chambers if c.startswith(mid + "_") or c.endswith("_" + mid)]

    def down(self, start: str | list[str]) -> dict[str, int]:
        return self._bfs(start, self.succ)

    def up(self, start: str | list[str]) -> dict[str, int]:
        return self._bfs(start, self.pred)

    @staticmethod
    def _bfs(start, adj) -> dict[str, int]:
        starts = [start] if isinstance(start, str) else list(start)
        dist = {s: 0 for s in starts}
        q = deque(starts)
        while q:
            n = q.popleft()
            for m in adj.get(n, []):
                if m not in dist:
                    dist[m] = dist[n] + 1
                    q.append(m)
        return dist

    def own_inlets(self, chambers: list[str]) -> list[str]:
        """Inlet ports with a channel straight into one of these chambers (the module's reagent lines)."""
        out = []
        for c in chambers:
            for p in self.pred.get(c, []):
                if p in self.ports and self.ports[p].get("type") == "inlet" and p not in out:
                    out.append(p)
        return out

    def own_outlets(self, chambers: list[str]) -> list[str]:
        out = []
        for c in chambers:
            for p in self.succ.get(c, []):
                if p in self.ports and self.ports[p].get("type") == "outlet" and p not in out:
                    out.append(p)
        return out

    def module_of_chamber(self, chamber: str) -> str | None:
        for mid, chs in self.module_chambers.items():
            if chamber in chs:
                return mid
        return None

    def port_module(self, port: str) -> str | None:
        for c in self.succ.get(port, []) + self.pred.get(port, []):
            m = self.module_of_chamber(c)
            if m:
                return m
        return None

    def path_nodes(self, feeds: list[str], vents: list[str]) -> set[str]:
        """Nodes on some designed-direction path from a feed to a vent."""
        if not feeds or not vents:
            return set()
        return set(self.down(feeds)) & set(self.up(vents))


def units(text: str) -> str:
    """Display units for people: uL -> µL, '37 C' -> '37 °C' (data files keep the ASCII forms)."""
    text = re.sub(r"\buL\b", "µL", text)
    return re.sub(r"(\d) C\b", "\\1 °C", text)


def _index_like(st: dict) -> bool:
    """True when a step implies a different reagent per lane (index / barcode / 'a different ... mix')."""
    text = " ".join([st.get("text") or ""] + [str(r) for r in st.get("reagents") or []]).lower()
    return bool(re.search(r"\bindex|barcod|different\b", text))


def match_port(reagent: str, ports: list[str]) -> str | None:
    """The port whose name best matches the reagent (shared words), or None."""
    want = _tokens(reagent)
    best, score = None, 0
    for p in ports:
        s = len(want & _tokens(p))
        if snake(reagent) + "_in" == p:
            s += 10
        if s > score:
            best, score = p, s
    return best


# --- step planning ---------------------------------------------------------------------------------


class ChipPlanner:
    def __init__(self, chip_plan: dict, spec: dict | None, ir: dict, constraints: dict, plan: dict):
        self.cp = chip_plan
        self.spec = spec
        self.ir = ir
        self.cons = constraints or {}
        self.plan = plan
        self.cid = chip_plan["id"]
        self.g = ChipGraph(spec, chip_plan) if spec else None
        self.checks: list[dict] = []
        self.span: dict[int, list[str]] = {}   # IR step -> other modules (different parts) that share it
        # hand-offs (set by run()): where the material comes in, where it leaves, and for which chip
        self.in_port: str | None = None
        self.prev_chip: str | None = None
        self.out_port: str | None = None
        self.next_chip: str | None = None
        self.next_port: str | None = None
        self.collected = False
        self.lane_groups: dict[str, list[dict]] = {}   # module id -> its parallel lanes [{"module", "chamber"}]
        self.flow_range = _cget(self.cons, "off_chip.flow_range_ul_min")
        self.pump_channels = _cget(self.cons, "off_chip.pump_channels")
        self.max_kpa = _cget(self.cons, "operating.max_pressure_kpa")
        self.equipment = set(_cget(self.cons, "off_chip.equipment", []) or [])
        self.immiscible = {snake(r["id"]) for r in ir.get("reagents", []) if r.get("immiscible")}
        self.net = None
        if spec:
            try:
                self.net = _network_module().Network(spec)
            except Exception as e:  # geometry the solver can't read
                self.check("error", f"the flow solver can't read chip {self.cid}: {e}")

    def check(self, severity: str, message: str, step: int | None = None) -> dict:
        c = {"severity": severity, "message": message}
        if step is not None:
            c["step"] = step
        self.checks.append(c)
        return c

    # -- which IR steps run here, on which module

    def assigned_steps(self) -> list[tuple[dict, dict]]:
        by_n = {s["n"]: s for s in self.ir.get("steps", [])}
        owner: dict[int, str] = {}
        for m in self.cp.get("modules", []):
            for n in m.get("steps", []) or []:
                owner.setdefault(int(n), m["id"])
        for k, v in (self.plan.get("coverage") or {}).items():
            if isinstance(v, str) and v.startswith(self.cid + ".") and str(k).isdigit():
                owner.setdefault(int(k), v.split(".", 1)[1])
        for n in self.cp.get("steps", []) or []:
            if int(n) not in owner and self.cp.get("modules"):
                owner[int(n)] = self.cp["modules"][0]["id"]
                self.check("warning", f"IR step {n} is listed for chip {self.cid} but no module does it; "
                                      f"assumed {owner[int(n)]}", n)
        out = []
        mods = {m["id"]: m for m in self.cp.get("modules", [])}
        for n in sorted(owner):
            if n not in by_n:
                self.check("warning", f"chip {self.cid} lists step {n}, which is not in the protocol IR", n)
                continue
            mod = mods.get(owner[n])
            if mod is None:
                self.check("error", f"step {n} is assigned to {self.cid}.{owner[n]}, but the chip plan has no such module", n)
                continue
            doing = [m for m in self.cp.get("modules", []) if n in (m.get("steps") or [])] or [mod]
            mod = self._primary(by_n[n], doing, mod)
            # parallel copies of the same part doing the same step (e.g. one PCR chamber per portion)
            twins = [m for m in doing if m is not mod and m.get("part") == mod.get("part")]
            if twins and self.g:
                group = [{"module": m["id"], "chamber": c} for m in [mod] + twins
                         if (c := (self.g.module_chambers.get(m["id"]) or [None])[0])]
                for m in [mod] + twins:
                    self.lane_groups[m["id"]] = group
            self.span[n] = [m["id"] for m in doing if m is not mod and m not in twins]   # e.g. join + mixer + junction
            out.append((by_n[n], [mod] + twins))
        return out

    def _primary(self, st: dict, doing: list[dict], default: dict) -> dict:
        """When several different parts share a step, the one where it really happens: for encapsulation the
        droplet junction (the module with the shell / oil lines), otherwise the first in flow order."""
        if len(doing) < 2 or st.get("action") not in ("encapsulate", "emulsify"):
            return default

        def score(m):
            words = f"{m.get('part', '')} {m['id']}".lower()
            s = 3 * any(w in words for w in ("droplet", "flow_focus", "focusing", "cross", "encapsul"))
            s += sum(1 for i in (m.get("inlets") or []) if any(w in i for w in ("oil", "shell", "polymer", "gel")))
            return s
        best = max(doing, key=score)
        return best if score(best) > 0 else default

    # -- main

    def build(self) -> tuple[dict, dict]:
        steps = self.assigned_steps()
        if not steps:
            self.check("warning", f"chip {self.cid} has no protocol steps assigned")
        prog_steps: list[dict] = []
        proto_steps: list[dict] = []
        t = 0.0
        loc: str | None = None   # module where the material sits (None: not on the chip yet)
        collected = False
        all_phases: list[dict] = []
        for i, (st, mods) in enumerate(steps):
            nxt = steps[i + 1][1][0]["id"] if i + 1 < len(steps) else None
            mode = self._effective_mode(st, mods[0])
            lane_ids = {m["id"] for m in mods}
            moves = []
            # the material has to be where the step happens: load it onto the chip, or move it between modules
            if self.g and mode not in ("flow", "load"):
                if loc is None and len(mods) > 1 and all(self._lane_lines(st, m) for m in mods):
                    # parallel chambers (e.g. a tri-culture): seed each through its own line, vented through its own outlet
                    moves += [self._move_phase(st, None, m, lines=self._lane_lines(st, m)) for m in mods]
                elif loc is None:
                    moves.append(self._move_phase(st, None, mods[0]))
                elif loc not in lane_ids:
                    moves.append(self._move_phase(st, loc, mods[0]))
            moves = [m for m in moves if m]
            if moves:
                loc = mods[0]["id"]
            if mode == "fill" and len(mods) > 1:
                phases = self._lane_fills(st, mods, loc, nxt)
                loc = mods[0]["id"]
            elif mode == "culture" and len(mods) > 1 and self.g:
                phases = self._lane_cultures(st, mods)
                loc = mods[0]["id"]
            else:
                phases, loc = self.plan_step(st, mods[0], loc, nxt)
            if loc is None and phases and phases[-1].get("collects"):
                collected = True
            if moves and st.get("action") == "bind_beads" and phases and phases[0]["phase"] == "fill":
                # beads first, then the (liquid) sample onto them: an exchange after the load would wash it to waste
                all_phases += phases[:1] + moves + phases[1:]
            else:
                all_phases += moves + phases
        if self.g and steps and loc is not None:   # whatever is still on the chip leaves as the product / hand-off
            fin = self._collect_phase(steps[-1][0], loc)
            if fin:
                all_phases.append(fin)
                collected = True
        self.collected = collected
        for ph in all_phases:
            if True:
                ph["t_start_s"] = round(t, 1)
                t += ph["duration_s"]
                ph["n"] = len(proto_steps) + 1
                ph["protocol"]["step"] = ph["n"]
                proto_steps.append(ph.pop("protocol"))
                prog_steps.append(ph)
        protocol = {
            "id": snake(f"{self.ir.get('id', 'protocol')}_{self.cid}"),
            "name": f"{self.ir.get('name') or self.ir.get('id')}: {self.cp.get('name') or self.cid}",
            "source": self._source_line(),
            "placeholder": True,
            "description": (f"Generated by autochip from protocol IR '{self.ir.get('id')}' for chip {self.cid} "
                            f"({(self.cp.get('purpose') or '').strip()}). Flows, volumes and times the source did not "
                            f"give are placeholders."),
            "steps": proto_steps or [self._idle_step()],
        }
        program = {
            "kind": "chip_run_program",
            "chip": self.cid,
            "chip_name": self.cp.get("name"),
            "purpose": self.cp.get("purpose"),
            "placeholder": True,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "port_states": "FEED = a syringe pump pushes at the given rate; VENT = open outlet to a tube; CAP = plugged",
            "limits": {"pump_channels": self.pump_channels, "flow_range_ul_min": self.flow_range,
                       "max_pressure_kpa": self.max_kpa, "chamber_shear_limit_dyn_cm2": CHAMBER_SHEAR_LIMIT,
                       "channel_shear_limit_cells_dyn_cm2": CHANNEL_SHEAR_LIMIT_CELLS},
            "ports": [{"id": p["id"], "type": p.get("type"), "role": p.get("role"),
                       "module": self.g.port_module(p["id"]) if self.g else None}
                      for p in (self.spec or {}).get("ports", [])],
            "total_duration_s": round(t, 1),
            "steps": prog_steps,
        }
        program["syringes"] = self.syringes(prog_steps)
        program["equipment"] = self.equipment_needed(prog_steps)
        return protocol, program

    def _source_line(self) -> str:
        src = self.ir.get("source") or {}
        bits = [b for b in (src.get("citation"), src.get("title"), src.get("file")) if b]
        return "; ".join(bits) or f"autochip {src.get('kind', 'source')}"

    def _idle_step(self) -> dict:
        target = next(iter(self.g.chambers), None) if self.g else None
        return {"step": 1, "operation": "hold_temp", "target": snake(target or "chamber"), "reagent": None,
                "volume_ul": None, "duration_s": 1, "temp_c": ROOM_C, "washes": None, "flow_ul_min": None,
                "note": "No steps were assigned to this chip."}

    # -- one IR step -> one or two phases

    # -- parallel lanes (split -> lane, lane, ... -> pool): one IR step done by several copies of a part

    def _lane_chamber(self, mod: dict) -> str | None:
        return ((self.g.module_chambers.get(mod["id"]) if self.g else None) or [None])[0]

    def _lane_leak(self, st: dict, mod: dict, siblings: list[str]) -> float:
        """Dry solve: fraction of one lane's fill that ends up in its sibling lanes (no checks recorded)."""
        if not self.net or not siblings:
            return 0.0
        ch = self._lane_chamber(mod)
        reagent = snake((st.get("reagents") or [ACTIONS.get(st.get("action"), ACTIONS["other"])[2]])[0])
        own = self.g.own_inlets([ch]) if ch else []
        feed = match_port(reagent, own) or (own[0] if own else None)
        if not feed:
            return 0.0
        outs = self.g.own_outlets([ch])
        if not outs:
            dist = self.g.down(ch)
            outs = sorted((p for p in dist if p in self.g.outlets), key=lambda p: (dist[p], p not in self.g.waste_outlets))
        if not outs:
            return 0.0
        nw = _network_module()
        try:
            sol = self.net.solve({feed: (nw.FEED, 10.0), outs[0]: nw.VENT})
        except Exception:
            return 0.0
        return sum(sol["chambers"].get(c, {}).get("throughput_ul_min", 0.0) for c in siblings) / 10.0

    def _balance_lanes(self, feeds: dict, vents: list[str], lanes: list[dict], rounds: int = 25) -> None:
        """Equal rates don't mean equal pressures when the lanes' paths differ, so some liquid would cross the splitter.
        Nudge each lane's rate by the flow it takes from (or gives to) the splitter side until none crosses."""
        if not self.net:
            return
        nw = _network_module()
        lane_of = {f["lane"]: p for p, f in feeds.items() if f.get("lane")}
        ch_of = {ln["module"]: ln["chamber"] for ln in lanes}
        # channels into each lane from a chamber (the splitter side), not from its own inlet port
        side = {m: [c for c in self.spec.get("channels", []) if c["to"] == ch_of[m] and c["from"] in self.g.chambers]
                for m in lane_of}
        target = sum(feeds[p]["ul_min"] for p in lane_of.values())
        for _ in range(rounds):
            states = {p: (nw.FEED, feeds[p]["ul_min"]) for p in feeds}
            states.update({v: nw.VENT for v in vents})
            try:
                sol = self.net.solve(states)
            except Exception:
                return
            cross = {m: sum(sol["channels"][c["id"]]["flow_ul_min"] for c in side[m]) for m in lane_of}
            if max((abs(v) for v in cross.values()), default=0) < 1e-4 * target:
                break
            for m, p in lane_of.items():
                feeds[p]["ul_min"] = max(feeds[p]["ul_min"] + cross[m], 1e-3)
        lo = (self.flow_range or [None, None])[0]
        for p in lane_of.values():
            f = feeds[p]
            f["ul_min"] = round(max(f["ul_min"], lo or 0), 3)
            f["time_s"] = round(f["volume_ul"] / f["ul_min"] * 60, 1)
            f["balanced"] = True

    def _lane_fills(self, st: dict, mods: list[dict], loc, nxt) -> list[dict]:
        """Fill parallel lanes. Each lane with its own outlet is filled on its own (siblings capped). Lanes that share
        the splitter and the pool can't be isolated by capping (a lone lane's fill runs back through the splitter into
        its siblings), so they are filled together, each from its own inlet at the same rate: no net flow crosses
        the splitter. One shared incubation follows."""
        n = st["n"]
        chs = [self._lane_chamber(m) for m in mods]
        sib = lambda j: [c for k, c in enumerate(chs) if k != j and c]  # noqa: E731
        leak = max((self._lane_leak(st, m, sib(j)) for j, m in enumerate(mods)), default=0.0)
        together = leak > 0.01 and (not self.pump_channels or len(mods) <= self.pump_channels)
        if leak > 0.01 and not together:
            self.check("error", f"step {n}: the {len(mods)} lanes ({', '.join(m['id'] for m in mods)}) share a splitter "
                                f"and a pool with no outlet per lane, so filling one lane pushes about {100 * leak:.0f}% "
                                f"of its reagent into the others; filling them together needs {len(mods)} pump lines "
                                f"(the constraints allow {self.pump_channels}). Give each lane its own outlet.", n)
        if together:
            phs, _ = self.plan_step(st, mods[0], loc, nxt, lanes=mods)
            return phs
        phases = []
        for j, mod in enumerate(mods):
            phs, _ = self.plan_step(st, mod, loc, nxt, siblings=sib(j), leak_reported=leak > 0.01, lane_index=j + 1,
                                    fill_only=j < len(mods) - 1)   # one shared incubation after the last lane
            phases += phs
        return phases

    # -- moving the material: onto the chip, between modules, off the chip

    def _effective_mode(self, st: dict, mod: dict) -> str:
        action = st.get("action", "other")
        mode = ACTIONS.get(action, ACTIONS["other"])[1]
        ch = (self.g.module_chambers.get(mod["id"]) or [None])[0] if self.g else None
        if action in STREAM_ADDS and ch and self._is_flow_module(mod, ch):
            return "flow"
        return mode

    def _material(self, st: dict, mod: dict | None, which: str = "input_material") -> str:
        m = st.get(which) or (mod or {}).get("holds")
        return m if m in MATERIALS else "sample"

    def _lane_lines(self, st: dict, mod: dict) -> tuple[str, str] | None:
        """(inlet, outlet) of a parallel chamber's own lines: its medium (or first) inlet and its own vent."""
        ch = self._lane_chamber(mod)
        if not ch or not self.g:
            return None
        own_in, own_out = self.g.own_inlets([ch]), self.g.own_outlets([ch])
        reagent = snake((st.get("reagents") or ["culture_medium"])[0])
        p = match_port(reagent, own_in) or match_port("medium", own_in) or (own_in[0] if own_in else None)
        v = next((o for o in own_out if o in self.g.waste_outlets), own_out[0] if own_out else None)
        return (p, v) if p and v else None

    def _lane_cultures(self, st: dict, mods: list[dict]) -> list[dict]:
        """Parallel culture chambers (e.g. a tri-culture around a junction). 'Apart': every chamber gets its own
        medium changes from its own line, vented through its own outlet, so nothing crosses between them. 'Sharing
        medium' (when the step says so): one chamber is fed and the others are vented, so its conditioned medium
        reaches them through the junction (Module 1's interaction mode). Half the time each when both are asked."""
        n = st["n"]
        lanes = [{"module": m["id"], "chamber": c} for m in mods if (c := self._lane_chamber(m))]
        text = st.get("text", "") or ""
        reagent = snake((st.get("reagents") or ["culture_medium"])[0])
        holds = next((x for x in (st.get("input_material"), mods[0].get("holds")) if x in MATERIALS), "cells")
        temp = float(st.get("temp_c") or self.cp.get("temp_c") or DEFAULT_TEMP["culture"])
        dur = float(st.get("duration_s") or 3 * 86400)
        share = bool(re.search(r"shar|together|interact|conditioned|co-?cultur", text, re.I))
        own_vents = all(self._lane_lines(st, m) for m in mods)
        no_share = share and not own_vents   # sharing medium needs a vent on each chamber
        if no_share:
            share = False
        apart_s = dur / 2 if share else dur
        interval = 86400.0 if apart_s > 86400 else max(apart_s / 2, 60.0)
        q, defaulted = self._rate(st, holds)
        base = {"ir_step": n, "ir_action": "culture", "ir_text": text, "module": mods[0]["id"], "target": lanes[0]["chamber"],
                "holds": holds, "magnet": False, "light": None, "issues": [], "lanes": lanes}
        feeds, vents, missing, lane_reagents = {}, [], [], []
        for ln, m in zip(lanes, mods):
            own_in = self.g.own_inlets([ln["chamber"]])
            p = match_port(reagent, own_in) or match_port("medium", own_in) or (own_in[0] if own_in else None)
            v = (self._lane_lines(st, m) or (None, None))[1]
            if not p:
                missing.append(ln["module"])
                continue
            vol = round(max(1.5 * (self._chamber_volume(ln["chamber"]) or 50.0), 1.0), 1)
            n_ex = max(1, int(apart_s // interval))
            r = snake(p[:-3]) if p.endswith("_in") else reagent
            feeds[p] = {"reagent": r, "ul_min": q, "volume_ul": round(vol * n_ex, 1), "time_s": round(vol / q * 60, 1),
                        "repeat_every_s": interval, "repeats": n_ex, "lane": ln["module"]}
            if own_vents:
                vents.append(v)
            lane_reagents.append(r)
        if missing:
            base["issues"].append(self.check("error", f"step {n}: chamber(s) {', '.join(missing)} have no medium inlet of "
                                                      f"their own, so they can't be cultured apart", n))
        if feeds and not own_vents:
            # no vent per chamber: feed every chamber's own line at once through the shared outlet, rates balanced so
            # no medium crosses between chambers
            vents = self._vents_for(base, [ln["chamber"] for ln in lanes], product=False)
            if len(feeds) > 1 and vents:
                self._balance_lanes(feeds, vents, lanes)
            if no_share:
                base["issues"].append(self.check("warning", f"step {n}: sharing medium needs a vent on each chamber; "
                                                            f"with one shared outlet the chambers can only exchange "
                                                            f"factors by diffusion across the junction, so they stay "
                                                            f"apart for the whole culture", n))
        self._equipment_check(base, temp, n)
        targets = [ln["chamber"] for ln in lanes if any(f.get("lane") == ln["module"] for f in feeds.values())]
        first = next(iter(feeds.values()), None)
        vol0 = first["volume_ul"] / max(first["repeats"], 1) if first else 50.0
        proto = {"operation": "culture", "targets": targets or [base["target"]], "reagents": lane_reagents or [reagent],
                 "exchange_interval_s": interval, "reagent": reagent, "volume_ul": round(vol0, 1), "duration_s": apart_s,
                 "temp_c": temp, "washes": None, "flow_ul_min": q,
                 "note": self._note(f"culture the {len(lanes)} chambers apart at {temp:g} C for {fmt_dur(apart_s)}: every "
                                    f"{fmt_dur(interval)} each gets its own medium (FEED {', '.join(feeds)}; VENT "
                                    f"{', '.join(vents)}), all ports capped in between. {text}", base)}
        watch = (f"every {fmt_dur(interval)}, all {len(feeds)} pumps together: each chamber's medium leaves through its "
                 f"own vent and nothing crosses the junction; check each culture on the microscope")
        out = [self._phase(base, kind="culture", title=f"Culture apart {fmt_dur(apart_s)} ({len(feeds)} chambers, own "
                                                         f"medium each, change every {fmt_dur(interval)})",
                           protocol=proto, feeds=feeds, vents=vents, duration_s=apart_s, temp_c=temp, watch=watch,
                           wait_s=apart_s, extra={"rate_defaulted": defaulted,
                                                  "repeat": {"every_s": interval, "times": max(1, int(apart_s // interval))}})]
        if share and len(feeds) > 1:
            src_p = next(iter(feeds))
            src_lane = feeds[src_p]["lane"]
            others = [v for v, f in zip(vents, feeds.values()) if f["lane"] != src_lane]
            n_ex = max(1, int((dur - apart_s) // interval))
            f2 = {src_p: {"reagent": feeds[src_p]["reagent"], "ul_min": q, "volume_ul": round(vol0 * n_ex, 1),
                          "time_s": round(vol0 / q * 60, 1), "repeat_every_s": interval, "repeats": n_ex}}
            b2 = {**base, "issues": [], "share": True}
            proto2 = {"operation": "culture", "targets": targets, "reagents": [feeds[src_p]["reagent"]] * len(targets),
                      "exchange_interval_s": interval, "reagent": feeds[src_p]["reagent"], "volume_ul": round(vol0, 1),
                      "duration_s": dur - apart_s, "temp_c": temp, "washes": None, "flow_ul_min": q,
                      "note": self._note(f"culture sharing medium for {fmt_dur(dur - apart_s)}: every {fmt_dur(interval)} "
                                         f"FEED {src_p} only and VENT {', '.join(others)}, so {src_lane}'s conditioned "
                                         f"medium reaches the other chambers through the junction (rotate which chamber "
                                         f"is fed to let every type condition the others). {text}", b2)}
            out.append(self._phase(b2, kind="culture", title=f"Culture sharing medium {fmt_dur(dur - apart_s)} (feed "
                                                              f"{src_lane}, vent the others)",
                                   protocol=proto2, feeds=f2, vents=others, duration_s=dur - apart_s, temp_c=temp,
                                   watch=f"medium from {src_lane} reaches the other chambers through the junction and "
                                         f"leaves at {', '.join(others)}",
                                   wait_s=dur - apart_s, extra={"rate_defaulted": defaulted, "share": True,
                                                                "repeat": {"every_s": interval, "times": n_ex}}))
        return out

    def _move_phase(self, st: dict, src: str | None, mod: dict, lines: tuple[str, str] | None = None) -> dict | None:
        """LOAD the material onto the chip (src None: from the hand-off / the bench) or TRANSFER it from module src
        to mod: push from upstream, vent beyond the destination, stop when it has arrived. lines: (inlet, vent) to
        seed one parallel chamber through its own lines."""
        g, n = self.g, st["n"]
        to_ch = g.module_chambers.get(mod["id"]) or []
        if not to_ch:
            return None
        target = to_ch[0]
        material = self._material(st, mod)
        base = {"ir_step": n, "ir_action": "load" if src is None else "transfer", "ir_text": st.get("text", ""),
                "module": mod["id"], "target": target, "holds": material if material in MATERIALS else None,
                "magnet": False, "light": None, "issues": []}
        lanes = None if lines else self.lane_groups.get(mod["id"])
        if lanes:   # parallel lanes: the material goes into all of them at once
            base["lanes"] = lanes
            to_ch = [ln["chamber"] for ln in lanes]
        elif lines:
            base["siblings"] = [ln["chamber"] for ln in self.lane_groups.get(mod["id"], []) if ln["chamber"] != target]
        q, defaulted = self._rate({}, base["holds"])
        src_ch = (g.module_chambers.get(src) or [None])[0] if src else None
        if src is None:
            port = lines[0] if lines else self._carrier(base, None)
            reagent = snake(f"{material}_for_{mod['id']}") if lines else snake(f"{material}_sample")
            vol = round(max(1.5 * (self._chamber_volume(target) or 20.0), 10.0), 1)
            origin = f"the hand-off from {self.prev_chip}" if self.prev_chip else "the bench"
        else:
            own_src = g.own_inlets(g.module_chambers.get(src) or [])
            port = self._carrier(base, src) if g.load_inlets else (own_src[0] if own_src else None)
            reagent = "carrier_buffer"
            vol = round(max(2 * ((self._chamber_volume(src_ch) or 0) + (self._chamber_volume(target) or 0)), 10.0), 1)
        feeds = {port: {"reagent": reagent, "ul_min": q, "volume_ul": vol, "time_s": round(vol / q * 60, 1)}} if port else {}
        vents = [lines[1]] if lines else self._vents_for(base, to_ch, product=False)
        held = bool(vents) and vents[0] in g.own_outlets(to_ch) and material in ("cells", "capsules", "beads", "droplets")
        dur = vol / q * 60
        if src is None:
            proto = {"operation": "load", "target": target, "reagent": reagent, "volume_ul": vol, "duration_s": round(dur, 1),
                     "temp_c": None, "washes": None, "flow_ul_min": q}
            title = f"Load {material} from {origin}"
            note = (f"load the {material} from {origin}: syringe on {port or '?'} at {q:g} uL/min, VENT "
                    f"{', '.join(vents) or 'none'}")
        else:
            proto = {"operation": "transfer", "source": src_ch, "target": target, "reagent": None, "volume_ul": vol,
                     "duration_s": round(dur, 1), "temp_c": None, "washes": None, "flow_ul_min": q}
            title = f"Move {material}: {src_ch} → {target}"
            note = f"move the {material} from {src_ch} to {target}: FEED {port or '?'} (carrier buffer), VENT {', '.join(vents) or 'none'}"
        proto["note"] = self._note(note + ". Every other port capped.", base)
        watch = (f"the {material} arrives in {target}" + (f" and stays there; liquid leaves at {', '.join(vents)}" if held
                 else f"; stop the pump as soon as it is there, before it runs on towards {', '.join(vents) or 'the outlet'}"))
        ph = self._phase(base, kind="move", title=title, protocol=proto, feeds=feeds, vents=vents, duration_s=dur,
                         temp_c=None, watch=watch, extra={"rate_defaulted": defaulted})
        if src is None and port:
            ph["loads_from"] = {"port": port, "chip": self.prev_chip}
        return ph

    def _collect_phase(self, st: dict, loc: str) -> dict | None:
        """The material leaves the chip: through the hand-off output (or the product outlet) into a labelled tube."""
        g, n = self.g, st["n"]
        src = g.module_chambers.get(loc) or []
        if not src:
            return None
        mod = g.modules.get(loc) or {}
        material = self._material(st, mod, "output_material")
        base = {"ir_step": n, "ir_action": "collect", "ir_text": st.get("text", ""), "module": loc, "target": src[0],
                "holds": material if material in MATERIALS else None,
                "magnet": any(w in f"{mod.get('part', '')} {loc}" for w in ("magnet", "bead")), "light": None, "issues": []}
        q, defaulted = self._rate({}, base["holds"])
        down = g.down(src)
        outs = [self.out_port] if self.out_port in g.outlets and self.out_port in down else []
        outs = outs or [p for p in sorted(down, key=lambda p: down[p]) if p in g.product_outlets]
        if not outs:
            base["issues"].append(self.check("error", f"step {n}: no product outlet downstream of {src[0]}: the {material} "
                                                      f"can't be collected from chip {self.cid}", n))
            return None
        own = [p for p in g.own_inlets(src) if p not in g.load_inlets]
        lanes = self.lane_groups.get(loc)
        if lanes:   # parallel lanes: push from upstream of the splitter so every lane empties into the pool together
            base["lanes"] = lanes
            port = self._carrier(base, loc)
        else:
            port = own[-1] if own else self._carrier(base, loc)
        vol = round(max(2 * (self._chamber_volume(src[0]) or 10.0), 10.0), 1)
        label = f"{self.cid}: {material} after step {n}"
        if self.next_chip:
            label += f", for {self.next_chip} {self.next_port}"
        base["collect_label"] = label
        feeds = {port: {"reagent": "collection_buffer", "ul_min": q, "volume_ul": vol, "time_s": round(vol / q * 60, 1)}} if port else {}
        proto = {"operation": "transfer", "source": src[0], "target": outs[0], "reagent": None, "volume_ul": vol,
                 "duration_s": round(vol / q * 60, 1), "temp_c": None, "washes": None, "flow_ul_min": q,
                 "note": self._note(f"collect the {material}: FEED {port or '?'} (buffer push), VENT {outs[0]} into a "
                                    f"tube labelled '{label}'. Every other port capped.", base)}
        watch = (f"the {material} leaves {src[0]} through {outs[0]} into the tube labelled '{label}'"
                 + ("; magnet on so the beads stay behind" if base["magnet"] else "") + "; never to waste")
        ph = self._phase(base, kind="collect", title=f"Collect the {material} ({outs[0]})", protocol=proto, feeds=feeds,
                         vents=outs[:1], duration_s=vol / q * 60, temp_c=None, watch=watch, extra={"rate_defaulted": defaulted})
        ph["collects"] = {"port": outs[0], "label": label, "material": material}
        return ph

    def plan_step(self, st: dict, mod: dict, loc: str | None, next_module: str | None,
                  lanes: list[dict] | None = None, siblings: list[str] | None = None, leak_reported: bool = False,
                  lane_index: int | None = None, fill_only: bool = False):
        n, action = st["n"], st.get("action", "other")
        op, mode, default_reagent, default_hold = ACTIONS.get(action, ACTIONS["other"])
        chambers = self.g.module_chambers.get(mod["id"], []) if self.g else []
        if self.g and not chambers:
            self.check("error", f"step {n}: module {mod['id']} has no chamber in chip spec {self.cid}; "
                                f"the step can't be placed", n)
        target = chambers[0] if chambers else snake(mod["id"])
        # what sits in the module during this step: the step's own material wins (a trap holds droplets, then capsules)
        holds = next((x for x in (st.get("input_material"), mod.get("holds"), st.get("output_material"))
                      if x in MATERIALS), None)
        if action in STREAM_ADDS and self._is_flow_module(mod, target):
            op, mode = "mix", "flow"
        cond0 = st.get("conditions") or {}
        if op == "cure" and not cond0.get("light_nm") and cond0.get("cold"):
            op, default_hold = "hold_temp", st.get("duration_s") or 1200   # a cold gelation hold, not a light cure
            self.check("warning", f"step {n} is labelled 'cure' but asks for cold and no light: run as a cold hold", n)
        reagents = [snake(r) for r in st.get("reagents", []) or []]
        reagent = reagents[0] if reagents else default_reagent
        cond = st.get("conditions") or {}
        temp = st.get("temp_c")
        if temp is None and action in DEFAULT_TEMP:
            temp = DEFAULT_TEMP[action]
        if temp is None and cond.get("cold"):
            temp = 4.0
        if temp is None and self.cp.get("temp_c") is not None and mode in ("hold", "culture"):
            temp = self.cp["temp_c"]
        base = {"ir_step": n, "ir_action": action, "ir_text": st.get("text", ""), "module": mod["id"],
                "target": target, "holds": holds, "magnet": bool(cond.get("magnet")) or action == "magnetic_separate",
                "light": None, "issues": []}
        if lanes:
            base["lanes"] = [{"module": m["id"], "chamber": self._lane_chamber(m)} for m in lanes if self._lane_chamber(m)]
        if siblings:
            base["siblings"] = list(siblings)
            base["leak_reported"] = leak_reported   # the lane cross-flow is already one error for the whole step
        if lane_index:
            base["lane_index"] = lane_index
        phases: list[dict] = []
        new_loc = mod["id"]
        if mode == "hold":
            # reagents the step needs must be in place before it: e.g. photoinitiator before a light cure
            need = list(reagents)
            if self.g and op == "cure" and not need:
                own = self.g.own_inlets(chambers or [target])
                pi = next((p for p in own if "photoinit" in p or "initiator" in p), None)
                if pi or re.search(r"photoinit", st.get("text", "") or "", re.I):
                    need = [snake(pi[:-3]) if pi and pi.endswith("_in") else "photoinitiator"]
            if need and self.g:
                phases.append(self._fill_phase({**base, "issues": []}, {**st, "repeats": 1}, "exchange", need[0], need,
                                               None, flush=1.5))
            phases.append(self._hold_phase(base, st, op, temp, default_hold, cond))
        elif mode == "culture":
            phases += self._culture_phases(base, st, reagent, temp)
        elif mode == "fill":
            hold_s = st.get("duration_s") if st.get("duration_s") is not None else default_hold
            cycling = action == "amplify" or bool(cond.get("thermal_cycles"))
            # an enzyme / lysis / RT / PCR step: a short flush (1.5 chamber volumes), then stopped flow for the incubation
            phases.append(self._fill_phase(base, st, op, reagent, reagents, temp,
                                           flush=1.5 if (hold_s or cycling) else FLUSH_VOLUMES))
            b2 = {**base, "issues": []}
            if fill_only:
                pass
            elif cycling:
                phases.append(self._cycle_phase(b2, st, cond))
            elif hold_s:
                phases.append(self._hold_phase(b2, {**st, "duration_s": hold_s}, "hold_temp",
                                               temp if temp is not None else ROOM_C, default_hold, cond,
                                               after_fill=True, assumed=st.get("duration_s") is None))
        elif mode == "load":
            phases.append(self._load_phase(base, st, reagent))
        else:   # flow / out
            ph, new_loc = self._flow_phase(base, st, op, mode, reagent, reagents, temp, loc, next_module)
            phases.append(ph)
        return phases, new_loc

    def _is_flow_module(self, mod: dict, target: str) -> bool:
        ctype = (self.g.chambers.get(target) or {}).get("type") if self.g else None
        if ctype in FLOW_CHAMBER_TYPES:
            return True
        if ctype in ("culture", "exchange", "well", "cell_wash", "qc_window"):
            return False
        return any(w in (mod.get("part") or "") for w in FLOW_PART_WORDS)

    def _rate(self, st: dict, holds: str | None) -> tuple[float, bool]:
        """(rate, defaulted): the IR's rate if given (checked against the pump range later), else a default by material."""
        lo, hi = (self.flow_range or [None, None])
        if st.get("flow_ul_min"):
            return float(st["flow_ul_min"]), False
        q = DEFAULT_FLOW.get(holds or "", DEFAULT_FLOW_OTHER)
        if lo is not None and q < lo:
            q = float(lo)
        if hi is not None and q > hi:
            q = float(hi)
        return q, True

    def _chamber_volume(self, target: str) -> float | None:
        c = (self.g.chambers.get(target) if self.g else None) or {}
        return c.get("volume_ul")

    def _module_chambers(self, base) -> list[str]:
        return (self.g.module_chambers.get(base["module"]) if self.g else None) or [base["target"]]

    # -- phase builders

    def _phase(self, base: dict, *, kind: str, title: str, protocol: dict, feeds: dict, vents: list[str],
               duration_s: float, temp_c, watch: str, wait_s: float = 0.0, extra: dict | None = None) -> dict:
        ph = dict(base)
        ph.update({"phase": kind, "title": title, "operation": protocol["operation"], "duration_s": round(float(duration_s), 1),
                   "temp_c": temp_c, "wait_s": round(float(wait_s), 1), "watch": watch, "protocol": protocol})
        ports = {}
        for pd in (self.spec or {}).get("ports", []):
            p = pd["id"]
            if p in feeds:
                ports[p] = {"state": "FEED", **feeds[p]}
            elif p in vents:
                to = ("waste" if self.g and p in self.g.waste_outlets and not base.get("collect_label") else
                      f"tube labelled '{base['collect_label']}'" if base.get("collect_label") else "waste tube")
                ports[p] = {"state": "VENT", "to": to}
            else:
                ports[p] = {"state": "CAP"}
        ph["ports"] = ports
        ph["feeds"] = [p for p in feeds if p in ports]
        ph["vents"] = [p for p in vents if p in ports]
        if extra:
            ph.update(extra)
        ph["flow_check"] = (self.flow_check(ph) if ph["feeds"] else
                            {"ok": True, "solved": False, "note": "no flow: every port capped"})
        changed = ph["flow_check"].get("rates_changed")
        if changed:   # the check lowered default rates (shear): keep the protocol, port table and timing in line
            for p, q in changed.items():
                ports[p]["ul_min"] = q
                if ports[p].get("volume_ul"):
                    ports[p]["time_s"] = round(ports[p]["volume_ul"] / q * 60, 1)
            main = ports[ph["feeds"][0]]
            if protocol.get("flow_ul_min"):
                protocol["flow_ul_min"] = main["ul_min"]
            if kind in ("fill", "load", "out", "flow") and main.get("time_s") and protocol["operation"] != "culture":
                per_wash = main["time_s"]
                ph["duration_s"] = round(per_wash, 1)
                protocol["duration_s"] = round(per_wash, 1)
        return ph

    def _note(self, text: str, base: dict) -> str:
        return f"IR step {base['ir_step']} ({base['module']}): {text}".strip()[:700]

    def _hold_phase(self, base, st, op, temp, default_hold, cond, after_fill=False, assumed=False) -> dict:
        n = base["ir_step"]
        dur = float(st.get("duration_s") or default_hold or 60)
        target = base["target"]
        light = None
        text = st.get("text", "")
        if op == "cure":
            nm = float(cond.get("light_nm") or 405)
            light = {"wavelength_nm": nm, "duration_s": dur}
            proto = {"operation": "cure", "target": target, "reagent": None, "volume_ul": None, "duration_s": dur,
                     "temp_c": temp, "washes": None, "flow_ul_min": None, "wavelength_nm": nm,
                     "note": self._note(f"cure under {nm:g} nm light for {fmt_dur(dur)}, all ports capped. {text}", base)}
            watch = f"{nm:g} nm light over {target} for {fmt_dur(dur)}; nothing flows"
            title = f"Cure ({nm:g} nm)"
            if self.equipment and not self.equipment & {"led_405nm", "uv_lamp"}:
                base["issues"].append(self.check("warning", f"step {n}: light curing needs an LED or UV lamp, which is not in "
                                                            f"the constraints' equipment list", n))
        elif op == "count":
            proto = {"operation": "count", "target": target, "reagent": None, "volume_ul": None, "duration_s": dur,
                     "temp_c": temp, "washes": None, "flow_ul_min": None,
                     "note": self._note(f"read count / viability at {target} on the microscope. {text}", base)}
            watch = f"microscope on {target}: count, viability, clumps"
            title = "Count / image"
        else:
            temp = ROOM_C if temp is None else temp
            cycles = cond.get("thermal_cycles")
            proto = {"operation": "hold_temp", "target": target, "reagent": None, "volume_ul": None,
                     "duration_s": max(dur, 1.0), "temp_c": float(temp), "washes": None, "flow_ul_min": None,
                     "note": self._note(("incubate in place: " if after_fill else "") + f"all ports capped, hold "
                                        f"{temp:g} C for {fmt_dur(dur)}" + (" (placeholder time)" if assumed else "")
                                        + (f", {cycles} thermal cycles" if cycles else "") + f". {text}", base)}
            watch = (f"temperature steady at {temp:g} C" + (f" ({cycles} thermal cycles)" if cycles else "")
                     + "; nothing moving; capped ports not leaking")
            title = "Incubate" if after_fill else "Hold"
        self._equipment_check(base, temp, n)
        return self._phase(base, kind="hold", title=title, protocol=proto, feeds={}, vents=[], duration_s=dur,
                           temp_c=temp, watch=watch, wait_s=dur, extra={"light": light})

    def _material_limit(self) -> tuple[float | None, str | None]:
        """Lowest max working temperature over the chip's layer materials (contracts/materials.json)."""
        if not hasattr(self, "_mat_limit"):
            best, name = None, None
            try:
                mats = json.loads((ROOT / "contracts" / "materials.json").read_text())["materials"]
            except (OSError, ValueError, KeyError):
                mats = {}
            for layer in (self.spec or {}).get("layers", []):
                m = str(layer.get("material", "")).lower()
                hit = next((v for k, v in mats.items() if m == k or m in [a.lower() for a in v.get("aliases", [])]), None)
                if hit and (best is None or hit["max_working_temp_c"] < best):
                    best, name = hit["max_working_temp_c"], hit.get("name", m)
            self._mat_limit = (best, name)
        return self._mat_limit

    def _zone(self, chamber: str) -> dict | None:
        return next((z for z in (self.spec or {}).get("thermal_zones", []) or [] if chamber in z.get("chambers", [])), None)

    def _cycle_phase(self, base, st, cond) -> dict:
        """PCR: an explicit thermal-cycling program (placeholder temperatures and times) on a thermocycler block or a
        peltier stage, for the chip's thermal zone or the whole chip."""
        n, target = base["ir_step"], base["target"]
        m = re.search(r"(\d+)\s*cycles", st.get("text", "") or "")
        given = int(cond.get("thermal_cycles") or (m.group(1) if m else 0) or 0)
        # No cycle number in the source: leave it to the lab (a placeholder N), never invent one. The time
        # estimate assumes a generic count but doesn't print it.
        cycles = given if given else "N"
        prog = {"placeholder": True, "initial_denature": {"temp_c": 95, "s": 120}, "cycles": given or None,
                "denature": {"temp_c": 95, "s": 15}, "anneal": {"temp_c": 60, "s": 30}, "extend": {"temp_c": 72, "s": 30},
                "final_extend": {"temp_c": 72, "s": 300}, "then_hold_c": 4}
        dur = 120 + (given or 25) * 75 + 300
        peak = 95.0
        zone = self._zone(target)
        where = f"thermal zone {zone['id']}" if zone else "the whole chip"
        device = next((d for d in ("thermocycler", "peltier_stage") if d in self.equipment), None)
        if self.equipment and not device:
            base["issues"].append(self.check("warning", f"step {n}: thermal cycling needs a thermocycler (flat block) or a "
                                                        f"peltier stage; the constraints list neither", n))
        text = (f"{cycles} cycles of {prog['denature']['temp_c']} °C {prog['denature']['s']} s / {prog['anneal']['temp_c']} °C "
                f"{prog['anneal']['s']} s / {prog['extend']['temp_c']} °C {prog['extend']['s']} s, after "
                f"{prog['initial_denature']['temp_c']} °C {prog['initial_denature']['s']} s, then {prog['final_extend']['temp_c']} "
                f"°C {prog['final_extend']['s'] // 60} min and hold {prog['then_hold_c']} °C (placeholder program)")
        proto = {"operation": "hold_temp", "target": target, "reagent": None, "volume_ul": None, "duration_s": float(dur),
                 "temp_c": peak, "washes": None, "flow_ul_min": None,
                 "note": self._note(f"thermal cycling on {device or 'a thermocycler / peltier stage'} ({where}), all ports "
                                    f"capped: {text}. {st.get('text', '')}", base)}
        self._equipment_check(base, peak, n, what="thermal cycling")
        watch = f"block runs the program on {where}; ports stay capped; no bubbles or evaporation in {target}"
        return self._phase(base, kind="hold", title=f"Thermal cycling: {cycles} cycles", protocol=proto, feeds={}, vents=[],
                           duration_s=dur, temp_c=peak, watch=watch, wait_s=dur,
                           extra={"thermal_program": prog, "thermal_zone": zone["id"] if zone else None,
                                  "device": device or "thermocycler or peltier stage", "program_text": text})

    def _equipment_check(self, base, temp, n, what: str = ""):
        if temp is None:
            return
        limit, mat = self._material_limit()
        if limit is not None and temp > limit:
            base["issues"].append(self.check("error", f"step {n}: {what + ' at ' if what else ''}{temp:g} C is above what the "
                                                      f"chip material ({mat}) can take ({limit:g} C): make this chip "
                                                      f"(or its heated zone) from a material rated for it, or move the step "
                                                      f"off chip", n))
        if not self.equipment:
            return
        if what == "thermal cycling":
            return
        if temp > 30 and not self.equipment & {"heater", "incubator_37c", "peltier_stage", "thermocycler"}:
            base["issues"].append(self.check("warning", f"step {n}: {temp:g} C needs a heater, which the constraints don't list", n))
        if temp < 15 and not self.equipment & {"cold_plate", "peltier_stage"}:
            base["issues"].append(self.check("warning", f"step {n}: {temp:g} C needs a cold plate, which the constraints don't list", n))

    def _reagent_feed(self, base, reagent: str) -> str | None:
        """The inlet for a reagent going into the target module (own named inlet > own other inlet > upstream inlet)."""
        n = base["ir_step"]
        own = self.g.own_inlets(self._module_chambers(base))
        p = match_port(reagent, own)
        if p:
            return p
        if own:
            p = own[0]
            base["issues"].append(self.check(
                "info" if p in self.g.load_inlets else "warning",
                f"step {n}: module {base['module']} has no inlet named for {reagent}; it goes in through "
                f"{p} (swap that syringe and prime the line first)", n))
            return p
        dist = self.g.up(base["target"])
        ups = [q for q in dist if q in self.g.ports and self.g.ports[q].get("type") == "inlet"]
        ups.sort(key=lambda q: (q not in self.g.load_inlets, dist[q]))
        if ups:
            p = ups[0]
            through = sorted(c for c in self.g.path_nodes([p], [base["target"]]) if c in self.g.chambers and c != base["target"])
            base["issues"].append(self.check(
                "warning", f"step {n}: module {base['module']} has no inlet of its own; {reagent} comes in through {p}"
                           + (f" and passes {', '.join(through)} on the way (they must be empty or rinsed by then)"
                              if through else ""), n))
            return p
        base["issues"].append(self.check(
            "error", f"step {n}: no inlet reaches {base['target']} for {reagent}: the chip needs an inlet on "
                     f"module {base['module']} (e.g. {snake(reagent)}_in)", n))
        return None

    def _vents_for(self, base, dest_chambers: list[str], product: bool) -> list[str]:
        """Outlet to open: the destination's own waste (or product) port, else the nearest one downstream."""
        g, n = self.g, base["ir_step"]
        own = g.own_outlets(dest_chambers)
        pref = g.product_outlets if product else g.waste_outlets
        cand = [p for p in own if p in pref]
        if not cand:
            dist = g.down(dest_chambers)
            outs = [(d, p) for p, d in dist.items() if p in g.ports and g.ports[p].get("type") == "outlet"]
            outs.sort(key=lambda dp: (dp[0], dp[1] not in pref, dp[1]))
            cand = [outs[0][1]] if outs else []
        if not cand:
            base["issues"].append(self.check("error", f"step {n}: no outlet downstream of {', '.join(dest_chambers)}: "
                                                      f"liquid fed here has nowhere to go", n))
        return cand[:1]

    def _fill_phase(self, base, st, op, reagent, reagents, temp, flush: float = FLUSH_VOLUMES) -> dict:
        target = base["target"]
        q, defaulted = self._rate(st, base["holds"])
        vol_ch = self._chamber_volume(target)
        washes = int(st.get("repeats") or 1)
        if vol_ch:
            vol = round(max(flush * vol_ch, 1.0), 1)
            vol_note = f"{flush:g} chamber volumes ({vol:g} uL) per wash" + (
                f"; the bench protocol used {st['volume_ul']:g} uL" if st.get("volume_ul") else "")
        else:
            vol = float(st.get("volume_ul") or 100.0)
            vol_note = f"{vol:g} uL per wash"
        feeds, vents = {}, []
        lanes = base.get("lanes") or []
        if self.g and lanes:
            # all lanes at once: each lane's own inlet, same rate, one shared vent beyond the pool
            lane_reagents = []
            per_lane = _index_like(st)
            for k, ln in enumerate(lanes, 1):
                own = self.g.own_inlets([ln["chamber"]])
                p = match_port(reagent, own) or (own[0] if own else None)
                if not p:
                    base["issues"].append(self.check("error", f"step {base['ir_step']}: lane {ln['module']} has no inlet "
                                                              f"of its own for {reagent}", base["ir_step"]))
                    continue
                if not match_port(reagent, [p]) and not _index_like(st):
                    base["issues"].append(self.check("info", f"step {base['ir_step']}: {reagent} goes into {ln['module']} "
                                                             f"through {p} (swap that syringe and prime the line first)",
                                                     base["ir_step"]))
                lv = round(max(flush * (self._chamber_volume(ln["chamber"]) or vol / flush), 1.0), 1)
                r = f"index_mix_{k}" if per_lane else reagent   # an index / barcode step: a different mix per lane
                feeds[p] = {"reagent": r, "ul_min": q, "volume_ul": round(lv * washes, 1),
                            "time_s": round(lv * washes / q * 60, 1), "lane": ln["module"]}
                lane_reagents.append(r)
            vents = self._vents_for(base, [ln["chamber"] for ln in lanes], product=False)
            base["lane_reagents"] = lane_reagents
            if per_lane and len(set(lane_reagents)) < len(lane_reagents):
                base["issues"].append(self.check("error", f"step {base['ir_step']}: this step needs a different reagent "
                                                          f"per lane, but lanes share {reagent}", base["ir_step"]))
            if len(feeds) > 1 and vents:
                self._balance_lanes(feeds, vents, lanes)
        elif self.g:
            if base.get("lane_index") and _index_like(st):   # one lane of an index step, filled on its own
                reagent = f"index_mix_{base['lane_index']}"
            port = self._reagent_feed(base, reagent)
            if port:
                total = round(vol * washes, 1)
                feeds[port] = {"reagent": reagent, "ul_min": q, "volume_ul": total, "time_s": round(total / q * 60, 1)}
                own = [p for p in self.g.own_inlets(self._module_chambers(base)) if p != port]
                for r in reagents[1:]:   # several reagents at once: one line each if the module has them
                    p2 = match_port(r, own)
                    if p2 and p2 not in feeds:
                        feeds[p2] = {"reagent": r, "ul_min": q, "volume_ul": total, "time_s": round(total / q * 60, 1)}
                vents = self._vents_for(base, self._module_chambers(base), product=False)
        flow_s = vol * washes / q * 60
        if lanes and feeds:
            flow_s = max(f["time_s"] for f in feeds.values())
            proto = {"operation": "exchange", "targets": [ln["chamber"] for ln in lanes if any(f.get("lane") == ln["module"]
                                                                                              for f in feeds.values())],
                     "reagents": base["lane_reagents"], "reagent": reagent, "volume_ul": vol,
                     "duration_s": round(flow_s, 1), "temp_c": temp, "washes": washes, "flow_ul_min": q}
        elif op == "mix":
            proto = {"operation": "mix", "target": target, "reagent": reagent, "volume_ul": vol, "duration_s": round(flow_s, 1),
                     "temp_c": temp, "washes": None, "flow_ul_min": q}
        else:
            proto = {"operation": "exchange", "target": target, "reagent": reagent, "volume_ul": vol,
                     "duration_s": round(flow_s, 1), "temp_c": temp, "washes": washes, "flow_ul_min": q}
            if vents and self.g and vents[0] in self.g.own_outlets([target]) and vents[0] in self.g.waste_outlets:
                proto["waste"] = vents[0]
        feed_txt = ", ".join(f"{p} ({f['reagent']}) at {f['ul_min']:g} uL/min" for p, f in feeds.items()) or "no inlet"
        proto["note"] = self._note(f"{'mix in' if op == 'mix' else 'exchange to'} {reagent}: FEED {feed_txt}, VENT "
                                   f"{', '.join(vents) or 'none'}, all else capped; {vol_note}"
                                   + (f" x {washes} washes" if washes > 1 else "")
                                   + (" (default flow)" if defaulted else "") + f". {st.get('text', '')}", base)
        watch = (f"{reagent} reaches {target}" + (f" and leaves at {', '.join(vents)}" if vents else "")
                 + "; no bubbles in the chamber" + ("; magnet on, beads stay put" if base["magnet"] else "")
                 + ("; capsules stay on the mesh" if base["holds"] == "capsules" else "")
                 + ("; cells stay in place" if base["holds"] == "cells" and st.get("action") != "dissociate" else ""))
        if lanes:
            watch = (f"start all {len(feeds)} pumps together at the same rate: each lane fills from its own inlet and "
                     f"nothing crosses the splitter; outflow leaves at {', '.join(vents) or '?'}")
        return self._phase(base, kind="fill", title=f"{'Mix' if op == 'mix' else 'Exchange'}: {reagent}"
                           + (f" x{washes}" if washes > 1 else "")
                           + (f", all {len(lanes)} lanes at once" if lanes else ""), protocol=proto, feeds=feeds, vents=vents,
                           duration_s=flow_s, temp_c=temp, watch=watch,
                           extra={"rate_defaulted": defaulted, "washes": washes})

    def _culture_phases(self, base, st, reagent, temp) -> list[dict]:
        target = base["target"]
        n = base["ir_step"]
        q, defaulted = self._rate(st, base["holds"])
        dur = float(st.get("duration_s") or 3 * 86400)
        interval = 86400.0 if dur > 86400 else max(dur / 2, 60.0)
        vol_ch = self._chamber_volume(target) or 50.0
        vol = round(max(1.5 * vol_ch, 1.0), 1)   # periodic medium change: 1.5 chamber volumes each time
        n_ex = max(1, int(dur // interval))
        feeds, vents = {}, []
        if self.g:
            port = self._reagent_feed(base, reagent)
            if port:
                feeds[port] = {"reagent": reagent, "ul_min": q, "volume_ul": round(vol * n_ex, 1),
                               "time_s": round(vol / q * 60, 1), "repeat_every_s": interval, "repeats": n_ex}
                vents = self._vents_for(base, self._module_chambers(base), product=False)
        temp = 37.0 if temp is None else temp
        proto = {"operation": "culture", "targets": [target], "reagents": [reagent], "exchange_interval_s": interval,
                 "reagent": reagent, "volume_ul": vol, "duration_s": dur, "temp_c": float(temp), "washes": None,
                 "flow_ul_min": q,
                 "note": self._note(f"culture at {temp:g} C for {fmt_dur(dur)}; every {fmt_dur(interval)} FEED "
                                    f"{', '.join(feeds) or 'no inlet'} with {reagent} ({vol:g} uL at {q:g} uL/min), VENT "
                                    f"{', '.join(vents) or 'none'}; all ports capped in between. {st.get('text', '')}", base)}
        if (st.get("conditions") or {}).get("co2") and self.equipment and not self.equipment & {"co2_supply", "incubator_37c"}:
            base["issues"].append(self.check("warning", f"step {n}: culture asks for CO2; use a CO2-independent medium or an "
                                                        f"incubator (neither is in the constraints' equipment)", n))
        self._equipment_check(base, temp, n)
        watch = (f"every {fmt_dur(interval)}: fresh {reagent} reaches {target}, old medium leaves at "
                 f"{', '.join(vents) or '?'}; check morphology / confluence on the microscope; {temp:g} C in between")
        return [self._phase(base, kind="culture", title=f"Culture {fmt_dur(dur)}, medium change every {fmt_dur(interval)}",
                            protocol=proto, feeds=feeds, vents=vents, duration_s=dur, temp_c=temp, watch=watch,
                            wait_s=max(dur - n_ex * vol / q * 60, 0.0),
                            extra={"rate_defaulted": defaulted, "repeat": {"every_s": interval, "times": n_ex}})]

    def _load_phase(self, base, st, reagent) -> dict:
        target = base["target"]
        q, defaulted = self._rate(st, base["holds"])
        vol = float(st.get("volume_ul") or round(1.5 * (self._chamber_volume(target) or 50.0), 1))
        feeds, vents = {}, []
        if self.g:
            port = self._carrier(base, None)
            if port:
                feeds[port] = {"reagent": reagent, "ul_min": q, "volume_ul": vol, "time_s": round(vol / q * 60, 1)}
                vents = self._vents_for(base, self._module_chambers(base), product=False)
        proto = {"operation": "load", "target": target, "reagent": reagent, "volume_ul": vol,
                 "duration_s": round(vol / q * 60, 1), "temp_c": st.get("temp_c"), "washes": None, "flow_ul_min": q,
                 "note": self._note(f"load {reagent}: FEED {', '.join(feeds) or 'no inlet'} at {q:g} uL/min, VENT "
                                    f"{', '.join(vents) or 'none'}. {st.get('text', '')}", base)}
        watch = f"{base['holds'] or 'sample'} arrive in {target}; stop the pump when it is full; no bubbles"
        return self._phase(base, kind="load", title=f"Load {reagent}", protocol=proto, feeds=feeds, vents=vents,
                           duration_s=vol / q * 60, temp_c=st.get("temp_c"), watch=watch, extra={"rate_defaulted": defaulted})

    def _carrier(self, base, loc: str | None) -> str | None:
        """The inlet that pushes the material: the load inlet that reaches where the material is (or the target)."""
        g, n = self.g, base["ir_step"]
        start = ((g.module_chambers.get(loc) or [None])[0] if loc else None) or base["target"]
        ups = g.up(start)
        if loc is None and self.in_port in g.inlets and self.in_port in ups:
            return self.in_port            # the material arrives through the hand-off port
        cands = [p for p in g.load_inlets if p in ups]
        if cands:
            return cands[0]
        own_t = g.own_inlets(self._module_chambers(base))
        others = [p for p in g.inlets if p in ups and p not in own_t]
        if not others and loc:
            others = [p for p in g.own_inlets(g.module_chambers.get(loc) or []) if p in ups]
        if others:
            base["issues"].append(self.check("warning", f"step {n}: no load inlet reaches {start}; {others[0]} pushes the "
                                                        f"material instead", n))
            return others[0]
        base["issues"].append(self.check("error", f"step {n}: no inlet reaches {start} to bring the material in or push it "
                                                  f"through {base['target']} (the chip needs a load inlet, e.g. inlet_1)", n))
        return None

    def _flow_phase(self, base, st, op, mode, reagent, reagents, temp, loc, next_module):
        n, target = base["ir_step"], base["target"]
        g = self.g
        q, defaulted = self._rate(st, base["holds"])
        chambers = self._module_chambers(base)
        feeds: dict[str, dict] = {}
        vents: list[str] = []
        new_loc = base["module"]
        material = st.get("input_material") or base["holds"] or "sample"
        from_outside = loc is None
        proto_extra: dict = {}
        moves_to = None
        if mode == "out" and op in ("collect", "transfer", "harvest") and g and next_module and next_module != base["module"]:
            nxt = g.module_chambers.get(next_module) or []
            if nxt and nxt[0] in g.down(chambers):   # "collect" mid-chip: the material moves on to the next module
                mode, op, moves_to = "flow", "transfer", nxt[0]
                if loc is None:
                    loc, from_outside = base["module"], False
        if g:
            carrier = self._carrier(base, None if from_outside else loc)
            carrier_reagent = snake(f"{material}_suspension") if from_outside else "carrier_buffer"
            own = [p for p in g.own_inlets(chambers) if p != carrier]
            if op == "encapsulate":
                enc = (self.spec or {}).get("encapsulation") or {}
                f_h = {"core": float(enc.get("core_flow_ul_h", ENCAP_FLOWS_UL_H["core"])),
                       "shell": float(enc.get("shell_flow_ul_h", ENCAP_FLOWS_UL_H["shell"])),
                       "oil": float(enc.get("oil_flow_ul_h", ENCAP_FLOWS_UL_H["oil"]))}
                if st.get("flow_ul_min"):
                    f_h["core"] = float(st["flow_ul_min"]) * 60
                oil_name = next((r for r in reagents if r in self.immiscible or "oil" in r), "oil")
                core_name = next((r for r in reagents if any(w in r for w in ("core", "cell"))), "core_solution")
                shell_name = next((r for r in reagents if r not in (oil_name, core_name)), "shell_polymer")
                # the cells come in through the carrier; a module line named for the core stays capped
                core_lines = [p for p in own if match_port(core_name, [p]) and not match_port(oil_name, [p])
                              and not match_port(shell_name, [p])]
                free = [p for p in own if p not in core_lines]
                oil_p = match_port(oil_name, free) or next((p for p in free if g.ports[p].get("role") == "oil"), None)
                shell_p = match_port(shell_name, [p for p in free if p != oil_p])
                unnamed = [p for p in free if p not in (oil_p, shell_p)]
                for key in ("shell", "oil"):
                    if (shell_p if key == "shell" else oil_p) is None and unnamed:
                        p = unnamed.pop(0)
                        base["issues"].append(self.check("warning", f"step {n}: assumed {p} is the {key} line of "
                                                                    f"{base['module']} (no port is named for it)", n))
                        if key == "shell":
                            shell_p = p
                        else:
                            oil_p = p
                # a core-solution line on another module of this step (e.g. a merge before the junction) joins the cells
                span_ch = [c for mid in self.span.get(n, []) for c in (g.module_chambers.get(mid) or [])]
                core_join = next((p for p in g.own_inlets(span_ch) if p != carrier and match_port(core_name, [p])), None)
                if carrier:
                    share = 0.5 if core_join else 1.0
                    feeds[carrier] = {"reagent": (snake(f"{material}_suspension") if core_join else
                                                  snake(f"{material}_in_{core_name}")) if from_outside else core_name,
                                      "ul_min": round(f_h["core"] * share / 60, 3)}
                    if core_join:
                        feeds[core_join] = {"reagent": core_name, "ul_min": round(f_h["core"] * 0.5 / 60, 3)}
                warm = bool(re.search(r"\bwarm", st.get("text", "") or "", re.I))
                for p, nm, key in ((shell_p, shell_name, "shell"), (oil_p, oil_name, "oil")):
                    if p:
                        feeds[p] = {"reagent": nm, "ul_min": round(f_h[key] / 60, 3)}
                        if key == "shell" and warm:   # "kept warm": a heated syringe and line; the cold zone is downstream
                            feeds[p]["line_temp_c"] = 37.0
                    else:
                        has = ", ".join(g.own_inlets(chambers)) or "no inlets of its own"
                        base["issues"].append(self.check(
                            "error", f"step {n}: encapsulation needs its own {key} inlet on module {base['module']} "
                                     f"(e.g. {snake(nm)}_in); the module has {has}, none free for the {key}", n))
                proto_extra = {"flows_ul_h": f_h, "capsules": DEFAULT_CAPSULES}
                q = round(sum(f["ul_min"] for f in feeds.values()), 3) or q
            else:
                if carrier:
                    feeds[carrier] = {"reagent": carrier_reagent, "ul_min": q}
                if op == "mix":
                    for r in (reagents or [reagent]):
                        p = match_port(r, own) or (own[0] if own and not any(x in own for x in feeds) else None)
                        if p and p not in feeds:
                            feeds[p] = {"reagent": r, "ul_min": q}
                    if not any(p in own for p in feeds):
                        base["issues"].append(self.check(
                            "error", f"step {n}: {reagent} must join the stream at {base['module']}, which has no inlet "
                                     f"for it (e.g. {snake(reagent)}_in)", n))
                elif op == "release":
                    p = match_port(reagent, own) or (own[0] if own else None)
                    if p:
                        feeds = {p: {"reagent": reagent, "ul_min": q}}
                    elif carrier:
                        feeds[carrier]["reagent"] = reagent
                        base["issues"].append(self.check("warning", f"step {n}: {base['module']} has no inlet for {reagent}; "
                                                                    f"it comes in through {carrier}", n))
                elif mode == "out" and not from_outside and own:
                    # push the material out with the module's own buffer line: nothing upstream is disturbed
                    feeds = {own[0]: {"reagent": "collection_buffer" if op == "collect" else "carrier_buffer", "ul_min": q}}
            # where the material goes next
            dest = None
            if mode != "out" and next_module and next_module != base["module"]:
                nxt_ch = g.module_chambers.get(next_module) or []
                if nxt_ch and nxt_ch[0] in g.down(chambers):
                    dest = nxt_ch
            if op == "split":
                # the material fans out into every chamber the splitter feeds (through junctions), not just the next one
                fan = []
                for c in chambers:
                    for nx in g.succ.get(c, []):
                        if nx in g.chambers:
                            fan += ([x for x in g.succ.get(nx, []) if x in g.chambers]
                                    if g.chambers[nx].get("type") == "junction" else [nx])
                if len(fan) > 1:
                    dest = list(dict.fromkeys(fan))
                    base["lanes"] = [{"module": g.module_of_chamber(c) or c, "chamber": c} for c in dest]
            if dest and base.get("lanes") and op == "split":
                own = [g.own_outlets([c]) for c in dest]
                if all(own):   # every lane has its own outlet: vent them all, the material stays in the lanes
                    vents = list(dict.fromkeys(next((p for p in o if p in g.waste_outlets), o[0]) for o in own))
                else:
                    vents = self._vents_for(base, dest, product=False)
                new_loc = next_module
            elif dest:
                vents = self._vents_for(base, dest, product=False)
                new_loc = next_module
            else:
                vents = self._vents_for(base, chambers, product=True)
                new_loc = base["module"] if op in ("split", "pool") else None
            sep = any(w in f"{(g.modules.get(base['module']) or {}).get('part', '')}".lower() for w in SEPARATOR_WORDS)
            if sep:   # a spiral separator: cells go on downstream, the old buffer leaves through its own waste line
                vents = list(dict.fromkeys(vents + [p for p in g.own_outlets(chambers) if p in g.waste_outlets]))
            # push 2x the volume between where the material sits and where it goes (the bench volume doesn't scale)
            src_v = self._chamber_volume((g.module_chambers.get(loc) or [""])[0]) if loc and loc != base["module"] else 0
            dst_v = self._chamber_volume(dest[0]) if dest else 0
            vol = round(max(2 * ((src_v or 0) + (self._chamber_volume(target) or 0) + (dst_v or 0)), 10.0), 1)
            minutes = float(st.get("duration_s") or 1800) / 60.0
            for p, f in feeds.items():
                f["volume_ul"] = round(f["ul_min"] * minutes if op == "encapsulate" else vol * f["ul_min"] / max(q, 1e-9), 1)
                f["time_s"] = round(f["volume_ul"] / f["ul_min"] * 60, 1) if f["ul_min"] else 0.0
        dur = float(st.get("duration_s") or max((f.get("time_s", 0) for f in feeds.values()), default=0.0) or 60.0)
        src_ch = (g.module_chambers.get(loc) or [None])[0] if (g and loc) else None
        stop_at = new_loc if (new_loc and new_loc != base["module"]) else None
        feed_txt = ", ".join(f"{p} ({f['reagent']}) at {f['ul_min']:g} uL/min" for p, f in feeds.items()) or "none"
        total_vol = round(sum(float(f.get("volume_ul") or 0) for f in feeds.values()), 1)
        common = {"reagent": None, "volume_ul": total_vol or None, "duration_s": round(dur, 1), "temp_c": temp,
                  "washes": None, "flow_ul_min": round(q, 3) if q else None}
        if op == "encapsulate":
            proto = {"operation": "encapsulate", "target": target, **common, **proto_extra}
        elif op == "mix":
            proto = {"operation": "mix", "target": target, **common, "reagent": reagent}
        elif op == "transfer":
            if moves_to:
                srcc, tgt = target, moves_to
            elif mode == "out":
                srcc, tgt = target, (vents[0] if vents else target)
            else:
                srcc, tgt = (src_ch if src_ch and src_ch != target else None), target
            if srcc is None:
                proto = {"operation": "load", "target": target, **common, "reagent": snake(carrier_reagent if g else reagent),
                         "volume_ul": float(st.get("volume_ul") or 20.0)}
            else:
                proto = {"operation": "transfer", "source": srcc, "target": tgt, **common}
        elif op == "split":
            fans_out = bool(base.get("lanes"))   # the splitter itself feeds the lanes: the material leaves it
            proto = {"operation": "split", "target": target if fans_out or not src_ch else src_ch, **common}
        elif op == "pool":
            proto = {"operation": "pool", "target": target, **common}
        elif op == "release":
            proto = {"operation": "release", "target": target, **common, "reagent": reagent}
        elif op in ("collect", "harvest"):
            proto = {"operation": op, "targets": [target], **common, "reagent": reagent if op == "harvest" else None}
        else:
            proto = {"operation": op, "target": target, **common}
        proto["note"] = self._note(f"{op}: FEED {feed_txt}, VENT {', '.join(vents) or 'none'}, all else capped. "
                                   f"{st.get('text', '')}", base)
        if op == "encapsulate":
            watch = (f"steady droplet break-up at {target} (microscope); droplets travel on towards "
                     f"{', '.join(vents) or '?'}; no oil backing up into the core line")
            hot = [f"{p} ({f['reagent']})" for p, f in feeds.items() if f.get("line_temp_c")]
            if hot:
                watch += (f"; keep {', '.join(hot)} warm ({feeds[hot[0].split(' ')[0]]['line_temp_c']:g} C syringe heater) "
                          f"up to the chip; the cold zone starts downstream")
        elif mode == "out":
            watch = f"material leaves through {', '.join(vents) or '?'} into a collection tube; stop when {target} is clear"
        else:
            start = (f"material moves out of {target}" if src_ch == target else
                     f"material moves from {src_ch or 'the inlet'} through {target}")
            watch = start + (f" towards {', '.join(vents)}" if vents else "") + "; streams merge evenly; no clogging"
        # residence in a delay line / mixer the stream runs through: its volume over the TOTAL flow (all merged streams)
        q_total = sum(float(f.get("ul_min") or 0) for f in feeds.values())
        if g and q_total > 0:
            through = [target] + [c for c in (dest or []) if c != target]
            for c in [c for c in through if g.chambers.get(c, {}).get("type") == "mixer"]:
                v = self._chamber_volume(c)
                if v:
                    watch += f"; residence in {c} ≈ {fmt_dur(v / q_total * 60)} ({v:g} uL at the total {q_total:g} uL/min)"
        if op == "split" and base.get("lanes"):
            watch += f"; stop the pumps once the material is spread over {', '.join(ln['chamber'] for ln in base['lanes'])}"
        elif stop_at:
            watch += f"; stop the pumps once the material has reached {stop_at} (its next step)"
        title = {"encapsulate": "Encapsulate (co-flow)", "mix": f"Mix {reagent} into the stream",
                 "transfer": f"Move on to {moves_to}" if moves_to else "Pass through",
                 "split": "Split", "pool": "Pool", "release": f"Release with {reagent}", "collect": "Collect",
                 "harvest": "Harvest"}.get(op, op)
        self._equipment_check(base, temp, n)
        leaves = bool(g) and new_loc is None and bool(vents) and vents[0] not in g.waste_outlets
        if leaves:   # the material leaves the chip here: into a labelled tube (or the next chip), never to waste
            material = self._material(st, None, "output_material")
            label = f"{self.cid}: {material} after step {n}" + (f", for {self.next_chip} {self.next_port}"
                                                               if self.next_chip else "")
            base["collect_label"] = label
            watch += f"; collect into the tube labelled '{label}'"
        ph = self._phase(base, kind="flow" if mode == "flow" else "out", title=title, protocol=proto, feeds=feeds,
                         vents=vents, duration_s=dur, temp_c=temp, watch=watch, extra={"rate_defaulted": defaulted})
        if leaves:
            ph["collects"] = {"port": vents[0], "label": base["collect_label"], "material": material}
        return ph, new_loc

    # -- flow check

    def flow_check(self, ph: dict) -> dict:
        n = ph["ir_step"]
        res: dict = {"ok": True, "solved": False}
        if not self.net or not self.g:
            return {"ok": False, "solved": False, "note": "no chip spec to solve"}
        feeds = {p: float(ph["ports"][p]["ul_min"]) for p in ph["feeds"]}
        vents = list(ph["vents"])
        issues: list[dict] = []

        def add(sev, msg):
            issues.append({"severity": sev, "message": msg})
            ph["issues"].append(self.check(sev, f"step {n} ({ph['title']}): {msg}", n))

        if self.pump_channels and len(feeds) > self.pump_channels:
            add("error", f"{len(feeds)} feeds at once ({', '.join(feeds)}) but only {self.pump_channels} pump lines")
        lo, hi = (self.flow_range or [None, None])
        for p, q in feeds.items():
            if lo is not None and q < lo:
                add("error", f"{p} at {q:g} uL/min is below the pump's {lo:g} uL/min minimum")
            if hi is not None and q > hi:
                add("error", f"{p} at {q:g} uL/min is above the pump's {hi:g} uL/min maximum")
        if not vents:
            add("error", "nothing is vented: the fed liquid has nowhere to go")
            res.update(ok=False, issues=issues)
            return res
        nw = _network_module()
        states = {p: (nw.FEED, q) for p, q in feeds.items()}
        states.update({p: nw.VENT for p in vents})
        try:
            sol = self.net.solve(states)
        except Exception as e:
            add("error", f"the flow solver found no steady flow for these port states ({e})")
            res.update(ok=False, issues=issues)
            return res
        holds = ph.get("holds")
        limit = CHAMBER_SHEAR_LIMIT.get(holds or "")
        tgt_shear = sol["chambers"].get(ph["target"], {}).get("shear_dyn_cm2", 0.0)
        if limit and tgt_shear > limit and ph.get("rate_defaulted") and ph["phase"] in ("fill", "load", "culture", "out"):
            f = 0.9 * limit / tgt_shear
            new = {p: round(max(q * f, lo or 0.01), 3) for p, q in feeds.items()}
            states.update({p: (nw.FEED, q) for p, q in new.items()})
            sol = self.net.solve(states)
            res["rates_changed"] = new
            add("info", f"default rate lowered from {', '.join(f'{q:g}' for q in feeds.values())} to "
                        f"{', '.join(f'{q:g}' for q in new.values())} uL/min to keep shear in {ph['target']} under "
                        f"{limit:g} dyn/cm2")
            feeds = new
            tgt_shear = sol["chambers"].get(ph["target"], {}).get("shear_dyn_cm2", 0.0)
        res["solved"] = True
        total = sum(feeds.values())
        thru = sol["chambers"].get(ph["target"], {}).get("throughput_ul_min", 0.0)
        res["through_target_ul_min"] = round(thru, 4)
        res["total_feed_ul_min"] = round(total, 4)
        if thru <= 1e-6:
            add("error", f"no flow passes through {ph['target']}: these port states bypass the target")
        elif thru < 0.5 * total and not ph.get("lanes"):
            add("warning", f"only {100 * thru / total:.0f}% of the feed passes through {ph['target']}")
        # flow where it shouldn't be
        path = self.g.path_nodes(list(feeds), vents)
        up_of_target = set(self.g.up(ph["target"])) - {ph["target"]}
        lanes = [ln["chamber"] for ln in ph.get("lanes") or []]
        siblings = list(ph.get("siblings") or [])
        if lanes:   # several lanes fed at once: each must get flow, and (for a fill) mostly its own reagent
            mix = (sol.get("routing") or {}).get("inflow_mix", {})
            own_port = {f.get("lane"): p for p, f in ph["ports"].items() if f.get("state") == "FEED" and f.get("lane")}
            dry = [c for c in lanes if sol["chambers"].get(c, {}).get("throughput_ul_min", 0) <= 1e-6]
            if dry:
                add("error", f"no flow reaches lane(s) {', '.join(dry)}")
            if ph["phase"] in ("fill", "culture") and not ph.get("share"):
                for ln in ph.get("lanes") or []:
                    p = own_port.get(ln["module"])
                    frac = (mix.get(ln["chamber"]) or {}).get(p, 0.0) if p else 0.0
                    if p and frac < 0.95:
                        add("error", f"lane {ln['chamber']} gets only {100 * frac:.0f}% of its own reagent from {p}; "
                                     f"the rest crosses over from the other lanes")
                res["lane_purity"] = {ln["chamber"]: round((mix.get(ln["chamber"]) or {}).get(own_port.get(ln["module"]), 0.0), 4)
                                      for ln in ph.get("lanes") or [] if own_port.get(ln["module"])}
        if siblings:   # one lane filled on its own: its siblings must stay untouched
            sib_q = {c: sol["chambers"].get(c, {}).get("throughput_ul_min", 0.0) for c in siblings}
            res["sibling_flow_ul_min"] = round(sum(sib_q.values()), 5)
            wet = [c for c, v in sib_q.items() if v > 0.01 * max(total, 1e-9)]
            if wet:
                add("info" if ph.get("leak_reported") else "error",
                    f"filling {ph['target']} also flushes sibling lane(s) {', '.join(wet)} "
                    f"({100 * sum(sib_q.values()) / max(total, 1e-9):.0f}% of the feed): cross-contamination")
        # with lanes, a little liquid can cross between them through the pool / splitter that joins them
        stray_floor = 0.05 if (lanes or siblings) else 0.01
        stray, passed = [], []
        for c, row in sol["chambers"].items():
            if c == ph["target"] or c in lanes or c in siblings or ph.get("share"):
                continue   # (sharing medium: crossing the junction into the other chambers is the point)
            if row["throughput_ul_min"] <= 0.01 * max(total, 1e-9):
                continue
            if c not in path and row["throughput_ul_min"] <= stray_floor * max(total, 1e-9):
                add("info", f"{100 * row['throughput_ul_min'] / max(total, 1e-9):.1f}% of the feed crosses {c} "
                            f"between the lanes")
                continue
            mod = self.g.module_of_chamber(c)
            holds_c = (self.g.modules.get(mod) or {}).get("holds") if mod else None
            if c not in path:
                sev = "error" if (c in up_of_target and holds_c not in (None, "none", "liquid")) else "warning"
                if siblings and ph.get("leak_reported"):
                    sev = "info"
                stray.append(c)
                add(sev, f"liquid flows into {c}" + (f" (module {mod}, holds {holds_c})" if holds_c else "")
                         + ", off the intended path: cap the port that feeds it or change the vent")
            else:
                passed.append(c)
        ups = [c for c in passed if c in up_of_target]
        downs = [c for c in passed if c not in up_of_target]
        if ups and ph["phase"] == "fill":
            add("warning", f"the reagent passes upstream chamber(s) {', '.join(ups)} before reaching {ph['target']}")
        mod_here = ph.get("module")
        part_here = (self.g.modules.get(mod_here) or {}).get("part")
        parallel = [c for c in downs if (m := self.g.module_of_chamber(c)) and m != mod_here
                    and ph["ir_step"] in ((self.g.modules.get(m) or {}).get("steps") or [])
                    and (self.g.modules.get(m) or {}).get("part") == part_here]
        if parallel:
            add("warning", f"the outflow of {ph['target']} runs through {', '.join(parallel)}, which do the same step on "
                           f"other material (parallel lanes laid out in series): give each lane its own outlet")
        if downs:
            add("info", f"the outflow passes {', '.join(downs)} on its way to {', '.join(vents)}")
        res["passes"] = passed
        res["stray"] = stray
        p_kpa = sol["max_pressure_mbar"] / 10.0
        res["max_pressure_kpa"] = round(p_kpa, 4)
        if self.max_kpa:
            if p_kpa > self.max_kpa:
                add("error", f"{p_kpa:.2f} kPa on chip exceeds the {self.max_kpa:g} kPa limit")
            elif p_kpa > 0.8 * self.max_kpa:
                add("warning", f"{p_kpa:.2f} kPa on chip is within 20% of the {self.max_kpa:g} kPa limit")
        res["target_shear_dyn_cm2"] = round(tgt_shear, 5)
        if limit and tgt_shear > limit:
            add("warning", f"shear in {ph['target']} is {tgt_shear:.2f} dyn/cm2, above the {limit:g} dyn/cm2 "
                           f"comfort limit for {holds}; lower the rate")
        if holds == "cells":
            ends = {ch["id"]: (ch["from"], ch["to"]) for ch in self.spec.get("channels", [])}
            worst = max(((r["shear_dyn_cm2"], cid) for cid, r in sol["channels"].items()
                         if all(e in path for e in ends.get(cid, ()))), default=(0.0, None))
            res["max_channel_shear_dyn_cm2"] = round(worst[0], 4)
            if worst[0] > CHANNEL_SHEAR_LIMIT_CELLS:
                add("warning", f"shear in channel {worst[1]} is {worst[0]:.1f} dyn/cm2 where cells pass "
                               f"(> {CHANNEL_SHEAR_LIMIT_CELLS:g}); lower the rate or widen the channel")
        foreign = ((sol.get("routing") or {}).get("foreign_medium") or {}).get(ph["target"])
        if foreign and ph["phase"] == "fill":
            add("warning", f"liquid from {', '.join(foreign)} reaches {ph['target']} during the exchange")
        res["port_flow_ul_min"] = {p: r["into_chip_ul_min"] for p, r in sol["ports"].items()
                                   if abs(r["into_chip_ul_min"]) > 1e-9}
        res["issues"] = issues
        res["ok"] = not any(i["severity"] == "error" for i in issues)
        return res

    # -- summaries

    def syringes(self, steps: list[dict]) -> list[dict]:
        """One row per (port, reagent) in order of first use, with the total volume to load."""
        rows: dict[tuple, dict] = {}
        for ph in steps:
            for p, st in ph["ports"].items():
                if st["state"] != "FEED":
                    continue
                r = rows.setdefault((p, st.get("reagent")), {"port": p, "reagent": st.get("reagent"), "volume_ul": 0.0,
                                                             "steps": [], "first_step": ph["n"]})
                r["volume_ul"] += float(st.get("volume_ul") or 0)
                r["steps"].append(ph["n"])
        out = []
        for r in rows.values():
            r["load_ul"] = round(max(r["volume_ul"] * SYRINGE_MARGIN, 10.0), 0)
            r["volume_ul"] = round(r["volume_ul"], 1)
            size = next((s for s in SYRINGES_ML if s * 1000 >= r["load_ul"]), None)
            r["syringe_ml"] = size
            if size is None:
                self.check("error", f"{r['port']} needs {r['load_ul'] / 1000:.1f} mL of {r['reagent']} over steps "
                                    f"{', '.join(map(str, r['steps']))}: more than a {SYRINGES_ML[-1]} mL syringe holds; "
                                    f"refill between steps or use a reservoir")
            out.append(r)
        return out

    def equipment_needed(self, steps: list[dict]) -> list[str]:
        need = []
        n_feeds = max((len(ph["feeds"]) for ph in steps), default=0)
        if n_feeds:
            need.append(f"syringe pump with {n_feeds} independent line{'s' if n_feeds > 1 else ''}")
        temps = sorted({ph["temp_c"] for ph in steps if ph.get("temp_c") is not None})
        hot = [t for t in temps if t > 30]
        cold = [t for t in temps if t < 15]
        if hot:
            need.append(f"heater / stage-top incubator ({', '.join(f'{t:g}' for t in hot)} C)")
        if cold:
            need.append(f"cold plate ({', '.join(f'{t:g}' for t in cold)} C)")
        nms = sorted({ph["light"]["wavelength_nm"] for ph in steps if ph.get("light")})
        if nms:
            need.append(f"LED / lamp at {', '.join(f'{x:g}' for x in nms)} nm")
        cyc = [ph for ph in steps if ph.get("thermal_program")]
        if cyc:
            need.append(f"{cyc[0]['device']} for thermal cycling ({cyc[0]['thermal_zone'] or 'whole chip'})")
        warm = sorted({(f.get("reagent"), f["line_temp_c"]) for ph in steps for f in ph["ports"].values()
                       if f.get("line_temp_c")})
        for r, tc in warm:
            need.append(f"syringe heater / warm jacket on the {r} syringe and line ({tc:g} C)")
        if any(ph.get("magnet") for ph in steps):
            need.append("magnet under the chip")
        if any(ph["operation"] in ("count", "encapsulate", "culture") for ph in steps):
            need.append("microscope")
        need.append("tubing for every used port, plugs for capped ports, waste and collection tubes")
        return need


# --- outputs ---------------------------------------------------------------------------------------


def port_summary(ph: dict) -> str:
    feeds = [f"{p} {f['ul_min']:g} uL/min ({f.get('reagent')})" for p, f in ph["ports"].items() if f["state"] == "FEED"]
    vents = [p for p, f in ph["ports"].items() if f["state"] == "VENT"]
    if not feeds and not vents:
        return "all capped"
    return "FEED " + "; ".join(feeds) + " | VENT " + (", ".join(vents) or "none") + " | rest capped"


def _md(s) -> str:
    return str(s if s is not None else "").replace("|", "/").replace("\n", " ")


def write_run_sheet(path: Path, cp: dict, program: dict, checks: list[dict], spec: dict | None) -> None:
    L = [f"# Run sheet: {cp.get('name') or cp['id']} (`{cp['id']}`)", ""]
    L.append(f"**Purpose:** {cp.get('purpose', '')}  ")
    L.append(f"**Total time:** {fmt_dur(program['total_duration_s'])}  ")
    L.append("**Status:** generated; placeholder values: check flows, volumes and times before a real run.")
    L += ["", "Port states: **FEED** = a syringe pump pushes into that port; **VENT** = open outlet into a tube; "
              "**CAP** = plugged. Ports not named in a step are capped.", "",
          "## Setup", "", "### Ports and syringes", "",
          "| Port | Type | Connect | Load (uL) | Syringe | Used in steps |", "|---|---|---|---|---|---|"]
    syr: dict[str, list[dict]] = {}
    for r in program["syringes"]:
        syr.setdefault(r["port"], []).append(r)
    vent_use: dict[str, list[int]] = {}
    vent_to: dict[str, set] = {}
    for ph in program["steps"]:
        for p in ph["vents"]:
            vent_use.setdefault(p, []).append(ph["n"])
            vent_to.setdefault(p, set()).add(ph["ports"][p].get("to") or "waste tube")
    for p in (spec or {}).get("ports", []):
        pid = p["id"]
        if pid in syr:
            rs = syr[pid]
            what = "syringe: " + ", then ".join(f"{r['reagent']} (from step {r['first_step']})" for r in rs)
            if len(rs) > 1:
                what += "; swap the syringe and prime the line between reagents"
            loads = " / ".join(f"{r['load_ul']:g}" for r in rs)
            sizes = " / ".join(f"{r['syringe_ml']:g} mL" if r.get("syringe_ml") else "too big: refill" for r in rs)
            used = ", ".join(str(s) for r in rs for s in r["steps"])
            L.append(f"| `{pid}` | {p.get('type')} | {_md(what)} | {loads} | {sizes} | {used} |")
        elif pid in vent_use:
            dest = "; ".join(sorted(vent_to.get(pid) or {"waste tube"}))
            L.append(f"| `{pid}` | {p.get('type')} | tubing to {_md(dest)} | | | {', '.join(map(str, vent_use[pid]))} |")
        else:
            L.append(f"| `{pid}` | {p.get('type')} | plug (not used) | | | |")
    L += ["", "### Equipment", ""] + [f"- {e}" for e in program["equipment"]]
    L += ["", "Before step 1: fill every syringe, prime each feed line up to its port (no air), connect the outlet "
              "tubing, plug every port, then open only the ports step 1 needs.", "",
          "## Steps", "", "| # | IR step | Start | Module | What | Port states | Volume | Time | Temp | Watch |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for ph in program["steps"]:
        vol = sum(float(f.get("volume_ul") or 0) for f in ph["ports"].values() if f["state"] == "FEED")
        flag = " (!)" if any(i["severity"] == "error" for i in ph["issues"]) else ""
        extra = []
        if ph.get("light"):
            extra.append(f"light {ph['light']['wavelength_nm']:g} nm")
        if ph.get("magnet"):
            extra.append("magnet on")
        if ph.get("repeat"):
            extra.append(f"exchange every {fmt_dur(ph['repeat']['every_s'])}")
        what = ph["title"] + (f" ({', '.join(extra)})" if extra else "")
        temp = "" if ph.get("temp_c") is None else f"{ph['temp_c']:g} C"
        L.append(f"| {ph['n']}{flag} | {ph['ir_step']} | {fmt_time(ph['t_start_s'])} | {ph['module']} | {_md(what)} | "
                 f"{_md(port_summary(ph))} | {f'{vol:g} uL' if vol else ''} | {fmt_dur(ph['duration_s'])} | {temp} | "
                 f"{_md(ph['watch'])} |")
    L += ["", "## Flow check (operator/network.py)", "",
          "| # | Through target (uL/min) | Max pressure (kPa) | Target shear (dyn/cm2) | Result |", "|---|---|---|---|---|"]
    for ph in program["steps"]:
        fc = ph["flow_check"]
        if not fc.get("solved"):
            why = fc.get("note") or "; ".join(i["message"] for i in fc.get("issues", [])) or "not solved"
            L.append(f"| {ph['n']} | | | | {'ERROR: ' if not fc.get('ok') else ''}{_md(why)} |")
            continue
        msgs = [i["message"] for i in fc.get("issues", []) if i["severity"] != "info"]
        L.append(f"| {ph['n']} | {fc['through_target_ul_min']:g} | {fc['max_pressure_kpa']:.3g} | "
                 f"{fc['target_shear_dyn_cm2']:.3g} | {'OK' if fc['ok'] else 'ERROR'}"
                 f"{(': ' + _md('; '.join(msgs))) if msgs else ''} |")
    tw = program.get("twin_run")
    L += ["", "## Digital twin replay", ""]
    if not tw:
        L.append("No twin run was made (no chip spec, or the protocol did not validate).")
    elif not tw.get("ok"):
        L.append(f"The operator's planner could not turn this protocol into a twin run: {tw.get('error')}")
    elif tw.get("in_repo"):
        L.append(f"`runs/{cp['id']}.twin_run.json` ({tw['events']} events) stores the chip as the repo-relative path "
                 f"`{tw['chip_spec']}`, so the twin can load and replay it.")
    else:
        L.append(f"**The twin can't load this run:** the output folder is outside the repo, so "
                 f"`runs/{cp['id']}.twin_run.json` stores the chip as an absolute path "
                 f"(`{tw.get('chip_spec') or 'none'}`). Re-run with `--out` inside the repo (e.g. the default "
                 f"`autochip/out/<name>`) to get a run the twin can replay.")
    L += ["", "## Checks", ""]
    if not checks:
        L.append("No problems found.")
    for c in sorted(checks, key=lambda c: SEVERITY_ORDER.index(c["severity"]) if c["severity"] in SEVERITY_ORDER else 9):
        L.append(f"- **{c['severity']}**: {c['message']}")
    path.write_text(units("\n".join(L) + "\n"))


def _handoff_port(spec: dict | None, name: str | None, kind: str) -> str:
    """The port a hand-off leaves from (kind outlet) or arrives at (kind inlet)."""
    if not spec:
        return f"{kind}_1"
    ports = {p["id"]: p for p in spec.get("ports", [])}
    if name and name in ports:
        return name
    for cand in ((f"{snake(name)}_{'out' if kind == 'outlet' else 'in'}" if name else None), f"{kind}_1"):
        if cand and cand in ports and ports[cand].get("type") == kind:
            return cand
    typed = [p for p, d in ports.items() if d.get("type") == kind]
    roles = ("harvest", "release", "transfer") if kind == "outlet" else ("load", "cells", "transfer")
    pref = [p for p in typed if ports[p].get("role") in roles]
    return (pref or typed or [f"{kind}_1"])[0]


def pipeline_sequence(plan: dict, ir: dict, programs: dict[str, dict], specs: dict[str, dict]) -> list[dict]:
    """Every IR step in order, with hand-offs between chips, as rows of {kind, ...} (report.py renders these too)."""
    by_n = {s["n"]: s for s in ir.get("steps", [])}
    cov = {int(k): v for k, v in (plan.get("coverage") or {}).items() if str(k).isdigit()}
    chip_steps: dict[str, list[int]] = {}
    for c in plan.get("chips", []):
        ns = {int(x) for x in c.get("steps", []) or []}
        for m in c.get("modules", []):
            ns |= {int(x) for x in m.get("steps", []) or []}
        for n in ns:
            mid = next((m["id"] for m in c.get("modules", []) if n in (m.get("steps") or [])), None)
            if mid:
                cov.setdefault(n, f"{c['id']}.{mid}")
        chip_steps[c["id"]] = sorted(ns | {k for k, v in cov.items() if isinstance(v, str) and v.startswith(c["id"] + ".")})
    off_grp = {int(n): g for g in plan.get("off_chip", []) for n in g.get("steps", [])}
    miss = {int(n): g for g in plan.get("missing_parts", []) for n in g.get("steps", [])}
    for n in off_grp:
        cov.setdefault(n, "off_chip")
    for n in miss:
        cov.setdefault(n, "missing")
    phases_by_ir: dict[tuple, list[dict]] = {}
    for cid, prog in programs.items():
        for ph in prog.get("steps", []):
            phases_by_ir.setdefault((cid, ph["ir_step"]), []).append(ph)
    before: dict[int, list[dict]] = {}
    after: dict[int, list[dict]] = {}
    tail: list[dict] = []
    for h in plan.get("handoffs", []):
        a, b = h["from_chip"], h["to_chip"]
        b_steps = chip_steps.get(b) or []
        b_first = min(b_steps) if b_steps else None
        a_steps = [n for n in chip_steps.get(a, []) if b_first is None or n < b_first] or chip_steps.get(a, [])
        a_last = max(a_steps) if a_steps else None
        out_port = _handoff_port(specs.get(a), h.get("from_output"), "outlet")
        in_port = _handoff_port(specs.get(b), h.get("to_input"), "inlet")
        via = sorted(int(x) for x in h.get("via_steps", []) or [])
        row = {"kind": "handoff", "from_chip": a, "to_chip": b, "from_port": out_port, "to_port": in_port,
               "carries": h.get("carries"), "method": h.get("method"), "via_steps": via}
        direct = h.get("method") in ("tubing", "chip_to_chip_connector") and not via
        if direct:
            row["text"] = (f"Hand-off {a} → {b} ({h.get('carries')}): move the {out_port} tubing of {a} to {in_port} of {b}"
                           + (" with the chip-to-chip connector" if h.get("method") == "chip_to_chip_connector" else "")
                           + "; keep every other port of both chips capped.")
            if b_first is not None:
                before.setdefault(b_first, []).append(row)
            else:
                tail.append(row)
        else:
            how = "pipette it out" if h.get("method") == "manual_pipette" else "collect it"
            out_row = {**row, "phase": "out",
                       "text": f"Hand-off out of {a} ({h.get('carries')}): {how} from {out_port} of {a} into a tube"
                               + (f", then do bench step(s) {', '.join(map(str, via))}" if via else "") + "."}
            in_row = {**row, "phase": "in",
                      "text": f"Hand-off into {b} ({h.get('carries')}): load the tube into the syringe on {in_port} of {b} "
                              f"(the sample line for {b}'s first step)."}
            (after.setdefault(a_last, []) if a_last is not None else tail).append(out_row)
            (before.setdefault(b_first, []) if b_first is not None else tail).append(in_row)
    rows: list[dict] = []
    for n in sorted(set(by_n) | set(cov)):
        rows += before.get(n, [])
        st = by_n.get(n, {"n": n, "text": "(not in the protocol IR)", "action": "other"})
        where = cov.get(n)
        base = {"n": n, "text": st.get("text"), "action": st.get("action")}
        if where is None:
            rows.append({"kind": "unassigned", **base})
        elif where == "off_chip":
            g = off_grp.get(n, {})
            rows.append({"kind": "off_chip", **base, "why": g.get("why"),
                         "equipment": g.get("equipment") or st.get("equipment") or []})
        elif where == "missing":
            g = miss.get(n, {})
            rows.append({"kind": "missing", **base, "need": g.get("need"), "why": g.get("why"),
                         "workaround": g.get("workaround")})
        else:
            cid, _, mid = where.partition(".")
            phs = phases_by_ir.get((cid, n), [])
            rows.append({"kind": "on_chip", **base, "chip": cid, "module": mid,
                         "phases": [{"n": p["n"], "title": p["title"], "ports": port_summary(p),
                                     "duration_s": p["duration_s"], "temp_c": p.get("temp_c"), "watch": p["watch"],
                                     "light": p.get("light"), "magnet": p.get("magnet"),
                                     "errors": [i["message"] for i in p["issues"] if i["severity"] == "error"]}
                                    for p in phs]})
        rows += after.get(n, [])
    return rows + tail


def write_pipeline_sheet(path: Path, plan: dict, ir: dict, rows: list[dict], programs: dict, all_checks: dict) -> None:
    L = [f"# Lab run sheet: {ir.get('name') or ir.get('id')}", ""]
    if ir.get("goal"):
        L += [f"**Goal:** {ir['goal']}  "]
    L.append("**Chips:** " + ", ".join(f"{c['id']} ({c.get('name', '')})" for c in plan.get("chips", [])) + "  ")
    L.append(f"**Steps:** {len(ir.get('steps', []))}: {sum(1 for r in rows if r['kind'] == 'on_chip')} on chip, "
             f"{sum(1 for r in rows if r['kind'] == 'off_chip')} at the bench, "
             f"{sum(1 for r in rows if r['kind'] == 'missing')} waiting for a missing part  ")
    L.append("**Status:** generated; values the source did not give are placeholders.")
    L += ["", "Each chip has its own sheet (`<chip>.run_sheet.md`) with the syringe set-up and the full flow check. "
              "FEED = the pump pushes into the port, VENT = open to a tube; every port not named is capped.", "",
          "## Before you start", ""]
    for c in plan.get("chips", []):
        prog = programs.get(c["id"])
        if not prog or not prog.get("syringes"):
            L.append(f"- **{c['id']}**: nothing to set up (no chip spec or no flow steps; see its checks).")
            continue
        syr = ", ".join(f"{r['port']} ← {r['reagent']} ({r['load_ul']:g} uL)" for r in prog["syringes"])
        L.append(f"- **{c['id']}** ({c.get('name', '')}): syringes {syr}. Equipment: {'; '.join(prog['equipment'])}.")
    L += ["", "## Protocol", ""]
    for r in rows:
        if r["kind"] == "handoff":
            L += [f"> **{r['text']}**", ""]
        elif r["kind"] == "on_chip":
            L.append(f"**Step {r['n']}** · chip **{r['chip']}**, module `{r['module']}`: {r['text']}")
            for p in r["phases"]:
                bits = [p["ports"], fmt_dur(p["duration_s"])]
                if p.get("temp_c") is not None:
                    bits.append(f"{p['temp_c']:g} C")
                if p.get("light"):
                    bits.append(f"light {p['light']['wavelength_nm']:g} nm")
                if p.get("magnet"):
                    bits.append("magnet on")
                L.append(f"  - {r['chip']} #{p['n']} {p['title']}: {'; '.join(b for b in bits if b)}. Watch: {p['watch']}.")
                for e in p["errors"]:
                    L.append(f"  - **(!) fix before the run:** {e}")
            if not r["phases"]:
                L.append("  - **(!)** no run step was generated for this (see the chip's checks).")
            L.append("")
        elif r["kind"] == "off_chip":
            eq = f" Equipment: {', '.join(r['equipment'])}." if r.get("equipment") else ""
            why = f" (Off chip because: {r['why']})" if r.get("why") else ""
            L += [f"**Step {r['n']}** · at the bench: {r['text']}{eq}{why}", ""]
        elif r["kind"] == "missing":
            L.append(f"**Step {r['n']}** · **(!) missing part** ({r.get('need') or 'no part'}): {r['text']}")
            if r.get("workaround"):
                L.append(f"  - Workaround for now: {r['workaround']}")
            elif r.get("why"):
                L.append(f"  - Why: {r['why']}")
            L.append("")
        else:
            L += [f"**Step {r['n']}** · **(!)** not assigned to a chip, the bench or a missing part: {r['text']}", ""]
    L += ["## Open problems", ""]
    errs = [(cid, c) for cid, cs in all_checks.items() for c in cs if c["severity"] == "error"]
    open_rows = [r for r in rows if r["kind"] in ("missing", "unassigned")]
    if not errs and not open_rows:
        L.append("None found.")
    for cid, c in errs:
        L.append(f"- **{cid}**: {c['message']}")
    for r in open_rows:
        L.append(f"- step {r['n']}: " + (f"missing part: {r.get('need')}" + (f" (workaround: {r['workaround']})"
                                                                              if r.get("workaround") else "")
                                         if r["kind"] == "missing" else "not assigned anywhere"))
    path.write_text(units("\n".join(L) + "\n"))


def try_twin_run(protocol: dict, spec: dict, spec_path: Path | None, out_path: Path) -> dict:
    """Run the operator's deterministic planner (operator/planner.py) on the protocol + chip and write the run file
    the twin replays. Never raises."""
    op_dir = str(ROOT / "operator")
    added = op_dir not in sys.path
    if added:
        sys.path.insert(0, op_dir)
    try:
        import planner  # operator/planner.py
        adapter = planner.plan(protocol, spec)
        try:
            chip_spec = planner.repo_relative(spec_path) if spec_path else ""
            in_repo = bool(chip_spec)
        except Exception:
            chip_spec, in_repo = str(spec_path or ""), False
        header = {"protocol_id": protocol["id"], "protocol_name": protocol["name"], "chip_id": spec["id"],
                  "chip_spec": chip_spec, "operator": "planner",
                  "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                  "placeholder": True}
        run_obj = adapter.write(out_path, header)
        return {"ok": True, "path": str(out_path), "events": len(run_obj["events"]), "in_repo": in_repo,
                "chip_spec": chip_spec}
    except Exception as e:
        msg = str(e).strip().splitlines()
        return {"ok": False, "error": f"{type(e).__name__}: {msg[0] if msg else ''}"[:400]}
    finally:
        if added and op_dir in sys.path:
            sys.path.remove(op_dir)


# --- entry point -----------------------------------------------------------------------------------


def run(plan, ir, chips, constraints, out_dir):
    out_dir = Path(out_dir)
    runs_dir = out_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    # a re-run into the same folder: drop run files of chips that are no longer in the plan
    current = {cp["id"] for cp in plan.get("chips", [])}
    for suffix in (".protocol.json", ".run.json", ".run_sheet.md", ".twin_run.json"):
        for p in runs_dir.glob(f"*{suffix}"):
            if p.name[:-len(suffix)] not in current and "." not in p.name[:-len(suffix)]:
                p.unlink()
    proto_v = _validator("protocol")
    results: dict[str, dict] = {}
    programs: dict[str, dict] = {}
    specs: dict[str, dict] = {}
    for cp in plan.get("chips", []):
        cid = cp["id"]
        entry = (chips or {}).get(cid) or {}
        spec = entry.get("spec") if isinstance(entry, dict) else None
        cpl = ChipPlanner(cp, spec, ir, constraints, plan)
        if not spec:
            cpl.check("error", f"chip {cid} has no chip spec from the layout stage: port states and flow checks skipped")
        hin = next((h for h in plan.get("handoffs", []) if h.get("to_chip") == cid), None)
        hout = next((h for h in plan.get("handoffs", []) if h.get("from_chip") == cid), None)
        if hin:
            cpl.in_port, cpl.prev_chip = _handoff_port(spec, hin.get("to_input"), "inlet"), hin["from_chip"]
        if hout:
            nxt_spec = ((chips or {}).get(hout["to_chip"]) or {}).get("spec")
            cpl.out_port, cpl.next_chip = _handoff_port(spec, hout.get("from_output"), "outlet"), hout["to_chip"]
            cpl.next_port = _handoff_port(nxt_spec, hout.get("to_input"), "inlet")
        protocol, program = cpl.build()
        specs[cid] = spec or {}
        if spec and program["steps"]:
            if hin and not any((ph["ports"].get(cpl.in_port) or {}).get("state") == "FEED" for ph in program["steps"]):
                cpl.check("error", f"the hand-off from {hin['from_chip']} arrives at {cpl.in_port}, but no step of {cid} "
                                   f"loads it: the sample never gets onto this chip")
            if hout and not any((ph.get("collects") or {}).get("port") == cpl.out_port for ph in program["steps"]):
                cpl.check("error", f"the hand-off to {hout['to_chip']} leaves through {cpl.out_port}, but no step of {cid} "
                                   f"sends the material out there")
            if not hout and not cpl.collected:
                cpl.check("error", f"chip {cid} never collects its product into a tube: it would end up in waste or "
                                   f"stay on the chip")
        errs = sorted(proto_v.iter_errors(protocol), key=lambda e: list(e.absolute_path))
        for e in errs[:10]:
            cpl.check("error", f"run protocol does not match protocol.schema.json at "
                               f"{'/'.join(map(str, e.absolute_path)) or '(top)'}: {e.message[:200]}")
        files = {}
        p_proto = runs_dir / f"{cid}.protocol.json"
        p_proto.write_text(json.dumps(protocol, indent=2) + "\n")
        files["protocol"] = str(p_proto)
        spec_path = out_dir / "chips" / f"{cid}.json"
        if spec and not errs:
            tw = try_twin_run(protocol, spec, spec_path if spec_path.exists() else None, runs_dir / f"{cid}.twin_run.json")
            program["twin_run"] = tw
            if tw["ok"]:
                files["twin_run"] = tw["path"]
                cpl.check("info", f"the operator's planner (operator/planner.py) accepts this protocol: {tw['events']} "
                                  f"events in runs/{cid}.twin_run.json"
                                  + (f", chip stored as {tw['chip_spec']}" if tw.get("in_repo") else
                                     " (the output folder is outside the repo, so the chip path is absolute and the "
                                     "twin can't load this run)"))
            else:
                blocked = "safety blocked" in tw["error"]
                known = blocked and any("can take" in c["message"] for c in cpl.checks if c["severity"] == "error")
                cpl.check("info" if known else "error" if blocked else "warning",
                          ("the operator's safety check blocks this protocol (so there is no twin run): " if blocked else
                           "the operator's planner could not turn this protocol into a twin run: ") + tw["error"])
        program["protocol"] = _rel(p_proto, out_dir)
        program["chip_spec"] = _rel(spec_path, out_dir) if spec_path.exists() else None
        program["checks"] = cpl.checks
        p_prog = runs_dir / f"{cid}.run.json"
        p_prog.write_text(json.dumps(program, indent=2) + "\n")
        files["run"] = str(p_prog)
        p_sheet = runs_dir / f"{cid}.run_sheet.md"
        write_run_sheet(p_sheet, cp, program, cpl.checks, spec)
        files["run_sheet"] = str(p_sheet)
        programs[cid] = program
        results[cid] = {"protocol": protocol, "checks": cpl.checks, "files": files, "program": program}
    rows = pipeline_sequence(plan, ir, programs, specs)
    p_seq = runs_dir / "pipeline_run.json"
    p_seq.write_text(json.dumps({"kind": "pipeline_run_sequence", "protocol_id": ir.get("id"), "rows": rows}, indent=2) + "\n")
    p_pipe = runs_dir / "pipeline_run_sheet.md"
    write_pipeline_sheet(p_pipe, plan, ir, rows, programs, {cid: r["checks"] for cid, r in results.items()})
    for r in results.values():
        r["files"]["pipeline_run_sheet"] = str(p_pipe)
        r["files"]["pipeline_run"] = str(p_seq)
    return results

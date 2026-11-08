"""S9 verify: every generated chip and protocol through the repo's real checks, plus the
cross-chip checks no single-chip tool can do.

    python assay2chip/stages/s9_verify.py --id my_assay
    python assay2chip/stages/s9_verify.py --id my_assay --figures    # also write the SVGs

Writes `assay2chip/work/<id>/verify.json`. A chip this pipeline makes must pass exactly
the checks a hand-designed chip passes (assay2chip/README.md), so this stage does not
invent a verification path: it runs the four mature tools and parses what they say.

  tools/validate.py           contracts, schemas, cross-file ids, pipelines
  tools/check_chip_spec.py    design rules at real scale, per chip + its protocol
  cad/make_figures.py         planar collisions - in one layer a crossing is a short circuit
  operator/run.py             the protocol actually plans into commands on that chip

SCOPE OF THE VERDICT. `tools/check_chip_spec.py` reports 60 port-overlap errors on main's
hand-made chips (a rule added after they were drawn). Those are not ours and this stage
must not pass or fail on them, so every finding carries a `scope`: 'pipeline' for a chip
in partition.json, 'preexisting' for anything else. Only pipeline-scope blockers fail the
stage. The pre-existing count is reported, never hidden and never counted against us.

Two side effects, both deliberate:

  * `cad/make_figures.py` writes SVGs into cad/figures/. By default this stage calls its
    `geometry_warnings()` in process instead, which produces the same ERROR/WARN lines
    with no files written, so a test run leaves nothing behind. `--figures` runs the
    whole tool and writes the drawings, which is what a real run wants.
  * `operator/run.py` writes run files. They go to `assay2chip/work/<id>/runs/` via its
    own `runs_dir` argument, NOT to the repo's `runs/`, so this stage cannot invalidate
    `runs/index.json` and `tools/validate.py` stays at zero errors.

THE CROSS-CHIP CHECKS (nothing else in the repo does these)
-----------------------------------------------------------
Every existing tool checks one chip in isolation. The audit's worst findings were all at
the joins, so this stage adds three checks that span chips:

  1. `handoff_particle_width` / `handoff_particle_depth` - particle size against channel
     width and depth on BOTH sides of every hand-off. Both sides, because a line that
     leaves through a 400 um channel and arrives in a 150 um one is fine on the chip that
     made it and blocked on the chip that receives it, and each chip passes its own check.
     Width limit: `capsule_channel.width_factor_low` x particle. Depth limit: the particle
     diameter itself - a 260 um sphere cannot cross a 150 um deep channel at any width.
  2. `volume_continuity` - what one chip delivers against what the next one draws. The
     audit found Module 3 releasing 76.9 uL into Module 4 drawing 9.6 uL with no
     concentration step documented anywhere. A deficit is a blocker (the step cannot run);
     a surplus needs a documented concentration step and is reported with both numbers.
  3. `capsule_diameter_disagreement` - ONE capsule diameter across the whole run. This
     repo once held four coexisting values, which quietly invalidated every trap gap and
     channel width derived from them. Compared across contracts/parameters.json, every
     op in unitops.json, every hand-off's `particle_um` (swollen) and every generated
     chip spec's own `capsule` block.

A finding's shape is `assay2chip/report/findings.py`, shared with S10 so the report
renders both with one function.

RE-RUN REQUESTS. When a blocker's fix is upstream, the finding carries `rerun_from` and
the stage returns the earliest one (see assay2chip/stages/__init__.py). Earliest, because
re-running from s6 also re-runs s7 and s8, and fixing geometry before re-routing is the
only order that can work. S9 never edits geometry itself.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io as _io
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "operator") not in sys.path:
    sys.path.insert(0, str(ROOT / "operator"))

from assay2chip.lib import contracts, io as stage_io  # noqa: E402
from assay2chip.report import findings as F  # noqa: E402

VALIDATE = ROOT / "tools" / "validate.py"
CHECK_CHIP_SPEC = ROOT / "tools" / "check_chip_spec.py"
MAKE_FIGURES = ROOT / "cad" / "make_figures.py"
OPERATOR_RUN = ROOT / "operator" / "run.py"

TOOL_TIMEOUT_S = 300

# Only paths OUTSIDE assay2chip/work/<id>/ need declaring (assay2chip/stages/__init__.py).
# verify.json and the generated run files are inside it, so the one declaration is the
# drawings - and they are written only with --figures.
WRITES = ["cad/figures/{chip_id}_layout.svg", "cad/figures/{chip_id}_stack.svg",
          "cad/figures/{chip_id}_features.svg"]


def writes(ctx: Any = None) -> list[str]:
    """Nothing outside the work directory unless the drawings were asked for."""
    return list(WRITES) if getattr(ctx, "figures", False) else []

# Severity for a tool's own prefix. A tool ERROR is a blocker by definition: the repo's
# CI fails on it, so a chip carrying one is not buildable.
SEVERITY_OF = {"ERROR": "blocker", "WARN": "medium", "TODO": "low", "OK": None}


class VerifyError(RuntimeError):
    """S9 could not run at all (not: S9 found problems - that is a verdict, not an error)."""


# --- the rule table: tool line -> structured finding ---------------------------------


@dataclass(frozen=True)
class Rule:
    """One recognised tool message.

    `pattern` must name its groups: `obs` and `lim` are the two numbers that disagree,
    `feat` the feature the message is about. Everything else is prose.
    """

    pattern: str
    rule: str
    observed: str
    limit: str
    units: str = ""
    rerun_from: str | None = None
    what: str = ""
    severity: str | None = None       # override the tool's prefix (rarely needed)
    regex: re.Pattern = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "regex", re.compile(self.pattern))


# S6 sizes and places parts; S7 routes; S8 writes the protocol from the built geometry.
# `rerun_from` names the earliest stage that can actually fix the finding.
RULES: tuple[Rule, ...] = (
    # --- tools/check_chip_spec.py, retention and particle fit ------------------------
    Rule(r"trap\.gap_um (?P<obs>[\d.]+) um >= 0\.4 x capsule diameter \((?P<lim>[\d.]+) um\)",
         "trap_gap", "trap.gap_um", "0.4 x capsule diameter", "um", "s6",
         "the trap gap lets capsules through"),
    Rule(r"chamber '(?P<feat>[^']+)': [\w ]*opening (?P<obs>[\d.]+) um >= 0\.4 x capsule "
         r"diameter \((?P<lim>[\d.]+) um\)",
         "retention_opening", "retention opening", "0.4 x capsule diameter", "um", "s6",
         "the retention feature lets capsules through"),
    Rule(r"chamber '(?P<feat>[^']+)': mesh_pore_um (?P<obs>[\d.]+) >= 0\.4 x capsule "
         r"diameter \((?P<lim>[\d.]+) um\)",
         "retention_opening", "mesh pore", "0.4 x capsule diameter", "um", "s6",
         "the mesh pore lets capsules through"),
    Rule(r"channel '(?P<feat>[^']+)' carries capsules but is (?P<obs>[\d.]+) um wide, "
         r"< 1\.5 x capsule diameter \((?P<lim>[\d.]+) um\)",
         "capsule_channel_width", "channel width", "1.5 x capsule diameter", "um", "s6",
         "a capsule-carrying channel is narrow enough to clog"),
    Rule(r"channel '(?P<feat>[^']+)' carries cells but is (?P<obs>[\d.]+) um wide, "
         r"< (?P<lim>[\d.]+) um",
         "cell_channel_width", "channel width", "cell outlet minimum", "um", "s6",
         "a cell-carrying channel is narrower than a cell clump"),
    Rule(r"outlet '(?P<feat>[^']+)' passes capsules but is (?P<obs>[\d.]+) um wide, "
         r"< 1\.5 x capsule diameter \((?P<lim>[\d.]+) um\)",
         "capsule_outlet_width", "outlet width", "1.5 x capsule diameter", "um", "s6",
         "a capsule outlet is narrow enough to clog"),
    Rule(r"chamber '(?P<feat>[^']+)' is (?P<obs>[\d.]+) um high but culture happens there"
         r".*swollen capsules are (?P<lim>[\d.]+) um",
         "culture_chamber_height", "chamber height", "swollen capsule diameter", "um", "s6",
         "a culture chamber is shallower than a swollen capsule"),
    Rule(r"channel '(?P<feat>[^']+)' is (?P<obs>[\d.]+) um deep, shallower than a "
         r"(?P<lim>[\d.]+) um capsule",
         "channel_depth_vs_capsule", "channel depth", "capsule diameter", "um", "s6",
         "a channel is shallower than the capsule it carries"),
    # --- cell filter against the nozzle, including across a hand-off -----------------
    Rule(r"cell_filter\.gap_um (?P<obs>[\d.]+) >= encapsulation nozzle (?P<lim>[\d.]+) um",
         "cell_filter_vs_nozzle", "cell filter gap", "encapsulation nozzle", "um", "s6",
         "clumps that pass the filter can block the nozzle"),
    Rule(r"filter '(?P<feat>[^']+)' gap (?P<obs>[\d.]+) um >= encapsulation nozzle "
         r"(?P<lim>[\d.]+) um",
         "cell_filter_vs_nozzle", "filter gap", "encapsulation nozzle", "um", "s6",
         "clumps that pass the filter can block the nozzle"),
    Rule(r"filter '(?P<feat>[^']+)' gap (?P<obs>[\d.]+) um >= the (?P<lim>[\d.]+) um nozzle of "
         r"\S+ downstream",
         "cell_filter_vs_downstream_nozzle", "filter gap", "downstream nozzle", "um", "s4",
         "clumps that pass this filter can block the next chip's nozzle"),
    # --- temperature, envelope, floors ----------------------------------------------
    Rule(r"thermal zone '(?P<feat>[^']+)' reaches (?P<obs>[\d.]+) C, above .*max working "
         r"temperature \((?P<lim>[\d.]+) C\)",
         "temperature_vs_material", "zone temperature", "material maximum", "C", "s4",
         "a thermal zone is hotter than the chip material allows"),
    Rule(r"step (?P<feat>\d+) holds '[^']*' at (?P<obs>[\d.]+) C, above .*max working "
         r"temperature \((?P<lim>[\d.]+) C\)",
         "temperature_vs_material", "hold temperature", "material maximum", "C", "s8",
         "a protocol hold is hotter than the chip material allows"),
    Rule(r"footprint (?P<feat>[xy]) (?P<obs>[\d.]+) mm > build envelope \S+ (?P<lim>[\d.]+) mm",
         "envelope", "footprint", "build envelope", "mm", "s6",
         "the chip is bigger than the build envelope"),
    Rule(r"chamber '(?P<feat>[^']+)' is (?P<obs>[\d.]+) mm deep, > max depth (?P<lim>[\d.]+) mm",
         "max_depth", "chamber depth", "max etch depth", "mm", "s6",
         "a chamber is deeper than the single etch depth"),
    Rule(r"channel '(?P<feat>[^']+)' is (?P<obs>[\d.]+) mm deep, > max depth (?P<lim>[\d.]+) mm",
         "max_depth", "channel depth", "max etch depth", "mm", "s6",
         "a channel is deeper than the single etch depth"),
    Rule(r"channel '(?P<feat>[^']+)' is (?P<obs>[\d.]+) mm wide, below the (?P<lim>[\d.]+) mm "
         r"minimum open channel",
         "min_open_channel", "channel width", "min open channel", "mm", "s6",
         "a channel is below the printable in-plane floor"),
    Rule(r"(?P<obs>\d+) ports > max (?P<lim>[\d.]+) ports per chip",
         "port_budget", "ports", "max ports per chip", "ports", "s4",
         "the chip needs more ports than the envelope allows: the assay must be split"),
    Rule(r"ports '(?P<feat>[^']+)' and '[^']+' are (?P<obs>[\d.]+) mm apart, closer than the "
         r"(?P<lim>[\d.]+) mm minimum port pitch",
         "port_pitch", "port spacing", "min port pitch", "mm", "s7",
         "two ports are closer than the drill pitch"),
    Rule(r"port overlap: '(?P<feat>[^']+)' (?:[\d.]+) mm hole opens onto (?P<obs>[\d.]+) mm2 "
         r"of cut channel/chamber \(needs >= (?P<lim>[\d.]+) mm2",
         "port_overlap", "cut area under the hole", "needed cut area", "mm2", "s7",
         "a port hole does not open onto enough cut channel: it needs a port pad"),
    Rule(r"(?:chamber|channel) '(?P<feat>[^']+)' declares block '[^']+', which is not in the "
         r"CAD block library",
         "unknown_block", "", "", "", "s6",
         "a part names a block that is not in contracts/blocks.json"),
    Rule(r"(?P<feat>\S+) is (?P<obs>[\d.]+) um, below the fabrication minimum \((?P<lim>[\d.]+) um\)",
         "below_fabrication_minimum", "feature", "fabrication minimum", "um", None,
         "a feature is smaller than we can make ourselves (fine for the design)"),
    # TODO lines name material grades and feature counts. They carry no two numbers that
    # disagree, so they get rules of their own: the generic fallback would otherwise
    # report 'TOPAS 5013 / 6013' as a measurement against a limit.
    Rule(r"compatibility TODO: (?P<feat>.+?) with ",
         "material_compatibility_unknown", "", "", "", None,
         "nobody has checked this material against these reagents"),
    Rule(r"fabrication TODO: (?P<obs>\d+) fabricated features can't be checked",
         "fabrication_limits_unknown", "features unchecked", "", "", None,
         "cad/fabrication.json has no measured kerf or minimum feature yet"),
    Rule(r"not fabricable here \(by design\): (?P<feat>.+)",
         "not_fabricable_here", "", "", "", None,
         "these parts are deliberately bought, not printed"),
    Rule(r"material '(?P<feat>[^']+)' is not in contracts/materials\.json",
         "material_unknown", "", "", "", "s6",
         "the chip names a material with no temperature or compatibility data"),
    Rule(r"barcode collisions: about (?P<obs>[\d.]+)% of the ~(?P<feat>[\d,]+) cells",
         "barcode_collisions", "cells sharing a barcode", "collision warning threshold",
         "%", "s4",
         "too many cells share a barcode for the data to be single-cell"),
    # --- cad/make_figures.py, planar collisions -------------------------------------
    Rule(r"channels '(?P<feat>[^']+)' and '[^']+' overlap in plan view \((?P<obs>[\d.]+) mm of "
         r"overlap\)",
         "channel_collision", "overlap", "minimum gap", "mm", "s7",
         "two channels touch in plan view: in one layer that is one channel"),
    Rule(r"channels '(?P<feat>[^']+)' and '[^']+' overlap by (?P<obs>[\d.]+) mm where they leave",
         "channel_collision", "overlap", "minimum gap", "mm", "s7",
         "two channels overlap where they leave a shared feature"),
    Rule(r"channels '(?P<feat>[^']+)' and '[^']+' cross at \([^)]*\)",
         "channel_crossing", "", "", "", "s7",
         "two channel centrelines cross: in one layer that is a short circuit"),
    Rule(r"port '(?P<feat>[^']+)' and channel '[^']+' overlap by (?P<obs>[\d.]+) mm",
         "port_on_channel", "overlap", "minimum gap", "mm", "s7",
         "a port is drilled into a channel it is not connected to"),
    Rule(r"chambers '(?P<feat>[^']+)' and '[^']+' overlap in plan view",
         "chamber_collision", "", "", "", "s7",
         "two chambers overlap: one chamber, not two"),
    Rule(r"channel '(?P<feat>[^']+)' and chamber '[^']+' overlap by (?P<obs>[\d.]+) mm",
         "channel_through_chamber", "overlap", "minimum gap", "mm", "s7",
         "a channel runs through a chamber it is not connected to"),
    Rule(r"port '(?P<feat>[^']+)' and chamber '[^']+' overlap: the port is drilled into a chamber",
         "port_in_chamber", "", "", "", "s7",
         "a port is drilled into a chamber it is not connected to"),
    Rule(r"port '(?P<feat>[^']+)' at \([^)]*\) is not fully inside the (?P<lim>[\d.]+) x [\d.]+ mm "
         r"board",
         "port_off_board", "", "board x", "mm", "s7",
         "a port is not fully inside the board"),
    Rule(r"(?:channels|chambers|ports) '(?P<feat>[^']+)' and [^ ]+ are (?P<obs>[\d.]+) mm apart, "
         r"a thinner rib than (?P<lim>[\d.]+) mm, UNDER the [\d.]+ mm minimum wall",
         "thin_wall", "clearance", "thin-rib threshold", "mm", "s7",
         "two features leave a rib under the minimum wall", "high"),
    Rule(r"(?:channel|chamber|port)s? '(?P<feat>[^']+)' and [^ ]+ are (?P<obs>[\d.]+) mm apart, "
         r"a thinner rib than (?P<lim>[\d.]+) mm",
         "thin_rib", "clearance", "thin-rib threshold", "mm", "s7",
         "two features are closer than the thin-rib threshold"),
    # --- tools/validate.py ----------------------------------------------------------
    Rule(r"channel '(?P<feat>[^']+)' (?:from|to) '[^']+' is not a chamber or port",
         "dangling_channel", "", "", "", "s7",
         "a channel ends somewhere that does not exist"),
    Rule(r"step (?P<feat>\d+): target '[^']+' not in chip",
         "protocol_target_missing", "", "", "", "s8",
         "a protocol step names something the chip does not have"),
    Rule(r"id '(?P<feat>[^']+)' is used by both",
         "duplicate_id", "", "", "", "s6",
         "two parts share an id"),
    Rule(r"(?P<feat>\S+) wash\(es\) x [\d.]+ uL at [\d.]+ uL/min needs at least (?P<obs>[\d.]+) s, "
         r"but duration_s is (?P<lim>[\d.]+)",
         "wash_duration", "time needed", "duration_s", "s", "s8",
         "a wash step is shorter than the volume it moves needs"),
)

_NUMBER = re.compile(r"(-?\d+(?:\.\d+)?)\s*(um|mm2|mm|uL|ul|C|s|%)?")


def classify(line: str, *, source: str, chip: str | None, scope: str) -> dict:
    """One tool line -> one finding. Unrecognised lines still carry their two numbers."""
    prefix, _, rest = line.strip().partition("  ")
    prefix = prefix.strip()
    severity = SEVERITY_OF.get(prefix, "medium")
    text = rest.strip() or line.strip()
    if severity is None:
        raise ValueError(f"{prefix} lines are not findings: {line!r}")

    for rule in RULES:
        m = rule.regex.search(text)
        if not m:
            continue
        groups = m.groupdict()
        return F.finding(
            rule.severity or severity,
            source,
            rule.rule,
            what=rule.what,
            chip=chip,
            feature=groups.get("feat"),
            observed=F.number(rule.observed, _float(groups.get("obs")), rule.units),
            limit=F.number(rule.limit, _float(groups.get("lim")), rule.units),
            rerun_from=rule.rerun_from if (rule.severity or severity) == "blocker" else None,
            scope=scope,
            message=text,
        )

    numbers = [(float(v), u or "") for v, u in _NUMBER.findall(text)][:2]
    quoted = re.findall(r"'([^']+)'", text)
    return F.finding(
        severity,
        source,
        "unclassified",
        what=text.split(":")[0][:140],
        chip=chip,
        feature=quoted[0] if quoted else None,
        observed=F.number("first number in the message", *numbers[0]) if numbers else None,
        limit=F.number("second number in the message", *numbers[1]) if len(numbers) > 1 else None,
        rerun_from=None,
        scope=scope,
        message=text,
    )


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --- running the tools ---------------------------------------------------------------


@dataclass
class ToolRun:
    """One tool invocation, kept whole so a verdict can always be traced to its output."""

    tool: str
    argv: list[str]
    ran: bool = False
    exit_code: int | None = None
    stdout: str = ""
    seconds: float = 0.0
    skipped: str | None = None

    def summary(self) -> dict:
        return {
            "tool": self.tool,
            "argv": self.argv,
            "ran": self.ran,
            "exit_code": self.exit_code,
            "seconds": round(self.seconds, 2),
            "skipped": self.skipped,
            "lines": self.stdout.count("\n"),
        }


def _shell(tool: str, argv: Sequence[str], timeout: int = TOOL_TIMEOUT_S) -> ToolRun:
    run = ToolRun(tool, [str(a) for a in argv])
    script = ROOT / tool
    if not script.exists():
        run.skipped = f"{tool} is not in this checkout"
        return run
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, str(script), *[str(a) for a in argv]],
            cwd=ROOT, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        run.seconds = time.monotonic() - started
        run.skipped = f"{tool} did not finish within {timeout} s"
        return run
    run.ran = True
    run.exit_code = proc.returncode
    run.stdout = proc.stdout + (("\n" + proc.stderr) if proc.stderr.strip() else "")
    run.seconds = time.monotonic() - started
    return run


def _load_module(name: str, path: Path):
    """Import a repo script under a private name, so sys.modules['run'] is never clobbered."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise VerifyError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def finding_lines(stdout: str) -> list[str]:
    """The ERROR / WARN / TODO lines of a tool's output, in order."""
    return [
        line.strip() for line in stdout.splitlines()
        if line.strip().split("  ")[0].strip() in ("ERROR", "WARN", "TODO")
    ]


def run_validate(our_chips: set[str], protocol_of: dict[str, str]) -> tuple[ToolRun, list[dict]]:
    """tools/validate.py over the whole repo, each line attributed to a chip and a scope.

    It has no per-file mode, so the output covers main's files too. Attribution is by the
    path or the chip id in the line; anything that names none of our chips is
    'preexisting' and cannot fail this stage.
    """
    run = _shell("tools/validate.py", [])
    rows = []
    for line in finding_lines(run.stdout):
        chip = _chip_in(line, our_chips, protocol_of)
        rows.append(classify(
            line, source="tools/validate.py", chip=chip,
            scope=F.PIPELINE if chip in our_chips else F.PREEXISTING,
        ))
    return run, rows


def _chip_in(line: str, our_chips: set[str], protocol_of: dict[str, str]) -> str | None:
    """Which chip a validate.py line is about: its path, a protocol we wrote, or an id."""
    for match in re.finditer(r"chip/([a-z0-9_]+)\.json", line):
        if match.group(1) in our_chips:
            return match.group(1)
    for match in re.finditer(r"protocols/([a-z0-9_]+)\.json", line):
        if match.group(1) in protocol_of:
            return protocol_of[match.group(1)]
    for chip_id in sorted(our_chips, key=len, reverse=True):
        if re.search(rf"\b{re.escape(chip_id)}\b", line):
            return chip_id
    return None


def run_check_chip_spec(chip_id: str, chip_path: Path, protocol_path: Path | None
                        ) -> tuple[ToolRun, list[dict]]:
    """tools/check_chip_spec.py on ONE chip, which is what keeps main's 60 port-overlap
    errors out of our verdict: the tool is never asked about main's chips."""
    argv: list[Any] = [chip_path.relative_to(ROOT).as_posix()]
    if protocol_path is not None:
        argv += ["--protocol", protocol_path.relative_to(ROOT).as_posix()]
    run = _shell("tools/check_chip_spec.py", argv)
    rows = [
        classify(line, source="tools/check_chip_spec.py", chip=chip_id, scope=F.PIPELINE)
        for line in finding_lines(run.stdout)
    ]
    return run, rows


def run_collisions(chip_id: str, chip: dict, write_figures: bool = False
                   ) -> tuple[ToolRun, list[dict]]:
    """cad/make_figures.py's planar collision check.

    In process by default (`geometry_warnings`), which gives the identical ERROR/WARN
    lines without writing SVGs - so verifying a chip has no side effects. With
    `write_figures` the whole tool runs and the drawings land in cad/figures/.
    """
    if write_figures:
        run = _shell("cad/make_figures.py", ["--chip", f"chip/{chip_id}.json", "--all"])
        lines = finding_lines(run.stdout)
    else:
        run = ToolRun("cad/make_figures.py", ["geometry_warnings(chip)  # in process"])
        started = time.monotonic()
        try:
            figures = _load_module("_a2c_make_figures", MAKE_FIGURES)
            messages, tightest = figures.geometry_warnings(chip)
        except Exception as error:                      # a chip too broken to draw
            run.seconds = time.monotonic() - started
            run.skipped = f"cad/make_figures.py could not read the chip: {error}"
            return run, [F.finding(
                "blocker", "cad/make_figures.py", "undrawable", chip=chip_id,
                what="the chip cannot be drawn, so its collisions cannot be checked",
                rerun_from="s6", message=f"{type(error).__name__}: {error}",
            )]
        run.ran = True
        run.seconds = time.monotonic() - started
        run.exit_code = 1 if any(m.startswith("ERROR") for m in messages) else 0
        run.stdout = "\n".join(messages) + (
            f"\ntightest clearance {tightest[0]:.2f} mm between {tightest[1]}"
            if tightest[0] != float("inf") else ""
        )
        lines = finding_lines(run.stdout)
    rows = [
        classify(line, source="cad/make_figures.py", chip=chip_id, scope=F.PIPELINE)
        for line in lines
    ]
    return run, rows


def run_operator(chip_id: str, chip: dict, protocol: dict, chip_path: Path,
                 protocol_path: Path, runs_dir: Path) -> tuple[ToolRun, list[dict]]:
    """operator/run.py: does this protocol actually plan into commands on this chip?

    Called in process with `runs_dir` pointing into the work directory, so no run file
    lands in the repo's runs/ and runs/index.json stays correct.
    """
    run = ToolRun("operator/run.py", [
        "--protocol", protocol_path.relative_to(ROOT).as_posix(),
        "--chip", chip_path.relative_to(ROOT).as_posix(),
        f"# runs_dir={runs_dir.relative_to(ROOT).as_posix()}",
    ])
    started = time.monotonic()
    try:
        planner = _load_module("_a2c_planner", ROOT / "operator" / "planner.py")
        operator_run = _load_module("_a2c_operator_run", OPERATOR_RUN)
    except Exception as error:
        run.skipped = f"operator/run.py is not importable here: {error}"
        return run, []

    problems = planner.check_inputs(protocol, chip)
    rows = [
        classify(f"ERROR  {problem}", source="operator/run.py", chip=chip_id, scope=F.PIPELINE)
        for problem in problems
    ]
    if problems:
        run.ran = True
        run.exit_code = 1
        run.seconds = time.monotonic() - started
        run.stdout = "\n".join(f"ERROR  {p}" for p in problems)
        return run, rows

    out = _io.StringIO()
    runs_dir.mkdir(parents=True, exist_ok=True)
    try:
        with contextlib.redirect_stdout(out):
            written = operator_run.generate(protocol_path, chip_path, runs_dir=runs_dir)
    except Exception as error:
        run.ran = True
        run.exit_code = 1
        run.seconds = time.monotonic() - started
        run.stdout = out.getvalue() + f"\nERROR  {type(error).__name__}: {error}"
        rows.append(F.finding(
            "blocker", "operator/run.py", "protocol_does_not_plan", chip=chip_id,
            what="the protocol cannot be turned into commands on this chip",
            rerun_from="s8", message=f"{type(error).__name__}: {error}",
        ))
        return run, rows

    run.ran = True
    run.exit_code = 0
    run.seconds = time.monotonic() - started
    run.stdout = out.getvalue()
    for path in written:
        rows += _run_file_findings(chip_id, Path(path))
    return run, rows


def _run_file_findings(chip_id: str, path: Path) -> list[dict]:
    """A generated run file with no events, or with skipped steps, is worth saying."""
    try:
        run_file = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        return [F.finding("blocker", "operator/run.py", "run_file_unreadable", chip=chip_id,
                          what="operator/run.py wrote a run file that cannot be read",
                          rerun_from="s8", message=str(error))]
    rows = []
    events = run_file.get("events") or []
    if not events:
        rows.append(F.finding(
            "blocker", "operator/run.py", "protocol_plans_nothing", chip=chip_id,
            what="the protocol planned into zero commands, so the chip does nothing",
            observed=F.number("events", 0), limit=F.number("events", 1), rerun_from="s8",
            message=f"{path.name} has no events",
        ))
    skipped = [x for x in run_file.get("log", []) if x.get("kind") == "skip"]
    if skipped:
        rows.append(F.finding(
            "medium", "operator/run.py", "protocol_steps_skipped", chip=chip_id,
            what="the planner skipped steps, so part of the assay did not run",
            observed=F.number("steps skipped", len(skipped)),
            limit=F.number("steps skipped", 0),
            message="; ".join(str(x.get("why") or x.get("message") or x)[:120] for x in skipped[:4]),
        ))
    return rows


# --- the cross-chip checks ------------------------------------------------------------


def _port(chip: dict, port_id: str) -> dict | None:
    return next((p for p in chip.get("ports", []) if p.get("id") == port_id), None)


def _channels_at(chip: dict, port_id: str) -> list[dict]:
    return [c for c in chip.get("channels", []) if port_id in (c.get("from"), c.get("to"))]


CARRIES_PARTICLES = {"cells", "capsules", "library"}


def check_handoff_particles(partition: dict, specs: dict[str, dict]) -> list[dict]:
    """Particle size against channel width AND depth on both sides of every hand-off.

    Both sides deliberately: each chip passes its own `check_chip_spec.py` run, because
    that tool only ever sees one chip. The mismatch lives in the join, and the join is
    the one thing no existing tool looks at.
    """
    try:
        width_factor = float(contracts.param("capsule_channel.width_factor_low"))
        factor_src = "contracts/parameters.json capsule_channel.width_factor_low"
    except contracts.ContractError as error:
        return [F.finding("medium", "s9_cross_chip", "contract_gap",
                          what="the channel width factor is not in the contracts, so the "
                               "hand-off width check cannot run",
                          message=str(error))]
    rows: list[dict] = []
    for handoff in partition.get("handoffs", []):
        label = f"{handoff['from']}.{handoff['from_port']} -> {handoff['to']}.{handoff['to_port']}"
        particle = handoff.get("particle_um")
        if particle is None:
            if handoff.get("carries") in CARRIES_PARTICLES:
                rows.append(F.finding(
                    "medium", "s9_cross_chip", "handoff_particle_unknown", chip=handoff["from"],
                    feature=label, rerun_from="s4",
                    what=f"the hand-off carries {handoff['carries']} but states no particle "
                         f"size, so neither side's channels can be checked",
                    message=f"{label}: particle_um is null",
                ))
            continue
        need_width = width_factor * float(particle)
        for side, chip_id, port_id in (("leaving", handoff["from"], handoff["from_port"]),
                                       ("arriving", handoff["to"], handoff["to_port"])):
            chip = specs.get(chip_id)
            if chip is None:
                rows.append(F.finding(
                    "blocker", "s9_cross_chip", "handoff_chip_missing", chip=chip_id,
                    feature=label, rerun_from="s6",
                    what=f"the chip on the {side} side of the hand-off has no spec to check",
                    message=f"{label}: chip/{chip_id}.json is not there",
                ))
                continue
            port = _port(chip, port_id)
            if port is None:
                rows.append(F.finding(
                    "blocker", "s9_cross_chip", "handoff_port_missing", chip=chip_id,
                    feature=label, rerun_from="s6",
                    what=f"the {side} port of the hand-off is not a port on that chip",
                    message=f"{label}: '{port_id}' is not a port of {chip_id}",
                ))
                continue
            if port.get("width_um") is not None and float(port["width_um"]) < need_width:
                rows.append(F.finding(
                    "blocker", "s9_cross_chip", "handoff_particle_width", chip=chip_id,
                    feature=f"{port_id} ({side})", rerun_from="s6",
                    what=f"the port the material passes through {side} is narrower than "
                         f"{width_factor:g}x the particle in the line",
                    observed=F.number(f"{port_id} bore", float(port["width_um"]), "um"),
                    limit=F.number(f"{width_factor:g} x particle", need_width, "um"),
                    message=f"{label}: {port_id} is {float(port['width_um']):g} um, "
                            f"particle {float(particle):g} um ({factor_src})",
                ))
            for channel in _channels_at(chip, port_id):
                width = float(channel.get("width_mm", 0)) * 1000
                depth = float(channel.get("depth_mm", 0)) * 1000
                if width and width < need_width:
                    rows.append(F.finding(
                        "blocker", "s9_cross_chip", "handoff_particle_width", chip=chip_id,
                        feature=f"{channel['id']} ({side})", rerun_from="s6",
                        what=f"the channel at the {side} end of the hand-off is narrow "
                             f"enough to clog on what the line carries",
                        observed=F.number(f"{channel['id']} width", width, "um"),
                        limit=F.number(f"{width_factor:g} x particle", need_width, "um"),
                        message=f"{label}: {channel['id']} is {width:g} um wide, particle "
                                f"{float(particle):g} um ({factor_src})",
                    ))
                if depth and depth < float(particle):
                    rows.append(F.finding(
                        "blocker", "s9_cross_chip", "handoff_particle_depth", chip=chip_id,
                        feature=f"{channel['id']} ({side})", rerun_from="s6",
                        what=f"the channel at the {side} end is shallower than the particle "
                             f"it must pass: no width makes a sphere fit a shallower duct",
                        observed=F.number(f"{channel['id']} depth", depth, "um"),
                        limit=F.number("particle diameter", float(particle), "um"),
                        message=f"{label}: {channel['id']} is {depth:g} um deep, particle "
                                f"{float(particle):g} um",
                    ))
    return rows


# A surplus bigger than the draw itself (ratio >= 2) means a concentration step must
# exist, not just a bit of dead volume. This is a REPORTING threshold chosen here, not a
# contract value, and every finding says so in `limit.name`.
VOLUME_SURPLUS_RATIO = 2.0


def check_volume_continuity(partition: dict, unitops: dict) -> list[dict]:
    """What one chip delivers against what the next one draws.

    The audit found Module 3 releasing 76.9 uL into Module 4 drawing 9.6 uL at near-packed
    density, with the concentration step documented nowhere. Nothing in the repo compares
    those two numbers, because each chip's own protocol is internally consistent.
    """
    ops = {op["op"]: op for op in unitops.get("ops", [])}
    chips = {c["chip_id"]: c for c in partition.get("chips", [])}
    rows: list[dict] = []

    def volume(chip_id: str, which: str) -> tuple[float | None, str]:
        entry = chips.get(chip_id)
        if not entry:
            return None, f"{chip_id} is not in partition.json"
        numbers = [op["op"] for op in (ops.get(n) for n in entry["ops"]) if op]
        if not numbers:
            return None, f"{chip_id} has no ops with a volume"
        chosen = numbers[-1] if which == "out" else numbers[0]
        fluid = (ops[chosen].get("fluid") or {})
        return fluid.get("volume_ul"), f"op {chosen} ({ops[chosen]['kind']})"

    for handoff in partition.get("handoffs", []):
        label = f"{handoff['from']} -> {handoff['to']}"
        stated = handoff.get("volume_ul")
        produced, produced_from = volume(handoff["from"], "out")
        drawn, drawn_from = volume(handoff["to"], "in")
        delivered = stated if stated is not None else produced
        delivered_from = "handoff volume_ul" if stated is not None else produced_from

        if delivered is None or drawn is None:
            rows.append(F.finding(
                "medium", "s9_cross_chip", "volume_continuity_unknown", chip=handoff["from"],
                feature=label, rerun_from="s4",
                what="one side of the hand-off has no volume, so continuity cannot be checked",
                observed=F.number(f"delivered ({delivered_from})", delivered, "uL"),
                limit=F.number(f"drawn ({drawn_from})", drawn, "uL"),
                message=f"{label}: delivered {delivered}, drawn {drawn}",
            ))
            continue

        delivered, drawn = float(delivered), float(drawn)
        if drawn > delivered:
            rows.append(F.finding(
                "blocker", "s9_cross_chip", "volume_continuity", chip=handoff["to"],
                feature=label, rerun_from="s4",
                what="the receiving chip draws more than the one before it delivers: the "
                     "step cannot run as planned",
                observed=F.number(f"delivered ({delivered_from})", delivered, "uL"),
                limit=F.number(f"drawn ({drawn_from})", drawn, "uL"),
                fix=F.fix("carry the shortfall forward in S4, or resize the receiving op",
                          predicted=f"delivered >= {drawn:g} uL"),
                message=f"{label}: delivers {delivered:g} uL into a step drawing {drawn:g} uL",
            ))
        elif drawn > 0 and delivered / drawn >= VOLUME_SURPLUS_RATIO:
            rows.append(F.finding(
                "high", "s9_cross_chip", "volume_continuity", chip=handoff["from"],
                feature=label, rerun_from="s4",
                what="the hand-off needs a concentration step that nothing documents: the "
                     "chip delivers much more than the next one draws",
                observed=F.number(f"delivered ({delivered_from})", delivered, "uL"),
                limit=F.number(
                    f"drawn ({drawn_from}); surplus ratio {VOLUME_SURPLUS_RATIO:g} is a "
                    f"reporting threshold in s9_verify.py, not a contract value",
                    drawn, "uL"),
                fix=F.fix("add a concentrate/collect op before the hand-off, or state the "
                          "spin in `how`", predicted=f"{delivered:g} uL -> about {drawn:g} uL"),
                message=f"{label}: delivers {delivered:g} uL into a step drawing {drawn:g} uL "
                        f"({delivered / drawn:.1f}x)",
            ))
        elif delivered != drawn:
            rows.append(F.finding(
                "medium", "s9_cross_chip", "volume_continuity", chip=handoff["from"],
                feature=label,
                what="the two sides of the hand-off disagree on volume, within a factor of two",
                observed=F.number(f"delivered ({delivered_from})", delivered, "uL"),
                limit=F.number(f"drawn ({drawn_from})", drawn, "uL"),
                message=f"{label}: delivers {delivered:g} uL, next step draws {drawn:g} uL",
            ))

        dead = handoff.get("dead_volume_ul")
        if dead is not None and delivered is not None:
            dead = float(dead)
            if dead >= delivered:
                rows.append(F.finding(
                    "blocker", "s9_cross_chip", "handoff_dead_volume", chip=handoff["from"],
                    feature=label, rerun_from="s4",
                    what="the line holds the whole transfer: nothing arrives",
                    observed=F.number("dead volume", dead, "uL"),
                    limit=F.number("transferred", delivered, "uL"),
                    message=f"{label}: {dead:g} uL dead in a {delivered:g} uL transfer",
                ))
            elif dead > 0:
                rows.append(F.finding(
                    "low", "s9_cross_chip", "handoff_dead_volume", chip=handoff["from"],
                    feature=label,
                    what="some of the transfer stays in the line",
                    observed=F.number("dead volume", dead, "uL"),
                    limit=F.number("transferred", delivered, "uL"),
                    message=f"{label}: {dead:g} uL dead in a {delivered:g} uL transfer",
                ))
    return rows


def check_one_capsule_diameter(partition: dict, unitops: dict, specs: dict[str, dict]
                               ) -> list[dict]:
    """ONE capsule diameter across the whole run. This repo once had four coexisting.

    Four values meant every trap gap, channel width and chamber height derived from them
    was right for some other chip. Hand-off `particle_um` is the SWOLLEN diameter by
    contract, so it is compared against diameter x swell factor, not against diameter.
    """
    unswollen: list[tuple[float, str]] = []
    swollen: list[tuple[float, str]] = []
    swell: list[tuple[float, str]] = []
    try:
        unswollen.append((contracts.capsule_diameter_um(),
                          "contracts/parameters.json capsule.diameter_um"))
        swell.append((contracts.capsule_swell_factor(),
                      "contracts/parameters.json capsule.swell_factor"))
    except contracts.ContractError as error:
        return [F.finding("medium", "s9_cross_chip", "contract_gap",
                          what="the capsule diameter is not in the contracts, so one "
                               "diameter across the run cannot be checked",
                          message=str(error))]

    for op in unitops.get("ops", []):
        particles = op.get("particles") or {}
        if particles.get("kind") == "capsules" and particles.get("diameter_um") is not None:
            unswollen.append((float(particles["diameter_um"]),
                              f"unitops.json op {op['op']} ({op['kind']})"))
    for handoff in partition.get("handoffs", []):
        if handoff.get("carries") == "capsules" and handoff.get("particle_um") is not None:
            swollen.append((float(handoff["particle_um"]),
                            f"partition.json handoff {handoff['from']} -> {handoff['to']}"))
    for chip_id, chip in sorted(specs.items()):
        capsule = chip.get("capsule") or {}
        if capsule.get("diameter_um") is not None:
            unswollen.append((float(capsule["diameter_um"]),
                              f"chip/{chip_id}.json capsule.diameter_um"))
        if capsule.get("swell_factor") is not None:
            swell.append((float(capsule["swell_factor"]),
                          f"chip/{chip_id}.json capsule.swell_factor"))

    rows: list[dict] = []
    rows += _one_value("capsule_diameter_disagreement", unswollen, "um",
                       "unswollen capsule diameter",
                       "every trap gap, channel width and chamber height is derived from "
                       "this number, so two values mean some chip is sized for a capsule "
                       "that does not exist")
    rows += _one_value("capsule_swell_disagreement", swell, "x", "capsule swell factor",
                       "the swollen diameter sets chamber height and port bore")
    if len(set(round(v, 3) for v, _ in unswollen)) == 1 and swollen:
        expected = unswollen[0][0] * max(v for v, _ in swell)
        for value, where in swollen:
            if abs(value - expected) > 0.5:
                rows.append(F.finding(
                    "high", "s9_cross_chip", "capsule_diameter_disagreement",
                    feature=where, rerun_from="s4",
                    what="a hand-off states a swollen capsule diameter that is not the "
                         "contract diameter times the swell factor",
                    observed=F.number(f"particle_um at {where}", value, "um"),
                    limit=F.number("capsule.diameter_um x swell_factor", round(expected, 1), "um"),
                    message=f"{where}: {value:g} um, expected {expected:g} um",
                ))
    return rows


def _one_value(rule: str, values: list[tuple[float, str]], units: str, what: str,
               why: str) -> list[dict]:
    distinct = sorted({round(v, 3) for v, _ in values})
    if len(distinct) <= 1:
        return []
    row = F.finding(
        "blocker", "s9_cross_chip", rule, rerun_from="s4",
        what=f"{len(distinct)} different values of the {what} coexist in this run: {why}",
        observed=F.number(f"distinct {what}s", len(distinct), ""),
        limit=F.number("one value across the run", 1, ""),
        fix=F.fix(f"take the {what} from contracts/parameters.json everywhere",
                  predicted=f"{distinct[0]:g} {units} throughout".strip()),
        message="; ".join(f"{v:g} {units} ({where})".strip() for v, where in sorted(values)),
    )
    row["values"] = [{"value": v, "where": where} for v, where in sorted(values)]
    return [row]


# --- the stage -----------------------------------------------------------------------


HONEST_LIMITS = [
    "Static checks only. Nothing here simulates flow: that is S10, and a chip that "
    "passes S9 can still trap, leak or shear.",
    "tools/check_chip_spec.py is run per chip, so main's 60 pre-existing port-overlap "
    "errors are reported as 'preexisting' and cannot pass or fail this stage.",
    "operator/run.py proves the protocol PLANS into commands. It does not run hardware "
    "and no number here was measured.",
    "The cross-chip checks use the volumes and particle sizes the earlier stages stated. "
    "A hand-off with a null volume is reported as unknown, not assumed.",
]


def chip_artefacts(chip_id: str) -> tuple[Path, Path]:
    """Where S6/S7 and S8 put this chip's spec and protocol."""
    return ROOT / "chip" / f"{chip_id}.json", ROOT / "protocols" / f"{chip_id}.json"


def verify(assay_id: str, *, write_figures: bool = False) -> dict:
    """Verify every chip in partition.json. Returns the verify.json payload."""
    partition = stage_io.read_stage(assay_id, "partition")
    try:
        unitops = stage_io.read_stage(assay_id, "unitops")
    except stage_io.StageFileMissing:
        unitops = {"ops": []}

    work = stage_io.work_dir(assay_id, create=True)
    chip_ids = [c["chip_id"] for c in partition.get("chips", [])]
    our_chips = set(chip_ids)

    specs: dict[str, dict] = {}
    protocols: dict[str, dict] = {}
    missing: list[str] = []
    for chip_id in chip_ids:
        spec_path, protocol_path = chip_artefacts(chip_id)
        for path, label, stage in ((spec_path, "chip spec", "s6/s7"),
                                   (protocol_path, "protocol", "s8")):
            if not path.exists():
                missing.append(f"{path.relative_to(ROOT).as_posix()} ({label}, written by "
                               f"{stage}) is not there")
        if spec_path.exists():
            try:
                specs[chip_id] = json.loads(spec_path.read_text())
            except ValueError as error:
                missing.append(f"chip/{chip_id}.json is not readable JSON: {error}")
        if protocol_path.exists():
            try:
                protocols[chip_id] = json.loads(protocol_path.read_text())
            except ValueError as error:
                missing.append(f"protocols/{chip_id}.json is not readable JSON: {error}")

    rows: list[dict] = []
    tools: list[dict] = []
    protocol_of = {chip_id: chip_id for chip_id in protocols}

    validate_run, validate_rows = run_validate(our_chips, protocol_of)
    tools.append(validate_run.summary())
    rows += validate_rows

    for chip_id in chip_ids:
        spec_path, protocol_path = chip_artefacts(chip_id)
        if chip_id in specs:
            tool, found = run_check_chip_spec(
                chip_id, spec_path, protocol_path if chip_id in protocols else None)
            tools.append(tool.summary())
            rows += found
            tool, found = run_collisions(chip_id, specs[chip_id], write_figures)
            tools.append(tool.summary())
            rows += found
        if chip_id in specs and chip_id in protocols:
            tool, found = run_operator(chip_id, specs[chip_id], protocols[chip_id],
                                       spec_path, protocol_path, work / "runs")
            tools.append(tool.summary())
            rows += found

    cross = (check_handoff_particles(partition, specs)
             + check_volume_continuity(partition, unitops)
             + check_one_capsule_diameter(partition, unitops, specs))
    rows += cross

    mine = F.of_scope(rows, F.PIPELINE)
    preexisting = F.of_scope(rows, F.PREEXISTING)
    blockers = F.blockers(rows)
    ok = not blockers and not missing
    rerun = F.rerun_request(rows)

    payload: dict[str, Any] = {
        "id": assay_id,
        "stage": "s9_verify",
        "chips": chip_ids,
        "ok": ok,
        "verdict": {
            "ok": ok,
            "scope": "the chips in partition.json only",
            "reason": _verdict_reason(ok, blockers, missing, preexisting),
        },
        "prerequisites_missing": missing,
        "tools": tools,
        "findings": rows,
        "cross_chip": [r["rule"] for r in cross],
        "counts": {
            "pipeline": F.counts(mine),
            "preexisting": F.counts(preexisting),
        },
        "limits": HONEST_LIMITS,
    }
    if rerun and not ok:
        payload["rerun_from"] = rerun["rerun_from"]
        payload["reason"] = rerun["reason"]
    stage_io.write_json(work / "verify.json", payload)
    return payload


def _verdict_reason(ok: bool, blockers: Sequence[dict], missing: Sequence[str],
                    preexisting: Sequence[dict]) -> str:
    note = (f" {len(F.blockers(preexisting, scope=None))} error(s) on main's hand-made "
            f"chips are reported separately and are not ours." if preexisting else "")
    if missing:
        return (f"{len(missing)} artefact(s) an earlier stage should have written are not "
                f"there, so the chips cannot be verified.{note}")
    if ok:
        return ("Every generated chip and protocol passes validate.py, check_chip_spec.py, "
                "the planar collision check and operator/run.py, and the cross-chip checks "
                f"found no blocker.{note}")
    rules = sorted({r["rule"] for r in blockers})
    return f"{len(blockers)} blocker(s) on the pipeline's own chips: {', '.join(rules)}.{note}"


def run(assay_id: str = "", *, id: str = "", figures: bool = False, **_ignored) -> dict:
    """Stage entry point for assay2chip/run.py.

    Returns a `rerun_from` request when a blocker's fix is upstream, and raises
    VerifyError when a blocker has to be fixed by a human - a stage that found an ERROR
    must not report success (assay2chip/README.md: S9 must be 0 errors).
    """
    payload = verify(assay_id or id, write_figures=figures)
    if payload["ok"]:
        return payload
    if payload.get("rerun_from"):
        return {"rerun_from": payload["rerun_from"], "reason": payload["reason"],
                "verify": payload}
    raise VerifyError(payload["verdict"]["reason"])


def render(payload: dict) -> str:
    """The stage's own console summary. The report is S12's job, not this one's."""
    lines = [f"s9_verify: {payload['id']} - {len(payload['chips'])} chip(s), "
             f"{'OK' if payload['ok'] else 'FAILED'}"]
    for tool in payload["tools"]:
        state = "skipped" if tool["skipped"] else f"exit {tool['exit_code']}"
        lines.append(f"  {tool['tool']:<28} {state}"
                     + (f"  ({tool['skipped']})" if tool["skipped"] else ""))
    for name, counts in payload["counts"].items():
        lines.append(f"  {name:<12} " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    for row in F.blockers(payload["findings"]):
        lines.append(f"  BLOCKER  {row['chip'] or '-'}  {row['rule']}  "
                     f"{F.one_line(row)}  {row['what']}")
    for problem in payload["prerequisites_missing"]:
        lines.append(f"  MISSING  {problem}")
    lines.append(f"  {payload['verdict']['reason']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="s9_verify",
        description="S9: run the repo's checks over every chip the pipeline generated")
    parser.add_argument("--id", required=True, help="work id: assay2chip/work/<id>/")
    parser.add_argument("--figures", action="store_true",
                        help="also write the SVG drawings into cad/figures/")
    parser.add_argument("--json", action="store_true", help="print verify.json instead")
    args = parser.parse_args(argv)

    try:
        payload = verify(args.id, write_figures=args.figures)
    except stage_io.StageFileMissing as error:
        print(f"s9_verify: {error}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2) if args.json else render(payload))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

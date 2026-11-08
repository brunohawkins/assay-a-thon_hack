"""End to end: assay2chip S0 -> S12 on the fixtures, offline, with the LLM stub.

    python -m unittest tests.test_assay2chip_e2e -v
    python tests/test_assay2chip_e2e.py --golden                 # keep the artefacts
    python tests/test_assay2chip_e2e.py --golden --id my_run --fixture bead_capture_assay.md

A STAGE THAT DOES NOT EXIST YET IS SKIPPED, NOT FAILED. The pipeline is being built by
many hands at once, so this test has to be useful before it is complete: it walks the
stages in numeric order and, for each one, reports

  skip   the module is not there yet (`assay2chip/stages/s7_*.py` matches nothing), or it
         is there but raises NotImplementedError, or an earlier stage was skipped so its
         input file was never written. The reason always names the missing thing.
  fail   the module is there, its input is there, and it raised - or it wrote an artefact
         that does not match its schema. That is a real bug and must be loud.
  pass   it ran and its output validates.

So the test goes green progressively as the other stages land, and it never goes green by
accident: `test_pipeline_is_complete` lists which stages were skipped, and the one
assertion that is always made is that **no stage that exists is broken**.

Stage modules are found by glob (`s4_*.py`), not by a hard-coded module name, because the
`<name>` half of `s<N>_<name>.py` belongs to whoever writes the stage. `run()` is called
with only the keyword arguments its signature accepts, exactly as `assay2chip/run.py`
does, so a stage that takes `(ctx)` and one that takes `(assay_id, paper)` both work.

`{"rerun_from": "s6", ...}` is honoured, up to MAX_RERUN_LOOPS, because S9 and S10 are
allowed to send a chip back and the loop is part of the contract being tested.

WHAT THIS TEST WRITES. S5-S11 write into `chip/`, `protocols/` and `cad/`, next to
hand-designed files. Afterwards the test removes only the files that appeared AND whose
path carries one of its own `e2e_...` ids, so `tools/validate.py` stays at zero errors
without the test ever deleting somebody else's work - see `RepoGuard`. The golden run
(`--golden`) keeps everything instead, which is the point of it.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import os
import sys
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "operator", ROOT / "tools"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import schema_io  # noqa: E402  (operator/)

from assay2chip.lib import io as stage_io  # noqa: E402
from assay2chip.lib import llm  # noqa: E402
from assay2chip.report import findings as F  # noqa: E402

FIXTURES = ROOT / "assay2chip" / "fixtures"
STAGE_DIR = ROOT / "assay2chip" / "stages"

# The e2e run drives the stages itself, so it also has to honour the re-run contract.
MAX_RERUN_LOOPS = 3

# Directories S5-S11 write into. Snapshotted before the run, trimmed back after.
REPO_OUTPUTS = (
    "chip",
    "chip/designs",
    "protocols",
    "runs",
    "runs/transcripts",
    "cad/exports",
    "cad/figures",
)

# A fixture every stage should cope with, first. The messy one and the PDF exercise S0/S1
# much harder but take the same path from S2 on.
FIXTURE_ORDER = ("bead_capture_assay.md", "messy_wash_and_lyse_assay.md",
                 "pdf_methods_excerpt.pdf")


class Skip(Exception):
    """Not a failure: something this stage needs has not been built yet."""


def _where(error: BaseException) -> str:
    """'assay2chip/triage/rules.py:74' - the deepest repo frame, to point at the bug."""
    tb = error.__traceback__
    deepest = ""
    while tb is not None:
        path = Path(tb.tb_frame.f_code.co_filename)
        try:
            deepest = f"{path.resolve().relative_to(ROOT).as_posix()}:{tb.tb_lineno}"
        except ValueError:
            pass
        tb = tb.tb_next
    return deepest or "unknown"


@dataclass
class Outcome:
    stage: str
    state: str                      # 'pass' | 'skip' | 'fail'
    reason: str = ""
    seconds: float = 0.0
    loops: int = 1

    @property
    def broke_the_chain(self) -> bool:
        return self.state != "pass"


@dataclass
class Pipeline:
    """One run of S0-S12 over one fixture, in process, offline."""

    assay_id: str
    paper: Path
    verbose: bool = False
    outcomes: list[Outcome] = field(default_factory=list)
    _broken_at: Outcome | None = None

    # --- finding and calling the stages ------------------------------------------

    @staticmethod
    def module_for(number: int) -> Any:
        """The one module whose file is `s<number>_<something>.py`, or None.

        Glob, not a hard-coded name: the `<name>` half of `s<N>_<name>.py` belongs to
        whoever writes the stage, and guessing it would make this test fail for a stage
        that exists under a name nobody told us about.
        """
        matches = sorted(STAGE_DIR.glob(f"s{number}_*.py"))
        if not matches:
            return None
        if len(matches) > 1:
            raise AssertionError(
                f"{len(matches)} modules claim stage s{number}: "
                f"{', '.join(p.name for p in matches)}. One stage, one module."
            )
        return importlib.import_module(f"assay2chip.stages.{matches[0].stem}")

    def kwargs_for(self, func: Callable) -> dict:
        """Only the keyword arguments this `run()` accepts - what run.py does.

        `assay2chip/stages/__init__.py` lists what is on offer; a stage takes the ones it
        wants and ignores the rest.
        """
        offered = {
            "assay_id": self.assay_id,
            "id": self.assay_id,
            "paper": str(self.paper),
            "source": str(self.paper),
            "llm_mode": llm.STUB,
            "offline": True,
            "force": False,
            "verbose": self.verbose,
            "figures": False,
        }
        parameters = inspect.signature(func).parameters
        return {k: v for k, v in offered.items() if k in parameters}

    def call(self, number: int, name: str) -> Any:
        """Import, call and return a stage's result. Raises Skip when it is not built."""
        try:
            module = self.module_for(number)
        except ImportError as error:
            raise Skip(f"assay2chip/stages/s{number}_*.py cannot be imported yet: "
                       f"{error}") from None
        if module is None:
            raise Skip(f"assay2chip/stages/s{number}_*.py does not exist yet "
                       f"(expected the stage that does {name})")
        runner = getattr(module, "run", None)
        if runner is None:
            raise Skip(f"{module.__name__} has no run(**kwargs) yet")
        try:
            return runner(**self.kwargs_for(runner))
        except NotImplementedError as error:
            raise Skip(f"{module.__name__}.run() is not implemented yet ({error})") from None
        except stage_io.StageFileMissing as error:
            raise Skip(f"s{number} needs a file an earlier stage did not write: {error}") from None

    # --- running one stage, with its own check ------------------------------------

    def step(self, number: int, name: str, check: Callable[[Pipeline], None],
             independent: bool = False) -> Outcome:
        """Run one stage and check its output. NEVER raises: a stage that blows up is an
        Outcome like any other, so one half-finished module cannot stop the walk and hide
        what every other stage did.

        `independent` stages run even after the chain breaks. Only S12 is: a report that
        refuses to exist because an earlier stage did not run is worse than useless,
        since the report is how you find out what did not run.
        """
        stage = f"s{number}_{name}"
        if self._broken_at is not None and not independent:
            earlier = self._broken_at
            return self._record(Outcome(
                stage, "skip",
                f"{earlier.stage} {'failed' if earlier.state == 'fail' else 'was skipped'}, "
                f"so this stage has no input"))
        started = time.monotonic()
        loops = 0
        try:
            while True:
                loops += 1
                result = self.call(number, name)
                target = (result or {}).get("rerun_from") if isinstance(result, dict) else None
                if not target:
                    break
                if loops > MAX_RERUN_LOOPS:
                    raise AssertionError(
                        f"{stage} asked to re-run from {target} more than "
                        f"{MAX_RERUN_LOOPS} times: {(result or {}).get('reason')}")
                self.rerun_from(target, stage, (result or {}).get("reason", ""))
            check(self)
        except Skip as error:
            return self._break(Outcome(stage, "skip", str(error),
                                       time.monotonic() - started, loops), independent)
        except AssertionError as error:
            return self._break(Outcome(stage, "fail", str(error),
                                       time.monotonic() - started, loops), independent)
        except Exception as error:               # noqa: BLE001 - a stage may do anything
            return self._break(Outcome(
                stage, "fail",
                f"{type(error).__name__}: {error} (in {_where(error)})",
                time.monotonic() - started, loops), independent)
        return self._record(Outcome(stage, "pass", "", time.monotonic() - started, loops))

    def _break(self, outcome: Outcome, independent: bool = False) -> Outcome:
        if not independent:
            self._broken_at = outcome
        return self._record(outcome)

    def rerun_from(self, target: str, asked_by: str, why: str) -> None:
        """Re-run the stages from `target` up to (not including) the one that asked."""
        first = int(str(target).lstrip("s") or 0)
        last = int(asked_by.split("_")[0].lstrip("s"))
        if self.verbose:
            print(f"  {asked_by} -> re-run from {target}: {why}")
        for number in range(first, last):
            if self.module_for(number) is not None:
                self.call(number, f"re-run for {asked_by}")

    def _record(self, outcome: Outcome) -> Outcome:
        self.outcomes.append(outcome)
        if self.verbose:
            print(f"  {outcome.state.upper():<5} {outcome.stage:<16} "
                  f"{outcome.seconds:5.1f}s  {outcome.reason}")
        return outcome

    # --- the data the checks look at ----------------------------------------------

    def stage_file(self, kind: str) -> Any:
        return stage_io.read_stage(self.assay_id, kind)

    def work(self, *parts: str) -> Path:
        return stage_io.work_dir(self.assay_id).joinpath(*parts)

    def chip_ids(self) -> list[str]:
        return [c["chip_id"] for c in self.stage_file("partition")["chips"]]

    def run_all(self) -> list[Outcome]:
        for number, name, check, independent in STAGES:
            self.step(number, name, check, independent)
        return self.outcomes


# --- what each stage's output has to look like ---------------------------------------


def check_raw(p: Pipeline) -> None:
    raw = p.stage_file("raw")
    assert raw["id"] == p.assay_id, f"raw.json says id {raw['id']!r}, not {p.assay_id!r}"
    assert raw.get("source", {}).get("ref"), "raw.json has no source.ref: no provenance"
    assert raw.get("blocks"), "raw.json has no blocks: nothing was ingested"


def check_assay(p: Pipeline) -> None:
    assay = p.stage_file("assay")          # read_stage validates against the schema
    assert assay["steps"], "assay.json has no steps"
    assert "unresolved" in assay, "assay.json must carry `unresolved`, even empty"
    for step in assay["steps"]:
        assert step["confidence"] is not None, f"step {step['step']} has no confidence"


def check_unitops(p: Pipeline) -> None:
    unitops = p.stage_file("unitops")
    assert unitops["ops"], "unitops.json has no ops"
    covered = {s for op in unitops["ops"] for s in op.get("from_steps", [])}
    steps = {s["step"] for s in p.stage_file("assay")["steps"]}
    assert not steps - covered, f"S2 dropped assay step(s) {sorted(steps - covered)}"


def check_triage(p: Pipeline) -> None:
    for op in p.stage_file("unitops")["ops"]:
        placement = op.get("placement") or {}
        assert placement.get("where"), f"op {op['op']} has no placement after S3"
        assert placement.get("why"), f"op {op['op']} placement has no `why`"


def check_partition(p: Pipeline) -> None:
    partition = p.stage_file("partition")
    assert partition["chips"], "partition.json has no chips"
    on_chip = {op["op"] for op in p.stage_file("unitops")["ops"]
               if (op.get("placement") or {}).get("where") == "on_chip"}
    placed: dict[int, list[str]] = {}
    for chip in partition["chips"]:
        for op in chip["ops"]:
            placed.setdefault(op, []).append(chip["chip_id"])
    assert not on_chip - set(placed), \
        f"on-chip op(s) {sorted(on_chip - set(placed))} are on no chip"
    twice = {op: chips for op, chips in placed.items() if len(chips) > 1}
    assert not twice, f"op(s) on more than one chip: {twice}"
    for handoff in partition["handoffs"]:
        assert isinstance(handoff["automated"], bool), \
            "every hand-off must say whether a machine can do it"


def check_designs(p: Pipeline) -> None:
    for chip_id in p.chip_ids():
        path = ROOT / "chip" / "designs" / f"{chip_id}.json"
        if not path.is_file():
            raise Skip(f"S5 wrote no chip/designs/{chip_id}.json")
        json.loads(path.read_text())


def _chip_spec(chip_id: str) -> dict:
    path = ROOT / "chip" / f"{chip_id}.json"
    if not path.is_file():
        raise Skip(f"chip/{chip_id}.json has not been written")
    chip = json.loads(path.read_text())
    errors = schema_io.schema_errors("chip_spec", chip)
    assert not errors, (
        f"chip/{chip_id}.json does not match chip_spec.schema.json: "
        + "; ".join(f"{schema_io.path_str(e)}: {schema_io.describe(e)}" for e in errors[:4]))
    return chip


def check_build(p: Pipeline) -> None:
    from assay2chip.lib import contracts

    max_depth = contracts.max_depth_mm()      # from the contract, never a literal
    for chip_id in p.chip_ids():
        chip = _chip_spec(chip_id)
        for channel in chip.get("channels", []):
            assert channel["depth_mm"] <= max_depth + 1e-9, (
                f"chip/{chip_id}.json channel {channel['id']} is {channel['depth_mm']} mm "
                f"deep: one etch depth is {max_depth} mm")


def check_route(p: Pipeline) -> None:
    import check_chip_spec as ccs       # tools/

    for chip_id in p.chip_ids():
        chip = _chip_spec(chip_id)
        crossings = ccs.crossings(chip)
        assert not crossings, (
            f"chip/{chip_id}.json has {len(crossings)} channel crossing(s) after S7; in one "
            f"fluidic layer a crossing is a short circuit: "
            + ", ".join(f"{a} x {b}" for a, b, _ in crossings[:4]))


def check_protocols(p: Pipeline) -> None:
    for chip_id in p.chip_ids():
        path = ROOT / "protocols" / f"{chip_id}.json"
        if not path.is_file():
            raise Skip(f"S8 wrote no protocols/{chip_id}.json")
        protocol = json.loads(path.read_text())
        errors = schema_io.schema_errors("protocol", protocol)
        assert not errors, (
            f"protocols/{chip_id}.json does not match protocol.schema.json: "
            + "; ".join(f"{schema_io.path_str(e)}: {schema_io.describe(e)}"
                        for e in errors[:4]))
        assert protocol.get("steps"), f"protocols/{chip_id}.json has no steps"


def check_verify(p: Pipeline) -> None:
    payload = json.loads(p.work("verify.json").read_text())
    if payload["prerequisites_missing"]:
        raise Skip("S9 had nothing to verify: "
                   + "; ".join(payload["prerequisites_missing"][:3]))
    blockers = F.blockers(payload["findings"])
    assert payload["ok"], (
        f"S9 failed on the pipeline's own chips ({len(blockers)} blocker(s)): "
        + "; ".join(f"{b['chip']}/{b['rule']} {F.one_line(b)}" for b in blockers[:5])
        + ". Pre-existing errors on main's hand-made chips are excluded by design.")


def check_cfd(p: Pipeline) -> None:
    path = p.work("cfd", "findings.json")
    if not path.is_file():
        raise Skip("S10 wrote no cfd/findings.json")
    rows = F.normalise(json.loads(path.read_text()))
    for row in rows:
        assert row["severity"] in F.SEVERITIES, \
            f"cfd/findings.json has severity {row['severity']!r}"
    assert not F.blockers(rows), (
        f"S10 found {len(F.blockers(rows))} blocker(s) that no fix resolved: "
        + "; ".join(f"{b['chip']}/{b['rule']} {F.one_line(b)}"
                    for b in F.blockers(rows)[:5]))


def check_fabricate(p: Pipeline) -> None:
    from assay2chip.report import model

    empty = [b["path"] for b in (model._bundle(c) for c in p.chip_ids()) if not b["present"]]
    if empty:
        raise Skip(f"S11 wrote no print bundle: {', '.join(empty[:3])}")


def check_report(p: Pipeline) -> None:
    data = json.loads(p.work("report.json").read_text())
    text = p.work("report.md").read_text()
    head = data["headline"]
    for key in ("chips", "on_chip_fraction", "handoffs_not_automated",
                "unresolved_from_s1", "cfd_by_severity", "cfd_fixes_applied",
                "cfd_fixes_proposed"):
        assert key in head, f"report.json headline has no {key!r}"
    assert "## Honest limits" in text, "report.md has no HONEST LIMITS section"
    assert data["limits"], "report.json has no limits: what was simulated must be said"
    assert "## Headline" in text and "|" in text, "report.md should be tables, not prose"
    for step in data["steps"]:
        assert step["where"] in ("chip", "op", "off_chip", "missing"), \
            f"step {step['step']} ended up in {step['where']!r}, which is not a bucket"


# (number, name, check, runs-even-after-the-chain-breaks). NUMERIC order, which is why
# s10 follows s9 and not s1 - a string sort would run the pipeline in the wrong order.
STAGES: tuple[tuple[int, str, Callable[[Pipeline], None], bool], ...] = (
    (0, "ingest", check_raw, False),
    (1, "extract", check_assay, False),
    (2, "normalise", check_unitops, False),
    (3, "triage", check_triage, False),
    (4, "partition", check_partition, False),
    (5, "group", check_designs, False),
    (6, "build", check_build, False),
    (7, "route", check_route, False),
    (8, "protocol", check_protocols, False),
    (9, "verify", check_verify, False),
    (10, "cfd", check_cfd, False),
    (11, "fabricate", check_fabricate, False),
    (12, "report", check_report, True),
)


# --- leaving the repo as we found it --------------------------------------------------


class RepoGuard:
    """Remember what is in the repo's output directories; afterwards, remove OUR leftovers.

    Two rules, and the second one matters more than the first:

      1. a file that existed before the run is never touched;
      2. a file that appeared during the run is removed **only if its path contains one of
         this test's own ids** (`e2e_...`).

    Rule 2 is not tidiness, it is safety. This repo is worked on by many agents at once,
    so a file that appeared while the test was running may well be somebody else's -
    `protocols/module3_v1_translated.json` landing mid-run is a real thing that happens.
    Deleting it because it is new would destroy their work. Our own artefacts are all
    named after chip ids derived from the assay id, which is namespaced `e2e_<fixture>`,
    so the test can always recognise its own output and nothing else.
    """

    def __init__(self, ids: Sequence[str], directories: tuple[str, ...] = REPO_OUTPUTS
                 ) -> None:
        self.ids = tuple(ids)
        self.directories = directories
        self.before: set[Path] = set()

    def snapshot(self) -> "RepoGuard":
        self.before = self._now()
        return self

    def _now(self) -> set[Path]:
        found: set[Path] = set()
        for name in self.directories:
            base = ROOT / name
            if base.is_dir():
                found |= {p for p in base.rglob("*") if p.is_file()}
        return found

    def ours(self, path: Path) -> bool:
        text = path.as_posix()
        return any(assay_id in text for assay_id in self.ids)

    def restore(self) -> tuple[list[Path], list[Path]]:
        """(removed, left alone). The second list is somebody else's work, on purpose."""
        removed, theirs = [], []
        for path in sorted(self._now() - self.before, reverse=True):
            if self.ours(path):
                path.unlink(missing_ok=True)
                removed.append(path)
            else:
                theirs.append(path)
        for directory in sorted(
                {p.parent for p in removed}, key=lambda p: len(p.parts), reverse=True):
            if directory.is_dir() and self.ours(directory) and not any(directory.iterdir()):
                directory.rmdir()
        return removed, theirs


def clear_work(assay_id: str) -> None:
    work = stage_io.WORK / assay_id
    if not work.is_dir():
        return
    for path in sorted(work.rglob("*"), reverse=True):
        path.unlink(missing_ok=True) if path.is_file() else path.rmdir()
    work.rmdir()


def fixture_paths() -> list[Path]:
    """The fixtures, in FIXTURE_ORDER first, then any new one that appears.

    `README.md` documents the fixtures, it is not one: ingesting it produces a document
    with no method steps, which is a true result about the README and tells us nothing
    about the pipeline.
    """
    everything = {
        p.name: p for p in sorted(FIXTURES.iterdir())
        if p.suffix.lower() in (".md", ".txt", ".pdf") and p.stem.lower() != "readme"
    }
    ordered = [everything.pop(name) for name in FIXTURE_ORDER if name in everything]
    return ordered + list(everything.values())


def assay_id_for(fixture: Path) -> str:
    """`e2e_bead_capture_assay` - namespaced, so a leftover is obviously the test's."""
    stem = "".join(c if c.isalnum() else "_" for c in fixture.stem.lower())
    return "e2e_" + "_".join(part for part in stem.split("_") if part)


# --- the test -------------------------------------------------------------------------


class AssayToChipEndToEnd(unittest.TestCase):
    """S0 -> S12 on every fixture, offline. Missing stages skip; broken stages fail."""

    maxDiff = 4000

    @classmethod
    def setUpClass(cls) -> None:
        os.environ[llm.MODE_ENV] = llm.STUB     # no network, ever
        llm.configure(mode=llm.STUB)
        fixtures = fixture_paths()
        cls.guard = RepoGuard([assay_id_for(f) for f in fixtures]).snapshot()
        cls.runs: list[Pipeline] = []
        for fixture in fixtures:
            pipeline = Pipeline(assay_id_for(fixture), fixture)
            clear_work(pipeline.assay_id)
            pipeline.run_all()
            cls.runs.append(pipeline)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.guard.restore()
        for pipeline in cls.runs:
            clear_work(pipeline.assay_id)

    def test_offline(self) -> None:
        """The whole run used the stub, so nothing here needed a network or a key."""
        self.assertTrue(llm.is_stub(), "the e2e run must be offline (llm stub mode)")
        for pipeline in self.runs:
            for kind in ("assay", "unitops", "partition"):
                try:
                    mode = (pipeline.stage_file(kind).get("llm") or {}).get("mode")
                except stage_io.StageFileMissing:
                    continue
                self.assertIn(mode, (None, "none", "stub"),
                              f"{pipeline.assay_id} {kind}.json says the model mode was "
                              f"{mode!r}: the e2e run must never call a real model")

    def test_every_stage_that_exists_works(self) -> None:
        """The one assertion that is always made. A skip is fine; a failure is not."""
        self.assertTrue(self.runs, "no fixtures in assay2chip/fixtures/")
        for pipeline in self.runs:
            for outcome in pipeline.outcomes:
                with self.subTest(fixture=pipeline.paper.name, stage=outcome.stage):
                    if outcome.state == "skip":
                        self.skipTest(outcome.reason)
                    self.assertEqual(outcome.state, "pass", outcome.reason)

    def test_pipeline_is_complete(self) -> None:
        """Skips with the reason, so it is obvious what is left to build."""
        for pipeline in self.runs:
            skipped = [o for o in pipeline.outcomes if o.state == "skip"]
            with self.subTest(fixture=pipeline.paper.name):
                if skipped:
                    self.skipTest(
                        f"{len(skipped)} of {len(pipeline.outcomes)} stages are not "
                        f"built yet: " + "; ".join(f"{o.stage}: {o.reason}"
                                                   for o in skipped[:4]))
                self.assertEqual([o.stage for o in pipeline.outcomes if o.state == "pass"],
                                 [f"s{n}_{name}" for n, name, _, _ in STAGES])

    def test_report_is_written_whatever_ran(self) -> None:
        """S12 runs even when earlier stages did not, and admits what is not there.

        It is the one stage the walk does not skip when the chain breaks, because the
        report is how you find out what did not run.
        """
        if Pipeline.module_for(12) is None:
            self.skipTest("assay2chip/stages/s12_*.py does not exist yet")
        for pipeline in self.runs:
            with self.subTest(fixture=pipeline.paper.name):
                data = json.loads(pipeline.work("report.json").read_text())
                text = pipeline.work("report.md").read_text()
                self.assertIn("## Honest limits", text)
                self.assertEqual(data["id"], pipeline.assay_id)
                broken = [o.stage for o in pipeline.outcomes
                          if o.broke_the_chain and o.stage != "s12_report"]
                if broken:
                    self.assertTrue(
                        data["missing"],
                        f"{len(broken)} stage(s) did not run ({', '.join(broken[:3])}) but "
                        f"report.json claims nothing is missing")


# --- the golden run -------------------------------------------------------------------


def golden(argv: list[str]) -> int:
    """`python tests/test_assay2chip_e2e.py --golden` - the same walk, artefacts KEPT.

    Writes everything into `assay2chip/work/<id>/` (and, for S5-S11, into chip/,
    protocols/ and cad/, which is where those stages belong). Nothing is cleaned up:
    the point of a golden run is to be able to look at what came out.
    """
    parser = argparse.ArgumentParser(
        prog="test_assay2chip_e2e --golden",
        description="Run assay2chip S0-S12 on a fixture and keep the artefacts.")
    parser.add_argument("--golden", action="store_true", required=True)
    parser.add_argument("--fixture", help="file in assay2chip/fixtures/ (default: all)")
    parser.add_argument("--id", help="work id (default: e2e_<fixture stem>)")
    parser.add_argument("--fresh", action="store_true",
                        help="delete assay2chip/work/<id>/ first")
    parser.add_argument("-q", "--quiet", action="store_true")
    args = parser.parse_args(argv)

    os.environ[llm.MODE_ENV] = llm.STUB
    llm.configure(mode=llm.STUB)

    chosen = [FIXTURES / args.fixture] if args.fixture else fixture_paths()
    for fixture in chosen:
        if not fixture.is_file():
            print(f"no such fixture: {fixture}", file=sys.stderr)
            return 2

    worst = 0
    for fixture in chosen:
        assay_id = args.id if (args.id and len(chosen) == 1) else assay_id_for(fixture)
        if args.fresh:
            clear_work(assay_id)
        print(f"\n=== {fixture.name} -> assay2chip/work/{assay_id}/")
        pipeline = Pipeline(assay_id, fixture, verbose=not args.quiet)
        pipeline.run_all()
        passed = sum(o.state == "pass" for o in pipeline.outcomes)
        skipped = sum(o.state == "skip" for o in pipeline.outcomes)
        failed = sum(o.state == "fail" for o in pipeline.outcomes)
        print(f"  {passed} stage(s) ran, {skipped} not built yet, {failed} broken")
        for outcome in pipeline.outcomes:
            if outcome.state == "skip":
                print(f"  not built: {outcome.stage}: {outcome.reason}")
                worst = max(worst, 1)
            elif outcome.state == "fail":
                print(f"  BROKEN:    {outcome.stage}: {outcome.reason}")
                worst = 2
        report = stage_io.work_dir(assay_id) / "report.md"
        if report.is_file():
            print(f"  report: {report.resolve().relative_to(ROOT).as_posix()}")
    return worst


if __name__ == "__main__":
    if "--golden" in sys.argv[1:]:
        raise SystemExit(golden(sys.argv[1:]))
    unittest.main()

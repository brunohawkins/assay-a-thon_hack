"""autochip core: agent client (JSON extraction, repair loop, replay, CLI/API backends), constraints loading
('extends', consistency, presets) and the CLI orchestrator (resume paths, stale chips, errors).

No network: the live backends are exercised with subprocess.run / urlopen patched.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autochip import cli, constraints as C, llm  # noqa: E402
from autochip.llm import Agent, AgentError, extract_json  # noqa: E402

EXAMPLE = ROOT / "autochip" / "examples" / "spc_capsule_protocol.md"


# ============================================================================================ JSON extraction
class ExtractJson(unittest.TestCase):
    def test_plain_and_braces_inside_strings(self):
        self.assertEqual(extract_json('{"a": "x}y{", "b": {"c": "}"}}'), {"a": "x}y{", "b": {"c": "}"}})

    def test_prose_with_braces_before_and_after(self):
        text = 'Use {placeholders} as told. Here it is:\n{"chips": [1, 2]}\nDone {ok}.'
        self.assertEqual(extract_json(text), {"chips": [1, 2]})

    def test_fence_wins_over_an_example_in_the_prose(self):
        text = 'An example would be {"chips": [], "note": "much longer example object than the answer"}.\n' \
               '```json\n{"chips": [1]}\n```'
        self.assertEqual(extract_json(text), {"chips": [1]})

    def test_largest_object_wins_without_fences(self):
        self.assertEqual(extract_json('{"x": 1} then the answer {"chips": [1, 2, 3], "id": "p"}'),
                         {"chips": [1, 2, 3], "id": "p"})

    def test_fence_inside_a_string_and_unlabelled_fence(self):
        text = '```\n{"note": "write ```code``` here", "n": 1}\n```'
        self.assertEqual(extract_json(text), {"note": "write ```code``` here", "n": 1})
        self.assertEqual(extract_json('``` json\n{"n": 2}\n```'), {"n": 2})

    def test_nothing_usable(self):
        for bad in ("", "   ", "no json here", '{"truncated": [1, 2', "[1, 2]"):
            with self.assertRaises(AgentError):
                extract_json(bad)


# ============================================================================================ agent loop
class FakeAgent(Agent):
    """claude-cli agent whose calls return queued replies (or raise) instead of running the CLI."""

    def __init__(self, replies, **kw):
        super().__init__(backend="claude-cli", quiet=True, **kw)
        self.replies = list(replies)
        self.seen: list[list[dict]] = []

    def _call(self, system, messages):
        self.seen.append(messages)
        r = self.replies.pop(0)
        self._meta = {}
        if isinstance(r, Exception):
            raise r
        if isinstance(r, tuple):            # (text, stop_reason)
            self._meta = {"stop_reason": r[1]}
            return r[0]
        return r


class AskJson(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_repair_loop_sends_errors_back_and_logs(self):
        a = FakeAgent(['{"n": 1}', 'fixed: ```json\n{"n": 2}\n```'], log_dir=self.dir)
        obj = a.ask_json("probe", "sys", "user", validate=lambda o: [] if o["n"] == 2 else ["n must be 2"])
        self.assertEqual(obj, {"n": 2})
        self.assertIn("n must be 2", a.seen[1][-1]["content"])
        self.assertEqual(a.seen[1][1], {"role": "assistant", "content": '{"n": 1}'})
        log = json.loads((self.dir / "probe.log.json").read_text())
        self.assertTrue(log["accepted"])
        self.assertEqual([e["errors"] for e in log["exchanges"]], [["n must be 2"], []])
        self.assertEqual(json.loads((self.dir / "probe.json").read_text()), {"n": 2})

    def test_validator_crash_goes_back_to_the_agent(self):
        a = FakeAgent(['{"wrong": 1}', '{"chips": []}'], log_dir=self.dir)
        obj = a.ask_json("probe", "s", "u", validate=lambda o: [] if len(o["chips"]) == 0 else ["x"])
        self.assertEqual(obj, {"chips": []})
        self.assertIn("KeyError", a.seen[1][-1]["content"])

    def test_failure_removes_a_stale_answer_and_keeps_the_log(self):
        (self.dir / "probe.json").write_text('{"old": true}')
        a = FakeAgent(["nope", "still nope", "no"], log_dir=self.dir)
        with self.assertRaises(AgentError):
            a.ask_json("probe", "s", "u")
        self.assertFalse((self.dir / "probe.json").exists())
        log = json.loads((self.dir / "probe.log.json").read_text())
        self.assertFalse(log["accepted"])
        self.assertEqual(len(log["exchanges"]), 3)

    def test_call_error_is_logged(self):
        a = FakeAgent([AgentError("claude CLI timed out after 1s")], log_dir=self.dir)
        with self.assertRaises(AgentError):
            a.ask_json("probe", "s", "u")
        log = json.loads((self.dir / "probe.log.json").read_text())
        self.assertIn("timed out", log["exchanges"][0]["error"])
        self.assertEqual(log["user"], "u")

    def test_cut_off_reply_asks_for_a_shorter_answer(self):
        a = FakeAgent([('{"chips": [{"id": "a"}, {"id": "b"', "max_tokens"), '{"chips": []}'], log_dir=self.dir)
        self.assertEqual(a.ask_json("probe", "s", "u"), {"chips": []})
        self.assertIn("cut off", a.seen[1][-1]["content"])

    def test_rules_backend_returns_none(self):
        self.assertIsNone(Agent(backend="rules", quiet=True).ask_json("x", "s", "u"))


class Replay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name) / "src"
        self.src.mkdir()
        (self.src / "partition__resplit_ab.json").write_text('{"chips": [1]}')
        (self.src / "partition__resplit_ab.log.json").write_text(json.dumps(
            {"backend": "api", "system": "SYS", "user": "USER", "exchanges": [{"attempt": 0, "reply": "r"}]}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_replay_reads_the_key_and_copies_the_original_log(self):
        dst = Path(self.tmp.name) / "dst"
        a = Agent(backend="replay", replay_dir=self.src, log_dir=dst, quiet=True)
        self.assertEqual(a.ask_json("partition", "s", "u", key="resplit_ab"), {"chips": [1]})
        log = json.loads((dst / "partition__resplit_ab.log.json").read_text())
        self.assertEqual((log["system"], log["user"]), ("SYS", "USER"))
        self.assertIn("replayed_from", log)

    def test_replay_into_its_own_folder_keeps_the_log(self):
        before = (self.src / "partition__resplit_ab.log.json").read_text()
        a = Agent(backend="replay", replay_dir=self.src, log_dir=self.src, quiet=True)
        a.ask_json("partition", "s", "u", key="resplit_ab")
        self.assertEqual((self.src / "partition__resplit_ab.log.json").read_text(), before)

    def test_missing_broken_or_invalid_answer(self):
        a = Agent(backend="replay", replay_dir=self.src, quiet=True)
        with self.assertRaises(AgentError):
            a.ask_json("partition", "s", "u")
        (self.src / "extract.json").write_text("{not json")
        with self.assertRaises(AgentError):
            a.ask_json("extract", "s", "u")
        with self.assertRaises(AgentError):
            a.ask_json("partition", "s", "u", key="resplit_ab", validate=lambda o: ["bad"])

    def test_replay_folder_must_exist(self):
        with self.assertRaises(ValueError):
            Agent(backend="replay", replay_dir=Path(self.tmp.name) / "nope")
        with self.assertRaises(ValueError):
            Agent(backend="replay")


# ============================================================================================ backends
class CliBackend(unittest.TestCase):
    def _run(self, stdout, returncode=0, stderr="", model=None):
        a = Agent(backend="claude-cli", model=model, quiet=True)
        done = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)
        with mock.patch("autochip.llm.subprocess.run", return_value=done) as run:
            text = a._call("SYSTEM", [{"role": "user", "content": "hi"}])
        return text, run.call_args

    def test_command_replaces_the_system_prompt_and_has_no_tools(self):
        text, call = self._run(json.dumps({"type": "result", "is_error": False, "result": '{"n": 1}'}), model="sonnet")
        cmd = call.args[0]
        self.assertEqual(text, '{"n": 1}')
        self.assertEqual(cmd[cmd.index("--system-prompt") + 1], "SYSTEM")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertNotIn("--append-system-prompt", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
        self.assertIn("-p", cmd)
        self.assertNotEqual(Path(call.kwargs["cwd"]).resolve(), Path.cwd().resolve())   # no CLAUDE.md pick-up
        self.assertIn("hi", call.kwargs["input"])

    def test_result_after_noise_and_list_form(self):
        rec = {"type": "result", "is_error": False, "result": "ok"}
        self.assertEqual(self._run("warning: something\n" + json.dumps(rec))[0], "ok")
        self.assertEqual(self._run(json.dumps([{"type": "system"}, rec]))[0], "ok")

    def test_errors(self):
        with self.assertRaises(AgentError):
            self._run(json.dumps({"type": "result", "is_error": True, "subtype": "error", "result": "quota"}))
        with self.assertRaises(AgentError):
            self._run("", returncode=1, stderr="not logged in")
        with self.assertRaises(AgentError):
            self._run("plain text, not the json record")
        a = Agent(backend="claude-cli", quiet=True)
        with mock.patch("autochip.llm.subprocess.run", side_effect=FileNotFoundError("claude")):
            with self.assertRaises(AgentError):
                a._call("s", [{"role": "user", "content": "u"}])
        with mock.patch("autochip.llm.subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 1)):
            with self.assertRaises(AgentError):
                a._call("s", [{"role": "user", "content": "u"}])


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ApiBackend(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k"})
        self.env.start()
        self.delays = mock.patch.object(llm, "RETRY_DELAYS_S", (0, 0))
        self.delays.start()

    def tearDown(self):
        self.env.stop()
        self.delays.stop()

    def _err(self, code):
        return urllib.error.HTTPError(llm.API_URL, code, "x", {}, io.BytesIO(b'{"error": "overloaded"}'))

    def test_retries_overloaded_then_succeeds(self):
        ok = _Resp(json.dumps({"stop_reason": "end_turn", "content": [{"type": "text", "text": "{}"}]}).encode())
        with mock.patch("autochip.llm.urllib.request.urlopen", side_effect=[self._err(529), ok]) as u:
            self.assertEqual(Agent(backend="api", quiet=True)._call("s", [{"role": "user", "content": "u"}]), "{}")
        self.assertEqual(u.call_count, 2)

    def test_no_retry_on_bad_request_and_timeouts_are_agent_errors(self):
        a = Agent(backend="api", quiet=True)
        msgs = [{"role": "user", "content": "u"}]
        with mock.patch("autochip.llm.urllib.request.urlopen", side_effect=[self._err(400)]) as u:
            with self.assertRaises(AgentError):
                a._call("s", msgs)
        self.assertEqual(u.call_count, 1)
        with mock.patch("autochip.llm.urllib.request.urlopen", side_effect=TimeoutError("read timed out")):
            with self.assertRaises(AgentError):
                a._call("s", msgs)
        with mock.patch("autochip.llm.urllib.request.urlopen", return_value=_Resp(b"<html>proxy</html>")):
            with self.assertRaises(AgentError):
                a._call("s", msgs)

    def test_max_tokens_is_recorded(self):
        ok = _Resp(json.dumps({"stop_reason": "max_tokens", "content": [{"type": "text", "text": '{"a": ['}]}).encode())
        a = Agent(backend="api", quiet=True)
        with mock.patch("autochip.llm.urllib.request.urlopen", return_value=ok):
            a._call("s", [{"role": "user", "content": "u"}])
        self.assertEqual(a._meta["stop_reason"], "max_tokens")


# ============================================================================================ constraints
class Constraints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, name, obj):
        p = self.dir / name
        p.write_text(obj if isinstance(obj, str) else json.dumps(obj))
        return p

    def test_presets_load_and_are_consistent(self):
        for p in C.list_presets():
            c = C.load(p)
            self.assertEqual(c["id"], p)
            self.assertEqual(C.consistency_errors(c), [], p)

    def test_preset_ports_fit_their_connector(self):
        # A Luer hub or a NanoPort is ~8-10 mm across: ports can't sit closer than that.
        for p in C.list_presets():
            c = C.load(p)
            if C.get(c, "ports.connector") in ("luer", "nanoport"):
                self.assertGreaterEqual(C.get(c, "ports.min_pitch_mm"), 9, p)

    def test_pdms_preset_is_one_flow_layer_plus_control(self):
        c = C.load("pdms_soft_lithography")
        self.assertFalse(C.multilayer(c))
        self.assertFalse(C.crossings_allowed(c))
        self.assertTrue(C.get(c, "fabrication.on_chip_valves.allowed"))

    def test_extends_merge_lists_replace_and_null_clears(self):
        p = self.put("lab.json", {"id": "lab", "extends": "assayathon_demo",
                                  "envelope": {"max_chip_mm": {"x": 28}, "edge_margin_mm": None},
                                  "off_chip": {"equipment": ["syringe_pump"]}, "ports": {"max_per_chip": None}})
        c = C.load(p)
        self.assertEqual(c["id"], "lab")
        self.assertEqual(c["envelope"]["max_chip_mm"], {"x": 28, "y": 20})        # objects merge key by key
        self.assertEqual(c["off_chip"]["equipment"], ["syringe_pump"])             # lists are replaced
        self.assertIsNone(C.get(c, "ports.max_per_chip"))                          # null = no limit
        self.assertEqual(C.get(c, "fabrication.method"), "resin_print_hot_emboss")
        self.assertNotIn("extends", c)

    def test_extends_a_file_relative_to_the_extending_file(self):
        sub = self.dir / "sub"
        sub.mkdir()
        self.put("sub/base.json", {"id": "base", "extends": "resin_print_multilayer", "ports": {"max_per_chip": 7}})
        child = self.put("sub/child.json", {"id": "child", "extends": "base.json", "envelope": {"max_chips": 3}})
        cwd = os.getcwd()
        os.chdir(ROOT)                      # a working directory that is not the file's folder
        try:
            c = C.load(child)
        finally:
            os.chdir(cwd)
        self.assertEqual((c["ports"]["max_per_chip"], c["envelope"]["max_chips"], c["id"]), (7, 3, "child"))

    def test_extends_loop_and_bad_values(self):
        self.put("a.json", {"id": "a", "extends": "b.json", "fabrication": {"method": "other"}})
        self.put("b.json", {"id": "b", "extends": "a.json", "fabrication": {"method": "other"}})
        with self.assertRaisesRegex(C.ConstraintsError, "loop"):
            C.load(self.dir / "a.json")
        with self.assertRaisesRegex(C.ConstraintsError, "extends"):
            C.load(self.put("c.json", {"id": "c", "extends": ["unconstrained"]}))
        with self.assertRaisesRegex(C.ConstraintsError, "not valid JSON"):
            C.load(self.put("d.json", '{"id": "d", '))
        with self.assertRaisesRegex(C.ConstraintsError, "one JSON object"):
            C.load(self.put("e.json", "[1]"))
        with self.assertRaisesRegex(C.ConstraintsError, "no constraints file"):
            C.load(self.dir / "missing.json")

    def test_contradictions_are_refused(self):
        cases = {
            "crossings": {"extends": "unconstrained", "fabrication": {"multilayer": {"allowed": False}}},
            "depth": {"extends": "unconstrained", "fabrication": {"depth": {"min_mm": 0.5, "max_mm": 0.2}}},
            "allowed depth": {"extends": "laser_cut_laminate", "fabrication": {"depth": {"max_mm": 0.3}}},
            "chip > build area": {"extends": "assayathon_demo", "envelope": {"max_chip_mm": {"x": 200, "y": 20}}},
            "pitch < hole": {"extends": "unconstrained", "ports": {"min_diameter_mm": 3, "min_pitch_mm": 2}},
            "margins": {"extends": "assayathon_demo", "envelope": {"edge_margin_mm": 10}},
            "flow range": {"extends": "unconstrained", "off_chip": {"flow_range_ul_min": [10, 1]}},
            "temp range": {"extends": "unconstrained", "operating": {"temp_range_c": [90, 4]}},
        }
        for i, (what, body) in enumerate(cases.items()):
            with self.subTest(what):
                with self.assertRaisesRegex(C.ConstraintsError, "contradict"):
                    C.load(self.put(f"x{i}.json", {"id": f"x{i}", **body}))
        # Switching a preset feature off in one line is fine (leftover valve type / vias are ignored).
        ok = self.put("ok.json", {"id": "ok", "extends": "resin_print_multilayer",
                                  "fabrication": {"multilayer": {"allowed": False}, "channel_crossings_allowed": False}})
        self.assertFalse(C.multilayer(C.load(ok)))

    def test_envelope_needs_both_sides(self):
        with self.assertRaises(C.ConstraintsError):
            C.load(self.put("h.json", {"id": "h", "fabrication": {"method": "other"}, "envelope": {"max_chip_mm": {"x": 30}}}))

    def test_summary_tells_the_agent_every_limit(self):
        c = C.load("pdms_soft_lithography")
        c["operating"] = {"max_pressure_kpa": 200, "biocompatible_required": True, "sterilisable": True}
        c["off_chip"]["flow_range_ul_min"] = [0.1, 100]
        c["parts"]["forbidden"] = ["spiral_channel"]
        s = C.summary(c)
        for want in ("0.025, 0.05, 0.1 mm", "depth/width at most 5", "200 kPa", "biocompatible", "sterilisation",
                     "0.1-100 uL/min", "never: spiral_channel", "connector press_fit_tubing", "edge margin 2 mm"):
            self.assertIn(want, s)
        u = C.summary(C.load("unconstrained"))
        self.assertNotIn("max no limit", u)
        self.assertNotIn("None", u)


# ============================================================================================ CLI orchestrator
def _main(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = cli.main([str(a) for a in argv])
        except SystemExit as e:          # argparse errors
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


class Cli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name) / "base"
        rc, out, err = _main("run", EXAMPLE, "--backend", "rules", "--out", cls.base, "--quiet")
        assert rc == 0, err
        cls.report = (cls.base / "report.md").read_text()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def copy(self, name: str) -> Path:
        d = Path(self.tmp.name) / name
        shutil.copytree(self.base, d)
        return d

    def test_layout_record_written(self):
        rec = json.loads((self.base / cli.LAYOUT_RECORD).read_text())
        plan = json.loads((self.base / "chip_plan.json").read_text())
        self.assertEqual(set(rec), {c["id"] for c in plan["chips"]})

    def test_resume_from_runplan_and_report_keeps_layout_findings(self):
        for stage in ("runplan", "report"):
            with self.subTest(stage):
                d = self.copy(f"from_{stage}")
                rc, _, err = _main("run", "--resume", d, "--from-stage", stage, "--backend", "rules", "--quiet")
                self.assertEqual(rc, 0, err)
                self.assertEqual((d / "report.md").read_text(), self.report)

    def test_resume_ignores_stale_chip_files_and_rechecks_edited_specs(self):
        d = self.copy("stale")
        (d / "chips" / "zz_old_chip.json").write_text("{}")
        plan = json.loads((d / "chip_plan.json").read_text())
        cid = plan["chips"][0]["id"]
        spec = json.loads((d / "chips" / f"{cid}.json").read_text())
        spec["description"] = "edited by hand"
        (d / "chips" / f"{cid}.json").write_text(json.dumps(spec))
        chips = cli._load_layout(d, plan, json.loads((d / "constraints.json").read_text()), "report", lambda *a: None)
        self.assertEqual(set(chips), {c["id"] for c in plan["chips"]})
        self.assertEqual(chips[cid]["spec"]["description"], "edited by hand")

    def test_resplit_or_edited_plan_removes_old_chip_files(self):
        d = self.copy("prune")
        rec = json.loads((d / cli.LAYOUT_RECORD).read_text())
        rec["zz_old_chip"] = {"spec_sha1": "x", "violations": []}
        (d / cli.LAYOUT_RECORD).write_text(json.dumps(rec))
        for f in ("chips/zz_old_chip.json", "designs/zz_old_chip.json", "runs/zz_old_chip.protocol.json",
                  "cad/zz_old_chip/README.md", "designs/keep_me.json", "cad/keep_me/x.stl"):
            (d / f).parent.mkdir(parents=True, exist_ok=True)
            (d / f).write_text("{}")
        rc, _, err = _main("run", "--resume", d, "--from-stage", "layout", "--backend", "rules", "--quiet")
        self.assertEqual(rc, 0, err)
        for f in ("chips/zz_old_chip.json", "designs/zz_old_chip.json", "runs/zz_old_chip.protocol.json", "cad/zz_old_chip"):
            self.assertFalse((d / f).exists(), f)
        for f in ("designs/keep_me.json", "cad/keep_me/x.stl"):        # not ours: never named by a plan or record
            self.assertTrue((d / f).exists(), f)

    def test_resume_without_from_stage_continues(self):
        d = self.copy("cont")
        self.assertEqual(cli.continue_stage(d), "report")
        for f in (d / "runs").glob("*.protocol.json"):
            f.unlink()
        self.assertEqual(cli.continue_stage(d), "runplan")
        (d / "protocol_ir.json").unlink()
        self.assertEqual(cli.continue_stage(d), "extract")
        rc, _, err = _main("run", "--resume", d, "--backend", "rules", "--quiet")
        self.assertEqual(rc, 0, err)
        self.assertTrue((d / "protocol_ir.json").exists())

    def test_errors_leave_no_folder_behind(self):
        t = Path(self.tmp.name)
        bad = t / "bad.json"
        bad.write_text('{"id": "bad", ')
        cases = [("run", t / "missing.md", "--backend", "rules", "--out", t / "e1"),
                 ("run", EXAMPLE, "--backend", "rules", "--out", t / "e2", "--constraints", bad),
                 ("run", EXAMPLE, "--backend", "replay", "--out", t / "e3"),
                 ("run", "--resume", t / "e4", "--from-stage", "layout", "--backend", "rules")]
        for args in cases:
            with self.subTest(args[1:3]):
                rc, _, err = _main(*args)
                self.assertEqual(rc, 2, err)
                self.assertFalse(Path(args[args.index("--out") + 1] if "--out" in args else args[2]).exists())
        rc, _, err = _main("constraints", "--show", bad)
        self.assertEqual(rc, 2)
        self.assertIn("not valid JSON", err)

    def test_resume_from_ingest_needs_an_input(self):
        rc, _, err = _main("run", "--resume", self.base, "--from-stage", "ingest", "--backend", "rules")
        self.assertEqual(rc, 2)
        self.assertIn("input file or --prompt", err)


class FeedbackLoop(unittest.TestCase):
    """partition <-> layout rounds: rules and live agents re-split, a replay doesn't; same plan / same errors stop."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name) / "base"
        rc, _, err = _main("run", EXAMPLE, "--backend", "rules", "--out", cls.base, "--quiet")
        assert rc == 0, err

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _run(self, name, backend, variant_plans=False, fixed_errors=True, rounds=3):
        from autochip.stages import layout, partition
        d = Path(self.tmp.name) / name
        shutil.copytree(self.base, d)
        real_part, real_layout = partition.run, layout.run
        calls = {"partition": 0, "layout": 0}

        first: list[dict] = []

        def fake_partition(*a, **k):
            calls["partition"] += 1
            plan = real_part(*a, **k)
            first.append(first[0] if first else plan)
            if variant_plans:                       # a different plan every round
                return dict(first[0], id=f"{first[0].get('id', 'plan')}_{calls['partition']}")
            return json.loads(json.dumps(first[0]))  # the same plan every round, whatever the rules do

        def fake_layout(*a, **k):
            calls["layout"] += 1
            res = real_layout(*a, **k)
            cid = next(iter(res))
            msg = "too big" if fixed_errors else f"too big, round {calls['layout']}"
            res[cid]["violations"] = res[cid]["violations"] + [{"severity": "error", "rule": "test", "message": msg}]
            return res

        extra = ["--replay", self.base / "agent"] if backend == "replay" else []
        with mock.patch.object(partition, "run", fake_partition), mock.patch.object(layout, "run", fake_layout):
            rc, out, err = _main("run", "--resume", d, "--from-stage", "partition", "--backend", backend,
                                 "--rounds", rounds, *extra)
        self.assertEqual(rc, 0, err)
        return calls, out

    def test_rules_stop_when_the_resplit_gives_the_same_plan(self):
        calls, out = self._run("same_plan", "rules")
        self.assertEqual(calls, {"partition": 2, "layout": 1})
        self.assertIn("same plan", out)

    def test_rules_stop_when_the_errors_do_not_change(self):
        calls, out = self._run("same_errors", "rules", variant_plans=True)
        self.assertEqual(calls, {"partition": 2, "layout": 2})
        self.assertIn("same constraint errors", out)

    def test_rules_use_all_rounds_while_things_change(self):
        calls, _ = self._run("changing", "rules", variant_plans=True, fixed_errors=False, rounds=2)
        self.assertEqual(calls, {"partition": 3, "layout": 3})

    def test_replay_resplits_from_saved_answers(self):
        # A replay asks partition again with the layout errors (it finds the saved re-split answer, if any);
        # here the re-split gives the same plan, so the loop stops after one re-split.
        from autochip.stages import layout, partition
        d = Path(self.tmp.name) / "replay"
        shutil.copytree(self.base, d)
        plan = json.loads((d / "chip_plan.json").read_text())
        calls = []
        real_layout = layout.run

        def fake_layout(*a, **k):
            res = real_layout(*a, **k)
            cid = next(iter(res))
            res[cid]["violations"] = res[cid]["violations"] + [{"severity": "error", "rule": "t", "message": "x"}]
            return res
        with mock.patch.object(partition, "run", lambda *a, **k: calls.append(k.get("feedback")) or plan), \
                mock.patch.object(layout, "run", fake_layout):
            rc, _, err = _main("run", "--resume", d, "--from-stage", "partition", "--backend", "replay", "--quiet")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[0])
        self.assertTrue(calls[1] and calls[1][0].endswith(': x'))


if __name__ == "__main__":
    unittest.main()

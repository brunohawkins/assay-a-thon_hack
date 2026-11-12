"""The module catalogue: every pipeline stage, in chain order, and what each chip does.

    python tools/module_catalog.py            # writes docs/modules.md, docs/modules.json,
                                              # docs/videos/manifest.json
    python tools/module_catalog.py --check    # exit 1 if any of the three is out of date

Everything is read from data, nothing is hand-written, so re-run it after any merge:

  protocols/pipeline_mvp.json     stages, chain order, handoffs, roadmap (off-chip) stages
  protocols/pipeline_status.md    off-chip bench stages ("2b", "3b" rows), status, broken joins
  chip/<id>.json                  ports, chambers, thermal zones, footprint, description
  chip/<base>_target_v*.json      the FULL SPEC design beside a demo chip, when one exists
  protocols/<id>.json             the ordered steps (the module's exact function)
  runs/index.json                 which protocol a chip runs, when the pipeline does not say
  cad/figures/, cad/exports/<id>/print/, assay2chip/work/pipeline_mvp/cfd/
                                  plan SVGs, 3D renders, CFD outputs
  tools/demo_plan.py, docs/demo.md
                                  demo procedure and the shot to film, when present
  docs/videos/manifest.json       recorded video urls (kept across regenerations)

Any of these may be missing: the entry says so instead of failing. The generator is
deterministic (no timestamps, sorted globs), so --check works in CI.
"""

from __future__ import annotations

import argparse
import importlib.util
import itertools
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PIPELINE = "protocols/pipeline_mvp.json"
STATUS = "protocols/pipeline_status.md"
OUT_MD = "docs/modules.md"
OUT_JSON = "docs/modules.json"
VIDEO_MANIFEST = "docs/videos/manifest.json"
CFD_DIR = "assay2chip/work/pipeline_mvp/cfd"

# The six demo chips being printed now. FROZEN: listed here only to label them; this tool
# never writes to a chip file.
PRINTED = [
    {"slot": "Module 1", "chip": "chip/module1_v1.json"},
    {"slot": "Module 2", "chip": "chip/spc_demo_v1.json"},
    {"slot": "Module 3", "chip": "chip/module3_v1_translated.json"},
    {"slot": "Module 4", "chip": "chip/module4_v1.json"},
    {"slot": "Module 5", "chip": "chip/module5_v1.json"},
    {"slot": "Module 6", "chip": "chip/module6_v1.json"},
]
PRINTED_BY_CHIP = {p["chip"]: p["slot"] for p in PRINTED}

DEMO_FOOTPRINT = (30, 20)
OFF_CHIP_WORDS = re.compile(r"off-chip|off chip|at the bench|bench tube|in a tube|by hand|bought", re.I)
LIMIT_WORDS = re.compile(
    r"\b(cannot|can not|can't|not printed|unbuildable|below the|(?:fabrication|feature|printable|in-plane) floor|"
    r"clamped|lost|TBC|undocumented|untested|no longer|must be re-derived|does not match|too shallow|"
    r"too small|duplicate)\b", re.I)


# ---------------------------------------------------------------- reading


def _read_json(root: Path, rel: str | None):
    if not rel:
        return None
    p = root / rel
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _rel_if(root: Path, rel: str) -> str | None:
    return rel if (root / rel).exists() else None


def _glob(root: Path, pattern: str) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.glob(pattern) if p.is_file())


def _sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z(])", text) if s.strip()]


def _clip(text: str, n: int = 220) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _md_tables(md: str) -> dict[str, list[dict]]:
    """Every pipe table in a markdown file, keyed by the heading above it."""
    tables: dict[str, list[dict]] = {}
    heading = ""
    header: list[str] | None = None
    for line in md.splitlines():
        if line.startswith("#"):
            heading, header = line.lstrip("#").strip(), None
            continue
        if not line.startswith("|"):
            header = None
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            header = cells
            tables.setdefault(heading, [])
        elif set("".join(cells)) <= set("-: "):
            continue
        else:
            tables[heading].append(dict(zip(header, cells)))
    return tables


def _md_bullets(md: str, heading_word: str) -> list[str]:
    out, inside = [], False
    for line in md.splitlines():
        if line.startswith("#"):
            inside = heading_word.lower() in line.lower()
            continue
        if inside and line.lstrip().startswith(("- ", "* ")):
            out.append(line.lstrip()[2:].strip())
    return out


def _strip_md(s: str) -> str:
    return re.sub(r"[`*]", "", s or "").strip()


def load_status(root: Path) -> dict:
    p = root / STATUS
    if not p.is_file():
        return {"present": False, "chain": [], "joins": [], "notes": []}
    md = p.read_text()
    tables = _md_tables(md)
    chain = next((rows for h, rows in tables.items() if h.lower().startswith("chain")), [])
    joins = next((rows for h, rows in tables.items() if "join" in h.lower()), [])
    return {"present": True, "chain": chain, "joins": joins, "notes": _md_bullets(md, "notes")}


# ---------------------------------------------------------------- demo plan


def _demo_from_plan_py(root: Path) -> dict[str, dict]:
    """tools/demo_plan.py, if present: any plan it exposes, keyed by chip id or module."""
    path = root / "tools" / "demo_plan.py"
    if not path.is_file():
        return {}
    try:
        spec = importlib.util.spec_from_file_location("_demo_plan_for_catalog", path)
        mod = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(path.parent))
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.path.remove(str(path.parent))
        plan = None
        for name in ("demo_plan", "build_plan", "plan", "load_plan"):
            fn = getattr(mod, name, None)
            if callable(fn):
                try:
                    plan = fn()
                    break
                except TypeError:
                    try:
                        plan = fn(root)
                        break
                    except (Exception, SystemExit):  # noqa: BLE001 - an unknown API must not break the catalogue
                        continue
                except (Exception, SystemExit):  # noqa: BLE001
                    continue
        if plan is None:
            for name in ("PLAN", "DEMO_PLAN", "MODULES", "STEPS"):
                if hasattr(mod, name):
                    plan = getattr(mod, name)
                    break
    except (Exception, SystemExit):  # noqa: BLE001
        return {}
    try:
        return _normalise_plan(plan, "tools/demo_plan.py")
    except (Exception, SystemExit):  # noqa: BLE001
        return {}


def _normalise_plan(plan, source: str) -> dict[str, dict]:
    if isinstance(plan, dict):
        for key in ("modules", "chips", "steps"):
            if isinstance(plan.get(key), (list, dict)):
                plan = plan[key]
                break
    items = plan.items() if isinstance(plan, dict) else ((None, x) for x in (plan or []))
    out: dict[str, dict] = {}
    for key, entry in items:
        if not isinstance(entry, dict):
            continue
        keys = {str(k) for k in (key, entry.get("chip_id"), entry.get("chip"), entry.get("module"),
                                 entry.get("slot")) if k}
        keys |= {Path(k).stem for k in keys if k.endswith(".json")}
        proc = entry.get("procedure") or entry.get("steps") or entry.get("demo") or []
        if isinstance(proc, str):
            proc = [proc]
        proc = [x if isinstance(x, str) else (x.get("text") or x.get("action") or json.dumps(x, sort_keys=True))
                for x in proc]
        shot = entry.get("shot") or entry.get("film") or entry.get("video") or entry.get("camera")
        if isinstance(shot, dict):
            shot = shot.get("text") or shot.get("what") or json.dumps(shot, sort_keys=True)
        rec = {"source": source, "procedure": [_clip(str(x), 300) for x in proc], "shot": shot or None}
        for k in keys:
            out[k] = rec
    return out


def _demo_from_md(root: Path) -> dict[str, dict]:
    """docs/demo.md, if present: the section whose heading names the chip or the slot."""
    p = root / "docs" / "demo.md"
    if not p.is_file():
        return {}
    sections: list[tuple[str, list[str]]] = []
    for line in p.read_text().splitlines():
        if re.match(r"#{2,4}\s", line):
            sections.append((line.lstrip("#").strip(), []))
        elif sections:
            sections[-1][1].append(line)
    out: dict[str, dict] = {}
    for title, lines in sections:
        keys = set(re.findall(r"[a-z0-9_]+_v\d+(?:_[a-z]+)?", title))
        keys |= {f"Module {n}" for n in re.findall(r"\bModule\s+(\d+)\b", title, re.I)}
        if not keys:
            continue
        proc = [re.sub(r"^(\d+[.)]|[-*])\s+", "", ln.strip()) for ln in lines
                if re.match(r"\s*(\d+[.)]|[-*])\s+", ln)]
        shot = next((re.sub(r"^(\d+[.)]|[-*])\s+", "", ln.strip()) for ln in lines
                     if re.search(r"\b(film|shot|camera|record)\b", ln, re.I)), None)
        rec = {"source": "docs/demo.md", "procedure": [_clip(x, 300) for x in proc], "shot": shot}
        for k in keys:
            out.setdefault(k, rec)
    return out


def load_demo(root: Path) -> dict[str, dict]:
    plan = _demo_from_md(root)
    plan.update(_demo_from_plan_py(root))  # the generated plan wins over prose
    return plan


# ---------------------------------------------------------------- per module


def _fmt_dur(s) -> str:
    if s is None:
        return "—"
    try:
        s = float(s)
    except (TypeError, ValueError):
        return str(s)
    if s < 120:
        return f"{s:g} s"
    if s < 7200:
        return f"{s / 60:.0f} min"
    if s < 172800:
        return f"{s / 3600:.1f} h".replace(".0 h", " h")
    return f"{s / 86400:.1f} d".replace(".0 d", " d")


def _num(x):
    if x is None or isinstance(x, bool):
        return x
    try:
        return round(float(x), 3)
    except (TypeError, ValueError):
        return x  # "TBC" and the like are kept as written


def exact_steps(protocol: dict | None) -> list[dict]:
    """The ordered protocol steps, reduced to what the module physically does."""
    if not protocol:
        return []
    out = []
    for i, st in enumerate(protocol.get("steps") or [], 1):
        if not isinstance(st, dict):
            continue
        targets = _str_list(st.get("targets") or st.get("target"))
        reagents = _str_list(st.get("reagents") or st.get("reagent"))
        kind = st.get("exchange_kind")
        if kind is None and st.get("operation") == "exchange":
            kind = "sweep (undeclared)"  # absence is held to the sweep rule (protocolgen/exchange.py)
        ports = st.get("ports") if isinstance(st.get("ports"), dict) else {}
        out.append({
            "step": st.get("step", i),
            "operation": st.get("operation"),
            "target": targets,
            "reagent": sorted(set(reagents), key=reagents.index),
            "volume_ul": _num(st.get("volume_ul")),
            "washes": st.get("washes"),
            "flow_ul_min": _num(st.get("flow_ul_min")),
            "duration_s": _num(st.get("duration_s")),
            "temp_c": st.get("temp_c"),
            "exchange_kind": kind,
            "feed": _str_list(ports.get("feed")),
            "vent": _str_list(ports.get("vent")),
        })
    return out


def _port_usage(steps: list[dict]) -> dict[str, dict]:
    use: dict[str, dict] = {}
    for st in steps:
        for role in ("feed", "vent"):
            for p in st[role]:
                use.setdefault(p, {"feed": 0, "vent": 0})[role] += 1
    return use


def _scale(spec: dict) -> str:
    fp = spec.get("footprint") or {}
    x, y = fp.get("x"), fp.get("y")
    size = f"{_cell(x)} x {_cell(y)} mm" if x and y else "footprint not stated"
    if spec.get("scale"):
        return f"{str(spec['scale']).upper()} {size}"
    if (x, y) == DEMO_FOOTPRINT or "DEMO SCALE" in (spec.get("description") or ""):
        return f"DEMO {size}"
    return f"TARGET {size}"


def _depth(spec: dict) -> float | None:
    layers = spec.get("layers") or []
    for layer in layers:
        if layer.get("id") in ("fluidic", "channels", "spacer"):
            return layer.get("thickness_mm")
    return layers[1].get("thickness_mm") if len(layers) == 3 else None


def _sort_key(x):
    return (0, x, "") if isinstance(x, (int, float)) else (1, 0, str(x))


def _thermal(spec: dict, steps: list[dict]) -> list[dict]:
    zones = [{"id": z.get("id"), "chambers": z.get("chambers", []), "min_c": z.get("min_temp_c"),
              "max_c": z.get("max_temp_c"), "purpose": z.get("purpose"), "source": "chip thermal_zones"}
             for z in spec.get("thermal_zones") or []]
    if zones:
        return zones
    temps: dict[str, set] = {}
    for st in steps:
        if st["temp_c"] is not None:
            for t in st["target"]:
                temps.setdefault(t, set()).add(_num(st["temp_c"]))
    by_set: dict[tuple, list[str]] = {}
    for chamber, ts in sorted(temps.items()):
        by_set.setdefault(tuple(sorted(ts, key=_sort_key)), []).append(chamber)
    return [{"id": "protocol_" + "_".join(chambers), "chambers": chambers, "min_c": ts[0], "max_c": ts[-1],
             "temps_c": list(ts), "purpose": None,
             "source": "protocol temperatures (no thermal_zones in the chip)"}
            for ts, chambers in sorted(by_set.items(), key=lambda kv: kv[1])]


def _str_list(x) -> list[str]:
    if x is None:
        return []
    return [str(i) for i in x] if isinstance(x, (list, tuple)) else [str(x)]


def _off_chip(spec: dict, build: dict | None) -> list[str]:
    out = []
    if isinstance(build, dict) and OFF_CHIP_WORDS.search(str(build.get("method") or "")):
        out.append(f"build: {build['method']}")
    for p in spec.get("ports") or []:
        hit = next((s for s in _sentences(str(p.get("note") or "")) if OFF_CHIP_WORDS.search(s)), None)
        if hit:
            out.append(f"{p.get('id')}: {_clip(hit, 180)}")
    hit = [s for s in _sentences(spec.get("description") or "") if OFF_CHIP_WORDS.search(s)]
    out += [_clip(s, 200) for s in hit[:3]]
    return out


def _limits(spec: dict, pipe_mod: dict | None, chain_no: str | None, chip_id: str, status: dict) -> list[dict]:
    out = []
    for s in [s for s in _sentences(spec.get("description") or "") if LIMIT_WORDS.search(s)][:4]:
        out.append({"source": "chip description", "text": _clip(s)})
    if pipe_mod and pipe_mod.get("note"):
        out.append({"source": PIPELINE, "text": _clip(pipe_mod["note"], 300)})
    for row in status["chain"]:
        if chip_id and f"`{chip_id}`" in row.get("Chip", "") and row.get("Status") not in (None, "BUILT"):
            out.append({"source": STATUS, "text": f"Chain status: {_strip_md(row['Status'])}"})
    for row in status["joins"]:
        nums = re.findall(r"\d+[a-z]?", row.get("Join", ""))
        if chain_no and chain_no in nums:
            out.append({"source": STATUS, "text": f"Join {_strip_md(row['Join'])}: {_clip(_strip_md(row.get('Problem', '')), 260)}"})
    for note in status["notes"]:
        if chip_id and chip_id in note:
            out.append({"source": STATUS, "text": _clip(_strip_md(note), 260)})
    return out


def _artifacts(root: Path, chip_id: str) -> dict:
    cfd_figs = _glob(root, f"{CFD_DIR}/figures/{chip_id}_s*_*.svg")
    steps = sorted({int(m.group(1)) for f in cfd_figs if (m := re.search(rf"{re.escape(chip_id)}_s(\d+)_", f))})
    return {
        "plan_svgs": _glob(root, f"cad/figures/{chip_id}_*.svg"),
        "renders_3d": sorted(set(_glob(root, f"cad/exports/{chip_id}/**/render*.*"))),
        "cfd": {
            "field": _rel_if(root, f"{CFD_DIR}/{chip_id}.field.json"),
            "mesh": _rel_if(root, f"{CFD_DIR}/{chip_id}.mesh.json"),
            "report": _rel_if(root, f"{CFD_DIR}/report.md") if cfd_figs else None,
            "steps_simulated": steps,
            "figures": cfd_figs,
        },
        "flow_runs": [],  # filled from runs/index.json
    }


def _protocol_for(root: Path, chip_id: str, pipe_mod: dict | None, runs: list[dict]) -> str | None:
    if pipe_mod and pipe_mod.get("protocol") and (root / pipe_mod["protocol"]).is_file():
        return pipe_mod["protocol"]
    for r in runs:
        if r.get("chip_id") == chip_id and not r.get("scenario") and not r.get("synthetic"):
            p = f"protocols/{r.get('protocol_id')}.json"
            if (root / p).is_file():
                return p
    return None


def _target_design(root: Path, chip_id: str) -> dict | None:
    base = re.sub(r"_v\d+.*$", "", chip_id)
    for rel in _glob(root, f"chip/{base}_target_v*.json"):
        spec = _read_json(root, rel) or {}
        return {"label": "FULL SPEC", "chip": rel, "id": spec.get("id"), "name": spec.get("name"),
                "scale": _scale(spec).replace("DEMO", "TARGET") if spec else None,
                "plan_svgs": _glob(root, f"cad/figures/{spec.get('id', Path(rel).stem)}_*.svg")}
    return None


def chip_entry(root: Path, chip_rel: str, pipe_mod: dict | None, chain_no: str | None,
               status: dict, runs: list[dict], demo: dict, videos: dict) -> dict:
    spec = _read_json(root, chip_rel) or {}
    chip_id = spec.get("id") or Path(chip_rel).stem
    proto_rel = _protocol_for(root, chip_id, pipe_mod, runs)
    steps = exact_steps(_read_json(root, proto_rel))
    use = _port_usage(steps)
    slot = PRINTED_BY_CHIP.get(chip_rel)
    if not slot:
        m = re.match(r"(Module \d+)", (pipe_mod or {}).get("name") or spec.get("name") or "")
        slot = m.group(1) if m else ((pipe_mod or {}).get("name") or spec.get("name") or chip_id).split(":")[0]
    art = _artifacts(root, chip_id)
    art["flow_runs"] = sorted(f"runs/{r['file']}" for r in runs
                              if r.get("chip_id") == chip_id and (root / "runs" / r.get("file", "")).is_file())
    d = demo.get(chip_id) or demo.get(chip_rel) or demo.get(slot) or (demo.get(pipe_mod["module"]) if pipe_mod else None)
    return {
        "chip_id": chip_id,
        "chip": chip_rel if (root / chip_rel).is_file() else None,
        "chip_name": spec.get("name"),
        "scale": _scale(spec) if spec else None,
        "fluidic_depth_mm": _depth(spec),
        "printed_for_demo": chip_rel in PRINTED_BY_CHIP,
        "slot": slot,
        "placeholder": spec.get("placeholder"),
        "protocol": proto_rel,
        "function": steps,
        "ports": [{"id": p.get("id"), "type": p.get("type"), "role": p.get("role"),
                   "fed_in_steps": use.get(p.get("id"), {}).get("feed", 0),
                   "vented_in_steps": use.get(p.get("id"), {}).get("vent", 0)}
                  for p in spec.get("ports") or [] if isinstance(p, dict)],
        "chambers": [{"id": c.get("id"), "type": c.get("type"), "block": c.get("block"),
                      "volume_ul": _num(c.get("volume_ul"))} for c in spec.get("chambers") or [] if isinstance(c, dict)],
        "thermal_zones": _thermal(spec, steps),
        "off_chip": _off_chip(spec, (pipe_mod or {}).get("build")),
        "known_limits": _limits(spec, pipe_mod, chain_no, chip_id, status),
        "full_spec": _target_design(root, chip_id),
        "artifacts": art,
        "demo": d,
        "video": videos.get(chip_id),
    }


# ---------------------------------------------------------------- chain


def _handoff_end(h: dict, side: str) -> dict:
    e = h.get(side) or {}
    if not isinstance(e, dict):
        return {"stage": str(e), "ports": []}
    ports = _str_list(e.get("ports") or e.get("port"))
    return {"stage": e.get("module") or e.get("roadmap"), "ports": ports}


def _handoff(h: dict, spec_to: dict | None) -> dict:
    particle = h.get("particle_um")
    psrc = "handoff" if particle is not None else None
    if particle is None and h.get("carries") == "capsules" and spec_to and (spec_to.get("capsule") or {}).get("diameter_um"):
        particle, psrc = spec_to["capsule"]["diameter_um"], "receiving chip design (capsule.diameter_um)"
    return {
        "id": h.get("id"),
        "carries": h.get("carries"),
        "from": _handoff_end(h, "from"),
        "to": _handoff_end(h, "to"),
        "after_step": h.get("after_step"),
        "volume_ul": h.get("volume_ul"),
        "particle_um": particle,
        "particle_source": psrc,
        "connector": (h.get("connector") or {}).get("status"),
        "note": next((_clip(x, 260) for x in _sentences(str(h.get("note") or ""))), None),
    }


def _status_index(status: dict) -> tuple[dict[str, str], list[dict]]:
    """chip id -> chain number, and the off-chip bench rows ("2b") to insert after a stage."""
    chip_no, bench = {}, []
    for row in status["chain"]:
        no = row.get("#", "").strip()
        ids = re.findall(r"`([^`]+)`", row.get("Chip", ""))
        if ids:
            chip_no[ids[0]] = no
        elif re.fullmatch(r"\d+[a-z]", no):
            bench.append({"after": re.match(r"\d+", no).group(0), "no": no, "row": row})
    return chip_no, bench


def _end_key(h: dict, side: str):
    e = h.get(side)
    return (e.get("module"), e.get("roadmap")) if isinstance(e, dict) else (e, None)


def _is_off_chip(m: dict) -> bool:
    if m.get("off_chip") is True or str(m.get("kind", "")).lower() in {"off_chip", "off-chip", "offchip", "bench", "kit"}:
        return True
    if str(m.get("status", "")).lower() in {"off_chip", "off-chip", "bench", "kit", "bought"}:
        return True
    build = m.get("build") if isinstance(m.get("build"), dict) else {}
    return not m.get("chip") and bool(OFF_CHIP_WORDS.search(str(build.get("method") or "")))


def build_catalog(root: Path = ROOT) -> dict:
    root = Path(root)
    pipe = _read_json(root, PIPELINE) or {}
    status = load_status(root)
    runs = (_read_json(root, "runs/index.json") or {}).get("runs") or []
    demo = load_demo(root)
    videos = {v["chip_id"]: v for v in (_read_json(root, VIDEO_MANIFEST) or {}).get("videos", [])
              if isinstance(v, dict) and v.get("chip_id")}
    chip_no, bench = _status_index(status)
    handoffs = pipe.get("handoffs") or []
    modules = [m for m in pipe.get("modules") or [] if isinstance(m, dict)]
    for m in modules:  # a chip given as a list: the first is the stage's chip
        if isinstance(m.get("chip"), list):
            m["chip"] = next((c for c in m["chip"] if isinstance(c, str)), None)
    specs = {m.get("module"): _read_json(root, m.get("chip")) for m in modules}

    handoffs = [h for h in handoffs if isinstance(h, dict)]

    def joins(key):
        return ([_handoff(h, specs.get(_end_key(h, "to")[0])) for h in handoffs if key in _end_key(h, "to")],
                [_handoff(h, specs.get(_end_key(h, "to")[0])) for h in handoffs if key in _end_key(h, "from")])

    stages: list[dict] = []
    seen_chips: set[str] = set()
    for m in modules:
        key = m.get("module")
        arrives, leaves = joins(key)
        stage = {"stage": key, "name": m.get("name"), "status": m.get("status"),
                 "status_note": m.get("status_note"), "owner": m.get("owner"),
                 "kind": "OFF-CHIP" if _is_off_chip(m) else ("CHIP" if m.get("chip") else "PLACEHOLDER"),
                 "arrives": arrives, "leaves": leaves}
        if m.get("chip"):
            seen_chips.add(m["chip"])
            spec = specs.get(key) or {}
            no = chip_no.get(spec.get("id") or Path(m["chip"]).stem)
            stage["module"] = chip_entry(root, m["chip"], m, no, status, runs, demo, videos)
            stages.append(stage)
            for b in (x for x in bench if no and x["after"] == no):
                stages.append({"stage": f"bench_{b['no']}", "name": _strip_md(b["row"].get("Stage")),
                               "status": _strip_md(b["row"].get("Status")), "kind": "OFF-CHIP",
                               "where": _strip_md(b["row"].get("Chip")),
                               "protocol": _strip_md(b["row"].get("Protocol")), "source": STATUS,
                               "arrives": [], "leaves": []})
        else:
            stage["note"] = _clip(m.get("note") or "", 300) or None
            stages.append(stage)
    for r in pipe.get("roadmap") or []:
        arrives, leaves = joins(r.get("id"))
        stage = {"stage": r.get("id"), "name": r.get("name"), "kind": "OFF-CHIP",
                 "status": "roadmap", "note": _clip(r.get("note") or "", 300) or None,
                 "arrives": arrives, "leaves": leaves}
        # A branch the one-chain format files under roadmap although it has a real chip
        # (Module 6 off Module 1): attach that chip and say where the branch leaves from.
        text = f"{r.get('name') or ''} {r.get('note') or ''}"
        if re.search(r"\bbranch", text, re.I):
            stage["status"] = "branch"
            stage["branch_from"] = next((h["from"]["stage"] for h in arrives if h["from"]["stage"]), None)
            chip_rel = r.get("chip") if isinstance(r.get("chip"), str) else next(
                (c for c in re.findall(r"chip/[\w.-]+\.json", text) if (root / c).is_file()), None)
            if chip_rel and chip_rel not in seen_chips:
                seen_chips.add(chip_rel)
                stage["kind"] = "CHIP"
                stage["module"] = chip_entry(root, chip_rel, {"name": r.get("name"), "protocol": r.get("protocol")},
                                             None, status, runs, demo, videos)
        stages.append(stage)
    for i, s in enumerate(stages, 1):
        s["order"] = i

    # Printed chips that the pipeline file does not (yet) list: shown after the chain.
    extra = []
    for p in PRINTED:
        if p["chip"] in seen_chips:
            continue
        entry = chip_entry(root, p["chip"], None, None, status, runs, demo, videos)
        extra.append({"stage": entry["chip_id"], "name": entry["chip_name"], "kind": "CHIP",
                      "status": "not in pipeline_mvp.json", "in_pipeline": False,
                      "arrives": [], "leaves": [], "module": entry})

    return {
        "generated_by": "tools/module_catalog.py",
        "pipeline": PIPELINE if pipe else None,
        "pipeline_id": pipe.get("id"),
        "variant": pipe.get("variant"),
        "inputs": {
            "pipeline": bool(pipe), "status": status["present"], "runs_index": bool(runs),
            "demo_plan": _rel_if(root, "tools/demo_plan.py"), "demo_md": _rel_if(root, "docs/demo.md"),
        },
        "printed_for_demo": [dict(p, chip_id=Path(p["chip"]).stem) for p in PRINTED],
        "stages": stages,
        "not_in_pipeline": extra,
    }


# ---------------------------------------------------------------- videos


def default_shot(mod: dict) -> str:
    feeds = []
    for st in mod["function"]:
        for p in st["feed"]:
            if p not in feeds:
                feeds.append(p)
    targets = []
    for st in mod["function"]:
        for t in st["target"]:
            if t not in targets:
                targets.append(t)
    f = ", ".join(feeds[:4]) or "its inlets"
    t = ", ".join(targets[:4]) or "its chambers"
    return (f"Top-down macro of {mod['chip_id']} on a backlight, ruler in shot: food dye pushed in through "
            f"{f}, filling {t} in protocol order, then flushed out.")


def _short_name(mod: dict | None, chip_id: str) -> str:
    return re.sub(r"^Module \d+:\s*", "", (mod or {}).get("chip_name") or chip_id)


def build_video_manifest(catalog: dict, old: dict | None) -> dict:
    old_by = {v.get("chip_id"): v for v in (old or {}).get("videos", []) if isinstance(v, dict)}
    mods = {s["module"]["chip"]: s["module"] for s in catalog["stages"] + catalog["not_in_pipeline"]
            if s.get("module")}
    videos = []
    for p in PRINTED:
        mod = mods.get(p["chip"])
        chip_id = Path(p["chip"]).stem
        prev = old_by.get(chip_id, {})
        shot = (mod or {}).get("demo", {}) and mod["demo"].get("shot")
        videos.append({
            "module": p["slot"],
            "chip_id": chip_id,
            "title": f"{p['slot']}: {_short_name(mod, chip_id)} (printed demo chip)",
            "shot": shot or (default_shot(mod) if mod and mod["function"] else
                             f"Top-down macro of {chip_id} on a backlight with a ruler in shot, dye through every inlet."),
            "shot_source": ((mod or {}).get("demo") or {}).get("source") if shot else "generated from the protocol",
            "url": prev.get("url"),
            "release_tag": prev.get("release_tag"),
            "recorded": bool(prev.get("recorded", False)),
            "duration_s": prev.get("duration_s"),
        })
    return {"about": "One video slot per printed demo chip. Videos live in GitHub Release assets "
                     "(tag demo-videos), never in git: see docs/videos/README.md. Generated by "
                     "tools/module_catalog.py; url, release_tag, recorded and duration_s are kept "
                     "across regenerations and filled by tools/publish_video.py.",
            "videos": videos}


# ---------------------------------------------------------------- markdown


def _cell(x) -> str:
    if x is None or x == [] or x == "":
        return "—"
    if isinstance(x, list):
        return ", ".join(str(i) for i in x)
    if isinstance(x, float):
        return f"{x:g}"
    return str(x).replace("|", "/")


def _cycle_key(s: dict) -> tuple:
    return (s["operation"], tuple(s["reagent"]), s["volume_ul"], s["flow_ul_min"], s["duration_s"],
            s["temp_c"], s["exchange_kind"])


def compress_steps(steps: list[dict]) -> list[dict]:
    """Rows for the markdown table: repeated runs and repeated cycles (one pass per lane or per
    sublibrary) are folded, their targets merged. Nothing is dropped: every step is in the JSON."""
    keys = [_cycle_key(s) for s in steps]
    rows, i, n = [], 0, len(steps)
    while i < n:
        best_p, best_r = 1, 1
        for p in range(1, (n - i) // 2 + 1):
            r = 1
            while i + (r + 1) * p <= n and keys[i + r * p:i + (r + 1) * p] == keys[i:i + p]:
                r += 1
            if r >= 2 and p * r > best_p * best_r:
                best_p, best_r = p, r
        end = i + best_p * best_r
        if best_r > 1 and best_p > 1:
            rows.append({"header": f"steps {steps[i]['step']}-{steps[end - 1]['step']}: "
                                   f"the next {best_p} steps, {best_r} times"})
        for k in range(best_p):
            group = [steps[i + k + j * best_p] for j in range(best_r)]
            targets = []
            for g in group:
                targets += [t for t in g["target"] if t not in targets]
            label = str(group[0]["step"])
            if best_r > 1 and best_p == 1:
                label = f"{group[0]['step']}-{group[-1]['step']} (×{best_r})"
            rows.append(dict(group[0], target=targets, label=label))
        i = end
    return rows


def _steps_md(steps: list[dict]) -> list[str]:
    if not steps:
        return ["_No protocol found for this chip._", ""]
    rows = compress_steps(steps)
    lines = []
    if len(rows) <= 40:
        lines += ["| Step | Operation | Target | Reagent | µL | µL/min | Time | °C | Exchange |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for s in rows:
            if "header" in s:
                lines.append(f"| **{s['header']}** | | | | | | | | |")
                continue
            lines.append(f"| {s['label']} | {_cell(s['operation'])} | {_cell(s['target'])} | {_cell(s['reagent'])} | "
                         f"{_cell(s['volume_ul'])} | {_cell(s['flow_ul_min'])} | {_fmt_dur(s['duration_s'])} | "
                         f"{_cell(s['temp_c'])} | {_cell(s['exchange_kind'])} |")
    else:
        # Long protocols: one row per reagent, in order of first use. Every step is in modules.json.
        lines.append(f"{len(steps)} steps, summarised per reagent in order of first use "
                     f"(every step: `docs/modules.json`).")
        lines += ["", "| Steps | Reagent | Operations | Targets | Count | Total time | °C |",
                  "|---|---|---|---|---|---|---|"]
        order: dict[str, list[dict]] = {}
        for s in steps:
            order.setdefault(", ".join(s["reagent"]) or f"({s['operation']})", []).append(s)
        for reagent, ss in order.items():
            ops = sorted({str(s["operation"]) for s in ss})
            tg = sorted({t for s in ss for t in s["target"]})
            temps = sorted({s["temp_c"] for s in ss if s["temp_c"] is not None}, key=_sort_key)
            total = sum(s["duration_s"] for s in ss if isinstance(s["duration_s"], (int, float)))
            lines.append(f"| {ss[0]['step']}-{ss[-1]['step']} | {reagent} | {', '.join(ops)} | {', '.join(tg)} | "
                         f"{len(ss)} | {_fmt_dur(total)} | {_cell(temps)} |")
    return lines + [""]


def _temps(z: dict) -> str:
    if z.get("temps_c"):
        return ", ".join(_cell(t) for t in z["temps_c"])
    return _cell(z["min_c"]) if z["min_c"] == z["max_c"] else f"{_cell(z['min_c'])} to {_cell(z['max_c'])}"


def _links(paths: list[str], n: int = 6) -> str:
    if not paths:
        return "—"
    shown = [f"[{Path(p).name}](../{p})" for p in paths[:n]]
    return ", ".join(shown) + (f" (+{len(paths) - n} more)" if len(paths) > n else "")


def _joins_md(title: str, hs: list[dict]) -> list[str]:
    out = []
    for h in hs:
        frm = f"{h['from']['stage']}:{'/'.join(h['from']['ports']) or '—'}"
        to = f"{h['to']['stage']}:{'/'.join(h['to']['ports']) or '—'}"
        vol = f"{_cell(h['volume_ul'])} µL" if h["volume_ul"] is not None else "volume not stated"
        size = ("size not stated" if h["particle_um"] is None else f"{_cell(h['particle_um'])} µm"
                if h["particle_source"] == "handoff" else f"receiver designed for {_cell(h['particle_um'])} µm")
        out.append(f"- **{title}** {h['carries']} {frm} → {to}; {vol}; {size}. {h['note'] or ''}".rstrip())
    return out


def module_md(stage: dict) -> list[str]:
    m = stage["module"]
    printed = "**YES**" if m["printed_for_demo"] else "no"
    title = re.sub(rf"^{re.escape(m['slot'])}:\s*", "", stage["name"] or m["chip_name"] or m["chip_id"])
    lines = [f"## {stage.get('order', '+')}. {m['slot']}: {title}", "",
             "| | |", "|---|---|",
             f"| Chip | `{m['chip'] or 'missing'}` ({m['chip_id']}) |",
             f"| Scale | {m['scale'] or '—'}, {_cell(m['fluidic_depth_mm'])} mm deep |",
             f"| Printed for the demo | {printed} |",
             f"| Protocol | `{m['protocol'] or 'none'}` ({len(m['function'])} steps) |",
             f"| Status | {_cell(stage.get('status'))} |"]
    if m["full_spec"]:
        fs = m["full_spec"]
        lines.append(f"| FULL SPEC design | `{fs['chip']}` ({fs['scale'] or '—'}) |")
    v = m["video"]
    if m["printed_for_demo"]:
        lines.append(f"| Video | {('[watch](' + v['url'] + ')') if v and v.get('url') else 'slot open, not recorded'} |")
    lines += ["", "**Exact function**", ""] + _steps_md(m["function"])
    counted = any(p["fed_in_steps"] or p["vented_in_steps"] for p in m["ports"])
    lines += ["**Ports**: " + ("; ".join(
        f"`{p['id']}` {p['type'] or ''}{('/' + p['role']) if p['role'] else ''}"
        + (f" (feed {p['fed_in_steps']}, vent {p['vented_in_steps']})" if counted else "")
        for p in m["ports"]) or "—") + ("" if counted or not m["ports"] else " (the protocol names no ports)"), "",
        "**Chambers**: " + ("; ".join(
            f"`{c['id']}` {c['block'] or c['type'] or ''} {_cell(c['volume_ul'])} µL" for c in m["chambers"]) or "—"), "",
        "**Thermal**: " + ("; ".join(
            f"{_cell(z['chambers'])}: {_temps(z)} °C" + (f" ({z['purpose']})" if z['purpose'] else "")
            for z in m["thermal_zones"]) or "room temperature only"), ""]
    joins = _joins_md("In:", stage.get("arrives", [])) + _joins_md("Out:", stage.get("leaves", []))
    if joins:
        lines += joins + [""]
    if m["off_chip"]:
        lines += ["**Off-chip**: " + " · ".join(m["off_chip"][:4]), ""]
    if m["known_limits"]:
        lines += ["**Known limits**", ""] + [f"- {x['text']}" for x in m["known_limits"][:6]] + [""]
    a = m["artifacts"]
    lines += [f"**Files**: plan {_links(a['plan_svgs'])} · renders {_links(a['renders_3d'])} · "
              f"CFD steps {_cell(a['cfd']['steps_simulated'])} ({len(a['cfd']['figures'])} maps) · "
              f"flow runs {_links(a['flow_runs'], 3)}", ""]
    if m["demo"]:
        lines += [f"**Demo** ({m['demo']['source']}): " + (" → ".join(m["demo"]["procedure"][:8]) or "—"), ""]
    else:
        lines += ["**Demo**: no demo procedure yet (tools/demo_plan.py / docs/demo.md absent).", ""]
    return lines


def render_md(cat: dict) -> str:
    out = ["# Modules", "",
           f"Generated by `tools/module_catalog.py` from `{cat['pipeline'] or 'no pipeline file'}`; do not edit. "
           "Same data for the twin and web GUI: `docs/modules.json`. Videos: `docs/videos/manifest.json`.", "",
           "## Chain", "", "| # | Stage | Where | Printed | Status |", "|---|---|---|---|---|"]
    for s in cat["stages"] + cat["not_in_pipeline"]:
        m = s.get("module")
        where = f"`{m['chip_id']}` ({m['scale']})" if m else s["kind"]
        printed = ("**YES**" if m["printed_for_demo"] else "no") if m else "—"
        no = s.get("order", "+")
        if s.get("branch_from"):
            no = f"{no} (branch off {s['branch_from']})"
        out.append(f"| {no} | {_cell(s['name'])} | {where} | {printed} | {_cell(s.get('status'))} |")
    out.append("")
    if cat["not_in_pipeline"]:
        out += ["`+` printed but not listed in the pipeline file yet.", ""]
    for s in cat["stages"] + cat["not_in_pipeline"]:
        if s.get("module"):
            out += module_md(s)
        else:
            label = "OFF-CHIP" if s["kind"] == "OFF-CHIP" else "PLACEHOLDER (no chip)"
            out += [f"## {s.get('order', '+')}. {label}: {s['name']}", ""]
            bits = [s.get("note"), (f"Where: {s['where']}." if s.get("where") else None),
                    (f"Protocol: {s['protocol']}." if s.get("protocol") else None)]
            out += [" ".join(b for b in bits if b) or "—", ""]
            joins = _joins_md("In:", s["arrives"]) + _joins_md("Out:", s["leaves"])
            out += (joins + [""]) if joins else []
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------- main


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def generate(root: Path = ROOT) -> dict[str, str]:
    """The three outputs as {repo-relative path: text}. Pure: writes nothing."""
    root = Path(root)
    old = _read_json(root, VIDEO_MANIFEST)
    cat = build_catalog(root)
    manifest = build_video_manifest(cat, old)
    # The catalogue shows the slots as they will be written.
    by_chip = {v["chip_id"]: v for v in manifest["videos"]}
    for s in cat["stages"] + cat["not_in_pipeline"]:
        if s.get("module") and s["module"]["printed_for_demo"]:
            s["module"]["video"] = by_chip.get(s["module"]["chip_id"])
    return {OUT_JSON: _dump(cat), OUT_MD: render_md(cat), VIDEO_MANIFEST: _dump(manifest)}


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if an output is out of date")
    args = ap.parse_args(argv)
    outputs = generate(root)
    stale = []
    for rel, text in outputs.items():
        p = Path(root) / rel
        current = p.read_text() if p.is_file() else None
        if current == text:
            continue
        stale.append(rel)
        if not args.check:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
    if args.check:
        for rel in stale:
            print(f"out of date: {rel} (run python tools/module_catalog.py)")
        return 1 if stale else 0
    for rel in outputs:
        print(f"{'wrote' if rel in stale else 'unchanged'} {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

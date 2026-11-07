"""Validate every contract file and check that IDs line up across them.

    python tools/validate.py

Checks
  - protocols/*.json, chip/*.json, cad/*_spec.json, runs/*.json against their schemas
  - every protocol target exists as a chamber or port in the chip spec it's run against
    (the chips named by its runs; a protocol with no run yet is checked against the chip
    spec that fits it best)
  - chip specs: unique ids, channels connect real chambers/ports, parts inside the footprint,
    valves sit on real channels with a control port, module chips use their prefix on every id
  - runs: protocol, chip and every chamber id exist; events in time order; index.json up to date
  - cad/exports/*.stl whose name isn't a chamber, port, channel or chip id (warning)
  - pipelines (protocols/*.json with "kind": "pipeline"): files exist, no superseded chip,
    hand-off ports exist and go from an outlet to an inlet (TBC ports are listed as
    warnings), after_step exists, each hand-off's connector block agrees with the two
    modules' build.connector (mismatches are listed as warnings: the demo needs adapters)

Exit code 0 when there are no errors (warnings are fine), 1 otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))

import commands  # noqa: E402
import planner  # noqa: E402
import schema_io  # noqa: E402


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def rel(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def locate(kind: str, data: dict, error) -> str:
    """Human location for a schema error: 'step 4', "chamber 'split_2'", 'event 3'."""
    path = list(error.absolute_path)
    if len(path) >= 2 and isinstance(path[1], int):
        container, idx = path[0], path[1]
        try:
            item = data[container][idx]
        except (KeyError, IndexError, TypeError):
            item = {}
        if kind == "protocol" and container == "steps":
            return f"step {item.get('step', idx + 1) if isinstance(item, dict) else idx + 1}"
        if kind == "run" and container == "events":
            return f"event {idx}"
        if kind == "chip_spec" and isinstance(item, dict) and item.get("id"):
            return f"{container[:-1]} '{item['id']}'"
        return f"{container}[{idx}]"
    return ""


def load_folder(pattern: str, kind: str, report: Report,
                skip: tuple[str, ...] = ()) -> tuple[dict[Path, dict], set[Path]]:
    """Load and schema-check every file matching pattern.

    Returns (files that parsed, files that failed their schema).
    """
    loaded: dict[Path, dict] = {}
    failed: set[Path] = set()
    for path in sorted(ROOT.glob(pattern)):
        if path.name in skip:
            continue
        try:
            data = schema_io.load_json(path)
        except ValueError as e:
            report.error(f"{rel(path)}: {str(e).split(': ', 1)[-1]}")
            continue
        if not isinstance(data, dict):
            report.error(f"{rel(path)}: expected a JSON object at the top level")
            continue
        if kind == "protocol" and schema_io.is_pipeline(data):
            continue  # checked by check_pipelines
        for err in schema_io.schema_errors(kind, data):
            where = locate(kind, data, err)
            report.error(f"{rel(path)}{' ' + where if where else ''}: {schema_io.describe(err)}")
            failed.add(path)
        loaded[path] = data
    return loaded, failed


def ids_unique(files: dict[Path, dict], label: str, report: Report) -> dict[str, Path]:
    by_id: dict[str, Path] = {}
    for path, data in files.items():
        pid = data.get("id")
        if not isinstance(pid, str):
            continue
        if pid in by_id:
            report.error(f"{rel(path)}: {label} id '{pid}' is also used by {rel(by_id[pid])}")
        else:
            by_id[pid] = path
    return by_id


def check_protocol(path: Path, protocol: dict, report: Report) -> None:
    steps = protocol.get("steps") or []
    for i, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        if step.get("step") != i:
            report.warn(f"{rel(path)}: steps should be numbered 1, 2, 3... in order; "
                        f"position {i} is numbered {step.get('step')}")
        if step.get("operation") == "exchange":
            try:
                needed = step["washes"] * step["volume_ul"] / step["flow_ul_min"] * 60 * max(1, len(step.get("targets") or []))
                too_short = step["duration_s"] + 1e-9 < needed
            except (TypeError, KeyError, ZeroDivisionError):
                continue
            if too_short:
                report.warn(f"{rel(path)} step {step.get('step')}: {step['washes']} wash(es) x "
                            f"{step['volume_ul']} uL at {step['flow_ul_min']} uL/min needs at least "
                            f"{needed:.0f} s, but duration_s is {step.get('duration_s')}")


def check_chip(path: Path, chip: dict, report: Report) -> None:
    where = rel(path)
    seen: dict[str, str] = {}
    for group in ("chambers", "ports", "channels", "valves", "features", "stages"):
        for item in chip.get(group, []):
            iid = item.get("id")
            if iid in seen:
                report.error(f"{where}: id '{iid}' is used by both a {seen[iid]} and a {group[:-1]}; ids must be unique")
            seen[iid] = group[:-1]
    module = chip.get("module")
    if module:
        prefix = module["id"] + "_"
        named = [(g, i.get("id")) for g in ("chambers", "ports", "channels", "valves", "features", "stages", "layers", "thermal_zones")
                 for i in chip.get(g, [])]
        bad = [iid for _, iid in named + [("chip", chip["id"])] if not str(iid).startswith(prefix)]
        if bad:
            report.error(f"{where}: module {module['id']} chip, but these ids don't start with '{prefix}': {', '.join(bad)}")
        if module.get("superseded_by") and not (ROOT / module["superseded_by"]).is_file():
            report.error(f"{where}: superseded_by '{module['superseded_by']}' does not exist")
    if chip.get("translated_from") and not (ROOT / chip["translated_from"]).is_file():
        report.error(f"{where}: translated_from '{chip['translated_from']}' does not exist")
    ports_by_id = {pt["id"]: pt for pt in chip.get("ports", [])}
    channel_ids = {ch.get("id") for ch in chip.get("channels", [])}
    for v in chip.get("valves", []):
        if v.get("channel") not in channel_ids:
            report.error(f"{where}: valve '{v.get('id')}' sits on '{v.get('channel')}', which is not a channel")
        cp = ports_by_id.get(v.get("control_port"))
        if cp is None or cp.get("type") != "control":
            report.error(f"{where}: valve '{v.get('id')}' control_port '{v.get('control_port')}' is not a port of type control")
        for cid in v.get("connects", []):
            if cid not in commands.chamber_ids(chip):
                report.error(f"{where}: valve '{v.get('id')}' connects '{cid}', which is not a chamber")
    for st in chip.get("stages", []):
        for end in ("from_port", "to_port"):
            if st.get(end) not in ports_by_id:
                report.error(f"{where}: stage '{st.get('id')}' {end} '{st.get(end)}' is not a port")
    for f in chip.get("features", []):
        for cid in f.get("covers", []):
            if cid not in commands.chamber_ids(chip):
                report.error(f"{where}: feature '{f.get('id')}' covers '{cid}', which is not a chamber")
    for name, rp in (chip.get("run_parameters") or {}).items():
        if rp["default"] not in rp["values"]:
            report.error(f"{where}: run parameter '{name}' default '{rp['default']}' is not one of its values")
    places = commands.chamber_ids(chip) | commands.port_ids(chip)
    for ch in chip.get("channels", []):
        for end in ("from", "to"):
            if ch.get(end) not in places:
                report.error(f"{where}: channel '{ch.get('id')}' {end} '{ch.get(end)}' is not a chamber or port")

    fx, fy = chip["footprint"]["x"], chip["footprint"]["y"]
    height = sum(layer["thickness_mm"] for layer in chip["layers"])
    for c in chip["chambers"]:
        (cx, cy, cz), (sx, sy, sz) = c["center"], c["size"]
        if cx - sx / 2 < 0 or cx + sx / 2 > fx or cy - sy / 2 < 0 or cy + sy / 2 > fy:
            report.warn(f"{where}: chamber '{c['id']}' sticks out of the {fx} x {fy} mm footprint")
        if cz - sz / 2 < 0 or cz + sz / 2 > height:
            report.warn(f"{where}: chamber '{c['id']}' sticks out of the {height} mm layer stack")
        box = sx * sy * sz
        if c["volume_ul"] > box * 1.01:
            report.warn(f"{where}: chamber '{c['id']}' volume_ul {c['volume_ul']} is more than its "
                        f"box can hold ({box:.0f} uL; 1 mm^3 = 1 uL)")
    for p in chip["ports"]:
        px, py, _ = p["position"]
        if not (0 <= px <= fx and 0 <= py <= fy):
            report.warn(f"{where}: port '{p['id']}' is outside the footprint")
    chamber_set = commands.chamber_ids(chip)
    for zone in chip.get("thermal_zones", []):
        for cid in zone.get("chambers", []):
            if cid not in chamber_set:
                report.error(f"{where}: thermal zone '{zone.get('id')}' lists '{cid}', which is not a chamber")


def check_protocol_on_chip(ppath: Path, protocol: dict, chip: dict, report: Report,
                           schema_ok: bool) -> None:
    """Targets must exist in the chip. Runs even on files with schema errors, so people
    see every problem in one go; the split/pool wiring check needs a valid file."""
    places = commands.place_ids(chip)
    variants = ((chip.get("run_parameters") or {}).get("variant") or {}).get("values", [])
    steps = protocol.get("steps") if isinstance(protocol.get("steps"), list) else []
    for i, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        targets = [t for t in commands.step_targets(step) if isinstance(t, str)]
        missing = [t for t in targets if t not in places]
        for target in missing:
            report.error(f"{rel(ppath)} step {step.get('step', i)}: target '{target}' not in chip {chip['id']}")
        if "variant" in step and step["variant"] not in variants:
            report.error(f"{rel(ppath)} step {step.get('step', i)}: variant '{step['variant']}' is not a variant of chip "
                         f"{chip['id']} ({', '.join(variants) or 'it has none'})")
        if schema_ok and targets and not missing:
            try:
                planner.step_to_commands(step, chip)
            except planner.PlanError as e:
                report.error(f"{rel(ppath)} {e} (chip {chip['id']})")


def check_run(path: Path, run: dict, protocols: dict[str, tuple[Path, dict]],
              chips: dict[str, tuple[Path, dict]], report: Report) -> None:
    where = rel(path)
    proto = protocols.get(run.get("protocol_id"))
    chip_entry = chips.get(run.get("chip_id"))
    if proto is None:
        report.error(f"{where}: protocol_id '{run.get('protocol_id')}' has no file in protocols/")
    if chip_entry is None:
        report.error(f"{where}: chip_id '{run.get('chip_id')}' has no chip spec in chip/ or cad/")

    spec_path = run.get("chip_spec")
    if spec_path:
        target = ROOT / spec_path
        if not target.is_file():
            report.error(f"{where}: chip_spec '{spec_path}' does not exist (the twin loads the chip from it)")
        elif chip_entry is not None and target.resolve() != chip_entry[0].resolve():
            report.error(f"{where}: chip_spec '{spec_path}' does not hold chip '{run.get('chip_id')}'")
    else:
        report.warn(f"{where}: no chip_spec path; the twin will not know which chip to draw")

    if proto and chip_entry:
        if (proto[1].get("placeholder") or chip_entry[1].get("placeholder")) and not run.get("placeholder"):
            report.error(f"{where}: made from placeholder files, so it must say \"placeholder\": true")

    if run.get("simulated_hardware") and not run.get("scenario"):
        report.error(f"{where}: simulated run without its scenario")
    if run.get("report") and not (ROOT / run["report"]).is_file():
        report.error(f"{where}: report '{run['report']}' does not exist (regenerate with operator/run.py)")
    if run.get("transcript") and not (ROOT / run["transcript"]).is_file():
        report.error(f"{where}: transcript '{run['transcript']}' does not exist")

    step_numbers = {s.get("step") for s in proto[1]["steps"]} if proto else set()
    chambers = (commands.chamber_ids(chip_entry[1]) | commands.stage_ids(chip_entry[1])) if chip_entry else None
    last_t = -1.0
    for i, ev in enumerate(run.get("events", [])):
        t = ev.get("t_s", 0)
        if isinstance(t, (int, float)):
            if t < last_t:
                report.error(f"{where} event {i}: t_s {t} is earlier than the event before it")
            last_t = t
        if chambers is not None and isinstance(ev.get("args"), dict):
            for cid in commands.ids_in(ev.get("command"), ev["args"]):
                if cid not in chambers:
                    report.error(f"{where} event {i} ({ev.get('command')}): '{cid}' not in chip {run.get('chip_id')}")
            for vid in commands.ids_in(ev.get("command"), ev["args"], commands.VALVE_ARGS):
                if vid not in commands.valve_ids(chip_entry[1]):
                    report.error(f"{where} event {i} ({ev.get('command')}): valve '{vid}' not in chip {run.get('chip_id')}")
        step = ev.get("protocol_step")
        if proto and step is not None and step not in step_numbers:
            report.error(f"{where} event {i}: protocol_step {step} is not a step in {proto[1]['id']}")


def check_index(run_files: dict[Path, dict], report: Report) -> None:
    index_path = ROOT / "runs" / "index.json"
    fix = "run: python operator/run.py --reindex"
    if not index_path.exists():
        if run_files:
            report.error(f"runs/index.json is missing, so the twin can't list runs ({fix})")
        return
    try:
        index = schema_io.load_json(index_path)
    except ValueError as e:
        report.error(f"runs/index.json: {e} ({fix})")
        return
    listed = {e.get("file") for e in index.get("runs", []) if isinstance(e, dict)}
    for name in sorted(listed):
        if not (ROOT / "runs" / str(name)).is_file():
            report.error(f"runs/index.json lists '{name}', which does not exist ({fix})")
    for path in run_files:
        if path.name not in listed:
            report.error(f"runs/index.json does not list {path.name}, so it won't appear in the twin ({fix})")


def check_contract_data(report: Report) -> None:
    """contracts/parameters.json and contracts/materials.json."""
    path = ROOT / "contracts" / "parameters.json"
    if path.exists():
        data = schema_io.load_json(path)
        for err in schema_io.schema_errors("parameters", data):
            report.error(f"contracts/parameters.json {schema_io.path_str(err)}: {schema_io.describe(err)}")
        sources = set(data.get("sources", {}))
        for key, prm in data.get("parameters", {}).items():
            for src in prm.get("source", []) if isinstance(prm, dict) else []:
                if src not in sources:
                    report.error(f"contracts/parameters.json {key}: source '{src}' is not in the sources table")
    path = ROOT / "contracts" / "materials.json"
    if path.exists():
        mats = schema_io.load_json(path).get("materials", {})
        allowed = {"compatible", "limited", "incompatible", "TODO"}
        for mid, m in mats.items():
            if not isinstance(m.get("max_working_temp_c"), (int, float)):
                report.error(f"contracts/materials.json {mid}: max_working_temp_c must be a number")
            for reagent, verdict in (m.get("chemical_compatibility") or {}).items():
                if verdict not in allowed:
                    report.error(f"contracts/materials.json {mid}.{reagent}: '{verdict}' must be one of {', '.join(sorted(allowed))}")


def check_scenarios(report: Report) -> int:
    """scenarios/*.json: referenced files exist, sensor and fault names are known."""
    known_sensors = {"pump_volume_ul", "pressure_kpa", "outlet_absorbance", "temperature_c", "capsules_counted"}
    known_faults = {"clog", "reagent_low", "capsule_loss", "sensor_fault"}
    paths = sorted((ROOT / "scenarios").glob("*.json"))
    for path in paths:
        try:
            scn = schema_io.load_json(path)
        except ValueError as e:
            report.error(f"{rel(path)}: {e}")
            continue
        for key in ("protocol", "chip", "module2_chip"):
            if key in scn and not (ROOT / scn[key]).is_file():
                report.error(f"{rel(path)}: {key} '{scn[key]}' does not exist")
        for name in scn.get("sensors", {}):
            if name not in known_sensors:
                report.error(f"{rel(path)}: unknown sensor '{name}' (known: {', '.join(sorted(known_sensors))})")
        for f in scn.get("faults", []):
            if f.get("type") not in known_faults:
                report.error(f"{rel(path)}: unknown fault type '{f.get('type')}' (known: {', '.join(sorted(known_faults))})")
            if f.get("type") == "sensor_fault" and f.get("sensor") not in known_sensors:
                report.error(f"{rel(path)}: sensor_fault on unknown sensor '{f.get('sensor')}'")
    return len(paths)


def connector_status(a: str | None, b: str | None) -> str:
    """match / mismatch / unknown (either side not decided) for two build.connector values."""
    if a in (None, "TBC") or b in (None, "TBC"):
        return "unknown"
    return "match" if a == b else "mismatch"


def check_pipelines(report: Report) -> int:
    found = schema_io.pipelines()
    for path, pipe in found:
        check_pipeline(rel(path), pipe, report)
    return len(found)


def check_pipeline(where: str, pipe: dict, report: Report, root: Path = ROOT) -> None:
    errs = schema_io.schema_errors("pipeline", pipe)
    for err in errs:
        report.error(f"{where} {schema_io.path_str(err)}: {schema_io.describe(err)}")
    if errs:
        return
    # module id -> (entry, chip or None, protocol or None, switched automatically?)
    mods: dict[str, tuple[dict, dict | None, dict | None, bool]] = {}
    for m in pipe["modules"]:
        name = m["module"]
        chip_path, status, _ = schema_io.pipeline_chip(m, root)
        switched = chip_path != m.get("chip")
        if switched:
            report.warn(f"{where}: module {name} now uses {chip_path} (found by {m['prefer']['glob']}) instead of "
                        f"{m.get('chip')}; check its hand-off ports and build block")
        for key in ("protocol", "scenario", "source"):
            if m.get(key) and not (root / m[key]).is_file():
                report.error(f"{where}: module {name}: {key} '{m[key]}' does not exist")
        chip = proto = None
        if status == "missing":
            if chip_path:
                report.error(f"{where}: module {name} is missing but names a chip ({chip_path})")
        elif not (root / chip_path).is_file():
            report.error(f"{where}: module {name}: chip '{chip_path}' does not exist")
            continue
        else:
            chip = schema_io.load_json(root / chip_path)
            block = chip.get("module") or {}
            if block.get("status") == "superseded":
                report.error(f"{where}: module {name} uses {chip_path}, which is superseded by "
                             f"{block.get('superseded_by')}; superseded designs stay out of the pipeline")
            if status == "translated" and not chip.get("translated_from"):
                report.error(f"{where}: module {name} is 'translated' but {chip_path} has no translated_from")
            for inst in m.get("instances", []):
                try:
                    planner.run_params_for(chip, inst["run_parameters"])
                except planner.PlanError as e:
                    report.error(f"{where}: module {name} instance '{inst['name']}': {e}")
        if m.get("protocol") and (root / m["protocol"]).is_file():
            proto = schema_io.load_json(root / m["protocol"])
        mods[name] = (m, chip, proto, switched)
    roadmap = {r["id"] for r in pipe.get("roadmap", [])}
    tbc: list[str] = []
    for h in pipe["handoffs"]:
        for side, want in (("from", "outlet"), ("to", "inlet")):
            end = h[side]
            if "roadmap" in end:
                if end["roadmap"] not in roadmap:
                    report.error(f"{where}: hand-off '{h['id']}' {side} roadmap '{end['roadmap']}' is not in roadmap")
                continue
            if end["module"] not in mods:
                report.error(f"{where}: hand-off '{h['id']}' {side} module '{end['module']}' is not in modules")
                continue
            _, chip, _, switched = mods[end["module"]]
            known = {p["id"]: p for p in (chip or {}).get("ports", [])}
            for pid in ([end["port"]] if "port" in end else end["ports"]):
                if pid == "TBC":
                    tbc.append(f"{h['id']} ({side} {end['module']})")
                elif chip is None:
                    report.error(f"{where}: hand-off '{h['id']}' {side} port '{pid}': module {end['module']} has "
                                 f"no chip yet, so its ports must be TBC")
                elif pid not in known:
                    (report.warn if switched else report.error)(
                        f"{where}: hand-off '{h['id']}' {side} port '{pid}' is not in {end['module']}'s chip")
                elif known[pid]["type"] != want:
                    report.error(f"{where}: hand-off '{h['id']}' {side} '{pid}' is an {known[pid]['type']}, not an {want}")
        src = h["from"].get("module")
        if "after_step" in h and src in mods and mods[src][2] is not None:
            steps = {s.get("step") for s in mods[src][2].get("steps", [])}
            if h["after_step"] not in steps:
                report.error(f"{where}: hand-off '{h['id']}' after_step {h['after_step']} is not a step of {src}'s protocol")
        dst = h["to"].get("module")
        if src in mods and dst in mods:
            a = (mods[src][0].get("build") or {}).get("connector", "TBC")
            b = (mods[dst][0].get("build") or {}).get("connector", "TBC")
            want_status, c = connector_status(a, b), h.get("connector")
            if c is None:
                report.error(f"{where}: hand-off '{h['id']}' has no connector block (expected {want_status}: {a} -> {b})")
            elif (c["from"], c["to"], c["status"]) != (a, b, want_status):
                report.error(f"{where}: hand-off '{h['id']}' connector says {c['from']} -> {c['to']} {c['status']}, "
                             f"but the modules' build blocks give {a} -> {b} {want_status}")
            elif want_status != "match":
                report.warn(f"{where}: hand-off '{h['id']}' connector {want_status} ({a} -> {b}): {c.get('warning', '')}")
    if tbc:
        report.warn(f"{where}: ports to be confirmed (TBC): {'; '.join(tbc)}")


def check_cad(chips: dict[str, tuple[Path, dict]], report: Report) -> int:
    # An STL may be one part (named by chamber, port or channel id) or a whole
    # one-piece print (named by the chip spec id, e.g. hello_world_plate.stl).
    known = set(chips)
    for _, chip in chips.values():
        known |= commands.chamber_ids(chip) | commands.port_ids(chip) | commands.valve_ids(chip)
        known |= {x["id"] for g in ("channels", "features") for x in chip.get(g, []) if isinstance(x, dict) and "id" in x}
    stls = [p for p in sorted((ROOT / "cad" / "exports").glob("*")) if p.suffix.lower() == ".stl"]
    for p in stls:
        if p.stem not in known:
            report.warn(f"{rel(p)}: '{p.stem}' is not a chamber, port, channel or chip id in any chip spec "
                        f"(name STLs exactly by id, e.g. exchange_1.stl, or by chip id for a one-piece print)")
    return len(stls)


def best_chip(protocol: dict, candidates: list[tuple[Path, dict]]) -> tuple[Path, dict]:
    """The chip spec containing the most of this protocol's targets."""
    steps = protocol.get("steps") if isinstance(protocol.get("steps"), list) else []
    wanted = {t for s in steps if isinstance(s, dict) for t in commands.step_targets(s)}
    return min(candidates, key=lambda pc: len(wanted - commands.place_ids(pc[1])))


def main() -> int:
    report = Report()
    protocol_files, bad_protocols = load_folder("protocols/*.json", "protocol", report)
    chip_files, bad_chips = load_folder("chip/*.json", "chip_spec", report)
    cad_specs, bad_cad_specs = load_folder("cad/*_spec.json", "chip_spec", report)  # e.g. the hello world plate
    chip_files.update(cad_specs)
    bad_chips |= bad_cad_specs
    run_files, bad_runs = load_folder("runs/*.json", "run", report, skip=("index.json",))

    # Only files that passed their schema take part in cross-file checks.
    ok_protocols = {p: d for p, d in protocol_files.items() if p not in bad_protocols}
    ok_chips = {p: d for p, d in chip_files.items() if p not in bad_chips}
    ok_runs = {p: d for p, d in run_files.items() if p not in bad_runs}

    protocols = {pid: (p, protocol_files[p]) for pid, p in ids_unique(protocol_files, "protocol", report).items()}
    chips = {cid: (p, chip_files[p]) for cid, p in ids_unique(chip_files, "chip", report).items()}

    for path, protocol in protocol_files.items():
        check_protocol(path, protocol, report)
    for path, chip in ok_chips.items():
        check_chip(path, chip, report)

    # Which chips is each protocol run against? From its runs; with no run yet, the chip
    # spec that fits it best (so a protocol isn't flagged for every other chip in the repo).
    pairs: dict[str, set[str]] = {}
    for run in ok_runs.values():
        pairs.setdefault(run["protocol_id"], set()).add(run["chip_id"])
    usable = [chips[c] for c in sorted(chips) if chips[c][0] in ok_chips]
    for path, protocol in protocol_files.items():
        paired = pairs.get(protocol.get("id"))
        if paired:
            against = [chips[c] for c in sorted(paired) if c in chips and chips[c][0] in ok_chips]
        else:
            against = [best_chip(protocol, usable)] if usable else []
        for _, chip in against:
            check_protocol_on_chip(path, protocol, chip, report, path in ok_protocols)
        if path in ok_protocols and protocol["id"] not in pairs:
            report.warn(f"{rel(path)}: no run yet (python operator/run.py --protocol {rel(path)} --chip <chip spec>)")

    for path, run in ok_runs.items():
        check_run(path, run, protocols, chips, report)
    check_index(run_files, report)
    check_contract_data(report)
    n_scn = check_scenarios(report)
    n_pipe = check_pipelines(report)
    n_stl = check_cad(chips, report)

    print("capsule-processor contract check")
    print(f"  protocols/    {len(protocol_files)} file(s)")
    print(f"  chip specs    {len(chip_files)} file(s) (chip/ and cad/*_spec.json)")
    print(f"  runs/         {len(run_files)} file(s)")
    print(f"  cad/exports/  {n_stl} STL file(s)")
    print(f"  scenarios/    {n_scn} file(s)")
    print(f"  pipelines     {n_pipe} file(s) (protocols/ with kind: pipeline)")
    print()
    for line in report.errors:
        print(f"ERROR  {line}")
    for line in report.warnings:
        print(f"WARN   {line}")
    if report.errors or report.warnings:
        print()
    status = "FAILED" if report.errors else "OK"
    print(f"{status}: {len(report.errors)} error(s), {len(report.warnings)} warning(s)")
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

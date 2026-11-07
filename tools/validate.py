"""Validate every contract file and check that IDs line up across them.

    python tools/validate.py

Checks
  - protocols/*.json, chip/*.json, cad/*_spec.json, runs/*.json against their schemas
  - every protocol target exists as a chamber or port in the chip spec it's run against
    (the chips named by its runs; a protocol with no run yet is checked against the chip
    spec that fits it best)
  - chip specs: unique ids, channels connect real chambers/ports, parts inside the footprint
  - runs: protocol, chip and every chamber id exist; events in time order; index.json up to date
  - cad/exports/*.stl whose name isn't a chamber, port, channel or chip id (warning)

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
                needed = step["washes"] * step["volume_ul"] / step["flow_ul_min"] * 60
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
    for group in ("chambers", "ports", "channels"):
        for item in chip.get(group, []):
            iid = item.get("id")
            if iid in seen:
                report.error(f"{where}: id '{iid}' is used by both a {seen[iid]} and a {group[:-1]}; ids must be unique")
            seen[iid] = group[:-1]
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


def check_protocol_on_chip(ppath: Path, protocol: dict, chip: dict, report: Report,
                           schema_ok: bool) -> None:
    """Targets must exist in the chip. Runs even on files with schema errors, so people
    see every problem in one go; the split/pool wiring check needs a valid file."""
    places = commands.chamber_ids(chip) | commands.port_ids(chip)
    steps = protocol.get("steps") if isinstance(protocol.get("steps"), list) else []
    for i, step in enumerate(steps, start=1):
        if not isinstance(step, dict) or not isinstance(step.get("target"), str):
            continue
        target = step["target"]
        if target not in places:
            report.error(f"{rel(ppath)} step {step.get('step', i)}: target '{target}' not in chip {chip['id']}")
            continue
        if schema_ok:
            try:
                planner.step_to_command(step, chip)
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

    step_numbers = {s.get("step") for s in proto[1]["steps"]} if proto else set()
    chambers = commands.chamber_ids(chip_entry[1]) if chip_entry else None
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


def check_cad(chips: dict[str, tuple[Path, dict]], report: Report) -> int:
    # An STL may be one part (named by chamber, port or channel id) or a whole
    # one-piece print (named by the chip spec id, e.g. hello_world_plate.stl).
    known = set(chips)
    for _, chip in chips.values():
        known |= commands.chamber_ids(chip) | commands.port_ids(chip)
        known |= {ch["id"] for ch in chip.get("channels", []) if isinstance(ch, dict) and "id" in ch}
    stls = [p for p in sorted((ROOT / "cad" / "exports").glob("*")) if p.suffix.lower() == ".stl"]
    for p in stls:
        if p.stem not in known:
            report.warn(f"{rel(p)}: '{p.stem}' is not a chamber, port, channel or chip id in any chip spec "
                        f"(name STLs exactly by id, e.g. exchange_1.stl, or by chip id for a one-piece print)")
    return len(stls)


def best_chip(protocol: dict, candidates: list[tuple[Path, dict]]) -> tuple[Path, dict]:
    """The chip spec containing the most of this protocol's targets."""
    steps = protocol.get("steps") if isinstance(protocol.get("steps"), list) else []
    wanted = {s.get("target") for s in steps if isinstance(s, dict)}
    return min(candidates, key=lambda pc: len(wanted - (commands.chamber_ids(pc[1]) | commands.port_ids(pc[1]))))


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
    n_stl = check_cad(chips, report)

    print("capsule-processor contract check")
    print(f"  protocols/    {len(protocol_files)} file(s)")
    print(f"  chip specs    {len(chip_files)} file(s) (chip/ and cad/*_spec.json)")
    print(f"  runs/         {len(run_files)} file(s)")
    print(f"  cad/exports/  {n_stl} STL file(s)")
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

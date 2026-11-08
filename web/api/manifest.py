"""Reading assay2chip's run manifest and presenting it as the API's run shape.

The manifest (`assay2chip/work/<id>/run_manifest.json`, written by assay2chip/run.py) is
the single source of truth for what a run did: one entry per stage execution with its
status, input and output hashes, timings, the files it wrote outside the work directory,
and whether the language model was real or stubbed. Nothing here invents a second status
track - `StageView` only fills the gaps the manifest cannot know (a stage that is running
right now, and the stages a `--from` skipped), and says so.

run.py writes the manifest when the run finishes, so during a run the view is updated
from the subprocess's own stage lines and then corrected by the manifest the moment it
lands. A correction is emitted like any other change, so the timeline never silently
disagrees with the file.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import paths, stages

SEVERITIES = ("blocker", "high", "medium", "low")


def work_dir(run_id: str) -> Path:
    return paths.WORK / run_id


def manifest_path(run_id: str) -> Path:
    return work_dir(run_id) / paths.MANIFEST_NAME


def read_json(path) -> Optional[object]:
    """A JSON file, or None if it is missing, unreadable or half-written.

    Stages write atomically, but a file produced by something else may be mid-write, and
    an unparseable artefact must not take the API down.
    """
    try:
        with open(path, "rb") as handle:
            return json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def load(run_id: str) -> Optional[dict]:
    data = read_json(manifest_path(run_id))
    return data if isinstance(data, dict) else None


def exists(run_id: str) -> bool:
    return manifest_path(run_id).exists()


def known_run_ids() -> List[str]:
    """Every id with a work directory, newest first by modification time."""
    if not paths.WORK.is_dir():
        return []
    rows = []
    for entry in paths.WORK.iterdir():
        if entry.is_dir() and not entry.name.startswith("."):
            try:
                rows.append((entry.stat().st_mtime, entry.name))
            except OSError:
                continue
    rows.sort(reverse=True)
    return [name for _, name in rows]


def title(run_id: str, manifest: Optional[dict] = None) -> str:
    """The paper's title when S0 captured one, else the run id."""
    raw = read_json(work_dir(run_id) / "raw.json")
    if isinstance(raw, dict):
        for key in ("title", "name"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(manifest, dict):
        source = manifest.get("args", {}).get("paper")
        if isinstance(source, str) and source.strip():
            return source.strip()
    return run_id


def created(run_id: str, manifest: Optional[dict] = None) -> Optional[str]:
    if isinstance(manifest, dict) and manifest.get("created_at"):
        return manifest["created_at"]
    try:
        stamp = work_dir(run_id).stat().st_mtime
    except OSError:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(stamp))


# --- the files a run produced ------------------------------------------------

def entry_outputs(entry: dict) -> List[str]:
    """Every path one manifest stage entry says it wrote, repo-relative."""
    out = []
    output = entry.get("output") or {}
    if isinstance(output, dict) and output.get("path"):
        out.append(str(output["path"]))
    for item in entry.get("extra_writes") or []:
        if isinstance(item, dict) and item.get("path") and item.get("exists") is not False:
            out.append(str(item["path"]))
        elif isinstance(item, str):
            out.append(item)
    for item in entry.get("declared_writes") or []:
        if isinstance(item, str):
            out.append(item)
    seen, unique = set(), []
    for path in out:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def listed_paths(run_id: str, manifest: Optional[dict] = None) -> Dict[str, str]:
    """{repo-relative path: the stage that touched it} for everything the manifest names.

    This is the allowlist /file starts from, so it includes inputs as well as outputs: a
    stage's input is a file an earlier stage of the same run produced.
    """
    manifest = manifest if manifest is not None else load(run_id)
    out = {}
    if not manifest:
        return out
    for entry in manifest.get("stages") or []:
        if not isinstance(entry, dict):
            continue
        stage = str(entry.get("stage") or "")
        source = entry.get("input") or {}
        if isinstance(source, dict) and source.get("path"):
            out.setdefault(str(source["path"]), stage)
        for path in entry_outputs(entry):
            out[path] = stage
    out.setdefault(paths.repo_rel(manifest_path(run_id)), "run")
    return out


def chip_ids(run_id: str, manifest: Optional[dict] = None) -> List[str]:
    """The chips this run owns: from partition.json, plus any chip/<id>.json it wrote."""
    found = []
    partition = read_json(work_dir(run_id) / "partition.json")
    if isinstance(partition, dict):
        for chip in partition.get("chips") or []:
            if isinstance(chip, dict) and chip.get("chip_id"):
                found.append(str(chip["chip_id"]))
    for path in listed_paths(run_id, manifest):
        posix = str(path)
        if posix.startswith("chip/") and posix.endswith(".json"):
            found.append(Path(posix).stem)
        elif posix.startswith("cad/exports/"):
            parts = posix.split("/")
            if len(parts) > 3:
                found.append(parts[2])
    seen, unique = set(), []
    for chip in found:
        safe = chip.strip().strip("/")
        if safe and "/" not in safe and safe not in seen and ".." not in safe:
            seen.add(safe)
            unique.append(safe)
    return unique


def findings(run_id: str) -> List[dict]:
    """The CFD findings, from the shape assay2chip/cfd/README.md specifies."""
    data = read_json(work_dir(run_id) / paths.FINDINGS_REL)
    if isinstance(data, dict):
        data = data.get("findings")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def findings_by_severity(run_id: str, items: Optional[List[dict]] = None) -> Dict[str, int]:
    items = findings(run_id) if items is None else items
    if not items:
        return {}
    counts = {}
    for item in items:
        key = str(item.get("severity") or "unknown").lower()
        counts[key] = counts.get(key, 0) + 1
    ordered = {key: counts[key] for key in SEVERITIES if key in counts}
    for key in sorted(counts):
        ordered.setdefault(key, counts[key])
    return ordered


def finding_id(item: dict, index: int) -> str:
    value = item.get("id")
    if value is None:
        value = "%s:%s:%s" % (item.get("chip_id"), item.get("class"), item.get("feature_id"))
    return "%s#%d" % (value, index) if value in (None, "None::None") else str(value)


# --- the stage timeline ------------------------------------------------------

class StageView:
    """The thirteen stages of one run, as the API reports them.

    Built from the manifest when there is one. While a run is in flight the orchestrator
    has not written the manifest yet, so `note_running` / `note_finished` carry the
    subprocess's own stage lines; `apply_manifest` then overwrites them from the file.
    Every mutator returns the rows that actually changed, which is what drives the SSE
    `stage` events.
    """

    def __init__(self, plan_tokens: Optional[List[str]] = None):
        self.rows = {}
        self.order = []
        for number, name, description, _ in stages.SPINE:
            token = stages.token(number)
            self.order.append(token)
            self.rows[token] = {
                "stage": token,
                "name": name,
                "label": stages.label(number, name),
                "description": description,
                "status": "pending",
                "started": None,
                "seconds": None,
                "outputs": [],
                "error": None,
                "loop": 0,
                "source": "plan",
            }
        self.plan = None
        self.set_plan(plan_tokens)

    def set_plan(self, plan_tokens: Optional[List[str]]) -> List[dict]:
        """Mark the stages outside this run's --from/--to range as skipped."""
        if not plan_tokens:
            return []
        wanted = {str(item).split("_")[0] for item in plan_tokens}
        self.plan = [token for token in self.order if token in wanted]
        changed = []
        for token in self.order:
            row = self.rows[token]
            if token not in wanted and row["status"] == "pending":
                row["status"] = "skipped"
                row["error"] = None
                row["source"] = "plan"
                changed.append(dict(row))
        return changed

    def list(self) -> List[dict]:
        return [dict(self.rows[token]) for token in self.order]

    def note_running(self, token: str) -> List[dict]:
        row = self.rows.get(token)
        if row is None or row["status"] == "running" or row["source"] == "manifest":
            return []
        row["status"] = "running"
        row["started"] = row["started"] or _now()
        row["source"] = "live"
        return [dict(row)]

    def note_finished(self, token, status, seconds=None, message=None) -> List[dict]:
        row = self.rows.get(token)
        if row is None or row["source"] == "manifest":
            return []
        mapped = stages.api_status(status)
        row["status"] = mapped
        row["seconds"] = seconds if seconds is not None else row["seconds"]
        row["error"] = message if mapped in ("failed", "not_implemented") else None
        row["message"] = message
        row["source"] = "live"
        return [dict(row)]

    def next_pending(self, after: Optional[str] = None) -> Optional[str]:
        """The next stage in the plan that has not finished - the one about to run."""
        sequence = self.plan or self.order
        if after is not None and after in sequence:
            sequence = sequence[sequence.index(after) + 1:]
        for token in sequence:
            if self.rows[token]["status"] in ("pending", "running"):
                return token
        return None

    def apply_manifest(self, manifest: Optional[dict]) -> List[dict]:
        """Overwrite the live view with what the manifest says. Returns the changes."""
        if not isinstance(manifest, dict):
            return []
        changed = self.set_plan(manifest.get("plan"))
        entries = [e for e in (manifest.get("stages") or []) if isinstance(e, dict)]
        for entry in entries:
            token = str(entry.get("stage") or "")
            row = self.rows.get(token)
            if row is None:
                continue
            fresh = {
                "status": stages.api_status(entry.get("status")),
                "started": entry.get("started_at") or row["started"],
                "seconds": entry.get("duration_s"),
                "outputs": entry_outputs(entry),
                "loop": entry.get("loop", 0),
                "source": "manifest",
            }
            message = entry.get("message")
            fresh["error"] = message if fresh["status"] in ("failed", "not_implemented") else None
            fresh["message"] = message
            llm = entry.get("llm") or {}
            if isinstance(llm, dict) and llm.get("mode"):
                fresh["llm"] = llm.get("mode")
            before = {key: row.get(key) for key in fresh}
            if before != fresh:
                row.update(fresh)
                changed.append(dict(row))
            else:
                row.update(fresh)
        return changed


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


def view_for(run_id: str, manifest: Optional[dict] = None) -> StageView:
    """A stage view for a run that is not in flight: whatever the manifest records."""
    manifest = manifest if manifest is not None else load(run_id)
    view = StageView(manifest.get("plan") if manifest else None)
    view.apply_manifest(manifest)
    return view


def run_status(manifest: Optional[dict], rows: Optional[List[dict]] = None) -> str:
    """One word for the whole run, honest about a stage that was never written."""
    if manifest and manifest.get("status") in ("ok", "error", "refused"):
        if manifest["status"] == "ok":
            statuses = [row["status"] for row in (rows or [])]
            if "failed" in statuses:
                return "failed"
            return "done"
        return "failed"
    if rows:
        if any(row["status"] == "running" for row in rows):
            return "running"
        if any(row["status"] == "failed" for row in rows):
            return "failed"
        if all(row["status"] in stages.TERMINAL for row in rows):
            return "done"
    return "unknown"


def artifacts(run_id: str, manifest: Optional[dict] = None) -> List[dict]:
    """Every file of this run the API can serve, with its kind, stage and size."""
    manifest = manifest if manifest is not None else load(run_id)
    by_path = listed_paths(run_id, manifest)
    rows = {}

    def add(path, stage):
        real = paths.ROOT / path
        try:
            if not real.is_file():
                return
            size = real.stat().st_size
        except OSError:
            return
        rows[str(path)] = {
            "path": str(path),
            "kind": paths.kind_for(path),
            "stage": stage or _stage_guess(path),
            "bytes": size,
        }

    directory = work_dir(run_id)
    if directory.is_dir():
        for current, directories, files in os.walk(directory):
            directories[:] = [d for d in directories if not d.startswith(".")]
            for name in sorted(files):
                if name.startswith("."):
                    continue
                full = Path(current) / name
                rel = paths.repo_rel(full)
                add(rel, by_path.get(rel))
    for path, stage in sorted(by_path.items()):
        add(path, stage)
    for chip in chip_ids(run_id, manifest):
        for figure in sorted(paths.FIGURE_DIR.glob("%s_*.svg" % chip)):
            add(paths.repo_rel(figure), "s9")
        export = paths.EXPORT_DIR / chip
        if export.is_dir():
            for item in sorted(export.rglob("*")):
                if item.is_file():
                    add(paths.repo_rel(item), "s11")
    return [rows[key] for key in sorted(rows)]


_STAGE_BY_FILE = {
    "raw.json": "s0", "assay.json": "s1", "unitops.json": "s3",
    "partition.json": "s4", "verify.json": "s9",
    "report.md": "s12", "report.json": "s12",
    paths.MANIFEST_NAME: "run",
}


def _stage_guess(path) -> Optional[str]:
    posix = str(path)
    name = Path(posix).name
    if name in _STAGE_BY_FILE:
        return _STAGE_BY_FILE[name]
    if "/cfd/" in posix:
        return "s10"
    if posix.startswith("chip/designs/"):
        return "s5"
    if posix.startswith("chip/"):
        return "s7"
    if posix.startswith("protocols/"):
        return "s8"
    if posix.startswith("cad/figures/"):
        return "s9"
    if posix.startswith("cad/exports/") or posix.startswith("print/"):
        return "s11"
    if "/%s/" % paths.UPLOAD_DIRNAME in posix:
        return "upload"
    return None

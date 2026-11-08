"""Read and write the assay2chip stage files, schema-validated, atomically.

Every stage reads the previous stage's file and writes its own. Nothing else should
open those files by hand, because this module is what guarantees three things:

  1. the file matches contracts/schemas/<kind>.schema.json on BOTH read and write, so a
     broken file is caught where it is produced and never travels down the pipeline;
  2. the write is atomic (temp file + os.replace), so a crash cannot leave half a file
     where the next stage will happily read it;
  3. the error names the stage, the file and the offending JSON path.

Work directory: assay2chip/work/<id>/.

    from assay2chip.lib.io import read_stage, write_stage

    assay = read_stage(assay_id, "assay")          # dict, already validated
    write_stage(assay_id, "unitops", unitops)      # validates, then writes

Style follows operator/schema_io.py, and the JSON reading, writing and error wording
come from it rather than being duplicated here.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "operator"))

import schema_io  # noqa: E402

from jsonschema import Draft202012Validator  # noqa: E402
from referencing import Registry, Resource  # noqa: E402

WORK = ROOT / "assay2chip" / "work"
SCHEMA_DIR = ROOT / "contracts" / "schemas"

# Stage file per kind, and which stage writes it (used in error messages).
FILES: dict[str, str] = {
    "raw": "raw.json",
    "assay": "assay.json",
    "unitops": "unitops.json",
    "partition": "partition.json",
    "report": "report.json",
    "manifest": "run_manifest.json",
}
WRITTEN_BY: dict[str, str] = {
    "raw": "S0 ingest",
    "assay": "S1 extract",
    "unitops": "S2 normalise / S3 triage",
    "partition": "S4 partition",
    "report": "S10 report",
    "manifest": "assay2chip/run.py",
}
# Kinds with a formal contract. raw.json and report.json have none yet, so they are
# read and written unvalidated - and this module says so rather than pretending.
SCHEMAS: dict[str, str] = {
    "assay": "assay.schema.json",
    "unitops": "unitops.schema.json",
    "partition": "partition.schema.json",
}

load_json = schema_io.load_json


class StageError(Exception):
    """Anything wrong with a stage file."""


class StageFileMissing(StageError, FileNotFoundError):
    """The stage file a stage wants to read has not been written yet."""


class StageValidationError(StageError):
    """A stage file does not match its schema. Lists every offending path."""


_validators: dict[str, Draft202012Validator] | None = None


def schemas() -> dict[str, dict]:
    """The three assay2chip schemas, by kind."""
    return {kind: load_json(SCHEMA_DIR / name) for kind, name in SCHEMAS.items()}


def validator(kind: str) -> Draft202012Validator:
    """Validator for 'assay', 'unitops' or 'partition'."""
    global _validators
    if _validators is None:
        mine = schemas()
        # Register the repo's schemas too, so a future $ref to chip_spec resolves offline.
        everything = list(mine.values()) + list(schema_io.schemas().values())
        registry = Registry().with_resources(
            (s["$id"], Resource.from_contents(s)) for s in everything
        )
        _validators = {k: Draft202012Validator(s, registry=registry) for k, s in mine.items()}
    if kind not in _validators:
        raise StageError(
            f"no schema for stage kind {kind!r}; schema-backed kinds are "
            f"{', '.join(sorted(SCHEMAS))}"
        )
    return _validators[kind]


def known_kind(kind: str) -> str:
    if kind not in FILES:
        raise StageError(
            f"unknown stage kind {kind!r}; known kinds are {', '.join(FILES)}"
        )
    return kind


def work_dir(assay_id: str, create: bool = False) -> Path:
    """assay2chip/work/<id>/."""
    path = WORK / assay_id
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def stage_path(assay_id: str, kind: str) -> Path:
    """Where a stage file lives. `kind` is a key of FILES, or a relative path
    inside the work directory for the files with no contract (e.g. 'cfd/findings.json')."""
    if kind in FILES:
        return work_dir(assay_id) / FILES[kind]
    if "/" in kind or "." in kind:
        return work_dir(assay_id) / kind
    known_kind(kind)
    return work_dir(assay_id) / FILES[kind]


def has_stage(assay_id: str, kind: str) -> bool:
    return stage_path(assay_id, kind).exists()


def problems(kind: str, data: Any) -> list[str]:
    """Human-readable schema problems, in document order. Empty list means valid.

    Each line is 'ops[2].placement: 'why' is missing' - the JSON path, then what is wrong.
    """
    out = []
    for err in sorted(
        validator(kind).iter_errors(data), key=lambda e: list(map(str, e.absolute_path))
    ):
        where = schema_io.path_str(err)
        out.append(f"{where}: {schema_io.describe(err)}" if where else schema_io.describe(err))
    return out


def check(kind: str, data: Any, path: Path | str | None = None) -> None:
    """Raise StageValidationError unless `data` matches the schema for `kind`.

    Kinds with no schema (raw, report) pass through untouched.
    """
    if kind not in SCHEMAS:
        return
    errs = problems(kind, data)
    if not errs:
        return
    where = Path(path).resolve().relative_to(ROOT).as_posix() if path else f"<{kind}>"
    stage = WRITTEN_BY.get(kind, "?")
    lines = "\n".join(f"  - {e}" for e in errs)
    raise StageValidationError(
        f"{where} (stage {stage}, kind '{kind}') does not match "
        f"contracts/schemas/{SCHEMAS[kind]}:\n{lines}"
    )


def read_stage(assay_id: str, kind: str) -> Any:
    """Read a stage file and validate it. Raises StageFileMissing if it is not there yet."""
    path = stage_path(assay_id, kind)
    if not path.exists():
        rel = path.resolve().relative_to(ROOT).as_posix() if path.is_absolute() else str(path)
        raise StageFileMissing(
            f"{rel} is not there: stage {WRITTEN_BY.get(kind, '?')} has not run for "
            f"id '{assay_id}' yet (python assay2chip/run.py --id {assay_id} --to "
            f"{_stage_token(kind)})"
        )
    data = load_json(path)
    check(kind, data, path)
    return data


def write_stage(assay_id: str, kind: str, data: Any) -> Path:
    """Validate `data`, then write it atomically to its stage file. Returns the path.

    The validation happens BEFORE anything touches the disk, so a stage that produced a
    bad file leaves no file at all rather than a plausible-looking broken one.
    """
    path = stage_path(assay_id, kind)
    check(kind, data, path)
    write_json(path, data)
    return path


def write_json(path: Path | str, data: Any) -> Path:
    """Atomic, UTF-8, 2-space indent, trailing newline - same bytes as schema_io.write_json."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def write_text(path: Path | str, text: str) -> Path:
    """Atomic text write, for report.md and figures."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def sha256_file(path: Path | str) -> str | None:
    """Hash of a file's bytes, or None if it is not there (so the manifest can say so)."""
    path = Path(path)
    if not path.exists():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_data(data: Any) -> str:
    """Hash of data as canonical JSON, for comparing two in-memory structures."""
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _stage_token(kind: str) -> str:
    return {"raw": "s0", "assay": "s1", "unitops": "s3", "partition": "s4", "report": "s10"}.get(
        kind, "s10"
    )

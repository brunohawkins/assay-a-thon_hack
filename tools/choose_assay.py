"""Point the pipeline at one assay: protocols/pipeline_mvp.json becomes a copy of that variant.

    python tools/choose_assay.py capseq      # CapSeq, RNA   (protocols/pipeline_capseq.json)
    python tools/choose_assay.py tipseq      # TIP-seq, DNA  (protocols/pipeline_tipseq.json)
    python tools/choose_assay.py             # say which one is chosen now

Edit the variant files, not pipeline_mvp.json: it records which one it copies ("same_as") and
tools/validate.py fails if they drift apart. The twin's pipeline view opens on the chosen one and
has a toggle for the other. Standard library + jsonschema.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))

import schema_io  # noqa: E402

MVP = ROOT / "protocols" / "pipeline_mvp.json"


def variants() -> dict[str, Path]:
    out = {}
    for p in sorted((ROOT / "protocols").glob("pipeline_*.json")):
        data = schema_io.load_json(p)
        if p != MVP and schema_io.is_pipeline(data) and data.get("variant"):
            out[data["variant"]] = p
    return out


def choose(variant: str, mvp: Path = MVP) -> dict:
    src = variants()[variant]
    data = schema_io.load_json(src)
    out = {"kind": data["kind"], "id": "pipeline_mvp", "name": f"MVP pipeline ({variant}): {data['name']}",
           "same_as": src.relative_to(ROOT).as_posix(), "variant": variant}
    out.update({k: v for k, v in data.items() if k not in out and k not in ("id", "name")})
    errs = schema_io.schema_errors("pipeline", out)
    if errs:
        raise ValueError("; ".join(schema_io.describe(e) for e in errs))
    schema_io.write_json(mvp, out)
    return out


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    known = variants()
    if not args:
        now = schema_io.load_json(MVP).get("variant", "?") if MVP.is_file() else "none"
        print(f"chosen: {now}; variants: {', '.join(known)}")
        return 0
    if args[0] not in known:
        print(f"unknown variant '{args[0]}'; known: {', '.join(known)}", file=sys.stderr)
        return 2
    out = choose(args[0])
    print(f"protocols/pipeline_mvp.json now copies {out['same_as']} ({len(out['modules'])} modules)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

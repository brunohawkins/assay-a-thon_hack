"""Copy Yuvraj's Flowmatter block library into the twin (his branch stays unmerged).

    python tools/vendor_flowmatter.py                     # from origin/chip/flowmatter-paper-to-chip
    python tools/vendor_flowmatter.py --ref <branch or commit>

The twin draws a chamber from its Flowmatter block STL when the chip spec names a block the
library has (chamber "block": "culture_chamber", "y_junction", ...; see twin/blocks.js). This
copies, unchanged, from Yuvraj's branch:

    cad/library/<part>.stl            -> twin/vendor/flowmatter/stl/<part>.stl
    chip/components/catalogue.json    -> twin/vendor/flowmatter/catalogue.json

and writes twin/vendor/flowmatter/SOURCE.json with the commit. Standard library; needs git.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "twin" / "vendor" / "flowmatter"


def git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True).stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ref", default="origin/chip/flowmatter-paper-to-chip")
    args = ap.parse_args(argv)
    try:
        commit = git("rev-parse", "--short", args.ref).decode().strip()
        catalogue_bytes = git("show", f"{args.ref}:chip/components/catalogue.json")
    except subprocess.CalledProcessError as e:
        print(f"Could not read {args.ref}: {e.stderr.decode().strip()} (git fetch first?)", file=sys.stderr)
        return 1
    catalogue = json.loads(catalogue_bytes)
    (OUT / "stl").mkdir(parents=True, exist_ok=True)
    for old in (OUT / "stl").glob("*.stl"):
        old.unlink()
    (OUT / "catalogue.json").write_bytes(catalogue_bytes)
    for part in catalogue["parts"]:
        (OUT / "stl" / f"{part['part']}.stl").write_bytes(git("show", f"{args.ref}:{part['stl']}"))
    (OUT / "SOURCE.json").write_text(json.dumps({"ref": args.ref, "commit": commit, "files": ["catalogue.json", "stl/*.stl"],
                                                 "note": "Unchanged copies from Yuvraj's branch; refresh with tools/vendor_flowmatter.py."}, indent=2) + "\n")
    size = sum(p.stat().st_size for p in (OUT / "stl").glob("*.stl"))
    print(f"copied {len(catalogue['parts'])} block STLs ({size / 1e6:.2f} MB) and the catalogue from {args.ref} @ {commit} "
          f"into {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

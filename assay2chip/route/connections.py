"""S7's input: which part openings must be joined, what each carries, how wide, how long.

S6 writes `assay2chip/work/<id>/connections_<chip_id>.json` beside its placed
`chip/<chip_id>.json`. This module is the ONE place that file is read, and it is
deliberately liberal about its shape, because S6 is being written in parallel. The
canonical shape S7 routes from:

```json
{ "id": "my_assay", "chip_id": "my_assay_c1",
  "connections": [
    { "id": "conn_1", "order": 1,
      "from": {"port": "inlet_1", "feature": "inlet_1_pad", "opening": "pad",
               "position": [1.0, 6.75], "direction": "none", "opening_width_mm": 1.2},
      "to":   {"part": "lyse_12", "feature": "lyse_12", "opening": "inlet",
               "position": [2.0, 6.8357], "direction": "-x", "opening_width_mm": 2.1082},
      "carries": "capsules|droplets|cells|liquid|oil|signals",
      "min_width_mm": 0.2, "width_from": "which contract rule set that",
      "why": "...", "must_not_cross": ["conn_3", "conn_4"] } ] }
```

Four fields of an endpoint carry the real information, and S7 uses all four:

  * `feature` is the ROUTABLE ID - the part the channel actually meets. For a port it is
    the port's PAD, not the hole, because S6 cuts a pad under every port so the
    port-overlap rule in tools/check_chip_spec.py is already satisfied and the channel only
    has to reach the pad's rim. `part` / `port` / `chamber` / `id` are read as fallbacks.
  * `direction` is the opening's OUTWARD NORMAL as a sign and an axis - `-x` means the
    channel arrives travelling in -x, so it meets the part's EAST wall. `none` (a pad) means
    any wall. This, not `opening`, is what fixes the side.
  * `opening` is the opening's NAME on that block (`inlet`, `backflush`, `filtrate`,
    `pad`), not a compass word. A compass word is still accepted, because the hand-written
    fixtures use one, but a name is never an error.
  * `position` is the exact point S6 wants the mouth at, and `opening_width_mm` is how far
    along the wall the mouth may wander from it. S7 honours the point when it can and says
    in the report when it could not: S6 chose those points so two openings on one part
    cannot collide, and moving one silently would undo that.

`must_not_cross` is S6's own read of which connections interleave. S7 does not trust it as
a constraint - it proves planarity on the geometry instead - but it is carried into the
report, and a pair S6 flagged that S7 finds no conflict with is worth knowing about.

Other variations accepted, so a small difference in wording is not a pipeline failure:
`from`/`to` may be a bare id string; `min_width_mm` may be `width_mm`, `min_width_um` or
`width_um`; `target_length_mm` may be `length_target_mm` or `target_length_um`;
`balance_group` may be `balance`, `balanced_with` or `group`; the list may be called
`connections`, `nets` or `links`. `carries` defaults to "capsules", the STRICT case (it
triggers the particle width rule), so an omission cannot silently produce a channel too
narrow for what flows through it.

TODO(S6): there is still no contracts/schemas/connections.schema.json. When one lands,
validate through assay2chip.lib.io instead of normalising here. The fallback below means S7
is runnable without a connections file at all: it re-routes the channels the chip spec
already declares, which is also how an S10 fix-up re-enters the stage.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[2]

CARRIES = ("capsules", "droplets", "cells", "liquid", "oil", "signals")
SIDES = ("east", "north", "west", "south")
LIST_KEYS = ("connections", "nets", "links")
# `feature` first: it is the id of the thing the channel physically meets, and for a port
# that is the PAD, not the hole. Getting this order wrong routes to the wrong feature.
PART_KEYS = ("feature", "part", "chamber", "port", "id", "opening_of")
SIDE_KEYS = ("side", "wall", "openings", "sides", "opening")
# An opening's outward normal, as S6 writes it, to the wall it therefore sits on.
DIRECTION_SIDE = {"-x": "west", "+x": "east", "-y": "south", "+y": "north",
                  "x-": "west", "x+": "east", "y-": "south", "y+": "north"}
COMPASS = {"e": "east", "n": "north", "w": "west", "s": "south",
           "right": "east", "left": "west", "top": "north", "bottom": "south",
           "east": "east", "north": "north", "west": "west", "south": "south"}


class ConnectionError_(ValueError):
    """The connections file cannot be read as a list of connections."""


@dataclass
class Net:
    """One connection to route, in the router's own terms."""

    id: str
    a: str                                   # 'from' feature id
    b: str                                   # 'to' feature id
    a_end: "End | None" = None               # the whole 'from' endpoint, as S6 wrote it
    b_end: "End | None" = None
    order: int | None = None                 # S6's flow order, a tie-break for net ordering
    must_not_cross: tuple[str, ...] = ()      # S6's own read; S7 proves it on the geometry
    carries: str = "capsules"
    min_width_mm: float | None = None
    particle_um: float | None = None
    particle_swollen: bool = False
    capsule_passable: bool | None = None
    target_length_mm: float | None = None
    balance_group: str | None = None
    channel_id: str | None = None             # id to emit; defaults to ch_<id>
    note: str = ""
    audit: dict = field(default_factory=dict)

    @property
    def emit_id(self) -> str:
        return self.channel_id or (self.id if self.id.startswith("ch_") else f"ch_{self.id}")

    @property
    def a_sides(self) -> tuple[str, ...]:
        return self.a_end.sides if self.a_end else ()

    @property
    def b_sides(self) -> tuple[str, ...]:
        return self.b_end.sides if self.b_end else ()

    def as_dict(self) -> dict:
        out = {"id": self.id, "from": self.a, "to": self.b, "carries": self.carries,
               "min_width_mm": self.min_width_mm, "particle_um": self.particle_um,
               "target_length_mm": self.target_length_mm,
               "balance_group": self.balance_group, "channel": self.emit_id,
               "note": self.note}
        for key, end in (("from_opening", self.a_end), ("to_opening", self.b_end)):
            if end is None:
                continue
            out[key] = {"feature": end.feature, "opening": end.opening or None,
                        "sides": list(end.sides) or None,
                        "requested_point": list(end.point) if end.point else None,
                        "port": end.port or None}
        if self.must_not_cross:
            out["must_not_cross"] = list(self.must_not_cross)
        return out


@dataclass(frozen=True)
class End:
    """One end of a connection, as S7 needs it: what to meet, which wall, and where."""

    feature: str
    sides: tuple[str, ...] = ()               # () means any wall
    point: tuple[float, float] | None = None  # the mouth S6 asked for, if it named one
    reach_mm: float | None = None             # how far along the wall the mouth may move
    opening: str = ""                         # S6's name for the opening, for the report
    port: str = ""                            # the port this feature is the pad of, if any


def _endpoint(value: Any, which: str, where: str) -> End:
    if isinstance(value, str):
        return End(value)
    if not isinstance(value, dict):
        raise ConnectionError_(
            f"{where}: '{which}' must be a part id or an object, not {type(value).__name__}")
    feature = next((value[k] for k in PART_KEYS if value.get(k)), None)
    if not isinstance(feature, str):
        raise ConnectionError_(
            f"{where}: '{which}' names no feature to route to (looked for "
            f"{', '.join(PART_KEYS)})")
    sides = _sides_of(value, where, which)
    point = _point(value.get("position"), where, which)
    reach = value.get("opening_width_mm")
    return End(feature, sides, point, float(reach) if reach else None,
               str(value.get("opening") or ""), str(value.get("port") or ""))


def _sides_of(value: dict, where: str, which: str) -> tuple[str, ...]:
    """Which wall(s) this opening may sit on.

    `direction` wins, because it is the opening's outward normal and therefore says which
    wall it is cut in. `opening` is only read as a side when it happens to be a compass
    word: on a real S6 file it is an opening NAME (`inlet`, `backflush`, `pad`) and naming
    one must never be an error. An endpoint with neither is free to use any wall.
    """
    raw_dir = value.get("direction")
    if isinstance(raw_dir, str) and raw_dir.strip().lower() not in ("", "none", "any"):
        token = raw_dir.strip().lower()
        if token in DIRECTION_SIDE:
            return (DIRECTION_SIDE[token],)
        if token in COMPASS:
            return (COMPASS[token],)
        raise ConnectionError_(
            f"{where}: '{which}' direction {raw_dir!r} is not an outward normal "
            f"({', '.join(sorted(DIRECTION_SIDE))}), a compass word or 'none'")
    for key in SIDE_KEYS:
        raw = value.get(key)
        if raw is None:
            continue
        items = [raw] if isinstance(raw, str) else list(raw)
        words = [COMPASS.get(str(s).strip().lower()) for s in items]
        if all(words):
            return tuple(dict.fromkeys(words))
        if key != "opening":
            raise ConnectionError_(
                f"{where}: '{which}' {key} {raw!r} is not one of {', '.join(SIDES)}")
        return ()      # an opening NAME, not a side: no constraint, S7 picks the wall
    return ()


def _point(raw: Any, where: str, which: str) -> tuple[float, float] | None:
    if raw is None:
        return None
    try:
        x, y = float(raw[0]), float(raw[1])
    except (TypeError, ValueError, IndexError, KeyError):
        raise ConnectionError_(
            f"{where}: '{which}' position {raw!r} is not an [x, y] point in mm") from None
    return (x, y)


def _length_mm(raw: dict) -> float | None:
    for key in ("target_length_mm", "length_target_mm", "target_length"):
        if raw.get(key) is not None:
            return float(raw[key])
    if raw.get("target_length_um") is not None:
        return float(raw["target_length_um"]) / 1000.0
    return None


def _width_mm(raw: dict) -> float | None:
    for key in ("min_width_mm", "width_mm"):
        if raw.get(key) is not None:
            return float(raw[key])
    for key in ("min_width_um", "width_um"):
        if raw.get(key) is not None:
            return float(raw[key]) / 1000.0
    return None


def normalise(data: dict | Sequence[dict]) -> list[Net]:
    """A connections file (or a bare list) as `Net`s. Raises on anything ambiguous."""
    if isinstance(data, dict):
        raw_list = next((data[k] for k in LIST_KEYS if isinstance(data.get(k), list)), None)
        if raw_list is None:
            raise ConnectionError_(
                f"no connection list: expected one of {', '.join(LIST_KEYS)} in the file")
    else:
        raw_list = list(data)

    nets: list[Net] = []
    seen: set[str] = set()
    for n, raw in enumerate(raw_list, start=1):
        if not isinstance(raw, dict):
            raise ConnectionError_(f"connection {n} is {type(raw).__name__}, not an object")
        cid = str(raw.get("id") or f"conn_{n}")
        where = f"connection '{cid}'"
        if cid in seen:
            raise ConnectionError_(f"{where}: duplicate connection id")
        seen.add(cid)
        if "from" not in raw or "to" not in raw:
            raise ConnectionError_(f"{where}: needs both 'from' and 'to'")
        a_end = _endpoint(raw["from"], "from", where)
        b_end = _endpoint(raw["to"], "to", where)
        a, b = a_end.feature, b_end.feature
        if a == b:
            raise ConnectionError_(f"{where}: joins '{a}' to itself")
        carries = str(raw.get("carries") or "capsules").lower()
        if carries not in CARRIES:
            raise ConnectionError_(
                f"{where}: carries '{carries}' is not one of {', '.join(CARRIES)} "
                f"(contracts/schemas/chip_spec.schema.json channel.carries)")
        group = next((raw[k] for k in ("balance_group", "balance", "balanced_with", "group")
                      if raw.get(k)), None)
        audit = dict(raw.get("audit") or {})
        for key in ("why", "width_from", "order"):
            if raw.get(key) is not None:
                audit.setdefault(key, raw[key])
        nets.append(Net(
            id=cid, a=a, b=b, a_end=a_end, b_end=b_end, carries=carries,
            order=int(raw["order"]) if str(raw.get("order", "")).strip().isdigit() else None,
            must_not_cross=tuple(raw.get("must_not_cross") or ()),
            min_width_mm=_width_mm(raw), particle_um=raw.get("particle_um"),
            particle_swollen=bool(raw.get("particle_swollen")),
            capsule_passable=raw.get("capsule_passable"),
            target_length_mm=_length_mm(raw),
            balance_group=str(group) if group else None,
            channel_id=raw.get("channel") or raw.get("channel_id"),
            note=str(raw.get("note") or raw.get("why") or ""), audit=audit))
    return nets


def from_chip(chip: dict) -> list[Net]:
    """Fallback: the connections implied by the channels a chip spec already declares.

    Used when there is no connections file - an S10 fix-up re-entering S7, or a
    hand-written chip being re-routed. Widths and `carries` are taken from the existing
    channel, so re-routing never narrows a line that was deliberately wide.
    """
    out = []
    for ch in chip.get("channels", []):
        out.append(Net(
            id=ch["id"], a=ch["from"], b=ch["to"],
            a_end=End(ch["from"]), b_end=End(ch["to"]),
            carries=str(ch.get("carries") or "capsules"),
            min_width_mm=float(ch["width_mm"]),
            capsule_passable=ch.get("capsule_passable"),
            channel_id=ch["id"],
            note=str(ch.get("note") or ""),
            audit={"source": "the chip spec's existing channels (no connections file)"}))
    return out


def path_for(assay_id: str, chip_id: str) -> Path:
    return ROOT / "assay2chip" / "work" / assay_id / f"connections_{chip_id}.json"


def load(assay_id: str, chip_id: str, chip: dict | None = None) -> tuple[list[Net], str]:
    """S6's connections for this chip, else the chip's own channels. Returns (nets, source)."""
    import json

    path = path_for(assay_id, chip_id)
    if path.is_file():
        data = json.loads(path.read_text())
        return normalise(data), path.relative_to(ROOT).as_posix()
    if chip is None:
        raise ConnectionError_(
            f"{path.relative_to(ROOT).as_posix()} is not there: stage S6 build has not run "
            f"for chip '{chip_id}' yet, and no chip spec was given to fall back to")
    return from_chip(chip), "the chip spec's existing channels (no connections file)"

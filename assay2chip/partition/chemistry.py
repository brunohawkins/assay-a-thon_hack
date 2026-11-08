"""Which fluids may share a wetted path, and which must never meet.

There are two different answers and S4 needs both:

``HARD`` pairs must not share a chip at all. The one that matters here is a chaotropic
guanidinium lysis buffer (``contracts/parameters.json lysis.gitc_molar`` records the 4 M
GITC) meeting hypochlorite: the two react to release cyanide gas, so no shared channel,
no shared waste line and no shared board. That is a safety cut, not an optimisation.

``SEPARATE_PATH`` pairs may share a board but not a wetted path, so each keeps its own
outlet. The repo already does this by hand: ``module3_v1_translated`` carries both
``waste`` and ``lysis_waste``, because the chaotropic lysate must not mix with the later
enzymatic washes - carryover inactivates the reverse transcriptase downstream. Those
extra outlets cost ports, so chemistry routinely forces a cut *indirectly*, through
``fabrication_floors.max_ports_per_chip``, and the cut's `why` says so.

**This table is not a contract.** ``contracts/materials.json`` records material-versus-
reagent compatibility, but nothing in the repo records reagent-versus-reagent, so the
groups and pairs below live here with their reasoning attached and every use of them is
reported in the partition's ``unresolved`` with ``assumed_from`` naming this module. If a
contract for it ever lands, this file should read it instead.

``unitops.json`` has no reagent list - S2 drops reagent names when it canonicalises - so
a group is derived from the op kind, then refined by keywords in the op's own prose
(``note``, ``placement.why``). Keyword matching is deliberately narrow and additive: it
can only make the plan more conservative, never less.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

from assay2chip.partition.ops import Op

#: What each group is, and why it is kept apart.
GROUPS: Dict[str, str] = {
    "chaotropic": "guanidinium lysis buffer (contracts/parameters.json lysis.gitc_molar)",
    "oxidiser": "hypochlorite or peroxide decontamination fluid",
    "enzyme": "reverse transcriptase, polymerase, protease or collagenase mix",
    "oil": "fluorinated or mineral carrier oil for droplet generation",
    "alcohol": "ethanol or isopropanol, as in an SPRI clean-up",
    "photoinitiator": "LAP photoinitiator for the 405 nm crosslink",
    "aqueous": "medium, wash buffer, resuspension buffer: the default",
}

#: Keywords that pin a group down when the op's prose names a reagent. Narrow on purpose.
KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "chaotropic": ("guanidin", "gitc", "chaotrop", "thiocyanate", "lysis buffer"),
    "oxidiser": ("hypochlorite", "bleach", "peroxide", "chlorine", "decontaminat"),
    "enzyme": ("enzyme", "reverse transcript", "polymerase", "protease", "proteinase",
               "collagenase", "trypsin", "ligase", "rt mix", "pcr", "amplification mix"),
    "oil": ("fluorinated oil", "mineral oil", "carrier oil", "emulsion", "hfe"),
    "alcohol": ("ethanol", "isopropanol", "spri"),
    "photoinitiator": ("lap", "photoinitiator", "crosslink"),
}

#: Cannot share a chip. Safety first, then anything that destroys the sample outright.
HARD: Tuple[FrozenSet[str], ...] = (
    frozenset({"chaotropic", "oxidiser"}),
)

#: Can share a chip, must not share a wetted path, so each needs its own outlet.
SEPARATE_PATH: Tuple[FrozenSet[str], ...] = (
    frozenset({"chaotropic", "enzyme"}),
    frozenset({"chaotropic", "aqueous"}),
    frozenset({"oil", "aqueous"}),
    frozenset({"oil", "enzyme"}),
    frozenset({"alcohol", "enzyme"}),
)

HARD_WHY: Dict[FrozenSet[str], str] = {
    frozenset({"chaotropic", "oxidiser"}):
        "a chaotropic guanidinium lysis fluid and hypochlorite release cyanide gas on "
        "contact, so they may not share a wetted path, a waste line or a board",
}

SEPARATE_WHY: Dict[FrozenSet[str], str] = {
    frozenset({"chaotropic", "enzyme"}):
        "chaotropic carryover inactivates the enzymes downstream, so the lysate keeps "
        "its own outlet (as module3_v1_translated's lysis_waste does)",
    frozenset({"chaotropic", "aqueous"}):
        "chaotropic lysate must not back up into a buffer line, so it keeps its own outlet",
    frozenset({"oil", "aqueous"}):
        "an oil phase would emulsify in a shared aqueous waste line",
    frozenset({"oil", "enzyme"}):
        "residual oil fouls an enzymatic path",
    frozenset({"alcohol", "enzyme"}):
        "alcohol carryover from a clean-up inhibits the enzymes",
}

#: Group -> the reagent keys contracts/materials.json uses, so the material chooser can
#: ask whether a candidate polymer tolerates what this chip actually touches.
MATERIAL_REAGENTS: Dict[str, Tuple[str, ...]] = {
    "chaotropic": ("guanidine_thiocyanate", "lysis_buffer"),
    "oxidiser": (),
    "enzyme": ("rt_mix", "pcr_mix", "amp_mix", "protease", "collagenase", "trypsin_edta"),
    "oil": ("fluorinated_oil",),
    "alcohol": ("ethanol", "isopropanol"),
    "photoinitiator": ("lap_photoinitiator",),
    "aqueous": ("wash_buffer", "culture_media", "resuspension_buffer"),
}

#: Default group per op kind, used when the prose names nothing.
BY_KIND: Dict[str, str] = {
    "lyse": "chaotropic",
    "thermocycle": "enzyme",
    "encapsulate": "oil",
    "cure": "photoinitiator",
}

SOURCE = "assay2chip/partition/chemistry.py (no reagent-pair contract exists)"


def group_of(op: Op, carried: Optional[str] = None) -> str:
    """The wetted-path group of one op.

    `carried` is the group the fluid arrived in. A wash straight after a lysis is still
    carrying chaotrope, which is precisely why module3 needs a second waste port, so a
    wash with nothing of its own inherits what came before it.
    """
    for group, words in KEYWORDS.items():
        if any(word in op.text for word in words):
            return group
    if op.kind in BY_KIND:
        return BY_KIND[op.kind]
    if op.kind == "wash" and carried:
        return carried
    return "aqueous"


def groups_in_order(ops: Sequence[Op]) -> List[str]:
    """One group per op, with carryover applied along the flow."""
    out: List[str] = []
    carried: Optional[str] = None
    for op in ops:
        group = group_of(op, carried)
        out.append(group)
        if group != "aqueous" or op.kind == "lyse":
            carried = group
        elif op.kind not in ("wash",):
            carried = group
    return out


def hard_conflicts(ops: Sequence[Op]) -> List[Tuple[str, str, str]]:
    """(group a, group b, why) for every pair on this chip that may not coexist."""
    present = set(groups_in_order(ops))
    out = []
    for pair in HARD:
        if pair <= present:
            a, b = sorted(pair)
            out.append((a, b, HARD_WHY[pair]))
    return out


def waste_paths(ops: Sequence[Op]) -> List[Tuple[str, List[int]]]:
    """How many separate outlets the waste needs, and which ops share each one.

    Groups that may legally share a path are merged, so the count is the real number of
    outlet ports rather than one per op.
    """
    groups = groups_in_order(ops)
    buckets: List[Tuple[Set[str], List[int]]] = []
    for op, group in zip(ops, groups):
        if not op.makes_waste:
            continue
        for members, numbers in buckets:
            if all(frozenset({group, other}) not in SEPARATE_PATH
                   and frozenset({group, other}) not in HARD for other in members):
                members.add(group)
                numbers.append(op.number)
                break
        else:
            buckets.append(({group}, [op.number]))
    return [("+".join(sorted(members)), numbers) for members, numbers in buckets]


def separation_reasons(ops: Sequence[Op]) -> List[str]:
    """One sentence per pair that forced an extra outlet, for the chip's `note`."""
    present = sorted(set(groups_in_order(ops)))
    out = []
    for i, a in enumerate(present):
        for b in present[i + 1:]:
            pair = frozenset({a, b})
            if pair in SEPARATE_PATH:
                out.append(SEPARATE_WHY[pair])
    return out


def material_reagents(ops: Sequence[Op]) -> List[str]:
    """contracts/materials.json reagent keys this chip's fluids map onto."""
    out: Set[str] = set()
    for group in set(groups_in_order(ops)):
        out.update(MATERIAL_REAGENTS.get(group, ()))
    return sorted(out)

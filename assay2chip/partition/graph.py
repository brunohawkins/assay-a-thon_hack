"""Planarity: can this chip's connectivity be cut into ONE fluidic layer?

``contracts/blocks.json`` excludes every multilayer block ("We print one fluidic layer,
so anything needing a second layer, a via or a crossing channel is unavailable. This is
why no chip may route two channels across each other"), so a chip whose connectivity
graph is non-planar cannot be built at any size. Area and port count can be solved by
moving to a bigger envelope; a crossing cannot. That makes this the one constraint in S4
that is topological rather than dimensional.

Two questions, and they are not the same one:

``is_planar(graph)``
    Is there any embedding with no crossing?

``fits_one_layer(graph, boundary)``
    Is there an embedding with no crossing **and** every port on the outer boundary?
    Ports are through-holes drilled from the top at >= 5 mm pitch, and a port in the
    middle of the board still has to reach the rim with tubing, so S4 treats ports as
    boundary vertices. A graph planar in the abstract can fail this: K4 is planar, but
    K4 with all four vertices required on the outer face is not (it becomes K5).

    The reduction is the standard one: G has an embedding with every vertex of S on one
    face iff G plus an apex vertex joined to all of S is planar. So this function builds
    that augmented graph and calls ``is_planar``.

The test itself is Demoucron-Malgrange-Pertuiset path addition, which is exact (not a
heuristic and not just Euler's bound) and small enough to read:

  1. a graph is planar iff every biconnected component is planar, so split first;
  2. embed any cycle of the component, giving two faces;
  3. repeatedly take a *fragment* (a connected piece of the not-yet-embedded edges, which
     by definition does not run through an already-embedded vertex) and the faces whose
     boundary contains all of that fragment's attachment vertices;
  4. a fragment with no admissible face proves non-planarity - and it is also the honest
     answer to give S4, because it names the ops involved;
  5. otherwise embed one path of the fragment into one admissible face, splitting that
     face in two, and repeat until nothing is left.

Because every component handed to step 2 is biconnected, every face stays a simple cycle,
which is what makes the face-splitting in ``_split_face`` safe.

Everything is deterministic: vertices are sorted at every choice point, so the same graph
always gives the same answer and the same Kuratowski-style witness.

    from assay2chip.partition import graph

    g = graph.undirected({"op_1": ["in_1", "op_2"], "op_2": ["out_1"]})
    graph.is_planar(g).planar                      # True
    graph.fits_one_layer(g, ["in_1", "out_1"])     # PlanarityResult
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

Graph = Dict[str, Set[str]]

APEX = "__boundary__"


@dataclass(frozen=True)
class PlanarityResult:
    """The verdict, plus enough detail for a cut's `why` to name real ops."""

    planar: bool
    reason: str
    #: Vertices of the fragment that could not be placed, when non-planar.
    witness: Tuple[str, ...] = ()
    #: Vertices already embedded that the fragment had to reach, when non-planar.
    attachments: Tuple[str, ...] = ()
    #: Counted after self-loops and parallel edges are dropped.
    vertices: int = 0
    edges: int = 0

    def __bool__(self) -> bool:
        return self.planar


# --- building and basic graph work -------------------------------------------

def undirected(adjacency: Dict[str, Iterable[str]]) -> Graph:
    """A simple undirected graph from any mapping of vertex -> neighbours.

    Self-loops are dropped (a channel from a part back to itself crosses nothing) and
    every edge is made symmetric, so callers may list an edge once or twice.
    """
    graph: Graph = {}
    for vertex, neighbours in adjacency.items():
        graph.setdefault(str(vertex), set())
        for other in neighbours:
            other = str(other)
            if other == str(vertex):
                continue
            graph.setdefault(other, set())
            graph[str(vertex)].add(other)
            graph[other].add(str(vertex))
    return graph


def add_edge(graph: Graph, a: str, b: str) -> None:
    if a == b:
        return
    graph.setdefault(a, set()).add(b)
    graph.setdefault(b, set()).add(a)


def edge_count(graph: Graph) -> int:
    return sum(len(n) for n in graph.values()) // 2


def edges(graph: Graph) -> List[Tuple[str, str]]:
    """Each edge once, as a sorted pair, in sorted order."""
    out = set()
    for a, neighbours in graph.items():
        for b in neighbours:
            out.add((a, b) if a <= b else (b, a))
    return sorted(out)


def components(graph: Graph) -> List[Set[str]]:
    seen: Set[str] = set()
    out: List[Set[str]] = []
    for start in sorted(graph):
        if start in seen:
            continue
        stack = [start]
        group: Set[str] = set()
        while stack:
            vertex = stack.pop()
            if vertex in group:
                continue
            group.add(vertex)
            stack.extend(sorted(graph[vertex] - group))
        seen |= group
        out.append(group)
    return out


def biconnected_components(graph: Graph) -> List[List[Tuple[str, str]]]:
    """Edge sets of the biconnected components (Hopcroft-Tarjan, iterative).

    A graph is planar iff all of its biconnected components are, and Demoucron needs
    biconnectivity, so this is the first thing ``is_planar`` does. Iterative because a
    long serial chip is a long path and recursion on it is pointless risk.
    """
    index: Dict[str, int] = {}
    low: Dict[str, int] = {}
    parent: Dict[str, Optional[str]] = {}
    stack_edges: List[Tuple[str, str]] = []
    out: List[List[Tuple[str, str]]] = []
    counter = 0

    for root in sorted(graph):
        if root in index:
            continue
        parent[root] = None
        work: List[Tuple[str, List[str]]] = [(root, sorted(graph[root]))]
        index[root] = low[root] = counter
        counter += 1
        while work:
            vertex, pending = work[-1]
            if pending:
                other = pending.pop(0)
                if other == parent.get(vertex):
                    continue
                if other in index:
                    if index[other] < index[vertex]:  # back edge
                        stack_edges.append((vertex, other))
                        low[vertex] = min(low[vertex], index[other])
                    continue
                stack_edges.append((vertex, other))
                parent[other] = vertex
                index[other] = low[other] = counter
                counter += 1
                work.append((other, sorted(graph[other])))
                continue
            work.pop()
            if not work:
                break
            up = work[-1][0]
            low[up] = min(low[up], low[vertex])
            if low[vertex] >= index[up]:
                piece: List[Tuple[str, str]] = []
                while stack_edges:
                    edge = stack_edges.pop()
                    piece.append(edge)
                    if edge == (up, vertex):
                        break
                if piece:
                    out.append(piece)
        if stack_edges:  # whatever is left of this root's tree
            out.append(list(stack_edges))
            stack_edges = []
    return out


def _subgraph(component_edges: Sequence[Tuple[str, str]]) -> Graph:
    graph: Graph = {}
    for a, b in component_edges:
        add_edge(graph, a, b)
    return graph


# --- Demoucron ----------------------------------------------------------------

def _find_cycle(graph: Graph) -> List[str]:
    """A simple cycle, as a vertex list in order. Empty if the graph is a forest.

    Built from a breadth-first spanning tree and one non-tree edge, closed through the
    two endpoints' lowest common ancestor, so the result is always a *simple* cycle -
    which is what the face bookkeeping below depends on.
    """
    parent: Dict[str, Optional[str]] = {}
    depth: Dict[str, int] = {}
    tree_edges: Set[Tuple[str, str]] = set()
    for root in sorted(graph):
        if root in parent:
            continue
        parent[root] = None
        depth[root] = 0
        queue = [root]
        while queue:
            vertex = queue.pop(0)
            for other in sorted(graph[vertex]):
                if other in parent:
                    continue
                parent[other] = vertex
                depth[other] = depth[vertex] + 1
                tree_edges.add((vertex, other) if vertex <= other else (other, vertex))
                queue.append(other)

    for a, b in edges(graph):
        if (a, b) in tree_edges:
            continue
        left, right = [a], [b]
        x, y = a, b
        while depth[x] > depth[y]:
            x = parent[x]
            left.append(x)
        while depth[y] > depth[x]:
            y = parent[y]
            right.append(y)
        while x != y:
            x, y = parent[x], parent[y]
            left.append(x)
            right.append(y)
        # left: a..lca, right: b..lca. The cycle is a..lca..b plus the edge b-a.
        return left + list(reversed(right[:-1]))
    return []


@dataclass
class _Fragment:
    vertices: Set[str] = field(default_factory=set)
    edges: List[Tuple[str, str]] = field(default_factory=list)
    attachments: Set[str] = field(default_factory=set)

    def key(self) -> Tuple:
        return (tuple(sorted(self.attachments)), tuple(sorted(self.vertices)))


def _fragments(graph: Graph, embedded: Set[str],
               embedded_edges: Set[Tuple[str, str]]) -> List[_Fragment]:
    """Connected pieces of the unembedded edges, not conducting through embedded vertices.

    An edge whose BOTH ends are already embedded is a fragment on its own: that is the
    case that matters most on a chip, because it is a channel between two parts that are
    already placed.
    """
    remaining = [e for e in edges(graph) if e not in embedded_edges]
    out: List[_Fragment] = []
    used: Set[Tuple[str, str]] = set()

    for a, b in remaining:
        if (a, b) in used:
            continue
        if a in embedded and b in embedded:
            used.add((a, b))
            out.append(_Fragment({a, b}, [(a, b)], {a, b}))

    free = {v for e in remaining for v in e if v not in embedded}
    seen: Set[str] = set()
    for start in sorted(free):
        if start in seen:
            continue
        fragment = _Fragment()
        stack = [start]
        while stack:
            vertex = stack.pop()
            if vertex in fragment.vertices:
                continue
            fragment.vertices.add(vertex)
            seen.add(vertex)
            for other in sorted(graph[vertex]):
                edge = (vertex, other) if vertex <= other else (other, vertex)
                if edge in embedded_edges:
                    continue
                fragment.edges.append(edge)
                if other in embedded:
                    fragment.vertices.add(other)
                    fragment.attachments.add(other)
                elif other not in fragment.vertices:
                    stack.append(other)
        fragment.edges = sorted(set(fragment.edges))
        out.append(fragment)
    return out


def _fragment_path(fragment: _Fragment, embedded: Set[str],
                   face: Sequence[str]) -> List[str]:
    """A path across the fragment between two attachments that both lie on `face`.

    Interior vertices are fragment-internal (not embedded) by construction, which is
    exactly what makes the path safe to draw inside one face.
    """
    on_face = sorted(a for a in fragment.attachments if a in face)
    if len(on_face) < 2:
        return []
    adjacency: Graph = {}
    for a, b in fragment.edges:
        add_edge(adjacency, a, b)
    start = on_face[0]
    targets = set(on_face[1:])
    # Breadth-first so the embedded path is the shortest one, keeping faces small.
    queue: List[List[str]] = [[start]]
    seen = {start}
    while queue:
        path = queue.pop(0)
        tip = path[-1]
        if tip in targets and len(path) >= 2:
            return path
        if len(path) >= 2 and tip in embedded:
            continue  # cannot run a channel *through* a placed part
        for other in sorted(adjacency.get(tip, ())):
            if other in seen:
                continue
            seen.add(other)
            queue.append(path + [other])
    return []


def _split_face(face: List[str], path: List[str]) -> Tuple[List[str], List[str]]:
    """Draw `path` inside `face` and return the two faces it becomes.

    `face` is a simple cycle (guaranteed while the component is biconnected), so the two
    ends of the path appear on it exactly once each.
    """
    a, b = path[0], path[-1]
    i, j = face.index(a), face.index(b)
    forward = face[i:j + 1] if i <= j else face[i:] + face[:j + 1]
    backward = face[j:i + 1] if j <= i else face[j:] + face[:i + 1]
    interior = path[1:-1]
    return forward + list(reversed(interior)), backward + list(interior)


def _planar_biconnected(graph: Graph) -> PlanarityResult:
    count_v, count_e = len(graph), edge_count(graph)
    if count_v <= 4:
        return PlanarityResult(True, "four vertices or fewer are always planar",
                               vertices=count_v, edges=count_e)
    if count_e > 3 * count_v - 6:
        return PlanarityResult(
            False,
            "Euler's bound: %d channels between %d parts and ports exceeds the 3V-6 = %d "
            "a single layer can hold" % (count_e, count_v, 3 * count_v - 6),
            vertices=count_v, edges=count_e)

    cycle = _find_cycle(graph)
    if not cycle:
        return PlanarityResult(True, "a tree has no cycle, so nothing can cross",
                               vertices=count_v, edges=count_e)

    embedded = set(cycle)
    embedded_edges: Set[Tuple[str, str]] = set()
    for a, b in zip(cycle, cycle[1:] + cycle[:1]):
        embedded_edges.add((a, b) if a <= b else (b, a))
    faces: List[List[str]] = [list(cycle), list(reversed(cycle))]

    # Each pass embeds at least one edge, so this cannot run longer than |E|.
    for _ in range(count_e + 1):
        fragments = _fragments(graph, embedded, embedded_edges)
        if not fragments:
            return PlanarityResult(True, "every channel was embedded with no crossing",
                                   vertices=count_v, edges=count_e)
        scored = []
        for fragment in sorted(fragments, key=lambda f: f.key()):
            admissible = [f for f in faces if fragment.attachments <= set(f)]
            if not admissible:
                return PlanarityResult(
                    False,
                    "no single-layer face can hold the connections between %s: the "
                    "channels would have to cross"
                    % ", ".join(sorted(fragment.attachments)),
                    witness=tuple(sorted(fragment.vertices)),
                    attachments=tuple(sorted(fragment.attachments)),
                    vertices=count_v, edges=count_e)
            scored.append((len(admissible), fragment, admissible))
        scored.sort(key=lambda item: (item[0], item[1].key()))
        _, fragment, admissible = scored[0]
        face = admissible[0]
        path = _fragment_path(fragment, embedded, face)
        if not path:
            return PlanarityResult(
                False,
                "a piece of the network attaches to the rest at only one point and "
                "cannot be routed: %s" % ", ".join(sorted(fragment.vertices)),
                witness=tuple(sorted(fragment.vertices)),
                attachments=tuple(sorted(fragment.attachments)),
                vertices=count_v, edges=count_e)
        faces.remove(face)
        faces.extend(_split_face(face, path))
        embedded |= set(path)
        for a, b in zip(path, path[1:]):
            embedded_edges.add((a, b) if a <= b else (b, a))

    return PlanarityResult(False, "the single-layer routing search did not terminate",
                           vertices=count_v, edges=count_e)


def is_planar(graph: Graph) -> PlanarityResult:
    """Exact planarity test. True means an embedding with no crossing channel exists."""
    if not graph:
        return PlanarityResult(True, "nothing to route")
    total_v, total_e = len(graph), edge_count(graph)
    for piece in biconnected_components(graph):
        if len(piece) < 3:
            continue  # a bridge or a single edge crosses nothing
        result = _planar_biconnected(_subgraph(piece))
        if not result.planar:
            return PlanarityResult(False, result.reason, result.witness,
                                   result.attachments, total_v, total_e)
    return PlanarityResult(True, "every channel was embedded with no crossing",
                           vertices=total_v, edges=total_e)


def fits_one_layer(graph: Graph, boundary: Iterable[str]) -> PlanarityResult:
    """Planar AND every port on the outer boundary.

    This is the question a chip actually asks. The apex reduction is the standard one:
    an embedding with all of `boundary` on one face exists iff the graph plus a vertex
    joined to every boundary vertex is planar. Ports are through-holes that tubing has
    to reach, and blocks.json allows no second layer to reach them under the board, so
    a port that cannot sit on the rim is a redesign, not a detail.
    """
    boundary = [b for b in dict.fromkeys(boundary) if b in graph]
    if not boundary:
        return is_planar(graph)
    augmented: Graph = {k: set(v) for k, v in graph.items()}
    for port in boundary:
        add_edge(augmented, APEX, port)
    result = is_planar(augmented)
    if result.planar:
        return PlanarityResult(True, "planar with every port reachable from the rim",
                               vertices=len(graph), edges=edge_count(graph))
    witness = tuple(v for v in result.witness if v != APEX)
    attachments = tuple(v for v in result.attachments if v != APEX)
    return PlanarityResult(
        False,
        result.reason + " (with every port held on the board boundary)",
        witness, attachments, len(graph), edge_count(graph))


def is_outerplanar(graph: Graph) -> PlanarityResult:
    """Every vertex on the outer face. Used in tests, where the answers are known."""
    return fits_one_layer(graph, sorted(graph))

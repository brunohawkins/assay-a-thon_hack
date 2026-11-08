"""S7's routing engine: connections in, planar channels out.

    from assay2chip.route import Router, normalise, check, cad_path_support

  connections.py  S6's connections_<chip_id>.json, normalised into `Net`s
  obstacles.py    the keep-out model and the routing grid
  maze.py         Lee / A* search on that grid
  meander.py      length and resistance targets, folded in as serpentines
  router.py       net ordering, rip-up and reroute, width and depth rules
  planarity.py    the PATH-AWARE planar check, and the probe for whether cad/ reads `path`
  geometry.py     the plane geometry all of the above share

The stage that drives it is assay2chip/stages/s7_route.py.
"""

from .connections import Net, from_chip, load, normalise
from .geometry import corners, length, simplify
from .meander import Meander, fold_to_length, resistance_targets
from .obstacles import Grid, blocked_mask, features_of
from .planarity import Conflict, cad_path_support, check, errors, straight_line_proxy
from .router import (DEFAULT_GRID_MM, Failure, RouteOutcome, RoutedNet, Router, block_for,
                     connection_graph_is_overfull)

__all__ = [
    "Conflict", "DEFAULT_GRID_MM", "Failure", "Grid", "Meander", "Net", "RouteOutcome",
    "RoutedNet", "Router", "blocked_mask", "block_for", "cad_path_support", "check",
    "connection_graph_is_overfull", "corners", "errors", "features_of", "fold_to_length",
    "from_chip", "length", "load", "normalise", "resistance_targets",
    "simplify", "straight_line_proxy",
]

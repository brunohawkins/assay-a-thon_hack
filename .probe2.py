import json, sys
from pathlib import Path
import numpy as np
sys.path.insert(0, ".")
from assay2chip.cfd import mesh as cfdmesh, scenario as cfdscen, solve as cfdsolve, transport

for chip_id, proto_id, cid, grids in (
    ("module6_v1", "module6_v1", "cx_n", [None, 0.2, 0.05]),
    ("module3_v1_translated", "module3_v1", "main_trap", [None, 0.3]),
):
    for g in grids:
        m = cfdmesh.mesh_chip(Path(f"chip/{chip_id}.json"), grid_mm=g)
        run = cfdscen.build_protocol(Path(f"protocols/{proto_id}.json"),
                                     Path(f"chip/{chip_id}.json"), worst_cases=False)
        recs = [r for r in run.step_scenarios if r.operation == "exchange" and cid in (r.targets or [])]
        fields = cfdsolve.solve_steps(m, [cfdscen.make_scenario(r) for r in run.step_scenarios])
        i = run.step_scenarios.index(recs[0])
        f = fields[i]
        age = transport.mean_age(m, f, diffusivity_m2_s=0.0)
        dz = transport.dead_zones(m, f, age, cid)
        topo = transport.build_topology(m, f)
        mask = topo.node_mask(cid)
        sp = topo.speed_mm_s[mask]
        vol = topo.volume_m3[mask]
        wmean = float((sp*vol).sum()/vol.sum())
        order = np.argsort(sp); cum = np.cumsum(vol[order])/vol.sum()
        med = float(sp[order][np.searchsorted(cum, 0.5)])
        print(f"{chip_id:<24} grid={str(g):<5} cells={dz.cell_count:<6} "
              f"age%={dz.long_age_volume_fraction*100:6.2f} slow%={dz.low_speed_volume_fraction*100:6.2f} "
              f"either%={dz.either_volume_fraction*100:6.2f} "
              f"speed mean={wmean:.4g} median={med:.4g} mean/med={wmean/max(med,1e-30):.2f} "
              f"min={sp.min():.3g} max={sp.max():.3g}")

import sys
from pathlib import Path
import numpy as np
sys.path.insert(0, ".")
from assay2chip.cfd import mesh as cfdmesh, scenario as cfdscen, solve as cfdsolve, transport

m = cfdmesh.mesh_chip(Path("chip/module6_v1.json"), grid_mm=0.2)
run = cfdscen.build_protocol(Path("protocols/module6_v1.json"),
                             Path("chip/module6_v1.json"), worst_cases=False)
fields = cfdsolve.solve_steps(m, [cfdscen.make_scenario(r) for r in run.step_scenarios])
i = next(k for k, r in enumerate(run.step_scenarios) if r.operation == "exchange" and "cx_n" in (r.targets or []))
f = fields[i]
age = transport.mean_age(m, f, diffusivity_m2_s=0.0)
topo = transport.build_topology(m, f)
print("topology notes:", topo.notes)
mask = topo.node_mask("cx_n")
idx = np.flatnonzero(mask)
sp = topo.speed_mm_s[idx]
ag = np.array([age.age_s[topo.ids[j]] for j in idx])
x = topo.x_mm[idx]; y = topo.y_mm[idx]
thr = 0.1 * float((sp*topo.volume_m3[idx]).sum()/topo.volume_m3[idx].sum())
slow = sp < thr
print(f"{slow.sum()} of {len(idx)} cells below threshold {thr:.3e}")
print("their speeds:", np.unique(np.round(sp[slow], 10))[:8])
print("their age range:", ag[slow].min(), ag[slow].max(), " all-cell mean age:", ag.mean())
print("their x range:", x[slow].min(), x[slow].max(), " y range:", y[slow].min(), y[slow].max())
print("fast cells x range:", x[~slow].min(), x[~slow].max(), " y:", y[~slow].min(), y[~slow].max())
# how many distinct x columns, and is slow a column pattern?
for xv in sorted(set(np.round(x, 3))):
    col = np.isclose(x, xv)
    print(f"  x={xv:7.3f}  n={col.sum():3d}  slow={slow[col].sum():3d}  "
          f"mean|u|={sp[col].mean():.4e}  mean age={ag[col].mean():7.2f}")
# raw field u for a slow cell
slow_ids = [topo.ids[j] for j in idx[slow]][:3]
for nid in slow_ids:
    print(nid, "u_mm_s =", f.u_mm_s.get(nid), " age =", age.age_s[nid])

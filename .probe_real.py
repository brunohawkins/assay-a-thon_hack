import json, sys, time
from pathlib import Path
sys.path.insert(0, ".")
from assay2chip.cfd import mesh as cfdmesh, scenario as cfdscen, solve as cfdsolve, transport
from assay2chip.lib import contracts

D = transport.diffusivity("dye").m2_s

for chip_id, proto_id in (("module3_v1_translated", "module3_v1"), ("module6_v1", "module6_v1")):
    m = cfdmesh.mesh_chip(Path(f"chip/{chip_id}.json"))
    run = cfdscen.build_protocol(Path(f"protocols/{proto_id}.json"),
                                 Path(f"chip/{chip_id}.json"), worst_cases=False)
    recs = run.step_scenarios
    scens = [cfdscen.make_scenario(r) for r in recs]
    fields = cfdsolve.solve_steps(m, scens)
    cells = {}
    for n in m.nodes:
        if n.kind == "cell":
            cells[n.feature_id] = cells.get(n.feature_id, 0) + 1
    print(f"\n### {chip_id}  {len(m.nodes)} nodes {len(m.edges)} edges  cells/chamber {cells}")
    print(f"{'step':>4} {'reagent':<34} {'chamber':<8} {'dead%D0':>8} {'dead%Dye':>8} "
          f"{'CV':>6} {'reach%':>8} {'WM%':>7} {'need':>6} {'xWM':>5} {'regime':<18} "
          f"{'sev':<8} {'diffuse s':>12}")
    for rec, f in zip(recs, fields):
        if rec.operation != "exchange":
            continue
        for cid in (rec.targets or []):
            if cells.get(cid, 0) < 4:
                continue
            t0 = time.time()
            try:
                dry = transport.mean_age(m, f, diffusivity_m2_s=0.0)
                ddz = transport.dead_zones(m, f, dry, cid)
                age = transport.mean_age(m, f, diffusivity_m2_s=D)
                dz = transport.dead_zones(m, f, age, cid)
                chk = transport.check_exchange(m, f, rec, cid, diffusivity_m2_s=D)
                w = chk.washout
                src = ddz if ddz.diffusive_clearance_mean_s is not None else dz
                diff = ("-" if src.diffusive_clearance_mean_s is None else
                        f"{src.diffusive_clearance_mean_s:.0f}-{src.diffusive_clearance_max_s:.0f}")
                print(f"{rec.step:>4} {str(rec.fluid)[:34]:<34} {cid:<8} "
                      f"{ddz.either_volume_fraction*100:8.2f} {dz.either_volume_fraction*100:8.2f} "
                      f"{w.cv_delivered:6.2f} {w.fraction_reached*100:8.4f} "
                      f"{w.well_mixed_fraction*100:7.3f} "
                      f"{('%.2f'%w.cv_required_cfd) if w.cv_required_cfd else '>cap':>6} "
                      f"{('%.2f'%w.nonideality_factor) if w.nonideality_factor else '-':>5} "
                      f"{w.regime:<18} {chk.severity:<8} {diff:>12}  [{time.time()-t0:.0f}s]")
            except Exception as e:
                print(f"{rec.step:>4} {cid}: {type(e).__name__}: {str(e)[:160]}")

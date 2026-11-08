"""The pipeline stages, one module each, run in NUMERIC order by assay2chip/run.py.

    s0_ingest  s1_extract  s2_normalise  s3_triage  s4_partition  s5_group  s6_build
    s7_route   s8_protocol s9_verify     s10_cfd    s11_fabricate s12_report

Numeric, not alphabetical: s10 comes after s9. `run.py` sorts on the integer in the file
name, so never rely on a string sort anywhere in this package.

Each module reads the previous stage's file and writes its own, and no stage mutates an
earlier stage's output (S3 is the one exception the spine allows: it adds `placement` to
the ops in unitops.json, the file S2 wrote).

What a stage module must provide
--------------------------------

    def run(**kwargs) -> dict | None

`run.py` inspects the signature and passes only the keyword arguments the function
accepts, so take the ones you need and ignore the rest. The ones on offer:

    ctx          StageContext: ctx.id, ctx.paper, ctx.work_dir, ctx.read(), ctx.write(),
                 ctx.llm_mode, ctx.force, ctx.loop, ctx.log()
    assay_id     the run id (also offered as `id`)
    paper        the --paper argument (also offered as `source`)
    work_root    assay2chip/work
    llm_mode     'stub' or 'real'
    force        the --force flag
    offline      true when no network is allowed (stub mode implies it)
    verbose      the -v flag

Reading and writing the stage files:

    from assay2chip.lib.io import read_stage, write_stage

    unitops = read_stage(assay_id, "unitops")       # validated on the way in
    write_stage(assay_id, "partition", partition)   # validated, then atomic

Declaring writes outside assay2chip/work/<id>/
----------------------------------------------

S5-S11 write into chip/, protocols/ and cad/, where hand-designed files live. Declare
them so the orchestrator can record their hashes and refuse to clobber anything this
pipeline did not make:

    WRITES = ["chip/designs/{chip_id}.json"]        # {chip_id} expands per chip
    # or, when the paths depend on the data:
    def writes(ctx) -> list[str]: ...

A declared path that already exists and is not in this pipeline's ledger stops the stage
before it runs. `--force` overrides, loudly.

Asking for a re-run (S10 only, in practice)
-------------------------------------------

When an automatic fix changes geometry, return a re-run request instead of re-running
anything yourself:

    return {"rerun_from": "s6", "reason": "widened m3_wash_1 to 0.6 mm; re-route and re-verify"}

The orchestrator re-runs from that stage, logs the reason in the manifest, and caps the
number of loops (run.py MAX_RERUN_LOOPS, default 3). It never loops silently.
"""

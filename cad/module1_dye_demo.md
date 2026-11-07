# Module 1 v1: food-dye demo - moved

**Replaced by [docs/demo.md](../docs/demo.md).** This file is kept only because
`protocols/pipeline_mvp.json` cites it by name.

It held a hand-written procedure for the 140 x 75 mm laser-cut board: three syringe pumps,
2 mm square lid holes, and fill times of 1:14-1:51. The chip is now 30 x 20 mm, resin-printed
and hot-embossed, with 0.8 mm drilled ports, and the bench has **one** pump. Every number in
the old procedure was 6-8x too long, and its own first line had said "Out of date" for weeks
because a hand-written time cannot follow a rescale.

So the procedure is no longer written down. `tools/demo_plan.py` derives it from the chip
specs and `contracts/parameters.json` and writes `docs/demo.md`:

```
python tools/demo_plan.py            # the per-chip table
python tools/demo_plan.py --write    # regenerate docs/demo.md
python tools/demo_plan.py --check    # fail if docs/demo.md is out of date
```

For Module 1 specifically it derives: feed `p_in`, vent `p_mid`, plug the other four, at
`dye_demo.bench_flow_ul_min` - the rate that fills `n_cx` in about 30 s, which is also the
rate the twin and `operator/module1_operator.py` read from that same contract key. Predicted
dye arrival times come from `operator/module1_operator.py --times`.

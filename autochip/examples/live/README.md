# Live agent runs (saved for the demo)

Three full runs made with a real agent (`--backend claude-cli`, Claude Code's `claude -p`) on Sunday 8 Nov, on the final code.
Each folder is a complete output: open `report.html` first. `agent/` holds every exchange with the agent
(what it was asked, what it answered, what the checks sent back) and the accepted answers.

Regenerated on 8 Nov after merging main, which added the port-overlap rule to `tools/check_chip_spec.py`: port lines
now run across their whole hole and are at least 0.25 mm wide, so every chip here passes it. The `agent/` answers are
the live run's, unchanged; everything else was rebuilt from them offline (`run_meta.json` says replay).
`prompt_unconstrained/` and `protocol_resin_multilayer/` with the full replay below (same chips, same re-splits).
`paper_assayathon_demo/` with `--resume <folder> --from-stage layout` on its saved final plan, because the new widths
change one round-1 error message (a chip size) and so miss the saved re-split answer. It ends on the same 3 chips
and the same two open retention errors as the live run.

| Folder | Input | Constraints | What happened |
|---|---|---|---|
| `prompt_unconstrained/` | plain-English goal (`../capsule_scrnaseq_prompt.txt`) | `unconstrained` | The agent wrote a 25-step protocol (assumptions flagged; cycle numbers and lysis/RT/enzyme conditions left as placeholders for the lab), fixed one problem the checks sent back, then split it into 4 chips: tri-culture (neurons, microglia, astrocytes in separate chambers around a junction) + dissociation → encapsulation → lysis, RT, split into 4 index-PCR lanes (own index mix each), release → bead clean-up. One chip broke a rule, the agent re-split it; no open problems. |
| `paper_assayathon_demo/` | methods paper PDF (`../spc_methods_paper.pdf`) | `assayathon_demo` (30 × 20 mm, one layer, 6 ports) | 4 chips broke the 30 × 20 mm rules; the agent re-split twice and moved what can't fit to the bench. Final: 3 chips (encapsulation; emulsion break, cure, wash; lysis, RT). Open problems it reports honestly: capsule retention needs a gap the single-layer print can't make (bought mesh / second layer needed), and the 30 µm cell filter is a missing part. |
| `protocol_resin_multilayer/` | wet-lab protocol (`../spc_capsule_protocol.md`) | `resin_print_multilayer` | Layout flagged that resin can't take PCR temperatures; the agent re-planned around it. Final: 4 chips; one missing part (30 µm cell filter, strain at the bench). |

Replay any of them offline in a couple of seconds (no network, same result):

```bash
python -m autochip run --prompt "$(cat autochip/examples/capsule_scrnaseq_prompt.txt)" \
    --backend replay --replay autochip/examples/live/prompt_unconstrained/agent --out autochip/out/demo_prompt
python -m autochip run autochip/examples/spc_methods_paper.pdf --constraints assayathon_demo \
    --backend replay --replay autochip/examples/live/paper_assayathon_demo/agent --out autochip/out/demo_paper
```

Run it live instead (needs Claude Code logged in, ~2 min):

```bash
python -m autochip run autochip/examples/spc_methods_paper.pdf --constraints assayathon_demo --backend claude-cli
```

All inputs are generic, published-style protocols with placeholder values.

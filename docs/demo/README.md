# Sunday demo: the presenter view

```
python tools/demo.py              # serves the repo and opens http://localhost:8000/twin/?present
python tools/demo.py --step 4     # start at step 4
python tools/demo.py --twin       # the normal twin instead
```

Standard library only; if port 8000 is taken it uses the next free one and prints the URL.

**Controls.** Big **Next** / **Back** buttons, or → ← / PageDown PageUp (what a presentation
clicker sends) / Space; Home and End jump to the first and last step; **F** toggles full
screen. The cursor hides after a moment. The twin's own shortcuts are off, so a stray key
can't change anything; every step sets the twin up from scratch, so Back, Next and fast
clicking always land on a clean step.

**Offline.** No network at all: three.js is vendored in `twin/vendor/`, every file comes from
this repo, and the AI operator is the **scripted** one (no API call; a recorded LLM run is
not used here). `tools/record_presenter.py` refuses any request outside the repo, so a clean
recording is also the offline check.

**What is real.** Everything on screen is the digital twin: simulated hardware, flows from the
chip's network model, labelled "Simulation" on every step. Module 1 is the chip being made for
the food-dye demo — a resin-printed tool, hot-embossed, not laser-cut from sheet
(`cad/fabrication.json`); the other modules exist in the twin only. The single-cell barcoding
stage between Modules 3 and 4 is our chemistry on **bought** droplet hardware: its features are
below the 200 µm in-plane floor, so there is no printable chip of ours for it.

## The steps

| # | Step | What the audience sees |
|---|---|---|
| 1 | From a prompt to a chip | A typed prompt, then: Flowmatter parts → chip spec → our checker passes it; the chip appears with its real part shapes. A cached result of Yuvraj's tool (`prompt_to_chip/`), so it runs offline. ![](step_1_prompt.jpg) |
| 2 | The whole pipeline, at real scale | The 3D scene: every chip (30 × 20 mm) in a snake, tubes between them, the sample moving chip to chip; dashed NEXT ghosts for the barcoding and hairpin placeholders. ![](step_2_pipeline.jpg) |
| 3 | Module 1: separate growth | Red, yellow and blue dye (neurons, glia, astrocytes) fill their own chambers and leave through p_mid. ![](step_3_grow.jpg) |
| 4 | Module 1: interaction | Only the neuron inlet is fed: red crosses the junction into the other two chambers. ![](step_4_interact.jpg) |
| 5 | Unsafe request refused | “Cap p_mid and vent p_g_out” is typed; the operator answers in one sentence from the live safety check (the glia chamber would get 33% neuron and 33% astrocyte medium); BLOCKED, ports unchanged. ![](step_5_refuse.jpg) |
| 6 | Harvest, and on down the line | All three chambers flush out through p_mid; then, in the pipeline scene, the cells move on to cell prep and encapsulation. ![](step_6_harvest.jpg) |
| 7 | Module 4 | Rotary-valve fill A B C D D C B A, then thermocycling on all four chambers. ![](step_7_m4.jpg) |
| 8 | Module 5 | Collagenase release at 37 °C, magnetic bead capture, one sublibrary per pass. ![](step_8_m5.jpg) |

Each step is a big title and one short caption at the bottom; no side panels.

## Backup video

[presenter_backup.webm](presenter_backup.webm): the full sequence at 1920×1080, about 90 s.
Re-record after changing the twin:

```
pip install playwright && python -m playwright install chromium   # once
python tools/record_presenter.py      # video + one screenshot per step, here
```

With ffmpeg installed it trims the loading screen and re-encodes the video small (VP9).

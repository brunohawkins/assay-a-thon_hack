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
chip's network model, labelled "Simulation" on every step. Module 1 is the chip being
laser-cut for the food-dye demo; the other modules exist in the twin only.

## The steps

| # | Step | What the audience sees |
|---|---|---|
| 1 | From a prompt to a chip | A typed prompt; the steps grouped into Flowmatter library parts; the chip spec Yuvraj's tool built; our checker passes it; the chip appears with its real part shapes. A cached result (`prompt_to_chip/`, from his branch), so it runs offline. ![](step_1_prompt.jpg) |
| 2 | Pipeline | All six modules left to right with FINAL / DRAFT / TRANSLATED / MISSING badges; cells move card to card. ![](step_2_pipeline.jpg) |
| 3 | Module 1: separate growth | Red, yellow and blue dye (neurons, glia, astrocytes) fill their own chambers and leave through p_mid; time ×60. ![](step_3_grow.jpg) |
| 4 | Module 1: interaction | Only the neuron inlet is fed: red crosses the junction into the other two chambers. ![](step_4_interact.jpg) |
| 5 | Unsafe request refused | Asked to cap p_mid and vent p_g_out during separate growth, the operator checks the flow model and refuses in one sentence (the glia chamber would get 33% neuron and 33% astrocyte medium); the ports stay as they were. ![](step_5_refuse.jpg) |
| 6 | Harvest | All three chambers flush out through p_mid; then, in the pipeline, the cells move on to Module 2. ![](step_6_harvest.jpg) |
| 7 | Module 4 | Rotary-valve fill A B C D D C B A, index primers, thermocycling on all four chambers. ![](step_7_m4.jpg) |
| 8 | Module 5 | Collagenase release at 37 °C, dilution, magnetic bead capture (magnet on under bead_cx). ![](step_8_m5.jpg) |

## Backup video

[presenter_backup.webm](presenter_backup.webm): the full sequence at 1920×1080, about 90 s.
Re-record after changing the twin:

```
pip install playwright && python -m playwright install chromium   # once
python tools/record_presenter.py      # video + one screenshot per step, here
```

With ffmpeg installed it trims the loading screen and re-encodes the video small (VP9).

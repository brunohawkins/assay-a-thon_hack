# Module 1 v1: food-dye demo (Sunday)

Run food dye through the laser-cut chip (`chip/module1_v1.json`) next to the twin, and
check that the liquid goes where the design says. No cells, so flow rates can be higher
than the protocol's (shear only matters with cells).

**Predicted times** below come from `operator/network.py` and `operator/module1_operator.py`
(flows solved on the DXF geometry; dye moved through each channel, chamber and the
2 × 2 × 3 mm inlet hole). Each is a range: **first tint** (the fast centre of the stream,
~2/3 of the mean) to **fully coloured** (plug flow). Start the stopwatch when the dye
enters the port hole: the tubing to the chip is not in the prediction.

## Kit

| Syringe | Dye | Port it goes on |
|---|---|---|
| A | red | `p_in` (neuron side) |
| B | yellow | `p_g_in` (glia) |
| C | blue | `p_a_in` (astrocyte) |

- Three syringe pumps (or one multi-syringe pump), set to **50 µL/min** each. The
  protocol itself runs at 11 µL/min; at that rate every time below is 4.5× longer (table at the end).
- Plugs for the ports you cap, and open tubing (or nothing) for the ports you vent, into a waste dish.
- Prime the chip with water first, so the chambers start clear and there are no bubbles.
- In the twin: open tab **1 v1 (MJ)**, tick **Physical demo**, set the bench rate to the rate
  you use, pick the mode, and press **Start stopwatch** when you start the pumps. Type the
  times you measure into the **measured** boxes; the twin shows them against the prediction.

Port positions (mm from the bottom-left corner of the board): `p_in` (12, 37.5),
`p_g_in` (74, 64), `p_a_in` (74, 11), `p_mid` (82, 37.5), `p_g_out` (128, 52), `p_a_out` (128, 23).

## 1. Separate growth: three colours meet only at the junction

| Port | `p_in` | `p_g_in` | `p_a_in` | `p_mid` | `p_g_out` | `p_a_out` |
|---|---|---|---|---|---|---|
| Set to | **red, 50 µL/min** | **yellow, 50 µL/min** | **blue, 50 µL/min** | **open (vent)** | plug | plug |

**What you should see:** each chamber fills with its own colour only: red `n_cx`, yellow
`g_cx`, blue `a_cx`. The three streams meet in the small junction `j_mid`, which turns a
muddy mix, and leave together through `p_mid`. **No colour should enter another chamber.**
150 µL/min leaves `p_mid`.

| Dye | reaches `j_mid` | reaches `p_mid` |
|---|---|---|
| red (from `p_in`) | 1:14 – 1:51 | 1:14 – 1:52 |
| yellow (from `p_g_in`) | 1:14 – 1:50 | 1:14 – 1:51 |
| blue (from `p_a_in`) | 1:14 – 1:50 | 1:14 – 1:51 |

What to watch for:
- the inlet and the junction channel of `g_cx` and of `a_cx` both join the chamber's
  **left** edge, about 10 mm apart: the right-hand part of those two chambers may colour
  slowly (the flow mostly takes the short way round). That is worth seeing for real.
- **If a colour creeps into another chamber, a plug is leaking or a port is open that
  shouldn't be.** The twin blocks exactly this case: venting `p_g_out` instead of `p_mid` pushes
  red and blue medium into `g_cx`.

## 2. Interaction N → G+A: red crosses into the other two chambers

Run straight after separate growth (so `g_cx` is yellow and `a_cx` blue). Stop syringes B and C.

| Port | `p_in` | `p_g_in` | `p_a_in` | `p_mid` | `p_g_out` | `p_a_out` |
|---|---|---|---|---|---|---|
| Set to | **red, 50 µL/min** | plug | plug | **plug** | **open (vent)** | **open (vent)** |

**What you should see:** red leaves `n_cx`, crosses `j_mid` and splits **50 / 50** into
`g_cx` and `a_cx` (the layout is mirror-symmetric). Yellow and blue are pushed out ahead of
it through `p_g_out` and `p_a_out`; the two chambers turn orange and purple, then red.

| Dye | reaches `j_mid` | reaches `p_g_out` | reaches `p_a_out` |
|---|---|---|---|
| red (from `p_in`) | 1:14 – 1:51 | 3:22 – 5:04 | 3:22 – 5:04 |

`p_g_in` and `p_a_in` must be **plugged**, not just switched off: a stopped pump with an open
line acts as a vent, and red would also run back up those lines.

## 3. Harvest

**3a. Harvest as the design doc lists it** (feed `p_in`, vent `p_mid`):

| Port | `p_in` | `p_g_in` | `p_a_in` | `p_mid` | `p_g_out` | `p_a_out` |
|---|---|---|---|---|---|---|
| Set to | **clear water, 50 µL/min** | plug | plug | **open (vent)** | plug | plug |

**What you should see:** only `n_cx` flushes. Clear water pushes its contents through
`j_mid` and out of `p_mid` (first water at `p_mid` after 1:15 – 1:53). **`g_cx` and `a_cx` keep
their colour: no flow goes through them.** This is a finding: with these ports, the doc's harvest
mode can't sweep the glia and astrocyte chambers.

**3b. Collect as the protocol does it** (back-flush the two outer chambers too):

| Port | `p_in` | `p_g_in` | `p_a_in` | `p_mid` | `p_g_out` | `p_a_out` |
|---|---|---|---|---|---|---|
| Set to | **red, 50 µL/min** | plug | plug | **open (vent)** | **yellow, 50 µL/min** | **blue, 50 µL/min** |

**What you should see:** all three chambers are swept towards the junction at once and
everything pools out of `p_mid` (150 µL/min). That is the hand-off to Module 2.

| Dye | reaches `j_mid` | reaches `p_mid` |
|---|---|---|
| red (from `p_in`) | 1:14 – 1:51 | 1:14 – 1:52 |
| yellow (from `p_g_out`) | 1:14 – 1:51 | 1:14 – 1:51 |
| blue (from `p_a_out`) | 1:14 – 1:51 | 1:14 – 1:51 |

## 4. Pumps off: diffusion

Stop every pump and plug every port. Colours slowly spread out of `j_mid` into the three
branch channels: about **8.8 mm in 12 hours** (the design doc's estimate). The branches are
14–17 mm long, so overnight the dye should not reach a chamber, but a longer stop would.

## At the protocol's rate (11 µL/min per syringe)

| Mode | to `j_mid` | to the outlets |
|---|---|---|
| Separate growth | 5:35 – 8:25 | `p_mid` 5:36 – 8:28 |
| Interaction N → G+A | 5:37 – 8:25 | `p_g_out`, `p_a_out` 15:20 – 23:00 |
| Harvest (doc) | 5:37 – 8:25 | `p_mid` 5:42 – 8:33 |
| Collect (protocol) | 5:35 – 8:25 | `p_mid` 5:37 – 8:28 |

Running pressure at 50 µL/min is about 0.27 mbar (0.06 mbar at 11 µL/min): far below what a
pressure controller holds, which is why the design doc says to drive the chip with syringe pumps.

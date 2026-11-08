# Hardware roadmap

Today every closed-loop run uses the **virtual chip** (SIMULATED HARDWARE): no physical
pump, valves or sensors. This page is the plan for moving the same control loop onto real
hardware: which **class** of part fills each slot, the **one spec that actually decides**
the choice, which adapter in `operator/adapters/` it plugs into, and which part of the
control loop it closes. It is ordered by what unblocks the most work first.

- **Procurement detail lives outside the repo.** Suppliers, part numbers and quotes are
  kept in `private/` (git-ignored) per `CONFIDENTIALITY.md`. Here, cost is only an
  **order of magnitude** — enough to sequence the roadmap, not a shopping list. Re-cost
  before ordering: parts in this space move often.
- **Drivers:** the pump driver is written, and the valve and sensor drivers are documented
  stubs. All of them are **UNTESTED – no hardware available**.

## Where the hardware fits

```
                 ┌──────────── actuators ────────────┐
 operator ─▶ plan ─▶ safety ─▶  syringe pumps   (move liquid)      ─▶  chip
                                pinch valves    (route it)
                                Peltier stage   (hold temperature)
    ▲                                                                    │
    │            ┌──────────── sensors ──────────────┐                  │
    └── refit ◀──  pump volume     (did it deliver?)   ◀─────────────────┘
        + alerts   pressure        (is the mesh clogging?)
                   outlet absorbance (is the wash done?)
                   temperature     (is it at temperature?)
                   camera          (are capsules escaping?)
```

A small microcontroller reads the sensors and switches the valves over USB serial. The
protocol for that is already written down in `adapters/valves.py` and `adapters/sensors.py`.

## The phases

### Phase 1 — prove the pump driver against real motion

**Buy:** one syringe pump (slot 1).
**Goal:** run Module 3 (the capsule wash) open loop, with a person switching routes by hand
on stopcocks, and diff the dry-run adapter's printed actions against real plunger motion.
**Why first:** nothing moves without a pump, and its driver is the only one already written,
so this phase tests code we have rather than code we would have to write.
**Exit criteria:** commanded volume and flow match delivered volume within the pump's own
reported error; `pump_volume_ul` comes back through the adapter.

### Phase 2 — close the clog and wash loops

**Buy:** pressure sensor + microcontroller (slots 2 and 5), then the absorbance sensor
(slot 3) on the same microcontroller, ideally in one order.
**Goal:** the two cheapest sensors carry most of the closed loop's value.
- Pressure closes the **clog loop** and feeds the mesh-resistance refit. In the simulated
  clog scenario the closed loop lost **0** capsules where the same run open loop lost
  **2,109 of 10,000** (true values from the simulation). It also lets us *measure* the
  escape pressure, still a TODO placeholder (5 kPa) in `contracts/parameters.json`.
- Absorbance answers **"is the wash complete?"** so washes can stop on signal instead of
  running a fixed volume, and it refits chamber volume (simulated: model error +11.1% →
  +1.1%).
**Exit criteria:** measured-vs-predicted refits run on real readings; `pressure_high` and
the back-flush recovery fire from hardware, not from the simulator.

### Phase 3 — remove the human from the loop

**Buy:** the remaining pumps (slot 1, one per reagent), then the pinch valves (slot 4).
**Goal:** a run needs no syringe swaps and no hand-switched routes. Order matters: extra
pumps are cheaper than valves and remove the more annoying interruption first. Buy valves
once the closed loop works on the bench and manual routing is the measured bottleneck —
until then the pump adapter just logs each route for a person to set.
**Exit criteria:** a full protocol runs start to finish untouched, including back-flush
recovery.

### Phase 4 — measure what we currently infer, and extend coverage

**Buy as needed:** camera (slot 6), Peltier stage (slot 7).
- The camera turns "capsules lost" from a model estimate into a measurement and makes
  `capsule_count_drop` real. **Blocked on software:** the counting code is not written.
- The Peltier stage is only needed for protocols using `hold_temp`. Module 3 (the capsule
  wash) has none; the three full placeholder protocols do. **Blocked on software:** there
  is no heater driver yet.

## Part slots in detail

### Slot 1. Syringe pumps: actuation

| | |
|---|---|
| What | A motor pushes a syringe plunger at a set rate, delivering exact volumes (µL) at exact flows (µL/min). One pump per reagent: the example rig has three (capsule suspension, wash buffer, resuspension buffer). |
| Spec that decides | Volume and flow resolution at our working range, and a text command set over serial. |
| Cost, order of magnitude | Commercial lab single-channel pumps: **high hundreds of dollars each**, so **order 1k** for the three-pump rig. An open-source motorised-syringe design is roughly **an order of magnitude cheaper** for the same three channels (published: Booeshaghi et al. 2019, *Sci. Rep.*, [PMC6711986](https://pmc.ncbi.nlm.nih.gov/articles/PMC6711986)). Syringes and tubing: small, not yet costed. |
| Adapter | `operator/adapters/syringe_pump.py` (`SyringePumpAdapter`, `SyringePumpDriver`). Command strings live in `contracts/hardware/pump_<model>.json`. `pump_generic.json` uses the plain-ASCII RS-232 style (`DIA`, `RAT`, `VOL`, `RUN`, `STP`, `DIS`) that many lab pumps share, so such a pump needs only a JSON file. An open-source pump runs its own microcontroller firmware and needs its own command file; whether it also needs driver changes is unknown until tried. Pump-to-reagent mapping: `contracts/hardware/rig_example.json`. |
| Closes | **Actuation:** nothing moves without it. It also gives the first sensor reading, `pump_volume_ul` (volume actually dispensed, from `DIS`). That checks delivery against the plan and feeds the reservoir and waste bookkeeping behind the `reagent_low` alert. |

### Slot 2. Pressure sensor: clog detection and the mesh model

| | |
|---|---|
| What | Measures the pressure just upstream of the retention mesh, on a tee in the inlet line. |
| Spec that decides | **Range.** Capsules escape at 5 kPa and the alert fires at 4.25 kPa, so a **±1 psi (±6.9 kPa) differential** part is the right size: it spans the alert and escape points with headroom. A **±0.5 kPa** part is too small — it saturates below the escape pressure. Second spec: the **media rating**. Check it before letting liquid touch the port; if the part is rated dry-gas only, isolate it. Prefer a barbed-port version for tubing. |
| Cost, order of magnitude | Board-mount differential sensors: **tens of dollars**. Purpose-built inline microfluidic pressure sensors do the same job for **order 1k**, which is not worth it at this range. |
| Adapter | `PressureSensor` in `operator/adapters/sensors.py` → reading `pressure_kpa`, read through the microcontroller (`READ pressure_kpa` → `VALUE <n>`). |
| Closes | **The clog loop and the mesh-resistance model.** Every time slice the controller compares measured and predicted pressure, then refits the mesh resistance. The monitor raises `pressure_high` at 85% of the escape limit, and the operator back-flushes. In the simulated clog scenario, the closed loop lost **0** capsules; the same clog run open loop lost **2,109 of 10,000** (true values from the simulation). It also lets us *measure* the escape pressure, which is a TODO placeholder (5 kPa) in `contracts/parameters.json`. |

### Slot 3. Outlet absorbance: is the wash complete?

| | |
|---|---|
| What | An LED and a light sensor across a clear section of the outlet tube. A dye in the incoming reagent shows how much of the old liquid has been flushed out. |
| Spec that decides | Enough channels or the right band to track the dye against the tubing background; a breakout that speaks I²C to the same microcontroller as slot 2. A multi-channel light/colour sensor breakout is the easy form factor. |
| Cost, order of magnitude | Sensor breakout: **tens of dollars**, plus an LED, clear tubing and a printed holder (a few small parts). Commercial inline flow cells: not costed, and not needed for this. |
| Adapter | `AbsorbanceSensor` in `operator/adapters/sensors.py` → reading `outlet_absorbance`. |
| Closes | **"Is the wash complete?"** and the **chamber-volume model**: the controller refits the chamber volume from how fast the outlet signal rises. In the simulated closed-loop scenario, that cut the model's chamber-volume error from +11.1% to +1.1%. Caveat: the dye has to be compatible with whatever is in the capsules; check before using it with real samples. |

### Slot 4. Pinch valves: routing

| | |
|---|---|
| What | A solenoid squeezes silicone tubing shut, so liquid only ever touches the tubing. The valves switch the flow path between forward wash, back-flush and release. |
| Spec that decides | Tubing ID (1/32" and 1/16" ID silicone are the common sizes; the smaller bore is usually the cheaper part), 2-way normally-closed action, and a 12 VDC coil so one supply runs the whole rig. |
| Cost, order of magnitude | **The most expensive item per unit:** several hundred dollars each, so **order 1k–2k** for the example rig's four valves (`v_inlet`, `v_waste`, `v_backflush`, `v_release`). Some ranges are quoted rather than listed, so treat any figure as indicative until re-costed. MOSFET or relay channels to switch them: a few small parts. |
| Adapter | `ValveController` in `operator/adapters/valves.py` (stub; the serial protocol `SET` / `ROUTE` / `GET` / `ALL 0` is specified there). Named routes come from `contracts/hardware/rig_example.json`. |
| Closes | **Routing without a person.** Until valves exist, the syringe-pump adapter logs each route for someone to set by hand with stopcocks. That works, but it makes the back-flush recovery slow and manual. |

### Slot 5. Microcontroller (shared by sensors and valves)

| | |
|---|---|
| What | Runs the small firmware that answers `READ <sensor>` and `SET <valve>` over USB serial (115200 8N1). |
| Spec that decides | Native USB serial, I²C for the sensor breakouts, and enough GPIO for the valve switch channels. Any current hobbyist microcontroller board clears this. |
| Cost, order of magnitude | **A few dollars** — negligible against everything else, so buy spares. |
| Adapter | The serial link used by `sensors.py` and `valves.py`. Firmware not written. |
| Closes | Nothing on its own: it is the bridge every sensor and valve goes through. |

### Slot 6. Camera: capsule counting and retention

| | |
|---|---|
| What | A camera over the outlet channel counts capsules going past (a blob counter); a second view of the trap could check the cell layer. |
| Spec that decides | **Resolving 70–140 µm capsules in a flowing channel.** That needs a macro or microscope lens, not just a sensor — the lens, not the camera body, is the hard part and is not yet specified. Frame rate must beat the capsule transit time. |
| Cost, order of magnitude | Camera module and a single-board host computer: **tens of dollars each**. Suitable optics: not costed; the published open-source pump design above includes a tested microscope build worth copying. |
| Adapter | `CapsuleCounter` in `operator/adapters/sensors.py` → reading `capsules_counted`. The counting software is not written. |
| Closes | **Capsule loss, measured directly.** Today loss is only inferred from pressure. A counter makes the `capsule_count_drop` alert real, and turns "capsules lost" into a measurement rather than a model estimate. |

### Slot 7. Peltier stage + temperature sensor: `hold_temp`

| | |
|---|---|
| What | A thermoelectric (Peltier) plate under the chip heats or cools it; a thermistor on the plate reads the temperature. |
| Spec that decides | **The driver must match the plate's current draw, and must be an H-bridge** so the stage can heat *and* cool (polarity reverses). A small motor-driver board is the usual choice but is typically rated below a 12 V / 5 A plate, so either run the plate below full power or size the driver up. A 10 kΩ epoxy-coated thermistor is the standard readback. |
| Cost, order of magnitude | Plate with heatsink, thermistor and driver board: **tens of dollars** all in. Power supply, fan and plate-to-chip contact: not costed, and the thermal contact is the real engineering problem. |
| Adapter | Reading: `TemperatureSensor` in `operator/adapters/sensors.py` → `temperature_c`. Output: **no driver yet**. `syringe_pump.py` raises `NotImplementedError` for `hold_temp`, and an `adapters/heater.py` on the same serial link still needs writing. |
| Closes | **`hold_temp`.** The model already has first-order heating, but the controller does not refit the heating time constant yet. `safety.py` enforces the material limits from `contracts/materials.json` (PMMA 80 °C, printed resin 50 °C). |

## Budget shape

Rough totals, to sequence the phases rather than to order against:

| | Order of magnitude |
|---|---|
| Phase 1 (one commercial pump) | high hundreds of dollars |
| Phase 1 (open-source three-pump build instead) | low hundreds of dollars |
| Phase 2 (both sensors + microcontroller + LED/tubing) | under a hundred dollars |
| Phase 3 (two more pumps + four valves) | order 2k–3k, valve-dominated |
| Phase 4 (camera + optics, Peltier stage) | low hundreds of dollars, plus unspecified optics |

The shape of this is the point: **Phase 2 is the cheapest phase and closes the most
loop.** Phase 3 is where the money goes, and it buys convenience rather than capability,
so it should wait until the loop is proven on the bench.

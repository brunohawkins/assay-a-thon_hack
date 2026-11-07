# Hardware roadmap

Today every closed-loop run uses the **virtual chip** (SIMULATED HARDWARE): no physical
pump, valves or sensors. This page lists what to buy to run the same loop on a real chip:
what each part is, what it costs, which adapter in `operator/adapters/` it plugs into, and
which part of the control loop it closes. It ends with the order to buy things in.

- **Prices** are public listings found on 2026-11-07, single units, in the listing's own
  currency, before shipping. Tax is excluded unless marked "incl.". Old catalogue and launch
  prices are labelled as such. Where no public price was found the cost says **unknown**.
  Check the seller before ordering: several of these move often.
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

## 1. Syringe pumps: actuation

| | |
|---|---|
| What | A motor pushes a syringe plunger at a set rate, delivering exact volumes (µL) at exact flows (µL/min). One pump per reagent: the example rig has three (capsule suspension, wash buffer, resuspension buffer). |
| Cost | **Commercial:** New Era NE-1000 single-channel, US$815 ([SIS](https://www.sisweb.com/lc/new-era-ne1000.htm)); an older New Era catalogue lists US$750 ([catalogue page](https://www.classe.cornell.edu/~dms79/D-lineNotes/components/Syringe%20Pumps/NE-1000.htm)). Three pumps ≈ US$2,450. **Open source:** the Poseidon system from the Pachter lab: "all the parts for 3 pumps and a microscope cost under $400" ([Poseidon hardware page](https://pachterlab.github.io/poseidon/hardware); paper: [Booeshaghi et al. 2019, *Sci. Rep.*](https://pmc.ncbi.nlm.nih.gov/articles/PMC6711986)). Syringes and tubing: unknown. |
| Adapter | `operator/adapters/syringe_pump.py` (`SyringePumpAdapter`, `SyringePumpDriver`). Command strings live in `contracts/hardware/pump_<model>.json`. `pump_generic.json` uses the plain-ASCII RS-232 style (`DIA`, `RAT`, `VOL`, `RUN`, `STP`, `DIS`) that many lab pumps share, so such a pump needs only a JSON file. A Poseidon pump runs its own Arduino firmware and needs its own command file; whether it also needs driver changes is unknown until tried. Pump-to-reagent mapping: `contracts/hardware/rig_example.json`. |
| Closes | **Actuation:** nothing moves without it. It also gives the first sensor reading, `pump_volume_ul` (volume actually dispensed, from `DIS`). That checks delivery against the plan and feeds the reservoir and waste bookkeeping behind the `reagent_low` alert. |

## 2. Pressure sensor: clog detection and the mesh model

| | |
|---|---|
| What | Measures the pressure just upstream of the retention mesh, on a tee in the inlet line. |
| Cost | **Board-mount:** Honeywell ABP2 ±1 psi (±6.9 kPa) differential, US$23.73 (ABP2MDAN001PDSA3XX, [DigiKey](https://www.digikey.ca/en/products/detail/honeywell-sensing-and-productivity-solutions/ABP2MDAN001PDSA3XX/25993803)); barbed-port version AUD 43.53 (ABP2DRRT001PDAA3XX, [DigiKey AU](https://www.digikey.com.au/en/products/detail/honeywell-sensing-and-productivity-solutions/ABP2DRRT001PDAA3XX/14542799)). **Range matters:** capsules escape at 5 kPa and the alert fires at 4.25 kPa, so ±1 psi fits. The ±0.5 kPa ABP2 parts (US$22.08, [DigiKey BR](https://www.digikey.com.br/en/products/detail/honeywell-sensing-and-productivity-solutions/ABP2MRRN002NDAA3XX/26264555)) are too small. Check the exact part's media rating before letting liquid touch the port: if it is dry-gas only, isolate it. **Microfluidic inline:** Elveflow MPS, US$886–1,012 depending on range ([WPI](https://wpiinc.com/var-505879-high-accuracy-microfluidic-pressure-sensor)). |
| Adapter | `PressureSensor` in `operator/adapters/sensors.py` → reading `pressure_kpa`, read through the microcontroller (`READ pressure_kpa` → `VALUE <n>`). |
| Closes | **The clog loop and the mesh-resistance model.** Every time slice the controller compares measured and predicted pressure, then refits the mesh resistance. The monitor raises `pressure_high` at 85% of the escape limit, and the operator back-flushes. In the simulated clog scenario, the closed loop lost **0** capsules; the same clog run open loop lost **2,109 of 10,000** (true values from the simulation). It also lets us *measure* the escape pressure, which is a TODO placeholder (5 kPa) in `contracts/parameters.json`. |

## 3. Outlet absorbance: is the wash complete?

| | |
|---|---|
| What | An LED and a light sensor across a clear section of the outlet tube. A dye in the incoming reagent shows how much of the old liquid has been flushed out. |
| Cost | Adafruit AS7341 10-channel light/colour sensor breakout, US$18.95 ([Adafruit](https://www.adafruit.com/products/4698)). LED, clear tubing and a printed holder: unknown, a few parts. Commercial inline flow cells: unknown. |
| Adapter | `AbsorbanceSensor` in `operator/adapters/sensors.py` → reading `outlet_absorbance`. |
| Closes | **"Is the wash complete?"** and the **chamber-volume model**: the controller refits the chamber volume from how fast the outlet signal rises. In the simulated closed-loop scenario, that cut the model's chamber-volume error from +11.1% to +1.1%. Caveat: the dye has to be compatible with whatever is in the capsules; check before using it with real samples. |

## 4. Pinch valves: routing

| | |
|---|---|
| What | A solenoid squeezes silicone tubing shut, so liquid only ever touches the tubing. The valves switch the flow path between forward wash, back-flush and release. |
| Cost | Cole-Parmer VapLock 2-way normally-closed pinch valve, 12 VDC: 1/32" ID silicone NZ$403.70 incl. GST ([John Morris Group](https://www.johnmorrisgroup.co.nz/cole-parmer-vaplock-solenoid-operated-pinch-valve-two-way-normally-closed-132-id-silicone-12-vdc-2)); 1/16" ID NZ$898.15 incl. GST ([John Morris Group](https://www.johnmorrisgroup.co.nz/cole-parmer-vaplock-2-way-pinch-valve-normally-closed-silicone-tubing-116-inner-diameter-316-outer-diameter-12-vdc-2)). Bio-Chem Fluidics pinch valves: unknown, quote only ([catalogue](https://www.chromtech.net.au/pdf2/BCF-Pinch%20Valve%20Brochure%20020112.pdf)). The example rig has four valves (`v_inlet`, `v_waste`, `v_backflush`, `v_release`): about NZ$1,600 at the cheapest listing. MOSFET or relay channels to switch them: unknown, a few parts. |
| Adapter | `ValveController` in `operator/adapters/valves.py` (stub; the serial protocol `SET` / `ROUTE` / `GET` / `ALL 0` is specified there). Named routes come from `contracts/hardware/rig_example.json`. |
| Closes | **Routing without a person.** Until valves exist, the syringe-pump adapter logs each route for someone to set by hand with stopcocks. That works, but it makes the back-flush recovery slow and manual. |

## 5. Microcontroller (shared by sensors and valves)

| | |
|---|---|
| What | Runs the small firmware that answers `READ <sensor>` and `SET <valve>` over USB serial (115200 8N1). |
| Cost | Raspberry Pi Pico 2, US$5 at launch ([Tom's Hardware](https://tomshardware.com/raspberry-pi/raspberry-pi-pico/raspberry-pi-pico-2-launches-with-arm-risc-v-cores-hands-on-with-the-new-dollar5-microcontroller)). |
| Adapter | The serial link used by `sensors.py` and `valves.py`. Firmware not written. |
| Closes | Nothing on its own: it is the bridge every sensor and valve goes through. |

## 6. Camera: capsule counting and retention

| | |
|---|---|
| What | A camera over the outlet channel counts capsules going past (a blob counter); a second view of the trap could check the monolayer. |
| Cost | Raspberry Pi High Quality Camera, US$50 at launch, lenses from US$25 at launch ([Raspberry Pi](https://www.raspberrypi.com/news/new-product-raspberry-pi-high-quality-camera-on-sale-now-at-50)). Host computer: Raspberry Pi 5 4 GB, US$60 at launch, raised to US$70 in Dec 2025 ([Phoronix](https://www.phoronix.com/news/Raspberry-Pi-Price-Hikes-RAM)). Seeing 70–140 µm capsules needs a macro or microscope lens: unknown (the Poseidon microscope is one tested design, inside its under-$400 total). |
| Adapter | `CapsuleCounter` in `operator/adapters/sensors.py` → reading `capsules_counted`. The counting software is not written. |
| Closes | **Capsule loss, measured directly.** Today loss is only inferred from pressure. A counter makes the `capsule_count_drop` alert real, and turns "capsules lost" into a measurement rather than a model estimate. |

## 7. Peltier stage + temperature sensor: `hold_temp`

| | |
|---|---|
| What | A thermoelectric (Peltier) plate under the chip heats or cools it; a thermistor on the plate reads the temperature. |
| Cost | Adafruit 12 V 5 A Peltier module with heatsink, AUD 22.65 incl. GST ([Core Electronics](https://core-electronics.com.au/peltier-thermo-electric-cooler-module-12v-5a.html)). 10 kΩ epoxy thermistor, US$4.00 ([Adafruit](https://adafruit.com/product/372)). H-bridge driver (heat *and* cool), Adafruit DRV8871, US$7.50 ([Adafruit](https://www.adafruit.com/products/3190)): rated 3.6 A, so run the Peltier below full power or use a bigger driver. Power supply, fan, and plate-to-chip contact: unknown. |
| Adapter | Reading: `TemperatureSensor` in `operator/adapters/sensors.py` → `temperature_c`. Output: **no driver yet**. `syringe_pump.py` raises `NotImplementedError` for `hold_temp`, and an `adapters/heater.py` on the same serial link still needs writing. |
| Closes | **`hold_temp`.** The model already has first-order heating, but the controller does not refit the heating time constant yet. `safety.py` enforces the material limits from `contracts/materials.json` (PMMA 80 °C, printed resin 50 °C). |

## Buying order, and why

1. **One syringe pump** (or the Poseidon build). Nothing runs without one, and its driver
   is already written. One pump plus hand-switched stopcocks is enough to run Module 3
   (the capsule wash) open loop and compare the dry-run adapter's printed actions with real motion.
2. **Pressure sensor + Pico 2** (about US$30). The cheapest item with the biggest payoff.
   It closes the clog loop, which is what protects the capsules (simulated: 0 vs 2,109
   lost), and it lets us measure the escape pressure that is still a placeholder.
3. **Absorbance sensor** (about US$19 plus LED and tubing), ideally in the same order as
   step 2, on the same microcontroller. It says when a wash is really done, so washes can
   stop early instead of running a fixed volume.
4. **Remaining pumps**, one per reagent, so a run needs no syringe swaps.
5. **Pinch valves.** The most expensive item per unit. Until then the operator pauses and a
   person switches routes. Buy them once the closed loop works on the bench and manual
   routing is the bottleneck.
6. **Camera.** Measures capsule loss directly, but needs counting software that does not
   exist yet.
7. **Peltier stage.** Only needed for protocols with `hold_temp`. Module 3 (the capsule wash) has none;
   the three full placeholder protocols do.

| Starter kit (steps 1–3) | Approximate cost |
|---|---|
| Commercial pump (NE-1000) + ABP2 + AS7341 + Pico 2 | ≈ US$863 + LED/tubing/syringes |
| Poseidon (3 pumps + microscope) + ABP2 + AS7341 + Pico 2 | < US$450 + LED/tubing/syringes |

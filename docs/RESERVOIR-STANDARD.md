# Gardyn reservoir standard

Measured constants for the Gardyn Home reservoir, and the method for calibrating
a unit against them.

This exists because the shipped water constants are nominal placeholders
(`WATER_FULL_CM=5`, `WATER_EMPTY_CM=20`, `TANK_CAPACITY_GALLONS=5`) that do not
describe any real tank. Every reading derived from them — depth, percentage,
gallons remaining — is wrong by a fixed factor on every installation.

The numbers below were measured gallon-by-gallon on two towers on 2026-09-21.
See [Raw data](#raw-data) for everything collected and
[Limits](#limits-of-this-data) for what it does and does not support.

## The one concept to get right

The ultrasonic sensor reports the **airgap**: the distance from the sensor face
down to the water surface. It *grows as the tank drains*. Depth is its
complement, measured up from the tank floor:

```
depth = WATER_EMPTY_CM - airgap
```

`WATER_EMPTY_CM` is the airgap with the reservoir empty, i.e. the sensor's height
above the tank floor. **Every threshold in the config is an airgap, so every
threshold is meaningless without that unit's own `WATER_EMPTY_CM`.** Copying a
threshold between towers without converting it is the most common way to end up
with an interlock that fires at the wrong level.

## The standard

Two kinds of constant. Depths belong to the tank and are the same everywhere.
Airgaps belong to the installation and must be derived per unit.

### Universal — the tank

| Quantity | Value | |
|---|---|---|
| Usable depth, empty → fill line | **18.54 cm** | 7.30" |
| Capacity to the fill line | **6.0 gal** | 22.7 L |
| Reservoir footprint | 60.3 × 27.9 cm | 23¾" × 11" |
| Reservoir height | 25.4 cm | 10" |
| Empty-tank airgap (typical) | **23.1 cm** | see below |

Recommended threshold depths:

| Threshold | Depth | | Rationale |
|---|---|---|---|
| Pump cutoff | 10.16 cm | 4.00" | intake head; see [Why depth](#why-the-thresholds-are-depths-not-percentages) |
| Low-water alert | 13.97 cm | 5.50" | warns ~1.5" of water before the cutoff |
| Cleaning-run cutoff | 5.08 cm | 2.00" | cleaning runs on a deliberately drained tank |

### Per unit — the installation

Only one value has to be measured: **`WATER_EMPTY_CM`**, the airgap with the
reservoir empty and fully seated. Across three towers it varied by just 1.2 mm
(23.05 / 23.08 / 23.17), so **23.1 is a sound fallback** — but measuring it takes
two minutes and catches a failure mode described below.

Everything else follows:

```
WATER_FULL_CM      = WATER_EMPTY_CM - 18.54
PUMP_CUTOFF_CM     = WATER_EMPTY_CM - 10.16
WATER_LOW_CM       = WATER_EMPTY_CM - 13.97
CLEANING_CUTOFF_CM = WATER_EMPTY_CM - 5.08
TANK_CAPACITY_GALLONS = 6.0
```

Worked example for the three reference towers:

| | Tower 1 | Tower 2 | Tower 3 |
|---|---|---|---|
| `WATER_EMPTY_CM` | 23.05 | 23.08 | 23.17 |
| `WATER_FULL_CM` | 4.51 | 4.54 | **4.63** ← measured |
| `PUMP_CUTOFF_CM` | 12.89 | 12.92 | 13.01 |
| `WATER_LOW_CM` | 9.08 | 9.11 | 9.20 |
| `CLEANING_CUTOFF_CM` | 17.97 | 18.00 | 18.09 |

## Calibrating a unit

1. **Empty and seat the reservoir.** Fully empty, and firmly seated in its
   cradle — see [the seating trap](#the-seating-trap).
2. **Take the empty baseline.** Read the airgap several times and take the
   median. If `mqtt.service` is running, ask *it* for the reading rather than
   reading the sensor yourself — two processes triggering the same ultrasonic
   sensor cross-talk and both return wrong values. Publish to
   `<identifier>/water/level/get` and watch `<identifier>/water/level`. During
   first-time setup, before the service starts, reading the driver directly is
   safe.
3. **Sanity-check it** against the table below.
4. **Derive the rest** with the formulas above and write them to `.env`.
5. **Re-take the baseline after any reservoir work.** Non-negotiable; see below.

### Interpreting the baseline

| Reading | Meaning |
|---|---|
| 23.1 ± 0.2 cm | Healthy. Use it. |
| ~100.00 | **Not a reading.** `gpiozero.DistanceSensor` defaults to `max_distance=1` m and saturates at 1.0, which becomes 100.00 cm. It means no echo returned: reservoir absent, sensor displaced, or nothing within range. Never treat it as a depth. |
| Noticeably under 23 | Water still in the tank, or the reservoir is not fully seated. |
| More than ~2 mm off 23.1 | Investigate seating before accepting it. |

### The seating trap

A reservoir that is *almost* seated reads **~5 mm shallower** than one that is
fully home. On one reference tower this produced a baseline of 23.60 instead of
23.08 — and it went unnoticed because the readings were rock steady, 0.8 mm
spread across twelve samples, both before and after reseating.

**Low spread proves the sensor is healthy. It does not prove the tank is
seated.** Only reseating and re-measuring does. A 5 mm error shifts every
derived threshold by 5 mm, including the dry-run interlock.

## Why the thresholds are depths, not percentages

The pump cutoff protects the pump, and what a pump needs is **head over its
intake** — an absolute depth. Expressing it as a percentage couples it to the
capacity figure, so any error in capacity silently moves the interlock.

Intake designs also differ between units; some draw from the bottom, some from
the side. The failure case is an intake stranded above water while a level
reading still looks reasonable. 4" of water leaves usable margin for that on a
Gardyn Home.

For reference, on this tank 4.00" of depth is about **3.2 gallons remaining of
6.0**, i.e. a little over half. "Pump refuses" therefore does *not* mean "nearly
empty" — the percentage is what should drive topping up.

## Capacity is 6.0 gallons, not 5

Tower 3 reached its moulded fill line on exactly six one-gallon fills, at an
airgap of 4.63 cm — a depth of 18.54 cm.

The shipped `TANK_CAPACITY_GALLONS=5` understates the tank by 20%, and it is the
single largest error this exercise found. It feeds every gallons-remaining
display.

A plain box of this footprint would hold 8.1 gallons to that depth, so roughly
38% of the volume is taken up by internal structure — pump well, tower-base
mounts, floor ribs.

### The linear model is fine once capacity is right

`gallons_remaining()` maps airgap to volume with a straight line. Against the
true 6-gallon capacity the fit is good — worst residual 0.77 cm of depth, about
0.25 gal:

| Gal | Measured depth | Linear | Residual |
|---|---|---|---|
| 1 | 3.26 | 3.09 | +0.17 |
| 2 | 6.18 | 6.18 | 0.00 |
| 3 | 9.61 | 9.27 | +0.34 |
| 4 | 13.13 | 12.36 | +0.77 |
| 5 | 15.53 | 15.45 | +0.08 |
| 6 | 18.54 | 18.54 | 0.00 |

Residuals are all ≥ 0, so the tank is slightly convex — it fills faster than
linear near the bottom, where the floor is cluttered. The effect is real but
small enough that a straight line remains the right model.

## Model detection cannot select these constants

**Do not key reservoir calibration off `detect_model()`.** It infers the model
from the temperature/humidity chip alone:

```python
if i2c_device_present(DHT20_ADDRESS) or config.SENSOR_TYPE == "DHT20":
    return "gardyn 3.0"
if config.SENSOR_TYPE == "AM2320":
    return "gardyn 2.0"
```

Sensor generation and reservoir generation are **independent axes**, and only the
first is observable. In the reference fleet:

| Tower | Sensor | `detect_model()` | Actual reservoir |
|---|---|---|---|
| 1 | AM2320 | "gardyn 2.0" | 1st Edition |
| 2 | DHT20 | "gardyn 3.0" | 2nd generation |
| 3 | DHT20 | "gardyn 3.0" | **1st Edition** |

Towers 2 and 3 report an identical model string and their tanks measure 3%
apart. Tower 1 reports "2.0" while carrying a 1st Edition tank. Any mapping from
model string to reservoir constants gets at least one of these wrong.

Asking the installer to declare a generation is not a fix either — the
distinction is not marked on the unit, and it was only established here by
measurement.

**This is why the standard needs no model question.** Every safety threshold is
a depth, and depths are generation-independent. The generations differ only in
capacity, by 3%, which affects a displayed number and nothing else.

For a future model, default to the universal constants and measure the baseline.
If the tank is genuinely different, the baseline check flags it rather than
silently applying wrong constants.

## The generation difference

At an identical six gallons:

| Tower | Reservoir | Depth at 6 gal | Mean effective area |
|---|---|---|---|
| 3 | 1st Edition | 18.54 cm | 1225 cm² |
| 2 | 2nd generation | 19.13 cm | 1187 cm² |

5.9 mm apart — beyond pour error accumulated over six fills. The 2nd generation
holds about **3% less water per cm of depth**, traceable to a more obstructed
floor: at the first gallon its effective area was 901 cm² against 1161 cm².

Capacity to the same 18.54 cm depth is therefore **6.0 gal (1st Ed)** and
**5.8 gal (2nd gen)**. Using 6.0 for both costs a 2nd-gen owner a 3% optimistic
reading — smaller than the error in topping a tank up by hand, and far better
than the 20% error in the shipped default.

## Raw data

Airgap in cm, measured through the MQTT service, median of 8–17 samples per
point. Spread was ≤ 0.9 mm at every point.

**Tower 2** — 2nd generation reservoir, empty baseline 23.08

| Gal | Airgap | Depth | Rise | Eff. area |
|---|---|---|---|---|
| 0 | 23.08 | 0.00 | — | — |
| 1 | 18.88 | 4.20 | 4.20 | 901 |
| 2 | 16.13 | 6.95 | 2.75 | 1376 |
| 3 | 12.79 | 10.29 | 3.34 | 1133 |
| 4 | 10.38 | 12.70 | 2.41 | 1571 |
| 5 | 7.29 | 15.79 | 3.09 | 1225 |
| 6 | 3.95 | 19.13 | 3.34 | 1133 |

**Tower 3** — 1st Edition reservoir, empty baseline 23.17

| Gal | Airgap | Depth | Rise | Eff. area |
|---|---|---|---|---|
| 0 | 23.17 | 0.00 | — | — |
| 1 | 19.91 | 3.26 | 3.26 | 1161 |
| 2 | 16.99 | 6.18 | 2.92 | 1296 |
| 3 | 13.56 | 9.61 | 3.43 | 1104 |
| 4 | 10.04 | 13.13 | 3.52 | 1075 |
| 5 | 7.64 | 15.53 | 2.40 | 1577 |
| 6 | 4.63 | 18.54 | 3.01 | 1258 |

Gallon 6 on Tower 3 reached the moulded fill line exactly.

Per-gallon rises wobble by up to 1.1 cm and do so **out of phase between the two
towers**, while the cumulative depths converge to within 1.7% by gallon 5. Two
tanks with genuinely different geometry could not do that, so the wobble is pour
variance amplified by differencing consecutive readings — not tank shape. Read
the cumulative columns; treat the per-gallon column as noisy.

## Limits of this data

Stated plainly, so nobody over-trusts these numbers:

- **Two towers filled gallon-by-gallon**, both by one person using a one-gallon
  jug filled by eye to roughly ±1–2 fl oz (~±1%).
- **The fill line was measured on one tank.** Tower 2 has no moulded line; its
  fill level is transferred by depth, on the basis that both tanks share the same
  external dimensions.
- **The 3% generation difference rests on a single pair of units.** It is
  consistent across the whole fill, but it is n=1 per generation.
- **Capacity is 6.0 ± ~0.1 gal**, limited by pour accuracy rather than by the
  sensor.
- **Near-full readings are the least reliable.** The sensor's minimum range is
  ~2 cm and the fill line sits at ~4.6 cm. In practice the readings there were
  stable (0.0–0.8 mm spread), but the margin is thin.
- Tower 1 contributed an empty baseline only; it was not filled incrementally,
  and its 23.05 predates the discovery of the seating trap.

Independent measurements from other units are welcome, particularly from a
2nd-generation tank with a moulded fill line.

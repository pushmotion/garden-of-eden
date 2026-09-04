# Deployment notes — PushMotion Gardyn tower

Context for anyone (human or agent) picking up this fork. It covers what differs
from upstream, how the water sensor was calibrated, how to verify the unit
safely, and what is still open.

Host-specific access details (addresses, accounts, the firewall rule that lets
the Pi reach its broker across two networks) are deliberately **not** in this
file — this fork is public. They live in the operator's private notes.

## What this deployment is

A Gardyn 1.0 tower driven by a Raspberry Pi Zero W (`armv6l`), reporting to a
Home Assistant instance via HA's Mosquitto add-on. HA is on a *different*
network segment from the Pi, so MQTT crosses a routed boundary — if the device
silently drops out of HA, suspect that route before suspecting the Pi.

Hardware matches the stock Gardyn 1.0 layout: AM2320 temp/humidity, PCT2075 PCB
temp (`0x48`), INA219 pump power monitor (`0x40`), an ultrasonic water sensor
(echo GPIO19 / trigger GPIO26), grow light on GPIO18, pump on GPIO24, physical
button on GPIO13, and two USB cameras (even-numbered `/dev/video*` nodes; the
odd ones are metadata).

## Why this fork exists

Upstream `iot-root/garden-of-eden` at 2.0.0 ships three defects that make the
pump effectively unusable from Home Assistant:

1. `pump/speed/set` never publishes `pump/state`, so HA shows the pump OFF while
   it runs — and once HA believes it is off, the brightness slider stops
   responding until the service restarts.
2. `pump/command` ON runs a water-low guard that returns early without
   publishing state, so the power button looks completely inert.
3. `pump/speed/set` has **no** water guard, so the speed slider bypasses the
   dry-run protection the power button enforces. Because (1) and (2) make the
   button appear broken, the slider is exactly what a user reaches for.

The fix is on `feat/gardyn-tower-local`, this fork's build branch (see
[Branch model](#branch-model)). The same work is proposed upstream as
`iot-root/garden-of-eden#95`; if that merges, this branch can rebase onto it
with no divergence.

**The Pi must not track `iot-root` `main` directly** — doing so silently
reintroduces all three defects.

## Branch model

Two branches, with deliberately different jobs. Getting these confused is the
single easiest way to break a running tower.

| Branch | Role |
|---|---|
| `feat/gardyn-tower-local` | **The build.** Everything the towers actually run. The Pi deploys from here and nothing else. |
| `main` | **A mirror of upstream `iot-root/main`.** Never carries fork work, so it always answers "what does upstream have?" |

Remotes follow the standard convention: `origin` is this fork, `upstream` is
`iot-root`. They were once reversed, which made a reflexive `git push` aim at
upstream.

Keeping `main` a pure mirror is what makes comparison possible. Fork work is
never merged into it — that is why the tower branch is a long-lived branch rather
than something that lands on `main`.

```bash
# Refresh the mirror. --ff-only guarantees main can never diverge; if this
# refuses to fast-forward, something has been committed to main by mistake.
git fetch upstream
git checkout main && git merge --ff-only upstream/main && git push origin main

# What is ours that upstream does not have:
git log --oneline main..feat/gardyn-tower-local

# What upstream has that the build does not:
git log --oneline feat/gardyn-tower-local..main

# Take upstream's changes into the build (review the diff above first):
git checkout feat/gardyn-tower-local && git merge main
```

Upstream merging one of this fork's PRs will bring the same content back under
different commit hashes, so expect the occasional trivial conflict where content
matches but history does not. Resolve in favour of the build branch and move on.

## Water sensor: what the numbers mean

The ultrasonic sensor measures the **airgap** from the sensor face down to the
water surface. That number *grows as the tank drains*, which is why the stock
"Water Level" entity reads backwards. Depth is its complement:

```
depth = WATER_EMPTY_CM - airgap
```

`WATER_EMPTY_CM` is the airgap with the reservoir empty (sensor face to tank
floor); `WATER_FULL_CM` is the airgap at the fill line. Both live in `.env` and
are **unit-specific** — the shipped defaults will not match a given tower's
sensor mounting.

Calibrated values for this unit:

| Variable | Value | Meaning |
|---|---|---|
| `WATER_EMPTY_CM` | 23.05 | sensor face to tank floor |
| `WATER_FULL_CM` | 4.81 | airgap at the fill line |
| `WATER_LOW_CM` | 9.1 | **alert** ≈ 13.95 cm depth ≈ 5.5" ≈ 76% remaining |
| `PUMP_CUTOFF_CM` | 12.9 | **interlock** ≈ 10.15 cm depth ≈ **4.0"** ≈ 56% remaining |
| `TANK_CAPACITY_GALLONS` | 5 | **unverified** — upstream default, see Open items |

Usable depth is therefore ~18.2 cm (7.2").

### Where the 4-inch cutoff comes from

`PUMP_CUTOFF_CM=12.9` is **4 inches of water above the tank floor**, and that
number is about the pump, not the percentage it happens to correspond to:

- Gardyn units vary in intake design — some pumps draw from the bottom, others
  from the side. 4" clears both.
- It carries roughly 1.5" of margin for the tower sitting tilted, which is the
  realistic failure case: a level reading says there is water, and the intake is
  on the high side of a sloped surface with none over it.

Two consequences worth knowing before changing it:

**Only ~44% of the tank is usable.** Total usable depth is 18.24 cm (7.18"), so
a 4" floor leaves 3.18" of working range. That is a property of a shallow
reservoir and a pump that needs head, not something to tune away — expect to
refill at what looks like a half-full tank.

**The alert had to move with it.** 4" is 55.6% remaining, and the previous alert
sat at 55.1% — the cutoff would have tripped essentially before you were ever
warned. `WATER_LOW_CM=9.1` (5.5", 76%) puts the warning 1.5" of water ahead of
the stop, mirroring the tilt margin. If you raise the cutoff, raise the alert
too: `pump_cutoff()` rejects an inverted pair and silently falls back to the
alert value, which would leave you with no interlock beyond the warning.

### Why two thresholds

They answer different questions, and one number could not serve both. The alert
wants to be early enough to be a useful "top me up"; the interlock wants to be
late enough that it does not withhold water from a tank that still has plenty.
While `WATER_LOW_CM` was doing both jobs at 20.0 (17% remaining) the alert was
almost useless — by the time it fired there was very little left.

Between 55% and 30% the tower now warns and keeps watering. Below 30% it stops.

`PUMP_CUTOFF_CM` unset falls back to `WATER_LOW_CM`, which is exactly the old
single-threshold behaviour, so this is safe to leave unconfigured.

**Both are airgaps, so both are meaningless without the geometry above.**
Upstream's shipped `WATER_LOW_CM=11` is 60% on *its* nominal 5/20 tank — but 66%
on this one, which is why copying a threshold between towers does not work.
Under the old single-threshold scheme that was merely a noisy alert; now that
the value also gates the pump, an imported number would refuse to water a
two-thirds-full tank.

### How to recalibrate

Take **both** measurements at the same moment — the tank level changes, and a
mismatched pair silently corrupts the constant:

1. Read the current airgap *via the service* (see below).
2. Dip a ruler to the tank floor and read the actual water depth.
3. `WATER_EMPTY_CM = airgap + depth`.
4. To set `WATER_FULL_CM`, fill to the line and read the airgap again.

The current values were validated against two independent observations: at a
14.16 cm airgap a ruler read 3.5" and the model predicted 49% ("about half");
after filling, it predicted 100% ("nearly full"). Both matched.

### One calibration, every surface

`config.py` holds the calibration; **nothing else may keep a copy of it.** Every
derived figure — depth, percent, gallons — comes from `tank_readings()` in
`app/lib/water.py`, and both surfaces read that same result:

| surface | how it gets the numbers |
|---|---|
| Home Assistant | `mqtt.py` publishes `water/depth`, `water/percent`, `water/gallons` |
| Web UI | `GET /distance` returns `depth`, `percent`, `gallons` alongside the airgap |
| Either, for labels | `GET /system` returns `water_full_cm`, `water_empty_cm`, `tank_capacity_gallons`, `water_low_cm`, `pump_cutoff_cm` |

This is not tidiness. The web page used to derive its fill bar in JavaScript from
a hardcoded 5→20 cm tank, which is exactly `config.py`'s default — so it agreed
with Home Assistant on an uncalibrated tower and silently diverged on a
calibrated one. On this unit (4.81→23.05) the bar read **47%** where HA read
**56%** at the cutoff, and showed a flat **0%** with roughly 0.8 gal left.
`tests/test_tank_parity.py` pins the real thresholds against the measured
geometry and fails if any client starts deriving its own again.

The pump cap travels the same way: `MAX_PUMP_RUN_SECONDS` is read by the pump
routes, the schedule compiler, `bin/water.sh`, the HA duration control and the
web UI's inputs. Raising it in `.env` now raises all of them.

### Inches or centimetres

The tower **measures, stores, publishes and calibrates in metric, always.** Every
threshold in `.env` is centimetres, every MQTT payload is centimetres, every
constant in `config.py` is centimetres. That is not negotiable: a unit preference
that could move a threshold would be a safety bug, and `tests/test_display_units.py`
asserts that switching it moves none of them.

Units are a *display* choice, made in three independent places:

| surface | how to change it | scope |
|---|---|---|
| Web UI | Settings → Units → Metric / Imperial | that browser, remembered |
| Web UI default | `DISPLAY_UNITS=imperial` in `.env` | any browser that has not chosen |
| Home Assistant | HA's own unit system, or per-entity override | that HA instance |

This mirrors temperature exactly, which has worked this way all along: the tower
publishes Celsius and this HA instance displays Fahrenheit because HA converts.
Every distance entity is `device_class: distance`, so HA can show inches the same
way — the tower never sends them.

`DISPLAY_UNITS` accepts `imperial`, `us` or `customary`; anything else, including
a typo, falls back to metric rather than guessing. Imperial covers all three
kinds of reading — °F, inches and gallons — while metric shows °C, cm and litres.

Note the asymmetry in the tank: capacity is configured as
`TANK_CAPACITY_GALLONS` because that is how the manufacturer specifies it, so
metric mode converts gallons *to* litres for display while imperial passes them
through untouched.

### Two traps

**Do not run `app/sensors/distance/distance.py` standalone while `mqtt.service`
is running.** Two processes triggering one ultrasonic sensor cross-talk, and the
readings are wrong — this produced a bogus 14.16 cm reading that nearly became a
calibration constant. Read through the service instead:

```bash
mosquitto_pub -h "$MQTT_BROKER" -u "$MQTT_USERNAME" -P "$MQTT_PASSWORD" \
  -t gardyn_01/water/level/get -m 1
mosquitto_sub -h "$MQTT_BROKER" -u "$MQTT_USERNAME" -P "$MQTT_PASSWORD" \
  -t 'gardyn_01/water/#' -v
```

**Mind the sensor's minimum range** (~2 cm for HC-SR04-class parts). At the fill
line the airgap is only ~4.8 cm, so filling much higher degrades accuracy rather
than improving it. That is effectively the ceiling for this mounting.

## Testing

Run the suite in **WSL or any non-Pi Linux** — never on the Pi. `tests/_hwstub.py`
injects fake GPIO modules *only when the real libraries are absent*, so on the Pi
the tests import real `gpiozero` and seize live hardware.

```bash
python3 -m venv ~/gedev && ~/gedev/bin/pip install -r requirements-dev.txt
~/gedev/bin/python -m unittest discover -t . -s tests -p 'test_*.py'
~/gedev/bin/ruff check . && ~/gedev/bin/black --check .
```

CI gates on all three. Expect **at least 185 tests** on this branch and **126**
on a branch cut from upstream `main` — the difference is this fork's own
coverage, so a materially lower count is a hint that something did not merge,
not that tests vanished.

Deliberately a floor, not an exact figure. The number moves with almost every
PR, it was written down in four places, and correcting it by hand is how
DEPLOYMENT.md ended up claiming 177 while the README said 185. If you do want
the live count, take it from the runner rather than from `grep -c 'def test_'`
— that undercounts, because `tests/test_api.py` loads 17 tests from 16
definitions.

Do not put the venv in `/tmp`: WSL clears it between sessions.

**`app/__init__.py` imports every sensor blueprint at module level**, so even
importing `app.lib.grow` constructs the GPIO drivers. That is why the suite must
not run on the Pi — it is not a theoretical hazard, it seizes the live pins on a
tower with plants in it.

## Updating the Pi

`.env` is gitignored and survives branch switches. The systemd unit at
`/etc/systemd/system/mqtt.service` is a **copy**, not a symlink into the repo, so
switching branches does not change the running service definition — but it also
means edits to `services/etc/systemd/system/mqtt.service` in the repo have no
effect until copied into place.

```bash
cd ~/garden-of-eden
tar czf ~/gardyn-backup-$(date +%Y%m%d-%H%M%S).tar.gz --exclude=venv --exclude=.git .
git fetch origin && git reset --hard origin/feat/gardyn-tower-local
venv/bin/pip install -r requirements.txt
sudo systemctl restart mqtt.service garden-api.service
```

Two services run, not one: `mqtt.service` (Home Assistant) and
`garden-api.service` (REST API + built-in web UI on `:5000`, served by waitress).
Restart both — a stale API process keeps serving the previous UI.

Then confirm: `systemctl is-active mqtt.service garden-api.service`, and check the
journal for `Connected with result code Success` plus the absence of tracebacks.

Restarting is now safe for the actuators. It was not before: constructing a
driver used to write 0 to its pin, so every API restart switched the lights and
pump off mid-photoperiod, silently overriding cron. Verify with
`pigs gdc 18` (light) and `pigs gdc 24` (pump) — those read the true duty cycle
straight from pigpiod, in units of 1/10000, so `6500` is 65%. They are the only
trustworthy source when the dashboard and HA disagree.

## Verifying the unit

All of these are passive and safe:

```bash
mosquitto_pub ... -t gardyn_01/refresh/all -m PRESS   # force-poll everything
sudo i2cdetect -y 1                                # expect 0x40, 0x48, 0x5c
journalctl -u mqtt.service --no-pager -n 50
```

The **Refresh All** button re-reads every sensor and both cameras on demand and
*reports* the light and pump duty cycle without changing either. `Refresh Status`
reads `OK` or `PARTIAL: <what failed>`; `Last Refresh` carries the timestamp.

**`bin/api-test.sh` spins the pump motor** (`control_pump 30`). Do not run it
unless the pump is submerged.

## Home Assistant dashboard

**[`docs/homeassistant/pm-example.yaml`](homeassistant/pm-example.yaml) is the
dashboard these towers use.** It is a Sections layout (HA 2024.3+) grouped by
function — status first, then lighting, pump, one-time runs, environment,
cameras, diagnostics, and it is the only example covering all 37 discovered
entities. `lovelace-example.yaml` alongside it is the older plain card list and
a **subset** — 25 entities, with one-time pump runs, the derived water readings
(depth/gallons), the refresh controls and the diagnostic log all absent. Keep it
as a minimal starting point, not as an equivalent layout.

`automations/lights.yml` and `automations/pump.yml` are an **alternative** to
the built-in scheduler, not an addition to it. Cron already drives the light and
pump from the saved schedule; importing these puts HA on the same actuators with
no coordination between them. Use one or the other, never both.

Apply it via Settings → Dashboards → ⋮ → Edit → **Raw configuration editor**.
That editor *replaces* the whole dashboard, so if the target dashboard already
holds cards for anything else, paste the `views:` entry into your existing config
instead of overwriting the file wholesale.

### Entity ids are pinned, not derived

Every discovery payload sets `object_id` to its `unique_id`, so entity ids are
always `<domain>.<MQTT_IDENTIFIER>_<suffix>` — `sensor.gardyn_01_water_depth`.

Left to itself, HA derives the id from the *display name* under rules that vary
by release and by whether the device name collides with another device. This
tower demonstrated the failure: it ended up with both `sensor.gardyn_temperature`
and `sensor.gardyn_1_gardyn_water_depth`, depending on when each entity was first
seen. Pinning the id removes HA's naming rules from the equation, and means
retitling an entity in the UI can never move it out from under a dashboard.

The suffix is the `unique_id`, which is not always the display-name slug —
"Water Remaining" is `sensor.<id>_water_percent`, "PCB Temperature" is
`sensor.<id>_pcb_temp`, "Add Plant Food" is `binary_sensor.<id>_food`. Generate
the definitive list for any identifier with:

```bash
python - <<'PY'
import json, tests, mqtt          # tests/ loads the hardware stubs
class C:
    def __init__(self): self.pub = []
    def publish(self, topic, payload=None, **kw): self.pub.append((topic, payload))
c = C(); mqtt.send_discovery_messages(c)
for t, p in sorted(c.pub):
    d = json.loads(p); print(f'{t.split("/")[1]}.{d["object_id"]:40} {d["name"]}')
PY
```

### Migrating a tower that HA already discovered

`object_id` only applies when an entity is **first created**. HA matches on
`unique_id`, which is unchanged, so an already-registered entity keeps its old
id forever. To adopt the pinned ids on a tower HA has already seen:

1. Settings → Devices & Services → **MQTT** → the Gardyn device → ⋮ → **Delete**
2. It reappears within seconds — discovery configs are retained on the broker,
   so HA re-creates every entity, this time honouring `object_id`
3. Paste the dashboard

What this costs: recorder history and long-term statistics stay attached to the
old ids and are eventually purged, and any automation, script or dashboard
referencing an old id must be repointed. Entity customisations (renames, area
assignment, hidden/disabled flags) are lost with the registry entries. Nothing
on the Pi is affected — the schedule, grow state and crontab live outside HA.

### Running more than one tower

**Set one line per tower:**

```ini
MQTT_IDENTIFIER=gardyn_02
```

That is the whole configuration. `MQTT_BASETOPIC` defaults to `MQTT_IDENTIFIER`,
so the identifier namespaces the state/command topics, the HA device name, and
every entity id together.

This used to require setting both, and getting it wrong was silent and bad:
`mqtt.py` subscribes to `BASE_TOPIC + "/#"` and every state/command topic hangs
off `BASE_TOPIC`, so two units left on a shared base topic receive each other's
commands — one tower's light switch drives both. Since `.env-dist` also
hardcoded `MQTT_BASETOPIC=gardyn`, following the documented setup produced
exactly that collision. Both are fixed; override `MQTT_BASETOPIC` only to keep
an existing single-tower deployment on its old topics.

The discovery *topic* keeps its `gardyn` node_id segment
(`homeassistant/sensor/gardyn/<object_id>/config`) on purpose. HA identifies
entities by `unique_id` and `object_id`, both already per-tower, so the segment
is only a grouping label — and moving it would strand the old retained configs
at their old topics, leaving HA showing every entity twice.

The dashboard then ports by find/replacing `gardyn_01` with `gardyn_02`. Lovelace
is static YAML and does not auto-populate for a new device — one view per tower
is the straightforward approach. For dashboards that genuinely adapt on their
own, the HACS cards `auto-entities` (match `sensor.gardyn_*` and let cards fill
themselves) and `decluttering-card` (define the tower layout once, instantiate it
per identifier) are the usual answers; both are third-party.

## Scheduling

2.0.0's schedule entities compile into the **Pi's crontab**, shelling out to
`/usr/local/bin/light` and `/usr/local/bin/water`. Edit in Home Assistant; the Pi
executes it, so schedules survive HA downtime. There is no need to choose between
HA automations and Pi cron — this is both.

Set the times/brightness, then flip the enable switches; verify with
`crontab -l`. Pump duration is in **minutes**, and `MAX_PUMP_RUN_SECONDS` (300)
hard-caps any single run at 5 minutes regardless of how it was started.

Vacation mode overrides the schedule with lights 10:00–16:00 at 50% and a 3-minute
pump run at 12:00.

### Current schedule

| | |
|---|---|
| Lights | 05:00–21:00 daily at 65% (16 h photoperiod) |
| Pump | 01:00, 06:00, 09:00, 12:00, 15:00, 18:00, 21:00 — 3 min each, 21 min/day |
| Default on-duty | `DEFAULT_BRIGHTNESS=50`, `DEFAULT_PUMP_SPEED=50` |

That compiles to 63 marked crontab lines (14 light + 49 pump). The 01:00 run is
for overnight root-zone oxygen rather than moisture. `bin/water.sh` hardcodes
`SPEED=50`, so scheduled pump runs are at 50% regardless of `DEFAULT_PUMP_SPEED`;
the light schedule's 65% is a per-window value and is unrelated to
`DEFAULT_BRIGHTNESS`, which only seeds the manual slider.

**Timezone:** cron fires in the Pi's local zone, which is `America/New_York`
(a region zone, not a fixed offset), with NTP active. Schedules therefore track
daylight saving automatically and stay at 05:00/21:00 local year-round. Verify
with `timedatectl` after any OS reinstall.

### What HA's scalar schedule controls can and cannot do

The MQTT entities describe **one** window/run per day, so HA's "pump time" shows
`01:00` — the first of the seven. The rest are real and in cron, but invisible to
HA. All four setters therefore edit the saved schedule in place rather than
replacing it:

| Control | Effect |
|---|---|
| Brightness, Pump minutes | Applied to **every** existing window/run; count and times untouched |
| Lights on/off, Pump run at | Moves only the **first** window/run of each day; later ones survive |

Adding, removing or retiming the later windows/runs still needs
`apply_schedule()`, `POST /schedule`, or the web UI heatmap, then a
`mqtt.service` restart so HA re-reads it.

Both pairs used to collapse the whole week down to their single value, silently
discarding a multi-cycle schedule — the pump pair was fixed in `b05497e`, the
light pair after it. Four regression tests in `tests/test_mqtt_control.py` build
a two-window, two-run Monday and assert the second of each survives; keep them
green if you touch these setters.

## Grow cycle

State lives in `~/.garden_grow.json`, which **does not exist until the "Grow
Start" button is pressed** — until then HA shows in-memory defaults (stage
`germination`, food `Ok`). Press it when planting; it resets stage, start date and
acknowledgements, so press it per planting rather than casually.

Reminder thresholds (all configurable): food every 7 days, thinning at day 14,
root check at day 21, harvest at day 35. Grow Stage is a manual select — nothing
advances it automatically.

The `button` event entity reads `unknown` until the physical button is pressed
once; after that it reports `single` / `double` / `long` and works as an
automation trigger.

## How watering is guarded

Every path that can energize the pump now refuses below `PUMP_CUTOFF_CM`, not
just the Home Assistant buttons. That includes cron, which is how the tower
actually waters — it previously had no check at all.

The cron path cannot read the sensor itself: a second process triggering the
ultrasonic sensor cross-talks with `mqtt.service`'s polling and both come back
wrong (the same trap described above). So the service records its
median-filtered verdict to `~/.garden_state.json` on every check, and
`bin/water.sh` consults that:

```bash
# what the guard would decide right now, without running the pump
cd ~/garden-of-eden && PYTHONPATH=. venv/bin/python -m app.lib.water_guard
#   exit 0 = would water, exit 1 = would refuse

# water anyway, below the cutoff (the escape hatch from iot-root#83)
water 180 --override-low-water-level
```

A verdict older than `WATER_READING_MAX_AGE_SECONDS` (default 3 × the poll
interval, so 540s) is ignored — that is what stops a stopped service from
withholding water forever.

**Verified on this tower** across aware/naive timestamps × fresh/stale, a
garbage timestamp and a corrupt state file: it refuses only on a fresh reading
genuinely below the cutoff. Worth re-running after any change to
`app/lib/water.py` or `water_guard.py`, because the failure that matters is
silent — a guard that refuses when it should allow looks identical to a guard
working correctly until the plants dry out.

## Over-temperature alert

The PCT2075 that reports PCB temperature also drives a comparator output wired
to GPIO 25. **The chip trips that pin itself**, so the alert fires even if
`mqtt.service` is wedged -- which is the only reason it beats watching the
published reading. It surfaces as the HA `PCB Over Temperature` problem sensor,
**notify-only**: nothing cuts the lights or the pump.

Thresholds are `OVER_TEMP_HIGH` / `OVER_TEMP_HYSTERESIS`, currently **65 / 58 C**,
and they are measured rather than guessed. Three days of 2-minute samples, room
air 22.3-26.4 C:

| state | mean | max |
|---|---|---|
| lights off, pump off | 33.07 | 42.88 * |
| lights on, pump off | 41.64 | 44.75 |
| lights on, pump on | 43.39 | 43.62 |

\* thermal lag at the 21:00 lights-off transition, not steady state.

The lights are the heat source (+8.6 C between dark and lit); the pump adds ~2 C.
Worst rise above room air was 18.9 C, which projects to ~56 C in a 35 C room --
so 65 C cannot false-alarm in a hot week, and 58 C sits above that projection so
it cannot chatter either.

**This chip measures the carrier board, not the SoC.** Across 2046 paired
samples the CPU ran 8.0-13.9 C hotter (mean 10.9). So 65 C here is roughly
73-79 C at the processor, under Raspberry Pi's documented 85 C throttle point --
the alert fires *before* the Pi protects itself, which is what makes it worth
having. Treat it as "something is wrong in the enclosure", not as a CPU guard.

For reference, the tower is nowhere near trouble: SoC peaked at 57.8 C over
three days, `vcgencmd get_throttled` reads `0x0` (never, sticky since boot), and
the ARM clock stays at its full 1000 MHz.

### Do not use 36 / 34

Those were the shipped defaults, and they fall *between* the lights-off idle and
the lights-on normal -- the alert would have tripped every morning with the
lights and cleared every night, alarming 16 hours a day while still looking like
a working sensor. Nothing read them, so nothing broke.
`tests/test_over_temp.py` now fails if either value drifts back into the normal
operating envelope.

### Checking it

```bash
# Read-only apart from applying the configured thresholds. Safe alongside
# mqtt.service -- unlike the ultrasonic sensor, a second I2C reader is harmless.
cd ~/garden-of-eden && venv/bin/python -m app.sensors.pcb_temp.over_temp
```

Polarity is the chip's active-low default and stays that way. The bench script
this replaced inverted it, which is worse twice over: a half-configured process
leaves the pin reading the opposite of what any other reader assumes, and an
unpowered or disconnected chip reads as "fine" rather than failing loud.

## Known quirks

- **AM2320 intermittent I2C failures.** The sensor NAKs its first wake-up probe,
  so humidity occasionally goes unavailable in HA. Sensor behaviour, not wiring.
  Surfaced by the `Last Log` sensor, and `Refresh All` reports `PARTIAL` rather
  than aborting the whole run.
- **Camera freshness.** `IMAGE_INTERVAL_SECONDS` defaults to 3600, so dashboard
  images look stale between captures. Use Refresh All for an immediate frame.
- **The dry-run guard fails open, deliberately and everywhere.** A failed
  distance read, a stale verdict, a corrupt state file, an unparseable timestamp
  — all let the pump run. Withholding water indefinitely is a worse failure than
  the dry run this guards against, so every branch errs that way and says so in
  the log. `main()` in `app/lib/water_guard.py` carries a catch-all for the same
  reason: an unhandled exception was once the only path that could refuse, and
  it did (see below).

## Open items

- [x] ~~**`state_lib.save_state()` is never called from the MQTT handlers**~~ —
      fixed. The MQTT command paths now go through `commit_light_state()` /
      `commit_pump_state()`, which publish the real duty cycle, sync the toggle
      flag the physical button switches against, and persist for power-loss
      recovery. `publish_*_state()` stays read-only for `on_connect` and Refresh
      All. This also fixed a second symptom: because the MQTT handlers never
      updated `light_state`/`pump_state`, turning the light on from HA left the
      next button press turning it *on again* rather than off.
- [ ] **`TANK_CAPACITY_GALLONS=5` is unverified** — upstream's default, not
      measured for this tower. Only affects the Water Gallons entity; depth and
      percentage are unaffected.
- [ ] **The pump ON path has never been exercised under test.** Every other path
      is verified; this one energizes the motor and was left for a physical check.
      The *refusal* path is verified — see "How watering is guarded" — but that
      only proves the pump stays off, not that it runs correctly when allowed.
- [x] ~~**The cutoff depth has not been checked against the pump intake.**~~ —
      resolved 2026-09-01. Set to 4" of water, which clears both bottom- and
      side-draw intakes and carries ~1.5" of tilt margin. See "Where the 4-inch
      cutoff comes from". The number is now grounded in the hardware rather
      than in a percentage that felt about right.
- [ ] **GPIO header bridge scan not run.** A `pinctrl`-based script exists at
      `~/gpio-bridge-test.sh` to check the resoldered 40-pin header. Requires the
      harness unplugged with `mqtt.service` and `pigpiod` stopped. A multimeter
      continuity check with the Pi powered off remains the definitive test for
      GPIO-to-5V shorts, which software cannot detect.
- [x] ~~**No schedule is configured.**~~ — configured 2026-08-30, see
      "Current schedule" above.
- [ ] **The Pi still permits password SSH.** Key auth is set up; consider
      `PasswordAuthentication no` in `/etc/ssh/sshd_config`.
- [ ] **AI plant-health monitoring** — planned, not built. The service already
      publishes camera JPEGs to `gardyn_01/image/{upper,lower}_camera`, so an HA
      automation could pass a frame plus sensor readings to a vision model. Gate
      it on a time condition rather than every image publish, and decide where
      assessments are stored if growth trends are to be tracked over time.

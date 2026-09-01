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
| `WATER_LOW_CM` | 20.0 | dry-run cutoff ≈ 3 cm depth ≈ 17% remaining |
| `TANK_CAPACITY_GALLONS` | 5 | **unverified** — upstream default, see Open items |

Usable depth is therefore ~18.2 cm (7.2").

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

### Two traps

**Do not run `app/sensors/distance/distance.py` standalone while `mqtt.service`
is running.** Two processes triggering one ultrasonic sensor cross-talk, and the
readings are wrong — this produced a bogus 14.16 cm reading that nearly became a
calibration constant. Read through the service instead:

```bash
mosquitto_pub -h "$MQTT_BROKER" -u "$MQTT_USERNAME" -P "$MQTT_PASSWORD" \
  -t gardyn/water/level/get -m 1
mosquitto_sub -h "$MQTT_BROKER" -u "$MQTT_USERNAME" -P "$MQTT_PASSWORD" \
  -t 'gardyn/water/#' -v
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

CI gates on all three. Expect **177 tests** on this branch and **126** on a
branch cut from upstream `main` — the difference is this fork's own coverage, so
a lower count is a hint that something did not merge, not that tests vanished.

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
mosquitto_pub ... -t gardyn/refresh/all -m PRESS   # force-poll everything
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
cameras, diagnostics. `lovelace-example.yaml` alongside it is the older plain
card list, kept as a simpler starting point.

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

Pinned ids make the *entities* unambiguous, but two towers still need separate
**topic namespaces**, because `mqtt.py` subscribes to `BASE_TOPIC + "/#"` and
every state/command topic hangs off `BASE_TOPIC`. Two units both on the default
`MQTT_BASETOPIC=gardyn` would receive each other's commands — turning on one
tower's light would turn on both.

Per tower, set **both**, to the same value:

```ini
MQTT_IDENTIFIER=gardyn_02     # namespaces entity ids + discovery object_ids
MQTT_BASETOPIC=gardyn_02      # namespaces state/command topics and the HA device name
```

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

## Known quirks

- **AM2320 intermittent I2C failures.** The sensor NAKs its first wake-up probe,
  so humidity occasionally goes unavailable in HA. Sensor behaviour, not wiring.
  Surfaced by the `Last Log` sensor, and `Refresh All` reports `PARTIAL` rather
  than aborting the whole run.
- **Camera freshness.** `IMAGE_INTERVAL_SECONDS` defaults to 3600, so dashboard
  images look stale between captures. Use Refresh All for an immediate frame.
- **The dry-run guard fails open.** If the distance read itself fails, the pump is
  allowed to run — matching upstream's `is_water_low()` contract, so a dead sensor
  cannot brick the pump. It is logged when it happens.

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
      publishes camera JPEGs to `gardyn/image/{upper,lower}_camera`, so an HA
      automation could pass a frame plus sensor readings to a vision model. Gate
      it on a time condition rather than every image publish, and decide where
      assessments are stored if growth trends are to be tracked over time.

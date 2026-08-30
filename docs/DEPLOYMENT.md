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

The fix is on `feat/gardyn-tower-local` (PR #1 on this fork). The same work is
proposed upstream as `iot-root/garden-of-eden#95`; if that merges, this branch
can rebase onto it with no divergence.

**The Pi must not track `iot-root` `main` directly** — doing so silently
reintroduces all three defects.

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
python3 -m venv ~/gv && ~/gv/bin/pip install -r requirements-dev.txt
~/gv/bin/python -m unittest discover -t . -s tests -p 'test_*.py'   # 126 tests
~/gv/bin/ruff check . && ~/gv/bin/black --check .
```

CI gates on all three.

## Updating the Pi

`.env` is gitignored and survives branch switches. The systemd unit at
`/etc/systemd/system/mqtt.service` is a **copy**, not a symlink into the repo, so
switching branches does not change the running service definition — but it also
means edits to `services/etc/systemd/system/mqtt.service` in the repo have no
effect until copied into place.

```bash
cd ~/garden-of-eden
tar czf ~/gardyn-backup-$(date +%Y%m%d-%H%M%S).tar.gz --exclude=venv --exclude=.git .
git fetch fork && git reset --hard fork/feat/gardyn-tower-local
venv/bin/pip install -r requirements.txt
sudo systemctl restart mqtt.service
```

Then confirm: `systemctl is-active mqtt.service`, and check the journal for
`Connected with result code Success` plus the absence of tracebacks.

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
- [ ] **No schedule is configured.** Verified: no user crontab, no root crontab,
      no `/etc/cron.d` entries, no systemd timers. The tower currently runs no
      automation at all.
- [ ] **The Pi still permits password SSH.** Key auth is set up; consider
      `PasswordAuthentication no` in `/etc/ssh/sshd_config`.
- [ ] **AI plant-health monitoring** — planned, not built. The service already
      publishes camera JPEGs to `gardyn/image/{upper,lower}_camera`, so an HA
      automation could pass a frame plus sensor readings to a vision model. Gate
      it on a time condition rather than every image publish, and decide where
      assessments are stored if growth trends are to be tracked over time.

<img src="docs/_banner.svg" width="800px">

# Garden of Eden

Truly own that which is yours!

> ### This is the PushMotion fork
>
> Firmware for a **Gardyn** hydroponic tower on a Raspberry Pi. This fork tracks
> upstream [`iot-root/garden-of-eden`](https://github.com/iot-root/garden-of-eden)
> 2.0.0 and adds pump-control fixes, actuator-state correctness, non-destructive
> scheduling, deterministic Home Assistant entity ids, and multi-tower support.
> **[What this fork adds](#what-this-fork-adds)** documents every change.
>
> - **Build branch:** `feat/gardyn-tower-local` — the full stack the towers run.
>   `main` is kept a pure mirror of `upstream/main`.
> - **Running a tower?** Read [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) first. It
>   covers the water calibration, the two ways to take a wrong sensor reading,
>   where tests may safely run (**never on the Pi**), and the open items.
> - **Upstream `main` is not safe to run** on a tower: it reintroduces three pump
>   defects described [below](#1-pump-control-was-unusable-from-home-assistant).

If you are interested in collaborating please review the [CONTRIBUTORS](CONTRIBUTORS.md) for commit styling guides.

## Video Tutorial for Gardyn of Eden and Homeassistant

Thanks to "Yong" for very well edited video tutorial.

[Video Tutorial](https://www.youtube.com/watch?v=gH5yu8JwS8Y)

## Project Status & Milestones

Work in progress. We should be picking up some steam here to give the DYI community the features you deserve.

[Milestones](https://github.com/iot-root/garden-of-eden/milestones)

![image](https://github.com/user-attachments/assets/403248f5-b7d4-4cb1-921a-0458f515f387)

## What's new (v2 overhaul)

A broad overhaul closing out the open milestones:

- **Built-in web UI** — a self-contained control page served by the firmware at
  `http://gardyn.local:5000/` (controls, live sensors + pump power, cameras, grow
  cycle, full schedule editor, run-pump-for-N-seconds). Auto-starts as a service;
  no separate app required. See [`docs/access.md`](docs/access.md).
- **Headless-friendly** — `setup.sh` keeps **SSH on** and sets up mDNS so the unit
  is reachable at `gardyn.local` right after flashing.

> Installing on a Pi? Follow [`docs/INSTALL.md`](docs/INSTALL.md) — a step-by-step,
> brick-safe handoff (dry-run, backups, uninstall). On this fork the code is on
> **`feat/gardyn-tower-local`**, not `main`.
- **Self-sufficient REST API** — camera, scheduling, grow-cycle, and system/model
  endpoints (see below), with optional API-key auth.
- **Home Assistant** — the physical button is now an HA `event` entity
  (single/double/long); example dashboard in
  [`docs/homeassistant/`](docs/homeassistant/lovelace-example.yaml).
- **Grow-cycle reminders** — thinning, root-check, harvest, and nutrient
  notifications via MQTT/REST.
- **Resilience** — shared pigpio connection, sensor auto-reprobe, power-loss
  state recovery, graceful shutdown.
- **Ops** — Docker/compose, CI (lint + tests), automated changelog, hardened
  setup with OS checks and camera udev rules.
- **Config** — all pins/addresses/thresholds live in `config.py` / `.env`.

See [`docs/design.md`](docs/design.md) for architecture, and
[`docs/maintenance.md`](docs/maintenance.md) for upkeep.

## What this fork adds

30 commits on top of upstream 2.0.0, across 35 files. Grouped by what they fix
or add, with the reasoning where the behaviour is non-obvious.

### 1. Pump control was unusable from Home Assistant

Three defects that compound each other. All three are still present upstream, so
a tower must not track `iot-root` `main`.

| # | Defect | Effect |
|---|--------|--------|
| 1 | `pump/speed/set` never published `pump/state` | HA showed the pump **OFF while it ran**; once HA believed it was off it stopped sending brightness commands entirely, so the slider went dead until a service restart |
| 2 | `pump/command` ON hit the water-low guard and returned **without publishing state** | the power button looked completely inert — no feedback, no error |
| 3 | `pump/speed/set` had **no water guard at all** | the speed slider bypassed the dry-run protection the power button enforced |

The interaction is what makes this severe: (1) and (2) make the button look
broken, so the slider is exactly what a user reaches for — and the slider is the
one path that could run the pump dry. Proposed upstream as
[`iot-root/garden-of-eden#95`](https://github.com/iot-root/garden-of-eden/pull/95).

### 2. Actuator state now reflects the hardware

The tower is driven from four places — cron, MQTT, REST, and the physical button
— and nothing reconciled them, so Home Assistant routinely showed the opposite of
reality.

- **Constructing a driver no longer resets the pin.** `PWMLED(pin)` defaults to
  `initial_value=0`, and `app/__init__.py` imports every blueprint at module
  level — so merely *starting the API* wrote 0 and killed the lights mid
  photoperiod, invisibly, until the next cron on-time. Fixed by seeding from
  `hardware.current_duty_fraction()`. (`initial_value=None` is not usable —
  gpiozero range-checks it and raises `TypeError`.)
- **Changes made outside the service are detected.** `reconcile_actuator_state()`
  polls both duty cycles every `ACTUATOR_POLL_SECONDS` (default 15) and
  republishes on change, so a cron-driven light change reaches HA. gpiozero does
  read PWM duty back through pigpiod — verified: an external `light 30` reads as
  30.0 in a process that constructed its `Light` at 65.
- **State is persisted on every change**, over MQTT as well as REST, so
  power-loss recovery restores a true value and the button's next press does not
  fight whatever HA last did.
- **`apply_scheduled_state()` replays the schedule's expected light state on
  every MQTT connect**, so a power cut self-heals instead of leaving the tower
  dark until the next on-time. Pump runs are deliberately *never* auto-resumed: a
  crash-looping service would re-trigger watering on every connect, and a missed
  run self-heals at the next scheduled one.
- `DEFAULT_BRIGHTNESS` / `DEFAULT_PUMP_SPEED` are honoured from config.

### 3. Scheduling: scalar edits no longer destroy a schedule

The MQTT entities describe **one** window/run per day, but the schedule can hold
several. All four setters used to write their single value across all seven days,
silently discarding the rest — a tower watering seven times a day dropped to once,
21 min/day of watering to 3, with no warning. Both pairs are now surgical:

| Control | Behaviour |
|---|---|
| Brightness, Pump minutes | applied to **every** existing window/run; count and times untouched |
| Lights on/off, Pump run at | moves only the **first** window/run of each day; later ones survive |

A scalar cannot express a multi-window day, so moving one window is predictable
and reversible where collapsing the day to it is not. Four regression tests build
a two-window, two-run Monday and assert the second of each survives.

**One-time pump runs** were added for the case those controls were being misused
for. `One-Time Pump Run` arms a single dated cron entry under its own marker,
written directly rather than through `apply_schedule()`, so arming one cannot
rewrite the recurring schedule — and the marker contains `CRON_MARKER` as a
substring, so both `apply_schedule()` and `installed_cron_lines()` exclude it
explicitly. Without that, applying a schedule would silently cancel a pending
one-off. Cron has no concept of "once", so each line carries its ISO timestamp
and the nightly refresh prunes spent entries.

**New schedule endpoints:** `/schedule/state` (derived state), `/schedule/next`,
`/schedule/validate` (dry run), `/schedule/cron` (compiled lines), and
`GET`/`POST`/`DELETE` `/schedule/pump/once`.

`MAX_PUMP_RUN_SECONDS` (300) hard-caps any single run on every path — MQTT, REST,
CLI and cron — and is now surfaced read-only as `Max Pump Run Time`. The cap is
armed by the pump *being on* rather than by whoever switched it on, so a run
started by cron, or by a `water.sh` killed before its exit trap, is covered too.

### 3b. The dry-run guard covers every path, not just the buttons

`WATER_LOW_CM` was read only by the MQTT command handler. `bin/water.sh` — what
cron runs, and so how the tower actually waters — had no water check at all, and
neither did `POST /pump/on` or `/pump/run`. Every unattended run was unguarded.

The CLI can't take its own reading (two processes on one ultrasonic sensor
cross-talk and both come back wrong), so `mqtt.py` records its median-filtered
verdict and `bin/water.sh` consults that, ignoring it once stale.

| Setting | Role | Default |
|---|---|---|
| `WATER_LOW_CM` | **alert** — early enough to be a useful "top me up" | `11` (60% on the nominal tank) |
| `PUMP_CUTOFF_CM` | **interlock** — where watering actually stops | `15.5` (30%); unset falls back to `WATER_LOW_CM` |

One value could not be both: tuned as an alert it refuses to water a
two-thirds-full tank, tuned as a cutoff the alert only fires when it is nearly
too late. Both are *airgaps*, so both are meaningless without `WATER_FULL_CM` /
`WATER_EMPTY_CM` — a threshold copied between towers means a different
percentage on each.

**Set the cutoff from the pump, not from a percentage.** Intake designs differ
between units — some draw from the bottom, some from the side — so choose a
water depth that clears yours, plus margin for the tower standing tilted, which
is the case that leaves the intake above water while a level reading still looks
fine. On a Gardyn Home, 4" of water with ~1.5" of tilt margin works out as
`PUMP_CUTOFF_CM = WATER_EMPTY_CM - 10.16`. Check what that leaves you: these
reservoirs are shallow, and a 4" floor can put more than half the tank out of
reach. If you raise the cutoff, raise the alert with it — `pump_cutoff()`
rejects an inverted pair and falls back to the alert, leaving no interlock
beyond the warning.

**The guard fails open on purpose.** A failed read, a stale verdict, a corrupt
state file — all let the run proceed, because withholding water indefinitely is
a worse failure than the dry run being guarded against.
`water --override-low-water-level` forces a run regardless.

### 4. Home Assistant: deterministic entities, multi-tower safe

- **Every discovery payload pins `object_id`** to its `unique_id`, so entity ids
  are always `<domain>.<MQTT_IDENTIFIER>_<suffix>`. Left to itself HA derives the
  id from the *display name*, under rules that vary by release and by whether the
  device name collides with another — which left one tower straddling two schemes
  (`sensor.gardyn_temperature` alongside `sensor.gardyn_1_gardyn_water_depth`).
  Pinning also means renaming an entity in the UI can never move it out from
  under a dashboard.
- **`MQTT_BASETOPIC` defaults to `MQTT_IDENTIFIER`**, so each tower gets its own
  topic namespace. This is not cosmetic: `mqtt.py` subscribes to
  `BASE_TOPIC + "/#"`, so two units sharing a base topic receive each other's
  commands and one tower's light switch drives both. A second tower is now one
  line: `MQTT_IDENTIFIER=gardyn_02`.
- **New entities:** Water Depth, Water Remaining (%), Water Gallons, Next Pump
  Run, Pending One-Time Pump Run, Max Pump Run Time, Manual Pump Run Time,
  One-Time Pump Run, Refresh All / Refresh Status / Last Refresh, Last Log.
- **Refresh All** re-reads every sensor and both cameras on demand and *reports*
  the light and pump duty cycle without changing either; `Refresh Status` reads
  `OK` or `PARTIAL: <what failed>`.
- **[`bin/ha-align-entity-ids.py`](bin/ha-align-entity-ids.py)** renames existing
  registry entries onto the pinned scheme. `object_id` only applies when HA first
  creates an entry — it matches on `unique_id` and reuses the old entity_id
  forever — so a tower discovered before this change needs a rename. Renaming
  beats deleting the device: the registry entry survives, so recorder history and
  long-term statistics follow the entity. Registry writes are websocket-only, so
  this cannot be done with `curl`. Dry run by default.
- **Dashboards:** [`pm-example.yaml`](docs/homeassistant/pm-example.yaml) is a
  Sections layout grouped by function (status first, then lighting, pump,
  one-time runs, environment, cameras, diagnostics) and covers all 37 discovered
  entities. [`lovelace-example.yaml`](docs/homeassistant/lovelace-example.yaml)
  is a plain card list and a **25-entity subset** — no one-time pump runs, no
  derived water readings, no refresh controls. Its header lists the omissions.
- **Automations:** [`automations/`](automations/) holds optional HA time
  triggers for the light and pump. They are an *alternative* to the built-in
  scheduler, not a companion — running both leaves cron and HA fighting over the
  same actuator.

### 5. Web UI

- Camera stills and timelapses are fetched **with the API key**, and a 401
  prompts for a key instead of failing silently.
- Brightness and speed sliders are **seeded from real hardware state** rather
  than defaults.
- Cameras are captured **one at a time** — concurrent capture was unreliable on
  the Pi Zero.
- Each frame is **stamped with its capture time**; timelapse frame rate scales to
  the archive length (`TIMELAPSE_TARGET_SECONDS`) so short archives play at a
  watchable speed instead of flashing past, and archive status is reported.
- The plant grid is laid out **as the physical towers**, so the screen matches
  what you are looking at.
- A **Fahrenheit or Celsius preference** is remembered.
- Pump power stats are rounded to two decimals.

### 6. Grow cycle

Recurring reminders are **anchored to the last acknowledgement** rather than to
the cycle start, so acknowledging one does not immediately re-fire it.
`NUTRIENT_REMINDER_DAYS` and `RESERVOIR_CHANGE_DAYS` are configurable.

### 7. Testing and ops

- **185+ tests**, up from 126 on upstream `main`, including offline HA discovery validation,
  MQTT control-path tests, and schedule regression tests.
- `docs/DEPLOYMENT.md` documents the branch model, water calibration and how to
  redo it, the two ways to take a wrong sensor reading, and the open items.

> **Tests must never run on the Pi.** `tests/_hwstub.py` injects fakes only when
> the real GPIO libs are *absent*, and `app/__init__.py` imports every sensor
> blueprint at module level — so on a tower, importing even `app.lib.grow`
> instantiates real GPIO drivers on a unit full of plants.

### REST API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/light/on` `/light/off` | toggle grow light |
| POST/GET | `/light/brightness` | set/get brightness (0–100) |
| POST | `/pump/on` `/pump/off` | toggle pump |
| POST/GET | `/pump/speed` | set/get pump speed |
| GET | `/pump/stats` | INA219 power data |
| GET | `/distance` `/distance/measure` | water-level distance (cm) |
| GET | `/temperature` `/humidity` `/pcb-temp` | environment sensors |
| GET | `/camera/upper` `/camera/lower` | capture a still (JPEG) |
| GET/POST | `/camera/timelapse/<cam>` | fetch / build a timelapse |
| GET | `/camera/timelapse/<cam>/status` | frame count, archive status |
| GET/POST | `/schedule` | read / replace the lights+pump schedule |
| GET | `/schedule/state` | derived state — what the schedule says should be true now |
| GET | `/schedule/next` | next light transition and next pump run |
| POST | `/schedule/validate` | **dry run** — validate and compile without applying |
| GET | `/schedule/cron` | the compiled crontab lines |
| GET/POST/DELETE | `/schedule/pump/once` | list / arm / clear one-time pump runs |
| GET | `/grow` · POST `/grow/start` `/grow/stage` `/grow/acknowledge` | grow-cycle |
| GET/POST | `/pods` · POST `/pods/<id>` | pod contents (what is planted where) |
| GET | `/system` | identity, version, detected model/profile |

Optional API-key auth applies to every route when `GARDEN_API_KEY` is set
(`X-API-Key` header); localhost bypasses it, and `/` plus `/static` stay open so
the web UI can load and prompt for a key.

### Run with Docker

```bash
cp .env-dist .env          # edit MQTT + identity
sudo pigpiod -p 8888       # pigpiod on the Pi host
docker compose up -d       # api (:5000) + mqtt + optional broker
```

See [`docs/integrations/`](docs/integrations/README.md) for Telegraf, ThingsBoard,
and Alexa.

### Test it without a Pi

A simulator runs the whole stack with fake hardware so you can try the web UI,
REST API, and Home Assistant discovery on your laptop:

```bash
python -m venv .venv-dev && .venv-dev/bin/pip install -r requirements-dev.txt
.venv-dev/bin/python -m simulator.serve     # http://localhost:5000/
.venv-dev/bin/python -m simulator.mqtt_sim  # MQTT for Home Assistant (needs a broker)
```

See [`docs/simulator.md`](docs/simulator.md).

## Table of Contents

- [Garden of Eden](#garden-of-eden)
  - [Project Status \& Milestones](#project-status--milestones)
  - [What this fork adds](#what-this-fork-adds)
    - [1. Pump control was unusable from Home Assistant](#1-pump-control-was-unusable-from-home-assistant)
    - [2. Actuator state now reflects the hardware](#2-actuator-state-now-reflects-the-hardware)
    - [3. Scheduling: scalar edits no longer destroy a schedule](#3-scheduling-scalar-edits-no-longer-destroy-a-schedule)
    - [3b. The dry-run guard covers every path, not just the buttons](#3b-the-dry-run-guard-covers-every-path-not-just-the-buttons)
    - [4. Home Assistant: deterministic entities, multi-tower safe](#4-home-assistant-deterministic-entities-multi-tower-safe)
    - [5. Web UI](#5-web-ui)
    - [6. Grow cycle](#6-grow-cycle)
    - [7. Testing and ops](#7-testing-and-ops)
  - [Table of Contents](#table-of-contents)
  - [Getting Started](#getting-started)
    - [Prerequisites](#prerequisites)
  - [Usage](#usage)
    - [MQTT with HomeAssistant](#mqtt-with-homeassistant)
    - [Testing](#testing)
    - [Controlling Individual Sensors](#controlling-individual-sensors)
    - [REST API](#rest-api)
      - [Endpoints](#endpoints)
      - [Postman](#postman)
    - [Cron Job](#cron-job)
  - [Hardware Overview](#hardware-overview)
    - [Air Temp \& Humidity Sensor](#air-temp--humidity-sensor)
    - [Pump Power Monitor](#pump-power-monitor)
    - [PCB Temp Sensor](#pcb-temp-sensor)
    - [Lights](#lights)
      - [Method](#method)
      - [Pins](#pins)
    - [Pump](#pump)
      - [Method](#method-1)
      - [Pins](#pins-1)
    - [Camera](#camera)
      - [Method](#method-2)
      - [Devices](#devices)
    - [Water Level Sensor](#water-level-sensor)
      - [Pins](#pins-2)
      - [Method](#method-3)
      - [References](#references)
    - [Momentary Button](#momentary-button)
    - [Electrical Diagrams](#electrical-diagrams)
      - [Sensors](#sensors)
      - [Power and Header](#power-and-header)
    - [Recommendations](#recommendations)
      - [Upgrading the Pi Zero 2](#upgrading-the-pi-zero-2)
  - [Design Decisions](#design-decisions)
    - [Python Version 3.6 \>=](#python-version-36-)
    - [Delays in Reading Temp/Humidity data](#delays-in-reading-temphumidity-data)
    - [GPIO](#gpio)
  - [Folder Structure](#folder-structure)

## Getting Started

### Prerequisites

Start with a clean install of Linux. Use the [RaspberryPi Imager](https://www.raspberrypi.com/software/). Ensure ssh and wifi is setup. Once the image is written, pop the SDcard into the pi and ssh into it.

```bash
# clone repo
git clone git@github.com:iot-root/garden-of-eden.git
cd garden-of-eden 
```

Update the `.env` with mqtt broker info

```
cp .env-dist .env
nano .env
```

Install dependencies, and run services pigpiod, mqtt.service

```
./bin/setup.sh`
```

Ensure the pigpiod daemon is running

```
sudo systemctl status pigpiod
sudo systemctl status mqtt.service
```

## Usage

## Quick Toggle Guide

> Ensure your press is quick and within the time frame for the action to register correctly. The press time window can be modified directly in the `mqtt.py` file.

- **One Press** (within 1 second): 
  - **Action**: Toggles the **Lights** on or off. 
  - **Description**: A single, swift press will illuminate or darken your space with ease.

- **Two Presses** (within 1 second): 
  - **Action**: Toggles the **Pump** on or off.
  - **Description**: Need to water the garden or fill up the pool? Double tap for action!


### MQTT with HomeAssistant

Every entity is created automatically by MQTT discovery — nothing is added by
hand. 37 entities appear under one device.

**Entity ids are deterministic:** `<domain>.<MQTT_IDENTIFIER>_<suffix>`, because
each discovery payload pins `object_id` to its `unique_id`. With
`MQTT_IDENTIFIER=gardyn_01` you get `sensor.gardyn_01_water_depth`, and a
dashboard ports to another tower by find/replacing the identifier.

The suffix is the `unique_id`, which is **not always the display-name slug** —
deliberately, since the display name is cosmetic and the unique_id is the
contract:

| Display name | Entity id |
|---|---|
| Water Remaining | `sensor.<id>_water_percent` |
| PCB Temperature | `sensor.<id>_pcb_temp` |
| Add Plant Food | `binary_sensor.<id>_food` |
| Last Refresh | `sensor.<id>_refresh_last` |
| Last Log | `sensor.<id>_log` |
| Lights Schedule | `switch.<id>_sched_lights` |

Generate the definitive list for any identifier:

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

**Running more than one tower:** set `MQTT_IDENTIFIER` per unit and leave
`MQTT_BASETOPIC` unset. Two towers sharing a base topic would receive each
other's commands. See [Running more than one tower](docs/DEPLOYMENT.md).

Example dashboards live in [`docs/homeassistant/`](docs/homeassistant/).

You need a mqtt broker either on the gardyn pi or homeassistant.

To install on the pi run

```
sudo apt-get install mosquitto mosquitto-clients
```

Add mqtt-broker username and password:

`sudo mosquitto_passwd -c /etc/mosquitto/passwd <USERNAME>`

> Note: make sure to update the .env file which is used by `config.py` for `mqtt.py`

Run `sudo nano /etc/mosquitto/mosquitto.conf` and change the following lines to match:

```
allow_anonymous false
password_file /etc/mosquitto/passwd
listener 1883
```


Here are some additional options that you could set in `/etc/mosquitto/mosquitto.conf`:

```
pid_file /run/mosquitto/mosquitto.pid

persistence true
persistence_location /var/lib/mosquitto/

log_dest file /var/log/mosquitto/mosquitto.log

listener 1883 0.0.0.0

allow_anonymous false
password_file /etc/mosquitto/passwd

include_dir /etc/mosquitto/conf.d
```


Restart the service

```
sudo systemctl restart mosquitto
```

you just need to edit the `.env` with the mosquitto username and password created above in /etc/mosquitto/passwd.


Check the configuration works:

`sudo journalctl -xeu mosquitto.service`


If you havent already, run `./bin/setup.sh`, this will install all OS dependencies, install the python libs, and run services pigpiod, mqtt.service

Ensure the pigpiod, mqtt, and broker daemon is running

```
sudo systemctl status pigpiod
sudo systemctl status mqtt.service
sudo systemctl status mosquitto
```

Go to your homeassistant instance:
If your broker is on the gardyn pi, make sure to install the service mqtt, go to settings->devices&services->mqtt and add your gardyn pi host, port, username and password.
The device should then appear in your homeassistant discovery settings.

To test locally on gardyn pi. The topic prefix below is `MQTT_BASETOPIC`, which
defaults to `MQTT_IDENTIFIER` (`gardyn_01`) — substitute your own if you changed
it, and note that a second tower gets its own prefix so the two cannot receive
each other's commands:

Light:

```
mosquitto_pub -t "gardyn_01/light/command" -m "ON" -u gardyn -P "somepassword"
mosquitto_pub -t "gardyn_01/light/command" -m "OFF" -u gardyn -P "somepassword"
```

Pump:

```
mosquitto_pub -t "gardyn_01/pump/command" -m "ON" -u gardyn -P "somepassword"
mosquitto_pub -t "gardyn_01/pump/command" -m "OFF" -u gardyn -P "somepassword"
```

Sensors:

Open two terminals on the gardyn pi, in one run:

`mosquitto_sub -t "gardyn_01/water/level" -u gardyn -P "somepassword"`

In the second gardyn pi terminal, run:

`mosquitto_pub -t "gardyn_01/water/level/get" -m "" -r -u gardyn -P "somepassword"`

### Testing

Activate python venv `source venv/bin/activate`

Start the Flask REST API `python run.py`

Test options:

```bash
# unit tests — the CI gate. Run this OFF the Pi.
python -m unittest discover -t . -s tests -p 'test_*.py'

# lint + format, also gated in CI
ruff check . && black --check .

# individual test module
python -m unittest tests.test_distance

# REST endpoints against a running run.py
./bin/api-test.sh
```

Two things that will bite you otherwise:

- **Use the `discover -t . -s tests` form.** The hardware-stub bootstrap lives in
  the `tests` package `__init__`, so a plain `python -m unittest` imports `app`
  directly and fails off-Pi.
- **Never run the suite on a Pi.** `tests/_hwstub.py` injects fakes only when the
  real GPIO libs are *absent*. On a tower they are present, the stubs disengage,
  and importing the app instantiates real GPIO drivers on a unit full of plants.
  Use a laptop, WSL, or the [simulator](docs/simulator.md).
- **`bin/api-test.sh` spins the pump motor** (`control_pump 30`). Do not run it
  unless the pump is submerged.

### Controlling Individual Sensors

Activate python venv `source venv/bin/activate`

Examples:

```bash
python app/sensors/distance/distance.py
python app/sensors/humidity/humidity.py
python app/sensors/light/light.py [--on] [--off] [--brightness INT%]
python app/sensors/pcb_temp/pcb_temp.py
python app/sensors/pump/pump.py [--on] [--off] [--speed INT%] [--factory-host STR%] [--factory-port INT%]
python app/sensors/temperature/temperature.py
```

### REST API

Activate python venv `source venv/bin/activate`

Then Run `python run.py`, this will print the ip to send requests.

> **Note:** if run.py errors with: AttributeError: module 'dotenv' has no attribute 'find_dotenv'

```
pip uninstall python-dotenv
python run.py
```

#### Endpoints

```
[GET] http://<pi-ip>:5000/distance

[GET] http://<pi-ip>:5000/humidity

[POST] http://<pi-ip>:5000/light/on
[POST] http://<pi-ip>:5000/light/off
[POST] http://<pi-ip>:5000/light/brightness body:{"value": 50 }
[GET] http://<pi-ip>:5000/light/brightness

[GET] http://<pi-ip>:5000/temperature

[GET] http://<pi-ip>:5000/pcb-temp

[POST] http://<pi-ip>:5000/pump/on
[POST] http://<pi-ip>:5000/pump/off
[POST] http://<pi-ip>:5000/pump/speed body:{"value": 50 }
[GET] http://<pi-ip>:5000/pump/speed
[GET] http://<pi-ip>:5000/pump/stats
```

#### Postman

Export this [Postman collection](https://www.postman.com/orange-shadow-8689/workspace/garden-of-eden/collection/8244324-e9d8f79e-d3f2-423e-b0d1-a4ca5b1b08ca?action=share&creator=8244324&active-environment=8244324-861384b4-b4e3-48a3-8da1-181705bd2d8c), add to your private workspace, add the `pi-ip` env variable and you should be good to go.

### Cron Job

Run `crontab -e`, select your preferred editor and then add the following job. Edit as needed.

> Note: update your paths for the following...

```text
# †urn on lights at 6am, 9am, 5pm, and turn off at 8pm
0 6 * * * /home/gardyn/projects/garden-of-eden/venv/bin/python /home/gardyn/projects/garden-of-eden/app/sensors/light/light.py --on --brightness 50
0 9 * * * /home/gardyn/projects/garden-of-eden/venv/bin/python /home/gardyn/projects/garden-of-eden/app/sensors/light/light.py --on --brightness 70
0 17 * * * /home/gardyn/projects/garden-of-eden/venv/bin/python /home/gardyn/projects/garden-of-eden/app/sensors/light/light.py --on --brightness 50
0 20 * * * /home/gardyn/projects/garden-of-eden/venv/bin/python /home/gardyn/projects/garden-of-eden/app/sensors/light/light.py --off

# Pump run at 8am for 5 minutes
0 8 * * * /home/gardyn/projects/garden-of-eden/venv/bin/python /home/gardyn/projects/garden-of-eden/app/sensors/pump/pump.py --on --speed 100
5 8 * * * /home/gardyn/projects/garden-of-eden/venv/bin/python /home/gardyn/projects/garden-of-eden/app/sensors/pump/pump.py --off

# Pump run at 4pm 5 minutes
0 16 * * * /home/gardyn/projects/garden-of-eden/venv/bin/python /home/gardyn/projects/garden-of-eden/app/sensors/pump/pump.py --on --speed 100
5 16 * * * /home/gardyn/projects/garden-of-eden/venv/bin/python /home/gardyn/projects/garden-of-eden/app/sensors/pump/pump.py --off

# Pump run at 9pm for 5 minutes
0 21 * * * /home/gardyn/projects/garden-of-eden/venv/bin/python /home/gardyn/projects/garden-of-eden/app/sensors/pump/pump.py --on --speed 100
5 21 * * * /home/gardyn/projects/garden-of-eden/venv/bin/python /home/gardyn/projects/garden-of-eden/app/sensors/pump/pump.py --off

# Collect sensor data every 30 mins
*/30 * * * * /home/gardyn/projects/garden-of-eden/bin/get-sensor-data.sh
```

## Hardware Overview

Depending on the system you have, here is a breakdown of the hardware.

Notes:

- GPIO num is different than pin number. See (<https://pinout.xyz/>)

### Air Temp & Humidity Sensor

- temp/humidity sensor AM2320 at address of `0x38`

### Pump Power Monitor

- motor power usage sensor INA219 at address of `0x40`

### PCB Temp Sensor

- pcb temp sensor PCT2075 at address `pf 0x48`

When you run `sudo i2cdetect -y 1`, you should see something like:

```
     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f
00:          -- -- -- -- -- -- -- -- -- -- -- -- --
10: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
20: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
30: -- -- -- -- -- -- -- -- 38 -- -- -- -- -- -- --
40: 40 -- -- -- -- -- -- -- 48 -- -- -- -- -- -- --
50: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
60: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
70: -- -- -- -- -- -- -- --
```

### Lights

LED full spectrum lights.

#### Method

- Lights are driven by PWM duty and a frequency of 8 kHz.

#### Pins

- [GPIO-18 | PIN-12](https://pinout.xyz/pinout/pin12_gpio18/)

### Pump

#### Method

- The pump is driven by PWM with max duty of 30% and frequency of 50 Hz
- There is a current sensor to measure pump draw and a overtemp sensor to determine if board monitor PCB temp.

#### Pins

- [GPIO-24 | PIN-18](https://pinout.xyz/pinout/pin18_gpio24/)

Notes:

- Pump duty cycle is limited, likely full on is too much current draw for the system.

### Camera

Two USB cameras.

#### Method

- image capture with fswebcam

#### Devices

- /dev/video0
- /dev/video1

### Water Level Sensor

Uses the ultrasonic distance sensor DYP-A01-V2.0.

#### Pins

- [GPIO-19 | PIN-35](https://pinout.xyz/pinout/pin35_gpio19/): water level in (trigger)
- [GPIO-26 | PIN-37](https://pinout.xyz/pinout/pin37_gpio26/): water level out (echo)

#### Method

- Uses time between the echo and response to deterine the distances.

#### References

- <https://www.google.com/search?q=DYP-A01-V2.0>
- <https://www.dypcn.com/uploads/A02-Datasheet.pdf>

### Momentary Button

`<section incomplete>`

### Electrical Diagrams

Incase you need to troubleshoot any problems with your system.

#### Sensors

<img src="docs/pcb1.png" width="800px">

#### Power and Header

<img src="docs/pcb2.png" width="800px">

### Recommendations

#### Upgrading the Pi Zero 2

For better performance, the Pi Zero can be replaced with a Pi Zero 2. This will enable the use of VS Code Remote Server to edit files and debug the python code remotely. The VS Code remote server uses OpenSSH and the minimum architecture is ARMv7.

> Buy one **without** a header, you will need to solder one on in the opposite direction.

## Design Decisions

### Python Version 3.6 >=

Minimum python version of 3.6 to support `printf()`

### Delays in Reading Temp/Humidity data

Reading sensor values  with inherently long delays and responding to the REST API. To minimize the delay in subsequent readings the value is cached and given if another read occurs within two seconds.

### GPIO

Using `gpiozero` to leverage `pigpio` daemon which is hardware driven and more efficient.This ensures better accuracy of the distance sensor and is less cpu intensive when using PWMs.

## Folder Structure

```text
<garden-of-eden>
├── config.py               all pins, I2C addresses, thresholds, paths, flags
├── run.py                  Flask REST API entry point
├── mqtt.py                 MQTT service + Home Assistant discovery (mqtt.service)
├── app
│   ├── __init__.py         create_app(): blueprints, CORS, optional API-key auth
│   ├── lib                 shared helpers, not tied to one sensor
│   │   ├── hardware.py     get_pin_factory(), detect_model(), current_duty_fraction()
│   │   ├── lib.py          check_sensor_guard(): 400 on bad init, 503 on hw error
│   │   ├── water.py        is_water_low()
│   │   ├── grow.py         grow-cycle state + reminder cadence
│   │   ├── state.py        actuator state persistence for power-loss recovery
│   │   └── logging_config.py
│   ├── sensors             one folder per sensor: <name>.py driver + routes.py
│   │   ├── light/ pump/ distance/ temperature/ humidity/ pcb_temp/
│   │   ├── camera/         stills + timelapse
│   │   ├── schedule/       schedule.py compiles the schedule into crontab lines
│   │   └── grow/ pods/ system/
│   ├── integrations        alexa.py, thingsboard.py (documented glue points)
│   └── web                 self-contained index.html control UI served at /
├── bin
│   ├── setup.sh            one-time Pi setup (idempotent)
│   ├── update.sh           in-place update (garden-update)
│   ├── light.sh water.sh   CLI wrappers cron calls (/usr/local/bin/light|water)
│   ├── schedule-refresh.sh nightly: expire vacation mode, prune spent one-offs
│   ├── ha-align-entity-ids.py  rename HA entity ids onto the pinned scheme
│   └── api-test.sh         curls every REST endpoint (spins the pump)
├── simulator               full stack with stateful fake hardware, off-Pi
├── services                systemd unit, mosquitto + telegraf configs, udev rules
├── docs                    DEPLOYMENT.md, INSTALL.md, design, access, homeassistant/
└── tests                   185+ tests; _hwstub.py fakes GPIO when libs are absent
```

Every sensor follows the same shape — `<name>.py` (a driver class with an
argparse `__main__` so it runs standalone), `routes.py` (a Flask blueprint whose
handlers are wrapped in `check_sensor_guard`), and `__init__.py`. Adding a sensor
is a new folder plus one `register_blueprint` line. Drivers are instantiated at
import inside `try/except` → `None`, so the app still imports off-Pi and the
guard returns a clean 400/503 instead of crashing.

**Three entry points, one driver layer.** `mqtt.py`, `run.py`/`app/`, and the
per-driver CLIs all import the *same* classes from `app/sensors/*`. Behaviour
changes — how the pump ramps, how the water guard works — belong in the driver,
not in any single entry point.

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **This is the PushMotion fork, not upstream.** Read [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
> before changing `mqtt.py`, the water-sensor logic, or anything pump-related. It
> covers why this fork diverges from `iot-root/garden-of-eden`, the tower's water
> calibration and how to redo it, two ways to take a *wrong* sensor reading, where
> tests may safely run (never on the Pi — the hardware stubs disengage there), and
> the open items. The Pi tracks this fork's `feat/gardyn-tower-local`; pointing it
> back at upstream `main` silently reintroduces three pump defects.

## What this is

Firmware/control software for a **Gardyn** hydroponic system running on a Raspberry Pi (Zero 2 / Zero W) inside the unit. It talks to physical hardware over GPIO and I2C: lights (PWM), water pump, distance/water-level sensor, temperature & humidity, PCB temp, pump power monitor, USB cameras, and a momentary button.

It exposes three control surfaces over the **same sensor drivers**:
- **MQTT** (`mqtt.py`) — long-running service, Home Assistant auto-discovery, runs as `mqtt.service`.
- **Flask REST API** (`run.py` + `app/`) — HTTP control of each sensor.
- **CLI** — each driver module is runnable directly with argparse flags (see below).

A separate repo (added as a working dir: `/home/heather/dev/garden-of-eden-app`) is the **desktop/mobile controller** (Electron + React, single `index.html`). It does NOT use the Flask app in this repo directly — it ships its own `pi/run.py` that wraps `app.create_app()` to add API-key auth, camera capture, cron scheduling, and timelapse. When changing REST routes here, keep `garden-of-eden-app/pi/run.py` in sync.

## Running & development

This code only fully runs **on the Pi** — it imports `gpiozero`, `pigpio`, `RPi.GPIO`, `picamera`, etc. and requires the `pigpiod` daemon plus I2C hardware. Most logic cannot be exercised on a dev laptop; rely on the unit tests (which mock GPIO) for off-Pi work.

```bash
# One-time Pi setup: apt deps, venv, I2C enable, sensor detection, symlinks,
# and installs+starts mqtt.service. Idempotent.
./bin/setup.sh

source venv/bin/activate          # all commands below assume the venv is active

python run.py                     # Flask REST API on 0.0.0.0:5000 (debug)
python mqtt.py                    # MQTT service (normally run via systemd)
```

### Tests (run off-Pi)

CI and local tests run **without hardware**: `tests/_hwstub.py` (loaded by
`tests/__init__.py`) injects fake `board`/`gpiozero`/`adafruit_*`/`pigpio`/`smbus`
modules into `sys.modules`, but only when the real libs are absent.

```bash
python -m venv .venv-dev && .venv-dev/bin/pip install -r requirements-dev.txt
# CI gate — note the -t . so the tests PACKAGE (and its stub bootstrap) loads,
# while app/ is not scanned as test modules:
python -m unittest discover -t . -s tests -p 'test_*.py'
ruff check . && black --check .
```

Because the stub bootstrap lives in the `tests` package `__init__`, plain
`python -m unittest` (which imports `app` directly) fails off-Pi — always use the
`discover -t . -s tests` form above.

### Manual checks

```bash
./bin/api-test.sh                 # curls every REST endpoint (needs run.py running)
# Run a driver directly (each has its own __main__ + argparse):
python app/sensors/light/light.py [--on] [--off] [--brightness INT]
python app/sensors/pump/pump.py [--on] [--off] [--speed INT]
python app/sensors/distance/distance.py
```

Symlinks `light` and `water` (created by setup.sh → `bin/light.sh`, `bin/water.sh`) are convenience CLI wrappers on the Pi.

## Architecture

### Sensor module pattern (the core convention)
Every sensor lives in `app/sensors/<name>/` and follows the same shape:
- `<name>.py` — the hardware driver: a class (e.g. `Light`, `Pump`, `Distance`) or function, plus a `__main__`/argparse block so it's runnable standalone. Drivers accept a `pin_factory` so callers can inject `PiGPIOFactory`.
- `routes.py` — a Flask `Blueprint` that instantiates the driver once and wraps each route with the `check_sensor_guard` decorator.
- `__init__.py`.

`app/__init__.py` (`create_app`) registers each blueprint under its own `url_prefix`: sensors (`/light`, `/pump`, `/distance`, `/temperature`, `/humidity`, `/pcb-temp`) plus `/camera`, `/schedule`, `/grow`, `/pods`, `/system`, and the built-in web UI at `/` (`app/web/`, a self-contained `index.html` served by `web_blueprint`; the `/` and `/static` paths bypass API-key auth so the page can load and prompt for a key). Adding a sensor = new folder following this pattern + one `register_blueprint` line. `create_app` also applies CORS and registers optional API-key auth (`_register_auth`, active only when `GARDEN_API_KEY` is set; localhost bypasses).

### Shared helpers (`app/lib/`)
- `hardware.py` — **`get_pin_factory()`** returns the one shared `PiGPIOFactory` (honors `PIGPIO_HOST`/`PIGPIO_PORT` for Docker); **`detect_model()`** infers the Gardyn model from I2C addresses.
- `lib.py` — `check_sensor_guard(sensor, name)`: 400 if the sensor is `None` (failed init), **503** if the handler raises (hardware unavailable), 400 on `ValueError`.
- `logging_config.py` — `configure_logging()` used by `run.py`, `mqtt.py`, CLIs (level via `LOG_LEVEL`).
- `water.py` — `is_water_low`, `pump_cutoff` (resolves the interlock, falling back to the alert), `is_reading_fresh`, and **`tank_readings()`**, the single derivation of depth/percent/gallons from the airgap. Nothing downstream of `config.py` may keep its own copy of the tank geometry: `mqtt.py` publishes from `tank_readings()` and `GET /distance` returns it, so HA and the web UI show the same computed numbers.
- `water_guard.py` / `cleaning_guard.py` — CLI entry points for the cron path (`bin/water.sh`), which cannot read the sensor itself without cross-talking with the service, so they act on the persisted verdict. Both fail *open*: a stopped service must not withhold water indefinitely.
- `ambient.py` — `stable_reading()`, a median-of-N plus a plausibility band for the AM2320/DHT20. The part intermittently returns the *humidity* value in the temperature slot, and a desynced read is a well-formed float, so the drivers' exception retries never fire on it.
- `grow.py` (grow-cycle state, reminders and nutrient dose), `state.py` (actuator state persistence for power-loss recovery), `persist.py` (atomic JSON writes), `locking.py`, `runtime.py`, `pods.py`, `light_schedule.py`, `cleaning.py`.

Route drivers are instantiated at import inside a `try/except` → `None` on failure, so the app imports off-Pi and `check_sensor_guard` handles the degraded case.

**The blueprint imports live inside `create_app()`, not at module scope, and must stay there.** Each `routes.py` builds its driver at import, so importing them from `app/__init__.py` meant *any* `import app.<anything>` constructed the full set of GPIO drivers. `bin/water.sh` → `pump.py` → `app.lib.hardware` hit exactly that, so every cron watering run quietly built a second `DistanceSensor` beside the MQTT service's own — and two processes triggering one ultrasonic sensor cross-talk, so both read wrong.

### Configuration is centralized
All pins, I2C addresses, thresholds, file paths, and feature flags live in `config.py` (env-driven, hex-aware int parsing) and are documented in `.env-dist`. Drivers default their pins/addresses from `config.*` — don't hardcode.

This is enforced, not just encouraged. Values that were re-typed elsewhere have each caused a real defect: the web UI's hardcoded tank geometry disagreed with HA on any calibrated tower, and `MAX_PUMP_RUN_SECONDS` was ignored by `bin/water.sh`, which kept its own `300`. Both now derive from config, and `tests/test_tank_parity.py` and `tests/test_calibration_exposure.py` fail if a client starts deriving its own again.

`GET /system` is how non-Python clients get these values (tank calibration, both water thresholds, the pump cap, `DISPLAY_UNITS`) — a client should read them, never hardcode them.

`DISPLAY_UNITS` (`metric`/`imperial`) is presentation only. The tower always measures, stores, publishes and calibrates in metric — the same arrangement temperature has always had, where it publishes Celsius and HA converts for display. A unit preference that could move a threshold would be a safety bug, and `tests/test_display_units.py` asserts it cannot.

### Three entry points, one driver layer
`mqtt.py`, `run.py`/`app/`, and the per-driver CLIs all import the **same** driver classes from `app/sensors/*`. Behavior changes (e.g. how the pump ramps speed) belong in the driver, not in any single entry point.

### mqtt.py specifics
- Uses the shared `get_pin_factory()` and `configure_logging()`.
- Physical **button** (`gpiozero.Button`, `BUTTON_PIN`): single press → toggle light, double → toggle pump, long press (`when_held`) → event only. All presses publish to `<MQTT_IDENTIFIER>/button/event` as JSON `{"event_type": ...}` and are exposed as an HA `event` entity (#78).
- Publishes **Home Assistant MQTT discovery** (`send_discovery_messages`), reporting `detect_model()` as the device model. Topic base `BASE_TOPIC`.
- Publishes telemetry on a timer plus grow-cycle stage/reminders; water-low logic uses `app.lib.water.is_water_low`.
- **Power-loss recovery**: restores actuator state on connect (`restore_actuator_state`), persists state on every toggle (`app.lib.state`), and turns the pump off on SIGTERM/SIGINT (`graceful_shutdown`).
- **Over-temperature alert** (`app/sensors/pcb_temp/over_temp.py`): the PCT2075 drives a comparator output on `OVER_TEMP_ALERT_PIN` *itself*, so the alert fires even when the service is wedged — that is the whole reason it beats thresholding the published reading. `mqtt.py` programs the chip at startup, watches the pin's edges, and publishes a notify-only `problem` binary sensor. Optional by design: if the chip or pin is unavailable it logs and leaves the entity absent rather than taking the service down, and it publishes *nothing* rather than a confident `OFF` nobody is watching. Keep the chip's **active-low** default; inverting it makes a disconnected chip read as "fine".

### Simulator (off-Pi)
`simulator/` runs the full stack with realistic fake hardware. `fake_hardware.install()` injects stateful fakes into `sys.modules` (distinct from `tests/_hwstub.py`, which is bare mocks for unit tests) and must run before importing `app`. `python -m simulator.serve` serves the web UI + REST (reloader off; camera returns a placeholder JPEG; **crontab is sandboxed** so schedules never touch the host). `python -m simulator.mqtt_sim` runs `mqtt.py` via `runpy` against a local broker for Home Assistant testing. `tests/test_discovery.py` validates HA discovery offline; `tests/test_integration.py` smoke-tests every GET route.

### Packaging & CI
- `Dockerfile` + `docker-compose.yml` run the API and MQTT service in containers, talking to a host `pigpiod` via `PIGPIO_HOST`. `requirements.txt` is Pi/runtime deps; `requirements-dev.txt` is pure-Python for CI.
- `.github/workflows/ci.yml` (ruff + black + unittest, matrix 3.9/3.11), `release-please.yml` (changelog/versioning from Conventional Commits), and the existing `enforce-pr-title.yml`.
- `bin/setup.sh` checks OS compatibility and installs camera udev rules; `bin/update.sh` (`garden-update`) updates in place.

### Configuration
All config flows through `config.py`, which reads `.env` (copy from `.env-dist`) via `python-dotenv`. Includes MQTT broker/credentials, device identity for HA, camera device paths/resolution, and `SENSOR_TYPE` (AM2320 for Gardyn 1.0/2.0, DHT20 for 3.0+ — auto-detected by setup.sh via I2C address).

## Hardware notes that affect code

- Requires the **pigpiod** daemon; drivers use `PiGPIOFactory` rather than the default gpiozero pin factory. `mqtt.service` depends on `pigpiod.service`.
- I2C device addresses are meaningful: PCT2075 `0x48` (PCB temp), INA219 `0x40` (pump power), DHT20 `0x38`, AM2320 `0x5c`. The AM2320 needs a wakeup sequence and won't show in a plain `i2cdetect`.
- Targets **Python 3.9+**; pinned deps in `requirements.txt` are chosen for ARM/Pi compatibility — be cautious bumping versions.

## Project documentation (`docs/`)

- **`DEPLOYMENT.md`** — the one to read first. Branch model, water calibration and how to redo it, how watering is guarded, the over-temperature alert, units, the two ways to take a *wrong* sensor reading, and open items.
- `RESERVOIR-STANDARD.md` — the measured reservoir standard for the Gardyn Home tank.
- `FLEET-READINESS.md` — what a second/third tower needs before it goes live.
- `pizero2-upgrade.md` — moving a tower from a Zero W to a Zero 2 W.
- `simulator.md` — running the full stack off-Pi.
- `INSTALL.md`, `access.md`, `design.md`, `maintenance.md` — inherited from upstream.
- `homeassistant/` — two example dashboards. `pm-example.yaml` covers all 47 discovered entities; `lovelace-example.yaml` is a 24-entity subset and its header lists every omission.

## Commit conventions

Conventional Commits are enforced by project norms (see `CONTRIBUTORS.md`): `<type>(<scope>): <description>` where type ∈ `feat|fix|docs|style|refactor|test|chore`, `!` for breaking changes with a `BREAKING CHANGE:` footer.

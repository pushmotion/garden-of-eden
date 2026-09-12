# AGENTS.md

Entry point for coding agents (OpenAI Codex, Cursor, etc.). Claude Code reads
`CLAUDE.md`, which is the fuller version of this file — **read that next**, then
`docs/DEPLOYMENT.md`. This file exists so an agent that only looks for
`AGENTS.md` still lands on the things that will bite it.

## What this is

Firmware for a **Gardyn** hydroponic tower on a Raspberry Pi Zero W. It drives
real hardware over GPIO/I2C: grow lights (PWM), a water pump, an ultrasonic
water-level sensor, temp/humidity, PCB temp, and USB cameras.

**This is the PushMotion fork, not upstream `iot-root/garden-of-eden`.** The
fork carries pump fixes upstream does not have. Pointing a tower back at
upstream `main` silently reintroduces three pump defects.

## Read these before touching anything

| File | Why |
|---|---|
| `CLAUDE.md` | Architecture, sensor-module pattern, how to run things |
| `docs/DEPLOYMENT.md` | **Required before changing `mqtt.py`, water logic, or anything pump-related.** Calibration, traps, runbooks |
| `.env-dist` | Every config knob, documented |

## Hard rules

1. **Never run the test suite on a Pi.** `tests/_hwstub.py` injects fake GPIO
   only when the real libraries are *absent*. On a tower the stubs disengage and
   importing the app drives real hardware attached to living plants. Run tests
   on a dev machine only.

2. **The pump safety cap is load-bearing.** `MAX_PUMP_RUN_SECONDS` (300) is
   enforced independently in five places: the pump routes, the MQTT safety
   timer, the schedule cron compiler, `bin/water.sh`, and the Home Assistant
   duration control. Do not raise it to enable a longer run — add a separate,
   explicitly-bounded budget instead (see "Cleaning mode" in `docs/DEPLOYMENT.md`).

3. **The ultrasonic sensor cannot be read by two processes at once.** They
   cross-talk and *both* return wrong values — this once nearly became a
   calibration constant. `mqtt.service` owns polling; everything else reads the
   verdict it persists to the state file.

4. **Never commit secrets.** `.env` and `.ha_token` are gitignored. **This fork
   is public**, and a token has already leaked into a public PR once because a
   gitignore rule was added *after* the file was tracked (which does nothing).
   CI now blocks tracked secret files — do not work around it. Tower IPs,
   hostnames and credentials are deliberately kept out of `docs/` too.

5. **Nothing tower-specific is ever committed.** Per-unit state lives in `.env`
   and `~/.garden_*.json`, outside the repo. That separation is what keeps every
   code change upstreamable as-is. Put new per-tower state in files, not in
   tracked defaults.

6. **Water thresholds are per-unit and are not transferable.** They are
   *airgaps* — distance from the sensor face down to the water — so they are
   meaningless without that unit's own `WATER_EMPTY_CM`. Copying a threshold
   between towers can refuse to water a two-thirds-full reservoir. Measure the
   empty-tank airgap per unit, then derive the rest.

## Fleet shape

Two towers, both Pi Zero W on Raspbian 13 (trixie) / Python 3.13:

- **gardyn_01** — an original Gardyn Home 1. AM2320 temp/humidity, INA219 pump
  power monitor present, two USB cameras.
- **gardyn_02** — a Gardyn Home 2, but **the product name does not match the
  code's model taxonomy**: it carries a **DHT20**, so `detect_model()` reports
  `"gardyn 3.0"`. It has **no INA219** at any address, so `/pump/stats` and the
  HA pump-power entities do not work on it.

Trust an I2C scan over the model on the box when provisioning a new unit.

Addresses, SSH keys and credentials are **not** in this repo — ask the operator.

## Branch model

`feat/gardyn-tower-local` is THE BUILD — long-lived, never landed on `main`.
`main` is kept a pure mirror of `upstream/main` so it always answers "what does
upstream have?"; refresh it with `git merge --ff-only upstream/main`. The towers
track the build branch. `origin` = the pushmotion fork, `upstream` = `iot-root`.

## Conventions

Conventional Commits (`<type>(<scope>): <description>`, type ∈
`feat|fix|docs|style|refactor|test|chore`) are enforced by CI on PR titles.

CI gate, runnable off-Pi:

```bash
python -m unittest discover -t . -s tests -p 'test_*.py'
ruff check . && black --check .
```

Note the `-t .` — the hardware-stub bootstrap lives in the `tests` package
`__init__`, so a plain `python -m unittest` fails off-Pi.

There is also a full off-Pi simulator with stateful fake hardware:

```bash
python -m simulator.serve      # web UI + REST on :5000, no Pi needed
```

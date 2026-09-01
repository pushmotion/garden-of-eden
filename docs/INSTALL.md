# Installing on a Raspberry Pi (handoff notes)

These notes are written for the person **and the Claude Code session** doing the
install on the actual Gardyn Pi. Follow them top to bottom. The install is
designed to be brick-safe (dry-run, backups, uninstall) — see "Safety" below.

> **Important:** install from **`feat/gardyn-tower-local`** on
> `pushmotion/garden-of-eden`. That is the build branch — the only thing a tower
> should ever run. This fork's `main` is kept as a pure mirror of upstream and
> deliberately carries none of the fork's work.
>
> The reason is not features: the v2 overhaul this guide was first written for
> has since shipped upstream as 2.0.0. It is that upstream 2.0.0 still contains
> three pump defects which make the pump unusable from Home Assistant, and one
> of which lets the speed slider bypass the dry-run guard. See
> [Branch model](DEPLOYMENT.md#branch-model) and
> [Why this fork exists](DEPLOYMENT.md#why-this-fork-exists) — that document is
> the source of truth for both, so this one does not restate them.

---

## 0. Prerequisites (flashing)

Flash Raspberry Pi OS (Lite is fine) with the **Raspberry Pi Imager**. In the
⚙ advanced options set:

- **Enable SSH** (you need it to run the installer)
- **Username/password** (e.g. user `gardyn`)
- **Hostname** `gardyn`
- **Wi-Fi** SSID + password + country

Boot the Pi, then SSH in:

```bash
ssh gardyn@gardyn.local      # or use the Pi's IP if .local doesn't resolve
```

## 1. Get the code (the right branch)

```bash
git clone https://github.com/pushmotion/garden-of-eden.git
cd garden-of-eden
git checkout feat/gardyn-tower-local
```

Check it took — every later step assumes it:

```bash
git branch --show-current      # feat/gardyn-tower-local
```

## 2. Configure

```bash
cp .env-dist .env
nano .env
```

Set at least:
- `MQTT_USERNAME` / `MQTT_PASSWORD` (your mosquitto creds)
- `MQTT_IDENTIFIER` — **unique per unit**, e.g. `gardyn_01`. It namespaces the
  MQTT topics, the Home Assistant device, and every entity id
  (`sensor.gardyn_01_water_depth`), so it is the only line a second tower needs
  to change. Use lowercase letters, digits and underscores only: HA slugifies
  entity ids, so a hyphen would make the entity prefix (`gardyn_xx`) disagree
  with the topic namespace (`gardyn-xx`). Leave `MQTT_BASETOPIC` unset — it
  defaults to this.
- `WATER_LOW_CM` (low-water threshold; `0` disables)
- Optionally `GARDEN_API_KEY` to require a key for remote API access.

`SENSOR_TYPE` is **auto-detected** by setup (AM2320 vs DHT20) — leave it unset.

## 3. Install (safe)

```bash
./bin/setup.sh --dry-run     # preview every system change; makes NO changes
./bin/setup.sh               # asks for confirmation before applying
```

What it does: apt deps, a Python venv, enables I2C, adds you to `i2c/gpio/dialout`
groups, installs the `light`/`water`/`garden-update` CLIs, camera udev rules,
enables SSH + mDNS, and installs/starts the `mqtt.service` and `garden-api.service`
systemd units. Takes a few minutes (apt + pip).

## 4. Reboot

I2C and new group membership need a reboot to take effect:

```bash
sudo reboot
```

## 5. Verify

```bash
sudo systemctl status pigpiod mqtt.service garden-api.service   # all active
sudo i2cdetect -y 1                                             # expect 0x40,0x48, and 0x38 or 0x5c
curl localhost:5000/health                                      # {"status":"ok"}
curl localhost:5000/system                                      # model/version
curl localhost:5000/temperature                                 # a number (503 = sensor unreachable)
./bin/api-test.sh                                               # exercises all endpoints
```

Then open the web UI from any device on the network: **http://gardyn.local:5000/**

### Hardware sanity (do gently)
- Toggle the **light** first (lowest risk).
- For the **pump**, use a short run (e.g. `water 5`) and watch it — make sure it
  actually moves water and stops. Don't run the pump dry.

## 6. Home Assistant (optional)

Have an MQTT broker (mosquitto on the Pi or on HA). With `.env` MQTT creds set and
`mqtt.service` running, the device **auto-discovers** in HA with all entities
(light, pump, temp, humidity, PCB temp, water level + low alert, cameras, button).
Dashboard example: `docs/homeassistant/lovelace-example.yaml`.

## Safety / recovery

- `setup.sh --dry-run` shows everything first and changes nothing.
- Every system file edited (`config.txt`, `/etc/modules`, `/etc/hosts`) is backed
  up to `<file>.garden.bak`.
- **Undo the install:** `./bin/uninstall.sh` (stops/removes services, restores
  backups, removes our cron entries; leaves apt packages + SSH alone).
- **Won't boot after an I2C change?** Put the SD card in any computer and copy
  `config.txt.garden.bak` over `config.txt` on the boot partition. No reflash.
- Logs: `journalctl -u garden-api.service`, `journalctl -u mqtt.service`, and in
  the repo dir `mqtt.log` (MQTT service), `garden-api.log` (REST API), and
  `gardyn.log` (only the `light`/`water` CLIs — each process writes its own file
  so the two services never interleave).

## Updating later

```bash
garden-update        # = bin/update.sh: git pull + pip install + restart services
```

It pulls **whatever branch is checked out** (`git pull --ff-only`), so it only
keeps you on the build branch if you were on it to begin with — one more reason
to confirm the branch in step 1.

---

## Notes for the Claude doing the install

- **Always run `./bin/setup.sh --dry-run` first**, show the user the 8-line plan,
  and get an explicit OK before running the real install. Use `--yes` only if the
  user has agreed to skip the prompt.
- Confirm you're on **`feat/gardyn-tower-local`** (`git branch --show-current`).
  If it says `main`, stop: that is the upstream mirror, and deploying it
  reintroduces the pump defects this fork exists to fix.
- The **simulator** (`python -m simulator.serve` / `mqtt_sim`) is for *off-Pi*
  testing only — do **not** run it on the Pi; the real services serve the app.
- Expect a **reboot** between install and verification.
- Off-Pi-only quirks don't apply here: on the Pi, `fswebcam` (cameras) and real
  I2C sensors exist, so `/camera/*` and sensor endpoints should return real data.
- Raspberry Pi OS Bookworm keeps boot config at `/boot/firmware/config.txt`;
  setup detects this automatically — don't hand-edit the wrong path.
- If a service is failing, check `journalctl -u <svc>` before changing anything;
  most issues are a missing `.env` value or pigpiod not running.

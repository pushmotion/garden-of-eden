import os

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


def _get_bool(name, default=False):
    """Parse a truthy/falsy environment variable."""
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _get_int(name, default):
    """Parse an int env var, accepting decimal ("72") or hex ("0x48")."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    raw = raw.strip()
    try:
        # base 0 auto-detects 0x/0o/0b prefixes; plain decimal also works.
        return int(raw, 0)
    except ValueError:
        return int(raw)


def _get_float(name, default):
    return float(os.getenv(name, str(default)))


# ---------------------------------------------------------------------------
# MQTT configuration
# ---------------------------------------------------------------------------
BROKER = os.getenv("MQTT_BROKER", "localhost")
PORT = _get_int("MQTT_PORT", 1883)
KEEP_ALIVE_INTERVAL = _get_int("MQTT_KEEPALIVE_INTERVAL", 60)

# Topic / device identity (used for Home Assistant discovery)
VERSION = os.getenv("MQTT_VERSION", "1.0.0")
# Keep this slug-safe (a-z, 0-9, underscore). It becomes both the MQTT topic
# namespace and the entity-id prefix, and Home Assistant slugifies entity ids --
# so the old "gardyn-xx" default published to `gardyn-xx/...` while HA showed
# `sensor.gardyn_xx_...`, quietly disagreeing with itself.
IDENTIFIER = os.getenv("MQTT_IDENTIFIER", "gardyn_01")
MODEL = os.getenv("MQTT_DEVICE_MODEL", "gardyn 3.0")
# Defaults to the identifier so each tower gets its own topic namespace. A
# shared base topic is not cosmetic: mqtt.py subscribes to BASE_TOPIC + "/#",
# so two units on the same one receive each other's commands and one tower's
# light switch drives both. It also names the Home Assistant device, so sharing
# it makes HA disambiguate the duplicate and mangle every entity id.
# Override only to keep an existing single-tower deployment on its old topics.
BASE_TOPIC = os.getenv("MQTT_BASETOPIC", IDENTIFIER)

USERNAME = os.getenv("MQTT_USERNAME")
PASSWORD = os.getenv("MQTT_PASSWORD")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.getenv("LOG_FILE", "gardyn.log")

# ---------------------------------------------------------------------------
# pigpio connection (PiGPIOFactory). For Docker, point at the pigpiod
# container, e.g. PIGPIO_HOST=pigpiod PIGPIO_PORT=8888.
# ---------------------------------------------------------------------------
PIGPIO_HOST = os.getenv("PIGPIO_HOST") or None
PIGPIO_PORT = _get_int("PIGPIO_PORT", 8888)

# ---------------------------------------------------------------------------
# Sensor / hardware model
# SENSOR_TYPE is the temp/humidity chip: "AM2320" (Gardyn 1.0/2.0) or
# "DHT20" (Gardyn 3.0+). MODEL_OVERRIDE forces a Gardyn model instead of
# auto-detecting at runtime (see app/lib/hardware.py).
# ---------------------------------------------------------------------------
SENSOR_TYPE = os.getenv("SENSOR_TYPE")
MODEL_OVERRIDE = os.getenv("GARDYN_MODEL") or None

# Per-model hardware profiles (issues #72, #84). Differences between Gardyn
# generations are captured here so detection/UX can adapt. Pin defaults still
# come from the env vars above; this table documents expected sensors and any
# known per-model deviations (extend as hardware is characterized).
MODELS = {
    "gardyn 1.0": {"temp_humidity": "AM2320", "cameras": 2},
    "gardyn 2.0": {"temp_humidity": "AM2320", "cameras": 2},
    "gardyn 3.0": {"temp_humidity": "DHT20", "cameras": 2},
    "gardyn studio": {"temp_humidity": "DHT20", "cameras": 2},
}

# ---------------------------------------------------------------------------
# GPIO pin assignments (BCM numbering)
# ---------------------------------------------------------------------------
LIGHT_PIN = _get_int("LIGHT_PIN", 18)
LIGHT_FREQUENCY = _get_int("LIGHT_FREQUENCY", 8000)

PUMP_PIN = _get_int("PUMP_PIN", 24)
PUMP_FREQUENCY = _get_int("PUMP_FREQUENCY", 50)

DISTANCE_ECHO_PIN = _get_int("DISTANCE_ECHO_PIN", 19)
DISTANCE_TRIGGER_PIN = _get_int("DISTANCE_TRIGGER_PIN", 26)

BUTTON_PIN = _get_int("BUTTON_PIN", 13)
OVER_TEMP_ALERT_PIN = _get_int("OVER_TEMP_ALERT_PIN", 25)

# Default actuator levels applied when toggled on
DEFAULT_BRIGHTNESS = _get_int("DEFAULT_BRIGHTNESS", 50)
DEFAULT_PUMP_SPEED = _get_int("DEFAULT_PUMP_SPEED", 100)

# Hard safety cap: the pump may never run longer than this in one go, no matter
# what a schedule, API call, or CLI invocation requests. Enforced in the pump
# routes, the schedule cron compiler, and bin/water.sh. 300s = 5 minutes.
MAX_PUMP_RUN_SECONDS = _get_int("MAX_PUMP_RUN_SECONDS", 300)

# ---------------------------------------------------------------------------
# I2C device addresses
# ---------------------------------------------------------------------------
PCB_TEMP_ADDRESS = _get_int("PCB_TEMP_ADDRESS", 0x48)
INA219_ADDRESS = _get_int("INA219_ADDRESS", 0x40)

# Over-temperature thresholds for the PCB sensor (deg C)
OVER_TEMP_HIGH = _get_float("OVER_TEMP_HIGH", 36)
OVER_TEMP_HYSTERESIS = _get_float("OVER_TEMP_HYSTERESIS", 34)

# ---------------------------------------------------------------------------
# Water level alerting
# WATER_LOW_CM is the distance (cm) from the sensor to the water surface above
# which the tank is considered low. 0/unset disables the alert.
# ---------------------------------------------------------------------------
WATER_LOW_CM = _get_float("WATER_LOW_CM", 0) or None

# The airgap (cm) at which the pump is refused outright, as opposed to merely
# alerted on. Separate from WATER_LOW_CM because the two answer different
# questions: the alert wants to be early enough to be useful ("top me up"),
# while the interlock wants to be late enough that it does not withhold water
# from a tank that still has plenty. One value cannot be both -- set it early
# and a full-ish tank stops being watered; set it late and the alert is useless.
#
# Both are airgaps, so both are meaningless without WATER_FULL_CM/WATER_EMPTY_CM
# below: a threshold is only a percentage once you know the tank's geometry.
# Unset falls back to WATER_LOW_CM, which is exactly the previous behaviour --
# one threshold doing both jobs.
PUMP_CUTOFF_CM = _get_float("PUMP_CUTOFF_CM", 0) or None

# How often (seconds) the MQTT service re-reads the tank and refreshes the
# low-water alert. Kept short so a transient false alarm self-clears quickly.
WATER_CHECK_SECONDS = _get_int("WATER_CHECK_SECONDS", 180)

# How old (seconds) the MQTT service's last water reading may be and still gate
# the cron/CLI watering path. The CLI cannot read the sensor itself without
# cross-talking with the service, so it acts on the persisted verdict instead --
# and only while that verdict still describes the tank.
#
# The default tolerates a couple of missed poll cycles. Do not raise it far: a
# stale "blocked" verdict from a service that has stopped would withhold water
# indefinitely, so this is also the window after which watering fails *open*.
WATER_READING_MAX_AGE_SECONDS = _get_int("WATER_READING_MAX_AGE_SECONDS", WATER_CHECK_SECONDS * 3)

# How often (seconds) the MQTT service re-reads the light/pump duty cycles and
# republishes them. cron, the REST API, the CLI and the physical button all drive
# the hardware without going through the MQTT service, so without polling Home
# Assistant drifts out of step with reality. 0 disables the poll.
ACTUATOR_POLL_SECONDS = _get_int("ACTUATOR_POLL_SECONDS", 15)

# Which unit system clients should display by default: "metric" (cm, C, L) or
# "imperial" (in, F, gal). This is a *presentation* default only -- the tower
# always measures, stores, publishes and calibrates in metric, exactly as it
# always published Celsius and let Home Assistant convert. Changing it must
# never change a threshold, a calibration constant or anything on the wire.
#
# The web UI uses it as the starting point for a browser that has not chosen
# yet; a viewer's own choice still wins and is remembered per browser. Home
# Assistant ignores it and applies its own unit system, which is why the tower
# publishes Celsius to an HA instance that displays Fahrenheit.
_IMPERIAL_ALIASES = ("imperial", "us", "customary")
DISPLAY_UNITS = (
    "imperial"
    if os.getenv("DISPLAY_UNITS", "metric").strip().lower() in _IMPERIAL_ALIASES
    else "metric"
)

# Tank geometry for the cm->gallons readout: distance (cm) from the sensor to the
# water surface when the tank is full vs empty, and the tank capacity in gallons
# (Gardyn Home ~5 gal, Studio ~4 gal). Calibrate FULL/EMPTY to your unit.
WATER_FULL_CM = _get_float("WATER_FULL_CM", 5)
WATER_EMPTY_CM = _get_float("WATER_EMPTY_CM", 20)
TANK_CAPACITY_GALLONS = _get_float("TANK_CAPACITY_GALLONS", 5)

# ---------------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------------
UPPER_CAMERA_DEVICE = os.getenv("UPPER_CAMERA_DEVICE", "/dev/video0")
LOWER_CAMERA_DEVICE = os.getenv("LOWER_CAMERA_DEVICE", "/dev/video2")
UPPER_IMAGE_PATH = os.getenv("UPPER_IMAGE_PATH", "/tmp/upper_camera.jpg")
LOWER_IMAGE_PATH = os.getenv("LOWER_IMAGE_PATH", "/tmp/lower_camera.jpg")
CAMERA_RESOLUTION = os.getenv("CAMERA_RESOLUTION", "640x480")
IMAGE_INTERVAL_SECONDS = _get_int("IMAGE_INTERVAL_SECONDS", 3600)

# Timelapse: archive a timestamped frame on each capture, capped at MAX_FRAMES,
# and assemble into mp4 at TIMELAPSE_FPS. Stored under the repo by default.
TIMELAPSE_DIR = os.getenv(
    "TIMELAPSE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "timelapse")
)
TIMELAPSE_MAX_FRAMES = _get_int("TIMELAPSE_MAX_FRAMES", 720)
TIMELAPSE_FPS = _get_int("TIMELAPSE_FPS", 12)
# A build works with any number of frames, but a short archive played at the full
# frame rate flashes past. Frame rate is scaled down to stretch small archives to
# roughly TIMELAPSE_TARGET_SECONDS; MIN_FRAMES is only the point at which the UI
# stops warning that there is not much history yet (168 = 7 days of hourly frames).
TIMELAPSE_MIN_FRAMES = _get_int("TIMELAPSE_MIN_FRAMES", 168)
TIMELAPSE_TARGET_SECONDS = _get_int("TIMELAPSE_TARGET_SECONDS", 10)

# ---------------------------------------------------------------------------
# REST API auth (optional). When GARDEN_API_KEY is set, non-localhost
# requests must send it via the X-API-Key header. Localhost (cron) bypasses.
# ---------------------------------------------------------------------------
GARDEN_API_KEY = os.getenv("GARDEN_API_KEY", "")

# ---------------------------------------------------------------------------
# State persistence (actuator + grow-cycle state, for power-loss recovery)
# ---------------------------------------------------------------------------
STATE_FILE = os.path.expanduser(os.getenv("STATE_FILE", "~/.garden_state.json"))
SCHEDULE_FILE = os.path.expanduser(os.getenv("SCHEDULE_FILE", "~/.garden_schedule.json"))

# Per-pod plant tracking (name + shape code). POD_COUNT pods (Gardyn Home = 30).
POD_COUNT = _get_int("POD_COUNT", 30)
PODS_FILE = os.path.expanduser(os.getenv("PODS_FILE", "~/.garden_pods.json"))

# ---------------------------------------------------------------------------
# Grow-cycle & notifications
# ---------------------------------------------------------------------------
GROW_STATE_FILE = os.path.expanduser(os.getenv("GROW_STATE_FILE", "~/.garden_grow.json"))
# Days after a cycle starts to remind about each task (0 disables a reminder)
THINNING_REMINDER_DAYS = _get_int("THINNING_REMINDER_DAYS", 14)
ROOT_CHECK_REMINDER_DAYS = _get_int("ROOT_CHECK_REMINDER_DAYS", 21)
HARVEST_REMINDER_DAYS = _get_int("HARVEST_REMINDER_DAYS", 35)
# Recurring reminder cadences (days), measured from the last acknowledgement.
# Feeding every 7 days suits a system whose solution strength is measured; with
# no EC/TDS sensor on the unit the safer default is a longer cadence plus a
# periodic partial reservoir swap, which is what actually resets accumulation.
NUTRIENT_REMINDER_DAYS = _get_int("NUTRIENT_REMINDER_DAYS", 14)
RESERVOIR_CHANGE_DAYS = _get_int("RESERVOIR_CHANGE_DAYS", 49)

# ---------------------------------------------------------------------------
# External integrations (scaffolded; see app/integrations/ and docs/integrations/)
# ---------------------------------------------------------------------------
ALEXA_ENABLED = _get_bool("ALEXA_ENABLED", False)

THINGSBOARD_ENABLED = _get_bool("THINGSBOARD_ENABLED", False)
THINGSBOARD_HOST = os.getenv("THINGSBOARD_HOST", "")
THINGSBOARD_TOKEN = os.getenv("THINGSBOARD_TOKEN", "")

TELEGRAF_ENABLED = _get_bool("TELEGRAF_ENABLED", False)

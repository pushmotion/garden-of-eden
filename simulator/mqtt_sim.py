"""Run the MQTT service with simulated hardware against a local broker, so Home
Assistant can discover and control the simulated Gardyn.

    # 1. start a broker (either of these):
    docker compose up broker          # bundled mosquitto on :1883
    #   or: sudo apt install mosquitto && sudo systemctl start mosquitto
    # 2. run the simulator:
    python -m simulator.mqtt_sim

Point Home Assistant's MQTT integration at the same broker; the simulated device
auto-discovers with all entities (light, pump, sensors, water alert, cameras,
button event). Override the broker with MQTT_BROKER / MQTT_PORT env vars.
"""

import os
import runpy

from simulator import _sandbox

# Seed sim config before mqtt.py imports config.
_sandbox.seed_env(
    MQTT_BROKER="localhost",
    MQTT_PORT="1883",
    MQTT_IDENTIFIER="gardyn_sim",
)

from simulator import fake_hardware  # noqa: E402

fake_hardware.install()

# Sandbox the crontab writer so schedule changes from HA never touch the host's
# real crontab (mqtt.py's schedule toggles call apply_schedule).
from app.sensors.schedule import schedule as _schedule  # noqa: E402

_sandbox.sandbox_crontab(_schedule)

if __name__ == "__main__":
    print("Garden of Eden MQTT simulator -> broker", os.environ["MQTT_BROKER"])
    # Execute mqtt.py as __main__ with the fakes already in sys.modules.
    runpy.run_path(
        os.path.join(os.path.dirname(__file__), "..", "mqtt.py"),
        run_name="__main__",
    )

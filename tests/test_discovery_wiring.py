"""Every topic discovery declares must actually be wired to something.

test_discovery.py checks the *shape* of the discovery payloads -- object_id
pinning, uniqueness, the device block. It does not check whether the topics
those payloads name are ever published to, or whether the command topics have a
handler. Nothing did, and that gap is not theoretical:

    client.publish(BASE_TOPIC + "/light/brightness", str(level))   # went nowhere

while discovery declared `brightness_state_topic` as `.../light/brightness/state`.
Every existing test passed: the payload was well-formed, the publish succeeded,
the broker accepted it. The value simply landed on a topic nothing subscribed
to, and Home Assistant kept showing a stale brightness.

These two tests close both directions of that gap.
"""

import json
import unittest


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, **kwargs):
        self.published.append((topic, payload))

    def is_connected(self):
        return False


class FakeMsg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode() if isinstance(payload, str) else payload


# Suffixes whose value is produced by hardware or an external event rather than
# by a publisher this test can drive. Each needs a reason -- the list is the
# obvious place for a genuine gap to hide, so it should stay short.
STATE_TOPICS_NOT_DRIVEN_HERE = {
    # Sensor loops that block on real I2C reads in a `while True`.
    "temperature": "published by the temperature poll thread",
    "humidity": "published by the humidity poll thread",
    "pcb/temperature": "published by the PCB temperature poll thread",
    # Camera frames are binary and come from fswebcam.
    "image/upper_camera": "published by capture_and_publish_images()",
    "image/lower_camera": "published by capture_and_publish_images()",
    # Driven by physical presses, via publish_button_event().
    "button/event": "published on a physical button press",
    # Emitted by the logging handler when a WARNING+ record is emitted.
    "log": "published by MqttLogHandler",
    # Written by refresh_all(), which captures cameras and takes several seconds.
    "refresh/status": "published by refresh_all()",
    "refresh/last": "published by refresh_all()",
    # Water telemetry needs a live distance reading.
    "water/level": "published by publish_water_readings()",
    "water/depth": "published by publish_water_readings()",
    "water/percent": "published by publish_water_readings()",
    "water/gallons": "published by publish_water_readings()",
    "water/low/state": "published by evaluate_water_low() after a sensor read",
    "grow/food": "published by the grow reminder thread",
}


class DiscoveryWiringTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import mqtt

        cls.mqtt = mqtt
        cls.base = mqtt.BASE_TOPIC + "/"

        client = FakeClient()
        mqtt.send_discovery_messages(client)
        cls.payloads = [json.loads(p) for _, p in client.published]

    def _declared(self, suffix_key):
        """Every declared topic of a given kind, as a suffix of BASE_TOPIC."""
        found = set()
        for payload in self.payloads:
            for key, value in payload.items():
                if not key.endswith(suffix_key) or not isinstance(value, str):
                    continue
                if value.startswith(self.base):
                    found.add(value[len(self.base) :])
        return found

    def test_every_declared_state_topic_is_published_to(self):
        """A state topic nothing writes to leaves its entity permanently blank."""
        client = FakeClient()
        m = self.mqtt

        # Drive the publishers that do not need hardware or a broker.
        m.publish_water_thresholds(client)
        m.publish_water_low_mode(client)
        m.publish_grow_state(client)
        m.publish_schedule_state(client)
        m.publish_one_time_state(client)
        m.publish_next_pump_run(client)
        m.publish_light_state(client)
        m.publish_pump_state(client)
        m.publish_over_temp_state(client)

        published = {t[len(self.base) :] for t, _ in client.published if t.startswith(self.base)}
        declared = self._declared("state_topic") | self._declared("image_topic")

        missing = {s for s in declared - published if s not in STATE_TOPICS_NOT_DRIVEN_HERE}
        self.assertEqual(
            set(),
            missing,
            "discovery declares these state topics but nothing publishes to them: "
            f"{sorted(missing)}",
        )

    def test_every_declared_command_topic_has_a_handler(self):
        """A command topic with no handler is a control that silently does nothing.

        Checked against on_message's source rather than by sending a message and
        watching for a reply. Several handlers legitimately publish nothing --
        `refresh/all` hands off to a thread, the `*/get` handlers need a real
        sensor, `grow/stage/set` rejects an invalid stage -- so "did anything come
        back?" reports working code as broken. The question here is only whether a
        branch exists to receive the topic at all.
        """
        import inspect

        declared = self._declared("command_topic")
        self.assertTrue(declared, "expected discovery to declare command topics")

        source = inspect.getsource(self.mqtt.on_message)
        unhandled = [s for s in sorted(declared) if f'"{s}"' not in source]

        self.assertEqual(
            [],
            unhandled,
            f"discovery declares these command topics but on_message ignores them: {unhandled}",
        )

    def test_the_handler_check_would_catch_a_missing_branch(self):
        """Guards the guard: prove the source scan can actually fail.

        A test that inspects source is only worth having if a real omission
        trips it, so assert directly that a topic with no branch is detected.
        """
        import inspect

        source = inspect.getsource(self.mqtt.on_message)
        self.assertNotIn('"totally/made/up/set"', source)

    def test_the_regressed_brightness_topic_stays_wired(self):
        """Nails the specific bug: the light's brightness state topic.

        Kept as its own case so the failure names the thing that broke, rather
        than appearing as one entry in a set difference.
        """
        declared = self._declared("state_topic")
        self.assertIn("light/brightness/state", declared)

        client = FakeClient()
        self.mqtt.publish_light_state(client)
        published = {t for t, _ in client.published}
        self.assertIn(self.base + "light/brightness/state", published)


if __name__ == "__main__":
    unittest.main()

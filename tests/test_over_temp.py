"""The hardware over-temperature alert.

Thresholds come from three days of 2-minute samples on a live tower rather than
from a guess, so the tests are written against those measurements:

    lights off            32.6-33.1 C
    lights on             41.6 C mean, 44.8 C peak
    scheduled pump adds   ~2 C
    worst rise above room 18.9 C   (room air 22.3-26.4 C)
    SoC above this chip   8.0-13.9 C

The shipped defaults were 36/34 C, which falls *between* the lights-off idle and
the lights-on normal. Had anything read them, the alert would have tripped every
morning with the lights and cleared every night -- alarming 16 hours a day while
still looking like a working sensor. Nothing read them, so nothing broke; these
tests exist so those values cannot come back.
"""

import unittest
from unittest.mock import patch

import config
from app.sensors.pcb_temp import over_temp

# Measured envelope, from docs and .env-dist.
IDLE_C = 33.1  # lights-off maximum
LIGHTS_ON_NORMAL_C = 41.6  # lights-on mean
LIGHTS_ON_PEAK_C = 44.8
HOT_ROOM_PROJECTION_C = 55.9  # 35 C room + 18.9 C rise + 2 C pump
SOC_THROTTLE_C = 85.0  # Raspberry Pi's documented limit
SOC_OFFSET_MIN_C = 8.0
SOC_OFFSET_MAX_C = 13.9


class ThresholdSanityTestCase(unittest.TestCase):
    """What the configured numbers have to satisfy to be worth wiring up."""

    def test_the_trip_point_clears_everything_ever_measured(self):
        self.assertGreater(config.OVER_TEMP_HIGH, LIGHTS_ON_PEAK_C)

    def test_the_trip_point_survives_a_hot_room(self):
        """A threshold that only clears today's room is a summer false alarm."""
        self.assertGreater(config.OVER_TEMP_HIGH, HOT_ROOM_PROJECTION_C)

    def test_the_clear_point_is_also_above_a_hot_room(self):
        """Otherwise it would latch on in a heatwave and refuse to clear."""
        self.assertGreater(config.OVER_TEMP_HYSTERESIS, HOT_ROOM_PROJECTION_C)

    def test_it_trips_before_the_soc_throttles(self):
        """The whole point of an early warning: fire before the SoC protects itself.

        The processor runs 8-14 C hotter than this chip, so even on the smallest
        observed offset the alert has to land under 85 C at the SoC.
        """
        self.assertLess(config.OVER_TEMP_HIGH + SOC_OFFSET_MAX_C, SOC_THROTTLE_C)
        self.assertLess(config.OVER_TEMP_HIGH + SOC_OFFSET_MIN_C, SOC_THROTTLE_C)

    def test_the_old_defaults_would_have_alarmed_all_day_every_day(self):
        """Why 36/34 is gone. This is the regression that must not return.

        36 C sits between the lights-off idle (33.1 C) and the lights-on normal
        (41.6 C), so the old pair would have tripped at 05:00 when the lights
        came on and cleared at 21:00 when they went off -- alarming for the
        whole 16-hour light period, every day. Not permanently stuck, which is
        arguably worse: it would have looked like a working sensor.
        """
        old_trip, old_clear = 36.0, 34.0
        self.assertGreater(LIGHTS_ON_NORMAL_C, old_trip)  # trips with the lights
        self.assertLess(IDLE_C, old_clear)  # clears overnight, so it looks alive
        # The configured pair does neither.
        self.assertGreater(config.OVER_TEMP_HYSTERESIS, LIGHTS_ON_PEAK_C)


class UsableThresholdsTestCase(unittest.TestCase):
    def test_a_normal_pair_is_accepted(self):
        self.assertEqual((65.0, 58.0), over_temp.usable_thresholds(65, 58))

    def test_an_inverted_pair_is_refused(self):
        """Clear above trip leaves the comparator no band, so the pin chatters."""
        self.assertIsNone(over_temp.usable_thresholds(58, 65))

    def test_an_equal_pair_is_refused(self):
        self.assertIsNone(over_temp.usable_thresholds(65, 65))

    def test_unset_means_leave_the_chip_alone(self):
        """A tower that has not chosen keeps the factory 80/75 C."""
        self.assertIsNone(over_temp.usable_thresholds(None, None))
        self.assertIsNone(over_temp.usable_thresholds(0, 0))
        self.assertIsNone(over_temp.usable_thresholds(65, None))


class ConfigureTestCase(unittest.TestCase):
    def test_the_chip_is_programmed_and_read_back(self):
        trip, clear, active_high = over_temp.configure(65, 58)
        self.assertEqual(65.0, trip)
        self.assertEqual(58.0, clear)
        self.assertFalse(active_high, "the chip's active-low default must be kept")

    def test_defaults_come_from_config(self):
        trip, clear, _ = over_temp.configure()
        self.assertEqual(config.OVER_TEMP_HIGH, trip)
        self.assertEqual(config.OVER_TEMP_HYSTERESIS, clear)

    def test_a_bad_pair_leaves_the_factory_settings_in_place(self):
        """Refusing to program is safer than programming something incoherent."""
        trip, clear, _ = over_temp.configure(58, 65)
        self.assertEqual(80.0, trip)
        self.assertEqual(75.0, clear)

    def test_polarity_is_not_inverted(self):
        """The bench script set active-high; an unpowered chip then reads 'fine'."""
        self.assertFalse(over_temp.ALERT_ACTIVE_HIGH)

    def test_the_bounce_time_is_one_pigpio_will_accept(self):
        """gpiozero's pigpio backend raises above 0.3 s, at construction.

        Caught on the tower rather than here the first time: the service logged
        "bounce must be between 0 and 0.3" and ran on without the alert, which
        is the right failure but a silent one.
        """
        self.assertLessEqual(over_temp.ALERT_BOUNCE_SECONDS, over_temp.MAX_PIGPIO_BOUNCE_SECONDS)
        self.assertGreater(over_temp.ALERT_BOUNCE_SECONDS, 0)

    def test_the_pin_maps_asserted_to_pressed(self):
        """Callers must never have to reason about open-drain polarity."""
        with patch.object(over_temp, "Button") as button:
            over_temp.alert_pin(pin=25, pin_factory=None)
        kwargs = button.call_args.kwargs
        # Active-low: idle is pulled high, the chip sinks it on alert, so a
        # pull-up makes is_pressed mean "alerting".
        self.assertTrue(kwargs["pull_up"])


class MqttReportingTestCase(unittest.TestCase):
    class Client:
        def __init__(self):
            self.published = []

        def publish(self, topic, payload=None, **kwargs):
            self.published.append((topic, payload))

    def setUp(self):
        import mqtt

        self.mqtt = mqtt
        self.client = self.Client()

    def states(self):
        suffix = "/pcb/over_temp/state"
        return [p for t, p in self.client.published if t.endswith(suffix)]

    def test_a_quiet_pin_reports_off(self):
        with patch.object(self.mqtt.over_temp_alert, "is_pressed", False):
            self.mqtt.publish_over_temp_state(self.client)
        self.assertEqual(["OFF"], self.states())

    def test_an_asserted_pin_reports_on(self):
        with patch.object(self.mqtt.over_temp_alert, "is_pressed", True):
            self.mqtt.publish_over_temp_state(self.client)
        self.assertEqual(["ON"], self.states())

    def test_an_unavailable_alert_publishes_nothing(self):
        """Better unknown in HA than a confident OFF nothing is watching."""
        with patch.object(self.mqtt, "over_temp_alert", None):
            self.mqtt.publish_over_temp_state(self.client)
        self.assertEqual([], self.states())

    def test_it_is_discovered_as_a_notify_only_problem_sensor(self):
        import json

        self.mqtt.send_discovery_messages(self.client)
        payloads = [json.loads(p) for _, p in self.client.published if p]
        found = [p for p in payloads if p.get("unique_id", "").endswith("_pcb_over_temp")]
        self.assertTrue(found, "PCB Over Temperature entity was not discovered")
        entity = found[0]
        self.assertEqual("problem", entity["device_class"])
        self.assertEqual("diagnostic", entity["entity_category"])
        # Notify-only: no command topic means HA has nothing to actuate, which
        # is the decision here -- a false positive must not cost a grow cycle.
        self.assertNotIn("command_topic", entity)


if __name__ == "__main__":
    unittest.main()

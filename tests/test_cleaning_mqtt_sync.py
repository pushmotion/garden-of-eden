"""The Home Assistant cleaning switch must flip off when a run ends.

A cleaning run is almost always ended by something that holds no MQTT client:
the watchdog thread in the pump routes, POST /pump/clean/stop, POST /pump/off,
or the physical button. Each stops the pump and clears the session, and none of
them can announce it.

The reconcile loop used to publish only *while* a run was active, so the ON->OFF
edge was never sent. The retained "ON" stood: the pump stopped exactly on time
and the switch in Home Assistant sat on indefinitely -- reported from a live
tower after a 60-minute run.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

import config
from app.lib import cleaning


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, **kwargs):
        self.published.append((topic, payload))

    def is_connected(self):
        return False


class CleaningStateSyncTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import mqtt

        cls.mqtt = mqtt

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        for p in (
            patch.object(config, "STATE_FILE", self.path),
            patch.object(config, "CLEANING_CUTOFF_CM", 18.0),
            patch.object(config, "WATER_EMPTY_CM", 23.05),
            patch.object(config, "CLEANING_MIN_DEPTH_CM", 2.0),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def _fill_tank(self):
        """Record a fresh reading, as the MQTT service would.

        Without one the cleaning water guard fails closed -- by design -- and
        ends the run, which is not what these tests are about.
        """
        from datetime import datetime

        from app.lib import state as state_lib

        state_lib.save_state(
            water_airgap_cm=14.0,
            water_checked_at=datetime.now().isoformat(timespec="seconds"),
        )

    def _state_payloads(self, client):
        base = self.mqtt.BASE_TOPIC + "/pump/clean/state"
        return [payload for topic, payload in client.published if topic == base]

    def test_switch_turns_off_when_a_run_ends_elsewhere(self):
        """The regression: nothing published the edge, so HA stayed ON."""
        client = FakeClient()
        # No active run, but the previous pass saw one -- i.e. the watchdog
        # stopped the pump and cleared the session between ticks.
        still_active = self.mqtt.sync_cleaning_state(client, was_active=True)

        self.assertFalse(still_active)
        self.assertIn("OFF", self._state_payloads(client))

    def test_active_run_keeps_publishing_the_countdown(self):
        self._fill_tank()
        cleaning.start(seconds=3600)
        self.addCleanup(lambda: cleaning.clear(cleaning.DONE_STOPPED))
        client = FakeClient()

        still_active = self.mqtt.sync_cleaning_state(client, was_active=True)

        self.assertTrue(still_active)
        self.assertIn("ON", self._state_payloads(client))

    def test_idle_tower_publishes_nothing(self):
        """Without this, every poll would spam the broker forever."""
        client = FakeClient()
        still_active = self.mqtt.sync_cleaning_state(client, was_active=False)
        self.assertFalse(still_active)
        self.assertEqual(client.published, [])

    def test_the_edge_is_published_exactly_once(self):
        """Then the tower goes quiet again, rather than repeating OFF."""
        client = FakeClient()
        active = self.mqtt.sync_cleaning_state(client, was_active=True)
        first = len(client.published)
        self.assertGreater(first, 0)

        self.mqtt.sync_cleaning_state(client, was_active=active)
        self.assertEqual(len(client.published), first)

    def test_a_run_this_service_ends_itself_is_not_double_published(self):
        """enforce_cleaning_deadline publishes its own stop."""
        client = FakeClient()
        with patch.object(self.mqtt, "enforce_cleaning_deadline", lambda c: True):
            with patch.object(self.mqtt, "publish_cleaning_state") as pub:
                self.assertFalse(self.mqtt.sync_cleaning_state(client, was_active=True))
                pub.assert_not_called()

    def test_remaining_and_result_are_published_on_the_edge(self):
        """The switch is the visible symptom; these were stale too."""
        cleaning.clear(cleaning.DONE_COMPLETED)
        client = FakeClient()
        self.mqtt.sync_cleaning_state(client, was_active=True)

        topics = {topic for topic, _ in client.published}
        base = self.mqtt.BASE_TOPIC + "/pump/clean/"
        self.assertIn(base + "remaining", topics)
        self.assertIn(base + "result", topics)
        remaining = [p for t, p in client.published if t == base + "remaining"]
        self.assertEqual(remaining, ["0"])


if __name__ == "__main__":
    unittest.main()

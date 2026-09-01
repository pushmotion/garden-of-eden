"""The MQTT service's pump cap must cover a pump it did not start.

config.py promises MAX_PUMP_RUN_SECONDS holds "no matter what a schedule, API
call, or CLI invocation requests". Only the command paths used to arm it, so a
pump energized by cron -- or by a `water.sh` killed before its EXIT trap could
run -- had nothing holding a deadline for it.

These drive the arming helpers directly rather than the reconcile loop, which is
an infinite `while True: ... sleep()`.
"""

import unittest


class PumpSafetyCapTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import mqtt

        cls.mqtt = mqtt

    def setUp(self):
        self.mqtt._cancel_pump_safety()
        self.addCleanup(self.mqtt._cancel_pump_safety)

    def _pending(self):
        with self.mqtt._pump_timer_lock:
            return self.mqtt._pump_safety_pending_locked()

    def test_observing_a_running_pump_arms_the_cap(self):
        """The whole point: a pump nobody told us about still gets a deadline."""
        self.assertFalse(self._pending())
        self.assertTrue(self.mqtt._ensure_pump_safety_armed())
        self.assertTrue(self._pending())

    def test_polling_does_not_extend_a_pending_deadline(self):
        """The way this fix could silently backfire.

        The reconcile loop runs every ACTUATOR_POLL_SECONDS. If each tick re-armed
        the timer, the deadline would slide forward forever and the cap would
        never fire -- strictly worse than not arming at all, because it would look
        covered. Only the first call may start a timer.
        """
        self.assertTrue(self.mqtt._ensure_pump_safety_armed())
        first = self.mqtt._pump_off_timer

        for _ in range(5):  # five more poll ticks
            self.assertFalse(
                self.mqtt._ensure_pump_safety_armed(),
                "a pending cap must not be re-armed by polling",
            )
        self.assertIs(self.mqtt._pump_off_timer, first, "the original timer must survive")

    def test_command_path_still_restarts_the_clock(self):
        """A fresh command is not an observation -- it may reset the deadline.

        Otherwise holding the speed slider would inherit the remainder of an
        earlier run's countdown.
        """
        self.mqtt._ensure_pump_safety_armed()
        first = self.mqtt._pump_off_timer
        self.mqtt._arm_pump_safety()
        self.assertIsNot(self.mqtt._pump_off_timer, first)
        self.assertTrue(self._pending())

    def test_cancel_clears_the_cap(self):
        self.mqtt._ensure_pump_safety_armed()
        self.mqtt._cancel_pump_safety()
        self.assertFalse(self._pending())
        # ...and the next observation is free to arm a fresh one.
        self.assertTrue(self.mqtt._ensure_pump_safety_armed())

    def test_a_fired_cap_does_not_block_re_arming(self):
        """If the auto-off failed to stop the pump, the next tick must re-arm.

        A fired timer is no longer pending, so the reconcile loop -- which checks
        every tick, not only when the duty cycle changes -- gets another chance.
        """
        self.mqtt._ensure_pump_safety_armed()
        timer = self.mqtt._pump_off_timer
        timer.cancel()
        timer.finished.set()  # stand in for "already ran"
        self.assertFalse(self._pending())
        self.assertTrue(self.mqtt._ensure_pump_safety_armed())


if __name__ == "__main__":
    unittest.main()

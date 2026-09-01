"""Home Assistant and the web UI must never disagree about the same tank.

Grounded in this tower's measured calibration rather than the config defaults,
because the defaults are precisely where the old bug hid: the web page derived
its fill bar from a hardcoded 5..20 cm tank, which happens to match config.py's
defaults, so the disagreement only appeared once a tower was actually calibrated.

    WATER_FULL_CM         4.81   airgap with the tank full
    WATER_EMPTY_CM       23.05   airgap with the tank empty
    TANK_CAPACITY_GALLONS 5.0
    WATER_LOW_CM          9.1    alert    (5.5" of water, ~76%)
    PUMP_CUTOFF_CM       12.9    interlock (4.0" of water, ~55.6%)

Against that tank the old hardcoded formula was wrong by 4-8 points and read a
full 0% while roughly 0.8 gallons remained.
"""

import unittest

from app.lib.water import tank_readings

FULL_CM = 4.81
EMPTY_CM = 23.05
CAPACITY = 5.0
ALERT_CM = 9.1
CUTOFF_CM = 12.9


# What the web page used to compute: a 5..20 cm tank, whatever the tower said.
def legacy_page_percent(cm):
    return max(0.0, min(100.0, (1 - (cm - 5) / 15) * 100))


class TankReadingsTestCase(unittest.TestCase):
    def readings(self, cm):
        return tank_readings(cm, FULL_CM, EMPTY_CM, CAPACITY)

    def test_full_tank_reads_full(self):
        r = self.readings(FULL_CM)
        self.assertAlmostEqual(100.0, r["percent"], places=1)
        self.assertAlmostEqual(CAPACITY, r["gallons"], places=1)

    def test_empty_tank_reads_empty(self):
        r = self.readings(EMPTY_CM)
        self.assertAlmostEqual(0.0, r["percent"], places=1)
        self.assertAlmostEqual(0.0, r["gallons"], places=1)

    def test_the_alert_threshold_is_where_we_measured_it(self):
        """9.1 cm was chosen as 5.5 inches of water, ~76% full."""
        self.assertAlmostEqual(76.5, self.readings(ALERT_CM)["percent"], delta=0.5)

    def test_the_cutoff_threshold_is_where_we_measured_it(self):
        """12.9 cm was chosen as 4.0 inches of water, ~55.6% full."""
        self.assertAlmostEqual(55.6, self.readings(CUTOFF_CM)["percent"], delta=0.5)

    def test_the_cutoff_still_leaves_water_above_the_pump(self):
        """The whole point of 4 inches: the pump must not be drawing air."""
        self.assertGreater(self.readings(CUTOFF_CM)["gallons"], 2.0)

    def test_depth_is_measured_up_from_the_floor(self):
        self.assertAlmostEqual(EMPTY_CM - CUTOFF_CM, self.readings(CUTOFF_CM)["depth_cm"], places=2)

    def test_overfill_is_reported_not_hidden(self):
        """Depth is clamped at zero only. A tank past the full mark says so."""
        r = self.readings(FULL_CM - 2)
        self.assertGreater(r["depth_cm"], EMPTY_CM - FULL_CM)
        self.assertAlmostEqual(100.0, r["percent"], places=1)  # percent still clamps

    def test_a_failed_reading_derives_nothing(self):
        r = tank_readings(None, FULL_CM, EMPTY_CM, CAPACITY)
        self.assertIsNone(r["percent"])
        self.assertIsNone(r["gallons"])
        self.assertIsNone(r["depth_cm"])

    def test_an_inverted_calibration_derives_nothing_rather_than_lying(self):
        r = tank_readings(10.0, EMPTY_CM, FULL_CM, CAPACITY)
        self.assertIsNone(r["percent"])
        self.assertEqual(10.0, r["level_cm"])  # the raw airgap is still true


class LegacyDisagreementTestCase(unittest.TestCase):
    """The regression this centralization exists to prevent.

    These assert the *old* behaviour was wrong, so that reintroducing a private
    copy of the tank geometry in any client fails here rather than in the field.
    """

    def test_the_old_page_formula_disagreed_at_the_cutoff(self):
        real = tank_readings(CUTOFF_CM, FULL_CM, EMPTY_CM, CAPACITY)["percent"]
        self.assertGreater(abs(real - legacy_page_percent(CUTOFF_CM)), 5.0)

    def test_the_old_page_formula_showed_empty_with_gallons_left(self):
        cm = 20.0
        self.assertEqual(0.0, legacy_page_percent(cm))
        self.assertGreater(tank_readings(cm, FULL_CM, EMPTY_CM, CAPACITY)["gallons"], 0.5)

    def test_the_two_agree_only_on_the_default_calibration(self):
        """Why this went unnoticed: on 5/20 the old formula was exactly right."""
        for cm in (5.0, 8.0, 12.5, 20.0):
            with self.subTest(cm=cm):
                self.assertAlmostEqual(
                    legacy_page_percent(cm),
                    tank_readings(cm, 5.0, 20.0, CAPACITY)["percent"],
                    places=6,
                )


if __name__ == "__main__":
    unittest.main()

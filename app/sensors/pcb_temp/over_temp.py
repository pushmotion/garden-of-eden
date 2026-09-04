"""Hardware over-temperature alert on the PCT2075's OS output pin.

The PCT2075 that reports PCB temperature also carries a comparator output (OS)
wired to ``OVER_TEMP_ALERT_PIN``. The chip drives that pin itself: once the
programmed threshold is crossed it asserts with no software in the loop, so the
alert still fires if the service that would otherwise notice has hung. That is
the whole reason to prefer it over a polling loop.

**This measures the carrier PCB, not the SoC.** Across three days on a live
tower the SoC ran 8.0-13.9 C hotter than this chip (mean 10.9), so the offset is
real and it varies with what is generating the heat. Treat the threshold as
"the board is far hotter than it has any business being", not as a precise
guard on the processor -- Raspberry Pi's own firmware already throttles the SoC
at 85 C, and states plainly that throttling causes no harm.

Polarity: the chip powers up **active-low** and open-drain, and this module
keeps it that way. The bench script this replaces inverted it to active-high,
which is worse in two ways -- a crashed or half-configured process leaves the
pin reading the opposite of what any other reader assumes, and an unpowered or
disconnected chip then reads as "fine" rather than failing loud.
"""

import logging
import os
import sys

import adafruit_pct2075
import board
from gpiozero import Button

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))
import config

logger = logging.getLogger(__name__)

# The chip's own power-on default, kept deliberately. See the module docstring.
ALERT_ACTIVE_HIGH = False

# gpiozero's pigpio backend rejects anything above 0.3 s ("bounce must be
# between 0 and 0.3"), and it raises at construction -- found the hard way on
# real hardware with 0.5. Matches the physical button's value. The comparator
# output is a clean level from a chip rather than a mechanical contact, so this
# is only guarding against noise on the line, not real bounce.
ALERT_BOUNCE_SECONDS = 0.2
MAX_PIGPIO_BOUNCE_SECONDS = 0.3


def usable_thresholds(high_c, hysteresis_c):
    """Validate a (trip, clear) pair, or return ``None`` to leave the chip alone.

    The clear point has to sit *below* the trip point, or the comparator has no
    band to sit in and the pin chatters on every reading. An inverted or equal
    pair is a configuration mistake, and the safe response is to keep whatever
    the chip already had -- its 80/75 C power-on default is at least coherent --
    rather than program something that produces a stream of false alerts.

    Both unset also means "leave it alone", which is how a tower that has not
    chosen thresholds keeps the factory ones.
    """
    if not high_c or not hysteresis_c:
        return None
    if hysteresis_c >= high_c:
        logger.error(
            "Refusing over-temp thresholds %.1f/%.1f C: the clear point must be "
            "below the trip point. Leaving the chip at its current settings.",
            high_c,
            hysteresis_c,
        )
        return None
    return float(high_c), float(hysteresis_c)


def configure(high_c=None, hysteresis_c=None, address=None):
    """Program the comparator and return what the chip reads back.

    Returns ``(trip_c, clear_c, active_high)`` as read from the device, so the
    caller can log what the hardware actually accepted rather than what it was
    asked for -- the registers quantise to the part's resolution.
    """
    high_c = config.OVER_TEMP_HIGH if high_c is None else high_c
    hysteresis_c = config.OVER_TEMP_HYSTERESIS if hysteresis_c is None else hysteresis_c
    address = config.PCB_TEMP_ADDRESS if address is None else address

    pct = adafruit_pct2075.PCT2075(board.I2C(), address=address)

    wanted = usable_thresholds(high_c, hysteresis_c)
    if wanted is not None:
        pct.high_temperature_threshold, pct.temperature_hysteresis = wanted
        pct.high_temp_active_high = ALERT_ACTIVE_HIGH

    return (
        pct.high_temperature_threshold,
        pct.temperature_hysteresis,
        pct.high_temp_active_high,
    )


def alert_pin(pin=None, pin_factory=None):
    """A ``Button`` on the alert pin whose ``is_pressed`` means "over temperature".

    ``pull_up`` follows ``ALERT_ACTIVE_HIGH`` so the mapping holds either way:
    with the chip's active-low default the pin is pulled high when idle and the
    chip sinks it on alert, so ``pull_up=True`` makes "asserted" and "pressed"
    the same thing. Callers never have to reason about the polarity.
    """
    pin = config.OVER_TEMP_ALERT_PIN if pin is None else pin
    return Button(
        pin,
        pull_up=not ALERT_ACTIVE_HIGH,
        pin_factory=pin_factory,
        bounce_time=ALERT_BOUNCE_SECONDS,
    )


if __name__ == "__main__":
    # Standalone: report what the chip is configured to do and whether it is
    # asserting right now. Read-only apart from applying the configured
    # thresholds, and safe to run alongside mqtt.service -- unlike the
    # ultrasonic sensor, a second reader on the I2C bus does not corrupt
    # anything. Deliberately not a loop: the service owns the alert.
    from app.lib.hardware import get_pin_factory
    from app.lib.logging_config import configure_logging
    from app.sensors.pcb_temp.pcb_temp import get_pcb_temperature

    configure_logging()
    try:
        trip, clear, active_high = configure()
        print(f"PCB temperature   {get_pcb_temperature():.2f} C")
        print(f"alert trips at    {trip:.1f} C")
        print(f"alert clears at   {clear:.1f} C")
        print(f"polarity          {'active-high' if active_high else 'active-low'}")
        alert = alert_pin(pin_factory=get_pin_factory())
        print(f"alerting now?     {'YES' if alert.is_pressed else 'no'}")
        alert.close()
    except Exception as exc:
        print(f"Error: {exc}")
    except KeyboardInterrupt:
        print("Script interrupted.")

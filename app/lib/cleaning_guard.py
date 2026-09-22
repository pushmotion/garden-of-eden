"""Is a cleaning run in progress? For the cron/CLI watering path.

    python -m app.lib.cleaning_guard    # exit 0 = cleaning, 1 = not cleaning

``bin/water.sh`` asks this twice, for two different reasons:

1. Before starting, so a scheduled watering run does not fight a cleaning run
   for the pump.
2. In its ``EXIT`` trap, which otherwise turns the pump off unconditionally --
   so a three-minute cron watering that happened to land mid-clean would end a
   two-hour cleaning cycle at its own three-minute mark, silently.

(2) is the reason this exists as a guard rather than as a plain early exit: the
trap fires on *every* exit, including the early one, so skipping the run is not
by itself enough to leave the pump alone.

Failure fails *open toward watering*: any error here reports "not cleaning", so
the scheduled run proceeds. Getting it wrong in that direction costs a cleaning
cycle somebody can restart; getting it wrong in the other direction silently
suppresses watering for as long as the bad state persists, which costs plants.
"""

import sys

CLEANING, NOT_CLEANING = 0, 1


def main(argv=None):
    try:
        from app.lib import cleaning as cleaning_lib

        active = cleaning_lib.session()
    except Exception as exc:  # noqa: BLE001 - deliberately broad; see module docstring
        print(f"cleaning guard failed ({exc!r}); assuming no cleaning run", file=sys.stderr)
        return NOT_CLEANING

    if active is None:
        return NOT_CLEANING

    print(f"cleaning run in progress, {active['remaining_seconds']}s remaining")
    return CLEANING


if __name__ == "__main__":
    sys.exit(main())

# Fleet readiness and rollout

The production build is `feat/gardyn-tower-local`. Keep `main` as the upstream
mirror. The review branch includes the separate cleaning feature and fleet fixes;
merging to the production branch makes code eligible for tower auto-updates.

## Dashboard views

MQTT discovery creates entities and device entries, not this project's custom
dashboard. Missing tower sections with existing entities require dashboard setup.
Generate views using each tower's actual `MQTT_IDENTIFIER`:

```bash
python bin/ha-dashboard.py <identifier-1> <identifier-2> <identifier-3> --output fleet-dashboard.yaml
```

The generator runs off-Pi, requires no HA credentials and includes all 47 discovery
entities per tower. Create a new HA dashboard and import the generated YAML in
its raw configuration editor. Back up an existing dashboard before editing it;
replacing its raw configuration replaces all its views. Verify entity IDs against
the registry first, particularly for older devices. Discovery's `default_entity_id`
does not rename existing entities. Use `bin/ha-align-entity-ids.py` in its default
dry-run mode to review mismatches. Do not delete devices to force naming changes.

## Cleaning contract

Cleaning is for an empty tower without plants, before or between grows. It uses
a separate bounded pump duration while suspending normal watering and lighting
schedules. Manual lighting remains available. Existing schedules and normal pump
speed are preserved. A fade already running is cancelled at its next step.
When cleaning ends, lighting adopts the current scheduled setting; with no enabled
lighting schedule, the manual setting remains. Missed watering is not replayed.

The configured cleaning cutoff and fresh MQTT water readings are required.
Cleaning does not bypass protection against running the pump dry. Calibrate each
tower's empty/full readings and verify minimum depth against its own intake.
Starting cleaning must persist its deadline before energizing the pump. A restart
may resume only a still-valid session with fresh water data. Old timers and
watchdogs cannot stop or restart a replacement session.

The saved cron entries must be refreshed when deploying this version so lighting
commands carry `--scheduled`. The new updater does this automatically. For a
controlled initial deployment, run from the repository after activating the code:

```bash
venv/bin/python -c 'from app.sensors.schedule import schedule; schedule.refresh()'
```

## Deployment order

1. Validate the candidate off-Pi. Do not run the test suite on tower hardware.
2. Use the assembled, plant-free tower for the first supervised deployment.
   Verify its unique identity, broker connectivity, hardware and water readings
   before attempting cleaning. Check cameras independently; absent hardware does
   not become available through a software update.
3. Verify the newly provisioned tower independently when powered. Never copy
   another tower's water calibration or identity.
4. Update the growing tower after the empty-tower checks succeed, at a planned
   time outside watering. Confirm lighting and the next watering event afterward.

Setup now requires explicit `MQTT_IDENTIFIER` in `.env` and `GARDEN_HOSTNAME` in
the setup invocation. It checks broker/identity and sensor configuration before
starting services. Existing systemd units need the updated setup applied during
the maintenance window to gain boot-indicator ordering; ordinary updates do not
regenerate those units. Setup starts services, so schedule that operation deliberately.

The updater accepts only the production branch and an exact successful push-CI
revision, or an explicitly supplied `GARDEN_APPROVED_REV` matching that SHA.
It defers during cleaning, refuses tracked local edits, and aborts code activation
and restarts on dependency-install failure. Dependency installation uses the
existing venv, so a failed partial install can still require dependency repair.
A failed service restart leaves `.update-pending` for retry. The first deployment
still uses the previously installed updater unless performed manually.

`/health` identifies HTTP-process health only. `/health` and `/system` include
revision, water-reading freshness and camera-path presence. Those are diagnostic
signals, not proof of camera capture, MQTT delivery or physical water circulation.
Verify those behaviors on each tower during commissioning.

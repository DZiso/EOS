# Predictive heating in this fork

This optional extension plans integer thermostat targets for air-to-air heat
pumps. It uses EOS weather/PV forecasts, time-of-use electricity prices, coupled
room temperatures, shared outdoor compressors and a passive AC battery.
Gas heating is unavailable. No new measurement hardware is required.

Base: upstream `v0.4.0rc3`, commit
`525dad182564031529b8930feb883b37788158f4`. The existing GENETIC electrical
optimizer is unchanged. Heating has its own bounded deterministic local search
over thermostat schedules. It does not claim a globally optimal solution.

## Installation

Add `https://github.com/DZiso/EOS#feat/predictive-heating` as a Home Assistant app
repository and install **EOS Predictive Heating**. Leave automatic app updates
disabled during evaluation. The app builds this fork from source. API ports are
unpublished by default; EOSdash uses Home Assistant ingress.

App options: `heating_enabled` and `heating_config`. The latter is a JSON string
containing `HeatingSettings` (see the API schema/source). Use `mode: "observe"`
initially. Credentials come from the Supervisor environment, never from that JSON.

Example for two rooms sharing an outdoor unit:

```json
{
  "mode": "observe",
  "rooms": [
    {"id": "living", "climate_entity": "climate.living",
     "temperature_entity": "sensor.living_temperature", "min_c": 19, "max_c": 22},
    {"id": "office", "climate_entity": "climate.office",
     "temperature_entity": "sensor.office_temperature", "min_c": 19, "max_c": 22}
  ],
  "compressors": [{"id": "outdoor", "rooms": ["living", "office"]}],
  "couplings": [{"a": "living", "b": "office", "kw_per_k": 0.15}],
  "day_price_per_kwh": 0.30,
  "night_price_per_kwh": 0.18,
  "cheap_start_hour": 0,
  "cheap_end_hour": 5,
  "quiet_start_hour": 22,
  "quiet_end_hour": 7
}
```

Optional `solar_planes` initializes a new installation with BrightSky and
Forecast.Solar. Each plane has `surface_tilt` (degrees), `surface_azimuth`
(north=0, east=90), and `peakpower` (kWp). Coordinates come from the existing
Home Assistant configuration. Initialization happens once; subsequent EOS UI
configuration edits are preserved. Otherwise configure providers in EOS normally.

Standalone installations set `EOS_HEATING_CONFIG` to a settings JSON file,
`EOS_HEATING_HA_URL` to the HA API base including `/api`, and
`EOS_HEATING_HA_TOKEN` to an existing token. Disabled installations perform no
heating network work.

## Model and limitations

- Heat capacity, losses and energy-conserving exchange between connected rooms.
- Shared outdoor heating/electrical limits, estimated quiet derating, cycling
  losses, standby input and cold-weather/defrost COP reduction.
- Integer targets held for 30 or 60 minutes; 15-minute electricity slots and
  five-minute thermal integration.
- Passive battery surplus charging and self-consumption discharge, with separate
  PV and battery limits, SoC bounds and conversion losses. No battery commands.
- Import cost, foregone export revenue, and terminal values for remaining warmth
  and stored electricity. Comfort has lexicographic priority over cost.
- UTC simulation instants and local tariff/quiet windows, including DST changes.

Initial thermal parameters, COP and quiet derating are **unvalidated priors**.
Outputs are estimates, not measured efficiency or verified savings. The configured
household baseline must exclude heating. Unverified indoor-head electricity
counters must not be summed as independent meters of shared compressors.
Unverified battery limits are flagged. Changing SoC ceilings can be supplied per
slot through the API; the live adapter currently holds the observed/configured
ceiling over its horizon. This release does not automatically fit or certify COP.

## Observation and execution

Observation records only configured telemetry once per minute, retaining 30 days
of daily JSONL files under `/data/heating`, and plans every 15 minutes. Source
timestamps distinguish unchanged values from fresh readings. Missing/stale room
temperatures, forecasts or SoC block a new plan; missing SoC never means zero.
The Heating tab displays freshness, targets, predictions, estimates and warnings.

Observation never calls climate services. An unapplied proposed schedule cannot
validate a model against actual temperatures; associated residuals are explicitly
labeled as unapplied-schedule residuals. Review actual heating periods and inputs
before validating thermal parameters.

Control requires `mode: "control"`, `validated_model: true`, and a configured
`control_switch_entity` (HA input boolean) currently on. It also requires usable
room temperatures, Heat mode, integer targets and Quiet support during quiet hours.
Only the current part of a fresh feasible plan is applied. Failed/stale/infeasible
plans use the configured comfort fallback when room readings remain usable;
otherwise existing device targets are retained. No mains switching or invented
compressor commands. Quiet is applied without conflicting fan/sleep commands.

Manual changes suspend control for four hours by default. Ownership, target-hold
times and overrides persist across restarts. Turning the helper off releases
ownership. Two minutes are allowed for cloud acknowledgements; subsequent
unexpected device state is treated as an override.

The executor waits up to 30 seconds for HA to acknowledge each command before
sending the next dependent command. Mode changes reapply the preset and target:
Samsung devices can restore old settings when switching modes. A missing
acknowledgement stops the batch without blind retries. Status distinguishes sent,
confirmed and attempted commands; a request timeout leaves the sent count unknown.
Temperature freshness is checked on every observation, including between plans.

An AC's internal temperature can differ substantially from the occupied-room
sensor. Check this during commissioning; a matching numerical setpoint alone
does not validate room comfort or the thermal model.

Rollback: stop the app or disable `heating_enabled`. Observation has no device
actions to undo. In control mode, disable its helper and restore the desired
normal thermostat targets. Existing solar/battery automations are not modified.

## API and tests

- `GET /v1/heating/status`: configuration and runtime status.
- `GET /v1/heating/plan`: last plan with generation timestamp.
- `POST /v1/heating/plan`: calculate from `{settings, inputs}` without storing or
  executing commands. Prices are currency per **kWh**, unlike GENETIC's per-Wh
  API. Powers are **kW**, energies **kWh**, temperatures **degrees Celsius**.

Tests cover heat conservation, shared capacity, battery balance, quiet operation,
tariff/DST boundaries, missing inputs, comfort infeasibility, observation mode,
manual overrides and the API:

```sh
uv run --locked --extra dev pytest -q tests/test_heating.py tests/test_heating_runtime.py
```

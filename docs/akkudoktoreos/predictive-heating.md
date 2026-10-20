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
temperatures or weather forecasts block a new plan. Uncertain battery SoC or
configured battery limits select solar-only planning when the independently
validated PV forecast covers the horizon: tariffs and direct PV consumption still
count, but battery contribution and export revenue do not. Surplus PV has an unknown
destination and is reported as `unallocated_pv_kwh`, not credited as storage or export.
If that solar forecast is also unavailable, stale or incomplete, tariff-only
planning prices all electricity as grid imports and retries PV every five minutes.
Missing SoC stays unknown; it is never rewritten as a measured zero. A fresh,
valid battery report automatically restores full planning on the next control
cycle, without waiting for the normal planning interval. Mode changes invalidate
plans with different energy assumptions. Room comfort and quiet hours still apply.
The Heating tab displays freshness, targets, predictions, estimates and warnings.

**Follow-up: monitor Zendure connection history during daylight and solar
charging for intermittent disconnections.** Zendure can suppress unchanged HA
sensor writes, so an old charge value can mean either idle operation or lost
communication. Planning without battery credit must not hide genuine daytime outages.
Runtime status and observation logs retain the battery source timestamp, age,
last reported value, uncertainty reason and selected planning mode. Compare these
with Zendure's connection history; its status codes also represent operating
modes and must not be treated as a simple online/offline flag.
Configure `battery.connection_entity` and `battery.connection_healthy_states`
to veto battery credit immediately on an explicit unhealthy connection, even
when SoC is recent. For the inspected Zendure integration the healthy codes are
`["1", "2", "3", "10", "11", "12"]`; `0` denotes unavailable/unknown, while
`2` and `3` are operating modes. Recheck these mappings after integration changes.
An unchanged healthy mode is not a heartbeat and never renews the SoC timestamp.
Connection evidence is retained in status and observation logs. Battery recovery
replans on the next cycle; an outage retains direct PV planning when available.

Observation never calls climate services. A proposed schedule cannot validate a
model against actual temperatures unless its actions and disturbances match the
observed run. Logs retain actual command outcomes separately from model estimates.

Basic comfort control requires `mode: "control"` and a configured
`control_switch_entity` (HA input boolean) currently on. It does not need a COP or
thermal model. Each room uses `fallback_c` as its normal room target, bounded by
the actual comfort limits. Missing forecasts or a missing sensor in another room do not
stop a healthy room's thermostat.

For native AC thermostat control, use `room_feedback: false`, set
`temperature_entity` to the AC's climate entity and `temperature_attribute` to
`current_temperature`. The automatic setpoint stays inside the comfort range;
no external sensor correction is added. A fresh accepted plan's current target
is applied on the next control tick, including when the previous target changed
recently. `block_minutes` sets the planner's target blocks; it does not impose
an additional controller hold. Unchanged targets do not resend commands.
Manual overrides, command acknowledgement and the ten-minute minimum off time
still take priority.

A room can have a separate `night_comfort`, for example
`{"min_c": 18, "max_c": 20, "fallback_c": 19}`. This applies during
`quiet_start_hour`–`quiet_end_hour` in the configured timezone (including daylight
saving changes). Outside that window the room's usual limits and fallback apply.
Both the planner and controller use these limits, including when forecasts or
temperature reports are missing. The planner splits blocks at the transition;
an old daytime target hold cannot delay the night limit. Manual overrides retain
their configured expiry across the transition.

Set `room_feedback: true` for an occupied-room sensor below the indoor head. The
controller seeds a bounded AC setpoint offset from the head/room difference, then
adjusts it by at most 0.5°C after a new room report and a 30-minute held demand.
The default offset limit is ±5°C. Repeated polling of one report never accumulates
corrections. The dashboard shows room targets and AC setpoints separately. A
room near its upper comfort boundary pauses heating; restarting has a ten-minute
minimum off time. An off command never includes preset or temperature commands,
because some Samsung devices turn on when those settings are written while off.

`temperature_max_age_seconds` can reflect each source's actual report cadence;
the real source timestamp and age remain visible. Optional `availability_entity`
requires an integration connection sensor to be on. If a room reading is lost,
an already-owned unit returns to its uncorrected native `fallback_c` thermostat
target, respecting minimum off time and manual overrides. Without ownership, an
external-sensor room is left alone. Rooms with `room_feedback: false` use the AC's
own thermostat, including when remote temperature reports stop. They stay in Heat
at their target instead of being switched off by rounded AC temperature reports.
This cannot guarantee occupied-room comfort during a sensor outage.

To apply EOS schedules, set `optimization_enabled: true` and either provide a
validated model or explicitly set `allow_estimated_model_control: true` for a
trial. The latter keeps `validated_model: false` and all estimate warnings.
Fresh feasible schedules choose bounded room targets; the comfort controller
translates them to AC setpoints. Missing/stale/infeasible plans return to basic
comfort control. Cached forecasts with missing retrieval metadata trigger a real
refresh, limited to once per 15 minutes; cached data is never relabelled fresh.

The planner's `comfort_margin_c` is an interior preference costing €0.01 per
degree-hour, not an additional hard limit. A configured 18°C minimum can therefore
produce an 18°C AC target. Actual lower comfort limits remain hard constraints,
including when heating capacity is insufficient.

Heat-only ACs cannot instantly cool an already warm room or meet a lower night
ceiling at the transition. The planner simulates minimum permitted targets as a
reference, with the same shared compressors and room couplings. At every
five-minute step, a schedule may inherit excess warmth only up to that reference;
it may not increase or prolong it. Such an accepted plan has
`comfort_status: "recovering"`, lists `recovering_rooms`, and still reports the
full `comfort_violation_degree_hours`. Its
`avoidable_comfort_violation_degree_hours` must be zero within numerical tolerance.
This is a modelled recovery, not a guarantee that actual rooms are comfortable.
The dashboard distinguishes accepted recovery from rejected schedules. Native
thermostats continue controlling heat; rounded head temperatures do not trigger
repeated off/on cycling.

Manual changes suspend that room's control for four hours by default. Ownership,
feedback offsets, external-feedback hold times and overrides persist across app restarts.
Turning the HA helper off releases ownership and stops further commands; it does
not turn the ACs off. The executor checks the helper before every command.

The executor waits up to 30 seconds for HA to acknowledge each command before
sending the next dependent command. Mode changes reapply the preset and target:
Samsung devices can restore old settings when switching modes. A missing
acknowledgement stops that room's batch without blind retries; other rooms still
run. Status distinguishes sent, confirmed and attempted commands; a request
timeout leaves the sent count unknown. Two minutes are allowed for final cloud
state convergence; later unexpected state is treated as a manual override.
Quiet is applied without conflicting fan/sleep commands. No gas, mains-switching
or battery commands are sent.

Rollback: stop the app or disable `heating_enabled`. Observation has no device
actions to undo. In control mode, disable its helper and restore the desired
normal thermostat targets. Existing solar/battery automations are not modified.

## API and tests

- `GET /v1/heating/status`: configuration and runtime status.
- `GET /v1/heating/plan`: last plan with generation timestamp.
- `GET /v1/heating/observations?day=YYYY-MM-DD&limit=120`: bounded UTC-day
  telemetry with source age, room decisions and actual command outcomes.
- `POST /v1/heating/plan`: calculate from `{settings, inputs}` without storing or
  executing commands. Prices are currency per **kWh**, unlike GENETIC's per-Wh
  API. Powers are **kW**, energies **kWh**, temperatures **degrees Celsius**.

Tests cover heat conservation, shared capacity, battery balance, quiet operation,
tariff/DST boundaries, missing inputs, comfort infeasibility, observation mode,
manual overrides and the API:

```sh
uv run --locked --extra dev pytest -q tests/test_heating*.py
```

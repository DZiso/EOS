"""Behavioral tests for heating physics, costs, execution and failure recovery."""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from akkudoktoreos.heating.controller import commands, read_number, read_temperatures
from akkudoktoreos.heating.models import (
    Battery,
    ComfortRange,
    Compressor,
    Coupling,
    ForecastSlot,
    HeatingSettings,
    PlanningInput,
    Room,
)
from akkudoktoreos.heating.planner import (
    comfort_at,
    control_blocks,
    plan,
    quiet_at,
    simulate,
    tariff_at,
)
from akkudoktoreos.heating.runtime import interpolate
from akkudoktoreos.server.rest.heating import router


def config(**overrides):
    values: dict[str, Any] = dict(
        enabled=True,
        horizon_hours=6,
        rooms=[
            Room(id="living", climate_entity="climate.living", temperature_entity="sensor.living"),
            Room(id="office", climate_entity="climate.office", temperature_entity="sensor.office"),
        ],
        compressors=[Compressor(id="outdoor", rooms=["living", "office"])],
        couplings=[Coupling(a="living", b="office", kw_per_k=0.15)],
    )
    values.update(overrides)
    return HeatingSettings(**values)


def inputs(cfg, at=None, outside=5, pv=0, temps=None, soc=None):
    at = at or datetime(2026, 1, 15, 1, tzinfo=timezone.utc)
    return PlanningInput(
        generated_at=at,
        temperatures_c=temps or {r.id: 20.0 for r in cfg.rooms},
        battery_soc=soc,
        slots=[
            ForecastSlot(
                at=at + timedelta(minutes=15 * i),
                outside_c=outside,
                pv_kw=pv,
                baseline_kw=0.3,
                import_price_per_kwh=tariff_at(cfg, at + timedelta(minutes=15 * i)),
                export_price_per_kwh=cfg.export_price_per_kwh,
            )
            for i in range(cfg.horizon_hours * 4)
        ],
    )


def states(cfg, now):
    result: dict[str, dict[str, Any]] = {"input_boolean.heating": {"state": "on"}}
    for room in cfg.rooms:
        result[room.temperature_entity] = {"state": "20", "last_reported": now.isoformat()}
        result[room.climate_entity] = {
            "state": "heat",
            "attributes": {
                "temperature": 20,
                "preset_mode": "none",
                "hvac_modes": ["off", "heat", "cool"],
                "preset_modes": ["none", "quiet"],
                "target_temp_step": 1,
            },
        }
    return result


def controlled():
    return config(
        mode="control", validated_model=True, control_switch_entity="input_boolean.heating"
    )


def test_room_exchange_conserves_energy():
    cfg = config()
    for room in cfg.rooms:
        room.loss_kw_per_k = 0
    data = inputs(cfg, temps={"living": 22, "office": 18})
    result = simulate(cfg, data, [[0, 0]] * 6, True)
    final = result["slots"][-1]["temperatures_c"]
    assert final["living"] < 22 and final["office"] > 18
    assert sum(final.values()) == pytest.approx(40)


def test_shared_compressor_capacity_is_not_counted_twice():
    cfg = config()
    cfg.compressors[0].max_heat_kw = 2
    cfg.compressors[0].quiet_capacity_factor = 1
    for room in cfg.rooms:
        room.loss_kw_per_k = 0
    data = inputs(cfg, temps={"living": 10, "office": 10})
    result = simulate(cfg, data, [[22, 22]] * 6, True)
    end = result["slots"][0]["temperatures_c"]
    heat = sum((end[r.id] - 10) * r.capacity_kwh_per_k for r in cfg.rooms)
    assert heat == pytest.approx(2 * 0.25)
    assert sum(result["slots"][0]["compressor_kwh"].values()) == result["slots"][0]["heating_kwh"]


def test_quiet_capacity_reduces_heat_without_exceeding_power_limit():
    cfg = config()
    data = inputs(cfg, temps={"living": 10, "office": 10})
    quiet = simulate(cfg, data, [[22, 22]] * 6, True)
    cfg.compressors[0].quiet_capacity_factor = 1
    loud = simulate(cfg, data, [[22, 22]] * 6, True)
    assert (
        quiet["slots"][0]["temperatures_c"]["living"] < loud["slots"][0]["temperatures_c"]["living"]
    )
    assert all(
        s["heating_kwh"] <= 0.25 * cfg.compressors[0].max_electric_kw for s in quiet["slots"]
    )


def test_passive_battery_balance_and_limits():
    cfg = config(battery=Battery(max_soc=0.8, charge_kw=1.2, discharge_kw=0.2))
    data = inputs(cfg, pv=3, soc=0.5)
    output = simulate(cfg, data, [[20, 20]] * 6, True)
    for slot in output["slots"]:
        assert slot["battery_soc"] <= 0.8 + 1e-9
        assert slot["battery_charge_kwh"] <= 0.3
        assert slot["grid_import_kwh"] - slot["grid_export_kwh"] == pytest.approx(
            (0.3 - 3) * 0.25
            + slot["heating_kwh"]
            + slot["battery_charge_kwh"]
            - slot["battery_discharge_kwh"]
        )
    data = inputs(cfg, pv=0, soc=0.8)
    output = simulate(cfg, data, [[20, 20]] * 6, True)
    assert all(s["battery_discharge_kwh"] <= 0.05 for s in output["slots"])
    assert all(s["battery_soc"] >= 0.1 - 1e-9 for s in output["slots"])


def test_lowered_soc_ceiling_does_not_force_discharge():
    cfg = config(battery=Battery(max_soc=0.8))
    result = simulate(cfg, inputs(cfg, pv=5, soc=1), [[20, 20]] * 6, True)
    assert result["slots"][0]["battery_soc"] == 1
    assert result["slots"][0]["battery_charge_kwh"] == 0


def test_missing_soc_does_not_mean_empty():
    cfg = config(battery=Battery())
    with pytest.raises(ValueError, match="SoC"):
        plan(cfg, inputs(cfg))


def test_tariff_only_plan_ignores_solar_and_battery_credit_even_when_supplied():
    cfg = config(battery=Battery())
    data = inputs(cfg, pv=20, soc=0.8)
    data.energy_mode = "tariff_only"
    result = plan(cfg, data)
    assert result["energy_mode"] == "tariff_only"
    for slot in result["slots"]:
        assert slot["battery_soc"] is None
        assert slot["battery_charge_kwh"] == slot["battery_discharge_kwh"] == slot["grid_export_kwh"] == 0
        assert slot["grid_import_kwh"] == pytest.approx(0.3*0.25 + slot["heating_kwh"])


@pytest.mark.parametrize("pv", [0, 0.4, 20])
def test_solar_only_uses_direct_pv_without_battery_or_export_credit(pv):
    cfg = config(battery=Battery())
    data = inputs(cfg, pv=pv, soc=0.8)
    data.energy_mode = "solar_only"
    result = plan(cfg, data)
    assert result["energy_mode"] == "solar_only"
    expected_cost = 0.0
    for slot, source in zip(result["slots"], data.slots):
        assert slot["battery_soc"] is None
        assert slot["battery_charge_kwh"] == slot["battery_discharge_kwh"] == slot["grid_export_kwh"] == 0
        net = (source.baseline_kw - source.pv_kw) * 0.25 + slot["heating_kwh"]
        assert slot["grid_import_kwh"] == pytest.approx(max(0, net))
        assert slot["unallocated_pv_kwh"] == pytest.approx(max(0, -net))
        expected_cost += max(0, net) * source.import_price_per_kwh
    assert result["estimated_cost"] == pytest.approx(expected_cost)


def test_solar_only_changes_schedule_to_use_sun_and_lowers_grid_import():
    cfg = config(battery=Battery(), horizon_hours=12)
    data = inputs(cfg, at=datetime(2026, 1, 15, 7, tzinfo=timezone.utc), outside=8)
    data.energy_mode = "solar_only"
    sunny = data.model_copy(deep=True)
    for slot in sunny.slots[8:24]:
        slot.pv_kw = 4
    with_sun, without_sun = plan(cfg, sunny), plan(cfg, data)
    assert with_sun["feasible"] and without_sun["feasible"]
    assert with_sun["estimated_cost"] < without_sun["estimated_cost"]
    assert sum(s["grid_import_kwh"] for s in with_sun["slots"]) < sum(s["grid_import_kwh"] for s in without_sun["slots"])
    assert [s["targets_c"] for s in with_sun["slots"]] != [s["targets_c"] for s in without_sun["slots"]]


@pytest.mark.parametrize(
    "stamp,cheap",
    [
        ("2026-10-24T22:00:00+00:00", True),
        ("2026-10-25T00:30:00+00:00", True),
        ("2026-10-25T01:30:00+00:00", True),
        ("2026-10-25T04:00:00+00:00", False),
        ("2026-03-29T00:30:00+00:00", True),
        ("2026-03-29T03:00:00+00:00", False),
    ],
)
def test_tariff_uses_berlin_wall_clock_across_dst(stamp, cheap):
    cfg = config()
    assert tariff_at(cfg, datetime.fromisoformat(stamp)) == (0.18 if cheap else 0.30)


def test_dst_horizon_contains_real_quarter_hours():
    cfg = config(horizon_hours=24)
    data = inputs(cfg, datetime(2026, 10, 24, 22, tzinfo=timezone.utc))
    assert len(data.slots) == 96
    assert data.slots[-1].at.timestamp() - data.slots[0].at.timestamp() == 95 * 900


def test_incomplete_forecast_is_rejected():
    cfg = config()
    data = inputs(cfg)
    data.slots.pop()
    with pytest.raises(ValueError, match="Forecast length"):
        plan(cfg, data)


def test_non_contiguous_forecast_is_rejected():
    payload = inputs(config()).model_dump()
    payload["slots"][2]["at"] += timedelta(minutes=15)
    with pytest.raises(ValidationError, match="contiguous"):
        PlanningInput.model_validate(payload)


def test_unknown_and_duplicate_compressor_assignments_are_rejected():
    cfg = config().model_dump()
    cfg["compressors"][0]["rooms"] = ["living", "living"]
    with pytest.raises(ValidationError, match="exactly one"):
        HeatingSettings.model_validate(cfg)


def test_optimizer_preserves_feasible_comfort_and_integer_targets():
    cfg = config()
    output = plan(cfg, inputs(cfg))
    assert output["feasible"]
    assert output["comfort_violation_degree_hours"] == 0
    assert all(
        19 <= t <= 22 and int(t) == t for s in output["slots"] for t in s["targets_c"].values()
    )
    assert all(s["quiet"] for s in output["slots"][:16])


def test_infeasible_cold_weather_is_reported():
    cfg = config()
    cfg.compressors[0].max_heat_kw = 0.1
    output = plan(cfg, inputs(cfg, outside=-20))
    assert not output["feasible"]
    assert output["comfort_violation_degree_hours"] > 0


@pytest.mark.parametrize("energy_mode", ["full", "solar_only", "tariff_only"])
def test_hot_office_does_not_reject_safe_coupled_recovery(energy_mode):
    cfg = config(horizon_hours=24, battery=Battery())
    for room in cfg.rooms:
        room.night_comfort = ComfortRange(min_c=18,max_c=22,fallback_c=19)
    data = inputs(cfg, at=datetime(2026,10,19,18,tzinfo=timezone.utc),
                  temps={'living':22,'office':25},soc=0.14,outside=10)
    data.energy_mode = energy_mode
    output = plan(cfg,data)
    assert output['feasible'] and output['comfort_status'] == 'recovering'
    assert 'office' in output['recovering_rooms']
    assert output['comfort_violation_degree_hours'] > 0
    assert output['avoidable_comfort_violation_degree_hours'] < 1e-6
    blocks = control_blocks(cfg,data)
    starts = [i for i,b in enumerate(blocks) if i == 0 or b != blocks[i-1]]
    minimum = [[int(comfort_at(cfg,r,data.slots[i].at).min_c) for r in cfg.rooms] for i in starts]
    ceiling = simulate(cfg,data,minimum,True)['temperature_trace']
    targets = [[output['slots'][i]['targets_c'][r.id] for r in cfg.rooms] for i in starts]
    replay = simulate(cfg,data,targets,True)
    for step, temperatures in enumerate(replay['temperature_trace']):
        for i, room in enumerate(cfg.rooms):
            band = comfort_at(cfg,room,data.slots[step//3].at)
            assert temperatures[i] >= band.min_c - 1e-6
            assert temperatures[i] <= max(band.max_c,ceiling[step][i]) + 1e-6


def test_recovery_allowance_cannot_buy_avoidable_overheating_or_underheating():
    cfg = config()
    for room in cfg.rooms:
        room.night_comfort = ComfortRange(min_c=18,max_c=20,fallback_c=19)
    cfg.quiet_start_hour = cfg.quiet_end_hour = 0
    data = inputs(cfg, temps={'living':25,'office':20})
    for slot in data.slots: slot.import_price_per_kwh = -1
    ceiling = simulate(cfg,data,[[18,18]]*6,True)['temperature_trace']
    overheated = simulate(cfg,data,[[22,22]]*6,recovery_ceiling=ceiling)
    assert overheated['avoidable_violation'] > 0
    output = plan(cfg,data)
    assert output['feasible']
    assert output['avoidable_comfort_violation_degree_hours'] < 1e-6
    cfg.compressors[0].max_heat_kw = 0.1
    cold = plan(cfg,inputs(cfg,temps={'living':25,'office':17},outside=-20))
    assert not cold['feasible'] and cold['comfort_status'] == 'infeasible'


def test_night_transition_accepts_passive_cooling_and_margin_is_soft():
    cfg = config()
    cfg.rooms[0].night_comfort = ComfortRange(min_c=18,max_c=20,fallback_c=19)
    cfg.rooms[1].night_comfort = ComfortRange(min_c=18,max_c=22,fallback_c=19)
    data = inputs(cfg,at=datetime(2026,10,19,19,45,tzinfo=timezone.utc),
                  temps={'living':21.9,'office':21},outside=8)
    output = plan(cfg,data)
    assert output['feasible'] and output['comfort_status'] == 'recovering'
    assert all(s['targets_c']['living'] <= 20 for s in output['slots'][1:])
    # Exactly the user's lower limit is admissible even with a nonzero margin.
    cfg.quiet_start_hour = cfg.quiet_end_hour = 0
    cold_data = inputs(cfg,temps={'living':18,'office':18})
    at_minimum = simulate(cfg,cold_data,[[18,18]]*6)
    assert at_minimum['violation'] == at_minimum['avoidable_violation'] == 0
    assert any(18 in s['targets_c'].values() for s in plan(cfg,cold_data)['slots'])


def test_solar_changes_electrical_cost():
    cfg = config()
    grid = simulate(cfg, inputs(cfg, pv=0), [[20, 20]] * 6)
    solar = simulate(cfg, inputs(cfg, pv=3), [[20, 20]] * 6)
    assert solar["cost"] < grid["cost"]
    assert grid["heating_kwh"] == solar["heating_kwh"]


def test_observation_mode_cannot_emit_commands_even_with_a_plan():
    cfg = config()
    now = inputs(cfg).generated_at
    assert commands(cfg, {"feasible": True}, states(cfg, now), now, {}) == []


def test_control_requires_enable_helper():
    with pytest.raises(ValidationError, match="enable switch"):
        config(mode="control")
    cfg = controlled()
    now = inputs(cfg).generated_at
    current = states(cfg, now)
    current["input_boolean.heating"]["state"] = "off"
    assert commands(cfg, None, current, now, {}) == []


def test_fallback_is_quiet_and_does_not_set_fan_or_control_battery():
    cfg = controlled()
    now = inputs(cfg).generated_at
    calls = commands(cfg, None, states(cfg, now), now, {})
    assert len(calls) == 4
    assert {c["service"] for c in calls} == {"set_preset_mode", "set_temperature"}
    assert all(
        c["data"].get("preset_mode") == "quiet" or c["data"].get("temperature") == 20
        for c in calls
    )


def test_manual_override_persists_and_prevents_reassertion():
    cfg = controlled()
    now = inputs(cfg).generated_at
    current = states(cfg, now)
    memory: dict[str, Any] = {}
    commands(cfg, None, current, now, memory)
    later = now + timedelta(minutes=3)
    current = states(cfg, later)
    current["climate.living"]["attributes"]["temperature"] = 22
    calls = commands(cfg, None, current, later, memory)
    assert all(c["data"]["entity_id"] != "climate.living" for c in calls)
    assert memory["climate.living"]["override_until"] > later.timestamp()


@pytest.mark.parametrize(
    "state",
    [
        None,
        {"state": "unknown"},
        {"state": "unavailable"},
        {"state": "NaN", "last_reported": "2026-01-15T01:00:00+00:00"},
        {"state": "20", "last_reported": "2025-01-15T01:00:00+00:00"},
        {"state": "20", "last_reported": "2027-01-15T01:00:00+00:00"},
    ],
)
def test_missing_stale_and_nonfinite_measurements_are_rejected(state):
    with pytest.raises((ValueError, TypeError)):
        read_number(state, datetime(2026, 1, 15, 1, tzinfo=timezone.utc), 3600)


def test_forecast_gap_and_night_interpolation():
    assert interpolate([(0, 0), (12 * 3600, 0)], 6 * 3600, solar=True) == 0
    with pytest.raises(ValueError, match="gap"):
        interpolate([(0, 10), (12 * 3600, 10)], 6 * 3600)
    with pytest.raises(ValueError, match="cover"):
        interpolate([(0, 10), (3600, 12)], 7200)


def test_api_runs_plan_and_rejects_invalid_input():
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    cfg = config()
    response = client.post(
        "/v1/heating/plan",
        json={
            "settings": cfg.model_dump(mode="json"),
            "inputs": inputs(cfg).model_dump(mode="json"),
        },
    )
    assert response.status_code == 200
    assert response.json()["gas_available"] is False
    response = client.post("/v1/heating/plan", json={"settings": {}, "inputs": {}})
    assert response.status_code == 422


@pytest.mark.parametrize(
    'stamp,night',
    [('2026-10-19T19:59:59+00:00', False), ('2026-10-19T20:00:00+00:00', True),
     ('2026-10-20T04:59:59+00:00', True), ('2026-10-20T05:00:00+00:00', False),
     ('2026-10-25T00:30:00+00:00', True), ('2026-10-25T01:30:00+00:00', True),
     ('2026-10-25T06:00:00+00:00', False), ('2026-03-29T05:00:00+00:00', False)],
)
def test_night_comfort_uses_local_clock_including_dst(stamp, night):
    cfg = config()
    room = cfg.rooms[0]
    room.night_comfort = ComfortRange(min_c=18, max_c=20, fallback_c=19)
    band = comfort_at(cfg, room, datetime.fromisoformat(stamp))
    assert (band.min_c, band.max_c) == ((18, 20) if night else (19, 22))


@pytest.mark.parametrize('start', ['2026-10-19T19:45:00+00:00', '2026-10-20T04:45:00+00:00'])
def test_optimizer_splits_unaligned_blocks_at_night_and_day_boundaries(start):
    cfg = config()
    cfg.rooms[0].night_comfort = ComfortRange(min_c=18, max_c=20, fallback_c=19)
    output = plan(cfg, inputs(cfg, datetime.fromisoformat(start)))
    assert len(output['slots']) == 24
    for slot in output['slots']:
        night = quiet_at(cfg, datetime.fromisoformat(slot['at']))
        assert (18 if night else 19) <= slot['targets_c']['living'] <= (20 if night else 22)
        assert 19 <= slot['targets_c']['office'] <= 22


def test_invalid_night_range_and_margin_are_rejected():
    with pytest.raises(ValidationError, match='fallback'):
        ComfortRange(min_c=18, max_c=20, fallback_c=21)
    cfg = config().model_dump()
    cfg['rooms'][0]['night_comfort'] = {'min_c':18, 'max_c':19, 'fallback_c':19}
    cfg['comfort_margin_c'] = 0.5
    with pytest.raises(ValidationError, match='margin'):
        HeatingSettings.model_validate(cfg)

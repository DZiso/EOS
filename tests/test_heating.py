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
    Compressor,
    Coupling,
    ForecastSlot,
    HeatingSettings,
    PlanningInput,
    Room,
)
from akkudoktoreos.heating.planner import plan, quiet_at, simulate, tariff_at
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


def test_control_requires_validation_and_enable_helper():
    with pytest.raises(ValidationError, match="validated"):
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
    assert len(calls) == 2
    assert all(
        c["service"] == "set_preset_mode" and c["data"]["preset_mode"] == "quiet" for c in calls
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

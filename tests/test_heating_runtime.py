"""Integration checks for the observation worker and executable control boundary."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from akkudoktoreos.heating import runtime
from akkudoktoreos.heating.controller import commands
from akkudoktoreos.heating.models import Compressor, HeatingSettings, Room


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path))
    cfg = HeatingSettings(
        enabled=True,
        horizon_hours=6,
        rooms=[Room(id="room", climate_entity="climate.room", temperature_entity="sensor.room")],
        compressors=[Compressor(id="outdoor", rooms=["room"])],
    )
    now = datetime.now(timezone.utc)
    states = {
        "sensor.room": {"state": "20", "last_reported": now.isoformat()},
        "climate.room": {
            "state": "heat",
            "attributes": {
                "temperature": 20,
                "preset_mode": "none",
                "hvac_modes": ["off", "heat"],
                "preset_modes": ["none", "quiet"],
            },
        },
    }
    fake = Mock()
    fake.states.return_value = states
    monkeypatch.setattr(runtime, "HAClient", lambda: fake)
    monkeypatch.setattr(runtime, "settings", lambda: cfg)
    monkeypatch.setattr(runtime, "bootstrap_forecasts", AsyncMock())

    async def forecast(key, at):
        return [
            (at.timestamp() - 3600 + i * 3600, 8 if key == "weather_temp_air" else 0)
            for i in range(10)
        ]

    monkeypatch.setattr(runtime, "forecast_points", forecast)
    monkeypatch.setattr(runtime, "_last_plan_attempt", 0)
    return cfg, fake, states


@pytest.mark.asyncio
async def test_observer_records_and_plans_without_service_calls(tmp_path, monkeypatch):
    cfg, fake, states = setup(tmp_path, monkeypatch)
    await runtime.heating_tick()
    fake.apply.assert_not_called()
    status = json.loads((tmp_path / "heating/status.json").read_text())
    assert status["state"] == "observing"
    assert status["commands_sent"] == 0
    proposal = json.loads((tmp_path / "heating/plan.json").read_text())
    assert len(proposal["slots"]) == 24
    assert len(list((tmp_path / "heating").glob("observations-*.jsonl"))) == 1


@pytest.mark.asyncio
async def test_stale_measurement_blocks_plan_but_keeps_observing(tmp_path, monkeypatch):
    cfg, fake, states = setup(tmp_path, monkeypatch)
    states["sensor.room"]["last_reported"] = (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).isoformat()
    await runtime.heating_tick()
    fake.apply.assert_not_called()
    status = json.loads((tmp_path / "heating/status.json").read_text())
    assert status["state"] == "waiting_for_data"
    assert "room" in status["error"]
    assert not (tmp_path / "heating/plan.json").exists()
    assert len(list((tmp_path / "heating").glob("observations-*.jsonl"))) == 1


@pytest.mark.asyncio
async def test_forecast_failure_does_not_relabel_old_plan_as_new(tmp_path, monkeypatch):
    cfg, fake, states = setup(tmp_path, monkeypatch)
    await runtime.heating_tick()
    previous = (tmp_path / "heating/plan.json").read_text()
    monkeypatch.setattr(runtime, "_last_plan_attempt", 0)
    monkeypatch.setattr(
        runtime, "forecast_points", AsyncMock(side_effect=ValueError("forecast stale"))
    )
    await runtime.heating_tick()
    assert (tmp_path / "heating/plan.json").read_text() == previous
    fake.apply.assert_not_called()
    assert json.loads((tmp_path / "heating/status.json").read_text())["state"] == "waiting_for_data"


def test_stale_plan_fallback_overrides_hold_period(tmp_path, monkeypatch):
    cfg, fake, states = setup(tmp_path, monkeypatch)
    cfg.mode = "control"
    cfg.validated_model = True
    cfg.control_switch_entity = "input_boolean.enable"
    states["input_boolean.enable"] = {"state": "on"}
    now = datetime.now(timezone.utc)
    states["climate.room"]["attributes"]["temperature"] = 22
    memory: dict[str, Any] = {"climate.room": {"last_change": now.timestamp() - 60}}
    calls = commands(cfg, None, states, now, memory)
    target_calls = [c for c in calls if c["service"] == "set_temperature"]
    assert len(target_calls) == 1 and target_calls[0]["data"]["temperature"] == 20


def test_settings_are_disabled_without_an_options_file(tmp_path, monkeypatch):
    monkeypatch.setenv("EOS_HEATING_CONFIG", str(tmp_path / "absent.json"))
    assert runtime.settings().enabled is False


def test_pending_cloud_ack_does_not_retry_and_extend_lease(tmp_path, monkeypatch):
    cfg, fake, states = setup(tmp_path, monkeypatch)
    cfg.mode = "control"
    cfg.validated_model = True
    cfg.control_switch_entity = "input_boolean.enable"
    states["input_boolean.enable"] = {"state": "on"}
    now = datetime.now(timezone.utc)
    states["climate.room"]["attributes"]["temperature"] = 22
    memory: dict[str, Any] = {}
    assert commands(cfg, None, states, now, memory)
    deadline = memory["climate.room"]["pending_until"]
    assert commands(cfg, None, states, now + timedelta(seconds=60), memory) == []
    assert memory["climate.room"]["pending_until"] == deadline
    assert commands(cfg, None, states, now + timedelta(seconds=180), memory) == []
    assert memory["climate.room"]["override_until"] > now.timestamp() + 180

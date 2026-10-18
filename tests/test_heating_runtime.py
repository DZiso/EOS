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
    monkeypatch.setattr(runtime, "_last_planning_error", None)
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


def test_mode_change_reasserts_target_and_preset(tmp_path, monkeypatch):
    cfg, fake, states = setup(tmp_path, monkeypatch)
    cfg.mode = "control"
    cfg.validated_model = True
    cfg.control_switch_entity = "input_boolean.enable"
    states["input_boolean.enable"] = {"state": "on"}
    states["climate.room"]["state"] = "off"
    # Both settings already match, but Samsung can reset them on power-up.
    cfg.quiet_start_hour = cfg.quiet_end_hour = 0
    states["climate.room"]["attributes"]["preset_mode"] = "quiet"
    calls = commands(cfg, None, states, datetime.now(timezone.utc), {})
    assert [c["service"] for c in calls] == [
        "set_hvac_mode", "set_preset_mode", "set_temperature"
    ]
    assert calls[-1]["data"]["temperature"] == 20


@pytest.mark.asyncio
async def test_temperature_becoming_stale_invalidates_status_between_plans(tmp_path, monkeypatch):
    cfg, fake, states = setup(tmp_path, monkeypatch)
    await runtime.heating_tick()
    states["sensor.room"]["last_reported"] = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat()
    await runtime.heating_tick()
    status = json.loads((tmp_path / "heating/status.json").read_text())
    assert status["state"] == "waiting_for_data"
    assert status["commands_sent"] == 0
    fake.apply.assert_not_called()


def dispatch_calls():
    return [
        {"domain": "climate", "service": service,
         "data": {"entity_id": "climate.room", key: value}}
        for service, key, value in [
            ("set_hvac_mode", "hvac_mode", "heat"),
            ("set_preset_mode", "preset_mode", "quiet"),
            ("set_temperature", "temperature", 20),
        ]
    ]


def test_dispatch_waits_for_feedback_before_dependent_commands(monkeypatch):
    client = object.__new__(runtime.HAClient)
    seen = []
    state: dict[str, Any] = {
        "state": "off", "attributes": {"temperature": 24, "preset_mode": "none"}
    }
    pending = None
    reads = 0

    def request(method, path, body=None):
        nonlocal pending, reads
        seen.append((method, path))
        if method == "POST":
            assert pending is None, "next command was sent before cloud acknowledgement"
            pending = body
            reads = 0
            return []
        reads += 1
        if reads == 2 and pending:
            for key, value in pending.items():
                if key == "hvac_mode":
                    state["state"] = value
                elif key != "entity_id":
                    state["attributes"][key] = value
            pending = None
        return state

    monkeypatch.setattr(client, "request", request)
    monkeypatch.setattr(runtime.time, "sleep", lambda _: None)
    assert client.apply(dispatch_calls()) == 3
    assert [method for method, _ in seen] == ["POST", "GET", "GET"] * 3


def test_missing_ack_stops_batch_and_preserves_partial_counts(monkeypatch):
    client = object.__new__(runtime.HAClient)
    client.ack_timeout_seconds = 0
    fake = Mock(return_value={"state": "off", "attributes": {}})
    monkeypatch.setattr(client, "request", fake)
    with pytest.raises(runtime.CommandDispatchError) as caught:
        client.apply(dispatch_calls())
    assert (caught.value.sent, caught.value.confirmed, caught.value.attempted) == (1, 0, 1)
    assert sum(c.args[0] == "POST" for c in fake.call_args_list) == 1


def test_request_timeout_reports_unknown_sent_count(monkeypatch):
    client = object.__new__(runtime.HAClient)
    monkeypatch.setattr(client, "request", Mock(side_effect=TimeoutError("private request")))
    with pytest.raises(runtime.CommandDispatchError) as caught:
        client.apply(dispatch_calls())
    assert caught.value.sent is None
    assert caught.value.confirmed == 0
    assert "private" not in str(caught.value)


@pytest.mark.asyncio
async def test_partial_dispatch_error_is_not_reported_as_zero_commands(tmp_path, monkeypatch):
    cfg, fake, states = setup(tmp_path, monkeypatch)
    cfg.mode = "control"
    cfg.control_switch_entity = "input_boolean.enable"
    states["input_boolean.enable"] = {"state": "on"}
    states["climate.room"]["state"] = "off"
    fake.apply.side_effect = runtime.CommandDispatchError(2, 1, 2)
    await runtime.heating_tick()
    status = json.loads((tmp_path / "heating/status.json").read_text())
    assert status["state"] == "command_error"
    assert status["commands_sent"] == 2
    assert status["commands_confirmed"] == 1
    assert status["commands_attempted"] == 2
    fake.apply.assert_called_once()


def test_dashboard_refresh_keeps_ingress_prefix_and_rejects_stale_runtime(tmp_path, monkeypatch):
    from fasthtml.common import to_xml

    from akkudoktoreos.server.dash import context, heating

    monkeypatch.setattr(context, "ROOT_PATH", "/api/hassio_ingress/test-prefix")
    monkeypatch.setenv("EOS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EOS_HEATING_CONFIG", str(tmp_path / "absent.json"))
    runtime.save_json(tmp_path / "heating/status.json", {"state": "waiting_for_data"})
    runtime.save_json(
        tmp_path / "heating/plan.json",
        {"generated_at": datetime.now(timezone.utc).isoformat()},
    )
    html = to_xml(heating.Heating())
    assert 'hx-get="/api/hassio_ingress/test-prefix/eosdash/heating"' in html
    assert "No fresh plan" in html
    assert ">Fresh plan<" not in html


@pytest.mark.asyncio
async def test_forecast_outage_does_not_stop_comfort_control(tmp_path, monkeypatch):
    cfg, fake, states = setup(tmp_path, monkeypatch)
    cfg.mode = "control"
    cfg.control_switch_entity = "input_boolean.enable"
    states["input_boolean.enable"] = {"state": "on"}
    states["climate.room"]["state"] = "off"
    monkeypatch.setattr(runtime, "forecast_points", AsyncMock(side_effect=ValueError("forecast stale")))
    await runtime.heating_tick()
    status = json.loads((tmp_path / "heating/status.json").read_text())
    assert status["state"] == "controlling"
    assert status["planning_error"] == "forecast stale"
    assert status["rooms"]["room"]["source"] == "comfort"
    assert status["commands_confirmed"] == 3
    fake.apply.assert_called_once()


@pytest.mark.asyncio
async def test_failed_command_does_not_stop_another_room(tmp_path, monkeypatch):
    from copy import deepcopy

    cfg, fake, states = setup(tmp_path, monkeypatch)
    cfg.mode = "control"
    cfg.control_switch_entity = "input_boolean.enable"
    states["input_boolean.enable"] = {"state": "on"}
    states["climate.room"]["state"] = "off"
    cfg.rooms.append(Room(id="other", climate_entity="climate.other", temperature_entity="sensor.other"))
    cfg.compressors[0].rooms.append("other")
    states["climate.other"] = deepcopy(states["climate.room"])
    states["sensor.other"] = deepcopy(states["sensor.room"])
    fake.apply.side_effect = [runtime.CommandDispatchError(1, 0, 1), 3]
    await runtime.heating_tick()
    status = json.loads((tmp_path / "heating/status.json").read_text())
    assert fake.apply.call_count == 2
    assert status["commands_sent"] == 4 and status["commands_confirmed"] == 3
    assert status["rooms"]["room"]["state"] == "command_error"
    assert status["rooms"]["other"]["dispatch"]["confirmed"] == 3


@pytest.mark.asyncio
async def test_disable_during_dispatch_releases_ownership(tmp_path, monkeypatch):
    cfg, fake, states = setup(tmp_path, monkeypatch)
    cfg.mode = "control"
    cfg.control_switch_entity = "input_boolean.enable"
    states["input_boolean.enable"] = {"state": "on"}
    states["climate.room"]["state"] = "off"
    fake.apply.side_effect = runtime.ControlDisabled(0, 0, 0)
    await runtime.heating_tick()
    status = json.loads((tmp_path / "heating/status.json").read_text())
    assert status["state"] == "disabled" and status["commands_sent"] == 0
    assert json.loads((tmp_path / "heating/ownership.json").read_text()) == {}


@pytest.mark.asyncio
async def test_feedback_and_manual_override_survive_worker_restart(tmp_path, monkeypatch):
    cfg, fake, states = setup(tmp_path, monkeypatch)
    cfg.mode = "control"
    cfg.rooms[0].room_feedback = True
    cfg.control_switch_entity = "input_boolean.enable"
    states["input_boolean.enable"] = {"state": "on"}
    states["climate.room"]["attributes"]["current_temperature"] = 23
    states["climate.room"]["last_reported"] = states["sensor.room"]["last_reported"]
    await runtime.heating_tick()
    path = tmp_path / "heating/ownership.json"
    ownership = json.loads(path.read_text())
    assert ownership["climate.room"]["setpoint_offset_c"] == 3
    ownership["climate.room"]["override_until"] = datetime.now(timezone.utc).timestamp() + 3600
    ownership["climate.room"]["expected"] = None
    runtime.save_json(path, ownership)
    fake.apply.reset_mock()
    monkeypatch.setattr(runtime, "_last_plan_attempt", 0)
    await runtime.heating_tick()
    fake.apply.assert_not_called()
    assert json.loads(path.read_text())["climate.room"]["setpoint_offset_c"] == 3
    status = json.loads((tmp_path / "heating/status.json").read_text())
    assert status["rooms"]["room"]["state"] == "manual_override"

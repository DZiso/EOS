"""Independent comfort control, sensor cadence, and forecast recovery regressions."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from akkudoktoreos.core import coreabc
from akkudoktoreos.heating import runtime
from akkudoktoreos.heating.controller import commands, temperature_status
from akkudoktoreos.heating.models import Compressor, HeatingSettings, Room
from akkudoktoreos.server.rest.heating import router

NOW = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)


def setup_control():
    config = HeatingSettings(
        enabled=True, mode="control", control_switch_entity="input_boolean.heating",
        rooms=[Room(id="room", climate_entity="climate.room", temperature_entity="sensor.room", room_feedback=True, fallback_c=21),
               Room(id="other", climate_entity="climate.other", temperature_entity="sensor.other", fallback_c=21)],
        compressors=[Compressor(id="shared", rooms=["room", "other"])],
    )
    states: dict[str, Any] = {"input_boolean.heating": {"state": "on"}}
    for room in config.rooms:
        states[room.temperature_entity] = {"state": "20", "last_reported": NOW.isoformat()}
        states[room.climate_entity] = {
            "state": "heat", "last_reported": NOW.isoformat(),
            "attributes": {"temperature": 22, "current_temperature": 24, "preset_mode": "none",
                           "hvac_modes": ["heat", "off"], "preset_modes": ["quiet", "none"], "target_temp_step": 1},
        }
    return config, states, {}


def acknowledge(states, calls):
    for c in calls:
        state = states[c['data']['entity_id']]
        for k,v in c['data'].items():
            if k == 'hvac_mode': state['state'] = v
            elif k != 'entity_id': state['attributes'][k] = v


def test_basic_control_does_not_need_a_cop_model():
    cfg, states, memory = setup_control()
    assert not cfg.validated_model
    calls = commands(cfg, None, states, NOW, memory)
    assert any(c['data'].get('temperature') == 25 for c in calls)
    with pytest.raises(ValidationError, match='explicit estimated-model'):
        HeatingSettings.model_validate({**cfg.model_dump(), 'optimization_enabled':True})


def test_sensor_failure_is_isolated_to_its_room():
    cfg, states, memory = setup_control()
    states['sensor.room']['state'] = 'unavailable'
    decisions: dict[str, Any] = {}
    calls = commands(cfg, None, states, NOW, memory, decisions)
    assert calls and all(c['data']['entity_id'] == 'climate.other' for c in calls)
    assert decisions['room']['state'] == 'waiting_for_temperature'


def test_lost_room_sensor_releases_corrected_target_to_native_fallback():
    cfg, states, memory = setup_control()
    acknowledge(states, commands(cfg, None, states, NOW, memory))
    assert states['climate.room']['attributes']['temperature'] == 25
    states['sensor.room']['state'] = 'unavailable'
    decisions: dict[str, Any] = {}
    calls = commands(cfg, None, states, NOW + timedelta(minutes=2), memory, decisions)
    assert any(c['data'] == {'entity_id':'climate.room','temperature':21} for c in calls)
    assert decisions['room']['state'] == 'device_fallback'


def test_old_but_within_configured_cadence_is_not_relabelled_fresh():
    cfg, states, memory = setup_control()
    cfg.rooms[0].temperature_max_age_seconds = 14400
    states['sensor.room']['last_reported'] = (NOW-timedelta(hours=2)).isoformat()
    q = temperature_status(cfg, states, NOW)['room']
    assert q['usable'] and q['age_seconds'] == 7200
    assert q['source_at'] == states['sensor.room']['last_reported']
    states['sensor.room']['last_reported'] = (NOW-timedelta(hours=5)).isoformat()
    assert not temperature_status(cfg, states, NOW)['room']['usable']


def test_disconnected_temperature_integration_rejects_even_recent_values():
    cfg, states, memory = setup_control()
    cfg.rooms[0].availability_entity = 'binary_sensor.connected'
    states['binary_sensor.connected'] = {'state':'off'}
    assert not temperature_status(cfg, states, NOW)['room']['usable']


def test_feedback_cannot_wind_up_on_one_unchanged_report():
    cfg, states, memory = setup_control()
    cfg.rooms[0].temperature_max_age_seconds = 14400
    acknowledge(states, commands(cfg, None, states, NOW, memory))
    initial = memory['climate.room']['setpoint_offset_c']
    for minute in range(1, 120):
        acknowledge(states, commands(cfg, None, states, NOW+timedelta(minutes=minute), memory))
    assert memory['climate.room']['setpoint_offset_c'] == initial
    assert states['climate.room']['attributes']['temperature'] == 25


def test_feedback_only_makes_bounded_corrections_after_a_new_room_report():
    cfg, states, memory = setup_control()
    acknowledge(states, commands(cfg, None, states, NOW, memory))
    later = NOW+timedelta(minutes=31)
    states['sensor.room'].update(state='19.5',last_reported=later.isoformat())
    acknowledge(states, commands(cfg, None, states, later, memory))
    assert memory['climate.room']['setpoint_offset_c'] == 4.5
    assert states['climate.room']['attributes']['temperature'] == 26


def test_upper_limit_sends_only_off_not_samsung_setpoints_while_off():
    cfg, states, memory = setup_control()
    states['sensor.room']['state'] = '21.8'
    calls = [c for c in commands(cfg,None,states,NOW,memory) if c['data']['entity_id']=='climate.room']
    assert calls == [{'domain':'climate','service':'set_hvac_mode','data':{'entity_id':'climate.room','hvac_mode':'off'}}]
    acknowledge(states,calls)
    states['sensor.room']['state'] = '20'
    assert not [c for c in commands(cfg,None,states,NOW+timedelta(minutes=5),memory) if c['data']['entity_id']=='climate.room']
    assert [c for c in commands(cfg,None,states,NOW+timedelta(minutes=11),memory) if c['data']['entity_id']=='climate.room'][0]['data']['hvac_mode']=='heat'


def test_manual_off_takes_priority_over_feedback():
    cfg, states, memory = setup_control()
    acknowledge(states,commands(cfg,None,states,NOW,memory))
    states['climate.room']['state'] = 'off'
    decisions: dict[str, Any] = {}
    calls = commands(cfg,None,states,NOW+timedelta(minutes=3),memory,decisions)
    assert all(c['data']['entity_id']!='climate.room' for c in calls)
    assert decisions['room']['state']=='manual_override'


def test_optimizer_targets_are_optional_and_bounded_by_room_comfort():
    cfg, states, memory = setup_control()
    proposal = {'feasible':True,'generated_at':NOW.isoformat(),'slots':[{'at':NOW.isoformat(),'targets_c':{'room':30,'other':10}}]}
    decisions: dict[str, Any] = {}
    commands(cfg,proposal,states,NOW,memory,decisions)
    assert decisions['room']['room_target_c']==21
    cfg.optimization_enabled=True;cfg.allow_estimated_model_control=True
    commands(cfg,proposal,states,NOW,{},decisions)
    assert decisions['room']['room_target_c']==21.75
    assert decisions['other']['room_target_c']==19.25
    assert decisions['room']['source']=='eos'


@pytest.mark.asyncio
async def test_cached_forecast_without_metadata_gets_a_real_refresh(monkeypatch):
    provider = SimpleNamespace(record_keys=['weather_temp_air'],update_datetime=None)
    prediction = SimpleNamespace(enabled_providers=[provider],key_to_raw_series=AsyncMock(return_value=pd.Series([5.0],index=[NOW])))
    async def refresh(**kwargs): provider.update_datetime=NOW
    ems = SimpleNamespace(run=AsyncMock(side_effect=refresh))
    monkeypatch.setattr(coreabc,'get_prediction',lambda:prediction)
    monkeypatch.setattr(coreabc,'get_ems',lambda:ems)
    monkeypatch.setattr(runtime,'_last_forecast_recovery',float('-inf'))
    assert await runtime.forecast_points('weather_temp_air',NOW)==[(NOW.timestamp(),5.0)]
    assert ems.run.await_args.kwargs['force_update'] is True
    await runtime.forecast_points('weather_temp_air',NOW)
    assert ems.run.await_count==1


@pytest.mark.asyncio
async def test_unsuccessful_forecast_recovery_is_not_called_fresh_or_retried_each_minute(monkeypatch):
    provider = SimpleNamespace(record_keys=['weather_temp_air'],update_datetime=None)
    ems = SimpleNamespace(run=AsyncMock())
    monkeypatch.setattr(coreabc,'get_prediction',lambda:SimpleNamespace(enabled_providers=[provider]))
    monkeypatch.setattr(coreabc,'get_ems',lambda:ems)
    monkeypatch.setattr(runtime,'_last_forecast_recovery',float('-inf'))
    for _ in range(2):
        with pytest.raises(ValueError,match='No updated'):
            await runtime.forecast_points('weather_temp_air',NOW)
    assert ems.run.await_count==1


def test_enable_switch_is_checked_between_dependent_commands(monkeypatch):
    client = object.__new__(runtime.HAClient)
    control = iter(['on','off'])
    sent=[]
    def request(method,path,body=None):
        if method=='GET':return {'state':next(control)}
        sent.append(body);return []
    monkeypatch.setattr(client,'request',request)
    monkeypatch.setattr(client,'wait_for_ack',Mock())
    batch=[{'domain':'climate','service':'set_hvac_mode','data':{'entity_id':'climate.room','hvac_mode':'heat'}},
           {'domain':'climate','service':'set_temperature','data':{'entity_id':'climate.room','temperature':25}}]
    with pytest.raises(runtime.ControlDisabled) as error:client.apply(batch,'input_boolean.heating')
    assert len(sent)==1 and error.value.sent==1 and error.value.confirmed==1


def test_observation_export_is_bounded_and_does_not_accept_paths(tmp_path,monkeypatch):
    monkeypatch.setenv('EOS_DATA_DIR',str(tmp_path))
    p=tmp_path/'heating';p.mkdir()
    (p/'observations-2026-01-15.jsonl').write_text(''.join(json.dumps({'at':i})+'\n' for i in range(5)))
    app=FastAPI();app.include_router(router);client=TestClient(app)
    result=client.get('/v1/heating/observations?day=2026-01-15&limit=2')
    assert result.json()['records']==[{'at':3},{'at':4}]
    assert client.get('/v1/heating/observations?day=../options').status_code==422
    assert client.get('/v1/heating/observations?limit=99999').status_code==422


def test_native_thermostat_stays_armed_when_its_rounded_temperature_is_warm():
    cfg, states, memory = setup_control()
    states['sensor.other']['state'] = '22'
    states['climate.other']['state'] = 'off'
    calls = [c for c in commands(cfg,None,states,NOW,memory) if c['data']['entity_id']=='climate.other']
    assert calls[0]['data']['hvac_mode']=='heat'
    assert calls[-1]['data']['temperature']==21


def test_native_thermostat_needs_no_remote_temperature_to_keep_heating_automatic():
    cfg, states, memory = setup_control()
    states['sensor.other']['state']='unavailable'
    states['climate.other']['state']='off'
    decisions: dict[str, Any] = {}
    calls = [c for c in commands(cfg,None,states,NOW,memory,decisions) if c['data']['entity_id']=='climate.other']
    assert calls[0]['data']['hvac_mode']=='heat'
    assert calls[-1]['data']['temperature']==21
    assert decisions['other']['source']=='device_thermostat'


def test_lost_external_sensor_after_comfort_pause_uses_native_fallback_after_minimum_off_time():
    cfg, states, memory = setup_control()
    states['sensor.room']['state']='21.8'
    acknowledge(states,commands(cfg,None,states,NOW,memory))
    states['sensor.room']['state']='unavailable'
    assert not [c for c in commands(cfg,None,states,NOW+timedelta(minutes=5),memory) if c['data']['entity_id']=='climate.room']
    calls=[c for c in commands(cfg,None,states,NOW+timedelta(minutes=11),memory) if c['data']['entity_id']=='climate.room']
    assert calls[0]['data']['hvac_mode']=='heat' and calls[-1]['data']['temperature']==21


def test_three_hour_override_expires_and_control_resumes_after_memory_reload():
    cfg, states, memory = setup_control()
    cfg.manual_override_hours = 3
    acknowledge(states, commands(cfg, None, states, NOW, memory))
    changed_at = NOW + timedelta(minutes=3)
    states['climate.room']['attributes']['temperature'] = 24
    commands(cfg, None, states, changed_at, memory)
    assert memory['climate.room']['override_until'] == changed_at.timestamp() + 3*3600
    # Persisted ownership is all the worker needs after a restart.
    restored = json.loads(json.dumps(memory))
    before_expiry = changed_at + timedelta(hours=3, seconds=-1)
    states['sensor.room']['last_reported'] = before_expiry.isoformat()
    decisions: dict[str, Any] = {}
    assert not [c for c in commands(cfg,None,states,before_expiry,restored,decisions) if c['data']['entity_id']=='climate.room']
    assert decisions['room']['state']=='manual_override'
    after_expiry = changed_at + timedelta(hours=3, seconds=1)
    calls=[c for c in commands(cfg,None,states,after_expiry,restored,decisions) if c['data']['entity_id']=='climate.room']
    assert calls and decisions['room']['state']=='active'
    assert decisions['room']['room_target_c']==21

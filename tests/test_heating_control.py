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
from akkudoktoreos.heating.models import ComfortRange, Compressor, HeatingSettings, Room
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
    assert decisions['room']['room_target_c']==22
    assert decisions['other']['room_target_c']==19
    assert decisions['room']['source']=='eos'


def test_exact_night_minimum_reaches_native_thermostat_and_override_still_wins():
    cfg, states, memory = setup_control()
    cfg.optimization_enabled = cfg.allow_estimated_model_control = True
    cfg.quiet_start_hour = cfg.quiet_end_hour = 0
    for room in cfg.rooms:
        room.room_feedback = False
        room.night_comfort = ComfortRange(min_c=18, max_c=20, fallback_c=19)
    proposal = {'feasible':True, 'generated_at':NOW.isoformat(),
                'slots':[{'at':NOW.isoformat(), 'targets_c':{'room':18,'other':18}}]}
    memory['climate.other'] = {'override_until': (NOW+timedelta(hours=2)).timestamp()}
    decisions: dict[str, Any] = {}
    calls = commands(cfg,proposal,states,NOW,memory,decisions)
    assert decisions['room']['room_target_c'] == decisions['room']['ac_target_c'] == 18
    assert decisions['room']['preset'] == 'quiet'
    assert decisions['other']['state'] == 'manual_override'
    assert any(c['data'].get('temperature') == 18 for c in calls)
    assert all(c['data']['entity_id'] == 'climate.room' for c in calls)


@pytest.mark.parametrize('previous_target,next_target', [(21, 19), (19, 18), (19, 21)])
def test_native_thermostat_follows_new_plan_without_an_execution_hold(previous_target, next_target):
    cfg, states, memory = setup_control()
    cfg.optimization_enabled = cfg.allow_estimated_model_control = True
    room = cfg.rooms[1]
    room.night_comfort = ComfortRange(min_c=18, max_c=22, fallback_c=19)
    room.temperature_entity = room.climate_entity
    room.temperature_attribute = 'current_temperature'
    before = datetime.fromisoformat('2026-10-20T02:47:36+00:00')  # 04:47 Berlin
    states[room.climate_entity]['last_reported'] = before.isoformat()
    states[room.climate_entity]['attributes']['current_temperature'] = 20
    proposal = {'feasible': True, 'generated_at': before.isoformat(),
                'slots': [{'at': before.isoformat(), 'targets_c': {'other': previous_target}}]}
    acknowledge(states, commands(cfg, proposal, states, before, memory))
    assert states[room.climate_entity]['attributes']['temperature'] == previous_target
    assert memory[room.climate_entity]['last_change'] == before.timestamp()

    # Reproduce the 05:02 plan revision after the cheap tariff ends. Persisted
    # last_change from the previous release must not delay the new plan.
    memory = json.loads(json.dumps(memory))
    later = before + timedelta(minutes=15)
    proposal = {'feasible': True, 'generated_at': later.isoformat(),
                'slots': [{'at': later.isoformat(), 'targets_c': {'other': next_target}}]}
    decisions: dict[str, Any] = {}
    calls = [c for c in commands(cfg, proposal, states, later, memory, decisions)
             if c['data']['entity_id'] == room.climate_entity]
    assert calls == [{'domain': 'climate', 'service': 'set_temperature',
                      'data': {'entity_id': room.climate_entity, 'temperature': next_target}}]
    assert decisions['other']['room_target_c'] == decisions['other']['ac_target_c'] == next_target
    acknowledge(states, calls)
    assert states[room.climate_entity]['state'] == 'heat'
    assert states[room.climate_entity]['attributes']['preset_mode'] == 'quiet'
    assert not [c for c in commands(cfg, proposal, states, later + timedelta(minutes=1), memory)
                if c['data']['entity_id'] == room.climate_entity]


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


@pytest.mark.parametrize('forecast', [False, True])
def test_native_night_band_overrides_a_recent_day_target_hold_and_returns_by_day(forecast):
    cfg, states, memory = setup_control()
    room = cfg.rooms[1]
    room.night_comfort = ComfortRange(min_c=18, max_c=20, fallback_c=19)
    cfg.optimization_enabled = forecast
    cfg.allow_estimated_model_control = forecast
    night = datetime.fromisoformat('2026-10-19T20:00:00+00:00')  # 22:00 Berlin
    before = night - timedelta(minutes=1)
    states['sensor.other'].update(state='19', last_reported=before.isoformat())
    proposal = {'feasible': True, 'generated_at': before.isoformat(),
                'slots': [{'at': before.isoformat(), 'targets_c': {'other': 22}}]}
    acknowledge(states, commands(cfg, proposal, states, before, memory))
    assert states['climate.other']['attributes']['temperature'] == (22 if forecast else 21)
    # Persisted ownership and a still-fresh daytime plan must not hold a high target at night.
    memory = json.loads(json.dumps(memory))
    decisions: dict[str, Any] = {}
    acknowledge(states, commands(cfg, proposal, states, night, memory, decisions))
    assert states['climate.other']['attributes']['temperature'] == (20 if forecast else 19)
    assert states['climate.other']['attributes']['preset_mode'] == 'quiet'
    assert decisions['other']['setpoint_offset_c'] == 0
    # Even with a stale temperature and expired forecast, the time-based fallback works.
    morning = datetime.fromisoformat('2026-10-20T05:00:00+00:00')
    acknowledge(states, commands(cfg, proposal, states, morning, memory, decisions))
    assert states['climate.other']['attributes']['temperature'] == 21
    assert states['climate.other']['attributes']['preset_mode'] == 'none'


def test_disabling_feedback_releases_old_corrected_target_despite_hold():
    cfg, states, memory = setup_control()
    acknowledge(states, commands(cfg, None, states, NOW, memory))
    assert states['climate.room']['attributes']['temperature'] == 25
    cfg.rooms[0].room_feedback = False
    cfg.optimization_enabled = cfg.allow_estimated_model_control = True
    later = NOW + timedelta(minutes=1)
    proposal = {'feasible': True, 'generated_at': later.isoformat(),
                'slots': [{'at': later.isoformat(), 'targets_c': {'room': 21}}]}
    acknowledge(states, commands(cfg, proposal, states, later, memory))
    assert states['climate.room']['attributes']['temperature'] <= 22


def test_manual_override_is_preserved_across_night_boundary():
    cfg, states, memory = setup_control()
    cfg.rooms[1].night_comfort = ComfortRange(min_c=18, max_c=20, fallback_c=19)
    night = datetime.fromisoformat('2026-10-19T20:00:00+00:00')
    memory['climate.other'] = {'override_until': night.timestamp() + 3600}
    assert not [c for c in commands(cfg, None, states, night, memory)
                if c['data']['entity_id'] == 'climate.other']


def native_off_setup():
    cfg, states, memory = setup_control()
    cfg.planned_off = cfg.optimization_enabled = cfg.allow_estimated_model_control = True
    room = cfg.rooms[1]
    room.temperature_entity = room.climate_entity
    room.temperature_attribute = 'current_temperature'
    return cfg, states, memory


def off_plan(at, target):
    return {'feasible': True, 'generated_at': at.isoformat(),
            'slots': [{'at': at.isoformat(), 'targets_c': {'other': target}}]}


def other_calls(calls):
    return [c for c in calls if c['data']['entity_id'] == 'climate.other']


def test_planned_off_switches_mode_only_and_restarts_after_minimum_off_time():
    cfg, states, memory = native_off_setup()
    decisions: dict[str, Any] = {}
    calls = other_calls(commands(cfg, off_plan(NOW, 'off'), states, NOW, memory, decisions))
    assert calls == [{'domain': 'climate', 'service': 'set_hvac_mode',
                      'data': {'entity_id': 'climate.other', 'hvac_mode': 'off'}}]
    assert decisions['other']['state'] == 'planned_off'
    acknowledge(states, calls)
    # Samsung reports preset none while off: that is not a manual override.
    states['climate.other']['attributes']['preset_mode'] = 'none'
    later = NOW + timedelta(minutes=5)
    assert not other_calls(commands(cfg, off_plan(later, 'off'), states, later, memory, decisions))
    assert decisions['other']['state'] == 'planned_off'
    assert not other_calls(commands(cfg, off_plan(later, 21), states, later, memory, decisions))
    assert decisions['other']['state'] == 'minimum_off_time'
    # A planned off period holds for one block while the room is comfortable...
    held = NOW + timedelta(minutes=30)
    states['climate.other']['last_reported'] = held.isoformat()
    assert not other_calls(commands(cfg, off_plan(held, 21), states, held, memory, decisions))
    assert decisions['other']['state'] == 'minimum_off_time'
    # ...but restarts after ten minutes once the room nears its minimum.
    states['climate.other']['attributes']['current_temperature'] = 19
    assert other_calls(commands(cfg, off_plan(held, 21), states, held, json.loads(json.dumps(memory))))
    states['climate.other']['attributes']['current_temperature'] = 24
    restart = NOW + timedelta(minutes=61)
    states['climate.other']['last_reported'] = restart.isoformat()
    calls = other_calls(commands(cfg, off_plan(restart, 21), states, restart, memory, decisions))
    assert [c['service'] for c in calls] == ['set_hvac_mode', 'set_preset_mode', 'set_temperature']
    acknowledge(states, calls)
    assert states['climate.other']['state'] == 'heat'
    assert states['climate.other']['attributes']['temperature'] == 21
    # A user switching the unit on while planned off is still a manual override.
    off_again = restart + timedelta(hours=2)
    states['climate.other']['last_reported'] = off_again.isoformat()
    acknowledge(states, other_calls(commands(cfg, off_plan(off_again, 'off'), states, off_again, memory)))
    assert states['climate.other']['state'] == 'off'
    states['climate.other']['state'] = 'heat'
    after = off_again + timedelta(minutes=5)
    assert not other_calls(commands(cfg, off_plan(after, 'off'), states, after, memory, decisions))
    assert decisions['other']['state'] == 'manual_override'


@pytest.mark.parametrize('reading,ran_minutes,reason', [
    (19, 120, 'below_comfort_floor'),   # 19 < min 19 + 0.25 margin
    (24, 30, 'minimum_run_time'),
])
def test_planned_off_is_refused_near_the_floor_and_before_minimum_run(reading, ran_minutes, reason):
    cfg, states, memory = native_off_setup()
    states['climate.other']['attributes']['current_temperature'] = reading
    memory['climate.other'] = {'last_mode_change': (NOW - timedelta(minutes=ran_minutes)).timestamp()}
    decisions: dict[str, Any] = {}
    calls = other_calls(commands(cfg, off_plan(NOW, 'off'), states, NOW, memory, decisions))
    assert decisions['other']['off_refused'] == reason
    assert all(c['service'] != 'set_hvac_mode' for c in calls)
    assert decisions['other']['ac_target_c'] == 19

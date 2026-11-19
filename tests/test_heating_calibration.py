"""The daily model fit must recover known room physics from thermostat-grade telemetry."""

import json
import math
from datetime import datetime, timedelta, timezone

from akkudoktoreos.heating import calibration
from akkudoktoreos.heating.models import Compressor, HeatingSettings, Room
from akkudoktoreos.heating.planner import cop_at

LOSS, CAPACITY = 0.03, 1.5  # true values; the configured guesses below are 3x and 1.3x off
START = datetime(2026, 11, 1, tzinfo=timezone.utc)


def config():
    return HeatingSettings(
        rooms=[Room(id="room", climate_entity="climate.room", temperature_entity="climate.room",
                    energy_entity="sensor.room_energy", loss_kw_per_k=0.09, capacity_kwh_per_k=2)],
        compressors=[Compressor(id="outdoor", rooms=["room"])],
    )


def write_days(directory, days, phantom_day=None):
    """One record a minute: whole-degree intake that reads 2 K high while the unit
    heats, a unit thermostat acting on that reading, energy reported every 12 min."""
    group = config().compressors[0]
    room, energy, reported = 19.0, 100.0, 100.0
    for minute in range(days * 1440):
        at = START + timedelta(minutes=minute)
        outside = 6 + 3 * math.sin(2 * math.pi * (at.hour - 9) / 24)
        phantom = at.day - 1 == phantom_day
        mode = "heat" if at.hour < 5 or 9 <= at.hour < 15 or room < 18.5 else "off"
        setpoint = 21
        heating = (mode == "heat" or phantom) and room + 2 < setpoint + 1
        intake = round(room + (2 if heating else 0.3))
        heat = 2.0 if heating else 0.0
        room += (heat - LOSS * (room - outside)) / CAPACITY / 60
        energy += heat / cop_at(group, outside) / 60
        if minute % 12 == 0:
            reported = round(energy, 2)
        record = {
            "at": at.isoformat(), "outside_c": outside,
            "entities": {
                "climate.room": {"state": "off" if phantom else mode,
                                 "attributes": {"temperature": setpoint, "current_temperature": intake}},
                "sensor.room_energy": {"state": str(reported)},
            },
        }
        with (directory / f"observations-{at:%Y-%m-%d}.jsonl").open("a") as stream:
            stream.write(json.dumps(record) + "\n")


def test_fit_recovers_loss_and_capacity_and_replaces_the_configured_guess(tmp_path):
    write_days(tmp_path, 7)
    with (tmp_path / "observations-2026-11-03.jsonl").open("a") as stream:
        stream.write('}\n\x00\x00\n[1]\n{"at": 5}\n{"at": "2026-11-03T23:5')  # what a crash leaves behind
    cfg = config()
    result = calibration.update(cfg, tmp_path, START + timedelta(days=6, hours=1))
    room = result["rooms"]["room"]
    assert room["applied"] and room["days"] == 6 and room["tau_source"] == "fitted"
    assert abs(room["loss_kw_per_k"] / LOSS - 1) < 0.2
    assert abs(room["capacity_kwh_per_k"] / CAPACITY - 1) < 0.35
    fitted = calibration.apply(cfg, result["rooms"]).rooms[0]
    assert (fitted.loss_kw_per_k, fitted.capacity_kwh_per_k) == (room["loss_kw_per_k"], room["capacity_kwh_per_k"])


def test_too_little_data_or_no_meter_keeps_the_configured_model(tmp_path):
    write_days(tmp_path, 3)
    cfg = config()
    result = calibration.update(cfg, tmp_path, START + timedelta(days=2, hours=1))
    assert result["rooms"]["room"] == {"days": 2, "applied": False, "reason": "not enough data"}
    assert calibration.apply(cfg, result["rooms"]) == cfg
    cfg.rooms[0].energy_entity = None
    assert calibration.update(cfg, tmp_path, START + timedelta(days=9))["rooms"]["room"]["reason"].startswith("no energy")


def test_a_unit_that_runs_while_reported_off_is_not_read_as_a_slow_cooling_room(tmp_path):
    write_days(tmp_path, 7, phantom_day=3)
    room = calibration.update(config(), tmp_path, START + timedelta(days=7, hours=1))["rooms"]["room"]
    assert room["applied"] and room["days"] == 6  # the phantom day is left out
    assert abs(room["capacity_kwh_per_k"] / CAPACITY - 1) < 0.35

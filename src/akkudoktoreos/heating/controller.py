"""Room thermostat and optional scheduling, with per-room HA ownership leases."""

import math
from datetime import datetime
from typing import Any

from akkudoktoreos.heating.models import HeatingSettings, Room
from akkudoktoreos.heating.planner import comfort_at, quiet_at


def parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required")
    return result


def read_number(
    state: dict[str, Any] | None, now: datetime, max_age: int, attribute: str | None = None
) -> float:
    if not state or state.get("state") in ("unknown", "unavailable"):
        raise ValueError("Measurement is unavailable")
    stamp = state.get("last_reported") or state.get("last_updated")
    if not isinstance(stamp, str):
        raise ValueError("Measurement has no source timestamp")
    age = now.timestamp() - parse_time(stamp).timestamp()
    if not -30 <= age <= max_age:
        raise ValueError("Measurement is stale or future-dated")
    value = state.get("attributes", {}).get(attribute) if attribute else state.get("state")
    if value is None:
        raise ValueError("Measurement has no numeric value")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Measurement is not finite")
    return number


def temperature_status(
    settings: HeatingSettings, states: dict[str, dict[str, Any]], now: datetime
) -> dict[str, dict[str, Any]]:
    """Retain source age; accepting an unchanged value never makes it a new reading."""
    result = {}
    for room in settings.rooms:
        state = states.get(room.temperature_entity, {})
        stamp = state.get("last_reported") or state.get("last_updated")
        limit = room.temperature_max_age_seconds or settings.measurement_max_age_seconds
        item: dict[str, Any] = {
            "entity": room.temperature_entity,
            "source_at": stamp,
            "max_age_seconds": limit,
            "usable": False,
        }
        try:
            if isinstance(stamp, str):
                item["age_seconds"] = round(now.timestamp() - parse_time(stamp).timestamp())
            if (
                room.availability_entity
                and states.get(room.availability_entity, {}).get("state") != "on"
            ):
                raise ValueError("Temperature integration is unavailable")
            value = read_number(state, now, limit, room.temperature_attribute)
            value += room.temperature_offset_c
            if not 0 <= value <= 45:
                raise ValueError("Temperature outside plausible bounds")
            item.update(temperature_c=value, usable=True, error=None)
        except (ValueError, TypeError) as exc:
            item["error"] = str(exc)
        result[room.id] = item
    return result


def read_temperatures(
    settings: HeatingSettings, states: dict[str, dict[str, Any]], now: datetime
) -> dict[str, float]:
    result = {}
    for room, item in temperature_status(settings, states, now).items():
        if not item["usable"]:
            raise ValueError(f"No usable temperature for {room}")
        result[room] = item["temperature_c"]
    return result


def _signature(state: dict[str, Any]) -> dict[str, Any]:
    attrs = state.get("attributes", {})
    return {
        "hvac_mode": state.get("state"),
        "temperature": attrs.get("temperature"),
        "preset_mode": attrs.get("preset_mode") or "none",
    }


def _room_offset(
    room: Room,
    state: dict[str, Any],
    reading: dict[str, Any],
    room_target: float,
    now: datetime,
    lease: dict[str, Any],
) -> float:
    """Bounded integral correction, updated only on new room reports.

    Seed from the indoor-head/room difference when available. Subsequent steps
    use occupied-room error after a held demand, not hot outlet-air fluctuations.
    A constant or old room report can never accumulate repeated corrections.
    """
    if "setpoint_offset_c" not in lease:
        try:
            head = read_number(state, now, reading["max_age_seconds"], "current_temperature")
            offset = head - reading["temperature_c"]
        except (ValueError, TypeError):
            offset = 0.0
        lease.update(
            setpoint_offset_c=max(
                -room.max_setpoint_offset_c, min(room.max_setpoint_offset_c, offset)
            ),
            feedback_source_at=reading["source_at"],
            feedback_at=now.timestamp(),
        )
    elif (
        reading["source_at"] != lease.get("feedback_source_at")
        and now.timestamp() - lease.get("feedback_at", 0) >= room.feedback_interval_seconds
    ):
        previous_target = lease.get("room_target_c", room_target)
        error = previous_target - reading["temperature_c"]
        held = now.timestamp() - lease.get("last_change", 0) >= room.feedback_interval_seconds
        if held and not lease.get("comfort_paused") and abs(error) > 0.3:
            step = max(-0.5, min(0.5, 0.5 * error))
            lease["setpoint_offset_c"] = max(
                -room.max_setpoint_offset_c,
                min(room.max_setpoint_offset_c, lease["setpoint_offset_c"] + step),
            )
        lease.update(feedback_source_at=reading["source_at"], feedback_at=now.timestamp())
    return float(lease["setpoint_offset_c"])


def commands(
    settings: HeatingSettings,
    proposal: dict[str, Any] | None,
    states: dict[str, dict[str, Any]],
    now: datetime,
    memory: dict[str, Any],
    decisions: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Calculate independent room decisions; observation never emits commands."""
    decisions = decisions if decisions is not None else {}
    readings = temperature_status(settings, states, now)
    for room in settings.rooms:
        decisions[room.id] = {"measurement": readings[room.id], "state": "observing"}
    if settings.mode != "control" or not settings.enabled:
        return []
    if (
        not settings.control_switch_entity
        or states.get(settings.control_switch_entity, {}).get("state") != "on"
    ):
        memory.clear()
        for decision in decisions.values():
            decision["state"] = "disabled"
        return []
    slot = None
    if (
        settings.optimization_enabled
        and (settings.validated_model or settings.allow_estimated_model_control)
        and proposal
        and proposal.get("feasible")
    ):
        age = now.timestamp() - parse_time(proposal["generated_at"]).timestamp()
        if 0 <= age <= settings.plan_max_age_seconds:
            slot = next(
                (
                    s
                    for s in proposal.get("slots", [])
                    if 0 <= now.timestamp() - parse_time(s["at"]).timestamp() < 900
                ),
                None,
            )
    calls = []
    quiet = quiet_at(settings, now)
    for room in settings.rooms:
        decision = decisions[room.id]
        entity = room.climate_entity
        state = states.get(entity, {})
        attrs = state.get("attributes", {})
        if state.get("state") in (None, "unknown", "unavailable"):
            decision["state"] = "device_unavailable"
            continue
        if "heat" not in attrs.get("hvac_modes", []) or (
            quiet and "quiet" not in attrs.get("preset_modes", [])
        ):
            decision["state"] = "unsupported_device"
            continue
        lease = memory.setdefault(entity, {})
        signature = _signature(state)
        previous = lease.get("expected")
        if previous and signature != previous:
            if now.timestamp() <= lease.get("pending_until", 0):
                decision["state"] = "awaiting_acknowledgement"
                continue
            lease.update(
                override_until=now.timestamp() + settings.manual_override_hours * 3600,
                expected=None,
            )
        if now.timestamp() < lease.get("override_until", 0):
            decision.update(state="manual_override", override_until=lease["override_until"])
            continue
        reading = readings[room.id]
        if not reading["usable"] and room.room_feedback and not lease.get("expected"):
            decision["state"] = "waiting_for_temperature"
            continue
        band = comfort_at(settings, room, now)
        comfort_key = [band.min_c, band.max_c, band.fallback_c]
        band_changed = lease.get("comfort_range") != comfort_key
        lease["comfort_range"] = comfort_key
        room_target = (
            band.fallback_c
            if slot is None
            else slot.get("targets_c", {}).get(room.id, band.fallback_c)
        )
        if not isinstance(room_target, (float, int)) or not math.isfinite(room_target):
            room_target = band.fallback_c
        room_target = min(band.max_c, max(band.min_c, room_target))
        temperature = reading.get("temperature_c") if reading["usable"] else None
        decision.update(
            room_target_c=room_target, source="eos" if slot else "comfort", state="active"
        )
        if temperature is None:
            # Stop using a corrected high setpoint when the room sensor is lost.
            # Native thermostats need no remote temperature report. A room with
            # external feedback must already be owned before using this fallback.
            target = band.fallback_c
            decision.update(state="device_fallback", source="device_thermostat")
        else:
            if room.room_feedback and temperature >= band.max_c - settings.comfort_margin_c:
                lease["comfort_paused"] = True
            elif not room.room_feedback or temperature <= room_target - 0.3:
                lease["comfort_paused"] = False
            if lease.get("comfort_paused"):
                decision.update(
                    state="comfort_paused", hvac_mode="off", ac_target_c=signature["temperature"]
                )
                if signature["hvac_mode"] != "off" and "off" in attrs.get("hvac_modes", []):
                    calls.append(
                        {
                            "domain": "climate",
                            "service": "set_hvac_mode",
                            "data": {"entity_id": entity, "hvac_mode": "off"},
                        }
                    )
                    lease.update(
                        expected={**signature, "hvac_mode": "off"},
                        pending_until=now.timestamp() + 120,
                        last_mode_change=now.timestamp(),
                    )
                continue
            offset = (
                _room_offset(room, state, reading, room_target, now, lease)
                if room.room_feedback
                else 0.0
            )
            target = int(math.floor(room_target + offset + 0.5))
            decision["setpoint_offset_c"] = offset
        if (
            signature["hvac_mode"] == "off"
            and now.timestamp() - lease.get("last_mode_change", 0) < 600
        ):
            decision["state"] = "minimum_off_time"
            continue
        minimum, maximum = attrs.get("min_temp", 16), attrs.get("max_temp", 30)
        if (
            attrs.get("target_temp_step", 1) != 1
            or not isinstance(minimum, (int, float))
            or not isinstance(maximum, (int, float))
            or not math.isfinite(minimum)
            or not math.isfinite(maximum)
            or minimum > maximum
        ):
            decision["state"] = "unsupported_temperature_range"
            continue
        target = int(max(minimum, min(maximum, target)))
        urgent = (
            temperature is None
            or temperature < band.min_c
            or temperature >= band.max_c - settings.comfort_margin_c
            or band_changed
        )
        hold = room.feedback_interval_seconds if room.room_feedback else settings.block_minutes * 60
        if (
            (slot is not None or room.room_feedback)
            and not urgent
            and now.timestamp() - lease.get("last_change", 0) < hold
        ):
            target = signature.get("temperature") or target
        if not room.room_feedback:
            # Never carry a previous high target into a lower native comfort band.
            # This also releases persisted offsets when external feedback is disabled.
            target = int(max(minimum, min(maximum, max(band.min_c, min(band.max_c, target)))))
        desired_preset = "quiet" if quiet else "none"
        wanted = {"hvac_mode": "heat", "temperature": target, "preset_mode": desired_preset}
        decision.update(ac_target_c=target, hvac_mode="heat", preset=desired_preset)
        lease["room_target_c"] = room_target
        mode_changed = signature["hvac_mode"] != "heat"
        preset_changed = signature["preset_mode"] != desired_preset
        entity_calls = []
        for key, service in (
            ("hvac_mode", "set_hvac_mode"),
            ("preset_mode", "set_preset_mode"),
            ("temperature", "set_temperature"),
        ):
            force = mode_changed or (key == "temperature" and preset_changed)
            if signature[key] != wanted[key] or force:
                entity_calls.append(
                    {
                        "domain": "climate",
                        "service": service,
                        "data": {"entity_id": entity, key: wanted[key]},
                    }
                )
        if entity_calls:
            calls.extend(entity_calls)
            lease.update(expected=wanted, pending_until=now.timestamp() + 120)
            if target != signature["temperature"]:
                lease["last_change"] = now.timestamp()
            if mode_changed:
                lease["last_mode_change"] = now.timestamp()
    return calls

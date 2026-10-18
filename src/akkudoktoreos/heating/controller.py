"""Home Assistant execution boundary, independent of the optimizer.

Observation mode has no service-call path. Control requires explicit configuration,
a validated model, a live enable helper, fresh inputs and supported climate modes.
Ownership/override leases are persisted by the runtime across restarts.
"""

import math
from datetime import datetime
from typing import Any

from akkudoktoreos.heating.models import HeatingSettings
from akkudoktoreos.heating.planner import quiet_at


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


def read_temperatures(
    settings: HeatingSettings, states: dict[str, dict[str, Any]], now: datetime
) -> dict[str, float]:
    result = {}
    for room in settings.rooms:
        try:
            value = read_number(
                states.get(room.temperature_entity),
                now,
                settings.measurement_max_age_seconds,
                room.temperature_attribute,
            )
            value += room.temperature_offset_c
            if not 0 <= value <= 45:
                raise ValueError("Temperature outside plausible bounds")
            result[room.id] = value
        except (ValueError, TypeError) as exc:
            raise ValueError(f"No usable temperature for {room.id}") from exc
    return result


def _signature(state: dict[str, Any]) -> dict[str, Any]:
    attrs = state.get("attributes", {})
    return {
        "hvac_mode": state.get("state"),
        "temperature": attrs.get("temperature"),
        "preset_mode": attrs.get("preset_mode") or "none",
    }


def commands(
    settings: HeatingSettings,
    proposal: dict[str, Any] | None,
    states: dict[str, dict[str, Any]],
    now: datetime,
    memory: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return allowlisted service calls; do not execute them here."""
    if settings.mode != "control" or not settings.enabled or not settings.validated_model:
        return []
    if (
        not settings.control_switch_entity
        or states.get(settings.control_switch_entity, {}).get("state") != "on"
    ):
        # Turning control off explicitly gives ownership back to the user.
        memory.clear()
        return []
    temperatures = read_temperatures(settings, states, now)
    slot = None
    if proposal and proposal.get("feasible"):
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
        entity = room.climate_entity
        state = states.get(entity, {})
        attrs = state.get("attributes", {})
        if state.get("state") in (None, "unknown", "unavailable"):
            continue
        if "heat" not in attrs.get("hvac_modes", []) or (
            quiet and "quiet" not in attrs.get("preset_modes", [])
        ):
            continue
        lease = memory.setdefault(entity, {})
        signature = _signature(state)
        previous = lease.get("expected")
        if previous and signature != previous and now.timestamp() <= lease.get("pending_until", 0):
            # Await the existing acknowledgement; retrying here would extend
            # the grace period forever and conceal a manual override or failure.
            continue
        if previous and signature != previous and now.timestamp() > lease.get("pending_until", 0):
            lease["override_until"] = now.timestamp() + settings.manual_override_hours * 3600
            lease["expected"] = None
        if now.timestamp() < lease.get("override_until", 0):
            continue
        target = (
            room.fallback_c
            if slot is None
            else slot.get("targets_c", {}).get(room.id, room.fallback_c)
        )
        if not isinstance(target, (int, float)) or not math.isfinite(target):
            target = room.fallback_c
        target = int(min(room.max_c, max(room.min_c, round(target))))
        if temperatures[room.id] < room.min_c:
            target = max(target, room.fallback_c)
        if temperatures[room.id] > room.max_c:
            target = int(room.min_c)
        target_step = attrs.get("target_temp_step", 1)
        if target_step != 1 or not attrs.get("min_temp", 16) <= target <= attrs.get("max_temp", 30):
            continue
        # Minimum hold across re-plans. Quiet boundaries and comfort recovery may
        # interrupt the hold; no relay power cycling is ever used.
        urgent = temperatures[room.id] < room.min_c or temperatures[room.id] > room.max_c
        if (
            slot is not None
            and not urgent
            and now.timestamp() - lease.get("last_change", 0) < settings.block_minutes * 60
        ):
            target = signature.get("temperature") or target
        desired_preset = "quiet" if quiet else "none"
        wanted = {"hvac_mode": "heat", "temperature": target, "preset_mode": desired_preset}
        entity_calls = []
        for key, service in (
            ("hvac_mode", "set_hvac_mode"),
            ("preset_mode", "set_preset_mode"),
            ("temperature", "set_temperature"),
        ):
            if signature.get(key) != wanted[key]:
                entity_calls.append(
                    {
                        "domain": "climate",
                        "service": service,
                        "data": {"entity_id": entity, key: wanted[key]},
                    }
                )
        if entity_calls:
            calls.extend(entity_calls)
            lease["expected"] = wanted
            lease["pending_until"] = now.timestamp() + 120
            if target != signature.get("temperature"):
                lease["last_change"] = now.timestamp()
    return calls

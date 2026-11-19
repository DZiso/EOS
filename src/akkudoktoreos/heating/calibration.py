"""Daily fit of room heat loss and heat capacity from the add-on's own telemetry.

Two ratios of sums per room, so integer thermostat readings average out:

    loss     = heat delivered / degree-hours to outside          [kW/K]
    tau      = degree-hours while coasting / temperature dropped [h]
    capacity = tau * loss                                        [kWh/K]

Both are effective values: household and solar gains are not measured and end up
in a lower loss. That is what makes planned energy match metered energy.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from pydantic import ValidationError

from akkudoktoreos.heating.models import HeatingSettings
from akkudoktoreos.heating.planner import cop_at

KEYS = ("hours", "heat_kwh", "degree_hours", "coast_degree_hours", "coast_drop_k", "phantom_kwh")
SETTLE_SECONDS = 1800  # an indoor unit's own sensor needs this long after its last heat
MIN_DAYS = 3
MIN_COAST_DROP_K = 10  # whole-degree readings: fewer kelvin than this is rounding noise
# ponytail: an idle unit's sensor sits under the ceiling and may cool slower than the
# room, so a very slow fit is capped. A room sensor as temperature_entity lifts that.
TAU_HOURS = (5.0, 100.0)


def day_sums(
    records: Iterable[dict[str, Any]],
    rooms: list[tuple[str, str, str]],
    cop: Callable[[str, float], float],
) -> dict[str, dict[str, float]]:
    """Additive sums for one UTC day; rooms are (room id, climate entity, energy entity).

    Builtins only, so the same source can be run against live records elsewhere.
    """
    sums = {room[0]: dict.fromkeys(KEYS, 0.0) for room in rooms}
    last: dict[str, tuple[int, float, float, float, str, int, int]] = {}
    for record in records:
        stamp = record["at"]
        second = int(stamp[11:13]) * 3600 + int(stamp[14:16]) * 60 + int(stamp[17:19])
        entities = record.get("entities", {})
        for room, climate, meter in rooms:
            state = entities.get(climate, {})
            attrs = state.get("attributes", {})
            mode = state.get("state")
            try:
                outside = float(record["outside_c"])
                reading = float(attrs["current_temperature"])
                energy = float(entities[meter]["state"])
            except (KeyError, TypeError, ValueError):
                last.pop(room, None)
                continue
            if mode not in ("heat", "off"):
                last.pop(room, None)
                continue
            setpoint = attrs.get("temperature")
            # Same cap as the planner input: a heating unit warms its own sensor.
            inside = (
                min(reading, setpoint)
                if mode == "heat" and isinstance(setpoint, (int, float))
                else reading
            )
            previous = last.get(room)
            off_since = mode_since = second
            if previous:
                if previous[4] == mode:
                    mode_since = previous[6]
                hours = (second - previous[0]) / 3600
                used = energy - previous[3]
                if 0 < hours <= 0.25 and 0 <= used <= 1:
                    total = sums[room]
                    total["hours"] += hours
                    total["heat_kwh"] += used * cop(room, outside)
                    total["degree_hours"] += (previous[1] - outside) * hours
                    # A unit's last report arrives minutes after it stops; later ones
                    # mean it runs while Home Assistant shows it off.
                    if mode == "off" and second - mode_since >= SETTLE_SECONDS:
                        total["phantom_kwh"] += used
                    # Energy reported while "off" means the unit was still running.
                    if mode == "off" and previous[4] == "off" and used <= 0.005:
                        off_since = previous[5]
                        if previous[0] - off_since >= SETTLE_SECONDS:
                            total["coast_degree_hours"] += (previous[2] - outside) * hours
                            total["coast_drop_k"] += previous[2] - reading
            last[room] = (second, inside, reading, energy, mode, off_since, mode_since)
    return sums


def fit(days: list[dict[str, dict[str, float]]], settings: HeatingSettings) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for room in settings.rooms:
        rows = [
            row
            for row in (day.get(room.id) for day in days)
            if row and row["hours"] >= 20 and row["phantom_kwh"] <= 0.2
        ]
        total = {key: sum(row[key] for row in rows) for key in KEYS}
        item: dict[str, Any] = {"days": len(rows), "applied": False}
        result[room.id] = item
        if not room.energy_entity:
            item["reason"] = "no energy_entity configured"
            continue
        if len(rows) < MIN_DAYS or total["degree_hours"] < 100 or total["heat_kwh"] <= 0:
            item["reason"] = "not enough data"
            continue
        loss = total["heat_kwh"] / total["degree_hours"]
        # Leave one day out, predict its heat from the others: an out-of-sample check.
        item["day_error"] = round(
            sum(
                abs(
                    (total["heat_kwh"] - row["heat_kwh"])
                    / (total["degree_hours"] - row["degree_hours"])
                    * row["degree_hours"]
                    - row["heat_kwh"]
                )
                for row in rows
            )
            / total["heat_kwh"],
            3,
        )
        tau = room.capacity_kwh_per_k / room.loss_kw_per_k if room.loss_kw_per_k else 33.0
        item["tau_source"] = "configured"
        slowest = TAU_HOURS[1]
        if total["coast_drop_k"] >= MIN_COAST_DROP_K:
            fitted = total["coast_degree_hours"] / total["coast_drop_k"]
            tau, item["tau_source"] = min(slowest, max(TAU_HOURS[0], fitted)), "fitted"
        elif total["coast_degree_hours"] >= MIN_COAST_DROP_K * slowest:
            # Coasted long enough to cool that much even at the slowest rate, and did not.
            tau, item["tau_source"] = slowest, "fitted"
        capacity = tau * loss
        item.update(
            loss_kw_per_k=round(loss, 4), capacity_kwh_per_k=round(capacity, 3), tau_h=round(tau, 1)
        )
        if not (0.002 <= loss <= 2 and 0.2 <= capacity <= 30 and item["day_error"] <= 0.5):
            item["reason"] = "implausible fit"
            continue
        item["applied"] = True
    return result


def apply(settings: HeatingSettings, rooms: dict[str, Any]) -> HeatingSettings:
    """Planner settings with every accepted fit in place of the configured estimate."""
    raw = settings.model_dump()
    for room in raw["rooms"]:
        item = rooms.get(room["id"], {})
        if item.get("applied"):
            room.update(
                loss_kw_per_k=item["loss_kw_per_k"], capacity_kwh_per_k=item["capacity_kwh_per_k"]
            )
    try:
        return HeatingSettings.model_validate(raw)
    except ValidationError:
        return settings


def update(settings: HeatingSettings, directory: Path, now: datetime) -> dict[str, Any]:
    """Refit from every complete stored day. Call once per day, off the event loop."""
    group = {room: g for g in settings.compressors for room in g.rooms}
    rooms = [(r.id, r.climate_entity, r.energy_entity) for r in settings.rooms if r.energy_entity]
    today = "observations-" + now.strftime("%Y-%m-%d") + ".jsonl"
    days = []
    for path in sorted(directory.glob("observations-*.jsonl")):
        if path.name == today:
            continue
        with path.open() as stream:
            records = (json.loads(line) for line in stream if line.endswith("}\n"))
            days.append(day_sums(records, rooms, lambda room, outside: cop_at(group[room], outside)))
    return {"fitted_on": now.strftime("%Y-%m-%d"), "rooms": fit(days, settings)}

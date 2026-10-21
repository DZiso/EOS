"""Receding-horizon search over real thermostat targets, with passive AC storage.

This model estimates heat and electrical input. It does not measure COP. Heat
exchange is energy-conserving, and compressor groups are independent of room
couplings. Controls are hourly/half-hourly targets; the simulation is 15 minutes.
"""

import math
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from akkudoktoreos.heating.models import (
    ComfortRange,
    HeatingSettings,
    PlanningInput,
    Room,
)

# Planned-off sentinel in target rows; Samsung targets start at 16 C.
OFF = 0
# With planned_off: estimated cost per hour of leaving an AC in Heat. Its own
# thermostat still runs where the model predicts no demand; a Heat/Quiet 19 C
# bedroom drew 0.078 kWh/h (about 2.3 ct) on 2026-10-19. Unvalidated estimate.
IDLE_HEAT_COST_PER_HOUR = 0.02
# Cost of each off<->heat switch: an off period must save about three idle hours.
MODE_SWITCH_COST = 0.03


def in_window(hour: int, start: int, end: int) -> bool:
    if start == end:
        return True
    return start <= hour < end if start < end else hour >= start or hour < end


def quiet_at(settings: HeatingSettings, at: datetime) -> bool:
    return in_window(
        at.astimezone(ZoneInfo(settings.timezone)).hour,
        settings.quiet_start_hour,
        settings.quiet_end_hour,
    )


def comfort_at(settings: HeatingSettings, room: Room, at: datetime) -> ComfortRange:
    return room.night_comfort if room.night_comfort and quiet_at(settings, at) else room


def control_blocks(settings: HeatingSettings, data: PlanningInput) -> list[int]:
    """Split target holds at night/day boundaries, including unaligned horizons."""
    indices: list[int] = []
    block = -1
    start = 0
    previous_night = None
    scheduled = any(r.night_comfort is not None for r in settings.rooms)
    for step, slot in enumerate(data.slots):
        night = quiet_at(settings, slot.at) if scheduled else False
        if step == 0 or step - start >= settings.block_minutes // 15 or night != previous_night:
            block += 1
            start = step
        indices.append(block)
        previous_night = night
    return indices


def tariff_at(settings: HeatingSettings, at: datetime) -> float:
    cheap = in_window(
        at.astimezone(ZoneInfo(settings.timezone)).hour,
        settings.cheap_start_hour,
        settings.cheap_end_hour,
    )
    return settings.night_price_per_kwh if cheap else settings.day_price_per_kwh


def simulate(
    settings: HeatingSettings,
    data: PlanningInput,
    targets: list[list[int]],
    detail: bool = False,
    recovery_ceiling: list[list[float]] | None = None,
) -> dict[str, Any]:
    rooms = settings.rooms
    indices = {r.id: i for i, r in enumerate(rooms)}
    temperatures = [data.temperatures_c[r.id] for r in rooms]
    battery = settings.battery if data.energy_mode == "full" else None
    stored = (
        data.battery_soc * battery.capacity_kwh if battery and data.battery_soc is not None else 0
    )
    initial_stored = stored
    cost = violation = heating_kwh = imported = exported = 0.0
    avoidable_violation = margin_violation = 0.0
    temperature_trace = []
    recovering_rooms: set[str] = set()
    records = []
    blocks = control_blocks(settings, data)
    for step, slot in enumerate(data.slots):
        target = targets[blocks[step]]
        quiet = quiet_at(settings, slot.at)
        bands = [comfort_at(settings, r, slot.at) for r in rooms]
        slot_electric = 0.0
        group_energy = {g.id: 0.0 for g in settings.compressors}
        # Five-minute substeps reduce thermostat overshoot and numerical error.
        for substep in range(3):
            dt = 1 / 12
            flows = [
                -r.loss_kw_per_k * (temperatures[i] - slot.outside_c) for i, r in enumerate(rooms)
            ]
            for edge in settings.couplings:
                a, b = indices[edge.a], indices[edge.b]
                flow = edge.kw_per_k * (temperatures[b] - temperatures[a])
                flows[a] += flow
                flows[b] -= flow
            demand = [
                0.0
                if target[i] == OFF
                else min(
                    r.max_heat_kw,
                    max(0.0, (target[i] - temperatures[i]) * r.capacity_kwh_per_k / dt - flows[i]),
                )
                for i, r in enumerate(rooms)
            ]
            heat = [0.0] * len(rooms)
            for group in settings.compressors:
                members = [indices[r] for r in group.rooms]
                requested = sum(demand[i] for i in members)
                cop = max(
                    1.2, min(5.0, group.cop_at_7c + group.cop_slope_per_k * (slot.outside_c - 7))
                )
                if -5 <= slot.outside_c <= 5:
                    cop *= group.defrost_factor
                # Capacity and electricity are limited once per outdoor unit.
                capacity = min(group.max_heat_kw, group.max_electric_kw * cop)
                if not group.min_outside_c <= slot.outside_c <= group.max_outside_c:
                    capacity = 0
                if quiet:
                    capacity *= group.quiet_capacity_factor
                delivered = min(requested, capacity)
                fraction = delivered / requested if requested else 0.0
                for i in members:
                    heat[i] = demand[i] * fraction
                electric = delivered / cop
                if 0 < electric < group.min_electric_kw:
                    # Conservative cycling loss, not an invented continuous minimum load.
                    electric /= 0.85
                standby = group.standby_kw if any(target[i] != OFF for i in members) else 0.0
                electric = min(group.max_electric_kw, electric + standby)
                group_energy[group.id] += electric * dt
                slot_electric += electric * dt
            for i, room in enumerate(rooms):
                temperatures[i] += (flows[i] + heat[i]) * dt / room.capacity_kwh_per_k
                low, high = bands[i].min_c, bands[i].max_c
                actual = max(0.0, low - temperatures[i]) + max(0.0, temperatures[i] - high)
                ceiling = high
                if recovery_ceiling is not None:
                    ceiling = max(high, recovery_ceiling[step * 3 + substep][i])
                avoidable_violation += (
                    max(0.0, low - temperatures[i]) + max(0.0, temperatures[i] - ceiling)
                ) * dt
                violation += actual * dt
                if temperatures[i] > high + 1e-6 and ceiling > high:
                    recovering_rooms.add(room.id)
                preferred = max(0.0, low + settings.comfort_margin_c - temperatures[i]) + max(
                    0.0, temperatures[i] - high + settings.comfort_margin_c
                )
                # The interior margin is a small cost preference, never a hard
                # comfort boundary. In particular, an 18 C minimum means 18 C.
                margin_violation += max(0.0, preferred - actual) * dt
            if detail:
                temperature_trace.append(temperatures[:])
        pv_kw = slot.pv_kw if data.energy_mode != "tariff_only" else 0.0
        net = (slot.baseline_kw - pv_kw) * 0.25 + slot_electric
        battery_charge = battery_discharge = 0.0
        if battery and data.battery_soc is not None:
            cap_soc = slot.battery_max_soc if slot.battery_max_soc is not None else battery.max_soc
            if net < 0:
                battery_charge = min(
                    -net,
                    battery.charge_kw * 0.25,
                    max(0, cap_soc * battery.capacity_kwh - stored) / battery.charge_efficiency,
                )
                stored += battery_charge * battery.charge_efficiency
                net += battery_charge
            else:
                battery_discharge = min(
                    net,
                    battery.discharge_kw * 0.25,
                    max(0, stored - battery.min_soc * battery.capacity_kwh)
                    * battery.discharge_efficiency,
                )
                stored -= battery_discharge / battery.discharge_efficiency
                net -= battery_discharge
        grid_in, grid_out = max(0.0, net), max(0.0, -net)
        # With unknown storage, direct PV still offsets demand. The destination
        # of any surplus is unknown: do not claim export income or stored energy.
        pv_surplus = grid_out if data.energy_mode == "solar_only" else 0.0
        if data.energy_mode == "solar_only":
            grid_out = 0.0
        cost += grid_in * slot.import_price_per_kwh - grid_out * slot.export_price_per_kwh
        heating_kwh += slot_electric
        imported += grid_in
        exported += grid_out
        if detail:
            records.append(
                {
                    "at": slot.at.isoformat(),
                    "targets_c": dict(zip(indices, ("off" if t == OFF else t for t in target))),
                    "temperatures_c": dict(zip(indices, temperatures)),
                    "quiet": quiet,
                    "heating_kwh": slot_electric,
                    "compressor_kwh": group_energy,
                    "grid_import_kwh": grid_in,
                    "grid_export_kwh": grid_out,
                    "unallocated_pv_kwh": pv_surplus,
                    "battery_charge_kwh": battery_charge,
                    "battery_discharge_kwh": battery_discharge,
                    "battery_soc": stored / battery.capacity_kwh if battery else None,
                }
            )
    # Value remaining energy at a conservative replacement price. Prevent the
    # horizon ending with a deliberately cold house or an artificially empty battery.
    replacement = max(0, min(s.import_price_per_kwh for s in data.slots))
    thermal_value = (
        sum(
            max(0, temperatures[i] - comfort_at(settings, r, data.slots[-1].at).min_c)
            * r.capacity_kwh_per_k
            for i, r in enumerate(rooms)
        )
        * replacement
        / 3
    )
    battery_value = (
        (stored - initial_stored) * replacement * (battery.discharge_efficiency if battery else 1)
    )
    changes = sum(
        sum(a != b for a, b in zip(targets[i - 1], targets[i])) for i in range(1, len(targets))
    )
    idle = switches = 0.0
    if settings.planned_off:
        idle = 0.25 * sum(t != OFF for b in blocks for t in targets[b]) * IDLE_HEAT_COST_PER_HOUR
        switches = MODE_SWITCH_COST * sum(
            (a == OFF) != (b == OFF)
            for i in range(1, len(targets))
            for a, b in zip(targets[i - 1], targets[i])
        )
    objective = (
        cost
        - thermal_value
        - battery_value
        + changes * 0.002
        + margin_violation * 0.01
        + idle
        + switches
    )
    return {
        "cost": cost,
        "objective": objective,
        "violation": violation,
        "avoidable_violation": avoidable_violation,
        "recovering_rooms": sorted(recovering_rooms),
        "temperature_trace": temperature_trace,
        "heating_kwh": heating_kwh,
        "grid_import_kwh": imported,
        "grid_export_kwh": exported,
        "slots": records,
    }


def plan(settings: HeatingSettings, data: PlanningInput) -> dict[str, Any]:
    if not settings.rooms:
        raise ValueError("No heating rooms configured")
    if set(data.temperatures_c) != {r.id for r in settings.rooms}:
        raise ValueError("A current temperature is required for every configured room")
    if any(not math.isfinite(t) or not 0 <= t <= 45 for t in data.temperatures_c.values()):
        raise ValueError("Invalid room temperature")
    if data.energy_mode == "full" and settings.battery and data.battery_soc is None:
        raise ValueError("Battery SoC is required; missing does not mean empty")
    if len(data.slots) != settings.horizon_hours * 4:
        raise ValueError("Forecast length must match the configured horizon")
    blocks = control_blocks(settings, data)
    starts = [i for i, block in enumerate(blocks) if i == 0 or block != blocks[i - 1]]
    bands = [[comfort_at(settings, r, data.slots[i].at) for r in settings.rooms] for i in starts]
    count = len(starts)
    fallback = [[band.fallback_c for band in row] for row in bands]
    # Heat-only ACs cannot instantly cool an already warm room or meet a lower
    # night ceiling at the boundary. Compare every five-minute step with the
    # same coupled/shared-compressor model at minimum permitted targets. A
    # schedule may inherit that excess heat, but must not prolong or increase it.
    # Lower comfort limits remain hard: insufficient heating capacity is not
    # excused by this recovery allowance.
    minimum_targets = [[int(band.min_c) for band in row] for row in bands]
    ceiling = simulate(settings, data, minimum_targets, detail=True)["temperature_trace"]
    baseline = simulate(settings, data, fallback, recovery_ceiling=ceiling)

    def score(result: dict[str, Any]) -> tuple[float, float]:
        # Lexicographic priority: price can never purchase a comfort violation.
        return (round(result["avoidable_violation"], 8), result["objective"])

    def search(initial: list[list[int]]) -> tuple[list[list[int]], dict[str, Any]]:
        # Deterministic coordinate descent keeps runtime bounded on a Raspberry Pi.
        # This is a local search, with no claim of a globally optimal schedule.
        targets = [row[:] for row in initial]
        best = simulate(settings, data, targets, recovery_ceiling=ceiling)
        for _ in range(2):
            changed = False
            for block in range(count - 1, -1, -1):
                for i, room in enumerate(settings.rooms):
                    original = chosen = targets[block][i]
                    band = bands[block][i]
                    values = list(range(math.ceil(band.min_c), math.floor(band.max_c) + 1))
                    if settings.planned_off and not room.room_feedback:
                        values.append(OFF)
                    for value in values:
                        if value == original:
                            continue
                        targets[block][i] = value
                        candidate = simulate(settings, data, targets, recovery_ceiling=ceiling)
                        if score(candidate) < score(best):
                            chosen, best = value, candidate
                    targets[block][i] = chosen
                    changed |= chosen != original
            if not changed:
                break
        return targets, best

    targets, best = search(fallback)
    if settings.planned_off:
        # Single-block moves cannot open a multi-hour off period: one off block
        # pays two mode switches. Restart from that plan with every block whose
        # target is at or below the predicted room temperature already off.
        trace = simulate(settings, data, targets, detail=True)["temperature_trace"]
        seed = [
            [
                OFF
                if not room.room_feedback
                and value
                <= (trace[starts[b] * 3 - 1][i] if starts[b] else data.temperatures_c[room.id])
                else value
                for i, (room, value) in enumerate(zip(settings.rooms, row))
            ]
            for b, row in enumerate(targets)
        ]
        off_targets, off_best = search(seed)
        if score(off_best) < score(best):
            targets = off_targets
    result = simulate(settings, data, targets, detail=True, recovery_ceiling=ceiling)
    feasible = result["avoidable_violation"] < 1e-6
    warnings = list(data.warnings)
    if data.energy_mode == "tariff_only":
        warnings.append(
            "Tariff-only estimate: all electricity is priced as grid import; "
            "no solar or battery savings are assumed."
        )
    elif data.energy_mode == "solar_only":
        warnings.append(
            "Solar-only estimate: tariff and direct forecast PV are used; "
            "no battery savings or export revenue are assumed."
        )
    if not settings.validated_model:
        warnings.append(
            "Thermal parameters and COP are unvalidated estimates; savings are not measured."
        )
    if data.energy_mode == "full" and settings.battery and not settings.battery_limits_verified:
        warnings.append("Battery limits are provisional; no battery commands are generated.")
    if not feasible:
        warnings.append(
            "Predicted comfort is infeasible; use the comfort fallback, not this schedule."
        )
    elif result["violation"] > 1e-6:
        warnings.append(
            "Recovery plan: existing heat or a lower night ceiling requires passive cooling in "
            + ", ".join(result["recovering_rooms"])
            + ". The schedule must not add to the excess above the minimum-target trajectory."
        )
    return {
        "generated_at": data.generated_at.isoformat(),
        "mode": settings.mode,
        "energy_mode": data.energy_mode,
        "feasible": feasible,
        "comfort_status": (
            "infeasible"
            if not feasible
            else "recovering"
            if result["violation"] > 1e-6
            else "within_bounds"
        ),
        "recovering_rooms": result["recovering_rooms"],
        "validated_model": settings.validated_model,
        "estimated_cost": result["cost"],
        "baseline_estimated_cost": baseline["cost"],
        "estimated_difference": baseline["cost"] - result["cost"],
        "comfort_violation_degree_hours": result["violation"],
        "avoidable_comfort_violation_degree_hours": result["avoidable_violation"],
        "heating_kwh": result["heating_kwh"],
        "warnings": warnings,
        "slots": result["slots"],
        "battery_control": "existing_self_consumption",
        "gas_available": False,
    }

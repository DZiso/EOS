"""Receding-horizon search over real thermostat targets, with passive AC storage.

This model estimates heat and electrical input. It does not measure COP. Heat
exchange is energy-conserving, and compressor groups are independent of room
couplings. Controls are hourly/half-hourly targets; the simulation is 15 minutes.
"""

import math
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from akkudoktoreos.heating.models import HeatingSettings, PlanningInput


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


def tariff_at(settings: HeatingSettings, at: datetime) -> float:
    cheap = in_window(
        at.astimezone(ZoneInfo(settings.timezone)).hour,
        settings.cheap_start_hour,
        settings.cheap_end_hour,
    )
    return settings.night_price_per_kwh if cheap else settings.day_price_per_kwh


def simulate(
    settings: HeatingSettings, data: PlanningInput, targets: list[list[int]], detail: bool = False
) -> dict[str, Any]:
    rooms = settings.rooms
    indices = {r.id: i for i, r in enumerate(rooms)}
    temperatures = [data.temperatures_c[r.id] for r in rooms]
    battery = settings.battery
    stored = (
        data.battery_soc * battery.capacity_kwh if battery and data.battery_soc is not None else 0
    )
    initial_stored = stored
    cost = violation = heating_kwh = imported = exported = 0.0
    records = []
    steps_per_block = settings.block_minutes // 15
    for step, slot in enumerate(data.slots):
        target = targets[step // steps_per_block]
        quiet = quiet_at(settings, slot.at)
        slot_electric = 0.0
        group_energy = {g.id: 0.0 for g in settings.compressors}
        # Five-minute substeps reduce thermostat overshoot and numerical error.
        for _ in range(3):
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
                min(
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
                electric = min(group.max_electric_kw, electric + group.standby_kw)
                group_energy[group.id] += electric * dt
                slot_electric += electric * dt
            for i, room in enumerate(rooms):
                temperatures[i] += (flows[i] + heat[i]) * dt / room.capacity_kwh_per_k
                low = room.min_c + settings.comfort_margin_c
                high = room.max_c - settings.comfort_margin_c
                violation += (
                    max(0.0, low - temperatures[i]) + max(0.0, temperatures[i] - high)
                ) * dt
        net = (slot.baseline_kw - slot.pv_kw) * 0.25 + slot_electric
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
        cost += grid_in * slot.import_price_per_kwh - grid_out * slot.export_price_per_kwh
        heating_kwh += slot_electric
        imported += grid_in
        exported += grid_out
        if detail:
            records.append(
                {
                    "at": slot.at.isoformat(),
                    "targets_c": dict(zip(indices, target)),
                    "temperatures_c": dict(zip(indices, temperatures)),
                    "quiet": quiet,
                    "heating_kwh": slot_electric,
                    "compressor_kwh": group_energy,
                    "grid_import_kwh": grid_in,
                    "grid_export_kwh": grid_out,
                    "battery_charge_kwh": battery_charge,
                    "battery_discharge_kwh": battery_discharge,
                    "battery_soc": stored / battery.capacity_kwh if battery else None,
                }
            )
    # Value remaining energy at a conservative replacement price. Prevent the
    # horizon ending with a deliberately cold house or an artificially empty battery.
    replacement = max(0, min(s.import_price_per_kwh for s in data.slots))
    thermal_value = (
        sum(max(0, temperatures[i] - r.min_c) * r.capacity_kwh_per_k for i, r in enumerate(rooms))
        * replacement
        / 3
    )
    battery_value = (
        (stored - initial_stored) * replacement * (battery.discharge_efficiency if battery else 1)
    )
    changes = sum(
        sum(a != b for a, b in zip(targets[i - 1], targets[i])) for i in range(1, len(targets))
    )
    objective = cost - thermal_value - battery_value + changes * 0.002
    return {
        "cost": cost,
        "objective": objective,
        "violation": violation,
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
    if settings.battery and data.battery_soc is None:
        raise ValueError("Battery SoC is required; missing does not mean empty")
    if len(data.slots) != settings.horizon_hours * 4:
        raise ValueError("Forecast length must match the configured horizon")
    block_size = settings.block_minutes // 15
    count = math.ceil(len(data.slots) / block_size)
    fallback = [[r.fallback_c for r in settings.rooms] for _ in range(count)]
    targets = [row[:] for row in fallback]
    baseline = simulate(settings, data, fallback)
    best = baseline

    def score(result: dict[str, Any]) -> tuple[float, float]:
        # Lexicographic priority: price can never purchase a comfort violation.
        return (round(result["violation"], 8), result["objective"])

    # Deterministic coordinate descent keeps runtime bounded on a Raspberry Pi.
    # This is a local search, with no claim of a globally optimal schedule.
    for _ in range(2):
        changed = False
        for block in range(count - 1, -1, -1):
            for i, room in enumerate(settings.rooms):
                original = chosen = targets[block][i]
                for value in range(math.ceil(room.min_c), math.floor(room.max_c) + 1):
                    if value == original:
                        continue
                    targets[block][i] = value
                    candidate = simulate(settings, data, targets)
                    if score(candidate) < score(best):
                        chosen, best = value, candidate
                targets[block][i] = chosen
                changed |= chosen != original
        if not changed:
            break
    result = simulate(settings, data, targets, detail=True)
    feasible = result["violation"] < 1e-6
    warnings = list(data.warnings)
    if not settings.validated_model:
        warnings.append("Thermal parameters and COP are unvalidated estimates; observe only.")
    if settings.battery and not settings.battery_limits_verified:
        warnings.append("Battery limits are provisional; no battery commands are generated.")
    if not feasible:
        warnings.append(
            "Predicted comfort is infeasible; use the comfort fallback, not this schedule."
        )
    return {
        "generated_at": data.generated_at.isoformat(),
        "mode": settings.mode,
        "feasible": feasible,
        "validated_model": settings.validated_model,
        "estimated_cost": result["cost"],
        "baseline_estimated_cost": baseline["cost"],
        "estimated_difference": baseline["cost"] - result["cost"],
        "comfort_violation_degree_hours": result["violation"],
        "heating_kwh": result["heating_kwh"],
        "warnings": warnings,
        "slots": result["slots"],
        "battery_control": "existing_self_consumption",
        "gas_available": False,
    }

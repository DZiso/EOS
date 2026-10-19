"""Persistent observation, EOS forecast integration and guarded HA execution."""

import asyncio
import json
import math
import os
import time
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from loguru import logger

from akkudoktoreos.heating.controller import (
    commands,
    parse_time,
    read_number,
    read_temperatures,
    temperature_status,
)
from akkudoktoreos.heating.models import ForecastSlot, HeatingSettings, PlanningInput
from akkudoktoreos.heating.planner import plan, tariff_at


def data_dir() -> Path:
    return Path(os.environ.get("EOS_DATA_DIR", ".")) / "heating"


def settings() -> HeatingSettings:
    path = Path(os.environ.get("EOS_HEATING_CONFIG", "/data/options.json"))
    if not path.exists():
        return HeatingSettings()
    raw = json.loads(path.read_text())
    if path.name == "options.json":
        config = raw.get("heating_config", "{}")
        raw = json.loads(config) if isinstance(config, str) else config
        raw["enabled"] = bool(json.loads(path.read_text()).get("heating_enabled", False))
    return HeatingSettings.model_validate(raw)


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, allow_nan=False, separators=(",", ":")))
    temporary.chmod(0o600)
    temporary.replace(path)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


class CommandDispatchError(RuntimeError):
    """Preserve partial dispatch evidence without leaking HTTP credentials."""

    def __init__(
        self, sent: int | None, confirmed: int, attempted: int, entity: str = "climate device"
    ) -> None:
        super().__init__(
            f"Home Assistant did not confirm the heating command sequence for {entity}"
        )
        self.sent = sent
        self.confirmed = confirmed
        self.attempted = attempted


class ControlDisabled(CommandDispatchError):
    """The live enable switch was turned off during a command sequence."""


class HAClient:
    """Use the existing Supervisor token, never include credentials in results."""

    def __init__(self) -> None:
        self.base = os.environ.get("EOS_HEATING_HA_URL", "http://supervisor/core/api").rstrip("/")
        token = os.environ.get("EOS_HEATING_HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            raise ValueError("Home Assistant credentials are not configured")
        self.token: str = token

    def request(self, method: str, path: str, body: Any = None) -> Any:
        response = requests.request(
            method,
            self.base + path,
            json=body,
            headers={"Authorization": "Bearer " + self.token},
            timeout=20,
        )
        response.raise_for_status()
        return response.json()

    def states(self) -> dict[str, dict[str, Any]]:
        return {s["entity_id"]: s for s in self.request("GET", "/states")}

    ack_timeout_seconds = 30.0

    def wait_for_ack(self, entity: str, expected: dict[str, Any]) -> None:
        deadline = time.monotonic() + self.ack_timeout_seconds
        while True:
            state = self.request("GET", "/states/" + entity)
            actual = {
                "hvac_mode": state.get("state"),
                "preset_mode": state.get("attributes", {}).get("preset_mode") or "none",
                "temperature": state.get("attributes", {}).get("temperature"),
            }
            if all(actual.get(key) == value for key, value in expected.items()):
                return
            if time.monotonic() >= deadline:
                raise ValueError("Heating command acknowledgement timed out")
            time.sleep(1)

    def apply(self, calls: list[dict[str, Any]], enable_entity: str | None = None) -> int:
        fields = {
            "set_hvac_mode": "hvac_mode",
            "set_preset_mode": "preset_mode",
            "set_temperature": "temperature",
        }
        # Validate the entire batch before making any changes.
        for call in calls:
            if (
                call["domain"] != "climate"
                or call["service"] not in fields
                or not call["data"].get("entity_id", "").startswith("climate.")
            ):
                raise ValueError("Unsupported heating command")
        sent = confirmed = attempted = 0
        expected: dict[str, dict[str, Any]] = {}
        for call in calls:
            entity = call["data"]["entity_id"]
            if enable_entity:
                try:
                    enabled = self.request("GET", "/states/" + enable_entity).get("state") == "on"
                except Exception:
                    enabled = False
                if not enabled:
                    raise ControlDisabled(sent, confirmed, attempted, entity)
            attempted += 1
            try:
                self.request("POST", "/services/climate/" + call["service"], call["data"])
            except Exception:
                # A timeout can occur after HA accepted the command.
                raise CommandDispatchError(None, confirmed, attempted, entity) from None
            sent += 1
            expected.setdefault(entity, {})[fields[call["service"]]] = call["data"][
                fields[call["service"]]
            ]
            try:
                # Wait for cloud feedback before the next dependent command.
                self.wait_for_ack(entity, expected[entity])
            except Exception:
                raise CommandDispatchError(sent, confirmed, attempted, entity) from None
            confirmed += 1
        return confirmed


def interpolate(points: list[tuple[float, float]], stamp: float, solar: bool = False) -> float:
    if not points or stamp < points[0][0] or stamp > points[-1][0]:
        raise ValueError("Forecast does not cover the planning horizon")
    pos = bisect_right([p[0] for p in points], stamp)
    if pos == len(points):
        return points[-1][1]
    left, right = points[pos - 1], points[pos]
    gap = right[0] - left[0]
    if gap > 10800 and not (solar and left[1] == right[1] == 0 and gap <= 64800):
        raise ValueError("Forecast contains an unsupported gap")
    return left[1] + (right[1] - left[1]) * (stamp - left[0]) / gap


async def bootstrap_forecasts(config: HeatingSettings, client: HAClient) -> None:
    """Initialize a new heating installation; preserve subsequent EOS UI edits."""
    from akkudoktoreos.core.coreabc import get_config, get_ems
    from akkudoktoreos.core.emsettings import EnergyManagementMode

    marker = data_dir() / "forecast-setup.json"
    if marker.exists() or not config.solar_planes:
        return
    eos_config = get_config()
    site = await asyncio.to_thread(client.request, "GET", "/config")
    update: dict[str, Any] = {
        "ems": {"mode": "PREDICTION", "interval": 3600},
        "prediction": {"hours": 48},
    }
    if eos_config.general.latitude is None or eos_config.general.longitude is None:
        update["general"] = {"latitude": site["latitude"], "longitude": site["longitude"]}
    if eos_config.weather.provider is None:
        update["weather"] = {"provider": "BrightSky"}
    if eos_config.pvforecast.provider is None:
        update["pvforecast"] = {
            "provider": "PVForecastForecastSolar",
            "planes": [p.model_dump() for p in config.solar_planes],
        }
    eos_config.merge_settings_from_dict(update)
    eos_config.to_config_file()
    save_json(marker, {"configured_at": datetime.now(timezone.utc).isoformat()})
    await get_ems().run(mode=EnergyManagementMode.PREDICTION)


_last_forecast_recovery = float("-inf")


async def forecast_points(key: str, now: datetime) -> list[tuple[float, float]]:
    from akkudoktoreos.core.coreabc import get_ems, get_prediction
    from akkudoktoreos.core.emsettings import EnergyManagementMode

    global _last_forecast_recovery

    prediction = get_prediction()
    source = None
    for provider in prediction.enabled_providers:
        if key in provider.record_keys:
            source = provider
            break
    if source is not None and source.update_datetime is None:
        # Cached BrightSky data can survive restart without its in-memory
        # retrieval timestamp. Fetch real new data; never relabel cached data fresh.
        # The EMS lock serializes this with scheduled forecast refreshes.
        if time.monotonic() - _last_forecast_recovery >= 900:
            _last_forecast_recovery = time.monotonic()
            await get_ems().run(mode=EnergyManagementMode.PREDICTION, force_update=True)
    if source is None or source.update_datetime is None:
        raise ValueError(f"No updated EOS forecast provider for {key}")
    age = now.timestamp() - source.update_datetime.timestamp()
    if not -60 <= age <= 3 * 3600:
        raise ValueError(f"EOS forecast for {key} is stale")
    series = await prediction.key_to_raw_series(key)
    points = sorted(
        (parse_time(str(dt)).timestamp(), float(value))
        for dt, value in series.items()
        if math.isfinite(float(value))
    )
    return points


def battery_status(
    config: HeatingSettings, states: dict[str, dict[str, Any]], now: datetime
) -> dict[str, Any]:
    """Report uncertainty without treating a cached unchanged SoC as a new report."""
    battery = config.battery
    report: dict[str, Any] = {
        "usable": True,
        "state": "not_configured",
        "measurements": {},
        "error": None,
    }
    if battery is None:
        return report
    report["state"] = "fresh"
    values = {}
    for attr, entity, scale in (
        ("soc", battery.soc_entity, 100),
        ("max_soc", battery.max_soc_entity, 100),
        ("charge_kw", battery.charge_limit_entity, 1000),
        ("discharge_kw", battery.discharge_limit_entity, 1000),
    ):
        if not entity:
            continue
        state = states.get(entity, {})
        stamp = state.get("last_reported") or state.get("last_updated")
        measurement: dict[str, Any] = {
            "entity": entity,
            "source_at": stamp,
            "last_value": state.get("state"),
            "usable": False,
        }
        report["measurements"][attr] = measurement
        try:
            if isinstance(stamp, str):
                measurement["age_seconds"] = round(now.timestamp() - parse_time(stamp).timestamp())
            value = read_number(state, now, config.measurement_max_age_seconds) / scale
            if attr == "soc" and not 0 <= value <= 1:
                raise ValueError("Battery SoC is outside 0–100%")
            if attr != "soc":
                type(battery).model_validate({**battery.model_dump(), attr: value})
            values[attr] = value
            measurement.update(usable=True, value=value)
        except (ValueError, TypeError, OverflowError) as exc:
            error = f"{entity}: {exc}"
            measurement["error"] = error
            report.update(usable=False, state="uncertain", error=error)
    if battery.connection_entity:
        entity = battery.connection_entity
        state = states.get(entity, {})
        stamp = state.get("last_reported") or state.get("last_updated")
        health: dict[str, Any] = {
            "entity": entity,
            "source_at": stamp,
            "last_value": state.get("state"),
            "usable": False,
        }
        report["measurements"]["connection"] = health
        try:
            if not isinstance(stamp, str):
                raise ValueError("Battery connection status has no source timestamp")
            age = now.timestamp() - parse_time(stamp).timestamp()
            health["age_seconds"] = round(age)
            if age < -30:
                raise ValueError("Battery connection status is future-dated")
            if state.get("state") not in battery.connection_healthy_states:
                raise ValueError("Battery integration reports an unhealthy or unknown connection")
            # Status codes are persistent modes, not heartbeat reports. Healthy
            # status never renews the independently checked SoC timestamp.
            health["usable"] = True
        except (ValueError, TypeError, OverflowError) as exc:
            error = f"{entity}: {exc}"
            health["error"] = error
            report.update(usable=False, state="uncertain", error=error)
    report["values"] = values
    return report


async def planning_input(
    config: HeatingSettings, states: dict[str, dict[str, Any]], now: datetime
) -> PlanningInput:
    temperatures = read_temperatures(config, states, now)
    soc = None
    warnings = [
        "Household baseline is a configured estimate excluding HVAC; verify against observations."
    ]
    battery = config.battery
    report = battery_status(config, states, now)
    battery_uncertain = not report["usable"]
    if battery_uncertain:
        # TODO: Monitor Zendure connection-status history during daylight/solar
        # charging for intermittent disconnects. This fallback must not hide them:
        # an old SoC can mean either normal idle operation or lost communication.
        # Keep its real timestamp and uncertainty visible; never refresh cached data.
        warnings.append("Battery data uncertain; no battery savings assumed. " + report["error"])
    elif battery:
        values = report["values"]
        soc = values["soc"]
        updates = {key: value for key, value in values.items() if key != "soc"}
        config.battery = type(battery).model_validate({**battery.model_dump(), **updates})
    outside = await forecast_points("weather_temp_air", now)
    # UTC arithmetic creates 96 real quarter-hours even across DST transitions.
    start = datetime.fromtimestamp(math.floor(now.timestamp() / 900) * 900, timezone.utc)
    solar_powers = None
    try:
        solar = await forecast_points("pvforecast_ac_power", now)
        # Extend sunrise/sunset zeroes only to boundaries of forecast-covered days.
        local_zone = ZoneInfo(config.timezone)
        if solar and solar[0][1] == 0:
            first_day = datetime.fromtimestamp(solar[0][0], local_zone).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            if first_day.timestamp() < solar[0][0]:
                solar.insert(0, (first_day.timestamp(), 0))
        if solar and solar[-1][1] == 0:
            next_day = datetime.fromtimestamp(solar[-1][0], local_zone).replace(
                hour=0, minute=0, second=0, microsecond=0
            ) + timedelta(days=1)
            if next_day.timestamp() > solar[-1][0]:
                solar.append((next_day.timestamp(), 0))
        # Validate the entire horizon before granting any direct solar credit.
        solar_powers = [
            max(0, interpolate(solar, (start + timedelta(minutes=15 * i)).timestamp(), solar=True))
            / 1000
            for i in range(config.horizon_hours * 4)
        ]
    except Exception as exc:
        if not battery_uncertain:
            raise
        warnings.append("Solar forecast unavailable; tariff-only planning. " + safe_error(exc))
    slots = []
    for i in range(config.horizon_hours * 4):
        at = start + timedelta(minutes=15 * i)
        slots.append(
            ForecastSlot(
                at=at,
                outside_c=interpolate(outside, at.timestamp()),
                pv_kw=solar_powers[i] if solar_powers is not None else 0,
                baseline_kw=config.baseline_kw,
                import_price_per_kwh=tariff_at(config, at),
                export_price_per_kwh=config.export_price_per_kwh,
            )
        )
    return PlanningInput(
        generated_at=now,
        temperatures_c=temperatures,
        battery_soc=soc,
        energy_mode=(
            "full"
            if not battery_uncertain
            else "solar_only"
            if solar_powers is not None
            else "tariff_only"
        ),
        slots=slots,
        warnings=warnings,
    )


def record_observation(
    config: HeatingSettings,
    states: dict[str, dict[str, Any]],
    now: datetime,
    proposal: dict[str, Any] | None,
    control: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entities = {r.temperature_entity for r in config.rooms} | {
        r.climate_entity for r in config.rooms
    }
    entities |= set(config.hvac_energy_entities)
    if config.house_power_entity:
        entities.add(config.house_power_entity)
    if config.battery:
        entities.add(config.battery.soc_entity)
        if config.battery.connection_entity:
            entities.add(config.battery.connection_entity)
    observation: dict[str, Any] = {"at": now.isoformat(), "entities": {}}
    # Record only configured telemetry; never dump all household entities.
    for entity in sorted(entities):
        state = states.get(entity, {})
        attrs = state.get("attributes", {})
        observation["entities"][entity] = {
            "state": state.get("state"),
            "last_reported": state.get("last_reported", state.get("last_updated")),
            "attributes": {
                k: attrs[k]
                for k in (
                    "temperature",
                    "current_temperature",
                    "preset_mode",
                    "unit_of_measurement",
                )
                if k in attrs
            },
        }
    quality = temperature_status(config, states, now)
    observation["measurements"] = quality
    observation["temperatures_c"] = {
        room: item["temperature_c"] for room, item in quality.items() if item["usable"]
    }
    if not all(item["usable"] for item in quality.values()):
        observation["temperature_quality"] = "partially_unavailable_or_stale"
    observation["control"] = control
    if proposal:
        observation["proposal_generated_at"] = proposal.get("generated_at")
        observation["error_kind"] = "proposal_comparison_is_not_model_validation"
    path = data_dir() / ("observations-" + now.strftime("%Y-%m-%d") + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        stream.write(json.dumps(observation, allow_nan=False) + "\n")
    path.chmod(0o600)
    for old in path.parent.glob("observations-*.jsonl"):
        if now.timestamp() - old.stat().st_mtime > 30 * 86400:
            old.unlink()
    return observation


_lock = asyncio.Lock()
_last_plan_attempt = 0.0
_last_planning_mode: str | None = None
_last_planning_error: str | None = None


def safe_error(exc: Exception) -> str:
    return str(exc) if isinstance(exc, (ValueError, CommandDispatchError)) else type(exc).__name__


async def heating_tick() -> None:
    """Keep the room thermostat running independently of forecast failures."""
    global _last_plan_attempt, _last_planning_error, _last_planning_mode
    if _lock.locked():
        return
    async with _lock:
        now = datetime.now(timezone.utc)
        config = None
        states: dict[str, dict[str, Any]] = {}
        memory = load_json(data_dir() / "ownership.json", {})
        proposal = load_json(data_dir() / "plan.json", None)
        status: dict[str, Any] = {
            "at": now.isoformat(),
            "state": "waiting_for_data",
            "mode": "disabled",
            "commands_sent": 0,
            "commands_confirmed": 0,
            "commands_attempted": 0,
            "rooms": {},
            "error": None,
        }
        try:
            config = settings()
            status.update(enabled=config.enabled, mode=config.mode)
            if not config.enabled:
                status["state"] = "disabled"
                save_json(data_dir() / "status.json", status)
                return
            client = HAClient()
            states = await asyncio.to_thread(client.states)
            readings = temperature_status(config, states, now)
            usable = all(item["usable"] for item in readings.values())
            status["temperatures_c"] = {
                room: item["temperature_c"] for room, item in readings.items() if item["usable"]
            }
            status["battery"] = battery_status(config, states, now)
            # Battery changes invalidate incompatible plans immediately. PV is
            # checked independently during planning; a PV outage retries in five
            # minutes without making every control tick a forecast request.
            requested_mode = "full" if status["battery"]["usable"] else "solar_only"
            allowed_modes = (
                {"full"} if status["battery"]["usable"] else {"solar_only", "tariff_only"}
            )
            energy_mode = requested_mode
            status["energy_mode"] = energy_mode
            # Failed forecast attempts retry at most every five minutes. Missing
            # room values still prevent optimization but never stop other rooms.
            interval = (
                300
                if _last_planning_error
                or proposal is None
                or proposal.get("energy_mode") == "tariff_only"
                else 900
            )
            if (
                now.timestamp() - _last_plan_attempt >= interval
                or requested_mode != _last_planning_mode
            ):
                _last_plan_attempt = now.timestamp()
                _last_planning_mode = requested_mode
                try:
                    await bootstrap_forecasts(config, client)
                    inputs = await planning_input(config, states, now)
                    proposal = await asyncio.to_thread(plan, config, inputs)
                    save_json(data_dir() / "input.json", inputs.model_dump(mode="json"))
                    save_json(data_dir() / "plan.json", proposal)
                    _last_planning_error = None
                except Exception as exc:
                    _last_planning_error = safe_error(exc)
                    logger.warning("Heating planning unavailable: {}", _last_planning_error)
            if proposal and proposal.get("energy_mode", "full") in allowed_modes:
                energy_mode = proposal.get("energy_mode", "full")
            status["energy_mode"] = energy_mode
            status["planning_error"] = _last_planning_error
            status["last_plan_at"] = proposal.get("generated_at") if proposal else None
            fresh = bool(
                proposal
                and usable
                and not _last_planning_error
                and proposal.get("energy_mode", "full") in allowed_modes
                and 0
                <= now.timestamp() - parse_time(proposal["generated_at"]).timestamp()
                <= config.plan_max_age_seconds
            )
            status["plan_fresh"] = fresh
            status["plan_accepted"] = bool(fresh and proposal and proposal.get("feasible"))
            status["comfort_status"] = (
                proposal.get("comfort_status") if fresh and proposal else None
            )
            status["recovering_rooms"] = (
                proposal.get("recovering_rooms", []) if fresh and proposal else []
            )
            decisions: dict[str, Any] = {}
            calls = commands(config, proposal if fresh else None, states, now, memory, decisions)
            status["rooms"] = decisions
            status["state"] = "observing" if config.mode == "observe" else "controlling"
            if config.mode == "observe" and (not usable or _last_planning_error):
                status.update(
                    state="waiting_for_data",
                    error=_last_planning_error or "Some room temperatures are unavailable",
                )
            if config.mode == "control" and all(
                d["state"] == "disabled" for d in decisions.values()
            ):
                status["state"] = "disabled"
            # Persist the intended signatures before dispatch, including partial
            # failures, so a restart cannot cause an immediate blind retry.
            save_json(data_dir() / "ownership.json", memory)
            by_entity: dict[str, list[dict[str, Any]]] = {}
            for call in calls:
                by_entity.setdefault(call["data"]["entity_id"], []).append(call)
            for entity, batch in by_entity.items():
                room_id = next(r.id for r in config.rooms if r.climate_entity == entity)
                decision = decisions[room_id]
                try:
                    await asyncio.to_thread(client.apply, batch, config.control_switch_entity)
                    result: dict[str, int | None] = {
                        "sent": len(batch),
                        "confirmed": len(batch),
                        "attempted": len(batch),
                    }
                except CommandDispatchError as exc:
                    result = {
                        "sent": exc.sent,
                        "confirmed": exc.confirmed,
                        "attempted": exc.attempted,
                    }
                    if isinstance(exc, ControlDisabled):
                        memory.clear()
                        status["state"] = "disabled"
                        for item in decisions.values():
                            item["state"] = "disabled"
                    else:
                        decision.update(state="command_error", error=str(exc))
                        status.update(state="command_error", error=str(exc))
                decision["dispatch"] = result
                for key in ("sent", "confirmed", "attempted"):
                    field = "commands_" + key
                    status[field] = (
                        None
                        if result[key] is None or status[field] is None
                        else status[field] + result[key]
                    )
                save_json(data_dir() / "ownership.json", memory)
                if status["state"] == "disabled":
                    break
            status["fallback_active"] = any(d.get("source") != "eos" for d in decisions.values())
        except Exception as exc:
            status["state"] = "cycle_error"
            status["error"] = safe_error(exc)
            logger.warning("Heating cycle unavailable: {}", status["error"])
        if config:
            await asyncio.to_thread(record_observation, config, states, now, proposal, status)
        save_json(data_dir() / "status.json", status)

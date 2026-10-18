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

    def apply(self, calls: list[dict[str, Any]]) -> int:
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


async def forecast_points(key: str, now: datetime) -> list[tuple[float, float]]:
    from akkudoktoreos.core.coreabc import get_prediction

    prediction = get_prediction()
    source = None
    for provider in prediction.enabled_providers:
        if key in provider.record_keys:
            source = provider
            break
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


async def planning_input(
    config: HeatingSettings, states: dict[str, dict[str, Any]], now: datetime
) -> PlanningInput:
    temperatures = read_temperatures(config, states, now)
    soc = None
    warnings = [
        "Household baseline is a configured estimate excluding HVAC; verify against observations."
    ]
    battery = config.battery
    if battery:
        soc = (
            read_number(states.get(battery.soc_entity), now, config.measurement_max_age_seconds)
            / 100
        )
        # Live power/SoC limits are read when configured. Never silently replace
        # an unavailable limit with zero, and never issue commands to the battery.
        updates = {}
        for attr, entity, scale in (
            ("max_soc", battery.max_soc_entity, 100),
            ("charge_kw", battery.charge_limit_entity, 1000),
            ("discharge_kw", battery.discharge_limit_entity, 1000),
        ):
            if entity:
                updates[attr] = (
                    read_number(states.get(entity), now, config.measurement_max_age_seconds) / scale
                )
        if updates:
            config.battery = type(battery).model_validate({**battery.model_dump(), **updates})
    outside = await forecast_points("weather_temp_air", now)
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
    # UTC arithmetic creates 96 real quarter-hours even across DST transitions.
    start = datetime.fromtimestamp(math.floor(now.timestamp() / 900) * 900, timezone.utc)
    slots = []
    for i in range(config.horizon_hours * 4):
        at = start + timedelta(minutes=15 * i)
        slots.append(
            ForecastSlot(
                at=at,
                outside_c=interpolate(outside, at.timestamp()),
                pv_kw=max(0, interpolate(solar, at.timestamp(), solar=True)) / 1000,
                baseline_kw=config.baseline_kw,
                import_price_per_kwh=tariff_at(config, at),
                export_price_per_kwh=config.export_price_per_kwh,
            )
        )
    return PlanningInput(
        generated_at=now,
        temperatures_c=temperatures,
        battery_soc=soc,
        slots=slots,
        warnings=warnings,
    )


def record_observation(
    config: HeatingSettings,
    states: dict[str, dict[str, Any]],
    now: datetime,
    proposal: dict[str, Any] | None,
) -> dict[str, Any]:
    entities = {r.temperature_entity for r in config.rooms} | {
        r.climate_entity for r in config.rooms
    }
    entities |= set(config.hvac_energy_entities)
    if config.house_power_entity:
        entities.add(config.house_power_entity)
    if config.battery:
        entities.add(config.battery.soc_entity)
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
    try:
        actual = read_temperatures(config, states, now)
        observation["temperatures_c"] = actual
        if proposal:
            # A slot's temperature is its end temperature, not its starting value.
            point = min(
                proposal.get("slots", []),
                key=lambda s: abs(parse_time(s["at"]).timestamp() + 900 - now.timestamp()),
                default=None,
            )
            if point and abs(parse_time(point["at"]).timestamp() + 900 - now.timestamp()) < 120:
                observation["prediction_error_c"] = {
                    r: actual[r] - point["temperatures_c"][r] for r in actual
                }
                observation["error_kind"] = (
                    "proposed_schedule_not_applied"
                    if config.mode == "observe"
                    else "controlled_schedule"
                )
    except ValueError:
        observation["temperature_quality"] = "unavailable_or_stale"
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


async def heating_tick() -> None:
    """Collect once per minute and re-plan every fifteen minutes."""
    global _last_plan_attempt
    if _lock.locked():
        return
    async with _lock:
        now = datetime.now(timezone.utc)
        config = None
        states: dict[str, dict[str, Any]] = {}
        client = None
        memory = load_json(data_dir() / "ownership.json", {})
        proposal = load_json(data_dir() / "plan.json", None)
        try:
            config = settings()
            if not config.enabled:
                return
            client = HAClient()
            await bootstrap_forecasts(config, client)
            states = await asyncio.to_thread(client.states)
            observation = await asyncio.to_thread(record_observation, config, states, now, proposal)
            # Quality can deteriorate between 15-minute planning runs.
            # Mark it immediately rather than displaying an old plan as ready.
            read_temperatures(config, states, now)
            previous_status = load_json(data_dir() / "status.json", {})
            if (
                now.timestamp() - _last_plan_attempt >= 900
                or proposal is None
                or previous_status.get("state") == "waiting_for_data"
            ):
                _last_plan_attempt = now.timestamp()
                inputs = await planning_input(config, states, now)
                proposal = await asyncio.to_thread(plan, config, inputs)
                save_json(data_dir() / "input.json", inputs.model_dump(mode="json"))
                save_json(data_dir() / "plan.json", proposal)
            calls = commands(config, proposal, states, now, memory)
            # Persist expected command state before dispatch. A partial cloud
            # failure is then recognized on subsequent reads, including restarts.
            save_json(data_dir() / "ownership.json", memory)
            if calls:
                await asyncio.to_thread(client.apply, calls)
            save_json(
                data_dir() / "status.json",
                {
                    "at": now.isoformat(),
                    "enabled": True,
                    "mode": config.mode,
                    "state": "observing" if config.mode == "observe" else "controlling",
                    "last_plan_at": proposal.get("generated_at") if proposal else None,
                    "temperatures_c": observation.get("temperatures_c"),
                    "commands_sent": len(calls),
                    "commands_confirmed": len(calls),
                    "error": None,
                },
            )
        except Exception as exc:
            # Do not leak request URLs or tokens from third-party exceptions.
            error = (
                str(exc)
                if isinstance(exc, (ValueError, CommandDispatchError))
                else type(exc).__name__
            )
            logger.warning("Heating cycle unavailable: {}", error)
            status = {
                "at": now.isoformat(),
                "state": "command_error"
                if isinstance(exc, CommandDispatchError)
                else "waiting_for_data",
                "mode": config.mode if config else "disabled",
                "error": error,
                "commands_sent": exc.sent if isinstance(exc, CommandDispatchError) else 0,
                "commands_confirmed": exc.confirmed if isinstance(exc, CommandDispatchError) else 0,
                "commands_attempted": exc.attempted if isinstance(exc, CommandDispatchError) else 0,
            }
            if config and client and states and not isinstance(exc, CommandDispatchError):
                try:
                    fallback_calls = commands(config, None, states, now, memory)
                    save_json(data_dir() / "ownership.json", memory)
                    if fallback_calls:
                        await asyncio.to_thread(client.apply, fallback_calls)
                        status.update(
                            commands_sent=len(fallback_calls),
                            commands_confirmed=len(fallback_calls),
                            commands_attempted=len(fallback_calls),
                            fallback_active=True,
                        )
                except CommandDispatchError as fallback_error:
                    status.update(
                        state="command_error",
                        commands_sent=fallback_error.sent,
                        commands_confirmed=fallback_error.confirmed,
                        commands_attempted=fallback_error.attempted,
                    )
                except Exception:
                    logger.warning("Heating comfort fallback unavailable; device targets retained")
            save_json(data_dir() / "status.json", status)

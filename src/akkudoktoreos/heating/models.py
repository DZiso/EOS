"""Validated inputs for thermal planning; powers are kW and energies are kWh."""

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ComfortRange(Model):
    min_c: float = Field(default=19, ge=16, le=28)
    max_c: float = Field(default=22, ge=17, le=30)
    fallback_c: int = Field(default=20, ge=16, le=30)

    @model_validator(mode="after")
    def bounds(self) -> "ComfortRange":
        if not self.min_c < self.max_c or not self.min_c <= self.fallback_c <= self.max_c:
            raise ValueError("Require min_c < max_c and fallback within comfort range")
        if self.min_c % 1 or self.max_c % 1:
            raise ValueError("Samsung targets require integer comfort limits")
        return self


class Room(ComfortRange):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    climate_entity: str = Field(pattern=r"^climate\.[a-z0-9_]+$")
    temperature_entity: str = Field(pattern=r"^(sensor|climate)\.[a-z0-9_]+$")
    temperature_attribute: str | None = None
    temperature_offset_c: float = Field(default=0, ge=-5, le=5)
    temperature_max_age_seconds: int | None = Field(default=None, ge=60, le=21600)
    availability_entity: str | None = Field(default=None, pattern=r"^binary_sensor\.[a-z0-9_]+$")
    room_feedback: bool = False
    max_setpoint_offset_c: float = Field(default=5, ge=0, le=8)
    feedback_interval_seconds: int = Field(default=1800, ge=900, le=7200)
    capacity_kwh_per_k: float = Field(default=2, ge=0.2, le=30)
    loss_kw_per_k: float = Field(default=0.06, ge=0, le=2)
    max_heat_kw: float = Field(default=3.2, gt=0, le=20)
    night_comfort: ComfortRange | None = Field(
        default=None, description="Optional comfort range during the configured quiet hours."
    )


class Coupling(Model):
    a: str
    b: str
    kw_per_k: float = Field(ge=0, le=2)


class Compressor(Model):
    id: str
    rooms: list[str] = Field(min_length=1, max_length=4)
    max_heat_kw: float = Field(default=5.6, gt=0, le=30)
    max_electric_kw: float = Field(default=2.5, gt=0, le=15)
    quiet_capacity_factor: float = Field(default=0.65, gt=0, le=1)
    min_electric_kw: float = Field(default=0.25, ge=0, le=5)
    standby_kw: float = Field(default=0.01, ge=0, le=0.2)
    cop_at_7c: float = Field(default=3.5, ge=1, le=7)
    cop_slope_per_k: float = Field(default=0.06, ge=0, le=0.2)
    defrost_factor: float = Field(default=0.85, gt=0, le=1)
    min_outside_c: float = Field(default=-15, ge=-40, le=5)
    max_outside_c: float = Field(default=24, ge=10, le=40)


class Battery(Model):
    capacity_kwh: float = Field(default=1.92, gt=0, le=200)
    min_soc: float = Field(default=0.1, ge=0, le=1)
    max_soc: float = Field(default=0.8, gt=0, le=1)
    charge_kw: float = Field(default=1.2, ge=0, le=30)
    discharge_kw: float = Field(default=0.2, ge=0, le=30)
    charge_efficiency: float = Field(default=0.95, gt=0, le=1)
    discharge_efficiency: float = Field(default=0.95, gt=0, le=1)
    soc_entity: str = "sensor.hyper_2000_electric_level"
    max_soc_entity: str | None = None
    charge_limit_entity: str | None = None
    discharge_limit_entity: str | None = None
    connection_entity: str | None = Field(
        default=None,
        description="Optional integration status; an unhealthy state vetoes battery credit.",
    )
    connection_healthy_states: list[str] = Field(
        default_factory=lambda: ["on"],
        min_length=1,
        description="Exact healthy states, including any valid operating-mode codes. This is not a heartbeat.",
    )

    @model_validator(mode="after")
    def bounds(self) -> "Battery":
        if self.min_soc >= self.max_soc:
            raise ValueError("Battery min_soc must be below max_soc")
        return self


class SolarPlane(Model):
    surface_tilt: float = Field(ge=0, le=90)
    surface_azimuth: float = Field(ge=0, lt=360)
    peakpower: float = Field(gt=0, le=500)


class HeatingSettings(Model):
    enabled: bool = False
    mode: Literal["observe", "control"] = "observe"
    timezone: str = "Europe/Berlin"
    rooms: list[Room] = Field(default_factory=list, max_length=8)
    compressors: list[Compressor] = Field(default_factory=list, max_length=4)
    couplings: list[Coupling] = Field(default_factory=list)
    battery: Battery | None = None
    day_price_per_kwh: float = Field(default=0.30, ge=-1, le=5)
    night_price_per_kwh: float = Field(default=0.18, ge=-1, le=5)
    export_price_per_kwh: float = Field(default=0.072466, ge=-1, le=5)
    cheap_start_hour: int = Field(default=0, ge=0, le=23)
    cheap_end_hour: int = Field(default=5, ge=0, le=23)
    quiet_start_hour: int = Field(default=22, ge=0, le=23)
    quiet_end_hour: int = Field(default=7, ge=0, le=23)
    horizon_hours: int = Field(default=24, ge=6, le=36)
    block_minutes: Literal[30, 60] = 60
    comfort_margin_c: float = Field(default=0.25, ge=0, le=0.5)
    measurement_max_age_seconds: int = Field(default=3600, ge=60, le=7200)
    plan_max_age_seconds: int = Field(default=1200, ge=60, le=1800)
    manual_override_hours: float = Field(default=4, ge=0.25, le=24)
    baseline_kw: float = Field(default=0.3, ge=0, le=20)
    validated_model: bool = False
    optimization_enabled: bool = False
    allow_estimated_model_control: bool = False
    planned_off: bool = Field(
        default=False,
        description="Let accepted plans switch native-thermostat ACs off between heating blocks.",
    )
    battery_limits_verified: bool = False
    control_switch_entity: str | None = Field(default=None, pattern=r"^input_boolean\.[a-z0-9_]+$")
    solar_planes: list[SolarPlane] = Field(default_factory=list, max_length=8)
    house_power_entity: str | None = None
    hvac_energy_entities: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def topology(self) -> "HeatingSettings":
        ZoneInfo(self.timezone)
        ids = [r.id for r in self.rooms]
        entities = [r.climate_entity for r in self.rooms]
        assigned = [r for g in self.compressors for r in g.rooms]
        if len(set(ids)) != len(ids) or len(set(entities)) != len(entities):
            raise ValueError("Room IDs and climate entities must be unique")
        if sorted(assigned) != sorted(ids):
            raise ValueError("Every room must belong to exactly one compressor")
        if len({g.id for g in self.compressors}) != len(self.compressors):
            raise ValueError("Compressor IDs must be unique")
        seen = set()
        for edge in self.couplings:
            pair = tuple(sorted((edge.a, edge.b)))
            if edge.a == edge.b or not set(pair) <= set(ids) or pair in seen:
                raise ValueError("Couplings must connect two distinct known rooms, once")
            seen.add(pair)
        for room in self.rooms:
            conductance = room.loss_kw_per_k + sum(
                e.kw_per_k for e in self.couplings if room.id in (e.a, e.b)
            )
            if conductance / room.capacity_kwh_per_k > 12:
                raise ValueError(
                    "Thermal parameters are unstable at the five-minute integration step"
                )
        if self.enabled and not self.rooms:
            raise ValueError("Enabled heating needs rooms")
        ranges = [band for r in self.rooms for band in (r, r.night_comfort) if band is not None]
        if any(band.max_c - band.min_c <= 2 * self.comfort_margin_c for band in ranges):
            raise ValueError("Comfort margin leaves no usable temperature range")
        if self.mode == "control" and not self.control_switch_entity:
            raise ValueError("Control needs a Home Assistant enable switch")
        if self.optimization_enabled and not (
            self.validated_model or self.allow_estimated_model_control
        ):
            raise ValueError(
                "Optimization needs a validated model or explicit estimated-model opt-in"
            )
        return self


class ForecastSlot(Model):
    at: datetime
    outside_c: float = Field(ge=-40, le=50)
    pv_kw: float = Field(ge=0, le=1000)
    baseline_kw: float = Field(ge=0, le=100)
    import_price_per_kwh: float = Field(ge=-1, le=5)
    export_price_per_kwh: float = Field(ge=-1, le=5)
    battery_max_soc: float | None = Field(default=None, ge=0, le=1)


class PlanningInput(Model):
    generated_at: datetime
    temperatures_c: dict[str, float]
    battery_soc: float | None = Field(default=None, ge=0, le=1)
    energy_mode: Literal["full", "solar_only", "tariff_only"] = "full"
    slots: list[ForecastSlot] = Field(min_length=24, max_length=144)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def timestamps(self) -> "PlanningInput":
        if self.generated_at.utcoffset() is None:
            raise ValueError("generated_at must include timezone")
        for i, slot in enumerate(self.slots):
            if slot.at.utcoffset() is None:
                raise ValueError("Forecast timestamps must include timezone")
            if i and abs(slot.at.timestamp() - self.slots[i - 1].at.timestamp() - 900) > 0.1:
                raise ValueError("Forecast must contain contiguous 15-minute UTC instants")
        if abs(self.slots[0].at.timestamp() - self.generated_at.timestamp()) > 900:
            raise ValueError("Forecast must begin within 15 minutes of the input timestamp")
        return self

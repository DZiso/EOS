"""Read-only runtime status and an isolated thermal planning API."""

import json
from collections import deque
from datetime import date, datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from akkudoktoreos.heating.models import HeatingSettings, Model, PlanningInput
from akkudoktoreos.heating.planner import plan
from akkudoktoreos.heating.runtime import data_dir, load_json, settings

router = APIRouter(prefix="/v1/heating", tags=["heating"])


class HeatingRequest(Model):
    settings: HeatingSettings
    inputs: PlanningInput


@router.post("/plan")
def calculate_plan(request: HeatingRequest) -> dict[str, Any]:
    """Simulate and optimize a schedule without executing or storing commands."""
    try:
        return plan(request.settings, request.inputs)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/status")
def heating_status() -> dict[str, Any]:
    config = settings()
    return {
        "configuration": config.model_dump(),
        "runtime": load_json(data_dir() / "status.json", {"state": "not_started"}),
    }


@router.get("/plan")
def current_plan() -> dict[str, Any]:
    result = load_json(data_dir() / "plan.json", None)
    if result is None:
        raise HTTPException(404, "No heating plan has been calculated")
    return result


@router.get("/observations")
def observations(day: date | None = None, limit: int = Query(120, ge=1, le=1440)) -> dict[str, Any]:
    """Read bounded UTC-day telemetry, including actual command outcomes."""
    selected = day or datetime.now(timezone.utc).date()
    path = data_dir() / ("observations-" + selected.isoformat() + ".jsonl")
    records = []
    if path.exists():
        with path.open() as stream:
            for line in deque(stream, maxlen=limit):
                try:
                    records.append(json.loads(line))
                except ValueError:
                    # A concurrent append may leave its final line incomplete.
                    continue
    return {"day": selected.isoformat(), "count": len(records), "records": records}

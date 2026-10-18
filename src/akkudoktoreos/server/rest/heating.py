"""Read-only runtime status and an isolated thermal planning API."""

from typing import Any

from fastapi import APIRouter, HTTPException

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

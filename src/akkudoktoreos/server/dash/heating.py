"""Heating observation page, served through the existing EOSdash ingress."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fasthtml.common import H2, H3, Div, Li, P, Table, Tbody, Td, Th, Thead, Tr, Ul

from akkudoktoreos.heating.controller import parse_time
from akkudoktoreos.heating.runtime import data_dir, load_json, settings
from akkudoktoreos.server.dash.context import request_url_for


def Heating() -> Div:
    config = settings()
    state = load_json(data_dir() / "status.json", {})
    proposal = load_json(data_dir() / "plan.json", {})
    timezone_local = ZoneInfo(config.timezone)
    room_names = [r.id for r in config.rooms]
    room_rows = []
    for room in config.rooms:
        decision = state.get("rooms", {}).get(room.id, {})
        measurement = decision.get("measurement", {})
        age = measurement.get("age_seconds")
        room_rows.append(
            Tr(
                Td(room.id),
                Td(str(measurement.get("temperature_c", "—")) + "°C"),
                Td(f"{max(0, age) / 60:.0f} min" if age is not None else "unknown"),
                Td(str(decision.get("room_target_c", "—"))),
                Td(str(decision.get("ac_target_c", "—"))),
                Td(decision.get("source", "observation").replace("_", " ")),
                Td(decision.get("state", "starting").replace("_", " ")),
            )
        )
    rows = []
    for slot in proposal.get("slots", [])[::4]:
        rows.append(
            Tr(
                Td(parse_time(slot["at"]).astimezone(timezone_local).strftime("%a %H:%M %Z")),
                *(
                    Td(f"{slot['targets_c'][r]}° → {slot['temperatures_c'][r]:.1f}°")
                    for r in room_names
                ),
                Td("Quiet" if slot["quiet"] else "Normal"),
            )
        )
    fresh = False
    if proposal:
        fresh = (
            state.get("plan_fresh", False)
            and 0
            <= datetime.now(timezone.utc).timestamp()
            - parse_time(proposal["generated_at"]).timestamp()
            <= config.plan_max_age_seconds
        )
    return Div(
        H2("Predictive heating"),
        P(
            "Observation mode — proposed targets are not sent to your air conditioners."
            if config.mode == "observe"
            else "Room control is enabled by the Home Assistant heating switch."
        ),
        P(
            f"Runtime: {state.get('state', 'not started')} · Latest plan: {proposal.get('generated_at', 'none')}"
        ),
        P("Fresh plan" if fresh else "No fresh plan — shown schedules must not be applied"),
        P(state.get("error") or ""),
        P("Planning: " + (state.get("planning_error") or "available")),
        P("Comfort: " + ", ".join(f"{r.id}: {r.min_c:g}–{r.max_c:g}°C" for r in config.rooms)),
        P("Battery: existing automatic operation. Gas heating: unavailable."),
        H3("Room control"),
        P(
            "Room targets and AC setpoints can differ because the sensors are at different heights. Age is time since the source last reported; unchanged readings are not relabelled as fresh."
        ),
        Table(
            Thead(
                Tr(
                    *(
                        Th(label)
                        for label in (
                            "Room",
                            "Temperature",
                            "Reading age",
                            "Room target °C",
                            "AC setpoint °C",
                            "Target source",
                            "Status",
                        )
                    )
                )
            ),
            Tbody(*room_rows),
        ),
        H3("Model estimates"),
        P(
            f"Heating electricity: {proposal.get('heating_kwh', 0):.2f} kWh · "
            f"Household net cost: €{proposal.get('estimated_cost', 0):.2f} · "
            f"Compared with constant comfort targets: €{proposal.get('estimated_difference', 0):.2f}"
        ),
        Ul(*(Li(w) for w in proposal.get("warnings", []))),
        H3("Proposed targets → temperature after 15 minutes"),
        P(
            "Hourly rows from a 15-minute simulation. Estimates are not measured efficiency or guaranteed savings."
        ),
        Table(Thead(Tr(Th("Time"), *(Th(r) for r in room_names), Th("Operation"))), Tbody(*rows)),
        id="heating-status",
        hx_get=request_url_for("heating"),
        hx_trigger="every 60s",
        hx_swap="outerHTML",
        cls="space-y-4 p-6 overflow-x-auto",
    )

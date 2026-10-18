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
            state.get("state") in {"observing", "controlling"}
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
            else "Automatic control requires the Home Assistant enable switch."
        ),
        P(
            f"Runtime: {state.get('state', 'not started')} · Latest plan: {proposal.get('generated_at', 'none')}"
        ),
        P("Fresh plan" if fresh else "No fresh plan — shown schedules must not be applied"),
        P(state.get("error") or ""),
        P("Comfort: " + ", ".join(f"{r.id}: {r.min_c:g}–{r.max_c:g}°C" for r in config.rooms)),
        P("Battery: existing automatic operation. Gas heating: unavailable."),
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

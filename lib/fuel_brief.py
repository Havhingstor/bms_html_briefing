"""Attach minimum-return-fuel cells using DTC coordinates and a CAM fuel profile."""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

from lib.fuel import ASSUMPTIONS, FEET_PER_NM, FuelModelError, minimum_fuel_lb

_LAND_ACTIONS = {7, 27}


def _attr(value: Any, name: str, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _integer(value: Any) -> int | None:
    try:
        number = float(value)
        return int(number) if not isinstance(value, bool) and math.isfinite(number) and number.is_integer() else None
    except (TypeError, ValueError, OverflowError):
        return None


def _callsign(value: Any) -> str:
    return "".join(str(value or "").split()).casefold()


def _path(value: Any) -> str:
    return os.path.normcase(str(Path(value).expanduser().resolve())) if value else ""


def _coordinates(point: Any) -> tuple[float, float]:
    try:
        values = float(_attr(point, "coord_x")), float(_attr(point, "coord_y"))
    except (TypeError, ValueError):
        raise ValueError("DTC steerpoint coordinates are missing.") from None
    if not all(math.isfinite(v) for v in values):
        raise ValueError("DTC steerpoint coordinates are invalid.")
    return values


def _matched_flight(summary: dict, briefing: Any) -> dict:
    package_number = _integer(_attr(_attr(briefing, "overview"), "package_id"))
    packages = [p for p in summary.get("packages", []) if _integer(p.get("package_number")) == package_number]
    if package_number is None or len(packages) != 1:
        raise ValueError("The loaded save does not uniquely match this briefing package.")
    own = _attr(briefing, "own_flight")
    number, callsign = _integer(_attr(own, "flight")), _callsign(_attr(own, "callsign"))
    flights = packages[0].get("flights", [])
    if number is not None:
        matches = [f for f in flights if _integer(f.get("flight_number")) == number]
    else:
        matches = [f for f in flights if callsign and _callsign(f.get("callsign")) == callsign]
    if len(matches) != 1:
        raise ValueError("The loaded save does not uniquely match this briefing flight.")
    flight = matches[0]
    if callsign and _callsign(flight.get("callsign")) != callsign:
        raise ValueError("The briefing and saved flight callsigns differ.")
    return flight


def build_fuel_plan(*, summary: Any, briefing: Any, dtc: Any, bms_base_dir: str, theater: str) -> dict:
    """Return non-editable values and tooltips keyed by displayed STPT number."""
    rows = list(_attr(briefing, "steerpoints", []) or [])
    cells = {str(_attr(row, "number")): {"value": None, "tooltip": ASSUMPTIONS} for row in rows}
    result = {"tooltip": ASSUMPTIONS, "steerpoints": cells}

    def unavailable(reason: str) -> dict:
        for cell in cells.values():
            cell["tooltip"] = "Minimum fuel unavailable: " + reason + " " + ASSUMPTIONS
        return result

    if not isinstance(summary, dict):
        return unavailable("Load the matching campaign save.")
    if (_path(summary.get("source_bms_base_dir")) != _path(bms_base_dir)
            or str(summary.get("source_theater") or "").casefold() != str(theater or "").casefold()):
        return unavailable("Reload the save for the current BMS installation and theater.")
    try:
        flight = _matched_flight(summary, briefing)
        profile = flight.get("fuel_profile") or {}
        rate = profile.get("rate_lb_nm")
        if rate is None:
            return unavailable(profile.get("unavailable_reason") or "Aircraft performance data is missing.")

        points = {}
        for point in _attr(dtc, "steerpoints", []) or []:
            number = _integer(_attr(point, "number"))
            if number is None or number < 1 or number in points:
                raise ValueError("DTC steerpoint numbers are invalid or duplicated.")
            points[number] = point
        landings = sorted(n for n, p in points.items() if _integer(_attr(p, "action")) in _LAND_ACTIONS)
        if not landings:
            raise ValueError("The DTC has no planned landing steerpoint.")
        if len(landings) > 2:
            raise ValueError("The DTC does not uniquely identify home and alternate.")
        landing_number = landings[0]
        return_coords = [_coordinates(points[number]) for number in landings]

        # The user selects the matching mission files. Route coordinates and
        # return destinations come solely from DTC; CAM supplies aircraft data.
        seen = set()
        for row in rows:
            number = _integer(_attr(row, "number"))
            if number is None or number in seen:
                raise ValueError("Briefing steerpoint numbers are invalid or duplicated.")
            seen.add(number)
            if number > landing_number:
                cells[str(_attr(row, "number"))]["tooltip"] = "Outside the planned route through landing. " + ASSUMPTIONS
                continue
            point = points.get(number)
            if point is None:
                raise ValueError("The briefing and DTC steerpoint numbers differ.")
            coords = _coordinates(point)
            if (_integer(_attr(point, "action")) in {1, *_LAND_ACTIONS}
                    or str(_attr(row, "description", "")).casefold() in {"takeoff", "land"}):
                # Airport rows have no return-fuel figure, even with valid inputs.
                continue
            distance = min(math.dist(coords, destination) for destination in return_coords) / FEET_PER_NM
            fuel = minimum_fuel_lb(distance, rate)
            cell = cells[str(_attr(row, "number"))]
            capacity = profile.get("internal_fuel_lb")
            if capacity is not None and fuel > capacity:
                cell["tooltip"] = "Direct return exceeds internal fuel capacity at this profile. " + ASSUMPTIONS
                continue
            cell["value"] = fuel
            cell["tooltip"] = ASSUMPTIONS
        return result
    except (ValueError, TypeError, KeyError, FuelModelError) as exc:
        # Never leave partly computed values when required inputs are invalid.
        for cell in cells.values():
            cell["value"] = None
        return unavailable(str(exc))

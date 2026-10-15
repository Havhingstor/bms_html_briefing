"""Select the player's saved aircraft using the briefing's rank/pilot-name roster."""
from __future__ import annotations

import math
from typing import Any

from lib.fuel_brief import _matched_flight, _matched_member, _path


def _pilot_name(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _store_labels(stores: list[dict], category: str) -> list[str]:
    # Never combine different weapon IDs just because their short names coincide.
    grouped = {}
    for store in stores:
        if store["category"] == category or (store["category"] == "both" and category in ("aa", "ag")):
            if not store["name"]:
                return []
            key = store["weapon_id"]
            name, count = grouped.get(key, (store["name"], 0))
            grouped[key] = name, count + store["count"]
    return [f"{count}× {name}" if count > 1 else name for name, count in grouped.values()]


def _whole_number(value: Any) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return f"{value:,.0f}"
    return None


def build_admin_data(*, summary: Any, briefing: Any, player_name: str,
                     bms_base_dir: str, theater: str) -> dict:
    if not isinstance(summary, dict) or not _pilot_name(player_name):
        return {}
    if (not bms_base_dir or not theater
            or _path(summary.get("source_bms_base_dir")) != _path(bms_base_dir)
            or str(summary.get("source_theater") or "").casefold() != theater.casefold()):
        return {}
    try:
        flight = _matched_flight(summary, briefing)
        member = _matched_member(flight, briefing, player_name)
        stores = member["stores"]
        classified = all(s["category"] is not None for s in stores)
        # A missing classification may be a tank, so do not show partial lists.
        categories = {"aa": "aa", "ag": "ag", "misc": "other", "tanks": "tanks"}
        groups = {key: _store_labels(stores, category) if classified else []
                  for key, category in categories.items()}
        if member.get("loaded_cft"):
            groups["tanks"].append("CFT")
        liftoff = member.get("liftoff_speed_kcas")
        liftoff_text = _whole_number(liftoff)
        if liftoff_text is not None and not 70 <= liftoff <= 330:
            liftoff_text = None
        liftoff_tooltip = "Liftoff speed (KCAS)."
        aoa = member.get("liftoff_aoa_deg")
        if (liftoff_text is not None and isinstance(aoa, (int, float))
                and not isinstance(aoa, bool) and math.isfinite(aoa) and 0 < aoa < 30):
            liftoff_tooltip = f"Lift-off speed at full (AB) thrust, {aoa:g} degrees AoA, at sea-level."
        return {
            **groups,
            "gross_weight": _whole_number(member.get("gross_weight_lb")),
            "takeoff_fuel": _whole_number(member.get("takeoff_fuel_lb")),
            "drag_index": _whole_number(member.get("drag_index")),
            "liftoff_speed": liftoff_text,
            "liftoff_tooltip": liftoff_tooltip,
        }
    except (ValueError, TypeError, KeyError):
        return {}

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class ChartRole(str, Enum):
    DEPARTURE = "departure"
    ARRIVAL = "arrival"
    ALTERNATE = "alternate"


class ChartKind(str, Enum):
    GROUND = "ground"
    PARKING = "parking"
    LOCAL = "local"


ROLE_ORDER = tuple(ChartRole)
KIND_ORDER = tuple(ChartKind)


@dataclass(frozen=True)
class ChartSelection:
    role: ChartRole
    kind: ChartKind

    @property
    def id(self) -> str:
        return f"{self.role.value}_{self.kind.value}"


ALL_SELECTIONS = tuple(
    ChartSelection(role, kind) for role in ROLE_ORDER for kind in KIND_ORDER
)
SELECTIONS_BY_ID = {selection.id: selection for selection in ALL_SELECTIONS}


def parse_chart_selections(raw: str | None) -> tuple[tuple[ChartSelection, ...], list[str]]:
    selections: list[ChartSelection] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for token in str(raw or "").split(","):
        selection_id = token.strip().casefold()
        if not selection_id:
            continue
        selection = SELECTIONS_BY_ID.get(selection_id)
        if selection is None:
            warnings.append(f"Charts: ignored invalid selection {token.strip()!r}.")
            continue
        if selection_id in seen:
            continue
        seen.add(selection_id)
        selections.append(selection)
    return tuple(selections), warnings


def serialize_chart_selections(selections: Iterable[ChartSelection]) -> str:
    seen: set[str] = set()
    values: list[str] = []
    for selection in selections:
        if selection.id in seen:
            continue
        seen.add(selection.id)
        values.append(selection.id)
    return ", ".join(values)


__all__ = [
    "ALL_SELECTIONS",
    "ChartKind",
    "ChartRole",
    "ChartSelection",
    "KIND_ORDER",
    "ROLE_ORDER",
    "parse_chart_selections",
    "serialize_chart_selections",
]

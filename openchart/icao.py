"""Conservative ICAO fallback matching against the packaged airport catalog."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from importlib.resources import files
import math
import re
import unicodedata

from .geography import (
    FEET_PER_METER,
    GeographicProjectionError,
    TransverseMercatorProjection,
)


ICAO_PATTERN = re.compile(r"[A-Z]{4}")
AIRFIELD_NAME_PATTERN = re.compile(
    r"\b(?:airport|airbase|air\s+base|airfield|airstrip|aerodrome)\b",
    re.IGNORECASE,
)
NON_AIRPORT_PATTERN = re.compile(
    r"\b(?:army|brigade|camp|corps|division|helobase|heliport|highway)\b",
    re.IGNORECASE,
)
IGNORED_NAME_WORDS = frozenset(
    (
        "aerodrome",
        "air",
        "airbase",
        "airfield",
        "airport",
        "airstrip",
        "base",
        "international",
        "intl",
    )
)
EARTH_RADIUS_KM = 6_371.0088
EXACT_NAME_MAX_DISTANCE_KM = 50.0
COORDINATE_MATCH_MAX_DISTANCE_KM = 1.5
FUZZY_MATCH_MAX_DISTANCE_KM = 10.0
FUZZY_NAME_THRESHOLD = 0.8


class IcaoCatalogError(RuntimeError):
    """Raised when the packaged airport catalog has an invalid schema."""


@dataclass(frozen=True)
class IcaoAirport:
    icao: str
    name: str
    latitude: float
    longitude: float


@lru_cache(maxsize=1)
def load_icao_airports() -> tuple[IcaoAirport, ...]:
    """Load valid four-letter records from the packaged IP2Location CSV."""

    resource = files("openchart.assets").joinpath("iata-icao.csv")
    with resource.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required_fields = {"icao", "airport", "latitude", "longitude"}
        if reader.fieldnames is None or not required_fields.issubset(
            reader.fieldnames
        ):
            raise IcaoCatalogError(
                "iata-icao.csv is missing one or more required fields: "
                "icao, airport, latitude, longitude"
            )
        airports: list[IcaoAirport] = []
        for row in reader:
            icao = row["icao"].strip().upper()
            if ICAO_PATTERN.fullmatch(icao) is None:
                continue
            try:
                latitude = float(row["latitude"])
                longitude = float(row["longitude"])
            except (TypeError, ValueError):
                continue
            if (
                not math.isfinite(latitude)
                or not math.isfinite(longitude)
                or not -90.0 <= latitude <= 90.0
                or not -180.0 <= longitude <= 180.0
            ):
                continue
            airports.append(
                IcaoAirport(
                    icao=icao,
                    name=row["airport"].strip(),
                    latitude=latitude,
                    longitude=longitude,
                )
            )
    return tuple(airports)


def infer_icao(
    airfield_name: str,
    position_x_ft: float,
    position_y_ft: float,
    projection_string: str | None,
    *,
    airports: tuple[IcaoAirport, ...] | None = None,
) -> str | None:
    """Return a catalog ICAO only when name and/or position is convincing."""

    if (
        projection_string is None
        or AIRFIELD_NAME_PATTERN.search(airfield_name) is None
        or NON_AIRPORT_PATTERN.search(airfield_name) is not None
    ):
        return None
    try:
        projection = TransverseMercatorProjection.from_proj_string(
            projection_string
        )
        latitude, longitude = projection.unproject(
            position_y_ft / FEET_PER_METER,
            position_x_ft / FEET_PER_METER,
        )
    except GeographicProjectionError:
        return None

    airfield_key = _normalized_airfield_name(airfield_name)
    nearby: list[tuple[IcaoAirport, float, float]] = []
    for airport in load_icao_airports() if airports is None else airports:
        distance = _great_circle_distance_km(
            latitude,
            longitude,
            airport.latitude,
            airport.longitude,
        )
        if distance > EXACT_NAME_MAX_DISTANCE_KM:
            continue
        airport_key = _normalized_airfield_name(airport.name)
        similarity = SequenceMatcher(
            None,
            airfield_key,
            airport_key,
        ).ratio()
        nearby.append((airport, distance, similarity))

    exact = tuple(
        item
        for item in nearby
        if airfield_key
        and airfield_key == _normalized_airfield_name(item[0].name)
    )
    if exact:
        return min(exact, key=lambda item: (item[1], item[0].icao))[0].icao

    coordinate_matches = tuple(
        item for item in nearby if item[1] <= COORDINATE_MATCH_MAX_DISTANCE_KM
    )
    if coordinate_matches:
        return min(
            coordinate_matches,
            key=lambda item: (item[1], -item[2], item[0].icao),
        )[0].icao

    fuzzy = tuple(
        item
        for item in nearby
        if item[1] <= FUZZY_MATCH_MAX_DISTANCE_KM
        and item[2] >= FUZZY_NAME_THRESHOLD
    )
    if not fuzzy:
        return None
    return min(
        fuzzy,
        key=lambda item: (-item[2], item[1], item[0].icao),
    )[0].icao


def _normalized_airfield_name(value: str) -> str:
    ascii_value = (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", errors="ignore")
        .decode("ascii")
        .casefold()
    )
    without_parentheses = re.sub(r"\([^)]*\)", " ", ascii_value)
    words = re.findall(r"[a-z0-9]+", without_parentheses)
    return " ".join(word for word in words if word not in IGNORED_NAME_WORDS)


def _great_circle_distance_km(
    left_latitude: float,
    left_longitude: float,
    right_latitude: float,
    right_longitude: float,
) -> float:
    left_latitude_radians = math.radians(left_latitude)
    right_latitude_radians = math.radians(right_latitude)
    latitude_delta = math.radians(right_latitude - left_latitude)
    longitude_delta = math.radians(right_longitude - left_longitude)
    haversine = (
        math.sin(latitude_delta / 2.0) ** 2
        + math.cos(left_latitude_radians)
        * math.cos(right_latitude_radians)
        * math.sin(longitude_delta / 2.0) ** 2
    )
    return EARTH_RADIUS_KM * 2.0 * math.atan2(
        math.sqrt(haversine),
        math.sqrt(1.0 - haversine),
    )

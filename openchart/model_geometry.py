"""Airport surface geometry recovered from authored BMS graphics models."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math
from pathlib import Path
import re

from .geometry import Point
from .source import AirportData, ObjectiveFeatureDefinition
from .vendor.opentaxiway_bml import (
    OpenTaxiwayError,
    Triangle3D,
    find_chart_lod_model,
    find_highest_lod_model,
    load_bml_triangles,
)


BUILDING_FEATURE_TERMS = (
    "hangar",
    "shelter",
    "terminal",
    "maintenance",
    "rffs",
)
BUILDING_FEATURE_TYPES = frozenset(
    (
        2,   # tower and authored ATC components
        6,   # industrial processing building
        10,  # depots and depot shelters
        12,  # warehouses, receiving, garages, RFFS
        14,  # fuel storage and service buildings
        21,  # apartments and construction structures
        23,  # control/converter buildings
        34,  # technical buildings and blast barriers
        35,  # barracks
        37,  # water towers
        39,  # terminals and ATC building components
        45,  # hangars, HAS, maintenance, stores
        52,  # administration and loading-dock buildings
        55,  # guard buildings
        56,  # transformer buildings
        57,  # ammunition storage
        59,  # offices and squadron buildings
        60,  # processing buildings
    )
)
NON_BUILDING_FEATURE_TERMS = ("wall", "fence", "blast barrier")
FEATURE_DOMAIN = 3
FEATURE_CLASS = 2
AIRPORT_SURFACE_FEATURE_TYPE = 30
NON_SURFACE_NAME_PATTERN = re.compile(
    r"\b(?:lights?|signs?|buildings?|hangars?)\b|free\s+feature",
    re.IGNORECASE,
)
TAXIWAY_NAME_PATTERN = re.compile(
    r"taxiway|(?:^|[^a-z0-9])twy(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TaxiwaySurface:
    """One aggregate taxiway model placed in objective-local coordinates."""

    feature_index: int
    graphics_id: int
    source_path: Path
    triangles: tuple[tuple[Point, Point, Point], ...]
    hull: tuple[Point, ...]


@dataclass(frozen=True)
class BuildingModel:
    """One reusable top-down projection of an authored graphics model."""

    graphics_id: int
    source_path: Path
    triangles: tuple[tuple[Point, Point, Point], ...]
    hull: tuple[Point, ...]


@dataclass(frozen=True)
class BuildingShape:
    """One building model instance placed by its objective feature row."""

    feature_index: int
    name: str
    offset_x: float
    offset_y: float
    heading: float
    model: BuildingModel
    hull: tuple[Point, ...]


def build_taxiway_surfaces(airport: AirportData) -> tuple[TaxiwaySurface, ...]:
    """Load and place every aggregate taxiway feature in an airport layout."""

    surfaces: list[TaxiwaySurface] = []
    for feature in airport.layout.features:
        if not is_airport_surface_feature(feature):
            continue
        graphics_id = feature.graphics_normal
        if graphics_id is None:
            continue
        model_path = find_chart_lod_model(airport.models_dirs, graphics_id)
        if model_path is None:
            continue
        model_triangles = _cached_bml_triangles(model_path)
        triangles = tuple(
            tuple(_place_model_vertex(vertex, feature) for vertex in triangle)
            for triangle in model_triangles
        )
        surfaces.append(
            TaxiwaySurface(
                feature_index=feature.index,
                graphics_id=graphics_id,
                source_path=model_path,
                triangles=triangles,
                hull=_convex_hull(
                    point for triangle in triangles for point in triangle
                ),
            )
        )
    return tuple(surfaces)


def build_building_shapes(airport: AirportData) -> tuple[BuildingShape, ...]:
    """Project authored 3D building models into reusable chart silhouettes."""

    shapes: list[BuildingShape] = []
    for feature in airport.layout.features:
        if not is_building_feature(feature):
            continue
        graphics_id = feature.graphics_normal
        if graphics_id is None:
            continue
        model_path = find_highest_lod_model(airport.models_dirs, graphics_id)
        if model_path is None:
            continue
        try:
            model = _cached_building_model(model_path, graphics_id)
        except (OpenTaxiwayError, ValueError):
            continue
        shapes.append(
            BuildingShape(
                feature_index=feature.index,
                name=feature.name or "Building",
                offset_x=feature.offset_x,
                offset_y=feature.offset_y,
                heading=feature.heading,
                model=model,
                hull=tuple(
                    _place_model_vertex((point.x, 0.0, point.y), feature)
                    for point in model.hull
                ),
            )
        )
    return tuple(shapes)


def is_building_feature(feature: ObjectiveFeatureDefinition) -> bool:
    if feature.name == "Control Tower":
        return False
    lowered = (feature.name or "").casefold()
    if any(term in lowered for term in NON_BUILDING_FEATURE_TERMS):
        return False
    if (
        feature.class_table is not None
        and feature.class_table.type_ in BUILDING_FEATURE_TYPES
    ):
        return True
    return any(term in lowered for term in BUILDING_FEATURE_TERMS)


def is_airport_surface_feature(feature: ObjectiveFeatureDefinition) -> bool:
    """Return whether an authored feature is airport pavement geometry.

    BMS feature type 30 is the deterministic taxiway/airport-surface class.
    Some theater databases also assign that type to lighting helpers, so
    exclude those by their authored names.  A conservative taxiway/TWY name
    fallback supports third-party data whose class-table metadata is absent or
    incorrect without admitting taxiway signs or lights.
    """

    name = (feature.name or "").strip()
    if not name or NON_SURFACE_NAME_PATTERN.search(name) is not None:
        return False
    class_table = feature.class_table
    if (
        class_table is not None
        and getattr(class_table, "domain", None) == FEATURE_DOMAIN
        and getattr(class_table, "class_", None) == FEATURE_CLASS
        and getattr(class_table, "type_", None) == AIRPORT_SURFACE_FEATURE_TYPE
    ):
        return True
    return TAXIWAY_NAME_PATTERN.search(name) is not None


@lru_cache(maxsize=64)
def _cached_bml_triangles(path: Path) -> tuple[Triangle3D, ...]:
    return load_bml_triangles(path)


@lru_cache(maxsize=64)
def _cached_building_model(path: Path, graphics_id: int) -> BuildingModel:
    projected: list[tuple[Point, Point, Point]] = []
    for triangle in _cached_bml_triangles(path):
        points = tuple(Point(vertex[0], vertex[2]) for vertex in triangle)
        area = _signed_area(*points)
        if abs(area) <= 1e-5:
            continue
        if area < 0.0:
            points = (points[0], points[2], points[1])
        projected.append(points)
    if not projected:
        raise ValueError(f"{path}: building model has no top-down surface area")
    triangles = tuple(projected)
    return BuildingModel(
        graphics_id=graphics_id,
        source_path=path,
        triangles=triangles,
        hull=_convex_hull(point for triangle in triangles for point in triangle),
    )


def _signed_area(left: Point, middle: Point, right: Point) -> float:
    return (
        (middle.x - left.x) * (right.y - left.y)
        - (middle.y - left.y) * (right.x - left.x)
    )


def _place_model_vertex(
    vertex: tuple[float, float, float],
    feature: ObjectiveFeatureDefinition,
) -> Point:
    """Transform BML X/Z ground-plane coordinates into objective coordinates."""

    model_x, _, model_z = vertex
    heading = math.radians(feature.heading)
    return Point(
        feature.offset_x
        + model_z * math.sin(heading)
        + model_x * math.cos(heading),
        feature.offset_y
        + model_z * math.cos(heading)
        - model_x * math.sin(heading),
    )


def _convex_hull(points) -> tuple[Point, ...]:
    unique = sorted({(point.x, point.y) for point in points})
    if len(unique) <= 1:
        return tuple(Point(x, y) for x, y in unique)

    def cross(
        origin: tuple[float, float],
        left: tuple[float, float],
        right: tuple[float, float],
    ) -> float:
        return (
            (left[0] - origin[0]) * (right[1] - origin[1])
            - (left[1] - origin[1]) * (right[0] - origin[0])
        )

    lower: list[tuple[float, float]] = []
    for point in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0.0:
            lower.pop()
        lower.append(point)
    upper: list[tuple[float, float]] = []
    for point in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0.0:
            upper.pop()
        upper.append(point)
    return tuple(Point(x, y) for x, y in (*lower[:-1], *upper[:-1]))

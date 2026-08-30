"""Render one airport ground chart as self-contained SVG."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import math
import re
import xml.etree.ElementTree as ET

from .geometry import (
    NUMBERED_PARKING_TYPES,
    ArrestingSystem,
    NumberedParkingPosition,
    ParkingChart,
    ParkingPosition,
    Point,
    RunwayGeometry,
    TaxiwayLabel,
    build_arresting_systems,
    build_parking_positions,
    build_route_edges,
    build_runways,
    build_taxiway_labels,
)
from .geography import (
    FEET_PER_METER,
    GeographicProjectionError,
    TransverseMercatorProjection,
)
from .model_geometry import (
    BuildingShape,
    TaxiwaySurface,
    build_building_shapes,
    build_taxiway_surfaces,
    is_building_feature,
)
from .source import AirportData, NavigationAidData
from .symbols import VORTAC_CIRCLE, VORTAC_PATHS, windsock_symbol_element


SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)
PAGE_WIDTH = 1400
PAGE_HEIGHT = 1980
PAGE_WIDTH_MM = 210
PAGE_HEIGHT_MM = 297
FRAME_INSET = 18
CONTENT_INSET = FRAME_INSET + 10
MAP_LEFT = CONTENT_INSET
MAP_TOP = 130
MAP_WIDTH = PAGE_WIDTH - CONTENT_INSET * 2
FOOTER_INFO_Y = 1800
MAP_BOTTOM = FOOTER_INFO_Y - 32
MAP_HEIGHT = MAP_BOTTOM - MAP_TOP
MAP_PADDING_RATIO = 0.045
MAP_ROTATION_STEPS = (1.0, 0.1, 0.01)
EMPTY_MAP_HALF_EXTENT_FT = 6076.115485564304
GRATICULE_MINOR_MINUTES = 0.1
GRATICULE_MAJOR_MINUTES = 0.5
GRATICULE_UNITS_PER_DEGREE = 600
GRATICULE_MAJOR_UNIT_INTERVAL = 5
GRATICULE_LINE_SAMPLE_COUNT = 32
GRATICULE_MAX_COORDINATE_COUNT = 2_000
GRATICULE_MINOR_TICK_LENGTH = 7.0
GRATICULE_LABEL_EDGE_OFFSET = 62.0
GRATICULE_LABEL_HEIGHT = 23.0
FOOTER_LINE_GAP = 28
FOOTER_ELEVATION_Y = FOOTER_INFO_Y + FOOTER_LINE_GAP
FOOTER_RUNWAY_Y = FOOTER_ELEVATION_Y + FOOTER_LINE_GAP
FOOTER_RUNWAY_GAP = FOOTER_LINE_GAP
BOTTOM_ANNOTATION_Y = PAGE_HEIGHT - 40
NORTH_INDICATOR_BASE_X = 1250.0
NORTH_INDICATOR_BASE_Y = 1908.0
NORTH_TRUE_LENGTH = 82.0
NORTH_MAGNETIC_LENGTH = 62.0
VARIATION_LABEL_X = 1050
VARIATION_LABEL_Y = 1830
PAGE_UNITS_PER_POINT = PAGE_WIDTH * 25.4 / (PAGE_WIDTH_MM * 72.0)
PARKING_LABEL_FONT_POINTS = 7.0
TOWER_LABEL_FONT_POINTS = 9.0
NAVAID_LABEL_FONT_POINTS = 9.0
TAXIWAY_LABEL_FONT_POINTS = 9.0
RUNWAY_LABEL_FONT_POINTS = 11.0
RUNWAY_DIMENSION_FONT_POINTS = 7.0
PARKING_LABEL_FONT_SIZE = PARKING_LABEL_FONT_POINTS * PAGE_UNITS_PER_POINT
TOWER_LABEL_FONT_SIZE = TOWER_LABEL_FONT_POINTS * PAGE_UNITS_PER_POINT
NAVAID_LABEL_FONT_SIZE = NAVAID_LABEL_FONT_POINTS * PAGE_UNITS_PER_POINT
TAXIWAY_LABEL_FONT_SIZE = TAXIWAY_LABEL_FONT_POINTS * PAGE_UNITS_PER_POINT
RUNWAY_LABEL_FONT_SIZE = RUNWAY_LABEL_FONT_POINTS * PAGE_UNITS_PER_POINT
RUNWAY_DIMENSION_FONT_SIZE = RUNWAY_DIMENSION_FONT_POINTS * PAGE_UNITS_PER_POINT
PARKING_LABEL_SCALE = PARKING_LABEL_FONT_SIZE / 8.0
NAVAID_LABEL_SCALE = NAVAID_LABEL_FONT_SIZE / 12.0
TAXIWAY_LABEL_SCALE = TAXIWAY_LABEL_FONT_SIZE / 13.0
RUNWAY_LABEL_SCALE = RUNWAY_LABEL_FONT_SIZE / 17.0
RUNWAY_LABEL_THRESHOLD_OFFSET = RUNWAY_LABEL_FONT_SIZE * 0.75
RUNWAY_LABEL_COLLISION_STEP = RUNWAY_LABEL_FONT_SIZE * 0.2
TOWER_LABEL_SCALE = TOWER_LABEL_FONT_SIZE / 11.0
TOWER_RADIUS = 9.0 * TOWER_LABEL_SCALE
PARKING_LABEL_HALF_WIDTH = 7.0 * PARKING_LABEL_SCALE
PARKING_LABEL_HALF_HEIGHT = 6.5 * PARKING_LABEL_SCALE
PARKING_LABEL_GAP = 2.0 * PARKING_LABEL_SCALE
PARKING_MARKER_RADIUS = 6.5 * PARKING_LABEL_SCALE
PARKING_NEIGHBOR_DISTANCE = 35.0 * PARKING_LABEL_SCALE
PARKING_LABEL_RADII = tuple(
    radius * PARKING_LABEL_SCALE
    for radius in (
        9.0,
        14.0,
        19.0,
        24.0,
        30.0,
        37.0,
        45.0,
        54.0,
        64.0,
    )
)
PARKING_LABEL_DIRECTIONS = 16
NAVAID_LABEL_MIN_WIDTH = 44.0 * NAVAID_LABEL_SCALE
NAVAID_LABEL_CHARACTER_WIDTH = 7.2 * NAVAID_LABEL_SCALE
NAVAID_LABEL_PADDING = 6.0 * NAVAID_LABEL_SCALE
NAVAID_LABEL_TOP_OFFSET = 14.0 * NAVAID_LABEL_SCALE
NAVAID_LABEL_HEIGHT = 19.0 * NAVAID_LABEL_SCALE
NAVAID_LABEL_BASELINE_OFFSET = 29.0 * NAVAID_LABEL_SCALE
VORTAC_RENDER_SCALE = 0.26
ARRESTING_SYSTEM_CROSSBAR_SCALE = 2.0
WINDSOCK_FEATURE_CLASSIFICATION = (3, 2, 64)
WINDSOCK_SYMBOL_SIZE = 30.0
CLASS_STYLES = {
    "page": "fill:#fff",
    "border": "fill:none;stroke:#182026;stroke-width:2",
    "title": "font-size:39px;font-weight:700;letter-spacing:-0.7px",
    "subtitle": "font-size:16px;font-weight:700;letter-spacing:2.2px;fill:#52606a",
    "taxiway-surface": "fill:#e4e7e6;stroke:none;fill-rule:nonzero",
    "route-line": "fill:none;stroke:#858c8c;stroke-width:3;stroke-linecap:round;stroke-linejoin:round",
    "runway": "fill:#20272b;stroke:#0d1113;stroke-width:2",
    "runway-dimensions": "fill:#fff;font-size:16.46px;font-weight:700;text-anchor:middle;dominant-baseline:central",
    "arresting-system": "fill:none;stroke:#182026;stroke-width:1.6;stroke-linecap:square;stroke-linejoin:miter",
    "runway-end-text": "fill:#182026;font-size:25.87px;font-weight:700;text-anchor:middle;dominant-baseline:central",
    "parking": "fill:#fff;stroke:#4e5a5e;stroke-width:1.5",
    "parking-shelter": "fill:#d9e8de;stroke:#426657;stroke-width:1.5",
    "parking-number": "fill:#fff;stroke:#384448;stroke-width:1.2",
    "parking-number-unavailable": "fill:#c83f34;stroke:#8e261f;stroke-width:1.2",
    "parking-number-text": "font-size:16.46px;font-weight:700;text-anchor:middle;dominant-baseline:central",
    "parking-number-text-unavailable": "fill:#fff;font-size:16.46px;font-weight:700;text-anchor:middle;dominant-baseline:central",
    "parking-number-leader": "fill:none;stroke:#687376;stroke-width:1;stroke-linecap:round",
    "taxiway-label": "font-size:21.17px;font-weight:700;text-anchor:middle;dominant-baseline:central;paint-order:stroke;stroke:#fff;stroke-width:6.51;stroke-linejoin:round",
    "navaid-symbol": "fill:#182026;stroke:#182026;stroke-width:2;stroke-linejoin:miter",
    "navaid-label-background": "fill:#fff",
    "navaid-label": "font-size:21.17px;font-weight:700;text-anchor:middle",
    "building-shape": "fill:#aeb7b7;stroke:none;fill-rule:nonzero",
    "building": "fill:#aeb7b7;stroke:#596467;stroke-width:1.2",
    "tower": "fill:#fff;stroke:#1a626d;stroke-width:1.8",
    "tower-text": "fill:#1a626d;font-size:21.17px;font-weight:700;text-anchor:middle;dominant-baseline:central",
    "north-true": "fill:none;stroke:#182026;stroke-width:2;stroke-linecap:round;stroke-linejoin:round",
    "north-magnetic": "fill:none;stroke:#59656a;stroke-width:1.7;stroke-linecap:round;stroke-linejoin:round",
    "north-label": "font-size:14px;font-weight:700;text-anchor:middle;dominant-baseline:central",
    "frequency": "fill:#126b3b;font-size:22.5px;font-weight:700",
    "meta-value": "fill:#182026;font-size:22.5px;font-weight:700",
    "runway-summary": "fill:#182026;font-size:22.5px;font-weight:700",
    "small": "fill:#59656a;font-size:12px",
    "warning": "fill:#a2452d;font-size:24px;font-weight:700;letter-spacing:1px",
}


@dataclass(frozen=True)
class MapTransform:
    scale: float
    left: float
    bottom: float
    min_x: float
    min_y: float
    rotation_degrees: float = 0.0

    def point(self, point: Point) -> tuple[float, float]:
        rotated = _rotate_point(point, self.rotation_degrees)
        return (
            self.left + (rotated.x - self.min_x) * self.scale,
            self.bottom - (rotated.y - self.min_y) * self.scale,
        )

    def north_direction(self) -> tuple[float, float]:
        angle = math.radians(self.rotation_degrees)
        return (-math.sin(angle), -math.cos(angle))

    def unpoint(self, x: float, y: float) -> Point:
        """Return the unrotated layout point at one screen coordinate."""

        rotated = Point(
            self.min_x + (x - self.left) / self.scale,
            self.min_y + (self.bottom - y) / self.scale,
        )
        return _rotate_point(rotated, -self.rotation_degrees)


@dataclass(frozen=True)
class AirportRenderContext:
    """Airport geometry shared by every ground and parking chart page."""

    airport: AirportData
    runways: tuple[RunwayGeometry, ...]
    arresting_systems: tuple[ArrestingSystem, ...]
    routes: tuple[tuple[Point, Point], ...]
    taxiway_surfaces: tuple[TaxiwaySurface, ...]
    building_shapes: tuple[BuildingShape, ...]
    ground_parking: tuple[ParkingPosition, ...]
    taxiway_labels: tuple[TaxiwayLabel, ...]


def build_airport_render_context(airport: AirportData) -> AirportRenderContext:
    """Prepare immutable geometry for all chart pages of one airport."""

    if not isinstance(airport, AirportData):
        raise TypeError("airport must be AirportData")
    return AirportRenderContext(
        airport=airport,
        runways=build_runways(
            airport.layout,
            airport.atc,
            airport.magnetic_variation_degrees,
        ),
        arresting_systems=build_arresting_systems(airport.layout),
        routes=build_route_edges(airport.layout),
        taxiway_surfaces=build_taxiway_surfaces(airport),
        building_shapes=build_building_shapes(airport),
        ground_parking=build_parking_positions(airport.layout),
        taxiway_labels=build_taxiway_labels(airport.layout),
    )


@dataclass(frozen=True)
class ParkingLabelPlacement:
    position: NumberedParkingPosition
    anchor_x: float
    anchor_y: float
    label_x: float
    label_y: float


@dataclass(frozen=True)
class GraticuleLabel:
    axis: str
    coordinate_units: int
    x: float
    y: float
    text: str


@dataclass(frozen=True)
class GraticuleTick:
    varying_coordinate_units: int
    start: tuple[float, float]
    end: tuple[float, float]


@dataclass(frozen=True)
class GraticuleLine:
    axis: str
    coordinate_units: int
    points: tuple[tuple[float, float], ...]
    ticks: tuple[GraticuleTick, ...]
    label: GraticuleLabel


def render_airport_chart(airport: AirportData, output_path: str | Path) -> Path:
    """Render an unnumbered airport ground chart and return its path."""

    return _write_svg(output_path, render_airport_chart_svg(airport))


def render_airport_parking_chart(
    airport: AirportData,
    parking_chart: ParkingChart,
    output_path: str | Path,
) -> Path:
    """Render one runway-end parking chart and return its path."""

    return _write_svg(
        output_path,
        render_airport_parking_chart_svg(airport, parking_chart),
    )


def render_airport_chart_svg(
    airport: AirportData,
    *,
    context: AirportRenderContext | None = None,
) -> bytes:
    """Return a self-contained unnumbered airport ground-chart SVG."""

    return _render_airport_chart_svg(
        airport,
        parking_chart=None,
        context=_resolve_airport_render_context(airport, context),
    )


def render_airport_parking_chart_svg(
    airport: AirportData,
    parking_chart: ParkingChart,
    *,
    context: AirportRenderContext | None = None,
) -> bytes:
    """Return one self-contained runway-end parking-chart SVG."""

    return _render_airport_chart_svg(
        airport,
        parking_chart=parking_chart,
        context=_resolve_airport_render_context(airport, context),
    )


def _resolve_airport_render_context(
    airport: AirportData,
    context: AirportRenderContext | None,
) -> AirportRenderContext:
    selected = context or build_airport_render_context(airport)
    if not isinstance(selected, AirportRenderContext):
        raise TypeError("context must be AirportRenderContext or None")
    if selected.airport is not airport:
        raise ValueError("render context belongs to a different AirportData instance")
    return selected


def _render_airport_chart_svg(
    airport: AirportData,
    *,
    parking_chart: ParkingChart | None,
    context: AirportRenderContext,
) -> bytes:
    """Render a deterministic ground or runway-end parking SVG payload."""

    runways = context.runways
    arresting_systems = context.arresting_systems
    routes = context.routes
    taxiway_surfaces = context.taxiway_surfaces
    building_shapes = context.building_shapes
    ground_parking = context.ground_parking
    taxiway_labels = context.taxiway_labels
    map_parking = (
        ground_parking
        if parking_chart is None
        else (
            *parking_chart.positions,
            *(
                position
                for position in ground_parking
                if position.type_ not in NUMBERED_PARKING_TYPES
            ),
        )
    )
    transform = _map_transform(
        airport,
        runways,
        arresting_systems,
        routes,
        map_parking,
        taxiway_surfaces,
        building_shapes,
    )
    chart_name = (
        "Airport Ground Chart"
        if parking_chart is None
        else f"Parking Chart RWY {parking_chart.designator}"
    )

    root = _element(
        "svg",
        {
            "width": f"{PAGE_WIDTH_MM}mm",
            "height": f"{PAGE_HEIGHT_MM}mm",
            "viewBox": f"0 0 {PAGE_WIDTH} {PAGE_HEIGHT}",
            "role": "img",
            "aria-label": f"{airport.name} {chart_name.casefold()}",
        },
    )
    _element("title", parent=root, text=f"{airport.name} {chart_name}")
    _element(
        "desc",
        parent=root,
        text=(
            "Generated from matching Falcon BMS theater, campaign, objective "
            "feature, point, and station data."
        ),
    )
    _style(root)
    _element(
        "rect",
        {
            "class": "page",
            "x": "0",
            "y": "0",
            "width": str(PAGE_WIDTH),
            "height": str(PAGE_HEIGHT),
        },
        root,
    )
    _element(
        "rect",
        {
            "class": "border",
            "x": str(FRAME_INSET),
            "y": str(FRAME_INSET),
            "width": str(PAGE_WIDTH - FRAME_INSET * 2),
            "height": str(PAGE_HEIGHT - FRAME_INSET * 2),
        },
        root,
    )
    _header(root, airport, parking_chart)
    graticule_lines = _geographic_graticule(
        airport,
        transform,
    )
    _geographic_graticule_labels(
        root,
        tuple(line.label for line in graticule_lines),
    )

    map_group = _element(
        "g",
        {
            "id": "airport-layout",
            "data-rotation-degrees": _number(transform.rotation_degrees),
        },
        root,
    )
    _taxiway_surfaces(map_group, taxiway_surfaces, transform)
    _routes(map_group, routes, transform)
    _runways(map_group, runways, transform)
    _arresting_systems(map_group, arresting_systems, transform)
    _features(map_group, airport, building_shapes, transform)
    if parking_chart is None:
        _parking(map_group, ground_parking, transform)
    else:
        _parking(
            map_group,
            tuple(
                position
                for position in ground_parking
                if position.type_ not in NUMBERED_PARKING_TYPES
            ),
            transform,
        )
        _numbered_parking(map_group, parking_chart.positions, transform)
    _navigation_aids(map_group, airport.navigation_aids, transform)
    _taxiway_labels(map_group, taxiway_labels, transform)
    _geographic_graticule_overlay(root, graticule_lines)
    parking_count = (
        len(ground_parking)
        if parking_chart is None
        else len(parking_chart.positions)
    )
    _footer(
        root,
        airport,
        runways,
        parking_count,
        parking_chart,
        transform,
    )

    ET.indent(root, space="  ")
    payload = ET.tostring(root, encoding="unicode", xml_declaration=False)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n' + payload + "\n"
    ).encode("utf-8")


def _write_svg(output_path: str | Path, payload: bytes) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(payload)
    return output


def output_filename(airport: AirportData) -> str:
    code = airport.icao.casefold() if airport.icao else f"camp-{airport.campaign_id}"
    name = re.sub(r"[^a-z0-9]+", "-", airport.name.casefold()).strip("-")
    return f"{code}-{name}-ground-chart.svg"


def parking_output_filename(airport: AirportData, designator: str) -> str:
    code = airport.icao.casefold() if airport.icao else f"camp-{airport.campaign_id}"
    name = re.sub(r"[^a-z0-9]+", "-", airport.name.casefold()).strip("-")
    runway = re.sub(r"[^a-z0-9]+", "", designator.casefold())
    return f"{code}-{name}-parking-chart-rwy{runway}.svg"


def parking_pdf_output_filename(airport: AirportData) -> str:
    """Return the filename for the combined runway-end parking PDF."""

    code = airport.icao.casefold() if airport.icao else f"camp-{airport.campaign_id}"
    name = re.sub(r"[^a-z0-9]+", "-", airport.name.casefold()).strip("-")
    return f"{code}-{name}-parking-chart.pdf"


def _style(root: ET.Element) -> None:
    _element(
        "style",
        parent=root,
        text="""
text { font-family: Arial, Helvetica, sans-serif; fill: #182026; }
.page { fill: #fff; }
.border { fill: none; stroke: #182026; stroke-width: 2; }
.title { font-size: 39px; font-weight: 700; letter-spacing: -0.7px; }
.subtitle { font-size: 16px; font-weight: 700; letter-spacing: 2.2px; fill: #52606a; }
.graticule-line { fill: none; stroke: #aeb8bb; stroke-width: 1; }
.graticule-tick { fill: none; stroke: #7d898d; stroke-width: 1.2; }
.graticule-label-background { fill: #fff; }
.graticule-label { fill: #52606a; font-size: 16.46px; font-weight: 700; text-anchor: middle; dominant-baseline: central; }
.taxiway-surface { fill: #e4e7e6; stroke: none; fill-rule: nonzero; }
.route-line { fill: none; stroke: #858c8c; stroke-width: 3; stroke-linecap: round; stroke-linejoin: round; }
.runway { fill: #20272b; stroke: #0d1113; stroke-width: 2; }
.runway-dimensions { fill: #fff; font-size: 16.46px; font-weight: 700; text-anchor: middle; dominant-baseline: central; }
.arresting-system { fill: none; stroke: #182026; stroke-width: 1.6; stroke-linecap: square; stroke-linejoin: miter; }
.runway-end-text { fill: #182026; font-size: 25.87px; font-weight: 700; text-anchor: middle; dominant-baseline: central; }
.parking { fill: #fff; stroke: #4e5a5e; stroke-width: 1.5; }
.parking-shelter { fill: #d9e8de; stroke: #426657; stroke-width: 1.5; }
.parking-number { fill: #fff; stroke: #384448; stroke-width: 1.2; }
.parking-number-unavailable { fill: #c83f34; stroke: #8e261f; stroke-width: 1.2; }
.parking-number-text { font-size: 16.46px; font-weight: 700; text-anchor: middle; dominant-baseline: central; }
.parking-number-text-unavailable { fill: #fff; font-size: 16.46px; font-weight: 700; text-anchor: middle; dominant-baseline: central; }
.parking-number-leader { fill: none; stroke: #687376; stroke-width: 1; stroke-linecap: round; }
.taxiway-label { font-size: 21.17px; font-weight: 700; text-anchor: middle; dominant-baseline: central; paint-order: stroke; stroke: #fff; stroke-width: 6.51; stroke-linejoin: round; }
.navaid-symbol { fill: #182026; stroke: #182026; stroke-width: 2; stroke-linejoin: miter; }
.navaid-label-background { fill: #fff; }
.navaid-label { font-size: 21.17px; font-weight: 700; text-anchor: middle; }
.building-shape { fill: #aeb7b7; stroke: none; fill-rule: nonzero; }
.building { fill: #aeb7b7; stroke: #596467; stroke-width: 1.2; }
.tower { fill: #fff; stroke: #1a626d; stroke-width: 1.8; }
.tower-text { fill: #1a626d; font-size: 21.17px; font-weight: 700; text-anchor: middle; dominant-baseline: central; }
.north-true { fill: none; stroke: #182026; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
.north-magnetic { fill: none; stroke: #59656a; stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }
.north-label { font-size: 14px; font-weight: 700; text-anchor: middle; dominant-baseline: central; }
.frequency { fill: #126b3b; font-size: 22.5px; font-weight: 700; }
.meta-value { fill: #182026; font-size: 22.5px; font-weight: 700; }
.runway-summary { fill: #182026; font-size: 22.5px; font-weight: 700; }
.small { fill: #59656a; font-size: 12px; }
.warning { fill: #a2452d; font-size: 24px; font-weight: 700; letter-spacing: 1px; }
""",
    )


def _header(
    root: ET.Element,
    airport: AirportData,
    parking_chart: ParkingChart | None,
) -> None:
    _element(
        "text",
        {"class": "title", "x": str(CONTENT_INSET), "y": "65"},
        root,
        airport.name,
    )
    _element(
        "text",
        {"class": "subtitle", "x": str(CONTENT_INSET + 2), "y": "98"},
        root,
        (
            _theater_subtitle("AIRPORT GROUND CHART", airport.theater_name)
            if parking_chart is None
            else _theater_subtitle(
                f"PARKING CHART  •  RWY {parking_chart.designator}",
                airport.theater_name,
            )
        ),
    )
    _communications(root, airport)


def _communications(root: ET.Element, airport: AirportData) -> None:
    x = PAGE_WIDTH - CONTENT_INSET
    station = airport.station
    if station is None:
        _element(
            "text",
            {
                "class": "frequency",
                "x": str(x),
                "y": "42",
                "text-anchor": "end",
            },
            root,
            "NO STATION ROW",
        )
        return
    rows = (
        ("ATIS", _frequency(station.atis_vhf)),
        ("GND", _frequency(station.ground_uhf)),
        ("TWR", f"{_frequency(station.tower_uhf)} / {_frequency(station.tower_vhf)}"),
        ("APP/DEP", _frequency(station.approach_uhf)),
    )
    for index, (label, value) in enumerate(rows):
        _element(
            "text",
            {
                "class": "frequency",
                "x": str(x),
                "y": str(42 + index * 23),
                "text-anchor": "end",
            },
            root,
            f"{label} {value}",
        )


def _map_transform(
    airport,
    runways,
    arresting_systems,
    routes,
    parking,
    taxiway_surfaces: tuple[TaxiwaySurface, ...] = (),
    building_shapes: tuple[BuildingShape, ...] = (),
) -> MapTransform:
    points: list[Point] = []
    for feature in airport.layout.features:
        if (
            feature.name == "Control Tower"
            or is_building_feature(feature)
            or _is_windsock_feature(feature)
        ):
            points.append(Point(feature.offset_x, feature.offset_y))
    for left, right in routes:
        points.extend((left, right))
    for runway in runways:
        points.extend((runway.start, runway.end))
    for system in arresting_systems:
        points.extend(_arresting_system_render_endpoints(system))
    points.extend(position.point for position in parking)
    points.extend(
        Point(aid.offset_x, aid.offset_y) for aid in airport.navigation_aids
    )
    for surface in taxiway_surfaces:
        points.extend(surface.hull)
    for shape in building_shapes:
        points.extend(shape.hull)
    if not points:
        points = [
            Point(-EMPTY_MAP_HALF_EXTENT_FT, -EMPTY_MAP_HALF_EXTENT_FT),
            Point(EMPTY_MAP_HALF_EXTENT_FT, EMPTY_MAP_HALF_EXTENT_FT),
        ]
    rotation_degrees = _best_map_rotation(points)
    rotated_points = tuple(
        _rotate_point(point, rotation_degrees) for point in points
    )
    min_x = min(point.x for point in rotated_points)
    max_x = max(point.x for point in rotated_points)
    min_y = min(point.y for point in rotated_points)
    max_y = max(point.y for point in rotated_points)
    width = max(1.0, max_x - min_x)
    height = max(1.0, max_y - min_y)
    padding = max(width, height) * MAP_PADDING_RATIO
    min_x -= padding
    max_x += padding
    min_y -= padding
    max_y += padding
    width = max_x - min_x
    height = max_y - min_y
    scale = min(MAP_WIDTH / width, MAP_HEIGHT / height)
    used_width = width * scale
    used_height = height * scale
    left = MAP_LEFT + (MAP_WIDTH - used_width) / 2.0
    bottom = MAP_TOP + (MAP_HEIGHT + used_height) / 2.0
    return MapTransform(
        scale,
        left,
        bottom,
        min_x,
        min_y,
        rotation_degrees,
    )


def _best_map_rotation(points: list[Point]) -> float:
    """Return the north-favoring rotation that maximizes map scale."""

    best_angle = 0.0
    search_radius = 90.0
    for step in MAP_ROTATION_STEPS:
        first = math.ceil(-search_radius / step)
        last = math.floor(search_radius / step)
        candidates = (
            _normalize_map_rotation(best_angle + index * step)
            for index in range(first, last + 1)
        )
        best_angle = max(
            candidates,
            key=lambda angle: _map_rotation_score(points, angle),
        )
        search_radius = step
    return round(best_angle, 2)


def _map_rotation_score(
    points: list[Point],
    angle_degrees: float,
) -> tuple[float, float, float, float]:
    min_x, max_x, min_y, max_y = _rotated_bounds(points, angle_degrees)
    width = max_x - min_x
    height = max_y - min_y
    width = max(1.0, width)
    height = max(1.0, height)
    padding = max(width, height) * MAP_PADDING_RATIO
    scale = min(
        MAP_WIDTH / (width + padding * 2.0),
        MAP_HEIGHT / (height + padding * 2.0),
    )
    northward = math.cos(math.radians(angle_degrees))
    return (scale, northward, -abs(angle_degrees), -angle_degrees)


def _normalize_map_rotation(angle_degrees: float) -> float:
    """Normalize a 180-degree-symmetric fit toward page north."""

    return (angle_degrees + 90.0) % 180.0 - 90.0


def _rotate_point(point: Point, angle_degrees: float) -> Point:
    angle = math.radians(angle_degrees)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    return Point(
        point.x * cosine - point.y * sine,
        point.x * sine + point.y * cosine,
    )


def _rotated_bounds(
    points: list[Point],
    angle_degrees: float,
) -> tuple[float, float, float, float]:
    angle = math.radians(angle_degrees)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    first = points[0]
    first_x = first.x * cosine - first.y * sine
    first_y = first.x * sine + first.y * cosine
    min_x = max_x = first_x
    min_y = max_y = first_y
    for point in points[1:]:
        x = point.x * cosine - point.y * sine
        y = point.x * sine + point.y * cosine
        min_x = min(min_x, x)
        max_x = max(max_x, x)
        min_y = min(min_y, y)
        max_y = max(max_y, y)
    return min_x, max_x, min_y, max_y


def _north_indicator(
    parent: ET.Element,
    transform: MapTransform,
    magnetic_variation_degrees: float | None,
) -> None:
    true_x, true_y = transform.north_direction()
    group = _element(
        "g",
        {
            "id": "north-indicator",
            "data-true-direction-x": _number(true_x),
            "data-true-direction-y": _number(true_y),
        },
        parent,
    )
    _element("title", parent=group, text="True and magnetic north")
    _element(
        "text",
        {
            "class": "meta-value",
            "x": str(VARIATION_LABEL_X),
            "y": str(VARIATION_LABEL_Y),
        },
        group,
        _magnetic_variation_label_value(magnetic_variation_degrees),
    )
    true_tip = _direction_arrow(
        group,
        class_name="north-true",
        direction=(true_x, true_y),
        length=NORTH_TRUE_LENGTH,
    )
    _north_side_label(group, true_tip, (true_x, true_y))

    if magnetic_variation_degrees is None:
        return
    magnetic_x, magnetic_y = _rotate_screen_direction(
        true_x,
        true_y,
        magnetic_variation_degrees,
    )
    group.set("data-magnetic-direction-x", _number(magnetic_x))
    group.set("data-magnetic-direction-y", _number(magnetic_y))
    _direction_arrow(
        group,
        class_name="north-magnetic",
        direction=(magnetic_x, magnetic_y),
        length=NORTH_MAGNETIC_LENGTH,
    )


def _direction_arrow(
    parent: ET.Element,
    *,
    class_name: str,
    direction: tuple[float, float],
    length: float,
) -> tuple[float, float]:
    direction_x, direction_y = direction
    perpendicular_x = -direction_y
    perpendicular_y = direction_x
    tip_x = NORTH_INDICATOR_BASE_X + direction_x * length
    tip_y = NORTH_INDICATOR_BASE_Y + direction_y * length
    arrow_length = 8.0
    arrow_half_width = 5.5
    arrow_base_x = tip_x - direction_x * arrow_length
    arrow_base_y = tip_y - direction_y * arrow_length
    _element(
        "path",
        {
            "class": class_name,
            "d": (
                f"M{_number(NORTH_INDICATOR_BASE_X)} "
                f"{_number(NORTH_INDICATOR_BASE_Y)} "
                f"L{_number(tip_x)} {_number(tip_y)} "
                f"M{_number(arrow_base_x + perpendicular_x * arrow_half_width)} "
                f"{_number(arrow_base_y + perpendicular_y * arrow_half_width)} "
                f"L{_number(tip_x)} {_number(tip_y)} "
                f"L{_number(arrow_base_x - perpendicular_x * arrow_half_width)} "
                f"{_number(arrow_base_y - perpendicular_y * arrow_half_width)}"
            ),
        },
        parent,
    )
    return tip_x, tip_y


def _north_side_label(
    parent: ET.Element,
    tip: tuple[float, float],
    direction: tuple[float, float],
) -> None:
    side_offset = 13.0
    perpendicular_x = -direction[1]
    perpendicular_y = direction[0]
    _element(
        "text",
        {
            "class": "north-label",
            "x": _number(tip[0] + perpendicular_x * side_offset),
            "y": _number(tip[1] + perpendicular_y * side_offset),
        },
        parent,
        "N",
    )


def _rotate_screen_direction(
    x: float,
    y: float,
    angle_degrees: float,
) -> tuple[float, float]:
    angle = math.radians(angle_degrees)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    return (
        x * cosine - y * sine,
        x * sine + y * cosine,
    )


def _geographic_graticule(
    airport: AirportData,
    transform: MapTransform,
) -> tuple[GraticuleLine, ...]:
    """Build projected major lines with 0.1-minute ticks along each line."""

    if airport.projection_string is None:
        return ()
    try:
        projection = TransverseMercatorProjection.from_proj_string(
            airport.projection_string
        )
    except GeographicProjectionError:
        return ()

    bounds = _geographic_map_bounds(airport, transform, projection)
    latitude_units = _graticule_coordinate_units(bounds[0], bounds[1])
    longitude_units = _graticule_coordinate_units(bounds[2], bounds[3])
    if latitude_units is None or longitude_units is None:
        return ()

    graticule_lines: list[GraticuleLine] = []
    latitude_margin = 2.0 / GRATICULE_UNITS_PER_DEGREE
    longitude_margin = 2.0 / GRATICULE_UNITS_PER_DEGREE
    axis_specs = (
        (
            "latitude",
            latitude_units,
            longitude_units,
            bounds[2] - longitude_margin,
            bounds[3] + longitude_margin,
            ("left", "right", "top", "bottom"),
        ),
        (
            "longitude",
            longitude_units,
            latitude_units,
            bounds[0] - latitude_margin,
            bounds[1] + latitude_margin,
            ("bottom", "top", "left", "right"),
        ),
    )
    for (
        axis,
        coordinate_range,
        varying_range,
        varying_minimum,
        varying_maximum,
        edge_priority,
    ) in axis_specs:
        for coordinate_units in coordinate_range:
            if coordinate_units % GRATICULE_MAJOR_UNIT_INTERVAL != 0:
                continue
            coordinate = coordinate_units / GRATICULE_UNITS_PER_DEGREE
            points = _projected_graticule_points(
                airport,
                transform,
                projection,
                axis=axis,
                coordinate=coordinate,
                varying_minimum=varying_minimum,
                varying_maximum=varying_maximum,
            )
            intersections = _graticule_rectangle_intersections(points)
            if len(intersections) < 2:
                continue
            label_point = _graticule_label_point(
                intersections,
                edge_priority=edge_priority,
            )
            if label_point is None:
                continue
            label = GraticuleLabel(
                axis=axis,
                coordinate_units=coordinate_units,
                x=label_point[0],
                y=label_point[1],
                text=_format_geographic_coordinate(
                    coordinate,
                    axis=axis,
                ),
            )
            graticule_lines.append(
                GraticuleLine(
                    axis=axis,
                    coordinate_units=coordinate_units,
                    points=points,
                    ticks=_graticule_inline_ticks(
                        airport,
                        transform,
                        projection,
                        axis=axis,
                        coordinate=coordinate,
                        varying_range=varying_range,
                    ),
                    label=label,
                )
            )

    return tuple(graticule_lines)


def _geographic_graticule_labels(
    root: ET.Element,
    labels: tuple[GraticuleLabel, ...],
) -> None:
    if not labels:
        return
    definitions = _element("defs", parent=root)
    clip_path = _element(
        "clipPath",
        {"id": "airport-map-clip"},
        definitions,
    )
    _element(
        "rect",
        {
            "x": _number(MAP_LEFT),
            "y": _number(MAP_TOP),
            "width": _number(MAP_WIDTH),
            "height": _number(MAP_HEIGHT),
        },
        clip_path,
    )
    frame_clip_path = _element(
        "clipPath",
        {"id": "chart-frame-clip"},
        definitions,
    )
    frame_clip_inset = FRAME_INSET + 2.0
    _element(
        "rect",
        {
            "x": _number(frame_clip_inset),
            "y": _number(frame_clip_inset),
            "width": _number(PAGE_WIDTH - frame_clip_inset * 2.0),
            "height": _number(PAGE_HEIGHT - frame_clip_inset * 2.0),
        },
        frame_clip_path,
    )
    mask = _element(
        "mask",
        {
            "id": "graticule-label-gap-mask",
            "maskUnits": "userSpaceOnUse",
            "x": _number(MAP_LEFT),
            "y": _number(MAP_TOP),
            "width": _number(MAP_WIDTH),
            "height": _number(MAP_HEIGHT),
        },
        definitions,
    )
    _element(
        "rect",
        {
            "x": _number(MAP_LEFT),
            "y": _number(MAP_TOP),
            "width": _number(MAP_WIDTH),
            "height": _number(MAP_HEIGHT),
            "fill": "#fff",
        },
        mask,
    )
    for label in labels:
        width = _graticule_label_width(label)
        _element(
            "rect",
            {
                "x": _number(label.x - width / 2.0 - 2.0),
                "y": _number(label.y - GRATICULE_LABEL_HEIGHT / 2.0 - 2.0),
                "width": _number(width + 4.0),
                "height": _number(GRATICULE_LABEL_HEIGHT + 4.0),
                "fill": "#000",
            },
            mask,
        )
    group = _element(
        "g",
        {
            "id": "geographic-graticule-labels",
            "clip-path": "url(#chart-frame-clip)",
        },
        root,
    )
    for label in labels:
        coordinate_id = (
            f"lat-{label.coordinate_units}"
            if label.axis == "latitude"
            else f"lon-{label.coordinate_units}"
        )
        label_group = _element(
            "g",
            {
                "class": "graticule-label-group",
                "data-axis": label.axis,
                "data-coordinate-id": coordinate_id,
            },
            group,
        )
        width = _graticule_label_width(label)
        _element(
            "rect",
            {
                "class": "graticule-label-background",
                "x": _number(label.x - width / 2.0),
                "y": _number(label.y - GRATICULE_LABEL_HEIGHT / 2.0),
                "width": _number(width),
                "height": _number(GRATICULE_LABEL_HEIGHT),
            },
            label_group,
        )
        _element(
            "text",
            {
                "class": "graticule-label",
                "x": _number(label.x),
                "y": _number(label.y),
            },
            label_group,
            label.text,
        )


def _geographic_graticule_overlay(
    root: ET.Element,
    lines: tuple[GraticuleLine, ...],
) -> None:
    if not lines:
        return
    group = _element(
        "g",
        {
            "id": "geographic-graticule",
            "data-major-minutes": _number(GRATICULE_MAJOR_MINUTES),
            "data-minor-minutes": _number(GRATICULE_MINOR_MINUTES),
            "clip-path": "url(#airport-map-clip)",
            "mask": "url(#graticule-label-gap-mask)",
        },
        root,
    )
    _element(
        "title",
        parent=group,
        text=(
            "Latitude and longitude lines every 0.5 minute with "
            "0.1-minute inline ticks"
        ),
    )
    line_group = _element("g", {"id": "graticule-lines"}, group)
    tick_group = _element("g", {"id": "graticule-ticks"}, group)
    for line in lines:
        coordinate = line.coordinate_units / GRATICULE_UNITS_PER_DEGREE
        coordinate_id = (
            f"lat-{line.coordinate_units}"
            if line.axis == "latitude"
            else f"lon-{line.coordinate_units}"
        )
        _graticule_line(
            line_group,
            line.points,
            axis=line.axis,
            coordinate=coordinate,
            coordinate_id=coordinate_id,
        )
        for tick in line.ticks:
            _graticule_inline_tick(
                tick_group,
                line,
                tick,
                coordinate_id=coordinate_id,
            )


def _graticule_label_width(label: GraticuleLabel) -> float:
    return max(72.0, len(label.text) * 9.0 + 12.0)


def _geographic_map_bounds(
    airport: AirportData,
    transform: MapTransform,
    projection: TransverseMercatorProjection,
) -> tuple[float, float, float, float]:
    geographic_points: list[tuple[float, float]] = []
    for index in range(9):
        fraction = index / 8.0
        x = MAP_LEFT + MAP_WIDTH * fraction
        y = MAP_TOP + MAP_HEIGHT * fraction
        for screen_x, screen_y in (
            (x, MAP_TOP),
            (x, MAP_BOTTOM),
            (MAP_LEFT, y),
            (MAP_LEFT + MAP_WIDTH, y),
        ):
            layout_point = transform.unpoint(screen_x, screen_y)
            north_ft, east_ft = _layout_to_theater_position(
                airport,
                layout_point,
            )
            geographic_points.append(
                projection.unproject(
                    east_ft / FEET_PER_METER,
                    north_ft / FEET_PER_METER,
                )
            )
    latitudes = tuple(point[0] for point in geographic_points)
    longitudes = tuple(point[1] for point in geographic_points)
    return min(latitudes), max(latitudes), min(longitudes), max(longitudes)


def _graticule_coordinate_units(
    minimum: float,
    maximum: float,
) -> range | None:
    first = math.floor(minimum * GRATICULE_UNITS_PER_DEGREE) - 2
    last = math.ceil(maximum * GRATICULE_UNITS_PER_DEGREE) + 2
    if last - first + 1 > GRATICULE_MAX_COORDINATE_COUNT:
        return None
    return range(first, last + 1)


def _projected_graticule_points(
    airport: AirportData,
    transform: MapTransform,
    projection: TransverseMercatorProjection,
    *,
    axis: str,
    coordinate: float,
    varying_minimum: float,
    varying_maximum: float,
) -> tuple[tuple[float, float], ...]:
    points: list[tuple[float, float]] = []
    for index in range(GRATICULE_LINE_SAMPLE_COUNT + 1):
        varying = varying_minimum + (
            varying_maximum - varying_minimum
        ) * index / GRATICULE_LINE_SAMPLE_COUNT
        points.append(
            _projected_graticule_point(
                airport,
                transform,
                projection,
                axis=axis,
                coordinate=coordinate,
                varying=varying,
            )
        )
    return tuple(points)


def _projected_graticule_point(
    airport: AirportData,
    transform: MapTransform,
    projection: TransverseMercatorProjection,
    *,
    axis: str,
    coordinate: float,
    varying: float,
) -> tuple[float, float]:
    latitude, longitude = (
        (coordinate, varying)
        if axis == "latitude"
        else (varying, coordinate)
    )
    easting_m, northing_m = projection.project(latitude, longitude)
    layout_point = _theater_position_to_layout(
        airport,
        northing_m * FEET_PER_METER,
        easting_m * FEET_PER_METER,
    )
    return transform.point(layout_point)


def _graticule_inline_ticks(
    airport: AirportData,
    transform: MapTransform,
    projection: TransverseMercatorProjection,
    *,
    axis: str,
    coordinate: float,
    varying_range: range,
) -> tuple[GraticuleTick, ...]:
    ticks: list[GraticuleTick] = []
    tangent_delta = 0.01 / 60.0
    half_length = GRATICULE_MINOR_TICK_LENGTH / 2.0
    edge_clearance = GRATICULE_MINOR_TICK_LENGTH
    right = MAP_LEFT + MAP_WIDTH
    for varying_units in varying_range:
        if varying_units % GRATICULE_MAJOR_UNIT_INTERVAL == 0:
            continue
        varying = varying_units / GRATICULE_UNITS_PER_DEGREE
        center_x, center_y = _projected_graticule_point(
            airport,
            transform,
            projection,
            axis=axis,
            coordinate=coordinate,
            varying=varying,
        )
        if not (
            MAP_LEFT + edge_clearance < center_x < right - edge_clearance
            and MAP_TOP + edge_clearance < center_y < MAP_BOTTOM - edge_clearance
        ):
            continue
        before_x, before_y = _projected_graticule_point(
            airport,
            transform,
            projection,
            axis=axis,
            coordinate=coordinate,
            varying=varying - tangent_delta,
        )
        after_x, after_y = _projected_graticule_point(
            airport,
            transform,
            projection,
            axis=axis,
            coordinate=coordinate,
            varying=varying + tangent_delta,
        )
        tangent_x = after_x - before_x
        tangent_y = after_y - before_y
        tangent_length = math.hypot(tangent_x, tangent_y)
        if tangent_length <= 1e-9:
            continue
        normal_x = -tangent_y / tangent_length * half_length
        normal_y = tangent_x / tangent_length * half_length
        ticks.append(
            GraticuleTick(
                varying_coordinate_units=varying_units,
                start=(center_x - normal_x, center_y - normal_y),
                end=(center_x + normal_x, center_y + normal_y),
            )
        )
    return tuple(ticks)


def _layout_to_theater_position(
    airport: AirportData,
    point: Point,
) -> tuple[float, float]:
    angle = math.radians(airport.placement.heading)
    delta_north = point.x * math.sin(angle) + point.y * math.cos(angle)
    delta_east = point.x * math.cos(angle) - point.y * math.sin(angle)
    return (
        airport.placement.position_x + delta_north,
        airport.placement.position_y + delta_east,
    )


def _theater_position_to_layout(
    airport: AirportData,
    north_ft: float,
    east_ft: float,
) -> Point:
    delta_north = north_ft - airport.placement.position_x
    delta_east = east_ft - airport.placement.position_y
    angle = math.radians(airport.placement.heading)
    return Point(
        delta_north * math.sin(angle) + delta_east * math.cos(angle),
        delta_north * math.cos(angle) - delta_east * math.sin(angle),
    )


def _graticule_line(
    parent: ET.Element,
    points: tuple[tuple[float, float], ...],
    *,
    axis: str,
    coordinate: float,
    coordinate_id: str,
) -> None:
    path = " ".join(
        (
            "M" if index == 0 else "L"
        ) + f"{_number(point[0])} {_number(point[1])}"
        for index, point in enumerate(points)
    )
    _element(
        "path",
        {
            "class": f"graticule-line graticule-{axis}-line",
            "d": path,
            f"data-{axis}": _matrix_number(coordinate),
            "data-coordinate-id": coordinate_id,
        },
        parent,
    )


def _graticule_rectangle_intersections(
    points: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float, str], ...]:
    intersections: list[tuple[float, float, str, float]] = []
    right = MAP_LEFT + MAP_WIDTH
    for segment_index, ((x1, y1), (x2, y2)) in enumerate(
        zip(points, points[1:])
    ):
        delta_x = x2 - x1
        delta_y = y2 - y1
        candidates: list[tuple[float, float, str, float]] = []
        if abs(delta_x) > 1e-9:
            for edge, boundary in (("left", MAP_LEFT), ("right", right)):
                fraction = (boundary - x1) / delta_x
                y = y1 + fraction * delta_y
                if -1e-9 <= fraction <= 1.0 + 1e-9 and MAP_TOP <= y <= MAP_BOTTOM:
                    candidates.append((boundary, y, edge, fraction))
        if abs(delta_y) > 1e-9:
            for edge, boundary in (("top", MAP_TOP), ("bottom", MAP_BOTTOM)):
                fraction = (boundary - y1) / delta_y
                x = x1 + fraction * delta_x
                if -1e-9 <= fraction <= 1.0 + 1e-9 and MAP_LEFT <= x <= right:
                    candidates.append((x, boundary, edge, fraction))
        for x, y, edge, fraction in candidates:
            if any(math.hypot(x - old[0], y - old[1]) < 0.01 for old in intersections):
                continue
            intersections.append((x, y, edge, segment_index + fraction))
    intersections.sort(key=lambda item: item[3])
    return tuple((x, y, edge) for x, y, edge, _ in intersections)


def _graticule_inline_tick(
    parent: ET.Element,
    line: GraticuleLine,
    tick: GraticuleTick,
    *,
    coordinate_id: str,
) -> None:
    coordinate = line.coordinate_units / GRATICULE_UNITS_PER_DEGREE
    varying = tick.varying_coordinate_units / GRATICULE_UNITS_PER_DEGREE
    varying_axis = "longitude" if line.axis == "latitude" else "latitude"
    _element(
        "path",
        {
            "class": "graticule-tick graticule-minor-tick",
            "d": (
                f"M{_number(tick.start[0])} {_number(tick.start[1])} "
                f"L{_number(tick.end[0])} {_number(tick.end[1])}"
            ),
            "data-axis": line.axis,
            f"data-{line.axis}": _matrix_number(coordinate),
            f"data-{varying_axis}-tick": _matrix_number(varying),
            "data-tick-coordinate": str(tick.varying_coordinate_units),
            "data-placement": "inline",
            "data-coordinate-id": coordinate_id,
        },
        parent,
    )


def _graticule_label_point(
    intersections: tuple[tuple[float, float, str], ...],
    *,
    edge_priority: tuple[str, ...],
) -> tuple[float, float] | None:
    chosen = next(
        (
            intersection
            for edge in edge_priority
            for intersection in intersections
            if intersection[2] == edge
        ),
        None,
    )
    if chosen is None:
        return None
    other = max(
        (item for item in intersections if item is not chosen),
        key=lambda item: math.hypot(item[0] - chosen[0], item[1] - chosen[1]),
        default=None,
    )
    if other is None:
        return None
    delta_x = other[0] - chosen[0]
    delta_y = other[1] - chosen[1]
    distance = math.hypot(delta_x, delta_y)
    if distance <= GRATICULE_LABEL_EDGE_OFFSET * 2.0:
        return None
    fraction = GRATICULE_LABEL_EDGE_OFFSET / distance
    return (
        chosen[0] + delta_x * fraction,
        chosen[1] + delta_y * fraction,
    )


def _format_geographic_coordinate(value: float, *, axis: str) -> str:
    if axis == "latitude":
        cardinal = "N" if value >= 0.0 else "S"
    elif axis == "longitude":
        cardinal = "E" if value >= 0.0 else "W"
    else:
        raise ValueError(f"unknown geographic coordinate axis {axis!r}")
    absolute = abs(value)
    degrees = int(math.floor(absolute))
    minutes = round((absolute - degrees) * 60.0, 1)
    if minutes >= 60.0:
        degrees += 1
        minutes = 0.0
    return f"{degrees}°{minutes:04.1f}′{cardinal}"


def _routes(parent, routes, transform) -> None:
    group = _element("g", {"id": "taxi-routes"}, parent)
    for left, right in routes:
        x1, y1 = transform.point(left)
        x2, y2 = transform.point(right)
        attrs = {
            "x1": _number(x1),
            "y1": _number(y1),
            "x2": _number(x2),
            "y2": _number(y2),
        }
        _element("line", {**attrs, "class": "route-line"}, group)


def _taxiway_surfaces(
    parent: ET.Element,
    surfaces: tuple[TaxiwaySurface, ...],
    transform: MapTransform,
) -> None:
    """Render filled authored model triangles without internal boundaries."""

    group = _element("g", {"id": "taxiway-surfaces"}, parent)
    for surface in surfaces:
        commands: list[str] = []
        for triangle in surface.triangles:
            first, second, third = (
                transform.point(point) for point in triangle
            )
            commands.append(
                f"M{_pair(first)} L{_pair(second)} L{_pair(third)} Z"
            )
        path = _element(
            "path",
            {
                "class": "taxiway-surface",
                "d": " ".join(commands),
                "data-feature-index": str(surface.feature_index),
                "data-graphics-id": str(surface.graphics_id),
                "data-source-model": surface.source_path.name,
                "data-triangle-count": str(len(surface.triangles)),
            },
            group,
        )
        _element(
            "title",
            parent=path,
            text=(
                f"Authored taxiway surface, graphics {surface.graphics_id}, "
                f"{surface.source_path.name}"
            ),
        )


def _oriented_box(
    center_x: float,
    center_y: float,
    width: float,
    height: float,
    rotation: float,
) -> tuple[tuple[float, float], ...]:
    angle = math.radians(rotation)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    half_width = width / 2.0
    half_height = height / 2.0
    return tuple(
        (
            center_x + local_x * cosine - local_y * sine,
            center_y + local_x * sine + local_y * cosine,
        )
        for local_x, local_y in (
            (-half_width, -half_height),
            (half_width, -half_height),
            (half_width, half_height),
            (-half_width, half_height),
        )
    )


def _oriented_boxes_overlap(
    left: tuple[tuple[float, float], ...],
    right: tuple[tuple[float, float], ...],
) -> bool:
    for box in (left, right):
        for start, end in zip(box, box[1:] + box[:1]):
            edge_x = end[0] - start[0]
            edge_y = end[1] - start[1]
            axis_x = -edge_y
            axis_y = edge_x
            left_projection = [x * axis_x + y * axis_y for x, y in left]
            right_projection = [x * axis_x + y * axis_y for x, y in right]
            if max(left_projection) <= min(right_projection):
                return False
            if max(right_projection) <= min(left_projection):
                return False
    return True


def _runways(parent, runways, transform) -> None:
    group = _element("g", {"id": "runways"}, parent)
    for runway in runways:
        corners = _runway_corners(runway)
        _element(
            "polygon",
            {
                "class": "runway",
                "points": " ".join(
                    _pair(transform.point(point)) for point in corners
                ),
            },
            group,
        )
    for runway in runways:
        if runway.length_meters is None or runway.width_meters is None:
            continue
        start_x, start_y = transform.point(runway.start)
        end_x, end_y = transform.point(runway.end)
        label_x = (start_x + end_x) / 2.0
        label_y = (start_y + end_y) / 2.0
        rotation = math.degrees(math.atan2(end_y - start_y, end_x - start_x))
        if rotation > 90.0:
            rotation -= 180.0
        elif rotation <= -90.0:
            rotation += 180.0
        length_meters = math.floor(runway.length_meters + 0.5)
        width_meters = math.floor(runway.width_meters + 0.5)
        _element(
            "text",
            {
                "class": "runway-dimensions",
                "x": _number(label_x),
                "y": _number(label_y),
                "transform": (
                    f"rotate({_number(rotation)} "
                    f"{_number(label_x)} {_number(label_y)})"
                ),
                "data-length-meters": str(length_meters),
                "data-width-meters": str(width_meters),
            },
            group,
            f"{length_meters} X {width_meters} M",
        )
    placed_label_boxes: list[tuple[tuple[float, float], ...]] = []
    for runway in runways:
        for runway_end in runway.ends:
            threshold_x, threshold_y = transform.point(runway_end.point)
            opposite_point = (
                runway.end
                if runway_end.point == runway.start
                else runway.start
            )
            opposite_x, opposite_y = transform.point(opposite_point)
            inward_x = opposite_x - threshold_x
            inward_y = opposite_y - threshold_y
            inward_length = max(1.0, math.hypot(inward_x, inward_y))
            inward_x /= inward_length
            inward_y /= inward_length
            rotation = math.degrees(math.atan2(inward_x, -inward_y))
            text_width = (
                len(runway_end.designator) * RUNWAY_LABEL_FONT_SIZE * 0.56
            )
            label_offset = RUNWAY_LABEL_THRESHOLD_OFFSET
            while True:
                label_x = threshold_x - inward_x * label_offset
                label_y = threshold_y - inward_y * label_offset
                label_box = _oriented_box(
                    label_x,
                    label_y,
                    text_width,
                    RUNWAY_LABEL_FONT_SIZE,
                    rotation,
                )
                if not any(
                    _oriented_boxes_overlap(label_box, placed_box)
                    for placed_box in placed_label_boxes
                ):
                    break
                label_offset += RUNWAY_LABEL_COLLISION_STEP
            placed_label_boxes.append(label_box)
            _element(
                "text",
                {
                    "class": "runway-end-text",
                    "x": _number(label_x),
                    "y": _number(label_y),
                    "textLength": _number(text_width),
                    "lengthAdjust": "spacingAndGlyphs",
                    "transform": (
                        f"rotate({_number(rotation)} "
                        f"{_number(label_x)} {_number(label_y)})"
                    ),
                },
                group,
                runway_end.designator,
            )


def _arresting_systems(
    parent: ET.Element,
    systems: tuple[ArrestingSystem, ...],
    transform: MapTransform,
) -> None:
    """Render authored arrestor cables with opposed hooked arrows."""

    group = _element("g", {"id": "arresting-systems"}, parent)
    _element("title", parent=group, text="Aircraft arresting systems")
    hook_length = 18.0
    arrow_length = 6.5
    arrow_half_width = 4.0
    for index, system in enumerate(systems):
        start, end = _arresting_system_render_endpoints(system)
        start_x, start_y = transform.point(start)
        end_x, end_y = transform.point(end)
        cross_x = end_x - start_x
        cross_y = end_y - start_y
        length = math.hypot(cross_x, cross_y)
        if length < 0.1:
            continue
        cross_x /= length
        cross_y /= length
        axis_x = -cross_y
        axis_y = cross_x

        start_tip_x = start_x - axis_x * hook_length
        start_tip_y = start_y - axis_y * hook_length
        end_tip_x = end_x + axis_x * hook_length
        end_tip_y = end_y + axis_y * hook_length
        start_back_x = start_tip_x + axis_x * arrow_length
        start_back_y = start_tip_y + axis_y * arrow_length
        end_back_x = end_tip_x - axis_x * arrow_length
        end_back_y = end_tip_y - axis_y * arrow_length

        path = (
            f"M{_number(start_tip_x)} {_number(start_tip_y)} "
            f"L{_number(start_x)} {_number(start_y)} "
            f"L{_number(end_x)} {_number(end_y)} "
            f"L{_number(end_tip_x)} {_number(end_tip_y)} "
            f"M{_number(start_back_x + cross_x * arrow_half_width)} "
            f"{_number(start_back_y + cross_y * arrow_half_width)} "
            f"L{_number(start_tip_x)} {_number(start_tip_y)} "
            f"L{_number(start_back_x - cross_x * arrow_half_width)} "
            f"{_number(start_back_y - cross_y * arrow_half_width)} "
            f"M{_number(end_back_x + cross_x * arrow_half_width)} "
            f"{_number(end_back_y + cross_y * arrow_half_width)} "
            f"L{_number(end_tip_x)} {_number(end_tip_y)} "
            f"L{_number(end_back_x - cross_x * arrow_half_width)} "
            f"{_number(end_back_y - cross_y * arrow_half_width)}"
        )
        _element(
            "path",
            {
                "class": "arresting-system",
                "d": path,
                "data-arresting-system": str(index),
                "data-runway-number": str(system.runway_number),
            },
            group,
        )


def _arresting_system_render_endpoints(
    system: ArrestingSystem,
) -> tuple[Point, Point]:
    midpoint_x = (system.start.x + system.end.x) / 2.0
    midpoint_y = (system.start.y + system.end.y) / 2.0
    half_x = (
        (system.end.x - system.start.x)
        * ARRESTING_SYSTEM_CROSSBAR_SCALE
        / 2.0
    )
    half_y = (
        (system.end.y - system.start.y)
        * ARRESTING_SYSTEM_CROSSBAR_SCALE
        / 2.0
    )
    return (
        Point(midpoint_x - half_x, midpoint_y - half_y),
        Point(midpoint_x + half_x, midpoint_y + half_y),
    )


def _parking(parent, positions, transform) -> None:
    group = _element("g", {"id": "parking-positions"}, parent)
    for position in positions:
        x, y = transform.point(position.point)
        if position.type_ in NUMBERED_PARKING_TYPES:
            _element(
                "circle",
                {
                    "class": "parking",
                    "cx": _number(x),
                    "cy": _number(y),
                    "r": "3.4",
                },
                group,
            )
        else:
            _element(
                "rect",
                {
                    "class": "parking-shelter",
                    "x": _number(x - 4),
                    "y": _number(y - 4),
                    "width": "8",
                    "height": "8",
                    "rx": "1",
                },
                group,
            )


def _numbered_parking(
    parent: ET.Element,
    positions: tuple[NumberedParkingPosition, ...],
    transform: MapTransform,
) -> None:
    placements = _layout_numbered_parking(positions, transform)
    leaders = _element("g", {"id": "parking-number-leaders"}, parent)
    for placement in placements:
        axis_x = placement.label_x - placement.anchor_x
        axis_y = placement.label_y - placement.anchor_y
        distance = math.hypot(axis_x, axis_y)
        if distance < 0.5:
            continue
        visible_length = max(0.0, distance - PARKING_MARKER_RADIUS)
        end_x = placement.anchor_x + axis_x / distance * visible_length
        end_y = placement.anchor_y + axis_y / distance * visible_length
        _element(
            "line",
            {
                "class": "parking-number-leader",
                "x1": _number(placement.anchor_x),
                "y1": _number(placement.anchor_y),
                "x2": _number(end_x),
                "y2": _number(end_y),
            },
            leaders,
        )

    group = _element("g", {"id": "parking-numbers"}, parent)
    for placement in placements:
        position = placement.position
        x, y = placement.label_x, placement.label_y
        unavailable = position.group == -1
        marker_class = (
            "parking-number-unavailable" if unavailable else "parking-number"
        )
        text_class = (
            "parking-number-text-unavailable"
            if unavailable
            else "parking-number-text"
        )
        common = {
            "class": marker_class,
            "data-parking-number": str(position.number),
        }
        if position.type_ == 12:
            _element(
                "rect",
                {
                    **common,
                    "x": _number(x - PARKING_LABEL_HALF_WIDTH),
                    "y": _number(y - PARKING_LABEL_HALF_HEIGHT),
                    "width": _number(PARKING_LABEL_HALF_WIDTH * 2.0),
                    "height": _number(PARKING_LABEL_HALF_HEIGHT * 2.0),
                    "rx": _number(1.5 * PARKING_LABEL_SCALE),
                },
                group,
            )
        else:
            _element(
                "circle",
                {
                    **common,
                    "cx": _number(x),
                    "cy": _number(y),
                    "r": _number(PARKING_MARKER_RADIUS),
                },
                group,
            )
        _element(
            "text",
            {
                "class": text_class,
                "x": _number(x),
                "y": _number(y),
            },
            group,
            str(position.number),
        )


def _layout_numbered_parking(
    positions: tuple[NumberedParkingPosition, ...],
    transform: MapTransform,
) -> tuple[ParkingLabelPlacement, ...]:
    anchors = tuple(transform.point(position.point) for position in positions)
    label_points = _displace_parking_label_points(
        anchors,
        bounds=(
            MAP_LEFT + PARKING_LABEL_HALF_WIDTH,
            MAP_LEFT + MAP_WIDTH - PARKING_LABEL_HALF_WIDTH,
            MAP_TOP + PARKING_LABEL_HALF_HEIGHT,
            MAP_TOP + MAP_HEIGHT - PARKING_LABEL_HALF_HEIGHT,
        ),
    )
    return tuple(
        ParkingLabelPlacement(
            position=position,
            anchor_x=anchor[0],
            anchor_y=anchor[1],
            label_x=label_point[0],
            label_y=label_point[1],
        )
        for position, anchor, label_point in zip(
            positions,
            anchors,
            label_points,
        )
    )


def _displace_parking_label_points(
    anchors: tuple[tuple[float, float], ...],
    *,
    bounds: tuple[float, float, float, float] | None = None,
) -> tuple[tuple[float, float], ...]:
    """Greedily place labels near their anchors without box collisions."""

    if not anchors:
        return ()
    neighbor_counts = tuple(
        sum(
            math.hypot(left[0] - right[0], left[1] - right[1])
            < PARKING_NEIGHBOR_DISTANCE
            for right_index, right in enumerate(anchors)
            if left_index != right_index
        )
        for left_index, left in enumerate(anchors)
    )
    placement_order = sorted(
        range(len(anchors)),
        key=lambda index: (-neighbor_counts[index], index),
    )
    placed: list[tuple[float, float] | None] = [None] * len(anchors)
    occupied: list[tuple[float, float]] = []
    for index in placement_order:
        anchor = anchors[index]
        candidates = [anchor]
        direction_shift = (index * 5) % PARKING_LABEL_DIRECTIONS
        for radius in PARKING_LABEL_RADII:
            for direction_index in range(PARKING_LABEL_DIRECTIONS):
                direction = (
                    direction_index + direction_shift
                ) % PARKING_LABEL_DIRECTIONS
                angle = 2.0 * math.pi * direction / PARKING_LABEL_DIRECTIONS
                candidates.append(
                    (
                        anchor[0] + radius * math.cos(angle),
                        anchor[1] + radius * math.sin(angle),
                    )
                )

        best_candidate = anchor
        best_key: tuple[int, float, int] | None = None
        for rank, candidate in enumerate(candidates):
            if bounds is not None and not (
                bounds[0] <= candidate[0] <= bounds[1]
                and bounds[2] <= candidate[1] <= bounds[3]
            ):
                continue
            overlap_areas = tuple(
                _parking_label_overlap_area(candidate, existing)
                for existing in occupied
            )
            overlap_count = sum(area > 0.0 for area in overlap_areas)
            key = (overlap_count, sum(overlap_areas), rank)
            if best_key is None or key < best_key:
                best_candidate = candidate
                best_key = key
            if overlap_count == 0:
                break
        placed[index] = best_candidate
        occupied.append(best_candidate)

    return tuple(point for point in placed if point is not None)


def _parking_label_overlap_area(
    left: tuple[float, float],
    right: tuple[float, float],
) -> float:
    required_x = PARKING_LABEL_HALF_WIDTH * 2.0 + PARKING_LABEL_GAP
    required_y = PARKING_LABEL_HALF_HEIGHT * 2.0 + PARKING_LABEL_GAP
    overlap_x = max(0.0, required_x - abs(left[0] - right[0]))
    overlap_y = max(0.0, required_y - abs(left[1] - right[1]))
    return overlap_x * overlap_y


def _taxiway_labels(
    parent: ET.Element,
    labels: tuple[TaxiwayLabel, ...],
    transform: MapTransform,
) -> None:
    group = _element("g", {"id": "taxiway-labels"}, parent)
    for label in labels:
        x, y = transform.point(label.point)
        _element(
            "text",
            {
                "class": "taxiway-label",
                "x": _number(x),
                "y": _number(y),
            },
            group,
            label.letter,
        )


def _navigation_aids(
    parent: ET.Element,
    aids: tuple[NavigationAidData, ...],
    transform: MapTransform,
) -> None:
    group = _element("g", {"id": "navigation-aids"}, parent)
    for aid in aids:
        x, y = transform.point(Point(aid.offset_x, aid.offset_y))
        channel_label = f"CH {aid.channel}{aid.band}"
        label_width = max(
            NAVAID_LABEL_MIN_WIDTH,
            len(channel_label) * NAVAID_LABEL_CHARACTER_WIDTH
            + NAVAID_LABEL_PADDING,
        )
        item = _element(
            "g",
            {
                "class": "navigation-aid",
                "data-campaign-id": str(aid.campaign_id),
                "data-kind": aid.kind,
            },
            group,
        )
        _element(
            "title",
            parent=item,
            text=(
                f"{aid.name}, channel {aid.channel}{aid.band}, "
                f"range {aid.range_nm} nautical miles"
            ),
        )
        symbol = _element(
            "g",
            {
                "class": "navaid-symbol",
                "data-symbol": "public-domain-vortac",
                "transform": (
                    f"translate({_number(x)} {_number(y)}) "
                    f"scale({_number(VORTAC_RENDER_SCALE)}) "
                    f"translate(-{VORTAC_CIRCLE[0]} -{VORTAC_CIRCLE[1]})"
                ),
            },
            item,
        )
        for path, fill in VORTAC_PATHS:
            attributes = {"d": path}
            if fill is not None:
                attributes["fill"] = fill
            _element("path", attributes, symbol)
        _element(
            "circle",
            {
                "cx": str(VORTAC_CIRCLE[0]),
                "cy": str(VORTAC_CIRCLE[1]),
                "r": str(VORTAC_CIRCLE[2]),
            },
            symbol,
        )
        _element(
            "rect",
            {
                "class": "navaid-label-background",
                "x": _number(x - label_width / 2.0),
                "y": _number(y + NAVAID_LABEL_TOP_OFFSET),
                "width": _number(label_width),
                "height": _number(NAVAID_LABEL_HEIGHT),
                "rx": _number(2.0 * NAVAID_LABEL_SCALE),
            },
            item,
        )
        _element(
            "text",
            {
                "class": "navaid-label",
                "x": _number(x),
                "y": _number(y + NAVAID_LABEL_BASELINE_OFFSET),
            },
            item,
            channel_label,
        )


def _features(
    parent: ET.Element,
    airport: AirportData,
    building_shapes: tuple[BuildingShape, ...],
    transform: MapTransform,
) -> None:
    buildings = _element("g", {"id": "selected-features"}, parent)
    _building_shapes(buildings, building_shapes, transform)
    _windsocks(buildings, airport, transform)
    shaped_feature_indices = {
        shape.feature_index for shape in building_shapes
    }
    for feature in airport.layout.features:
        name = feature.name or ""
        if name != "Control Tower" and not is_building_feature(feature):
            continue
        if feature.index in shaped_feature_indices:
            continue
        point = Point(feature.offset_x, feature.offset_y)
        x, y = transform.point(point)
        if name == "Control Tower":
            _element(
                "circle",
                {
                    "class": "tower",
                    "cx": _number(x),
                    "cy": _number(y),
                    "r": _number(TOWER_RADIUS),
                },
                buildings,
            )
            _element(
                "text",
                {"class": "tower-text", "x": _number(x), "y": _number(y)},
                buildings,
                "T",
            )
            continue
        rotation = feature.heading - transform.rotation_degrees
        _element(
            "rect",
            {
                "class": "building",
                "x": _number(x - 5),
                "y": _number(y - 3),
                "width": "10",
                "height": "6",
                "rx": "1",
                "transform": (
                    f"rotate({_number(rotation)} {_number(x)} {_number(y)})"
                ),
            },
            buildings,
        )


def _is_windsock_feature(feature) -> bool:
    class_table = feature.class_table
    return (
        class_table is not None
        and (
            class_table.domain,
            class_table.class_,
            class_table.type_,
        )
        == WINDSOCK_FEATURE_CLASSIFICATION
        and (feature.name or "").casefold() == "windsock"
    )


def _windsocks(
    parent: ET.Element,
    airport: AirportData,
    transform: MapTransform,
) -> None:
    group = _element("g", {"id": "windsocks"}, parent)
    for feature in airport.layout.features:
        if not _is_windsock_feature(feature):
            continue
        x, y = transform.point(Point(feature.offset_x, feature.offset_y))
        symbol = windsock_symbol_element(
            x=x,
            y=y,
            size=WINDSOCK_SYMBOL_SIZE,
            attributes={
                "class": "windsock-symbol",
                "role": "img",
                "aria-label": "Windsock",
                "data-symbol": "public-domain-windsock",
                "data-feature-index": str(feature.index),
                "data-graphics-id": str(feature.graphics_normal or ""),
                "data-authored-heading": _number(feature.heading),
            },
        )
        group.append(symbol)
        _element("title", parent=symbol, text="Windsock")


def _building_shapes(
    parent: ET.Element,
    shapes: tuple[BuildingShape, ...],
    transform: MapTransform,
) -> None:
    if not shapes:
        return
    definitions = _element("defs", parent=parent)
    models = {
        shape.model.graphics_id: shape.model
        for shape in shapes
    }
    for graphics_id in sorted(models):
        model = models[graphics_id]
        commands = [
            f"M{_pair((left.x, left.y))} "
            f"L{_pair((middle.x, middle.y))} "
            f"L{_pair((right.x, right.y))} Z"
            for left, middle, right in model.triangles
        ]
        _element(
            "path",
            {
                "id": f"building-model-{graphics_id}",
                "d": " ".join(commands),
                "data-source-model": model.source_path.name,
                "data-triangle-count": str(len(model.triangles)),
            },
            definitions,
        )

    for shape in shapes:
        origin = transform.point(Point(shape.offset_x, shape.offset_y))
        heading = math.radians(shape.heading)
        model_x = transform.point(
            Point(
                shape.offset_x + math.cos(heading),
                shape.offset_y - math.sin(heading),
            )
        )
        model_z = transform.point(
            Point(
                shape.offset_x + math.sin(heading),
                shape.offset_y + math.cos(heading),
            )
        )
        _element(
            "use",
            {
                "class": "building-shape",
                "href": f"#building-model-{shape.model.graphics_id}",
                "transform": (
                    "matrix("
                    f"{_matrix_number(model_x[0] - origin[0])} "
                    f"{_matrix_number(model_x[1] - origin[1])} "
                    f"{_matrix_number(model_z[0] - origin[0])} "
                    f"{_matrix_number(model_z[1] - origin[1])} "
                    f"{_number(origin[0])} {_number(origin[1])}"
                    ")"
                ),
                "data-feature-index": str(shape.feature_index),
                "data-graphics-id": str(shape.model.graphics_id),
                "data-name": shape.name,
            },
            parent,
        )


def _footer(
    root,
    airport,
    runways,
    parking_count,
    parking_chart,
    transform,
) -> None:
    _element(
        "text",
        {
            "class": "meta-value",
            "x": str(CONTENT_INSET),
            "y": str(FOOTER_INFO_Y),
        },
        root,
        f"TCN: {_tacan_label(airport)}",
    )
    _element(
        "text",
        {
            "class": "meta-value",
            "x": str(CONTENT_INSET),
            "y": str(FOOTER_ELEVATION_Y),
        },
        root,
        _airport_elevation_label(airport),
    )
    if parking_chart is not None:
        _element(
            "text",
            {"class": "meta-value", "x": "680", "y": str(FOOTER_INFO_Y)},
            root,
            (
                f"PARKING: RWY {parking_chart.designator} / "
                f"{parking_count} POSITIONS"
            ),
        )

    summaries = []
    for runway in runways:
        designators = "/".join(
            sorted({end.designator for end in runway.ends})
        ) or f"#{runway.runway_number}"
        heading = runway.heading_true
        heading_suffix = "°T"
        if airport.magnetic_variation_degrees is not None:
            heading = (
                heading - airport.magnetic_variation_degrees
            ) % 360.0
            heading_suffix = "°"
        reciprocal = (heading + 180.0) % 360.0
        summaries.append(
            f"RWY {designators}  {heading:05.1f}{heading_suffix} / "
            f"{reciprocal:05.1f}{heading_suffix}"
        )
    for index, summary in enumerate(summaries):
        _element(
            "text",
            {
                "class": "runway-summary",
                "x": str(CONTENT_INSET),
                "y": str(FOOTER_RUNWAY_Y + index * FOOTER_RUNWAY_GAP),
            },
            root,
            summary,
        )
    _north_indicator(
        root,
        transform,
        airport.magnetic_variation_degrees,
    )
    _element(
        "text",
        {
            "class": "warning",
            "x": str(CONTENT_INSET),
            "y": str(BOTTOM_ANNOTATION_Y),
        },
        root,
        "GENERATED FROM FALCON BMS DATA  •  FOR BMS USE ONLY",
    )
    _element(
        "text",
        {
            "class": "small",
            "x": str(PAGE_WIDTH - CONTENT_INSET),
            "y": str(BOTTOM_ANNOTATION_Y),
            "text-anchor": "end",
        },
        root,
        airport.display_code,
    )


def _tacan_label(airport: AirportData) -> str:
    if airport.navigation_aids:
        aid = airport.navigation_aids[0]
        identifier = "" if aid.identifier is None else f"{aid.identifier} "
        return f"{identifier}{aid.channel}{aid.band}/{aid.range_nm}NM"
    station = airport.station
    if station is None or station.tacan_range <= 0:
        return "NOT AVAILABLE"
    return f"{station.tacan_channel}{station.tacan_band}/{station.tacan_range}NM"


def _airport_elevation_label(airport: AirportData) -> str:
    if airport.elevation_ft is None:
        return "AD ELEV: NOT AVAILABLE"
    return f"AD ELEV: {airport.elevation_ft} FT"


def _magnetic_variation_label_value(variation: float | None) -> str:
    if variation is None:
        return "VAR: NOT AVAILABLE"
    rounded = math.floor(abs(variation) * 2.0 + 0.5) / 2.0
    direction = "E" if variation >= 0.0 else "W"
    return f"VAR: {rounded:.1f}° {direction}"


def _theater_subtitle(chart_label: str, theater_name: str | None) -> str:
    return chart_label if theater_name is None else f"{chart_label}  •  {theater_name}"


def _frequency(value: int) -> str:
    if value <= 0:
        return "—"
    formatted = f"{value / 1000.0:.3f}".rstrip("0").rstrip(".")
    return formatted if "." in formatted else f"{formatted}.0"


def _runway_corners(runway: RunwayGeometry) -> tuple[Point, Point, Point, Point]:
    axis_x = runway.end.x - runway.start.x
    axis_y = runway.end.y - runway.start.y
    length = max(1.0, math.hypot(axis_x, axis_y))
    normal_x = -axis_y / length * runway.width / 2.0
    normal_y = axis_x / length * runway.width / 2.0
    return (
        Point(runway.start.x + normal_x, runway.start.y + normal_y),
        Point(runway.end.x + normal_x, runway.end.y + normal_y),
        Point(runway.end.x - normal_x, runway.end.y - normal_y),
        Point(runway.start.x - normal_x, runway.start.y - normal_y),
    )


def _element(tag, attributes=None, parent=None, text=None):
    element_attributes = dict(attributes or {})
    styles: list[str] = []
    if tag == "text":
        styles.append("font-family:Arial,Helvetica,sans-serif;fill:#182026")
    for class_name in element_attributes.get("class", "").split():
        style = CLASS_STYLES.get(class_name)
        if style is not None:
            styles.append(style)
    if element_attributes.get("style"):
        styles.append(element_attributes["style"])
    if styles:
        element_attributes["style"] = ";".join(styles)
    element = ET.Element(f"{{{SVG_NS}}}{tag}", element_attributes)
    if text is not None:
        element.text = text
    if parent is not None:
        parent.append(element)
    return element


def _number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _matrix_number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _pair(point: tuple[float, float]) -> str:
    return f"{_number(point[0])},{_number(point[1])}"

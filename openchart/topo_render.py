"""Render airport-centered topographic charts as self-contained SVG."""

from __future__ import annotations

import base64
import math
from pathlib import Path
import re
import struct
import xml.etree.ElementTree as ET
import zlib

from .geometry import (
    IlsApproach,
    Point,
    RunwayGeometry,
    build_ils_approaches,
    build_route_edges,
    build_runways,
)
from .model_geometry import TaxiwaySurface, build_taxiway_surfaces
from .render import (
    AirportRenderContext,
    BOTTOM_ANNOTATION_Y,
    CONTENT_INSET,
    FRAME_INSET,
    PAGE_HEIGHT,
    PAGE_HEIGHT_MM,
    PAGE_WIDTH,
    PAGE_WIDTH_MM,
)
from .source import AirportData, NavigationObjectiveData
from .symbols import NAVIGATION_SYMBOL_FILENAMES, navigation_symbol_element
from .topography import (
    DEFAULT_AREA_SIZE_NM,
    DEFAULT_ELEVATION_INTERVAL_FT,
    DEFAULT_MAXIMUM_PEAKS,
    DEFAULT_PEAK_GRID_SIZE,
    DEFAULT_PEAK_LOCAL_MAXIMUM_RADIUS_NM,
    DEFAULT_PEAK_MINIMUM_RELIEF_FT,
    DEFAULT_PEAK_MINIMUM_SEPARATION_NM,
    DEFAULT_SAMPLE_STRIDE,
    ElevationWindow,
    LandCoverWindow,
    MsaSector,
    TerrainPeak,
    TopographyError,
    build_msa_sectors,
    build_terrain_peaks,
    read_elevation_window,
    read_land_cover_window,
)


SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)
MAP_LEFT = FRAME_INSET
MAP_TOP = 112.0
MAP_SIZE = PAGE_WIDTH - FRAME_INSET * 2
MAP_BOTTOM = MAP_TOP + MAP_SIZE
MAP_RIGHT = MAP_LEFT + MAP_SIZE
ELEVATION_SCALE_SWATCH_HEIGHT = 16.0
TOPOGRAPHIC_RUNWAY_LENGTH = 52.0
TOPOGRAPHIC_RUNWAY_WIDTH = TOPOGRAPHIC_RUNWAY_LENGTH / 10.0
MSA_INDICATOR_RADIUS = 112.0
MSA_INDICATOR_CENTER_X = MAP_RIGHT - MSA_INDICATOR_RADIUS - 20.0
MSA_INDICATOR_CENTER_Y = MAP_TOP + MSA_INDICATOR_RADIUS + 28.0
NAVAID_SYMBOL_SIZE = 30.0
MINIMAP_LEFT = MAP_LEFT
MINIMAP_TOP = MAP_BOTTOM
MINIMAP_WIDTH = 564.0
MINIMAP_HEIGHT = 400.0
MINIMAP_PADDING = 30.0
MINIMAP_RUNWAY_LABEL_FONT_SIZE = 16.8
MINIMAP_RUNWAY_LABEL_OFFSET = MINIMAP_RUNWAY_LABEL_FONT_SIZE * 0.9
MINIMAP_RUNWAY_LABEL_COLLISION_STEP = MINIMAP_RUNWAY_LABEL_FONT_SIZE * 0.25
MINIMAP_ELEVATION_BOX_WIDTH = 132.0
MINIMAP_ELEVATION_BOX_HEIGHT = 42.0
COMMUNICATIONS_LEFT = MINIMAP_LEFT + MINIMAP_WIDTH
COMMUNICATIONS_TOP = MINIMAP_TOP
COMMUNICATIONS_WIDTH = 800.0
COMMUNICATION_CELL_HEIGHT = 120.0
COMMUNICATION_LABEL_OFFSET = 31.5
COMMUNICATION_VALUE_OFFSET = 68.0
COMMUNICATION_TOWER_VALUE_OFFSETS = (68.0, 101.0)
ILS_CELL_HEIGHT = COMMUNICATION_CELL_HEIGHT
ILS_VALUE_OFFSET = COMMUNICATION_VALUE_OFFSET
ILS_FEATHER_LENGTH_NM = 8.0
ILS_FEATHER_HALF_WIDTH_NM = 0.7
ILS_FEATHER_HEADING_POSITION = 0.72
ILS_FEATHER_HEADING_GAP_NM = 1.25
FEET_PER_NAUTICAL_MILE = 6076.11549
ELEVATION_PALETTE = (
    (230, 241, 244),  # Authored ocean, sea, and river cover.
    (251, 246, 237),  # 0–499 ft.
    (247, 232, 214),  # 500–999 ft.
    (242, 218, 191),  # 1,000–1,499 ft.
    (236, 203, 169),  # 1,500–1,999 ft.
    (229, 187, 147),  # 2,000–2,499 ft.
    (220, 170, 126),  # 2,500–2,999 ft.
    (210, 152, 106),  # 3,000–3,499 ft.
    (199, 135, 90),   # 3,500–3,999 ft.
    (187, 118, 76),   # 4,000–4,499 ft.
    (173, 101, 64),   # 4,500–4,999 ft.
    (158, 85, 54),    # 5,000 ft and above.
)
ELEVATION_PNG_COMPRESSION_LEVEL = 6


def render_topographic_chart(
    airport: AirportData,
    output_path: str | Path,
    *,
    size_nm: float = DEFAULT_AREA_SIZE_NM,
    elevation_interval_ft: int = DEFAULT_ELEVATION_INTERVAL_FT,
    sample_stride: int = DEFAULT_SAMPLE_STRIDE,
) -> Path:
    """Read local terrain and render an A4 airport-centered topo chart."""

    return _write_svg(
        output_path,
        render_topographic_chart_svg(
            airport,
            size_nm=size_nm,
            elevation_interval_ft=elevation_interval_ft,
            sample_stride=sample_stride,
        ),
    )


def render_topographic_chart_svg(
    airport: AirportData,
    *,
    size_nm: float = DEFAULT_AREA_SIZE_NM,
    elevation_interval_ft: int = DEFAULT_ELEVATION_INTERVAL_FT,
    sample_stride: int = DEFAULT_SAMPLE_STRIDE,
    context: AirportRenderContext | None = None,
) -> bytes:
    """Return a self-contained A4 airport-centered topographic SVG."""

    if airport.terrain_height_source is None:
        raise TopographyError(f"{airport.name}: theater has no terrain height map")
    window = read_elevation_window(
        airport.terrain_height_source,
        airport.placement.position_x,
        airport.placement.position_y,
        size_nm=size_nm,
        sample_stride=sample_stride,
    )
    land_cover = (
        None
        if airport.terrain_land_cover_source is None
        else read_land_cover_window(
            airport.terrain_land_cover_source,
            airport.placement.position_x,
            airport.placement.position_y,
            size_nm=size_nm,
            sample_stride=sample_stride,
        )
    )
    if land_cover is not None and (
        land_cover.row_count != window.row_count
        or land_cover.column_count != window.column_count
    ):
        raise TopographyError(
            f"{airport.name}: height and land-cover windows have different dimensions"
        )
    peaks = build_terrain_peaks(window)
    if context is None:
        runways = build_runways(
            airport.layout,
            airport.atc,
            airport.magnetic_variation_degrees,
        )
        routes = build_route_edges(airport.layout)
        taxiway_surfaces = build_taxiway_surfaces(airport)
    else:
        if not isinstance(context, AirportRenderContext):
            raise TypeError("context must be AirportRenderContext or None")
        if context.airport is not airport:
            raise ValueError(
                "render context belongs to a different AirportData instance"
            )
        runways = context.runways
        routes = context.routes
        taxiway_surfaces = context.taxiway_surfaces
    ils_approaches = _airport_ils_approaches(airport, runways)

    root = ET.Element(
        f"{{{SVG_NS}}}svg",
        {
            "width": f"{PAGE_WIDTH_MM}mm",
            "height": f"{PAGE_HEIGHT_MM}mm",
            "viewBox": f"0 0 {PAGE_WIDTH} {PAGE_HEIGHT}",
            "role": "img",
            "aria-label": f"{airport.name} topographic chart",
        },
    )
    _style(root)
    _element(
        "rect",
        {
            "class": "page",
            "width": str(PAGE_WIDTH),
            "height": str(PAGE_HEIGHT),
            "fill": "#fff",
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
            "fill": "none",
            "stroke": "#182026",
            "stroke-width": "2",
        },
        root,
    )
    _element(
        "text",
        {"class": "title", "x": str(CONTENT_INSET), "y": "62"},
        root,
        airport.name,
    )
    subtitle = "TOPOGRAPHIC CHART"
    if airport.theater_name:
        subtitle += f"  •  {airport.theater_name}"
    subtitle += f"  •  {size_nm:g} × {size_nm:g} NM"
    _element(
        "text",
        {"class": "subtitle", "x": str(CONTENT_INSET), "y": "91"},
        root,
        subtitle,
    )
    definitions = _element("defs", parent=root)
    clip = _element("clipPath", {"id": "topographic-map-clip"}, definitions)
    _element(
        "rect",
        {
            "x": _number(MAP_LEFT),
            "y": _number(MAP_TOP),
            "width": _number(MAP_SIZE),
            "height": _number(MAP_SIZE),
        },
        clip,
    )
    feather_pattern = _element(
        "pattern",
        {
            "id": "ils-feather-stipple",
            "patternUnits": "userSpaceOnUse",
            "width": "4",
            "height": "4",
        },
        definitions,
    )
    _element(
        "circle",
        {
            "cx": "1",
            "cy": "1",
            "r": "0.72",
            "fill": "#182026",
            "fill-opacity": "0.68",
        },
        feather_pattern,
    )
    map_group = _element(
        "g",
        {"id": "topographic-map", "clip-path": "url(#topographic-map-clip)"},
        root,
    )
    _elevation_shading(map_group, window, elevation_interval_ft, land_cover)
    range_ring = _airport_range_ring(map_group, airport, window)
    _ils_feathers(map_group, airport, window, ils_approaches)
    peak_boxes = _terrain_peaks(map_group, window, peaks)
    _airport_range_ring_label(*range_ring, peak_boxes)
    _runways(map_group, airport, window, runways)
    navigation_label_boxes = _topographic_navigation_aids(
        map_group,
        airport,
        window,
    )
    _airport_label(map_group, airport, window, navigation_label_boxes)
    _scale_bar(map_group, size_nm)
    _elevation_scale(map_group, size_nm, elevation_interval_ft)
    _north_indicator(map_group, airport)
    _msa_indicator(map_group, airport, window, runways)
    _element(
        "rect",
        {
            "id": "topographic-map-frame",
            "class": "map-frame",
            "x": _number(MAP_LEFT),
            "y": _number(MAP_TOP),
            "width": _number(MAP_SIZE),
            "height": _number(MAP_SIZE),
            "fill": "none",
            "stroke": "#182026",
            "stroke-width": "2",
        },
        root,
    )
    _airport_minimap(
        root,
        airport,
        taxiway_surfaces,
        runways,
        routes,
    )
    _communications(root, airport)
    _ils_frequency_row(root, airport, ils_approaches)
    _footer(root, airport)

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


def topographic_output_filename(airport: AirportData) -> str:
    code = airport.icao.casefold() if airport.icao else f"camp-{airport.campaign_id}"
    name = re.sub(r"[^a-z0-9]+", "-", airport.name.casefold()).strip("-")
    return f"{code}-{name}-topographic-chart.svg"


def _communications(root: ET.Element, airport: AirportData) -> None:
    station = airport.station
    if station is None:
        return
    rows: tuple[tuple[str, str | tuple[str, str]], ...] = (
        ("ATIS", _frequency(station.atis_vhf)),
        ("APP/DEP", _frequency(station.approach_uhf)),
        (
            "TOWER",
            (
                _frequency(station.tower_uhf),
                _frequency(station.tower_vhf),
            ),
        ),
        ("GROUND", _frequency(station.ground_uhf)),
    )
    group = _element(
        "g",
        {"id": "topographic-communications", "data-layout": "4x1"},
        root,
    )
    cell_width = COMMUNICATIONS_WIDTH / len(rows)
    for index, (label, value) in enumerate(rows):
        x = COMMUNICATIONS_LEFT + index * cell_width
        y = COMMUNICATIONS_TOP
        item = _element(
            "g",
            {
                "class": "frequency-box",
                "data-service": label,
            },
            group,
        )
        _element(
            "rect",
            {
                "class": (
                    "frequency-cell frequency-cell-tower"
                    if label == "TOWER"
                    else "frequency-cell"
                ),
                "x": _number(x),
                "y": _number(y),
                "width": _number(cell_width),
                "height": _number(COMMUNICATION_CELL_HEIGHT),
                "fill": "#fff",
                "stroke": "#182026",
                "stroke-width": "2.5" if label == "TOWER" else "1.5",
            },
            item,
        )
        _element(
            "text",
            {
                "class": "frequency-service",
                "x": _number(x + cell_width / 2.0),
                "y": _number(y + COMMUNICATION_LABEL_OFFSET),
                "font-size": "27",
                "font-weight": "400",
                "text-anchor": "middle",
            },
            item,
            label,
        )
        values = value if isinstance(value, tuple) else (value,)
        value_offsets = (
            COMMUNICATION_TOWER_VALUE_OFFSETS
            if label == "TOWER"
            else (COMMUNICATION_VALUE_OFFSET,)
        )
        for value_index, (frequency, value_offset) in enumerate(
            zip(values, value_offsets)
        ):
            attributes = {
                "class": (
                    "frequency-value tower-frequency-value"
                    if label == "TOWER"
                    else "frequency-value"
                ),
                "x": _number(x + cell_width / 2.0),
                "y": _number(y + value_offset),
                "font-size": "27",
                "font-weight": "700",
                "text-anchor": "middle",
            }
            if label == "TOWER":
                attributes["data-band"] = ("UHF", "VHF")[value_index]
            _element("text", attributes, item, frequency)


def _ils_frequency_row(
    root: ET.Element,
    airport: AirportData,
    approaches: tuple[IlsApproach, ...],
) -> None:
    station = airport.station
    if station is None:
        return
    group = _element(
        "g",
        {"id": "topographic-ils", "data-layout": "4x1"},
        root,
    )
    cell_width = COMMUNICATIONS_WIDTH / 4.0
    y = COMMUNICATIONS_TOP + COMMUNICATION_CELL_HEIGHT
    for index in range(4):
        x = COMMUNICATIONS_LEFT + index * cell_width
        approach = approaches[index] if index < len(approaches) else None
        item_attributes = {"class": "ils-box"}
        if approach is not None:
            item_attributes.update(
                {
                    "data-runway": approach.designator,
                    "data-frequency": _ils_frequency(
                        approach.frequency_hundredths_mhz
                    ),
                }
            )
        item = _element("g", item_attributes, group)
        _element(
            "rect",
            {
                "class": "frequency-cell ils-cell",
                "x": _number(x),
                "y": _number(y),
                "width": _number(cell_width),
                "height": _number(ILS_CELL_HEIGHT),
                "fill": "#fff",
                "stroke": "#182026",
                "stroke-width": "1.5",
            },
            item,
        )
        if approach is None:
            continue
        _element(
            "text",
            {
                "class": "frequency-service ils-service",
                "x": _number(x + cell_width / 2.0),
                "y": _number(y + COMMUNICATION_LABEL_OFFSET),
                "font-size": "27",
                "font-weight": "400",
                "text-anchor": "middle",
            },
            item,
            f"ILS {approach.designator}",
        )
        _element(
            "text",
            {
                "class": "frequency-value ils-value",
                "x": _number(x + cell_width / 2.0),
                "y": _number(y + ILS_VALUE_OFFSET),
                "font-size": "27",
                "font-weight": "700",
                "text-anchor": "middle",
            },
            item,
            _ils_frequency(approach.frequency_hundredths_mhz),
        )


def _frequency(value: int) -> str:
    if value <= 0:
        return "—"
    formatted = f"{value / 1000.0:.3f}".rstrip("0").rstrip(".")
    return formatted if "." in formatted else f"{formatted}.0"


def _ils_frequency(value: int) -> str:
    return f"{value / 100.0:.2f}"


def _airport_ils_approaches(
    airport: AirportData,
    runways: tuple[RunwayGeometry, ...],
) -> tuple[IlsApproach, ...]:
    if airport.station is None:
        return ()
    return build_ils_approaches(
        airport.layout,
        airport.station.runway_ils_frequencies,
        airport.magnetic_variation_degrees,
        airport.atc,
        runways=runways,
    )


def _elevation_shading(
    parent: ET.Element,
    window: ElevationWindow,
    interval_ft: int,
    land_cover: LandCoverWindow | None,
) -> None:
    _element(
        "image",
        {
            "id": "elevation-shading",
            "x": _number(MAP_LEFT),
            "y": _number(MAP_TOP),
            "width": _number(MAP_SIZE),
            "height": _number(MAP_SIZE),
            "preserveAspectRatio": "none",
            "data-band-interval-ft": str(interval_ft),
            "data-water-source": (
                "none" if land_cover is None else "authored-land-cover"
            ),
            "href": _elevation_png_data_url(window, interval_ft, land_cover),
        },
        parent,
    )


def _elevation_png_data_url(
    window: ElevationWindow,
    interval_ft: int,
    land_cover: LandCoverWindow | None = None,
) -> str:
    compressor = zlib.compressobj(level=ELEVATION_PNG_COMPRESSION_LEVEL)
    compressed_rows: list[bytes] = []
    for row_index, row in enumerate(window.rows):
        cover_row = None if land_cover is None else land_cover.rows[row_index]
        palette_indices = bytes(
            0
            if (
                cover_row is not None
                and cover_row[column] in land_cover.water_type_ids
            )
            else min(
                1 + max(0, elevation) // interval_ft,
                len(ELEVATION_PALETTE) - 1,
            )
            for column, elevation in enumerate(row)
        )
        payload = compressor.compress(b"\x00" + palette_indices)
        if payload:
            compressed_rows.append(payload)
    compressed_rows.append(compressor.flush())
    palette = bytes(component for color in ELEVATION_PALETTE for component in color)
    png = b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(
                b"IHDR",
                struct.pack(
                    ">IIBBBBB",
                    window.column_count,
                    window.row_count,
                    8,
                    3,
                    0,
                    0,
                    0,
                ),
            ),
            _png_chunk(b"PLTE", palette),
            _png_chunk(b"IDAT", b"".join(compressed_rows)),
            _png_chunk(b"IEND", b""),
        )
    )
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(kind)
    checksum = zlib.crc32(payload, checksum)
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", checksum & 0xFFFFFFFF)
    )


def _runways(
    parent: ET.Element,
    airport: AirportData,
    window: ElevationWindow,
    runways: tuple[RunwayGeometry, ...],
) -> None:
    group = _element("g", {"id": "airport-runways"}, parent)
    for runway in runways:
        start = _layout_to_grid(airport, window, runway.start)
        end = _layout_to_grid(airport, window, runway.end)
        start_screen = _screen_point(window, start)
        end_screen = _screen_point(window, end)
        axis_x = end_screen[0] - start_screen[0]
        axis_y = end_screen[1] - start_screen[1]
        screen_length = max(1.0, math.hypot(axis_x, axis_y))
        direction_x = axis_x / screen_length
        direction_y = axis_y / screen_length
        normal_x = -direction_y
        normal_y = direction_x
        center_x = (start_screen[0] + end_screen[0]) / 2.0
        center_y = (start_screen[1] + end_screen[1]) / 2.0
        half_length = TOPOGRAPHIC_RUNWAY_LENGTH / 2.0
        half_width = TOPOGRAPHIC_RUNWAY_WIDTH / 2.0
        marker_start_x = center_x - direction_x * half_length
        marker_start_y = center_y - direction_y * half_length
        marker_end_x = center_x + direction_x * half_length
        marker_end_y = center_y + direction_y * half_length
        corners = (
            (
                marker_start_x + normal_x * half_width,
                marker_start_y + normal_y * half_width,
            ),
            (
                marker_end_x + normal_x * half_width,
                marker_end_y + normal_y * half_width,
            ),
            (
                marker_end_x - normal_x * half_width,
                marker_end_y - normal_y * half_width,
            ),
            (
                marker_start_x - normal_x * half_width,
                marker_start_y - normal_y * half_width,
            ),
        )
        path = "M" + " L".join(
            f"{_number(x)},{_number(y)}" for x, y in corners
        ) + " Z"
        runway_group = _element(
            "g",
            {
                "class": "topographic-runway",
                "data-runway-number": str(runway.runway_number),
                "data-marker-length": _number(TOPOGRAPHIC_RUNWAY_LENGTH),
                "data-marker-width": _number(TOPOGRAPHIC_RUNWAY_WIDTH),
            },
            group,
        )
        _element(
            "path",
            {"class": "runway-marker", "d": path},
            runway_group,
        )


def _airport_range_ring(
    parent: ET.Element,
    airport: AirportData,
    window: ElevationWindow,
) -> tuple[ET.Element, float, float, float]:
    radius_nm = 15.0
    center_grid = window.grid_position(
        airport.placement.position_x,
        airport.placement.position_y,
    )
    center_x, center_y = _screen_point(window, center_grid)
    east_grid = window.grid_position(
        airport.placement.position_x,
        airport.placement.position_y
        + radius_nm * FEET_PER_NAUTICAL_MILE,
    )
    east_x, _ = _screen_point(window, east_grid)
    radius = abs(east_x - center_x)
    group = _element(
        "g",
        {
            "id": "airport-range-ring",
            "data-radius-nm": _number(radius_nm),
        },
        parent,
    )
    circle_attributes = {
        "cx": _number(center_x),
        "cy": _number(center_y),
        "r": _number(radius),
    }
    _element(
        "circle",
        {
            "class": "range-ring",
            "fill": "none",
            "stroke": "#182026",
            "stroke-width": "1.4",
            **circle_attributes,
        },
        group,
    )
    return group, center_x, center_y, radius


def _airport_range_ring_label(
    group: ET.Element,
    center_x: float,
    center_y: float,
    radius: float,
    occupied: tuple[tuple[float, float, float, float], ...],
) -> None:
    label_width = 58.0
    label_height = 22.0
    selected: tuple[float, float, int] | None = None
    for bearing in (180, 135, 225, 90, 270, 45, 315, 0):
        angle = math.radians(bearing)
        label_x = center_x + math.sin(angle) * (radius + 18.0)
        label_y = center_y - math.cos(angle) * (radius + 18.0)
        box = (
            label_x - label_width / 2.0,
            label_y - label_height / 2.0,
            label_x + label_width / 2.0,
            label_y + label_height / 2.0,
        )
        if (
            box[0] < MAP_LEFT + 4.0
            or box[1] < MAP_TOP + 4.0
            or box[2] > MAP_RIGHT - 4.0
            or box[3] > MAP_BOTTOM - 4.0
            or any(_boxes_overlap(box, other) for other in occupied)
        ):
            continue
        selected = label_x, label_y, bearing
        break
    if selected is None:
        selected = center_x, center_y + radius + 18.0, 180
    label_x, label_y, bearing = selected
    _element(
        "text",
        {
            "class": "range-ring-label",
            "x": _number(label_x),
            "y": _number(label_y),
            "fill": "#182026",
            "font-size": "16.8",
            "font-weight": "700",
            "letter-spacing": "0.7",
            "text-anchor": "middle",
            "dominant-baseline": "central",
            "data-placement-bearing": str(bearing),
        },
        group,
        "15 NM",
    )


def _ils_feathers(
    parent: ET.Element,
    airport: AirportData,
    window: ElevationWindow,
    approaches: tuple[IlsApproach, ...],
) -> None:
    group = _element("g", {"id": "ils-feathers"}, parent)
    length = ILS_FEATHER_LENGTH_NM * FEET_PER_NAUTICAL_MILE
    half_width = ILS_FEATHER_HALF_WIDTH_NM * FEET_PER_NAUTICAL_MILE

    def screen(point: Point) -> tuple[float, float]:
        return _screen_point(window, _layout_to_grid(airport, window, point))

    def path_between(*points: Point) -> str:
        coordinates = (screen(point) for point in points)
        return "M" + " L".join(
            f"{_number(x)},{_number(y)}" for x, y in coordinates
        )

    for approach in approaches:
        angle = math.radians(approach.course_true)
        inbound_x = math.sin(angle)
        inbound_y = math.cos(angle)
        normal_x = -inbound_y
        normal_y = inbound_x
        threshold = approach.threshold
        outer_center = Point(
            threshold.x - inbound_x * length,
            threshold.y - inbound_y * length,
        )
        outer_left = Point(
            outer_center.x + normal_x * half_width,
            outer_center.y + normal_y * half_width,
        )
        outer_right = Point(
            outer_center.x - normal_x * half_width,
            outer_center.y - normal_y * half_width,
        )
        notch = Point(
            outer_center.x + inbound_x * half_width * 1.1,
            outer_center.y + inbound_y * half_width * 1.1,
        )
        feather = _element(
            "g",
            {
                "class": "ils-feather",
                "data-runway": approach.designator,
                "data-frequency": _ils_frequency(
                    approach.frequency_hundredths_mhz
                ),
                "data-course-true": _number(approach.course_true),
                "data-course-magnetic": _number(approach.course_magnetic),
            },
            group,
        )
        outline = path_between(threshold, outer_left, notch, outer_right) + " Z"
        stippled_half = path_between(threshold, outer_left, notch) + " Z"
        seam_length = length - half_width * 1.1
        heading_distance = seam_length * ILS_FEATHER_HEADING_POSITION
        heading_gap_half = (
            ILS_FEATHER_HEADING_GAP_NM * FEET_PER_NAUTICAL_MILE / 2.0
        )
        heading_point = Point(
            threshold.x - inbound_x * heading_distance,
            threshold.y - inbound_y * heading_distance,
        )
        gap_inner = Point(
            threshold.x - inbound_x * (heading_distance - heading_gap_half),
            threshold.y - inbound_y * (heading_distance - heading_gap_half),
        )
        gap_outer = Point(
            threshold.x - inbound_x * (heading_distance + heading_gap_half),
            threshold.y - inbound_y * (heading_distance + heading_gap_half),
        )
        _element(
            "path",
            {
                "class": "ils-feather-underlay",
                "d": outline,
                "fill": "#fff",
                "fill-opacity": "0.4",
                "stroke": "none",
            },
            feather,
        )
        _element(
            "path",
            {
                "class": "ils-feather-stipple",
                "d": stippled_half,
                "fill": "url(#ils-feather-stipple)",
                "stroke": "none",
            },
            feather,
        )
        _element(
            "path",
            {
                "class": "ils-feather-outline",
                "d": outline,
                "fill": "none",
                "stroke": "#182026",
                "stroke-width": "1.4",
                "stroke-linejoin": "round",
            },
            feather,
        )
        _element(
            "path",
            {
                "class": "ils-feather-centerline",
                "d": (
                    path_between(threshold, gap_inner)
                    + " "
                    + path_between(gap_outer, notch)
                ),
                "fill": "none",
                "stroke": "#182026",
                "stroke-width": "2",
                "stroke-linecap": "round",
            },
            feather,
        )
        heading_x, heading_y = screen(heading_point)
        notch_x, notch_y = screen(notch)
        threshold_x, threshold_y = screen(threshold)
        text_angle = math.degrees(
            math.atan2(notch_y - threshold_y, notch_x - threshold_x)
        )
        if text_angle > 90.0:
            text_angle -= 180.0
        elif text_angle <= -90.0:
            text_angle += 180.0
        magnetic_heading = int(
            math.floor(approach.course_magnetic + 0.5)
        ) % 360
        _element(
            "text",
            {
                "class": "ils-feather-heading",
                "x": _number(heading_x),
                "y": _number(heading_y),
                "transform": (
                    f"rotate({_number(text_angle)} "
                    f"{_number(heading_x)} {_number(heading_y)})"
                ),
            },
            feather,
            f"{magnetic_heading:03d}°",
        )


def _terrain_peaks(
    parent: ET.Element,
    window: ElevationWindow,
    peaks: tuple[TerrainPeak, ...],
) -> tuple[tuple[float, float, float, float], ...]:
    group = _element(
        "g",
        {
            "id": "terrain-peaks",
            "data-grid-size": str(DEFAULT_PEAK_GRID_SIZE),
            "data-local-maximum-radius-nm": _number(
                DEFAULT_PEAK_LOCAL_MAXIMUM_RADIUS_NM
            ),
            "data-minimum-relief-ft": str(DEFAULT_PEAK_MINIMUM_RELIEF_FT),
            "data-minimum-separation-nm": _number(
                DEFAULT_PEAK_MINIMUM_SEPARATION_NM
            ),
            "data-maximum-peaks": str(DEFAULT_MAXIMUM_PEAKS),
        },
        parent,
    )
    positions = tuple(
        (peak, *_screen_point(window, (peak.row, peak.column)))
        for peak in peaks
    )
    occupied: list[tuple[float, float, float, float]] = [
        (x - 7.0, y - 7.0, x + 7.0, y + 7.0)
        for _, x, y in positions
    ]
    label_placements = (
        (11.0, 0.0, "start", "right"),
        (-11.0, 0.0, "end", "left"),
        (0.0, -13.0, "middle", "above"),
        (0.0, 14.0, "middle", "below"),
        (10.0, -11.0, "start", "upper-right"),
        (-10.0, -11.0, "end", "upper-left"),
    )
    for peak, x, y in positions:
        label = str(peak.elevation_ft)
        label_width = max(26.4, len(label) * 9.84)
        label_height = 19.2
        selected: tuple[float, float, str, str] | None = None
        selected_box: tuple[float, float, float, float] | None = None
        for offset_x, offset_y, anchor, placement in label_placements:
            label_x = x + offset_x
            label_y = y + offset_y
            if anchor == "start":
                left = label_x
            elif anchor == "end":
                left = label_x - label_width
            else:
                left = label_x - label_width / 2
            box = (
                left - 2,
                label_y - label_height / 2 - 2,
                left + label_width + 2,
                label_y + label_height / 2 + 2,
            )
            if (
                box[0] < MAP_LEFT + 4
                or box[1] < MAP_TOP + 4
                or box[2] > MAP_LEFT + MAP_SIZE - 4
                or box[3] > MAP_BOTTOM - 4
                or any(_boxes_overlap(box, other) for other in occupied)
            ):
                continue
            selected = (label_x, label_y, anchor, placement)
            selected_box = box
            break
        if selected is None or selected_box is None:
            offset_x, offset_y, anchor, placement = label_placements[0]
            label_x = min(
                MAP_LEFT + MAP_SIZE - label_width - 6,
                max(MAP_LEFT + 6, x + offset_x),
            )
            label_y = min(
                MAP_BOTTOM - label_height / 2 - 6,
                max(MAP_TOP + label_height / 2 + 6, y + offset_y),
            )
            selected = (label_x, label_y, "start", placement)
            selected_box = (
                label_x - 2,
                label_y - label_height / 2 - 2,
                label_x + label_width + 2,
                label_y + label_height / 2 + 2,
            )
        occupied.append(selected_box)
        label_x, label_y, anchor, placement = selected
        peak_group = _element(
            "g",
            {
                "class": "terrain-peak",
                "data-grid-row": str(peak.row),
                "data-grid-column": str(peak.column),
                "data-elevation-ft": str(peak.elevation_ft),
                "data-local-relief-ft": str(peak.local_relief_ft),
            },
            group,
        )
        _element(
            "path",
            {
                "class": "peak-marker",
                "d": (
                    f"M{_number(x)},{_number(y - 6)} "
                    f"L{_number(x + 5.5)},{_number(y + 4.5)} "
                    f"L{_number(x - 5.5)},{_number(y + 4.5)} Z"
                ),
            },
            peak_group,
        )
        _element(
            "text",
            {
                "class": "peak-label",
                "x": _number(label_x),
                "y": _number(label_y),
                "text-anchor": anchor,
                "data-placement": placement,
            },
            peak_group,
            label,
        )
    return tuple(occupied)


def _boxes_overlap(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    return not (
        left[2] <= right[0]
        or right[2] <= left[0]
        or left[3] <= right[1]
        or right[3] <= left[1]
    )


def _topographic_navigation_aids(
    parent: ET.Element,
    airport: AirportData,
    window: ElevationWindow,
) -> tuple[tuple[float, float, float, float], ...]:
    visible: list[tuple[NavigationObjectiveData, float, float]] = []
    for aid in airport.navigation_objectives:
        if aid.kind not in NAVIGATION_SYMBOL_FILENAMES:
            continue
        grid_point = window.grid_position(aid.position_x, aid.position_y)
        if not (
            0 <= grid_point[0] <= window.row_count - 1
            and 0 <= grid_point[1] <= window.column_count - 1
        ):
            continue
        x, y = _screen_point(window, grid_point)
        visible.append((aid, x, y))

    group = _element(
        "g",
        {"id": "topographic-navigation-aids"},
        parent,
    )
    half_symbol = NAVAID_SYMBOL_SIZE / 2.0
    occupied = [
        (
            x - half_symbol,
            y - half_symbol,
            x + half_symbol,
            y + half_symbol,
        )
        for _, x, y in visible
    ]
    placements = (
        (0.0, half_symbol + 11.0, "middle", "below"),
        (half_symbol + 8.0, 0.0, "start", "right"),
        (0.0, -half_symbol - 11.0, "middle", "above"),
        (-half_symbol - 8.0, 0.0, "end", "left"),
    )
    for aid, x, y in visible:
        label = _navigation_aid_label(aid)
        label_width = max(28.8, len(label) * 9.84)
        label_height = 19.2
        selected: tuple[float, float, str, str] | None = None
        selected_box: tuple[float, float, float, float] | None = None
        for offset_x, offset_y, anchor, placement in placements:
            label_x = x + offset_x
            label_y = y + offset_y
            if anchor == "start":
                left = label_x
            elif anchor == "end":
                left = label_x - label_width
            else:
                left = label_x - label_width / 2.0
            box = (
                left - 2,
                label_y - label_height / 2.0 - 2,
                left + label_width + 2,
                label_y + label_height / 2.0 + 2,
            )
            if (
                box[0] < MAP_LEFT + 4
                or box[1] < MAP_TOP + 4
                or box[2] > MAP_LEFT + MAP_SIZE - 4
                or box[3] > MAP_BOTTOM - 4
                or any(_boxes_overlap(box, other) for other in occupied)
            ):
                continue
            selected = (label_x, label_y, anchor, placement)
            selected_box = box
            break
        if selected is None or selected_box is None:
            label_x = min(
                MAP_LEFT + MAP_SIZE - label_width - 6,
                max(MAP_LEFT + 6, x + half_symbol + 8.0),
            )
            label_y = min(
                MAP_BOTTOM - label_height / 2.0 - 6,
                max(MAP_TOP + label_height / 2.0 + 6, y),
            )
            selected = (label_x, label_y, "start", "right-clamped")
            selected_box = (
                label_x - 2,
                label_y - label_height / 2.0 - 2,
                label_x + label_width + 2,
                label_y + label_height / 2.0 + 2,
            )
        occupied.append(selected_box)
        label_x, label_y, anchor, placement = selected
        item = _element(
            "g",
            {
                "class": "topographic-navigation-aid",
                "data-campaign-id": str(aid.campaign_id),
                "data-kind": aid.kind,
                "data-identifier": aid.identifier or "",
            },
            group,
        )
        title = aid.name
        if aid.channel > 0 and aid.range_nm > 0:
            title += (
                f", channel {aid.channel}{aid.band}, "
                f"range {aid.range_nm} nautical miles"
            )
        _element("title", parent=item, text=title)
        item.append(
            navigation_symbol_element(
                aid.kind,
                x=x - half_symbol,
                y=y - half_symbol,
                width=NAVAID_SYMBOL_SIZE,
                height=NAVAID_SYMBOL_SIZE,
                attributes={
                    "class": "topographic-navaid-symbol",
                    "data-symbol": aid.kind,
                },
            )
        )
        _element(
            "text",
            {
                "class": "topographic-navaid-label",
                "x": _number(label_x),
                "y": _number(label_y),
                "text-anchor": anchor,
                "data-placement": placement,
            },
            item,
            label,
        )
    return tuple(occupied)


def _navigation_aid_label(aid: NavigationObjectiveData) -> str:
    parts = [] if aid.identifier is None else [aid.identifier]
    if aid.channel > 0 and aid.range_nm > 0:
        parts.append(f"{aid.channel}{aid.band}")
    return " ".join(parts) if parts else aid.kind


def _airport_label(
    parent: ET.Element,
    airport: AirportData,
    window: ElevationWindow,
    occupied: tuple[tuple[float, float, float, float], ...] = (),
) -> None:
    row, column = window.grid_position(
        airport.placement.position_x,
        airport.placement.position_y,
    )
    x, y = _screen_point(window, (row, column))
    label = airport.display_code
    label_width = max(48.0, len(label) * 12.1)
    label_height = 24.0
    placements = (
        (20.0, -14.0, "start", "upper-right"),
        (20.0, 18.0, "start", "lower-right"),
        (-20.0, -14.0, "end", "upper-left"),
        (-20.0, 18.0, "end", "lower-left"),
        (0.0, -27.0, "middle", "above"),
        (0.0, 29.0, "middle", "below"),
    )
    selected: tuple[float, float, str, str] | None = None
    for offset_x, offset_y, anchor, placement in placements:
        label_x = x + offset_x
        label_y = y + offset_y
        if anchor == "start":
            left = label_x
        elif anchor == "end":
            left = label_x - label_width
        else:
            left = label_x - label_width / 2.0
        box = (
            left - 2.0,
            label_y - label_height / 2.0 - 2.0,
            left + label_width + 2.0,
            label_y + label_height / 2.0 + 2.0,
        )
        if (
            box[0] < MAP_LEFT + 4.0
            or box[1] < MAP_TOP + 4.0
            or box[2] > MAP_RIGHT - 4.0
            or box[3] > MAP_BOTTOM - 4.0
            or any(_boxes_overlap(box, other) for other in occupied)
        ):
            continue
        selected = (label_x, label_y, anchor, placement)
        break
    if selected is None:
        selected = (x + 20.0, y + 18.0, "start", "fallback")
    label_x, label_y, anchor, placement = selected
    group = _element(
        "g",
        {"id": "airport-label", "data-placement": placement},
        parent,
    )
    _element(
        "text",
        {
            "class": "airport-label",
            "x": _number(label_x),
            "y": _number(label_y),
            "text-anchor": anchor,
        },
        group,
        label,
    )


def _scale_bar(parent: ET.Element, size_nm: float) -> None:
    scale_nm = 10.0 if size_nm >= 20 else size_nm / 2.0
    length = MAP_SIZE * scale_nm / size_nm
    x = MAP_LEFT + 24
    y = MAP_BOTTOM - 94
    group = _element("g", {"id": "scale-bar"}, parent)
    _element(
        "line",
        {
            "class": "scale-halo",
            "x1": _number(x),
            "y1": _number(y),
            "x2": _number(x + length),
            "y2": _number(y),
        },
        group,
    )
    _element(
        "path",
        {
            "class": "scale-bar",
            "d": (
                f"M{_number(x)},{_number(y - 8)} V{_number(y + 8)} "
                f"M{_number(x)},{_number(y)} H{_number(x + length)} "
                f"M{_number(x + length)},{_number(y - 8)} V{_number(y + 8)}"
            ),
        },
        group,
    )
    _element(
        "text",
        {
            "class": "scale-label",
            "x": _number(x + length / 2),
            "y": _number(y - 12),
        },
        group,
        f"{scale_nm:g} NM",
    )


def _elevation_scale(
    parent: ET.Element,
    size_nm: float,
    interval_ft: int,
) -> None:
    group = _element("g", {"id": "elevation-scale"}, parent)
    scale_nm = 10.0 if size_nm >= 20 else size_nm / 2.0
    bar_left = MAP_LEFT + 24
    bar_width = MAP_SIZE * scale_nm / size_nm
    bar_top = MAP_BOTTOM - 45
    land_colors = ELEVATION_PALETTE[1:]
    swatch_width = bar_width / len(land_colors)
    _element(
        "text",
        {
            "class": "legend-title",
            "x": _number(bar_left),
            "y": _number(bar_top - 10),
        },
        group,
        "ELEVATION (FT)",
    )
    for index, color in enumerate(land_colors):
        _element(
            "rect",
            {
                "class": "elevation-band-swatch",
                "data-min-elevation-ft": str(index * interval_ft),
                "x": _number(bar_left + index * swatch_width),
                "y": _number(bar_top),
                "width": _number(swatch_width),
                "height": _number(ELEVATION_SCALE_SWATCH_HEIGHT),
                "fill": f"rgb({color[0]},{color[1]},{color[2]})",
                "stroke": "#fff",
                "stroke-width": "0.8",
            },
            group,
        )
    _element(
        "rect",
        {
            "x": _number(bar_left),
            "y": _number(bar_top),
            "width": _number(bar_width),
            "height": _number(ELEVATION_SCALE_SWATCH_HEIGHT),
            "fill": "none",
            "stroke": "#59656a",
            "stroke-width": "1",
        },
        group,
    )
    label_y = bar_top + ELEVATION_SCALE_SWATCH_HEIGHT + 17
    for index in range(0, len(land_colors), 2):
        label = f"{index * interval_ft}"
        if index == len(land_colors) - 1:
            label += "+"
        _element(
            "text",
            {
                "class": "legend-label",
                "x": _number(bar_left + (index + 0.5) * swatch_width),
                "y": _number(label_y),
            },
            group,
            label,
        )


def _north_indicator(parent: ET.Element, airport: AirportData) -> None:
    base_x = MAP_LEFT + 54
    base_y = MAP_TOP + 126
    true_direction = (0.0, -1.0)
    group = _element(
        "g",
        {
            "id": "north-indicator",
            "data-true-direction-x": "0",
            "data-true-direction-y": "-1",
        },
        parent,
    )
    true_path, true_tip = _north_arrow_path(
        base_x,
        base_y,
        true_direction,
        76.0,
    )
    _element("path", {"class": "north-arrow-halo", "d": true_path}, group)
    _element("path", {"class": "north-true", "d": true_path}, group)
    _element(
        "text",
        {
            "class": "north-label",
            "x": _number(true_tip[0] + 13),
            "y": _number(true_tip[1]),
        },
        group,
        "N",
    )
    if airport.magnetic_variation_degrees is not None:
        angle = math.radians(airport.magnetic_variation_degrees)
        magnetic_direction = (
            -true_direction[1] * math.sin(angle),
            true_direction[1] * math.cos(angle),
        )
        magnetic_path, _ = _north_arrow_path(
            base_x,
            base_y,
            magnetic_direction,
            58.0,
        )
        group.set("data-magnetic-direction-x", _number(magnetic_direction[0]))
        group.set("data-magnetic-direction-y", _number(magnetic_direction[1]))
        _element(
            "path",
            {"class": "north-arrow-halo", "d": magnetic_path},
            group,
        )
        _element(
            "path",
            {"class": "north-magnetic", "d": magnetic_path},
            group,
        )
        direction = "E" if airport.magnetic_variation_degrees >= 0 else "W"
        variation = abs(airport.magnetic_variation_degrees)
        _element(
            "text",
            {
                "class": "small map-small",
                "x": _number(base_x),
                "y": _number(base_y + 22),
                "text-anchor": "middle",
            },
            group,
            f"VAR {variation:.1f}° {direction}",
        )


def _msa_indicator(
    parent: ET.Element,
    airport: AirportData,
    window: ElevationWindow,
    runways: tuple[RunwayGeometry, ...],
) -> None:
    if not runways:
        return
    primary = max(
        runways,
        key=lambda runway: (
            _distance_points(runway.start, runway.end),
            -runway.runway_number,
        ),
    )
    sectors = build_msa_sectors(
        window,
        primary.heading_true,
        airport.magnetic_variation_degrees,
    )
    group = _element(
        "g",
        {
            "id": "msa-indicator",
            "data-radius-nm": "25",
            "data-anchor-runway-number": str(primary.runway_number),
            "data-anchor-heading-true": _number(primary.heading_true),
            "data-sector-count": str(len(sectors)),
        },
        parent,
    )
    _element(
        "circle",
        {
            "class": "msa-background",
            "cx": _number(MSA_INDICATOR_CENTER_X),
            "cy": _number(MSA_INDICATOR_CENTER_Y),
            "r": _number(MSA_INDICATOR_RADIUS),
        },
        group,
    )
    _element(
        "text",
        {
            "class": "msa-title",
            "x": _number(MSA_INDICATOR_CENTER_X),
            "y": _number(MSA_INDICATOR_CENTER_Y - MSA_INDICATOR_RADIUS - 8.0),
        },
        group,
        f"MSA {airport.display_code} 25 NM",
    )
    if len(sectors) > 1:
        for sector in sectors:
            inner_x, inner_y = _msa_point(sector.start_bearing, 9.0)
            outer_x, outer_y = _msa_point(
                sector.start_bearing,
                MSA_INDICATOR_RADIUS,
            )
            _element(
                "line",
                {
                    "class": "msa-boundary",
                    "x1": _number(inner_x),
                    "y1": _number(inner_y),
                    "x2": _number(outer_x),
                    "y2": _number(outer_y),
                },
                group,
            )
            bearing_x, bearing_y = _msa_point(
                sector.start_bearing,
                MSA_INDICATOR_RADIUS * 0.82,
            )
            _element(
                "text",
                {
                    "class": "msa-bearing",
                    "x": _number(bearing_x),
                    "y": _number(bearing_y),
                },
                group,
                f"{int(math.floor(sector.start_bearing + 0.5)) % 360:03d}°",
            )
    for sector in sectors:
        _msa_sector(group, sector)
    _element(
        "circle",
        {
            "class": "msa-center",
            "cx": _number(MSA_INDICATOR_CENTER_X),
            "cy": _number(MSA_INDICATOR_CENTER_Y),
            "r": "4",
        },
        group,
    )


def _msa_sector(parent: ET.Element, sector: MsaSector) -> None:
    label_x, label_y = _msa_point(
        sector.center_bearing,
        MSA_INDICATOR_RADIUS * 0.55,
    )
    label = str(sector.minimum_altitude_ft)
    box_width = max(48.0, len(label) * 11.5 + 16.0)
    box_height = 29.0
    group = _element(
        "g",
        {
            "class": "msa-sector",
            "data-start-bearing": _number(sector.start_bearing),
            "data-end-bearing": _number(sector.end_bearing),
            "data-span-degrees": _number(sector.span_degrees),
            "data-maximum-elevation-ft": str(sector.maximum_elevation_ft),
            "data-minimum-altitude-ft": str(sector.minimum_altitude_ft),
        },
        parent,
    )
    _element(
        "rect",
        {
            "class": "msa-altitude-box",
            "x": _number(label_x - box_width / 2.0),
            "y": _number(label_y - box_height / 2.0),
            "width": _number(box_width),
            "height": _number(box_height),
        },
        group,
    )
    _element(
        "text",
        {
            "class": "msa-altitude",
            "x": _number(label_x),
            "y": _number(label_y),
        },
        group,
        label,
    )


def _msa_point(bearing_degrees: float, distance: float) -> tuple[float, float]:
    angle = math.radians(bearing_degrees)
    return (
        MSA_INDICATOR_CENTER_X + math.sin(angle) * distance,
        MSA_INDICATOR_CENTER_Y - math.cos(angle) * distance,
    )


def _distance_points(left: Point, right: Point) -> float:
    return math.hypot(right.x - left.x, right.y - left.y)


def _north_arrow_path(
    base_x: float,
    base_y: float,
    direction: tuple[float, float],
    length: float,
) -> tuple[str, tuple[float, float]]:
    direction_x, direction_y = direction
    perpendicular_x = -direction_y
    perpendicular_y = direction_x
    tip_x = base_x + direction_x * length
    tip_y = base_y + direction_y * length
    arrow_base_x = tip_x - direction_x * 9.0
    arrow_base_y = tip_y - direction_y * 9.0
    path = (
        f"M{_number(base_x)},{_number(base_y)} "
        f"L{_number(tip_x)},{_number(tip_y)} "
        f"M{_number(arrow_base_x + perpendicular_x * 5.5)},"
        f"{_number(arrow_base_y + perpendicular_y * 5.5)} "
        f"L{_number(tip_x)},{_number(tip_y)} "
        f"L{_number(arrow_base_x - perpendicular_x * 5.5)},"
        f"{_number(arrow_base_y - perpendicular_y * 5.5)}"
    )
    return path, (tip_x, tip_y)


def _airport_minimap(
    root: ET.Element,
    airport: AirportData,
    taxiway_surfaces: tuple[TaxiwaySurface, ...],
    runways: tuple[RunwayGeometry, ...],
    routes: tuple[tuple[Point, Point], ...],
) -> None:
    towers = tuple(
        Point(feature.offset_x, feature.offset_y)
        for feature in airport.layout.features
        if feature.name == "Control Tower"
    )
    local_points = [
        point
        for runway in runways
        for point in _minimap_runway_corners(runway)
    ]
    local_points.extend(towers)
    if taxiway_surfaces:
        local_points.extend(
            point
            for surface in taxiway_surfaces
            for triangle in surface.triangles
            for point in triangle
        )
    else:
        local_points.extend(point for edge in routes for point in edge)
    if not local_points:
        return

    oriented = tuple(
        _minimap_north_up(point, airport.placement.heading)
        for point in local_points
    )
    minimum_x = min(point[0] for point in oriented)
    maximum_x = max(point[0] for point in oriented)
    minimum_y = min(point[1] for point in oriented)
    maximum_y = max(point[1] for point in oriented)
    span_x = max(1.0, maximum_x - minimum_x)
    span_y = max(1.0, maximum_y - minimum_y)
    inner_width = MINIMAP_WIDTH - MINIMAP_PADDING * 2
    inner_height = MINIMAP_HEIGHT - MINIMAP_PADDING * 2
    scale = min(inner_width / span_x, inner_height / span_y)
    content_width = span_x * scale
    content_height = span_y * scale
    offset_x = MINIMAP_LEFT + (MINIMAP_WIDTH - content_width) / 2.0
    offset_y = MINIMAP_TOP + (MINIMAP_HEIGHT - content_height) / 2.0

    def transform(point: Point) -> tuple[float, float]:
        x, y = _minimap_north_up(point, airport.placement.heading)
        return (
            offset_x + (x - minimum_x) * scale,
            offset_y + (y - minimum_y) * scale,
        )

    group = _element(
        "g",
        {
            "id": "airport-minimap",
            "data-orientation": "true-north-up",
            "data-taxiway-surface-count": str(len(taxiway_surfaces)),
        },
        root,
    )
    _element(
        "rect",
        {
            "class": "minimap-background",
            "x": _number(MINIMAP_LEFT),
            "y": _number(MINIMAP_TOP),
            "width": _number(MINIMAP_WIDTH),
            "height": _number(MINIMAP_HEIGHT),
            "fill": "#fff",
        },
        group,
    )
    taxiways = _element("g", {"id": "minimap-taxiways"}, group)
    if taxiway_surfaces:
        for surface in taxiway_surfaces:
            commands = []
            for triangle in surface.triangles:
                first, second, third = (transform(point) for point in triangle)
                commands.append(
                    f"M{_pair(first)} L{_pair(second)} L{_pair(third)} Z"
                )
            _element(
                "path",
                {
                    "class": "minimap-taxiway",
                    "data-feature-index": str(surface.feature_index),
                    "data-graphics-id": str(surface.graphics_id),
                    "d": " ".join(commands),
                    "fill": "#d5d9d8",
                    "stroke": "none",
                    "fill-rule": "nonzero",
                },
                taxiways,
            )
    else:
        for start, end in routes:
            start_screen = transform(start)
            end_screen = transform(end)
            _element(
                "line",
                {
                    "class": "minimap-taxi-route",
                    "x1": _number(start_screen[0]),
                    "y1": _number(start_screen[1]),
                    "x2": _number(end_screen[0]),
                    "y2": _number(end_screen[1]),
                    "fill": "none",
                    "stroke": "#a9b0b0",
                    "stroke-width": "2",
                    "stroke-linecap": "round",
                },
                taxiways,
            )

    runway_group = _element("g", {"id": "minimap-runways"}, group)
    for runway in runways:
        corners = tuple(transform(point) for point in _minimap_runway_corners(runway))
        _element(
            "path",
            {
                "class": "minimap-runway",
                "data-runway-number": str(runway.runway_number),
                "fill": "#20272b",
                "stroke": "#0d1113",
                "stroke-width": "0.8",
                "d": "M" + " L".join(_pair(point) for point in corners) + " Z",
            },
            runway_group,
        )

    elevation_box_x = (
        MINIMAP_LEFT + MINIMAP_WIDTH - MINIMAP_ELEVATION_BOX_WIDTH
    )
    placed_runway_label_boxes: list[tuple[float, float, float, float]] = [
        (
            elevation_box_x,
            MINIMAP_TOP,
            elevation_box_x + MINIMAP_ELEVATION_BOX_WIDTH,
            MINIMAP_TOP + MINIMAP_ELEVATION_BOX_HEIGHT,
        )
    ]
    runway_label_group = _element(
        "g",
        {"id": "minimap-runway-labels"},
        group,
    )
    for runway in runways:
        for runway_end in runway.ends:
            threshold_x, threshold_y = transform(runway_end.point)
            opposite_point = (
                runway.end
                if runway_end.point == runway.start
                else runway.start
            )
            opposite_x, opposite_y = transform(opposite_point)
            inward_x = opposite_x - threshold_x
            inward_y = opposite_y - threshold_y
            inward_length = max(1.0, math.hypot(inward_x, inward_y))
            inward_x /= inward_length
            inward_y /= inward_length
            rotation = math.degrees(math.atan2(inward_x, -inward_y))
            text_width = (
                len(runway_end.designator)
                * MINIMAP_RUNWAY_LABEL_FONT_SIZE
                * 0.56
            )
            label_offset = MINIMAP_RUNWAY_LABEL_OFFSET
            while True:
                label_x = threshold_x - inward_x * label_offset
                label_y = threshold_y - inward_y * label_offset
                radians = math.radians(rotation)
                half_width = text_width / 2.0
                half_height = MINIMAP_RUNWAY_LABEL_FONT_SIZE / 2.0
                extent_x = (
                    abs(math.cos(radians)) * half_width
                    + abs(math.sin(radians)) * half_height
                )
                extent_y = (
                    abs(math.sin(radians)) * half_width
                    + abs(math.cos(radians)) * half_height
                )
                label_box = (
                    label_x - extent_x,
                    label_y - extent_y,
                    label_x + extent_x,
                    label_y + extent_y,
                )
                if not any(
                    _boxes_overlap(label_box, placed_box)
                    for placed_box in placed_runway_label_boxes
                ):
                    break
                label_offset += MINIMAP_RUNWAY_LABEL_COLLISION_STEP
            placed_runway_label_boxes.append(label_box)
            _element(
                "text",
                {
                    "class": "minimap-runway-label",
                    "data-runway": runway_end.designator,
                    "x": _number(label_x),
                    "y": _number(label_y),
                    "textLength": _number(text_width),
                    "lengthAdjust": "spacingAndGlyphs",
                    "transform": (
                        f"rotate({_number(rotation)} "
                        f"{_number(label_x)} {_number(label_y)})"
                    ),
                },
                runway_label_group,
                runway_end.designator,
            )

    tower_group = _element("g", {"id": "minimap-towers"}, group)
    for tower in towers:
        x, y = transform(tower)
        _element(
            "rect",
            {
                "class": "minimap-tower-box",
                "x": _number(x - 8),
                "y": _number(y - 8),
                "width": "16",
                "height": "16",
                "fill": "#fff",
                "stroke": "#182026",
                "stroke-width": "1.5",
            },
            tower_group,
        )
        _element(
            "text",
            {
                "class": "minimap-tower-text",
                "x": _number(x),
                "y": _number(y),
                "font-size": "12",
                "font-weight": "700",
                "text-anchor": "middle",
                "dominant-baseline": "central",
            },
            tower_group,
            "T",
        )
    elevation_box = _element(
        "g",
        {"id": "minimap-elevation-box"},
        group,
    )
    _element(
        "rect",
        {
            "class": "minimap-elevation-background",
            "x": _number(elevation_box_x),
            "y": _number(MINIMAP_TOP),
            "width": _number(MINIMAP_ELEVATION_BOX_WIDTH),
            "height": _number(MINIMAP_ELEVATION_BOX_HEIGHT),
            "fill": "#fff",
            "stroke": "#182026",
            "stroke-width": "1.5",
        },
        elevation_box,
    )
    elevation = "—" if airport.elevation_ft is None else str(airport.elevation_ft)
    _element(
        "text",
        {
            "class": "minimap-elevation-label",
            "x": _number(
                elevation_box_x + MINIMAP_ELEVATION_BOX_WIDTH / 2.0
            ),
            "y": _number(
                MINIMAP_TOP + MINIMAP_ELEVATION_BOX_HEIGHT / 2.0
            ),
            "font-size": "14.4",
            "font-weight": "700",
            "text-anchor": "middle",
            "dominant-baseline": "central",
        },
        elevation_box,
        f"ELEV {elevation} FT",
    )
    _element(
        "rect",
        {
            "class": "minimap-frame",
            "x": _number(MINIMAP_LEFT),
            "y": _number(MINIMAP_TOP),
            "width": _number(MINIMAP_WIDTH),
            "height": _number(MINIMAP_HEIGHT),
            "fill": "none",
            "stroke": "#182026",
            "stroke-width": "1.5",
        },
        group,
    )


def _minimap_north_up(point: Point, heading_degrees: float) -> tuple[float, float]:
    angle = math.radians(heading_degrees)
    north = point.x * math.sin(angle) + point.y * math.cos(angle)
    east = point.x * math.cos(angle) - point.y * math.sin(angle)
    return east, -north


def _minimap_runway_corners(
    runway: RunwayGeometry,
) -> tuple[Point, Point, Point, Point]:
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


def _pair(point: tuple[float, float]) -> str:
    return f"{_number(point[0])},{_number(point[1])}"


def _footer(root: ET.Element, airport: AirportData) -> None:
    _element(
        "text",
        {"class": "warning", "x": str(CONTENT_INSET), "y": str(BOTTOM_ANNOTATION_Y)},
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


def _layout_to_grid(
    airport: AirportData,
    window: ElevationWindow,
    point: Point,
) -> tuple[float, float]:
    angle = math.radians(airport.placement.heading)
    delta_x = point.x * math.sin(angle) + point.y * math.cos(angle)
    delta_y = point.x * math.cos(angle) - point.y * math.sin(angle)
    return window.grid_position(
        airport.placement.position_x + delta_x,
        airport.placement.position_y + delta_y,
    )


def _screen_point(window: ElevationWindow, point: tuple[float, float]) -> tuple[float, float]:
    return (
        MAP_LEFT + point[1] / (window.column_count - 1) * MAP_SIZE,
        MAP_TOP + point[0] / (window.row_count - 1) * MAP_SIZE,
    )


def _style(root: ET.Element) -> None:
    _element(
        "style",
        parent=root,
        text=f"""
text {{ font-family: Arial, Helvetica, sans-serif; fill: #182026; }}
.page {{ fill: #fff; }}
.border {{ fill: none; stroke: #182026; stroke-width: 2; }}
.map-frame {{ fill: none; stroke: #182026; stroke-width: 2; }}
.title {{ font-size: 39px; font-weight: 700; letter-spacing: -0.7px; }}
.subtitle {{ font-size: 16px; font-weight: 700; letter-spacing: 2.2px; fill: #52606a; }}
.frequency-cell {{ fill: #fff; stroke: #182026; stroke-width: 1.5; }}
.frequency-cell-tower {{ stroke-width: 2.5; }}
.frequency-service {{ font-size: 27px; font-weight: 400; text-anchor: middle; }}
.frequency-value {{ font-size: 27px; font-weight: 700; text-anchor: middle; }}
.range-ring {{ fill: none; stroke: #182026; stroke-width: 1.4; }}
.range-ring-label {{ font-size: 16.8px; font-weight: 700; letter-spacing: 0.7px; }}
.ils-feather-underlay {{ fill: #fff; fill-opacity: 0.4; stroke: none; }}
.ils-feather-stipple {{ fill: url(#ils-feather-stipple); stroke: none; }}
.ils-feather-outline {{ fill: none; stroke: #182026; stroke-width: 1.4; stroke-linejoin: round; }}
.ils-feather-centerline {{ fill: none; stroke: #182026; stroke-width: 2; stroke-linecap: round; }}
.ils-feather-heading {{ font-size: 16.8px; font-weight: 700; text-anchor: middle; dominant-baseline: central; paint-order: stroke; stroke: #fff; stroke-width: 4; stroke-linejoin: round; }}
.runway-marker {{ fill: #20272b; stroke: none; }}
.peak-marker {{ fill: #182026; stroke: #fff; stroke-width: 2; stroke-linejoin: round; paint-order: stroke; }}
.peak-label {{ font-size: 16.8px; font-weight: 700; dominant-baseline: central; paint-order: stroke; stroke: #fff; stroke-width: 4; stroke-linejoin: round; }}
.topographic-navaid-label {{ font-size: 16.8px; font-weight: 700; dominant-baseline: central; paint-order: stroke; stroke: #fff; stroke-width: 4; stroke-linejoin: round; }}
.airport-label {{ font-size: 21.6px; font-weight: 700; paint-order: stroke; stroke: #fff; stroke-width: 5; stroke-linejoin: round; }}
.scale-halo {{ stroke: #fff; stroke-width: 7; }}
.scale-bar {{ fill: none; stroke: #182026; stroke-width: 2.5; }}
.scale-label {{ font-size: 18px; font-weight: 700; text-anchor: middle; paint-order: stroke; stroke:#fff; stroke-width: 5; }}
.legend-title {{ font-size: 14.4px; font-weight: 700; letter-spacing: 1px; paint-order: stroke; stroke: #fff; stroke-width: 4; }}
.legend-label {{ font-size: 12px; font-weight: 700; text-anchor: middle; paint-order: stroke; stroke: #fff; stroke-width: 3; }}
.north-arrow-halo {{ fill: none; stroke: #fff; stroke-width: 6; stroke-linecap: round; stroke-linejoin: round; }}
.north-true {{ fill: none; stroke: #c7252d; stroke-width: 2.3; stroke-linecap: round; stroke-linejoin: round; }}
.north-magnetic {{ fill: none; stroke: #59656a; stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }}
.north-label {{ fill: #c7252d; font-size: 16.8px; font-weight: 700; text-anchor: middle; dominant-baseline: central; paint-order: stroke; stroke: #fff; stroke-width: 4; }}
.msa-background {{ fill: #fff; fill-opacity: 0.82; stroke: #182026; stroke-width: 1.8; }}
.msa-title {{ font-size: 15.6px; font-weight: 700; text-anchor: middle; paint-order: stroke; stroke: #fff; stroke-width: 4; }}
.msa-boundary {{ stroke: #182026; stroke-width: 1.4; }}
.msa-bearing {{ font-size: 12px; font-weight: 700; text-anchor: middle; dominant-baseline: central; paint-order: stroke; stroke: #fff; stroke-width: 4; }}
.msa-altitude-box {{ fill: #fff; fill-opacity: 0.9; stroke: #182026; stroke-width: 1.2; }}
.msa-altitude {{ font-size: 17px; font-weight: 700; text-anchor: middle; dominant-baseline: central; }}
.msa-center {{ fill: #182026; stroke: #fff; stroke-width: 1.5; }}
.small {{ fill: #59656a; font-size: 12px; paint-order: stroke; stroke: #fff; stroke-width: 4; }}
.small.map-small {{ font-size: 14.4px; }}
.minimap-background {{ fill: #fff; }}
.minimap-frame {{ fill: none; stroke: #182026; stroke-width: 1.5; }}
.minimap-taxiway {{ fill: #d5d9d8; stroke: none; fill-rule: nonzero; }}
.minimap-taxi-route {{ fill: none; stroke: #a9b0b0; stroke-width: 2; stroke-linecap: round; }}
.minimap-runway {{ fill: #20272b; stroke: #0d1113; stroke-width: 0.8; }}
.minimap-runway-label {{ fill: #182026; font-size: 16.8px; font-weight: 700; text-anchor: middle; dominant-baseline: central; }}
.minimap-tower-box {{ fill: #fff; stroke: #182026; stroke-width: 1.5; }}
.minimap-tower-text {{ font-size: 12px; font-weight: 700; text-anchor: middle; dominant-baseline: central; }}
.minimap-elevation-label {{ font-size: 14.4px; font-weight: 700; text-anchor: middle; dominant-baseline: central; }}
.warning {{ fill: #a2452d; font-size: 24px; font-weight: 700; letter-spacing: 1px; }}
""",
    )


def _element(
    tag: str,
    attributes: dict[str, str] | None = None,
    parent: ET.Element | None = None,
    text: str | None = None,
) -> ET.Element:
    element = ET.Element(f"{{{SVG_NS}}}{tag}", attributes or {})
    if text is not None:
        element.text = text
    if parent is not None:
        parent.append(element)
    return element


def _number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")

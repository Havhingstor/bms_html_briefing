"""Bulk terrain sampling and contour extraction for topographic charts."""

from __future__ import annotations

from array import array
from dataclasses import dataclass
import math
import sys

from .source import TerrainHeightSource, TerrainLandCoverSource


FEET_PER_NAUTICAL_MILE = 6076.115485564304
HEIGHT_SAMPLE_BYTES = 2
LAND_COVER_SAMPLE_BYTES = 1
DEFAULT_AREA_SIZE_NM = 50.0
DEFAULT_ELEVATION_INTERVAL_FT = 500
DEFAULT_CONTOUR_INTERVAL_FT = DEFAULT_ELEVATION_INTERVAL_FT
DEFAULT_SAMPLE_STRIDE = 2
DEFAULT_PEAK_GRID_SIZE = 8
DEFAULT_PEAK_LOCAL_MAXIMUM_RADIUS_NM = 1.0
DEFAULT_PEAK_RELIEF_RADIUS_NM = 2.0
DEFAULT_PEAK_MINIMUM_RELIEF_FT = 500
DEFAULT_PEAK_MINIMUM_SEPARATION_NM = 3.0
DEFAULT_MAXIMUM_PEAKS = 12
DEFAULT_MSA_RADIUS_NM = 25.0
DEFAULT_MSA_CLEARANCE_FT = 1000
DEFAULT_MSA_MERGE_THRESHOLD_FT = 200
DEFAULT_OUTSIDE_ELEVATION_FT = 0

GridPoint = tuple[float, float]
GridSegment = tuple[GridPoint, GridPoint]


class TopographyError(RuntimeError):
    """Raised when terrain data cannot produce the requested chart."""


@dataclass(frozen=True)
class ElevationWindow:
    """A north-up rectangular subset of a BMS terrain height grid."""

    rows: tuple[array, ...]
    north_ft: float
    west_ft: float
    sample_spacing_ft: float
    source_stride: int
    requested_size_nm: float

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def column_count(self) -> int:
        return 0 if not self.rows else len(self.rows[0])

    @property
    def south_ft(self) -> float:
        return self.north_ft - (self.row_count - 1) * self.sample_spacing_ft

    @property
    def east_ft(self) -> float:
        return self.west_ft + (self.column_count - 1) * self.sample_spacing_ft

    @property
    def minimum_ft(self) -> int:
        return min(min(row) for row in self.rows)

    @property
    def maximum_ft(self) -> int:
        return max(max(row) for row in self.rows)

    def grid_position(
        self,
        position_x_ft: float,
        position_y_ft: float,
    ) -> GridPoint:
        """Return fractional row/column coordinates for a campaign position."""

        return (
            (self.north_ft - position_x_ft) / self.sample_spacing_ft,
            (position_y_ft - self.west_ft) / self.sample_spacing_ft,
        )


@dataclass(frozen=True)
class LandCoverWindow:
    """A north-up rectangular subset of authored uint8 type IDs."""

    rows: tuple[bytes, ...]
    water_type_ids: frozenset[int]
    source_stride: int
    requested_size_nm: float

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def column_count(self) -> int:
        return 0 if not self.rows else len(self.rows[0])

    @property
    def water_pixel_count(self) -> int:
        return sum(
            value in self.water_type_ids
            for row in self.rows
            for value in row
        )

    def is_water(self, row: int, column: int) -> bool:
        return self.rows[row][column] in self.water_type_ids


@dataclass(frozen=True)
class ContourLine:
    level_ft: int
    points: tuple[GridPoint, ...]


@dataclass(frozen=True)
class TerrainPeak:
    row: int
    column: int
    elevation_ft: int
    local_relief_ft: int


@dataclass(frozen=True)
class MsaSector:
    """One magnetic-bearing sector in an airport MSA indication."""

    start_bearing: float
    span_degrees: float
    maximum_elevation_ft: int
    minimum_altitude_ft: int

    @property
    def end_bearing(self) -> float:
        return (self.start_bearing + self.span_degrees) % 360.0

    @property
    def center_bearing(self) -> float:
        return (self.start_bearing + self.span_degrees / 2.0) % 360.0


@dataclass(frozen=True)
class _WindowBounds:
    start_row: int
    end_row: int
    start_column: int
    end_column: int
    sample_spacing_ft: float


@dataclass(frozen=True)
class _SampledAxis:
    count: int
    valid_start_index: int
    valid_end_index: int
    source_start: int
    source_end: int


def read_elevation_window(
    source: TerrainHeightSource,
    center_x_ft: float,
    center_y_ft: float,
    *,
    size_nm: float = DEFAULT_AREA_SIZE_NM,
    sample_stride: int = DEFAULT_SAMPLE_STRIDE,
) -> ElevationWindow:
    """Read a centered window, padding beyond the grid with flat 0 ft terrain."""

    bounds = _window_bounds(
        source.extent_ft,
        source.map_size_pixels,
        center_x_ft,
        center_y_ft,
        size_nm,
        sample_stride,
        "terrain height map",
    )
    size = source.map_size_pixels

    expected_size = size * size * HEIGHT_SAMPLE_BYTES
    try:
        actual_size = source.source_path.stat().st_size
    except OSError as error:
        raise TopographyError(
            f"cannot inspect terrain height map {source.source_path}: {error}"
        ) from error
    if actual_size < expected_size:
        raise TopographyError(
            f"{source.source_path}: expected at least {expected_size} bytes, "
            f"found {actual_size}"
        )

    columns = _sampled_axis(
        bounds.start_column,
        bounds.end_column,
        sample_stride,
        size,
    )
    native_columns = columns.source_end - columns.source_start + 1
    sampled_rows: list[array] = []
    try:
        with source.source_path.open("rb") as height_map:
            for source_row in range(
                bounds.start_row,
                bounds.end_row + 1,
                sample_stride,
            ):
                row = array("h", [DEFAULT_OUTSIDE_ELEVATION_FT]) * columns.count
                if not 0 <= source_row < size:
                    sampled_rows.append(row)
                    continue
                offset = (
                    source_row * size + columns.source_start
                ) * HEIGHT_SAMPLE_BYTES
                height_map.seek(offset)
                payload = height_map.read(native_columns * HEIGHT_SAMPLE_BYTES)
                if len(payload) != native_columns * HEIGHT_SAMPLE_BYTES:
                    raise TopographyError(
                        f"{source.source_path}: truncated terrain row {source_row}"
                    )
                values = array("h")
                values.frombytes(payload)
                if sys.byteorder != "little":
                    values.byteswap()
                row[
                    columns.valid_start_index : columns.valid_end_index + 1
                ] = values[::sample_stride]
                sampled_rows.append(row)
    except OSError as error:
        raise TopographyError(
            f"cannot read terrain height map {source.source_path}: {error}"
        ) from error

    north_ft = source.extent_ft * (size - bounds.start_row - 0.5) / size
    west_ft = source.extent_ft * (bounds.start_column + 0.5) / size
    return ElevationWindow(
        rows=tuple(sampled_rows),
        north_ft=north_ft,
        west_ft=west_ft,
        sample_spacing_ft=bounds.sample_spacing_ft,
        source_stride=sample_stride,
        requested_size_nm=size_nm,
    )


def read_land_cover_window(
    source: TerrainLandCoverSource,
    center_x_ft: float,
    center_y_ft: float,
    *,
    size_nm: float = DEFAULT_AREA_SIZE_NM,
    sample_stride: int = DEFAULT_SAMPLE_STRIDE,
) -> LandCoverWindow:
    """Read land cover, padding beyond the grid with a non-water type ID."""

    bounds = _window_bounds(
        source.extent_ft,
        source.map_size_pixels,
        center_x_ft,
        center_y_ft,
        size_nm,
        sample_stride,
        "terrain land-cover map",
    )
    size = source.map_size_pixels
    expected_size = size * size * LAND_COVER_SAMPLE_BYTES
    try:
        actual_size = source.source_path.stat().st_size
    except OSError as error:
        raise TopographyError(
            f"cannot inspect terrain land-cover map {source.source_path}: {error}"
        ) from error
    if actual_size < expected_size:
        raise TopographyError(
            f"{source.source_path}: expected at least {expected_size} bytes, "
            f"found {actual_size}"
        )

    columns = _sampled_axis(
        bounds.start_column,
        bounds.end_column,
        sample_stride,
        size,
    )
    native_columns = columns.source_end - columns.source_start + 1
    outside_type_id = next(
        value for value in range(256) if value not in source.water_type_ids
    )
    sampled_rows: list[bytes] = []
    try:
        with source.source_path.open("rb") as land_cover_map:
            for source_row in range(
                bounds.start_row,
                bounds.end_row + 1,
                sample_stride,
            ):
                row = bytearray((outside_type_id,)) * columns.count
                if not 0 <= source_row < size:
                    sampled_rows.append(bytes(row))
                    continue
                offset = source_row * size + columns.source_start
                land_cover_map.seek(offset)
                payload = land_cover_map.read(native_columns)
                if len(payload) != native_columns:
                    raise TopographyError(
                        f"{source.source_path}: truncated land-cover row {source_row}"
                    )
                row[
                    columns.valid_start_index : columns.valid_end_index + 1
                ] = payload[::sample_stride]
                sampled_rows.append(bytes(row))
    except OSError as error:
        raise TopographyError(
            f"cannot read terrain land-cover map {source.source_path}: {error}"
        ) from error
    return LandCoverWindow(
        rows=tuple(sampled_rows),
        water_type_ids=frozenset(source.water_type_ids),
        source_stride=sample_stride,
        requested_size_nm=size_nm,
    )


def _window_bounds(
    extent_ft: float,
    map_size_pixels: int,
    center_x_ft: float,
    center_y_ft: float,
    size_nm: float,
    sample_stride: int,
    source_label: str,
) -> _WindowBounds:
    if size_nm <= 0 or not math.isfinite(size_nm):
        raise ValueError("size_nm must be a positive finite number")
    if sample_stride < 1:
        raise ValueError("sample_stride must be at least one")
    if map_size_pixels < 2:
        raise TopographyError(f"{source_label} must contain at least 2 × 2 samples")
    if extent_ft <= 0 or not math.isfinite(extent_ft):
        raise TopographyError("terrain extent must be a positive finite number")

    _bounded_position(center_x_ft, extent_ft, "center_x_ft")
    _bounded_position(center_y_ft, extent_ft, "center_y_ft")
    sample_spacing_ft = extent_ft / map_size_pixels * sample_stride
    interval_count = max(
        2,
        round(size_nm * FEET_PER_NAUTICAL_MILE / sample_spacing_ft),
    )
    # An even interval count keeps the airport on the middle sampled row/column.
    if interval_count % 2:
        interval_count += 1

    center_row = map_size_pixels - 1 - int(
        center_x_ft / extent_ft * map_size_pixels
    )
    center_column = int(center_y_ft / extent_ft * map_size_pixels)
    half_span = interval_count // 2 * sample_stride
    return _WindowBounds(
        start_row=center_row - half_span,
        end_row=center_row + half_span,
        start_column=center_column - half_span,
        end_column=center_column + half_span,
        sample_spacing_ft=sample_spacing_ft,
    )


def _sampled_axis(
    start: int,
    end: int,
    stride: int,
    source_size: int,
) -> _SampledAxis:
    """Map one possibly padded sampled axis onto valid source indices."""

    count = (end - start) // stride + 1
    valid_start_index = max(0, (-start + stride - 1) // stride)
    valid_end_index = min(count - 1, (source_size - 1 - start) // stride)
    if valid_start_index > valid_end_index:
        raise TopographyError("terrain window does not intersect its source grid")
    return _SampledAxis(
        count=count,
        valid_start_index=valid_start_index,
        valid_end_index=valid_end_index,
        source_start=start + valid_start_index * stride,
        source_end=start + valid_end_index * stride,
    )


def build_contours(
    window: ElevationWindow,
    *,
    interval_ft: int = DEFAULT_CONTOUR_INTERVAL_FT,
    simplify_tolerance: float = 0.8,
) -> tuple[ContourLine, ...]:
    """Extract, stitch, and simplify constant-elevation lines."""

    if interval_ft <= 0:
        raise ValueError("interval_ft must be positive")
    if simplify_tolerance < 0 or not math.isfinite(simplify_tolerance):
        raise ValueError("simplify_tolerance must be a non-negative finite number")
    if window.row_count < 2 or window.column_count < 2:
        raise TopographyError("elevation window must contain at least 2 × 2 samples")
    if any(len(row) != window.column_count for row in window.rows):
        raise TopographyError("elevation window rows must have equal lengths")

    segments_by_level: dict[int, list[GridSegment]] = {}
    for row_index in range(window.row_count - 1):
        top = window.rows[row_index]
        bottom = window.rows[row_index + 1]
        for column_index in range(window.column_count - 1):
            values = (
                top[column_index],
                top[column_index + 1],
                bottom[column_index + 1],
                bottom[column_index],
            )
            minimum = min(values)
            maximum = max(values)
            level = (minimum // interval_ft + 1) * interval_ft
            while level <= maximum:
                segments = _cell_segments(row_index, column_index, values, level)
                if segments:
                    segments_by_level.setdefault(level, []).extend(segments)
                level += interval_ft

    lines: list[ContourLine] = []
    for level, segments in sorted(segments_by_level.items()):
        for points in _stitch_segments(segments):
            simplified = _simplify_path(points, simplify_tolerance)
            if len(simplified) >= 2:
                lines.append(ContourLine(level_ft=level, points=simplified))
    return tuple(lines)


def build_terrain_peaks(
    window: ElevationWindow,
    *,
    grid_size: int = DEFAULT_PEAK_GRID_SIZE,
    local_maximum_radius_nm: float = DEFAULT_PEAK_LOCAL_MAXIMUM_RADIUS_NM,
    relief_radius_nm: float = DEFAULT_PEAK_RELIEF_RADIUS_NM,
    minimum_relief_ft: int = DEFAULT_PEAK_MINIMUM_RELIEF_FT,
    minimum_separation_nm: float = DEFAULT_PEAK_MINIMUM_SEPARATION_NM,
    maximum_peaks: int = DEFAULT_MAXIMUM_PEAKS,
) -> tuple[TerrainPeak, ...]:
    """Select a sparse set of locally dominant grid-cell high points."""

    if grid_size < 1:
        raise ValueError("grid_size must be positive")
    if maximum_peaks < 0:
        raise ValueError("maximum_peaks must be non-negative")
    if minimum_relief_ft < 0:
        raise ValueError("minimum_relief_ft must be non-negative")
    for value, label in (
        (local_maximum_radius_nm, "local_maximum_radius_nm"),
        (relief_radius_nm, "relief_radius_nm"),
        (minimum_separation_nm, "minimum_separation_nm"),
    ):
        if value <= 0 or not math.isfinite(value):
            raise ValueError(f"{label} must be a positive finite number")
    if window.row_count < grid_size or window.column_count < grid_size:
        raise TopographyError("elevation window is smaller than the peak grid")
    if any(len(row) != window.column_count for row in window.rows):
        raise TopographyError("elevation window rows must have equal lengths")
    if maximum_peaks == 0:
        return ()

    local_radius = max(
        1,
        round(
            local_maximum_radius_nm
            * FEET_PER_NAUTICAL_MILE
            / window.sample_spacing_ft
        ),
    )
    relief_radius = max(
        local_radius,
        round(
            relief_radius_nm
            * FEET_PER_NAUTICAL_MILE
            / window.sample_spacing_ft
        ),
    )
    candidates: list[TerrainPeak] = []
    for grid_row in range(grid_size):
        row_start = grid_row * window.row_count // grid_size
        row_end = (grid_row + 1) * window.row_count // grid_size
        for grid_column in range(grid_size):
            column_start = grid_column * window.column_count // grid_size
            column_end = (
                (grid_column + 1) * window.column_count // grid_size
            )
            elevation, row, column = _cell_high_point(
                window,
                row_start,
                row_end,
                column_start,
                column_end,
            )
            if (
                row < local_radius
                or column < local_radius
                or row >= window.row_count - local_radius
                or column >= window.column_count - local_radius
            ):
                continue
            if elevation < _circular_maximum(window, row, column, local_radius):
                continue
            nearby = _circular_values(window, row, column, relief_radius)
            nearby.sort()
            local_relief = elevation - nearby[len(nearby) // 4]
            if local_relief < minimum_relief_ft:
                continue
            candidates.append(
                TerrainPeak(
                    row=row,
                    column=column,
                    elevation_ft=elevation,
                    local_relief_ft=local_relief,
                )
            )

    minimum_separation = (
        minimum_separation_nm
        * FEET_PER_NAUTICAL_MILE
        / window.sample_spacing_ft
    )
    minimum_separation_squared = minimum_separation * minimum_separation
    selected: list[TerrainPeak] = []
    for candidate in sorted(
        candidates,
        key=lambda item: (
            -item.local_relief_ft,
            -item.elevation_ft,
            item.row,
            item.column,
        ),
    ):
        if any(
            (candidate.row - other.row) ** 2
            + (candidate.column - other.column) ** 2
            < minimum_separation_squared
            for other in selected
        ):
            continue
        selected.append(candidate)
        if len(selected) == maximum_peaks:
            break
    return tuple(selected)


def build_msa_sectors(
    window: ElevationWindow,
    runway_heading_true: float,
    magnetic_variation_degrees: float | None,
    *,
    radius_nm: float = DEFAULT_MSA_RADIUS_NM,
    clearance_ft: int = DEFAULT_MSA_CLEARANCE_FT,
    merge_threshold_ft: int = DEFAULT_MSA_MERGE_THRESHOLD_FT,
    maximum_sectors: int = 4,
) -> tuple[MsaSector, ...]:
    """Build runway-oriented MSA sectors from the local terrain grid.

    Eight 45-degree terrain bins are paired into four quadrants whose centers
    follow the dominant runway and its reciprocal. Adjacent quadrants with
    similar rounded safe altitudes are then merged conservatively.
    """

    if window.row_count < 2 or window.column_count < 2:
        raise TopographyError("elevation window must contain at least 2 × 2 samples")
    if any(len(row) != window.column_count for row in window.rows):
        raise TopographyError("elevation window rows must have equal lengths")
    if radius_nm <= 0 or not math.isfinite(radius_nm):
        raise ValueError("radius_nm must be a positive finite number")
    if not math.isfinite(runway_heading_true):
        raise ValueError("runway_heading_true must be finite")
    if clearance_ft < 0:
        raise ValueError("clearance_ft must be non-negative")
    if merge_threshold_ft < 0:
        raise ValueError("merge_threshold_ft must be non-negative")
    if not 1 <= maximum_sectors <= 4:
        raise ValueError("maximum_sectors must be in 1..4")

    center_row = (window.row_count - 1) / 2.0
    center_column = (window.column_count - 1) / 2.0
    radius_samples = radius_nm * FEET_PER_NAUTICAL_MILE / window.sample_spacing_ft
    available_radius = min(
        center_row,
        center_column,
        window.row_count - 1 - center_row,
        window.column_count - 1 - center_column,
    )
    if radius_samples > available_radius + 1e-6:
        raise TopographyError(
            f"{radius_nm:g} NM MSA radius exceeds the elevation window"
        )

    variation = magnetic_variation_degrees or 0.0
    runway_heading_magnetic = (runway_heading_true - variation) % 360.0
    first_boundary = runway_heading_magnetic - 45.0
    maxima: list[int | None] = [None] * 8
    radius_squared = radius_samples * radius_samples
    heading = math.radians(runway_heading_true)
    heading_cosine = math.cos(heading)
    heading_sine = math.sin(heading)
    center_bin = min(
        7,
        int(((45.0 - runway_heading_true) % 360.0) / 45.0),
    )
    for row_index, row in enumerate(window.rows):
        north = center_row - row_index
        for column_index, elevation in enumerate(row):
            east = column_index - center_column
            if north * north + east * east > radius_squared:
                continue
            if north == 0.0 and east == 0.0:
                bin_index = center_bin
            else:
                # Rotate into the runway frame, then classify the octant with
                # comparisons. Magnetic variation cancels from the relative
                # angle, avoiding atan2/degrees/modulo for every terrain cell.
                forward = north * heading_cosine + east * heading_sine
                right = east * heading_cosine - north * heading_sine
                if forward >= 0.0:
                    if right >= 0.0:
                        bin_index = (
                            3
                            if forward == 0.0
                            else (1 if right < forward else 2)
                        )
                    else:
                        bin_index = (
                            7
                            if forward == 0.0 or -right > forward
                            else 0
                        )
                elif right >= 0.0:
                    bin_index = (
                        5
                        if right == 0.0
                        else (3 if right > -forward else 4)
                    )
                else:
                    bin_index = 5 if -right < -forward else 6
            current = maxima[bin_index]
            if current is None or elevation > current:
                maxima[bin_index] = elevation
    if any(value is None for value in maxima):
        raise TopographyError("MSA terrain sampling left an empty angular sector")

    def rounded_altitude(maximum: int) -> int:
        unrounded = maximum + clearance_ft
        return ((unrounded + 99) // 100) * 100

    # Working values are (first 45-degree bin, bin count, maximum elevation).
    sectors: list[tuple[int, int, int]] = [
        (
            index * 2,
            2,
            max(
                int(maxima[index * 2]),
                int(maxima[index * 2 + 1]),
            ),
        )
        for index in range(4)
    ]
    while len(sectors) > 1:
        candidates: list[tuple[int, int]] = []
        for index, sector in enumerate(sectors):
            following = sectors[(index + 1) % len(sectors)]
            difference = abs(
                rounded_altitude(sector[2])
                - rounded_altitude(following[2])
            )
            candidates.append((difference, index))
        difference, index = min(candidates)
        if len(sectors) <= maximum_sectors and difference > merge_threshold_ft:
            break
        following_index = (index + 1) % len(sectors)
        first = sectors[index]
        following = sectors[following_index]
        merged = (first[0], first[1] + following[1], max(first[2], following[2]))
        if following_index == 0:
            sectors = [merged, *sectors[1:index]]
        else:
            sectors[index] = merged
            del sectors[following_index]

    return tuple(
        MsaSector(
            start_bearing=(first_boundary + first_bin * 45.0) % 360.0,
            span_degrees=bin_count * 45.0,
            maximum_elevation_ft=maximum,
            minimum_altitude_ft=rounded_altitude(maximum),
        )
        for first_bin, bin_count, maximum in sectors
    )


def _cell_high_point(
    window: ElevationWindow,
    row_start: int,
    row_end: int,
    column_start: int,
    column_end: int,
) -> tuple[int, int, int]:
    best: tuple[int, int, int] | None = None
    for row_index in range(row_start, row_end):
        row = window.rows[row_index]
        row_maximum = max(row[column_start:column_end])
        column_index = row.index(row_maximum, column_start, column_end)
        candidate = (row_maximum, -row_index, -column_index)
        if best is None or candidate > best:
            best = candidate
    if best is None:
        raise TopographyError("peak grid cell contains no elevation samples")
    return (best[0], -best[1], -best[2])


def _circular_maximum(
    window: ElevationWindow,
    row: int,
    column: int,
    radius: int,
) -> int:
    radius_squared = radius * radius
    return max(
        window.rows[candidate_row][candidate_column]
        for candidate_row in range(row - radius, row + radius + 1)
        for candidate_column in range(column - radius, column + radius + 1)
        if (candidate_row - row) ** 2 + (candidate_column - column) ** 2
        <= radius_squared
    )


def _circular_values(
    window: ElevationWindow,
    row: int,
    column: int,
    radius: int,
) -> list[int]:
    radius_squared = radius * radius
    return [
        window.rows[candidate_row][candidate_column]
        for candidate_row in range(
            max(0, row - radius),
            min(window.row_count, row + radius + 1),
        )
        for candidate_column in range(
            max(0, column - radius),
            min(window.column_count, column + radius + 1),
        )
        if (candidate_row - row) ** 2 + (candidate_column - column) ** 2
        <= radius_squared
    ]


def _cell_segments(
    row: int,
    column: int,
    values: tuple[int, int, int, int],
    level: int,
) -> tuple[GridSegment, ...]:
    code = sum(1 << index for index, value in enumerate(values) if value >= level)
    if code in (0, 15):
        return ()

    edge_pairs: tuple[tuple[int, int], ...]
    if code == 5:
        edge_pairs = ((0, 1), (2, 3)) if sum(values) / 4 >= level else ((3, 0), (1, 2))
    elif code == 10:
        edge_pairs = ((3, 0), (1, 2)) if sum(values) / 4 >= level else ((0, 1), (2, 3))
    else:
        edge_pairs = {
            1: ((3, 0),),
            2: ((0, 1),),
            3: ((3, 1),),
            4: ((1, 2),),
            6: ((0, 2),),
            7: ((3, 2),),
            8: ((2, 3),),
            9: ((0, 2),),
            11: ((1, 2),),
            12: ((3, 1),),
            13: ((0, 1),),
            14: ((3, 0),),
        }[code]
    points = tuple(
        _edge_point(row, column, values, level, edge)
        for pair in edge_pairs
        for edge in pair
    )
    return tuple((points[index], points[index + 1]) for index in range(0, len(points), 2))


def _edge_point(
    row: int,
    column: int,
    values: tuple[int, int, int, int],
    level: int,
    edge: int,
) -> GridPoint:
    top_left, top_right, bottom_right, bottom_left = values
    if edge == 0:
        fraction = _fraction(level, top_left, top_right)
        return (float(row), column + fraction)
    if edge == 1:
        fraction = _fraction(level, top_right, bottom_right)
        return (row + fraction, float(column + 1))
    if edge == 2:
        fraction = _fraction(level, bottom_left, bottom_right)
        return (float(row + 1), column + fraction)
    fraction = _fraction(level, top_left, bottom_left)
    return (row + fraction, float(column))


def _fraction(level: int, start: int, end: int) -> float:
    if start == end:
        return 0.5
    return (level - start) / (end - start)


def _stitch_segments(segments: list[GridSegment]) -> tuple[tuple[GridPoint, ...], ...]:
    endpoints = [(_point_key(start), _point_key(end)) for start, end in segments]
    adjacency: dict[GridPoint, list[int]] = {}
    for segment_index, (start, end) in enumerate(endpoints):
        if start == end:
            continue
        adjacency.setdefault(start, []).append(segment_index)
        adjacency.setdefault(end, []).append(segment_index)
    used = bytearray(len(segments))
    paths: list[tuple[GridPoint, ...]] = []

    def trace(start: GridPoint, first_segment: int) -> tuple[GridPoint, ...]:
        points = [start]
        current = start
        segment_index = first_segment
        while not used[segment_index]:
            used[segment_index] = 1
            first, second = endpoints[segment_index]
            current = second if current == first else first
            points.append(current)
            if current == start or len(adjacency[current]) != 2:
                break
            candidates = [item for item in adjacency[current] if not used[item]]
            if not candidates:
                break
            segment_index = candidates[0]
        return tuple(points)

    for point, incident in adjacency.items():
        if len(incident) == 2:
            continue
        for segment_index in incident:
            if not used[segment_index]:
                paths.append(trace(point, segment_index))
    for segment_index, (start, _) in enumerate(endpoints):
        if not used[segment_index] and start in adjacency:
            paths.append(trace(start, segment_index))
    return tuple(paths)


def _point_key(point: GridPoint) -> GridPoint:
    return (round(point[0], 7), round(point[1], 7))


def _simplify_path(
    points: tuple[GridPoint, ...],
    tolerance: float,
) -> tuple[GridPoint, ...]:
    if tolerance == 0 or len(points) <= 2:
        return points
    if points[0] != points[-1]:
        return _rdp(points, tolerance)

    ring = points[:-1]
    if len(ring) <= 3:
        return points
    first_index = max(
        range(1, len(ring)),
        key=lambda index: _squared_distance(ring[0], ring[index]),
    )
    second_index = max(
        range(len(ring)),
        key=lambda index: _squared_distance(ring[first_index], ring[index]),
    )
    start_index, end_index = sorted((first_index, second_index))
    first_arc = ring[start_index : end_index + 1]
    second_arc = ring[end_index:] + ring[: start_index + 1]
    simplified = _rdp(first_arc, tolerance)[:-1] + _rdp(second_arc, tolerance)
    return simplified + (simplified[0],)


def _rdp(points: tuple[GridPoint, ...], tolerance: float) -> tuple[GridPoint, ...]:
    keep = bytearray(len(points))
    keep[0] = keep[-1] = 1
    threshold = tolerance * tolerance
    stack = [(0, len(points) - 1)]
    while stack:
        start_index, end_index = stack.pop()
        start = points[start_index]
        end = points[end_index]
        farthest_index = -1
        farthest_distance = threshold
        for index in range(start_index + 1, end_index):
            distance = _point_segment_distance_squared(points[index], start, end)
            if distance > farthest_distance:
                farthest_distance = distance
                farthest_index = index
        if farthest_index >= 0:
            keep[farthest_index] = 1
            stack.append((start_index, farthest_index))
            stack.append((farthest_index, end_index))
    return tuple(point for index, point in enumerate(points) if keep[index])


def _point_segment_distance_squared(
    point: GridPoint,
    start: GridPoint,
    end: GridPoint,
) -> float:
    delta_row = end[0] - start[0]
    delta_column = end[1] - start[1]
    length_squared = delta_row * delta_row + delta_column * delta_column
    if length_squared == 0:
        return _squared_distance(point, start)
    fraction = max(
        0.0,
        min(
            1.0,
            (
                (point[0] - start[0]) * delta_row
                + (point[1] - start[1]) * delta_column
            )
            / length_squared,
        ),
    )
    projection = (
        start[0] + fraction * delta_row,
        start[1] + fraction * delta_column,
    )
    return _squared_distance(point, projection)


def _squared_distance(first: GridPoint, second: GridPoint) -> float:
    return (first[0] - second[0]) ** 2 + (first[1] - second[1]) ** 2


def _bounded_position(value: float, extent_ft: float, label: str) -> float:
    if not math.isfinite(value) or value < 0 or value >= extent_ft:
        raise TopographyError(f"{label} must be within [0, {extent_ft:g})")
    return value

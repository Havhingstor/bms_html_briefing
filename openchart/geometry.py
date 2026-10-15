"""Deterministic geometry derived from authored OCD feature and point data."""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
import math

from .source import (
    ObjectiveFeatureLayout,
    ObjectivePointDefinition,
    ObjectivePointHeader,
)
from .vendor.opencam.support import AtcAirbaseData, AtcRunwayEntry


NUMBERED_PARKING_TYPES = frozenset((11, 12))
SHELTER_PARKING_TYPE = 16
ROUTE_HEADER_TYPE = 1
RUNWAY_METADATA_HEADER_TYPE = 8
RUNWAY_EDGE_POINT_TYPE = 8
ARRESTING_SYSTEM_POINT_TYPE = 9
ROUTE_COVERAGE_TOLERANCE = 20.0
ROUTE_SNAP_TOLERANCE = 30.0
PARKING_MERGE_TOLERANCE = 10.0
TAXIWAY_LABEL_MERGE_TOLERANCE = 100.0
FEET_TO_METERS = 0.3048


@dataclass(frozen=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True)
class RunwayEnd:
    designator: str
    point: Point


@dataclass(frozen=True)
class RunwayGeometry:
    """Runway points and axis heading in the unrotated objective layout."""

    runway_number: int
    heading_true: float
    start: Point
    end: Point
    width: float
    feature_indices: tuple[int, ...]
    ends: tuple[RunwayEnd, ...]
    length_meters: float | None = None
    width_meters: float | None = None


@dataclass(frozen=True)
class IlsApproach:
    """World bearings and a threshold point in the objective's local layout."""

    designator: str
    runway_number: int
    course_true: float
    course_magnetic: float
    threshold: Point
    frequency_hundredths_mhz: int


@dataclass(frozen=True)
class ArrestingSystem:
    runway_number: int
    start: Point
    end: Point


@dataclass(frozen=True)
class ParkingPosition:
    point: Point
    type_: int
    heading: float | None
    group: int | None


@dataclass(frozen=True)
class NumberedParkingPosition:
    number: int
    point: Point
    type_: int
    heading: float | None
    group: int | None


@dataclass(frozen=True)
class ParkingChart:
    designator: str
    runway_number: int
    heading_true: float
    positions: tuple[NumberedParkingPosition, ...]


@dataclass(frozen=True)
class TaxiwayLabel:
    point: Point
    letter: str


@dataclass(frozen=True)
class _HeaderRouteEdges:
    spines: tuple[tuple[Point, Point], ...]
    parking_spurs: tuple[tuple[Point, Point], ...]


def build_runways(
    layout: ObjectiveFeatureLayout,
    atc: AtcAirbaseData | None = None,
    magnetic_variation_degrees: float | None = None,
    *,
    objective_heading_degrees: float = 0.0,
) -> tuple[RunwayGeometry, ...]:
    """Build local runway geometry with BMS ATC labels for its world placement."""

    headers_by_runway: dict[int, list[ObjectivePointHeader]] = {}
    for header in layout.point_headers:
        if header.type_ == 1 and header.feature_dependency_indices:
            headers_by_runway.setdefault(header.runway_number, []).append(header)

    runways: list[RunwayGeometry] = []
    for runway_number in sorted(headers_by_runway):
        headers = headers_by_runway[runway_number]
        feature_indices = tuple(
            sorted(
                {
                    index
                    for header in headers
                    for index in header.feature_dependency_indices
                }
            )
        )
        if not feature_indices:
            continue
        heading = headers[0].data % 180.0
        outline_geometry = _runway_outline_geometry(layout, runway_number)
        if outline_geometry is not None:
            outline_start, outline_end, _ = outline_geometry
            outline_heading = _course_between(outline_start, outline_end) % 180.0
            if any(
                _axis_angular_distance(header.data, outline_heading) > 1.0
                for header in headers
            ):
                heading = outline_heading
        axis_x = math.sin(math.radians(heading))
        axis_y = math.cos(math.radians(heading))
        if outline_geometry is None:
            features = [layout.features[index] for index in feature_indices]
            normal_x = -axis_y
            normal_y = axis_x
            projections = sorted(
                feature.offset_x * axis_x + feature.offset_y * axis_y
                for feature in features
            )
            perpendiculars = [
                feature.offset_x * normal_x + feature.offset_y * normal_y
                for feature in features
            ]
            center_perpendicular = sum(perpendiculars) / len(perpendiculars)
            positive_gaps = [
                right - left
                for left, right in zip(projections, projections[1:])
                if right - left > 1.0
            ]
            extension = _median(positive_gaps) / 2.0 if positive_gaps else 500.0
            low = projections[0] - extension
            high = projections[-1] + extension
            start = Point(
                axis_x * low + normal_x * center_perpendicular,
                axis_y * low + normal_y * center_perpendicular,
            )
            end = Point(
                axis_x * high + normal_x * center_perpendicular,
                axis_y * high + normal_y * center_perpendicular,
            )
            width = 180.0
            physical_dimensions = None
        else:
            start, end, width = outline_geometry
            physical_dimensions = (
                _distance(start, end) * FEET_TO_METERS,
                width * FEET_TO_METERS,
            )

        runways.append(
            RunwayGeometry(
                runway_number=runway_number,
                heading_true=heading,
                start=start,
                end=end,
                width=width,
                feature_indices=feature_indices,
                ends=(),
                length_meters=(
                    None
                    if physical_dimensions is None
                    else physical_dimensions[0]
                ),
                width_meters=(
                    None
                    if physical_dimensions is None
                    else physical_dimensions[1]
                ),
            )
        )
    return _apply_runway_labels(
        layout, tuple(runways), atc, magnetic_variation_degrees,
        objective_heading_degrees,
    )


def build_ils_approaches(
    layout: ObjectiveFeatureLayout,
    runway_ils_frequencies: tuple[int, int, int, int],
    magnetic_variation_degrees: float | None,
    atc: AtcAirbaseData | None = None,
    *,
    runways: tuple[RunwayGeometry, ...] | None = None,
    objective_heading_degrees: float = 0.0,
) -> tuple[IlsApproach, ...]:
    """Associate Stations+Ils frequency slots with reciprocal approach headers.

    Each physical runway's database RunwayNumber owns two consecutive slots.
    Within its pair of headers, preserve authored order: the first slot serves
    the second header's approach, and the second slot serves the first header's
    approach. Keep zero-frequency slots in place so later assignments cannot
    shift; omit unserved approaches only after associating the slots.
    """

    runways_by_number = {
        runway.runway_number: runway
        for runway in (
            build_runways(
                layout,
                atc,
                magnetic_variation_degrees,
                objective_heading_degrees=objective_heading_degrees,
            )
            if runways is None
            else runways
        )
    }
    atc_by_header = _match_atc_to_route_headers(
        layout, atc, objective_heading_degrees,
    )
    headers_by_runway: dict[int, list[ObjectivePointHeader]] = {}
    for header in layout.point_headers:
        if header.type_ == ROUTE_HEADER_TYPE:
            headers_by_runway.setdefault(header.runway_number, []).append(header)
    variation = magnetic_variation_degrees or 0.0
    approaches: list[IlsApproach] = []
    for runway_number in range(len(runway_ils_frequencies) // 2):
        headers = headers_by_runway.get(runway_number, [])
        # An incomplete or ambiguous pair cannot resolve an opposite header.
        if len(headers) != 2:
            continue
        runway = runways_by_number.get(runway_number)
        if runway is None or not runway.ends:
            continue
        for offset, header in enumerate(reversed(headers)):
            frequency = runway_ils_frequencies[2 * runway_number + offset]
            if frequency <= 0:
                continue
            atc_entry = atc_by_header.get(header.index)
            local_course = _resolved_header_course(
                runway, header, atc_entry, objective_heading_degrees,
            )
            course_true = (local_course + objective_heading_degrees) % 360.0
            course_magnetic = (course_true - variation) % 360.0
            runway_end = _runway_end_for_course(runway, local_course)
            approaches.append(
                IlsApproach(
                    designator=runway_end.designator,
                    runway_number=header.runway_number,
                    course_true=course_true,
                    course_magnetic=course_magnetic,
                    threshold=runway_end.point,
                    frequency_hundredths_mhz=frequency,
                ),
            )
    return tuple(approaches)


def _runway_outline_geometry(
    layout: ObjectiveFeatureLayout,
    runway_number: int,
) -> tuple[Point, Point, float] | None:
    headers = tuple(
        header
        for header in layout.point_headers
        if header.type_ == RUNWAY_METADATA_HEADER_TYPE
        and header.runway_number == runway_number
    )
    if len(headers) != 1:
        return None
    edge_points = tuple(
        point
        for point in layout.points_for(headers[0])
        if point.type_ == RUNWAY_EDGE_POINT_TYPE
    )
    if len(edge_points) != 4:
        return None

    first_left, first_right, second_left, second_right = edge_points
    start = Point(
        (first_left.offset_x + first_right.offset_x) / 2.0,
        (first_left.offset_y + first_right.offset_y) / 2.0,
    )
    end = Point(
        (second_left.offset_x + second_right.offset_x) / 2.0,
        (second_left.offset_y + second_right.offset_y) / 2.0,
    )
    widths = (
        math.hypot(
            first_right.offset_x - first_left.offset_x,
            first_right.offset_y - first_left.offset_y,
        ),
        math.hypot(
            second_right.offset_x - second_left.offset_x,
            second_right.offset_y - second_left.offset_y,
        ),
    )
    width = sum(widths) / len(widths)
    if _distance(start, end) <= 1.0 or width <= 1.0:
        return None
    return start, end, width


def _route_header_course(
    layout: ObjectiveFeatureLayout,
    header: ObjectivePointHeader,
) -> float | None:
    """Return the signed local approach bearing from the first two PDX points.

    The authored approach direction is first point minus next point. Local x
    is east and y is north, so atan2(dx, dy) measures clockwise from north.
    Airport rotation and magnetic variation are applied when forming labels.
    """

    # A terminal flag marks the end of the route, independently of PointCount.
    if header.first_point_index < 0:
        return None
    points = layout.points[header.first_point_index : header.first_point_index + 2]
    if len(points) < 2 or (points[0].flags or 0) & 2:
        return None
    first, next_point = points[:2]
    dx = first.offset_x - next_point.offset_x
    dy = first.offset_y - next_point.offset_y
    if dx == 0.0 and dy == 0.0:
        return None
    # Keep the signed angle: label rounding truncates before wrapping to 0..360.
    return math.degrees(math.atan2(dx, dy))


def _apply_runway_labels(
    layout: ObjectiveFeatureLayout,
    runways: tuple[RunwayGeometry, ...],
    atc: AtcAirbaseData | None,
    magnetic_variation_degrees: float | None,
    objective_heading_degrees: float,
) -> tuple[RunwayGeometry, ...]:
    """Prefer ATC runway numbers; otherwise derive them from magnetic bearings.

    Add airport rotation to the local bearing, then subtract magnetic variation
    (east positive). The header's LandingPattern supplies the L/R suffix.
    """

    atc_by_header = _match_atc_to_route_headers(
        layout, atc, objective_heading_degrees,
    )
    runways_by_number = {r.runway_number: r for r in runways}
    ends_by_runway: dict[int, list[RunwayEnd]] = {}
    for header in layout.point_headers:
        if header.type_ != ROUTE_HEADER_TYPE:
            continue
        runway = runways_by_number.get(header.runway_number)
        if runway is None:
            continue
        entry = atc_by_header.get(header.index)
        course = _route_header_course(layout, header)
        if course is None:
            # Incomplete route points cannot reproduce the sim's lookup.
            # Keep the chart usable using the resolved physical runway axis.
            course = _resolved_header_course(
                runway, header, entry, objective_heading_degrees,
            )
        qfu = (
            entry.qfu if entry is not None else _qfu_for_magnetic_heading(
                course + objective_heading_degrees
                - (magnetic_variation_degrees or 0.0)
            )
        )
        suffix = {-1: "L", 1: "R"}.get(header.landing_pattern, "")
        ends_by_runway.setdefault(header.runway_number, []).append(
            RunwayEnd(f"{qfu:02d}{suffix}", _threshold_for_course(runway, course))
        )
    return tuple(
        replace(
            runway,
            ends=tuple(sorted(
                set(ends_by_runway.get(runway.runway_number, ())),
                key=lambda end: (end.designator, end.point.x, end.point.y),
            )),
        )
        for runway in runways
    )


def _match_atc_to_route_headers(
    layout: ObjectiveFeatureLayout,
    atc: AtcAirbaseData | None,
    objective_heading_degrees: float = 0.0,
) -> dict[int, AtcRunwayEntry]:
    if atc is None:
        return {}
    headers = tuple(
        sorted(
            (
                header
                for header in layout.point_headers
                if header.type_ == ROUTE_HEADER_TYPE
                and header.feature_dependency_indices
            ),
            key=lambda item: item.index,
        )
    )
    records = list(atc.runways)
    while len(records) > len(headers):
        seen: set[tuple[int, float, int]] = set()
        duplicate_index = None
        for index, entry in enumerate(records):
            key = (
                entry.runway_number,
                round(entry.heading_true, 6),
                entry.qfu,
            )
            if key in seen:
                duplicate_index = index
            else:
                seen.add(key)
        if duplicate_index is None:
            break
        records.pop(duplicate_index)
    if not headers or not records:
        return {}

    def cost(header_index: int, record_index: int) -> float:
        header = headers[header_index]
        record = records[record_index]
        runway_penalty = (
            0.0 if header.runway_number == record.runway_number else 1000.0
        )
        return runway_penalty + _angular_distance(
            header.data + objective_heading_degrees,
            record.heading_true,
        )

    if len(records) <= len(headers):

        @lru_cache(maxsize=None)
        def assign_record(
            record_index: int,
            used_headers: int,
        ) -> tuple[float, tuple[int, ...]]:
            if record_index == len(records):
                return 0.0, ()
            choices: list[tuple[float, tuple[int, ...]]] = []
            for header_index in range(len(headers)):
                bit = 1 << header_index
                if used_headers & bit:
                    continue
                remaining_cost, assignment = assign_record(
                    record_index + 1,
                    used_headers | bit,
                )
                choices.append(
                    (
                        cost(header_index, record_index) + remaining_cost,
                        (header_index, *assignment),
                    )
                )
            return min(choices)

        _, header_indices = assign_record(0, 0)
        return {
            headers[header_index].index: records[record_index]
            for record_index, header_index in enumerate(header_indices)
        }

    @lru_cache(maxsize=None)
    def assign_header(
        header_index: int,
        used_records: int,
    ) -> tuple[float, tuple[int, ...]]:
        if header_index == len(headers):
            return 0.0, ()
        choices: list[tuple[float, tuple[int, ...]]] = []
        for record_index in range(len(records)):
            bit = 1 << record_index
            if used_records & bit:
                continue
            remaining_cost, assignment = assign_header(
                header_index + 1,
                used_records | bit,
            )
            choices.append(
                (
                    cost(header_index, record_index) + remaining_cost,
                    (record_index, *assignment),
                )
            )
        return min(choices)

    _, record_indices = assign_header(0, 0)
    return {
        header.index: records[record_index]
        for header, record_index in zip(headers, record_indices)
    }


def _threshold_for_course(
    runway: RunwayGeometry,
    course_true: float,
) -> Point:
    forward = _course_between(runway.start, runway.end)
    return (
        runway.start
        if _angular_distance(course_true, forward)
        <= _angular_distance(course_true, (forward + 180.0) % 360.0)
        else runway.end
    )


def _resolved_header_course(
    runway: RunwayGeometry,
    header: ObjectivePointHeader,
    atc_entry: AtcRunwayEntry | None,
    objective_heading_degrees: float = 0.0,
) -> float:
    """Resolve a local course, retaining authored precision when on-axis."""

    if _axis_angular_distance(header.data, runway.heading_true) <= 1.0:
        return header.data % 360.0
    forward = _course_between(runway.start, runway.end)
    reverse = (forward + 180.0) % 360.0
    reference = (
        header.data if atc_entry is None
        else atc_entry.heading_true - objective_heading_degrees
    )
    return (
        forward
        if _angular_distance(reference, forward)
        <= _angular_distance(reference, reverse)
        else reverse
    )


def _runway_end_for_course(
    runway: RunwayGeometry,
    local_course: float,
) -> RunwayEnd:
    """Associate geometry by physical end, even when ATC markings are stale."""

    threshold = _threshold_for_course(runway, local_course)
    return min(
        runway.ends,
        key=lambda end: (_distance(end.point, threshold), end.designator),
    )


def build_arresting_systems(
    layout: ObjectiveFeatureLayout,
) -> tuple[ArrestingSystem, ...]:
    """Decode every authored type-9 pair in runway metadata."""

    systems: list[ArrestingSystem] = []
    for header in layout.point_headers:
        if header.type_ != RUNWAY_METADATA_HEADER_TYPE:
            continue
        pending: ObjectivePointDefinition | None = None
        for point in layout.points_for(header):
            if point.type_ != ARRESTING_SYSTEM_POINT_TYPE:
                pending = None
                continue
            if pending is None:
                pending = point
                continue
            systems.append(
                ArrestingSystem(
                    runway_number=header.runway_number,
                    start=Point(pending.offset_x, pending.offset_y),
                    end=Point(point.offset_x, point.offset_y),
                )
            )
            pending = None
    return tuple(systems)


def build_route_edges(
    layout: ObjectiveFeatureLayout,
) -> tuple[tuple[Point, Point], ...]:
    """Spatially merge all authored runway-end graphs into chart geometry."""

    route_headers = _ordered_route_headers(layout)
    if not route_headers:
        return ()
    observations = tuple(
        _build_header_route_edges(layout.points_for(header))
        for header in route_headers
    )
    spines = _merge_spatial_edges(
        tuple(observation.spines for observation in observations)
    )

    parking_spurs: list[tuple[Point, Point]] = []
    for observation in observations:
        prior_spurs = tuple(parking_spurs)
        new_spurs: list[tuple[Point, Point]] = []
        for parking, junction in observation.parking_spurs:
            if spines:
                distance, snapped = _closest_point_on_edges(junction, spines)
                if distance <= ROUTE_SNAP_TOLERANCE:
                    junction = snapped
            candidate = (parking, junction)
            if _edge_is_covered(
                candidate,
                prior_spurs,
                ROUTE_COVERAGE_TOLERANCE,
            ):
                continue
            if _distance(parking, junction) >= 0.1:
                new_spurs.append(candidate)
        parking_spurs.extend(new_spurs)

    return _deduplicate_edges((*spines, *parking_spurs))


def build_parking_positions(
    layout: ObjectiveFeatureLayout,
) -> tuple[ParkingPosition, ...]:
    """Return spatially unique parking positions across runway-end lists."""

    positions: list[ParkingPosition] = []
    for header in _ordered_route_headers(layout):
        for point in layout.points_for(header):
            if point.type_ not in NUMBERED_PARKING_TYPES:
                continue
            candidate = _parking_position(point)
            if any(
                existing.type_ == candidate.type_
                and _distance(existing.point, candidate.point)
                <= PARKING_MERGE_TOLERANCE
                for existing in positions
            ):
                continue
            positions.append(candidate)

    shelter_keys: set[tuple[float, float, int]] = set()
    for header in layout.point_headers:
        if header.type_ != 14:
            continue
        for point in layout.points_for(header):
            if point.type_ != SHELTER_PARKING_TYPE:
                continue
            candidate = _parking_position(point)
            key = (
                round(candidate.point.x, 3),
                round(candidate.point.y, 3),
                candidate.type_,
            )
            if key in shelter_keys:
                continue
            shelter_keys.add(key)
            positions.append(candidate)

    return tuple(
        sorted(
            positions,
            key=lambda item: (item.point.x, item.point.y, item.type_),
        )
    )


def build_parking_charts(
    layout: ObjectiveFeatureLayout,
    runways: tuple[RunwayGeometry, ...] | None = None,
    atc: AtcAirbaseData | None = None,
    magnetic_variation_degrees: float | None = None,
    *,
    objective_heading_degrees: float = 0.0,
) -> tuple[ParkingChart, ...]:
    """Return one exact, zero-based parking list for each runway end."""

    runway_geometries = (
        build_runways(
            layout, atc, magnetic_variation_degrees,
            objective_heading_degrees=objective_heading_degrees,
        )
        if runways is None
        else runways
    )
    runways_by_number = {
        runway.runway_number: runway for runway in runway_geometries
    }
    atc_by_header = _match_atc_to_route_headers(
        layout, atc, objective_heading_degrees,
    )
    charts: list[ParkingChart] = []
    for header in sorted(
        (
            item
            for item in layout.point_headers
            if item.type_ == ROUTE_HEADER_TYPE
        ),
        key=lambda item: item.index,
    ):
        runway = runways_by_number.get(header.runway_number)
        if runway is None or not runway.ends:
            continue
        atc_entry = atc_by_header.get(header.index)
        local_course = _resolved_header_course(
            runway, header, atc_entry, objective_heading_degrees,
        )
        runway_end = _runway_end_for_course(runway, local_course)
        positions = tuple(
            NumberedParkingPosition(
                number=number,
                point=_point_from_definition(point),
                type_=point.type_,
                heading=point.heading,
                group=point.parking_point_group,
            )
            for number, point in enumerate(
                point
                for point in layout.points_for(header)
                if point.type_ in NUMBERED_PARKING_TYPES
            )
        )
        charts.append(
            ParkingChart(
                designator=runway_end.designator,
                runway_number=header.runway_number,
                heading_true=(local_course + objective_heading_degrees) % 360.0,
                positions=positions,
            )
        )
    return tuple(charts)


def build_taxiway_labels(
    layout: ObjectiveFeatureLayout,
) -> tuple[TaxiwayLabel, ...]:
    """Return authored taxiway letters, merging reciprocal-list duplicates."""

    labels: list[TaxiwayLabel] = []
    for header in sorted(
        (
            item
            for item in layout.point_headers
            if item.type_ == ROUTE_HEADER_TYPE
        ),
        key=lambda item: item.index,
    ):
        for point in layout.points_for(header):
            value = point.taxiway_letter
            if value is None or not 1 <= value <= 26:
                continue
            candidate = TaxiwayLabel(
                point=_point_from_definition(point),
                letter=chr(ord("A") + value - 1),
            )
            if any(
                existing.letter == candidate.letter
                and _distance(existing.point, candidate.point)
                <= TAXIWAY_LABEL_MERGE_TOLERANCE
                for existing in labels
            ):
                continue
            labels.append(candidate)
    return tuple(labels)


def _ordered_route_headers(
    layout: ObjectiveFeatureLayout,
) -> tuple[ObjectivePointHeader, ...]:
    route_headers = tuple(
        header
        for header in layout.point_headers
        if header.type_ == ROUTE_HEADER_TYPE
    )
    headers_by_runway: dict[int, list[ObjectivePointHeader]] = {}
    for header in route_headers:
        headers_by_runway.setdefault(header.runway_number, []).append(header)
    runway_order = sorted(
        headers_by_runway,
        key=lambda runway_number: (
            sum(
                header.point_count
                for header in headers_by_runway[runway_number]
            ),
            runway_number,
        ),
    )
    return tuple(
        header
        for runway_number in runway_order
        for header in sorted(
            headers_by_runway[runway_number],
            key=lambda item: (item.data, item.index),
        )
    )


def _build_header_route_edges(
    points: tuple[ObjectivePointDefinition, ...],
) -> _HeaderRouteEdges:
    index_edges: dict[tuple[int, int], tuple[int, int]] = {}
    branch_starts = {
        point.branch_index
        for point in points
        if point.branch_index is not None
    }
    for local_index, point in enumerate(points):
        targets: list[int] = []
        if point.branch_index is not None:
            targets.append(point.branch_index)
        if point.root_index is not None:
            targets.append(point.root_index)
        terminal = bool((point.flags or 0) & 2)
        next_index = local_index + 1
        if (
            not terminal
            and next_index < len(points)
            and next_index not in branch_starts
        ):
            targets.append(next_index)
        for target_index in targets:
            if not 0 <= target_index < len(points):
                continue
            source = _point_from_definition(point)
            target = _point_from_definition(points[target_index])
            if _distance(source, target) < 0.1:
                continue
            index_key = tuple(sorted((local_index, target_index)))
            index_edges[index_key] = (local_index, target_index)

    neighbors: dict[int, set[int]] = {}
    for left_index, right_index in index_edges:
        neighbors.setdefault(left_index, set()).add(right_index)
        neighbors.setdefault(right_index, set()).add(left_index)

    parking_spurs: list[tuple[Point, Point]] = []
    for parking_index, point in enumerate(points):
        if point.type_ not in NUMBERED_PARKING_TYPES:
            continue
        parking_neighbors = sorted(neighbors.get(parking_index, ()))
        if len(parking_neighbors) != 2:
            continue
        left_index, right_index = parking_neighbors
        if (
            points[left_index].type_ in NUMBERED_PARKING_TYPES
            or points[right_index].type_ in NUMBERED_PARKING_TYPES
        ):
            continue

        index_edges.pop(tuple(sorted((parking_index, left_index))), None)
        index_edges.pop(tuple(sorted((parking_index, right_index))), None)
        bridge_key = tuple(sorted((left_index, right_index)))
        index_edges[bridge_key] = (left_index, right_index)

        parking = _point_from_definition(point)
        left = _point_from_definition(points[left_index])
        right = _point_from_definition(points[right_index])
        junction = _project_to_segment(parking, left, right)
        if _distance(parking, junction) >= 0.1:
            parking_spurs.append((parking, junction))

    spines = tuple(
        (
            _point_from_definition(points[source_index]),
            _point_from_definition(points[target_index]),
        )
        for source_index, target_index in index_edges.values()
    )
    return _HeaderRouteEdges(
        spines=_deduplicate_edges(spines),
        parking_spurs=_deduplicate_edges(tuple(parking_spurs)),
    )


def _merge_spatial_edges(
    edge_groups: tuple[tuple[tuple[Point, Point], ...], ...],
) -> tuple[tuple[Point, Point], ...]:
    selected: list[tuple[Point, Point]] = []
    keys: set[tuple[tuple[float, float], tuple[float, float]]] = set()
    for edge_group in edge_groups:
        prior_edges = tuple(selected)
        new_edges: list[tuple[Point, Point]] = []
        for candidate in edge_group:
            if _edge_is_covered(
                candidate,
                prior_edges,
                ROUTE_COVERAGE_TOLERANCE,
            ):
                continue
            left, right = candidate
            if prior_edges:
                left_distance, snapped_left = _closest_point_on_edges(
                    left,
                    prior_edges,
                )
                right_distance, snapped_right = _closest_point_on_edges(
                    right,
                    prior_edges,
                )
                if left_distance <= ROUTE_SNAP_TOLERANCE:
                    left = snapped_left
                if right_distance <= ROUTE_SNAP_TOLERANCE:
                    right = snapped_right
            if _distance(left, right) < 0.1:
                continue
            key = _edge_key(left, right)
            if key in keys:
                continue
            keys.add(key)
            new_edges.append((left, right))
        selected.extend(new_edges)
    return tuple(selected)


def _edge_is_covered(
    candidate: tuple[Point, Point],
    selected: tuple[tuple[Point, Point], ...],
    tolerance: float,
) -> bool:
    if not selected:
        return False
    left, right = candidate
    length = _distance(left, right)
    sample_count = max(2, min(32, math.ceil(length / 100.0)))
    for index in range(sample_count + 1):
        fraction = index / sample_count
        sample = Point(
            left.x + fraction * (right.x - left.x),
            left.y + fraction * (right.y - left.y),
        )
        distance, _ = _closest_point_on_edges(sample, selected)
        if distance > tolerance:
            return False
    return True


def _closest_point_on_edges(
    point: Point,
    edges: tuple[tuple[Point, Point], ...],
) -> tuple[float, Point]:
    best_distance = math.inf
    best_point = point
    for left, right in edges:
        candidate = _project_to_segment(point, left, right)
        distance = _distance(point, candidate)
        if distance < best_distance:
            best_distance = distance
            best_point = candidate
    return best_distance, best_point


def _deduplicate_edges(
    edges: tuple[tuple[Point, Point], ...],
) -> tuple[tuple[Point, Point], ...]:
    deduplicated: dict[
        tuple[tuple[float, float], tuple[float, float]],
        tuple[Point, Point],
    ] = {}
    for left, right in edges:
        if _distance(left, right) < 0.1:
            continue
        deduplicated[_edge_key(left, right)] = (left, right)
    return tuple(deduplicated[key] for key in sorted(deduplicated))


def _point_from_definition(point: ObjectivePointDefinition) -> Point:
    return Point(point.offset_x, point.offset_y)


def _parking_position(point: ObjectivePointDefinition) -> ParkingPosition:
    assert point.type_ is not None
    return ParkingPosition(
        point=_point_from_definition(point),
        type_=point.type_,
        heading=point.heading,
        group=point.parking_point_group,
    )


def _edge_key(
    left: Point,
    right: Point,
) -> tuple[tuple[float, float], tuple[float, float]]:
    endpoints = sorted(
        (
            (round(left.x, 2), round(left.y, 2)),
            (round(right.x, 2), round(right.y, 2)),
        )
    )
    return endpoints[0], endpoints[1]


def _project_to_segment(point: Point, start: Point, end: Point) -> Point:
    axis_x = end.x - start.x
    axis_y = end.y - start.y
    length_squared = axis_x * axis_x + axis_y * axis_y
    if length_squared == 0:
        return start
    fraction = (
        (point.x - start.x) * axis_x + (point.y - start.y) * axis_y
    ) / length_squared
    fraction = min(1.0, max(0.0, fraction))
    return Point(start.x + fraction * axis_x, start.y + fraction * axis_y)


def _distance(left: Point, right: Point) -> float:
    return math.hypot(right.x - left.x, right.y - left.y)


def _course_between(start: Point, end: Point) -> float:
    return math.degrees(math.atan2(end.x - start.x, end.y - start.y)) % 360.0


def _qfu_for_magnetic_heading(heading: float) -> int:
    # For simulator-compatible labels, truncate before wrapping, then round to
    # the nearest ten degrees (five rounds up). North is runway 36.
    degrees = int(heading) % 360
    rounded = ((degrees + 5) // 10) % 36
    return 36 if rounded == 0 else rounded


def _axis_angular_distance(left: float, right: float) -> float:
    return min(
        _angular_distance(left, right),
        _angular_distance(left, right + 180.0),
    )


def _angular_distance(left: float, right: float) -> float:
    difference = abs((left - right) % 360.0)
    return min(difference, 360.0 - difference)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0

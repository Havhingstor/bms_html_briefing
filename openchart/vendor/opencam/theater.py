"""Deterministic access to Falcon BMS new-terrain support data.

Adapted from OpenCAM commit e4dbcb54a9f4751f3045e1fd96f8627c5bf78115.
"""

from __future__ import annotations

from bisect import bisect_right
import csv
from dataclasses import dataclass
import math
from pathlib import Path
import re
import struct


FEET_PER_KILOMETER = 3280.839895013123
HEIGHT_SAMPLE_SIZE = 2
LAND_COVER_SAMPLE_SIZE = 1
LAND_COVER_BLOCK_PATTERN = re.compile(
    r"\bprotoLandCoverType\s*\{(?P<body>[^{}]*)\}",
    re.DOTALL,
)
LAND_COVER_NAME_PATTERN = re.compile(r'\bname\s*:\s*"([^"\r\n]*)"')
LAND_COVER_ID_PATTERN = re.compile(r"\bid\s*:\s*(-?\d+)")
LAND_COVER_MAP_ID_PATTERN = re.compile(
    r"\blandcoverMapIDs\s*:\s*(-?\d+)",
)


class TheaterDataError(RuntimeError):
    """Raised when theater metadata or a terrain support file is malformed."""


@dataclass(frozen=True)
class TheaterDataPaths:
    definition_path: Path
    metadata_path: Path
    height_map_path: Path | None
    land_cover_map_path: Path | None
    land_cover_types_path: Path | None
    magnetic_variation_path: Path | None


@dataclass(frozen=True)
class TheaterMetadata:
    source_path: Path
    name: str
    size_km: float
    map_size_pixels: int
    center_latitude: float
    center_longitude: float
    min_height_ft: int | None
    max_height_ft: int | None
    projection_string: str | None

    @property
    def extent_ft(self) -> float:
        return self.size_km * FEET_PER_KILOMETER


@dataclass(frozen=True)
class TerrainHeightMap:
    """An on-disk signed-int16 height grid in BMS storage orientation."""

    source_path: Path
    metadata: TheaterMetadata

    def elevation_ft(self, position_x_ft: float, position_y_ft: float) -> int:
        """Sample the authored height pixel at one campaign-world position."""

        x = _bounded_position(position_x_ft, self.metadata.extent_ft, "position_x_ft")
        y = _bounded_position(position_y_ft, self.metadata.extent_ft, "position_y_ft")
        size = self.metadata.map_size_pixels
        world_column = int(y / self.metadata.extent_ft * size)
        world_row = size - 1 - int(x / self.metadata.extent_ft * size)
        offset = (world_row * size + world_column) * HEIGHT_SAMPLE_SIZE
        with self.source_path.open("rb") as source:
            source.seek(offset)
            payload = source.read(HEIGHT_SAMPLE_SIZE)
        if len(payload) != HEIGHT_SAMPLE_SIZE:
            raise TheaterDataError(
                f"{self.source_path}: truncated height sample at byte {offset}"
            )
        return struct.unpack("<h", payload)[0]


@dataclass(frozen=True)
class LandCoverTypeDefinition:
    """One authored ``protoLandCoverType`` classification record."""

    id: int
    name: str
    landcover_map_ids: tuple[int, ...]


@dataclass(frozen=True)
class TerrainLandCoverMap:
    """An on-disk uint8 land-cover type-ID grid in BMS storage orientation."""

    source_path: Path
    definitions_path: Path | None
    metadata: TheaterMetadata
    type_definitions: tuple[LandCoverTypeDefinition, ...]

    def type_id_at(self, position_x_ft: float, position_y_ft: float) -> int:
        """Sample the authored land-cover type ID at a campaign position."""

        x = _bounded_position(position_x_ft, self.metadata.extent_ft, "position_x_ft")
        y = _bounded_position(position_y_ft, self.metadata.extent_ft, "position_y_ft")
        size = self.metadata.map_size_pixels
        world_column = int(y / self.metadata.extent_ft * size)
        world_row = size - 1 - int(x / self.metadata.extent_ft * size)
        offset = world_row * size + world_column
        with self.source_path.open("rb") as source:
            source.seek(offset)
            payload = source.read(LAND_COVER_SAMPLE_SIZE)
        if len(payload) != LAND_COVER_SAMPLE_SIZE:
            raise TheaterDataError(
                f"{self.source_path}: truncated land-cover sample at byte {offset}"
            )
        return payload[0]

    def type_definition(self, type_id: int) -> LandCoverTypeDefinition | None:
        """Return the authored definition for a stored type ID, if present."""

        if type(type_id) is not int or not 0 <= type_id <= 0xFF:
            raise ValueError(f"land-cover type ID must fit in uint8, got {type_id!r}")
        return next(
            (item for item in self.type_definitions if item.id == type_id),
            None,
        )

    def type_at(
        self,
        position_x_ft: float,
        position_y_ft: float,
    ) -> LandCoverTypeDefinition | None:
        """Return the definition for the sampled type ID, if one is authored."""

        return self.type_definition(self.type_id_at(position_x_ft, position_y_ft))


@dataclass(frozen=True)
class MagneticVariationGrid:
    """A rectangular theater-kilometer grid; negative values are west."""

    source_path: Path
    epoch: int
    x_km: tuple[float, ...]
    y_km: tuple[float, ...]
    values_degrees: tuple[tuple[float, ...], ...]

    def variation_degrees(self, x_km: float, y_km: float) -> float:
        """Bilinearly interpolate signed magnetic variation on the grid."""

        x = _bounded_axis_value(x_km, self.x_km, "x_km")
        y = _bounded_axis_value(y_km, self.y_km, "y_km")
        x_index, x_fraction = _interpolation_position(x, self.x_km)
        y_index, y_fraction = _interpolation_position(y, self.y_km)
        lower_left = self.values_degrees[y_index][x_index]
        lower_right = self.values_degrees[y_index][x_index + 1]
        upper_left = self.values_degrees[y_index + 1][x_index]
        upper_right = self.values_degrees[y_index + 1][x_index + 1]
        lower = lower_left + (lower_right - lower_left) * x_fraction
        upper = upper_left + (upper_right - upper_left) * x_fraction
        return lower + (upper - lower) * y_fraction

    def variation_at_campaign_position(
        self,
        position_x_ft: float,
        position_y_ft: float,
    ) -> float:
        return self.variation_degrees(
            position_x_ft / FEET_PER_KILOMETER,
            position_y_ft / FEET_PER_KILOMETER,
        )


@dataclass(frozen=True)
class TheaterData:
    paths: TheaterDataPaths
    metadata: TheaterMetadata
    height_map: TerrainHeightMap | None
    land_cover_map: TerrainLandCoverMap | None
    magnetic_variation: MagneticVariationGrid | None


def resolve_theater_data_paths(
    theater_dir: str | Path,
) -> TheaterDataPaths | None:
    """Resolve active new-terrain files from one theater support directory."""

    theater_path = Path(theater_dir).expanduser().resolve()
    base_data_dir = (
        theater_path
        if theater_path.name.casefold() == "data"
        else theater_path.parent
        if theater_path.parent.name.casefold() == "data"
        else theater_path
    )
    definition_path = resolve_theater_definition_path(
        theater_path,
        include_root_definition=True,
    )
    if definition_path is None:
        return None
    directives = load_theater_definition_directives(definition_path)
    if new_terrain_value := directives.get("newterraindir"):
        new_terrain_dir = _windows_relative_path(base_data_dir, new_terrain_value)
    elif terrain_value := directives.get("terraindir"):
        terrain_dir = _windows_relative_path(base_data_dir, terrain_value)
        new_terrain_dir = _casefold_path(terrain_dir, "NewTerrain")
    else:
        new_terrain_dir = _casefold_path(
            base_data_dir,
            "TerrData",
            "Korea",
            "NewTerrain",
        )
    metadata_path = _casefold_path(new_terrain_dir, "Theater.txt")
    if not metadata_path.is_file():
        return None
    height_map_candidate = _casefold_path(
        new_terrain_dir,
        "HeightMaps",
        "HeightMap.raw",
    )
    ground_types_dir = _casefold_path(new_terrain_dir, "GroundTypes")
    land_cover_map_candidate = _casefold_path(
        ground_types_dir,
        "LandCoverMap.raw",
    )
    land_cover_types_candidate = _casefold_path(
        ground_types_dir,
        "LandCoverTypes.txt",
    )
    weather_dir = _casefold_path(new_terrain_dir.parent, "Weather")
    magnetic_candidates = tuple(
        sorted(
            (
                path
                for path in weather_dir.iterdir()
                if path.is_file()
                and path.name.casefold().startswith("magvarmap_")
                and path.suffix.casefold() == ".csv"
            ),
            key=lambda path: path.name.casefold(),
        )
    ) if weather_dir.is_dir() else ()
    magnetic_variation_path = _select_magnetic_variation_path(
        magnetic_candidates,
        definition_path,
        new_terrain_dir,
        weather_dir,
    )
    return TheaterDataPaths(
        definition_path=definition_path,
        metadata_path=metadata_path,
        height_map_path=(
            height_map_candidate if height_map_candidate.is_file() else None
        ),
        land_cover_map_path=(
            land_cover_map_candidate if land_cover_map_candidate.is_file() else None
        ),
        land_cover_types_path=(
            land_cover_types_candidate
            if land_cover_types_candidate.is_file()
            else None
        ),
        magnetic_variation_path=magnetic_variation_path,
    )


def _select_magnetic_variation_path(
    candidates: tuple[Path, ...],
    definition_path: Path,
    new_terrain_dir: Path,
    weather_dir: Path,
) -> Path | None:
    """Resolve one theater-specific grid when stale copied grids coexist."""

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    expected_stems = (
        new_terrain_dir.parent.name,
        definition_path.stem,
    )
    for stem in dict.fromkeys(expected_stems):
        expected_name = f"magvarmap_{stem}.csv".casefold()
        matching = tuple(
            path for path in candidates if path.name.casefold() == expected_name
        )
        if len(matching) == 1:
            return matching[0]
    raise TheaterDataError(
        f"{weather_dir}: expected at most one MagVarMap CSV or one named "
        f"for the active terrain, found {len(candidates)}"
    )


def resolve_theater_definition_path(
    theater_dir: str | Path,
    *,
    include_root_definition: bool = True,
) -> Path | None:
    """Return the theater's single TDF from either supported location."""

    theater_path = Path(theater_dir).expanduser().resolve()
    definition_dirs = (
        _casefold_path(theater_path, "TerrData", "TheaterDefinition"),
        *(
            (_casefold_path(theater_path, "TheaterDefinition"),)
            if include_root_definition
            else ()
        ),
    )
    definitions = tuple(
        sorted(
            {
                path
                for directory in definition_dirs
                if directory.is_dir()
                for path in directory.iterdir()
                if path.is_file() and path.suffix.casefold() == ".tdf"
            },
            key=lambda path: str(path).casefold(),
        )
    )
    if not definitions:
        return None
    if len(definitions) != 1:
        locations = ", ".join(str(path) for path in definitions)
        raise TheaterDataError(
            f"{theater_path}: expected one theater definition, "
            f"found {len(definitions)}: {locations}"
        )
    return definitions[0]


def load_theater_metadata(path: str | Path) -> TheaterMetadata:
    source_path = Path(path)
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        source_path.read_text(encoding="utf-8-sig", errors="strict").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise TheaterDataError(
                f"{source_path}:{line_number}: expected key=value metadata"
            )
        normalized = key.strip().casefold()
        if normalized in values:
            raise TheaterDataError(
                f"{source_path}:{line_number}: duplicate metadata key {key.strip()!r}"
            )
        values[normalized] = value.strip()

    def required(name: str) -> str:
        try:
            return values[name.casefold()]
        except KeyError as exc:
            raise TheaterDataError(f"{source_path}: missing {name}") from exc

    def optional_integer(name: str) -> int | None:
        value = values.get(name.casefold())
        return None if value is None else _integer(value, source_path)

    return TheaterMetadata(
        source_path=source_path,
        name=required("Theater name"),
        size_km=_positive_float(required("Theater size in KM"), source_path),
        map_size_pixels=_positive_int(required("Map size in pixels"), source_path),
        center_latitude=_finite_float(required("Center latitude"), source_path),
        center_longitude=_finite_float(required("Center longitude"), source_path),
        min_height_ft=optional_integer("Min height in theater"),
        max_height_ft=optional_integer("Max height in theater"),
        projection_string=values.get("projection string") or None,
    )


def load_terrain_height_map(
    path: str | Path,
    metadata: TheaterMetadata,
) -> TerrainHeightMap:
    source_path = Path(path)
    expected_size = metadata.map_size_pixels**2 * HEIGHT_SAMPLE_SIZE
    actual_size = source_path.stat().st_size
    if actual_size != expected_size:
        raise TheaterDataError(
            f"{source_path}: expected {expected_size} height-map bytes, "
            f"found {actual_size}"
        )
    return TerrainHeightMap(source_path=source_path, metadata=metadata)


def load_land_cover_type_definitions(
    path: str | Path,
) -> tuple[LandCoverTypeDefinition, ...]:
    """Parse the identity fields from authored ``protoLandCoverType`` blocks."""

    source_path = Path(path)
    payload = source_path.read_text(encoding="utf-8-sig", errors="strict")
    payload = "\n".join(line.split("#", 1)[0] for line in payload.splitlines())
    definitions: list[LandCoverTypeDefinition] = []
    for block_number, match in enumerate(
        LAND_COVER_BLOCK_PATTERN.finditer(payload),
        start=1,
    ):
        body = match.group("body")
        name_match = LAND_COVER_NAME_PATTERN.search(body)
        id_match = LAND_COVER_ID_PATTERN.search(body)
        if name_match is None or id_match is None:
            raise TheaterDataError(
                f"{source_path}: land-cover block {block_number} needs name and id"
            )
        type_id = _integer(id_match.group(1), source_path)
        if not 0 <= type_id <= 0xFF:
            raise TheaterDataError(
                f"{source_path}: land-cover type ID {type_id} does not fit in uint8"
            )
        if any(item.id == type_id for item in definitions):
            raise TheaterDataError(
                f"{source_path}: duplicate land-cover type ID {type_id}"
            )
        definitions.append(
            LandCoverTypeDefinition(
                id=type_id,
                name=name_match.group(1),
                landcover_map_ids=tuple(
                    _integer(value, source_path)
                    for value in LAND_COVER_MAP_ID_PATTERN.findall(body)
                ),
            )
        )
    if not definitions:
        raise TheaterDataError(
            f"{source_path}: no protoLandCoverType definitions found"
        )
    return tuple(definitions)


def load_terrain_land_cover_map(
    path: str | Path,
    metadata: TheaterMetadata,
    definitions_path: str | Path | None = None,
) -> TerrainLandCoverMap:
    """Validate a uint8 type-ID map and attach its optional definitions."""

    source_path = Path(path)
    expected_size = metadata.map_size_pixels**2 * LAND_COVER_SAMPLE_SIZE
    actual_size = source_path.stat().st_size
    if actual_size != expected_size:
        raise TheaterDataError(
            f"{source_path}: expected {expected_size} land-cover bytes, "
            f"found {actual_size}"
        )
    resolved_definitions_path = (
        None if definitions_path is None else Path(definitions_path)
    )
    return TerrainLandCoverMap(
        source_path=source_path,
        definitions_path=resolved_definitions_path,
        metadata=metadata,
        type_definitions=(
            ()
            if resolved_definitions_path is None
            else load_land_cover_type_definitions(resolved_definitions_path)
        ),
    )


def load_magnetic_variation_grid(path: str | Path) -> MagneticVariationGrid:
    source_path = Path(path)
    lines = source_path.read_text(encoding="utf-8-sig", errors="strict").splitlines()
    if len(lines) < 4 or not lines[0].strip().startswith("#"):
        raise TheaterDataError(f"{source_path}: missing magnetic-grid epoch header")
    try:
        epoch = int(lines[0].strip()[1:])
    except ValueError as exc:
        raise TheaterDataError(
            f"{source_path}: invalid magnetic-grid epoch {lines[0]!r}"
        ) from exc
    rows = [_trim_csv_row(row) for row in csv.reader(lines[1:])]
    if not rows or not rows[0] or rows[0][0].strip().casefold() != "y\\x":
        raise TheaterDataError(f"{source_path}: expected y\\x grid header")
    x_axis = tuple(_grid_float(value, source_path) for value in rows[0][1:])
    if len(x_axis) < 2:
        raise TheaterDataError(f"{source_path}: magnetic grid needs at least two columns")
    y_axis: list[float] = []
    values: list[tuple[float, ...]] = []
    for row_number, row in enumerate(rows[1:], start=3):
        if not row:
            continue
        if len(row) != len(x_axis) + 1:
            raise TheaterDataError(
                f"{source_path}:{row_number}: expected {len(x_axis) + 1} "
                f"grid fields, found {len(row)}"
            )
        y_axis.append(_grid_float(row[0], source_path))
        values.append(
            tuple(_grid_float(value, source_path) for value in row[1:])
        )
    if len(y_axis) < 2:
        raise TheaterDataError(f"{source_path}: magnetic grid needs at least two rows")
    _require_strictly_increasing(x_axis, source_path, "x")
    _require_strictly_increasing(tuple(y_axis), source_path, "y")
    return MagneticVariationGrid(
        source_path=source_path,
        epoch=epoch,
        x_km=x_axis,
        y_km=tuple(y_axis),
        values_degrees=tuple(values),
    )


def load_theater_data(paths: TheaterDataPaths) -> TheaterData:
    metadata = load_theater_metadata(paths.metadata_path)
    return TheaterData(
        paths=paths,
        metadata=metadata,
        height_map=(
            None
            if paths.height_map_path is None
            else load_terrain_height_map(paths.height_map_path, metadata)
        ),
        land_cover_map=(
            None
            if paths.land_cover_map_path is None
            else load_terrain_land_cover_map(
                paths.land_cover_map_path,
                metadata,
                paths.land_cover_types_path,
            )
        ),
        magnetic_variation=(
            None
            if paths.magnetic_variation_path is None
            else load_magnetic_variation_grid(paths.magnetic_variation_path)
        ),
    )


def load_theater_definition_directives(path: str | Path) -> dict[str, str]:
    source_path = Path(path)
    directives: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        source_path.read_text(encoding="utf-8-sig", errors="strict").splitlines(),
        start=1,
    ):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[1].strip():
            continue
        key, value = parts
        normalized = key.casefold()
        if normalized in directives:
            raise TheaterDataError(
                f"{source_path}:{line_number}: duplicate theater directive {key!r}"
            )
        directives[normalized] = value.strip()
    return directives


def _windows_relative_path(root: Path, value: str) -> Path:
    parts = (
        part
        for part in value.replace("/", "\\").split("\\")
        if part
    )
    return _casefold_path(root, *parts)


def _casefold_path(root: Path, *parts: str) -> Path:
    current = root
    for part in parts:
        direct = current / part
        if direct.exists() or not current.is_dir():
            current = direct
            continue
        match = next(
            (
                child
                for child in current.iterdir()
                if child.name.casefold() == part.casefold()
            ),
            None,
        )
        current = direct if match is None else match
    return current


def _bounded_position(value: float, extent: float, name: str) -> float:
    numeric = _finite_number(value, name)
    if not 0.0 <= numeric < extent:
        raise IndexError(f"{name} {numeric} is outside 0 <= value < {extent}")
    return numeric


def _bounded_axis_value(
    value: float,
    axis: tuple[float, ...],
    name: str,
) -> float:
    numeric = _finite_number(value, name)
    if not axis[0] <= numeric <= axis[-1]:
        raise IndexError(
            f"{name} {numeric} is outside {axis[0]} <= value <= {axis[-1]}"
        )
    return numeric


def _interpolation_position(
    value: float,
    axis: tuple[float, ...],
) -> tuple[int, float]:
    index = min(len(axis) - 2, max(0, bisect_right(axis, value) - 1))
    fraction = (value - axis[index]) / (axis[index + 1] - axis[index])
    return index, fraction


def _trim_csv_row(row: list[str]) -> list[str]:
    while row and not row[-1].strip():
        row.pop()
    return row


def _require_strictly_increasing(
    values: tuple[float, ...],
    path: Path,
    axis_name: str,
) -> None:
    if any(right <= left for left, right in zip(values, values[1:])):
        raise TheaterDataError(
            f"{path}: magnetic-grid {axis_name} axis is not strictly increasing"
        )


def _finite_number(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite number")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"{name} must be a finite number")
    return numeric


def _grid_float(value: str, path: Path) -> float:
    try:
        return _finite_float(value, path)
    except TheaterDataError as exc:
        raise TheaterDataError(f"{path}: invalid magnetic-grid value {value!r}") from exc


def _finite_float(value: str, path: Path) -> float:
    try:
        numeric = float(value.strip())
    except ValueError as exc:
        raise TheaterDataError(f"{path}: invalid float {value!r}") from exc
    if not math.isfinite(numeric):
        raise TheaterDataError(f"{path}: non-finite float {value!r}")
    return numeric


def _positive_float(value: str, path: Path) -> float:
    numeric = _finite_float(value, path)
    if numeric <= 0:
        raise TheaterDataError(f"{path}: expected positive value, got {value!r}")
    return numeric


def _integer(value: str, path: Path) -> int:
    try:
        return int(value.strip())
    except ValueError as exc:
        raise TheaterDataError(f"{path}: invalid integer {value!r}") from exc


def _positive_int(value: str, path: Path) -> int:
    numeric = _integer(value, path)
    if numeric <= 0:
        raise TheaterDataError(f"{path}: expected positive integer, got {value!r}")
    return numeric

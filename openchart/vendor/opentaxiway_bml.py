"""Read triangle-list geometry from Falcon BMS BML model files.

This module is derived from OpenTaxiway by the Falcon BMS Team:
https://github.com/Benchmark-Sims/OpenTaxiway

Copyright (c) 2023 BENCHMARKSIMS.COM ("The Falcon BMS Team")
Licensed under the MIT License. See
``THIRD_PARTY_LICENSES/OpenTaxiway-LICENSE.md`` in the repository root.

The original command-line renderer also performs BMS database lookups and
exports standalone SVG/OBJ files. OpenChart needs only its BML decompression
and primitive-vertex parsing, so that code is kept here as a small library
module with deterministic errors and no diagnostic output.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from io import BytesIO
import lzma
from pathlib import Path
import re
import struct
from typing import BinaryIO


COMPRESSION_NONE = 0
COMPRESSION_LZ4 = 1
COMPRESSION_LZMA = 2
NODE_TYPE_PRIMITIVE = 1
SUPPORTED_VERTEX_SIZES = frozenset((32, 36, 40, 48))
MODEL_NAME_PATTERN = re.compile(
    r"^(?:model_(\d+)|.+_l(\d+))\.bml$",
    re.IGNORECASE,
)

Vertex3D = tuple[float, float, float]
Triangle3D = tuple[Vertex3D, Vertex3D, Vertex3D]


class OpenTaxiwayError(RuntimeError):
    """Raised when a BML model cannot be decoded without guessing."""


@dataclass(frozen=True)
class _Primitive:
    vertex_start_offset: int
    vertex_count: int
    vertex_size: int


def find_highest_lod_model(
    models_dir: str | Path | Iterable[str | Path],
    graphics_id: int,
) -> Path | None:
    """Return the highest numbered BML LOD from layered model roots."""

    for root in _model_roots(models_dir):
        candidates = _lod_models(root, graphics_id)
        if candidates:
            return max(candidates)[2]
    return None


def find_chart_lod_model(
    models_dir: str | Path | Iterable[str | Path],
    graphics_id: int,
    preferred_lod: int = 1,
) -> Path | None:
    """Return the model nearest the medium-distance airport-chart LOD.

    BMS LOD numbers increase with viewing distance: ``Model_0`` is the
    close-up mesh, while the largest number is the most distant mesh.  An
    airport-wide chart corresponds to the normal ``Model_1`` viewing range.
    Some models omit that LOD, so select the numerically closest available
    model and favor the more detailed one when two candidates are equidistant.
    """

    for root in _model_roots(models_dir):
        candidates = _lod_models(root, graphics_id)
        if candidates:
            return min(
                candidates,
                key=lambda candidate: (
                    abs(candidate[0] - preferred_lod),
                    candidate[0],
                    candidate[1],
                ),
            )[2]
    return None


def _model_roots(
    models_dir: str | Path | Iterable[str | Path],
) -> tuple[Path, ...]:
    if isinstance(models_dir, (str, Path)):
        return (Path(models_dir),)
    return tuple(Path(path) for path in models_dir)


def _lod_models(
    models_dir: Path,
    graphics_id: int,
) -> list[tuple[int, str, Path]]:
    model_dir = models_dir / str(graphics_id)
    if not model_dir.is_dir():
        return []
    candidates: list[tuple[int, str, Path]] = []
    for path in model_dir.iterdir():
        match = MODEL_NAME_PATTERN.fullmatch(path.name)
        if match is not None and path.is_file():
            lod = next(group for group in match.groups() if group is not None)
            candidates.append((int(lod), path.name.casefold(), path))
    return candidates


def load_bml_triangles(path: str | Path) -> tuple[Triangle3D, ...]:
    """Load authored sequential triangle vertices from one BML model."""

    source = Path(path)
    try:
        raw = source.read_bytes()
        version, payload = _decode_file(raw)
        primitives, vertex_buffer = _parse_payload(payload, version)
        triangles: list[Triangle3D] = []
        for primitive in primitives:
            if primitive.vertex_count <= 0:
                continue
            positions = _parse_vertex_positions(vertex_buffer, primitive)
            for index in range(0, len(positions) - 2, 3):
                triangles.append(
                    (
                        positions[index],
                        positions[index + 1],
                        positions[index + 2],
                    )
                )
    except OpenTaxiwayError:
        raise
    except (EOFError, OSError, lzma.LZMAError, struct.error) as exc:
        raise OpenTaxiwayError(f"{source}: {exc}") from exc
    if not triangles:
        raise OpenTaxiwayError(f"{source}: model contains no triangle geometry")
    return tuple(triangles)


def _decode_file(raw: bytes) -> tuple[int, bytes]:
    header_size = 4 + struct.calcsize("<IIQQ")
    if len(raw) < header_size:
        raise OpenTaxiwayError("BML file is smaller than its header")
    if raw[:4] != b"BML\x00":
        raise OpenTaxiwayError("invalid BML magic header")
    version, compression, payload_size, compressed_size = struct.unpack_from(
        "<IIQQ", raw, 4
    )
    available = len(raw) - header_size
    if available < compressed_size:
        raise OpenTaxiwayError(
            f"BML payload is truncated ({available} < {compressed_size})"
        )
    compressed = raw[header_size : header_size + compressed_size]
    payload = _decompress_payload(compression, payload_size, compressed)
    if len(payload) != payload_size:
        raise OpenTaxiwayError(
            f"BML payload size mismatch ({len(payload)} != {payload_size})"
        )
    return version, payload


def _decompress_payload(
    compression: int,
    expected_size: int,
    payload: bytes,
) -> bytes:
    if compression == COMPRESSION_NONE:
        return payload
    if compression == COMPRESSION_LZMA:
        try:
            return lzma.decompress(payload, format=lzma.FORMAT_ALONE)
        except lzma.LZMAError:
            if len(payload) < 5:
                raise OpenTaxiwayError("invalid BMS LZMA payload")
            bms_payload = payload[:5] + struct.pack("<Q", expected_size) + payload[5:]
            return lzma.decompress(bms_payload, format=lzma.FORMAT_ALONE)
    if compression == COMPRESSION_LZ4:
        try:
            import lz4.frame  # type: ignore[import-not-found]

            try:
                return lz4.frame.decompress(payload)
            except Exception:
                import lz4.block  # type: ignore[import-not-found]

                return lz4.block.decompress(payload, uncompressed_size=expected_size)
        except ImportError as exc:
            raise OpenTaxiwayError(
                "LZ4-compressed BML requires the optional 'lz4' package"
            ) from exc
    raise OpenTaxiwayError(f"unsupported BML compression value {compression}")


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    payload = stream.read(size)
    if payload is None or len(payload) != size:
        raise EOFError(
            f"unexpected EOF: wanted {size} bytes, got "
            f"{0 if payload is None else len(payload)}"
        )
    return payload


def _parse_payload(
    payload: bytes,
    file_version: int,
) -> tuple[tuple[_Primitive, ...], bytes]:
    stream = BytesIO(payload)
    _, material_count = struct.unpack("<II", _read_exact(stream, 8))
    if file_version == 1:
        _read_exact(stream, material_count * 4)
    else:
        for _ in range(material_count):
            name_length = struct.unpack("<i", _read_exact(stream, 4))[0]
            if not 0 <= name_length <= len(payload) - stream.tell():
                raise OpenTaxiwayError(f"invalid BML material name length {name_length}")
            _read_exact(stream, name_length)

    _, _, _, node_count = struct.unpack("<IIII", _read_exact(stream, 16))
    primitives: list[_Primitive] = []
    for _ in range(node_count):
        node_type, _, node_version = struct.unpack(
            "<III", _read_exact(stream, 12)
        )
        if node_type == NODE_TYPE_PRIMITIVE:
            (
                _,
                _,
                _,
                _,
                _,
                vertex_start_offset,
                vertex_count,
                vertex_size,
            ) = struct.unpack("<IfIIIIII", _read_exact(stream, 32))
            _read_exact(stream, 12)
            # RTModelPrimitive serializes two common boolean flags after the
            # reference point. The version-specific subclass follows them:
            # V1 stores five 32-bit legacy material/texture values, whereas
            # V2 stores one 16-bit material index.
            _read_exact(stream, 2)
            if node_version == 1:
                _read_exact(stream, 20)
            elif node_version == 2:
                _read_exact(stream, 2)
            else:
                raise OpenTaxiwayError(
                    f"unsupported BML primitive version {node_version}"
                )
            primitives.append(
                _Primitive(
                    vertex_start_offset=vertex_start_offset,
                    vertex_count=vertex_count,
                    vertex_size=vertex_size,
                )
            )
            continue
        body_sizes = {
            0: 0,
            2: 84,
            3: 0,
            4: 9,
            5: 0,
            6: 52,
            7: 0,
            8: 36,
        }
        body_size = body_sizes.get(node_type)
        if body_size is None:
            raise OpenTaxiwayError(f"unsupported BML node type {node_type}")
        _read_exact(stream, body_size)

    expected_vertex_bytes = max(
        (
            primitive.vertex_start_offset
            + primitive.vertex_count * primitive.vertex_size
            for primitive in primitives
        ),
        default=0,
    )
    vertex_buffer = _find_vertex_buffer(
        payload,
        stream.tell(),
        expected_vertex_bytes,
    )
    return tuple(primitives), vertex_buffer


def _find_vertex_buffer(
    payload: bytes,
    start_offset: int,
    minimum_size: int,
) -> bytes:
    """Find the serialized IB/VB pair whose vertex buffer ends the payload."""

    payload_size = len(payload)
    for offset in range(start_offset, payload_size - 7):
        index_size = struct.unpack_from("<I", payload, offset)[0]
        vertex_size_offset = offset + 4 + index_size
        if vertex_size_offset + 4 > payload_size:
            continue
        vertex_size = struct.unpack_from("<I", payload, vertex_size_offset)[0]
        vertex_offset = vertex_size_offset + 4
        if vertex_size < minimum_size:
            continue
        if vertex_offset + vertex_size == payload_size:
            return payload[vertex_offset:]
    raise OpenTaxiwayError("could not locate a complete BML vertex buffer")


def _parse_vertex_positions(
    vertex_buffer: bytes,
    primitive: _Primitive,
) -> tuple[Vertex3D, ...]:
    if primitive.vertex_size not in SUPPORTED_VERTEX_SIZES:
        raise OpenTaxiwayError(
            f"unsupported BML vertex size {primitive.vertex_size}"
        )
    end = (
        primitive.vertex_start_offset
        + primitive.vertex_count * primitive.vertex_size
    )
    if end > len(vertex_buffer):
        raise OpenTaxiwayError(
            f"primitive vertex range ends at {end}, buffer has {len(vertex_buffer)} bytes"
        )
    return tuple(
        struct.unpack_from(
            "<fff",
            vertex_buffer,
            primitive.vertex_start_offset + index * primitive.vertex_size,
        )
        for index in range(primitive.vertex_count)
    )

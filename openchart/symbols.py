"""Vector symbol geometry embedded directly in generated charts."""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
import re
from typing import Mapping
import xml.etree.ElementTree as ET


ASSET_DIRECTORY = Path(__file__).resolve().parent / "assets"
NAVIGATION_SYMBOL_FILENAMES = {
    "NDB": "NDB.svg",
    "TACAN": "TACAN.svg",
    "VOR": "VOR.svg",
    "VOR/DME": "VOR-DME.svg",
    "VORTAC": "VORTAC.svg",
}
WINDSOCK_SYMBOL_FILENAME = "windsock.svg"
SVG_NS = "http://www.w3.org/2000/svg"
_INLINE_GRAPHICS_TAGS = frozenset(
    ("circle", "ellipse", "g", "line", "path", "polygon", "polyline", "rect")
)
_DIMENSION_PATTERN = re.compile(
    r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"(?:[A-Za-z]+)?\s*"
)
_ROOT_STRUCTURAL_ATTRIBUTES = frozenset(
    ("height", "id", "preserveAspectRatio", "version", "viewBox", "width")
)


@lru_cache(maxsize=None)
def _navigation_symbol_source(
    kind: str,
) -> tuple[
    tuple[float, float, float, float],
    tuple[tuple[str, str], ...],
    tuple[ET.Element, ...],
]:
    filename = NAVIGATION_SYMBOL_FILENAMES.get(kind)
    if filename is None:
        raise ValueError(f"unsupported navigation symbol kind {kind!r}")
    return _symbol_source(filename)


@lru_cache(maxsize=None)
def _symbol_source(
    filename: str,
) -> tuple[
    tuple[float, float, float, float],
    tuple[tuple[str, str], ...],
    tuple[ET.Element, ...],
]:
    root = ET.fromstring(
        files("openchart.assets").joinpath(filename).read_bytes()
    )
    width = _svg_dimension(root.attrib.get("width"), filename, "width")
    height = _svg_dimension(root.attrib.get("height"), filename, "height")
    view_box = root.attrib.get("viewBox")
    if view_box is None:
        bounds = (0.0, 0.0, width, height)
    else:
        try:
            bounds = tuple(float(value) for value in view_box.split())
        except ValueError as exc:
            raise ValueError(f"{filename}: invalid SVG viewBox {view_box!r}") from exc
        if len(bounds) != 4 or bounds[2] <= 0 or bounds[3] <= 0:
            raise ValueError(f"{filename}: invalid SVG viewBox {view_box!r}")

    presentation = tuple(
        (name, value)
        for name, value in root.attrib.items()
        if name not in _ROOT_STRUCTURAL_ATTRIBUTES and not name.startswith("{")
    )
    graphics = tuple(
        _clean_inline_graphic(child)
        for child in root
        if _local_name(child.tag) in _INLINE_GRAPHICS_TAGS
    )
    if not graphics:
        raise ValueError(f"{filename}: SVG contains no inline graphics")
    return bounds, presentation, graphics


def navigation_symbol_element(
    kind: str,
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    attributes: Mapping[str, str] | None = None,
) -> ET.Element:
    """Return native SVG geometry fitted inside the requested viewport."""

    if width <= 0 or height <= 0:
        raise ValueError("navigation symbol dimensions must be positive")
    (min_x, min_y, source_width, source_height), presentation, graphics = (
        _navigation_symbol_source(kind)
    )
    scale = min(width / source_width, height / source_height)
    translate_x = x + (width - source_width * scale) / 2.0 - min_x * scale
    translate_y = y + (height - source_height * scale) / 2.0 - min_y * scale
    group_attributes = dict(presentation)
    group_attributes.update(attributes or {})
    group_attributes["transform"] = (
        f"matrix({_number(scale)} 0 0 {_number(scale)} "
        f"{_number(translate_x)} {_number(translate_y)})"
    )
    group = ET.Element(f"{{{SVG_NS}}}g", group_attributes)
    for graphic in graphics:
        group.append(deepcopy(graphic))
    return group


def windsock_symbol_element(
    *,
    x: float,
    y: float,
    size: float,
    attributes: Mapping[str, str] | None = None,
) -> ET.Element:
    """Return the cropped windsock pictogram at a fixed chart size."""

    if size <= 0:
        raise ValueError("windsock symbol size must be positive")
    (min_x, min_y, source_width, source_height), presentation, graphics = (
        _symbol_source(WINDSOCK_SYMBOL_FILENAME)
    )
    scale = min(size / source_width, size / source_height)
    translate_x = x - source_width * scale / 2.0 - min_x * scale
    translate_y = y - source_height * scale / 2.0 - min_y * scale
    group_attributes = dict(presentation)
    group_attributes.update(attributes or {})
    group_attributes["transform"] = (
        f"matrix({_number(scale)} 0 0 {_number(scale)} "
        f"{_number(translate_x)} {_number(translate_y)})"
    )
    group = ET.Element(f"{{{SVG_NS}}}g", group_attributes)
    for graphic in graphics:
        group.append(deepcopy(graphic))
    return group


def _svg_dimension(value: str | None, filename: str, name: str) -> float:
    match = None if value is None else _DIMENSION_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError(f"{filename}: invalid SVG {name} {value!r}")
    dimension = float(match.group(1))
    if dimension <= 0:
        raise ValueError(f"{filename}: SVG {name} must be positive")
    return dimension


def _clean_inline_graphic(source: ET.Element) -> ET.Element:
    graphic = deepcopy(source)
    for element in graphic.iter():
        if _local_name(element.tag) not in _INLINE_GRAPHICS_TAGS:
            raise ValueError(
                "symbol contains unsupported nested SVG element "
                f"{_local_name(element.tag)!r}"
            )
        for name, value in tuple(element.attrib.items()):
            if name == "id" or name.startswith("{"):
                del element.attrib[name]
            elif name.endswith("href") or "url(" in value.casefold():
                raise ValueError(
                    "symbol geometry must not reference another resource"
                )
    return graphic


def _local_name(name: str) -> str:
    return name.rpartition("}")[2]


def _number(value: float) -> str:
    return f"{value:.12g}"


# Public-domain Pictogram_VORTAC.svg from Wikimedia Commons:
# https://commons.wikimedia.org/wiki/File:Pictogram_VORTAC.svg
VORTAC_SOURCE_WIDTH = 97
VORTAC_SOURCE_HEIGHT = 85
VORTAC_SOURCE_STROKE = "#000"
VORTAC_SOURCE_STROKE_WIDTH = 2
VORTAC_PATHS: tuple[tuple[str, str | None], ...] = (
    ("m1,26 15-25 20,12-15,25zm60-13 20-12 15,25-20,12zM34,60h29v23H34z", None),
    ("m21,38 13,22h29l13-22V13H21", "none"),
)
VORTAC_CIRCLE = (48, 37, 7)


__all__ = [
    "ASSET_DIRECTORY",
    "NAVIGATION_SYMBOL_FILENAMES",
    "WINDSOCK_SYMBOL_FILENAME",
    "VORTAC_CIRCLE",
    "VORTAC_PATHS",
    "VORTAC_SOURCE_HEIGHT",
    "VORTAC_SOURCE_STROKE",
    "VORTAC_SOURCE_STROKE_WIDTH",
    "VORTAC_SOURCE_WIDTH",
    "navigation_symbol_element",
    "windsock_symbol_element",
]

from __future__ import annotations

import configparser
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import pymupdf

from lib.charts.service import MANAGED_BRIEF_RELATIVE_PATH


KNEEBOARD_ORDER_SECTION = "kneeboard_order"
KNEEBOARD_ORDER_KEY = "pages"
KNEEBOARD_ORDER_LEFT_KEY = "pages_left"
KNEEBOARD_ORDER_RIGHT_KEY = "pages_right"
RESERVED_BRIEF_PDF = "kneeboard.pdf"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
PDF_EXTENSION = ".pdf"


@dataclass(frozen=True)
class KneeboardPage:
    id: str
    kind: str
    label: str
    path: Path
    page_index: Optional[int] = None
    included: bool = True
    available: bool = True
    side: Optional[str] = None
    tooltip: Optional[str] = None
    parking_placeholder: bool = False
    aliases: tuple[str, ...] = ()

    def with_included(self, included: bool) -> "KneeboardPage":
        return replace(self, included=included)

    def with_side(self, side: str) -> "KneeboardPage":
        return replace(self, side=side)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "source": self.path.name,
            "page": None if self.page_index is None else self.page_index + 1,
            "included": self.included,
            "available": self.available,
            "side": self.side,
            "tooltip": self.tooltip,
        }


def max_kneeboard_pages(airframe: str) -> int:
    return 16 if airframe == "F-15" else 32


def discover_kneeboard_pages(
    conf: configparser.ConfigParser,
    airframe: str,
    *,
    bms_cfg: Any | None = None,
    chart_service: Any | None = None,
    path_resolver: Callable[[str], Path] | None = None,
) -> Tuple[List[KneeboardPage], List[str]]:
    src = (path_resolver or Path)(conf["system"]["pdf_output_dir"])
    max_pages = max_kneeboard_pages(airframe)
    pages: List[KneeboardPage] = []
    warnings: List[str] = []
    ignored_count = 0

    if not src.exists():
        return [], [f"Kneeboard order: PDF output folder does not exist: {src}"]

    canonical_brief = src / MANAGED_BRIEF_RELATIVE_PATH
    # One-time compatibility for output created before canonical brief storage
    # existed. New PDF generation always creates the managed source.
    brief_source = canonical_brief if canonical_brief.is_file() else src / RESERVED_BRIEF_PDF
    sources: list[Path] = []
    if brief_source.is_file():
        sources.append(brief_source)
    try:
        sources.extend(
            sorted(
                (
                    path for path in src.iterdir()
                    if path.is_file()
                    and path.name.casefold() != RESERVED_BRIEF_PDF
                    and _is_supported_source(path)
                ),
                key=lambda path: path.name.casefold(),
            )
        )
    except Exception as exc:
        return [], [f"Kneeboard order: failed to scan PDF output folder {src}: {exc}"]

    for path in sources:
        is_brief = path == brief_source
        for page in _source_pages(path, warnings, is_brief=is_brief):
            if len(pages) < max_pages:
                pages.append(page)
            else:
                ignored_count += 1

    if ignored_count:
        warnings.append(
            f"Kneeboard order: ignored {ignored_count} page(s) after the first {max_pages} for {airframe}."
        )

    if chart_service is not None and bms_cfg is not None:
        try:
            chart_slots, chart_warnings = chart_service.kneeboard_slots(conf, bms_cfg)
            warnings.extend(chart_warnings)
            pages.extend(
                KneeboardPage(
                    id=slot["id"],
                    kind="chart",
                    label=slot["label"],
                    path=Path(slot["path"]),
                    page_index=slot["page_index"],
                    available=bool(slot["available"]),
                    tooltip=str(slot.get("tooltip") or "") or None,
                    parking_placeholder=bool(slot["parking_placeholder"]),
                    aliases=tuple(slot.get("aliases") or ()),
                )
                for slot in chart_slots
            )
        except Exception as exc:
            warnings.append(f"Kneeboard order: failed to discover charts: {exc}")
    return pages, warnings


def resolve_kneeboard_sides(
    conf: configparser.ConfigParser,
    airframe: str,
    *,
    bms_cfg: Any | None = None,
    chart_service: Any | None = None,
    path_resolver: Callable[[str], Path] | None = None,
) -> Tuple[List[KneeboardPage], List[KneeboardPage], List[str]]:
    available, warnings = discover_kneeboard_pages(
        conf,
        airframe,
        bms_cfg=bms_cfg,
        chart_service=chart_service,
        path_resolver=path_resolver,
    )
    available_by_id = _pages_by_id_and_alias(available)
    left_tokens, right_tokens = parse_side_tokens(conf)
    sides: dict[str, list[KneeboardPage]] = {"left": [], "right": []}
    seen: set[str] = set()

    for side, tokens in (("left", left_tokens), ("right", right_tokens)):
        for page_id, included in tokens.items():
            page = available_by_id.get(page_id)
            if page is None:
                warnings.append(f"Kneeboard order: skipped missing page {page_id}.")
                continue
            if page.id in seen:
                continue
            seen.add(page.id)
            sides[side].append(page.with_included(included).with_side(side))

    for page in available:
        if page.id in seen:
            continue
        side = "left" if len(sides["left"]) <= len(sides["right"]) else "right"
        seen.add(page.id)
        sides[side].append(page.with_side(side))

    return _included_first(sides["left"]), _included_first(sides["right"]), warnings


def resolve_kneeboard_order(
    conf: configparser.ConfigParser,
    airframe: str,
    *,
    bms_cfg: Any | None = None,
    chart_service: Any | None = None,
    path_resolver: Callable[[str], Path] | None = None,
) -> Tuple[List[KneeboardPage], List[str]]:
    if (
        conf.has_section(KNEEBOARD_ORDER_SECTION)
        and KNEEBOARD_ORDER_KEY in conf[KNEEBOARD_ORDER_SECTION]
        and KNEEBOARD_ORDER_LEFT_KEY not in conf[KNEEBOARD_ORDER_SECTION]
        and KNEEBOARD_ORDER_RIGHT_KEY not in conf[KNEEBOARD_ORDER_SECTION]
    ):
        available, warnings = discover_kneeboard_pages(
            conf,
            airframe,
            bms_cfg=bms_cfg,
            chart_service=chart_service,
            path_resolver=path_resolver,
        )
        available_by_id = _pages_by_id_and_alias(available)
        ordered: list[KneeboardPage] = []
        seen: set[str] = set()
        for page_id, included in parse_order_tokens(conf).items():
            page = available_by_id.get(page_id)
            if page is None:
                warnings.append(f"Kneeboard order: skipped missing page {page_id}.")
                continue
            if page.id in seen:
                continue
            seen.add(page.id)
            ordered.append(page.with_included(included))
        ordered.extend(page for page in available if page.id not in seen)
        return _included_first(ordered), warnings
    left, right, warnings = resolve_kneeboard_sides(
        conf,
        airframe,
        bms_cfg=bms_cfg,
        chart_service=chart_service,
        path_resolver=path_resolver,
    )
    interleaved: list[KneeboardPage] = []
    for index in range(max(len(left), len(right))):
        if index < len(left):
            interleaved.append(left[index])
        if index < len(right):
            interleaved.append(right[index])
    return _included_first(interleaved), warnings


def serialize_order(pages: Iterable[Dict[str, Any]]) -> str:
    tokens: List[str] = []
    seen: set[str] = set()
    for page in pages:
        page_id = str(page.get("id") or "").strip()
        if not page_id or page_id in seen:
            continue
        seen.add(page_id)
        state = "on" if bool(page.get("included", True)) else "off"
        tokens.append(f"{page_id}:{state}")
    return ", ".join(tokens)


def _parse_raw_tokens(raw: str) -> Dict[str, bool]:
    parsed: Dict[str, bool] = {}
    for token in raw.split(","):
        item = token.strip()
        if not item:
            continue
        page_id, sep, state = item.rpartition(":")
        if not sep or state.lower() not in {"on", "off"} or not page_id:
            continue
        parsed[page_id] = state.lower() == "on"
    return parsed


def parse_order_tokens(conf: configparser.ConfigParser) -> Dict[str, bool]:
    if not conf.has_section(KNEEBOARD_ORDER_SECTION):
        return {}
    return _parse_raw_tokens(conf[KNEEBOARD_ORDER_SECTION].get(KNEEBOARD_ORDER_KEY, ""))


def parse_side_tokens(conf: configparser.ConfigParser) -> tuple[Dict[str, bool], Dict[str, bool]]:
    if not conf.has_section(KNEEBOARD_ORDER_SECTION):
        return {}, {}
    section = conf[KNEEBOARD_ORDER_SECTION]
    if KNEEBOARD_ORDER_LEFT_KEY in section or KNEEBOARD_ORDER_RIGHT_KEY in section:
        return (
            _parse_raw_tokens(section.get(KNEEBOARD_ORDER_LEFT_KEY, "")),
            _parse_raw_tokens(section.get(KNEEBOARD_ORDER_RIGHT_KEY, "")),
        )
    legacy = list(parse_order_tokens(conf).items())
    return dict(legacy[::2]), dict(legacy[1::2])


def save_kneeboard_sides(
    cfg: configparser.ConfigParser,
    pages_left: Iterable[Dict[str, Any]],
    pages_right: Iterable[Dict[str, Any]],
) -> None:
    if not cfg.has_section(KNEEBOARD_ORDER_SECTION):
        cfg[KNEEBOARD_ORDER_SECTION] = {}
    section = cfg[KNEEBOARD_ORDER_SECTION]
    section[KNEEBOARD_ORDER_LEFT_KEY] = serialize_order(pages_left)
    section[KNEEBOARD_ORDER_RIGHT_KEY] = serialize_order(pages_right)
    section.pop(KNEEBOARD_ORDER_KEY, None)


def save_kneeboard_order(cfg: configparser.ConfigParser, pages: Iterable[Dict[str, Any]]) -> None:
    if not cfg.has_section(KNEEBOARD_ORDER_SECTION):
        cfg[KNEEBOARD_ORDER_SECTION] = {}
    section = cfg[KNEEBOARD_ORDER_SECTION]
    section[KNEEBOARD_ORDER_KEY] = serialize_order(pages)
    section.pop(KNEEBOARD_ORDER_LEFT_KEY, None)
    section.pop(KNEEBOARD_ORDER_RIGHT_KEY, None)


def _is_supported_source(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS | {PDF_EXTENSION}


def _source_pages(path: Path, warnings: List[str], *, is_brief: bool = False) -> List[KneeboardPage]:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return [KneeboardPage(id=f"image:{path.name}", kind="image", label=path.name, path=path)]
    if ext != PDF_EXTENSION:
        return []
    try:
        with pymupdf.open(path) as document:
            page_count = len(document)
    except Exception as exc:
        warnings.append(f"Kneeboard order: skipped unreadable PDF {path.name}: {exc}")
        return []
    if page_count <= 0:
        warnings.append(f"Kneeboard order: skipped empty PDF {path.name}.")
        return []
    pages: List[KneeboardPage] = []
    for page_index in range(page_count):
        page_number = page_index + 1
        if is_brief:
            page_id = f"brief:{page_number}"
            kind = "brief"
            label = f"Briefing PDF page {page_number}"
        else:
            page_id = f"pdf:{path.name}:{page_number}"
            kind = "pdf"
            label = f"{path.name} page {page_number}"
        pages.append(KneeboardPage(page_id, kind, label, path, page_index))
    return pages


def _included_first(pages: List[KneeboardPage]) -> List[KneeboardPage]:
    return [page for page in pages if page.included] + [page for page in pages if not page.included]


def _pages_by_id_and_alias(
    pages: Iterable[KneeboardPage],
) -> dict[str, KneeboardPage]:
    result: dict[str, KneeboardPage] = {}
    for page in pages:
        result[page.id] = page
        for alias in page.aliases:
            result.setdefault(alias, page)
    return result


__all__ = [
    "KNEEBOARD_ORDER_SECTION",
    "KNEEBOARD_ORDER_KEY",
    "KNEEBOARD_ORDER_LEFT_KEY",
    "KNEEBOARD_ORDER_RIGHT_KEY",
    "KneeboardPage",
    "discover_kneeboard_pages",
    "max_kneeboard_pages",
    "parse_order_tokens",
    "parse_side_tokens",
    "resolve_kneeboard_order",
    "resolve_kneeboard_sides",
    "save_kneeboard_order",
    "save_kneeboard_sides",
    "serialize_order",
]

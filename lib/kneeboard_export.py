from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable

from PIL import Image
import pymupdf

from lib.kneeboard_order import KneeboardPage, resolve_kneeboard_sides
from lib.progress import ProgressCallback


logger = logging.getLogger("html_brief_log")
logger_ui = logging.getLogger("ui_logger")
SIDE_LIMIT = 16
F15_LIMIT = 16


def export_kneeboards(
    conf: Any,
    bms_conf: Any,
    *,
    chart_service: Any | None = None,
    progress: ProgressCallback | None = None,
) -> None:
    copy_to_kto = bms_conf.theater_config[bms_conf.theater]["copy_to_kto"] == "True"
    airframe = conf["bms"]["default_airframe"]
    if airframe not in {"F-16", "F-15"}:
        raise ValueError(f"Unsupported airframe for kneeboard export: {airframe}")
    output = bms_conf.theater_config[bms_conf.theater]["target_folder"]
    left, right, warnings = resolve_kneeboard_sides(
        conf,
        airframe,
        bms_cfg=bms_conf,
        chart_service=chart_service,
    )
    for warning in warnings:
        logger_ui.warning(warning)
    left = [page for page in left if page.included and page.available]
    right = [page for page in right if page.included and page.available]

    with tempfile.TemporaryDirectory() as temp_dir:
        if airframe == "F-16":
            _export_f16(
                left,
                right,
                temp_dir,
                output,
                bms_conf,
                copy_to_kto,
                progress=progress,
            )
        else:
            interleaved: list[KneeboardPage] = []
            for index in range(max(len(left), len(right))):
                if index < len(left):
                    interleaved.append(left[index])
                if index < len(right):
                    interleaved.append(right[index])
            if len(interleaved) > F15_LIMIT:
                logger_ui.warning(
                    "Kneeboard order: F-15 export omitted %d page(s) after the first %d.",
                    len(interleaved) - F15_LIMIT,
                    F15_LIMIT,
                )
            rendered = _render_export_pages(
                interleaved[:F15_LIMIT],
                temp_dir,
                prefix="f15",
                progress=progress,
            )
            _export_f15(
                rendered,
                temp_dir,
                output,
                bms_conf,
                copy_to_kto,
                progress=progress,
            )


def _export_f16(
    left: list[KneeboardPage],
    right: list[KneeboardPage],
    temp_dir: str,
    output: str,
    bms_conf: Any,
    copy_to_kto: bool,
    *,
    progress: ProgressCallback | None = None,
) -> None:
    if len(left) > SIDE_LIMIT:
        logger_ui.warning("Kneeboard order: F-16 left side omitted %d page(s) after the first %d.", len(left) - SIDE_LIMIT, SIDE_LIMIT)
    if len(right) > SIDE_LIMIT:
        logger_ui.warning("Kneeboard order: F-16 right side omitted %d page(s) after the first %d.", len(right) - SIDE_LIMIT, SIDE_LIMIT)
    limited_left = left[:SIDE_LIMIT]
    limited_right = right[:SIDE_LIMIT]
    render_total = len(limited_left) + len(limited_right)
    left_rendered = _render_export_pages(
        limited_left,
        temp_dir,
        prefix="left",
        progress=progress,
        progress_offset=0,
        progress_total=render_total,
    )
    right_rendered = _render_export_pages(
        limited_right,
        temp_dir,
        prefix="right",
        progress=progress,
        progress_offset=len(limited_left),
        progress_total=render_total,
    )
    output_count = max(len(left_rendered), len(right_rendered))
    for index in range(output_count):
        if progress is not None:
            progress(
                title="Exporting kneeboard",
                stage="kneeboard_write",
                message=f"Writing kneeboard DDS {index + 1} of {output_count}...",
                note=None,
                current=index,
                total=output_count,
                can_cancel=False,
            )
        target = Path(output) / f"{7982 + index}.dds"
        kto_target = Path(bms_conf.kto_target_folder) / f"{7982 + index}.dds"
        _backup_targets(target, kto_target, copy_to_kto)
        joined = Image.new("RGBA", (2048, 2048), "white")
        try:
            if index < len(left_rendered):
                with Image.open(Path(temp_dir) / left_rendered[index]) as image:
                    joined.paste(image.resize((1024, 2048)), (0, 0))
            if index < len(right_rendered):
                with Image.open(Path(temp_dir) / right_rendered[index]) as image:
                    joined.paste(image.resize((1024, 2048)), (1024, 0))
            joined.save(target)
            if copy_to_kto:
                joined.save(kto_target)
        finally:
            joined.close()
        if progress is not None:
            progress(current=index + 1, total=output_count)


def _export_f15(
    rendered: list[str],
    temp_dir: str,
    output: str,
    bms_conf: Any,
    copy_to_kto: bool,
    *,
    progress: ProgressCallback | None = None,
) -> None:
    output_count = len(rendered)
    for index, source_name in enumerate(rendered):
        if progress is not None:
            progress(
                title="Exporting kneeboard",
                stage="kneeboard_write",
                message=f"Writing kneeboard DDS {index + 1} of {output_count}...",
                note=None,
                current=index,
                total=output_count,
                can_cancel=False,
            )
        target = Path(output) / f"{1403 + index}.dds"
        kto_target = Path(bms_conf.kto_target_folder) / f"{1403 + index}.dds"
        _backup_targets(target, kto_target, copy_to_kto)
        joined = Image.new("RGBA", (2048, 2048), "white")
        try:
            with Image.open(Path(temp_dir) / source_name) as image:
                joined.paste(image.resize((1024, 2048)), (1024, 0))
            joined.save(target)
            if copy_to_kto:
                joined.save(kto_target)
        finally:
            joined.close()
        if progress is not None:
            progress(current=index + 1, total=output_count)


def _backup_targets(target: Path, kto_target: Path, copy_to_kto: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        logger_ui.info("Backing up %s to %s.bkp...", target, target)
        shutil.copyfile(target, str(target) + ".bkp")
    if copy_to_kto:
        kto_target.parent.mkdir(parents=True, exist_ok=True)
        if kto_target.is_file():
            logger_ui.info("Backing up %s to %s.bkp...", kto_target, kto_target)
            shutil.copyfile(kto_target, str(kto_target) + ".bkp")


def _render_export_pages(
    export_pages: Iterable[KneeboardPage],
    temp_dir: str,
    *,
    prefix: str = "page",
    progress: ProgressCallback | None = None,
    progress_offset: int = 0,
    progress_total: int | None = None,
) -> list[str]:
    export_pages = list(export_pages)
    total = len(export_pages) if progress_total is None else progress_total
    pages_conv: list[str] = []
    for index, page_ref in enumerate(export_pages):
        completed = progress_offset + index
        if progress is not None:
            progress(
                title="Exporting kneeboard",
                stage="kneeboard_render",
                message=f"Converting kneeboard page {completed + 1} of {total}...",
                note=None,
                current=completed,
                total=total,
                can_cancel=False,
            )
        out_name = f"{prefix}_{index:02d}.png"
        out_path = Path(temp_dir) / out_name
        if page_ref.kind == "image":
            logger_ui.info("Processing an image file: %s", page_ref.path.name)
            with Image.open(page_ref.path) as source_img:
                resized = source_img.resize((1024, 2048))
                try:
                    resized.save(out_path)
                finally:
                    resized.close()
        else:
            page_number = 1 if page_ref.page_index is None else page_ref.page_index + 1
            logger_ui.info("Processing PDF page: %s page %d", page_ref.path.name, page_number)
            with pymupdf.open(page_ref.path) as document:
                if page_ref.page_index is None or page_ref.page_index >= len(document):
                    logger_ui.warning("Kneeboard order: skipped missing PDF page %s.", page_ref.id)
                    if progress is not None:
                        progress(current=completed + 1, total=total)
                    continue
                document[page_ref.page_index].get_pixmap(dpi=150).save(out_path)
        pages_conv.append(out_name)
        if progress is not None:
            progress(current=completed + 1, total=total)
    return pages_conv


__all__ = ["export_kneeboards"]

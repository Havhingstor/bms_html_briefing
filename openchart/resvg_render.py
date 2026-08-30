"""Render OpenChart SVG pages through resvg with bundled fonts."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from importlib import resources
from io import BytesIO
import math
from typing import Sequence
import xml.etree.ElementTree as ET

from PIL import Image
import resvg_py


A4_WIDTH_MM = 210.0
A4_HEIGHT_MM = 297.0
DEFAULT_RASTER_DPI = 150.0
MM_PER_INCH = 25.4
FONT_FILENAMES = ("NimbusSans-Regular.otf", "NimbusSans-Bold.otf")


RESVG_OPTICAL_CENTER_DY = "0.12em"
RESVG_OPTICALLY_CENTERED_TEXT_CLASSES = frozenset(
    (
        "graticule-label",
        "ils-feather-heading",
        "minimap-elevation-label",
        "minimap-runway-label",
        "minimap-tower-text",
        "msa-altitude",
        "msa-bearing",
        "north-label",
        "parking-number-text",
        "parking-number-text-unavailable",
        "peak-label",
        "range-ring-label",
        "runway-dimensions",
        "runway-end-text",
        "taxiway-label",
        "topographic-navaid-label",
        "tower-text",
    )
)


@dataclass(frozen=True)
class PdfRenderOptions:
    """Raster resolution used for resvg PNG and PDF output."""

    dpi: float = DEFAULT_RASTER_DPI


@dataclass(frozen=True)
class PdfBackend:
    """Stable identity of the packaged SVG rendering backend."""

    version: str


class PdfBackendError(RuntimeError):
    """Structured failure raised by the resvg/Pillow adapter."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def prepare_svg_for_resvg(svg_payload: bytes) -> bytes:
    """Apply resvg-only optical text centering without changing source SVGs."""

    root = ET.fromstring(svg_payload)
    for element in root.iter():
        if not element.tag.endswith("text"):
            continue
        if (
            element.attrib.get("class")
            not in RESVG_OPTICALLY_CENTERED_TEXT_CLASSES
        ):
            continue
        if "dy" not in element.attrib:
            element.set("dy", RESVG_OPTICAL_CENTER_DY)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def inspect_pdf_backend(options: PdfRenderOptions | None = None) -> PdfBackend:
    """Return the pinned resvg and Pillow backend identity."""

    settings = validate_pdf_options(options)
    return PdfBackend(
        version=(
            f"resvg_py {resvg_py.__version__} "
            f"(resvg {resvg_py.__resvg_version__}); "
            f"Pillow {Image.__version__}; {settings.dpi:g} dpi; "
            "Nimbus Sans bundled"
        )
    )


def render_svg_page_png(
    svg_page: bytes,
    options: PdfRenderOptions | None = None,
) -> bytes:
    """Rasterize one self-contained A4 SVG page to an opaque PNG."""

    settings = validate_pdf_options(options)
    _validate_svg_page(svg_page, 0)
    width = round(A4_WIDTH_MM / MM_PER_INCH * settings.dpi)
    height = round(A4_HEIGHT_MM / MM_PER_INCH * settings.dpi)
    try:
        prepared = prepare_svg_for_resvg(svg_page).decode("utf-8")
        font_resources = resources.files("openchart.fonts")
        with ExitStack() as stack:
            font_files = [
                str(
                    stack.enter_context(
                        resources.as_file(font_resources.joinpath(filename))
                    )
                )
                for filename in FONT_FILENAMES
            ]
            payload = resvg_py.svg_to_bytes(
                svg_string=prepared,
                background="#ffffff",
                skip_system_fonts=True,
                width=width,
                height=height,
                dpi=settings.dpi,
                font_family="Nimbus Sans",
                sans_serif_family="Nimbus Sans",
                font_files=font_files,
                shape_rendering="geometric_precision",
                text_rendering="optimize_legibility",
                image_rendering="optimize_quality",
            )
    except (ET.ParseError, UnicodeDecodeError, ValueError, OSError) as exc:
        raise PdfBackendError(
            f"resvg PNG conversion failed: {exc}",
            code="svg_conversion_failed",
        ) from exc
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise PdfBackendError(
            "resvg did not produce a complete PNG image",
            code="invalid_png_output",
        )
    return payload


def render_svg_pages_pdf(
    svg_pages: Sequence[bytes],
    options: PdfRenderOptions | None = None,
    *,
    backend: PdfBackend | None = None,
) -> tuple[bytes, PdfBackend]:
    """Rasterize SVG pages with resvg and assemble one ordered A4 PDF."""

    settings = validate_pdf_options(options)
    if not svg_pages:
        raise PdfBackendError(
            "a PDF document needs at least one SVG page",
            code="empty_pdf_document",
        )
    for ordinal, payload in enumerate(svg_pages):
        _validate_svg_page(payload, ordinal)
    selected_backend = backend or inspect_pdf_backend(settings)
    png_pages = tuple(render_svg_page_png(payload, settings) for payload in svg_pages)
    return render_png_pages_pdf(
        png_pages,
        settings,
        backend=selected_backend,
    )


def render_png_pages_pdf(
    png_pages: Sequence[bytes],
    options: PdfRenderOptions | None = None,
    *,
    backend: PdfBackend | None = None,
) -> tuple[bytes, PdfBackend]:
    """Assemble already-rendered A4 PNG pages into one ordered PDF."""

    settings = validate_pdf_options(options)
    if not png_pages:
        raise PdfBackendError(
            "a PDF document needs at least one PNG page",
            code="empty_pdf_document",
        )
    for ordinal, payload in enumerate(png_pages):
        _validate_png_page(payload, ordinal)
    selected_backend = backend or inspect_pdf_backend(settings)

    images: list[Image.Image] = []
    try:
        for payload in png_pages:
            with Image.open(BytesIO(payload)) as source:
                source.load()
                images.append(source.convert("RGB"))

        width_px, height_px = images[0].size
        x_resolution = width_px * MM_PER_INCH / A4_WIDTH_MM
        y_resolution = height_px * MM_PER_INCH / A4_HEIGHT_MM
        output = BytesIO()
        images[0].save(
            output,
            format="PDF",
            save_all=True,
            append_images=images[1:],
            dpi=(x_resolution, y_resolution),
            quality=95,
            subsampling=0,
            creationDate=False,
            modDate=False,
        )
        pdf_bytes = output.getvalue()
    except (OSError, ValueError) as exc:
        raise PdfBackendError(
            f"cannot assemble resvg raster pages as PDF: {exc}",
            code="pdf_conversion_failed",
        ) from exc
    finally:
        for image in images:
            image.close()

    if not pdf_bytes.startswith(b"%PDF-") or b"%%EOF" not in pdf_bytes[-1024:]:
        raise PdfBackendError(
            "Pillow did not produce a complete PDF document",
            code="invalid_pdf_output",
        )
    return pdf_bytes, selected_backend


def validate_pdf_options(options: PdfRenderOptions | None) -> PdfRenderOptions:
    if options is None:
        return PdfRenderOptions()
    if not isinstance(options, PdfRenderOptions):
        raise PdfBackendError(
            "options must be PdfRenderOptions or None",
            code="invalid_pdf_options",
        )
    dpi = options.dpi
    if (
        isinstance(dpi, bool)
        or not isinstance(dpi, (int, float))
        or not math.isfinite(float(dpi))
        or dpi <= 0
    ):
        raise PdfBackendError(
            "PDF raster resolution must be a positive finite number",
            code="invalid_pdf_options",
        )
    return options


def _validate_svg_page(payload: bytes, ordinal: int) -> None:
    if not isinstance(payload, bytes) or not payload.strip():
        raise PdfBackendError(
            f"SVG page {ordinal} must be non-empty bytes",
            code="invalid_svg_page",
        )


def _validate_png_page(payload: bytes, ordinal: int) -> None:
    if (
        not isinstance(payload, bytes)
        or not payload.startswith(b"\x89PNG\r\n\x1a\n")
    ):
        raise PdfBackendError(
            f"PNG page {ordinal} must be complete PNG bytes",
            code="invalid_png_output",
        )


__all__ = [
    "DEFAULT_RASTER_DPI",
    "PdfRenderOptions",
    "inspect_pdf_backend",
    "render_png_pages_pdf",
    "render_svg_page_png",
    "render_svg_pages_pdf",
]

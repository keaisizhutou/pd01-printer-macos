"""Deterministic raster preparation for image and vertically mixed receipt layouts."""
from __future__ import annotations

import io
from pathlib import Path
from PIL import Image, UnidentifiedImageError
from printer_faults import fault, normalize_error

PAPER_WIDTH = 384
MAX_RASTER_HEIGHT = 4000
MAX_SOURCE_PIXELS = 16_000_000
MAX_LAYOUT_BLOCKS = 32


def check_height(height: int) -> None:
    if height > MAX_RASTER_HEIGHT:
        raise fault("paper_length_exceeded")


def png_bytes(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def image_raster(path: Path, width: int = PAPER_WIDTH) -> Image.Image:
    if not isinstance(width, int) or isinstance(width, bool) or not 1 <= width <= PAPER_WIDTH:
        raise fault("layout_invalid")
    try:
        with Image.open(path) as source:
            if source.width * source.height > MAX_SOURCE_PIXELS:
                raise fault("image_too_large")
            if getattr(source, "n_frames", 1) != 1:
                raise fault("image_multiple_frames")
            height = max(1, round(source.height * width / source.width))
            check_height(height)
            rgba = source.convert("RGBA")
    except FileNotFoundError:
        raise fault("image_not_found") from None
    except (UnidentifiedImageError, Image.DecompressionBombError):
        raise fault("image_invalid") from None
    except OSError as error:
        if error.errno:
            raise normalize_error(error) from error
        raise fault("image_invalid") from error
    white = Image.new("RGBA", rgba.size, "white")
    white.alpha_composite(rgba)
    gray = white.convert("L").resize((width, height), Image.Resampling.LANCZOS)
    return gray.point(lambda value: 0 if value < 160 else 255, mode="1")


def mixed_raster(blocks: list, base_dir: Path, render_text) -> tuple[bytes, int, int, str]:
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= MAX_LAYOUT_BLOCKS:
        raise fault("layout_invalid")
    pieces = []
    texts = []
    height = 0
    for block in blocks:
        if not isinstance(block, dict):
            raise fault("layout_invalid")
        kind = block.get("type")
        align = block.get("align", "center")
        if align not in ("left", "center", "right"):
            raise fault("alignment_invalid")
        if kind == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise fault("layout_invalid")
            data, _, _, _ = render_text(text, block.get("font_size", 36), align)
            with Image.open(io.BytesIO(data)) as source:
                piece = source.copy()
            texts.append(text)
        elif kind == "image":
            path = block.get("path")
            if not isinstance(path, str) or not path:
                raise fault("layout_invalid")
            path = Path(path).expanduser()
            piece = image_raster(path if path.is_absolute() else base_dir / path, block.get("width", PAPER_WIDTH))
        elif kind == "spacer":
            gap = block.get("height", 12)
            if not isinstance(gap, int) or isinstance(gap, bool) or not 1 <= gap <= 200:
                raise fault("layout_invalid")
            piece = Image.new("1", (PAPER_WIDTH, gap), 255)
        else:
            raise fault("layout_invalid")
        height += piece.height
        check_height(height)
        pieces.append((piece, align))
    if not any(block.get("type") != "spacer" for block in blocks):
        raise fault("layout_invalid")
    canvas = Image.new("1", (PAPER_WIDTH, height), 255)
    y = 0
    for piece, align in pieces:
        x = 0 if align == "left" else PAPER_WIDTH - piece.width if align == "right" else (PAPER_WIDTH - piece.width) // 2
        canvas.paste(piece, (x, y))
        y += piece.height
    return png_bytes(canvas), PAPER_WIDTH, height, "\n".join(texts)

"""Decode and verify bounded document images without fetching remote resources."""

import base64
import binascii
import struct
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

from triage.errors import TriageError

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
FORMATS = {"PNG", "JPEG", "WEBP", "TIFF", "GIF"}


@dataclass(frozen=True)
class PreparedImage:
    data_url: str


def prepare_image(content: str) -> PreparedImage:
    """Validate actual image bytes and normalize orientation and unsupported encodings."""
    if len(content) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise _too_large()
    try:
        raw = base64.b64decode(content, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise TriageError(
            "INVALID_IMAGE", "Content must be valid base64 image bytes.", 422
        ) from exc
    if len(raw) > MAX_IMAGE_BYTES:
        raise _too_large()
    try:
        with Image.open(BytesIO(raw)) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise _too_large()
            if image.format not in FORMATS or getattr(image, "n_frames", 1) != 1:
                raise TriageError(
                    "UNSUPPORTED_IMAGE",
                    "Use a single-page PNG, JPEG, WEBP, TIFF or GIF image.",
                    422,
                )
            image.verify()
        with Image.open(BytesIO(raw)) as image:
            image.load()
            transparent = "A" in image.getbands() or "transparency" in image.info
            mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(
                image.format or ""
            )
            if (
                mime is not None
                and image.getexif().get(274, 1) == 1
                and not transparent
                and image.mode != "CMYK"
            ):
                return PreparedImage(f"data:{mime};base64," + base64.b64encode(raw).decode("ascii"))
            original_format = image.format
            ImageOps.exif_transpose(image, in_place=True)
            mime = "image/jpeg" if original_format == "JPEG" else "image/png"
            with image.convert("RGBA") as rgba, Image.new("RGB", image.size, "white") as rgb:
                with rgba.getchannel("A") as alpha:
                    rgb.paste(rgba, mask=alpha)
                with BytesIO() as output:
                    rgb.save(
                        output,
                        format="JPEG" if mime == "image/jpeg" else "PNG",
                        **({"quality": 95} if mime == "image/jpeg" else {}),
                    )
                    encoded = output.getvalue()
        if len(encoded) > MAX_IMAGE_BYTES:
            raise _too_large()
        return PreparedImage(f"data:{mime};base64," + base64.b64encode(encoded).decode("ascii"))
    except Image.DecompressionBombError as exc:
        raise _too_large() from exc
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
        EOFError,
        OverflowError,
        struct.error,
    ) as exc:
        raise TriageError(
            "INVALID_IMAGE", "Content is not a complete supported document image.", 422
        ) from exc


def _too_large() -> TriageError:
    return TriageError(
        "IMAGE_TOO_LARGE",
        "Image must be at most 10 MiB before and after normalization, and 20 million pixels.",
        413,
    )

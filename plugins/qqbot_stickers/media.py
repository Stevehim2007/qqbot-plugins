"""Pure-stdlib image inspection helpers.

This module is intentionally independent from AstrBot.  Sticker data is
untrusted: callers must use the magic-byte and dimension checks here instead
of trusting a file extension, a component MIME type or an HTTP content type.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any

IMAGE_PATH_EXTENSIONS = (".jpg", ".png", ".gif", ".webp", ".bmp", ".avif")
SUPPORTED_MIME_TYPES = (
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/bmp",
    "image/avif",
)


class ImageValidationError(ValueError):
    """Image bytes failed a bounded, local safety check."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class ImageInfo:
    data: bytes
    mime: str
    extension: str
    width: int
    height: int
    size: int


def detect_image_magic(data: bytes) -> tuple[str, str] | None:
    """Return ``(mime, extension)`` based only on leading magic bytes."""

    if not isinstance(data, (bytes, bytearray, memoryview)):
        return None
    raw = bytes(data)
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if len(raw) >= 12 and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if raw.startswith(b"BM"):
        return "image/bmp", ".bmp"
    if len(raw) >= 12 and raw[4:12] in {b"ftypavif", b"ftypavis"}:
        return "image/avif", ".avif"
    return None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    offset = 2
    length = len(data)
    while offset + 1 < length:
        if data[offset] != 0xFF:
            offset += 1
            continue
        # Skip fill bytes.
        while offset + 1 < length and data[offset + 1] == 0xFF:
            offset += 1
        if offset + 1 >= length:
            return None
        marker = data[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > length:
            return None
        segment_length = int.from_bytes(data[offset : offset + 2], "big")
        if segment_length < 2 or offset + segment_length > length:
            return None
        if marker in sof_markers:
            if segment_length < 7:
                return None
            height = int.from_bytes(data[offset + 3 : offset + 5], "big")
            width = int.from_bytes(data[offset + 5 : offset + 7], "big")
            return width, height
        offset += segment_length
    return None


def _avif_dimensions(data: bytes) -> tuple[int, int] | None:
    # An AVIF/HEIF ``ispe`` property contains a 4-byte FullBox header then
    # width and height as big-endian uint32 values.  There can be several
    # ispe boxes; the first decoded image property is sufficient for bounds.
    start = 0
    while True:
        index = data.find(b"ispe", start)
        if index < 0:
            return None
        if index + 16 <= len(data):
            width = int.from_bytes(data[index + 8 : index + 12], "big")
            height = int.from_bytes(data[index + 12 : index + 16], "big")
            if width and height:
                return width, height
        start = index + 4


def image_dimensions(data: bytes, mime: str) -> tuple[int, int] | None:
    """Parse bounded dimensions for the six supported image containers."""

    if not isinstance(data, (bytes, bytearray, memoryview)):
        return None
    raw = bytes(data)
    try:
        if mime == "image/png":
            if len(raw) < 24 or raw[12:16] != b"IHDR":
                return None
            width = int.from_bytes(raw[16:20], "big")
            height = int.from_bytes(raw[20:24], "big")
            return width, height
        if mime == "image/gif":
            if len(raw) < 10:
                return None
            return (
                int.from_bytes(raw[6:8], "little"),
                int.from_bytes(raw[8:10], "little"),
            )
        if mime == "image/bmp":
            if len(raw) < 26:
                return None
            width = int.from_bytes(raw[18:22], "little", signed=True)
            height = int.from_bytes(raw[22:26], "little", signed=True)
            return abs(width), abs(height)
        if mime == "image/jpeg":
            return _jpeg_dimensions(raw)
        if mime == "image/webp":
            if len(raw) < 30:
                return None
            chunk_type = raw[12:16]
            if chunk_type == b"VP8X":
                width = 1 + int.from_bytes(raw[24:27], "little")
                height = 1 + int.from_bytes(raw[27:30], "little")
                return width, height
            if chunk_type == b"VP8 " and len(raw) >= 30:
                # Lossy WebP stores 14-bit dimensions in the key-frame header.
                if raw[23:26] != b"\x9d\x01\x2a":
                    return None
                width = int.from_bytes(raw[26:28], "little") & 0x3FFF
                height = int.from_bytes(raw[28:30], "little") & 0x3FFF
                return width, height
            if chunk_type == b"VP8L" and len(raw) >= 25:
                if raw[20] != 0x2F:
                    return None
                bits = int.from_bytes(raw[21:25], "little")
                width = (bits & 0x3FFF) + 1
                height = ((bits >> 14) & 0x3FFF) + 1
                return width, height
            return None
        if mime == "image/avif":
            return _avif_dimensions(raw)
    except (IndexError, OverflowError, ValueError):
        return None
    return None


def inspect_image_bytes(
    data: Any,
    *,
    max_bytes: int,
    max_dimension: int = 4096,
    max_pixels: int = 16_777_216,
    min_dimension: int = 1,
) -> ImageInfo:
    """Validate size, magic bytes and dimensions without decoding pixels."""

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ImageValidationError("invalid_image", "image payload is not bytes")
    raw = bytes(data)
    if not raw:
        raise ImageValidationError("invalid_image", "empty image payload")
    if len(raw) > max(1, int(max_bytes)):
        raise ImageValidationError("too_large", "image exceeds size limit")
    detected = detect_image_magic(raw)
    if detected is None:
        raise ImageValidationError("invalid_image", "unsupported image magic")
    mime, extension = detected
    dimensions = image_dimensions(raw, mime)
    if dimensions is None:
        raise ImageValidationError("invalid_image", "unreadable image dimensions")
    width, height = dimensions
    width = int(width)
    height = int(height)
    if (
        width < max(1, int(min_dimension))
        or height < max(1, int(min_dimension))
        or width > max(1, int(max_dimension))
        or height > max(1, int(max_dimension))
    ):
        raise ImageValidationError("invalid_dimensions", "image dimensions out of bounds")
    if width * height > max(1, int(max_pixels)):
        raise ImageValidationError("invalid_dimensions", "image pixel count out of bounds")
    return ImageInfo(
        data=raw,
        mime=mime,
        extension=extension,
        width=width,
        height=height,
        size=len(raw),
    )


def decode_base64_payload(raw: Any) -> bytes | None:
    """Decode the common base64 shapes returned by AstrBot components."""

    if isinstance(raw, (bytes, bytearray, memoryview)):
        return bytes(raw)
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    if value.startswith("data:"):
        if "," not in value:
            return None
        value = value.split(",", 1)[1]
    value = value.removeprefix("base64://").strip()
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        try:
            decoded = base64.b64decode(value)
        except (binascii.Error, ValueError):
            return None
    return decoded or None

"""Small header-only image fixtures used by stdlib unit tests."""

from __future__ import annotations


def png(width: int = 16, height: int = 16, *, tag: int = 0) -> bytes:
    data = bytearray(b"\x89PNG\r\n\x1a\n")
    data.extend(b"\x00\x00\x00\rIHDR")
    data.extend(int(width).to_bytes(4, "big"))
    data.extend(int(height).to_bytes(4, "big"))
    data.extend(b"\x08\x06\x00\x00\x00")
    data.extend(tag.to_bytes(4, "big", signed=False))
    data.extend(b"\x00" * 8)
    return bytes(data)


def gif(width: int = 16, height: int = 16, *, tag: int = 0) -> bytes:
    return (
        b"GIF89a"
        + int(width).to_bytes(2, "little")
        + int(height).to_bytes(2, "little")
        + bytes([0x80, 0, 0])
        + int(tag & 0xFFFF).to_bytes(2, "little")
        + b";"
    )


def bmp(width: int = 16, height: int = 16) -> bytes:
    data = bytearray(30)
    data[0:2] = b"BM"
    data[18:22] = int(width).to_bytes(4, "little", signed=True)
    data[22:26] = int(height).to_bytes(4, "little", signed=True)
    return bytes(data)


def jpeg(width: int = 16, height: int = 16) -> bytes:
    return (
        b"\xff\xd8\xff\xe0\x00\x04\x00\x00"
        + b"\xff\xc0\x00\x11\x08"
        + int(height).to_bytes(2, "big")
        + int(width).to_bytes(2, "big")
        + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
        + b"\x00" * 9
    )


def webp_vp8x(width: int = 16, height: int = 16) -> bytes:
    data = bytearray(30)
    data[0:4] = b"RIFF"
    data[4:8] = (22).to_bytes(4, "little")
    data[8:12] = b"WEBP"
    data[12:16] = b"VP8X"
    data[16:20] = (10).to_bytes(4, "little")
    data[20] = 0
    data[24:27] = (int(width) - 1).to_bytes(3, "little")
    data[27:30] = (int(height) - 1).to_bytes(3, "little")
    return bytes(data)


def webp_vp8l(width: int = 16, height: int = 16) -> bytes:
    data = bytearray(30)
    data[0:4] = b"RIFF"
    data[4:8] = (22).to_bytes(4, "little")
    data[8:12] = b"WEBP"
    data[12:16] = b"VP8L"
    data[16:20] = (10).to_bytes(4, "little")
    data[20] = 0x2F
    bits = (int(width) - 1) | ((int(height) - 1) << 14)
    data[21:25] = bits.to_bytes(4, "little")
    return bytes(data)


def avif(width: int = 16, height: int = 16) -> bytes:
    data = bytearray(b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1")
    data.extend(b"\x00\x00\x00\x14ispe")
    data.extend(b"\x00\x00\x00\x00")
    data.extend(int(width).to_bytes(4, "big"))
    data.extend(int(height).to_bytes(4, "big"))
    return bytes(data)

"""Bounded, stdlib-only image search engine.

The module is deliberately free of AstrBot imports.  It implements:

* Bing Images HTML parsing (primary) and Baidu Images JSON parsing (fallback);
* conservative relevance filtering, URL normalisation and de-duplication;
* per-hop URL/DNS validation for search pages, redirects and image downloads;
* image Content-Type, magic-byte and size validation;
* bounded candidate/download counts and temporary-file creation;
* deterministic natural-language intent detection and cooldown bookkeeping.

Nothing here calls a model, a browser or a non-stdlib HTTP library.
"""

from __future__ import annotations

import html as html_lib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import tempfile
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable

BING_IMAGES_ENDPOINT = "https://www.bing.com/images/search"
BING_IMAGES_REFERER = "https://www.bing.com/images/search"
BAIDU_IMAGES_ENDPOINT = "https://image.baidu.com/search/acjson"
BAIDU_IMAGES_REFERER = "https://image.baidu.com/"

IMAGE_SEARCH_MAX_CANDIDATES = 200
IMAGE_SEARCH_MAX_CANDIDATE_URLS = 15
IMAGE_SEARCH_MAX_RESULTS_PER_COMMAND = 5
IMAGE_SEARCH_MAX_REDIRECTS = 3
IMAGE_SEARCH_MAX_URL_CHARS = 4096
IMAGE_SEARCH_MAX_KEYWORD_CHARS_HARD_LIMIT = 300
IMAGE_SEARCH_MAX_RESPONSE_BYTES_HARD_LIMIT = 5_000_000
IMAGE_SEARCH_MAX_DOWNLOAD_BYTES_HARD_LIMIT = 20_971_520
IMAGE_SEARCH_DEFAULT_TIMEOUT = 12.0
IMAGE_SEARCH_DEFAULT_MAX_RESPONSE_BYTES = 2_000_000
IMAGE_SEARCH_DEFAULT_DOWNLOAD_TIMEOUT = 20.0
IMAGE_SEARCH_DEFAULT_DOWNLOAD_MAX_BYTES = 8_000_000
IMAGE_SEARCH_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 QQBotStudio-ImageSearch/2.0"
)
IMAGE_PATH_EXTENSIONS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".avif",
)
ALLOWED_IMAGE_CONTENT_TYPES = frozenset(
    {
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/bmp",
        "image/x-ms-bmp",
        "image/avif",
    }
)
OCTET_STREAM_TYPES = frozenset({"application/octet-stream", "binary/octet-stream"})
BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "instance-data",
    }
)
BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".lan",
    ".corp",
    ".home.arpa",
)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HOSTNAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z")
_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([\w.+-]+)", re.IGNORECASE)
_META_CHARSET_RE = re.compile(
    rb"<meta[^>]+charset\s*=\s*[\"']?([\w.+-]+)",
    re.IGNORECASE,
)

REASON_TEXT = {
    "invalid_url": "图片搜索地址不合法。",
    "invalid_keyword": "搜索关键词不合法。",
    "blocked_host": "图片搜索目标不是公开地址，已拒绝。",
    "blocked_ip": "图片搜索目标不是公开地址，已拒绝。",
    "dns_failed": "无法解析图片搜索域名。",
    "network": "图片搜索连接失败，请稍后再试。",
    "timeout": "图片搜索超时，请稍后再试。",
    "too_many_redirects": "图片搜索跳转次数过多。",
    "bad_redirect": "图片搜索跳转到了不安全的地址。",
    "http_error": "图片搜索服务返回了错误状态。",
    "too_large": "图片或搜索结果超过大小限制。",
    "bad_content_type": "图片搜索返回了不支持的内容类型。",
    "bad_encoding": "图片搜索返回了不支持的压缩格式。",
    "bad_magic": "下载内容不是受支持的图片格式。",
    "empty": "图片响应为空。",
    "no_results": "没有找到合适的图片。",
}


class ImageSearchError(RuntimeError):
    """Failure carrying a stable reason code for safe user-facing replies."""

    def __init__(self, reason: str) -> None:
        super().__init__(str(reason))
        self.reason = str(reason)


def safe_error_message(reason: str) -> str:
    return REASON_TEXT.get(str(reason or ""), "图片搜索失败，请稍后再试。")


def is_public_ip(value: object) -> bool:
    """True only for globally routable unicast addresses."""

    try:
        ip = ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    return bool(ip.is_global)


def _normalized_host(raw_host: str) -> str:
    host = str(raw_host or "").strip().lower().rstrip(".")
    if not host:
        raise ImageSearchError("invalid_url")
    if host in BLOCKED_HOSTS or host.endswith(BLOCKED_HOST_SUFFIXES):
        raise ImageSearchError("blocked_host")
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip = None
    if ip is not None:
        if not is_public_ip(ip):
            raise ImageSearchError("blocked_ip")
        return str(ip)
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ImageSearchError("invalid_url") from exc
    if not _HOSTNAME_RE.fullmatch(host):
        raise ImageSearchError("invalid_url")
    if "." not in host:
        # Single-label names are almost always intranet names.  Public DNS
        # names used for image search are FQDN-like, so reject conservatively.
        raise ImageSearchError("blocked_host")
    return host


def normalize_http_url(
    value: Any,
    *,
    require_image_extension: bool = False,
    extensions: tuple[str, ...] = IMAGE_PATH_EXTENSIONS,
) -> str:
    """Return a normalised public HTTP(S) URL or raise :class:`ImageSearchError`."""

    if not isinstance(value, str):
        raise ImageSearchError("invalid_url")
    raw = html_lib.unescape(value).strip()
    if not raw or len(raw) > IMAGE_SEARCH_MAX_URL_CHARS:
        raise ImageSearchError("invalid_url")
    if raw.startswith("//"):
        raw = "https:" + raw
    if "\\" in raw or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ImageSearchError("invalid_url")
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ImageSearchError("invalid_url") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ImageSearchError("invalid_url")
    if not parsed.netloc or not parsed.hostname:
        raise ImageSearchError("invalid_url")
    if parsed.username is not None or parsed.password is not None:
        raise ImageSearchError("invalid_url")
    if port is not None and not 1 <= port <= 65535:
        raise ImageSearchError("invalid_url")
    host = _normalized_host(parsed.hostname)
    if require_image_extension:
        path = urllib.parse.unquote(parsed.path or "").lower()
        if not any(path.endswith(extension) for extension in extensions):
            raise ImageSearchError("invalid_url")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    return urllib.parse.urlunsplit(
        (scheme, netloc, parsed.path or "/", parsed.query, "")
    )


def safe_http_url(value: Any, *, require_image_extension: bool = False) -> str:
    """Non-raising convenience wrapper used by parsers and fallbacks."""

    try:
        return normalize_http_url(
            value,
            require_image_extension=require_image_extension,
        )
    except ImageSearchError:
        return ""


def canonical_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    netloc = f"[{host}]" if ":" in host else host
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urllib.parse.urlunsplit(
        (parts.scheme.lower(), netloc, parts.path or "/", parts.query, "")
    )


def resolve_public_addresses(host: str, port: int) -> list[str]:
    """Resolve a host once and require every answer to be globally routable."""

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError) as exc:
        raise ImageSearchError("dns_failed") from exc
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]
        if not is_public_ip(address):
            raise ImageSearchError("blocked_ip")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ImageSearchError("dns_failed")
    return addresses


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, *, pinned_ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, *, pinned_ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        raw_sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw_sock, server_hostname=self.host)
        except Exception:
            try:
                raw_sock.close()
            except OSError:
                pass
            raise


def detect_image_magic(data: bytes) -> tuple[str, str] | None:
    """Detect a supported image format from magic bytes."""

    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if data.startswith(b"BM"):
        return "image/bmp", ".bmp"
    if len(data) >= 12 and data[4:12] in {b"ftypavif", b"ftypavis"}:
        return "image/avif", ".avif"
    return None


def image_dimensions(data: bytes, mime: str) -> tuple[int, int] | None:
    """Best-effort dimensions for tests/diagnostics; never required for safety."""

    try:
        if mime == "image/png" and len(data) >= 24 and data[12:16] == b"IHDR":
            return (
                int.from_bytes(data[16:20], "big"),
                int.from_bytes(data[20:24], "big"),
            )
        if mime == "image/gif" and len(data) >= 10:
            return (
                int.from_bytes(data[6:8], "little"),
                int.from_bytes(data[8:10], "little"),
            )
        if mime == "image/bmp" and len(data) >= 26:
            return (
                abs(int.from_bytes(data[18:22], "little", signed=True)),
                abs(int.from_bytes(data[22:26], "little", signed=True)),
            )
        if mime == "image/jpeg":
            offset = 2
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
            while offset + 9 < len(data):
                if data[offset] != 0xFF:
                    offset += 1
                    continue
                marker = data[offset + 1]
                offset += 2
                if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
                    continue
                if offset + 2 > len(data):
                    return None
                segment_length = int.from_bytes(data[offset : offset + 2], "big")
                if segment_length < 2:
                    return None
                if marker in sof_markers and offset + 7 <= len(data):
                    return (
                        int.from_bytes(data[offset + 5 : offset + 7], "big"),
                        int.from_bytes(data[offset + 3 : offset + 5], "big"),
                    )
                offset += segment_length
            return None
        if mime == "image/webp" and len(data) >= 30:
            chunk_type = data[12:16]
            if chunk_type == b"VP8X":
                return (
                    1 + int.from_bytes(data[24:27], "little"),
                    1 + int.from_bytes(data[27:30], "little"),
                )
            if chunk_type == b"VP8 ":
                return (
                    int.from_bytes(data[26:28], "little") & 0x3FFF,
                    int.from_bytes(data[28:30], "little") & 0x3FFF,
                )
        return None
    except (IndexError, ValueError):
        return None


def _content_type_matches(expected: str, raw_type: str) -> bool:
    value = str(raw_type or "").split(";", 1)[0].strip().lower()
    if not value:
        # Some CDNs omit Content-Type.  For search pages/images we still apply
        # magic/size validation, so an absent header is tolerated.
        return True
    if expected == "html":
        return (
            value in {"text/html", "application/xhtml+xml"}
            or value.startswith("text/")
            or value.endswith("+xml")
        )
    if expected == "json":
        return (
            value in {"application/json", "text/json", "text/plain"}
            or value.endswith("+json")
        )
    if expected == "image":
        return value in ALLOWED_IMAGE_CONTENT_TYPES or value in OCTET_STREAM_TYPES
    return False


def validate_image_payload(
    data: bytes,
    content_type: str,
    *,
    max_bytes: int,
    allowed_types: Iterable[str] | None = None,
) -> tuple[str, str]:
    """Validate size, declared type, configured allowlist and magic bytes."""

    if not data:
        raise ImageSearchError("empty")
    if len(data) > max_bytes:
        raise ImageSearchError("too_large")
    allowed = (
        {str(item).strip().lower() for item in allowed_types if str(item).strip()}
        if allowed_types is not None
        else set(ALLOWED_IMAGE_CONTENT_TYPES)
    )
    if not allowed:
        allowed = set(ALLOWED_IMAGE_CONTENT_TYPES)
    declared = str(content_type or "").split(";", 1)[0].strip().lower()
    if declared and declared not in allowed | OCTET_STREAM_TYPES:
        raise ImageSearchError("bad_content_type")
    detected = detect_image_magic(data)
    if detected is None:
        raise ImageSearchError("bad_magic")
    mime, suffix = detected
    normalized = {
        "image/jpg": "image/jpeg",
        "image/x-ms-bmp": "image/bmp",
    }.get(declared, declared)
    if normalized in ALLOWED_IMAGE_CONTENT_TYPES and normalized != mime:
        raise ImageSearchError("bad_content_type")
    if mime not in allowed:
        raise ImageSearchError("bad_content_type")
    return mime, suffix


def decode_http_text(data: bytes, content_type: str) -> str:
    charset = ""
    match = _CHARSET_RE.search(str(content_type or ""))
    if match:
        charset = match.group(1)
    if not charset:
        match = _META_CHARSET_RE.search(data[:2048])
        if match:
            charset = match.group(1).decode("ascii", "ignore")
    if not charset:
        charset = "utf-8"
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


@dataclass(slots=True)
class HopResult:
    status: int
    location: str
    content_type: str
    data: bytes


@dataclass(slots=True)
class HttpText:
    text: str
    final_url: str
    content_type: str
    data: bytes


def _request_once(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    headers: dict[str, str],
) -> HopResult:
    """One GET with a DNS-validated, pinned TCP connection."""

    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    port = parts.port or (443 if scheme == "https" else 80)
    addresses = resolve_public_addresses(host, port)
    connection_cls = (
        _PinnedHTTPSConnection if scheme == "https" else _PinnedHTTPConnection
    )
    conn: http.client.HTTPConnection = connection_cls(
        host,
        port,
        pinned_ip=addresses[0],
        timeout=timeout,
    )
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    try:
        try:
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            status = int(response.status)
            if status in _REDIRECT_STATUSES:
                location = str(response.headers.get("Location", "") or "").strip()
                return HopResult(status, location, "", b"")
            if status != 200:
                return HopResult(status, "", "", b"")
            raw_type = str(response.headers.get("Content-Type", "") or "")
            encoding = str(
                response.headers.get("Content-Encoding", "") or ""
            ).strip().lower()
            if encoding not in {"", "identity"}:
                raise ImageSearchError("bad_encoding")
            length = str(response.headers.get("Content-Length", "") or "").strip()
            if length:
                try:
                    if int(length) > max_bytes:
                        raise ImageSearchError("too_large")
                except ValueError:
                    pass
            data = response.read(max_bytes + 1) or b""
            if len(data) > max_bytes:
                raise ImageSearchError("too_large")
            return HopResult(status, "", raw_type, bytes(data))
        except ImageSearchError:
            raise
        except (socket.timeout, TimeoutError) as exc:
            raise ImageSearchError("timeout") from exc
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise ImageSearchError("network") from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass


def http_get(
    url: object,
    *,
    timeout: float = IMAGE_SEARCH_DEFAULT_TIMEOUT,
    max_bytes: int = IMAGE_SEARCH_DEFAULT_MAX_RESPONSE_BYTES,
    headers: dict[str, str] | None = None,
    max_redirects: int = IMAGE_SEARCH_MAX_REDIRECTS,
    expected: str = "html",
) -> HopResult:
    """GET a public URL with per-hop validation, bounds and content-type checks."""

    max_bytes = max(1024, min(int(max_bytes), IMAGE_SEARCH_MAX_RESPONSE_BYTES_HARD_LIMIT))
    max_redirects = max(0, min(int(max_redirects), IMAGE_SEARCH_MAX_REDIRECTS))
    timeout = max(0.5, min(float(timeout), 300.0))
    deadline = time.monotonic() + timeout
    current = normalize_http_url(url)
    request_headers = {
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
        "Connection": "close",
        "User-Agent": IMAGE_SEARCH_USER_AGENT,
    }
    if headers:
        request_headers.update({str(k): str(v) for k, v in headers.items()})
    for hop in range(max_redirects + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0.05:
            raise ImageSearchError("timeout")
        result = _request_once(
            current,
            timeout=min(remaining, timeout),
            max_bytes=max_bytes,
            headers=request_headers,
        )
        if result.status in _REDIRECT_STATUSES:
            if hop >= max_redirects:
                raise ImageSearchError("too_many_redirects")
            if not result.location:
                raise ImageSearchError("bad_redirect")
            try:
                current = normalize_http_url(
                    urllib.parse.urljoin(current, result.location)
                )
            except ImageSearchError as exc:
                raise ImageSearchError("bad_redirect") from exc
            continue
        if result.status != 200:
            raise ImageSearchError("http_error")
        if not _content_type_matches(expected, result.content_type):
            raise ImageSearchError("bad_content_type")
        return HopResult(result.status, "", result.content_type, result.data)
    raise ImageSearchError("too_many_redirects")


def fetch_search_html(
    search_url: str,
    *,
    timeout: float = IMAGE_SEARCH_DEFAULT_TIMEOUT,
    max_response_bytes: int = IMAGE_SEARCH_DEFAULT_MAX_RESPONSE_BYTES,
    max_redirects: int = IMAGE_SEARCH_MAX_REDIRECTS,
    user_agent: str = IMAGE_SEARCH_USER_AGENT,
) -> str:
    result = http_get(
        search_url,
        timeout=timeout,
        max_bytes=max_response_bytes,
        max_redirects=max_redirects,
        expected="html",
        headers={
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
            "User-Agent": user_agent,
            "Referer": BING_IMAGES_REFERER,
        },
    )
    return decode_http_text(result.data, result.content_type)


def fetch_baidu_json(
    search_url: str,
    *,
    timeout: float = IMAGE_SEARCH_DEFAULT_TIMEOUT,
    max_response_bytes: int = IMAGE_SEARCH_DEFAULT_MAX_RESPONSE_BYTES,
    max_redirects: int = IMAGE_SEARCH_MAX_REDIRECTS,
    user_agent: str = IMAGE_SEARCH_USER_AGENT,
) -> str:
    result = http_get(
        search_url,
        timeout=timeout,
        max_bytes=max_response_bytes,
        max_redirects=max_redirects,
        expected="json",
        headers={
            "Accept": "application/json,text/plain,*/*",
            "User-Agent": user_agent,
            "Referer": BAIDU_IMAGES_REFERER,
        },
    )
    return decode_http_text(result.data, result.content_type)


def build_bing_image_search_url(
    keyword: str,
    *,
    result_count: int = 3,
    safe_search: str = "strict",
) -> str:
    result_count = max(1, min(IMAGE_SEARCH_MAX_RESULTS_PER_COMMAND, int(result_count or 1)))
    fetch_count = min(35, max(10, result_count * 8))
    safe_mode = str(safe_search or "strict").strip().lower()
    if safe_mode not in {"strict", "moderate", "off"}:
        safe_mode = "strict"
    has_cjk = bool(re.search(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]", str(keyword or "")))
    params = urllib.parse.urlencode(
        {
            "q": keyword,
            "form": "HDRSC2",
            "first": "1",
            "count": str(fetch_count),
            "adlt": safe_mode,
            "setlang": "zh-hans" if has_cjk else "en-US",
            "qs": "n",
        }
    )
    return f"{BING_IMAGES_ENDPOINT}?{params}"


def build_baidu_image_search_url(keyword: str, *, result_count: int = 3) -> str:
    result_count = max(1, min(IMAGE_SEARCH_MAX_RESULTS_PER_COMMAND, int(result_count or 1)))
    fetch_count = min(30, max(10, result_count * 8))
    params = {
        "tn": "resultjson_com",
        "ipn": "rj",
        "ct": "201326592",
        "is": "",
        "fp": "result",
        "queryWord": keyword,
        "cl": "2",
        "lm": "-1",
        "ie": "utf-8",
        "oe": "utf-8",
        "adpicid": "",
        "st": "-1",
        "z": "",
        "ic": "0",
        "hd": "",
        "latest": "",
        "copyright": "",
        "word": keyword,
        "s": "",
        "se": "",
        "tab": "",
        "width": "",
        "height": "",
        "face": "0",
        "istype": "2",
        "qc": "",
        "nc": "1",
        "fr": "",
        "expermode": "",
        "nojc": "",
        "pn": "0",
        "rn": str(fetch_count),
        "gsm": "1e",
    }
    return f"{BAIDU_IMAGES_ENDPOINT}?{urllib.parse.urlencode(params)}"


def _has_image_path(url: str) -> bool:
    try:
        path = urllib.parse.urlsplit(url).path.lower()
    except ValueError:
        return False
    return any(path.endswith(extension) for extension in IMAGE_PATH_EXTENSIONS)


def parse_bing_image_candidates(page: str, *, limit: int = 1) -> list[dict[str, Any]]:
    """Parse Bing Images HTML into sanitised candidate dictionaries."""

    result_limit = max(1, min(IMAGE_SEARCH_MAX_CANDIDATES, int(limit or 1)))
    scan_limit = min(IMAGE_SEARCH_MAX_CANDIDATES, max(50, result_limit * 20))
    raw_candidates: list[dict[str, str]] = []

    # Primary parser: the m="{...}" JSON payload contains murl/t/desc/purl.
    for match in re.finditer(r'\bm="([^"]+)"', str(page or ""), re.IGNORECASE):
        if len(raw_candidates) >= scan_limit:
            break
        try:
            payload = json.loads(html_lib.unescape(match.group(1)))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        murl = payload.get("murl")
        if isinstance(murl, str) and murl.strip():
            raw_candidates.append(
                {
                    "url": murl.strip(),
                    "title": str(payload.get("t") or "").strip(),
                    "description": str(payload.get("desc") or "").strip(),
                    "page_url": str(payload.get("purl") or "").strip(),
                }
            )

    decoded_page = html_lib.unescape(str(page or ""))

    # Fallback 1: bare JSON "murl":"..." fragments.
    if len(raw_candidates) < scan_limit:
        pattern = re.compile(r'"murl"\s*:\s*"((?:\\.|[^"\\])*)"', re.IGNORECASE)
        for match in pattern.finditer(decoded_page):
            if len(raw_candidates) >= scan_limit:
                break
            encoded = match.group(1)
            try:
                url = json.loads(f'"{encoded}"')
            except (TypeError, ValueError):
                url = encoded.replace("\\/", "/")
            if isinstance(url, str) and url.strip():
                raw_candidates.append(
                    {"url": url.strip(), "title": "", "description": "", "page_url": ""}
                )

    # Fallback 2: image-looking absolute URLs anywhere in the HTML.
    if len(raw_candidates) < scan_limit:
        for match in re.finditer(
            r'https?://[^\s"\'<>\\]+', decoded_page, re.IGNORECASE
        ):
            if len(raw_candidates) >= scan_limit:
                break
            candidate = match.group(0).rstrip(".,);]}\\")
            if _has_image_path(candidate):
                raw_candidates.append(
                    {"url": candidate, "title": "", "description": "", "page_url": ""}
                )

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in raw_candidates:
        safe_url = safe_http_url(candidate.get("url"), require_image_extension=True)
        if not safe_url:
            continue
        key = canonical_url(safe_url)
        if key in seen:
            continue
        seen.add(key)
        page_url = safe_http_url(candidate.get("page_url"), require_image_extension=False)
        results.append(
            {
                "url": safe_url,
                "title": str(candidate.get("title") or "").strip(),
                "description": str(candidate.get("description") or "").strip(),
                "page_url": page_url,
            }
        )
        if len(results) >= result_limit:
            break
    return results


def parse_bing_image_urls(page: str, *, limit: int = 1) -> list[str]:
    return [str(item["url"]) for item in parse_bing_image_candidates(page, limit=limit)]


def parse_baidu_image_candidates(payload: str | dict[str, Any], *, limit: int = 1) -> list[dict[str, Any]]:
    """Extract safe URLs plus title/description metadata from Baidu JSON."""

    result_limit = max(1, min(IMAGE_SEARCH_MAX_CANDIDATES, int(limit or 1)))
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return []
    if not isinstance(payload, dict):
        return []
    entries = payload.get("data", [])
    if not isinstance(entries, list):
        return []

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        if len(results) >= result_limit:
            break
        if not isinstance(entry, dict):
            continue
        raw_candidates: list[Any] = [
            entry.get("middleURL"),
            entry.get("thumbURL"),
            entry.get("hoverURL"),
        ]
        page_url = ""
        replace_urls = entry.get("replaceUrl", [])
        if isinstance(replace_urls, list):
            for replacement in replace_urls:
                if not isinstance(replacement, dict):
                    continue
                raw_candidates.extend(
                    [replacement.get("ObjURL"), replacement.get("ObjUrl")]
                )
                if not page_url:
                    page_url = str(
                        replacement.get("FromURL")
                        or replacement.get("FromUrl")
                        or ""
                    )
        chosen_url = ""
        for raw_candidate in raw_candidates:
            safe_url = safe_http_url(raw_candidate, require_image_extension=False)
            if safe_url:
                chosen_url = safe_url
                break
        if not chosen_url:
            continue
        key = canonical_url(chosen_url)
        if key in seen:
            continue
        seen.add(key)
        title = ""
        for title_key in (
            "fromPageTitleEnc",
            "fromPageTitle",
            "bdSourceName",
            "bdFromPageTitlePrefix",
        ):
            title = str(entry.get(title_key) or "").strip()
            if title:
                break
        description_parts: list[str] = []
        for description_key in (
            "fromPageTitleEnc",
            "fromPageTitle",
            "bdSourceName",
            "bdFromPageTitlePrefix",
            "fromURLHost",
        ):
            value = str(entry.get(description_key) or "").strip()
            if value and value not in description_parts:
                description_parts.append(value)
        if not page_url:
            page_url = str(entry.get("fromURL") or entry.get("fromJumpUrl") or "")
        results.append(
            {
                "url": chosen_url,
                "title": title,
                "description": " ".join(description_parts),
                "page_url": safe_http_url(page_url, require_image_extension=False),
            }
        )
    return results


def parse_baidu_image_urls(payload: str | dict[str, Any], *, limit: int = 1) -> list[str]:
    return [str(item["url"]) for item in parse_baidu_image_candidates(payload, limit=limit)]


def dedupe_candidates(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicate candidates by canonical URL without mutating inputs."""

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        safe_url = safe_http_url(candidate.get("url"), require_image_extension=False)
        if not safe_url:
            continue
        key = canonical_url(safe_url)
        if key in seen:
            continue
        seen.add(key)
        copied = dict(candidate)
        copied["url"] = safe_url
        results.append(copied)
    return results


# ---------------------------------------------------------------------------
# Relevance filtering (conservative, deterministic, no model calls)
# ---------------------------------------------------------------------------

IMAGE_SEARCH_QUERY_ALIASES = {
    "千早爱音": ("anon chihaya", "chihaya anon"),
    "初音未来": ("hatsune miku", "miku hatsune"),
    "猫": ("cat", "neko"),
    "猫猫": ("cat", "neko"),
    "大树": ("tree", "big tree"),
}
RELEVANCE_VARIANT_TRANSLATION = str.maketrans(
    {
        "愛": "爱",
        "繪": "绘",
        "優": "优",
        "櫻": "樱",
        "涼": "凉",
        "麗": "丽",
        "亞": "亚",
        "郞": "郎",
        "郷": "乡",
        "鬥": "斗",
        "說": "说",
        "讀": "读",
        "書": "书",
        "寫": "写",
        "學": "学",
        "會": "会",
        "樂": "乐",
        "園": "园",
        "葉": "叶",
        "華": "华",
    }
)


def normalize_relevance_text(value: Any) -> str:
    text = html_lib.unescape(str(value or ""))
    text = unicodedata.normalize("NFKC", text).casefold()
    text = text.translate(RELEVANCE_VARIANT_TRANSLATION)
    text = re.sub(r"[\ue000-\uf8ff\u200b-\u200f\u202a-\u202e]+", " ", text)
    text = re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def relevance_query_variants(keyword: str) -> list[str]:
    query = normalize_relevance_text(keyword)
    if not query:
        return []
    variants = [query]
    query_compact = query.replace(" ", "")
    for canonical, aliases in IMAGE_SEARCH_QUERY_ALIASES.items():
        canonical_norm = normalize_relevance_text(canonical)
        alias_norms = [normalize_relevance_text(alias) for alias in aliases]
        group = [canonical_norm, *alias_norms]
        group_compacts = {variant.replace(" ", "") for variant in group if variant}
        if query_compact in group_compacts:
            for variant in group:
                if variant and variant not in variants:
                    variants.append(variant)
    return variants


def relevance_score_single(query: str, title_norm: str, desc_norm: str) -> float:
    if not query or (not title_norm and not desc_norm):
        return 0.0
    query_compact = query.replace(" ", "")
    title_compact = title_norm.replace(" ", "")
    desc_compact = desc_norm.replace(" ", "")
    title_tokens = re.findall(r"[a-z0-9]+", title_norm)
    desc_tokens = re.findall(r"[a-z0-9]+", desc_norm)
    query_ascii_tokens = re.findall(r"[a-z0-9]+", query)

    # ASCII queries match whole words, avoiding e.g. cat -> catalog.
    if query_ascii_tokens and not re.search(
        r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]", query
    ):
        if all(token in title_tokens for token in query_ascii_tokens):
            return 1.0 if query_compact in title_compact else 0.9
        if all(token in desc_tokens for token in query_ascii_tokens):
            return 0.9 if query_compact in desc_compact else 0.8
        combined_tokens = set(title_tokens) | set(desc_tokens)
        if all(token in combined_tokens for token in query_ascii_tokens):
            return 0.8
        return 0.0

    if query_compact and query_compact in title_compact:
        return 1.0
    if query_compact and query_compact in desc_compact:
        return 0.9

    cjk_query = "".join(
        ch
        for ch in query_compact
        if "\u3400" <= ch <= "\u4dbf" or "\u4e00" <= ch <= "\u9fff"
    )
    if len(cjk_query) >= 4:
        bigrams = {
            cjk_query[index : index + 2]
            for index in range(len(cjk_query) - 1)
        }
        combined_compact = title_compact + desc_compact
        matched = sum(1 for bigram in bigrams if bigram in combined_compact)
        ratio = matched / max(1, len(bigrams))
        if ratio >= 0.8:
            return 0.75
        if ratio >= 0.6:
            return 0.6
    return 0.0


def relevance_score(keyword: str, title: str, description: str) -> float:
    title_norm = normalize_relevance_text(title)
    desc_norm = normalize_relevance_text(description)
    if not title_norm and not desc_norm:
        return 0.0
    variants = relevance_query_variants(keyword)
    if not variants:
        return 0.0
    return max(
        relevance_score_single(variant, title_norm, desc_norm)
        for variant in variants
    )


def filter_candidates(
    keyword: str,
    candidates: list[dict[str, Any]],
    *,
    min_score: float,
) -> list[dict[str, Any]]:
    relevant: list[dict[str, Any]] = []
    for candidate in candidates:
        score = relevance_score(
            keyword,
            str(candidate.get("title", "") or ""),
            str(candidate.get("description", "") or ""),
        )
        candidate["relevance_score"] = score
        if score >= min_score:
            relevant.append(candidate)
    relevant.sort(
        key=lambda item: float(item.get("relevance_score", 0.0)),
        reverse=True,
    )
    return relevant


# ---------------------------------------------------------------------------
# Deterministic natural-language intent detection and cooldown state
# ---------------------------------------------------------------------------

IMAGE_SEARCH_NL_RE = re.compile(
    r"(?P<prefix>(?:帮(?:我|忙)|给(?:我|一下)|请(?:你)?|麻烦(?:你)?|你|去|来|再|能|可以|一下|\s)*)"
    r"(?P<action>搜索|搜|查找|找|发|来|整|搞|要)"
    r"\s*(?:(?P<quantity>(?:\d+|[一二两三四五六七八九十几]+|一些))(?!下))?\s*"
    r"(?:张|幅|个)?\s*"
    r"(?P<keyword>.{1,40}?)\s*(?:的)?\s*"
    r"(?P<noun>图片|照片|壁纸|头像|插画|美图|图集|图)"
)
IMAGE_SEARCH_NL_NEGATION_RE = re.compile(
    r"(?:^|[，。！？!?\s])(?:我)?(?:"
    r"找不到|没找到|没有找到|搜不到|没搜到|没有搜到|无法找到|"
    r"别找|不用找|不想找|不要找|不需要找)"
)
IMAGE_SEARCH_NL_REFUSAL_RE = re.compile(
    r"(?:不要|不用|别|不想|不需要|无需|没必要)"
    r".{0,8}(?:找|搜|发|来|整|搞|给|要)"
    r".{0,12}(?:图片|照片|壁纸|头像|插画|美图|图集|图)"
)
_GENERIC_KEYWORDS = {
    "",
    "张",
    "幅",
    "个",
    "的",
    "图",
    "图片",
    "照片",
    "壁纸",
    "头像",
    "插画",
    "美图",
    "图集",
    "好看的",
    "好看",
    "一些",
    "几张",
    "一下",
    "帮我",
    "给我",
    "你",
    "去",
    "来",
    "这个",
    "那个",
}


def clean_natural_language_image_keyword(
    raw: str,
    *,
    max_chars: int = IMAGE_SEARCH_MAX_KEYWORD_CHARS_HARD_LIMIT,
) -> str:
    value = re.sub(r"\s+", " ", str(raw or "")).strip()
    value = re.sub(
        r"^(?:帮我|给我|请你?|麻烦你?|你|去|来|再|一下|下|能|可以|帮忙|关于|有关|的)+",
        "",
        value,
    ).strip()
    value = re.sub(r"^(?:张|幅|个)+", "", value).strip()
    value = re.sub(
        r"(?:的)?(?:图片|照片|壁纸|头像|插画|美图|图集|图)$",
        "",
        value,
    ).strip()
    value = value.strip(" \t\r\n，。！？!?,.、:：;；\"'“”‘’")
    value = re.sub(r"^[的地得]+|[的地得]+$", "", value).strip()
    if value in _GENERIC_KEYWORDS:
        return ""
    return value[: max(1, min(int(max_chars or 1), IMAGE_SEARCH_MAX_KEYWORD_CHARS_HARD_LIMIT))]


def natural_language_result_count(
    quantity: str | None,
    *,
    default_limit: int = 3,
) -> int:
    default_limit = max(
        1, min(IMAGE_SEARCH_MAX_RESULTS_PER_COMMAND, int(default_limit or 1))
    )
    if not quantity:
        return default_limit
    normalized = str(quantity).strip()
    mapping = {
        "一": 1,
        "1": 1,
        "二": 2,
        "两": 2,
        "2": 2,
        "三": 3,
        "3": 3,
        "四": 4,
        "4": 4,
        "五": 5,
        "5": 5,
        "几": max(2, default_limit),
        "一些": max(2, default_limit),
    }
    return max(
        1,
        min(
            IMAGE_SEARCH_MAX_RESULTS_PER_COMMAND,
            mapping.get(normalized, default_limit),
        ),
    )


def detect_natural_language_image_search(
    text: str,
    *,
    default_count: int = 3,
    max_keyword_chars: int = 100,
) -> dict[str, Any] | None:
    """Conservatively detect an explicit request to find/send images."""

    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return None
    if IMAGE_SEARCH_NL_REFUSAL_RE.search(value):
        return None
    negations = list(IMAGE_SEARCH_NL_NEGATION_RE.finditer(value))
    for match in IMAGE_SEARCH_NL_RE.finditer(value):
        action_start = match.start("action")
        if any(
            negation.start() <= action_start < negation.end()
            for negation in negations
        ):
            continue
        keyword = clean_natural_language_image_keyword(
            match.group("keyword"),
            max_chars=max_keyword_chars,
        )
        if not keyword:
            continue
        return {
            "keyword": keyword,
            "result_count": natural_language_result_count(
                match.group("quantity"),
                default_limit=default_count,
            ),
        }
    return None


class CooldownTracker:
    """Small deterministic in-memory group/user cooldown helper."""

    def __init__(self, clock: Callable[[], float] | None = None):
        self._clock = clock or time.monotonic
        self._group: dict[str, float] = {}
        self._user: dict[str, float] = {}

    def remaining(
        self,
        group_key: str,
        user_key: str,
        *,
        group_seconds: int,
        user_seconds: int,
    ) -> int:
        now = float(self._clock())
        remaining = 0.0
        last_group = self._group.get(str(group_key))
        last_user = self._user.get(str(user_key))
        if last_group is not None:
            remaining = max(remaining, float(group_seconds) - (now - last_group))
        if last_user is not None:
            remaining = max(remaining, float(user_seconds) - (now - last_user))
        return max(0, int(remaining + 0.999)) if remaining > 0 else 0

    def consume(
        self,
        group_key: str,
        user_key: str,
        *,
        group_seconds: int,
        user_seconds: int,
    ) -> int:
        """Return remaining seconds when denied; reserve the slot when allowed."""

        remaining = self.remaining(
            group_key,
            user_key,
            group_seconds=group_seconds,
            user_seconds=user_seconds,
        )
        if remaining > 0:
            return remaining
        now = float(self._clock())
        self._group[str(group_key)] = now
        self._user[str(user_key)] = now
        return 0

    def clear(self) -> None:
        self._group.clear()
        self._user.clear()


# ---------------------------------------------------------------------------
# Search/download orchestration
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class SearchOptions:
    result_count: int = 3
    timeout: float = IMAGE_SEARCH_DEFAULT_TIMEOUT
    max_response_bytes: int = IMAGE_SEARCH_DEFAULT_MAX_RESPONSE_BYTES
    max_candidates: int = IMAGE_SEARCH_MAX_CANDIDATE_URLS
    max_redirects: int = IMAGE_SEARCH_MAX_REDIRECTS
    relevance_enabled: bool = True
    min_relevance_score: float = 0.6
    baidu_fallback_enabled: bool = True
    safe_search: str = "strict"
    user_agent: str = IMAGE_SEARCH_USER_AGENT
    download_timeout: float = IMAGE_SEARCH_DEFAULT_DOWNLOAD_TIMEOUT
    download_max_bytes: int = IMAGE_SEARCH_DEFAULT_DOWNLOAD_MAX_BYTES
    download_attempts: int = 8
    allowed_types: tuple[str, ...] = tuple(sorted(ALLOWED_IMAGE_CONTENT_TYPES))


def _bounded_options(options: SearchOptions | None) -> SearchOptions:
    base = options or SearchOptions()
    return replace(
        base,
        result_count=max(1, min(IMAGE_SEARCH_MAX_RESULTS_PER_COMMAND, int(base.result_count or 1))),
        timeout=max(0.5, min(float(base.timeout or 1), 300.0)),
        max_response_bytes=max(
            1024,
            min(int(base.max_response_bytes or 1024), IMAGE_SEARCH_MAX_RESPONSE_BYTES_HARD_LIMIT),
        ),
        max_candidates=max(1, min(IMAGE_SEARCH_MAX_CANDIDATE_URLS, int(base.max_candidates or 1))),
        max_redirects=max(0, min(IMAGE_SEARCH_MAX_REDIRECTS, int(base.max_redirects or 0))),
        min_relevance_score=max(0.0, min(1.0, float(base.min_relevance_score or 0.0))),
        download_timeout=max(0.5, min(float(base.download_timeout or 1), 300.0)),
        download_max_bytes=max(
            1024,
            min(int(base.download_max_bytes or 1024), IMAGE_SEARCH_MAX_DOWNLOAD_BYTES_HARD_LIMIT),
        ),
        download_attempts=max(1, int(base.download_attempts or 1)),
        allowed_types=tuple(
            str(item).strip().lower()
            for item in (base.allowed_types or ALLOWED_IMAGE_CONTENT_TYPES)
            if str(item).strip()
        )
        or tuple(sorted(ALLOWED_IMAGE_CONTENT_TYPES)),
    )


def _apply_relevance(
    keyword: str,
    candidates: list[dict[str, Any]],
    options: SearchOptions,
) -> list[dict[str, Any]]:
    if not options.relevance_enabled:
        for candidate in candidates:
            candidate["relevance_score"] = 1.0
        return candidates
    return filter_candidates(
        keyword,
        candidates,
        min_score=options.min_relevance_score,
    )


def search_image_candidates(
    keyword: str,
    options: SearchOptions | None = None,
) -> list[dict[str, Any]]:
    """Search Bing first and fall back to Baidu JSON when needed.

    Returns up to ``options.max_candidates`` safe candidates.  Raises
    :class:`ImageSearchError` only when every enabled source failed.
    """

    options = _bounded_options(options)
    text_keyword = str(keyword or "").strip()[
        :IMAGE_SEARCH_MAX_KEYWORD_CHARS_HARD_LIMIT
    ]
    if not text_keyword:
        raise ImageSearchError("invalid_keyword")
    result_limit = options.result_count
    candidate_limit = options.max_candidates
    raw_limit = min(
        IMAGE_SEARCH_MAX_CANDIDATES,
        max(20, candidate_limit * 3),
    )
    candidates: list[dict[str, Any]] = []
    errors: list[BaseException] = []
    bing_ok = False
    baidu_ok = False

    try:
        bing_url = build_bing_image_search_url(
            text_keyword,
            result_count=result_limit,
            safe_search=options.safe_search,
        )
        page = fetch_search_html(
            bing_url,
            timeout=options.timeout,
            max_response_bytes=options.max_response_bytes,
            max_redirects=options.max_redirects,
            user_agent=options.user_agent,
        )
        bing_ok = True
        parsed = _apply_relevance(
            text_keyword,
            parse_bing_image_candidates(page, limit=raw_limit),
            options,
        )
        for candidate in parsed:
            candidate["referer"] = BING_IMAGES_REFERER
            candidate["source"] = "bing"
            candidates.append(candidate)
    except Exception as exc:  # noqa: BLE001 - converted to safe error below
        errors.append(exc)

    if len(candidates) < result_limit and options.baidu_fallback_enabled:
        try:
            baidu_url = build_baidu_image_search_url(
                text_keyword,
                result_count=result_limit,
            )
            payload = fetch_baidu_json(
                baidu_url,
                timeout=options.timeout,
                max_response_bytes=options.max_response_bytes,
                max_redirects=options.max_redirects,
                user_agent=options.user_agent,
            )
            baidu_ok = True
            parsed = _apply_relevance(
                text_keyword,
                parse_baidu_image_candidates(payload, limit=raw_limit),
                options,
            )
            for candidate in parsed:
                candidate["referer"] = BAIDU_IMAGES_REFERER
                candidate["source"] = "baidu"
                candidates.append(candidate)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    candidates = dedupe_candidates(candidates)
    if options.relevance_enabled:
        candidates.sort(
            key=lambda item: float(item.get("relevance_score", 0.0)),
            reverse=True,
        )
    if not candidates and not bing_ok and not baidu_ok:
        if any(
            isinstance(error, ImageSearchError) and error.reason == "timeout"
            for error in errors
        ):
            raise ImageSearchError("timeout")
        raise ImageSearchError("network")
    return candidates[:candidate_limit]


def download_image_bytes(
    image_url: str,
    *,
    referer: str = BING_IMAGES_REFERER,
    timeout: float = IMAGE_SEARCH_DEFAULT_DOWNLOAD_TIMEOUT,
    max_bytes: int = IMAGE_SEARCH_DEFAULT_DOWNLOAD_MAX_BYTES,
    max_redirects: int = IMAGE_SEARCH_MAX_REDIRECTS,
    user_agent: str = IMAGE_SEARCH_USER_AGENT,
    allowed_types: Iterable[str] | None = None,
) -> tuple[bytes, str, str]:
    """Download and validate one image; return ``(data, mime, suffix)``."""

    max_bytes = max(1024, min(int(max_bytes), IMAGE_SEARCH_MAX_DOWNLOAD_BYTES_HARD_LIMIT))
    safe_url = normalize_http_url(image_url)
    result = http_get(
        safe_url,
        timeout=timeout,
        max_bytes=max_bytes,
        max_redirects=max_redirects,
        expected="image",
        headers={
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": str(referer or BING_IMAGES_REFERER),
            "User-Agent": user_agent,
        },
    )
    mime, suffix = validate_image_payload(
        result.data,
        result.content_type,
        max_bytes=max_bytes,
        allowed_types=allowed_types,
    )
    return result.data, mime, suffix


@dataclass(slots=True)
class DownloadedImage:
    path: str
    mime: str
    suffix: str
    source_url: str


def download_image_to_temp(
    image_url: str,
    *,
    referer: str = BING_IMAGES_REFERER,
    timeout: float = IMAGE_SEARCH_DEFAULT_DOWNLOAD_TIMEOUT,
    max_bytes: int = IMAGE_SEARCH_DEFAULT_DOWNLOAD_MAX_BYTES,
    max_redirects: int = IMAGE_SEARCH_MAX_REDIRECTS,
    user_agent: str = IMAGE_SEARCH_USER_AGENT,
    allowed_types: Iterable[str] | None = None,
    temp_dir: str | None = None,
) -> DownloadedImage:
    """Download one safe image into a bounded temporary file."""

    data, mime, suffix = download_image_bytes(
        image_url,
        referer=referer,
        timeout=timeout,
        max_bytes=max_bytes,
        max_redirects=max_redirects,
        user_agent=user_agent,
        allowed_types=allowed_types,
    )
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            prefix="qqbot_image_search_",
            suffix=suffix,
            dir=temp_dir,
            delete=False,
        ) as temporary:
            temp_path = temporary.name
            temporary.write(data)
            temporary.flush()
        return DownloadedImage(temp_path, mime, suffix, image_url)
    except Exception:
        if temp_path:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except OSError:
                pass
        raise


def download_images(
    candidates: list[dict[str, Any]],
    *,
    result_limit: int,
    options: SearchOptions | None = None,
    temp_dir: str | None = None,
) -> tuple[list[DownloadedImage], list[str]]:
    """Try candidates in order; return local files plus safe remote fallbacks."""

    options = _bounded_options(options)
    result_limit = max(
        1, min(IMAGE_SEARCH_MAX_RESULTS_PER_COMMAND, int(result_limit or 1))
    )
    safe_candidates = dedupe_candidates(candidates)
    remote_urls: list[str] = [str(c["url"]) for c in safe_candidates]
    max_attempts = min(
        len(safe_candidates),
        max(1, int(options.download_attempts)),
    )
    downloaded: list[DownloadedImage] = []
    for candidate in safe_candidates[:max_attempts]:
        if len(downloaded) >= result_limit:
            break
        try:
            downloaded.append(
                download_image_to_temp(
                    str(candidate["url"]),
                    referer=str(candidate.get("referer") or BING_IMAGES_REFERER),
                    timeout=options.download_timeout,
                    max_bytes=options.download_max_bytes,
                    max_redirects=options.max_redirects,
                    user_agent=options.user_agent,
                    allowed_types=options.allowed_types,
                    temp_dir=temp_dir,
                )
            )
        except Exception:
            # A bad candidate must not abort the whole search.
            continue
    return downloaded, remote_urls

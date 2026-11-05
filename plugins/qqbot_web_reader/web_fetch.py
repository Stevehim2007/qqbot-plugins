"""Bounded, stdlib-only reader for public web pages.

Security properties intentionally mirrored from the legacy enhancement engine:

* HTTPS-only by default (``allow_http`` is an explicit opt-in);
* credentials in URLs, private/loopback/link-local/reserved/non-global IPs,
  internal host names and every redirect hop are rejected;
* DNS is resolved before every hop and the connection is pinned to the
  validated address, preventing DNS rebinding between check and connect;
* response size, total time, redirect count, content type and content
  encoding are bounded;
* HTML is reduced to visible plain text and untrusted tool markers are
  neutralised before a result is returned.

The module has no AstrBot dependency so it can be tested in isolation.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import time
import urllib.parse
from dataclasses import dataclass
from html.parser import HTMLParser

WEB_FETCH_MAX_URL_CHARS = 2048
WEB_FETCH_MAX_CHARS_HARD_LIMIT = 20_000
WEB_FETCH_MAX_BYTES_HARD_LIMIT = 5 * 1024 * 1024
WEB_FETCH_MAX_REDIRECTS_HARD_LIMIT = 5
WEB_FETCH_DEFAULT_TIMEOUT = 10.0
WEB_FETCH_DEFAULT_MAX_BYTES = 262_144
WEB_FETCH_DEFAULT_MAX_CHARS = 4000
WEB_FETCH_DEFAULT_MAX_REDIRECTS = 3
WEB_FETCH_ALLOWED_SCHEMES = frozenset({"https"})
WEB_FETCH_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
WEB_FETCH_ALLOWED_CONTENT_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "text/plain",
        "text/xml",
        "application/xml",
        "application/json",
    }
)
WEB_FETCH_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata",
        "metadata.google.internal",
        "instance-data",
    }
)
WEB_FETCH_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".lan",
    ".corp",
    ".home.arpa",
)
_HOSTNAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HORIZONTAL_SPACE_RE = re.compile(r"[ \t\xa0\u3000]+")
_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([\w.+-]+)", re.IGNORECASE)
_META_CHARSET_RE = re.compile(
    rb"<meta[^>]+charset\s*=\s*[\"']?([\w.+-]+)",
    re.IGNORECASE,
)
_TOOL_MARKER_RE = re.compile(r"\[\s*\[")
_TOOL_MARKER_MAX = 2000

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; QQBotStudio-WebReader/2.0; "
    "+https://github.com/Stevehim2007/qqbot-plugins)"
)
WEB_FETCH_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,text/plain,application/json,"
        "application/xml;q=0.9,*/*;q=0.1"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "identity",
    "Cache-Control": "no-cache",
    "Connection": "close",
}

REASON_TEXT = {
    "invalid_url": "只支持普通 HTTPS 公开网页链接。",
    "blocked_host": "目标地址不是公开网页。",
    "blocked_ip": "目标地址不是公开网页。",
    "dns_failed": "无法解析该网页域名。",
    "network": "连接网页失败或超时。",
    "timeout": "读取网页超时，请稍后再试。",
    "too_many_redirects": "网页跳转次数过多。",
    "bad_redirect": "网页跳转到了不安全的地址。",
    "http_error": "网页返回了错误状态。",
    "too_large": "网页内容超过大小限制。",
    "bad_content_type": "网页不是可读取的文本内容。",
    "bad_encoding": "网页压缩格式不受支持。",
    "empty": "网页没有可读正文。",
}


class WebFetchError(Exception):
    """Failure carrying a stable reason code; message text stays internal."""

    def __init__(self, reason: str) -> None:
        super().__init__(str(reason))
        self.reason = str(reason)


def safe_error_message(reason: str) -> str:
    """Return a user-facing Chinese message that never leaks URL/exception data."""

    return REASON_TEXT.get(str(reason or ""), "无法读取该网页。")


def is_public_ip(value: object) -> bool:
    """True only for a globally routable IP address (IPv4-mapped aware)."""

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
        raise WebFetchError("invalid_url")
    if host in WEB_FETCH_BLOCKED_HOSTS or host.endswith(
        WEB_FETCH_BLOCKED_HOST_SUFFIXES
    ):
        raise WebFetchError("blocked_host")
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip = None
    if ip is not None:
        if not is_public_ip(ip):
            raise WebFetchError("blocked_ip")
        return str(ip)
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise WebFetchError("invalid_url") from exc
    if not _HOSTNAME_RE.fullmatch(host):
        raise WebFetchError("invalid_url")
    return host


def _host_allowed(host: str, allowed_hosts: object) -> bool:
    """Match a host against an optional exact/``*.suffix`` allowlist."""

    if not allowed_hosts:
        return True
    if isinstance(allowed_hosts, str):
        patterns = [allowed_hosts]
    else:
        try:
            patterns = [str(item) for item in allowed_hosts]
        except TypeError:
            return False
    normalized = str(host or "").lower().rstrip(".")
    for pattern in patterns:
        candidate = pattern.strip().lower().rstrip(".")
        if not candidate:
            continue
        if candidate.startswith("*."):
            if normalized.endswith(candidate[1:]):
                return True
        elif normalized == candidate:
            return True
    return False


def validate_fetch_url(
    raw: object,
    *,
    allow_http: bool = False,
    allowed_hosts: object = None,
) -> str:
    """Return a normalized public HTTPS URL or raise :class:`WebFetchError`."""

    value = str(raw or "").strip()
    if not value or len(value) > WEB_FETCH_MAX_URL_CHARS:
        raise WebFetchError("invalid_url")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise WebFetchError("invalid_url")
    if "\\" in value:
        raise WebFetchError("invalid_url")
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise WebFetchError("invalid_url") from exc
    scheme = parts.scheme.lower()
    allowed_schemes = {"https", "http"} if allow_http else WEB_FETCH_ALLOWED_SCHEMES
    if scheme not in allowed_schemes:
        raise WebFetchError("invalid_url")
    host = _normalized_host(parts.hostname or "")
    if not _host_allowed(host, allowed_hosts):
        raise WebFetchError("blocked_host")
    if parts.username is not None or parts.password is not None:
        raise WebFetchError("invalid_url")
    if port is not None and not 1 <= port <= 65535:
        raise WebFetchError("invalid_url")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    return urllib.parse.urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def resolve_public_addresses(host: str, port: int) -> list[str]:
    """Resolve a host once and require every answer to be globally routable."""

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError) as exc:
        raise WebFetchError("dns_failed") from exc
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0]).split("%", 1)[0]
        if not is_public_ip(address):
            raise WebFetchError("blocked_ip")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise WebFetchError("dns_failed")
    return addresses


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Connect to an already-validated IP so DNS cannot rebind mid-flight."""

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


@dataclass(slots=True)
class HopResult:
    status: int
    location: str
    content_type: str
    data: bytes


@dataclass(slots=True)
class PageResult:
    url: str
    title: str
    text: str
    truncated: bool
    content_type: str


def is_allowed_content_type(raw: object) -> bool:
    value = str(raw or "").split(";", 1)[0].strip().lower()
    if not value:
        return False
    if value in WEB_FETCH_ALLOWED_CONTENT_TYPES:
        return True
    return value.endswith("+json") or value.endswith("+xml")


def decode_body(data: bytes, content_type: str) -> str:
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


def normalize_text(raw: str) -> str:
    text = _CONTROL_RE.sub(
        "",
        str(raw or "").replace("\r\n", "\n").replace("\r", "\n"),
    )
    lines = [_HORIZONTAL_SPACE_RE.sub(" ", line).strip() for line in text.split("\n")]
    joined = "\n".join(line for line in lines if line)
    return re.sub(r"\n{3,}", "\n\n", joined).strip()


class _VisibleTextParser(HTMLParser):
    SKIP_TAGS = frozenset(
        {
            "script",
            "style",
            "noscript",
            "template",
            "svg",
            "math",
            "iframe",
            "object",
            "embed",
            "canvas",
        }
    )
    BLOCK_TAGS = frozenset(
        {
            "address",
            "article",
            "aside",
            "blockquote",
            "br",
            "dd",
            "div",
            "dl",
            "dt",
            "fieldset",
            "figcaption",
            "figure",
            "footer",
            "form",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "hr",
            "li",
            "main",
            "nav",
            "ol",
            "option",
            "p",
            "pre",
            "section",
            "table",
            "tbody",
            "td",
            "tfoot",
            "th",
            "thead",
            "tr",
            "ul",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self.title_parts: list[str] = []
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
            return
        if tag in self.BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag.lower() in self.BLOCK_TAGS and not self._skip_depth:
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in self.BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        self.chunks.append(data)


def neutralize_tool_markers(text: object) -> str:
    """Break ``[[tool:``-style sequences in untrusted text."""

    count = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        if count > _TOOL_MARKER_MAX:
            return match.group(0)
        return "[ ["

    return _TOOL_MARKER_RE.sub(_replace, str(text or ""))


def html_to_text(html: str) -> tuple[str, str]:
    """Return ``(title, visible body text)`` from best-effort HTML parsing."""

    parser = _VisibleTextParser()
    try:
        parser.feed(str(html or ""))
        parser.close()
    except Exception:  # Malformed HTML must never break a fetch.
        pass
    title = neutralize_tool_markers(normalize_text("".join(parser.title_parts))[:200])
    body = neutralize_tool_markers(normalize_text("".join(parser.chunks)))
    return title, body


def truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    limit = max(1, int(max_chars or 1))
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip(), True


def build_page_result(
    url: str,
    raw_text: str,
    content_type: str,
    *,
    max_chars: int,
) -> PageResult:
    declared = str(content_type or "").split(";", 1)[0].strip().lower()
    if declared in {"text/html", "application/xhtml+xml"}:
        title, body = html_to_text(raw_text)
    else:
        title, body = "", normalize_text(raw_text)
    text, truncated = truncate_text(body, max_chars)
    return PageResult(
        url=url,
        title=title,
        text=text,
        truncated=truncated,
        content_type=declared,
    )


def format_page_result(page: PageResult) -> str:
    """Format a page as safe plain text for a QQ reply."""

    title = neutralize_tool_markers(page.title).strip()
    header = f"网页：{title}" if title else f"网页：{page.url}"
    body = neutralize_tool_markers(page.text).strip() or "（网页没有可读正文）"
    parts = [header, "", body]
    if page.truncated:
        parts.extend(["", "（内容已截断）"])
    return "\n".join(parts)


def _request_once(
    url: str,
    *,
    timeout: float,
    max_bytes: int,
    user_agent: str = DEFAULT_USER_AGENT,
) -> HopResult:
    """Perform one GET without following redirects.

    The DNS answer is validated and the TCP connection is pinned to the first
    validated address.  Redirects are returned to the caller and revalidated
    on the next loop iteration.
    """

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
    headers = dict(WEB_FETCH_HEADERS)
    headers["User-Agent"] = str(user_agent or DEFAULT_USER_AGENT)
    try:
        try:
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            status = int(response.status)
            if status in WEB_FETCH_REDIRECT_STATUSES:
                location = str(response.headers.get("Location", "") or "").strip()
                return HopResult(status, location, "", b"")
            if status != 200:
                return HopResult(status, "", "", b"")
            raw_type = str(response.headers.get("Content-Type", "") or "")
            if not is_allowed_content_type(raw_type):
                raise WebFetchError("bad_content_type")
            encoding = str(response.headers.get("Content-Encoding", "") or "").strip().lower()
            if encoding not in {"", "identity"}:
                raise WebFetchError("bad_encoding")
            length = str(response.headers.get("Content-Length", "") or "").strip()
            if length:
                try:
                    if int(length) > max_bytes:
                        raise WebFetchError("too_large")
                except ValueError:
                    pass
            data = response.read(max_bytes + 1) or b""
            if len(data) > max_bytes:
                raise WebFetchError("too_large")
            return HopResult(status, "", raw_type, bytes(data))
        except WebFetchError:
            raise
        except (socket.timeout, TimeoutError) as exc:
            raise WebFetchError("timeout") from exc
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise WebFetchError("network") from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass


def fetch_page_text(
    url: object,
    *,
    timeout: float = WEB_FETCH_DEFAULT_TIMEOUT,
    max_bytes: int = WEB_FETCH_DEFAULT_MAX_BYTES,
    max_chars: int = WEB_FETCH_DEFAULT_MAX_CHARS,
    max_redirects: int = WEB_FETCH_DEFAULT_MAX_REDIRECTS,
    allow_http: bool = False,
    user_agent: str = DEFAULT_USER_AGENT,
    allowed_hosts: object = None,
) -> PageResult:
    """Fetch one public text page; raise :class:`WebFetchError` on failure."""

    max_chars = max(1, min(int(max_chars or 1), WEB_FETCH_MAX_CHARS_HARD_LIMIT))
    max_bytes = max(
        1024,
        min(int(max_bytes or 1024), WEB_FETCH_MAX_BYTES_HARD_LIMIT),
    )
    max_redirects = max(
        0,
        min(int(max_redirects or 0), WEB_FETCH_MAX_REDIRECTS_HARD_LIMIT),
    )
    timeout = max(0.5, min(float(timeout or WEB_FETCH_DEFAULT_TIMEOUT), 300.0))
    deadline = time.monotonic() + timeout
    current = validate_fetch_url(
        url,
        allow_http=allow_http,
        allowed_hosts=allowed_hosts,
    )
    for hop in range(max_redirects + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0.05:
            raise WebFetchError("timeout")
        hop_result = _request_once(
            current,
            timeout=min(remaining, timeout),
            max_bytes=max_bytes,
            user_agent=user_agent,
        )
        if hop_result.status in WEB_FETCH_REDIRECT_STATUSES:
            if hop >= max_redirects:
                raise WebFetchError("too_many_redirects")
            if not hop_result.location:
                raise WebFetchError("bad_redirect")
            try:
                current = validate_fetch_url(
                    urllib.parse.urljoin(current, hop_result.location),
                    allow_http=allow_http,
                    allowed_hosts=allowed_hosts,
                )
            except WebFetchError as exc:
                raise WebFetchError("bad_redirect") from exc
            continue
        if hop_result.status != 200:
            raise WebFetchError("http_error")
        raw_text = decode_body(hop_result.data, hop_result.content_type)
        return build_page_result(
            current,
            raw_text,
            hop_result.content_type,
            max_chars=max_chars,
        )
    raise WebFetchError("too_many_redirects")

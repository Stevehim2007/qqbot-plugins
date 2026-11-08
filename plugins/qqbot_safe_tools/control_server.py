"""In-plugin asyncio loopback RPC bridge (127.0.0.1 only).

The bridge authenticates the complete JSON request with HMAC-SHA256, rejects
replayed nonces and stale timestamps, accepts only a fixed RPC method table and
returns no secret material.  HTTP/JSON handling stays on the plugin event loop;
this module has no shell, SQL, arbitrary file or arbitrary method surface.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import math
import os
import re
import socket
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

CONTROL_PROTOCOL_VERSION = 1
CONTROL_DEFAULT_PORT = 6198
CONTROL_HOST = "127.0.0.1"
CONTROL_MAX_BODY_BYTES = 65_536
CONTROL_MAX_RESPONSE_BYTES = 65_536
CONTROL_MAX_CLOCK_SKEW_SECONDS = 60
CONTROL_MAX_HEADER_BYTES = 8_192
CONTROL_MAX_NONCES = 4_096
CONTROL_METHODS = frozenset(
    {
        "health.status",
        "tool.catalog",
        "tool.dispatch",
        "confirmation.preview",
        "confirmation.confirm",
        "confirmation.cancel",
        "tool.operation_status",
        "policy.snapshot",
        "policy.scope",
        "policy.scope_reset",
        "policy.audit",
        "policy.templates",
        "policy.template_set",
        "policy.template_delete",
        "policy.template_apply",
        "policy.param_schema",
        "policy.set",
        "policy.reset",
        "aux_budget.get",
        "aux_budget.set",
        "aux_budget.usage",
        "aux_budget.feature_set",
        "aux_budget.cycle_reset",
    }
)
_REQUEST_FIELDS = frozenset(
    {
        "version",
        "instance_id",
        "actor_id",
        "scope",
        "method",
        "request_id",
        "nonce",
        "timestamp",
        "body",
        "digest",
        "hmac",
    }
)
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,256}$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_HEX256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SECRET_ENV = "QQBOT_SAFE_TOOLS_CONTROL_SECRET_FILE"
_SCOPES = frozenset({"global", "persona", "conversation", "group", "user"})
_FORBIDDEN_ARG_KEYS = frozenset(
    {"path", "file", "filename", "sql", "shell", "command", "script", "eval", "exec"}
)


class ControlError(ValueError):
    def __init__(self, code: str, message: str = "", *, http_status: int = 400) -> None:
        self.code = str(code)
        self.message = str(message or code)
        self.http_status = int(http_status)
        super().__init__(self.message)


class ControlConfigError(ControlError):
    pass


@dataclass(frozen=True, slots=True)
class ControlRequest:
    version: int
    instance_id: str
    actor_id: str
    scope: str
    method: str
    request_id: str
    nonce: str
    timestamp: int
    body: dict[str, Any]
    digest: str

    def validate(self) -> bool:
        if self.version != CONTROL_PROTOCOL_VERSION:
            raise ControlError("unsupported_version", http_status=400)
        if not _SAFE_ID_RE.fullmatch(self.instance_id):
            raise ControlError("invalid_instance", http_status=403)
        if not _SAFE_ID_RE.fullmatch(self.actor_id):
            raise ControlError("invalid_actor", http_status=403)
        if self.scope not in _SCOPES:
            raise ControlError("invalid_scope", http_status=403)
        if self.method not in CONTROL_METHODS:
            raise ControlError("unsupported_method", http_status=400)
        if not _SAFE_ID_RE.fullmatch(self.request_id):
            raise ControlError("invalid_request_id", http_status=400)
        if not _NONCE_RE.fullmatch(self.nonce):
            raise ControlError("invalid_nonce", http_status=400)
        if isinstance(self.timestamp, bool) or not isinstance(self.timestamp, int):
            raise ControlError("invalid_timestamp", http_status=401)
        if not isinstance(self.body, dict):
            raise ControlError("invalid_body", http_status=400)
        _validate_json_body(self.body)
        body_digest = hashlib.sha256(
            _canonical_json(self.body).encode("utf-8")
        ).hexdigest()
        if not hmac.compare_digest(self.digest.lower(), body_digest):
            raise ControlError("digest_mismatch", http_status=401)
        return True


class NonceCache:
    """Bounded in-memory nonce replay cache for the one plugin process."""

    def __init__(self, *, ttl_seconds: int = CONTROL_MAX_CLOCK_SKEW_SECONDS, max_items: int = CONTROL_MAX_NONCES) -> None:
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_items = max(16, int(max_items))
        self._items: dict[str, float] = {}
        self._lock = threading.RLock()

    def accept(self, nonce: str, now: float) -> bool:
        current = float(now)
        with self._lock:
            expired = [
                key for key, expiry in self._items.items() if float(expiry) <= current
            ]
            for key in expired:
                self._items.pop(key, None)
            if nonce in self._items:
                return False
            if len(self._items) >= self.max_items:
                oldest = min(self._items, key=self._items.get)
                self._items.pop(oldest, None)
            self._items[nonce] = current + self.ttl_seconds
            return True

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ControlError("invalid_json", http_status=400) from exc


def canonical_signing_payload(request: Mapping[str, Any]) -> str:
    """Canonical HMAC payload for every control field except ``hmac``."""

    try:
        unsigned = {key: value for key, value in request.items() if key != "hmac"}
    except AttributeError as exc:
        raise ControlError("invalid_json", http_status=400) from exc
    return _canonical_json(unsigned)


def request_digest(body: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(dict(body)).encode("utf-8")).hexdigest()


def sign_control_request(secret: str | bytes, request: Mapping[str, Any]) -> dict[str, Any]:
    key = _secret_bytes(secret)
    payload = dict(request)
    payload.pop("hmac", None)
    if "body" in payload:
        payload["digest"] = request_digest(payload["body"])
    signature = hmac.new(
        key, canonical_signing_payload(payload).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    result = dict(payload)
    result["hmac"] = signature
    return result


def _secret_bytes(secret: str | bytes) -> bytes:
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if not isinstance(secret, (bytes, bytearray)):
        raise ControlConfigError("invalid_secret")
    value = bytes(secret)
    if len(value) < 16:
        raise ControlConfigError("invalid_secret")
    return value


def _validate_json_body(value: Any, *, depth: int = 0, nodes: list[int] | None = None) -> None:
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if nodes[0] > 2_000 or depth > 8:
        raise ControlError("body_too_complex", http_status=400)
    if value is None or isinstance(value, (str, int, bool)):
        if isinstance(value, str) and len(value) > 8_192:
            raise ControlError("body_too_large", http_status=413)
        if isinstance(value, int) and not isinstance(value, bool):
            if abs(value) > 2**63 - 1:
                raise ControlError("invalid_json", http_status=400)
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ControlError("invalid_json", http_status=400)
        return
    if isinstance(value, list):
        if len(value) > 64:
            raise ControlError("body_too_complex", http_status=400)
        for item in value:
            _validate_json_body(item, depth=depth + 1, nodes=nodes)
        return
    if isinstance(value, dict):
        if len(value) > 64:
            raise ControlError("body_too_complex", http_status=400)
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                raise ControlError("invalid_json", http_status=400)
            if key.casefold() in _FORBIDDEN_ARG_KEYS or any(
                token in key.casefold() for token in ("sql", "shell", "command")
            ):
                raise ControlError("forbidden_argument", http_status=400)
            _validate_json_body(item, depth=depth + 1, nodes=nodes)
        return
    raise ControlError("invalid_json", http_status=400)


def _load_json_body(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > CONTROL_MAX_BODY_BYTES:
        raise ControlError("body_too_large", http_status=413)

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            bytes(raw).decode("utf-8"),
            object_pairs_hook=pairs_hook,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError) as exc:
        raise ControlError("invalid_json", http_status=400) from exc
    if not isinstance(value, dict):
        raise ControlError("invalid_json", http_status=400)
    if set(value) != _REQUEST_FIELDS:
        raise ControlError("invalid_fields", http_status=400)
    _validate_json_body(value)
    return value


def _check_allowlist(value: str, allowed: Iterable[str] | None) -> bool:
    if allowed is None:
        return True
    allowed_set = {str(item) for item in allowed}
    return "*" in allowed_set or value in allowed_set


def verify_control_request(
    secret: str | bytes,
    request: Mapping[str, Any],
    *,
    nonce_cache: NonceCache,
    expected_instance: str | None = None,
    allowed_instances: Iterable[str] | None = None,
    allowed_actors: Iterable[str] | None = None,
    allowed_scopes: Iterable[str] | None = None,
    now: float | None = None,
) -> ControlRequest:
    if not isinstance(request, Mapping):
        raise ControlError("invalid_json", http_status=400)
    if set(request) != _REQUEST_FIELDS:
        raise ControlError("invalid_fields", http_status=400)
    key = _secret_bytes(secret)
    version = request.get("version")
    if version != CONTROL_PROTOCOL_VERSION:
        raise ControlError("unsupported_version", http_status=400)
    instance_id = str(request.get("instance_id", ""))
    actor_id = str(request.get("actor_id", ""))
    scope = str(request.get("scope", ""))
    method = str(request.get("method", ""))
    request_id = str(request.get("request_id", ""))
    nonce = str(request.get("nonce", ""))
    timestamp = request.get("timestamp")
    body = request.get("body")
    digest = str(request.get("digest", ""))
    supplied_hmac = str(request.get("hmac", ""))
    if expected_instance is not None and instance_id != str(expected_instance):
        raise ControlError("instance_mismatch", http_status=403)
    if not _check_allowlist(instance_id, allowed_instances):
        raise ControlError("instance_not_allowed", http_status=403)
    if not _check_allowlist(actor_id, allowed_actors):
        raise ControlError("actor_not_allowed", http_status=403)
    if not _check_allowlist(scope, allowed_scopes) or scope not in _SCOPES:
        raise ControlError("scope_not_allowed", http_status=403)
    if method not in CONTROL_METHODS:
        raise ControlError("unsupported_method", http_status=400)
    if not _SAFE_ID_RE.fullmatch(instance_id) or not _SAFE_ID_RE.fullmatch(actor_id):
        raise ControlError("invalid_identity", http_status=403)
    if not _SAFE_ID_RE.fullmatch(request_id):
        raise ControlError("invalid_request_id", http_status=400)
    if not _NONCE_RE.fullmatch(nonce):
        raise ControlError("invalid_nonce", http_status=400)
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise ControlError("invalid_timestamp", http_status=401)
    now_value = float(now if now is not None else time.time())
    if abs(now_value - float(timestamp)) > CONTROL_MAX_CLOCK_SKEW_SECONDS:
        raise ControlError("timestamp_expired", http_status=401)
    if not isinstance(body, dict):
        raise ControlError("invalid_body", http_status=400)
    _validate_json_body(body)
    body_digest = request_digest(body)
    if not _HEX256_RE.fullmatch(digest) or not hmac.compare_digest(
        digest.lower(), body_digest
    ):
        raise ControlError("digest_mismatch", http_status=401)
    expected_hmac = hmac.new(
        key, canonical_signing_payload(request).encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not _HEX256_RE.fullmatch(supplied_hmac) or not hmac.compare_digest(
        supplied_hmac.lower(), expected_hmac
    ):
        raise ControlError("bad_signature", http_status=401)
    if not nonce_cache.accept(nonce, now_value):
        raise ControlError("nonce_replayed", http_status=409)
    parsed = ControlRequest(
        version=CONTROL_PROTOCOL_VERSION,
        instance_id=instance_id,
        actor_id=actor_id,
        scope=scope,
        method=method,
        request_id=request_id,
        nonce=nonce,
        timestamp=int(timestamp),
        body=dict(body),
        digest=digest.lower(),
    )
    parsed.validate()
    return parsed


def resolve_secret_file(
    *,
    environ: Mapping[str, str] | None = None,
    data_dir: str | os.PathLike[str] | None = None,
) -> Path:
    environment = os.environ if environ is None else environ
    configured = str(environment.get(_SECRET_ENV, "") or "").strip()
    if configured:
        return Path(configured).expanduser()
    if data_dir is not None:
        return Path(data_dir).expanduser() / "control.secret"
    raise ControlConfigError("secret_not_configured")


def load_control_secret(
    path: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    data_dir: str | os.PathLike[str] | None = None,
    min_bytes: int = 16,
) -> bytes:
    """Read a mode-0600 secret file; never return/log the path in responses."""

    target = Path(path) if path is not None else resolve_secret_file(
        environ=environ, data_dir=data_dir
    )
    try:
        file_stat = target.stat()
    except OSError as exc:
        raise ControlConfigError("secret_missing") from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise ControlConfigError("secret_not_regular_file")
    if stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise ControlConfigError("secret_permissions_not_0600")
    if file_stat.st_size <= 0 or file_stat.st_size > 4_096:
        raise ControlConfigError("secret_file_size_invalid")
    try:
        secret = target.read_bytes().strip()
    except OSError as exc:
        raise ControlConfigError("secret_unreadable") from exc
    if len(secret) < max(16, int(min_bytes)):
        raise ControlConfigError("secret_too_short")
    return secret


def write_control_secret_file(path: str | os.PathLike[str], secret: str | bytes) -> Path:
    """Provisioning helper used by tests/installers; writes atomically as 0600."""

    key = _secret_bytes(secret)
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    fd = os.open(str(temp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        data = key + b"\n"
        offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0:
                raise ControlConfigError("secret_write_failed")
            offset += written
    finally:
        os.close(fd)
    os.chmod(temp, 0o600)
    os.replace(temp, target)
    os.chmod(target, 0o600)
    return target


class ControlServer:
    """Asyncio loopback-only HTTP bridge with fixed authenticated methods."""

    def __init__(
        self,
        secret: str | bytes,
        handler: Callable[[ControlRequest], Any],
        *,
        port: int = CONTROL_DEFAULT_PORT,
        expected_instance: str | None = None,
        allowed_instances: Iterable[str] | None = None,
        allowed_actors: Iterable[str] | None = None,
        allowed_scopes: Iterable[str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._secret = _secret_bytes(secret)
        if not callable(handler):
            raise ControlConfigError("handler_required")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65_535:
            raise ControlConfigError("invalid_port")
        self.handler = handler
        self.port = port
        self.expected_instance = expected_instance
        self.allowed_instances = None if allowed_instances is None else tuple(allowed_instances)
        self.allowed_actors = None if allowed_actors is None else tuple(allowed_actors)
        self.allowed_scopes = None if allowed_scopes is None else tuple(allowed_scopes)
        self.clock = clock
        self.nonce_cache = NonceCache()
        self._server: asyncio.AbstractServer | None = None
        self._start_lock = asyncio.Lock()
        self._running = False

    @property
    def running(self) -> bool:
        return bool(self._running and self._server is not None)

    @property
    def bound_port(self) -> int | None:
        if self._server is None or not self._server.sockets:
            return None
        try:
            return int(self._server.sockets[0].getsockname()[1])
        except (OSError, IndexError, TypeError):
            return None

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "host": CONTROL_HOST,
            "port": self.bound_port if self.running else self.port,
            "protocol_version": CONTROL_PROTOCOL_VERSION,
            "methods": sorted(CONTROL_METHODS),
        }

    async def start(self) -> dict[str, Any]:
        async with self._start_lock:
            if self.running:
                return self.status()
            self._server = await asyncio.start_server(
                self._handle_client,
                host=CONTROL_HOST,
                port=self.port,
                limit=CONTROL_MAX_HEADER_BYTES,
            )
            self._running = True
            return self.status()

    async def stop(self) -> dict[str, Any]:
        async with self._start_lock:
            server = self._server
            self._server = None
            self._running = False
            if server is not None:
                server.close()
                await server.wait_closed()
            self.nonce_cache.clear()
            return self.status()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await asyncio.wait_for(
                self._serve_one(reader, writer), timeout=5.0
            )
        except asyncio.LimitOverrunError:
            await self._write_response(
                writer, 431, {"ok": False, "error": "headers_too_large"}
            )
        except (asyncio.TimeoutError, ConnectionError, OSError):
            await self._write_response(
                writer, 408, {"ok": False, "error": "request_timeout"}
            )
        except ControlError as exc:
            await self._write_response(
                writer,
                exc.http_status,
                {"ok": False, "error": exc.code},
            )
        except Exception:
            await self._write_response(
                writer, 500, {"ok": False, "error": "internal_error"}
            )
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _read_http_request(
        self, reader: asyncio.StreamReader
    ) -> tuple[str, str, dict[str, str], bytes]:
        header_bytes = await reader.readuntil(b"\r\n\r\n")
        if len(header_bytes) > CONTROL_MAX_HEADER_BYTES:
            raise ControlError("headers_too_large", http_status=431)
        try:
            text = header_bytes.decode("iso-8859-1")
            lines = text.split("\r\n")
            method, target, version = lines[0].split(" ", 2)
            if version not in {"HTTP/1.1", "HTTP/1.0"}:
                raise ValueError("version")
            if "?" in target or "#" in target:
                raise ControlError("invalid_path", http_status=400)
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if not line:
                    continue
                if ":" not in line:
                    raise ValueError("header")
                name, value = line.split(":", 1)
                name = name.strip().lower()
                if not name or name in headers or len(name) > 64 or len(value) > 4_096:
                    raise ValueError("header")
                headers[name] = value.strip()
        except ControlError:
            raise
        except ValueError as exc:
            raise ControlError("invalid_http", http_status=400) from exc
        length_text = headers.get("content-length", "0")
        if not length_text.isdigit():
            raise ControlError("invalid_length", http_status=400)
        length = int(length_text)
        if length > CONTROL_MAX_BODY_BYTES:
            raise ControlError("body_too_large", http_status=413)
        body = await reader.readexactly(length) if length else b""
        return method, target, headers, body

    async def _serve_one(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        method, target, headers, body = await self._read_http_request(reader)
        if target == "/health" and method in {"GET", "HEAD"}:
            await self._write_response(
                writer, 200, {"status": "ok"}, head_only=method == "HEAD"
            )
            return
        if target != "/rpc" or method != "POST":
            raise ControlError("not_found", http_status=404)
        content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ControlError("content_type_required", http_status=415)
        request_json = _load_json_body(body)
        request = verify_control_request(
            self._secret,
            request_json,
            nonce_cache=self.nonce_cache,
            expected_instance=self.expected_instance,
            allowed_instances=self.allowed_instances,
            allowed_actors=self.allowed_actors,
            allowed_scopes=self.allowed_scopes,
            now=self.clock(),
        )
        if request.method == "health.status":
            result: Any = self.status()
        else:
            result = self.handler(request)
            if inspect.isawaitable(result):
                result = await result
        if not isinstance(result, Mapping):
            raise ControlError("invalid_handler_result", http_status=500)
        rendered = _canonical_json(dict(result))
        if len(rendered.encode("utf-8")) > CONTROL_MAX_RESPONSE_BYTES:
            raise ControlError("response_too_large", http_status=500)
        await self._write_response(
            writer,
            200,
            {
                "ok": True,
                "request_id": request.request_id,
                "result": dict(result),
            },
        )

    async def _write_response(
        self,
        writer: asyncio.StreamWriter,
        status_code: int,
        payload: Mapping[str, Any],
        *,
        head_only: bool = False,
    ) -> None:
        reason = {
            200: "OK",
            400: "Bad Request",
            401: "Unauthorized",
            403: "Forbidden",
            404: "Not Found",
            408: "Request Timeout",
            409: "Conflict",
            413: "Payload Too Large",
            415: "Unsupported Media Type",
            431: "Request Header Fields Too Large",
            500: "Internal Server Error",
        }.get(int(status_code), "Error")
        try:
            body = _canonical_json(dict(payload)).encode("utf-8")
        except ControlError:
            body = b'{"ok":false,"error":"internal_error"}'
            status_code = 500
        response = (
            f"HTTP/1.1 {int(status_code)} {reason}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "Cache-Control: no-store\r\n"
            "\r\n"
        ).encode("ascii")
        writer.write(response)
        if not head_only:
            writer.write(body)
        await writer.drain()

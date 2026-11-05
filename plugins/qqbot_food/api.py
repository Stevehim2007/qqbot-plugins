"""Bounded, fixed-host Food API client (standard library only).

Security properties enforced here, independent of callers:

* every URL is built from the fixed ``https://food.dlsz.top`` base and a small
  allow-list of GET paths (plus the single POST endpoint);
* redirects are never followed, and the final URL is re-validated;
* responses have a hard byte cap and a timeout;
* successful responses must be JSON (or text/plain that parses as JSON);
* HTTP status errors are returned as ``(status, payload)`` for the POST path so
  callers can map them to Chinese text without accepting a fake success.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Callable, NamedTuple

from . import food

USER_AGENT = "QQBotStudio-Food/3.0"
class FoodTransportError(food.FoodAPIError):
    """Network / timeout / oversize / malformed response with a safe reason."""


class FoodTimeoutError(FoodTransportError):
    """The transport exceeded its timeout."""


class FoodResponseTooLarge(FoodTransportError):
    """The response exceeded the configured byte cap."""


class FoodHTTPError(food.FoodAPIError):
    """A non-200 GET response; keeps only the numeric status."""

    def __init__(self, status: int):
        super().__init__("food API returned HTTP %s" % int(status))
        self.status = int(status)


class TransportResult(NamedTuple):
    status: int
    headers: dict[str, str]
    body: bytes
    final_url: str


Transport = Callable[[urllib.request.Request, float, int], TransportResult]


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Return ``None`` for every redirect so urllib raises HTTPError instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _header(headers: dict[str, str], name: str) -> str:
    needle = str(name).casefold()
    for key, value in (headers or {}).items():
        if str(key).casefold() == needle:
            return str(value or "")
    return ""


def urllib_transport(
    request: urllib.request.Request,
    timeout: float,
    max_bytes: int,
) -> TransportResult:
    """Default blocking transport used in production and thread tests."""

    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(
                getattr(response, "status", 0)
                or getattr(response, "getcode", lambda: 0)()
                or 200
            )
            final_url = str(
                getattr(response, "geturl", lambda: request.full_url)()
                or request.full_url
            )
            headers = {
                str(key): str(value)
                for key, value in dict(
                    getattr(response, "headers", {}) or {}
                ).items()
            }
            length = _header(headers, "Content-Length")
            if length:
                try:
                    length_value = int(length)
                except ValueError:
                    length_value = 0
                if length_value > max_bytes:
                    raise FoodResponseTooLarge("response too large")
            body = response.read(max_bytes + 1) or b""
            if len(body) > max_bytes:
                raise FoodResponseTooLarge("response too large")
            return TransportResult(status, headers, body, final_url)
    except urllib.error.HTTPError as exc:
        headers = {
            str(key): str(value)
            for key, value in dict(getattr(exc, "headers", {}) or {}).items()
        }
        body = b""
        try:
            body = exc.read(max_bytes + 1) or b""
        except Exception:
            body = b""
        finally:
            try:
                exc.close()
            except Exception:
                pass
        if len(body) > max_bytes:
            body = b""
        final_url = str(
            getattr(exc, "url", request.full_url) or request.full_url
        )
        return TransportResult(
            int(getattr(exc, "code", 0) or 0),
            headers,
            body,
            final_url,
        )
    except (socket.timeout, TimeoutError) as exc:
        raise FoodTimeoutError("food API timeout") from None
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), (socket.timeout, TimeoutError)):
            raise FoodTimeoutError("food API timeout") from None
        raise FoodTransportError("food API network error") from None
    except OSError:
        raise FoodTransportError("food API network error") from None


class FoodAPIClient:
    """Thin async-friendly wrapper around the fixed Food API."""

    def __init__(
        self,
        *,
        timeout: float = 12,
        max_bytes: int = 500_000,
        transport: Transport | None = None,
        user_agent: str = USER_AGENT,
    ):
        self.timeout = max(0.1, float(timeout))
        self.max_bytes = max(1, int(max_bytes))
        self._transport = transport or urllib_transport
        self.user_agent = str(user_agent or USER_AGENT)

    # ---- validation / decoding -------------------------------------
    def _check_bounded(self, result: TransportResult) -> None:
        length = _header(result.headers, "Content-Length")
        if length:
            try:
                length_value = int(length)
            except ValueError:
                length_value = 0
            if length_value > self.max_bytes:
                raise FoodResponseTooLarge("response too large")
        body = result.body or b""
        if len(body) > self.max_bytes:
            raise FoodResponseTooLarge("response too large")

    def _validate_get_final(self, final_url: str) -> None:
        if not food.validate_food_final_url(final_url):
            raise FoodTransportError("food API redirect rejected")

    def _validate_post_final(self, final_url: str) -> None:
        if not food.validate_food_submit_url(final_url):
            raise FoodTransportError("food submit redirect rejected")

    def _decode_json(self, result: TransportResult) -> Any:
        self._check_bounded(result)
        content_type = _header(result.headers, "Content-Type")
        if content_type:
            lowered = content_type.casefold()
            if "json" not in lowered and not lowered.startswith("text/"):
                raise FoodTransportError("food API content-type is not JSON")
        body = result.body or b""
        if isinstance(body, str):
            body = body.encode("utf-8")
        if not isinstance(body, (bytes, bytearray)):
            raise FoodTransportError("food API returned an invalid body")
        if not body.strip():
            raise FoodTransportError("food API returned an empty body")
        charset = "utf-8"
        if "charset=" in content_type.casefold():
            try:
                charset = content_type.casefold().split("charset=", 1)[1].split(";", 1)[0].strip() or "utf-8"
            except Exception:
                charset = "utf-8"
        try:
            text = body.decode(charset, errors="replace")
        except (LookupError, ValueError, TypeError):
            text = body.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            raise FoodTransportError("food API returned non-JSON") from None

    def _decode_error_json(self, result: TransportResult) -> Any:
        self._check_bounded(result)
        body = result.body or b""
        if isinstance(body, str):
            body = body.encode("utf-8")
        if not isinstance(body, (bytes, bytearray)):
            return {}
        if not body.strip():
            return {}
        content_type = _header(result.headers, "Content-Type")
        lowered_type = content_type.casefold()
        if (
            lowered_type
            and "json" not in lowered_type
            and not lowered_type.startswith("text/")
        ):
            return {}
        try:
            return json.loads(body.decode("utf-8", errors="replace"))
        except (TypeError, ValueError):
            return {}

    def _request_headers(self, method: str) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Accept-Encoding": "identity",
        }
        if method == "POST":
            headers["Content-Type"] = "application/json; charset=utf-8"
        return headers

    # ---- public operations -----------------------------------------
    def get_json(self, path: str, params: dict[str, Any]) -> Any:
        if path not in food.FOOD_ALLOWED_PATHS:
            raise food.FoodAPIError("unsupported food API path")
        url = food.build_food_url(path, params)
        if not food.validate_food_final_url(url):
            raise FoodTransportError("invalid food API URL")
        request = urllib.request.Request(
            url,
            headers=self._request_headers("GET"),
            method="GET",
        )
        result = self._transport(request, self.timeout, self.max_bytes)
        self._validate_get_final(result.final_url or url)
        if int(result.status) != 200:
            raise FoodHTTPError(int(result.status))
        return self._decode_json(result)

    def post_submit(self, payload: dict[str, Any]) -> tuple[int, Any]:
        """POST the one fixed upload endpoint; never follows redirects.

        HTTP error statuses are returned to the caller as ``(status, body)``
        so known site error codes can be mapped.  Transport-level failures
        (timeout, DNS, oversize, redirect, non-JSON success) raise a safe
        ``FoodTransportError``.
        """

        if not isinstance(payload, dict):
            raise FoodTransportError("invalid food submit payload")
        url = food.FOOD_SUBMIT_URL
        if not food.validate_food_submit_url(url):
            raise FoodTransportError("invalid food submit URL")
        try:
            body = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise FoodTransportError("invalid food submit payload") from None
        request = urllib.request.Request(
            url,
            data=body,
            headers=self._request_headers("POST"),
            method="POST",
        )
        result = self._transport(request, self.timeout, self.max_bytes)
        # A redirect (or proxy rewrite) must never receive the password body
        # or be treated as a success.  urllib's default transport does not
        # follow redirects; this also covers injected/custom transports.
        self._validate_post_final(result.final_url or url)
        self._check_bounded(result)
        if int(result.status) == 200:
            return 200, self._decode_json(result)
        return int(result.status), self._decode_error_json(result)

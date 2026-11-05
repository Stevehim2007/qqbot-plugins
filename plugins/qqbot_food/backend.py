"""Single remote Food backend adapter used by the ABI v1 provider.

Only the fixed Food API read paths and the single upload POST are reachable
here.  URL construction/validation lives in ``food.py`` and ``api.py``; this
module maps a bounded remote result to a structured outcome and never retries
a write, never logs a password and never exposes a raw exception.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import food
from .api import (
    FoodHTTPError,
    FoodResponseTooLarge,
    FoodTimeoutError,
    FoodTransportError,
)

GetJson = Callable[[str, dict[str, Any]], Awaitable[Any]]
PostSubmit = Callable[[dict[str, Any]], Awaitable[tuple[int, Any]]]

_SENSITIVE_KEYS = {
    "password",
    "password_hash",
    "secret",
    "token",
    "api_key",
    "authorization",
}
_MAX_REMOTE_TEXT = 200


@dataclass(frozen=True)
class BackendOutcome:
    state: str                 # succeeded / rejected / outcome_unknown
    remote_code: str = ""
    remote_data: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    message: str = ""
    retryable: bool = False
    side_effect_state: str = "none"
    already_submitted: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "remote_code": self.remote_code,
            "remote_data": dict(self.remote_data),
            "error_code": self.error_code,
            "message": self.message,
            "retryable": self.retryable,
            "side_effect_state": self.side_effect_state,
            "already_submitted": self.already_submitted,
            "evidence": dict(self.evidence),
        }


def _safe_text(value: Any, limit: int = _MAX_REMOTE_TEXT) -> str:
    text = food._safe_display(value, limit=limit)
    lowered = text.casefold()
    if any(
        token in lowered
        for token in ("http://", "https://", "file:", "javascript:", "data:")
    ):
        return "站点拒绝了请求"
    return text


def _sanitize_remote(value: Any, *, depth: int = 0) -> Any:
    if depth > 3:
        return ""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name.casefold() in _SENSITIVE_KEYS:
                continue
            result[name[:80]] = _sanitize_remote(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [
            _sanitize_remote(item, depth=depth + 1)
            for item in list(value)[:20]
        ]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _safe_text(value, limit=200)


class FoodBackend:
    def __init__(
        self,
        *,
        get_json: GetJson,
        post_submit: PostSubmit,
        timeout_seconds: float = 12.0,
    ):
        self._get_json = get_json
        self._post_submit = post_submit
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    async def get(self, path: str, params: dict[str, Any]) -> Any:
        if path not in food.FOOD_ALLOWED_PATHS:
            raise food.FoodAPIError("unsupported read path")
        return await self._get_json(path, params)

    async def submit(self, payload: dict[str, Any]) -> BackendOutcome:
        """POST once; classify proven rejection vs unknown side effect.

        No automatic retry: 5xx/3xx/timeout/oversize/transport errors all map
        to ``outcome_unknown`` because the remote may already have committed.
        """

        if not isinstance(payload, dict) or not payload:
            return BackendOutcome(
                state="rejected",
                error_code="invalid_args",
                message="提交参数不完整",
                retryable=True,
                side_effect_state="none",
            )
        if not food.validate_food_submit_url(food.FOOD_SUBMIT_URL):
            return BackendOutcome(
                state="rejected",
                error_code="invalid_args",
                message="提交地址不安全",
                retryable=True,
                side_effect_state="none",
            )
        try:
            status, body = await asyncio.wait_for(
                self._post_submit(payload),
                timeout=self.timeout_seconds + 2.0,
            )
        except FoodTimeoutError:
            return BackendOutcome(
                state="outcome_unknown",
                error_code="outcome_unknown",
                message="提交超时，结果未知，请勿自动重试",
                retryable=False,
                side_effect_state="unknown",
            )
        except FoodResponseTooLarge:
            return BackendOutcome(
                state="outcome_unknown",
                error_code="outcome_unknown",
                message="提交响应过大，结果未知，请勿自动重试",
                retryable=False,
                side_effect_state="unknown",
            )
        except (FoodTransportError, FoodHTTPError, food.FoodAPIError):
            return BackendOutcome(
                state="outcome_unknown",
                error_code="outcome_unknown",
                message="提交连接异常，结果未知，请勿自动重试",
                retryable=False,
                side_effect_state="unknown",
            )
        except asyncio.TimeoutError:
            return BackendOutcome(
                state="outcome_unknown",
                error_code="outcome_unknown",
                message="提交超时，结果未知，请勿自动重试",
                retryable=False,
                side_effect_state="unknown",
            )
        except Exception:
            return BackendOutcome(
                state="outcome_unknown",
                error_code="outcome_unknown",
                message="提交结果未知，请勿自动重试",
                retryable=False,
                side_effect_state="unknown",
            )

        parsed = food.parse_food_submit_response(status, body)
        remote_data = _sanitize_remote(body if isinstance(body, dict) else {})
        if parsed.get("ok"):
            return BackendOutcome(
                state="succeeded",
                remote_code="SUCCESS",
                remote_data=remote_data,
                message="提交成功",
                retryable=False,
                side_effect_state="committed",
                evidence={"success": True, "status": int(status)},
            )
        kind = str(parsed.get("kind", "") or "")
        remote_code = str(parsed.get("code", "") or kind).upper()
        if kind == "duplicate_username":
            # Explicit remote evidence: same restaurant + same username.  Only
            # callers whose payload hash matches this operation may turn this
            # into "already submitted".
            return BackendOutcome(
                state="succeeded",
                remote_code="DUPLICATE_USERNAME",
                remote_data=remote_data,
                message="站点显示该用户名已评价过这家店",
                retryable=False,
                side_effect_state="committed",
                already_submitted=True,
                evidence={
                    "duplicate_username": True,
                    "status": int(status),
                    "error_code": "DUPLICATE_USERNAME",
                },
            )
        if kind in (
            "missing_field",
            "invalid_score",
            "restaurant_not_found",
            "location_exists",
            "rejected",
        ):
            return BackendOutcome(
                state="rejected",
                remote_code=remote_code,
                remote_data=remote_data,
                error_code="site_" + kind,
                message=_safe_text(parsed.get("error")) or "站点拒绝了提交",
                retryable=True,
                side_effect_state="none",
                evidence={"status": int(status), "kind": kind},
            )
        # 3xx/5xx/unknown 2xx responses may have committed remotely.
        return BackendOutcome(
            state="outcome_unknown",
            remote_code=remote_code,
            remote_data=remote_data,
            error_code="outcome_unknown",
            message="站点未返回明确成功，结果未知，请勿自动重试",
            retryable=False,
            side_effect_state="unknown",
            evidence={"status": int(status), "kind": kind or "unknown"},
        )

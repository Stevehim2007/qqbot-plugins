"""Dedicated OpenAI-compatible auxiliary model client.

The API key is read from the Studio-managed ``qqbot_aux_ai.json`` file and is
never logged.  A missing/disabled configuration returns an empty string so the
caller can fall back to AstrBot's normal provider.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger("qqbot_memory_bank.aux_ai")


def settings_path(plugin_data_dir: Path) -> Path:
    return Path(plugin_data_dir) / "qqbot_aux_ai.json"


def load_settings(plugin_data_dir: Path) -> dict[str, Any]:
    path = settings_path(plugin_data_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def generate(
    *,
    plugin_data_dir: Path,
    system_prompt: str,
    prompt: str,
    max_tokens: int = 500,
    temperature: float = 0.1,
    timeout: float = 15.0,
) -> str:
    cfg = load_settings(plugin_data_dir)
    if not bool(cfg.get("enabled", False)):
        return ""
    key = str(cfg.get("api_key") or "").strip()
    if not key:
        return ""
    base = str(cfg.get("base_url") or "https://api.deepseek.com/v1").strip().rstrip("/")
    model = str(cfg.get("model") or "deepseek-chat").strip()
    if not base.startswith(("http://", "https://")) or not model:
        return ""
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": str(system_prompt or "")},
                {"role": "user", "content": str(prompt or "")},
            ],
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
            "stream": False,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        base + "/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST",
    )
    try:
        def _call() -> str:
            with urllib.request.urlopen(request, timeout=max(3.0, float(timeout))) as response:
                body = response.read().decode("utf-8", "replace")
            data = json.loads(body)
            choices = data.get("choices") if isinstance(data, dict) else None
            if not choices:
                return ""
            message = choices[0].get("message") or {}
            return str(message.get("content") or "").strip()

        return await asyncio.wait_for(asyncio.to_thread(_call), timeout=max(3.0, float(timeout)))
    except Exception:
        logger.warning("auxiliary AI request failed", exc_info=False)
        return ""

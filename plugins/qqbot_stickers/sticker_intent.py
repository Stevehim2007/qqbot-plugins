"""Conservative, deterministic natural-language sticker intent parsing.

The module never resolves or returns a filesystem path.  Callers must validate
the returned target against their own bounded category/tag schema before it is
used for a store operation.  The parser is deliberately narrow: unclear chat
is ignored rather than guessed.
"""

from __future__ import annotations

import re
from typing import Iterable

STICKER_REQUEST_RE = re.compile(
    r"^(?:(?:请|麻烦|可以|能不能|能|帮我|给我|你|我想|我要)\s*)*"
    r"(?:发|来|要|整|给)\s*"
    r"(?:一?个|一?张|几?个|一点|点)?\s*"
    r"(?P<target>.*?)\s*"
    r"(?:的)?\s*(?:表情包|表情|贴纸|图)"
    r"(?:吧|啊|呀|呗|嘛|哦|谢谢|行吗|可以吗)*"
    r"[!！。.？?~～\s]*$",
    re.IGNORECASE,
)

EMOTION_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("开心", "高兴", "快乐", "愉快", "哈哈"), "happy"),
    (("难过", "伤心", "悲伤", "不开心", "委屈"), "sad"),
    (("生气", "愤怒", "火大", "气死"), "angry"),
    (("思考", "想想", "沉思", "疑惑"), "thinking"),
    (("喜欢", "爱你", "爱心", "比心"), "love"),
    (("困", "睡觉", "睡", "累了", "晚安"), "sleepy"),
    (("惊讶", "震惊", "吃惊", "意外"), "surprised"),
    (("酷", "帅", "帅气", "得意"), "cool"),
)
SNARK_KEYWORDS = ("嘲讽", "阴阳", "无语", "鄙视", "挖苦", "阴阳怪气")
SNARK_EMOTIONS = ("angry", "cool")
GENERIC_TARGETS = {"表情", "表情包", "贴纸", "图"}
SAFE_CATEGORY_RE = re.compile(r"^[0-9A-Za-z_\-\u4e00-\u9fff]{1,32}$")

_COLLECT_ACTION_RE = re.compile(r"(?:收藏|保存)")
_COLLECT_PREFIX_RE = re.compile(
    r"^(?:帮我|给我|请(?:你)?|麻烦(?:你)?|你|我(?:想|要))\s*"
)
_COLLECT_OBJECT_PREFIX_RE = re.compile(
    r"^(?:把)?\s*(?:这个|这张|这条|这|它|刚才那个|上一张)?\s*"
    r"(?:表情包|表情|贴纸|图片|图)?\s*"
)
_COLLECT_REST_PREFIX_RE = re.compile(
    r"^(?:一下|起来|了)?\s*(?:到|去|为|进|放到|加进|归到|分类[:：]?)?\s*"
)
_COLLECT_REST_OBJECT_RE = re.compile(
    r"^(?:这个|这张|这条|这|它)?\s*(?:表情包|表情|贴纸|图片|图)?\s*"
)
_COLLECT_SUFFIX_RE = re.compile(
    r"(?:这个|这张|这条|这|它)?\s*(?:表情包|表情|贴纸|图片|图)?\s*"
    r"(?:一下|起来|了|吧|啦|呀|哦|嘛|谢谢|行吗|可以吗)?\s*$"
)
_COLLECT_NON_ACTION_RE = re.compile(
    r"^(?:夹|栏|列表|功能|按钮|目录|在哪|怎么|如何|吗|么)"
)


def _clean_target(raw: str) -> str:
    target = str(raw or "").strip()
    target = re.sub(r"^(?:一?个|一?张|几?个)\s*", "", target).strip()
    target = re.sub(r"(?:的|啊|呀|吧|呗|嘛|哦)\s*$", "", target).strip()
    return target.strip("，。！？!?,.、:：;；\"'“”‘’ ")


def _fold_names(values: Iterable[str]) -> tuple[set[str], dict[str, str]]:
    names: set[str] = set()
    mapping: dict[str, str] = {}
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        folded = text.casefold()
        names.add(folded)
        mapping.setdefault(folded, text)
    return names, mapping


def resolve_sticker_target(
    target: str,
    *,
    available_emotions: Iterable[str],
    available_categories: Iterable[str],
) -> dict[str, str]:
    clean = _clean_target(target)
    emotion_names, _emotion_mapping = _fold_names(available_emotions)
    category_names, category_mapping = _fold_names(available_categories)
    clean_folded = clean.casefold()
    if not clean:
        return {"kind": "generic", "target": ""}
    if clean_folded in category_names:
        return {
            "kind": "category",
            "value": category_mapping[clean_folded],
            "target": clean,
        }
    if clean_folded in emotion_names:
        return {"kind": "emotion", "value": clean_folded, "target": clean}
    for keywords, emotion in EMOTION_KEYWORDS:
        if any(keyword in clean for keyword in keywords):
            if emotion in emotion_names:
                return {"kind": "emotion", "value": emotion, "target": clean}
            return {"kind": "not_found", "target": clean}
    if any(keyword in clean for keyword in SNARK_KEYWORDS):
        for emotion in SNARK_EMOTIONS:
            if emotion in emotion_names:
                return {"kind": "emotion", "value": emotion, "target": clean}
        return {"kind": "not_found", "target": clean}
    if clean in GENERIC_TARGETS:
        return {"kind": "generic", "target": ""}
    return {"kind": "not_found", "target": clean}


def detect_sticker_request(
    text: str,
    *,
    available_emotions: Iterable[str],
    available_categories: Iterable[str],
) -> dict[str, str] | None:
    value = str(text or "").strip()
    if not value:
        return None
    match = STICKER_REQUEST_RE.fullmatch(value)
    if match is None:
        return None
    return resolve_sticker_target(
        match.group("target"),
        available_emotions=available_emotions,
        available_categories=available_categories,
    )


def detect_collect_request(text: str) -> dict[str, str] | None:
    """Detect an explicit ``收藏/保存 [picture] [到分类]`` request.

    Returns ``{"target": ..., "category": ...}``.  An empty category means the
    caller should use its configured default category.  A target is only
    returned when it is a single bounded safe token; callers still normalize
    and length-check it before persistence.
    """

    value = re.sub(r"\s+", " ", str(text or "")).strip()
    value = value.strip("，。！？!?,.、:：;；\"'“”‘’ ")
    if not value or value.startswith(
        ("收藏夹", "收藏栏", "收藏列表", "收藏功能", "保存列表")
    ):
        return None
    match = _COLLECT_ACTION_RE.search(value)
    if match is None:
        return None

    prefix = value[: match.start()].strip()
    prefix = _COLLECT_PREFIX_RE.sub("", prefix).strip()
    prefix = _COLLECT_OBJECT_PREFIX_RE.sub("", prefix).strip()
    # Anything left before the action is ordinary prose, not a command.
    if prefix:
        return None

    rest = value[match.end() :].strip()
    if ".." in rest or "\\" in rest or rest.startswith(("/", "\\")):
        return None
    rest = _COLLECT_REST_PREFIX_RE.sub("", rest).strip()
    rest = _COLLECT_REST_OBJECT_RE.sub("", rest).strip()
    rest = _COLLECT_SUFFIX_RE.sub("", rest).strip()
    rest = rest.strip("，。！？!?,.、:：;；\"'“”‘’ ")
    # Drop a conjunction commonly left after the action.
    rest = re.sub(r"^(?:到|为|分类[:：]?)\s*", "", rest).strip()
    if not rest:
        return {"target": "", "category": ""}
    if _COLLECT_NON_ACTION_RE.match(rest):
        return None
    parts = [
        part.strip()
        for part in re.split(r"[,，、;；|/]+", rest)
        if part.strip()
    ]
    if not parts or len(parts) > 8 or not all(
        SAFE_CATEGORY_RE.fullmatch(part) for part in parts
    ):
        return None
    return {"target": rest, "category": rest}


def detect_auto_collect_text(text: str) -> bool:
    """Return true only for a bare collect command with no category text."""

    value = re.sub(r"\s+", " ", str(text or "")).strip()
    value = value.strip("，。！？!?,.、:：;；\"'“”‘’ ")
    if not value:
        return False
    object_prefix = r"(?:这个|这张|这条|它)?\s*(?:表情包|表情|贴纸|图片|图)?"
    polite = r"(?:帮我|给我|请)?\s*(?:把)?\s*"
    action = r"(?:收藏|保存)(?:一下|起来|了)?(?:吧|啊|呀|呗|嘛|哦)?"
    return bool(
        re.fullmatch(
            rf"(?:{polite}{object_prefix}\s*{action}|"
            rf"{polite}{action}\s*{object_prefix})",
            value,
        )
    )

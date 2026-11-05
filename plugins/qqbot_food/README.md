# qqbot_food

Independent AstrBot >=4.29 plugin for searching `food.dlsz.top` and submitting a
restaurant review. It uses only the Python standard library plus AstrBot's
public plugin API. It never imports `qqbot_enhancer`, `qqbot_core_full` or any
other plugin source, never calls a chat/LLM provider, and only talks to the
fixed host `https://food.dlsz.top`.

## Commands

```text
/food 火锅
/food 南关区 火锅
/food 火锅 南关区
/food --tag 火锅 --location 长春市 --sort taste --order asc --limit 3
/吃什么 烤肉
/饭店 海底捞
```

The parser is deterministic:

- positional forms support the legacy `name location` and `location name`
  shapes; a token ending in 区/县/市/镇/乡/街道/路/街/省/州 is treated as a
  location, otherwise the two-token fallback is name then location;
- flags: `--name/-q`, `--tag/-t`, `--location/-l`, `--sort/-s`,
  `--order/-o`, `--limit/-n` (also `--flag=value`), `#标签`, `tag:标签`;
- sorts: `total`(default), `taste`, `value`, `reviews`, `name`, `latest`;
- orders: `asc`/`desc` (name defaults asc, other sorts default desc);
- limits are clamped to 1–5 and output is hard-capped by
  `food_reply_max_chars`;
- tags and locations are searched via `/api/tags/search` and
  `/api/locations/search`; ambiguous/not-found values ask for a clearer value
  instead of guessing.

Group location context is read from `food_group_locations` (JSON object
mapping group id to a path such as `{"123456789":"吉林省/长春市"}`). If the
group is not mapped and `food_group_location_infer_from_name` is on, a group
name containing an explicit 省/市/州 segment is used. The context is used for
searches without an explicit location and is merged into submitted addresses.

## Review submission via qqbot_safe_tools

`qqbot_safe_tools` dispatches markers to these stable methods:

```python
async def tool_search_restaurants(self, event, args: dict[str, str], clean_text: str)
async def tool_submit_food_review(self, event, args: dict[str, str], clean_text: str)
```

Both return an AstrBot `event.plain_result(...)` or `None` and never raise.

`submit_food_review` accepts `restaurant`, `location`, `taste`, `value`,
`comment` and optional `tags`. It does **not** accept a URL, password, date or
other fields. Valid arguments are persisted as a per-sender draft and the
plugin asks for confirmation; it never POSTs from the tool call itself. A bare
`确认` / `提交` / `要` / `添加` message for the same sender in the same session
then submits the pending draft without invoking the LLM. A repeated
confirmation can never POST twice: the draft moves atomically through
`pending_confirmation -> submitting -> submitted`, and a site
`DUPLICATE_USERNAME` response is treated as already submitted.

A draft is keyed by a SHA-256 digest of `unified_msg_origin + sender_id`, so
one sender cannot consume another sender's draft. If the sender id is empty,
there is no draft and no submission. Drafts expire after
`food_submit_draft_ttl_seconds`.

## Fixed API and security

Reads only use these fixed HTTPS GET paths:

- `/api/locations/search?q=...`
- `/api/restaurants?q=...&search_type=name&location_id=...&tag_ids=...`
- `/api/tags` and `/api/tags/search?q=...`

Writes use one endpoint only: `POST https://food.dlsz.top/api/upload`.

- No user-controlled URL/host/path; parameters are URL-encoded.
- Redirects are never followed and the final URL is re-validated.
- Requests have a configurable timeout and response byte cap; successful JSON
  is required.
- Payload fields are allow-listed and bounded. `tags` is always an array;
  `review_date` is always today's local date.
- `username` comes from the sender nickname (fallback configured);
  `password` is injected as the sender QQ id only at POST time. It never enters
  a tool argument, draft file, log line, reply or URL.
- 400/404/500 and known site codes (`DUPLICATE_USERNAME`,
  `LOCATION_EXISTS`, `RESTAURANT_NOT_FOUND`, `MISSING_*`, `INVALID_*`) map to
  bounded Chinese messages. Success is claimed only for
  `HTTP 200 + success: true`.

## Data

State is stored in AstrBot's plugin data directory, normally
`data/plugin_data/qqbot_food/food_state.json`. It contains only per-sender draft
fields, statuses and cooldown timestamps; no sender QQ, password or API secret.
Writes use a process lock, best-effort OS file lock, temp file, `fsync` and
`os.replace`.

## Config

See `_conf_schema.json`. It covers command enable/timeout/bytes/results/
cooldowns/keyword/location limits, group locations/name inference, read
replies, safe-tool search limits/sort defaults/replies, submit enable/timeout/
bytes/cooldown/draft TTL, all field limits, confirmation/duplicate/error
replies, and the legacy `food_nl_*` knobs (kept for config compatibility; this
plugin itself does not run LLM-style natural-language participation).

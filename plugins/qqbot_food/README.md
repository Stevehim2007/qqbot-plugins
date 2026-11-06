# qqbot_food 3.0.1

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
replies. Natural-language Food queries are owned by `qqbot_core_reply` /
`qqbot_safe_tools`; this plugin exposes only the `tool_search_restaurants`
provider entry.

## Canonical commands (ABI v1)

The root is `/food`, with `/美食` as the root alias.  Old `/吃什么`, `/饭店`
and legacy `/food <关键词>` search remain compatible.

```text
/food 查询 火锅
/food 位置 南关区
/food 标签 火锅
/food 提交 店名=A火锅 位置ID=10 味道=4.5 性价比=3.5 评价=好吃 标签=火锅
/food 确认 DRAFT_ID
/food 取消 DRAFT_ID
/food 状态 OP_ID
/美食 查询 火锅
/吃什么 火锅
```

`/food 提交` creates a persistent draft and returns its `DRAFT_ID`; it does
not submit.  `/food 确认 DRAFT_ID` consumes the current confirmation token for
that draft.  A draft must be previewed before confirmation, and an old token
becomes invalid whenever the payload or revision changes.

## Provider ABI v1

`contracts.py` defines protocol/ABI version 1, `RequestContext`,
`ProviderResult`, the HMAC confirmation-token issuer and an in-process
`ProviderRegistry`.  `main.py` registers exactly one `food` provider owned by
`qqbot_food`, marks readiness only after initialization, rejects duplicate
owners/ABI mismatches, and unregisters/closes the ledger in `terminate()`.

Fixed provider methods (each receives `request_context, args` and returns a
contract `status/data` dict; no URL, SQL, method name or free function is
accepted from a caller):

```python
food_query, food_locations, food_tags, food_set_location, food_change_tags,
food_draft_create, food_draft_preview, food_confirm, food_cancel,
food_operation_status
```

The existing safe-tool methods `tool_search_restaurants` and
`tool_submit_food_review` remain available for `qqbot_safe_tools`.

## Draft/operation state machine

```text
collecting -> draft -> awaiting_confirmation -> confirmed -> submitting
           -> succeeded / rejected / outcome_unknown
cancelled / expired are explicit branches
```

Drafts carry `draft_id`, actor/instance/bot/scope/persona, normalized payload,
payload hash, revision, timestamps and TTL.  Confirmation tokens bind draft id,
revision, payload hash, actor, scope, persona and expiry.  The SQLite CAS in
`ledger.py` ensures one concurrent confirmation can execute the remote POST.
Same idempotency key + same payload replays the stored result; a different
payload returns a 409-style `idempotency_conflict`.

A timeout, oversized response, 5xx, redirect or other ambiguous outcome becomes
`outcome_unknown`, is preserved across restart and is never automatically
resent.  Only explicit 4xx site rejections proven to have no side effect are
marked retryable.  A site `DUPLICATE_USERNAME` is treated as already submitted
only when that explicit evidence matches the operation payload.

## Data

- Legacy JSON confirmation state: `food_state.json` (existing safe-tool flow).
- ABI draft/operation ledger: `food_ledger.sqlite3` in the same plugin data
  directory, WAL mode, parameterized SQL, atomic transactions and mode `0600`.
  Unknown/in-flight operations are fail-closed on startup.
- The ledger is separate from qqbot_safe_tools' confirmation/dispatch ledger.

## Config groups (flat-schema equivalents)

Nested configuration is honored when the injected config provides it; the flat
`_conf_schema.json` also exposes equivalent keys while preserving every old
`food_*`/`food_submit_*`/`food_search_*` setting:

- query: `query_limit`, `query_name_max_chars`, `query_default_location`,
  `query_default_tags`, `query_cache_ttl_seconds`
- submit: `submit_backend_id`, `submit_timeout_seconds`,
  `submit_confirm_required`, `submit_idempotency_mode`,
  `submit_unknown_result_mode`, `submit_username_max_chars`
- drafts: `drafts_ttl_seconds`, `drafts_confirmation_ttl_seconds`,
  `draft_name_max_chars`, `draft_location_max_chars`,
  `draft_comment_max_chars`
- location/tags: `location_visibility`, `tags_max_count`, `tags_name_length`,
  `policy_id`
- provider: `provider_instance_id`, `provider_owner_epoch`

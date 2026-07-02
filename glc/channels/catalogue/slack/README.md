# Slack Events API adapter

A wire-format translator between the Slack Events API / Web API and the GLC
channel envelope (`glc.channels.envelope`). It implements the two halves of the
`ChannelAdapter` contract:

- `on_message(raw) -> ChannelMessage | None` — parse an Events API callback into
  a `ChannelMessage` (or drop it, returning `None`, when an untrusted sender is
  filtered out by the public-channel allowlist).
- `send(reply) -> Any` — turn a `ChannelReply` into a `chat.postMessage` body and
  dispatch it.

The adapter is **not** an HTTP server. Slack pushes events as signed POSTs, so an
integrator supplies a thin endpoint around the adapter (see below).

## Architecture

```
Slack ──signed POST──▶ your HTTP endpoint
                          │  1. verify_slack_signature(...)      (reject 403 on fail)
                          │  2. handle_url_verification(raw)      (echo challenge)
                          │  3. Adapter.on_message(raw)           ──▶ ChannelMessage | None
                          ▼
                       agent runtime
                          │
                          ▼
                       Adapter.send(ChannelReply) ──▶ chat.postMessage ──▶ Slack
```

Files in this slot:

- `adapter.py` — the `Adapter` class plus the module-level helpers
  `verify_slack_signature` and `handle_url_verification`.
- `schemas.py` — `SlackEventCallback`, `SlackMessageEvent`, `SlackPostMessage`
  (narrow projections of the wire formats; the canonical envelope is **not**
  redefined here).

## Environment variables

- `SLACK_BOT_TOKEN` — bot token (`xoxb-...`) used by `send` to call
  `chat.postMessage` and `conversations.open`. When unset (or when a `mock`
  transport is injected) `send` returns the payload instead of making a network
  call.
- `SLACK_SIGNING_SECRET` — app signing secret, passed to
  `verify_slack_signature` by your endpoint.

## HTTP Events API receiver (reference)

The adapter ships the verification + handshake helpers; you own the ~30-line
endpoint. A FastAPI example:

```python
import json
import os

from fastapi import FastAPI, Header, HTTPException, Request, Response

from glc.channels.catalogue.slack.adapter import (
    Adapter,
    handle_url_verification,
    verify_slack_signature,
)
from glc.channels.envelope import ChannelReply

app = FastAPI()
adapter = Adapter(config={"is_public_channel": False})
SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]


@app.post("/slack/events")
async def slack_events(
    request: Request,
    x_slack_signature: str | None = Header(default=None),
    x_slack_request_timestamp: str | None = Header(default=None),
):
    body = await request.body()  # raw bytes — sign over these, not re-serialised JSON

    if not verify_slack_signature(
        SIGNING_SECRET, x_slack_request_timestamp, x_slack_signature, body
    ):
        raise HTTPException(status_code=403, detail="bad Slack signature")

    raw = json.loads(body or b"{}")

    # One-time endpoint handshake.
    challenge = handle_url_verification(raw)
    if challenge is not None:
        return Response(content=challenge, media_type="text/plain")

    # Ack within 3s; do slow work out of band in production.
    msg = await adapter.on_message(raw)
    if msg is None:
        return {"ok": True, "dropped": True}

    if msg.trust_level != "untrusted":
        answer = await ask_agent(msg.text or "")  # your agent call
        await adapter.send(
            ChannelReply(
                channel="slack",
                channel_user_id=msg.channel_user_id,
                text=answer,
                thread_id=msg.thread_id,  # keep the reply in-thread
            )
        )
    return {"ok": True}
```

Notes:

- **Sign over the raw body.** `verify_slack_signature` computes
  HMAC-SHA256 over `v0:{timestamp}:{body}` and rejects timestamps older than
  5 minutes (replay guard). Re-serialising the parsed JSON will change the bytes
  and fail verification.
- **Respond fast.** Slack retries if you do not return within 3 seconds; offload
  the agent call to a task queue for production traffic.

## Threading (`thread_ts`)

Slack threads live entirely in the `thread_ts` field:

- Inbound: `on_message` copies `event.thread_ts` into `ChannelMessage.thread_id`.
- Outbound: set `ChannelReply.thread_id` and `send` writes it back as
  `thread_ts` on the `chat.postMessage` body, so the reply lands in the same
  thread. Drop it and the reply appears as a new top-level message.

## `message` vs `app_mention`

Both event types are accepted. The adapter computes `was_mentioned`:

- `True` for `app_mention` events, or
- `True` when the bot's user id appears as `<@Uxxx>` in a `message` event's text.

The bot id is taken from `config["bot_user_id"]` if set, otherwise from the
event's `authorizations[].user_id` (where `is_bot` is true). `was_mentioned` is
recorded in `ChannelMessage.metadata` and feeds the public-channel allowlist.

## Trust posture & allowlist

`on_message` calls `classify("slack", user_id)` to assign
`owner_paired` / `user_paired` / `untrusted`. When the adapter is configured with
`is_public_channel=True` and the sender is `untrusted`, it consults
`glc.security.allowlists.allowed(...)`. With the default
`mention_only_in_public: true`, an untrusted, unmentioned stranger in a public
channel is **silently dropped** (`on_message` returns `None`). In DMs / private
channels (`is_public_channel=False`) a stranger still yields an `untrusted`
envelope so the runtime can reply with a pairing prompt.

## Rate limits & `Retry-After`

Slack soft-limits posting to about 1 message/second/channel and returns HTTP 429
(or `{"ok": false, "error": "ratelimited"}`) when exceeded. `send` surfaces this
as a structured dict — `{"status": 429, "error": "ratelimited", "retry_after": ...}` —
so the caller can back off rather than silently dropping the reply.

## DM resolution

For a reply addressed to a `U...` user id with no cached conversation, `send`
calls `conversations.open` to obtain the real `D...` DM channel id (string
surgery on user ids does not yield valid channel ids). Ids that already start
with `C` / `D` / `G` are used as-is, and inbound events cache their
`channel` so subsequent replies skip the lookup.

## Tests

`tests/channels/test_slack.py` (7 tests) covers the owner/stranger trust paths,
wire-format `send`, disconnect handling, 429 propagation, public-channel
allowlist drop, and thread continuity. The mock at
`tests/channels/mocks/slack_mock.py` and the test file are fixed — do not edit
them.

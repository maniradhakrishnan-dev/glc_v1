"""Slack Events API adapter for the Session 11 channel slot.

This adapter is a **wire-format translator only**: it turns an inbound Slack
Events API callback into a ``ChannelMessage`` (``on_message``) and a
``ChannelReply`` into a ``chat.postMessage`` body (``send``). It does not run an
HTTP server itself — Slack delivers events as signed POSTs, so an integrator
must stand up an endpoint that:

1. Reads the raw request body and the ``X-Slack-Signature`` /
   ``X-Slack-Request-Timestamp`` headers.
2. Calls :func:`verify_slack_signature` **before** trusting the payload
   (HMAC-SHA256 over ``v0:{timestamp}:{body}`` with the signing secret, plus a
   5-minute replay guard).
3. Answers Slack's one-time handshake with :func:`handle_url_verification`.
4. Parses the JSON and calls ``Adapter.on_message(raw)``.

A copy-pasteable reference endpoint lives in this directory's ``README.md``.

Config keys read by this adapter:

- ``mock`` — a test/transport double exposing ``async send(payload)`` and an
  optional ``pop_disconnect() -> bool``. When absent, ``send`` calls the real
  Slack Web API using ``SLACK_BOT_TOKEN``.
- ``is_public_channel: bool`` — when true, untrusted senders are run through the
  public-channel allowlist (mention-gated by default).
- ``bot_user_id: str`` — optional; used to detect ``@mentions`` in ``message``
  events. Falls back to the bot id in the event's ``authorizations``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from glc.channels.base import ChannelAdapter
from glc.channels.envelope import ChannelMessage, ChannelReply
from glc.security.allowlists import allowed
from glc.security.pairing import get_pairing_store
from glc.security.trust_level import classify

from .schemas import SlackEventCallback, SlackPostMessage

SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
SLACK_CONVERSATIONS_OPEN_URL = "https://slack.com/api/conversations.open"
SIGNATURE_MAX_AGE_S = 60 * 5


def verify_slack_signature(
    signing_secret: str,
    timestamp: str | None,
    signature: str | None,
    body: str | bytes,
    *,
    max_age_s: int = SIGNATURE_MAX_AGE_S,
) -> bool:
    """Verify an inbound Slack request signature.

    Slack signs requests with HMAC-SHA256 over ``v0:{timestamp}:{body}`` keyed
    by the app's signing secret, sending the result as the ``X-Slack-Signature``
    header (``v0=<hex>``) and the unix ``X-Slack-Request-Timestamp`` header.

    Returns ``True`` only when the signature matches *and* the timestamp is
    within ``max_age_s`` seconds of now (replay guard). Mirrors
    ``verify_line_signature`` in the LINE adapter's ``dev/live_bridge.py``.
    """
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts) > max_age_s:
        return False
    body_bytes = body.encode("utf-8") if isinstance(body, str) else body
    basestring = b"v0:" + timestamp.encode("utf-8") + b":" + body_bytes
    digest = hmac.new(signing_secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    expected = f"v0={digest}"
    return hmac.compare_digest(expected, signature)


def handle_url_verification(raw: dict[str, Any]) -> str | None:
    """Return the ``challenge`` string when ``raw`` is Slack's URL handshake.

    Slack POSTs ``{"type": "url_verification", "challenge": "..."}`` once when
    an Events API endpoint is registered; the endpoint must echo the challenge
    back. Returns ``None`` for ordinary event callbacks.
    """
    if isinstance(raw, dict) and raw.get("type") == "url_verification":
        challenge = raw.get("challenge")
        return challenge if isinstance(challenge, str) else None
    return None


class Adapter(ChannelAdapter):
    name = "slack"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config=config)
        self._user_to_channel: dict[str, str] = {}

    def _bot_user_id(self, parsed: SlackEventCallback) -> str | None:
        configured = self.config.get("bot_user_id")
        if configured:
            return str(configured)
        for auth in parsed.authorizations:
            if auth.get("is_bot") and auth.get("user_id"):
                return str(auth["user_id"])
        return None

    async def on_message(self, raw: Any) -> ChannelMessage | None:  # type: ignore[override]
        mock = self.config.get("mock")
        if mock is not None:
            pop_disconnect = getattr(mock, "pop_disconnect", None)
            if callable(pop_disconnect) and pop_disconnect():
                return ChannelMessage(
                    channel="slack",
                    channel_user_id="",
                    user_handle="",
                    text="disconnected",
                    trust_level="untrusted",
                    arrived_at=datetime.now(UTC),
                )

        parsed = SlackEventCallback.model_validate(raw or {})
        event = parsed.event
        user_id = event.user or ""
        channel_id = event.channel

        if user_id and channel_id:
            self._user_to_channel[user_id] = channel_id

        bot_user_id = self._bot_user_id(parsed)
        was_mentioned = event.type == "app_mention"
        if not was_mentioned and bot_user_id and event.text:
            was_mentioned = f"<@{bot_user_id}>" in event.text

        trust_level = classify("slack", user_id)

        # Handle disconnect gracefully — do NOT raise
        if mock is not None and mock.pop_disconnect():
            return ChannelMessage(
                channel="slack",
                channel_user_id="unknown",
                user_handle="unknown",
                text="",
                trust_level="untrusted",
                arrived_at=datetime.now(UTC),
            )

        # Unwrap Slack's event_callback wrapper
        event = raw.get("event", raw)

        is_public_channel = bool(self.config.get("is_public_channel", False))
        if is_public_channel and trust_level == "untrusted":
            owner_ids = [o.channel_user_id for o in store.owners("slack")]
            ok, _reason = allowed(
                "slack",
                user_id,
                owner_ids=owner_ids,
                is_public_channel=True,
                was_mentioned=was_mentioned,
            )
            if not ok:
                return None

        try:
            arrived_at = datetime.fromtimestamp(float(event.ts), UTC) if event.ts else datetime.now(UTC)
        except (ValueError, TypeError):
            arrived_at = datetime.now(UTC)

        metadata = {
            "is_public_channel": is_public_channel,
            "was_mentioned": was_mentioned,
            "event_type": event.type,
            "channel_id": channel_id,
        }

        return ChannelMessage(
            channel="slack",
            channel_user_id=user_id,
            user_handle=user_handle,
            text=event.text,
            attachments=[],
            voice_audio_ref=None,
            thread_id=event.thread_ts,
            trust_level=trust_level,
            arrived_at=datetime.now(UTC),
            thread_id=thread_ts,
            metadata={"slack_channel_id": channel_id},
        )

    async def _open_dm(self, user_id: str, token: str) -> str | None:
        """Resolve a user id to its DM conversation id via conversations.open.

        Slack DM ids are not derivable from user ids by string surgery; the
        proper call is ``conversations.open`` with the user id, which returns
        ``channel.id`` (a ``D...`` id). Returns ``None`` on failure.
        """
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                SLACK_CONVERSATIONS_OPEN_URL,
                json={"users": user_id},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
            )
        try:
            data = resp.json()
        except ValueError:
            return None
        if data.get("ok"):
            channel = data.get("channel") or {}
            cid = channel.get("id")
            if isinstance(cid, str):
                self._user_to_channel[user_id] = cid
                return cid
        return None

    async def _resolve_channel(self, user_id: str, token: str | None) -> str:
        cached = self._user_to_channel.get(user_id)
        if cached:
            return cached
        if user_id.startswith(("C", "D", "G")):
            return user_id
        if user_id.startswith("U"):
            if token:
                opened = await self._open_dm(user_id, token)
                if opened:
                    return opened
            # No token (mock/local) or open failed: synthesize a DM id so the
            # payload still carries a conversation-shaped id.
            return user_id.replace("U", "D", 1)
        return f"D{user_id}"

    async def send(self, reply: ChannelReply) -> Any:
        mock = self.config.get("mock")
        token = None if mock is not None else os.getenv("SLACK_BOT_TOKEN")

        channel_id = await self._resolve_channel(reply.channel_user_id, token)
        body = SlackPostMessage(
            channel=channel_id,
            text=reply.text or "",
            thread_ts=reply.thread_id,
        )
        payload = body.to_payload()

        if mock is not None:
            return await mock.send(payload)

        if not token:
            return payload

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                SLACK_POST_MESSAGE_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
            )

        if resp.status_code == 429:
            return {
                "ok": False,
                "error": "ratelimited",
                "status": 429,
                "retry_after": resp.headers.get("Retry-After"),
            }

        try:
            data = resp.json()
        except ValueError:
            return {"ok": False, "status": resp.status_code, "body": resp.text}
        # Slack signals app-level rate limiting in-band too.
        if data.get("error") == "ratelimited":
            data.setdefault("status", 429)
            data.setdefault("retry_after", resp.headers.get("Retry-After"))
        return data

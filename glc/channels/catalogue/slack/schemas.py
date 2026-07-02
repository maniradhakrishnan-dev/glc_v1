"""Channel-specific Pydantic types for the slack adapter.

The canonical ``ChannelMessage`` / ``ChannelReply`` envelope lives in
``glc.channels.envelope`` and is **not** redefined here
(``scripts/validate_envelope.py`` forbids re-shaping it). These models are
narrow projections of the Slack Events API + Web API wire formats that the
adapter actually consumes/produces.

Wire-format sources:
  https://api.slack.com/events/message
  https://api.slack.com/events-api#event_type_structure
  https://api.slack.com/methods/chat.postMessage
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class SlackMessageEvent(BaseModel):
    """Inner ``event`` object of a ``message`` or ``app_mention`` event.

    Extra fields (``team``, ``blocks``, ``client_msg_id``, ...) are ignored so
    the projection stays stable as Slack adds fields.
    """

    type: str = "message"
    user: str | None = None
    text: str | None = None
    ts: str | None = None
    thread_ts: str | None = None
    channel: str | None = None
    channel_type: str | None = None

    model_config = ConfigDict(extra="ignore")


class SlackEventCallback(BaseModel):
    """Outer Events API envelope (``type == "event_callback"``)."""

    type: str | None = None
    event: SlackMessageEvent = SlackMessageEvent()
    team_id: str | None = None
    event_id: str | None = None
    event_time: int | None = None
    api_app_id: str | None = None
    authorizations: list[dict[str, Any]] = []

    model_config = ConfigDict(extra="ignore")


class SlackPostMessage(BaseModel):
    """Outbound ``chat.postMessage`` body built/validated by ``send``."""

    channel: str
    text: str
    thread_ts: str | None = None

    model_config = ConfigDict(extra="forbid")

    def to_payload(self) -> dict[str, Any]:
        """Serialise to the exact wire body, omitting an unset ``thread_ts``."""
        return self.model_dump(exclude_none=True)

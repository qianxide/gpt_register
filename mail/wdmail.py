"""Adapter for wdmail's ``/m`` inbox endpoint."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote, urlparse

from mail.parsing import base_mailbox_address, extract_code


def is_wdmail_url(url: str) -> bool:
    """Return whether *url* is the wdmail mailbox endpoint."""
    parsed = urlparse(str(url or "").strip())
    return (
        parsed.scheme.lower() in {"http", "https"}
        and str(parsed.hostname or "").lower() == "api.wdmail.top"
        and parsed.path.rstrip("/") == "/m"
    )


def wdmail_json_url(url: str) -> str:
    """Request JSON without corrupting mailbox query values.

    ``+`` is a valid character in an iCloud alias but is interpreted as a
    space by form-style query parsing.  Keep the original query spelling and
    escape only a literal plus in the ``e`` parameter.
    """
    parsed = urlparse(str(url or "").strip())
    parts: list[str] = []
    has_format = False
    for item in parsed.query.split("&") if parsed.query else []:
        if not item:
            continue
        key, separator, value = item.partition("=")
        decoded_key = unquote(key).lower()
        if decoded_key == "f":
            if not has_format:
                parts.append("f=json")
                has_format = True
            continue
        if decoded_key == "e" and separator:
            value = value.replace("+", "%2B")
        parts.append(key + (separator + value if separator else ""))
    if not has_format:
        parts.append("f=json")
    return parsed._replace(query="&".join(parts)).geturl()


class _MailText(HTMLParser):
    """Read visible message text without CSS, scripts, or attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored: list[str] = []

    def handle_starttag(self, tag, attrs):  # noqa: D401, ANN001, ARG002
        if tag in {"style", "script", "head"}:
            self.ignored.append(tag)

    def handle_endtag(self, tag):  # noqa: ANN001
        if self.ignored and tag == self.ignored[-1]:
            self.ignored.pop()

    def handle_data(self, data):  # noqa: ANN001
        if not self.ignored and data.strip():
            self.parts.append(data.strip())


def normalize_wdmail_inbox(data: Any, address: str) -> dict[str, Any]:
    """Map wdmail's status/data envelope to the common OTP message shape."""
    if not isinstance(data, dict):
        raise ValueError("wdmail returned a non-JSON inbox response")
    # `code` here is the API status (0), not the email verification code.
    if (
        data.get("code") not in (None, 0, "0")
        or str(data.get("success") or "").strip().lower() in {"false", "0", "no"}
    ):
        detail = data.get("message") or data.get("msg") or data.get("error") or data.get("code")
        raise ValueError("wdmail API error: " + str(detail))
    payload = data.get("data")
    if isinstance(payload, list):
        payload = {"messages": payload}
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        raise ValueError("wdmail response is missing data.messages")

    wanted = str(address or "").strip().lower()
    wanted_base = base_mailbox_address(wanted)
    messages: list[dict[str, Any]] = []

    def first_value(raw: dict[str, Any], *keys: str) -> str:
        body = raw.get("body") if isinstance(raw.get("body"), dict) else {}
        for key in keys:
            value = raw.get(key)
            if value not in (None, ""):
                return str(value)
            value = body.get(key)
            if value not in (None, ""):
                return str(value)
        return ""

    def received_at(raw: dict[str, Any]) -> datetime | None:
        value = first_value(raw, "received_at", "date", "timestamp", "created_at")
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError):
                return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone(timedelta(hours=8)))

    for raw in payload["messages"]:
        if not isinstance(raw, dict):
            continue
        recipient = first_value(raw, "recipient", "to", "alias_email")
        if not recipient:
            recipient = str(payload.get("recipient") or "")
        recipient = recipient.strip().lower()
        if wanted and (
            not recipient
            or (
                recipient != wanted
                and base_mailbox_address(recipient) != wanted_base
            )
        ):
            continue
        received = received_at(raw)
        if received is None:
            # Never use ingestion time for an undated message.
            continue

        parser = _MailText()
        html_body = first_value(raw, "html_body", "html_content", "html", "body_html")
        text_body = first_value(raw, "text_body", "text_content", "text", "body_text", "preview")
        parser.feed(html_body)
        parser.close()
        text = "\n".join(
            filter(None, [text_body, *parser.parts])
        )
        message = {
            "id": first_value(raw, "id", "message_id"),
            "subject": first_value(raw, "subject", "title"),
            "sender": first_value(raw, "sender", "from", "from_email"),
            "recipient": recipient,
            "received_at": received.isoformat(),
            "text_content": text,
            "html_content": html_body,
        }
        message["code"] = extract_code(message) or ""
        messages.append(message)

    messages.sort(
        key=lambda item: datetime.fromisoformat(item["received_at"]),
        reverse=True,
    )
    return {"ok": True, "messages": messages}

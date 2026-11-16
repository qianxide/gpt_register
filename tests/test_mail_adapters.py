from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

from mail import ICloudApiProvider, _direct_response_payload, _extract_code
from mail.parsing import parse_icloud_credentials
from mail.wdmail import wdmail_json_url


def _non_json_response(body: str) -> MagicMock:
    response = MagicMock(status_code=200, text=body)
    response.json.side_effect = ValueError("not json")
    return response


def test_icloud_parser_accepts_markdown_links_and_escaped_delimiters():
    rows = parse_icloud_credentials(
        r"box\@icloud.com----[https://api798.com/latest?email=box%40icloud.com&auth_code=TOKEN]"
        r"(https://api798.com/latest?email=box%40icloud.com\&auth_code=TOKEN)"
    )

    assert rows == [
        {
            "email": "box@icloud.com",
            "fetch_url": "https://api798.com/latest?email=box%40icloud.com&auth_code=TOKEN",
        }
    ]


def test_icloud_parser_accepts_mailbox_code_api_url_with_literal_at_path():
    rows = parse_icloud_credentials(
        r"opaque-14pop\@icloud.com----http://HOST:28888/api/v1/mailboxes/"
        r"opaque-14pop\@icloud.com/code?key=TOKEN"
    )

    assert rows == [
        {
            "email": "opaque-14pop@icloud.com",
            "fetch_url": (
                "http://HOST:28888/api/v1/mailboxes/"
                "opaque-14pop@icloud.com/code?key=TOKEN"
            ),
        }
    ]


def test_icloud_parser_accepts_wdmail_html_url_format():
    rows = parse_icloud_credentials(
        "pettier_remedy.9x@icloud.com----"
        "https://api.wdmail.top/m?f=html&e=pettier_remedy.9x@icloud.com"
    )

    assert rows == [
        {
            "email": "pettier_remedy.9x@icloud.com",
            "fetch_url": (
                "https://api.wdmail.top/m?f=html&e="
                "pettier_remedy.9x@icloud.com"
            ),
        }
    ]


def test_icloud_parser_accepts_haofanzi_latest_markdown_url_format():
    rows = parse_icloud_credentials(
        r"fleet_online_12\@icloud.com----"
        r"[https://icloud-api.haofanzi.net/api/mailboxes/latest?email_address="
        r"fleet_online_12@icloud.com&access_key=TOKEN]"
        r"(https://icloud-api.haofanzi.net/api/mailboxes/latest?email_address="
        r"fleet_online_12@icloud.com\&access_key=TOKEN)"
    )

    assert rows == [
        {
            "email": "fleet_online_12@icloud.com",
            "fetch_url": (
                "https://icloud-api.haofanzi.net/api/mailboxes/latest?"
                "email_address=fleet_online_12@icloud.com&access_key=TOKEN"
            ),
        }
    ]


def test_wdmail_json_url_escapes_literal_split_plus_without_rewriting_query():
    url = (
        "https://api.wdmail.top/m?f=html&e="
        "opaque-14pop+split02386766d49b@icloud.com"
    )

    assert wdmail_json_url(url) == (
        "https://api.wdmail.top/m?f=json&e="
        "opaque-14pop%2Bsplit02386766d49b@icloud.com"
    )


def test_mailbox_code_api_nested_json_is_used_for_wait_and_preview():
    url = (
        "http://HOST:28888/api/v1/mailboxes/"
        "opaque-14pop@icloud.com/code?key=TOKEN"
    )
    response = MagicMock(status_code=200, text="")
    response.json.return_value = {
        "success": True,
        "data": {
            "code": "654321",
            "subject": "OpenAI verification code",
            "received_at": "2026-09-14T16:00:00+08:00",
        },
    }
    provider = object.__new__(ICloudApiProvider)
    provider.conf = {
        "request_timeout": 10,
        "wait_timeout": 2,
        "wait_chunk": 1,
        "wait_interval": 0.2,
        "user_agent": "test-agent",
    }
    provider.session = MagicMock()
    provider.session.get.return_value = response
    mailbox = {"address": "opaque-14pop@icloud.com", "fetch_url": url}

    assert provider.wait_for_code(mailbox) == "654321"
    payload = provider._request_direct_url(mailbox, timeout=1)
    messages = provider._normalize_direct_messages(mailbox, payload)

    assert messages[0]["text_content"] == "654321"
    assert _extract_code(messages[0]) == "654321"
    assert provider.session.get.call_args.args[0] == url
    assert "params" not in provider.session.get.call_args.kwargs


def test_mailbox_code_api_success_false_is_reported_without_leaking_key():
    from mail import ICloudApiError

    url = (
        "http://HOST:28888/api/v1/mailboxes/"
        "opaque-14pop@icloud.com/code?key=TOKEN"
    )
    response = MagicMock(status_code=200, text="")
    response.json.return_value = {
        "success": False,
        "code": "mail_sync_failed",
        "message": "key=TOKEN upstream failure",
    }
    provider = object.__new__(ICloudApiProvider)
    provider.conf = {"request_timeout": 10, "user_agent": "test-agent"}
    provider.session = MagicMock()
    provider.session.get.return_value = response

    try:
        provider._request_direct_url(
            {"address": "opaque-14pop@icloud.com", "fetch_url": url},
            timeout=1,
        )
    except ICloudApiError as error:
        assert "TOKEN" not in str(error)
        assert "mail_sync_failed" in str(error)
    else:
        raise AssertionError("success=false direct response should raise")


def test_api798_latest_embedded_html_is_extracted_from_javascript():
    embedded = (
        "<html><head><title>ChatGPT verification code</title>"
        "<style>p { color: #123456; }</style></head>"
        "<body><p style=\"background-color:#F3F3F3\">654321</p></body></html>"
    )
    response = _non_json_response(
        "<html><body><h1>最新邮件</h1>"
        f"<script>var htmlContent = {json.dumps(embedded)};"
        "document.getElementById('emailFrame').srcdoc = htmlContent;</script>"
        "</body></html>"
    )

    payload = _direct_response_payload(response)
    message = payload["message"]

    assert message["subject"] == "ChatGPT verification code"
    assert _extract_code(message) == "654321"
    assert "#123456" not in message["text_content"]


def test_api798_latest_outer_receive_time_is_preserved_for_stale_filtering():
    embedded = (
        "<html><head><title>ChatGPT verification code</title></head>"
        "<body><p>654321</p></body></html>"
    )
    response = _non_json_response(
        "<html><body><div class='time'>接收时间：2026年09月13日 17:54:09 (北京时间)</div>"
        f"<script>var htmlContent = {json.dumps(embedded)};</script>"
        "</body></html>"
    )

    payload = _direct_response_payload(response)

    assert payload["received_at"] == datetime(
        2026,
        9,
        13,
        17,
        54,
        9,
        tzinfo=timezone(timedelta(hours=8)),
    )
    assert payload["message"]["received_at"] == payload["received_at"]


def test_api798_latest_skips_page_before_code_boundary():
    from mail import ICloudApiProvider

    def page(received_at: str, code: str) -> MagicMock:
        embedded = (
            "<html><head><title>ChatGPT verification code</title></head>"
            f"<body><p>{code}</p></body></html>"
        )
        return _non_json_response(
            "<html><body>"
            f"<div class='time'>接收时间：{received_at} (北京时间)</div>"
            f"<script>var htmlContent = {json.dumps(embedded)};</script>"
            "</body></html>"
        )

    provider = object.__new__(ICloudApiProvider)
    provider.conf = {
        "request_timeout": 10,
        "wait_timeout": 2,
        "wait_chunk": 1,
        "wait_interval": 0.2,
        "user_agent": "test-agent",
    }
    provider.session = MagicMock()
    provider.session.get.side_effect = [
        page("2026年09月13日 17:54:09", "111222"),
        page("2026年09月13日 17:55:09", "333444"),
    ]

    mailbox = {
        "address": "box@icloud.com",
        "fetch_url": "https://api798.com/latest?email=box%40icloud.com&auth_code=TOKEN",
        "_code_not_before": datetime(
            2026, 9, 13, 17, 54, 30, tzinfo=timezone(timedelta(hours=8))
        ),
    }

    assert provider.wait_for_code(mailbox) == "333444"
    assert provider.session.get.call_count == 2


def test_wdmail_direct_url_requests_json_and_normalizes_messages():
    url = "https://api.wdmail.top/m?f=html&e=box%40icloud.com"
    response = MagicMock(status_code=200, text="")
    response.json.return_value = {
        "code": 0,
        "data": {
            "recipient": "box@icloud.com",
            "messages": [
                {
                    "id": "message-1",
                    "recipient": "box@icloud.com",
                    "sender": "sender@example.com",
                    "subject": "ChatGPT verification code",
                    "received_at": "2026-09-13 17:00:00+08:00",
                    "html_body": "<style>.x{color:#123456}</style><p>654321</p>",
                    "text_body": "",
                }
            ],
        },
    }
    provider = object.__new__(ICloudApiProvider)
    provider.conf = {"request_timeout": 10, "user_agent": "test-agent"}
    provider.session = MagicMock()
    provider.session.get.return_value = response

    payload = provider._request_direct_url(
        {"address": "box@icloud.com", "fetch_url": url}, timeout=1
    )

    assert payload["messages"][0]["code"] == "654321"
    requested_url = provider.session.get.call_args.args[0]
    assert parse_qs(urlparse(requested_url).query) == {
        "f": ["json"],
        "e": ["box@icloud.com"],
    }


def test_wdmail_direct_url_uses_primary_message_for_split_alias():
    url = (
        "https://api.wdmail.top/m?f=html&e="
        "opaque-14pop+split02386766d49b@icloud.com"
    )
    response = MagicMock(status_code=200, text="")
    response.json.return_value = {
        "code": 0,
        "data": {
            "recipient": "opaque-14pop@icloud.com",
            "messages": [
                {
                    "id": "message-1",
                    "recipient": "opaque-14pop@icloud.com",
                    "subject": "ChatGPT verification code",
                    "received_at": "2026-09-15T12:00:00+08:00",
                    "html_body": "<p>Your code is <strong>654321</strong></p>",
                }
            ],
        },
    }
    provider = object.__new__(ICloudApiProvider)
    provider.conf = {"request_timeout": 10, "user_agent": "test-agent"}
    provider.session = MagicMock()
    provider.session.get.return_value = response

    payload = provider._request_direct_url(
        {
            "address": "opaque-14pop+split02386766d49b@icloud.com",
            "fetch_url": url,
        },
        timeout=1,
    )

    assert payload["messages"][0]["code"] == "654321"
    requested_url = provider.session.get.call_args.args[0]
    assert "%2B" in urlparse(requested_url).query
    assert parse_qs(urlparse(requested_url).query)["e"] == [
        "opaque-14pop+split02386766d49b@icloud.com"
    ]


def test_haofanzi_latest_direct_json_is_used_for_wait_and_preview():
    url = (
        "https://icloud-api.haofanzi.net/api/mailboxes/latest?"
        "email_address=fleet_online_12@icloud.com&access_key=TOKEN"
    )
    response = MagicMock(status_code=200, text="")
    response.json.return_value = {
        "status": "success",
        "data": {
            "latestEmail": {
                "id": "latest-1",
                "email_address": "fleet_online_12@icloud.com",
                "fromEmail": "noreply@example.com",
                "subject": "ChatGPT verification code",
                "bodyText": "Your OpenAI verification code is 654321",
                "receivedAt": "2026-09-18T12:00:00+08:00",
            }
        },
    }
    provider = object.__new__(ICloudApiProvider)
    provider.conf = {
        "request_timeout": 10,
        "wait_timeout": 2,
        "wait_chunk": 1,
        "wait_interval": 0.2,
        "user_agent": "test-agent",
    }
    provider.session = MagicMock()
    provider.session.get.return_value = response
    mailbox = {"address": "fleet_online_12@icloud.com", "fetch_url": url}

    assert provider.wait_for_code(mailbox) == "654321"
    messages = provider.fetch_recent_messages(mailbox)
    assert messages[0]["message_id"] == "latest-1"
    assert messages[0]["sender"] == "noreply@example.com"
    assert provider.session.get.call_args.args[0] == url


def test_wdmail_direct_url_falls_back_to_html_when_json_representation_is_unavailable():
    url = "https://api.wdmail.top/m?f=html&e=box%40icloud.com"
    json_response = _non_json_response(
        "<html><body><p>HTML response</p></body></html>"
    )
    html_response = _non_json_response(
        "<html><head><title>ChatGPT verification code</title></head>"
        "<body><p>Your code is 654321</p></body></html>"
    )
    provider = object.__new__(ICloudApiProvider)
    provider.conf = {"request_timeout": 10, "user_agent": "test-agent"}
    provider.session = MagicMock()
    provider.session.get.side_effect = [json_response, html_response]

    payload = provider._request_direct_url(
        {"address": "box@icloud.com", "fetch_url": url}, timeout=1
    )
    messages = provider._normalize_direct_messages(
        {"address": "box@icloud.com"}, payload
    )

    assert messages
    assert _extract_code(messages[0]) == "654321"
    assert [call.args[0] for call in provider.session.get.call_args_list] == [
        "https://api.wdmail.top/m?f=json&e=box%40icloud.com",
        url,
    ]

# -*- coding: utf-8 -*-
"""Pure mailbox parsing and OTP extraction helpers."""

from __future__ import annotations

import re
from html import unescape
from typing import Any
from urllib.parse import urlparse

ICLOUD_PROVIDER_TYPES = {"icloud_api", "icloud", "hide_my_email"}
OUTLOOK_PROVIDER_TYPES = {"outlook_token"}
MADDY_PROVIDER_TYPES = {
    "maddy",
    "maddy_webui",
    "maddy_webui_api",
    "maddy_api",
    "maddy_mail",
}
FASTMAIL_PROVIDER_TYPES = {
    "fastmail",
    "fastmail_api",
    "fastmail_mail",
    "mailcode",
    "mailcode_api",
}
MAILCOM_PROVIDER_TYPES = {"mailcom_api", "mailcom", "mail.com", "mailcom_alias"}
CANONICAL_PROVIDER_TYPES = {
    "outlook_token",
    "icloud_api",
    "maddy_webui",
    "fastmail_api",
    "mailcom_api",
}
SUPPORTED_PROVIDER_TYPES = (
    OUTLOOK_PROVIDER_TYPES
    | ICLOUD_PROVIDER_TYPES
    | MADDY_PROVIDER_TYPES
    | FASTMAIL_PROVIDER_TYPES
    | MAILCOM_PROVIDER_TYPES
)


def parse_outlook_credentials(text: str) -> list[dict[str, str]]:
    credentials: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_line in str(text or "").splitlines():
        line = str(raw_line or "").strip()
        if not line or line.startswith("#") or "----" not in line:
            continue
        parts = [str(p).strip() for p in line.split("----", 3)]
        if len(parts) != 4:
            continue
        email, password, client_id, refresh_token = parts
        if "@" not in email or not client_id or not refresh_token:
            continue
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        credentials.append(
            {
                "email": email,
                "password": password,
                "client_id": client_id,
                "refresh_token": refresh_token,
            }
        )
    return credentials


def parse_icloud_credentials(text: str) -> list[dict[str, str]]:
    """Parse ``email----api_key`` or ``email----fetch_url`` lines.

    A full HTTP(S) URL is kept separately so providers can request it as-is;
    this is used by mailbox services whose endpoint already contains the
    mailbox address and authentication query parameters.  Markdown links and
    escaped ``@``/``&`` characters are accepted because mailbox lists are
    frequently copied from chat or an HTML page.
    """
    credentials: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_line in str(text or "").splitlines():
        line = str(raw_line or "").strip()
        if not line or line.startswith("#") or "----" not in line:
            continue
        email, value = (str(part).strip() for part in line.split("----", 1))
        # Chat/Markdown copies sometimes escape punctuation in mailbox names.
        email = email.replace("\\@", "@").replace("\\_", "_").strip()
        if not email or not value:
            continue
        if "@" not in email:
            continue
        # skip accidental outlook 4-field lines in icloud box
        parts = [str(p).strip() for p in line.split("----")]
        if len(parts) == 4 and len(parts[2]) > 20 and len(parts[3]) > 20:
            continue
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        decoded = unescape(value).strip()
        link = re.fullmatch(
            r"\[[^\]\r\n]*\]\((https?://[^\s]+)\)",
            decoded,
            re.I,
        )
        if link:
            value = link.group(1)
        elif decoded.lower().startswith(("https://", "http://")):
            value = decoded
        # Some copied Markdown renders escape ampersands in the link target.
        if value.lower().startswith(("https://", "http://")):
            # Chat/Markdown escaping can affect either query separators or a
            # literal @ in a mailbox path (for example /mailboxes/name@host).
            value = (
                value.replace("\\&", "&")
                .replace("\\@", "@")
                .replace("\\_", "_")
                .strip()
            )
        parsed = urlparse(value)
        if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
            credentials.append({"email": email, "fetch_url": value})
        else:
            credentials.append({"email": email, "api_key": value})
    return credentials


def parse_domain_mailboxes(text: str) -> list[dict[str, str]]:
    """Parse optional cached domain-mail rows: email[----remote_id]."""
    credentials: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_line in str(text or "").splitlines():
        line = str(raw_line or "").strip()
        if not line or line.startswith("#"):
            continue
        parts = [str(p).strip() for p in line.split("----")]
        email = parts[0]
        if "@" not in email:
            continue
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        item = {"email": email}
        if len(parts) > 1 and parts[1]:
            item["id"] = parts[1]
        credentials.append(item)
    return credentials


def parse_fastmail_accounts(text: str) -> list[str]:
    """Parse mailcode-api master accounts, one email per line.

    Trailing fields (``email----password``) are tolerated and ignored;
    passwords live inside the mailcode-api service, not in this project.
    """
    accounts: list[str] = []
    seen: set[str] = set()
    for raw_line in str(text or "").splitlines():
        line = str(raw_line or "").strip()
        if not line or line.startswith("#"):
            continue
        email = line.split("----", 1)[0].strip().lower()
        if "@" not in email:
            continue
        if email in seen:
            continue
        seen.add(email)
        accounts.append(email)
    return accounts


def normalize_provider_type(raw: str) -> str:
    ptype = str(raw or "").strip().lower()
    if ptype in ICLOUD_PROVIDER_TYPES:
        return "icloud_api"
    if ptype in OUTLOOK_PROVIDER_TYPES:
        return "outlook_token"
    if ptype in MADDY_PROVIDER_TYPES:
        return "maddy_webui"
    if ptype in FASTMAIL_PROVIDER_TYPES:
        return "fastmail_api"
    if ptype in MAILCOM_PROVIDER_TYPES:
        return "mailcom_api"
    return ptype


def base_mailbox_address(email: str) -> str:
    email = str(email or "").strip().lower()
    if "@" not in email:
        return email
    local, domain = email.rsplit("@", 1)
    if "+" in local:
        local = local.split("+", 1)[0]
    return f"{local}@{domain}"


def extract_code(message: dict[str, Any]) -> str | None:
    content = (
        f"{message.get('subject', '')}\n"
        f"{message.get('text_content', '')}\n"
        f"{message.get('html_content', '')}"
    ).strip()
    if not content:
        return None
    # 所有分支统一加 (?<!\d)...(?!\d) 数字边界：订单号/时间戳等 7 位以上
    # 数字串的前 6 位不能被截断成"验证码"；"177010" 是模板噪音码需跳过
    match = re.search(
        r"background-color:\s*#F3F3F3[^>]*>[^<]*?((?<!\d)\d{6}(?!\d))",
        content,
        re.I,
    )
    if match and match.group(1) != "177010":
        return match.group(1)
    match = re.search(
        r"(?:Verification code|code is|代码为|验证码)[:\s]*((?<!\d)\d{6}(?!\d))",
        content,
        re.I,
    )
    if match and match.group(1) != "177010":
        return match.group(1)
    for code in re.findall(r">\s*((?<!\d)\d{6}(?!\d))\s*<|(?<![#&])\b((?<!\d)\d{6}(?!\d))\b", content):
        value = code[0] or code[1]
        if value and value != "177010":
            return value
    return None

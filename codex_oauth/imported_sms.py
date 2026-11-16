"""Receive-only adapter for leased, imported US SMS numbers.

The receive URL stays private to this client.  Acquisition only reserves a
number; the first inbox read is deliberately deferred until ``wait_code`` so
the caller can submit the phone challenge first.  That first post-submission
read establishes the baseline and later reads may return only new codes.
"""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from common.sms_number_pool import SmsPoolError

from .sms import SmsActivation, SmsError, SmsSettings


MAX_RESPONSE_BYTES = 1024 * 1024
_OTP = re.compile(r"(?<![0-9A-Za-z_])([0-9]{6})(?![0-9A-Za-z_])")
_CODE_KEYS = {"code", "otp", "smscode", "verificationcode", "verifycode", "passcode", "pin", "验证码"}
_TEXT_KEYS = {"sms", "text", "message", "msg", "content", "body", "smscontent", "messagecontent"}
_CONTAINER_KEYS = {"data", "result", "results", "messages", "list", "items", "records", "codes"}
_NON_CODE_FIELD = re.compile(
    r"(?:\b(?:phone|mobile|tel|msisdn|date|time|timestamp|id|status|status_code)\b|手机号|电话号码|日期|时间)"
    r"[\"']?\s*[:=：]?\s*\+?[0-9][0-9 \t().:/+_-]*",
    re.IGNORECASE,
)
_NUMERIC_GROUP = re.compile(
    r"(?<![0-9A-Za-z_])\+?[0-9]+(?:[ \t().:/_-]+[0-9]+)+(?![0-9A-Za-z_])"
)


class _SmsPool(Protocol):
    def reserve(self, *, lease_seconds: int) -> dict[str, Any]: ...
    def remember_code(self, lease_id: str, code: str) -> bool: ...
    def complete(self, lease_id: str) -> bool: ...
    def cancel(self, lease_id: str) -> bool: ...
    def summary(self) -> dict[str, int]: ...


class _VisibleText(HTMLParser):
    """Ignore attributes and script/style content when inspecting an HTML inbox."""

    _HIDDEN = {"script", "style", "head", "template", "noscript"}
    _BLOCKS = {"br", "p", "div", "li", "tr", "td", "th", "hr", "section", "article", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._HIDDEN:
            self.hidden.append(tag)
        if not self.hidden and tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _text_codes(text: str) -> list[str]:
    parser = _VisibleText()
    parser.feed(text)
    parser.close()
    text = "".join(parser.parts)
    # URLs, labelled metadata and grouped phone/date/time numbers are not OTPs.
    text = re.sub(r"(?:https?://|www\.)[^\s<>]+", " ", text, flags=re.IGNORECASE)
    text = _NON_CODE_FIELD.sub(" ", text)

    def mask_group(match):
        value = match.group()
        # An inbox can also contain several bare six-digit codes on one line.
        if re.fullmatch(r"[0-9]{6}(?:[ \t]+[0-9]{6})+", value):
            return value
        return " "

    text = _NUMERIC_GROUP.sub(mask_group, text)
    return _OTP.findall(text)


def _extract_codes(body: str, content_type: str = "") -> list[str]:
    """Read message/code fields, without treating JSON metadata as messages."""
    body = body.lstrip("\ufeff").strip()
    if not body:
        return []
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        if "json" in content_type.lower() or body.startswith(("{", '["', "[{", '"')):
            raise SmsError("导入号码池取码响应格式异常") from None
        return list(dict.fromkeys(_text_codes(body)))

    codes: list[str] = []

    def visit(value, *, scalar=True):
        if isinstance(value, dict):
            for key, item in value.items():
                key = re.sub(r"[_\s-]", "", str(key).lower())
                if key in _CODE_KEYS:
                    raw = str(item).strip()
                    if re.fullmatch(r"[0-9]{6}", raw):
                        codes.append(raw)
                elif key in _TEXT_KEYS or key in _CONTAINER_KEYS:
                    visit(item)
                elif isinstance(item, (dict, list)):
                    visit(item, scalar=False)
        elif isinstance(value, list):
            for item in value:
                visit(item, scalar=scalar)
        elif scalar and isinstance(value, str):
            codes.extend(_text_codes(value))
        elif scalar and isinstance(value, int) and re.fullmatch(r"[0-9]{6}", str(value)):
            codes.append(str(value))

    visit(payload)
    return list(dict.fromkeys(codes))


def _is_empty_inbox_404(body: bytes, headers: Any) -> bool:
    """The provider uses HTTP 404 for a valid, currently empty inbox."""
    if len(body) > MAX_RESPONSE_BYTES:
        return False
    try:
        charset = headers.get_content_charset() or "utf-8"
        payload = json.loads(body.decode(charset, "replace"))
    except (AttributeError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    messages = payload.get("messages")
    return (
        payload.get("count") == 0
        and isinstance(messages, list)
        and not messages
        and str(payload.get("msg", "")).strip().lower() == "no code."
        and str(payload.get("status", "")) == "000"
    )


def _validate_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname == "168smsapi.com"
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and parsed.path.startswith("/sms/")
            and len(parsed.path) > 5
            and not parsed.fragment
            and len(url) <= 2048
            and not re.search(r"[\s\\\x00-\x1f\x7f]", url)
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise SmsError("导入号码池取码链接校验失败") from None


class _SmsRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            _validate_url(newurl)
        except SmsError:
            fp.close()
            raise SmsError("导入号码池取码重定向被阻止") from None
        return super().redirect_request(req, fp, code, msg, headers, newurl)

    def http_error_302(self, req, fp, code, msg, headers):
        # urllib's default handler drains fp.read() without a limit.  This
        # receive-only client can close a redirect response without reading it.
        try:
            location = headers.get("Location") or headers.get("URI")
            if not location:
                raise SmsError("导入号码池取码重定向缺少目标")
            target = urljoin(req.full_url, location)
            following = self.redirect_request(req, fp, code, msg, headers, target)
            redirects = getattr(req, "_sms_redirects", 0)
            if redirects >= self.max_redirections:
                raise SmsError("导入号码池取码重定向次数超限")
            deadline = getattr(req, "_sms_deadline", time.monotonic() + req.timeout)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SmsError("导入号码池取码请求超时")
            following._sms_redirects = redirects + 1
            following._sms_deadline = deadline
        finally:
            fp.close()
        return self.parent.open(following, timeout=remaining)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def _digest(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


@dataclass
class _Lease:
    url: str = field(repr=False)
    seen: set[str] = field(repr=False)
    baseline_pending: bool = True


class ImportedPoolSmsClient:
    def __init__(self, settings: SmsSettings, *, timeout: float = 30.0, pool: _SmsPool | None = None):
        self.settings = settings
        self.timeout = max(3.0, float(timeout))
        if pool is None:
            try:
                from common.sms_number_pool import get_sms_pool

                pool = get_sms_pool()
            except Exception:
                raise SmsError("导入号码池初始化失败") from None
        self._pool = pool
        self._leases: dict[str, _Lease] = {}
        # An explicit empty ProxyHandler also disables Windows/environment proxies.
        self._opener = build_opener(ProxyHandler({}), _SmsRedirectHandler())

    def _read_codes(self, url: str, *, timeout: float | None = None) -> list[str]:
        _validate_url(url)
        request = Request(url, headers={
            "User-Agent": "codex-oauth-local/1.0",
            "Accept": "application/json, text/plain, text/html",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache, no-store",
        })
        request_timeout = self.timeout if timeout is None else timeout
        request._sms_deadline = time.monotonic() + request_timeout
        try:
            with self._opener.open(request, timeout=request_timeout) as response:
                _validate_url(response.geturl())
                if not 200 <= response.getcode() < 300:
                    raise SmsError("导入号码池取码服务响应异常")
                length = response.headers.get("Content-Length")
                if length is not None and (int(length) < 0 or int(length) > MAX_RESPONSE_BYTES):
                    raise SmsError("导入号码池取码响应超过大小限制")
                if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
                    raise SmsError("导入号码池取码响应格式异常")
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise SmsError("导入号码池取码响应超过大小限制")
                charset = response.headers.get_content_charset() or "utf-8"
                return _extract_codes(body.decode(charset, "replace"), response.headers.get("Content-Type", ""))
        except HTTPError as exc:
            status = int(exc.code)
            if status == 404:
                try:
                    body = exc.read(MAX_RESPONSE_BYTES + 1)
                    if _is_empty_inbox_404(body, exc.headers):
                        return []
                finally:
                    exc.close()
                raise SmsError("导入号码池取码请求失败（HTTP 404）") from None
            exc.close()
            raise SmsError(f"导入号码池取码请求失败（HTTP {status}）") from None
        except SmsError:
            raise
        except Exception:
            # urllib/decoding errors can contain the entire private URL/body.
            raise SmsError("导入号码池取码请求失败或超时") from None

    def acquire(self) -> SmsActivation:
        try:
            reservation = self._pool.reserve(
                lease_seconds=max(60, min(3600, math.ceil(self.settings.timeout_seconds + 2 * self.timeout + 60))),
            )
        except SmsPoolError as exc:
            # Store-authored diagnostics contain availability/cooldown counts only.
            raise SmsError(str(exc)) from None
        except Exception:
            raise SmsError("导入号码池分配失败，请检查可用号码和冷却状态") from None

        lease_id = ""
        try:
            # Reserve first: validation/snapshot failures still cool and release it.
            lease_id = reservation["lease_id"]
            phone = reservation["phone"]
            if (not isinstance(lease_id, str) or not lease_id
                    or reservation["country"] != "US"
                    or not isinstance(phone, str) or not re.fullmatch(r"\+?1[2-9][0-9]{9}", phone)):
                raise ValueError("invalid reservation")
            url = reservation["url"]
            if not isinstance(url, str):
                raise ValueError("invalid receive URL")
            # Validation is local only; this must not perform a provider
            # request before the caller submits the phone challenge.
            _validate_url(url)
            used_codes = reservation["used_codes"]
            if not isinstance(used_codes, list) or any(
                not isinstance(code, str) or not re.fullmatch(r"[0-9a-f]{64}", code) for code in used_codes
            ):
                raise ValueError("invalid code history")
            activation = SmsActivation(lease_id, phone, "US", "", provider="imported_pool", label="美国 +1")
            # Do not touch the receive URL here.  The caller must submit the
            # phone first; the first wait_code read is the post-submit baseline.
            self._leases[lease_id] = _Lease(url, set(used_codes))
        except SmsError:
            if lease_id:
                self.cancel(SmsActivation(lease_id, "", "US", ""))
            raise
        except Exception:
            if lease_id:
                self.cancel(SmsActivation(lease_id, "", "US", ""))
            raise SmsError("导入号码池号码租约初始化失败，请检查取码链接") from None
        return activation

    def wait_code(self, activation: SmsActivation, cancel_event: Any = None) -> str:
        deadline = time.monotonic() + self.settings.timeout_seconds
        interval = max(0.001, self.settings.poll_interval_ms / 1000.0)
        try:
            lease = self._leases.get(activation.activation_id)
            if activation.finished or lease is None:
                raise SmsError("导入号码池号码占用已结束")
            while time.monotonic() < deadline:
                if cancel_event is not None and cancel_event.is_set():
                    raise SmsError("任务已取消")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                codes = self._read_codes(lease.url, timeout=min(self.timeout, remaining))
                if cancel_event is not None and cancel_event.is_set():
                    raise SmsError("任务已取消")
                if time.monotonic() >= deadline:
                    break
                if lease.baseline_pending:
                    lease.seen.update(_digest(code) for code in codes)
                    lease.baseline_pending = False
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    if cancel_event is not None:
                        if cancel_event.wait(min(interval, remaining)):
                            raise SmsError("任务已取消")
                    else:
                        time.sleep(min(interval, remaining))
                    continue
                for code in codes:
                    digest = _digest(code)
                    if digest in lease.seen:
                        continue
                    # False also handles a code recorded concurrently by another reader.
                    accepted = self._pool.remember_code(activation.activation_id, code)
                    lease.seen.add(digest)
                    if cancel_event is not None and cancel_event.is_set():
                        raise SmsError("任务已取消")
                    if time.monotonic() >= deadline:
                        break
                    if accepted:
                        return code
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if cancel_event is not None:
                    if cancel_event.wait(min(interval, remaining)):
                        raise SmsError("任务已取消")
                else:
                    time.sleep(min(interval, remaining))
            raise SmsError("等待导入号码池短信验证码超时")
        except SmsError:
            self.cancel(activation)
            raise
        except Exception:
            self.cancel(activation)
            raise SmsError("导入号码池短信读取或记录失败") from None

    def finish(self, activation: SmsActivation) -> None:
        if activation.finished:
            return
        try:
            self._pool.complete(activation.activation_id)
        except Exception:
            raise SmsError("导入号码池成功状态保存失败") from None
        activation.finished = True
        self._leases.pop(activation.activation_id, None)

    def cancel(self, activation: SmsActivation) -> None:
        if activation.finished:
            return
        try:
            self._pool.cancel(activation.activation_id)
        except Exception:
            raise SmsError("导入号码池号码释放失败") from None
        activation.finished = True
        self._leases.pop(activation.activation_id, None)

    def balance(self) -> dict[str, str]:
        """Expose local inventory counts through the existing balance contract."""
        try:
            summary = self._pool.summary()
            result = {key: str(int(summary[key])) for key in
                      ("total", "available", "cooling", "reserved", "exhausted")}
            return {"balance": result["available"], **result}
        except Exception:
            raise SmsError("导入号码池数量查询失败") from None

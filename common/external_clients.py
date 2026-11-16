# -*- coding: utf-8 -*-
"""Bounded, synchronous clients for Scan and UPI; all retries belong to callers.

UPI responses are INTERNAL data: persist job_secret, then explicitly project any
public response. Never pass the raw status response (which may contain tokens or
logs) to a user. Sessions passed in are dedicated to these clients, not shared
with another HTTP integration or concurrently between worker threads.
"""
from __future__ import annotations

import json
import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


SCAN_BASE = "https://scan.fishmail.vip"
# The target paylink project uses the same v1 client-orders contract.  Keep a
# separate name so callers can distinguish the target publisher integration
# from the legacy scan client without changing the fixed, HTTPS endpoint.
CDK_SCAN_API_BASE = SCAN_BASE
UPI_BASE = "https://upi.43-255-159-106.sslip.io"
PUBLIC_UPI_BASE = "https://dasaobi.online/upi"
FOARGE_API_BASE = "https://foarge.com/api/publisher/v1"
MASI_API_BASE = "https://masi.cc.cd"
SEASHORE_API_BASE = "https://seashore.lol/api/publisher"
SEASHORE_PUBLISHER_BASE = SEASHORE_API_BASE
REQUEST_TIMEOUT = (5.0, 30.0)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 2 * 1024 * 1024
_JOB_ID = re.compile(r"[a-zA-Z0-9_-]{1,128}\Z")
_ERROR_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_MONEY = re.compile(r"[0-9]{1,32}(?:\.[0-9]{1,4})?\Z")

_MESSAGES = {
    "INVALID_API_KEY": "接口密钥无效或已撤销",
    "UNAUTHORIZED": "请检查服务凭证",
    "INVALID_TOKEN": "登录凭证已失效",
    "FORBIDDEN": "当前凭证无权执行此操作",
    "USER_DISABLED": "账号已被停用",
    "INVALID_CREDENTIALS": "服务凭证无效",
    "CREDENTIALS_REQUIRED": "请先配置所需服务凭证",
    "INVALID_REQUEST": "请求参数格式错误",
    "INVALID_LINK_COUNT": "提交链接数量不符合要求",
    "INVALID_PAYMENT_LINK": "支付链接格式无效",
    "INVALID_IDEMPOTENCY_KEY": "幂等键须为八至八十位有效字符",
    "DUPLICATE_LINK": "支付链接已提交或本批次存在重复链接",
    "INSUFFICIENT_BALANCE": "账户余额不足",
    "NO_ACCEPTING_SUPPLIERS": "当前暂无工作人员接单",
    "BALANCE_INCONSISTENT": "账户冻结余额异常，请联系管理员",
    "INTERNAL_ERROR": "外部服务内部异常",
    "UPSTREAM_ERROR": "外部服务暂时异常",
    "RATE_LIMITED": "请求过于频繁，请稍后再试",
    "NOT_FOUND": "所请求的资源不存在",
    "ORDER_NOT_FOUND": "订单不存在",
    "INVALID_ORDER_STATUS": "当前订单状态不支持此操作",
    "INVALID_JOB_ID": "任务编号格式错误",
    "JOB_NOT_FOUND": "任务不存在或任务凭证无效",
    "INVALID_JOB_SECRET": "任务凭证格式错误",
    "INVALID_CDK": "卡密无效或已失效",
    "CDK_EXPIRED": "卡密已过期",
    "CDK_EXHAUSTED": "卡密剩余次数不足",
    "INVALID_RESPONSE": "外部服务响应格式异常",
    "RESPONSE_TOO_LARGE": "外部服务响应超过大小限制",
    "REQUEST_TOO_LARGE": "请求内容超过大小限制",
    "REDIRECT_BLOCKED": "外部服务返回了非预期跳转",
    "NETWORK_ERROR": "外部服务网络连接异常",
    "TIMEOUT": "外部服务请求超时",
    "BUSINESS_ERROR": "外部服务未接受此请求",
    "HTTP_ERROR": "外部服务请求失败",
}


class ExternalClientError(RuntimeError):
    """Only safe metadata; no response, request, headers or upstream messages.

    uncertain concerns a mutation's outcome, not whether an error is retryable.
    A caller must reconcile an uncertain submission before deciding to retry.
    """

    def __init__(
        self, message: str, *, status: int | None = None,
        code: str = "HTTP_ERROR", uncertain: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.uncertain = uncertain
        self.retry_after = retry_after


def _error(code: str, status: int | None = None, *, uncertain: bool = False,
           retry_after: int | None = None) -> ExternalClientError:
    return ExternalClientError(
        _MESSAGES.get(code, _MESSAGES["BUSINESS_ERROR"]), status=status,
        code=code, uncertain=uncertain, retry_after=retry_after,
    )


def _retry_after(headers: Any) -> int | None:
    # Accept bounded delta-seconds only; never expose arbitrary header text.
    value = headers.get("Retry-After")
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,5}", value.strip()):
        seconds = int(value.strip())
        if seconds <= 86400:
            return seconds
    return None


def _strings(value: Any):
    if isinstance(value, str):
        if value:
            yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _safe_code(body: Any, fallback: str, sensitive: tuple[str, ...]) -> str:
    value = body.get("code") if isinstance(body, dict) else None
    if not isinstance(value, str) or not _ERROR_CODE.fullmatch(value):
        return fallback
    if value in ("OK", "UNKNOWN"):
        return fallback
    if any(secret in value for secret in sensitive):
        return fallback
    return value


def _http_code(status: int) -> str:
    if status >= 500:
        return "UPSTREAM_ERROR"
    return {
        400: "INVALID_REQUEST", 401: "UNAUTHORIZED", 403: "FORBIDDEN",
        404: "NOT_FOUND", 413: "REQUEST_TOO_LARGE", 422: "INVALID_REQUEST",
        429: "RATE_LIMITED",
    }.get(status, "HTTP_ERROR")


def _header(value: Any, code: str = "CREDENTIALS_REQUIRED") -> str:
    if (not isinstance(value, str) or not value or len(value) > 8192
            or value != value.strip()
            or any(ord(char) < 32 or ord(char) > 255 or ord(char) == 127 for char in value)):
        raise _error(code)
    return value


def _job_id(value: Any) -> str:
    if not isinstance(value, str) or not _JOB_ID.fullmatch(value):
        raise _error("INVALID_JOB_ID")
    return value


def _reject_constant(_value: str) -> None:
    raise ValueError("Non-finite JSON number")


class _Client:
    def __init__(self, session=None) -> None:
        self._session = requests.Session() if session is None else session
        self._session.trust_env = False
        if isinstance(self._session, requests.Session):
            # Injected sessions are dedicated: remove ambient auth, proxy,
            # query parameters and hooks that could replay or alter requests.
            self._session.proxies.clear()
            self._session.headers.clear()
            self._session.params.clear()
            self._session.cookies.clear()
            self._session.auth = None
            self._session.hooks = {"response": []}
            for adapter in self._session.adapters.values():
                if isinstance(adapter, HTTPAdapter):
                    adapter.max_retries = Retry(
                        total=0, connect=0, read=0, redirect=0, status=0, other=0,
                    )

    def close(self) -> None:
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _request(self, method: str, url: str, *, headers=None, params=None,
                 body=None, mutation: bool = False, scan_shape: str = "",
                 created_job: bool = False, upi_shape: str = "",
                 raw_shape: str = "") -> dict[str, Any]:
        # Public methods construct every URL locally; no returned status_url or
        # caller-provided base URL ever participates in routing.
        allowed = (
            url.startswith(SCAN_BASE + "/api/v1/"),
            url.startswith(FOARGE_API_BASE + "/"),
            url.startswith(MASI_API_BASE + "/"),
            url.startswith(UPI_BASE + "/api/v1/"),
            url.startswith(PUBLIC_UPI_BASE + "/api/"),
            url.startswith(SEASHORE_PUBLISHER_BASE + "/"),
        )
        if not any(allowed):
            raise _error("INVALID_REQUEST")
        request_headers = {"Accept": "application/json", **(headers or {})}
        if body is not None:
            request_headers["Content-Type"] = "application/json"
            try:
                encoded = json.dumps(body, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError, RecursionError):
                raise _error("INVALID_REQUEST") from None
            if len(encoded) > MAX_REQUEST_BYTES:
                raise _error("REQUEST_TOO_LARGE")
        sensitive = tuple(_strings((headers or {}, body)))
        # Include the bare Bearer token when checking untrusted error codes.
        sensitive += tuple(v[7:] for v in sensitive if v.startswith("Bearer "))
        self._session.trust_env = False
        response = None
        status = None
        retry_after = None
        uncertain = mutation
        try:
            response = self._session.request(
                method, url, headers=request_headers, params=params, json=body,
                timeout=REQUEST_TIMEOUT, stream=True, allow_redirects=False,
                proxies={}, verify=True,
            )
            status = response.status_code
            if type(status) is not int or not 100 <= status <= 599:
                status = None
                raise _error("INVALID_RESPONSE", uncertain=mutation)
            uncertain = mutation and not 400 <= status < 500
            retry_after = _retry_after(response.headers)
            if 300 <= status < 400:
                raise _error("REDIRECT_BLOCKED", status, uncertain=uncertain,
                             retry_after=retry_after)
            declared = response.headers.get("Content-Length", "")
            if isinstance(declared, str) and re.fullmatch(r"[0-9]+", declared):
                digits = declared.lstrip("0") or "0"
                if len(digits) > 7 or int(digits) > MAX_RESPONSE_BYTES:
                    raise _error("RESPONSE_TOO_LARGE", status, uncertain=uncertain,
                                 retry_after=retry_after)
            content = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise _error("RESPONSE_TOO_LARGE", status, uncertain=uncertain,
                                 retry_after=retry_after)
                content.extend(chunk)
            try:
                payload = json.loads(content, parse_constant=_reject_constant)
            except (ValueError, UnicodeError, RecursionError):
                code = "INVALID_RESPONSE" if status < 400 else _http_code(status)
                raise _error(code, status, uncertain=uncertain,
                             retry_after=retry_after) from None
            if not 200 <= status < 300:
                code = _safe_code(payload, _http_code(status), sensitive)
                raise _error(code, status, uncertain=uncertain, retry_after=retry_after)
            if not isinstance(payload, dict):
                raise _error("INVALID_RESPONSE", status, uncertain=mutation,
                             retry_after=retry_after)
            if raw_shape:
                if not _raw_shape_valid(raw_shape, payload):
                    raise _error("INVALID_RESPONSE", status, uncertain=mutation,
                                 retry_after=retry_after)
                return payload
            if scan_shape:
                if payload.get("code") != "OK":
                    if isinstance(payload.get("code"), str) and payload["code"]:
                        code = _safe_code(payload, "BUSINESS_ERROR", sensitive)
                        raise _error(code, status, retry_after=retry_after)
                    raise _error("INVALID_RESPONSE", status, uncertain=mutation,
                                 retry_after=retry_after)
                data = payload.get("data")
                if not _scan_data_valid(scan_shape, data):
                    raise _error("INVALID_RESPONSE", status, uncertain=mutation,
                                 retry_after=retry_after)
                return data
            if upi_shape == "batch" and ("jobs" in payload or "rejected" in payload):
                # Keep every row and credential, even with a malformed sibling.
                # These INTERNAL results must be persisted/reconciled by the
                # service before any retry; never put them on a public error.
                if (not _batch_result_valid(payload)
                        or (payload.get("ok") is False and payload.get("jobs"))):
                    payload["uncertain"] = True
                return payload
            if payload.get("ok") is False:
                code = _safe_code(payload, "BUSINESS_ERROR", sensitive)
                raise _error(code, status, retry_after=retry_after)
            if created_job and not _created_job_valid(payload):
                raise _error("INVALID_RESPONSE", status, uncertain=mutation,
                             retry_after=retry_after)
            if upi_shape == "batch" or (upi_shape == "fuse" and not _fused_cdk_valid(payload)):
                raise _error("INVALID_RESPONSE", status, uncertain=mutation,
                             retry_after=retry_after)
            return payload
        except requests.RequestException as exc:
            # A transport can attach a response before raising (including a
            # falsey 4xx/5xx requests.Response). Keep only its safe metadata.
            if response is None and exc.response is not None:
                response = exc.response
                candidate = getattr(response, "status_code", None)
                if type(candidate) is int and 100 <= candidate <= 599:
                    status = candidate
                    retry_after = _retry_after(response.headers)
                    uncertain = mutation and not 400 <= status < 500
            invalid = isinstance(exc, (requests.exceptions.InvalidHeader,
                                      requests.exceptions.InvalidURL,
                                      requests.exceptions.InvalidJSONError,
                                      requests.exceptions.InvalidSchema,
                                      requests.exceptions.MissingSchema))
            if invalid:
                code = "INVALID_REQUEST"
            elif isinstance(exc, requests.Timeout):
                code = "TIMEOUT"
            elif isinstance(exc, requests.exceptions.TooManyRedirects):
                code = "REDIRECT_BLOCKED"
            elif isinstance(exc, requests.HTTPError) and status is not None:
                code = _http_code(status)
            else:
                code = "NETWORK_ERROR"
            raise _error(code, status,
                         uncertain=False if invalid else uncertain,
                         retry_after=retry_after) from None
        finally:
            if response is not None:
                # Closing a broken stream must not mask the safe primary error.
                try:
                    response.close()
                except Exception:
                    pass


def _integer(value: Any, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _safe_identifier(value: Any, *, prefix: str = "") -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= 160
            and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", value))
            and (not prefix or value.startswith(prefix)))


def _raw_shape_valid(shape: str, payload: dict[str, Any]) -> bool:
    """Validate the small envelopes used by the non-legacy publisher APIs.

    The upstream services do not share the legacy ``{code,data}`` envelope.
    Keep validation here structural and let the service layer project only
    the fields needed by the UI.
    """
    if shape == "public_upi_create":
        return (_safe_identifier(payload.get("task_id"), prefix="cl_")
                and isinstance(payload.get("status"), str)
                and payload.get("status") in {"queued", "running", "cancel_requested", "succeeded", "failed", "cancelled"})
    if shape == "public_upi_batch":
        tasks = payload.get("tasks")
        return (isinstance(tasks, list) and 1 <= len(tasks) <= 100
                and all(isinstance(item, dict)
                        and _safe_identifier(item.get("task_id"), prefix="cl_")
                        and isinstance(item.get("status"), str)
                        for item in tasks))
    if shape == "public_upi_status":
        return (_safe_identifier(payload.get("task_id"), prefix="cl_")
                and isinstance(payload.get("status"), str))
    if shape == "public_upi_cancel":
        return (payload.get("ok") is True
                or (_safe_identifier(payload.get("task_id"), prefix="cl_")
                    and isinstance(payload.get("status"), str)))
    if shape == "seashore_me":
        return (isinstance(payload.get("remaining_uses"), (int, float))
                and not isinstance(payload.get("remaining_uses"), bool))
    if shape == "seashore_task":
        task = payload.get("task")
        return (isinstance(task, dict)
                and _safe_identifier(task.get("public_id"), prefix="ORD-")
                and isinstance(task.get("status"), str))
    if shape == "seashore_tasks":
        tasks = payload.get("tasks")
        return isinstance(tasks, list) and all(
            isinstance(item, dict) and _safe_identifier(item.get("public_id"), prefix="ORD-")
            for item in tasks
        )
    if shape == "seashore_cancel":
        return payload.get("ok") is True
    if shape in {"foarge_me", "foarge_task", "masi_status", "masi_order"}:
        # These publishers have changed non-sensitive response fields across
        # versions.  Confirm only that the JSON envelope is an object; the
        # service layer projects the small set of public fields it understands.
        return isinstance(payload, dict)
    return False


def _money(value: Any) -> bool:
    return isinstance(value, str) and _MONEY.fullmatch(value) is not None


def _order(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(value.get(key), str) and bool(value[key])
        for key in ("order_no", "status", "link")
    )


def _scan_data_valid(shape: str, data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    if shape == "config":
        return (type(data.get("available")) is bool
                and _integer(data.get("max_batch"), 1)
                and _integer(data.get("accepting_supplier_count"))
                and _money(data.get("unit_price"))
                and isinstance(data.get("cdk_purchase_url"), str))
    if shape == "balance":
        return (all(_money(data.get(key)) for key in ("balance", "frozen_balance", "unit_price"))
                and isinstance(data.get("cdk_purchase_url"), str))
    if shape == "orders":
        pagination = data.get("pagination")
        return (isinstance(data.get("items"), list) and all(map(_order, data["items"]))
                and isinstance(pagination, dict)
                and _integer(pagination.get("page"), 1)
                and _integer(pagination.get("page_size"), 1)
                and _integer(pagination.get("total"))
                and _integer(pagination.get("total_pages")))
    if shape == "submit":
        return (isinstance(data.get("batch_no"), str) and bool(data["batch_no"])
                and _integer(data.get("order_count"), 1)
                and isinstance(data.get("orders"), list)
                and len(data["orders"]) == data["order_count"]
                and all(map(_order, data["orders"]))
                and type(data.get("idempotent")) is bool
                and all(_money(data.get(key)) for key in ("balance", "frozen_balance", "total_amount")))
    return False


def _created_job_valid(data: dict[str, Any]) -> bool:
    # Remote URLs are deliberately unused, including for validation/routing.
    if data.get("ok") is not True:
        return False
    try:
        _job_id(data.get("job_id"))
        _header(data.get("job_secret"), "INVALID_JOB_SECRET")
    except ExternalClientError:
        return False
    return True


def _batch_result_valid(data: dict[str, Any]) -> bool:
    # Only the containers are confirmed. The service validates/maps individual
    # rows and decides business success; never discard partial job credentials.
    return isinstance(data.get("jobs"), list) and isinstance(data.get("rejected", []), list)


def _nonnegative_number(value: Any) -> bool:
    if type(value) not in (int, float, str):
        return False
    if isinstance(value, str) and (len(value) > 128 or not re.fullmatch(
            r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value)):
        return False
    try:
        number = Decimal(str(value))
        return number.is_finite() and number >= 0
    except InvalidOperation:
        return False


def _fused_cdk_valid(data: dict[str, Any]) -> bool:
    target = data.get("target")
    return (type(data.get("duplicate")) is bool
            and _nonnegative_number(data.get("transferred_points"))
            and isinstance(target, dict)
            and _nonnegative_number(target.get("remaining_uses")))


def _payment_base(value: Any) -> str:
    """Accept HTTPS DNS hosts, excluding local names and all IP literals.

    This validates configuration syntax without DNS/network side effects. The
    payment backend must also enforce public resolved addresses when connecting.
    """
    if (not isinstance(value, str) or not value or len(value) > 2048
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
            or any(char in value for char in "\\?#")):
        raise _error("INVALID_REQUEST")
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower().rstrip(".")
        labels = host.split(".")
        if (parts.scheme != "https" or parts.username is not None or parts.password is not None
                or (parts.port is not None and not 1 <= parts.port <= 65535)
                or len(host) > 253 or len(labels) < 2
                or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                       for label in labels)
                or not re.fullmatch(r"[a-z]{2,63}|xn--[a-z0-9-]{1,59}", labels[-1])
                or any(label in {"localhost", "localdomain", "local", "internal", "lan", "home"}
                       for label in labels)
                or labels[-1] in {"arpa", "onion", "test", "invalid"}):
            raise ValueError("Invalid payment host")
    except ValueError:
        raise _error("INVALID_REQUEST") from None
    return value.rstrip("/")


def _payment_credential(value: Any) -> str:
    if not isinstance(value, str):
        raise _error("CREDENTIALS_REQUIRED")
    return _header(value.strip())


def _payment_options(payment_platform, foarge_auth_mode, foarge_credential,
                     foarge_api_base, masi_cdk_ticket, masi_api_base,
                     masi_require_online_workers, seashore_cdk) -> dict[str, Any]:
    if (not isinstance(payment_platform, str)
            or payment_platform not in {"none", "foarge", "masi", "seashore"}
            or type(masi_require_online_workers) is not bool):
        raise _error("INVALID_REQUEST")
    body: dict[str, Any] = {"payment_platform": payment_platform}
    if payment_platform == "foarge":
        if not isinstance(foarge_auth_mode, str) or foarge_auth_mode not in {"pbk", "api_key", "cdk_session"}:
            raise _error("INVALID_REQUEST")
        body.update(foarge_auth_mode=foarge_auth_mode,
                    foarge_credential=_payment_credential(foarge_credential),
                    foarge_api_base=_payment_base(foarge_api_base), foarge_payment_method="upi")
    elif payment_platform == "masi":
        body.update(masi_cdk_ticket=_payment_credential(masi_cdk_ticket),
                    masi_api_base=_payment_base(masi_api_base),
                    masi_require_online_workers=masi_require_online_workers)
    elif payment_platform == "seashore":
        body["seashore_cdk"] = _payment_credential(seashore_cdk)
    return body


class ScanClient(_Client):
    def __init__(self, api_key="", bearer_token="", session=None) -> None:
        super().__init__(session)
        self._api_key = api_key
        self._bearer_token = bearer_token

    def config(self) -> dict[str, Any]:
        return self._request("GET", SCAN_BASE + "/api/v1/public/scan/config", scan_shape="config")

    def orders(self, page=1, page_size=20) -> dict[str, Any]:
        if not _integer(page, 1) or not _integer(page_size, 1):
            raise _error("INVALID_REQUEST")
        return self._request(
            "GET", SCAN_BASE + "/api/v1/client/orders",
            headers={"X-API-Key": _header(self._api_key)},
            params={"page": page, "page_size": page_size}, scan_shape="orders",
        )

    def submit(self, links, idempotency_key, access_tokens=None) -> dict[str, Any]:
        if not isinstance(links, (list, tuple)) or not links or any(
            not isinstance(link, str) or not link.strip() for link in links
        ):
            raise _error("INVALID_LINK_COUNT")
        if access_tokens is not None:
            if (not isinstance(access_tokens, (list, tuple))
                    or len(access_tokens) != len(links)
                    or any(not isinstance(token, str) or not token.strip()
                           or any(char.isspace() for char in token)
                           for token in access_tokens)):
                raise _error("INVALID_REQUEST")
        key = _header(idempotency_key, "INVALID_IDEMPOTENCY_KEY")
        if not 8 <= len(key) <= 80:
            raise _error("INVALID_IDEMPOTENCY_KEY")
        body = {"links": list(links)}
        # CDK-Scan's target API requires a one-to-one links/AT array.  Keep the
        # old shape when callers are using the manual scan UI, where no AT is
        # available and the existing endpoint accepts links alone.
        if access_tokens is not None:
            body["ats"] = [token.strip() for token in access_tokens]
        return self._request(
            "POST", SCAN_BASE + "/api/v1/client/orders",
            headers={"X-API-Key": _header(self._api_key), "Idempotency-Key": key},
            body=body, mutation=True, scan_shape="submit",
        )

    def submit_with_ats(self, links, access_tokens, idempotency_key) -> dict[str, Any]:
        """Submit the target CDK-Scan contract with one AT per payment link."""
        return self.submit(links, idempotency_key, access_tokens=access_tokens)

    def balance(self) -> dict[str, Any]:
        return self._request(
            "GET", SCAN_BASE + "/api/v1/user/overview",
            headers={"Authorization": "Bearer " + _header(self._bearer_token)},
            scan_shape="balance",
        )


class UpiClient(_Client):
    def __init__(self, cdk="", entry_proxy="", exit_proxy="", session=None) -> None:
        super().__init__(session)
        self._cdk = cdk
        self._entry_proxy = entry_proxy
        self._exit_proxy = exit_proxy

    def _extraction_body(self, use_promo, *payment_options) -> dict[str, Any]:
        if (type(use_promo) is not bool
                or not isinstance(self._entry_proxy, str)
                or not isinstance(self._exit_proxy, str)):
            raise _error("INVALID_REQUEST")
        return {"cdk": _header(self._cdk), "entry_proxy": self._entry_proxy,
                "exit_proxy": self._exit_proxy, "use_promo": use_promo,
                **_payment_options(*payment_options)}

    def create(self, token, use_promo=True, *, payment_platform="none",
               foarge_auth_mode="pbk", foarge_credential="", foarge_api_base=FOARGE_API_BASE,
               masi_cdk_ticket="", masi_api_base=MASI_API_BASE,
               masi_require_online_workers=True, seashore_cdk="") -> dict[str, Any]:
        if (not isinstance(token, (str, dict)) or not token
                or (isinstance(token, str) and not token.strip())):
            raise _error("INVALID_REQUEST")
        body = self._extraction_body(
            use_promo, payment_platform, foarge_auth_mode, foarge_credential, foarge_api_base,
            masi_cdk_ticket, masi_api_base, masi_require_online_workers, seashore_cdk,
        )
        return self._request(
            "POST", UPI_BASE + "/api/v1/extractions",
            body={**body, "token": token},
            mutation=True, created_job=True,
        )

    def create_batch(self, sessions, use_promo=True, *, payment_platform="none",
                     foarge_auth_mode="pbk", foarge_credential="", foarge_api_base=FOARGE_API_BASE,
                     masi_cdk_ticket="", masi_api_base=MASI_API_BASE,
                     masi_require_online_workers=True, seashore_cdk="") -> dict[str, Any]:
        """Return INTERNAL jobs/rejected rows, preserving partial creation.

        uncertain=True requires reconciliation, even if some jobs are usable.
        The service owns row mapping and persistence; counts need not match the
        submitted inputs. Rejected-only results describe a business failure.
        """
        if (not isinstance(sessions, (list, tuple)) or not sessions
                or any(not isinstance(token, (str, dict)) or not token
                       or (isinstance(token, str) and not token.strip()) for token in sessions)):
            raise _error("INVALID_REQUEST")
        body = self._extraction_body(
            use_promo, payment_platform, foarge_auth_mode, foarge_credential, foarge_api_base,
            masi_cdk_ticket, masi_api_base, masi_require_online_workers, seashore_cdk,
        )
        return self._request(
            "POST", UPI_BASE + "/api/v1/extractions/batch",
            body={**body, "sessions": list(sessions)}, mutation=True, upi_shape="batch",
        )

    def status(self, job_id, job_secret) -> dict[str, Any]:
        return self._request(
            "GET", UPI_BASE + "/api/v1/extractions/" + _job_id(job_id),
            headers={"X-UPI-Job-Secret": _header(job_secret, "INVALID_JOB_SECRET")},
        )

    def cancel(self, job_id, job_secret) -> dict[str, Any]:
        return self._request(
            "POST", UPI_BASE + "/api/v1/extractions/" + _job_id(job_id) + "/cancel",
            headers={"X-UPI-Job-Secret": _header(job_secret, "INVALID_JOB_SECRET")},
            body={}, mutation=True,
        )

    def cdk_status(self) -> dict[str, Any]:
        return self._request(
            "POST", UPI_BASE + "/api/v1/cdk/status", body={"cdk": _header(self._cdk)},
        )

    def cdk_fuse(self, codes, label="") -> dict[str, Any]:
        if (not isinstance(codes, (list, tuple)) or not 2 <= len(codes) <= 100
                or not isinstance(label, str) or len(label) > 120
                or any(ord(char) < 32 or ord(char) == 127 for char in label)):
            raise _error("INVALID_REQUEST")
        # Preserve the first card as the target; duplicates are input errors,
        # never silently change the requested fusion combination.
        normalized = [_payment_credential(code) for code in codes]
        if (len(set(normalized)) != len(normalized)
                or any(any(char.isspace() for char in code) for code in normalized)):
            raise _error("INVALID_REQUEST")
        return self._request(
            "POST", UPI_BASE + "/api/v1/cdk/fuse",
            body={"codes": normalized, "label": label.strip()}, mutation=True, upi_shape="fuse",
        )


def _cookie_value(value: Any) -> str:
    if (not isinstance(value, str) or not value or len(value) > 8192
            or any(ord(char) < 33 or ord(char) > 126 for char in value)):
        raise _error("INVALID_REQUEST")
    return value


def _public_proxy(value: Any) -> str:
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(ord(char) < 33 or ord(char) > 126 for char in value)):
        raise _error("INVALID_REQUEST")
    return value


class PublicUpiClient(_Client):
    """Client for the cookie-scoped公益 UPI extraction API."""

    def __init__(self, checkout_proxy="", update_proxy="", session_cookie="", session=None) -> None:
        super().__init__(session)
        self._checkout_proxy = checkout_proxy
        self._update_proxy = update_proxy
        self._session_cookie = ""
        if session_cookie:
            self._set_cookie(session_cookie)

    def _set_cookie(self, value: str) -> None:
        value = _cookie_value(value)
        self._session.cookies.set("upi_paid_session", value, domain="dasaobi.online", path="/upi")
        self._session_cookie = value

    @property
    def session_cookie(self) -> str:
        if self._session_cookie:
            return self._session_cookie
        try:
            values = [cookie.value for cookie in self._session.cookies
                      if getattr(cookie, "name", "") == "upi_paid_session"]
        except Exception:
            values = []
        if len(values) == 1:
            try:
                self._session_cookie = _cookie_value(values[0])
            except ExternalClientError:
                return ""
        return self._session_cookie

    def _body(self, access_tokens) -> dict[str, Any]:
        if (not isinstance(access_tokens, (list, tuple)) or not 1 <= len(access_tokens) <= 100
                or any(not isinstance(token, str) or not token.strip() or any(char.isspace() for char in token)
                       for token in access_tokens)):
            raise _error("INVALID_REQUEST")
        return {
            "access_tokens": [token.strip() for token in access_tokens],
            "checkout_proxy": _public_proxy(self._checkout_proxy),
            "update_proxy": _public_proxy(self._update_proxy),
        }

    def _headers(self) -> dict[str, str]:
        cookie = self.session_cookie
        if cookie:
            return {"Cookie": "upi_paid_session=" + cookie}
        return {}

    def create(self, access_token: str) -> dict[str, Any]:
        body = self._body([access_token])
        body = {"access_token": body["access_tokens"][0],
                "checkout_proxy": body["checkout_proxy"],
                "update_proxy": body["update_proxy"]}
        result = self._request(
            "POST", PUBLIC_UPI_BASE + "/api/tasks", headers=self._headers(),
            body=body, mutation=True, raw_shape="public_upi_create",
        )
        if self.session_cookie == "":
            raise _error("INVALID_RESPONSE", uncertain=True)
        return result

    def create_batch(self, access_tokens) -> dict[str, Any]:
        result = self._request(
            "POST", PUBLIC_UPI_BASE + "/api/tasks/batch", headers=self._headers(),
            body=self._body(access_tokens), mutation=True, raw_shape="public_upi_batch",
        )
        if self.session_cookie == "":
            raise _error("INVALID_RESPONSE", uncertain=True)
        return result

    def status(self, task_id: str) -> dict[str, Any]:
        task_id = _header(task_id, "INVALID_JOB_ID")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", task_id):
            raise _error("INVALID_JOB_ID")
        return self._request(
            "GET", PUBLIC_UPI_BASE + "/api/tasks/" + task_id,
            headers=self._headers(), raw_shape="public_upi_status",
        )

    def tasks(self) -> dict[str, Any]:
        return self._request(
            "GET", PUBLIC_UPI_BASE + "/api/tasks", headers=self._headers(),
        )

    def cancel(self, task_id: str) -> dict[str, Any]:
        task_id = _header(task_id, "INVALID_JOB_ID")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", task_id):
            raise _error("INVALID_JOB_ID")
        return self._request(
            "POST", PUBLIC_UPI_BASE + "/api/tasks/" + task_id + "/cancel",
            headers=self._headers(), body={}, mutation=True, raw_shape="public_upi_cancel",
        )


class SeashoreClient(_Client):
    """Client for Seashore's publisher API used by the group pipeline."""

    def __init__(self, cdk="", session=None) -> None:
        super().__init__(session)
        self._cdk = cdk

    def _headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer " + _header(self._cdk)}

    def me(self) -> dict[str, Any]:
        return self._request(
            "GET", SEASHORE_PUBLISHER_BASE + "/me", headers=self._headers(),
            raw_shape="seashore_me",
        )

    def create(self, email: str, access_token: str, pay_link: str) -> dict[str, Any]:
        if (not isinstance(email, str) or not re.fullmatch(r"[^@\s\x00-\x1f\x7f]{1,320}@[^@\s\x00-\x1f\x7f]{1,320}", email)
                or not isinstance(access_token, str) or not access_token.strip()
                or any(char.isspace() for char in access_token)
                or not isinstance(pay_link, str) or not pay_link.startswith(("http://", "https://"))
                or len(pay_link) > 4096 or any(char.isspace() or ord(char) < 32 for char in pay_link)):
            raise _error("INVALID_REQUEST")
        return self._request(
            "POST", SEASHORE_PUBLISHER_BASE + "/tasks", headers=self._headers(),
            body={"email": email, "access_token": access_token.strip(), "pay_link": pay_link},
            mutation=True, raw_shape="seashore_task",
        )

    def tasks(self) -> dict[str, Any]:
        return self._request(
            "GET", SEASHORE_PUBLISHER_BASE + "/tasks", headers=self._headers(),
            raw_shape="seashore_tasks",
        )

    def status(self, public_id: str) -> dict[str, Any]:
        public_id = _header(public_id, "INVALID_JOB_ID")
        if not _safe_identifier(public_id, prefix="ORD-"):
            raise _error("INVALID_JOB_ID")
        return self._request(
            "GET", SEASHORE_PUBLISHER_BASE + "/tasks/" + public_id,
            headers=self._headers(), raw_shape="seashore_task",
        )

    def cancel(self, public_id: str) -> dict[str, Any]:
        public_id = _header(public_id, "INVALID_JOB_ID")
        if not _safe_identifier(public_id, prefix="ORD-"):
            raise _error("INVALID_JOB_ID")
        return self._request(
            "POST", SEASHORE_PUBLISHER_BASE + "/tasks/" + public_id + "/cancel",
            headers=self._headers(), mutation=True, raw_shape="seashore_cancel",
        )

    def find(self, email: str, pay_link: str) -> dict[str, Any] | None:
        response = self.tasks()
        for item in response.get("tasks", []):
            if (isinstance(item, dict) and item.get("email") == email
                    and item.get("pay_link") == pay_link):
                return item
        return None


class FoargeClient(_Client):
    """Foarge publisher API v1 client.

    Foarge creates a task first and accepts the access token/payment link in a
    second request.  A failure in the second request is marked uncertain so a
    caller never silently falls through to another publisher and creates a
    duplicate order.
    """

    def __init__(self, cdk="", session=None) -> None:
        super().__init__(session)
        self._cdk = cdk

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": "Bearer " + _header(self._cdk),
            "User-Agent": "GPTRegisterExternalAPI/1.0",
        }

    @staticmethod
    def _task_id(payload: Any) -> str:
        task = payload.get("task") if isinstance(payload, dict) and isinstance(payload.get("task"), dict) else payload
        if not isinstance(task, dict):
            return ""
        for key in ("id", "task_id", "public_id", "order_id"):
            value = task.get(key)
            if isinstance(value, (str, int)) and str(value):
                return str(value)
        return ""

    def me(self) -> dict[str, Any]:
        return self._request(
            "GET", FOARGE_API_BASE + "/me", headers=self._headers(), raw_shape="foarge_me",
        )

    def create(self, email: str) -> dict[str, Any]:
        if not isinstance(email, str) or not re.fullmatch(
            r"[^@\s\x00-\x1f\x7f]{1,320}@[^@\s\x00-\x1f\x7f]{1,320}", email.strip()
        ):
            raise _error("INVALID_REQUEST")
        return self._request(
            "POST", FOARGE_API_BASE + "/tasks", headers=self._headers(),
            body={"payment_method": "upi", "account_email": email.strip()},
            mutation=True, raw_shape="foarge_task",
        )

    def submit_checkout(self, task_id: str, access_token: str, pay_link: str) -> dict[str, Any]:
        if (not _safe_identifier(task_id)
                or not isinstance(access_token, str) or not access_token.strip()
                or any(char.isspace() for char in access_token)
                or not isinstance(pay_link, str) or not pay_link.startswith(("http://", "https://"))
                or len(pay_link) > 4096 or any(char.isspace() or ord(char) < 32 for char in pay_link)):
            raise _error("INVALID_REQUEST")
        return self._request(
            "POST", FOARGE_API_BASE + "/tasks/" + task_id + "/submit-checkout",
            headers=self._headers(),
            body={"checkout_data": {"access_token": access_token.strip(), "pay_link": pay_link}},
            mutation=True, raw_shape="foarge_task",
        )

    def submit(self, email: str, access_token: str, pay_link: str) -> dict[str, Any]:
        created = self.create(email)
        task_id = self._task_id(created)
        if not task_id:
            raise _error("INVALID_RESPONSE", uncertain=True)
        try:
            submitted = self.submit_checkout(task_id, access_token, pay_link)
        except ExternalClientError as exc:
            # The task exists even when checkout submission's response is
            # lost; this must be reconciled instead of being auto-failed over.
            if not exc.uncertain:
                exc.uncertain = True
            raise
        return {"created": created, "submitted": submitted,
                "task": submitted.get("task") or created.get("task") or {}}


class MasiClient(_Client):
    """Masi integration API client."""

    def __init__(self, cdk="", session=None) -> None:
        super().__init__(session)
        self._cdk = cdk

    def _headers(self) -> dict[str, str]:
        return {"X-CDK": _header(self._cdk), "User-Agent": "GPTRegisterExternalAPI/1.0"}

    def check(self) -> dict[str, Any]:
        return self._request(
            "POST", MASI_API_BASE + "/api/integration/tickets/status",
            headers=self._headers(), body={}, raw_shape="masi_status",
        )

    def submit(self, email: str, access_token: str, pay_link: str) -> dict[str, Any]:
        if (not isinstance(access_token, str) or not access_token.strip()
                or any(char.isspace() for char in access_token)
                or not isinstance(pay_link, str) or not pay_link.startswith(("http://", "https://"))
                or len(pay_link) > 4096 or any(char.isspace() or ord(char) < 32 for char in pay_link)):
            raise _error("INVALID_REQUEST")
        body: dict[str, Any] = {"link": pay_link, "access_token": access_token.strip()}
        if isinstance(email, str) and email.strip():
            if not re.fullmatch(r"[^@\s\x00-\x1f\x7f]{1,320}@[^@\s\x00-\x1f\x7f]{1,320}", email.strip()):
                raise _error("INVALID_REQUEST")
            body["email"] = email.strip()
        return self._request(
            "POST", MASI_API_BASE + "/api/integration/orders",
            headers=self._headers(), body=body, mutation=True, raw_shape="masi_order",
        )


# Confirmed in the public /static/app.js (2026-09-19), not inferred from
# arbitrary upstream key names. No amount field is documented there/OpenAPI;
# add one only when its name and type have been confirmed.
_PUBLIC_LINKS = frozenset({
    "short_link", "verification_url", "provider_redirect_url", "checkout_url", "qr_data",
    "qr_image_png", "qr_image_svg",
})
_PUBLIC_STATES = frozenset({
    "payment_status", "payment_check_status", "upi_payment_state", "upi_setup_status",
    "payment_submit_status", "payment_platform", "plan_type", "foarge_status",
})
_PUBLIC_IDS = frozenset({"payment_submit_id", "foarge_task_id"})
_PUBLIC_ERRORS = frozenset({"payment_submit_error", "foarge_error"})
_SECRET_KEY = re.compile(
    r"token|secret|credential|password|passwd|authorization|api[_ -]?key|cdk|pbk|proxy|session",
    re.IGNORECASE,
)
_SECRET_TEXT = re.compile(
    r"token|secret|credential|password|passwd|authorization|bearer|api[_ -]?key|cdk|pbk|proxy|session"
    r"|卡密|密钥|凭证|代理|密码|口令|令牌|票据|://"
    r"|\b(?:sk|pk|key|sess)[_-][a-z0-9_-]+"
    r"|[^\s:@]+:[^\s@]+@[^\s]+"
    r"|\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}:[0-9]+"
    r"|\beyJ[a-z0-9_-]+\.[a-z0-9_-]+"
    r"|\b[a-z0-9_-]{40,}\b",
    re.IGNORECASE,
)
_PAYMENT_ERROR = "自动支付提交失败，请检查平台配置"
_PUBLIC_FLAGS = frozenset({
    "upi_mandate_succeeded", "entitlement_active", "has_active_subscription",
    "promo_requested", "promo_applied", "payment_submit_final",
})
_CREDENTIAL_QUERY_KEYS = frozenset({
    "access_token", "refresh_token", "id_token", "job_secret", "api_key", "cdk", "authorization",
})
_HIDE_QR_STATES = frozenset({
    "activation_pending", "verification_unavailable", "completed", "failed",
    "expired", "payment_timeout", "plus_not_received",
})


def _public_link(value: Any) -> bool:
    if (not isinstance(value, str) or not value or len(value) > MAX_RESPONSE_BYTES
            or any(char in value for char in "\r\n\x00")):
        return False
    try:
        parts = urlsplit(value)
        if parts.username is not None or parts.password is not None:
            return False
        # A payment token is part of the usable link; only named authentication
        # credentials are excluded. Do not substring-match the word "token".
        for component in (parts.query, parts.fragment):
            if any(key.lower() in _CREDENTIAL_QUERY_KEYS for key, _ in parse_qsl(component)):
                return False
        return (parts.scheme.lower() in ("http", "https", "upi") and bool(parts.netloc)) or value.startswith(
            ("data:image/png;base64,", "data:image/svg+xml;", "data:image/svg+xml,")
        )
    except ValueError:
        return False


def _public_secrets(data: dict[str, Any]) -> tuple[str, ...]:
    """Collect echoed credentials without trusting error text or nesting depth."""
    pending = [(data, False)]
    seen = set()
    secrets = set()
    while pending:
        value, sensitive = pending.pop()
        if isinstance(value, (dict, list, tuple)):
            marker = (id(value), sensitive)
            if marker in seen:
                continue
            seen.add(marker)
            if isinstance(value, dict):
                pending.extend((item, sensitive or bool(_SECRET_KEY.search(str(key))))
                               for key, item in value.items())
            else:
                pending.extend((item, sensitive) for item in value)
        elif sensitive and isinstance(value, str) and value.strip():
            secrets.add(value.strip().casefold())
            if value.lower().startswith("bearer "):
                secrets.add(value[7:].strip().casefold())
            try:
                parts = urlsplit(value)
                for part in (parts.username, parts.password):
                    if part:
                        secrets.add(unquote(part).casefold())
            except ValueError:
                pass
    return tuple(secret for secret in secrets if secret)


def _public_text_sensitive(value: str, secrets: tuple[str, ...]) -> bool:
    decoded = unquote(value).casefold()
    return bool(_SECRET_TEXT.search(decoded)) or any(secret in decoded for secret in secrets)


def public_upi_result(data: Any) -> dict[str, Any]:
    """Project a raw UPI result object (also accepts a status envelope).

    payment_link/qr_code/account_email/payment_status are always present for a
    result object. Link priority and QR visibility match public JS
    resultUrl/resultQr. Only the account email is public account information;
    unknown keys, nested objects, logs and credentials are excluded; selected
    payment errors are bounded and redacted. Submission success is not payment
    confirmation: payment_status and entitlement flags are never inferred from
    payment_submit_status or the legacy foarge_status.
    The main service normalizes the separate job status.
    """
    if not isinstance(data, dict):
        return {}
    result = data.get("result", data)
    if not isinstance(result, dict):
        return {}
    secrets = _public_secrets(data)
    public: dict[str, Any] = {}
    for key in _PUBLIC_LINKS:
        if _public_link(result.get(key)):
            public[key] = result[key]
    for key in _PUBLIC_STATES:
        value = result.get(key)
        if (isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value)
                and not _public_text_sensitive(value, secrets)):
            public[key] = value
    for key in _PUBLIC_IDS:
        value = result.get(key)
        if (isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}", value)
                and not _public_text_sensitive(value, secrets)):
            public[key] = value
    for key in _PUBLIC_ERRORS:
        value = result.get(key)
        if isinstance(value, str) and value:
            public[key] = (_PAYMENT_ERROR if len(value) > 512
                           or any(ord(char) < 32 or ord(char) == 127 for char in value)
                           or _public_text_sensitive(value, secrets) else value)
    # Match the public JS fallback for integrations predating payment_submit_*.
    for current, legacy in (("payment_submit_status", "foarge_status"),
                            ("payment_submit_id", "foarge_task_id"),
                            ("payment_submit_error", "foarge_error")):
        if not public.get(current) and legacy in public:
            public[current] = public[legacy]
    if not public.get("payment_platform") and public.get("foarge_status"):
        public["payment_platform"] = "foarge"
    for key in _PUBLIC_FLAGS:
        if type(result.get(key)) is bool:
            public[key] = result[key]
    expires_at = result.get("expires_at")
    if type(expires_at) in (int, float) and 0 <= expires_at <= 10**12 and math.isfinite(expires_at):
        public["expires_at"] = expires_at
    email = result.get("account_email")
    public["account_email"] = ""
    if (isinstance(email, str) and len(email) <= 320
            and re.fullmatch(r"[^@\s\x00-\x1f\x7f]+@[^@\s\x00-\x1f\x7f]+", email)):
        public["account_email"] = email
    public["payment_status"] = public.get("payment_status", "")
    public["payment_link"] = next((public[key] for key in (
        "short_link", "verification_url", "provider_redirect_url", "checkout_url", "qr_data",
    ) if public.get(key)), "")
    public["qr_code"] = ""
    if public["payment_status"] not in _HIDE_QR_STATES:
        public["qr_code"] = public.get("qr_image_png") or public.get("qr_image_svg", "")
    else:
        public.pop("qr_image_png", None)
        public.pop("qr_image_svg", None)
    return public

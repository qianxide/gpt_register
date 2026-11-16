"""External service routes; panel authentication is supplied by web_app."""

from __future__ import annotations

import asyncio
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_SESSIONS_CHARS = 1_000_000
_ID_PATTERN = r"^[A-Za-z0-9_-]+$"
_FIELDS = frozenset({
    "body", "query", "path", "scan", "upi", "api_key", "cdk_scan_api_key", "bearer_token",
    "clear_api_key", "clear_bearer_token", "cdk", "entry_proxy", "exit_proxy",
    "clear_cdk", "clear_entry_proxy", "clear_exit_proxy", "publisher_cdk", "clear_publisher_cdk", "clear_cdk_scan_api_key", "request_id", "links",
    "unit_price", "sessions_text", "use_promo", "page", "page_size", "refresh", "local_id",
    "group_id", "batch_size", "task_id", "account_keys", "selected_only",
    "payment_platform", "foarge_auth_mode", "foarge_api_base", "masi_api_base",
    "masi_require_online_workers", "foarge_credential", "masi_cdk_ticket", "seashore_cdk",
    "foarge_cdk", "masi_cdk", "clear_foarge_cdk", "clear_masi_cdk",
    "clear_foarge_credential", "clear_masi_cdk_ticket", "clear_seashore_cdk", "codes", "label",
    "upi_provider", "scan_provider", "email", "access_token", "pay_link", "target",
})
_VALIDATION_MESSAGES = {
    "missing": "缺少必填字段",
    "extra_forbidden": "存在多余字段",
    "json_invalid": "JSON 格式无效",
    "string_too_short": "文本长度不足",
    "string_too_long": "文本长度超出限制",
    "too_short": "条目数量不足",
    "too_long": "条目数量超出限制",
    "greater_than_equal": "数值低于下限",
    "less_than_equal": "数值超出上限",
    "string_pattern_mismatch": "文本格式无效",
    "literal_error": "请选择有效的选项",
    "bool_type": "请填写布尔值",
    "string_type": "请填写文本",
    "list_type": "请填写列表",
}
_PRIVATE_FIELDS = frozenset({
    "apikey", "bearertoken", "cdk", "entryproxy", "exitproxy", "authorization",
    "token", "accesstoken", "refreshtoken", "idtoken", "jobsecret", "sessiontoken", "password", "secret",
    "session", "sessions", "sessionstext", "request", "rawrequest", "requestbody",
    "raw", "rawresponse", "responsetext",
    "ticket", "cdkticket", "masicdkticket", "credential", "credentials", "foargecredential",
    "seashorecdk", "codes", "rawcodes", "sourcecodes", "cdks",
})
_CREDENTIAL = re.compile(
    r"(?i)\b(api[_-]?key|(?:bearer[_-]?|access[_-]?|refresh[_-]?|session[_-]?)token|job[_-]?secret|"
    r"(?:masi[_-]?)?cdk[_-]?ticket|ticket|(?:foarge[_-]?)?credentials?|(?:seashore[_-]?)?cdk|"
    r"(?:raw[_-]?|source[_-]?)?codes|cdks|"
    r"password|secret|entry[_-]?proxy|exit[_-]?proxy)\b[\"']?\s*[=:]\s*"
    r"(?:\[(?:[^\[\]\"']|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')*\]|"
    r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;}&]+)",
)
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;\"'}]+")


def _validation_detail(exc: RequestValidationError) -> list[dict]:
    """Neither error messages nor attacker-controlled field names are public."""
    errors = []
    for error in exc.errors()[:20]:
        kind = error.get("type", "")
        location = error.get("loc", ())
        loc = [part if type(part) is int or part in _FIELDS else "[field]" for part in location]
        if kind == "extra_forbidden" and loc:
            loc[-1] = "[field]"
        errors.append({
            "loc": loc,
            "msg": _VALIDATION_MESSAGES.get(kind, "请求参数无效"),
            "type": kind if kind in _VALIDATION_MESSAGES else "value_error",
        })
    return errors


class _ExternalRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        query_names = {field.alias for field in self.dependant.query_params}

        async def bounded_handler(request: Request):
            length = request.headers.get("content-length")
            if length is not None:
                try:
                    size = int(length)
                except ValueError:
                    raise HTTPException(400, "请求长度无效") from None
                if size < 0:
                    raise HTTPException(400, "请求长度无效")
                if size > MAX_BODY_BYTES:
                    raise HTTPException(413, "请求正文超出 2 MiB 限制")
            # Check the actual stream too: Content-Length can be absent or false.
            payload = bytearray()
            async for chunk in request.stream():
                if len(payload) + len(chunk) > MAX_BODY_BYTES:
                    raise HTTPException(413, "请求正文超出 2 MiB 限制")
                payload.extend(chunk)
            request._body = bytes(payload)
            if any(name not in query_names for name in request.query_params):
                raise HTTPException(422, [{
                    "loc": ["query", "[field]"], "msg": _VALIDATION_MESSAGES["extra_forbidden"],
                    "type": "extra_forbidden",
                }])
            try:
                return await handler(request)
            except RequestValidationError as exc:
                raise HTTPException(422, _validation_detail(exc)) from None

        return bounded_handler


router = APIRouter(prefix="/api/external", route_class=_ExternalRoute)

Credential = Annotated[str, Field(strict=True, max_length=8192)]
Proxy = Annotated[str, Field(strict=True, max_length=2048)]
APIBase = Annotated[str, Field(strict=True, min_length=1, max_length=2048, pattern=r"^https://\S+$")]
PaymentPlatform = Literal["none", "foarge", "masi", "seashore"]
ExternalTarget = Literal["seashore", "foarge", "cdk_scan", "masi", "auto"]
FuseCode = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=8192)]
RequestID = Annotated[str, Field(strict=True, min_length=8, max_length=80, pattern=_ID_PATTERN)]
Links = Annotated[
    list[Annotated[str, Field(strict=True, min_length=1, max_length=2048)]],
    Field(min_length=1, max_length=200),
]
Page = Annotated[int, Query(ge=1, le=1_000_000)]
PageSize = Annotated[int, Query(ge=1, le=100)]
RequestIDPath = Annotated[str, Path(min_length=8, max_length=80, pattern=_ID_PATTERN)]
LocalIDPath = Annotated[str, Path(min_length=1, max_length=128, pattern=_ID_PATTERN)]


class _RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ScanSettingsUpdate(_RequestModel):
    api_key: Credential | None = None
    cdk_scan_api_key: Credential | None = None
    bearer_token: Credential | None = None
    seashore_cdk: Credential | None = None
    publisher_cdk: Credential | None = None
    foarge_cdk: Credential | None = None
    masi_cdk: Credential | None = None
    clear_api_key: bool = False
    clear_cdk_scan_api_key: bool = False
    clear_bearer_token: bool = False
    clear_seashore_cdk: bool = False
    clear_publisher_cdk: bool = False
    clear_foarge_cdk: bool = False
    clear_masi_cdk: bool = False


class UpiSettingsUpdate(_RequestModel):
    cdk: Credential | None = None
    entry_proxy: Proxy | None = None
    exit_proxy: Proxy | None = None
    clear_cdk: bool = False
    clear_entry_proxy: bool = False
    clear_exit_proxy: bool = False
    payment_platform: PaymentPlatform | None = None
    foarge_auth_mode: Literal["pbk", "api_key", "cdk_session"] | None = None
    foarge_api_base: APIBase | None = None
    masi_api_base: APIBase | None = None
    masi_require_online_workers: bool = True
    foarge_credential: Credential | None = None
    masi_cdk_ticket: Credential | None = None
    seashore_cdk: Credential | None = None
    clear_foarge_credential: bool = False
    clear_masi_cdk_ticket: bool = False
    clear_seashore_cdk: bool = False

    @field_validator("foarge_api_base", "masi_api_base")
    @classmethod
    def validate_api_base(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            parsed = urlsplit(value)
            valid = (parsed.scheme == "https" and bool(parsed.hostname)
                     and parsed.username is None and parsed.password is None
                     and not parsed.query and not parsed.fragment
                     and parsed.port != 0 and not re.search(r"[\s\\\x00-\x1f\x7f]", value))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("请填写有效的 HTTPS API 地址")
        return value


class SettingsUpdate(_RequestModel):
    scan: ScanSettingsUpdate | None = None
    upi: UpiSettingsUpdate | None = None


class ScanQuoteRequest(_RequestModel):
    links: Links


class ScanOrderRequest(ScanQuoteRequest):
    request_id: RequestID
    # Preserve the decimal string exactly; pricing rules belong to the service.
    unit_price: str = Field(min_length=1, max_length=128)


class ExternalCheckRequest(_RequestModel):
    pass


class ExternalSubmitRequest(_RequestModel):
    email: str = Field(default="", max_length=320)
    access_token: str = Field(min_length=1, max_length=200_000)
    pay_link: str = Field(min_length=1, max_length=4096)
    request_id: RequestID | None = None
    unit_price: str = Field(default="", max_length=128)


class UpiCreateRequest(_RequestModel):
    request_id: RequestID
    sessions_text: str = Field(min_length=1, max_length=MAX_SESSIONS_CHARS)
    use_promo: bool = True
    payment_platform: PaymentPlatform | None = None


class UpiFuseRequest(_RequestModel):
    codes: list[FuseCode] = Field(min_length=2, max_length=100)
    label: str = Field(default="", max_length=120)


class EmptyRequest(_RequestModel):
    pass


class PipelinePreviewRequest(_RequestModel):
    group_id: str = Field(min_length=1, max_length=128)
    batch_size: Literal[10, 20, 30, 50] = 20
    account_keys: list[str] | None = Field(default=None, max_length=10000)


class PipelineStartRequest(PipelinePreviewRequest):
    request_id: RequestID
    use_promo: bool = True
    unit_price: str = Field(default="", max_length=128)
    upi_provider: Literal["legacy", "public"] = "legacy"
    scan_provider: Literal["cdk_scan", "seashore", "foarge", "masi", "auto"] = "cdk_scan"


def _request_secrets(kwargs: dict) -> tuple[str, ...]:
    values = []
    updates = kwargs.get("updates", {})
    for group in ("scan", "upi"):
        for key, value in updates.get(group, {}).items():
            if re.sub(r"[^a-z]", "", key.lower()) in _PRIVATE_FIELDS and isinstance(value, str) and value:
                values.append(value)
                if value.strip():
                    values.append(value.strip())
    if kwargs.get("sessions_text"):
        values.append(kwargs["sessions_text"])
    if kwargs.get("access_token"):
        values.append(kwargs["access_token"])
    values.extend(code for code in kwargs.get("codes", ()) if isinstance(code, str) and code)
    return tuple(sorted(set(values), key=len, reverse=True))


def _public_result(value, secrets: tuple[str, ...] = ()):
    """Keep public service metadata, with a final guard against raw payloads."""
    if isinstance(value, dict):
        return {
            key: _public_result(item, secrets) for key, item in value.items()
            if re.sub(r"[^a-z]", "", str(key).lower()) not in _PRIVATE_FIELDS
        }
    if isinstance(value, (list, tuple)):
        return [_public_result(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[redacted]")
        return _BEARER.sub("Bearer [redacted]", _CREDENTIAL.sub(r"\1=[redacted]", value))
    return value


def _error_response(status: int, message: str, code: str, retry_after=None) -> JSONResponse:
    body = {"detail": message, "message": message, "code": code}
    headers = {}
    # Only delta-seconds are accepted, never arbitrary upstream header text.
    if type(retry_after) is int and retry_after >= 0:
        body["retry_after"] = retry_after
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(body, status_code=status, headers=headers)


def _call_service(method: str, **kwargs):
    # The main agent can add the service after this router has been imported.
    try:
        from common.external_api import ExternalAPIError, get_external_service
    except ImportError:
        return _error_response(503, "外部服务暂未就绪", "service_unavailable")

    secrets = _request_secrets(kwargs)
    try:
        result = getattr(get_external_service(), method)(**kwargs)
        return _public_result(result, secrets)
    except ExternalAPIError as exc:
        status = exc.status if type(exc.status) is int and 400 <= exc.status <= 599 else 502
        # Panel api.js redirects every 401 to /login; upstream auth is separate.
        if status == 401:
            status = 502
        message = _public_result(exc.message, secrets) if isinstance(exc.message, str) else "外部服务返回错误"
        code = _public_result(exc.code, secrets) if isinstance(exc.code, str) else "external_api_error"
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code):
            code = "external_api_error"
        return _error_response(status, message, code, exc.retry_after)
    except KeyError as exc:
        message = str(exc.args[0] if exc.args else "资源不存在")[:400]
        return _error_response(404, message or "资源不存在", "not_found")
    except ValueError as exc:
        message = str(exc)[:400] or "请求参数格式错误"
        return _error_response(400, message, "invalid_request")
    except Exception:
        # Transport/storage exceptions can embed credentials or complete sessions.
        return _error_response(502, "外部服务请求失败，请稍后重试", "external_api_error")


@router.get("/settings")
async def settings():
    return await asyncio.to_thread(_call_service, "settings")


@router.put("/settings")
async def save_settings(body: SettingsUpdate):
    return await asyncio.to_thread(
        _call_service, "save_settings", updates=body.model_dump(exclude_unset=True, exclude_none=True),
    )


@router.get("/scan/config")
async def scan_config():
    return await asyncio.to_thread(_call_service, "scan_config")


@router.post("/scan/quote")
async def scan_quote(body: ScanQuoteRequest):
    return await asyncio.to_thread(_call_service, "scan_quote", links=body.links)


@router.get("/scan/orders")
async def scan_orders(page: Page = 1, page_size: PageSize = 20):
    return await asyncio.to_thread(_call_service, "scan_orders", page=page, page_size=page_size)


@router.post("/scan/orders")
async def scan_submit(body: ScanOrderRequest):
    return await asyncio.to_thread(_call_service, "scan_submit", **body.model_dump())


@router.get("/scan/submissions")
async def scan_submissions(page: Page = 1, page_size: PageSize = 20):
    return await asyncio.to_thread(_call_service, "scan_submissions", page=page, page_size=page_size)


@router.post("/scan/submissions/{request_id}/retry")
async def scan_retry(request_id: RequestIDPath, body: EmptyRequest):
    return await asyncio.to_thread(_call_service, "scan_retry", request_id=request_id)


@router.post("/scan/balance")
async def scan_balance(body: EmptyRequest):
    return await asyncio.to_thread(_call_service, "scan_balance")


@router.post("/seashore/check", include_in_schema=False)
async def external_seashore_check(body: ExternalCheckRequest):
    return await asyncio.to_thread(_call_service, "seashore_me")


@router.post("/publisher/check", include_in_schema=False)
async def external_publisher_check(body: ExternalCheckRequest):
    return await asyncio.to_thread(_call_service, "seashore_me")


@router.post("/foarge/check", include_in_schema=False)
async def external_foarge_check(body: ExternalCheckRequest):
    return await asyncio.to_thread(_call_service, "foarge_me")


@router.post("/cdk-scan/check", include_in_schema=False)
async def external_cdk_scan_check(body: ExternalCheckRequest):
    return await asyncio.to_thread(_call_service, "cdk_scan_check")


@router.post("/masi/check", include_in_schema=False)
async def external_masi_check(body: ExternalCheckRequest):
    return await asyncio.to_thread(_call_service, "masi_check")


@router.post("/{target}/submit", include_in_schema=False)
async def external_submit(target: ExternalTarget, body: ExternalSubmitRequest):
    payload = body.model_dump(exclude_none=True)
    payload.pop("request_id", None)
    payload["target"] = target
    payload["request_id"] = body.request_id or ""
    return await asyncio.to_thread(_call_service, "external_submit", **payload)


@router.post("/upi/credits")
async def upi_credits(body: EmptyRequest):
    return await asyncio.to_thread(_call_service, "upi_credits")


@router.post("/upi/fuse")
async def upi_fuse(body: UpiFuseRequest):
    return await asyncio.to_thread(_call_service, "upi_fuse", **body.model_dump())


@router.post("/upi/jobs")
async def upi_create(body: UpiCreateRequest):
    return await asyncio.to_thread(_call_service, "upi_create", **body.model_dump(exclude_none=True))


@router.get("/upi/jobs")
async def upi_jobs(page: Page = 1, page_size: PageSize = 20, refresh: bool = False):
    return await asyncio.to_thread(
        _call_service, "upi_jobs", page=page, page_size=page_size, refresh=refresh,
    )


@router.post("/upi/jobs/{local_id}/refresh")
async def upi_refresh(local_id: LocalIDPath, body: EmptyRequest):
    return await asyncio.to_thread(_call_service, "upi_refresh", local_id=local_id)


@router.post("/upi/jobs/{local_id}/cancel")
async def upi_cancel(local_id: LocalIDPath, body: EmptyRequest):
    return await asyncio.to_thread(_call_service, "upi_cancel", local_id=local_id)


@router.post("/pipeline/preview")
async def pipeline_preview(body: PipelinePreviewRequest):
    return await asyncio.to_thread(_call_service, "pipeline_preview", **body.model_dump())


@router.post("/pipeline/start")
async def pipeline_start(body: PipelineStartRequest):
    return await asyncio.to_thread(_call_service, "pipeline_start", **body.model_dump())


@router.get("/pipeline/status")
async def pipeline_status(page: Page = 1, page_size: PageSize = 20,
                          task_id: Annotated[str | None, Query(min_length=1, max_length=128, pattern=_ID_PATTERN)] = None,
                          selected_only: bool = False):
    return await asyncio.to_thread(_call_service, "pipeline_status", task_id=task_id, page=page,
                                   page_size=page_size, selected_only=selected_only)


@router.post("/pipeline/{task_id}/stop")
async def pipeline_stop(task_id: LocalIDPath, body: EmptyRequest):
    return await asyncio.to_thread(_call_service, "pipeline_stop", task_id=task_id)


@router.post("/pipeline/{task_id}/resume")
async def pipeline_resume(task_id: LocalIDPath, body: EmptyRequest):
    return await asyncio.to_thread(_call_service, "pipeline_resume", task_id=task_id)

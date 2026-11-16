"""Local imported-number management; authentication is applied by web_app."""

from __future__ import annotations

import asyncio
import re
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field


MAX_BODY_BYTES = 2_000_000
MAX_IMPORT_CHARS = 1_000_000
MAX_DELETE_IDS = 1000
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_CREDENTIAL = re.compile(
    r"\b(api[_-]?key|token|secret|password|key)\s*[=:]\s*[^\s,;]+", re.IGNORECASE,
)


def _public_result(value):
    """Keep UI metadata while excluding receive URLs and private provider data."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            name = re.sub(r"[^a-z]", "", str(key).lower())
            if name in {"key", "text", "raw", "rawresponse", "responsetext", "authorization"}:
                continue
            if any(part in name for part in ("url", "token", "secret", "password", "apikey")):
                continue
            result[key] = _public_result(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_public_result(item) for item in value]
    if isinstance(value, str):
        return _CREDENTIAL.sub(r"\1=[隐藏]", _URL.sub("[取码地址已隐藏]", value))
    return value


class _SmsPoolRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded_handler(request: Request):
            if request.method in {"POST", "DELETE"}:
                length = request.headers.get("content-length")
                if length is not None:
                    try:
                        size = int(length)
                    except ValueError:
                        raise HTTPException(400, "请求长度格式错误") from None
                    if size < 0:
                        raise HTTPException(400, "请求长度格式错误")
                    if size > MAX_BODY_BYTES:
                        raise HTTPException(413, "请求内容过大，请分批操作")
                # Bound chunked bodies as well, before FastAPI parses the JSON.
                payload = bytearray()
                async for chunk in request.stream():
                    if len(payload) + len(chunk) > MAX_BODY_BYTES:
                        raise HTTPException(413, "请求内容过大，请分批操作")
                    payload.extend(chunk)
                request._body = bytes(payload)
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # FastAPI's default validation errors echo the import text.
                errors = [
                    {"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
                    for error in exc.errors()
                ]
                raise HTTPException(422, _public_result(errors)) from None

        return bounded_handler


router = APIRouter(route_class=_SmsPoolRoute)


class SmsPoolImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(strict=True, min_length=1, max_length=MAX_IMPORT_CHARS)


class SmsPoolDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]] | None = Field(
        default=None, max_length=MAX_DELETE_IDS,
    )
    exhausted_only: bool = Field(default=False, strict=True)


def _call_pool(method: str, **kwargs) -> dict:
    from common.sms_number_pool import SmsPoolError, get_sms_pool

    try:
        result = getattr(get_sms_pool(), method)(**kwargs)
    except SmsPoolError:
        # Parser/storage exceptions may contain the original import line.
        raise HTTPException(400, "号码池操作失败，请检查输入后重试") from None
    except Exception:
        raise HTTPException(503, "号码池暂不可用，请稍后重试") from None
    return {"ok": True, **_public_result(result)}


@router.get("/api/sms-pool")
async def sms_pool_list(
    page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status: Literal["all", "available", "reserved", "cooling", "exhausted"] = "all",
    q: Annotated[str, Query(max_length=200)] = "",
):
    return await asyncio.to_thread(
        _call_pool, "list_numbers", page=page, page_size=page_size, status=status, q=q.strip(),
    )


@router.post("/api/sms-pool/import")
async def sms_pool_import(body: SmsPoolImportRequest):
    return await asyncio.to_thread(_call_pool, "import_text", text=body.text)


@router.delete("/api/sms-pool")
async def sms_pool_delete(body: SmsPoolDeleteRequest):
    if not body.ids and not body.exhausted_only:
        raise HTTPException(400, "请选择号码或指定仅删除已耗尽号码")
    return await asyncio.to_thread(
        _call_pool, "delete", ids=body.ids, exhausted_only=body.exhausted_only,
    )

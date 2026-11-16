"""mail.com adapter settings for the email-rebind page.

The web panel stores only the adapter connection and account allow-list. The
mail.com password is sent to the local adapter and remains there; it is never
written to this project's config or returned to the browser.
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import mail as mail_state

router = APIRouter()

_EMAIL_RE = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")
_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$", re.I)
MAILCOM_ALIAS_LIMIT = 9
MAILCOM_ALIAS_NAME_MODES = {"random", "name_digits"}
MAILCOM_IMPORT_CONCURRENCY_DEFAULT = 3
MAILCOM_IMPORT_CONCURRENCY_MAX = 10
MAILCOM_DELETE_CONCURRENCY_DEFAULT = 3
MAILCOM_DELETE_CONCURRENCY_MAX = 100
MAILCOM_SETTINGS_CONCURRENCY_DEFAULT = 3
MAILCOM_SETTINGS_CONCURRENCY_MAX = 50
MAILCOM_PROXY_POOL_MAX = 2000
MAILCOM_BATCH_ACCOUNT_MAX = 300


def _state() -> Any:
    import web_app

    return web_app


class MailcomSettingsRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str | None = Field(default=None, max_length=512)
    api_base: str | None = Field(default=None, max_length=2048)
    admin_key: str | None = Field(default=None, max_length=512)
    alias_domain: str = Field(default="random", max_length=254)
    alias_domains: list[str] | None = Field(default=None, max_length=50)
    alias_name_mode: str | None = Field(default=None, max_length=32)
    alias_name_length: int | None = Field(default=None, ge=8, le=32)
    import_concurrency: int | None = Field(default=None, ge=1, le=MAILCOM_IMPORT_CONCURRENCY_MAX)
    settings_concurrency: int | None = Field(
        default=None,
        ge=1,
        le=MAILCOM_SETTINGS_CONCURRENCY_MAX,
    )
    # Save credentials without an upstream login; verification is opt-in.
    verify: bool | None = False


class MailcomBulkImportRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2_000_000)
    api_base: str | None = Field(default=None, max_length=2048)
    admin_key: str | None = Field(default=None, max_length=512)
    alias_domain: str = Field(default="random", max_length=254)
    alias_domains: list[str] | None = Field(default=None, max_length=50)
    alias_name_mode: str | None = Field(default=None, max_length=32)
    alias_name_length: int | None = Field(default=None, ge=8, le=32)
    concurrency: int = Field(default=MAILCOM_IMPORT_CONCURRENCY_DEFAULT, ge=1, le=MAILCOM_IMPORT_CONCURRENCY_MAX)
    # See ``MailcomSettingsRequest.verify``.
    verify: bool | None = False


class MailcomProxyPoolRequest(BaseModel):
    enabled: bool = True
    text: str | None = Field(default=None, max_length=2_000_000)
    reassign: bool = False
    clear: bool = False


class MailcomAliasBatchDeleteRequest(BaseModel):
    emails: list[str] = Field(min_length=1, max_length=MAILCOM_BATCH_ACCOUNT_MAX)
    concurrency: int = Field(default=MAILCOM_DELETE_CONCURRENCY_DEFAULT, ge=1, le=MAILCOM_DELETE_CONCURRENCY_MAX)


class MailcomAccountBatchRemoveRequest(BaseModel):
    emails: list[str] = Field(min_length=1, max_length=MAILCOM_BATCH_ACCOUNT_MAX)
    concurrency: int = Field(default=MAILCOM_DELETE_CONCURRENCY_DEFAULT, ge=1, le=MAILCOM_DELETE_CONCURRENCY_MAX)


def _normal_email(value: Any) -> str:
    email = str(value or "").strip().lower()
    if not _EMAIL_RE.fullmatch(email):
        raise HTTPException(400, "mail.com 账号邮箱格式无效")
    return email


def _parse_proxy_pool_text(value: str) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for line_number, raw in enumerate(str(value or "").splitlines(), start=1):
        proxy = raw.strip()
        if not proxy or proxy.startswith("#"):
            continue
        if len(proxy) > 2048:
            raise HTTPException(400, f"第 {line_number} 行代理地址过长")
        if proxy not in seen:
            seen.add(proxy)
            items.append(proxy)
        if len(items) > MAILCOM_PROXY_POOL_MAX:
            raise HTTPException(400, f"代理池最多保存 {MAILCOM_PROXY_POOL_MAX} 条")
    return items


def _normal_domain(value: Any) -> str:
    domain = str(value or "random").strip().lower() or "random"
    if domain != "random" and not _DOMAIN_RE.fullmatch(domain):
        raise HTTPException(400, "mail.com 别名域名格式无效")
    return domain


def _normal_domains(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        raw_values = re.split(r"[,\n\r]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        raise HTTPException(400, "mail.com 别名域名列表格式无效")
    domains: list[str] = []
    for raw in raw_values:
        text = str(raw or "").strip().lower()
        if not text or text == "random":
            continue
        domain = _normal_domain(text)
        if domain not in domains:
            domains.append(domain)
    return domains


def _normal_alias_name_mode(value: Any, fallback: Any = "random") -> str:
    mode = str((fallback or "random") if value is None else value or "random").strip().lower()
    if mode not in MAILCOM_ALIAS_NAME_MODES:
        raise HTTPException(400, "mail.com 别名格式无效")
    return mode


def _normal_alias_name_length(value: Any, fallback: Any = 17, mode: str = "random") -> int:
    try:
        length = int(fallback if value is None else value)
    except (TypeError, ValueError):
        raise HTTPException(400, "mail.com 别名长度必须为 8 到 32 位") from None
    if not 8 <= length <= 32:
        raise HTTPException(400, "mail.com 别名长度必须为 8 到 32 位")
    # Accept legacy name_digits lengths while reserving space for a full name.
    return max(13, length) if mode == "name_digits" else length


def _normal_import_concurrency(value: Any, fallback: Any = MAILCOM_IMPORT_CONCURRENCY_DEFAULT) -> int:
    try:
        concurrency = int(fallback if value is None else value)
    except (TypeError, ValueError):
        concurrency = MAILCOM_IMPORT_CONCURRENCY_DEFAULT
    return max(1, min(MAILCOM_IMPORT_CONCURRENCY_MAX, concurrency))


def _normal_settings_concurrency(
    value: Any,
    fallback: Any = MAILCOM_SETTINGS_CONCURRENCY_DEFAULT,
) -> int:
    try:
        concurrency = int(fallback if value is None else value)
    except (TypeError, ValueError):
        concurrency = MAILCOM_SETTINGS_CONCURRENCY_DEFAULT
    return max(1, min(MAILCOM_SETTINGS_CONCURRENCY_MAX, concurrency))


def _normal_api_base(value: Any) -> str:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        raise HTTPException(400, "请填写 mail.com 适配器 API URL")
    try:
        parsed = urlparse(raw)
    except Exception:
        parsed = None
    if (
        parsed is None
        or parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(400, "mail.com API URL 必须是无凭证的 HTTP(S) 地址")
    return raw


def _safe_error(error: Any) -> str:
    value = str(error or "").strip()
    # Adapter errors already redact its admin key. This second pass also keeps
    # connection-library query strings from reaching the browser.
    value = re.sub(
        r"(?i)\b(key|token|secret|password|apikey|api_key|auth_code|authcode)=[^&\s'\"]+",
        r"\1=***",
        value,
    )
    return value[:500]


def _provider_entry(cfg: dict[str, Any]) -> dict[str, Any]:
    saved = _state()._find_provider(cfg, "mailcom_api")
    if isinstance(saved, dict):
        entry = dict(saved)
        if not str(entry.get("provider_ref") or "").strip():
            # State reservations are keyed by the stable positional provider
            # reference used by the shared mail provider factory.
            for index, item in enumerate(_state()._providers(cfg), start=1):
                if item is saved or (
                    isinstance(item, dict)
                    and _state().normalize_provider_type(str(item.get("type") or "")) == "mailcom_api"
                ):
                    entry["provider_ref"] = f"mailcom_api#{index}"
                    break
    else:
        entry = {
            "type": "mailcom_api",
            "enable": True,
            "rebind_only": True,
            "label": "mail.com 域名邮箱",
            "api_base": "",
            "admin_key": "",
            "admin_key_env": "MAILCODE_ADMIN_KEY",
            "accounts": "",
            "alias_domain": "random",
            "mailboxes": "",
        }
    entry["type"] = "mailcom_api"
    return entry


def _public_settings(
    cfg: dict[str, Any],
    *,
    account_email: str = "",
    domains: list[str] | None = None,
    login_ok: bool | None = None,
    error: str = "",
    include_usage: bool = True,
) -> dict[str, Any]:
    entry = _provider_entry(cfg)
    conf = _state()._make_config(
        cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {}
    )
    provider = _state().MailcomApiProvider(entry, conf)
    proxy_pool: dict[str, Any] = {
        "enabled": False,
        "count": 0,
        "assigned": 0,
        "generation": 0,
        "items": [],
    }
    try:
        fetch_proxy_pool = getattr(provider, "fetch_proxy_pool", None)
        if callable(fetch_proxy_pool):
            try:
                remote_proxy_pool = fetch_proxy_pool()
                if isinstance(remote_proxy_pool, dict):
                    proxy_pool.update(remote_proxy_pool)
            except Exception:
                pass
        accounts = list(provider.accounts)
        remote_rows: list[dict[str, Any]] = []
        fetch_accounts = getattr(provider, "fetch_accounts", None)
        if callable(fetch_accounts):
            try:
                remote_rows = [
                    row for row in fetch_accounts(force=True)
                    if isinstance(row, dict)
                ]
            except Exception:
                # The saved account list remains useful when the adapter is
                # temporarily offline or its management key needs repair.
                remote_rows = []
        if not accounts:
            accounts = [
                str(row.get("email") or "").strip().lower()
                for row in remote_rows
                if str(row.get("email") or "").strip()
            ]
        metadata = {
            str(row.get("email") or "").strip().lower(): row
            for row in remote_rows
            if str(row.get("email") or "").strip()
        }
        usage_metadata: dict[str, dict[str, int]] = {}
        local_usage_loaded = False
        settings_concurrency = _normal_settings_concurrency(
            entry.get("settings_concurrency", MAILCOM_SETTINGS_CONCURRENCY_DEFAULT)
        )
        state_snapshot = mail_state._load_state()
        bindings_snapshot = mail_state._known_mailbox_bindings()

        fetch_inventory = getattr(provider, "fetch_alias_inventory", None)
        summarize_inventory = getattr(provider, "alias_usage_summaries", None)
        if accounts and callable(fetch_inventory) and callable(summarize_inventory):
            try:
                inventory = fetch_inventory()
                summaries = summarize_inventory(
                    inventory,
                    accounts=accounts,
                    state=state_snapshot,
                    bindings=bindings_snapshot,
                )
                if isinstance(summaries, dict):
                    for value in accounts:
                        summary = summaries.get(value)
                        if not isinstance(summary, dict):
                            continue
                        normalized: dict[str, int] = {}
                        for field in ("total", "bound", "unbound", "available", "quarantined"):
                            try:
                                count = int(summary.get(field))
                            except (TypeError, ValueError):
                                continue
                            if count >= 0:
                                normalized[field] = count
                        if normalized:
                            usage_metadata[value] = normalized
                    local_usage_loaded = all(value in usage_metadata for value in accounts)
            except Exception:
                # Local inventory is supplemental. Keep the account list and
                # quota counts available if an older adapter is still running.
                usage_metadata = {}

        def read_usage(value: str) -> tuple[str, dict[str, int]]:
            # Use one provider/session per worker. requests.Session is not
            # shared across threads, and one slow account must not serialize
            # all other alias-count requests.
            worker = None
            try:
                worker = _provider_for(cfg)
                try:
                    summary = worker.alias_usage_summary(
                        value,
                        state=state_snapshot,
                        bindings=bindings_snapshot,
                    )
                except TypeError as error:
                    # Keep compatibility with lightweight provider fixtures
                    # and older adapters that do not accept snapshots.
                    if "unexpected keyword" not in str(error):
                        raise
                    summary = worker.alias_usage_summary(value)
                if not isinstance(summary, dict):
                    return value, {}
                normalized: dict[str, int] = {}
                for field in ("total", "bound", "unbound", "available"):
                    try:
                        count = int(summary.get(field))
                    except (TypeError, ValueError):
                        continue
                    if count >= 0:
                        normalized[field] = count
                if "unbound" in normalized and "available" in normalized:
                    normalized["quarantined"] = max(0, normalized["unbound"] - normalized["available"])
                return value, normalized
            except Exception:
                # The remote alias list is supplemental. A temporary
                # failure must not hide the saved account or quota count.
                return value, {}
            finally:
                if worker is not None:
                    worker.close()

        if accounts and include_usage and not local_usage_loaded:
            worker_count = min(settings_concurrency, len(accounts))
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="mailcom-settings",
            ) as pool:
                for value, summary in pool.map(read_usage, accounts):
                    if summary:
                        usage_metadata[value] = summary
        selected = str(account_email or "").strip().lower() or (accounts[0] if accounts else "")
        api_base = provider.api_base
        key_configured = bool(provider.admin_key)
        alias_domain = str(provider.alias_domain or "random")
        alias_domains = list(getattr(provider, "alias_domains", []) or [])
        if not alias_domains and alias_domain != "random":
            alias_domains = [alias_domain]
    finally:
        provider.close()
    account_rows: list[dict[str, Any]] = []
    for value in accounts:
        remote = metadata.get(value, {})
        usage = usage_metadata.get(value, {})
        remote_count = remote.get("aliasCount")
        if not isinstance(remote_count, (int, float)) or isinstance(remote_count, bool):
            remote_count = usage.get("total")
        remote_limit = remote.get("aliasLimit")
        if not isinstance(remote_limit, (int, float)) or isinstance(remote_limit, bool):
            remote_limit = MAILCOM_ALIAS_LIMIT
        account_rows.append({
            "email": value,
            "alias_count": int(remote_count) if isinstance(remote_count, (int, float)) else None,
            "alias_limit": int(remote_limit),
            "bound_alias_count": usage.get("bound"),
            "unbound_alias_count": usage.get("unbound"),
            "available_alias_count": usage.get("available"),
            "quarantined_alias_count": usage.get("quarantined"),
            "proxy": str(remote.get("proxy") or ""),
            "proxy_assigned": remote.get("proxyAssigned") is True,
            "proxy_active": remote.get("proxyActive") is True,
        })
    result: dict[str, Any] = {
        "ok": True,
        "configured": bool(api_base and key_configured),
        "api_base": api_base,
        "admin_key_configured": key_configured,
        "accounts": account_rows,
        "alias_limit": MAILCOM_ALIAS_LIMIT,
        "account_email": selected,
        "alias_domain": alias_domain,
        "alias_domains": alias_domains,
        "alias_name_mode": str(getattr(provider, "alias_name_mode", "random") or "random"),
        "alias_name_length": int(getattr(provider, "alias_name_length", 17) or 17),
        "import_concurrency": _normal_import_concurrency(
            entry.get("import_concurrency"), MAILCOM_IMPORT_CONCURRENCY_DEFAULT
        ),
        "settings_concurrency": settings_concurrency,
        "proxy_pool_enabled": proxy_pool.get("enabled") is True,
        "proxy_pool_count": max(0, int(proxy_pool.get("count") or 0)),
        "proxy_pool_assigned": max(0, int(proxy_pool.get("assigned") or 0)),
        "proxy_pool_generation": max(0, int(proxy_pool.get("generation") or 0)),
        "proxy_pool_items": [str(item) for item in (proxy_pool.get("items") or []) if isinstance(item, str)],
        "domains": list(domains or []),
        # The bulk local inventory provides all counters without a mail.com
        # login, including on the fast include_usage=0 settings request. Only
        # older adapters fall back to per-account reads when usage is requested.
        "usage_checked": bool(local_usage_loaded or include_usage),
        "usage_source": "local_inventory" if local_usage_loaded else ("per_account" if include_usage else ""),
    }
    if login_ok is not None:
        result["login_ok"] = bool(login_ok)
    if error:
        result["error"] = _safe_error(error)
    return result


def _provider_for(cfg: dict[str, Any]) -> Any:
    entry = _provider_entry(cfg)
    conf = _state()._make_config(
        cfg.get("mail") if isinstance(cfg.get("mail"), dict) else {}
    )
    conf["request_timeout"] = max(10.0, min(float(conf.get("request_timeout") or 30), 30.0))
    return _state().MailcomApiProvider(entry, conf)


def _configured_accounts(entry: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(
        value.strip().lower()
        for value in _state().parse_fastmail_accounts(str(entry.get("accounts") or ""))
        if value.strip()
    ))


def _remove_local_mailcom_state(account_email: str, addresses: set[str] | None = None) -> int:
    target = str(account_email or "").strip().lower()
    if not target:
        return 0
    normalized_addresses = None if addresses is None else {
        str(address or "").strip().lower()
        for address in addresses
        if str(address or "").strip()
    }
    removed = 0
    with mail_state._mailbox_state_lock:
        with mail_state._with_state_file_lock():
            store = mail_state._load_state_unlocked()
            for address, item in list(store.items()):
                if not isinstance(item, dict):
                    continue
                provider = mail_state.normalize_provider_type(str(item.get("provider") or ""))
                owner = str(item.get("account") or "").strip().lower()
                if (
                    provider == "mailcom_api"
                    and owner == target
                    and (normalized_addresses is None or str(address).strip().lower() in normalized_addresses)
                ):
                    store.pop(address, None)
                    removed += 1
            if removed:
                mail_state._save_state_unlocked(store)
    return removed


def _parse_bulk_accounts(value: str) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
    """Parse one ``email---password`` record per line without echoing secrets."""
    items: list[tuple[str, str]] = []
    invalid: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(str(value or "").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(
            r"^([^\s@<>]+@[^\s@<>]+?)(?:\s*-{3,}\s*|\t+)(.+)$",
            line,
        )
        if not match:
            invalid.append({"line": line_number, "error": "格式应为：邮箱---密码"})
            continue
        try:
            email = _normal_email(match.group(1))
        except HTTPException as error:
            invalid.append({"line": line_number, "error": str(error.detail)})
            continue
        password = match.group(2).strip()
        if not password:
            invalid.append({"line": line_number, "error": "密码不能为空"})
            continue
        if len(password) > 512:
            invalid.append({"line": line_number, "error": "密码长度超过 512 个字符"})
            continue
        if email in seen:
            invalid.append({"line": line_number, "error": f"账号重复：{email}"})
            continue
        seen.add(email)
        items.append((email, password))
    return items, invalid


@router.get("/api/mailcom/settings")
async def mailcom_settings(include_usage: bool = False):
    def _do() -> dict[str, Any]:
        return _public_settings(_state()._read_config(), include_usage=bool(include_usage))

    return await _state().asyncio.to_thread(_do)


@router.put("/api/mailcom/proxy-pool")
async def mailcom_proxy_pool_save(body: MailcomProxyPoolRequest):
    def _do() -> dict[str, Any]:
        cfg = _state()._read_config()
        provider = _provider_for(cfg)
        try:
            if body.clear:
                proxies: list[str] | None = []
            elif body.text is None or not str(body.text).strip():
                proxies = None
            else:
                proxies = _parse_proxy_pool_text(body.text)
            save_proxy_pool = getattr(provider, "save_proxy_pool", None)
            if not callable(save_proxy_pool):
                raise HTTPException(502, "当前 mailcode 服务版本不支持代理池")
            save_proxy_pool(
                enabled=bool(body.enabled) and not body.clear,
                proxies=proxies,
                reassign=bool(body.reassign),
            )
            return _public_settings(cfg)
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(502, _safe_error(error)) from None
        finally:
            provider.close()

    return await _state().asyncio.to_thread(_do)


@router.get("/api/mailcom/domains")
async def mailcom_domains(account_email: str):
    email = _normal_email(account_email)

    def _do() -> dict[str, Any]:
        cfg = _state()._read_config()
        entry = _provider_entry(cfg)
        configured = _configured_accounts(entry)
        provider = _provider_for(cfg)
        try:
            if configured and email not in configured:
                raise HTTPException(400, "该 mail.com 账号未保存在当前配置中")
            domains = provider.available_domains(email)
            return {
                "ok": True,
                "account_email": email,
                "domains": domains,
                "alias_domain": str(entry.get("alias_domain") or "random").strip().lower() or "random",
            }
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(502, _safe_error(error)) from None
        finally:
            provider.close()

    return await _state().asyncio.to_thread(_do)


@router.get("/api/mailcom/aliases")
async def mailcom_aliases(email: str):
    account = _normal_email(email)

    def _do() -> dict[str, Any]:
        provider = _provider_for(_state()._read_config())
        try:
            return {"ok": True, "account_email": account, "items": provider.list_aliases(account)}
        except Exception as error:
            raise HTTPException(502, _safe_error(error)) from None
        finally:
            provider.close()

    return await _state().asyncio.to_thread(_do)


@router.delete("/api/mailcom/aliases/batch")
async def mailcom_aliases_batch_delete(body: MailcomAliasBatchDeleteRequest):
    accounts: list[str] = []
    for value in body.emails:
        account = _normal_email(value)
        if account not in accounts:
            accounts.append(account)
    if not accounts:
        raise HTTPException(400, "请至少选择一个 mail.com 主邮箱")

    def _do() -> dict[str, Any]:
        cfg = _state()._read_config()
        worker_count = min(body.concurrency, len(accounts))

        def delete_one(account: str) -> dict[str, Any]:
            # Requests sessions and adapter caches belong to one worker only.
            provider = None
            try:
                provider = _provider_for(cfg)
                deleted = provider.delete_aliases(account)
                deleted_addresses = {
                    str(address or "").strip().lower()
                    for address in deleted.get("deleted", [])
                    if str(address or "").strip()
                }
                failed = [
                    {
                        "address": str(item.get("address") or ""),
                        "error": _safe_error(item.get("error") or "删除失败"),
                    }
                    for item in (deleted.get("failed") or [])
                    if isinstance(item, dict)
                ]
                return {
                    "email": account,
                    "requested": int(deleted.get("requested") or 0),
                    "deleted": sorted(deleted_addresses),
                    "failed": failed,
                    "remaining": int(deleted.get("remaining") or 0),
                    "removed_local": _remove_local_mailcom_state(account, deleted_addresses),
                }
            except Exception as error:
                return {
                    "email": account,
                    "requested": 0,
                    "deleted": [],
                    "failed": [{"address": "", "error": _safe_error(error)}],
                    "remaining": None,
                    "removed_local": 0,
                }
            finally:
                if provider is not None:
                    provider.close()

        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="mailcom-alias-delete") as pool:
            results = list(pool.map(delete_one, accounts))

        failed_accounts = [item["email"] for item in results if item.get("failed")]
        requested_aliases = sum(int(item.get("requested") or 0) for item in results)
        deleted_aliases = sum(len(item.get("deleted") or []) for item in results)
        result = _public_settings(
            _state()._read_config(),
            account_email=accounts[0] if len(accounts) == 1 else "",
            include_usage=False,
        )
        result["alias_batch_deletion"] = {
            "requested_accounts": len(accounts),
            "concurrency": worker_count,
            "processed_accounts": len(results),
            "deleted_accounts": len(accounts) - len(failed_accounts),
            "failed_accounts": failed_accounts,
            "requested_aliases": requested_aliases,
            "deleted_aliases": deleted_aliases,
            "results": results,
        }
        return result

    return await _state().asyncio.to_thread(_do)


@router.delete("/api/mailcom/aliases")
async def mailcom_aliases_delete(email: str):
    account = _normal_email(email)

    def _do() -> dict[str, Any]:
        provider = _provider_for(_state()._read_config())
        try:
            deleted = provider.delete_aliases(account)
        except Exception as error:
            # Return a structured per-account failure instead of converting an
            # upstream mailcode/proxy error into a generic HTTP 502. The local
            # alias state stays untouched until remote deletion is confirmed.
            result = _public_settings(
                _state()._read_config(), account_email=account, include_usage=False,
            )
            result["alias_deletion"] = {
                "requested": 0,
                "deleted": [],
                "failed": [{"address": "", "error": _safe_error(error)}],
                "remaining": None,
                "removed_local": 0,
            }
            return result
        finally:
            provider.close()

        deleted_addresses = {
            str(address or "").strip().lower()
            for address in deleted.get("deleted", [])
            if str(address or "").strip()
        }
        removed_local = _remove_local_mailcom_state(account, deleted_addresses)
        result = _public_settings(
            _state()._read_config(), account_email=account, include_usage=False,
        )
        result["alias_deletion"] = {
            "requested": int(deleted.get("requested") or 0),
            "deleted": sorted(deleted_addresses),
            "failed": [
                {
                    "address": str(item.get("address") or ""),
                    "error": _safe_error(item.get("error") or "删除失败"),
                }
                for item in (deleted.get("failed") or [])
                if isinstance(item, dict)
            ],
            "remaining": int(deleted.get("remaining") or 0),
            "removed_local": removed_local,
        }
        return result

    return await _state().asyncio.to_thread(_do)


@router.delete("/api/mailcom/accounts/batch")
async def mailcom_accounts_batch_remove(body: MailcomAccountBatchRemoveRequest):
    accounts = list(dict.fromkeys(_normal_email(value) for value in body.emails))

    def _do() -> dict[str, Any]:
        cfg = _state()._read_config()
        worker_count = min(body.concurrency, len(accounts))

        def remove_one(account: str) -> dict[str, Any]:
            provider = None
            try:
                provider = _provider_for(cfg)
                provider.delete_account(account)
                return {"email": account, "ok": True, "removed_local_aliases": 0}
            except Exception as error:
                return {
                    "email": account, "ok": False,
                    "error": _safe_error(error), "removed_local_aliases": 0,
                }
            finally:
                if provider is not None:
                    provider.close()

        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="mailcom-account-remove") as pool:
            results = list(pool.map(remove_one, accounts))

        removed_accounts = [item["email"] for item in results if item["ok"]]
        if removed_accounts:
            removed = set(removed_accounts)
            # One config transaction preserves failed/unselected/newly-added accounts.
            with _state()._edit_config() as updated:
                entry = _state()._ensure_provider(updated, "mailcom_api")
                entry["accounts"] = "\n".join(
                    item for item in _configured_accounts(entry) if item not in removed
                )
            for item in results:
                if item["ok"]:
                    item["removed_local_aliases"] = _remove_local_mailcom_state(item["email"])

        result = _public_settings(_state()._read_config(), account_email="", include_usage=False)
        result["account_batch_removal"] = {
            "requested_accounts": len(accounts),
            "processed_accounts": len(results),
            "concurrency": worker_count,
            "removed_accounts": removed_accounts,
            "failed_accounts": [item["email"] for item in results if not item["ok"]],
            "removed_local_aliases": sum(item["removed_local_aliases"] for item in results),
            "remote_aliases_retained": True,
            "results": results,
        }
        return result

    return await _state().asyncio.to_thread(_do)


@router.delete("/api/mailcom/accounts")
async def mailcom_account_delete(email: str):
    account = _normal_email(email)

    def _do() -> dict[str, Any]:
        cfg = _state()._read_config()
        provider = _provider_for(cfg)
        try:
            deleted = provider.delete_account(account)
        except Exception as error:
            raise HTTPException(502, _safe_error(error)) from None
        finally:
            provider.close()

        with _state()._edit_config() as updated:
            entry = _state()._ensure_provider(updated, "mailcom_api")
            remaining = [item for item in _configured_accounts(entry) if item != account]
            entry["accounts"] = "\n".join(remaining)
        removed_local_aliases = _remove_local_mailcom_state(account)
        result = _public_settings(_state()._read_config(), account_email="", include_usage=False)
        result.update({
            "deleted_account": account,
            "removed_local_aliases": removed_local_aliases,
            "remote_aliases_retained": bool(deleted.get("remote_aliases_retained", True)),
        })
        return result

    return await _state().asyncio.to_thread(_do)


@router.put("/api/mailcom/settings")
async def mailcom_settings_save(body: MailcomSettingsRequest):
    email = _normal_email(body.email)
    alias_domain = _normal_domain(body.alias_domain)
    alias_domains = _normal_domains(body.alias_domains)
    if alias_domains is None:
        alias_domains = [alias_domain] if alias_domain != "random" else []

    def _do() -> dict[str, Any]:
        current = _state()._read_config()
        current_entry = _provider_entry(current)
        saved_accounts = _configured_accounts(current_entry)
        alias_name_mode = _normal_alias_name_mode(
            body.alias_name_mode, current_entry.get("alias_name_mode", "random")
        )
        alias_name_length = _normal_alias_name_length(
            body.alias_name_length, current_entry.get("alias_name_length", 17), mode=alias_name_mode
        )
        import_concurrency = _normal_import_concurrency(
            body.import_concurrency, current_entry.get("import_concurrency", MAILCOM_IMPORT_CONCURRENCY_DEFAULT)
        )
        settings_concurrency = _normal_settings_concurrency(
            body.settings_concurrency,
            current_entry.get("settings_concurrency", MAILCOM_SETTINGS_CONCURRENCY_DEFAULT),
        )
        if not body.password and email not in saved_accounts:
            raise HTTPException(400, "首次添加 mail.com 账号时请输入邮箱密码")

        if body.api_base is not None:
            api_base = _normal_api_base(body.api_base)
        else:
            api_base = str(
                current_entry.get("api_base")
                or os.environ.get("MAILCODE_API_URL")
                or "http://127.0.0.1:18787"
            ).strip().rstrip("/")
            api_base = _normal_api_base(api_base)
        supplied_key = str(body.admin_key or "").strip()

        # Persist connection/account metadata before verification. A failed
        # login must remain visible so the user can correct the password later.
        with _state()._edit_config() as cfg:
            entry = _state()._ensure_provider(cfg, "mailcom_api")
            entry.update(
                {
                    "type": "mailcom_api",
                    "enable": True,
                    "rebind_only": True,
                    "label": str(entry.get("label") or "mail.com 域名邮箱"),
                    "api_base": api_base,
                    "alias_domain": alias_domains[0] if len(alias_domains) == 1 else "random",
                    "alias_domains": alias_domains,
                    "alias_name_mode": alias_name_mode,
                    "alias_name_length": alias_name_length,
                    "import_concurrency": import_concurrency,
                    "settings_concurrency": settings_concurrency,
                    "admin_key_env": str(entry.get("admin_key_env") or "MAILCODE_ADMIN_KEY"),
                }
            )
            if supplied_key and not any(marker in supplied_key for marker in ("…", "•", "***")):
                entry["admin_key"] = supplied_key
            accounts = list(saved_accounts)
            if email not in accounts:
                accounts.append(email)
            entry["accounts"] = "\n".join(accounts)
            snapshot = deepcopy(cfg)

        provider = _provider_for(snapshot)
        domains: list[str] = []
        verify_requested = body.verify is True
        account_saved = True
        try:
            # Importing an account is deliberately a storage operation.  The
            # adapter keeps the credential and invalidates its session cache;
            # upstream login is performed only when the caller explicitly asks
            # for verification.
            if body.password:
                try:
                    provider.import_account(email, body.password)
                except Exception as error:
                    account_saved = False
                    saved_error = _safe_error(error)
                    result = _public_settings(
                        snapshot,
                        account_email=email,
                        include_usage=False,
                    )
                    result.update({
                        "account_saved": False,
                        "verification": "not_checked",
                        "account_error": saved_error,
                    })
                    return result
            if verify_requested:
                domains = provider.available_domains(email)
                if alias_domains and not set(domains).intersection(alias_domains):
                    raise MailcomSettingsError("所选别名域名不在该账号的可用域名中")
                result = _public_settings(
                    snapshot,
                    account_email=email,
                    domains=domains,
                    login_ok=True,
                    include_usage=False,
                )
                result.update({"account_saved": account_saved, "verification": "passed"})
                return result
            result = _public_settings(
                snapshot,
                account_email=email,
                include_usage=False,
            )
            result.update({
                "account_saved": account_saved,
                "verification": "not_checked",
                "verification_message": "邮箱已保存；未执行登录检查，换绑时会自动跳过不可用账号。",
            })
            return result
        except Exception as error:
            # A failed check belongs to this account only.  The saved account
            # list remains usable and the allocator will continue with others.
            result = _public_settings(
                snapshot,
                account_email=email,
                domains=domains,
                login_ok=False,
                error=_safe_error(error),
                include_usage=False,
            )
            result.update({
                "account_saved": account_saved,
                "verification": "failed" if verify_requested else "not_checked",
                "account_error": _safe_error(error),
            })
            return result
        finally:
            provider.close()

    return await _state().asyncio.to_thread(_do)


@router.post("/api/mailcom/accounts/import-batch")
async def mailcom_accounts_import_batch(body: MailcomBulkImportRequest):
    items, invalid = _parse_bulk_accounts(body.text)
    if not items:
        detail = "没有可导入的 mail.com 账号，请按每行‘邮箱---密码’格式填写"
        if invalid:
            detail = f"{detail}；第 {invalid[0]['line']} 行：{invalid[0]['error']}"
        raise HTTPException(400, detail)
    alias_domain = _normal_domain(body.alias_domain)
    alias_domains = _normal_domains(body.alias_domains)
    if alias_domains is None:
        alias_domains = [alias_domain] if alias_domain != "random" else []

    def _do() -> dict[str, Any]:
        current = _state()._read_config()
        current_entry = _provider_entry(current)
        saved_accounts = _configured_accounts(current_entry)
        # Existing accounts are kept in the panel allow-list but are not
        # submitted to the adapter a second time. This prevents duplicate
        # imports from overwriting credentials or proxy bindings.
        existing_accounts = set(saved_accounts)
        probe = None
        try:
            probe = _provider_for(current)
            for row in probe.fetch_accounts(force=True):
                if isinstance(row, dict):
                    try:
                        existing_accounts.add(_normal_email(row.get("email")))
                    except HTTPException:
                        continue
        except Exception:
            # A temporary adapter inventory failure must not block genuinely
            # new accounts; import workers still report their own failures.
            pass
        finally:
            if probe is not None:
                probe.close()
        new_items = [(email, password) for email, password in items if email not in existing_accounts]
        skipped_items = [
            {
                "email": email,
                "ok": True,
                "checked": False,
                "skipped": True,
                "skip_reason": "already_saved",
                "domains": [],
            }
            for email, _password in items
            if email in existing_accounts
        ]
        alias_name_mode = _normal_alias_name_mode(
            body.alias_name_mode, current_entry.get("alias_name_mode", "random")
        )
        alias_name_length = _normal_alias_name_length(
            body.alias_name_length, current_entry.get("alias_name_length", 17), mode=alias_name_mode
        )
        import_concurrency = _normal_import_concurrency(
            body.concurrency, current_entry.get("import_concurrency", MAILCOM_IMPORT_CONCURRENCY_DEFAULT)
        )
        if body.api_base is not None:
            api_base = _normal_api_base(body.api_base)
        else:
            api_base = str(
                current_entry.get("api_base")
                or os.environ.get("MAILCODE_API_URL")
                or "http://127.0.0.1:18787"
            ).strip().rstrip("/")
            api_base = _normal_api_base(api_base)
        supplied_key = str(body.admin_key or "").strip()

        # Keep the panel config as an account allow-list. Passwords go only to
        # the local mailcode adapter and never enter this snapshot or response.
        with _state()._edit_config() as cfg:
            entry = _state()._ensure_provider(cfg, "mailcom_api")
            entry.update(
                {
                    "type": "mailcom_api",
                    "enable": True,
                    "rebind_only": True,
                    "label": str(entry.get("label") or "mail.com 域名邮箱"),
                    "api_base": api_base,
                    "alias_domain": alias_domains[0] if len(alias_domains) == 1 else "random",
                    "alias_domains": alias_domains,
                    "alias_name_mode": alias_name_mode,
                    "alias_name_length": alias_name_length,
                    "import_concurrency": import_concurrency,
                    "admin_key_env": str(entry.get("admin_key_env") or "MAILCODE_ADMIN_KEY"),
                }
            )
            if supplied_key and not any(marker in supplied_key for marker in ("…", "•", "***")):
                entry["admin_key"] = supplied_key
            accounts = list(saved_accounts)
            for email, _password in items:
                if email not in accounts:
                    accounts.append(email)
            entry["accounts"] = "\n".join(accounts)
            snapshot = deepcopy(cfg)

        verify_requested = body.verify is True

        def verify(item: tuple[str, str]) -> dict[str, Any]:
            email, password = item
            provider = _provider_for(snapshot)
            try:
                provider.import_account(email, password)
                if not verify_requested:
                    return {
                        "email": email,
                        "ok": True,
                        "checked": False,
                        "domains": [],
                    }
                domains = provider.available_domains(email)
                if alias_domains and not set(domains).intersection(alias_domains):
                    raise MailcomSettingsError("所选别名域名不在该账号的可用域名中")
                return {"email": email, "ok": True, "checked": True, "domains": domains}
            except Exception as error:
                return {
                    "email": email,
                    "ok": False,
                    "checked": verify_requested,
                    "error": _safe_error(error),
                }
            finally:
                provider.close()

        worker_count = min(import_concurrency, len(new_items))
        if new_items:
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="mailcom-import") as pool:
                checked = list(pool.map(verify, new_items))
        else:
            checked = []
        successful = [row for row in checked if row.get("ok") is True]
        all_results = [*skipped_items, *checked]
        selected = successful[0] if successful else (checked[0] if checked else skipped_items[0])
        selected_domains = list(selected.get("domains") or [])
        visible_results = []
        for row in all_results:
            visible = {
                "email": str(row.get("email") or ""),
                "ok": row.get("ok") is True,
                **({"domains": list(row.get("domains") or [])} if row.get("ok") is True else {}),
                **({"error": str(row.get("error") or "")} if row.get("ok") is not True else {}),
            }
            if not verify_requested:
                visible["checked"] = False
            if row.get("skipped") is True:
                visible["skipped"] = True
                visible["skip_reason"] = "already_saved"
            visible_results.append(visible)
        bulk = {
            "requested": len(items) + len(invalid),
            "saved": len(new_items),
            "skipped": len(skipped_items),
            "verified": sum(1 for row in successful if row.get("checked") is True),
            "failed": len(new_items) - len(successful),
            "unchecked": sum(1 for row in successful if row.get("checked") is not True),
            "invalid": len(invalid),
            "concurrency": worker_count,
            "results": visible_results,
            "invalid_lines": invalid,
        }
        if verify_requested:
            bulk.pop("unchecked", None)
        return_value = _public_settings(
            snapshot,
            account_email=str(selected.get("email") or ""),
            domains=selected_domains,
            login_ok=(bool(successful) if verify_requested and new_items else None),
            error=("批量导入的账号没有登录成功" if verify_requested and new_items and not successful else ""),
            include_usage=False,
        )
        return_value.update({
            "account_saved_count": len(successful),
            "account_skipped_count": len(skipped_items),
            "verification": "passed" if verify_requested and successful else ("failed" if verify_requested and new_items else "not_checked"),
            "verification_message": (
                "账号已保存；未执行登录检查，换绑时会自动跳过不可用账号。"
                if not verify_requested else ""
            ),
        })
        return_value["bulk"] = bulk
        return return_value

    return await _state().asyncio.to_thread(_do)


class MailcomSettingsError(RuntimeError):
    """A saved setting is syntactically valid but not accepted upstream."""

"""Panel-owned external API settings and recoverable submission records."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any
from urllib.parse import urlsplit
import uuid

from config import ROOT
from common import mysql_backend
from common.mysql_backend import connection as mysql_connection, enabled_for, ensure_schema

from common.external_clients import (
    ScanClient, UpiClient, PublicUpiClient, SeashoreClient, FoargeClient, MasiClient,
    ExternalClientError, CDK_SCAN_API_BASE, FOARGE_API_BASE, MASI_API_BASE,
    SCAN_BASE, UPI_BASE, PUBLIC_UPI_BASE, SEASHORE_PUBLISHER_BASE, public_upi_result,
)

PAYMENT_PLATFORMS = {"none", "foarge", "masi", "seashore"}
UPI_SECRET_FIELDS = {"cdk", "entry_proxy", "exit_proxy", "foarge_credential", "masi_cdk_ticket", "seashore_cdk"}
# The scan section also owns the credentials used by the target project's
# direct publisher APIs.  Keep the legacy aliases (api_key/seashore_cdk) so
# existing settings and UI clients remain compatible.
SCAN_SECRET_FIELDS = {"api_key", "bearer_token", "seashore_cdk", "foarge_cdk", "masi_cdk"}
SCAN_SETTING_ALIASES = {"cdk_scan_api_key": "api_key", "publisher_cdk": "seashore_cdk"}
UPI_PROVIDERS = {"legacy", "public"}
SCAN_PROVIDERS = {"cdk_scan", "seashore", "foarge", "masi", "auto"}
EXTERNAL_TARGET_ORDER = ("seashore", "foarge", "cdk_scan", "masi")
PAYMENT_DEFAULTS = {
    "payment_platform": "none", "foarge_auth_mode": "pbk",
    "foarge_api_base": "https://foarge.com/api/publisher/v1",
    "masi_api_base": "https://masi.cc.cd", "masi_require_online_workers": True,
}
SEASHORE_BASE = SEASHORE_PUBLISHER_BASE


class _MySQLExternalDB:
    def __init__(self, db):
        self._db = db

    def execute(self, sql, params=()):
        return self._db.execute(sql.replace("?", "%s"), params)


class ExternalAPIError(RuntimeError):
    def __init__(self, message, status=400, code="INVALID_REQUEST", retry_after=None):
        super().__init__(message)
        self.message, self.status, self.code, self.retry_after = message, status, code, retry_after


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _request_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", value):
        raise ExternalAPIError("提交编号格式无效")
    return value


def _payment_link(value):
    if not isinstance(value, str):
        raise ExternalAPIError("请填写支付链接")
    value = value.strip()
    try:
        parts = urlsplit(value)
        valid = (parts.scheme in {"http", "https"} and not parts.username and not parts.password
                 and parts.port in (None, 80, 443) and not re.search(r"[\s\\\x00-\x1f]", value)
                 and len(value) <= 4096 and (
                     parts.hostname in {"pay.nicepay.co.kr", "www.pay.nicepay.co.kr"} and len(parts.path) > 1
                     or parts.hostname == "payments.stripe.com" and parts.path.startswith("/upi/instructions/")
                     and len(parts.path) > len("/upi/instructions/")))
    except ValueError:
        valid = False
    if not valid:
        raise ExternalAPIError("仅支持 Stripe UPI instructions 或 pay.nicepay.co.kr 支付链接")
    return value


def _links(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 200:
        raise ExternalAPIError("每次提交 1–200 条支付链接")
    values = [_payment_link(value) for value in values]
    if len(set(values)) != len(values):
        raise ExternalAPIError("本次有重复链接，请去重后提交", code="DUPLICATE_LINK")
    return values


def _money(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result > 1000000 or result.as_tuple().exponent < -4:
            raise ValueError
    except (InvalidOperation, ValueError, TypeError):
        raise ExternalAPIError("单价格式错误，请刷新平台单价", code="INVALID_PRICE") from None
    return result


def _scrub(value, secrets=()):
    text = str(value or "")[:4000]
    for secret in sorted((s for s in secrets if isinstance(s, str) and s), key=len, reverse=True):
        text = text.replace(secret, "[已隐藏]")
    text = re.sub(r"eyJ[A-Za-z0-9_.-]{50,}", "[Token 已隐藏]", text)
    text = re.sub(r"(?i)(?:Bearer\s+)[^\s,;]+", "Bearer [已隐藏]", text)
    text = re.sub(r"(?i)((?:access[_-]?token|job[_-]?secret|api[_-]?key|(?:masi[_-]?)?cdk(?:[_-]?ticket)?|foarge[_-]?credential|seashore[_-]?cdk|password)[\s\"':=]+)[^\s,;\"']+", r"\1[已隐藏]", text)
    text = re.sub(r"://[^\s/@]+:[^\s/@]+@", "://[已隐藏]@", text)
    return text


def _parse_sessions(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 1_000_000:
        raise ExternalAPIError("请粘贴 Token 或 Session JSON，每次最多 100 个")
    text = text.strip()
    try:
        if text[0] in "[{":
            try:
                raw = json.loads(text)
                values = raw if isinstance(raw, list) else [raw]
            except json.JSONDecodeError:
                values = [json.loads(line) if line.strip().startswith("{") else line.strip()
                          for line in text.splitlines() if line.strip()]
        else:
            values = [line.strip() for line in text.splitlines() if line.strip()]
    except (json.JSONDecodeError, ValueError):
        raise ExternalAPIError("Session JSON 格式错误，请检查后重试") from None
    if not 1 <= len(values) <= 100:
        raise ExternalAPIError("每次提链支持 1–100 个账号")
    unique, seen = [], set()
    for value in values:
        if isinstance(value, dict):
            token = value.get("accessToken") or value.get("access_token")
            if not isinstance(token, str) or not token.strip():
                raise ExternalAPIError("Session JSON 需包含 accessToken 或 access_token")
        elif isinstance(value, str) and value.strip() and not re.search(r"\s", value):
            token = value
        else:
            raise ExternalAPIError("每行应为一个 Token 或完整 Session JSON")
        marker = _hash(token.strip())
        if marker not in seen:
            seen.add(marker)
            unique.append(value)
    return unique


def _api_base(value):
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (not isinstance(value, str) or len(value) > 2048 or parsed.scheme != "https"
                or not host or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.port not in (None, 443)
                or re.search(r"[\s\\\x00-\x1f]", value)
                or host == "localhost" or host.endswith((".localhost", ".local", ".internal"))):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host or not re.fullmatch(r"[a-z0-9.-]+", host) or re.fullmatch(r"[0-9.]+", host):
                raise ValueError
        else:
            if not address.is_global:
                raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise ExternalAPIError("支付平台 API Base 应为有效的公网 HTTPS 地址") from None
    return value.rstrip("/")


def _pipeline_record(record):
    from common.account_store import account_is_deactivated
    token = str(record.get("access_token") or "").strip()
    if not token and isinstance(record.get("session"), dict):
        token = str(record["session"].get("accessToken") or record["session"].get("access_token") or "").strip()
    return {"email": str(record.get("email") or ""), "token": token,
            "issue": "deactivated" if account_is_deactivated(record) else "" if token else "no_token"}


def _pipeline_group(group_id):
    import web_app
    from common.account_groups import list_groups
    from common.account_store import load_accounts, account_delete_key
    groups = list_groups(web_app.GROUPS_FILE)
    selected = next((group for group in groups if group["id"] == group_id), None)
    if selected is None:
        raise ExternalAPIError("账号分组不存在", 404)
    default_id = groups[0]["id"]
    known_groups = {group["id"] for group in groups}
    accounts = []
    for record in load_accounts(web_app.ACCOUNTS_FILE):
        assigned = str(record.get("group_id") or "")
        if (assigned if assigned in known_groups else default_id) != group_id:
            continue
        accounts.append({"key": account_delete_key(record), **_pipeline_record(record)})
    return {"id": group_id, "name": selected["name"], "accounts": accounts}


def _pipeline_tokens(keys):
    import web_app
    from common.account_store import load_account_records_by_keys
    records = load_account_records_by_keys(keys, path=web_app.ACCOUNTS_FILE)
    return {key: _pipeline_record(record) for key, record in records.items()}


class ExternalAPIService:
    def __init__(self, path, *, scan_factory=ScanClient, upi_factory=UpiClient,
                 public_upi_factory=PublicUpiClient, seashore_factory=SeashoreClient,
                 foarge_factory=FoargeClient, masi_factory=MasiClient,
                 executor=None, clock=None):
        self.path = Path(path)
        self._mysql = enabled_for(self.path, Path(ROOT) / "data/external_api.sqlite3")
        self.clock = clock or time.time
        self.scan_factory, self.upi_factory = scan_factory, upi_factory
        self.public_upi_factory, self.seashore_factory = public_upi_factory, seashore_factory
        self.foarge_factory, self.masi_factory = foarge_factory, masi_factory
        self.executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="external-upi")
        self._locks_guard = threading.Lock()
        self._refresh_locks = {}
        self._inflight_jobs: set[str] = set()
        self._pipeline_manager = None
        self._init()

    def _init(self):
        if self._mysql:
            ensure_schema("external_api", [
                "CREATE TABLE IF NOT EXISTS settings (name VARCHAR(64) PRIMARY KEY, payload LONGTEXT NOT NULL)",
                """CREATE TABLE IF NOT EXISTS scan_submissions (
                    id VARCHAR(80) COLLATE utf8mb4_bin PRIMARY KEY, fingerprint VARCHAR(64) NOT NULL,
                    key_scope VARCHAR(64) NOT NULL, links LONGTEXT NOT NULL,
                    unit_price VARCHAR(32) NOT NULL, status VARCHAR(32) NOT NULL,
                    result LONGTEXT NOT NULL DEFAULT ('{}'), error LONGTEXT NOT NULL DEFAULT (''),
                    error_code VARCHAR(128) NOT NULL DEFAULT '', retry_after INT NOT NULL DEFAULT 0,
                    created_at DOUBLE NOT NULL, updated_at DOUBLE NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS upi_batches (
                    id VARCHAR(80) COLLATE utf8mb4_bin PRIMARY KEY, fingerprint VARCHAR(64) NOT NULL,
                    provider VARCHAR(32) NOT NULL DEFAULT 'legacy', created_at DOUBLE NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS upi_jobs (
                    id VARCHAR(64) COLLATE utf8mb4_bin PRIMARY KEY,
                    batch_id VARCHAR(80) COLLATE utf8mb4_bin NOT NULL,
                    position INT NOT NULL, status VARCHAR(32) NOT NULL,
                    job_id VARCHAR(255) NOT NULL DEFAULT '', job_secret LONGTEXT NOT NULL DEFAULT (''),
                    provider VARCHAR(32) NOT NULL DEFAULT 'legacy', session_cookie LONGTEXT NOT NULL DEFAULT (''),
                    queue_position INT NOT NULL DEFAULT 0, result LONGTEXT NOT NULL DEFAULT ('{}'),
                    error LONGTEXT NOT NULL DEFAULT (''), cancel_requested TINYINT NOT NULL DEFAULT 0,
                    last_polled DOUBLE NOT NULL DEFAULT 0, created_at DOUBLE NOT NULL,
                    updated_at DOUBLE NOT NULL, payment_platform VARCHAR(32) NOT NULL DEFAULT 'none',
                    INDEX upi_jobs_created (created_at DESC, position),
                    INDEX upi_jobs_batch_position (batch_id, position))""",
            ])
            with mysql_connection("external_api") as db:
                db.execute("INSERT IGNORE INTO settings(name,payload) VALUES(%s,%s),(%s,%s)",
                           ("scan", "{}", "upi", "{}"))
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings(name TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS scan_submissions(
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, key_scope TEXT NOT NULL,
                    links TEXT NOT NULL, unit_price TEXT NOT NULL, status TEXT NOT NULL,
                    result TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '', retry_after INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS upi_batches(
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT 'legacy', created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS upi_jobs(
                    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, position INTEGER NOT NULL,
                    status TEXT NOT NULL, job_id TEXT NOT NULL DEFAULT '', job_secret TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT 'legacy', session_cookie TEXT NOT NULL DEFAULT '',
                    queue_position INTEGER NOT NULL DEFAULT 0, result TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '', cancel_requested INTEGER NOT NULL DEFAULT 0,
                    last_polled REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS upi_jobs_created ON upi_jobs(created_at DESC,position);
            """)
        with self._db(True) as db:
            batch_columns = {row["name"] for row in db.execute("PRAGMA table_info(upi_batches)")}
            if "provider" not in batch_columns:
                db.execute("ALTER TABLE upi_batches ADD COLUMN provider TEXT NOT NULL DEFAULT 'legacy'")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(upi_jobs)")}
            if "payment_platform" not in columns:
                db.execute("ALTER TABLE upi_jobs ADD COLUMN payment_platform TEXT NOT NULL DEFAULT 'none'")
            if "provider" not in columns:
                db.execute("ALTER TABLE upi_jobs ADD COLUMN provider TEXT NOT NULL DEFAULT 'legacy'")
            if "session_cookie" not in columns:
                db.execute("ALTER TABLE upi_jobs ADD COLUMN session_cookie TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def _db(self, write=False):
        if self._mysql:
            with mysql_connection("external_api") as db:
                yield _MySQLExternalDB(db)
            return
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=15000")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if db.in_transaction:
                db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @contextmanager
    def _upi_queue_db(self):
        """Serialize the MySQL queue-capacity check and inserts globally."""
        if not self._mysql:
            with self._db(True) as db:
                yield db
            return
        with mysql_backend.named_lock("upi-queue-limit", timeout=5) as acquired:
            if not acquired:
                raise ExternalAPIError("提链提交队列正在更新，请稍后重试", 429, "QUEUE_BUSY", 5)
            with self._db(True) as db:
                yield db

    def _settings(self):
        with self._db() as db:
            return {row["name"]: json.loads(row["payload"]) for row in db.execute("SELECT name,payload FROM settings")}

    def _pipeline(self):
        with self._locks_guard:
            if self._pipeline_manager is None:
                from common.external_pipeline import PipelineManager
                self._pipeline_manager = PipelineManager(self, _pipeline_group, _pipeline_tokens)
            return self._pipeline_manager

    def pipeline_preview(self, group_id, batch_size=20, account_keys=None):
        return self._pipeline().preview(group_id, batch_size, account_keys)

    def pipeline_start(self, request_id, group_id, batch_size=20, use_promo=True,
                       unit_price="", upi_provider="legacy", scan_provider="cdk_scan", account_keys=None):
        cfg = self._settings()
        upi_cfg = cfg.get("upi", {})
        scan_cfg = cfg.get("scan", {})
        if upi_provider == "public":
            if not all(upi_cfg.get(field) for field in ("entry_proxy", "exit_proxy")):
                raise ExternalAPIError("公益提链需要先保存入口代理和出口代理")
        elif not all(upi_cfg.get(field) for field in ("cdk", "entry_proxy", "exit_proxy")):
            raise ExternalAPIError("旧版 UPI 提链需要先保存 CDK、入口代理和出口代理")
        if scan_provider == "seashore":
            if not scan_cfg.get("seashore_cdk"):
                raise ExternalAPIError("Seashore 发布者 API 需要先保存发布者 CDK")
        elif scan_provider == "foarge":
            if not scan_cfg.get("foarge_cdk"):
                raise ExternalAPIError("Foarge 发布者 API 需要先保存 PBK")
        elif scan_provider == "masi":
            if not scan_cfg.get("masi_cdk"):
                raise ExternalAPIError("Masi API 需要先保存 CDK")
        elif scan_provider == "auto":
            if not any(scan_cfg.get(field) for field in ("seashore_cdk", "foarge_cdk", "api_key", "masi_cdk")):
                raise ExternalAPIError("自动切换至少需要保存一套外接 API 凭证")
        elif not scan_cfg.get("api_key"):
            raise ExternalAPIError("CDK-Scan 需要先保存 API Key")
        return self._pipeline().start(
            request_id, group_id, batch_size, use_promo, unit_price,
            upi_provider=upi_provider, scan_provider=scan_provider, account_keys=account_keys,
        )

    def pipeline_status(self, task_id=None, page=1, page_size=20, selected_only=False):
        return self._pipeline().status(task_id, page=page, page_size=page_size,
                                       selected_only=selected_only)

    def pipeline_stop(self, task_id):
        return self._pipeline().stop(task_id)

    def pipeline_resume(self, task_id):
        return self._pipeline().resume(task_id)

    def settings(self):
        saved = self._settings()
        result = {"scan": {"base_url": SCAN_BASE, "cdk_scan_api_base": CDK_SCAN_API_BASE}, "upi": {"base_url": UPI_BASE}}
        for name, fields in (("scan", SCAN_SECRET_FIELDS), ("upi", UPI_SECRET_FIELDS)):
            for field in fields:
                exists = bool(saved.get(name, {}).get(field))
                result[name]["has_" + field] = exists
                result[name][field + "_masked"] = "已保存 ·••••••••" if exists else ""
        # Public aliases used by the standalone paylink project.
        result["scan"]["has_cdk_scan_api_key"] = result["scan"].get("has_api_key", False)
        result["scan"]["cdk_scan_api_key_masked"] = result["scan"].get("api_key_masked", "")
        result["scan"]["has_publisher_cdk"] = result["scan"].get("has_seashore_cdk", False)
        result["scan"]["publisher_cdk_masked"] = result["scan"].get("seashore_cdk_masked", "")
        result["scan"]["seashore_api_base"] = SEASHORE_BASE
        result["scan"]["foarge_api_base"] = FOARGE_API_BASE
        result["scan"]["masi_api_base"] = MASI_API_BASE
        result["upi"]["public_api_base"] = PUBLIC_UPI_BASE
        upi = saved.get("upi", {})
        result["upi"].update({field: upi.get(field, default) for field, default in PAYMENT_DEFAULTS.items()})
        result["upi"]["seashore_api_base"] = SEASHORE_BASE
        return result

    def save_settings(self, updates):
        fields = {"scan": SCAN_SECRET_FIELDS, "upi": UPI_SECRET_FIELDS}
        if not isinstance(updates, dict) or set(updates) - set(fields):
            raise ExternalAPIError("接口配置字段无效")
        prepared = {}
        for name, supplied in updates.items():
            supplied = dict(supplied) if isinstance(supplied, dict) else supplied
            if name == "scan" and isinstance(supplied, dict):
                # Accept the target project's public names while persisting
                # the existing panel's canonical keys.
                for alias, canonical in SCAN_SETTING_ALIASES.items():
                    if alias in supplied and canonical not in supplied:
                        supplied[canonical] = supplied[alias]
                    if "clear_" + alias in supplied and "clear_" + canonical not in supplied:
                        supplied["clear_" + canonical] = supplied["clear_" + alias]
            allowed = fields[name] | {"clear_" + f for f in fields[name]}
            if name == "scan":
                allowed |= set(SCAN_SETTING_ALIASES) | {"clear_" + f for f in SCAN_SETTING_ALIASES}
            if name == "upi":
                allowed |= set(PAYMENT_DEFAULTS)
            if not isinstance(supplied, dict) or set(supplied) - allowed:
                raise ExternalAPIError("接口配置字段无效")
            prepared[name] = {}
            for field in fields[name]:
                raw = supplied.get(field)
                if raw is not None and not isinstance(raw, str):
                    raise ExternalAPIError("接口凭证应为文本")
                value = (raw or "").strip()
                if len(value) > 8192 or re.search(r"[\r\n\x00]", value):
                    raise ExternalAPIError("接口凭证格式错误")
                if value and field.endswith("proxy"):
                    from proxy_util import normalize_proxy_url
                    try:
                        value = normalize_proxy_url(value)
                        parsed = urlsplit(value)
                        if not parsed.hostname or not parsed.port or parsed.scheme not in {"http", "https", "socks5", "socks5h"}:
                            raise ValueError
                    except (ValueError, TypeError):
                        raise ExternalAPIError("请填写有效代理地址，支持 http://用户名:密码@主机:端口") from None
                if supplied.get("clear_" + field):
                    prepared[name][field] = ""
                elif value:
                    prepared[name][field] = value
            if name == "upi":
                for field in PAYMENT_DEFAULTS.keys() & supplied.keys():
                    value = supplied[field]
                    if field == "payment_platform" and (not isinstance(value, str) or value not in PAYMENT_PLATFORMS):
                        raise ExternalAPIError("自动支付平台只可选择 none、foarge、masi、seashore 中的一个")
                    if field == "foarge_auth_mode" and (not isinstance(value, str) or value not in {"pbk", "api_key", "cdk_session"}):
                        raise ExternalAPIError("Foarge 认证方式无效")
                    if field == "masi_require_online_workers" and not isinstance(value, bool):
                        raise ExternalAPIError("在线工作端选项应为布尔值")
                    if field.endswith("api_base"):
                        value = _api_base(value)
                    prepared[name][field] = value
        with self._db(True) as db:
            for name, fields_to_save in prepared.items():
                row = db.execute("SELECT payload FROM settings WHERE name=?" + (" FOR UPDATE" if self._mysql else ""),
                                 (name,)).fetchone()
                current = json.loads(row["payload"] if self._mysql else row[0]) if row else {}
                current.update(fields_to_save)
                if self._mysql:
                    db.execute("INSERT INTO settings(name,payload) VALUES(?,?) ON DUPLICATE KEY UPDATE payload=VALUES(payload)",
                               (name, _json(current)))
                else:
                    db.execute("INSERT OR REPLACE INTO settings(name,payload) VALUES(?,?)", (name, _json(current)))
        return self.settings()

    def _scan(self, *, public=False, balance=False):
        cfg = self._settings().get("scan", {})
        if not public and not cfg.get("bearer_token" if balance else "api_key"):
            raise ExternalAPIError("请先保存扫码平台的 Bearer Token 才能查询余额" if balance else "请先保存扫码平台 API Key")
        return self.scan_factory(api_key=cfg.get("api_key", ""), bearer_token=cfg.get("bearer_token", ""))

    def _seashore(self):
        cfg = self._settings().get("scan", {})
        cdk = str(cfg.get("seashore_cdk") or "").strip()
        if not cdk:
            raise ExternalAPIError("请先保存 Seashore 发布者 CDK")
        return self.seashore_factory(cdk=cdk)

    def _foarge(self):
        cfg = self._settings().get("scan", {})
        cdk = str(cfg.get("foarge_cdk") or "").strip()
        if not cdk:
            raise ExternalAPIError("请先保存 Foarge PBK")
        return self.foarge_factory(cdk=cdk)

    def _masi(self):
        cfg = self._settings().get("scan", {})
        cdk = str(cfg.get("masi_cdk") or "").strip()
        if not cdk:
            raise ExternalAPIError("请先保存 Masi CDK")
        return self.masi_factory(cdk=cdk)

    def _upi(self, cfg=None, *, create=False):
        cfg = cfg if cfg is not None else self._settings().get("upi", {})
        if create and not all(cfg.get(field) for field in ("cdk", "entry_proxy", "exit_proxy")):
            raise ExternalAPIError("请先保存提链 CDK、入口代理和出口代理")
        return self.upi_factory(cdk=cfg.get("cdk", ""), entry_proxy=cfg.get("entry_proxy", ""), exit_proxy=cfg.get("exit_proxy", ""))

    def _public_upi(self, cfg=None, *, create=False, session_cookie=""):
        cfg = cfg if cfg is not None else self._settings().get("upi", {})
        if create and not all(cfg.get(field) for field in ("entry_proxy", "exit_proxy")):
            raise ExternalAPIError("公益提链需要保存入口代理和出口代理")
        return self.public_upi_factory(
            checkout_proxy=cfg.get("entry_proxy", ""),
            update_proxy=cfg.get("exit_proxy", ""),
            session_cookie=session_cookie,
        )

    def _payment_options(self, cfg, platform=None):
        platform = cfg.get("payment_platform", "none") if platform is None else platform
        if not isinstance(platform, str) or platform not in PAYMENT_PLATFORMS:
            raise ExternalAPIError("自动支付平台只可选择一个")
        options = {"payment_platform": platform}
        secret_field = {"foarge": "foarge_credential", "masi": "masi_cdk_ticket", "seashore": "seashore_cdk"}.get(platform)
        if secret_field:
            if not cfg.get(secret_field):
                raise ExternalAPIError(f"请先保存 {platform} 的支付凭证")
            options[secret_field] = cfg[secret_field]
        if platform == "foarge":
            options.update(foarge_auth_mode=cfg.get("foarge_auth_mode", "pbk"),
                           foarge_api_base=cfg.get("foarge_api_base", PAYMENT_DEFAULTS["foarge_api_base"]))
        elif platform == "masi":
            options.update(masi_api_base=cfg.get("masi_api_base", PAYMENT_DEFAULTS["masi_api_base"]),
                           masi_require_online_workers=cfg.get("masi_require_online_workers", True))
        return options

    def upi_fuse(self, codes, label=""):
        if not isinstance(codes, list) or not 2 <= len(codes) <= 100:
            raise ExternalAPIError("融合需要 2–100 张 UPI CDK，第一张为保留卡")
        if not isinstance(label, str) or len(label) > 120 or re.search(r"[\x00-\x1f]", label):
            raise ExternalAPIError("融合备注最长 120 字符")
        if any(not isinstance(code, str) or not code.strip() or len(code) > 8192 or re.search(r"[\s\x00-\x1f]", code.strip()) for code in codes):
            raise ExternalAPIError("融合 CDK 格式错误")
        codes = [code.strip() for code in codes]
        if len(set(codes)) != len(codes):
            raise ExternalAPIError("融合列表含重复 CDK，请检查保留卡和源卡")
        try:
            data = self._call_client(self._upi(), "cdk_fuse", codes, label=label)
        except ExternalClientError as exc:
            if exc.uncertain:
                raise ExternalAPIError("融合结果待确认，请核对点数；重试时使用相同保留卡与原源卡组合", 502, "FUSION_UNKNOWN", exc.retry_after) from None
            raise ExternalAPIError(str(exc), 429 if exc.status == 429 else 400, exc.code, exc.retry_after) from None
        try:
            points = _money(data["transferred_points"])
            target = data["target"]
            if not isinstance(target, dict) or not isinstance(data.get("duplicate"), bool):
                raise ValueError
            public_target = {}
            for key in ("remaining_uses", "total_uses", "used_uses"):
                if key in target:
                    number = _money(target[key])
                    public_target[key] = str(number) if isinstance(target[key], str) else target[key]
            if isinstance(target.get("enabled"), bool):
                public_target["enabled"] = target["enabled"]
            if "remaining_uses" not in public_target:
                raise ValueError
        except (KeyError, ValueError, TypeError, ExternalAPIError):
            raise ExternalAPIError("融合响应待核对，请查询保留卡点数；请勿更换组合重复融合", 502, "FUSION_UNKNOWN") from None
        return {"ok": True, "duplicate": data["duplicate"], "transferred_points": str(points), "target": public_target}

    @staticmethod
    def _remote(action):
        try:
            return action()
        except ExternalClientError as error:
            raise ExternalAPIError(str(error), 429 if error.status == 429 else 502, error.code, error.retry_after) from None

    @staticmethod
    def _call_client(client, method, *args, **kwargs):
        try:
            return getattr(client, method)(*args, **kwargs)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def scan_config(self):
        data = self._remote(lambda: self._call_client(self._scan(public=True), "config"))
        try:
            limit = max(1, min(200, int(data["max_batch"])))
            price = str(_money(data["unit_price"]))
            available = data["available"]
            if not isinstance(available, bool):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ExternalAPIError("扫码平台配置响应异常", 502) from None
        return {"available": available, "unit_price": price, "max_batch": limit,
                "accepting_supplier_count": max(0, int(data.get("accepting_supplier_count") or 0))}

    def scan_quote(self, links):
        links = _links(links)
        cfg = self.scan_config()
        if len(links) > cfg["max_batch"]:
            raise ExternalAPIError(f"当前每批最多 {cfg['max_batch']} 条，请分批提交")
        return {**cfg, "count": len(links), "total_amount": str(_money(cfg["unit_price"]) * len(links))}

    def _scan_orders_public(self, items):
        config = self._settings()
        secret_values = [config.get("scan", {}).get(key, "") for key in ("api_key", "bearer_token")]
        secret_values.extend(config.get("upi", {}).get(key, "") for key in UPI_SECRET_FIELDS)
        allowed = {"order_no", "batch_id", "status", "unit_price", "charge_amount", "created_at", "completed_at", "claimed_at", "started_at"}
        output = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            row = {key: item[key] for key in allowed if key in item and isinstance(item[key], (str, int, float, type(None)))}
            for key in ("result", "failure_reason"):
                row[key] = _scrub(item.get(key), secret_values)
            try:
                row["link"] = _payment_link(item.get("link", ""))
            except ExternalAPIError:
                row["link"] = ""
            output.append(row)
        return output

    def scan_orders(self, page=1, page_size=20):
        page, page_size = max(1, int(page)), max(1, min(100, int(page_size)))
        value = self._remote(lambda: self._call_client(self._scan(), "orders", page=page, page_size=page_size))
        pagination = value.get("pagination") or {}
        try:
            total = max(0, int(pagination.get("total", 0)))
        except (ValueError, TypeError):
            raise ExternalAPIError("扫码订单分页响应异常", 502) from None
        return {"items": self._scan_orders_public(value.get("items")), "pagination": {
            "page": page, "page_size": page_size, "total": total, "total_pages": (total + page_size - 1) // page_size}}

    def scan_balance(self):
        value = self._remote(lambda: self._call_client(self._scan(balance=True), "balance"))
        return {key: str(_money(value[key])) for key in ("balance", "frozen_balance", "unit_price") if key in value}

    def _scan_public(self, row):
        return {"id": row["id"], "status": row["status"], "retryable": row["status"] == "unknown",
                "error": row["error"], "error_code": row["error_code"], "retry_after": row["retry_after"],
                "created_at": row["created_at"], "updated_at": row["updated_at"], "data": json.loads(row["result"])}

    def scan_submissions(self, page=1, page_size=20):
        page, page_size = max(1, int(page)), max(1, min(100, int(page_size)))
        with self._db(True) as db:
            db.execute("UPDATE scan_submissions SET status='unknown',error='提交结果待确认，请使用原提交编号重试' WHERE status='submitting' AND updated_at<?", (self.clock() - 120,))
            total_row = db.execute("SELECT COUNT(*) AS total FROM scan_submissions").fetchone()
            total = total_row["total"] if self._mysql else total_row[0]
            rows = db.execute("SELECT * FROM scan_submissions ORDER BY created_at DESC LIMIT ? OFFSET ?", (page_size, (page - 1) * page_size)).fetchall()
        return {"items": [self._scan_public(row) for row in rows], "total": total, "page": page, "page_size": page_size}

    def scan_submit(self, request_id, links, unit_price, access_tokens=None):
        request_id, links = _request_id(request_id), _links(links)
        if not str(unit_price or "").strip():
            unit_price = self.scan_config().get("unit_price", "")
        price = format(_money(unit_price).normalize(), "f")
        if access_tokens is not None:
            if (not isinstance(access_tokens, (list, tuple)) or len(access_tokens) != len(links)
                    or any(not isinstance(token, str) or not token.strip() or any(char.isspace() for char in token)
                           for token in access_tokens)):
                raise ExternalAPIError("每条支付链接必须对应一个有效 AT")
            access_tokens = [token.strip() for token in access_tokens]
        cfg = self._settings().get("scan", {})
        if not cfg.get("api_key"):
            raise ExternalAPIError("请先保存扫码平台 API Key")
        scope, fingerprint = _hash(cfg.get("api_key")), _hash({"links": links, "unit_price": price})
        with self._db() as db:
            row = db.execute("SELECT * FROM scan_submissions WHERE id=?", (request_id,)).fetchone()
        if row:
            if row["fingerprint"] != fingerprint or row["key_scope"] != scope:
                raise ExternalAPIError("此提交编号已用于其他内容或 API Key，请重新确认后提交", 409)
            return {"ok": True, "submission": self._scan_public(row)}
        quote = self.scan_quote(links)
        if not quote["available"]:
            raise ExternalAPIError("扫码平台当前无人接单，请稍后重试", 409, "NO_ACCEPTING_SUPPLIERS")
        if _money(quote["unit_price"]) != _money(price):
            raise ExternalAPIError("扫码单价已变化，请重新预览并确认费用", 409, "PRICE_CHANGED")
        with self._db(True) as db:
            now = self.clock()
            insert = """INSERT OR IGNORE INTO scan_submissions
                (id,fingerprint,key_scope,links,unit_price,status,created_at,updated_at) VALUES(?,?,?,?,?,'submitting',?,?)"""
            if self._mysql:
                insert = insert.replace("INSERT OR IGNORE", "INSERT") + " ON DUPLICATE KEY UPDATE id=id"
            inserted = db.execute(insert,
                (request_id, fingerprint, scope, _json(links), price, now, now)).rowcount
            row = db.execute("SELECT * FROM scan_submissions WHERE id=?", (request_id,)).fetchone()
        if not inserted:
            if row["fingerprint"] != fingerprint or row["key_scope"] != scope:
                raise ExternalAPIError("提交编号冲突，请重新确认", 409)
            return {"ok": True, "submission": self._scan_public(row)}
        return self._scan_send(
            row,
            self.scan_factory(api_key=cfg["api_key"], bearer_token=cfg.get("bearer_token", "")),
            access_tokens=access_tokens,
        )

    def _scan_send(self, row, client, *, access_tokens=None):
        result, error, code, retry_after = {}, "", "", 0
        try:
            submit_kwargs = {"access_tokens": access_tokens} if access_tokens is not None else {}
            data = self._call_client(client, "submit", json.loads(row["links"]), row["id"], **submit_kwargs)
            for key in ("batch_no", "order_count", "total_amount", "balance", "frozen_balance", "idempotent"):
                if key in data and isinstance(data[key], (str, int, float, bool)):
                    result[key] = data[key]
            result["orders"] = self._scan_orders_public(data.get("orders"))
            status = "submitted"
        except ExternalClientError as exc:
            status = "unknown" if exc.uncertain or exc.status == 429 else "failed"
            error, code, retry_after = str(exc), exc.code, max(0, int(exc.retry_after or 0))
        except Exception:
            status, error = "unknown", "提交结果待确认，请使用原提交编号重试"
        with self._db(True) as db:
            db.execute("UPDATE scan_submissions SET status=?,result=?,error=?,error_code=?,retry_after=?,updated_at=? WHERE id=?",
                       (status, _json(result), error, code, retry_after, self.clock(), row["id"]))
            saved = db.execute("SELECT * FROM scan_submissions WHERE id=?", (row["id"],)).fetchone()
        return {"ok": True, "submission": self._scan_public(saved)}

    def scan_retry(self, request_id):
        cfg = self._settings().get("scan", {})
        if not cfg.get("api_key"):
            raise ExternalAPIError("请先保存扫码平台 API Key")
        with self._db(True) as db:
            row = db.execute("SELECT * FROM scan_submissions WHERE id=?" + (" FOR UPDATE" if self._mysql else ""),
                             (_request_id(request_id),)).fetchone()
            if not row:
                raise ExternalAPIError("未找到此提交记录", 404)
            if row["key_scope"] != _hash(cfg["api_key"]):
                raise ExternalAPIError("扫码 API Key 已变更，请先恢复原 Key 再核对订单", 409)
            if row["status"] == "submitting" and row["updated_at"] < self.clock() - 120:
                db.execute("UPDATE scan_submissions SET status='unknown' WHERE id=?", (request_id,))
                row = db.execute("SELECT * FROM scan_submissions WHERE id=?", (request_id,)).fetchone()
            if row["status"] != "unknown":
                return {"ok": True, "submission": self._scan_public(row)}
            delay = row["retry_after"] - int(self.clock() - row["updated_at"])
            if delay > 0:
                raise ExternalAPIError(f"平台限流，请 {delay} 秒后再试", 429, "RATE_LIMITED", delay)
            db.execute("UPDATE scan_submissions SET status='submitting',updated_at=? WHERE id=?", (self.clock(), request_id))
        # Identical persisted links and Idempotency-Key: never create a new intent here.
        return self._scan_send(row, self.scan_factory(api_key=cfg["api_key"], bearer_token=cfg.get("bearer_token", "")))

    @staticmethod
    def _seashore_task_public(payload):
        task = payload.get("task") if isinstance(payload, dict) and isinstance(payload.get("task"), dict) else payload
        if not isinstance(task, dict):
            raise ExternalAPIError("Seashore 任务回执异常", 502)
        allowed = {"public_id", "email", "status", "status_text", "can_cancel", "created_at", "resolved_at", "pay_link"}
        result = {key: task[key] for key in allowed if key in task and isinstance(task[key], (str, int, float, bool, type(None)))}
        if not isinstance(result.get("public_id"), str) or not result["public_id"]:
            raise ExternalAPIError("Seashore 任务回执缺少编号", 502)
        return result

    def seashore_me(self):
        data = self._remote(lambda: self._call_client(self._seashore(), "me"))
        allowed = {"remaining_uses", "uses_total", "uses_used", "uses_held", "uses_consumed", "success_count", "pending_orders"}
        return {key: data[key] for key in allowed if key in data and isinstance(data[key], (int, float, str, bool, type(None)))}

    def seashore_tasks(self):
        data = self._remote(lambda: self._call_client(self._seashore(), "tasks"))
        return {"tasks": [self._seashore_task_public(item) for item in data.get("tasks", []) if isinstance(item, dict)]}

    def seashore_submit(self, email, access_token, pay_link):
        client = self._seashore()
        try:
            try:
                data = client.create(email, access_token, pay_link)
            except ExternalClientError as exc:
                if exc.uncertain:
                    try:
                        recovered = client.find(email, pay_link)
                    except ExternalClientError:
                        recovered = None
                    if recovered:
                        return self._seashore_task_public(recovered)
                raise ExternalAPIError(
                    str(exc), self._publisher_error_status(exc), exc.code, exc.retry_after,
                ) from None
            return self._seashore_task_public(data)
        finally:
            try:
                client.close()
            except Exception:
                pass

    def seashore_cancel(self, public_id):
        data = self._remote(lambda: self._call_client(self._seashore(), "cancel", public_id))
        return {"ok": data.get("ok") is True}

    @staticmethod
    def _publisher_task_public(payload, *, target: str):
        task = payload.get("task") if isinstance(payload, dict) and isinstance(payload.get("task"), dict) else payload
        if not isinstance(task, dict):
            raise ExternalAPIError(f"{target} 任务回执异常", 502)
        allowed = {
            "id", "task_id", "public_id", "order_id", "status", "status_text",
            "email", "account_email", "created_at", "resolved_at", "can_cancel",
        }
        result = {
            key: task[key] for key in allowed
            if key in task and isinstance(task[key], (str, int, float, bool, type(None)))
        }
        if not any(result.get(key) for key in ("id", "task_id", "public_id", "order_id")):
            raise ExternalAPIError(f"{target} 任务回执缺少编号", 502)
        return result

    @staticmethod
    def _publisher_error_status(error: ExternalClientError) -> int:
        """Map definite upstream rejections for the auto fallback chain."""
        if error.status == 429:
            return 429
        if error.code in {"UNAUTHORIZED", "INVALID_API_KEY", "INVALID_CREDENTIALS",
                          "INVALID_CDK", "CDK_EXPIRED", "CDK_EXHAUSTED"}:
            return 401
        if error.code in {"FORBIDDEN", "USER_DISABLED"}:
            return 403
        if error.code in {"INSUFFICIENT_BALANCE", "NO_ACCEPTING_SUPPLIERS",
                          "BALANCE_INCONSISTENT", "DUPLICATE_LINK", "INVALID_ORDER_STATUS"}:
            return 409
        if error.status in {400, 401, 403, 409}:
            return error.status
        return 502

    def foarge_me(self):
        data = self._remote(lambda: self._call_client(self._foarge(), "me"))
        return self._public_external_account(data, target="foarge")

    def cdk_scan_check(self):
        data = self._remote(lambda: self._call_client(self._scan(), "orders", page=1, page_size=1))
        pagination = data.get("pagination") if isinstance(data, dict) else {}
        if not isinstance(pagination, dict):
            pagination = {}
        try:
            total = max(0, int(pagination.get("total") or 0))
        except (TypeError, ValueError):
            raise ExternalAPIError("CDK-Scan 订单响应异常", 502) from None
        return {"total_orders": total}

    def masi_check(self):
        data = self._remote(lambda: self._call_client(self._masi(), "check"))
        return self._public_external_account(data, target="masi")

    @staticmethod
    def _public_external_account(data, *, target: str):
        if not isinstance(data, dict):
            raise ExternalAPIError(f"{target} 状态响应异常", 502)
        # Keep only scalar quota/status fields and a bounded nested ticket/task
        # summary.  Access tokens and credentials are filtered again by the
        # route guard, but are never intentionally projected here.
        allowed = {
            "remaining_uses", "uses_remaining", "uses_total", "uses_used", "uses_held",
            "uses_reserved", "uses_consumed", "available_uses", "pending_uses", "total_uses",
            "used_uses", "balance", "available",
            "status", "message", "success_count", "pending_orders", "total_orders",
            "enabled", "code",
        }
        result = {key: data[key] for key in allowed if key in data and isinstance(data[key], (str, int, float, bool, type(None)))}
        for key in ("ticket", "account", "data", "cdk"):
            nested = data.get(key)
            if isinstance(nested, dict):
                projected = {name: nested[name] for name in allowed if name in nested and isinstance(nested[name], (str, int, float, bool, type(None)))}
                # The route-level public scrubber removes nested credential
                # containers such as ``cdk`` and ``ticket``.  Quota/status
                # scalars are public metadata, so flatten only the allowlisted
                # values to keep them visible without exposing credentials.
                for name, value in projected.items():
                    result.setdefault(name, value)
        return result

    def foarge_submit(self, email, access_token, pay_link):
        try:
            data = self._call_client(self._foarge(), "submit", email, access_token, pay_link)
        except ExternalClientError as exc:
            status = self._publisher_error_status(exc)
            code = "EXTERNAL_SUBMIT_UNKNOWN" if exc.uncertain else exc.code
            raise ExternalAPIError(str(exc), status, code, exc.retry_after) from None
        task = self._publisher_task_public(data, target="Foarge")
        return {"ok": True, "target": "foarge", "name": "Foarge", "status": str(task.get("status") or "pending"),
                "status_text": str(task.get("status_text") or "已提交"),
                "order_id": str(task.get("id") or task.get("task_id") or task.get("public_id") or task.get("order_id") or "")}

    def masi_submit(self, email, access_token, pay_link):
        try:
            data = self._call_client(self._masi(), "submit", email, access_token, pay_link)
        except ExternalClientError as exc:
            status = self._publisher_error_status(exc)
            code = "EXTERNAL_SUBMIT_UNKNOWN" if exc.uncertain else exc.code
            raise ExternalAPIError(str(exc), status, code, exc.retry_after) from None
        order = data.get("order") if isinstance(data, dict) and isinstance(data.get("order"), dict) else {}
        return {"ok": True, "target": "masi", "name": "Masi",
                "status": str(order.get("status") or "awaiting_worker"),
                "status_text": "重复订单" if data.get("duplicate") else "已提交",
                "order_id": str(order.get("order_id") or order.get("id") or "")}

    def external_submit(self, target, *, email, access_token, pay_link, request_id="", unit_price=""):
        """Submit one extracted link to one target or the safe fallback chain."""
        target = str(target or "").strip().lower()
        if target not in set(EXTERNAL_TARGET_ORDER) | {"auto"}:
            raise ExternalAPIError("外接 API 目标无效")
        if target == "seashore":
            return {"ok": True, "target": target, "name": "Seashore",
                    **self.seashore_submit(email, access_token, pay_link)}
        if target == "foarge":
            return self.foarge_submit(email, access_token, pay_link)
        if target == "masi":
            return self.masi_submit(email, access_token, pay_link)
        if target == "cdk_scan":
            if not request_id:
                raise ExternalAPIError("CDK-Scan 提交需要稳定的提交编号")
            if not unit_price:
                unit_price = self.scan_config().get("unit_price", "")
            response = self.scan_submit(request_id, [pay_link], unit_price, access_tokens=[access_token])
            submission = response.get("submission") if isinstance(response, dict) else {}
            if not isinstance(submission, dict):
                raise ExternalAPIError("CDK-Scan 提交回执异常", 502, "INVALID_RESPONSE")
            submission_status = str(submission.get("status") or "unknown").lower()
            if submission_status == "unknown":
                raise ExternalAPIError(
                    str(submission.get("error") or "CDK-Scan 提交结果待确认"),
                    502, "EXTERNAL_SUBMIT_UNKNOWN",
                    submission.get("retry_after") if isinstance(submission.get("retry_after"), int) else None,
                )
            if submission_status != "submitted":
                error_code = str(submission.get("error_code") or "EXTERNAL_SUBMIT_FAILED")
                deterministic = {"INVALID_API_KEY", "UNAUTHORIZED", "FORBIDDEN", "INSUFFICIENT_BALANCE",
                                 "NO_ACCEPTING_SUPPLIERS", "INVALID_REQUEST", "INVALID_LINK_COUNT",
                                 "INVALID_PAYMENT_LINK", "PRICE_CHANGED"}
                status = 409 if error_code in deterministic else 502
                raise ExternalAPIError(
                    str(submission.get("error") or "CDK-Scan 提交失败"), status, error_code,
                    submission.get("retry_after") if isinstance(submission.get("retry_after"), int) else None,
                )
            data = submission.get("data") if isinstance(submission, dict) else {}
            orders = data.get("orders") if isinstance(data, dict) and isinstance(data.get("orders"), list) else []
            order = orders[0] if orders and isinstance(orders[0], dict) else {}
            return {"ok": True, "target": target, "name": "CDK-Scan",
                    "status": str(order.get("status") or submission.get("status") or "submitted"),
                    "status_text": "已提交", "order_id": str(order.get("order_no") or data.get("batch_no") or ""),
                    "submission": submission}

        settings = self._settings().get("scan", {})
        candidates = [name for name in EXTERNAL_TARGET_ORDER
                      if settings.get({"seashore": "seashore_cdk", "foarge": "foarge_cdk", "cdk_scan": "api_key", "masi": "masi_cdk"}[name])]
        attempts = []

        def quota_value(account):
            containers = [account]
            if isinstance(account, dict):
                containers.extend(account.get(key) for key in ("ticket", "account", "data"))
            for container in containers:
                if not isinstance(container, dict):
                    continue
                for key in ("remaining_uses", "available_uses", "uses_remaining", "balance"):
                    value = container.get(key)
                    if isinstance(value, bool):
                        continue
                    try:
                        parsed = int(str(value))
                    except (TypeError, ValueError):
                        continue
                    if str(value).strip().lstrip("-").isdigit():
                        return parsed
            return None

        for candidate in candidates:
            try:
                if candidate == "seashore":
                    account = self.seashore_me()
                elif candidate == "foarge":
                    account = self.foarge_me()
                elif candidate == "masi":
                    account = self.masi_check()
                else:
                    account = {}
                quota = quota_value(account)
                if quota is not None and quota <= 0:
                    attempts.append({"target": candidate, "status": "skipped", "reason": "额度已用完"})
                    continue
                result = self.external_submit(candidate, email=email, access_token=access_token, pay_link=pay_link, request_id=request_id, unit_price=unit_price)
                result["requested_target"] = "auto"
                result["attempts"] = attempts + [{"target": candidate, "status": "submitted", "reason": result.get("status_text", "已提交")}]
                return result
            except ExternalAPIError as exc:
                attempts.append({"target": candidate, "status": "failed", "reason": str(exc)})
                if exc.code == "EXTERNAL_SUBMIT_UNKNOWN" or exc.status not in {401, 403, 409, 429}:
                    return {"ok": False, "target": candidate, "requested_target": "auto",
                            "status": "unknown" if exc.code == "EXTERNAL_SUBMIT_UNKNOWN" else "error",
                            "status_text": "提交结果不确定，已停止自动切换" if exc.code == "EXTERNAL_SUBMIT_UNKNOWN" else "提交失败",
                            "code": exc.code, "error": str(exc), "attempts": attempts}
        return {"ok": False, "target": "auto", "requested_target": "auto", "status": "unavailable",
                "status_text": "没有可用的外接 API", "code": "no_external_api_available",
                "error": "所有已配置接口均无可用额度或当前不可用", "attempts": attempts}

    def upi_credits(self):
        cfg = self._settings().get("upi", {})
        if not cfg.get("cdk"):
            raise ExternalAPIError("请先保存提链 CDK")
        data = self._remote(lambda: self._call_client(self._upi(cfg), "cdk_status"))
        return {"data": {key: data[key] for key in ("enabled", "remaining_uses", "total_uses", "used_uses", "reserved_uses", "channel")
                         if key in data and isinstance(data[key], (int, float, str, bool, type(None)))}}

    def _upi_public(self, row):
        result = json.loads(row["result"])
        return {key: row[key] for key in ("id", "batch_id", "position", "status", "job_id", "queue_position", "created_at", "updated_at", "error", "payment_platform", "provider")} | {
            "retryable": False, "account_email": result.get("account_email", ""), "result": result}

    @staticmethod
    def _public_upi_result(data):
        if not isinstance(data, dict):
            return {}, ""
        result = data.get("result") if isinstance(data.get("result"), dict) else data
        link = result.get("upi_url") if isinstance(result, dict) else ""
        if not isinstance(link, str) or not link.strip() or len(link) > 4096 or any(ord(char) < 32 for char in link):
            link = ""
        else:
            link = link.strip()
        return ({"payment_link": link, "scan_link": link, "upi_url": link},
                str(result.get("error") or data.get("error") or "")[:4000] if isinstance(result, dict) else "")

    def upi_create(self, request_id, sessions_text, use_promo=True, payment_platform=None, provider="legacy"):
        request_id = _request_id(request_id)
        sessions = _parse_sessions(sessions_text)
        if provider not in UPI_PROVIDERS:
            raise ExternalAPIError("提链平台只可选择 legacy 或 public")
        cfg = self._settings().get("upi", {})
        if provider == "public":
            if not all(cfg.get(field) for field in ("entry_proxy", "exit_proxy")):
                raise ExternalAPIError("公益提链需要保存入口代理和出口代理")
            if payment_platform not in (None, "none"):
                raise ExternalAPIError("公益提链不支持旧版自动支付平台参数")
            options = {"payment_platform": "none"}
        else:
            if not all(cfg.get(field) for field in ("cdk", "entry_proxy", "exit_proxy")):
                raise ExternalAPIError("请先保存提链 CDK、入口代理和出口代理")
            options = self._payment_options(cfg, payment_platform)
        fingerprint = _hash({"provider": provider, "sessions": sessions, "use_promo": bool(use_promo), "settings": cfg, "payment": options})
        rows = []
        with self._upi_queue_db() as db:
            previous = db.execute("SELECT fingerprint FROM upi_batches WHERE id=?", (request_id,)).fetchone()
            if previous:
                if (previous["fingerprint"] if self._mysql else previous[0]) != fingerprint:
                    raise ExternalAPIError("此提交编号已用于其他内容，请勿重复提交", 409)
                rows = db.execute("SELECT * FROM upi_jobs WHERE batch_id=? ORDER BY position", (request_id,)).fetchall()
                return {"ok": True, "batch_id": request_id, "items": [self._upi_public(row) for row in rows]}
            pending_row = db.execute("SELECT COUNT(*) AS total FROM upi_jobs WHERE job_id='' AND status IN ('queued','submitting','cancelling')").fetchone()
            pending = pending_row["total"] if self._mysql else pending_row[0]
            if pending + len(sessions) > 200:
                raise ExternalAPIError("本地提链提交队列已满，请等待当前批次提交完成", 429, "QUEUE_FULL", 30)
            now = self.clock()
            batch_insert = "INSERT INTO upi_batches(id,fingerprint,provider,created_at) VALUES(?,?,?,?)"
            if self._mysql:
                batch_insert += " ON DUPLICATE KEY UPDATE id=id"
            inserted = db.execute(batch_insert, (request_id, fingerprint, provider, now)).rowcount
            if not inserted:
                previous = db.execute("SELECT fingerprint FROM upi_batches WHERE id=?" +
                                      (" FOR UPDATE" if self._mysql else ""), (request_id,)).fetchone()
                if (previous["fingerprint"] if self._mysql else previous[0]) != fingerprint:
                    raise ExternalAPIError("此提交编号已用于其他内容，请勿重复提交", 409)
                existing = db.execute("SELECT * FROM upi_jobs WHERE batch_id=? ORDER BY position", (request_id,)).fetchall()
                return {"ok": True, "batch_id": request_id, "items": [self._upi_public(row) for row in existing]}
            for position, session in enumerate(sessions, 1):
                identifier = uuid.uuid4().hex
                db.execute("INSERT INTO upi_jobs(id,batch_id,position,status,provider,created_at,updated_at,payment_platform) VALUES(?,?,?,'queued',?,?,?,?)",
                           (identifier, request_id, position, provider, now, now, options["payment_platform"]))
                rows.append((identifier, session))
        try:
            with self._locks_guard:
                self._inflight_jobs.update(identifier for identifier, _ in rows)
            if provider == "public":
                self.executor.submit(self._create_public_upi_batch, rows, cfg)
            elif len(rows) == 1:
                self.executor.submit(self._create_upi_job, rows[0][0], rows[0][1], cfg, bool(use_promo), options)
            else:
                self.executor.submit(self._create_upi_batch, rows, cfg, bool(use_promo), options)
        except Exception:
            with self._locks_guard:
                self._inflight_jobs.difference_update(identifier for identifier, _ in rows)
            for identifier, _ in rows:
                self._update_job(identifier, status="failed", error="本地任务队列提交失败，未请求提链平台")
        with self._db() as db:
            created = db.execute("SELECT * FROM upi_jobs WHERE batch_id=? ORDER BY position", (request_id,)).fetchall()
        return {"ok": True, "batch_id": request_id, "items": [self._upi_public(row) for row in created]}

    def _job(self, identifier):
        with self._db() as db:
            row = db.execute("SELECT * FROM upi_jobs WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise ExternalAPIError("提链任务不存在", 404)
        return row

    def _update_job(self, identifier, **fields):
        allowed = {"status", "job_id", "job_secret", "provider", "session_cookie", "queue_position", "result", "error", "cancel_requested", "last_polled"}
        if set(fields) - allowed:
            raise ValueError("Invalid job fields")
        fields["updated_at"] = self.clock()
        with self._db(True) as db:
            db.execute("UPDATE upi_jobs SET " + ",".join(key + "=?" for key in fields) + " WHERE id=?", [*fields.values(), identifier])
        return self._job(identifier)

    def _created_upi_job(self, identifier, response):
        job_id, secret = response.get("job_id"), response.get("job_secret")
        if (not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id)
                or not isinstance(secret, str) or not 1 <= len(secret) <= 8192 or re.search(r"[\s\x00-\x1f]", secret)):
            raise ValueError("Malformed create response")
        row = self._update_job(identifier, status="queued", job_id=job_id, job_secret=secret,
                               queue_position=max(0, int(response.get("queue_position") or 0)), error="")
        if row["cancel_requested"]:
            self.upi_cancel(identifier)

    def _created_public_upi_job(self, identifier, response, session_cookie):
        task_id = response.get("task_id") if isinstance(response, dict) else None
        upstream_status = str(response.get("status") or "queued") if isinstance(response, dict) else "queued"
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", task_id):
            raise ValueError("Malformed public UPI task response")
        if not isinstance(session_cookie, str) or not session_cookie:
            raise ValueError("Missing public UPI session cookie")
        local_status = {
            "queued": "queued", "running": "running", "cancel_requested": "cancelling",
            "succeeded": "done", "failed": "failed", "cancelled": "cancelled",
        }.get(upstream_status)
        if local_status is None:
            raise ValueError("Unexpected public UPI task status")
        result, error = self._public_upi_result(response)
        row = self._update_job(
            identifier, status=local_status, job_id=task_id, session_cookie=session_cookie,
            result=_json(result), error=error,
        )
        if row["cancel_requested"] and local_status not in {"done", "failed", "cancelled"}:
            self.upi_cancel(identifier)

    def _create_public_upi_batch(self, rows, cfg):
        active = []
        with self._db(True) as db:
            for identifier, token in rows:
                changed = db.execute(
                    "UPDATE upi_jobs SET status='submitting',updated_at=? "
                    "WHERE id=? AND status='queued' AND job_id='' AND cancel_requested=0",
                    (self.clock(), identifier),
                ).rowcount
                if changed:
                    active.append((identifier, token))
        try:
            if not active:
                return
            client = self._public_upi(cfg, create=True)
            try:
                response = client.create_batch([token for _, token in active])
                session_cookie = client.session_cookie
            finally:
                client.close()
            tasks = response.get("tasks") if isinstance(response, dict) else None
            if not isinstance(tasks, list) or len(tasks) != len(active):
                raise ValueError("Malformed public UPI batch response")
            for (identifier, _), task in zip(active, tasks):
                try:
                    self._created_public_upi_job(identifier, task, session_cookie)
                except Exception:
                    self._update_job(identifier, status="unknown", error="公益提链回执不完整，请到公益平台核对本行")
        except ExternalClientError as error:
            for identifier, _ in active:
                if not self._job(identifier)["job_id"]:
                    self._update_job(identifier, status="unknown" if error.uncertain else "failed", error=str(error))
        except Exception:
            for identifier, _ in active:
                if not self._job(identifier)["job_id"]:
                    self._update_job(identifier, status="unknown", error="公益提链提交结果待确认，请到公益平台核对；未自动重发")
        finally:
            with self._locks_guard:
                self._inflight_jobs.difference_update(identifier for identifier, _ in rows)

    def _create_upi_job(self, identifier, token, cfg, use_promo, payment_options=None):
        with self._db(True) as db:
            changed = db.execute("UPDATE upi_jobs SET status='submitting',updated_at=? WHERE id=? AND status='queued' AND job_id='' AND cancel_requested=0", (self.clock(), identifier)).rowcount
        if not changed:
            with self._locks_guard:
                self._inflight_jobs.discard(identifier)
            return
        try:
            response = self._call_client(self._upi(cfg), "create", token, use_promo=use_promo, **(payment_options or {}))
            self._created_upi_job(identifier, response)
        except ExternalClientError as exc:
            if self._job(identifier)["job_id"]:
                self._update_job(identifier, error=str(exc))
            else:
                self._update_job(identifier, status="unknown" if exc.uncertain else "failed", error=str(exc))
        except Exception:
            # The extraction API provides no idempotency guarantee. Do not retry POST.
            if self._job(identifier)["job_id"]:
                self._update_job(identifier, error="任务已创建，后续操作待核对，请刷新状态")
            else:
                self._update_job(identifier, status="unknown", error="提链提交结果待确认，请到提链平台核对后再决定是否新建")
        finally:
            with self._locks_guard:
                self._inflight_jobs.discard(identifier)

    def _create_upi_batch(self, rows, cfg, use_promo, payment_options):
        active = []
        with self._db(True) as db:
            for identifier, token in rows:
                changed = db.execute("UPDATE upi_jobs SET status='submitting',updated_at=? WHERE id=? AND status='queued' AND job_id='' AND cancel_requested=0", (self.clock(), identifier)).rowcount
                if changed:
                    active.append((identifier, token))
        try:
            if not active:
                return
            response = self._call_client(self._upi(cfg), "create_batch", [token for _, token in active],
                                         use_promo=use_promo, **payment_options)
            jobs, rejected = response.get("jobs"), response.get("rejected", [])
            if not isinstance(jobs, list) or not isinstance(rejected, list):
                raise ValueError("Malformed batch response")
            mapped, duplicates, remote_ids = {}, set(), {}
            positional = not rejected and len(jobs) == len(active) and all(isinstance(item, dict) and "row" not in item for item in jobs)
            for index, item in enumerate(jobs, 1):
                if not isinstance(item, dict):
                    continue
                row_number = index if positional else item.get("row")
                if type(row_number) is not int or not 1 <= row_number <= len(active):
                    continue
                if row_number in mapped:
                    duplicates.add(row_number)
                job_id = item.get("job_id")
                if isinstance(job_id, str) and job_id:
                    if job_id in remote_ids:
                        duplicates.update((row_number, remote_ids[job_id]))
                    remote_ids[job_id] = row_number
                mapped[row_number] = (True, item)
            for item in rejected:
                if not isinstance(item, dict):
                    continue
                row_number = item.get("row")
                if type(row_number) is not int or not 1 <= row_number <= len(active):
                    continue
                if row_number in mapped:
                    duplicates.add(row_number)
                mapped[row_number] = (False, item)
            sensitive = [cfg.get(key, "") for key in UPI_SECRET_FIELDS]
            sensitive += [token if isinstance(token, str) else str(token.get("accessToken") or token.get("access_token") or "") for _, token in active]
            for position, (identifier, _) in enumerate(active, 1):
                confirmed, item = mapped.get(position, (None, {}))
                if position in duplicates or confirmed is None:
                    self._update_job(identifier, status="unknown", error="批量提交缺少唯一对应回执，请到提链平台核对本行")
                elif confirmed:
                    try:
                        self._created_upi_job(identifier, item)
                    except Exception:
                        if not self._job(identifier)["job_id"]:
                            self._update_job(identifier, status="unknown", error="提链创建回执不完整，请到提链平台核对本行")
                        else:
                            self._update_job(identifier, error="任务已创建，后续操作待核对，请刷新状态")
                else:
                    self._update_job(identifier, status="failed", error=_scrub(item.get("error") or "提链平台拒绝此行，请检查输入与平台配置", sensitive))
        except ExternalClientError as error:
            for identifier, _ in active:
                if not self._job(identifier)["job_id"]:
                    self._update_job(identifier, status="unknown" if error.uncertain else "failed", error=str(error))
        except Exception:
            for identifier, _ in active:
                if not self._job(identifier)["job_id"]:
                    self._update_job(identifier, status="unknown", error="批量提链结果待确认，请到提链平台核对；未自动重新提交")
        finally:
            with self._locks_guard:
                self._inflight_jobs.difference_update(identifier for identifier, _ in rows)

    def _job_lock(self, identifier):
        with self._locks_guard:
            return self._refresh_locks.setdefault(identifier, threading.Lock())

    def upi_refresh(self, local_id):
        lock = self._job_lock(local_id)
        if not lock.acquire(blocking=False):
            return self._upi_public(self._job(local_id))
        try:
            row = self._job(local_id)
            if not row["job_id"] or row["status"] in {"done", "failed", "cancelled", "unknown"} or self.clock() - row["last_polled"] < 10:
                return self._upi_public(row)
            self._update_job(local_id, last_polled=self.clock())
            try:
                if row["provider"] == "public":
                    client = self._public_upi(session_cookie=row["session_cookie"])
                    try:
                        data = client.status(row["job_id"])
                    finally:
                        client.close()
                    upstream_status = str(data.get("status") or "")
                    status = {
                        "queued": "queued", "running": "running", "cancel_requested": "cancelling",
                        "succeeded": "done", "failed": "failed", "cancelled": "cancelled",
                    }.get(upstream_status, "")
                    if not status:
                        raise ValueError("Unexpected public UPI status")
                    result, public_error = self._public_upi_result(data)
                    current_cfg = self._settings().get("upi", {})
                    secret_values = [row["session_cookie"], *[current_cfg.get(key, "") for key in UPI_SECRET_FIELDS]]
                    safe_result = {key: (_scrub(value, secret_values) if isinstance(value, str) else value)
                                   for key, value in result.items()}
                    row = self._update_job(
                        local_id, status=status, result=_json(safe_result),
                        error=_scrub(public_error, secret_values), queue_position=0,
                    )
                    return self._upi_public(row)
                data = self._call_client(self._upi(), "status", row["job_id"], row["job_secret"])
                upstream_status = str(data.get("status") or "")
                status = {"error": "failed", "pending": "queued", "processing": "running"}.get(upstream_status, upstream_status)
                if status not in {"queued", "running", "done", "failed", "cancelled"}:
                    raise ValueError("Unexpected upstream status")
                raw_result = data.get("result") or {}
                result = public_upi_result(raw_result)
                result["scan_link"] = ""
                for key in ("payment_link", "provider_redirect_url", "verification_url", "short_link", "checkout_url"):
                    if result.get(key):
                        try:
                            result["scan_link"] = _payment_link(result[key])
                            break
                        except ExternalAPIError:
                            pass
                current_cfg = self._settings().get("upi", {})
                secret_values = [row["job_secret"], *[current_cfg.get(key, "") for key in UPI_SECRET_FIELDS]]
                safe_result = {key: (_scrub(value, secret_values) if isinstance(value, str) and key not in {"payment_link", "qr_code", "scan_link"} else value)
                               for key, value in result.items()}
                row = self._update_job(local_id, status=status, result=_json(safe_result),
                                       error=_scrub(data.get("error"), secret_values), queue_position=max(0, int(data.get("queue_position") or 0)))
            except ExternalClientError as exc:
                row = self._update_job(local_id, error=str(exc))
            except Exception:
                row = self._update_job(local_id, error="提链状态查询异常，稍后刷新重试")
            return self._upi_public(row)
        finally:
            lock.release()

    def upi_jobs(self, page=1, page_size=20, refresh=False):
        page, page_size = max(1, int(page)), max(1, min(100, int(page_size)))
        with self._db(True) as db:
            with self._locks_guard:
                inflight = set(self._inflight_jobs)
            stale = db.execute("SELECT id FROM upi_jobs WHERE job_id='' AND status IN ('queued','submitting','cancelling') AND updated_at<?", (self.clock() - 600,)).fetchall()
            for row in stale:
                if row["id"] not in inflight:
                    db.execute("UPDATE upi_jobs SET status='unknown',error='本地提交已中断，请到提链平台核对后再决定是否新建' WHERE id=?", (row["id"],))
            total_row = db.execute("SELECT COUNT(*) AS total FROM upi_jobs").fetchone()
            total = total_row["total"] if self._mysql else total_row[0]
            rows = db.execute("SELECT * FROM upi_jobs ORDER BY created_at DESC,position LIMIT ? OFFSET ?", (page_size, (page - 1) * page_size)).fetchall()
        if refresh:
            with ThreadPoolExecutor(max_workers=4) as executor:
                items = list(executor.map(lambda row: self.upi_refresh(row["id"]), rows))
        else:
            items = [self._upi_public(row) for row in rows]
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def upi_cancel(self, local_id):
        with self._job_lock(local_id):
            with self._db(True) as db:
                row = db.execute("SELECT * FROM upi_jobs WHERE id=?", (local_id,)).fetchone()
                if not row:
                    raise ExternalAPIError("提链任务不存在", 404)
                if row["status"] in {"done", "failed", "cancelled", "unknown"}:
                    return self._upi_public(row)
                if not row["job_id"]:
                    status = "cancelled" if row["status"] == "queued" else "cancelling"
                    db.execute("UPDATE upi_jobs SET status=?,cancel_requested=1,updated_at=? WHERE id=?", (status, self.clock(), local_id))
                    return self._upi_public(db.execute("SELECT * FROM upi_jobs WHERE id=?", (local_id,)).fetchone())
                db.execute("UPDATE upi_jobs SET cancel_requested=1 WHERE id=?", (local_id,))
            try:
                if row["provider"] == "public":
                    client = self._public_upi(session_cookie=row["session_cookie"])
                    try:
                        data = client.cancel(row["job_id"])
                    finally:
                        client.close()
                    status = "cancelled" if data.get("status") in {"cancelled", "cancel_requested"} or data.get("ok") is True else "cancelling"
                else:
                    data = self._call_client(self._upi(), "cancel", row["job_id"], row["job_secret"])
                    status = "cancelled" if data.get("status") == "cancelled" else "cancelling"
                row = self._update_job(local_id, status=status, error="", last_polled=0)
            except ExternalClientError as exc:
                row = self._update_job(local_id, error=str(exc))
            return self._upi_public(row)


_SERVICE = None
_SERVICE_LOCK = threading.Lock()


def get_external_service():
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            from config import ROOT
            _SERVICE = ExternalAPIService(Path(ROOT) / "data/external_api.sqlite3")
        return _SERVICE

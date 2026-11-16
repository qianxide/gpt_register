"""Persistent, exclusive leases for imported SMS numbers on this server."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from config import ROOT
from common import mysql_backend

MAX_USES = 3
COOLDOWN_SECONDS = 3600
_DEFAULT_DB_PATH = Path(ROOT) / "data" / "sms_number_pool.sqlite3"
_MYSQL_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS sms_numbers ("
    "id BIGINT PRIMARY KEY AUTO_INCREMENT, phone VARCHAR(32) NOT NULL UNIQUE, "
    "source_date VARCHAR(10) NOT NULL, receive_url TEXT NOT NULL, "
    "uses TINYINT NOT NULL DEFAULT 0 CHECK(uses BETWEEN 0 AND 3), cooldown_until DOUBLE NOT NULL DEFAULT 0, "
    "last_used_at DOUBLE NOT NULL DEFAULT 0, created_at DOUBLE NOT NULL, "
    "lease_id VARCHAR(32) NOT NULL DEFAULT '', lease_until DOUBLE NOT NULL DEFAULT 0, "
    "used_codes TEXT NOT NULL, deleted TINYINT NOT NULL DEFAULT 0, "
    "INDEX sms_numbers_available(deleted, uses, cooldown_until, lease_until), "
    "INDEX sms_numbers_ready(deleted, lease_id, last_used_at, id), "
    "INDEX sms_numbers_lease_expiry(lease_id, lease_until)) ENGINE=InnoDB",
    "CREATE TABLE IF NOT EXISTS sms_number_leases ("
    "lease_id VARCHAR(32) PRIMARY KEY, number_id BIGINT NOT NULL, "
    "status VARCHAR(16) NOT NULL DEFAULT 'active', created_at DOUBLE NOT NULL, "
    "finished_at DOUBLE NOT NULL DEFAULT 0) ENGINE=InnoDB",
)


class _MysqlRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


class _MysqlCursor:
    def __init__(self, cursor):
        self.cursor = cursor
        self.rowcount = cursor.rowcount

    def fetchone(self):
        row = self.cursor.fetchone()
        return _MysqlRow(row) if row is not None else None

    def fetchall(self):
        return [_MysqlRow(row) for row in self.cursor.fetchall()]


class _MysqlPoolConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=()):
        cursor = self.connection.execute(sql.replace("?", "%s"), params)
        return _MysqlCursor(cursor)

    def executemany(self, sql, params):
        return self.connection.executemany(sql.replace("?", "%s"), params)


class SmsPoolError(ValueError):
    """Public error which never includes a private receive URL."""


def parse_number_line(line: str) -> dict:
    parts = line.strip().lstrip("\ufeff").split("|", 2)
    if len(parts) != 3:
        raise SmsPoolError("格式应为 日期|号码|取码链接")
    source_date, phone, url = (p.strip() for p in parts)
    try:
        source_date = date.fromisoformat(source_date).isoformat()
    except ValueError:
        raise SmsPoolError("日期应为 YYYY-MM-DD") from None
    # These imports are US numbers, never infer +86 from eleven digits.
    phone = re.sub(r"[\s()\-]", "", phone)
    if phone.startswith("+1"):
        phone = phone[1:]
    if re.fullmatch(r"[2-9]\d{9}", phone):
        phone = "1" + phone
    if not re.fullmatch(r"1[2-9]\d{9}", phone):
        raise SmsPoolError("美国号码应为 1 开头的 11 位号码，或 10 位本地号码")
    url = html.unescape(re.sub(r"\\([_\-?&=])", r"\1", url))
    markdown = re.fullmatch(r"\[[^\]]*\]\(\s*(https://[^\s)]+)\s*\)", url)
    if markdown:
        url = markdown.group(1)
    url = url.strip("<>")
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname == "168smsapi.com"
                 and parsed.port in (None, 443) and not parsed.username and not parsed.password
                 and parsed.path.startswith("/sms/") and len(parsed.path) > 5
                 and not parsed.fragment and len(url) <= 2048
                 and not re.search(r"[\s\\\x00-\x1f]", url))
    except ValueError:
        valid = False
    if not valid:
        raise SmsPoolError("取码链接应为 https://168smsapi.com/sms/ 下的有效地址")
    return {"source_date": source_date, "phone": "+" + phone, "url": url}


class SmsNumberPool:
    def __init__(self, path: str | Path, *, clock=None):
        self.path = Path(path)
        self.clock = clock or time.time
        self._init_lock = threading.Lock()
        self._initialized = False

    @property
    def _mysql(self):
        return mysql_backend.enabled_for(self.path, _DEFAULT_DB_PATH)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        with self._init_lock:
            if not self._initialized:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS sms_numbers (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        phone TEXT NOT NULL UNIQUE,
                        source_date TEXT NOT NULL,
                        receive_url TEXT NOT NULL,
                        uses INTEGER NOT NULL DEFAULT 0 CHECK(uses BETWEEN 0 AND 3),
                        cooldown_until REAL NOT NULL DEFAULT 0,
                        last_used_at REAL NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        lease_id TEXT NOT NULL DEFAULT '',
                        lease_until REAL NOT NULL DEFAULT 0,
                        used_codes TEXT NOT NULL DEFAULT '[]',
                        deleted INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE INDEX IF NOT EXISTS sms_numbers_available
                        ON sms_numbers(deleted, uses, cooldown_until, lease_until);
                    CREATE TABLE IF NOT EXISTS sms_number_leases (
                        lease_id TEXT PRIMARY KEY,
                        number_id INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active',
                        created_at REAL NOT NULL,
                        finished_at REAL NOT NULL DEFAULT 0
                    );
                """)
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
                self._initialized = True
        return connection

    @contextmanager
    def _transaction(self):
        if self._mysql:
            mysql_backend.ensure_schema("sms_pool", _MYSQL_SCHEMA)
            with mysql_backend.connection("sms_pool") as raw:
                raw.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                connection = _MysqlPoolConnection(raw)
                now = float(self.clock())
                expired = connection.execute("""
                    SELECT lease_id FROM sms_numbers
                    WHERE lease_id<>'' AND lease_until<=? FOR UPDATE SKIP LOCKED
                """, (now,)).fetchall()
                if expired:
                    lease_ids = [row["lease_id"] for row in expired]
                    placeholders = ",".join("?" for _ in lease_ids)
                    connection.execute(
                        "UPDATE sms_number_leases SET status='expired', finished_at=? "
                        f"WHERE status='active' AND lease_id IN ({placeholders})",
                        (now, *lease_ids),
                    )
                    connection.execute(
                        f"UPDATE sms_numbers SET lease_id='', lease_until=0 "
                        f"WHERE lease_id IN ({placeholders}) AND lease_until<=?",
                        (*lease_ids, now),
                    )
                yield connection, now
            return
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = float(self.clock())
            # Expired workers never leave a number reserved indefinitely.
            connection.execute("""
                UPDATE sms_number_leases SET status='expired', finished_at=?
                WHERE status='active' AND lease_id IN (
                    SELECT lease_id FROM sms_numbers WHERE lease_id<>'' AND lease_until<=?
                )
            """, (now, now))
            connection.execute("""
                UPDATE sms_numbers SET lease_id='', lease_until=0
                WHERE lease_id<>'' AND lease_until<=?
            """, (now,))
            yield connection, now
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @staticmethod
    def _summary(connection, now):
        row = connection.execute("""
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(uses>=3),0) AS exhausted,
                   COALESCE(SUM(uses<3 AND lease_id<>''),0) AS reserved,
                   COALESCE(SUM(uses<3 AND lease_id='' AND cooldown_until>?),0) AS cooling,
                   COALESCE(SUM(uses<3 AND lease_id='' AND cooldown_until<=?),0) AS available
            FROM sms_numbers WHERE deleted=0
        """, (now, now)).fetchone()
        return {key: int(value or 0) for key, value in dict(row).items()}

    def summary(self):
        with self._transaction() as (connection, now):
            return self._summary(connection, now)

    @staticmethod
    def _public(row, now):
        status = ("exhausted" if row["uses"] >= MAX_USES else "reserved" if row["lease_id"]
                  else "cooling" if row["cooldown_until"] > now else "available")
        return {
            "id": row["id"], "phone": row["phone"], "country": "US",
            "source_date": row["source_date"], "uses": row["uses"], "max_uses": MAX_USES,
            "remaining_uses": max(0, MAX_USES - row["uses"]), "status": status,
            "cooldown_until": row["cooldown_until"],
            "cooldown_remaining": max(0, int(row["cooldown_until"] - now + 0.999)),
            "last_used_at": row["last_used_at"], "created_at": row["created_at"],
        }

    def list_numbers(self, *, page=1, page_size=20, status="all", q=""):
        conditions = {
            "all": "1=1", "exhausted": "uses>=3", "reserved": "uses<3 AND lease_id<>''",
            "cooling": "uses<3 AND lease_id='' AND cooldown_until>?",
            "available": "uses<3 AND lease_id='' AND cooldown_until<=?",
        }
        if status not in conditions:
            raise SmsPoolError("未知号码状态")
        page, page_size = max(1, int(page)), max(1, min(100, int(page_size)))
        with self._transaction() as (connection, now):
            params = [now] if status in ("cooling", "available") else []
            where = "deleted=0 AND (" + conditions[status] + ")"
            if q.strip():
                where += " AND (phone LIKE ? OR source_date LIKE ?)"
                params.extend(["%" + q.strip()[:120] + "%"] * 2)
            total = connection.execute("SELECT COUNT(*) FROM sms_numbers WHERE " + where, params).fetchone()[0]
            rows = connection.execute("SELECT * FROM sms_numbers WHERE " + where + " ORDER BY id LIMIT ? OFFSET ?",
                                      [*params, page_size, (page - 1) * page_size]).fetchall()
            return {"items": [self._public(row, now) for row in rows], "total": total,
                    "page": page, "page_size": page_size, "summary": self._summary(connection, now),
                    "max_uses": MAX_USES, "cooldown_seconds": COOLDOWN_SECONDS}

    def import_text(self, text: str):
        if not isinstance(text, str) or len(text) > 2_000_000:
            raise SmsPoolError("导入内容应为文本，且不超过 2 MB")
        lines = [(i, line) for i, line in enumerate(text.splitlines(), 1) if line.strip()]
        if not lines:
            raise SmsPoolError("请粘贴需要导入的号码")
        if len(lines) > 10000:
            raise SmsPoolError("每次最多导入 10000 行")
        parsed, errors = [], []
        for index, line in lines:
            try:
                parsed.append((index, parse_number_line(line)))
            except SmsPoolError as error:
                errors.append({"line": index, "error": str(error)})
        imported = duplicates = 0
        with self._transaction() as (connection, now):
            for index, item in parsed:
                if self._mysql:
                    inserted = connection.execute(
                        "INSERT IGNORE INTO sms_numbers(phone,source_date,receive_url,created_at,used_codes) "
                        "VALUES(?,?,?,?,?)",
                        (item["phone"], item["source_date"], item["url"], now, "[]"),
                    ).rowcount
                    if inserted:
                        imported += 1
                        continue
                row = connection.execute(
                    "SELECT * FROM sms_numbers WHERE phone=?" + (" FOR UPDATE" if self._mysql else ""),
                    (item["phone"],),
                ).fetchone()
                if row:
                    if row["lease_id"]:
                        errors.append({"line": index, "error": "号码正被任务使用，请稍后更新"})
                        continue
                    # Reimport can update the receive URL but cannot reset usage/cooldown.
                    connection.execute("UPDATE sms_numbers SET source_date=?, receive_url=?, deleted=0 WHERE id=?",
                                       (item["source_date"], item["url"], row["id"]))
                    duplicates += 1
                else:
                    connection.execute("INSERT INTO sms_numbers(phone,source_date,receive_url,created_at) VALUES(?,?,?,?)",
                                       (item["phone"], item["source_date"], item["url"], now))
                    imported += 1
            return {"imported": imported, "duplicates": duplicates, "invalid": len(errors),
                    "errors": errors[:100], "summary": self._summary(connection, now)}

    def reserve(self, *, lease_seconds=360):
        lease_seconds = max(60, min(3600, int(lease_seconds)))
        with self._transaction() as (connection, now):
            attempts = 20 if self._mysql else 1
            for attempt in range(attempts):
                row = connection.execute("""
                    SELECT * FROM sms_numbers
                """ + (" FORCE INDEX (sms_numbers_ready)" if self._mysql else "") + """
                    WHERE deleted=0 AND uses<3
                    AND lease_id='' AND cooldown_until<=? ORDER BY last_used_at, id LIMIT 1
                """ + (" FOR UPDATE SKIP LOCKED" if self._mysql else ""), (now,)).fetchone()
                if row is not None:
                    break
                stats = self._summary(connection, now)
                if not stats["available"] or attempt == attempts - 1:
                    break
                time.sleep(min(0.01 * (attempt + 1), 0.05))
            if row is None and self._mysql and stats["available"]:
                row = connection.execute("""
                    SELECT * FROM sms_numbers FORCE INDEX (sms_numbers_ready)
                    WHERE deleted=0 AND uses<3 AND lease_id='' AND cooldown_until<=?
                    ORDER BY last_used_at, id LIMIT 1 FOR UPDATE
                """, (now,)).fetchone()
            if row is None:
                stats = self._summary(connection, now)
                if stats["cooling"]:
                    until = connection.execute("SELECT MIN(cooldown_until) FROM sms_numbers WHERE deleted=0 AND uses<3 AND lease_id='' AND cooldown_until>?", (now,)).fetchone()[0]
                    seconds = max(1, int(until - now + 0.999))
                    raise SmsPoolError(f"号码池暂无可用号码；{stats['cooling']} 个冷却中，最早 {seconds} 秒后可用")
                raise SmsPoolError(f"号码池暂无可用号码（占用中 {stats['reserved']}，已用满 {stats['exhausted']}），请导入号码或稍后重试")
            lease_id = uuid.uuid4().hex
            connection.execute("UPDATE sms_numbers SET lease_id=?, lease_until=?, cooldown_until=? WHERE id=?",
                               (lease_id, now + lease_seconds, now + COOLDOWN_SECONDS, row["id"]))
            connection.execute("INSERT INTO sms_number_leases(lease_id,number_id,created_at) VALUES(?,?,?)",
                               (lease_id, row["id"], now))
            return {"lease_id": lease_id, "id": row["id"], "phone": row["phone"], "country": "US",
                    "url": row["receive_url"], "used_codes": json.loads(row["used_codes"])}

    def remember_code(self, lease_id: str, code: str):
        digest = hashlib.sha256(str(code).encode()).hexdigest()
        with self._transaction() as (connection, now):
            row = connection.execute(
                "SELECT id,used_codes FROM sms_numbers WHERE lease_id=? AND deleted=0" +
                (" FOR UPDATE" if self._mysql else ""), (lease_id,),
            ).fetchone()
            if not row:
                raise SmsPoolError("号码占用已结束，请重新获取号码")
            hashes = json.loads(row["used_codes"])
            if digest in hashes:
                return False
            hashes.append(digest)
            connection.execute("UPDATE sms_numbers SET used_codes=? WHERE id=?", (json.dumps(hashes[-64:]), row["id"]))
            return True

    def _release(self, lease_id: str, *, success: bool):
        with self._transaction() as (connection, now):
            row = connection.execute(
                "SELECT * FROM sms_numbers WHERE lease_id=? AND deleted=0" +
                (" FOR UPDATE" if self._mysql else ""), (lease_id,),
            ).fetchone()
            if not row:
                previous = connection.execute("SELECT status FROM sms_number_leases WHERE lease_id=?", (lease_id,)).fetchone()
                if success and previous and previous["status"] == "expired":
                    raise SmsPoolError("号码占用已过期，请核对本次手机验证结果")
                return False
            connection.execute("""
                UPDATE sms_numbers SET uses=uses+?, last_used_at=CASE WHEN ? THEN ? ELSE last_used_at END,
                    cooldown_until=?, lease_id='', lease_until=0 WHERE id=?
            """, (int(success), int(success), now, now + COOLDOWN_SECONDS, row["id"]))
            connection.execute("UPDATE sms_number_leases SET status=?, finished_at=? WHERE lease_id=?",
                               ("completed" if success else "cancelled", now, lease_id))
            return True

    def complete(self, lease_id: str):
        return self._release(lease_id, success=True)

    def cancel(self, lease_id: str):
        return self._release(lease_id, success=False)

    def delete(self, ids=None, *, exhausted_only=False):
        ids = list(dict.fromkeys(int(value) for value in (ids or [])))
        if len(ids) > 10000 or any(value < 1 for value in ids):
            raise SmsPoolError("请选择有效号码，每次最多删除 10000 个")
        if not ids and not exhausted_only:
            raise SmsPoolError("请先选择号码")
        with self._transaction() as (connection, now):
            where, params = "deleted=0", []
            if exhausted_only:
                where += " AND uses>=3"
            if ids:
                if self._mysql:
                    where += " AND id IN (" + ",".join("?" for _ in ids) + ")"
                    params.extend(ids)
                else:
                    # Avoid SQLite parameter limits for large selections.
                    connection.execute("CREATE TEMP TABLE selected_ids(id INTEGER PRIMARY KEY)")
                    connection.executemany("INSERT INTO selected_ids(id) VALUES(?)", ((value,) for value in ids))
                    where += " AND id IN (SELECT id FROM selected_ids)"
            skipped = connection.execute("SELECT COUNT(*) FROM sms_numbers WHERE " + where + " AND lease_id<>''", params).fetchone()[0]
            count = connection.execute("UPDATE sms_numbers SET deleted=1,receive_url='' WHERE " + where + " AND lease_id=''", params).rowcount
            return {"deleted": count, "skipped": skipped, "summary": self._summary(connection, now)}


_POOL = None
_POOL_LOCK = threading.Lock()


def get_sms_pool() -> SmsNumberPool:
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            from config import ROOT
            _POOL = SmsNumberPool(Path(ROOT) / "data" / "sms_number_pool.sqlite3")
        return _POOL

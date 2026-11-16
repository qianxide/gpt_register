"""Opt-in MySQL connection pool shared by the application's stores."""

from __future__ import annotations

import os
import queue
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import unquote, urlsplit


_POOL_LOCK = threading.Lock()
_SCHEMA_LOCK = threading.Lock()
_POOLS: dict[str, "_Pool"] = {}
_SCHEMAS: set[tuple[str, str]] = set()


def mysql_enabled() -> bool:
    return os.environ.get("GPT_REGISTER_DB_BACKEND", "sqlite").strip().lower() == "mysql"


def require_complete_migration() -> bool:
    return os.environ.get("GPT_REGISTER_MYSQL_REQUIRE_COMPLETE", "0").strip().lower() in {"1", "true", "yes"}


def enabled_for(path: str | Path, default_path: str | Path) -> bool:
    if not mysql_enabled():
        return False
    return Path(path).resolve() == Path(default_path).resolve()


def _settings() -> tuple[str, dict[str, Any], int, float]:
    raw = os.environ.get("GPT_REGISTER_MYSQL_DSN", "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"mysql", "mysql+pymysql"} or not parsed.hostname or not parsed.path.strip("/"):
        raise ValueError("GPT_REGISTER_MYSQL_DSN must be mysql://user:password@host:port/database")
    if parsed.query or parsed.fragment:
        raise ValueError("MySQL DSN query and fragment parameters are not supported")
    size = max(1, min(64, int(os.environ.get("GPT_REGISTER_MYSQL_POOL_SIZE", "16"))))
    timeout = max(0.1, float(os.environ.get("GPT_REGISTER_MYSQL_POOL_TIMEOUT", "15")))
    options = {
        "host": parsed.hostname,
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": unquote(parsed.path.lstrip("/")),
        "charset": "utf8mb4",
        "autocommit": False,
        "connect_timeout": 10,
        "read_timeout": 30,
        "write_timeout": 30,
    }
    return raw, options, size, timeout


class _Connection:
    def __init__(self, raw: Any):
        self.raw = raw

    def cursor(self) -> Any:
        return self.raw.cursor()

    def execute(self, sql: str, params: Iterable[Any] | None = None) -> Any:
        cursor = self.cursor()
        cursor.execute(sql, tuple(params or ()))
        return cursor

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> Any:
        cursor = self.cursor()
        cursor.executemany(sql, rows)
        return cursor

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()


class _Pool:
    def __init__(self, options: dict[str, Any], size: int, timeout: float):
        self.options = options
        self.timeout = timeout
        self.free: queue.LifoQueue[Any] = queue.LifoQueue(maxsize=size)
        self.slots = threading.BoundedSemaphore(size)

    def acquire(self) -> Any:
        if not self.slots.acquire(timeout=self.timeout):
            raise TimeoutError("MySQL connection pool exhausted")
        try:
            try:
                raw = self.free.get_nowait()
            except queue.Empty:
                import pymysql

                raw = pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **self.options)
            try:
                raw.ping(reconnect=False)
            except Exception:
                raw.close()
                import pymysql

                raw = pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **self.options)
            return raw
        except Exception:
            self.slots.release()
            raise

    def release(self, raw: Any, *, broken: bool = False) -> None:
        try:
            if broken:
                raw.close()
            else:
                try:
                    self.free.put_nowait(raw)
                except queue.Full:
                    raw.close()
        finally:
            self.slots.release()


def _pool() -> tuple[str, _Pool]:
    key, options, size, timeout = _settings()
    with _POOL_LOCK:
        pool = _POOLS.get(key)
        if pool is None:
            pool = _Pool(options, size, timeout)
            _POOLS[key] = pool
        return key, pool


@contextmanager
def connection(kind: str = "") -> Iterator[_Connection]:
    """Commit on success and roll back on failure, then return the connection."""
    del kind
    _, pool = _pool()
    raw = pool.acquire()
    broken = False
    try:
        wrapped = _Connection(raw)
        try:
            yield wrapped
            wrapped.commit()
        except BaseException:
            try:
                wrapped.rollback()
            except Exception:
                broken = True
            raise
    finally:
        pool.release(raw, broken=broken)


@contextmanager
def named_lock(name: str, timeout: int = 0) -> Iterator[bool]:
    """Hold a MySQL advisory lock on a dedicated pooled connection.

    The connection must stay checked out for the whole critical section;
    releasing the lock on a different pooled connection would be ineffective.
    SQLite callers do not need a process-crossing lock and receive ``True``.
    """
    if not mysql_enabled():
        yield True
        return
    _, pool = _pool()
    raw = pool.acquire()
    held = False
    broken = False
    lock_name = str(name)[:64]
    try:
        with raw.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s, %s) AS acquired", (lock_name, max(0, int(timeout))))
            row = cursor.fetchone()
            value = row.get("acquired") if isinstance(row, dict) else (row[0] if row else 0)
            held = value == 1
        yield held
    except BaseException:
        broken = True
        raise
    finally:
        if held:
            try:
                with raw.cursor() as cursor:
                    cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
            except Exception:
                broken = True
        pool.release(raw, broken=broken)


def ensure_schema(kind: str, ddl_statements: Iterable[str]) -> None:
    key, _ = _pool()
    schema_key = (key, kind)
    if schema_key in _SCHEMAS:
        return
    with _SCHEMA_LOCK:
        if schema_key in _SCHEMAS:
            return
        with connection(kind) as db:
            for statement in ddl_statements:
                db.execute(statement)
        _SCHEMAS.add(schema_key)


def assert_ready() -> None:
    """Reject a production start against a partially migrated schema."""
    if not mysql_enabled() or not require_complete_migration():
        return
    with connection("migration") as db:
        row = db.execute(
            "SELECT state, manifest_sha256 FROM codex_migration_meta "
            "WHERE migration_id = %s", (os.environ.get("GPT_REGISTER_MYSQL_MIGRATION_ID", "87-baseline"),)
        ).fetchone()
    if not row or row["state"] != "complete":
        raise RuntimeError("MySQL migration is not complete; refusing to start")

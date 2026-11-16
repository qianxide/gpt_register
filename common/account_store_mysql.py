"""MySQL 8 account-store dialect and connection adapter.

The account store's public API stays in account_store.py.  This module only
adapts its indexed SQL store calls; custom paths keep the legacy backend.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from pathlib import Path
from typing import Any, Iterable

from common import mysql_backend


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS accounts (
        canonical_key VARCHAR(640) COLLATE utf8mb4_bin NOT NULL PRIMARY KEY,
        email VARCHAR(320) COLLATE utf8mb4_bin NOT NULL DEFAULT '',
        account_id VARCHAR(320) COLLATE utf8mb4_bin NOT NULL DEFAULT '',
        group_id VARCHAR(255) COLLATE utf8mb4_bin NOT NULL DEFAULT '',
        health_status VARCHAR(64) COLLATE utf8mb4_bin NOT NULL DEFAULT '',
        registered_epoch DOUBLE NOT NULL DEFAULT 0,
        updated_epoch DOUBLE NOT NULL DEFAULT 0,
        payload LONGTEXT COLLATE utf8mb4_bin NOT NULL,
        public_payload LONGTEXT COLLATE utf8mb4_bin NOT NULL,
        is_deactivated TINYINT(1) NOT NULL DEFAULT 0,
        has_access_token TINYINT(1) NOT NULL DEFAULT 0,
        has_refresh_token TINYINT(1) NOT NULL DEFAULT 0,
        has_password TINYINT(1) NOT NULL DEFAULT 0,
        has_totp TINYINT(1) NOT NULL DEFAULT 0,
        at_expires_epoch BIGINT NOT NULL DEFAULT 0,
        trial_eligibility_status VARCHAR(64) COLLATE utf8mb4_bin NOT NULL DEFAULT '',
        trial_eligibility_reason VARCHAR(128) COLLATE utf8mb4_bin NOT NULL DEFAULT '',
        trial_eligibility_campaign_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL DEFAULT '',
        plan_type VARCHAR(64) COLLATE utf8mb4_bin NOT NULL DEFAULT 'free',
        KEY idx_accounts_email (email),
        KEY idx_accounts_account_id (account_id),
        KEY idx_accounts_group (group_id),
        KEY idx_accounts_registered_order
            (registered_epoch DESC, email(128) DESC, canonical_key(128) ASC),
        KEY idx_accounts_health (health_status),
        KEY idx_accounts_health_deactivated (health_status, is_deactivated),
        KEY idx_accounts_group_health (group_id, health_status, is_deactivated),
        KEY idx_accounts_trial_status (trial_eligibility_status),
        KEY idx_accounts_access_deactivated (has_access_token, is_deactivated),
        KEY idx_accounts_at_expiry (at_expires_epoch)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",
    """CREATE TABLE IF NOT EXISTS account_meta (
        name VARCHAR(100) COLLATE utf8mb4_bin NOT NULL PRIMARY KEY,
        value LONGTEXT COLLATE utf8mb4_bin NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin""",
)

_JSON_FIELD = re.compile(r"json_extract\((\w+(?:\.\w+)?),\s*'(\$\.\w+)'\)", re.I)
_BOOLEAN_FIELDS = frozenset({
    "is_deactivated", "has_access_token", "has_refresh_token",
    "has_password", "has_totp", "security_repair_available",
})
_NUMERIC_FIELDS = frozenset({"at_expires_epoch"})

_OPTIMIZED_COLUMNS = {
    "is_deactivated": "TINYINT(1) NOT NULL DEFAULT 0",
    "has_access_token": "TINYINT(1) NOT NULL DEFAULT 0",
    "has_refresh_token": "TINYINT(1) NOT NULL DEFAULT 0",
    "has_password": "TINYINT(1) NOT NULL DEFAULT 0",
    "has_totp": "TINYINT(1) NOT NULL DEFAULT 0",
    "at_expires_epoch": "BIGINT NOT NULL DEFAULT 0",
    "trial_eligibility_status": "VARCHAR(64) COLLATE utf8mb4_bin NOT NULL DEFAULT ''",
    "trial_eligibility_reason": "VARCHAR(128) COLLATE utf8mb4_bin NOT NULL DEFAULT ''",
    "trial_eligibility_campaign_id": "VARCHAR(128) COLLATE utf8mb4_bin NOT NULL DEFAULT ''",
    "plan_type": "VARCHAR(64) COLLATE utf8mb4_bin NOT NULL DEFAULT 'free'",
}
_OPTIMIZATION_LOCK = threading.Lock()
_OPTIMIZED_DATABASES: set[str] = set()


def enabled(path: Path, default_path: Path) -> bool:
    return mysql_backend.enabled_for(path, default_path)


def _ensure_optimized_schema() -> None:
    """Add scalar account fields to an existing MySQL deployment once.

    The JSON payload remains the source of truth for the public response. These
    columns are a synchronized query index, so old databases can be upgraded
    in place without changing account identity or stored credentials.
    """
    database_key = hashlib.sha256(
        os.environ.get("GPT_REGISTER_MYSQL_DSN", "").encode("utf-8")
    ).hexdigest()
    with _OPTIMIZATION_LOCK:
        if database_key in _OPTIMIZED_DATABASES:
            return
        with mysql_backend.connection("accounts-schema") as db:
            columns = {
                str(row.get("Field") or row.get("field") or "")
                for row in db.execute("SHOW COLUMNS FROM accounts").fetchall()
            }
            for name, definition in _OPTIMIZED_COLUMNS.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE accounts ADD COLUMN `{name}` {definition}")
            indexes = {
                str(row.get("Key_name") or row.get("key_name") or "")
                for row in db.execute("SHOW INDEX FROM accounts").fetchall()
            }
            index_ddl = {
                "idx_accounts_health_deactivated": "(health_status, is_deactivated)",
                "idx_accounts_group_health": "(group_id, health_status, is_deactivated)",
                "idx_accounts_trial_status": "(trial_eligibility_status)",
                "idx_accounts_access_deactivated": "(has_access_token, is_deactivated)",
                "idx_accounts_at_expiry": "(at_expires_epoch)",
            }
            for name, columns_sql in index_ddl.items():
                if name not in indexes:
                    db.execute(f"ALTER TABLE accounts ADD KEY `{name}` {columns_sql}")
            marker = db.execute(
                "SELECT value FROM account_meta WHERE name = %s",
                ("account_query_index_version",),
            ).fetchone()
            if not marker or str(marker.get("value") or "") != "2":
                db.execute(
                    """
                    UPDATE accounts
                       SET is_deactivated = IF(LOWER(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(public_payload, '$.is_deactivated')), '0')) IN ('true', '1'), 1, 0),
                           has_access_token = IF(LOWER(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(public_payload, '$.has_access_token')), '0')) IN ('true', '1'), 1, 0),
                           has_refresh_token = IF(LOWER(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(public_payload, '$.has_refresh_token')), '0')) IN ('true', '1'), 1, 0),
                           has_password = IF(LOWER(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(public_payload, '$.has_password')), '0')) IN ('true', '1'), 1, 0),
                           has_totp = IF(LOWER(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(public_payload, '$.has_totp')), '0')) IN ('true', '1'), 1, 0),
                           at_expires_epoch = CAST(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(public_payload, '$.at_expires_epoch')), '0') AS UNSIGNED),
                           trial_eligibility_status = LOWER(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(public_payload, '$.trial_eligibility_status')), '')),
                           trial_eligibility_reason = LOWER(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(public_payload, '$.trial_eligibility_reason')), '')),
                           trial_eligibility_campaign_id = LOWER(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(public_payload, '$.trial_eligibility_campaign_id')), '')),
                           plan_type = LOWER(COALESCE(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(public_payload, '$.plan_type')), ''), 'free'))
                    """
                )
                db.execute(
                    "INSERT INTO account_meta(name, value) VALUES (%s, %s) "
                    "ON DUPLICATE KEY UPDATE value=VALUES(value)",
                    ("account_query_index_version", "2"),
                )
        _OPTIMIZED_DATABASES.add(database_key)


def _replace_instr(sql: str) -> str:
    marker = re.compile(r"\binstr\(", re.I)
    while match := marker.search(sql):
        depth = 1
        quoted = False
        comma = -1
        end = -1
        for index in range(match.end(), len(sql)):
            char = sql[index]
            if char == "'":
                quoted = not quoted
            elif not quoted:
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        end = index
                        break
                elif char == "," and depth == 1:
                    comma = index
        if comma < 0 or end < 0:
            raise ValueError("Malformed SQLite instr expression")
        haystack = sql[match.end():comma].strip()
        needle = sql[comma + 1:end].strip()
        sql = sql[:match.start()] + f"LOCATE({needle}, {haystack})" + sql[end + 1:]
    return sql


def _mysql_sql(sql: str) -> str:
    statement = sql.strip()
    if statement == "BEGIN IMMEDIATE" or statement == "BEGIN":
        return "START TRANSACTION"
    if statement == "PRAGMA optimize":
        return "SELECT 1"
    if "ON CONFLICT" in statement:
        raise ValueError("SQLite conflict clause requires a dedicated MySQL statement")

    def json_field(match: re.Match[str]) -> str:
        expression, path = match.groups()
        value = f"JSON_UNQUOTE(JSON_EXTRACT({expression}, '{path}'))"
        field = path[2:]
        if field in _BOOLEAN_FIELDS:
            return f"({value} IN ('true', '1'))"
        if field in _NUMERIC_FIELDS:
            return f"CAST(COALESCE({value}, '0') AS UNSIGNED)"
        return value

    statement = _JSON_FIELD.sub(json_field, statement)
    statement = _replace_instr(statement)
    statement = statement.replace("CAST(strftime('%s', 'now') AS INTEGER)", "UNIX_TIMESTAMP()")
    statement = statement.replace("LIMIT -1 OFFSET ?", "LIMIT 18446744073709551615 OFFSET ?")
    statement = statement.replace("ESCAPE '\\'", "ESCAPE '!'")
    return statement.replace("?", "%s")


def _mysql_params(sql: str, params: Iterable[Any]) -> tuple[Any, ...]:
    if " LIKE " not in sql or "ESCAPE '\\'" not in sql:
        return tuple(params)
    like_positions = {
        match.start(1)
        for match in re.finditer(r"\bLIKE\s+(\?)\s+ESCAPE\s+'\\'", sql, re.I)
    }
    placeholder_positions = [match.start() for match in re.finditer(r"\?", sql)]
    converted = []
    for index, value in enumerate(params):
        if index < len(placeholder_positions) and placeholder_positions[index] in like_positions:
            value = re.sub(r"\\([\\%_])", r"!\1", value)
        converted.append(value)
    return tuple(converted)


class _Result:
    def __init__(self, cursor: Any):
        self.cursor = cursor
        self.rowcount = cursor.rowcount

    @staticmethod
    def _tuple(row: Any) -> tuple[Any, ...] | None:
        if row is None:
            return None
        return tuple(row.values()) if isinstance(row, dict) else tuple(row)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._tuple(self.cursor.fetchone())

    def fetchall(self) -> list[tuple[Any, ...]]:
        return [self._tuple(row) for row in self.cursor.fetchall()]

    def __iter__(self):
        for row in self.cursor:
            yield self._tuple(row)


class AccountMySQLConnection:
    is_mysql = True

    def __init__(self, manager: Any, connection: Any):
        self._manager = manager
        self._connection = connection
        self.total_changes = 0
        self._closed = False
        self._identity_locks: list[str] = []

    def lock_identities(self, records: Iterable[dict[str, Any]]) -> None:
        names = set()
        for record in records:
            for field in ("email", "account_id", "canonical_key"):
                value = str(record.get(field) or "").strip().lower()
                if value:
                    digest = hashlib.sha256(f"{field}:{value}".encode()).hexdigest()[:59]
                    names.add(f"acct:{digest}")
        for name in sorted(names - set(self._identity_locks)):
            row = self.execute("SELECT GET_LOCK(?, 30)", (name,)).fetchone()
            if row is None or row[0] != 1:
                raise TimeoutError("MySQL account identity lock timed out")
            self._identity_locks.append(name)

    def _release_identity_locks(self) -> None:
        while self._identity_locks:
            name = self._identity_locks.pop()
            self.execute("SELECT RELEASE_LOCK(?)", (name,))

    def execute(self, sql: str, params: Iterable[Any] = ()) -> _Result:
        cursor = self._connection.execute(_mysql_sql(sql), _mysql_params(sql, params))
        result = _Result(cursor)
        if sql.lstrip().upper().startswith(("DELETE ", "UPDATE ", "INSERT ")):
            self.total_changes += max(0, int(result.rowcount or 0))
        return result

    def executemany(self, sql: str, params: Iterable[Iterable[Any]]) -> _Result:
        cursor = self._connection.executemany(
            _mysql_sql(sql), [_mysql_params(sql, values) for values in params]
        )
        result = _Result(cursor)
        if sql.lstrip().upper().startswith(("DELETE ", "UPDATE ", "INSERT ")):
            self.total_changes += max(0, int(result.rowcount or 0))
        return result

    def commit(self) -> None:
        try:
            self._connection.commit()
        finally:
            self._release_identity_locks()

    def rollback(self) -> None:
        try:
            self._connection.rollback()
        finally:
            self._release_identity_locks()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._identity_locks:
                self.rollback()
            self._manager.__exit__(None, None, None)


def connect() -> AccountMySQLConnection:
    mysql_backend.ensure_schema("accounts", SCHEMA)
    _ensure_optimized_schema()
    manager = mysql_backend.connection("accounts")
    return AccountMySQLConnection(manager, manager.__enter__())

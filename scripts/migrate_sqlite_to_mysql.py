"""Import verified SQLite snapshots into an empty MySQL schema.

The source files are opened read-only and SQLite WAL sidecars are honored. Run with
``python -m scripts.migrate_sqlite_to_mysql`` from the repository root.
This command never changes production configuration or switches the backend.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

import pymysql

from common import mysql_backend


TABLES = (
    ("registered_accounts.sqlite3", "accounts", ("canonical_key",)),
    ("registered_accounts.sqlite3", "account_meta", ("name",)),
    ("task_history.sqlite3", "task_cards", ("card_id",)),
    ("task_history.sqlite3", "task_meta", ("name",)),
    ("account_operation_logs.sqlite3", "operation_events", ("event_id",)),
    ("account_operation_logs.sqlite3", "operation_aliases", ("alias_key", "account_key")),
    ("sms_number_pool.sqlite3", "sms_numbers", ("id",)),
    ("sms_number_pool.sqlite3", "sms_number_leases", ("lease_id",)),
    ("external_api.sqlite3", "settings", ("name",)),
    ("external_api.sqlite3", "scan_submissions", ("id",)),
    ("external_api.sqlite3", "upi_batches", ("id",)),
    ("external_api.sqlite3", "upi_jobs", ("id",)),
    ("external_api.sqlite3", "pipeline_tasks", ("id",)),
    ("external_api.sqlite3", "pipeline_items", ("task_id", "position")),
)


def _sqlite(path: Path) -> sqlite3.Connection:
    # immutable=1 skips WAL files. A verified snapshot may still carry a WAL
    # sidecar, so use a normal read-only connection to include committed rows.
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30.0)
    connection.row_factory = sqlite3.Row
    return connection


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_manifest(snapshot: Path) -> dict[str, dict]:
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    listed = {entry["file"]: entry for entry in manifest["snapshots"]}
    required = {name for name, _, _ in TABLES}
    for name in required:
        path = snapshot / name
        expected = listed.get(name)
        if not expected or not path.is_file() or _file_hash(path) != expected["sha256"]:
            raise ValueError(f"Snapshot hash mismatch: {name}")
        with closing(_sqlite(path)) as source:
            if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError(f"SQLite quick_check failed: {name}")
            for table, count in expected["counts"].items():
                if source.execute(f"SELECT COUNT(*) FROM `{table}`").fetchone()[0] != count:
                    raise ValueError(f"Snapshot count mismatch: {name}/{table}")
    return listed


def _target_is_empty() -> None:
    with mysql_backend.connection("migration") as target:
        rows = target.execute("SHOW TABLES").fetchall()
    if rows:
        raise ValueError("Target MySQL schema is not empty; create a new schema for this migration")


def _bootstrap_schema() -> None:
    from common.account_store_mysql import SCHEMA as account_schema
    from common.task_store import _MYSQL_SCHEMA as task_schema
    from common.account_operation_log import _MYSQL_SCHEMA as operation_schema
    from common.sms_number_pool import _MYSQL_SCHEMA as sms_schema
    from common.external_api import ExternalAPIService
    from common.external_pipeline import PipelineManager
    from config import ROOT

    for kind, ddl in (
        ("accounts", account_schema),
        ("task_history", task_schema),
        ("operation_logs", operation_schema),
        ("sms_pool", sms_schema),
    ):
        mysql_backend.ensure_schema(kind, ddl)
    service = ExternalAPIService(Path(ROOT) / "data" / "external_api.sqlite3")
    manager = PipelineManager(service, lambda _group: {}, lambda _keys: {})
    manager._executor.shutdown(wait=True)
    manager._poll_executor.shutdown(wait=True)
    service.executor.shutdown(wait=True)
    with mysql_backend.connection("migration") as target:
        target.execute(
            "CREATE TABLE IF NOT EXISTS codex_migration_meta ("
            "migration_id VARCHAR(128) PRIMARY KEY, state VARCHAR(32) NOT NULL, "
            "manifest_sha256 VARCHAR(64) NOT NULL, completed_at DOUBLE NOT NULL)"
        )
        target.execute("DELETE FROM settings")


def _columns(source: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in source.execute(f"PRAGMA table_info(`{table}`)")]


def _rows_digest(rows, columns: list[str], keys: tuple[str, ...]) -> tuple[int, str]:
    key_positions = [columns.index(key) for key in keys]
    row_hashes: list[tuple[tuple, bytes]] = []
    for row in rows:
        values = tuple(row[column] for column in columns)
        key = tuple(values[position] for position in key_positions)
        encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        row_hashes.append((key, hashlib.sha256(encoded).digest()))
    row_hashes.sort(key=lambda item: item[0])
    digest = hashlib.sha256()
    for key, row_hash in row_hashes:
        digest.update(json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        digest.update(row_hash)
    return len(row_hashes), digest.hexdigest()


def _import_table(snapshot: Path, filename: str, table: str, keys: tuple[str, ...]) -> dict:
    with closing(_sqlite(snapshot / filename)) as source:
        columns = _columns(source, table)
        if not columns or not set(keys).issubset(columns):
            raise ValueError(f"Missing source schema: {table}")
        with mysql_backend.connection("migration") as target:
            target_columns = {row["Field"] for row in target.execute(f"SHOW COLUMNS FROM `{table}`")}
            if not set(columns).issubset(target_columns):
                raise ValueError(f"Target schema missing columns: {table}")
            insert = f"INSERT INTO `{table}` (" + ",".join(f"`{column}`" for column in columns) + ") VALUES ("
            insert += ",".join("%s" for _ in columns) + ")"
            cursor = source.execute(f"SELECT * FROM `{table}`")
            total = 0
            while batch := cursor.fetchmany(100):
                target.executemany(insert, (tuple(row[column] for column in columns) for row in batch))
                total += len(batch)
        with mysql_backend.connection("migration") as target:
            actual = target.execute(f"SELECT COUNT(*) AS count FROM `{table}`").fetchone()["count"]
        if actual != total:
            raise ValueError(f"Imported row count mismatch: {table} {total}/{actual}")
    return {"table": table, "rows": total, "columns": columns, "keys": list(keys)}


def _verify_table(snapshot: Path, filename: str, table: str, keys: tuple[str, ...]) -> dict:
    with closing(_sqlite(snapshot / filename)) as source:
        columns = _columns(source, table)
        expected_count, expected_digest = _rows_digest(
            source.execute(f"SELECT * FROM `{table}`"), columns, keys
        )
    with mysql_backend.connection("migration") as target:
        select = ",".join(f"`{column}`" for column in columns)
        with target.raw.cursor(pymysql.cursors.SSDictCursor) as cursor:
            cursor.execute(f"SELECT {select} FROM `{table}`")
            actual_count, actual_digest = _rows_digest(cursor, columns, keys)
    if (expected_count, expected_digest) != (actual_count, actual_digest):
        raise ValueError(f"Full-row verification mismatch: {table}")
    return {"table": table, "rows": actual_count, "sha256": actual_digest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--confirm-empty-target", required=True)
    args = parser.parse_args()
    if not mysql_backend.mysql_enabled():
        raise ValueError("Set GPT_REGISTER_DB_BACKEND=mysql before migration")
    dsn = urlsplit(os.environ.get("GPT_REGISTER_MYSQL_DSN", ""))
    database = dsn.path.lstrip("/")
    if not database or args.confirm_empty_target != database:
        raise ValueError("--confirm-empty-target must equal the target MySQL database name")
    snapshot = args.snapshot.resolve(strict=True)
    _source_manifest(snapshot)
    _target_is_empty()
    _bootstrap_schema()
    imported = [_import_table(snapshot, *entry) for entry in TABLES]
    verified = [_verify_table(snapshot, *entry) for entry in TABLES]
    manifest_sha256 = _file_hash(snapshot / "manifest.json")
    with mysql_backend.connection("migration") as target:
        target.execute(
            "INSERT INTO codex_migration_meta(migration_id,state,manifest_sha256,completed_at) "
            "VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE state=VALUES(state), "
            "manifest_sha256=VALUES(manifest_sha256), completed_at=VALUES(completed_at)",
            ("87-baseline", "complete", manifest_sha256, time.time()),
        )
    report = {
        "snapshot": str(snapshot),
        "target_host": dsn.hostname,
        "target_database": database,
        "manifest_sha256": manifest_sha256,
        "verified_at_unix": time.time(),
        "tables": verified,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"tables": len(imported), "rows": sum(item["rows"] for item in verified), "report": str(args.report)}))


if __name__ == "__main__":
    main()

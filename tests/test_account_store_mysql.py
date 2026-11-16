from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from common import account_store, account_store_mysql, mysql_backend


class FakeCursor:
    def __init__(self, rows=(), rowcount=0):
        self.rows = list(rows)
        self.rowcount = rowcount

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def __iter__(self):
        return iter(self.rows)


class FakeMySQL:
    def __init__(self):
        self.statements = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=()):
        self.statements.append((sql, tuple(params)))
        if sql.startswith("SELECT GET_LOCK"):
            return FakeCursor([{"lock": 1}])
        if sql.startswith("SELECT RELEASE_LOCK"):
            return FakeCursor([{"released": 1}])
        if "SELECT value FROM account_meta" in sql:
            return FakeCursor([{"value": "3"}])
        if sql.startswith("SELECT COUNT(*) FROM accounts"):
            return FakeCursor([{"COUNT(*)": 0}])
        if sql.startswith("SELECT"):
            return FakeCursor()
        return FakeCursor(rowcount=1)

    def executemany(self, sql, rows):
        rows = list(rows)
        self.statements.append((sql, rows))
        return FakeCursor(rowcount=len(rows))

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class MySQLAccountStoreTests(unittest.TestCase):
    def test_dialect_preserves_json_filter_and_literal_search(self):
        sql = (
            "lower(COALESCE(json_extract(a.public_payload, '$.plan_type'), '')) "
            "LIKE ? ESCAPE '\\' AND "
            "COALESCE(json_extract(a.public_payload, '$.has_access_token'), 0) = 1 "
            "AND instr(lower(a.health_status), 'team') > 0 "
            "AND CAST(strftime('%s', 'now') AS INTEGER) > 0 LIMIT -1 OFFSET ?"
        )
        adapted = account_store_mysql._mysql_sql(sql)
        self.assertIn("JSON_UNQUOTE(JSON_EXTRACT(a.public_payload, '$.plan_type'))", adapted)
        self.assertIn("IN ('true', '1')", adapted)
        self.assertIn("LOCATE('team', lower(a.health_status))", adapted)
        self.assertIn("UNIX_TIMESTAMP()", adapted)
        self.assertIn("ESCAPE '!'", adapted)
        self.assertIn("LIMIT 18446744073709551615 OFFSET %s", adapted)
        self.assertEqual(account_store_mysql._mysql_params(sql, (r"%a\%b\_c\\d%", 10)), (r"%a!%b!_c!\d%", 10))
        self.assertEqual(
            account_store_mysql._mysql_params(
                "name LIKE ? ESCAPE '\\' AND group_id = ?",
                (r"%one\%%", r"%group\%"),
            ),
            ("%one!%%", r"%group\%"),
        )

    def test_default_path_writes_mysql_without_creating_sqlite(self):
        fake = FakeMySQL()

        @contextmanager
        def connection(_kind):
            yield fake

        with (
            patch.object(mysql_backend, "enabled_for", return_value=True),
            patch.object(mysql_backend, "ensure_schema") as ensure,
            patch.object(mysql_backend, "connection", connection),
            patch.object(account_store, "_connect_sqlite", side_effect=AssertionError("SQLite opened")),
        ):
            saved = account_store.upsert_account_record({"email": "one@example.com", "group_id": "A"})
            self.assertEqual(account_store.account_store_revision(), 3)

        self.assertEqual(saved["email"], "one@example.com")
        self.assertTrue(ensure.called)
        self.assertTrue(any("ON DUPLICATE KEY UPDATE" in sql for sql, _ in fake.statements))
        self.assertTrue(any("START TRANSACTION" in sql for sql, _ in fake.statements))
        self.assertTrue(any("GET_LOCK" in sql for sql, _ in fake.statements))
        self.assertTrue(any("RELEASE_LOCK" in sql for sql, _ in fake.statements))
        self.assertFalse(any("ON CONFLICT" in sql or "?" in sql for sql, _ in fake.statements))
        self.assertGreater(fake.commits, 0)

    def test_identity_lock_is_released_on_rollback(self):
        fake = FakeMySQL()

        @contextmanager
        def connection(_kind):
            yield fake

        with (
            patch.object(mysql_backend, "ensure_schema"),
            patch.object(mysql_backend, "connection", connection),
        ):
            db = account_store_mysql.connect()
            db.lock_identities([{"email": "same@example.com"}])
            db.rollback()
            db.close()
        self.assertEqual(fake.rollbacks, 1)
        self.assertEqual(sum("GET_LOCK" in sql for sql, _ in fake.statements), 1)
        self.assertEqual(sum("RELEASE_LOCK" in sql for sql, _ in fake.statements), 1)

    def test_public_page_uses_parameterized_mysql_filters(self):
        fake = FakeMySQL()
        item = account_store.account_public_view({"email": "one@example.com", "group_id": "A"})
        original_execute = fake.execute

        def execute(sql, params=()):
            if sql.startswith("SELECT public_payload FROM accounts"):
                fake.statements.append((sql, tuple(params)))
                return FakeCursor([{"public_payload": json.dumps(item)}])
            if sql.lstrip().startswith("SELECT\n"):
                fake.statements.append((sql, tuple(params)))
                return FakeCursor([dict(enumerate((1, 0, 0, 0, 0)))])
            return original_execute(sql, params)

        fake.execute = execute

        @contextmanager
        def connection(_kind):
            yield fake

        with (
            patch.object(mysql_backend, "enabled_for", return_value=True),
            patch.object(mysql_backend, "ensure_schema"),
            patch.object(mysql_backend, "connection", connection),
        ):
            page = account_store.query_account_public_page(
                20, query="one%_", status="alive", group="A", plan="team"
            )

        self.assertEqual(page["items"][0]["email"], "one@example.com")
        self.assertTrue(any("ESCAPE '!'" in sql for sql, _ in fake.statements))
        self.assertTrue(any("LOCATE('team'" in sql for sql, _ in fake.statements))
        self.assertTrue(any("%one!%!_%" in values for _, values in fake.statements))


@unittest.skipUnless(os.environ.get("GPT_REGISTER_MYSQL_TEST_DSN"), "isolated MySQL DSN not configured")
class MySQLAccountStoreIntegrationTests(unittest.TestCase):
    def test_same_email_concurrent_identity_change_is_serialized(self):
        email = f"mysql-race-{uuid.uuid4().hex[:12]}@example.test"
        barrier = Barrier(2)
        with patch.dict(os.environ, {
            "GPT_REGISTER_DB_BACKEND": "mysql",
            "GPT_REGISTER_MYSQL_DSN": os.environ["GPT_REGISTER_MYSQL_TEST_DSN"],
        }):
            try:
                account_store.upsert_account_record({"email": email})

                def write(user_id):
                    barrier.wait(timeout=10)
                    return account_store.upsert_account_record({"email": email, "user_id": user_id})

                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [executor.submit(write, f"user-{index}") for index in range(2)]
                    outcomes = []
                    for future in futures:
                        try:
                            outcomes.append(future.result(timeout=20))
                        except ValueError as exc:
                            self.assertIn("account_identity_conflict", str(exc))
                self.assertEqual(len(outcomes), 1)
                self.assertEqual(len(account_store.load_account_records_by_emails([email])), 1)
            finally:
                row = account_store.load_account_record_by_identity(email)
                if row:
                    account_store.delete_account_records(
                        [account_store.account_delete_key(row)], include_records=False
                    )

    def test_indexed_account_workflow_matches_json_filtering(self):
        suffix = uuid.uuid4().hex[:12]
        group = f"mysql-test-{suffix}"
        first = {
            "email": f"mysql%{suffix}@example.test",
            "group_id": group,
            "health_status": "alive",
            "plan_type": "pro",
            "access_token": "token-for-test",
            "registered_at": "2026-01-02T00:00:00Z",
        }
        second = {
            "email": f"mysql_{suffix}@example.test",
            "group_id": group,
            "health_status": "invalid",
            "plan_type": "free",
            "registered_at": "2026-01-01T00:00:00Z",
        }
        keys = []
        with patch.dict(os.environ, {
            "GPT_REGISTER_DB_BACKEND": "mysql",
            "GPT_REGISTER_MYSQL_DSN": os.environ["GPT_REGISTER_MYSQL_TEST_DSN"],
        }):
            try:
                saved = account_store.upsert_account_records([first, second])
                keys = [account_store.account_delete_key(item) for item in saved]
                self.assertEqual(account_store.count_accounts(group_id=group), 2)
                self.assertEqual(len(account_store.load_account_records_by_emails([first["email"]])), 1)
                page = account_store.query_account_public_page(1, group=group)
                self.assertEqual((page["total"], page["returned"]), (2, 1))
                self.assertEqual(page["items"][0]["email"], first["email"])
                self.assertEqual(account_store.query_account_public_page(group=group, query="mysql%")["total"], 1)
                self.assertEqual(account_store.query_account_public_page(group=group, query="mysql_")["total"], 1)
                self.assertEqual(account_store.query_account_public_page(group=group, status="alive")["total"], 1)
                self.assertEqual(account_store.query_account_public_page(group=group, at="has_at")["total"], 1)
                self.assertEqual(account_store.query_account_public_page(group=group, plan="pro")["total"], 1)
                changed, _ = account_store.update_account_records(
                    keys[:1], {"health_status": "limited"}, include_records=False
                )
                self.assertEqual(changed, 1)
                self.assertEqual(account_store.query_account_public_page(group=group, status="limited")["total"], 1)
                moved, _ = account_store.assign_account_group(
                    keys[:1], group + "-moved", include_records=False
                )
                self.assertEqual(moved, 1)
                self.assertEqual(account_store.reassign_account_group(group, group + "-moved"), 1)
                self.assertEqual(account_store.count_accounts(group_id=group + "-moved"), 2)
                with tempfile.TemporaryDirectory() as directory:
                    local_path = Path(directory) / "accounts.json"
                    account_store.replace_account_records([first, second], path=local_path)
                    local_page = account_store.query_account_public_page(group=group, query="mysql%", path=local_path)
                    self.assertEqual(local_page["total"], 1)
            finally:
                if keys:
                    removed, _ = account_store.delete_account_records(keys, include_records=False)
                    self.assertEqual(removed, 2)


if __name__ == "__main__":
    unittest.main()

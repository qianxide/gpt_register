"""Opt-in integration checks against an isolated MySQL 8 test database."""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import account_operation_log, mysql_backend, sms_number_pool, task_store


@pytest.fixture
def mysql_test(monkeypatch):
    dsn = os.environ.get("GPT_REGISTER_MYSQL_TEST_DSN")
    if not dsn:
        pytest.skip("GPT_REGISTER_MYSQL_TEST_DSN is not set")
    monkeypatch.setenv("GPT_REGISTER_DB_BACKEND", "mysql")
    monkeypatch.setenv("GPT_REGISTER_MYSQL_DSN", dsn)
    monkeypatch.setenv("GPT_REGISTER_MYSQL_POOL_SIZE", "64")
    return uuid.uuid4().hex


def test_mysql_task_history_upsert_delete_and_rollback(mysql_test):
    key = "mysql-test:" + mysql_test
    card = {"card_id": key, "index": 1, "batch_id": "mysql-test", "status": "running",
            "updated_at_epoch": 10}
    task_store.save_runtime([card], {"batch_id": key})
    try:
        assert any(item["card_id"] == key for item in task_store.load_runtime()["cards"])
        card["status"] = "completed"
        task_store.save_runtime([card], {"batch_id": key})
        assert next(item for item in task_store.load_runtime()["cards"] if item["card_id"] == key)["status"] == "completed"

        with pytest.raises(RuntimeError, match="rollback"):
            with mysql_backend.connection("task_history") as db:
                db.execute("INSERT INTO task_cards(card_id,updated_at_epoch,payload) VALUES (%s,%s,%s)",
                           (key + "-rolled-back", 11, "{}"))
                raise RuntimeError("rollback")
        assert not any(item["card_id"] == key + "-rolled-back" for item in task_store.load_runtime()["cards"])
    finally:
        assert task_store.delete_runtime([key]) == 1


def test_mysql_operation_log_aliases_and_redaction(mysql_test):
    old = f"old-{mysql_test}@example.test"
    new = f"new-{mysql_test}@example.test"
    account_id = "acct-" + mysql_test
    account_operation_log.append_operation_event(
        {"email": old}, "rebind", "running", message="start",
        path=account_operation_log.DEFAULT_DB_PATH,
    )
    account_operation_log.append_operation_event(
        {"email": new, "account_id": account_id}, "rebind", "success",
        aliases=(old,), metadata={"accessToken": "private-token", "result": "ok"},
        path=account_operation_log.DEFAULT_DB_PATH,
    )
    rows, count = account_operation_log.list_operation_events(new)
    assert count == 2
    assert {row["email"] for row in rows} == {old, new}
    assert any(row["metadata"].get("accessToken") == "***" for row in rows)
    old_rows, old_count = account_operation_log.list_operation_events(old)
    assert old_count == 2
    assert {row["id"] for row in old_rows} == {row["id"] for row in rows}


@pytest.mark.parametrize("count", [20, 50])
def test_mysql_sms_lease_exclusivity(mysql_test, count):
    pool = sms_number_pool.SmsNumberPool(sms_number_pool._DEFAULT_DB_PATH, clock=lambda: 2_000_000_000.0)
    seed = int(mysql_test[:7], 16) % 9000000
    numbers = [f"1573{seed + i:07d}" for i in range(count)]
    lines = [f"2026-10-14|{phone}|https://168smsapi.com/sms/mysql-test-{mysql_test}-{i}"
             for i, phone in enumerate(numbers)]
    result = pool.import_text("\n".join(lines))
    assert result["imported"] == count
    assert result["summary"]["available"] >= count
    ids = []
    try:
        with ThreadPoolExecutor(max_workers=count) as workers:
            leases = list(workers.map(lambda _: pool.reserve(), range(count)))
        assert len({item["id"] for item in leases}) == count
        assert len({item["lease_id"] for item in leases}) == count
        ids = [item["id"] for item in leases]

        # Two workers racing to complete one lease must consume one use only.
        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(pool.complete, [leases[0]["lease_id"]] * 2))
        assert sorted(outcomes) == [False, True]
        for lease in leases[1:]:
            assert pool.cancel(lease["lease_id"])
        with mysql_backend.connection("sms_pool") as db:
            row = db.execute("SELECT uses FROM sms_numbers WHERE id=%s", (leases[0]["id"],)).fetchone()
        assert row["uses"] == 1
    finally:
        with mysql_backend.connection("sms_pool") as db:
            db.execute("DELETE FROM sms_number_leases WHERE number_id IN ("
                       "SELECT id FROM sms_numbers WHERE receive_url LIKE %s)",
                       ("%mysql-test-" + mysql_test + "%",))
            db.execute("DELETE FROM sms_numbers WHERE receive_url LIKE %s",
                       ("%mysql-test-" + mysql_test + "%",))

"""Opt-in MySQL integration tests for privacy-safe mailbox counters."""

from __future__ import annotations

import os
import uuid

import pytest

from common import mail_registration_metrics as metrics
from common import mysql_backend


@pytest.fixture
def mysql_test(monkeypatch):
    dsn = os.environ.get("GPT_REGISTER_MYSQL_TEST_DSN")
    if not dsn:
        pytest.skip("GPT_REGISTER_MYSQL_TEST_DSN is not set")
    monkeypatch.setenv("GPT_REGISTER_DB_BACKEND", "mysql")
    monkeypatch.setenv("GPT_REGISTER_MYSQL_DSN", dsn)
    monkeypatch.delenv("MAIL_REGISTRATION_METRICS_DB", raising=False)
    return uuid.uuid4().hex


def test_mysql_metrics_replay_outcome_wait_and_private_columns(mysql_test):
    token = "attempt-" + mysql_test
    mailbox = {"provider": "smsbower_mail", "email": "private-" + mysql_test + "@gmail.com"}
    attempt_key = metrics._key(token)
    before = metrics.get_summary()
    assert before["ok"]
    try:
        assert metrics.begin_attempt(mailbox, attempt_id=token) == token
        assert metrics.begin_attempt(mailbox, attempt_id=token) == token
        assert metrics.record_otp_wait(token, "wait-1", 1.25, received=True)
        assert metrics.record_otp_wait(token, "wait-1", 1.25, received=True)
        assert metrics.record_otp_wait(token, "wait-2", 2.0, received=False)
        assert metrics.finish_attempt(token, "success")
        assert metrics.finish_attempt(token, "success")
        assert metrics.finish_attempt(token, "other_failed") is False

        after = metrics.get_summary()
        assert after["ok"]
        assert after["totals"]["attempted"] == before["totals"]["attempted"] + 1
        assert after["totals"]["success"] == before["totals"]["success"] + 1
        assert after["otp_wait"]["received_count"] == before["otp_wait"]["received_count"] + 1
        assert after["otp_wait"]["timeout_count"] == before["otp_wait"]["timeout_count"] + 1

        with mysql_backend.connection("mail_registration_metrics") as db:
            row = db.execute("SELECT * FROM mail_registration_attempts WHERE attempt_key=%s",
                             (attempt_key,)).fetchone()
            waits = db.execute("SELECT * FROM mail_registration_otp_waits WHERE attempt_key=%s",
                               (attempt_key,)).fetchall()
        assert row["domain"] == "gmail.com"
        assert row["outcome"] == "success"
        assert len(waits) == 2
        assert all("private-" not in repr(value) for value in (row, waits))
    finally:
        with mysql_backend.connection("mail_registration_metrics") as db:
            db.execute("DELETE FROM mail_registration_otp_waits WHERE attempt_key=%s", (attempt_key,))
            db.execute("DELETE FROM mail_registration_attempts WHERE attempt_key=%s", (attempt_key,))


def test_mysql_metrics_explicit_path_remains_sqlite(mysql_test, monkeypatch, tmp_path):
    path = tmp_path / "metrics.sqlite3"
    monkeypatch.setenv("MAIL_REGISTRATION_METRICS_DB", str(path))
    token = "sqlite-" + mysql_test
    assert metrics.begin_attempt({"provider": "online_disposablemail", "email": "x@icloud.com"},
                                 attempt_id=token) == token
    assert metrics.finish_attempt(token, "otp_timeout")
    assert path.is_file()
    assert metrics.get_summary("online_disposablemail")["totals"]["otp_timeout"] == 1


def test_mysql_metrics_database_failure_stays_best_effort(monkeypatch):
    monkeypatch.setattr(metrics, "_use_mysql", lambda: True)
    monkeypatch.setattr(metrics, "_mysql_connection", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    token = uuid.uuid4().hex
    assert metrics.begin_attempt({"provider": "smsbower_mail", "domain": "gmail.com"},
                                 attempt_id=token) is None
    assert metrics.finish_attempt(token, "success") is False
    assert metrics.record_otp_wait(token, "wait", 1.0, received=True) is False
    assert metrics.get_summary()["ok"] is False

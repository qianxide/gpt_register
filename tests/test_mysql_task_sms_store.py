from __future__ import annotations

from contextlib import contextmanager

from common import sms_number_pool, task_store


class _Cursor:
    rowcount = 1

    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row

    def fetchall(self):
        return []


class _Connection:
    def __init__(self, row):
        self.row = row
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append((sql, tuple(params)))
        if "SELECT * FROM sms_numbers" in sql:
            return _Cursor(self.row)
        return _Cursor()


def test_default_task_store_uses_mysql_without_touching_legacy_file(monkeypatch, tmp_path):
    expected = {"version": 1, "updated_at": "", "cards": [], "last_job": {}}
    monkeypatch.setenv("GPT_REGISTER_DB_BACKEND", "mysql")
    monkeypatch.setattr(task_store, "_mysql_read", lambda **kwargs: expected)
    monkeypatch.setattr(task_store, "_connect", lambda *_: (_ for _ in ()).throw(AssertionError("SQLite opened")))

    assert task_store.load_runtime() == expected
    assert task_store._use_mysql(tmp_path / "history.json") is False


def test_mysql_sms_reserve_locks_candidate_before_assigning_lease(monkeypatch):
    row = {"id": 7, "phone": "+15739699032", "receive_url": "https://168smsapi.com/sms/test",
           "used_codes": "[]"}
    raw = _Connection(row)

    @contextmanager
    def connection(_kind):
        yield raw

    monkeypatch.setenv("GPT_REGISTER_DB_BACKEND", "mysql")
    monkeypatch.setattr(sms_number_pool.mysql_backend, "ensure_schema", lambda *_: None)
    monkeypatch.setattr(sms_number_pool.mysql_backend, "connection", connection)
    pool = sms_number_pool.SmsNumberPool(sms_number_pool._DEFAULT_DB_PATH, clock=lambda: 1000)

    lease = pool.reserve()

    assert lease["id"] == 7
    assert any("FOR UPDATE" in sql for sql, _ in raw.sql)
    assert any("INSERT INTO sms_number_leases" in sql for sql, _ in raw.sql)


def test_mysql_sms_complete_locks_lease_before_increment(monkeypatch):
    row = {"id": 7, "phone": "+15739699032", "lease_id": "lease1"}
    raw = _Connection(row)

    @contextmanager
    def connection(_kind):
        yield raw

    monkeypatch.setenv("GPT_REGISTER_DB_BACKEND", "mysql")
    monkeypatch.setattr(sms_number_pool.mysql_backend, "ensure_schema", lambda *_: None)
    monkeypatch.setattr(sms_number_pool.mysql_backend, "connection", connection)
    pool = sms_number_pool.SmsNumberPool(sms_number_pool._DEFAULT_DB_PATH, clock=lambda: 1000)

    assert pool.complete("lease1") is True
    assert any("WHERE lease_id=%s AND deleted=0 FOR UPDATE" in sql for sql, _ in raw.sql)
    assert any("SET uses=uses+%s" in sql for sql, _ in raw.sql)

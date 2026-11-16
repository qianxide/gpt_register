"""Pool accounting and exclusivity across independent workers; no SMS requests."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
import threading

import pytest

from common.sms_number_pool import SmsNumberPool, SmsPoolError, parse_number_line


def line(phone="15739699032", url="https://168smsapi.com/sms/openai-/fixture-secret"):
    return f"2026-09-17|{phone}|{url}"


@pytest.fixture
def pool(tmp_path):
    clock = [2000000000.0]
    value = SmsNumberPool(tmp_path / "numbers.sqlite3", clock=lambda: clock[0])
    return value, clock


@pytest.mark.parametrize("phone", ["15739699032", "+15739699032", "5739699032", "+1 (573) 969-9032"])
def test_us_number_and_markdown_link(phone):
    parsed = parse_number_line(line(phone, "[取码](https://168smsapi.com/sms/openai-/fixture-secret)"))
    assert parsed["phone"] == "+15739699032"
    assert parsed["source_date"] == "2026-09-17"
    assert parsed["url"].endswith("/fixture-secret")


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/private", "https://168smsapi.com.attacker.invalid/sms/test",
    "https://168smsapi.com@localhost/sms/test", "https://168smsapi.com:abc/sms/test",
    "https://168smsapi.com/admin", "https://168smsapi.com/sms/test\nAuthorization: secret",
])
def test_invalid_urls_are_rejected_without_leaking_receive_credentials(url):
    with pytest.raises(SmsPoolError) as exc:
        parse_number_line(line(url=url))
    assert url not in str(exc.value)


def test_three_successes_cooldown_survives_new_instances_and_duplicate_import(pool):
    value, clock = pool
    value.import_text(line())
    for expected in range(1, 4):
        current = SmsNumberPool(value.path, clock=lambda: clock[0])
        lease = current.reserve()
        assert current.complete(lease["lease_id"]) is True
        assert current.complete(lease["lease_id"]) is False
        assert current.cancel(lease["lease_id"]) is False
        entry = current.list_numbers()["items"][0]
        assert entry["uses"] == expected
        assert entry["remaining_uses"] == 3 - expected
        current.import_text(line())
        assert current.list_numbers()["items"][0]["uses"] == expected
        with pytest.raises(SmsPoolError):
            current.reserve()
        clock[0] += 3599
        with pytest.raises(SmsPoolError):
            current.reserve()
        clock[0] += 1
    assert value.summary() == {"total": 1, "available": 0, "cooling": 0, "reserved": 0, "exhausted": 1}
    with pytest.raises(SmsPoolError):
        value.reserve()


def test_failed_number_does_not_consume_a_success_but_cools_down(pool):
    value, clock = pool
    value.import_text(line())
    lease = value.reserve()
    assert value.cancel(lease["lease_id"])
    assert not value.cancel(lease["lease_id"])
    entry = value.list_numbers()["items"][0]
    assert entry["uses"] == 0
    assert entry["status"] == "cooling"
    assert entry["cooldown_remaining"] == 3600
    clock[0] += 3600
    assert value.reserve()["phone"] == lease["phone"]


def test_concurrent_workers_never_share_one_lease(pool):
    value, clock = pool
    value.import_text("\n".join(line(str(15739699032 + i)) for i in range(5)))
    gate = threading.Barrier(16)
    def acquire(_):
        other = SmsNumberPool(value.path, clock=lambda: clock[0])
        gate.wait(timeout=10)
        try:
            return other.reserve()
        except SmsPoolError:
            return None
    with ThreadPoolExecutor(max_workers=16) as workers:
        leases = [row for row in workers.map(acquire, range(16)) if row]
    assert len(leases) == len({row["phone"] for row in leases}) == 5
    assert len({row["lease_id"] for row in leases}) == 5
    assert value.summary()["reserved"] == 5


def test_expired_worker_releases_reservation_without_resetting_cooldown(pool):
    value, clock = pool
    value.import_text(line())
    old = value.reserve(lease_seconds=120)
    clock[0] += 121
    assert value.summary()["reserved"] == 0
    assert value.summary()["cooling"] == 1
    clock[0] += 3479
    new = value.reserve()
    assert old["lease_id"] != new["lease_id"]
    assert value.cancel(old["lease_id"]) is False
    with pytest.raises(SmsPoolError, match="过期"):
        value.complete(old["lease_id"])
    assert value.summary()["reserved"] == 1
    assert value.complete(new["lease_id"])


def test_deletion_skips_active_numbers_and_reimport_does_not_reset_usage(pool):
    value, clock = pool
    value.import_text(line())
    lease = value.reserve()
    assert value.delete([lease["id"]])["skipped"] == 1
    assert value.import_text(line())["invalid"] == 1
    value.complete(lease["lease_id"])
    assert value.delete([lease["id"]])["deleted"] == 1
    assert value.summary()["total"] == 0
    value.import_text(line())
    entry = value.list_numbers()["items"][0]
    assert entry["uses"] == 1
    assert entry["status"] == "cooling"
    assert entry["id"] == lease["id"]


def test_exhausted_delete_keeps_available_numbers_and_respects_selection(pool):
    value, clock = pool
    value.import_text("\n".join(line(str(15739699032 + i)) for i in range(3)))
    for _ in range(3):
        # All three leased then only the first two complete, third cancels.
        leases = [value.reserve() for _ in range(3)]
        for lease in leases:
            (value.cancel if lease["id"] == 3 else value.complete)(lease["lease_id"])
        clock[0] += 3600
    assert value.delete([1], exhausted_only=True)["deleted"] == 1
    assert value.summary()["exhausted"] == 1
    assert value.delete(exhausted_only=True)["deleted"] == 1
    assert value.summary()["available"] == 1
    assert value.list_numbers()["items"][0]["id"] == 3


def test_codes_not_reused_across_leases_and_never_exposed_in_pool_list(pool):
    value, clock = pool
    value.import_text(line())
    lease = value.reserve()
    assert value.remember_code(lease["lease_id"], "654321")
    assert not value.remember_code(lease["lease_id"], "654321")
    value.cancel(lease["lease_id"])
    clock[0] += 3600
    new = value.reserve()
    assert new["used_codes"] == [hashlib.sha256(b"654321").hexdigest()]
    assert not value.remember_code(new["lease_id"], "654321")
    assert value.remember_code(new["lease_id"], "012345")
    public = json.dumps(value.list_numbers())
    assert all(secret not in public for secret in ("fixture-secret", "654321", "012345", new["lease_id"], new["used_codes"][0]))


def test_mixed_import_and_pagination(pool):
    value, clock = pool
    result = value.import_text(line() + "\ninvalid\n" + line() + "\n" + line("15739699033"))
    assert (result["imported"], result["duplicates"], result["invalid"]) == (2, 1, 1)
    assert result["errors"][0]["line"] == 2
    first = value.list_numbers(page=1, page_size=1)
    second = value.list_numbers(page=2, page_size=1)
    assert first["total"] == 2
    assert first["items"][0]["id"] != second["items"][0]["id"]
    assert value.list_numbers(q="9033")["total"] == 1
    value.reserve()
    assert value.list_numbers(status="reserved")["total"] == 1
    assert value.list_numbers(status="available")["total"] == 1


def test_online_backup_and_restore_keep_counters_and_cooldown(pool, tmp_path, monkeypatch):
    from common import panel_ops
    value, clock = pool
    value.import_text(line())
    first = value.reserve()
    value.complete(first["lease_id"])
    before = value.list_numbers()["items"]
    monkeypatch.setattr(panel_ops, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(panel_ops, "BACKUP_FILES", {})
    monkeypatch.setattr(panel_ops, "BACKUP_DB_FILES", {"sms_number_pool.sqlite3": value.path})
    monkeypatch.setattr(panel_ops, "BACKUP_DIR_SOURCES", {})
    saved = panel_ops.create_backup()
    value.delete([first["id"]])
    assert value.summary()["total"] == 0
    panel_ops.restore_backup(saved["name"])
    assert value.list_numbers()["items"] == before

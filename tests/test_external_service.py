"""State-machine tests using temporary storage and fully fake paid services."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading
from types import SimpleNamespace

import pytest

from common.external_api import ExternalAPIService, ExternalAPIError, _parse_sessions
from common.external_clients import ExternalClientError

LINK = "https://payments.stripe.com/upi/instructions/fixture-payment"
KEY = "csk_fixture_private_api_key"
CDK = "fixture_private_cdk"
TOKEN = "fixture_private_access_token"
PROXY = "http://fixture:private-proxy@proxy.example.test:10000"


class Queue:
    def __init__(self):
        self.calls = []
    def submit(self, action, *args):
        self.calls.append((action, args))
    def run(self):
        while self.calls:
            action, args = self.calls.pop(0)
            action(*args)


@pytest.fixture
def env(tmp_path):
    clock = [2000000000.0]
    log = SimpleNamespace(scan=[], create=[], batch=[], fuse=[], status=[], cancel=[], closes=0, scan_error=None,
                          create_error=None, status_error=None, cancel_error=None, scan_hook=None, create_hook=None,
                          batch_result=None, batch_hook=None, fuse_error=None)
    config = {"unit_price": "0.7", "available": True, "max_batch": 200, "accepting_supplier_count": 1}
    status = {"ok": True, "status": "done", "result": {"provider_redirect_url": LINK, "payment_status": "awaiting_payment"}}

    class Scan:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
        def close(self): log.closes += 1
        def config(self): return deepcopy(config)
        def submit(self, links, idempotency_key):
            log.scan.append({"links": links, "id": idempotency_key, "key": self.kwargs["api_key"]})
            if log.scan_hook: log.scan_hook()
            if log.scan_error: raise log.scan_error
            return {"batch_no": "Bfixture", "order_count": len(links), "total_amount": "0.7", "balance": "9.3", "frozen_balance": "0.7", "idempotent": False,
                    "orders": [{"order_no": "Ofixture", "status": "pending", "link": link, "charge_amount": "0.7"} for link in links]}
        def orders(self, **kwargs):
            return {"items": [{"order_no": "Ofixture", "status": "pending", "link": LINK, "access_token": TOKEN, "result": f"access_token={TOKEN}", "api_key": KEY}],
                    "pagination": {"total": 1}}
        def balance(self): return {"balance": "9.3", "frozen_balance": "0.7", "unit_price": "0.7", "api_key": KEY}

    class Upi:
        def __init__(self, **kwargs): self.kwargs = kwargs
        def close(self): log.closes += 1
        def create(self, token, use_promo=True, **payment):
            log.create.append({"token": token, "promo": use_promo, **self.kwargs, **payment})
            if log.create_hook: log.create_hook()
            if log.create_error: raise log.create_error
            return {"ok": True, "job_id": f"job-{len(log.create)}", "job_secret": "fixture_job_secret", "queue_position": 2}
        def create_batch(self, sessions, use_promo=True, **payment):
            log.batch.append({"sessions": sessions, "promo": use_promo, **payment})
            if log.batch_hook: log.batch_hook()
            if log.create_error: raise log.create_error
            return deepcopy(log.batch_result) if log.batch_result is not None else {
                "jobs": [{"row": index, "job_id": f"batch-job-{index}", "job_secret": f"private-batch-secret-{index}"}
                         for index, _ in enumerate(sessions, 1)], "rejected": []}
        def cdk_fuse(self, codes, label=""):
            log.fuse.append((codes, label))
            if log.fuse_error: raise log.fuse_error
            return {"duplicate": len(log.fuse) > 1, "transferred_points": 7,
                    "target": {"remaining_uses": 17, "total_uses": 27, "enabled": True, "code": codes[0]}, "codes": codes}
        def status(self, job_id, job_secret):
            log.status.append((job_id, job_secret))
            if log.status_error: raise log.status_error
            return deepcopy(status)
        def cancel(self, job_id, job_secret):
            log.cancel.append((job_id, job_secret))
            if log.cancel_error: raise log.cancel_error
            return {"ok": True, "status": "cancelled"}
        def cdk_status(self): return {"remaining_uses": 10, "total_uses": 20, "enabled": True, "cdk": CDK}

    queue = Queue()
    service = ExternalAPIService(tmp_path / "external.sqlite3", scan_factory=Scan, upi_factory=Upi, executor=queue, clock=lambda: clock[0])
    service.save_settings({"scan": {"api_key": KEY}, "upi": {"cdk": CDK, "entry_proxy": PROXY, "exit_proxy": PROXY}})
    return SimpleNamespace(service=service, log=log, config=config, queue=queue, clock=clock, status=status, Scan=Scan, Upi=Upi)


def test_settings_are_partial_private_and_persistent(env):
    service = env.service
    service.save_settings({"scan": {"api_key": "", "bearer_token": "private-bearer"}})
    public = json.dumps(service.settings())
    for secret in (KEY, CDK, PROXY, "private-bearer"):
        assert secret not in public
    service.scan_submit("fixture-id", [LINK], "0.7")
    assert env.log.scan[0]["key"] == KEY
    restarted = ExternalAPIService(service.path, executor=Queue())
    assert restarted.settings() == service.settings()
    service.save_settings({"scan": {"clear_api_key": True}})
    assert not service.settings()["scan"]["has_api_key"]
    assert service.settings()["scan"]["has_bearer_token"]


@pytest.mark.parametrize("updates", [
    {"scan": {"base_url": "https://evil.invalid"}}, {"upi": {"entry_proxy": "bad"}},
    {"scan": {"api_key": "secret\r\nX-Test: value"}}, {"scan": {"api_key": {"private": "secret"}}},
])
def test_invalid_settings_do_not_partially_write(env, updates):
    before = env.service._settings()
    with pytest.raises(ExternalAPIError): env.service.save_settings(updates)
    assert env.service._settings() == before


def test_scan_price_uses_decimal_and_live_limit(env):
    env.config.update(unit_price="0.0007", max_batch=2)
    quote = env.service.scan_quote([LINK, LINK + "2"])
    assert quote["total_amount"] == "0.0014"
    with pytest.raises(ExternalAPIError): env.service.scan_quote([LINK, LINK + "2", LINK + "3"])
    assert env.log.scan == []


@pytest.mark.parametrize("links", [[LINK, " " + LINK], ["https://evil.invalid/link"], ["https://payments.stripe.com@evil.invalid/upi/instructions/one"], []])
def test_scan_invalid_links_are_rejected_before_submission(env, links):
    with pytest.raises(ExternalAPIError): env.service.scan_submit("fixture-id", links, "0.7")
    assert not env.log.scan


def test_changed_price_and_closed_platform_require_new_confirmation(env):
    env.config["unit_price"] = "0.8"
    with pytest.raises(ExternalAPIError, match="单价"):
        env.service.scan_submit("fixture-id", [LINK], "0.7")
    env.config["available"] = False
    with pytest.raises(ExternalAPIError, match="无人接单"):
        env.service.scan_submit("fixture-id", [LINK], "0.8")
    assert not env.log.scan


def test_scan_same_request_replays_locally_and_never_double_submits(env):
    first = env.service.scan_submit("fixture-id", [LINK], "0.7")
    again = env.service.scan_submit("fixture-id", [LINK], "0.70")
    # Cosmetic decimal formatting must not make one intent create two orders.
    assert first == again
    assert len(env.log.scan) == 1
    with pytest.raises(ExternalAPIError): env.service.scan_submit("fixture-id", [LINK + "2"], "0.7")
    env.service.save_settings({"scan": {"api_key": "different-key"}})
    with pytest.raises(ExternalAPIError): env.service.scan_retry("fixture-id")


def test_concurrent_same_scan_intent_sends_only_one_request(env):
    started, finish = threading.Event(), threading.Event()
    def block():
        started.set()
        assert finish.wait(5)
    env.log.scan_hook = block
    with ThreadPoolExecutor(max_workers=2) as threads:
        first = threads.submit(env.service.scan_submit, "fixture-id", [LINK], "0.7")
        assert started.wait(5)
        second = threads.submit(env.service.scan_submit, "fixture-id", [LINK], "0.7").result(3)
        assert second["submission"]["status"] == "submitting"
        finish.set()
        assert first.result(5)["submission"]["status"] == "submitted"
    assert len(env.log.scan) == 1


def test_scan_timeout_retry_preserves_original_key_and_links(env):
    env.log.scan_error = ExternalClientError("请求超时", code="TIMEOUT", uncertain=True)
    first = env.service.scan_submit("fixture-id", [LINK], "0.7")
    assert first["submission"]["status"] == "unknown"
    assert first["submission"]["retryable"]
    env.service.scan_submit("fixture-id", [LINK], "0.7")
    assert len(env.log.scan) == 1
    env.log.scan_error = None
    assert env.service.scan_retry("fixture-id")["submission"]["status"] == "submitted"
    assert env.log.scan[0] == env.log.scan[1]


def test_scan_429_obeys_retry_after(env):
    env.log.scan_error = ExternalClientError("限流", status=429, code="RATE_LIMITED", retry_after=15)
    env.service.scan_submit("fixture-id", [LINK], "0.7")
    with pytest.raises(ExternalAPIError) as caught: env.service.scan_retry("fixture-id")
    assert caught.value.status == 429 and caught.value.retry_after == 15
    env.clock[0] += 15
    env.log.scan_error = None
    env.service.scan_retry("fixture-id")
    assert len(env.log.scan) == 2


def test_scan_business_failure_is_not_retried_with_same_key(env):
    env.log.scan_error = ExternalClientError("余额不足", status=409, code="INSUFFICIENT_BALANCE")
    result = env.service.scan_submit("fixture-id", [LINK], "0.7")
    assert result["submission"]["status"] == "failed"
    env.service.scan_retry("fixture-id")
    assert len(env.log.scan) == 1


def test_scan_order_and_balance_fields_are_projected(env):
    env.service.save_settings({"scan": {"bearer_token": "fixture-bearer"}})
    results = json.dumps([env.service.scan_orders(), env.service.scan_balance()])
    assert TOKEN not in results and KEY not in results
    assert LINK in results and "0.7" in results


def test_sessions_parser_handles_json_and_deduplicates_by_token():
    assert _parse_sessions(TOKEN + "\n" + TOKEN) == [TOKEN]
    assert _parse_sessions(json.dumps([TOKEN, {"accessToken": TOKEN}])) == [TOKEN]
    obj = {"access_token": TOKEN, "user": {"email": "fixture@example.invalid"}}
    assert _parse_sessions(json.dumps(obj, indent=2)) == [obj]
    with pytest.raises(ExternalAPIError): _parse_sessions('{"accessToken":')
    with pytest.raises(ExternalAPIError): _parse_sessions('{"cookie":"private"}')


def test_upi_queue_dedup_and_private_inputs_not_persisted(env):
    result = env.service.upi_create("fixture-batch", TOKEN + "\n" + TOKEN)
    assert len(result["items"]) == len(env.queue.calls) == 1
    again = env.service.upi_create("fixture-batch", TOKEN)
    assert len(again["items"]) == len(env.queue.calls) == 1
    assert TOKEN.encode() not in env.service.path.read_bytes()
    env.queue.run()
    assert env.log.create == [{"token": TOKEN, "promo": True, "cdk": CDK, "entry_proxy": PROXY, "exit_proxy": PROXY, "payment_platform": "none"}]
    jobs = env.service.upi_jobs()
    assert jobs["items"][0]["job_id"] == "job-1"
    assert all(secret not in json.dumps(jobs) for secret in ("fixture_job_secret", TOKEN, CDK, PROXY))


def test_upi_ambiguous_creation_never_retries_post(env):
    env.log.create_error = ExternalClientError("超时", code="TIMEOUT", uncertain=True)
    result = env.service.upi_create("fixture-batch", TOKEN)
    env.queue.run()
    local_id = result["items"][0]["id"]
    assert env.service.upi_refresh(local_id)["status"] == "unknown"
    env.service.upi_create("fixture-batch", TOKEN)
    env.queue.run()
    assert len(env.log.create) == 1 and not env.log.status


def test_upi_rejects_changed_payload_for_same_batch(env):
    env.service.upi_create("fixture-batch", TOKEN)
    with pytest.raises(ExternalAPIError): env.service.upi_create("fixture-batch", TOKEN + "different")


def test_cancel_queued_job_does_not_consume_remote_credit(env):
    result = env.service.upi_create("fixture-batch", TOKEN)
    assert env.service.upi_cancel(result["items"][0]["id"])["status"] == "cancelled"
    env.queue.run()
    assert not env.log.create and not env.log.cancel


def test_cancel_during_create_is_forwarded_after_secret_is_saved(env):
    result = env.service.upi_create("fixture-batch", TOKEN)
    identifier = result["items"][0]["id"]
    def cancel_inflight():
        assert env.service.upi_cancel(identifier)["status"] == "cancelling"
    env.log.create_hook = cancel_inflight
    env.queue.run()
    assert env.log.cancel == [("job-1", "fixture_job_secret")]
    assert env.service.upi_jobs()["items"][0]["status"] == "cancelled"


def test_refresh_after_restart_uses_saved_job_secret_and_safe_results(env):
    result = env.service.upi_create("fixture-batch", TOKEN)
    env.queue.run()
    env.status["result"].update(access_token=TOKEN, job_secret="fixture_job_secret", logs=[TOKEN], short_link="https://example.test/payment-short")
    restarted = ExternalAPIService(env.service.path, scan_factory=env.Scan, upi_factory=env.Upi, executor=Queue(), clock=lambda: env.clock[0])
    job = restarted.upi_refresh(result["items"][0]["id"])
    assert job["status"] == "done"
    assert job["result"]["scan_link"] == LINK
    assert all(secret not in json.dumps(job) for secret in (TOKEN, "fixture_job_secret", CDK))
    restarted.upi_refresh(job["id"])
    assert len(env.log.status) == 1


def test_poll_is_throttled_and_network_errors_preserve_running_job(env):
    result = env.service.upi_create("fixture-batch", TOKEN)
    env.queue.run()
    env.log.status_error = ExternalClientError("请求超时", code="TIMEOUT")
    identifier = result["items"][0]["id"]
    first = env.service.upi_refresh(identifier)
    assert first["status"] == "queued" and first["error"] == "请求超时"
    env.service.upi_refresh(identifier)
    assert len(env.log.status) == 1
    env.clock[0] += 10
    env.log.status_error = None
    assert env.service.upi_refresh(identifier)["status"] == "done"


def test_stale_local_task_recovery_does_not_touch_live_queue(env):
    result = env.service.upi_create("fixture-batch", TOKEN)
    env.clock[0] += 601
    assert env.service.upi_jobs()["items"][0]["status"] == "queued"
    restarted = ExternalAPIService(env.service.path, executor=Queue(), clock=lambda: env.clock[0])
    assert restarted.upi_jobs()["items"][0]["status"] == "unknown"
    env.queue.run()
    assert not env.log.create


def test_scan_stale_submit_becomes_reconcilable_without_resubmitting(env):
    env.service.scan_submit("fixture-id", [LINK], "0.7")
    with env.service._db(True) as db:
        db.execute("UPDATE scan_submissions SET status='submitting'")
    env.clock[0] += 121
    assert env.service.scan_submissions()["items"][0]["status"] == "unknown"
    assert len(env.log.scan) == 1


def test_missing_credentials_and_empty_credit_response(env):
    env.service.save_settings({"upi": {"clear_cdk": True}, "scan": {"clear_api_key": True}})
    with pytest.raises(ExternalAPIError): env.service.upi_create("fixture-batch", TOKEN)
    with pytest.raises(ExternalAPIError): env.service.upi_credits()
    with pytest.raises(ExternalAPIError): env.service.scan_submit("fixture-id", [LINK], "0.7")
    assert not env.log.create and not env.log.scan


@pytest.mark.parametrize("platform,secret", [("foarge", "foarge_credential"), ("masi", "masi_cdk_ticket"), ("seashore", "seashore_cdk")])
def test_selected_payment_platform_snapshot_and_credentials_stay_private(env, platform, secret):
    secret_fields = {"foarge_credential": "private-foarge", "masi_cdk_ticket": "private-masi", "seashore_cdk": "private-seashore"}
    env.service.save_settings({"upi": {**secret_fields, "payment_platform": platform}})
    response = env.service.upi_create("payment-fixture", TOKEN, payment_platform=platform)
    assert response["items"][0]["payment_platform"] == platform
    env.service.save_settings({"upi": {"payment_platform": "none"}})
    env.queue.run()
    sent = env.log.create[0]
    assert sent["payment_platform"] == platform
    assert sent[secret] == secret_fields[secret]
    assert all(field not in sent for field in secret_fields if field != secret)
    assert env.service.settings()["upi"]["has_" + secret]
    assert all(value not in json.dumps(env.service.settings()) + json.dumps(env.service.upi_jobs()) for value in secret_fields.values())


def test_none_does_not_send_saved_payment_secrets(env):
    env.service.save_settings({"upi": {"foarge_credential": "private-foarge", "masi_cdk_ticket": "private-masi", "seashore_cdk": "private-seashore"}})
    env.service.upi_create("none-fixture", TOKEN)
    env.queue.run()
    assert not any(k.startswith(("foarge_", "masi_", "seashore_")) for k in env.log.create[0])


@pytest.mark.parametrize("patch", [
    {"payment_platform": ["masi", "seashore"]}, {"payment_platform": "both"},
    {"masi_require_online_workers": "false"}, {"seashore_api_base": "https://evil.invalid"},
    {"masi_api_base": "http://localhost"}, {"masi_api_base": "https://user:secret@host.test"},
    {"masi_auto_submit": True}, {"foarge_auto_submit": True, "seashore_auto_submit": True},
])
def test_payment_invalid_combination_rejected_without_any_mutation(env, patch):
    before = env.service._settings()
    with pytest.raises(ExternalAPIError): env.service.save_settings({"upi": patch})
    assert env.service._settings() == before
    assert not env.log.create


def test_missing_selected_platform_secret_stops_before_queue(env):
    env.service.save_settings({"upi": {"payment_platform": "masi"}})
    with pytest.raises(ExternalAPIError, match="masi"):
        env.service.upi_create("missing-key-fixture", TOKEN)
    assert not env.queue.calls
    assert env.service.upi_jobs()["total"] == 0


def test_batch_uses_one_native_request_and_persists_every_secret(env):
    env.service.save_settings({"upi": {"payment_platform": "seashore", "seashore_cdk": "private-card"}})
    created = env.service.upi_create("batch-two-fixture", TOKEN + "-one\n" + TOKEN + "-two")
    assert len(env.queue.calls) == 1 and len(created["items"]) == 2
    env.queue.run()
    assert len(env.log.batch) == 1 and not env.log.create
    assert env.log.batch[0]["payment_platform"] == "seashore"
    assert env.log.batch[0]["seashore_cdk"] == "private-card"
    rows = env.service.upi_jobs()["items"]
    assert {row["job_id"] for row in rows} == {"batch-job-1", "batch-job-2"}
    assert "private-batch-secret" not in json.dumps(rows)
    assert env.service._job(rows[0]["id"])["job_secret"].startswith("private-batch-secret")


def test_batch_partial_failure_and_missing_receipt_keep_successes(env):
    created = env.service.upi_create("partial-fixture", "private-one\nprivate-two\nprivate-three")
    env.log.batch_result = {"jobs": [{"row": 1, "job_id": "job-first", "job_secret": "private-batch-secret"}],
                            "rejected": [{"row": 2, "error": "invalid private-two"}]}
    env.queue.run()
    rows = [env.service._job(item["id"]) for item in created["items"]]
    assert [row["status"] for row in rows] == ["queued", "failed", "unknown"]
    assert rows[0]["job_secret"] == "private-batch-secret"
    assert "private-two" not in rows[1]["error"]
    assert len(env.log.batch) == 1 and not env.log.create


def test_batch_timeout_is_not_replayed_as_individual_tasks(env):
    env.log.create_error = ExternalClientError("请求超时", uncertain=True)
    env.service.upi_create("uncertain-batch", "private-one\nprivate-two")
    env.queue.run()
    assert {row["status"] for row in env.service.upi_jobs()["items"]} == {"unknown"}
    env.service.upi_create("uncertain-batch", "private-one\nprivate-two")
    env.queue.run()
    assert len(env.log.batch) == 1 and not env.log.create


def test_batch_cancel_before_and_during_request_has_exact_row_mapping(env):
    created = env.service.upi_create("cancel-batch", "private-one\nprivate-two\nprivate-three")
    first, second, third = [row["id"] for row in created["items"]]
    env.service.upi_cancel(first)
    env.log.batch_hook = lambda: env.service.upi_cancel(second)
    env.queue.run()
    assert env.log.batch[0]["sessions"] == ["private-two", "private-three"]
    assert env.service._job(first)["status"] == "cancelled"
    assert env.service._job(second)["status"] == "cancelled"
    assert env.service._job(third)["job_id"] == "batch-job-2"
    assert env.log.cancel == [("batch-job-1", "private-batch-secret-1")]


def test_duplicate_batch_row_is_uncertain_not_overwritten(env):
    created = env.service.upi_create("duplicate-batch", "private-one\nprivate-two")
    env.log.batch_result = {"jobs": [{"row": 1, "job_id": "one", "job_secret": "secret-one"},
                                    {"row": 1, "job_id": "two", "job_secret": "secret-two"}], "rejected": []}
    env.queue.run()
    assert all(env.service._job(row["id"])["status"] == "unknown" for row in created["items"])


def test_fusion_retains_first_card_and_never_exposes_or_implicitly_saves_codes(env):
    cfg = env.service._settings()
    result = env.service.upi_fuse(["target-card", "source-a", "source-b"], "合并额度")
    assert env.log.fuse == [(["target-card", "source-a", "source-b"], "合并额度")]
    assert result == {"ok": True, "duplicate": False, "transferred_points": "7", "target": {"remaining_uses": 17, "total_uses": 27, "enabled": True}}
    assert env.service._settings() == cfg
    assert "target-card" not in json.dumps(result)
    assert env.service.upi_fuse(["target-card", "source-a", "source-b"])["duplicate"] is True


def test_fusion_duplicates_and_invalid_count_never_call_upstream(env):
    for codes in ([], ["one"], ["one", "one"], ["one", ""], [f"card-{i}" for i in range(101)]):
        with pytest.raises(ExternalAPIError): env.service.upi_fuse(codes)
    assert not env.log.fuse


def test_fusion_unknown_preserves_recovery_guidance_no_automatic_retry(env):
    env.log.fuse_error = ExternalClientError("请求超时", uncertain=True)
    with pytest.raises(ExternalAPIError) as caught:
        env.service.upi_fuse(["target-card", "source-card"])
    assert caught.value.code == "FUSION_UNKNOWN"
    assert "相同" in str(caught.value)
    assert len(env.log.fuse) == 1

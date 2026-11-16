"""Run with GPT_REGISTER_MYSQL_INTEGRATION_DSN against a disposable MySQL 8 schema."""

import os
import uuid

import pytest

from config import ROOT
from common.external_api import ExternalAPIService
from common.external_pipeline import PipelineManager


@pytest.mark.skipif(not os.environ.get("GPT_REGISTER_MYSQL_INTEGRATION_DSN"), reason="no disposable MySQL schema")
def test_external_mysql_round_trip_and_recovery(monkeypatch):
    monkeypatch.setenv("GPT_REGISTER_DB_BACKEND", "mysql")
    monkeypatch.setenv("GPT_REGISTER_MYSQL_DSN", os.environ["GPT_REGISTER_MYSQL_INTEGRATION_DSN"])
    suffix = uuid.uuid4().hex
    scan_id = "scan_" + suffix
    upi_id = "upi_" + suffix
    pipeline_id = "pipeline_" + suffix
    link = "https://payments.stripe.com/upi/instructions/" + suffix
    calls = []

    class Scan:
        def __init__(self, **kwargs):
            pass

        def submit(self, links, idempotency_key):
            calls.append((tuple(links), idempotency_key))
            return {"batch_no": "fixture", "orders": []}

        def close(self):
            pass

    class Queue:
        def submit(self, action, *args):
            return None

    service = ExternalAPIService(ROOT / "data/external_api.sqlite3", scan_factory=Scan, executor=Queue())
    service._settings = lambda: {
        "scan": {"api_key": "fixture-key"},
        "upi": {"cdk": "fixture-cdk", "entry_proxy": "http://proxy.example.test:1000",
                "exit_proxy": "http://proxy.example.test:1000"},
    }
    service.scan_quote = lambda links: {"available": True, "unit_price": "0.7"}
    account = {"key": "account-" + suffix, "email": "fixture@example.invalid", "token": "token-" + suffix, "issue": ""}
    group = {"id": "fixture-group", "name": "Fixture", "accounts": [account]}
    monkeypatch.setattr(PipelineManager, "_launch", lambda self, task_id: None)

    try:
        first = service.scan_submit(scan_id, [link], "0.7")
        same = service.scan_submit(scan_id, [link], "0.7")
        assert first["submission"]["status"] == same["submission"]["status"] == "submitted"
        assert calls == [((link,), scan_id)]
        with service._db(True) as db:
            db.execute("UPDATE scan_submissions SET status='unknown' WHERE id=?", (scan_id,))
        retried = service.scan_retry(scan_id)
        assert retried["submission"]["status"] == "submitted"
        assert calls == [((link,), scan_id), ((link,), scan_id)]

        case_variant = service.scan_submit(scan_id.upper(), [link], "0.7")
        assert case_variant["submission"]["id"] == scan_id.upper()

        first_upi = service.upi_create(upi_id, "token-" + suffix)
        same_upi = service.upi_create(upi_id, "token-" + suffix)
        assert len(first_upi["items"]) == len(same_upi["items"]) == 1
        assert first_upi["items"][0]["id"] == same_upi["items"][0]["id"]

        manager = PipelineManager(service, lambda group_id: group, lambda keys: {account["key"]: account})
        first_task = manager.start(pipeline_id, group["id"], batch_size=10, unit_price="0.7")
        same_task = manager.start(pipeline_id, group["id"], batch_size=10, unit_price="0.7")
        task_id = first_task["task"]["id"]
        assert same_task["task"]["id"] == task_id
        restarted = PipelineManager(service, lambda group_id: group, lambda keys: {account["key"]: account})
        assert restarted.status(task_id)["task"]["id"] == task_id
        assert restarted.status(task_id)["task"]["counts"]["queued"] == 1
    finally:
        with service._db(True) as db:
            db.execute("DELETE FROM scan_submissions WHERE id=?", (scan_id,))
            db.execute("DELETE FROM scan_submissions WHERE id=?", (scan_id.upper(),))
            db.execute("DELETE FROM upi_jobs WHERE batch_id=?", (upi_id,))
            db.execute("DELETE FROM upi_batches WHERE id=?", (upi_id,))
            db.execute("DELETE FROM pipeline_items WHERE task_id IN (SELECT id FROM pipeline_tasks WHERE request_id=?)", (pipeline_id,))
            db.execute("DELETE FROM pipeline_tasks WHERE request_id=?", (pipeline_id,))

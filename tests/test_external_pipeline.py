from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3

import pytest

from common.external_pipeline import PipelineManager


PAYMENT_PREFIX = "https://payments.stripe.com/upi/instructions/pipeline-fixture-"


class FakePipelineService:
    def __init__(self, path: Path, accounts, *, failed_tokens=(), unknown_scan=False):
        self.path = Path(path)
        self.clock = lambda: 2_000_000_000.0
        self.accounts = accounts
        self.failed_tokens = set(failed_tokens)
        self.unknown_scan = unknown_scan
        self.upi_requests = []
        self.scan_requests = []
        self.credit_reads = 0
        self._jobs = {}

    @contextmanager
    def _db(self, write=False):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if db.in_transaction:
                db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def upi_create(self, request_id, sessions_text, use_promo=True, payment_platform="none"):
        tokens = sessions_text.splitlines()
        self.upi_requests.append((request_id, tuple(tokens)))
        items = []
        for position, token in enumerate(tokens, 1):
            local_id = f"upi-{request_id}-{position}"
            status = "failed" if token in self.failed_tokens else "done"
            self._jobs[local_id] = {"status": status, "token": token}
            items.append({"id": local_id, "status": status,
                          "error": "fixture extraction failed" if status == "failed" else ""})
        return {"ok": True, "batch_id": request_id, "items": items}

    def upi_refresh(self, local_id):
        job = self._jobs[local_id]
        if job["status"] == "failed":
            return {"id": local_id, "status": "failed", "error": "fixture extraction failed", "result": {}}
        return {"id": local_id, "status": "done", "error": "",
                "result": {"payment_link": PAYMENT_PREFIX + local_id}}

    def scan_submit(self, request_id, links, unit_price):
        self.scan_requests.append((request_id, tuple(links), unit_price))
        if self.unknown_scan:
            return {"submission": {"id": request_id, "status": "unknown", "error": "fixture result pending"}}
        return {"submission": {"id": request_id, "status": "submitted", "data": {
            "orders": [{"link": link, "status": "pending"} for link in links],
        }}}

    def upi_credits(self):
        self.credit_reads += 1
        return {"data": {"enabled": True, "remaining_uses": "99", "used_uses": "1"}}


def make_manager(tmp_path, count=1, *, failed_tokens=(), unknown_scan=False):
    accounts = [
        {"key": f"account-{index}", "email": f"user{index}@example.invalid",
         "token": f"token-{index}", "issue": ""}
        for index in range(count)
    ]
    service = FakePipelineService(tmp_path / "external.sqlite3", accounts,
                                  failed_tokens=failed_tokens, unknown_scan=unknown_scan)
    group = {"id": "group-1", "name": "fixture group", "accounts": accounts}

    def group_loader(group_id):
        assert group_id == "group-1"
        return group

    def token_loader(keys):
        return {item["key"]: item for item in accounts if item["key"] in keys}

    manager = PipelineManager(service, group_loader, token_loader)
    manager._launch = lambda task_id: None
    return manager, service, accounts


def test_preview_validates_batch_size_and_skips_incomplete_accounts(tmp_path):
    manager, _, accounts = make_manager(tmp_path, 2)
    accounts[1]["token"] = ""
    accounts[1]["issue"] = "no_token"

    preview = manager.preview("group-1", 10)
    assert preview["total_count"] == 2
    assert preview["eligible_count"] == 1
    assert preview["skipped_count"] == 1
    assert preview["total_batches"] == 1
    with pytest.raises(ValueError, match="10、20、30、50"):
        manager.preview("group-1", 15)


def test_group_runs_batches_in_order_and_continues_after_item_failure(tmp_path):
    failed = "token-7"
    manager, service, _ = make_manager(tmp_path, 21, failed_tokens={failed})
    task = manager.start("pipeline-request-1", "group-1", batch_size=10, unit_price="0.7")

    manager._run(task["task"]["id"])
    result = manager.status(task["task"]["id"], page=1, page_size=100)
    assert result["task"]["status"] == "completed"
    assert result["task"]["current_batch"] == 3
    assert result["task"]["counts"]["submitted"] == 20
    assert result["task"]["counts"]["failed"] == 1
    assert [request_id.rsplit("-", 1)[-1] for request_id, _ in service.upi_requests] == ["1", "2", "3"]
    assert [request_id.rsplit("-", 1)[-1] for request_id, _, _ in service.scan_requests] == ["1", "2", "3"]
    assert service.credit_reads >= 3


def test_unknown_scan_result_is_terminal_and_is_not_resubmitted(tmp_path):
    manager, service, _ = make_manager(tmp_path, 2, unknown_scan=True)
    task = manager.start("pipeline-request-unknown", "group-1", batch_size=10, unit_price="0.7")

    manager._run(task["task"]["id"])
    manager._run(task["task"]["id"])
    result = manager.status(task["task"]["id"])
    assert result["task"]["status"] == "completed"
    assert result["task"]["counts"]["unknown"] == 2
    assert len(service.scan_requests) == 1


def test_stop_before_worker_starts_pauses_and_resume_continues(tmp_path):
    manager, service, _ = make_manager(tmp_path, 1)
    task = manager.start("pipeline-request-pause", "group-1", batch_size=10, unit_price="0.7")
    task_id = task["task"]["id"]

    assert manager.stop(task_id)["task"]["status"] == "stopping"
    manager._run(task_id)
    assert manager.status(task_id)["task"]["status"] == "paused"
    assert not service.upi_requests

    manager.resume(task_id)
    manager._run(task_id)
    assert manager.status(task_id)["task"]["status"] == "completed"


def test_restart_reconciles_saved_upi_request_id_without_new_request(tmp_path, monkeypatch):
    manager, service, _ = make_manager(tmp_path, 1)
    task = manager.start("pipeline-request-restart", "group-1", batch_size=10, unit_price="0.7")
    task_id = task["task"]["id"]
    with service._db(True) as db:
        db.execute("UPDATE pipeline_tasks SET status='running', current_batch=1 WHERE id=?", (task_id,))
        db.execute("UPDATE pipeline_items SET status='extracting', upi_request_id=? WHERE task_id=? AND position=1",
                   (f"{task_id}-u-1", task_id))

    # Simulate manager construction after a service restart without letting
    # its constructor launch the recovered worker before the assertion setup.
    monkeypatch.setattr(PipelineManager, "_launch", lambda self, task_id: None)
    restarted = PipelineManager(service, manager.group_loader, manager.token_loader)
    restarted._run(task_id)

    assert [request_id for request_id, _ in service.upi_requests] == [f"{task_id}-u-1"]
    assert restarted.status(task_id)["task"]["status"] == "completed"


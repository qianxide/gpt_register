"""Persistent group-to-UPI-to-scan pipeline.

The pipeline deliberately keeps account tokens out of its database.  Tokens are
loaded only for the current batch, while the UPI and scan services own their
respective idempotency records.  A stopped worker can therefore resume by
reconciling the saved local job and submission identifiers.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
import json
import re
import threading
import time
import uuid
from typing import Any, Callable

from common import mysql_backend
from common.mysql_backend import ensure_schema


BATCH_SIZES = frozenset({10, 20, 30, 50})
UPI_PROVIDERS = frozenset({"legacy", "public"})
SCAN_PROVIDERS = frozenset({"cdk_scan", "seashore", "foarge", "masi", "auto"})
_TERMINAL_UPI = frozenset({"done", "failed", "cancelled", "unknown"})
_TERMINAL_ITEM = frozenset({"submitted", "failed", "skipped", "unknown"})
_LINK_FIELDS = ("scan_link", "provider_redirect_url", "verification_url", "short_link", "checkout_url", "payment_link")


def _now(clock: Callable[[], float]) -> float:
    return float(clock())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _request_id(value: str) -> str:
    value = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", value):
        raise ValueError("流水线提交编号格式无效")
    return value


def _price(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = Decimal(text)
        if not parsed.is_finite() or parsed < 0 or parsed > Decimal("1000000"):
            raise ValueError
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("扫码单价格式无效") from None
    return format(parsed.normalize(), "f")


class PipelineManager:
    """Own durable pipeline records and at most one worker per task."""

    def __init__(self, service: Any, group_loader: Callable[[str], dict[str, Any]], token_loader: Callable[[list[str]], dict[str, dict[str, Any]]]):
        self.service = service
        self.group_loader = group_loader
        self.token_loader = token_loader
        self.clock = getattr(service, "clock", time.time)
        self._guard = threading.RLock()
        self._workers: dict[str, Any] = {}
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="external-pipeline")
        self._poll_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="external-pipeline-poll")
        self._init()

    def _init(self) -> None:
        if getattr(self.service, "_mysql", False):
            ensure_schema("external_pipeline", [
                """CREATE TABLE IF NOT EXISTS pipeline_tasks (
                    id VARCHAR(64) COLLATE utf8mb4_bin PRIMARY KEY,
                    request_id VARCHAR(80) COLLATE utf8mb4_bin NOT NULL UNIQUE,
                    group_id VARCHAR(255) NOT NULL, group_name TEXT NOT NULL,
                    batch_size INT NOT NULL, use_promo TINYINT NOT NULL DEFAULT 1,
                    unit_price VARCHAR(32) NOT NULL DEFAULT '',
                    upi_provider VARCHAR(32) NOT NULL DEFAULT 'legacy',
                    scan_provider VARCHAR(32) NOT NULL DEFAULT 'cdk_scan',
                    status VARCHAR(32) NOT NULL, stop_requested TINYINT NOT NULL DEFAULT 0,
                    total_count INT NOT NULL DEFAULT 0, eligible_count INT NOT NULL DEFAULT 0,
                    skipped_count INT NOT NULL DEFAULT 0, total_batches INT NOT NULL DEFAULT 0,
                    current_batch INT NOT NULL DEFAULT 0, selected_keys LONGTEXT NOT NULL DEFAULT (''),
                    last_credits LONGTEXT NOT NULL DEFAULT ('{}'),
                    error LONGTEXT NOT NULL DEFAULT (''), created_at DOUBLE NOT NULL,
                    started_at DOUBLE NOT NULL DEFAULT 0, completed_at DOUBLE NOT NULL DEFAULT 0,
                    updated_at DOUBLE NOT NULL,
                    INDEX pipeline_tasks_updated (updated_at DESC))""",
                """CREATE TABLE IF NOT EXISTS pipeline_items (
                    task_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL, position INT NOT NULL,
                    account_key VARCHAR(512) COLLATE utf8mb4_bin NOT NULL,
                    email VARCHAR(512) NOT NULL DEFAULT '',
                    status VARCHAR(32) NOT NULL, issue VARCHAR(64) NOT NULL DEFAULT '',
                    upi_request_id VARCHAR(128) NOT NULL DEFAULT '',
                    upi_job_id VARCHAR(128) NOT NULL DEFAULT '',
                    upi_status VARCHAR(32) NOT NULL DEFAULT '', link TEXT NOT NULL DEFAULT (''),
                    scan_request_id VARCHAR(128) NOT NULL DEFAULT '',
                    scan_status VARCHAR(32) NOT NULL DEFAULT '',
                    scan_result LONGTEXT NOT NULL DEFAULT ('{}'),
                    error LONGTEXT NOT NULL DEFAULT (''), updated_at DOUBLE NOT NULL,
                    PRIMARY KEY(task_id, position), UNIQUE KEY pipeline_task_account (task_id, account_key),
                    INDEX pipeline_items_task_status (task_id, status, position))""",
            ])
            with self.service._db(True) as db:
                db.execute(
                    "UPDATE pipeline_tasks SET status='queued', stop_requested=0, "
                    "error='服务重启后继续核对任务', updated_at=? "
                    "WHERE status IN ('running','stopping')",
                    (_now(self.clock),),
                )
                restartable = db.execute(
                    "SELECT id FROM pipeline_tasks WHERE status='queued'"
                ).fetchall()
            for row in restartable:
                self._launch(row["id"])
            return
        with self.service._db(True) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS pipeline_tasks(
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    group_id TEXT NOT NULL,
                    group_name TEXT NOT NULL,
                    batch_size INTEGER NOT NULL,
                    use_promo INTEGER NOT NULL DEFAULT 1,
                    unit_price TEXT NOT NULL DEFAULT '',
                    upi_provider TEXT NOT NULL DEFAULT 'legacy',
                    scan_provider TEXT NOT NULL DEFAULT 'cdk_scan',
                    status TEXT NOT NULL,
                    stop_requested INTEGER NOT NULL DEFAULT 0,
                    total_count INTEGER NOT NULL DEFAULT 0,
                    eligible_count INTEGER NOT NULL DEFAULT 0,
                    skipped_count INTEGER NOT NULL DEFAULT 0,
                    total_batches INTEGER NOT NULL DEFAULT 0,
                    current_batch INTEGER NOT NULL DEFAULT 0,
                    selected_keys TEXT NOT NULL DEFAULT '',
                    last_credits TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    started_at REAL NOT NULL DEFAULT 0,
                    completed_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS pipeline_tasks_updated ON pipeline_tasks(updated_at DESC);
                CREATE TABLE IF NOT EXISTS pipeline_items(
                    task_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    account_key TEXT NOT NULL,
                    email TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    issue TEXT NOT NULL DEFAULT '',
                    upi_request_id TEXT NOT NULL DEFAULT '',
                    upi_job_id TEXT NOT NULL DEFAULT '',
                    upi_status TEXT NOT NULL DEFAULT '',
                    link TEXT NOT NULL DEFAULT '',
                    scan_request_id TEXT NOT NULL DEFAULT '',
                    scan_status TEXT NOT NULL DEFAULT '',
                    scan_result TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(task_id, position),
                    UNIQUE(task_id, account_key)
                );
                CREATE INDEX IF NOT EXISTS pipeline_items_task_status ON pipeline_items(task_id, status, position);
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(pipeline_tasks)")}
            if "upi_provider" not in columns:
                db.execute("ALTER TABLE pipeline_tasks ADD COLUMN upi_provider TEXT NOT NULL DEFAULT 'legacy'")
            if "scan_provider" not in columns:
                db.execute("ALTER TABLE pipeline_tasks ADD COLUMN scan_provider TEXT NOT NULL DEFAULT 'cdk_scan'")
            if "selected_keys" not in columns:
                db.execute("ALTER TABLE pipeline_tasks ADD COLUMN selected_keys TEXT NOT NULL DEFAULT ''")
            # A process can stop after an upstream mutation and before the
            # local status write.  Requeue the worker; the durable request IDs
            # below let the next run reconcile the existing mutation.
            db.execute(
                "UPDATE pipeline_tasks SET status='queued', stop_requested=0, "
                "error='服务重启后继续核对任务', updated_at=? "
                "WHERE status IN ('running','stopping')",
                (_now(self.clock),),
            )
            restartable = db.execute(
                "SELECT id FROM pipeline_tasks WHERE status='queued'"
            ).fetchall()
        for row in restartable:
            self._launch(row["id"])

    def _task(self, task_id: str):
        with self.service._db() as db:
            return db.execute("SELECT * FROM pipeline_tasks WHERE id=?", (task_id,)).fetchone()

    def _items(self, task_id: str, *, statuses: tuple[str, ...] | None = None, limit: int | None = None):
        with self.service._db() as db:
            sql = "SELECT * FROM pipeline_items WHERE task_id=?"
            params: list[Any] = [task_id]
            if statuses:
                marks = ",".join("?" for _ in statuses)
                sql += f" AND status IN ({marks})"
                params.extend(statuses)
            sql += " ORDER BY position"
            if limit is not None:
                sql += " LIMIT ?"
                params.append(max(1, int(limit)))
            return db.execute(sql, params).fetchall()

    def _update_task(self, task_id: str, **fields: Any):
        allowed = {
            "status", "stop_requested", "current_batch", "last_credits", "error",
            "started_at", "completed_at", "updated_at", "unit_price", "upi_provider", "scan_provider",
        }
        if set(fields) - allowed:
            raise ValueError("Invalid pipeline task fields")
        fields.setdefault("updated_at", _now(self.clock))
        with self.service._db(True) as db:
            db.execute(
                "UPDATE pipeline_tasks SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?",
                [*fields.values(), task_id],
            )
            return db.execute("SELECT * FROM pipeline_tasks WHERE id=?", (task_id,)).fetchone()

    def _update_item(self, task_id: str, position: int, **fields: Any):
        allowed = {
            "status", "issue", "upi_request_id", "upi_job_id", "upi_status", "link",
            "scan_request_id", "scan_status", "scan_result", "error", "updated_at",
        }
        if set(fields) - allowed:
            raise ValueError("Invalid pipeline item fields")
        fields.setdefault("updated_at", _now(self.clock))
        with self.service._db(True) as db:
            db.execute(
                "UPDATE pipeline_items SET " + ",".join(f"{key}=?" for key in fields) +
                " WHERE task_id=? AND position=?",
                [*fields.values(), task_id, position],
            )
            return db.execute(
                "SELECT * FROM pipeline_items WHERE task_id=? AND position=?", (task_id, position)
            ).fetchone()

    def _is_stopping(self, task_id: str) -> bool:
        row = self._task(task_id)
        return not row or bool(row["stop_requested"])

    @staticmethod
    def _public_item(row) -> dict[str, Any]:
        try:
            scan_result = json.loads(row["scan_result"] or "{}")
        except Exception:
            scan_result = {}
        return {
            "position": row["position"], "email": row["email"], "status": row["status"],
            "issue": row["issue"], "upi_request_id": row["upi_request_id"],
            "upi_job_id": row["upi_job_id"], "upi_status": row["upi_status"],
            "link": row["link"], "scan_request_id": row["scan_request_id"],
            "scan_status": row["scan_status"], "scan_result": scan_result,
            "error": row["error"], "updated_at": row["updated_at"],
        }

    @staticmethod
    def _selected_keys(row) -> tuple[str, ...] | None:
        """Return the explicit selection saved with a task, if any.

        An empty column means the task was intentionally created for the
        complete group (the legacy behaviour).  New hand-picked tasks store a
        JSON list, which lets the status view hide the durable ``not_selected``
        rows without changing task accounting.
        """
        raw = str(row["selected_keys"] or "").strip()
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(value, list):
            return None
        keys = tuple(str(item or "").strip() for item in value if str(item or "").strip())
        return keys or None

    def _counts(self, task_id: str) -> dict[str, int]:
        with self.service._db() as db:
            rows = db.execute(
                "SELECT status, COUNT(*) AS count FROM pipeline_items WHERE task_id=? GROUP BY status",
                (task_id,),
            ).fetchall()
            not_selected_row = db.execute(
                "SELECT COUNT(*) AS total FROM pipeline_items WHERE task_id=? AND issue=?",
                (task_id, "not_selected"),
            ).fetchone()
            not_selected = int(not_selected_row["total"] if getattr(self.service, "_mysql", False) else not_selected_row[0])
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        terminal = sum(counts.get(key, 0) for key in _TERMINAL_ITEM)
        return {
            "total": sum(counts.values()), "queued": counts.get("queued", 0),
            "extracting": counts.get("extracting", 0), "extracted": counts.get("extracted", 0),
            "scanning": counts.get("scanning", 0), "submitted": counts.get("submitted", 0),
            "failed": counts.get("failed", 0), "skipped": counts.get("skipped", 0),
            "unknown": counts.get("unknown", 0), "finished": terminal,
            "not_selected": not_selected,
        }

    def _public_task(self, row) -> dict[str, Any]:
        if row is None:
            raise KeyError("流水线任务不存在")
        try:
            credits = json.loads(row["last_credits"] or "{}")
        except Exception:
            credits = {}
        selected_keys = self._selected_keys(row)
        return {
            "id": row["id"], "request_id": row["request_id"], "group_id": row["group_id"],
            "group_name": row["group_name"], "batch_size": row["batch_size"],
            "use_promo": bool(row["use_promo"]), "unit_price": row["unit_price"],
            "upi_provider": row["upi_provider"], "scan_provider": row["scan_provider"],
            "status": row["status"], "stop_requested": bool(row["stop_requested"]),
            "total_count": row["total_count"], "eligible_count": row["eligible_count"],
            "selected_count": len(selected_keys) if selected_keys is not None else None,
            "skipped_count": row["skipped_count"], "total_batches": row["total_batches"],
            "current_batch": row["current_batch"], "last_credits": credits,
            "error": row["error"], "counts": self._counts(row["id"]),
            "created_at": row["created_at"], "started_at": row["started_at"],
            "completed_at": row["completed_at"], "updated_at": row["updated_at"],
        }

    def _public_task_with_items(self, row, page: int, page_size: int, *, selected_only: bool = False) -> dict[str, Any]:
        task = self._public_task(row)
        page = max(1, int(page))
        page_size = max(1, min(100, int(page_size)))
        # Explicit account selections are persisted as ``not_selected`` rows
        # so totals and audit history still cover the whole group.  The UI
        # asks for selected_only to show only the rows the operator checked.
        # Tasks without an explicit selection keep the historical all-group
        # view.
        filter_selected = bool(selected_only and self._selected_keys(row) is not None)
        with self.service._db() as db:
            where = "task_id=?"
            params: list[Any] = [row["id"]]
            if filter_selected:
                where += " AND issue<>?"
                params.append("not_selected")
            total_row = db.execute(f"SELECT COUNT(*) AS total FROM pipeline_items WHERE {where}", params).fetchone()
            total = int(total_row["total"] if getattr(self.service, "_mysql", False) else total_row[0])
            item_params = [*params, page_size, (page - 1) * page_size]
            items = db.execute(
                f"SELECT * FROM pipeline_items WHERE {where} ORDER BY position LIMIT ? OFFSET ?",
                item_params,
            ).fetchall()
        return {"task": task, "items": [self._public_item(item) for item in items],
                "total": total, "page": page, "page_size": page_size,
                "total_pages": max(1, (total + page_size - 1) // page_size),
                "selected_only": filter_selected}

    def _resolve_price(self, value: Any) -> str:
        normalized = _price(value)
        if normalized:
            return normalized
        config = self.service.scan_config()
        candidate = config.get("unit_price") if isinstance(config, dict) else ""
        normalized = _price(candidate)
        if not normalized:
            raise ValueError("扫码平台未返回有效单价")
        return normalized

    @staticmethod
    def _response_items(response: Any) -> list[dict[str, Any]]:
        if not isinstance(response, dict) or not isinstance(response.get("items"), list):
            return []
        return [item if isinstance(item, dict) else {} for item in response["items"]]

    @staticmethod
    def _selected_accounts(accounts: list[dict[str, Any]], account_keys: list[str] | tuple[str, ...] | None):
        if account_keys is None:
            return accounts, None
        if not isinstance(account_keys, (list, tuple)) or not account_keys or len(account_keys) > 10000:
            raise ValueError("请选择至少一个账号，最多 10000 个")
        normalized = [str(key or "").strip() for key in account_keys]
        # Account delete keys commonly contain an email address (for example
        # ``email:user+tag@example.com``).  Validate them as opaque printable
        # identifiers rather than applying an account-id-only alphabet.
        if any(not re.fullmatch(r"[^\s\x00-\x1f\x7f]{1,256}", key) for key in normalized):
            raise ValueError("所选账号编号格式无效")
        if len(set(normalized)) != len(normalized):
            raise ValueError("所选账号不能重复")
        known = {str(item.get("key") or "") for item in accounts}
        unknown = [key for key in normalized if key not in known]
        if unknown:
            raise ValueError("所选账号不属于当前分组")
        selected = [item for item in accounts if str(item.get("key") or "") in set(normalized)]
        return selected, tuple(sorted(normalized))

    def preview(self, group_id: str, batch_size: int = 20, account_keys: list[str] | None = None) -> dict[str, Any]:
        if batch_size not in BATCH_SIZES:
            raise ValueError("批量数量只支持 10、20、30、50")
        group = self.group_loader(str(group_id or "").strip())
        accounts = list(group.get("accounts") or [])
        selected, selected_keys = self._selected_accounts(accounts, account_keys)
        selected_set = None if selected_keys is None else set(selected_keys)
        eligible = [item for item in selected if item.get("token") and not item.get("issue")]
        skipped = [
            {"position": index, "email": str(item.get("email") or ""),
             "issue": "not_selected" if selected_set is not None and str(item.get("key") or "") not in selected_set
                      else str(item.get("issue") or "no_token")}
            for index, item in enumerate(accounts, 1)
            if (selected_set is not None and str(item.get("key") or "") not in selected_set)
            or not (item.get("token") and not item.get("issue"))
        ]
        return {
            "group": {"id": group["id"], "name": group["name"]}, "batch_size": batch_size,
            "total_count": len(accounts), "eligible_count": len(eligible), "skipped_count": len(skipped),
            "total_batches": (len(eligible) + batch_size - 1) // batch_size,
            "selected_count": len(selected),
            "selected_keys": list(selected_keys) if selected_keys is not None else None,
            "accounts": [{"position": index, "key": str(item.get("key") or ""),
                          "email": str(item.get("email") or ""),
                          "eligible": bool(item.get("token") and not item.get("issue")),
                          "issue": str(item.get("issue") or "")}
                         for index, item in enumerate(accounts, 1)],
            "skipped": skipped[:100], "skipped_truncated": max(0, len(skipped) - 100),
        }

    def start(self, request_id: str, group_id: str, batch_size: int = 20, use_promo: bool = True,
              unit_price: str = "", *, upi_provider: str = "legacy", scan_provider: str = "cdk_scan",
              account_keys: list[str] | None = None) -> dict[str, Any]:
        if batch_size not in BATCH_SIZES:
            raise ValueError("批量数量只支持 10、20、30、50")
        if upi_provider not in UPI_PROVIDERS:
            raise ValueError("提链平台只可选择 legacy 或 public")
        if scan_provider not in SCAN_PROVIDERS:
            raise ValueError("扫码平台只可选择 cdk_scan、seashore、foarge、masi 或 auto")
        request_id = _request_id(request_id)
        group = self.group_loader(str(group_id or "").strip())
        accounts = list(group.get("accounts") or [])
        selected, selected_keys = self._selected_accounts(accounts, account_keys)
        selected_set = None if selected_keys is None else set(selected_keys)
        eligible = [item for item in selected if item.get("token") and not item.get("issue")]
        requested_price = _price(unit_price)
        if scan_provider == "seashore":
            requested_price = "0"
        now = _now(self.clock)
        task_id = "pipe_" + uuid.uuid4().hex
        total_batches = (len(eligible) + batch_size - 1) // batch_size
        def matches(existing):
            price = requested_price or str(existing["unit_price"] or "")
            return (existing["group_id"] == group["id"] and existing["batch_size"] == batch_size
                    and bool(existing["use_promo"]) == bool(use_promo)
                    and existing["unit_price"] == price
                    and existing["upi_provider"] == upi_provider
                    and existing["scan_provider"] == scan_provider
                    and (json.loads(existing["selected_keys"] or "") if existing["selected_keys"] else None)
                    == (list(selected_keys) if selected_keys is not None else None))
        with self.service._db(True) as db:
            existing = db.execute("SELECT * FROM pipeline_tasks WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if not matches(existing):
                    raise ValueError("此流水线提交编号已用于其他任务")
                row = existing
            else:
                price = requested_price or self._resolve_price(unit_price)
                status = "completed" if not eligible else "queued"
                completed_at = now if not eligible else 0
                insert = """INSERT INTO pipeline_tasks
                    (id,request_id,group_id,group_name,batch_size,use_promo,unit_price,upi_provider,scan_provider,status,
                     total_count,eligible_count,skipped_count,total_batches,selected_keys,created_at,completed_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
                if getattr(self.service, "_mysql", False):
                    insert += " ON DUPLICATE KEY UPDATE id=id"
                inserted = db.execute(
                    insert,
                    (task_id, request_id, group["id"], group["name"], batch_size, int(bool(use_promo)), price, upi_provider, scan_provider,
                     status, len(accounts), len(eligible), len(accounts) - len(eligible), total_batches,
                     _json(list(selected_keys)) if selected_keys is not None else "",
                     now, completed_at, now),
                ).rowcount
                if not inserted:
                    row = db.execute("SELECT * FROM pipeline_tasks WHERE request_id=? FOR UPDATE", (request_id,)).fetchone()
                    if not row or not matches(row):
                        raise ValueError("此流水线提交编号已用于其他任务")
                else:
                    eligible_keys = {str(item.get("key") or "") for item in eligible}
                    for position, item in enumerate(accounts, 1):
                        key = str(item.get("key") or "")
                        is_eligible = (selected_set is None or key in selected_set) and key in eligible_keys and bool(item.get("token")) and not item.get("issue")
                        db.execute(
                            """INSERT INTO pipeline_items
                            (task_id,position,account_key,email,status,issue,updated_at)
                            VALUES(?,?,?,?,?,?,?)""",
                            (task_id, position, key, str(item.get("email") or ""),
                             "queued" if is_eligible else "skipped",
                             "" if is_eligible else ("not_selected" if selected_set is not None and key not in selected_set else str(item.get("issue") or "no_token")), now),
                        )
                    row = db.execute("SELECT * FROM pipeline_tasks WHERE id=?", (task_id,)).fetchone()
        if row["status"] in {"queued", "paused", "failed", "stopping"}:
            self._launch(row["id"])
        return self._public_task_with_items(row, 1, 100, selected_only=True)

    def _launch(self, task_id: str) -> None:
        with self._guard:
            current = self._workers.get(task_id)
            if current is not None and not current.done():
                return
            self._workers[task_id] = self._executor.submit(self._run, task_id)

    def _mark_paused(self, task_id: str) -> None:
        self._update_task(task_id, status="paused", error="")

    def _mark_complete_if_empty(self, task_id: str) -> bool:
        with self.service._db() as db:
            pending_row = db.execute(
                "SELECT COUNT(*) AS total FROM pipeline_items WHERE task_id=? AND status NOT IN ('submitted','failed','skipped','unknown')",
                (task_id,),
            ).fetchone()
            pending = int(pending_row["total"] if getattr(self.service, "_mysql", False) else pending_row[0])
        if pending:
            return False
        self._update_task(task_id, status="completed", stop_requested=0, completed_at=_now(self.clock), error="")
        return True

    def _next_items(self, task_id: str, limit: int):
        rows = self._items(task_id, statuses=("extracted", "queued"), limit=limit)
        return rows

    def _token_map(self, rows) -> dict[str, dict[str, Any]]:
        keys = [str(row["account_key"]) for row in rows if row["account_key"]]
        return self.token_loader(keys)

    @staticmethod
    def _result_link(result: Any) -> str:
        if not isinstance(result, dict):
            return ""
        for field in _LINK_FIELDS:
            value = result.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _save_upi_result(self, task_id: str, row, job: dict[str, Any]) -> None:
        status = str(job.get("status") or "")
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        if status == "done":
            link = self._result_link(result)
            if link:
                self._update_item(task_id, row["position"], status="extracted", upi_status=status, link=link, error="")
            else:
                self._update_item(task_id, row["position"], status="failed", upi_status=status, error="提链成功但没有可提交到扫码平台的付款链接")
        elif status in {"failed", "cancelled"}:
            self._update_item(task_id, row["position"], status="failed", upi_status=status,
                              error=str(job.get("error") or "提链失败")[:4000])
        elif status == "unknown":
            self._update_item(task_id, row["position"], status="unknown", upi_status=status,
                              error=str(job.get("error") or "提链结果未知，请到提链平台核对")[:4000])
        else:
            self._update_item(task_id, row["position"], upi_status=status)

    def _wait_upi(self, task_id: str, row):
        job_id = str(row["upi_job_id"] or "")
        if not job_id:
            self._update_item(task_id, row["position"], status="unknown", error="提链本地回执缺少任务编号，请到提链平台核对")
            return None
        current = {"status": row["upi_status"] or "queued", "result": {}, "error": ""}
        while True:
            if self._is_stopping(task_id):
                return None
            try:
                current = self.service.upi_refresh(job_id)
            except Exception as exc:
                self._update_item(
                    task_id, row["position"], status="unknown", upi_status="unknown",
                    error=(str(exc).strip()[:4000] or "提链状态查询结果待确认"),
                )
                return None
            status = str(current.get("status") or "")
            if status in _TERMINAL_UPI:
                self._save_upi_result(task_id, row, current)
                return current
            if not status:
                self._update_item(task_id, row["position"], status="unknown", upi_status="unknown",
                                  error="提链状态响应缺少状态，请到提链平台核对")
                return None
            time.sleep(2)

    def _apply_upi_response(self, task_id: str, rows, response: Any) -> None:
        returned = self._response_items(response)
        for index, row in enumerate(rows):
            item = returned[index] if index < len(returned) else {}
            local_id = str(item.get("id") or "")
            if not local_id:
                self._update_item(
                    task_id, row["position"], status="unknown", upi_status="unknown",
                    error="提链回执缺少本地任务编号，请到提链平台核对",
                )
                continue
            self._update_item(
                task_id, row["position"], upi_job_id=local_id,
                upi_status=str(item.get("status") or "queued"),
                error=str(item.get("error") or ""),
            )

    def _recover_extraction(self, task_id: str, rows, task) -> None:
        groups: dict[str, list[Any]] = {}
        for row in rows:
            request_id = str(row["upi_request_id"] or "")
            if request_id:
                groups.setdefault(request_id, []).append(row)
            else:
                self._update_item(task_id, row["position"], status="unknown", upi_status="unknown",
                                  error="提链恢复记录缺少提交编号，请到提链平台核对")
        for request_id, group_rows in groups.items():
            # Replaying the exact batch request is locally idempotent.  It is
            # required even when only one row already has a local job ID,
            # because the request fingerprint covers the whole batch.
            tokens = self._token_map(group_rows)
            missing = [row for row in group_rows if not str((tokens.get(row["account_key"]) or {}).get("token") or "").strip()]
            if missing:
                for row in missing:
                    if not row["upi_job_id"]:
                        self._update_item(task_id, row["position"], status="unknown", upi_status="unknown",
                                          error="服务重启后缺少账号 access token，提链结果待确认")
                group_rows = [row for row in group_rows if row not in missing]
            if group_rows and any(not row["upi_job_id"] for row in group_rows):
                try:
                    kwargs = {"use_promo": bool(task["use_promo"]), "payment_platform": "none"}
                    if task["upi_provider"] != "legacy":
                        kwargs["provider"] = task["upi_provider"]
                    response = self.service.upi_create(
                        request_id,
                        "\n".join(str((tokens[row["account_key"]] or {}).get("token") or "").strip() for row in group_rows),
                        **kwargs,
                    )
                    self._apply_upi_response(task_id, group_rows, response)
                except Exception as exc:
                    for row in group_rows:
                        if not row["upi_job_id"]:
                            self._update_item(task_id, row["position"], status="unknown", upi_status="unknown",
                                              error=(str(exc).strip()[:4000] or "提链恢复结果待确认"))
            current = self._items(task_id, statuses=("extracting",), limit=task["batch_size"])
            current = [row for row in current if row["upi_request_id"] == request_id and row["upi_job_id"]]
            futures = [self._poll_executor.submit(self._wait_upi, task_id, row) for row in current]
            for future in futures:
                future.result()

    def _apply_scan_response(self, task_id: str, rows, response: Any) -> None:
        submission = response.get("submission") if isinstance(response, dict) else None
        if not isinstance(submission, dict):
            raise ValueError("扫码提交回执异常")
        submission_status = str(submission.get("status") or "unknown")
        result = submission.get("data") if isinstance(submission.get("data"), dict) else {}
        public_result = {"status": submission_status, "data": result}
        order_statuses: dict[str, str] = {}
        for order in result.get("orders", []) if isinstance(result, dict) else []:
            if isinstance(order, dict) and isinstance(order.get("link"), str):
                order_statuses[order["link"]] = str(order.get("status") or "")
        for row in rows:
            order_status = order_statuses.get(row["link"], "")
            if submission_status == "submitted" and order_status not in {"failed", "error", "expired"}:
                item_status, error = "submitted", ""
            elif submission_status == "failed":
                item_status, error = "failed", str(submission.get("error") or "扫码提交失败")[:4000]
            else:
                item_status, error = "unknown", str(submission.get("error") or "扫码提交结果待确认")[:4000]
            self._update_item(task_id, row["position"], status=item_status, scan_status=submission_status,
                              scan_result=_json(public_result), error=error)

    def _recover_scans(self, task_id: str, rows, task) -> None:
        if task["scan_provider"] == "seashore":
            self._scan_seashore_batch(task_id, rows, task, recovering=True)
            return
        if task["scan_provider"] in {"foarge", "masi", "auto"}:
            # These publisher APIs have no stable idempotency key.  A worker
            # can crash after the remote mutation and before the local write;
            # never replay such a request automatically.
            for row in rows:
                self._update_item(
                    task_id, row["position"], status="unknown", scan_status="unknown",
                    error="外接 API 提交结果待确认，请到对应平台核对；未自动重发",
                    scan_result=_json({"status": "unknown"}),
                )
            return
        groups: dict[str, list[Any]] = {}
        for row in rows:
            request_id = str(row["scan_request_id"] or "")
            if request_id:
                groups.setdefault(request_id, []).append(row)
            else:
                self._update_item(task_id, row["position"], status="unknown", scan_status="unknown",
                                  error="扫码恢复记录缺少提交编号，请到扫码平台核对")
        for request_id, group_rows in groups.items():
            try:
                tokens = self._token_map(group_rows)
                access_tokens = [str((tokens.get(row["account_key"]) or {}).get("token") or "").strip() for row in group_rows]
                try:
                    response = self.service.scan_submit(
                        request_id, [str(row["link"]) for row in group_rows], task["unit_price"],
                        access_tokens=access_tokens,
                    )
                except TypeError as exc:
                    if "access_tokens" not in str(exc):
                        raise
                    response = self.service.scan_submit(
                        request_id, [str(row["link"]) for row in group_rows], task["unit_price"],
                    )
                self._apply_scan_response(task_id, group_rows, response)
            except Exception as exc:
                message = str(exc).strip()[:4000] or "扫码提交结果待确认"
                for row in group_rows:
                    self._update_item(task_id, row["position"], status="unknown", scan_status="unknown",
                                      error=message, scan_result=_json({"status": "unknown"}))

    def _scan_seashore_batch(self, task_id: str, rows, task, recovering: bool = False) -> None:
        rows = [row for row in rows if row["status"] == "scanning" or row["status"] == "extracted"]
        if not rows:
            return
        token_records = self._token_map(rows)
        for row in rows:
            if not recovering:
                request_id = f"{task_id}-s-{int(task['current_batch'] or 0)}-{int(row['position'])}"
                self._update_item(task_id, row["position"], status="scanning", scan_request_id=request_id, scan_status="submitting", error="")

        def submit(row):
            token = str((token_records.get(row["account_key"]) or {}).get("token") or "").strip()
            if not token:
                self._update_item(task_id, row["position"], status="failed", scan_status="failed", error="账号在扫码前已缺少 access token")
                return
            try:
                result = self.service.seashore_submit(str(row["email"] or ""), token, str(row["link"] or ""))
                public_id = str(result.get("public_id") or "")
                if not public_id:
                    raise ValueError("Seashore 回执缺少任务编号")
                self._update_item(
                    task_id, row["position"], status="submitted", scan_request_id=public_id,
                    scan_status=str(result.get("status") or "pending"), scan_result=_json(result), error="",
                )
            except Exception as exc:
                upstream_status = getattr(exc, "status", 502)
                status = "unknown" if not isinstance(upstream_status, int) or upstream_status >= 500 else "failed"
                self._update_item(
                    task_id, row["position"], status=status, scan_status=status,
                    error=(str(exc).strip()[:4000] or "Seashore 提交结果待确认"),
                    scan_result=_json({"status": status}),
                )

        with ThreadPoolExecutor(max_workers=max(1, min(8, len(rows)))) as executor:
            futures = [executor.submit(submit, row) for row in rows]
            for future in futures:
                future.result()

    def _recover_inflight(self, task_id: str, task) -> bool:
        extracting = self._items(task_id, statuses=("extracting",))
        scanning = self._items(task_id, statuses=("scanning",))
        if not extracting and not scanning:
            return False
        if extracting:
            self._recover_extraction(task_id, extracting, task)
        if scanning and not self._is_stopping(task_id):
            self._recover_scans(task_id, scanning, task)
        return True

    def _start_extraction(self, task_id: str, rows, task) -> list:
        queued = [row for row in rows if row["status"] == "queued"]
        if not queued:
            return list(rows)
        token_records = self._token_map(queued)
        active = []
        for row in queued:
            record = token_records.get(row["account_key"])
            token = str((record or {}).get("token") or "").strip()
            if not token:
                self._update_item(task_id, row["position"], status="failed", error="账号在任务执行前已缺少 access token")
                continue
            active.append((row, token))
        if not active:
            return self._items(task_id, statuses=("extracted", "queued"), limit=task["batch_size"])
        batch_number = int(task["current_batch"] or 0)
        upi_request_id = f"{task['id']}-u-{batch_number}"
        for row, _ in active:
            self._update_item(task_id, row["position"], status="extracting", upi_request_id=upi_request_id, error="")
        try:
            kwargs = {"use_promo": bool(task["use_promo"]), "payment_platform": "none"}
            if task["upi_provider"] != "legacy":
                kwargs["provider"] = task["upi_provider"]
            response = self.service.upi_create(
                upi_request_id, "\n".join(token for _, token in active), **kwargs,
            )
        except Exception:
            for row, _ in active:
                self._update_item(task_id, row["position"], status="queued", error="提链服务暂时不可用，任务可恢复后继续")
            raise
        self._apply_upi_response(task_id, [row for row, _ in active], response)
        current = self._items(task_id, statuses=("extracting",), limit=task["batch_size"])
        futures = [self._poll_executor.submit(self._wait_upi, task_id, row) for row in current]
        for future in futures:
            future.result()
        return self._items(task_id, statuses=("extracted", "queued"), limit=task["batch_size"])

    def _scan_batch(self, task_id: str, rows, task) -> None:
        rows = [row for row in rows if row["status"] == "extracted" and row["link"]]
        if not rows:
            return
        if task["scan_provider"] == "seashore":
            self._scan_seashore_batch(task_id, rows, task)
            return
        if task["scan_provider"] in {"foarge", "masi", "auto"}:
            self._scan_external_batch(task_id, rows, task)
            return
        batch_number = int(task["current_batch"] or 0)
        scan_request_id = f"{task['id']}-s-{batch_number}"
        links = [str(row["link"]) for row in rows]
        token_records = self._token_map(rows)
        access_tokens = [str((token_records.get(row["account_key"]) or {}).get("token") or "").strip() for row in rows]
        for row in rows:
            self._update_item(task_id, row["position"], status="scanning", scan_request_id=scan_request_id, scan_status="submitting", error="")
        try:
            try:
                response = self.service.scan_submit(
                    scan_request_id, links, task["unit_price"], access_tokens=access_tokens,
                )
            except TypeError as exc:
                if "access_tokens" not in str(exc):
                    raise
                response = self.service.scan_submit(scan_request_id, links, task["unit_price"])
            self._apply_scan_response(task_id, rows, response)
        except Exception as exc:
            message = str(exc)[:4000] or "扫码提交结果待确认"
            for row in rows:
                self._update_item(task_id, row["position"], status="unknown", scan_status="unknown",
                                  error=message, scan_result=_json({"status": "unknown"}))

    def _scan_external_batch(self, task_id: str, rows, task) -> None:
        """Submit extracted links to a target project's direct publisher API."""
        token_records = self._token_map(rows)
        for row in rows:
            request_id = f"{task_id}-s-{int(task['current_batch'] or 0)}-{int(row['position'])}"
            self._update_item(
                task_id, row["position"], status="scanning", scan_request_id=request_id,
                scan_status="submitting", error="",
            )

        def submit(row):
            token = str((token_records.get(row["account_key"]) or {}).get("token") or "").strip()
            if not token:
                self._update_item(task_id, row["position"], status="failed", scan_status="failed", error="账号在外接提交前已缺少 access token")
                return
            request_id = str(row["scan_request_id"] or f"{task_id}-s-{int(task['current_batch'] or 0)}-{int(row['position'])}")
            try:
                result = self.service.external_submit(
                    task["scan_provider"], email=str(row["email"] or ""), access_token=token,
                    pay_link=str(row["link"] or ""), request_id=request_id, unit_price=task["unit_price"],
                )
                if not isinstance(result, dict) or not result.get("ok"):
                    status = "unknown" if isinstance(result, dict) and result.get("status") == "unknown" else "failed"
                    self._update_item(task_id, row["position"], status=status, scan_status=status,
                                      error=str((result or {}).get("error") or "外接 API 未接受提交")[:4000],
                                      scan_result=_json(result or {"status": status}))
                    return
                self._update_item(
                    task_id, row["position"], status="submitted",
                    scan_status=str(result.get("status") or "submitted"),
                    scan_result=_json(result), error="",
                )
            except Exception as exc:
                code = str(getattr(exc, "code", "") or "")
                status = "unknown" if code == "EXTERNAL_SUBMIT_UNKNOWN" else "failed"
                self._update_item(
                    task_id, row["position"], status=status, scan_status=status,
                    error=(str(exc).strip()[:4000] or "外接 API 提交结果待确认"),
                    scan_result=_json({"status": status}),
                )

        with ThreadPoolExecutor(max_workers=max(1, min(8, len(rows)))) as executor:
            futures = [executor.submit(submit, row) for row in rows]
            for future in futures:
                future.result()

    def _refresh_credits(self, task_id: str) -> None:
        task = self._task(task_id)
        if task is not None and task["upi_provider"] == "public":
            return
        try:
            result = self.service.upi_credits()
            data = result.get("data") if isinstance(result, dict) else {}
            if isinstance(data, dict):
                safe = {key: data[key] for key in ("enabled", "remaining_uses", "total_uses", "used_uses", "reserved_uses", "channel") if key in data}
                self._update_task(task_id, last_credits=_json(safe))
        except Exception:
            # Credits are observational; a failed refresh must not replay a paid mutation.
            return

    def _run(self, task_id: str) -> None:
        # The in-process worker map prevents duplicate workers in one service,
        # while the advisory lock also serializes the same task across service
        # instances sharing the MySQL database.
        if getattr(self.service, "_mysql", False):
            with mysql_backend.named_lock(f"pipeline:{task_id}", timeout=0) as acquired:
                if not acquired:
                    return
                return self._run_owned(task_id)
        return self._run_owned(task_id)

    def _run_owned(self, task_id: str) -> None:
        try:
            row = self._task(task_id)
            if row is None:
                return
            if row["status"] == "completed":
                return
            if row["status"] == "stopping" or bool(row["stop_requested"]):
                self._mark_paused(task_id)
                return
            row = self._update_task(task_id, status="running", stop_requested=0, started_at=row["started_at"] or _now(self.clock), error="")
            while True:
                if self._is_stopping(task_id):
                    self._mark_paused(task_id)
                    return
                if self._mark_complete_if_empty(task_id):
                    self._refresh_credits(task_id)
                    return
                task = self._task(task_id)
                if self._recover_inflight(task_id, task):
                    if self._is_stopping(task_id):
                        self._mark_paused(task_id)
                        return
                    self._refresh_credits(task_id)
                    continue
                rows = self._next_items(task_id, int(task["batch_size"]))
                if not rows:
                    if self._mark_complete_if_empty(task_id):
                        self._refresh_credits(task_id)
                        return
                    time.sleep(1)
                    continue
                row = self._update_task(task_id, current_batch=int(task["current_batch"] or 0) + 1)
                rows = self._start_extraction(task_id, rows, row)
                if self._is_stopping(task_id):
                    self._mark_paused(task_id)
                    return
                task = self._task(task_id)
                self._scan_batch(task_id, rows, task)
                self._refresh_credits(task_id)
        except Exception as exc:
            row = self._task(task_id)
            if row and bool(row["stop_requested"]):
                self._mark_paused(task_id)
            elif row:
                self._update_task(task_id, status="failed", error=str(exc)[:4000] or "流水线执行失败")
        finally:
            with self._guard:
                self._workers.pop(task_id, None)

    def status(self, task_id: str | None = None, page: int = 1, page_size: int = 20,
               selected_only: bool = False) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = max(1, min(100, int(page_size)))
        if task_id:
            row = self._task(str(task_id))
            if row is None:
                raise KeyError("流水线任务不存在")
            return self._public_task_with_items(row, page, page_size, selected_only=selected_only)
        with self.service._db() as db:
            total_row = db.execute("SELECT COUNT(*) AS total FROM pipeline_tasks").fetchone()
            total = int(total_row["total"] if getattr(self.service, "_mysql", False) else total_row[0])
            rows = db.execute(
                "SELECT * FROM pipeline_tasks ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (page_size, (page - 1) * page_size),
            ).fetchall()
        return {"items": [self._public_task(row) for row in rows], "total": total,
                "page": page, "page_size": page_size,
                "total_pages": max(1, (total + page_size - 1) // page_size)}

    def stop(self, task_id: str) -> dict[str, Any]:
        row = self._task(str(task_id))
        if row is None:
            raise KeyError("流水线任务不存在")
        if row["status"] not in {"completed", "failed", "paused"}:
            row = self._update_task(task_id, status="stopping", stop_requested=1)
        elif row["status"] == "paused":
            row = self._update_task(task_id, stop_requested=1)
        return self._public_task_with_items(row, 1, 100, selected_only=True)

    def resume(self, task_id: str) -> dict[str, Any]:
        row = self._task(str(task_id))
        if row is None:
            raise KeyError("流水线任务不存在")
        if row["status"] == "completed":
            return self._public_task_with_items(row, 1, 100, selected_only=True)
        row = self._update_task(task_id, status="queued", stop_requested=0, error="")
        self._launch(task_id)
        return self._public_task_with_items(row, 1, 100, selected_only=True)

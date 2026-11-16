from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class RegistrationResourceLifecycleTests(unittest.TestCase):
    def test_job_persists_only_changed_cards_and_keeps_final_failure(self):
        from web.routes.jobs import JobState

        state = JobState(persist=True)
        state.schedule_persist = lambda: None
        batches = []

        def save(cards, _metadata):
            batches.append([dict(card) for card in cards])

        with patch("web.routes.jobs._state", return_value=SimpleNamespace(save_task_runtime=save)):
            state.task_event(1, "success", step="done")
            state.persist_runtime()
            state.task_event(2, "failed", step="failed")
            state.persist_runtime()
            state.compact_finished_cards()
            state.persist_runtime()

        self.assertEqual([[card["index"] for card in batch] for batch in batches], [[1], [2], [2]])
        self.assertNotIn("steps", batches[-1][0])
        self.assertEqual(state.success_count, 1)
        self.assertEqual(state.fail_count, 1)

    def test_failed_persist_retries_dirty_card(self):
        from web.routes.jobs import JobState

        state = JobState(persist=True)
        state.schedule_persist = lambda: None
        state.task_event(1, "failed", step="retry")
        saved = []

        def save(cards, _metadata):
            saved.extend(cards)

        with patch("web.routes.jobs._state", return_value=SimpleNamespace(save_task_runtime=save)):
            with patch("web.routes.jobs._state", return_value=SimpleNamespace(
                save_task_runtime=lambda *_args: (_ for _ in ()).throw(RuntimeError("db down"))
            )):
                with self.assertRaisesRegex(RuntimeError, "db down"):
                    state.persist_runtime()
            state.persist_runtime()

        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["index"], 1)

    def test_protocol_count_mode_keeps_success_count_without_returning_records(self):
        from register.protocol import run_protocol_register

        emails = [f"account{index}@example.com" for index in range(1, 5)]
        records = {
            1: {"email": emails[0], "access_token": "token-1"},
            3: {"email": emails[2], "access_token": "token-3"},
        }

        def register_one(index, **_kwargs):
            return dict(records[index]) if index in records else None

        async def run(retain_results):
            return await run_protocol_register(
                {"registration": {"engine": "protocol", "max_transient_retries": 0}},
                total=len(emails),
                concurrency=2,
                email_plan=emails,
                retain_results=retain_results,
            )

        with (
            patch("mail.release_stale_in_use", return_value=0),
            patch("register.protocol.resolve_proxy_pool", return_value=[]),
            patch("register.protocol.register_one", side_effect=register_one),
        ):
            self.assertEqual(asyncio.run(run(False)), 2)
            self.assertEqual(asyncio.run(run(True)), [records[1], records[3]])

    def test_web_protocol_dispatch_requests_count_mode(self):
        from register.batch import run_register

        with patch(
            "register.protocol.run_protocol_register", new=AsyncMock(return_value=3)
        ) as protocol_register:
            result = asyncio.run(
                run_register(
                    {"registration": {"engine": "protocol"}},
                    retain_results=False,
                )
            )

        self.assertEqual(result, 3)
        self.assertFalse(protocol_register.await_args.kwargs["retain_results"])

    def test_web_job_accepts_protocol_success_count(self):
        from web.routes.jobs import JobState, _run_job_thread

        job = JobState(persist=False)
        job.running = True
        job.total = 4
        state = SimpleNamespace(
            JOB=job,
            _LogStream=lambda original, _job: original,
            load_config=lambda _path: {"registration": {}},
            CONFIG_FILE=SimpleNamespace(is_file=lambda: False),
            _find_provider=lambda *_args: {},
            _maddy_blocked_domains=lambda _provider: set(),
            _run_register_async=AsyncMock(return_value=2),
            _maintenance_config=lambda _config: {},
            _now_iso=lambda: "2026-10-13T00:00:00Z",
        )

        with patch("web.routes.jobs._state", return_value=state):
            _run_job_thread({"total": 4, "concurrency": 2, "engine": "protocol"})

        self.assertFalse(job.running)
        self.assertEqual(job.success_count, 2)
        self.assertEqual(job.fail_count, 2)
        self.assertEqual(job.results, [])
        self.assertEqual(job.last_error, "")

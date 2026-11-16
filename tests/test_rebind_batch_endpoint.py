from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import patch

import web.routes.accounts_rebind as rebind_routes


class RebindBatchEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        import web_app

        self.web_app = web_app
        with web_app._REBIND_RESERVATION_LOCK:
            web_app._REBIND_RESERVATIONS.clear()

    def tearDown(self) -> None:
        with self.web_app._REBIND_RESERVATION_LOCK:
            self.web_app._REBIND_RESERVATIONS.clear()

    @staticmethod
    def _record(email: str, account_id: str) -> dict:
        return {
            "email": email,
            "account_id": account_id,
            "password": "saved-password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }

    def test_preflight_reserves_each_batch_target_and_reports_items(self):
        web_app = self.web_app
        records = {
            "one@example.com": self._record("one@example.com", "one"),
            "two@example.com": self._record("two@example.com", "two"),
        }
        allocated: list[str] = []

        def reserve(_cfg, *, target_email, provider_type):
            allocated.append(target_email)
            address = target_email or f"auto-{len(allocated)}@example.net"
            return {"provider": provider_type, "address": address}

        body = web_app.AccountRebindPreflightRequest(
            text="one@example.com----one-target@example.net\ntwo@example.com----two-target@example.net",
            provider_type="maddy_webui",
        )
        with (
            patch.object(web_app, "_read_config", return_value={"mail": {}}),
            patch.object(rebind_routes, "_rebind_load_record", side_effect=lambda value: records.get(value.lower())),
            patch.object(rebind_routes, "_rebind_reserve_target", side_effect=reserve),
            patch.object(web_app, "account_is_deactivated", return_value=False),
        ):
            result = asyncio.run(web_app.account_rebind_preflight(body))

        self.assertTrue(result["eligible"])
        self.assertEqual(result["requested"], 2)
        self.assertEqual(result["matched"], 2)
        self.assertEqual(result["eligible_count"], 2)
        self.assertEqual(allocated, ["one-target@example.net", "two-target@example.net"])
        self.assertEqual(
            {item["target"]["email"] for item in result["items"]},
            {"one-target@example.net", "two-target@example.net"},
        )
        reservation_id = result["reservation_id"]
        self.assertTrue(reservation_id)
        with web_app._REBIND_RESERVATION_LOCK:
            reservation = web_app._REBIND_RESERVATIONS[reservation_id]
            self.assertTrue(reservation.get("batch"))
            self.assertEqual(len(reservation["items"]), 2)

    def test_mailcom_auto_preflight_defers_alias_creation_until_start(self):
        web_app = self.web_app
        record = self._record("one@example.com", "one")
        body = web_app.AccountRebindPreflightRequest(
            text="one@example.com",
            provider_type="mailcom_api",
            mailcom_domain="dr.com",
        )
        with (
            patch.object(web_app, "_read_config", return_value={"mail": {}}),
            patch.object(rebind_routes, "_rebind_load_record", return_value=record),
            patch.object(rebind_routes, "_rebind_reserve_target", side_effect=AssertionError("must defer allocation")),
            patch.object(web_app, "account_is_deactivated", return_value=False),
        ):
            result = asyncio.run(web_app.account_rebind_preflight(body))

        self.assertTrue(result["eligible"])
        self.assertIn("开始换绑时按需创建", result["message"])
        self.assertEqual(result["items"][0]["status"], "eligible")
        self.assertTrue(result["items"][0]["target"]["deferred"])
        self.assertEqual(result["items"][0]["target"]["domain"], "dr.com")
        with web_app._REBIND_RESERVATION_LOCK:
            reservation = web_app._REBIND_RESERVATIONS[result["reservation_id"]]
        child = reservation["items"][0]
        self.assertTrue(child["deferred_mailbox"])
        self.assertEqual(child["mailcom_domain"], "dr.com")
        self.assertNotIn("mailbox", child)

    def test_preflight_reports_unconfirmed_password_separately(self):
        web_app = self.web_app
        record = self._record("one@example.com", "one")
        record["password_set"] = False
        record["totp_secret"] = ""
        body = web_app.AccountRebindPreflightRequest(text="one@example.com")
        with (
            patch.object(web_app, "_read_config", return_value={"mail": {}}),
            patch.object(rebind_routes, "_rebind_load_record", return_value=record),
            patch.object(rebind_routes, "_rebind_reserve_target", side_effect=AssertionError("credentials gate must run first")),
            patch.object(web_app, "account_is_deactivated", return_value=False),
        ):
            result = asyncio.run(web_app.account_rebind_preflight(body))

        self.assertEqual(result["items"][0]["status"], "missing_credentials")
        self.assertEqual(result["items"][0]["missing"], ["password_unconfirmed", "totp"])
        self.assertEqual(result["missing_credentials"][0]["missing"], ["password_unconfirmed", "totp"])
        self.assertEqual(result["skipped_count"], 1)
        self.assertIn("已跳过", result["skipped"][0])

    def test_batch_skips_incomplete_credentials_and_keeps_eligible_accounts(self):
        web_app = self.web_app
        ready = self._record("ready@example.com", "ready")
        incomplete = self._record("incomplete@example.com", "incomplete")
        incomplete["totp_secret"] = ""
        records = {ready["email"]: ready, incomplete["email"]: incomplete}
        body = web_app.AccountRebindPreflightRequest(
            text="ready@example.com\nincomplete@example.com",
            provider_type="mailcom_api",
            mailcom_domain="dr.com",
        )
        with (
            patch.object(web_app, "_read_config", return_value={"mail": {}}),
            patch.object(rebind_routes, "_rebind_load_record", side_effect=lambda value: records.get(value.lower())),
            patch.object(rebind_routes, "_rebind_reserve_target", side_effect=AssertionError("mail.com allocation is deferred")),
            patch.object(web_app, "account_is_deactivated", return_value=False),
        ):
            result = asyncio.run(web_app.account_rebind_preflight(body))

        self.assertTrue(result["eligible"])
        self.assertEqual(result["eligible_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(
            [item["status"] for item in result["items"]],
            ["eligible", "missing_credentials"],
        )
        self.assertIn("TOTP 2FA", result["skipped"][0])
        with web_app._REBIND_RESERVATION_LOCK:
            reservation = web_app._REBIND_RESERVATIONS[result["reservation_id"]]
        self.assertEqual(len(reservation["items"]), 1)
        self.assertEqual(reservation["items"][0]["record_key"], web_app._account_key(ready))
        self.assertEqual(reservation["skipped"], result["skipped"])

    def test_start_accepts_deferred_mailcom_target_without_preflight_conflict_check(self):
        web_app = self.web_app
        record = self._record("one@example.com", "one")
        record_key = web_app._account_key(record)
        reservation_id = "deferred-mailcom-start"
        child = {
            "record_key": record_key,
            "source": rebind_routes._rebind_public_source(record),
            "email": "",
            "provider_type": "mailcom_api",
            "deferred_mailbox": True,
            "mailcom_domain": "dr.com",
        }
        with web_app._REBIND_RESERVATION_LOCK:
            web_app._REBIND_RESERVATIONS[reservation_id] = {
                "batch": True,
                "items": [child],
                "created_monotonic": time.monotonic(),
                "config": {"mail": {}},
            }
        captured = {}

        def fake_start(records, reservations, *, concurrency, worker, skipped=None):
            captured.update(records=records, reservations=reservations, concurrency=concurrency)
            return {"job_id": "deferred-job", "status": "queued", "total": len(records), "items": []}

        with (
            patch.object(rebind_routes, "_rebind_load_record", return_value=record),
            patch.object(rebind_routes, "_rebind_target_conflict", side_effect=AssertionError("target is allocated by the worker")),
            patch.object(web_app.ACCOUNT_REBIND_JOBS, "start", side_effect=fake_start),
        ):
            result = asyncio.run(
                web_app.account_rebind_start(
                    web_app.AccountRebindStartRequest(reservation_id=reservation_id, concurrency=1)
                )
            )

        self.assertEqual(result["job_id"], "deferred-job")
        self.assertEqual(captured["reservations"][record_key]["email"], "")
        self.assertTrue(captured["reservations"][record_key]["deferred_mailbox"])

    def test_start_claims_batch_and_passes_matching_reservations_to_job(self):
        web_app = self.web_app
        first = self._record("one@example.com", "one")
        second = self._record("two@example.com", "two")
        reservation_id = "batch-start-fixture"
        batch = {
            "batch": True,
            "created_monotonic": time.monotonic(),
            "config": {"mail": {}, "registration": {}, "proxy": {}},
            "skipped": ["incomplete@example.com: 已跳过（缺少 TOTP 2FA）"],
            "items": [
                {
                    "record_key": web_app._account_key(first),
                    "source": web_app._rebind_public_source(first),
                    "mailbox": {"provider": "maddy_webui", "address": "one-target@example.net"},
                    "email": "one-target@example.net",
                },
                {
                    "record_key": web_app._account_key(second),
                    "source": web_app._rebind_public_source(second),
                    "mailbox": {"provider": "maddy_webui", "address": "two-target@example.net"},
                    "email": "two-target@example.net",
                },
            ],
        }
        with web_app._REBIND_RESERVATION_LOCK:
            web_app._REBIND_RESERVATIONS[reservation_id] = batch
        captured = {}

        def fake_start(records, reservations, *, concurrency, worker, skipped=None):
            captured.update(
                records=records,
                reservations=reservations,
                concurrency=concurrency,
                skipped=list(skipped or []),
            )
            return {"job_id": "job-fixture", "status": "queued", "total": len(records), "items": []}

        with (
            patch.object(rebind_routes, "_rebind_load_record", side_effect=lambda value: {web_app._account_key(first): first, web_app._account_key(second): second}.get(value)),
            patch.object(rebind_routes, "_rebind_target_conflict", return_value=""),
            patch.object(web_app.ACCOUNT_REBIND_JOBS, "start", side_effect=fake_start),
        ):
            result = asyncio.run(
                web_app.account_rebind_start(
                    web_app.AccountRebindStartRequest(
                        reservation_id=reservation_id,
                        concurrency=2,
                    )
                )
            )

        self.assertEqual(result["job_id"], "job-fixture")
        self.assertEqual(result["skipped"], batch["skipped"])
        self.assertEqual(captured["concurrency"], 2)
        self.assertEqual(captured["skipped"], batch["skipped"])
        self.assertEqual({record["email"] for record in captured["records"]}, {first["email"], second["email"]})
        self.assertEqual(
            {reservation["email"] for reservation in captured["reservations"].values()},
            {"one-target@example.net", "two-target@example.net"},
        )
        with web_app._REBIND_RESERVATION_LOCK:
            self.assertNotIn(reservation_id, web_app._REBIND_RESERVATIONS)

    def test_start_skips_credentials_removed_after_preflight_and_keeps_other_rows(self):
        web_app = self.web_app
        first = self._record("ready@example.com", "ready")
        second_snapshot = self._record("changed@example.com", "changed")
        second_current = dict(second_snapshot, totp_secret="")
        first_key = web_app._account_key(first)
        second_key = web_app._account_key(second_snapshot)
        first_mailbox = {"provider": "maddy_webui", "address": "first-target@example.net"}
        second_mailbox = {"provider": "maddy_webui", "address": "second-target@example.net"}
        reservation_id = "start-credential-change-fixture"
        batch = {
            "batch": True,
            "created_monotonic": time.monotonic(),
            "config": {"mail": {}, "registration": {}, "proxy": {}},
            "items": [
                {
                    "record_key": first_key,
                    "source": rebind_routes._rebind_public_source(first),
                    "mailbox": first_mailbox,
                    "email": first_mailbox["address"],
                },
                {
                    "record_key": second_key,
                    "source": rebind_routes._rebind_public_source(second_snapshot),
                    "mailbox": second_mailbox,
                    "email": second_mailbox["address"],
                },
            ],
        }
        with web_app._REBIND_RESERVATION_LOCK:
            web_app._REBIND_RESERVATIONS[reservation_id] = batch
        records_by_key = {first_key: first, second_key: second_current}
        captured = {}

        def fake_start(records, reservations, *, concurrency, worker, skipped=None):
            captured.update(
                records=records,
                reservations=reservations,
                skipped=list(skipped or []),
            )
            return {"job_id": "credential-change-job", "status": "queued", "total": len(records), "items": []}

        with (
            patch.object(rebind_routes, "_rebind_load_record", side_effect=lambda value: records_by_key.get(value)),
            patch.object(rebind_routes, "_rebind_target_conflict", return_value=""),
            patch.object(web_app, "release_mailbox") as release_mailbox,
            patch.object(web_app.ACCOUNT_REBIND_JOBS, "start", side_effect=fake_start),
        ):
            result = asyncio.run(
                web_app.account_rebind_start(
                    web_app.AccountRebindStartRequest(reservation_id=reservation_id, concurrency=1)
                )
            )

        self.assertEqual(result["job_id"], "credential-change-job")
        self.assertEqual([record["email"] for record in captured["records"]], [first["email"]])
        self.assertEqual(captured["reservations"][first_key]["email"], first_mailbox["address"])
        self.assertEqual(captured["skipped"], ["changed@example.com: 已跳过（缺少或未确认 TOTP 2FA）"])
        self.assertEqual(result["skipped"], captured["skipped"])
        release_mailbox.assert_called_once_with(second_mailbox)

    def test_preflight_rejects_duplicate_addresses_returned_by_auto_pool(self):
        """A provider race must not put the same mailbox in two batch rows."""
        web_app = self.web_app
        records = {
            "one@example.com": self._record("one@example.com", "one"),
            "two@example.com": self._record("two@example.com", "two"),
        }
        duplicate = {"provider": "maddy_webui", "address": "same-target@example.net"}
        with (
            patch.object(web_app, "_read_config", return_value={"mail": {}}),
            patch.object(rebind_routes, "_rebind_load_record", side_effect=lambda value: records.get(value.lower())),
            patch.object(rebind_routes, "_rebind_reserve_target", return_value=dict(duplicate)),
            patch.object(web_app, "account_is_deactivated", return_value=False),
            patch.object(web_app, "release_mailbox") as release,
        ):
            result = asyncio.run(
                web_app.account_rebind_preflight(
                    web_app.AccountRebindPreflightRequest(
                        text="one@example.com\ntwo@example.com",
                    )
                )
            )

        self.assertEqual(result["eligible_count"], 1)
        self.assertEqual(len(result["items"]), 2)
        statuses = {item["input"]: item["status"] for item in result["items"]}
        self.assertEqual(statuses["one@example.com"], "eligible")
        self.assertEqual(statuses["two@example.com"], "target_conflict")
        # The duplicate candidate is returned immediately; the first remains
        # reserved for the batch and is released by tearDown.
        release.assert_called_once_with(duplicate)


if __name__ == "__main__":
    unittest.main()

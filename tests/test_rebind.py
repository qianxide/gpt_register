from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import patch

import web.routes.accounts_rebind as rebind_routes

from fastapi import HTTPException


class RebindEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        import web_app

        self.web_app = web_app
        with web_app._REBIND_RESERVATION_LOCK:
            web_app._REBIND_RESERVATIONS.clear()

    def tearDown(self) -> None:
        with self.web_app._REBIND_RESERVATION_LOCK:
            self.web_app._REBIND_RESERVATIONS.clear()

    def test_start_rejects_account_id_changed_after_preflight(self):
        web_app = self.web_app
        reservation_id = "fixture-reservation"
        source = {
            "email": "same@example.com",
            "account_id": "account-before",
        }
        with web_app._REBIND_RESERVATION_LOCK:
            web_app._REBIND_RESERVATIONS[reservation_id] = {
                "record_key": "email:same@example.com",
                "source": source,
                "mailbox": {
                    "provider": "maddy_webui",
                    "address": "target@example.com",
                },
                "email": "target@example.com",
                "created_monotonic": time.monotonic(),
            }

        current = {
            "email": "same@example.com",
            "account_id": "account-after",
            "password": "saved-password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
        with (
            patch.object(rebind_routes, "_rebind_load_record", return_value=current),
            patch.object(web_app, "release_mailbox") as release_mailbox,
        ):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(
                    web_app.account_rebind_start(
                        web_app.AccountRebindStartRequest(
                            reservation_id=reservation_id,
                        )
                    )
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("身份已变化", str(raised.exception.detail))
        release_mailbox.assert_called_once()
        with web_app._REBIND_RESERVATION_LOCK:
            self.assertNotIn(reservation_id, web_app._REBIND_RESERVATIONS)

    def test_start_rejects_target_occupied_after_preflight(self):
        web_app = self.web_app
        reservation_id = "target-conflict-reservation"
        source = {
            "email": "source@example.com",
            "account_id": "source-account",
        }
        mailbox = {
            "provider": "maddy_webui",
            "address": "target@example.com",
        }
        with web_app._REBIND_RESERVATION_LOCK:
            web_app._REBIND_RESERVATIONS[reservation_id] = {
                "record_key": "account_id:source-account",
                "source": source,
                "mailbox": mailbox,
                "email": mailbox["address"],
                "created_monotonic": time.monotonic(),
            }
        current = {
            "email": source["email"],
            "account_id": source["account_id"],
            "password": "saved-password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
        with (
            patch.object(rebind_routes, "_rebind_load_record", return_value=current),
            patch.object(rebind_routes, "_rebind_target_conflict", return_value="目标邮箱已在账号库中"),
            patch.object(web_app, "release_mailbox") as release_mailbox,
        ):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(
                    web_app.account_rebind_start(
                        web_app.AccountRebindStartRequest(
                            reservation_id=reservation_id,
                        )
                    )
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("目标邮箱已在账号库中", str(raised.exception.detail))
        release_mailbox.assert_called_once_with(mailbox)
        with web_app._REBIND_RESERVATION_LOCK:
            self.assertNotIn(reservation_id, web_app._REBIND_RESERVATIONS)

    def test_worker_setup_failure_releases_mailbox_and_records_error(self):
        web_app = self.web_app
        record = {
            "email": "setup-failure@example.com",
            "account_id": "setup-account",
            "password": "saved-password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
        mailbox = {
            "provider": "maddy_webui",
            "provider_ref": "maddy_webui#1",
            "address": "setup-target@example.com",
        }
        reservation = {
            "email": mailbox["address"],
            "mailbox": mailbox,
            "config": {"registration": {}, "proxy": {}, "mail": {}},
        }
        with (
            patch.object(web_app.ProtocolRuntimeSettings, "from_config", side_effect=RuntimeError("runtime fixture failure")),
            patch.object(web_app, "release_mailbox") as release_mailbox,
            patch.object(web_app, "update_account_records") as update_records,
            patch.object(web_app, "record_operation_event") as record_event,
        ):
            with self.assertRaisesRegex(RuntimeError, "runtime fixture failure"):
                web_app._rebind_worker(
                    index=1,
                    record=record,
                    reservation=reservation,
                    stop_check=lambda: False,
                    task_event=lambda *args, **kwargs: None,
                )

        release_mailbox.assert_called_once_with(mailbox)
        update_records.assert_called_once()
        self.assertEqual(record_event.call_args.args[1:3], ("email_rebind", "failed"))

    def test_worker_allocates_deferred_mailcom_alias_when_job_starts(self):
        web_app = self.web_app
        record = {
            "email": "source@example.com",
            "account_id": "source-account",
            "password": "saved-password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
        mailbox = {
            "provider": "mailcom_api",
            "provider_ref": "mailcom_api#1",
            "address": "new-alias@dr.com",
            "account": "owner@mail.com",
            "key": "alias-key",
        }
        cfg = {
            "mail": {"providers": [{"type": "mailcom_api", "accounts": "owner@mail.com", "alias_domain": "random"}]},
            "registration": {},
            "proxy": {},
        }
        reservation = {
            "deferred_mailbox": True,
            "provider_type": "mailcom_api",
            "mailcom_domain": "dr.com",
            "config": cfg,
        }
        with (
            patch.object(web_app.ProtocolRuntimeSettings, "from_config", return_value=object()),
            patch.object(rebind_routes, "_rebind_proxy_candidates", return_value=[""]),
            patch.object(rebind_routes, "_rebind_reserve_target", return_value=mailbox) as allocate,
            patch.object(web_app, "_rebind_target_conflict", return_value=""),
            patch.object(web_app, "run_rebind", side_effect=RuntimeError("fixture stop")),
            patch.object(web_app, "release_mailbox") as release_mailbox,
            patch.object(web_app, "update_account_records"),
            patch.object(web_app, "record_operation_event"),
        ):
            with self.assertRaisesRegex(RuntimeError, "fixture stop"):
                web_app._rebind_worker(
                    index=1,
                    record=record,
                    reservation=reservation,
                    stop_check=lambda: False,
                    task_event=lambda *args, **kwargs: None,
                )

        allocate.assert_called_once_with(
            cfg,
            target_email="",
            provider_type="mailcom_api",
            provider_ref="",
            mailcom_account="",
            mailcom_domain="dr.com",
        )
        release_mailbox.assert_called_once_with(mailbox)
        self.assertEqual(reservation["email"], "new-alias@dr.com")

    def test_stopping_before_deferred_mailcom_allocation_records_cancelled(self):
        web_app = self.web_app
        record = {
            "email": "source@example.com",
            "account_id": "source-account",
            "password": "saved-password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
        reservation = {
            "deferred_mailbox": True,
            "provider_type": "mailcom_api",
            "mailcom_domain": "dr.com",
            "config": {"mail": {}, "registration": {}, "proxy": {}},
        }
        with (
            patch.object(web_app.ProtocolRuntimeSettings, "from_config", return_value=object()),
            patch.object(rebind_routes, "_rebind_proxy_candidates", return_value=[""]),
            patch.object(rebind_routes, "_rebind_reserve_target", side_effect=AssertionError("must stop before allocating")),
            patch.object(web_app, "release_mailbox"),
            patch.object(web_app, "update_account_records"),
            patch.object(web_app, "record_operation_event") as record_event,
        ):
            with self.assertRaises(web_app.RebindError) as raised:
                web_app._rebind_worker(
                    index=1,
                    record=record,
                    reservation=reservation,
                    stop_check=lambda: True,
                    task_event=lambda *args, **kwargs: None,
                )

        self.assertEqual(raised.exception.code, "CANCELLED")
        self.assertEqual(record_event.call_args.args[2], "cancelled")
        self.assertEqual(record_event.call_args.kwargs["message"], "邮箱换绑已停止")

    def test_fastmail_alias_pool_is_supported_as_rebind_target(self):
        web_app = self.web_app
        mailbox = {
            "provider": "fastmail_api",
            "provider_ref": "fastmail_api#4",
            "address": "alias@fastmail.jp",
            "account": "master@fastmail.com",
            "alias_key": "alias-fixture",
        }
        cfg = {
            "mail": {
                "providers": [
                    {
                        "type": "fastmail_api",
                        "enable": True,
                        "api_base": "http://mailcode.example",
                        "admin_key": "fixture",
                    }
                ]
            }
        }
        with (
            patch.object(web_app, "account_email_set", return_value=set()),
            patch.object(web_app, "_known_mailbox_bindings", return_value={}),
            patch.object(web_app, "reconcile_registered_mailbox_states") as reconcile,
            patch.object(web_app, "create_mailbox", return_value=mailbox) as create,
        ):
            reserved = web_app._rebind_reserve_target(
                cfg,
                target_email="",
                provider_type="fastmail_api",
            )

        self.assertEqual(reserved, mailbox)
        reconcile.assert_called_once_with(
            cfg["mail"], provider_type="fastmail_api"
        )
        create.assert_called_once_with(
            cfg["mail"],
            username=None,
            provider_type="fastmail_api",
            allow_used=False,
        )

    def test_rebind_rejects_unsupported_target_provider(self):
        with self.assertRaisesRegex(ValueError, "fastmail_api"):
            self.web_app._rebind_reserve_target(
                {"mail": {}},
                target_email="",
                provider_type="outlook_token",
            )


if __name__ == "__main__":
    unittest.main()

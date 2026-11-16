from __future__ import annotations

import unittest
from unittest.mock import patch

import web.routes.accounts_rebind as rebind_routes


class RebindBatchHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        import web_app

        self.web_app = web_app
        with web_app._REBIND_RESERVATION_LOCK:
            web_app._REBIND_RESERVATIONS.clear()

    def tearDown(self) -> None:
        with self.web_app._REBIND_RESERVATION_LOCK:
            self.web_app._REBIND_RESERVATIONS.clear()

    def test_source_parser_accepts_lines_delimiters_and_pair_syntax(self):
        web_app = self.web_app
        body = web_app.AccountRebindPreflightRequest(
            keys=["account_id:fixture"],
            emails=["first@example.com"],
            text="first@example.com----first-target@example.net\nsecond@example.com -> second-target@example.net\nthird@example.com",
        )
        values, invalid = web_app._rebind_parse_sources(body)
        self.assertEqual(
            values,
            [
                "account_id:fixture",
                "first@example.com",
                "second@example.com",
                "third@example.com",
            ],
        )
        self.assertEqual(invalid, [])
        targets, pairs, target_invalid = web_app._rebind_parse_target_specs(body)
        self.assertEqual(targets, [])
        self.assertEqual(
            pairs,
            {
                "first@example.com": "first-target@example.net",
                "second@example.com": "second-target@example.net",
            },
        )
        self.assertEqual(target_invalid, [])

    def test_batch_reservation_release_releases_each_child_once(self):
        web_app = self.web_app
        first = {"provider": "maddy_webui", "address": "one-target@example.net"}
        second = {"provider": "fastmail_api", "address": "two-target@example.net"}
        reservation = {"items": [{"mailbox": first}, {"mailbox": second}]}
        with patch.object(web_app, "release_mailbox") as release:
            web_app._rebind_release_reservation_item(reservation)
        self.assertEqual(release.call_count, 2)
        self.assertEqual({call.args[0]["address"] for call in release.call_args_list}, {first["address"], second["address"]})

    def test_register_supersedes_only_overlapping_batch_source(self):
        web_app = self.web_app
        old_one = {"provider": "maddy_webui", "address": "old-one@example.net"}
        old_two = {"provider": "maddy_webui", "address": "old-two@example.net"}
        replacement = {"provider": "maddy_webui", "address": "replacement@example.net"}
        with web_app._REBIND_RESERVATION_LOCK:
            web_app._REBIND_RESERVATIONS["old"] = {
                "items": [
                    {"record_key": "email:one@example.com", "mailbox": old_one},
                    {"record_key": "email:two@example.com", "mailbox": old_two},
                ]
            }
        with patch.object(web_app, "release_mailbox") as release:
            web_app._rebind_register_reservation(
                "new",
                {
                    "items": [
                        {"record_key": "email:two@example.com", "mailbox": replacement}
                    ]
                },
            )
        with web_app._REBIND_RESERVATION_LOCK:
            self.assertNotIn("old", web_app._REBIND_RESERVATIONS)
            self.assertIn("new", web_app._REBIND_RESERVATIONS)
        self.assertEqual(release.call_count, 2)
        self.assertEqual(
            {call.args[0]["address"] for call in release.call_args_list},
            {old_one["address"], old_two["address"]},
        )

    def test_batch_source_loader_uses_email_index_once(self):
        web_app = self.web_app
        record = {
            "email": "one@example.com",
            "account_id": "one",
            "password": "saved-password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
        with (
            patch.object(web_app, "_load_accounts", web_app._DEFAULT_LOAD_ACCOUNTS),
            patch.object(web_app, "load_account_records_by_emails", return_value=[record]) as load,
            patch.object(web_app, "load_account_records_by_keys") as load_by_key,
        ):
            result = rebind_routes._rebind_load_records(["one@example.com"])

        self.assertEqual(result, [record])
        load.assert_called_once_with(["one@example.com"], path=web_app.ACCOUNTS_FILE)
        load_by_key.assert_not_called()


if __name__ == "__main__":
    unittest.main()

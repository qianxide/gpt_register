from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import threading
import time
from types import SimpleNamespace

import web.routes.accounts_rebind as rebind_routes
import web.routes.mailcom as mailcom_routes


def test_mailcom_settings_save_keeps_password_out_of_panel_config(monkeypatch):
    import web_app

    cfg = {"mail": {"providers": []}}
    imported = []
    written = []

    class FakeProvider:
        def __init__(self, entry, _conf):
            self.accounts = web_app.parse_fastmail_accounts(str(entry.get("accounts") or ""))
            self.api_base = str(entry.get("api_base") or "")
            self.admin_key = "fixture-admin-key"
            self.alias_domain = str(entry.get("alias_domain") or "random")

        def import_account(self, email, password):
            imported.append((email, password))

        def available_domains(self, _email):
            return ["mail.com", "email.com"]

        def close(self):
            pass

    @contextmanager
    def edit_config():
        yield cfg
        written.append(cfg)

    monkeypatch.setattr(web_app, "MailcomApiProvider", FakeProvider)
    monkeypatch.setattr(web_app, "_read_config", lambda: cfg)
    monkeypatch.setattr(web_app, "_edit_config", edit_config)

    result = asyncio.run(
        mailcom_routes.mailcom_settings_save(
            mailcom_routes.MailcomSettingsRequest(
                email="Owner@mail.com",
                password="private-password",
                api_base="http://127.0.0.1:18787",
                admin_key="fixture-admin-key",
                alias_domain="mail.com",
                alias_domains=["mail.com", "email.com"],
                alias_name_mode="name_digits",
                alias_name_length=12,
            )
        )
    )

    assert result["login_ok"] is True
    assert imported == [("owner@mail.com", "private-password")]
    assert written
    saved = written[0]["mail"]["providers"][0]
    assert saved["accounts"] == "owner@mail.com"
    assert saved["alias_name_mode"] == "name_digits"
    assert saved["alias_name_length"] == 12
    assert "password" not in saved


def test_mailcom_bulk_import_parses_lines_verifies_accounts_and_redacts_passwords(monkeypatch):
    import web_app

    cfg = {"mail": {"providers": []}}
    imported = []
    written = []

    class FakeProvider:
        def __init__(self, entry, _conf):
            self.accounts = web_app.parse_fastmail_accounts(str(entry.get("accounts") or ""))
            self.api_base = str(entry.get("api_base") or "")
            self.admin_key = "fixture-admin-key"
            self.alias_domain = str(entry.get("alias_domain") or "random")

        def import_account(self, email, password):
            imported.append((email, password))

        def available_domains(self, email):
            if email == "failed@mail.com":
                raise RuntimeError("upstream login failed")
            return ["mail.com", "email.com"]

        def close(self):
            pass

    @contextmanager
    def edit_config():
        yield cfg
        written.append(cfg)

    monkeypatch.setattr(web_app, "MailcomApiProvider", FakeProvider)
    monkeypatch.setattr(web_app, "_read_config", lambda: cfg)
    monkeypatch.setattr(web_app, "_edit_config", edit_config)

    result = asyncio.run(
        mailcom_routes.mailcom_accounts_import_batch(
            mailcom_routes.MailcomBulkImportRequest(
                text=(
                    "owner@mail.com----private-one\n"
                    "failed@mail.com---private-two\n"
                    "not-a-record\n"
                ),
                api_base="http://127.0.0.1:18787",
                admin_key="fixture-admin-key",
                alias_domain="mail.com",
                alias_name_mode="random",
                alias_name_length=20,
                concurrency=2,
            )
        )
    )

    assert result["login_ok"] is True
    assert result["bulk"] == {
        "requested": 3,
        "saved": 2,
        "verified": 1,
        "failed": 1,
        "invalid": 1,
        "concurrency": 2,
        "results": [
            {"email": "owner@mail.com", "ok": True, "domains": ["mail.com", "email.com"]},
            {"email": "failed@mail.com", "ok": False, "error": "upstream login failed"},
        ],
        "invalid_lines": [{"line": 3, "error": "格式应为：邮箱---密码"}],
    }
    assert set(imported) == {
        ("owner@mail.com", "private-one"),
        ("failed@mail.com", "private-two"),
    }
    assert written
    saved = written[0]["mail"]["providers"][0]
    assert saved["accounts"] == "owner@mail.com\nfailed@mail.com"
    assert saved["alias_name_mode"] == "random"
    assert saved["alias_name_length"] == 20
    assert saved["import_concurrency"] == 2
    assert result["bulk"]["concurrency"] == 2
    assert "private-one" not in json.dumps(saved)
    assert "private-two" not in json.dumps(result)


def test_mailcom_bulk_import_runs_logins_concurrently(monkeypatch):
    import web_app

    cfg = {"mail": {"providers": []}}
    lock = threading.Lock()
    active = 0
    peak = 0
    written = []

    class FakeProvider:
        def __init__(self, entry, _conf):
            self.accounts = web_app.parse_fastmail_accounts(str(entry.get("accounts") or ""))
            self.api_base = str(entry.get("api_base") or "")
            self.admin_key = "fixture-admin-key"
            self.alias_domain = str(entry.get("alias_domain") or "random")

        def import_account(self, _email, _password):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1

        def available_domains(self, _email):
            return ["mail.com"]

        def close(self):
            pass

    @contextmanager
    def edit_config():
        yield cfg
        written.append(cfg)

    monkeypatch.setattr(web_app, "MailcomApiProvider", FakeProvider)
    monkeypatch.setattr(web_app, "_read_config", lambda: cfg)
    monkeypatch.setattr(web_app, "_edit_config", edit_config)

    result = asyncio.run(
        mailcom_routes.mailcom_accounts_import_batch(
            mailcom_routes.MailcomBulkImportRequest(
                text="one@mail.com----one\ntwo@mail.com----two\nthree@mail.com----three\nfour@mail.com----four",
                api_base="http://127.0.0.1:18787",
                admin_key="fixture-admin-key",
                concurrency=3,
            )
        )
    )

    assert peak >= 2
    assert result["bulk"]["concurrency"] == 3
    assert written[0]["mail"]["providers"][0]["import_concurrency"] == 3


def test_public_settings_reports_bound_unbound_and_available_alias_counts(monkeypatch):
    import web_app

    class FakeProvider:
        def __init__(self, _entry, _conf):
            self.accounts = ["owner@mail.com"]
            self.api_base = "http://127.0.0.1:18787"
            self.admin_key = "fixture-admin-key"
            self.alias_domain = "random"
            self.alias_domains = []
            self.alias_name_mode = "random"
            self.alias_name_length = 17

        def fetch_accounts(self, force=False):
            return [{"email": "owner@mail.com", "aliasCount": 9, "aliasLimit": 9}]

        def alias_usage_summary(self, email):
            assert email == "owner@mail.com"
            return {"total": 9, "bound": 4, "unbound": 5, "available": 3}

        def close(self):
            pass

    monkeypatch.setattr(web_app, "MailcomApiProvider", FakeProvider)
    result = mailcom_routes._public_settings({"mail": {}})

    assert result["accounts"] == [{
        "email": "owner@mail.com",
        "alias_count": 9,
        "alias_limit": 9,
        "bound_alias_count": 4,
        "unbound_alias_count": 5,
        "available_alias_count": 3,
    }]


def test_public_settings_stays_configured_when_the_adapter_has_no_accounts(monkeypatch):
    import web_app

    class FakeProvider:
        def __init__(self, _entry, _conf):
            self.accounts = []
            self.api_base = "http://127.0.0.1:18787"
            self.admin_key = "fixture-admin-key"
            self.alias_domain = "random"
            self.alias_domains = []
            self.alias_name_mode = "random"
            self.alias_name_length = 17

        def fetch_accounts(self, force=False):
            return []

        def close(self):
            pass

    monkeypatch.setattr(web_app, "MailcomApiProvider", FakeProvider)
    result = mailcom_routes._public_settings({"mail": {}})

    assert result["configured"] is True
    assert result["accounts"] == []


def test_rebind_targets_only_returns_available_unbound_rows(monkeypatch):
    import web_app

    rows = [
        {
            "email": "free@mail.com",
            "provider_type": "mailcom_api",
            "provider_ref": "mailcom_api#3",
            "provider_label": "mail.com 域名邮箱",
            "enabled": True,
        },
        {
            "email": "used@mail.com",
            "provider_type": "mailcom_api",
            "provider_ref": "mailcom_api#3",
            "provider_label": "mail.com 域名邮箱",
            "enabled": True,
        },
        {
            "email": "disabled@mail.com",
            "provider_type": "mailcom_api",
            "provider_ref": "mailcom_api#3",
            "provider_label": "mail.com 域名邮箱",
            "enabled": False,
        },
    ]
    state = {
        "used@mail.com": {"state": "used"},
    }
    monkeypatch.setattr(web_app, "_read_config", lambda: {"mail": {}})
    monkeypatch.setattr(web_app, "_mailbox_items", lambda *_args, **_kwargs: rows)
    monkeypatch.setattr(web_app, "_load_state_file", lambda: state)
    monkeypatch.setattr(web_app, "account_email_set", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(web_app, "_known_mailbox_bindings", lambda: {})

    with web_app._REBIND_RESERVATION_LOCK:
        web_app._REBIND_RESERVATIONS.clear()
    result = asyncio.run(
        rebind_routes.account_rebind_targets(
            provider_type="mailcom_api",
            provider_ref="mailcom_api#3",
            limit=50,
        )
    )

    assert result["count"] == 1
    assert result["items"] == [
        {
            "email": "free@mail.com",
            "provider_type": "mailcom_api",
            "provider_ref": "mailcom_api#3",
            "label": "mail.com 域名邮箱",
            "state": "available",
        }
    ]

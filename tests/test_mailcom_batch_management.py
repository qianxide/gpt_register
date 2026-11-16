from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from threading import Barrier, Lock

import pytest
from pydantic import ValidationError

import mail
import web.routes.mailcom as routes


@pytest.mark.parametrize("request_type", [
    routes.MailcomAliasBatchDeleteRequest,
    routes.MailcomAccountBatchRemoveRequest,
])
@pytest.mark.parametrize("concurrency", [0, 11])
def test_batch_concurrency_bounds(request_type, concurrency):
    with pytest.raises(ValidationError):
        request_type(emails=["one@mail.com"], concurrency=concurrency)


def test_alias_deletion_overlaps_mailboxes_with_isolated_sessions_and_limit(monkeypatch):
    import web_app

    accounts = [f"owner{i}@mail.com" for i in range(4)]
    rendezvous = Barrier(2, timeout=3)
    lock = Lock()
    active = peak = 0
    providers = []
    cleared = []

    class Provider:
        def __init__(self):
            self.calls = []
            self.closed = False
            providers.append(self)

        def delete_aliases(self, email):
            nonlocal active, peak
            self.calls.append(email)
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                rendezvous.wait()
                if email == accounts[1]:
                    raise RuntimeError("mailbox login failed")
                return {
                    "requested": 1, "deleted": [f"{email.split('@')[0]}-alias@dr.com"],
                    "failed": [], "remaining": 0,
                }
            finally:
                with lock:
                    active -= 1

        def close(self):
            self.closed = True

    monkeypatch.setattr(web_app, "_read_config", lambda: {})
    monkeypatch.setattr(routes, "_provider_for", lambda _cfg: Provider())
    monkeypatch.setattr(routes, "_public_settings", lambda *_args, **_kw: {"ok": True})
    monkeypatch.setattr(routes, "_remove_local_mailcom_state", lambda email, addresses: cleared.append(email) or len(addresses))

    report = asyncio.run(routes.mailcom_aliases_batch_delete(
        routes.MailcomAliasBatchDeleteRequest(emails=accounts + [accounts[0].upper()], concurrency=2),
    ))["alias_batch_deletion"]

    assert peak == 2
    assert len(providers) == 4
    assert all(len(provider.calls) == 1 and provider.closed for provider in providers)
    assert [item["email"] for item in report["results"]] == accounts
    assert report["concurrency"] == 2
    assert report["deleted_aliases"] == 3
    assert report["failed_accounts"] == [accounts[1]]
    assert set(cleared) == set(accounts) - {accounts[1]}


def test_batch_remove_keeps_failed_unselected_and_new_accounts(monkeypatch, tmp_path):
    import web_app

    cfg = {"mail": {"providers": [{
        "type": "mailcom_api", "accounts": "one@mail.com\ntwo@mail.com\nkeep@mail.com",
        "alias_domains": ["dr.com"], "admin_key": "fixture-key",
    }]}}
    rendezvous = Barrier(2, timeout=3)
    providers = []
    saved = []
    monkeypatch.setattr(mail, "STATE_FILE", tmp_path / "mailbox-state.json")
    mail._save_state({
        "one-child@dr.com": {"provider": "mailcom_api", "account": "one@mail.com", "state": "used"},
        "two-child@dr.com": {"provider": "mailcom_api", "account": "two@mail.com", "state": "available"},
        "keep-child@dr.com": {"provider": "mailcom_api", "account": "keep@mail.com", "state": "used"},
    })

    class Provider:
        def __init__(self):
            self.closed = False
            providers.append(self)

        def delete_account(self, email):
            rendezvous.wait()
            if email == "two@mail.com":
                raise RuntimeError("adapter temporarily offline")
            return {"email": email, "remote_aliases_retained": True}

        def delete_aliases(self, _email):
            pytest.fail("removing a saved primary must not delete remote aliases")

        def close(self):
            self.closed = True

    @contextmanager
    def edit_config():
        cfg["mail"]["providers"][0]["accounts"] += "\nnew@mail.com"
        yield cfg
        saved.append(deepcopy(cfg))

    monkeypatch.setattr(web_app, "_read_config", lambda: cfg)
    monkeypatch.setattr(web_app, "_edit_config", edit_config)
    monkeypatch.setattr(routes, "_provider_for", lambda _cfg: Provider())
    monkeypatch.setattr(routes, "_public_settings", lambda *_args, **_kw: {"ok": True})

    report = asyncio.run(routes.mailcom_accounts_batch_remove(
        routes.MailcomAccountBatchRemoveRequest(
            emails=["ONE@mail.com", "two@mail.com", "one@mail.com"], concurrency=2,
        ),
    ))["account_batch_removal"]

    assert report["removed_accounts"] == ["one@mail.com"]
    assert report["failed_accounts"] == ["two@mail.com"]
    assert report["concurrency"] == 2
    assert report["remote_aliases_retained"] is True
    assert report["removed_local_aliases"] == 1
    assert len(saved) == 1
    assert saved[0]["mail"]["providers"][0]["accounts"] == "two@mail.com\nkeep@mail.com\nnew@mail.com"
    assert saved[0]["mail"]["providers"][0]["alias_domains"] == ["dr.com"]
    assert set(mail._load_state()) == {"two-child@dr.com", "keep-child@dr.com"}
    assert len(providers) == 2 and all(provider.closed for provider in providers)


def test_empty_success_set_keeps_every_local_alias(monkeypatch, tmp_path):
    monkeypatch.setattr(mail, "STATE_FILE", tmp_path / "mailbox-state.json")
    original = {
        "one@dr.com": {"provider": "mailcom_api", "account": "owner@mail.com", "state": "used"},
        "two@dr.com": {"provider": "mailcom_api", "account": "owner@mail.com", "state": "available"},
    }
    mail._save_state(original)
    before = mail._load_state()
    assert routes._remove_local_mailcom_state("owner@mail.com", set()) == 0
    assert mail._load_state() == before
    assert routes._remove_local_mailcom_state("owner@mail.com", {"one@dr.com"}) == 1
    assert set(mail._load_state()) == {"two@dr.com"}

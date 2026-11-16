"""Offline contract tests for the independent mail.com Node adapter."""

from __future__ import annotations

from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re
from threading import Barrier
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
import requests

import mail
import mail.mailcom as mailcom
from mail.mailcom import MailcomApiError, MailcomApiProvider


ACCOUNT = "owner@example.test"
OTHER_ACCOUNT = "other@example.test"
DOMAIN = "mail.example.test"
ADDRESS = f"retained@{DOMAIN}"


class FakeResponse:
    def __init__(self, data, status=200):
        self.data = data
        self.status_code = status
        self.text = str(data)

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return deepcopy(self.data)


class FakeAdapter:
    """Small stateful HTTP fixture: unsupported endpoints always fail."""

    def __init__(self):
        self.accounts = [{"email": ACCOUNT}]
        self.domains = defaultdict(lambda: [DOMAIN])
        self.aliases = defaultdict(list)
        self.calls = []
        self.responses = defaultdict(deque)
        self.created = 0
        self.closed = False

    def request(self, method, url, **kwargs):
        path = urlparse(url).path
        self.calls.append((method, path, deepcopy(kwargs)))
        route = (method, path)
        if self.responses[route]:
            response = self.responses[route].popleft()
            if callable(response):
                response = response(kwargs)
            if isinstance(response, Exception):
                raise response
            return response if isinstance(response, FakeResponse) else FakeResponse(response)
        params = kwargs.get("params", {})
        body = kwargs.get("json", {})
        if route == ("GET", "/api/v1/accounts"):
            return FakeResponse({"accounts": self.accounts})
        if route == ("GET", "/api/v1/alias-domains"):
            return FakeResponse({"domains": self.domains[params["account_email"]]})
        if route == ("GET", "/api/v1/aliases"):
            return FakeResponse({"items": self.aliases[params["account_email"]]})
        if route == ("POST", "/api/v1/aliases"):
            self.created += 1
            address = body.get("address") or f"created{self.created}@{body['domain']}"
            item = alias(address, account=body["account_email"], key=f"fixture-key-{self.created}")
            self.aliases[body["account_email"]].append(item)
            return FakeResponse({"item": item})
        if route == ("POST", "/api/v1/import"):
            self.accounts.extend({"email": row["email"]} for row in body.get("accounts", []))
            return FakeResponse({"ok": True})
        if method == "DELETE" and path.startswith("/api/v1/accounts/"):
            email = path.rsplit("/", 1)[-1]
            self.accounts = [row for row in self.accounts if row.get("email", "").lower() != email.replace("%40", "@").lower()]
            return FakeResponse({"ok": True, "email": email.replace("%40", "@"), "aliases_removed": 0})
        if route == ("GET", "/api/v1/code/wait"):
            return FakeResponse({"code": None, "messages": []})
        if route == ("GET", "/api/v1/messages"):
            return FakeResponse({"messages": []})
        raise AssertionError(f"Unexpected adapter endpoint: {route}")

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def close(self):
        self.closed = True

    def calls_to(self, method, path):
        return [kwargs for verb, route, kwargs in self.calls if (verb, route) == (method, path)]


class Clock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.001, seconds)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(mail, "STATE_FILE", tmp_path / "mail-state.json")
    monkeypatch.setattr(mail, "_known_mailbox_bindings", lambda: {})
    with mailcom._ACTIVE_RESERVATIONS_LOCK:
        mailcom._ACTIVE_RESERVATIONS.clear()
    for env_name in ("MAILCODE_ADMIN_KEY", "MAILCODE_API_URL", "FASTMAIL_ADMIN_KEY"):
        monkeypatch.delenv(env_name, raising=False)

    def reject_network(*args, **kwargs):
        raise AssertionError("These tests are offline; use the fake adapter")

    monkeypatch.setattr(requests.sessions.Session, "request", reject_network)


@pytest.fixture
def adapter(monkeypatch):
    adapter = FakeAdapter()
    monkeypatch.setattr(mail, "_http_session", lambda: adapter)
    clock = Clock()
    monkeypatch.setattr(mailcom, "time", SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep))
    return adapter


def provider(adapter, **overrides):
    entry = {
        "type": "mailcom_api", "provider_ref": "mailcom_api#2",
        "api_base": "http://adapter.example.test", "admin_key": "fixture-admin-key",
        "accounts": ACCOUNT, "alias_domain": DOMAIN,
    }
    entry.update(overrides)
    return MailcomApiProvider(entry, mail._make_config({
        "request_timeout": 8, "wait_timeout": 1, "wait_chunk": 5,
        "wait_interval": 0.2, "user_agent": "offline-mailcom-test",
    }))


def alias(address=ADDRESS, *, account=ACCOUNT, key="fixture-alias-key", status="active"):
    return {"address": address, "primaryEmail": account, "key": key, "status": status}


def seed(instance, address=ADDRESS, *, state="available", **metadata):
    entry = {
        "state": state, "provider": instance.name, "provider_ref": instance.provider_ref,
        "api_base": instance.api_base, "account": ACCOUNT, "key": "fixture-alias-key",
        "domain": address.rsplit("@", 1)[1],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    entry.update(metadata)
    store = mail._load_state()
    store[address] = entry
    mail._save_state(store)
    return entry


def mailbox(provider, address=ADDRESS):
    seed(provider, address, state="used")
    return provider._mailbox_payload(address, ACCOUNT, "fixture-alias-key")


def test_defaults_do_not_borrow_fastmail_credentials(adapter, monkeypatch):
    monkeypatch.setenv("FASTMAIL_ADMIN_KEY", "unrelated-fixture-key")
    instance = MailcomApiProvider({}, mail._make_config({}))
    assert instance.name == "mailcom_api"
    assert instance.api_base == "http://127.0.0.1:18787"
    assert instance.alias_domain == instance.default_domain == "random"
    assert instance.alias_name_mode == "random"
    assert instance.alias_name_length == 17
    assert instance.admin_key == ""
    with pytest.raises(MailcomApiError, match="MAILCODE_ADMIN_KEY"):
        instance.fetch_accounts()
    assert adapter.calls == []


def test_configuration_environment_precedence_and_account_parsing(adapter, monkeypatch):
    monkeypatch.setenv("MAILCODE_API_URL", "http://local.example.test/prefix/")
    monkeypatch.setenv("MAILCODE_ADMIN_KEY", "default-fixture")
    monkeypatch.setenv("MAILCOM_TEST_KEY", "selected-fixture")
    config = {"admin_key_env": "MAILCOM_TEST_KEY", "default_domain": DOMAIN.upper(),
              "accounts": f"# owners\n{ACCOUNT.upper()}\n{OTHER_ACCOUNT}\n{ACCOUNT}\n"}
    instance = MailcomApiProvider(config, mail._make_config({}))
    assert instance.api_base == "http://local.example.test/prefix"
    assert instance.admin_key == "selected-fixture"
    assert instance.accounts == [ACCOUNT, OTHER_ACCOUNT]
    assert instance.alias_domain == DOMAIN
    config.update(admin_key="explicit-fixture", api_base="http://explicit.example.test/")
    explicit = MailcomApiProvider(config, mail._make_config({}))
    assert explicit.admin_key == "explicit-fixture"
    assert explicit.api_base == "http://explicit.example.test"
    assert MailcomApiProvider({}, mail._make_config({})).admin_key == "default-fixture"
    assert MailcomApiProvider({"admin_key_env": "MAILCOM_MISSING_KEY"}, mail._make_config({})).admin_key == ""


def test_management_accounts_are_sanitized_and_cached(adapter):
    adapter.accounts = [{"email": ACCOUNT.upper(), "password": "fixture-password",
                         "cookies": {"session": "private-fixture"}, "hasPassword": True}]
    instance = provider(adapter, accounts="")
    assert instance.fetch_accounts() == [{"email": ACCOUNT, "hasPassword": True}]
    rows = instance.fetch_accounts()
    rows[0]["email"] = "mutated@example.test"
    assert instance.fetch_accounts()[0]["email"] == ACCOUNT
    instance.fetch_accounts(force=True)
    calls = adapter.calls_to("GET", "/api/v1/accounts")
    assert len(calls) == 2
    assert calls[0]["headers"]["X-API-Key"] == "fixture-admin-key"


def test_import_uses_login_timeout_invalidates_cache_and_preserves_password(adapter):
    instance = provider(adapter, accounts="")
    instance.fetch_accounts()
    assert instance.import_account(OTHER_ACCOUNT.upper(), "  fixture-password  ") is None
    call = adapter.calls_to("POST", "/api/v1/import")[0]
    assert call["json"] == {"accounts": [{"email": OTHER_ACCOUNT, "password": "  fixture-password  "}]}
    assert call["timeout"] >= 120
    assert instance.fetch_accounts() == [{"email": ACCOUNT}, {"email": OTHER_ACCOUNT}]
    assert "fixture-password" not in repr(instance.__dict__.get("_accounts_cache"))


def test_import_error_invalidates_cache_redacts_password_and_keeps_saved_account(adapter):
    instance = provider(adapter, accounts="")
    instance.fetch_accounts()

    def saved_then_failed(kwargs):
        adapter.accounts.append({"email": OTHER_ACCOUNT})
        return FakeResponse({"error": "domain login failed for fixture-password"}, 502)

    adapter.responses["POST", "/api/v1/import"].append(saved_then_failed)
    with pytest.raises(MailcomApiError, match="domain login failed") as caught:
        instance.import_account(OTHER_ACCOUNT, "fixture-password")
    assert "fixture-password" not in str(caught.value)
    assert instance.fetch_accounts()[-1] == {"email": OTHER_ACCOUNT}
    assert not any(method == "DELETE" for method, _, _ in adapter.calls)


def test_delete_account_uses_local_adapter_endpoint_and_invalidates_cache(adapter):
    instance = provider(adapter, accounts="")
    instance.fetch_accounts()
    result = instance.delete_account(ACCOUNT.upper())
    assert result == {"ok": True, "email": ACCOUNT, "aliases_removed": 0}
    call = adapter.calls_to("DELETE", f"/api/v1/accounts/{ACCOUNT.replace('@', '%40')}")[0]
    assert call["headers"]["X-API-Key"] == "fixture-admin-key"
    assert instance.fetch_accounts() == []


def test_creation_creates_when_no_reusable_remote_alias_exists(adapter):
    adapter.aliases[ACCOUNT] = []
    instance = provider(adapter, accounts="", alias_domain="random")
    created = instance.create_mailbox()
    assert created["address"] != ADDRESS
    assert created["provider"] == "mailcom_api"
    assert created["account"] == ACCOUNT
    assert created["address"] == created["login_address"] == created["base_address"]
    calls = adapter.calls_to("POST", "/api/v1/aliases")
    assert calls[0]["json"] == {"account_email": ACCOUNT, "domain": DOMAIN}
    assert adapter.calls_to("GET", "/api/v1/aliases")
    saved = mail._load_state()[created["address"]]
    assert saved["state"] == "in_use"
    assert saved["provider"] == instance.name
    assert saved["provider_ref"] == instance.provider_ref
    assert saved["api_base"] == instance.api_base
    assert saved["account"] == ACCOUNT
    assert saved["key"] == created["alias_key"]
    assert saved["domain"] == DOMAIN
    assert all("pool" not in path and "capacity" not in path for _, path, _ in adapter.calls)


def test_automatic_allocation_reuses_untracked_active_remote_alias_before_create(adapter, monkeypatch):
    instance = provider(adapter)
    free_address = f"unused-alias@{DOMAIN}"
    bound_address = f"bound-alias@{DOMAIN}"
    busy_address = f"busy-alias@{DOMAIN}"
    adapter.aliases[ACCOUNT] = [
        alias(free_address, key="free-key"),
        alias(bound_address, key="bound-key"),
        alias(busy_address, key="busy-key"),
    ]
    monkeypatch.setattr(mail, "_known_mailbox_bindings", lambda: {bound_address: "registered_account"})
    seed(instance, busy_address, state="used")

    created = instance.create_mailbox()

    assert created["address"] == free_address
    assert created["alias_key"] == "free-key"
    assert not adapter.calls_to("POST", "/api/v1/aliases")
    assert adapter.calls_to("GET", "/api/v1/aliases")[0]["params"] == {"account_email": ACCOUNT}
    saved = mail._load_state()[free_address]
    assert saved["state"] == "in_use"
    assert saved["account"] == ACCOUNT
    assert saved["key"] == "free-key"


def test_existing_alias_is_reused_before_domain_discovery_timeout(adapter, monkeypatch):
    occupied_address = f"occupied-alias@{DOMAIN}"
    free_address = f"later-alias@{DOMAIN}"
    adapter.aliases[ACCOUNT] = [alias(occupied_address)]
    adapter.aliases[OTHER_ACCOUNT] = [alias(free_address, account=OTHER_ACCOUNT, key="later-key")]
    monkeypatch.setattr(mail, "_known_mailbox_bindings", lambda: {occupied_address: "registered_account"})

    def domain_response(kwargs):
        if kwargs["params"]["account_email"] == OTHER_ACCOUNT:
            raise requests.Timeout("domain discovery timed out")
        return FakeResponse({"domains": [DOMAIN]})

    adapter.responses["GET", "/api/v1/alias-domains"].append(domain_response)
    adapter.responses["POST", "/api/v1/aliases"].append(
        FakeResponse({"error": "alias limit reached"}, 409)
    )
    instance = provider(adapter, accounts=f"{ACCOUNT}\n{OTHER_ACCOUNT}")

    created = instance.create_mailbox()

    assert created["address"] == free_address
    assert created["account"] == OTHER_ACCOUNT
    post_calls = adapter.calls_to("POST", "/api/v1/aliases")
    assert len(post_calls) == 1
    assert post_calls[0]["json"]["account_email"] == ACCOUNT
    assert not any(
        kwargs.get("params", {}).get("account_email") == OTHER_ACCOUNT
        for kwargs in adapter.calls_to("GET", "/api/v1/alias-domains")
    )


def test_automatic_allocation_reclaims_orphaned_in_use_remote_alias(adapter):
    instance = provider(adapter)
    free_address = f"orphaned-alias@{DOMAIN}"
    adapter.aliases[ACCOUNT] = [alias(free_address, key="orphaned-key")]
    seed(instance, free_address, state="in_use")

    created = instance.create_mailbox()

    assert created["address"] == free_address
    assert created["alias_key"] == "orphaned-key"
    assert not adapter.calls_to("POST", "/api/v1/aliases")


def test_alias_usage_summary_classifies_bound_unbound_and_available(adapter, monkeypatch):
    instance = provider(adapter)
    free_address = f"free-summary@{DOMAIN}"
    bound_address = f"bound-summary@{DOMAIN}"
    used_address = f"used-summary@{DOMAIN}"
    adapter.aliases[ACCOUNT] = [
        alias(free_address, key="free-summary-key"),
        alias(bound_address, key="bound-summary-key"),
        alias(used_address, key="used-summary-key"),
    ]
    seed(instance, used_address, state="used")
    monkeypatch.setattr(mail, "_known_mailbox_bindings", lambda: {bound_address: "registered_account"})

    assert instance.alias_usage_summary(ACCOUNT) == {
        "total": 3,
        "bound": 1,
        "unbound": 2,
        "available": 1,
    }


def test_name_digits_alias_creation_uses_configured_length(adapter):
    instance = provider(adapter, alias_name_mode="name_digits", alias_name_length=12)

    created = instance.create_mailbox()

    local_part = created["address"].split("@", 1)[0]
    assert len(local_part) == 12
    assert re.fullmatch(r"[a-z]+[0-9]{5,}", local_part)
    assert adapter.calls_to("POST", "/api/v1/aliases")[0]["json"]["address"] == created["address"]


def test_random_alias_creation_uses_custom_length(adapter):
    instance = provider(adapter, alias_name_mode="random", alias_name_length=11)

    created = instance.create_mailbox()

    local_part = created["address"].split("@", 1)[0]
    assert len(local_part) == 11
    assert re.fullmatch(r"m[a-f0-9]{10}", local_part)
    assert adapter.calls_to("POST", "/api/v1/aliases")[0]["json"]["address"] == created["address"]


def test_configured_accounts_do_not_expand_to_discovered_accounts(adapter):
    adapter.accounts = [{"email": OTHER_ACCOUNT}]
    instance = provider(adapter)
    created = instance.create_mailbox()
    assert created["account"] == ACCOUNT
    assert not adapter.calls_to("GET", "/api/v1/accounts")


def test_available_domains_and_random_selection_use_supported_values(adapter, monkeypatch):
    second_domain = "second.example.test"
    adapter.domains[ACCOUNT] = [DOMAIN.upper(), second_domain, DOMAIN]
    instance = provider(adapter, alias_domain="random")
    assert instance.available_domains(ACCOUNT.upper()) == [DOMAIN, second_domain]
    monkeypatch.setattr(mailcom.secrets, "choice", lambda values: values[-1])
    created = instance.create_mailbox()
    assert created["address"].endswith("@" + second_domain)
    assert adapter.calls_to("GET", "/api/v1/alias-domains")[0]["params"] == {"account_email": ACCOUNT}


def test_multiple_configured_alias_domains_selects_available_intersection(adapter):
    second_domain = "second.example.test"
    adapter.domains[ACCOUNT] = [DOMAIN, second_domain]
    instance = provider(adapter, alias_domain="random", alias_domains=[DOMAIN, second_domain])
    created = instance.create_mailbox()
    assert created["address"].endswith(("@" + DOMAIN, "@" + second_domain))
    assert adapter.calls_to("POST", "/api/v1/aliases")[0]["json"]["domain"] in {DOMAIN, second_domain}


def test_unsupported_domain_tries_next_owner_without_creating_on_first(adapter):
    adapter.domains[ACCOUNT] = ["different.example.test"]
    instance = provider(adapter, accounts=f"{ACCOUNT}\n{OTHER_ACCOUNT}")
    created = instance.create_mailbox()
    assert created["account"] == OTHER_ACCOUNT
    assert len(adapter.calls_to("POST", "/api/v1/aliases")) == 1


def test_unsupported_domain_reports_failure_without_post(adapter):
    instance = provider(adapter, alias_domain="not-offered.example.test")
    with pytest.raises(MailcomApiError, match="no supported alias domain"):
        instance.create_mailbox()
    assert not adapter.calls_to("POST", "/api/v1/aliases")


def test_explicit_new_address_uses_optional_address_field(adapter):
    instance = provider(adapter)
    created = instance.create_mailbox(ADDRESS.upper())
    assert created["address"] == ADDRESS
    assert adapter.calls_to("POST", "/api/v1/aliases")[0]["json"] == {
        "account_email": ACCOUNT, "domain": DOMAIN, "address": ADDRESS,
    }


def test_explicit_existing_active_alias_is_resolved_exactly(adapter):
    adapter.aliases[ACCOUNT] = [alias(f"retained+other@{DOMAIN}"), alias()]
    instance = provider(adapter, alias_domain="different.example.test")
    created = instance.create_mailbox(ADDRESS.upper())
    assert created["address"] == ADDRESS
    assert not adapter.calls_to("POST", "/api/v1/aliases")
    assert adapter.calls_to("GET", "/api/v1/aliases")[0]["params"] == {"account_email": ACCOUNT}


@pytest.mark.parametrize("changes,match", [
    ({"primaryEmail": OTHER_ACCOUNT}, "owner mismatch"),
    ({"primaryEmail": ""}, "owner mismatch"),
    ({"address": f"other@{DOMAIN}"}, "address mismatch"),
    ({"address": "alias@other.example.test"}, "address mismatch"),
    ({"key": ""}, "missing key"),
    ({"status": "pending"}, "not active"),
    ({"status": "unconfirmed"}, "not active"),
])
def test_created_alias_validation_retains_upstream_and_reserves_nothing(adapter, changes, match):
    invalid = dict(alias(), **changes)
    adapter.responses["POST", "/api/v1/aliases"].append({"item": invalid})
    instance = provider(adapter)
    with pytest.raises(MailcomApiError, match=match):
        instance.create_mailbox(ADDRESS)
    assert mail._load_state() == {}
    assert not any(method == "DELETE" for method, _, _ in adapter.calls)


@pytest.mark.parametrize("status", ["pending", "unconfirmed", ""])
def test_explicit_inactive_alias_is_not_recreated(adapter, status):
    adapter.aliases[ACCOUNT] = [alias(status=status)]
    with pytest.raises(MailcomApiError, match="not active"):
        provider(adapter).create_mailbox(ADDRESS)
    assert not adapter.calls_to("POST", "/api/v1/aliases")


def test_explicit_alias_from_wrong_owner_is_rejected(adapter):
    adapter.aliases[ACCOUNT] = [alias(account=OTHER_ACCOUNT)]
    with pytest.raises(MailcomApiError, match="owner mismatch"):
        provider(adapter).create_mailbox(ADDRESS)
    assert mail._load_state() == {}


def test_reuse_survives_restart_and_shared_state_updates(adapter):
    first = provider(adapter)
    original = first.create_mailbox()
    mail._set_state(original["address"], "available", "released locally")
    first.close()
    second = provider(adapter)
    reused = second.create_mailbox()
    assert reused == original
    assert len(adapter.calls_to("POST", "/api/v1/aliases")) == 1
    saved = mail._load_state()[original["address"]]
    assert saved["state"] == "in_use"
    assert saved["provider"] == "mailcom_api"


@pytest.mark.parametrize("metadata", [
    {"provider": "fastmail_api"}, {"provider_ref": "mailcom_api#99"},
    {"api_base": "http://different.example.test"}, {"account": OTHER_ACCOUNT},
    {"domain": "different.example.test"}, {"state": "used"},
    {"state": "failed"}, {"state": "token_invalid"}, {"state": "bound"},
    {"state": "in_use"}, {"state": "cooldown"},
])
def test_auto_reuse_excludes_foreign_or_unavailable_reservations(adapter, metadata):
    instance = provider(adapter)
    seed(instance, **metadata)
    adapter.aliases[ACCOUNT] = [alias()]
    if metadata.get("state") == "in_use":
        instance._mark_reservation_active(ADDRESS)
    created = instance.create_mailbox(allow_used=True)
    assert created["address"] != ADDRESS
    assert mail._load_state()[ADDRESS]["state"] == metadata.get("state", "available")


def test_auto_reuse_accepts_untracked_remote_alias_and_respects_domain(adapter):
    instance = provider(adapter)
    adapter.aliases[ACCOUNT] = [alias()]
    created = instance.create_mailbox()
    assert created["address"] == ADDRESS
    assert mail._load_state()[ADDRESS]["state"] == "in_use"
    wrong_domain_address = "prior@previous.example.test"
    adapter.aliases[ACCOUNT] = [alias(wrong_domain_address)]
    assert instance.create_mailbox()["address"] != wrong_domain_address


@pytest.mark.parametrize("reason", ["registered_account", "account_trash", "cdk"])
def test_bound_alias_is_never_automatically_reused_and_explicit_reauth_restores_used(adapter, monkeypatch, reason):
    instance = provider(adapter)
    seed(instance)
    adapter.aliases[ACCOUNT] = [alias()]
    monkeypatch.setattr(mail, "_known_mailbox_bindings", lambda: {ADDRESS: reason})
    assert instance.create_mailbox()["address"] != ADDRESS
    with pytest.raises(MailcomApiError, match="unavailable"):
        instance.create_mailbox(ADDRESS)
    restored = instance.create_mailbox(ADDRESS, allow_used=True)
    assert restored["alias_key"] == "fixture-alias-key"
    assert mail._load_state()[ADDRESS]["_prev_state"] == "used"
    mail._release_state(ADDRESS)
    assert mail._load_state()[ADDRESS]["state"] == "used"


def test_used_alias_requires_explicit_allow_used_and_survives_cancel(adapter):
    instance = provider(adapter)
    seed(instance, state="used")
    adapter.aliases[ACCOUNT] = [alias()]
    with pytest.raises(MailcomApiError, match="unavailable"):
        instance.create_mailbox(ADDRESS)
    original = instance.create_mailbox(ADDRESS, allow_used=True)
    mail._release_state(ADDRESS, any_state=True)
    assert mail._load_state()[ADDRESS]["state"] == "used"
    assert instance.create_mailbox(ADDRESS, allow_used=True) == original
    assert not adapter.calls_to("POST", "/api/v1/aliases")


def test_explicit_lookup_rejects_foreign_state_even_when_used_allowed(adapter):
    instance = provider(adapter)
    seed(instance, state="used", provider_ref="mailcom_api#foreign")
    with pytest.raises(MailcomApiError, match="provider_ref mismatch"):
        instance.create_mailbox(ADDRESS, allow_used=True)
    assert not adapter.calls


def test_stale_used_reauth_reservation_is_not_available_for_new_allocation(adapter):
    instance = provider(adapter)
    seed(instance, state="in_use", _prev_state="used", updated_at="2000-01-01T00:00:00+00:00")
    adapter.aliases[ACCOUNT] = [alias()]
    assert instance.create_mailbox()["address"] != ADDRESS


def test_atomic_reservation_rejects_concurrent_claims(adapter):
    instance = provider(adapter)
    seed(instance)
    barrier = Barrier(2)

    def reserve():
        barrier.wait(timeout=5)
        try:
            instance._reserve_address(ADDRESS, account=ACCOUNT, alias_key="fixture-alias-key", local_only=True)
            return "reserved"
        except MailcomApiError:
            return "busy"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: reserve(), range(2)))
    assert sorted(results) == ["busy", "reserved"]
    assert mail._load_state()[ADDRESS]["state"] == "in_use"


def test_reuse_rechecks_bindings_and_state_at_reservation_time(adapter, monkeypatch):
    instance = provider(adapter)
    seed(instance)
    adapter.aliases[ACCOUNT] = [alias()]
    original_request = instance._aliases

    def bound_during_lookup(account):
        rows = original_request(account)
        monkeypatch.setattr(mail, "_known_mailbox_bindings", lambda: {ADDRESS: "registered_account"})
        return rows

    monkeypatch.setattr(instance, "_aliases", bound_during_lookup)
    assert instance.create_mailbox()["address"] != ADDRESS


def test_pool_summary_counts_only_local_matching_available_reservations(adapter, monkeypatch):
    instance = provider(adapter)
    seed(instance)
    seed(instance, f"used@{DOMAIN}", state="used")
    bound = f"bound@{DOMAIN}"
    seed(instance, bound)
    seed(instance, f"foreign@{DOMAIN}", provider="fastmail_api")
    monkeypatch.setattr(mail, "_known_mailbox_bindings", lambda: {bound: "account_trash"})
    adapter.aliases[ACCOUNT] = [alias(f"untracked{n}@{DOMAIN}") for n in range(10)]
    assert instance.pool_summary() == {"total": 3, "available": 1, "accounts": 1, "error": ""}
    assert not adapter.calls_to("GET", "/api/v1/aliases")


def test_pool_summary_reports_discovery_failure_without_fabricated_capacity(adapter):
    adapter.responses["GET", "/api/v1/accounts"].append(FakeResponse({"error": "offline"}, 503))
    summary = provider(adapter, accounts="").pool_summary()
    assert summary["total"] == summary["available"] == summary["accounts"] == 0
    assert "503" in summary["error"]


@pytest.mark.parametrize("metadata", [
    {"provider": "fastmail_api"}, {"provider_ref": "mailcom_api#foreign"},
    {"api_base": "http://wrong.example.test"}, {"account": OTHER_ACCOUNT},
    {"alias_key": "wrong-fixture-key"}, {"login_address": f"retained+other@{DOMAIN}"},
])
def test_inbox_rejects_mixed_owner_address_or_credentials(adapter, metadata):
    instance = provider(adapter)
    box = mailbox(instance)
    box.update(metadata)
    with pytest.raises(MailcomApiError):
        instance.fetch_recent_messages(box)
    assert not adapter.calls


def test_inbox_restores_credentials_from_persisted_exact_alias(adapter):
    instance = provider(adapter)
    address = f"retained+exact@{DOMAIN}"
    mailbox(instance, address)
    box = {"address": address}
    assert instance._alias_credentials(box) == (address, ACCOUNT, "fixture-alias-key")
    instance.fetch_recent_messages(box)
    params = adapter.calls_to("GET", "/api/v1/messages")[0]["params"]
    assert params["email"] == address
    assert params["account"] == ACCOUNT
    assert params["key"] == "fixture-alias-key"


def test_wait_contract_after_exclusions_and_no_admin_header(adapter):
    instance = provider(adapter)
    box = mailbox(instance)
    now = datetime.now(timezone.utc)
    box.update(_received_after=(now - timedelta(seconds=4)).isoformat(),
               _code_not_before=now - timedelta(seconds=2), _seen_otp_codes=["111111"])
    adapter.responses["GET", "/api/v1/code/wait"].append({
        "code": "654321", "received_at": now.isoformat(), "mail": {"subject": "Verification code"},
    })
    assert instance.wait_for_code(box) == "654321"
    call = adapter.calls_to("GET", "/api/v1/code/wait")[0]
    params = call["params"]
    assert params["email"] == ADDRESS
    assert params["account"] == ACCOUNT
    assert params["key"] == "fixture-alias-key"
    assert params["after"] == int((now - timedelta(seconds=2)).timestamp()) - 2
    assert params["exclude"] == "111111"
    assert params["max_age_seconds"] == 600
    assert 1 <= params["timeout"] <= 60
    assert call["timeout"] >= params["timeout"] + 15
    assert "X-API-Key" not in call["headers"]
    assert not {"sync", "wait", "interval"}.intersection(params)
    assert box["_seen_otp_codes"] == ["111111", "654321"]


@pytest.mark.parametrize("timestamp_location", ["envelope", "mail", "camel_mail"])
def test_wait_rejects_stale_code_without_timestampless_duplicate(adapter, timestamp_location):
    instance = provider(adapter)
    box = mailbox(instance)
    now = datetime.now(timezone.utc)
    box["_code_not_before"] = now - timedelta(seconds=1)
    stale = {"code": "111111", "mail": {"subject": "Verification code", "text": "Code 111111"}}
    stamp = (now - timedelta(minutes=1)).isoformat()
    if timestamp_location == "envelope":
        stale["received_at"] = stamp
    else:
        stale["mail"]["received_at" if timestamp_location == "mail" else "receivedAt"] = stamp
    adapter.responses["GET", "/api/v1/code/wait"].extend([
        stale, {"code": "222222", "received_at": now.isoformat()},
    ])
    assert instance.wait_for_code(box) == "222222"


def test_wait_enforces_max_age_without_transaction_boundary(adapter):
    instance = provider(adapter)
    box = mailbox(instance)
    now = datetime.now(timezone.utc)
    adapter.responses["GET", "/api/v1/code/wait"].extend([
        {"code": "111111", "received_at": (now - timedelta(minutes=11)).isoformat()},
        {"code": "222222", "received_at": now.isoformat()},
    ])
    assert instance.wait_for_code(box) == "222222"


def test_wait_filters_alerts_non_codes_and_seen_codes(adapter):
    instance = provider(adapter)
    box = mailbox(instance)
    box["_seen_otp_codes"] = ["111111"]
    adapter.responses["GET", "/api/v1/code/wait"].extend([
        {"code": "222222", "mail": {"subject": "New sign-in security alert"}},
        {"messages": [{"code": "short", "subject": "Verification code"},
                      {"code": "111111", "subject": "Verification code"},
                      {"code": "333333", "subject": "Verification code"}]},
    ])
    assert instance.wait_for_code(box) == "333333"
    params = adapter.calls_to("GET", "/api/v1/code/wait")[1]["params"]
    assert "222222" in params["exclude"]
    assert "333333" in box["_seen_otp_codes"]


def test_transient_wait_error_reads_messages_cache_with_same_freshness_params(adapter):
    instance = provider(adapter)
    box = mailbox(instance)
    box["_seen_otp_codes"] = ["111111"]
    box["_code_not_before"] = datetime.now(timezone.utc) - timedelta(seconds=3)
    adapter.responses["GET", "/api/v1/code/wait"].append(FakeResponse({}, 503))
    adapter.responses["GET", "/api/v1/messages"].append({"messages": [
        {"subject": "Verification code", "text": "Your code is 654321", "received_at": datetime.now(timezone.utc).isoformat()},
    ]})
    assert instance.wait_for_code(box) == "654321"
    call = adapter.calls_to("GET", "/api/v1/messages")[0]
    assert call["params"]["email"] == ADDRESS
    assert call["params"]["exclude"] == "111111"
    assert "after" in call["params"]
    assert "_last_code_wait_error" not in box


def test_stopped_wait_makes_no_requests_and_keeps_state(adapter):
    instance = provider(adapter)
    box = mailbox(instance)
    before = mail._load_state()
    assert instance.wait_for_code(box, stop_check=lambda: True) is None
    assert adapter.calls == []
    assert mail._load_state() == before


def test_stop_during_long_poll_discards_returned_code_and_skips_cache(adapter):
    instance = provider(adapter)
    box = mailbox(instance)
    stopped = False

    def stop_then_code(kwargs):
        nonlocal stopped
        stopped = True
        return {"code": "654321"}

    adapter.responses["GET", "/api/v1/code/wait"].append(stop_then_code)
    assert instance.wait_for_code(box, stop_check=lambda: stopped) is None
    assert "654321" not in box["_seen_otp_codes"]
    assert not adapter.calls_to("GET", "/api/v1/messages")


def test_wait_timeout_and_error_do_not_release_or_delete_aliases(adapter):
    instance = provider(adapter)
    created = instance.create_mailbox()
    before = mail._load_state()
    assert instance.wait_for_code(created) is None
    assert mail._load_state() == before
    adapter.responses["GET", "/api/v1/code/wait"].append(FakeResponse({"error": "bad fixture-alias-key"}, 404))
    with pytest.raises(MailcomApiError, match="invalid key"):
        instance.wait_for_code(created)
    assert mail._load_state() == before
    assert len(adapter.aliases[ACCOUNT]) == 1
    assert not any(method == "DELETE" for method, _, _ in adapter.calls)


@pytest.mark.parametrize("state", ["used", "failed"])
def test_result_updates_keep_alias_metadata_and_upstream_alias(adapter, state):
    instance = provider(adapter)
    created = instance.create_mailbox()
    mail._set_state(created["address"], state, "rebind result")
    restored = provider(adapter)
    assert restored._alias_credentials({"address": created["address"]}) == (
        created["address"], ACCOUNT, created["alias_key"],
    )
    assert adapter.aliases[ACCOUNT][0]["address"] == created["address"]
    assert not any(method == "DELETE" for method, _, _ in adapter.calls)


def test_fetch_recent_messages_returns_panel_format_for_adapter_and_graph_items(adapter):
    instance = provider(adapter)
    box = mailbox(instance)
    now = datetime.now(timezone.utc)
    adapter.responses["GET", "/api/v1/messages"].append({"messages": [
        {"id": "node-message", "subject": "Verification code", "from": "sender@example.test",
         "text": "Your code is 654321", "html": "<p>654321</p>", "receivedAt": now.isoformat()},
        {"id": "graph-message", "subject": "Verification code", "from": {"emailAddress": {
            "name": "Sender", "address": "sender@example.test"}}, "bodyPreview": "Your code is 111111",
         "receivedDateTime": now.isoformat()},
    ]})
    messages = instance.fetch_recent_messages(box)
    assert messages[0] == {
        "provider": "mailcom_api", "mailbox": ADDRESS, "message_id": "node-message",
        "subject": "Verification code", "sender": "sender@example.test", "recipients": [ADDRESS],
        "text_content": "Your code is 654321", "html_content": "<p>654321</p>", "received_at": now,
    }
    assert messages[1]["sender"] == "Sender <sender@example.test>"
    assert messages[1]["text_content"] == "Your code is 111111"
    assert messages[1]["received_at"] == now


@pytest.mark.parametrize("field,value", [("email", f"retained+wrong@{DOMAIN}"), ("account", OTHER_ACCOUNT)])
def test_inbox_envelope_must_match_exact_address_and_owner(adapter, field, value):
    instance = provider(adapter)
    box = mailbox(instance)
    adapter.responses["GET", "/api/v1/messages"].append({field: value, "messages": []})
    with pytest.raises(MailcomApiError, match="mismatch"):
        instance.fetch_recent_messages(box)


@pytest.mark.parametrize("route,payload", [
    ("/api/v1/accounts", {"items": []}),
    ("/api/v1/alias-domains", {"domains": [{"name": DOMAIN}]}),
    ("/api/v1/aliases", {"aliases": [alias()]}),
])
def test_wrong_api_shapes_are_rejected_without_creating_aliases(adapter, route, payload):
    instance = provider(adapter, accounts="" if route.endswith("accounts") else ACCOUNT)
    adapter.responses["GET", route].append(payload)
    with pytest.raises(MailcomApiError):
        instance.create_mailbox(ADDRESS if route.endswith("aliases") else None)
    assert not adapter.calls_to("POST", "/api/v1/aliases")


def test_adapter_error_responses_and_request_exceptions_redact_credentials(adapter):
    instance = provider(adapter)
    box = mailbox(instance)
    adapter.responses["GET", "/api/v1/messages"].append(
        requests.ConnectionError("GET /messages?key=fixture-alias-key&email=box failed"))
    with pytest.raises(MailcomApiError) as caught:
        instance.fetch_recent_messages(box)
    assert "fixture-alias-key" not in str(caught.value)
    adapter.responses["GET", "/api/v1/alias-domains"].append(
        FakeResponse({"error": "bad fixture-admin-key"}, 403))
    with pytest.raises(MailcomApiError) as caught:
        instance.available_domains(ACCOUNT)
    assert "fixture-admin-key" not in str(caught.value)


def test_close_closes_only_local_http_session(adapter):
    instance = provider(adapter)
    instance.close()
    assert adapter.closed
    assert not adapter.calls

"""Pool routes and panel workflow contracts. HTTP/SMS calls are always mocked."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
import json
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
import urllib.request

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from common import sms_number_pool
from codex_oauth import imported_sms
from web.routes import codex_oauth as oauth
from web.routes import sms_pool as routes


SECRET = "private-fixture-receive-key"
RECEIVE_URL = f"https://168smsapi.com/sms/{SECRET}?token={SECRET}"
IMPORT_TEXT = f"2026-09-19|12025550123|{RECEIVE_URL}"
SUMMARY = {"total": 8, "available": 3, "reserved": 1, "cooling": 2, "exhausted": 2}


def unexpected_http(*args, **kwargs):
    pytest.fail("Unexpected network or paid SMS provider access")


@pytest.fixture(autouse=True)
def isolated_io(monkeypatch):
    import requests
    from curl_cffi import requests as curl_requests

    original_connect = socket.socket.connect

    def local_event_loop_connect(sock, address):
        # Windows asyncio builds its internal socketpair over loopback.
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect(sock, address)
        unexpected_http()

    monkeypatch.setattr(socket, "create_connection", unexpected_http)
    monkeypatch.setattr(socket.socket, "connect", local_event_loop_connect)
    monkeypatch.setattr(urllib.request, "urlopen", unexpected_http)
    monkeypatch.setattr(requests.Session, "request", unexpected_http)
    monkeypatch.setattr(curl_requests.Session, "request", unexpected_http)
    monkeypatch.setattr(oauth, "_PanelSmsBowerClient", unexpected_http)
    monkeypatch.setattr(oauth, "_PanelFirefoxSmsClient", unexpected_http)
    monkeypatch.setattr(oauth, "_smsbower_request", unexpected_http)
    monkeypatch.setattr(oauth, "check_sms_balance", unexpected_http)


@pytest.fixture
def pool(monkeypatch):
    store = Mock(spec=["list_numbers", "import_text", "delete", "summary"])
    store.summary.return_value = dict(SUMMARY)
    store.list_numbers.return_value = {
        "items": [{"id": 7, "phone": "+12025550123", "country": "US", "uses": 1,
                   "max_uses": 3, "remaining_uses": 2, "status": "cooling",
                   "cooldown_remaining": 3600}],
        "page": 1, "page_size": 20, "total": 1, "summary": dict(SUMMARY),
        "max_uses": 3, "cooldown_seconds": 3600,
    }
    store.import_text.return_value = {
        "imported": 1, "duplicates": 1, "invalid": 1,
        "errors": [{"line": 3, "error": "日期应为 YYYY-MM-DD"}], "summary": dict(SUMMARY),
    }
    store.delete.return_value = {"deleted": 1, "skipped": 1, "summary": dict(SUMMARY)}
    # Routes resolve this function lazily inside their asyncio worker threads.
    monkeypatch.setattr(sms_number_pool, "get_sms_pool", Mock(return_value=store))
    return store


@pytest.fixture
def config(monkeypatch, tmp_path):
    cfg = {"sms_code": {
        "enabled": True, "provider": "imported_pool", "api_key": SECRET,
        "firefox_api_password": SECRET, "firefox_api_token": SECRET,
        "country_order": "6=123|456", "max_price": "0.25", "max_phone_attempts": 1,
    }}

    @contextmanager
    def edit():
        yield cfg

    monkeypatch.setattr(oauth, "_state", lambda: SimpleNamespace(
        _read_config=lambda: deepcopy(cfg), _edit_config=edit, _mask_secret=lambda value: "***",
    ))
    state = deepcopy(oauth._STATE)
    state.update(running=False, mode="codex", batch_id="", logs=[], results=[], items=[], counts={})
    monkeypatch.setattr(oauth, "_STATE", state)
    monkeypatch.setattr(oauth, "_LOCK", threading.Lock())
    monkeypatch.setattr(oauth, "_STOP", threading.Event())
    monkeypatch.setattr(oauth, "_RT_HISTORY_PATH", tmp_path / "rt-history.json")
    monkeypatch.setattr(oauth, "CLIPROXY_AUTHS_DIR", tmp_path / "relay")
    monkeypatch.setattr(oauth, "totp_from_secret", lambda *args, **kwargs: "111111")
    monkeypatch.setattr(oauth, "exchange_code", Mock(return_value={"refresh_token": "mock-rt"}))
    monkeypatch.setattr(oauth, "_persist_sms_tokens", Mock(return_value={"base_hash": "missing"}))
    monkeypatch.setattr(oauth, "_mark_relay_synced", Mock())
    monkeypatch.setattr(oauth, "_sync_cliproxy_auth", Mock(return_value="mock-auth.json"))
    return cfg


@pytest.fixture
def api(pool, config):
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(oauth.router)
    with TestClient(app) as client:
        yield client


def test_list_preserves_ui_metadata_and_passes_pagination_filters(api, pool):
    response = api.get("/api/sms-pool", params={
        "page": 2, "page_size": 10, "status": "cooling", "q": " 202555 ",
    })
    assert response.status_code == 200
    pool.list_numbers.assert_called_once_with(page=2, page_size=10, status="cooling", q="202555")
    assert response.json() == {"ok": True, **pool.list_numbers.return_value}


def test_list_defaults(api, pool):
    assert api.get("/api/sms-pool").status_code == 200
    pool.list_numbers.assert_called_once_with(page=1, page_size=20, status="all", q="")


@pytest.mark.parametrize("params", [
    {"page": 0}, {"page": -1}, {"page": 1_000_001}, {"page": "bad"},
    {"page_size": 0}, {"page_size": 101}, {"status": "unknown"}, {"q": "x" * 201},
])
def test_invalid_query_never_reaches_store(api, pool, params):
    assert api.get("/api/sms-pool", params=params).status_code == 422
    pool.list_numbers.assert_not_called()


def test_import_returns_partial_results_and_never_echoes_urls(api, pool):
    pool.import_text.return_value["errors"].append({
        "line": 4, "error": f"bad address {RECEIVE_URL} api_key={SECRET}",
    })
    response = api.post("/api/sms-pool/import", json={"text": IMPORT_TEXT})
    assert response.status_code == 200
    pool.import_text.assert_called_once_with(text=IMPORT_TEXT)
    result = response.json()
    assert (result["imported"], result["duplicates"], result["invalid"]) == (1, 1, 1)
    assert result["errors"][0] == {"line": 3, "error": "日期应为 YYYY-MM-DD"}
    assert result["errors"][1]["line"] == 4
    assert SECRET not in response.text
    assert "https://" not in response.text


def test_list_filters_private_fields_recursively(api, pool):
    pool.list_numbers.return_value["items"][0].update(
        receive_url=RECEIVE_URL, api_key=SECRET, lease_token=SECRET,
        metadata={"url": RECEIVE_URL, "token": SECRET, "label": "美国"},
    )
    response = api.get("/api/sms-pool")
    assert response.status_code == 200
    assert SECRET not in response.text
    item = response.json()["items"][0]
    assert item["uses"] == 1
    assert item["metadata"] == {"label": "美国"}


@pytest.mark.parametrize("body, ids, exhausted", [
    ({"ids": [7, 9]}, [7, 9], False),
    ({"exhausted_only": True}, None, True),
    ({"ids": [], "exhausted_only": True}, [], True),
    ({"ids": [7], "exhausted_only": True}, [7], True),
])
def test_delete_selection_and_exhausted_filter(api, pool, body, ids, exhausted):
    response = api.request("DELETE", "/api/sms-pool", json=body)
    assert response.status_code == 200
    pool.delete.assert_called_once_with(ids=ids, exhausted_only=exhausted)
    assert response.json() == {"ok": True, **pool.delete.return_value}


@pytest.mark.parametrize("body, status", [
    ({}, 400), ({"ids": []}, 400), ({"ids": None}, 400),
    ({"ids": [True]}, 422), ({"ids": ["7"]}, 422), ({"ids": [1.5]}, 422),
    ({"ids": [0]}, 422), ({"ids": [-1]}, 422), ({"ids": [2**63]}, 422),
    ({"ids": list(range(1, routes.MAX_DELETE_IDS + 2))}, 422),
    ({"exhausted_only": "true"}, 422), ({"exhausted_ony": True}, 422),
])
def test_delete_validation_never_deletes_implicitly(api, pool, body, status):
    assert api.request("DELETE", "/api/sms-pool", json=body).status_code == status
    pool.delete.assert_not_called()


@pytest.mark.parametrize("body", [
    {}, {"text": ""}, {"text": 123}, {"text": [SECRET]},
    {"text": {"url": RECEIVE_URL}}, {"text": IMPORT_TEXT, "extra": SECRET},
])
def test_import_validation_drops_input_from_errors(api, pool, body):
    response = api.post("/api/sms-pool/import", json=body)
    assert response.status_code == 422
    assert SECRET not in response.text
    assert all("input" not in error for error in response.json()["detail"])
    pool.import_text.assert_not_called()


def test_import_character_limit_does_not_echo_input(api, pool):
    response = api.post("/api/sms-pool/import", json={
        "text": IMPORT_TEXT + "x" * routes.MAX_IMPORT_CHARS,
    })
    assert response.status_code == 422
    assert len(response.content) < 1000
    assert SECRET not in response.text
    pool.import_text.assert_not_called()


@pytest.mark.parametrize("method, path", [("POST", "/api/sms-pool/import"), ("DELETE", "/api/sms-pool")])
@pytest.mark.parametrize("with_length", [False, True])
def test_body_byte_limit_covers_chunked_requests(api, pool, monkeypatch, method, path, with_length):
    monkeypatch.setattr(routes, "MAX_BODY_BYTES", 64)
    headers = {"content-type": "application/json"}
    if with_length:
        headers["content-length"] = "1000"
        content = b"{}"
    else:
        content = iter([b'{"text":"', b"x" * 65, b'"}'])
    response = api.request(method, path, content=content, headers=headers)
    assert response.status_code == 413
    pool.import_text.assert_not_called()
    pool.delete.assert_not_called()


@pytest.mark.parametrize("length", ["invalid", "-1"])
def test_bad_content_length(api, pool, length):
    response = api.post("/api/sms-pool/import", content=b"{}", headers={"content-length": length})
    assert response.status_code == 400
    pool.import_text.assert_not_called()


def test_malformed_json_does_not_echo_receive_url(api, pool):
    response = api.post("/api/sms-pool/import", content=f'{{"text":"{RECEIVE_URL}',
                        headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert SECRET not in response.text
    pool.import_text.assert_not_called()


@pytest.mark.parametrize("method, path, operation, body", [
    ("GET", "/api/sms-pool", "list_numbers", None),
    ("POST", "/api/sms-pool/import", "import_text", {"text": IMPORT_TEXT}),
    ("DELETE", "/api/sms-pool", "delete", {"ids": [7]}),
])
@pytest.mark.parametrize("error_type, status", [(sms_number_pool.SmsPoolError, 400), (RuntimeError, 503)])
def test_storage_errors_never_expose_urls_or_keys(api, pool, method, path, operation, body, error_type, status):
    getattr(pool, operation).side_effect = error_type(RECEIVE_URL)
    response = api.request(method, path, json=body)
    assert response.status_code == status
    assert SECRET not in response.text


def test_all_storage_work_including_factory_runs_off_event_loop(pool, monkeypatch):
    event_thread = threading.get_ident()
    worker_threads = []

    def checked(value):
        def call(*args, **kwargs):
            worker_threads.append(threading.get_ident())
            assert threading.get_ident() != event_thread
            with pytest.raises(RuntimeError, match="no running event loop"):
                asyncio.get_running_loop()
            return value
        return call

    monkeypatch.setattr(sms_number_pool, "get_sms_pool", checked(pool))
    for name in ("list_numbers", "import_text", "delete"):
        operation = getattr(pool, name)
        operation.side_effect = checked(operation.return_value)

    async def requests():
        await routes.sms_pool_list()
        await routes.sms_pool_import(routes.SmsPoolImportRequest(text=IMPORT_TEXT))
        await routes.sms_pool_delete(routes.SmsPoolDeleteRequest(ids=[7]))

    asyncio.run(requests())
    assert len(worker_threads) == 6


@pytest.mark.parametrize("provider", ["imported_pool", " IMPORTED_POOL "])
def test_config_save_load_normalizes_pool_and_keeps_credentials_private(api, config, provider):
    response = api.put("/api/sms-config", json={
        "enabled": True, "provider": provider, "country_order": "6", "max_price": "0.50",
    })
    assert response.status_code == 200
    assert config["sms_code"]["provider"] == "imported_pool"
    assert config["sms_code"]["api_key"] == SECRET
    public = api.get("/api/sms-config")
    assert public.status_code == 200
    assert SECRET not in public.text
    assert public.json()["provider"] == "imported_pool"
    assert public.json()["configured"] is True
    assert public.json()["country_order"] == "US"
    assert public.json()["max_price"] == ""


def test_unknown_provider_is_rejected_before_config_write(api, config):
    before = deepcopy(config)
    assert api.put("/api/sms-config", json={"provider": "imported_pol"}).status_code == 422
    assert config == before
    assert api.get("/api/sms-countries", params={"provider": "imported_pol"}).status_code == 400


@pytest.mark.parametrize("saved_provider", ["smsbower", "firefox"])
def test_switch_to_pool_and_back_preserves_all_supplier_settings(api, config, saved_provider):
    config["sms_code"].update(
        provider=saved_provider, enabled=False, timeout_seconds=420, poll_interval_ms=8000,
        service="dr-custom", firefox_api_name="fixture-user", firefox_docking_code="eng-1096-old",
        firefox_did="old", firefox_item_id="1096", firefox_country_order="eng",
        firefox_docking_code_history=["eng-1096-old"],
    )
    before = deepcopy(config["sms_code"])
    response = api.put("/api/sms-config", json={"provider": "imported_pool", "enabled": True})
    assert response.status_code == 200
    assert config["sms_code"] == {**before, "provider": "imported_pool", "enabled": True}
    pool_settings = oauth._effective_sms_config(config)
    assert pool_settings["country_order"] == "US"
    assert pool_settings["max_price"] == ""
    # Per-run switching also uses the original provider's stored parameters.
    override = oauth._effective_sms_config(config, {"provider": saved_provider})
    assert override["country_order"] == before["country_order"]
    assert override["max_price"] == (before["max_price"] if saved_provider == "smsbower" else "")
    assert api.put("/api/sms-config", json={"provider": saved_provider}).status_code == 200
    assert config["sms_code"] == {**before, "enabled": True}


def test_pool_full_body_ignores_provider_specific_values(api, config):
    before = deepcopy(config["sms_code"])
    body = oauth.SmsConfigUpdate(
        provider="imported_pool", enabled=True, api_key="new-unused-key", country_order="US", max_price="",
        firefox_api_password="new-unused-password", firefox_docking_code="usa-1096-new",
    ).model_dump()
    assert api.put("/api/sms-config", json=body).status_code == 200
    common_fields = {"enabled", "provider", "poll_interval_ms", "timeout_seconds", "max_phone_attempts"}
    assert config["sms_code"] == {**before, **{key: body[key] for key in common_fields}}


def test_partial_provider_update_preserves_secrets_and_unset_fields(api, config):
    config["sms_code"].update(provider="firefox", firefox_api_name="fixture-user", timeout_seconds=420,
                              firefox_docking_code="eng-1096-old", firefox_docking_code_history=["eng-1096-old"])
    before = deepcopy(config["sms_code"])
    response = api.put("/api/sms-config", json={
        "api_key": " ", "firefox_api_password": "", "firefox_api_token": "", "timeout_seconds": 240,
    })
    assert response.status_code == 200
    assert config["sms_code"] == {**before, "timeout_seconds": 240}
    assert api.put("/api/sms-config", json={}).status_code == 200
    assert config["sms_code"] == {**before, "timeout_seconds": 240}
    assert api.put("/api/sms-config", json={"firefox_docking_code": "usa-1096-new"}).status_code == 200
    assert config["sms_code"]["firefox_docking_code_history"] == ["usa-1096-new", "eng-1096-old"]
    assert config["sms_code"]["firefox_api_name"] == "fixture-user"


@pytest.mark.parametrize("saved_provider", ["smsbower", "imported_pool", " IMPORTED_POOL "])
def test_run_override_fixes_country_and_price_without_mutating_config(config, saved_provider):
    config["sms_code"]["provider"] = saved_provider
    before = deepcopy(config)
    options = oauth.SmsRunOptions(provider="imported_pool", country_order="6=123", max_price="0.2",
                                  max_phone_attempts=3, timeout_seconds=60)
    effective = oauth._effective_sms_config(config, options)
    assert config == before
    assert effective["provider"] == "imported_pool"
    assert effective["country_order"] == "US"
    assert effective["max_price"] == ""
    assert oauth._country_specs(effective) == [("US", [], None)]
    public = oauth._safe_sms_settings(effective)
    assert public["configured"] is True
    assert public["country_order"] == "US"
    assert public["price_cap_supported"] is False
    assert public["max_price"] == ""
    assert (public["max_phone_attempts"], public["timeout_seconds"]) == (3, 60)
    assert SECRET not in json.dumps(public)


def test_safe_pool_settings_without_effective_config(config):
    public = oauth._safe_sms_settings(config["sms_code"])
    assert public["country_order"] == "US"
    assert public["max_price"] == ""
    assert public["price_cap_supported"] is False


@pytest.mark.parametrize("explicit_provider", [False, True])
def test_countries_read_pool_summary_before_paid_cache(api, pool, monkeypatch, explicit_provider):
    cache = {"version": 2, "at": time.time(),
             "payload": {"provider": "smsbower", "items": [{"id": "6"}]}}
    monkeypatch.setattr(oauth, "_SMS_COUNTRIES_CACHE", cache)
    monkeypatch.setattr(oauth, "_sms_client", unexpected_http)
    response = api.get("/api/sms-countries", params={"provider": "imported_pool"} if explicit_provider else {})
    assert response.status_code == 200
    result = response.json()
    assert result["provider"] == "imported_pool"
    assert result["items"] == [{"id": "US", "name": "美国", "calling": "+1",
                               "count": 3, "price": "", "tiers": []}]
    assert cache["payload"]["provider"] == "smsbower"
    pool.summary.assert_called_once_with()


@pytest.mark.parametrize("available", [0, 3])
def test_config_test_returns_availability_without_client_or_balance(api, pool, monkeypatch, available):
    pool.summary.return_value["available"] = available
    monkeypatch.setattr(oauth, "_sms_client", unexpected_http)
    response = api.post("/api/sms-config/test")
    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is True
    assert result["provider"] == "imported_pool"
    assert result["provider_label"] == "导入号码池"
    assert result["message"].startswith(f"可用 {available}")
    assert result["summary"] == pool.summary.return_value
    assert result["checked_at"]
    assert "balance" not in result
    pool.summary.assert_called_once_with()


def test_summary_failures_are_private_and_never_fall_back(api, pool):
    pool.summary.side_effect = RuntimeError(RECEIVE_URL)
    countries = api.get("/api/sms-countries", params={"provider": "imported_pool"})
    check = api.post("/api/sms-config/test")
    assert countries.status_code == 503
    assert check.json()["ok"] is False
    assert SECRET not in countries.text + check.text


@pytest.mark.parametrize("enabled", [False, True])
def test_sms_client_dispatches_actual_pool_branch_without_vendor_key(monkeypatch, enabled):
    factory = Mock()
    monkeypatch.setattr(imported_sms, "ImportedPoolSmsClient", factory)
    settings = oauth.SmsSettings.from_mapping({"provider": "imported_pool", "enabled": enabled})
    client = oauth._sms_client(settings, [("6", ["123"], "0.25")])
    if enabled:
        factory.assert_called_once_with(settings, timeout=30.0)
        assert client is factory.return_value
    else:
        assert client is None
        factory.assert_not_called()


def test_sms_client_pool_constructor_failure_does_not_fall_back(monkeypatch):
    monkeypatch.setattr(imported_sms, "ImportedPoolSmsClient", Mock(side_effect=oauth.SmsError("pool failed")))
    settings = oauth.SmsSettings.from_mapping({"provider": "imported_pool", "enabled": True, "api_key": SECRET})
    with pytest.raises(oauth.SmsError, match="pool failed"):
        oauth._sms_client(settings)


def mock_phone_flow(monkeypatch, failure=""):
    events = []
    stop = threading.Event()

    def step(name):
        events.append(name)
        if failure == name:
            raise oauth.SmsError(f"fixture {name} failed")

    class Protocol:
        def __init__(self, *args, **kwargs):
            step("login")
            self.verifier = "mock-pkce"
            self.session = SimpleNamespace(close=lambda: step("close"))

        def bootstrap(self): pass
        def submit_email(self): pass
        def verify_password(self, password): pass
        def mfa_id(self): return "mock-mfa"
        def issue_mfa_challenge(self, challenge): pass
        def verify_mfa(self, *args): return SimpleNamespace(json=lambda: {"page": {"type": "add_phone"}})
        def send_phone(self, phone): step("send")

        def submit_phone_otp(self, code):
            assert code == "654321"
            step("otp")
            events.append("otp_accepted")

        def select_workspace(self, *args, **kwargs):
            step("workspace")
            return "http://localhost/?code=mock-oauth-code"

    class PoolClient:
        def acquire(self):
            step("acquire")
            return oauth.SmsActivation("lease-id", "+12025550123", "US", SECRET,
                                       label="美国", provider="imported_pool")

        def wait_code(self, activation, *, cancel_event):
            assert cancel_event is stop
            step("wait")
            if failure == "cancel_wait":
                stop.set()
                raise oauth.SmsError("fixture cancelled")
            return "654321"

        def finish(self, activation):
            assert "otp_accepted" in events
            step("finish")

        def cancel(self, activation):
            step("cancel")

    factory = Mock(return_value=PoolClient())
    monkeypatch.setattr(oauth, "ProtocolClient", Protocol)
    # Leave _sms_client intact: exercise its real imported_pool dispatch.
    monkeypatch.setattr(imported_sms, "ImportedPoolSmsClient", factory)
    return SimpleNamespace(events=events, stop=stop, factory=factory)


def account():
    return {"email": "pool@example.invalid", "password": "mock-password", "totp_secret": "mock-totp"}


@pytest.mark.parametrize("failure", ["", "finish"])
def test_pool_workflow_finishes_only_after_otp_acceptance(config, monkeypatch, failure):
    flow = mock_phone_flow(monkeypatch, failure)
    result = oauth._process_one(account(), oauth._effective_sms_config(config), [], stop_event=flow.stop)
    assert result["status"] == "success"
    assert flow.events == ["login", "acquire", "send", "wait", "otp", "otp_accepted", "finish", "workspace", "close"]
    assert flow.factory.call_args.args[0].provider == "imported_pool"
    assert flow.factory.call_count == 1
    assert result["phone_attempts"][0]["provider"] == "imported_pool"
    assert result["phone_attempts"][0]["result"] == "success"
    assert SECRET not in json.dumps(result)
    assert "654321" not in json.dumps(result)
    oauth._append_rt_history(account()["email"], result)
    stats = asyncio.run(oauth.sms_stats())
    assert stats["countries"][0]["name"] == "导入号码池 · 美国"
    assert stats["summary"]["success"] == 1


@pytest.mark.parametrize("failure", ["send", "wait", "otp", "cancel_wait"])
def test_pool_workflow_failures_cancel_without_finishing(config, monkeypatch, failure):
    flow = mock_phone_flow(monkeypatch, failure)
    attempts = []
    with pytest.raises(RuntimeError):
        oauth._process_one(account(), oauth._effective_sms_config(config), [],
                           stop_event=flow.stop, phone_attempts=attempts)
    assert flow.events.count("cancel") == 1
    assert "finish" not in flow.events
    assert "workspace" not in flow.events
    assert flow.events[-1] == "close"
    assert attempts[0]["result"] == ("cancelled" if failure == "cancel_wait" else "failed")
    oauth.exchange_code.assert_not_called()
    oauth._persist_sms_tokens.assert_not_called()


def test_pool_acquire_failure_stops_without_paid_fallback(config, monkeypatch):
    config["sms_code"]["max_phone_attempts"] = 3
    flow = mock_phone_flow(monkeypatch, "acquire")
    attempts = []
    with pytest.raises(oauth.SmsError, match="fixture acquire failed"):
        oauth._process_one(account(), oauth._effective_sms_config(config), [],
                           stop_event=flow.stop, phone_attempts=attempts)
    assert flow.events == ["login", "acquire", "close"]
    assert len(attempts) == 1
    assert attempts[0]["provider"] == "imported_pool"
    assert attempts[0]["result"] == "acquire_failed"
    oauth.exchange_code.assert_not_called()


def test_routes_with_temporary_pool_expose_three_uses_and_hour_cooldown(api, monkeypatch, tmp_path):
    now = [1_000_000.0]
    store = sms_number_pool.SmsNumberPool(tmp_path / "pool.sqlite3", clock=lambda: now[0])
    monkeypatch.setattr(sms_number_pool, "get_sms_pool", lambda: store)
    imported = api.post("/api/sms-pool/import", json={"text": IMPORT_TEXT})
    assert imported.json()["imported"] == 1
    assert api.post("/api/sms-pool/import", json={"text": IMPORT_TEXT}).json()["duplicates"] == 1
    for uses in range(1, 4):
        lease = store.reserve()
        assert store.complete(lease["lease_id"]) is True
        listing = api.get("/api/sms-pool")
        assert SECRET not in listing.text
        result = listing.json()
        row = result["items"][0]
        assert row["country"] == "US"
        assert row["uses"] == uses
        assert row["remaining_uses"] == 3 - uses
        assert row["status"] == ("exhausted" if uses == 3 else "cooling")
        assert result["cooldown_seconds"] == row["cooldown_remaining"] == 3600
        assert api.post("/api/sms-config/test").json()["summary"]["available"] == 0
        now[0] += 3600
    deleted = api.request("DELETE", "/api/sms-pool", json={"exhausted_only": True})
    assert deleted.json()["deleted"] == 1
    assert api.get("/api/sms-pool").json()["total"] == 0

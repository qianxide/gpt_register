"""External route contracts using only a fake service, never live providers."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib.util
import json
import socket
import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock
import urllib.request
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from web.routes import external_api as routes


PREFIX = "/api/external"
REQUEST_ID = "request_1234-AB"
LOCAL_ID = "d8ee6041-647b-4b72-a59d-2c46057477cd"
SECRET = "private-fixture-credential"
SESSIONS = '{"accessToken":"private-fixture-session"}\nsecond-fixture-session'
LINKS = ["https://example.invalid/first", "https://example.invalid/second"]
FUSE_CODES = [SECRET + "-first-cdk", SECRET + "-second-cdk"]
UPI_SECRETS = {
    "foarge_credential": SECRET + "-foarge",
    "masi_cdk_ticket": SECRET + "-masi",
    "seashore_cdk": SECRET + "-seashore",
}
PUBLIC_SETTINGS = {
    "scan": {"api_key_configured": True, "api_key_masked": "***1234", "has_bearer_token": True},
    "upi": {"cdk_configured": True, "cdk_masked": "***5678", "has_entry_proxy": True,
            "has_foarge_credential": True, "foarge_credential_masked": "***6789",
            "has_masi_cdk_ticket": True, "masi_cdk_ticket_masked": "***7890",
            "has_seashore_cdk": True, "seashore_cdk_masked": "***8901"},
}
UPDATES = {
    "scan": {"api_key": SECRET, "bearer_token": "fixture-bearer", "clear_api_key": False,
             "clear_bearer_token": True},
    "upi": {"cdk": "fixture-cdk", "entry_proxy": "http://user:pass@example.invalid:80",
            "exit_proxy": "socks5://example.invalid:1080", "clear_cdk": False,
            "clear_entry_proxy": True, "clear_exit_proxy": False},
}
RESULTS = {
    "settings": PUBLIC_SETTINGS,
    "save_settings": PUBLIC_SETTINGS,
    "scan_config": {"unit_price": "0.025", "max_links": 200},
    "scan_quote": {"count": 2, "total": "0.050"},
    "scan_orders": {"items": [{"id": "order-1", "status": "queued"}], "total": 1},
    "scan_submit": {"ok": True, "submission": {"request_id": REQUEST_ID, "status": "queued"}},
    "scan_submissions": {"items": [{"request_id": REQUEST_ID, "status": "queued"}], "total": 1},
    "scan_retry": {"ok": True, "submission": {"request_id": REQUEST_ID, "status": "queued"}},
    "scan_balance": {"balance": "12.50"},
    "upi_credits": {"credits": 7},
    "upi_fuse": {"ok": True, "duplicate": False, "transferred_points": 12,
                 "target": {"id": "fused-cdk-1", "label": "合并卡", "credits": 12}},
    "upi_create": {"ok": True, "batch_id": REQUEST_ID,
                   "items": [{"id": LOCAL_ID, "status": "queued"}]},
    "upi_jobs": {"items": [{"local_id": LOCAL_ID, "status": "running"}], "total": 1},
    "upi_refresh": {"local_id": LOCAL_ID, "status": "completed"},
    "upi_cancel": {"local_id": LOCAL_ID, "status": "cancelled"},
}
CASES = [
    ("GET", "/settings", None, {}, "settings", {}),
    ("PUT", "/settings", UPDATES, {}, "save_settings", {"updates": UPDATES}),
    ("GET", "/scan/config", None, {}, "scan_config", {}),
    ("POST", "/scan/quote", {"links": LINKS}, {}, "scan_quote", {"links": LINKS}),
    ("GET", "/scan/orders", None, {}, "scan_orders", {"page": 1, "page_size": 20}),
    ("GET", "/scan/orders", None, {"page": 7, "page_size": 100}, "scan_orders",
     {"page": 7, "page_size": 100}),
    ("POST", "/scan/orders", {"request_id": REQUEST_ID, "links": LINKS, "unit_price": "0.02500"},
     {}, "scan_submit", {"request_id": REQUEST_ID, "links": LINKS, "unit_price": "0.02500"}),
    ("GET", "/scan/submissions", None, {}, "scan_submissions", {"page": 1, "page_size": 20}),
    ("GET", "/scan/submissions", None, {"page": 3, "page_size": 8}, "scan_submissions",
     {"page": 3, "page_size": 8}),
    ("POST", f"/scan/submissions/{REQUEST_ID}/retry", {}, {}, "scan_retry", {"request_id": REQUEST_ID}),
    ("POST", "/scan/balance", {}, {}, "scan_balance", {}),
    ("POST", "/upi/credits", {}, {}, "upi_credits", {}),
    ("POST", "/upi/fuse", {"codes": [" \t" + FUSE_CODES[0] + "\n", FUSE_CODES[1]]}, {}, "upi_fuse",
     {"codes": FUSE_CODES, "label": ""}),
    ("POST", "/upi/jobs", {"request_id": REQUEST_ID, "sessions_text": SESSIONS}, {}, "upi_create",
     {"request_id": REQUEST_ID, "sessions_text": SESSIONS, "use_promo": True}),
    ("POST", "/upi/jobs", {"request_id": REQUEST_ID, "sessions_text": SESSIONS, "use_promo": False},
     {}, "upi_create", {"request_id": REQUEST_ID, "sessions_text": SESSIONS, "use_promo": False}),
    ("GET", "/upi/jobs", None, {}, "upi_jobs", {"page": 1, "page_size": 20, "refresh": False}),
    ("GET", "/upi/jobs", None, {"page": 2, "page_size": 100, "refresh": "true"}, "upi_jobs",
     {"page": 2, "page_size": 100, "refresh": True}),
    ("POST", f"/upi/jobs/{LOCAL_ID}/refresh", {}, {}, "upi_refresh", {"local_id": LOCAL_ID}),
    ("POST", f"/upi/jobs/{LOCAL_ID}/cancel", {}, {}, "upi_cancel", {"local_id": LOCAL_ID}),
]
EMPTY_ACTIONS = [
    f"/scan/submissions/{REQUEST_ID}/retry", "/scan/balance", "/upi/credits",
    f"/upi/jobs/{LOCAL_ID}/refresh", f"/upi/jobs/{LOCAL_ID}/cancel",
]


class FakeExternalAPIError(RuntimeError):
    def __init__(self, message, status=400, code="invalid_request", retry_after=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.retry_after = retry_after


@pytest.fixture(autouse=True)
def no_provider_network(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Unexpected provider network access")

    original_connect = socket.socket.connect

    def event_loop_connect(sock, address):
        # Windows uses loopback to create asyncio's internal socketpair.
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect(sock, address)
        unexpected()

    monkeypatch.setattr(socket, "create_connection", unexpected)
    monkeypatch.setattr(socket.socket, "connect", event_loop_connect)
    monkeypatch.setattr(urllib.request, "urlopen", unexpected)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", unexpected)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", unexpected)


@pytest.fixture
def backend(monkeypatch):
    service = Mock(spec=list(RESULTS))
    worker_threads = []

    def checked(mock):
        def run(*args, **kwargs):
            worker_threads.append(threading.get_ident())
            with pytest.raises(RuntimeError, match="no running event loop"):
                asyncio.get_running_loop()
            return mock.return_value
        return run

    for name, result in RESULTS.items():
        method = getattr(service, name)
        method.return_value = deepcopy(result)
        method.side_effect = checked(method)
    factory = Mock(return_value=service)
    factory.side_effect = checked(factory)
    module = ModuleType("common.external_api")
    module.ExternalAPIError = FakeExternalAPIError
    module.get_external_service = factory
    # Works before the service exists and isolates it if another agent adds it.
    monkeypatch.setitem(sys.modules, "common.external_api", module)
    return SimpleNamespace(service=service, factory=factory, module=module,
                           worker_threads=worker_threads, event_threads=[])


@pytest.fixture
def api(backend):
    app = FastAPI()
    app.include_router(routes.router)

    @app.middleware("http")
    async def event_thread(request, call_next):
        backend.event_threads.append(threading.get_ident())
        return await call_next(request)

    with TestClient(app) as client:
        yield client


def assert_private_validation(response, backend):
    assert response.status_code == 422, response.text
    assert SECRET not in response.text
    assert "private-fixture-session" not in response.text
    assert len(response.content) < 4096
    for error in response.json()["detail"]:
        assert set(error) == {"loc", "msg", "type"}
    backend.factory.assert_not_called()
    assert backend.service.mock_calls == []


@pytest.mark.parametrize("method,path,body,params,operation,expected", CASES)
def test_forwarding_responses_and_worker_threads(api, backend, method, path, body, params, operation, expected):
    response = api.request(method, PREFIX + path, json=body, params=params)
    assert response.status_code == 200, response.text
    assert response.json() == RESULTS[operation]
    backend.factory.assert_called_once_with()
    getattr(backend.service, operation).assert_called_once_with(**expected)
    assert len(backend.service.mock_calls) == 1
    assert len(backend.worker_threads) == 2
    assert set(backend.worker_threads).isdisjoint(backend.event_threads)
    assert SECRET not in response.text
    assert "private-fixture-session" not in response.text


@pytest.mark.parametrize("operation,path,body", [
    ("scan_submit", "/scan/orders", {"request_id": REQUEST_ID, "links": LINKS, "unit_price": "0.1"}),
    ("scan_retry", f"/scan/submissions/{REQUEST_ID}/retry", {}),
])
@pytest.mark.parametrize("status", ["unknown", "failed"])
def test_scan_submission_outcomes_are_preserved_without_implicit_retry(api, backend, operation, path, body, status):
    result = {"ok": True, "submission": {"id": REQUEST_ID, "status": status,
                                         "retryable": status == "unknown", "error": "Provider outcome"}}
    getattr(backend.service, operation).return_value = result
    response = api.post(PREFIX + path, json=body)
    assert response.status_code == 200
    assert response.json() == result
    getattr(backend.service, operation).assert_called_once()
    assert len(backend.service.mock_calls) == 1


def test_openapi_contract(api):
    schema = api.get("/openapi.json").json()
    expected = {(PREFIX + path, method.lower()) for method, path, *_ in CASES}
    expected -= {(PREFIX + f"/scan/submissions/{REQUEST_ID}/retry", "post"),
                 (PREFIX + f"/upi/jobs/{LOCAL_ID}/refresh", "post"),
                 (PREFIX + f"/upi/jobs/{LOCAL_ID}/cancel", "post")}
    expected |= {(PREFIX + "/scan/submissions/{request_id}/retry", "post"),
                 (PREFIX + "/upi/jobs/{local_id}/refresh", "post"),
                 (PREFIX + "/upi/jobs/{local_id}/cancel", "post")}
    expected |= {(PREFIX + "/pipeline/preview", "post"),
                 (PREFIX + "/pipeline/start", "post"),
                 (PREFIX + "/pipeline/status", "get"),
                 (PREFIX + "/pipeline/{task_id}/stop", "post"),
                 (PREFIX + "/pipeline/{task_id}/resume", "post")}
    assert {(path, method) for path, methods in schema["paths"].items() for method in methods} == expected
    assert len(expected) == 20
    models = schema["components"]["schemas"]
    for name in ("SettingsUpdate", "ScanSettingsUpdate", "UpiSettingsUpdate", "ScanQuoteRequest",
                 "ScanOrderRequest", "UpiCreateRequest", "UpiFuseRequest", "EmptyRequest"):
        assert models[name]["additionalProperties"] is False
    links = models["ScanQuoteRequest"]["properties"]["links"]
    assert (links["minItems"], links["maxItems"]) == (1, 200)
    assert (links["items"]["minLength"], links["items"]["maxLength"]) == (1, 2048)
    request_id = models["ScanOrderRequest"]["properties"]["request_id"]
    assert (request_id["minLength"], request_id["maxLength"], request_id["pattern"]) == (8, 80, r"^[A-Za-z0-9_-]+$")
    assert models["UpiCreateRequest"]["properties"]["sessions_text"]["maxLength"] == 1_000_000
    fuse = models["UpiFuseRequest"]["properties"]
    assert (fuse["codes"]["minItems"], fuse["codes"]["maxItems"]) == (2, 100)
    assert (fuse["codes"]["items"]["minLength"], fuse["codes"]["items"]["maxLength"]) == (1, 8192)
    assert (fuse["label"]["default"], fuse["label"]["maxLength"]) == ("", 120)
    for model in ("UpiSettingsUpdate", "UpiCreateRequest"):
        assert models[model]["properties"]["payment_platform"]["anyOf"] == [
            {"enum": ["none", "foarge", "masi", "seashore"], "type": "string"}, {"type": "null"},
        ]
    upi_settings = models["UpiSettingsUpdate"]["properties"]
    assert upi_settings["foarge_auth_mode"]["anyOf"][0]["enum"] == ["pbk", "api_key", "cdk_session"]
    assert "seashore_api_base" not in upi_settings
    for field in ("foarge_api_base", "masi_api_base"):
        assert upi_settings[field]["anyOf"][0]["maxLength"] == 2048
    for model, name, limit in [("ScanSettingsUpdate", "api_key", 8192),
                               ("ScanSettingsUpdate", "bearer_token", 8192),
                               ("UpiSettingsUpdate", "cdk", 8192),
                               ("UpiSettingsUpdate", "foarge_credential", 8192),
                               ("UpiSettingsUpdate", "masi_cdk_ticket", 8192),
                               ("UpiSettingsUpdate", "seashore_cdk", 8192),
                               ("UpiSettingsUpdate", "entry_proxy", 2048),
                               ("UpiSettingsUpdate", "exit_proxy", 2048)]:
        assert models[model]["properties"][name]["anyOf"][0]["maxLength"] == limit
    assert routes.MAX_BODY_BYTES == 2 * 1024 * 1024


@pytest.mark.parametrize("updates,expected", [
    ({}, {}), ({"scan": {}}, {"scan": {}}),
    ({"scan": None, "upi": None}, {}),
    ({"scan": {"api_key": None}, "upi": {"cdk": None}}, {"scan": {}, "upi": {}}),
    ({"scan": {"api_key": ""}}, {"scan": {"api_key": ""}}),
    ({"upi": {"clear_cdk": False}}, {"upi": {"clear_cdk": False}}),
    ({"upi": {"clear_exit_proxy": True}}, {"upi": {"clear_exit_proxy": True}}),
])
def test_partial_settings_only_forward_supplied_non_null_fields(api, backend, updates, expected):
    assert api.put(PREFIX + "/settings", json=updates).status_code == 200
    backend.service.save_settings.assert_called_once_with(updates=expected)


@pytest.mark.parametrize("group,field,limit", [
    ("scan", "api_key", 8192), ("scan", "bearer_token", 8192), ("upi", "cdk", 8192),
    ("upi", "entry_proxy", 2048), ("upi", "exit_proxy", 2048),
])
@pytest.mark.parametrize("extra", [0, 1])
def test_settings_length_boundaries(api, backend, group, field, limit, extra):
    updates = {group: {field: SECRET + "x" * (limit - len(SECRET) + extra)}}
    response = api.put(PREFIX + "/settings", json=updates)
    if extra:
        assert_private_validation(response, backend)
    else:
        assert response.status_code == 200
        backend.service.save_settings.assert_called_once_with(updates=updates)


@pytest.mark.parametrize("body", [
    {SECRET: SECRET}, {"scan": {SECRET: SECRET}}, {"upi": {SECRET: SECRET}},
    {"scan": {"api_key": {SECRET: SECRET}}}, {"scan": {"bearer_token": [SECRET]}},
    {"upi": {"cdk": {SECRET: SECRET}}}, {"upi": {"entry_proxy": [SECRET]}},
    {"scan": {"api_key": 123}}, {"scan": {"clear_api_key": "true"}},
    {"scan": {"clear_bearer_token": 1}}, {"upi": {"clear_cdk": "false"}},
    {"upi": {"clear_entry_proxy": 0}}, {"upi": {"clear_exit_proxy": None}},
    {"scan": SECRET}, {"upi": [SECRET]}, [SECRET],
])
def test_settings_validation_has_no_secrets_in_locations_or_messages(api, backend, body):
    assert_private_validation(api.put(PREFIX + "/settings", json=body), backend)


@pytest.mark.parametrize("platform,auth_mode", [
    ("none", "pbk"), ("foarge", "api_key"), ("masi", "cdk_session"), ("seashore", "pbk"),
])
def test_upi_provider_settings_forward_without_redacting_public_values(api, backend, platform, auth_mode):
    public = {
        "payment_platform": platform, "foarge_auth_mode": auth_mode,
        "foarge_api_base": "https://foarge.example.invalid/api",
        "masi_api_base": "https://masi.example.invalid/v1/",
        "masi_require_online_workers": False,
    }
    updates = {"upi": {**public, **UPI_SECRETS, "clear_foarge_credential": False,
                       "clear_masi_cdk_ticket": False, "clear_seashore_cdk": False}}
    result = deepcopy(PUBLIC_SETTINGS)
    result["upi"].update(public)
    backend.service.save_settings.return_value = result
    response = api.put(PREFIX + "/settings", json=updates)
    assert response.status_code == 200
    assert response.json() == result
    backend.service.save_settings.assert_called_once_with(updates=updates)
    assert SECRET not in response.text


def test_upi_partial_settings_drop_nulls_and_preserve_clear_flags(api, backend):
    supplied = dict.fromkeys(["payment_platform", "foarge_auth_mode", "foarge_api_base", "masi_api_base",
                              *UPI_SECRETS])
    expected = {"clear_" + field: True for field in UPI_SECRETS}
    expected["masi_require_online_workers"] = True
    response = api.put(PREFIX + "/settings", json={"upi": {**supplied, **expected}})
    assert response.status_code == 200
    backend.service.save_settings.assert_called_once_with(updates={"upi": expected})


def test_upi_settings_reject_invalid_enums_and_non_boolean_flags(api, backend):
    invalid = [
        {"payment_platform": SECRET}, {"payment_platform": ["foarge", "masi"]},
        {"foarge_auth_mode": SECRET}, {"foarge_auth_mode": 1},
        {"masi_require_online_workers": "true"}, {"masi_require_online_workers": 1},
        {"masi_require_online_workers": None}, {"clear_foarge_credential": "false"},
        {"clear_masi_cdk_ticket": 0}, {"clear_seashore_cdk": None},
    ]
    for changes in invalid:
        response = api.put(PREFIX + "/settings", json={"upi": changes})
        assert_private_validation(response, backend)
        assert any("\u4e00" <= char <= "\u9fff" for char in response.json()["detail"][0]["msg"])


@pytest.mark.parametrize("field", ["foarge_api_base", "masi_api_base"])
def test_upi_api_bases_are_bounded_https_strings(api, backend, field):
    base = "https://provider.example.invalid/"
    maximum = base + "x" * (2048 - len(base))
    updates = {"upi": {field: maximum}}
    assert api.put(PREFIX + "/settings", json=updates).status_code == 200
    backend.service.save_settings.assert_called_once_with(updates=updates)
    backend.factory.reset_mock()
    backend.service.reset_mock()
    for value in (maximum + "x", "", 123, "http://example.invalid", "https://",
                  "https://" + SECRET + "@example.invalid", base + "\n" + SECRET,
                  "https://example.invalid:bad", base + "?ticket=" + SECRET,
                  base + "#" + SECRET, "https://example.invalid\\" + SECRET):
        assert_private_validation(api.put(PREFIX + "/settings", json={"upi": {field: value}}), backend)


def test_new_upi_credentials_keep_strict_string_and_length_limits(api, backend):
    for size in (0, 8192):
        updates = {"upi": {field: "x" * size for field in UPI_SECRETS}}
        assert api.put(PREFIX + "/settings", json=updates).status_code == 200
        backend.service.save_settings.assert_called_once_with(updates=updates)
        backend.factory.reset_mock()
        backend.service.reset_mock()
    for value in (SECRET + "x" * 8192, 123, [SECRET], {SECRET: SECRET}):
        updates = {"upi": dict.fromkeys(UPI_SECRETS, value)}
        assert_private_validation(api.put(PREFIX + "/settings", json=updates), backend)


def test_upi_settings_reject_seashore_api_base_override(api, backend):
    for value in ("https://" + SECRET + ".invalid", None):
        response = api.put(PREFIX + "/settings", json={"upi": {"seashore_api_base": value}})
        assert_private_validation(response, backend)
        assert response.json()["detail"][0]["type"] == "extra_forbidden"


@pytest.mark.parametrize("path", ["/scan/orders", "/scan/submissions", "/upi/jobs"])
@pytest.mark.parametrize("params", [
    {"page": 0}, {"page": -1}, {"page": 1_000_001}, {"page": SECRET},
    {"page_size": 0}, {"page_size": 101}, {"page_size": "1.5"}, {SECRET: SECRET},
])
def test_pagination_validation(api, backend, path, params):
    assert_private_validation(api.get(PREFIX + path, params=params), backend)


@pytest.mark.parametrize("path,params", [
    ("/upi/jobs", {"refresh": SECRET}), ("/settings", {SECRET: SECRET}),
    ("/scan/orders", {"refresh": "true"}), ("/scan/config", {"cdk": SECRET}),
])
def test_invalid_or_unknown_query_values(api, backend, path, params):
    assert_private_validation(api.get(PREFIX + path, params=params), backend)


@pytest.mark.parametrize("path", ["/scan/quote", "/scan/orders"])
@pytest.mark.parametrize("links", [[], ["x"] * 201, [""], ["x" * 2049], [123], [True], [None], SECRET])
def test_link_validation(api, backend, path, links):
    body = {"links": links}
    if path == "/scan/orders":
        body.update(request_id=REQUEST_ID, unit_price="0.1")
    assert_private_validation(api.post(PREFIX + path, json=body), backend)


def test_maximum_links_preserve_order_and_duplicates(api, backend):
    links = ["x" * 2048] * 199 + ["last"]
    response = api.post(PREFIX + "/scan/quote", json={"links": links})
    assert response.status_code == 200
    backend.service.scan_quote.assert_called_once_with(links=links)


@pytest.mark.parametrize("path", ["/scan/orders", "/upi/jobs"])
@pytest.mark.parametrize("request_id", ["x" * 7, "x" * 81, "bad id-123", "request:1234",
                                       "request123\n", "中文request123", 12345678, True, None])
def test_request_id_is_strict_and_fully_anchored(api, backend, path, request_id):
    body = {"request_id": request_id}
    if path == "/scan/orders":
        body.update(links=LINKS, unit_price="0.1")
    else:
        body["sessions_text"] = SESSIONS
    assert_private_validation(api.post(PREFIX + path, json=body), backend)


@pytest.mark.parametrize("request_id", ["a" * 8, "A_-9" * 20, LOCAL_ID])
def test_request_id_accepts_boundaries_and_uuid_hyphens(api, backend, request_id):
    body = {"request_id": request_id, "links": LINKS, "unit_price": "0.1"}
    assert api.post(PREFIX + "/scan/orders", json=body).status_code == 200
    backend.service.scan_submit.assert_called_once_with(**body)


@pytest.mark.parametrize("unit_price", [0.1, True, None, "", "9" * 129])
def test_price_requires_a_bounded_string(api, backend, unit_price):
    body = {"request_id": REQUEST_ID, "links": LINKS, "unit_price": unit_price}
    assert_private_validation(api.post(PREFIX + "/scan/orders", json=body), backend)


@pytest.mark.parametrize("unit_price", ["1.2300", "1e-6", "NaN", "-1", "not-a-decimal"])
def test_service_owns_decimal_validation(api, backend, unit_price):
    backend.service.scan_submit.side_effect = FakeExternalAPIError("Invalid unit price", code="invalid_price")
    body = {"request_id": REQUEST_ID, "links": LINKS, "unit_price": unit_price}
    response = api.post(PREFIX + "/scan/orders", json=body)
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid unit price"
    backend.service.scan_submit.assert_called_once_with(**body)


@pytest.mark.parametrize("changes", [
    {"sessions_text": ""}, {"sessions_text": SECRET + "x" * 1_000_000},
    {"sessions_text": {SECRET: SECRET}}, {"sessions_text": 123},
    {"use_promo": "true"}, {"use_promo": 1}, {"use_promo": None}, {SECRET: SECRET},
])
def test_upi_create_validation(api, backend, changes):
    body = {"request_id": REQUEST_ID, "sessions_text": SESSIONS, **changes}
    assert_private_validation(api.post(PREFIX + "/upi/jobs", json=body), backend)


def test_maximum_session_character_count(api, backend):
    body = {"request_id": REQUEST_ID, "sessions_text": "s" * 1_000_000, "use_promo": False}
    assert api.post(PREFIX + "/upi/jobs", json=body).status_code == 200
    backend.service.upi_create.assert_called_once_with(**body)


@pytest.mark.parametrize("platform", ["none", "foarge", "masi", "seashore", None])
def test_upi_create_forwards_platform_and_omits_null(api, backend, platform):
    body = {"request_id": REQUEST_ID, "sessions_text": SESSIONS, "payment_platform": platform}
    response = api.post(PREFIX + "/upi/jobs", json=body)
    assert response.status_code == 200
    expected = {"request_id": REQUEST_ID, "sessions_text": SESSIONS, "use_promo": True}
    if platform is not None:
        expected["payment_platform"] = platform
    backend.service.upi_create.assert_called_once_with(**expected)


def test_upi_create_rejects_invalid_or_multiple_platforms(api, backend):
    for platform in (SECRET, "FOARGE", ["foarge", "masi"], True, 1):
        body = {"request_id": REQUEST_ID, "sessions_text": SESSIONS, "payment_platform": platform}
        assert_private_validation(api.post(PREFIX + "/upi/jobs", json=body), backend)


def test_upi_fuse_accepts_bounds_after_trimming_and_preserves_order(api, backend):
    codes = ["x"] + [f"fixture-cdk-{index}" for index in range(98)] + ["z" * 8192]
    body = {"codes": [" \t" + code + "\n " for code in codes], "label": "合" * 120}
    response = api.post(PREFIX + "/upi/fuse", json=body)
    assert response.status_code == 200
    assert response.json() == RESULTS["upi_fuse"]
    backend.service.upi_fuse.assert_called_once_with(codes=codes, label=body["label"])


def test_upi_fuse_rejects_invalid_counts_and_non_string_or_blank_codes(api, backend):
    for codes in ([], [SECRET], [SECRET] * 101, SECRET, None, {SECRET: SECRET},
                  [SECRET, 1], [SECRET, True], [SECRET, None], [SECRET, ""],
                  [SECRET, " \t\n "], [SECRET, SECRET + "x" * 8192]):
        assert_private_validation(api.post(PREFIX + "/upi/fuse", json={"codes": codes}), backend)


def test_upi_fuse_rejects_invalid_labels_and_extra_body_fields(api, backend):
    for changes in ({"label": "标" * 121}, {"label": 1}, {"label": None},
                    {"label": [SECRET]}, {SECRET: SECRET}, {"payment_platform": "masi"}):
        body = {"codes": FUSE_CODES, **changes}
        assert_private_validation(api.post(PREFIX + "/upi/fuse", json=body), backend)


def test_upi_fuse_rejects_missing_malformed_bodies_and_extra_query_fields(api, backend):
    assert_private_validation(api.post(PREFIX + "/upi/fuse"), backend)
    for body in ({}, [SECRET]):
        assert_private_validation(api.post(PREFIX + "/upi/fuse", json=body), backend)
    response = api.post(PREFIX + "/upi/fuse", content='{"codes":["' + SECRET,
                        headers={"content-type": "application/json"})
    assert_private_validation(response, backend)
    response = api.post(PREFIX + "/upi/fuse", json={"codes": FUSE_CODES}, params={SECRET: SECRET})
    assert_private_validation(response, backend)


@pytest.mark.parametrize("path", EMPTY_ACTIONS)
@pytest.mark.parametrize("body", [{SECRET: SECRET}, {"token": SECRET}, [SECRET], ""])
def test_empty_actions_reject_non_objects_and_extra_fields(api, backend, path, body):
    assert_private_validation(api.post(PREFIX + path, json=body), backend)


@pytest.mark.parametrize("path", EMPTY_ACTIONS + ["/scan/quote", "/scan/orders", "/upi/jobs"])
def test_post_body_is_required(api, backend, path):
    assert_private_validation(api.post(PREFIX + path), backend)


@pytest.mark.parametrize("path", ["/scan/quote", "/scan/orders"])
def test_scan_extra_field_names_are_private(api, backend, path):
    body = {"links": LINKS, SECRET: SECRET}
    if path == "/scan/orders":
        body.update(request_id=REQUEST_ID, unit_price="0.1")
    assert_private_validation(api.post(PREFIX + path, json=body), backend)


@pytest.mark.parametrize("path", [
    "/scan/submissions/short/retry", "/scan/submissions/" + "x" * 81 + "/retry",
    "/scan/submissions/" + quote("request123\n", safe="") + "/retry",
    "/upi/jobs/" + "x" * 129 + "/refresh", "/upi/jobs/bad:id/cancel",
    "/upi/jobs/" + quote("bad id", safe="") + "/refresh",
])
def test_path_identifier_validation(api, backend, path):
    assert_private_validation(api.post(PREFIX + path, json={}), backend)


@pytest.mark.parametrize("method,path", [("PUT", "/settings"), ("POST", "/upi/jobs"),
                                         ("POST", "/scan/quote"), ("POST", "/scan/balance")])
@pytest.mark.parametrize("content", ['{"cdk":"' + SECRET, '{"' + SECRET + '":', 'null'])
def test_malformed_json_and_null_bodies_do_not_echo_input(api, backend, method, path, content):
    response = api.request(method, PREFIX + path, content=content, headers={"content-type": "application/json"})
    assert_private_validation(response, backend)


@pytest.mark.parametrize("method,path", [("PUT", "/settings"), ("POST", "/upi/jobs"),
                                         ("POST", "/scan/balance"), ("GET", "/settings")])
@pytest.mark.parametrize("length", [None, "2", "2097153"])
def test_actual_and_declared_body_size_limits(api, backend, method, path, length):
    headers = {"content-type": "application/json"}
    if length is not None:
        headers["content-length"] = length
    content = b"{}" if length == "2097153" else iter([b" " * (2 * 1024 * 1024), b"x"])
    response = api.request(method, PREFIX + path, content=content, headers=headers)
    assert response.status_code == 413
    backend.factory.assert_not_called()


def test_exact_body_byte_limit_is_accepted(api, backend):
    content = b"{}" + b" " * (2 * 1024 * 1024 - 2)
    response = api.put(PREFIX + "/settings", content=content, headers={"content-type": "application/json"})
    assert response.status_code == 200
    backend.service.save_settings.assert_called_once_with(updates={})


def test_multibyte_sessions_obey_byte_limit_before_character_limit(api, backend):
    content = json.dumps({"request_id": REQUEST_ID, "sessions_text": "字" * 800_000}, ensure_ascii=False).encode()
    response = api.post(PREFIX + "/upi/jobs", content=content, headers={"content-type": "application/json"})
    assert response.status_code == 413
    backend.factory.assert_not_called()


@pytest.mark.parametrize("length", [SECRET, "-1", "1.5"])
def test_invalid_content_length_is_private(api, backend, length):
    response = api.put(PREFIX + "/settings", content=b"{}", headers={"content-length": length})
    assert response.status_code == 400
    assert SECRET not in response.text
    backend.factory.assert_not_called()


def test_large_number_of_validation_errors_is_bounded(api, backend):
    response = api.put(PREFIX + "/settings", json={f"{SECRET}-{i}": SECRET for i in range(200)})
    assert_private_validation(response, backend)
    assert len(response.json()["detail"]) <= 20


@pytest.mark.parametrize("method,path,body,params,operation,expected", CASES)
def test_all_routes_map_service_errors(api, backend, method, path, body, params, operation, expected):
    getattr(backend.service, operation).side_effect = FakeExternalAPIError("Please retry later", 429, "rate_limited", 7)
    response = api.request(method, PREFIX + path, json=body, params=params)
    assert response.status_code == 429
    assert response.json() == {"detail": "Please retry later", "message": "Please retry later",
                               "code": "rate_limited", "retry_after": 7}
    assert response.headers["retry-after"] == "7"
    getattr(backend.service, operation).assert_called_once_with(**expected)


@pytest.mark.parametrize("status,expected", [(400, 400), (401, 502), (403, 403), (404, 404),
                                           (409, 409), (422, 422), (500, 500), (502, 502),
                                           (503, 503), (200, 502), (302, 502), (999, 502)])
def test_status_mapping_never_turns_external_auth_into_panel_401(api, backend, status, expected):
    backend.service.scan_balance.side_effect = FakeExternalAPIError("Provider operation failed", status, "provider_error")
    response = api.post(PREFIX + "/scan/balance", json={})
    assert response.status_code == expected
    assert isinstance(response.json()["detail"], str)
    assert response.json()["code"] == "provider_error"
    assert "retry-after" not in response.headers


@pytest.mark.parametrize("retry_after", [None, -1, True, "7\r\nX-Secret: " + SECRET])
def test_invalid_retry_after_is_not_reflected(api, backend, retry_after):
    backend.service.upi_credits.side_effect = FakeExternalAPIError("Wait", 429, "rate_limited", retry_after)
    response = api.post(PREFIX + "/upi/credits", json={})
    assert response.status_code == 429
    assert "retry_after" not in response.json()
    assert "retry-after" not in response.headers
    assert SECRET not in response.text


@pytest.mark.parametrize("factory_error", [False, True])
def test_unexpected_errors_never_echo_exception_text(api, backend, factory_error):
    target = backend.factory if factory_error else backend.service.upi_create
    target.side_effect = RuntimeError(SECRET + SESSIONS)
    response = api.post(PREFIX + "/upi/jobs", json={"request_id": REQUEST_ID, "sessions_text": SESSIONS})
    assert response.status_code == 502
    assert response.json()["code"] == "external_api_error"
    assert SECRET not in response.text
    assert "private-fixture-session" not in response.text


def test_factory_service_error_uses_the_same_mapping(api, backend):
    backend.factory.side_effect = FakeExternalAPIError("Settings missing", 400, "missing_settings")
    response = api.get(PREFIX + "/settings")
    assert response.status_code == 400
    assert response.json()["detail"] == "Settings missing"
    assert response.json()["code"] == "missing_settings"
    assert backend.service.mock_calls == []


def test_declared_error_redacts_submitted_credentials_and_credential_labels(api, backend):
    backend.service.save_settings.side_effect = FakeExternalAPIError(
        f"Rejected {SECRET}; bearer_token={SECRET}; cdk='{SECRET}'; Bearer {SECRET}",
        400, "invalid_credentials",
    )
    response = api.put(PREFIX + "/settings", json={"scan": {"api_key": SECRET}})
    assert response.status_code == 400
    assert SECRET not in response.text
    assert response.json()["code"] == "invalid_credentials"
    assert "[redacted]" in response.json()["detail"]


def test_declared_error_never_reflects_sessions_or_arbitrary_code(api, backend):
    backend.service.upi_create.side_effect = FakeExternalAPIError("Rejected " + SESSIONS, 400, SESSIONS)
    response = api.post(PREFIX + "/upi/jobs", json={"request_id": REQUEST_ID, "sessions_text": SESSIONS})
    assert response.status_code == 400
    assert "private-fixture-session" not in response.text
    assert "second-fixture-session" not in response.text
    assert response.json()["code"] == "external_api_error"


def test_new_upi_secrets_are_redacted_without_losing_chinese_errors(api, backend):
    stored = ["saved-ticket", "saved-foarge", "saved-seashore", "saved-cdk-ticket",
              "saved-first-code", "saved-second-code"]
    message = ("凭证校验失败，请更新配置；" + "; ".join(UPI_SECRETS.values())
               + f"; ticket='{stored[0]}'; foargeCredential={stored[1]}; seashore_cdk={stored[2]}"
               + f'; masi_cdk_ticket="{stored[3]}"; codes=["{stored[4]}", "{stored[5]}"]')
    backend.service.save_settings.side_effect = FakeExternalAPIError(message, 422, "invalid_credentials")
    updates = {"upi": {field: " " + value + " " for field, value in UPI_SECRETS.items()}}
    response = api.put(PREFIX + "/settings", json=updates)
    backend.service.save_settings.assert_called_once_with(updates=updates)
    assert response.status_code == 422
    assert response.json()["detail"].startswith("凭证校验失败，请更新配置；")
    assert response.json()["detail"] == response.json()["message"]
    assert response.json()["code"] == "invalid_credentials"
    for secret in [*UPI_SECRETS.values(), *stored]:
        assert secret not in response.text


def test_upi_fuse_redacts_results_and_errors_without_mutating_service_data(api, backend):
    result = deepcopy(RESULTS["upi_fuse"])
    result.update(duplicate=True, codes=FUSE_CODES, raw_codes=FUSE_CODES)
    result["target"].update(UPI_SECRETS)
    result["target"].update(ticket=SECRET, cdk=SECRET, cdk_masked="***1234",
                            note="合并完成：" + " / ".join(FUSE_CODES))
    backend.service.upi_fuse.return_value = result
    body = {"codes": FUSE_CODES, "label": "合并卡"}
    response = api.post(PREFIX + "/upi/fuse", json=body)
    expected = deepcopy(RESULTS["upi_fuse"])
    expected["duplicate"] = True
    expected["target"].update(cdk_masked="***1234", note="合并完成：[redacted] / [redacted]")
    assert response.status_code == 200
    assert response.json() == expected
    assert result["codes"] == FUSE_CODES
    assert result["target"]["ticket"] == SECRET
    for error, status in [(FakeExternalAPIError("合并失败：" + repr(FUSE_CODES), 409, FUSE_CODES[0]), 409),
                          (RuntimeError(repr(FUSE_CODES)), 502)]:
        backend.service.upi_fuse.side_effect = error
        response = api.post(PREFIX + "/upi/fuse", json=body)
        assert response.status_code == status
        assert response.json()["code"] == "external_api_error"
        assert SECRET not in response.text
    assert backend.service.upi_fuse.call_count == 3


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_settings_keep_public_masks_but_drop_accidental_plaintext(api, backend, method):
    result = deepcopy(PUBLIC_SETTINGS)
    result["scan"].update(api_key=SECRET, bearer_token=SECRET)
    result["upi"].update(cdk=SECRET, entry_proxy=SECRET, exit_proxy=SECRET, **UPI_SECRETS)
    result["upi"].update(ticket=SECRET, foargeCredential=SECRET, masiCdkTicket=SECRET,
                         seashoreCDK=SECRET, codes=FUSE_CODES)
    operation = "settings" if method == "GET" else "save_settings"
    getattr(backend.service, operation).return_value = result
    response = api.request(method, PREFIX + "/settings", json={} if method == "PUT" else None)
    assert response.status_code == 200
    assert response.json() == PUBLIC_SETTINGS
    assert SECRET not in response.text
    assert result["scan"]["api_key"] == SECRET  # Filtering does not mutate the service result.


def test_job_response_drops_raw_request_and_nested_private_provider_fields(api, backend):
    result = deepcopy(RESULTS["upi_jobs"])
    result["items"][0].update(
        sessions_text=SESSIONS, request={"request_id": REQUEST_ID, "sessions_text": SESSIONS},
        raw_response={"accessToken": SECRET}, job_secret=SECRET,
        metadata={"bearer_token": SECRET, "sessionToken": SECRET, "attempts": 2,
                  "ticket": SECRET, "foarge_credential": SECRET, "seashore_cdk": SECRET,
                  "cdk_ticket": SECRET, "codes": FUSE_CODES},
    )
    backend.service.upi_jobs.return_value = result
    response = api.get(PREFIX + "/upi/jobs")
    assert response.status_code == 200
    assert response.json()["items"][0] == {"local_id": LOCAL_ID, "status": "running", "metadata": {"attempts": 2}}
    assert SECRET not in response.text
    assert "private-fixture-session" not in response.text


def test_job_public_payment_links_and_qr_codes_are_preserved(api, backend):
    result = {"id": LOCAL_ID, "status": "done", "result": {
        "payment_link": "https://payments.stripe.com/upi/instructions/fixture?token=payment-token",
        "qr_code": "data:image/png;base64,aW1hZ2U=", "payment_status": "pending",
        "upi_uri": "upi://pay?pa=merchant&pn=Fixture&am=1.00",
    }}
    backend.service.upi_refresh.return_value = result
    response = api.post(PREFIX + f"/upi/jobs/{LOCAL_ID}/refresh", json={})
    assert response.status_code == 200
    assert response.json() == result


def test_module_import_and_schema_work_without_service(monkeypatch):
    monkeypatch.setitem(sys.modules, "common.external_api", None)
    spec = importlib.util.spec_from_file_location("_external_routes_lazy_test", routes.__file__)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router)
    with TestClient(app) as client:
        assert client.get("/openapi.json").status_code == 200
        response = client.get(PREFIX + "/settings")
    assert response.status_code == 503
    assert response.json()["code"] == "service_unavailable"


def test_panel_middleware_remains_responsible_for_auth(backend):
    app = FastAPI()
    app.include_router(routes.router)

    @app.middleware("http")
    async def panel_auth(request, call_next):
        if request.headers.get("x-fixture-panel-session") != "valid":
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Session required"}, status_code=401)
        return await call_next(request)

    with TestClient(app) as client:
        assert client.get(PREFIX + "/settings").status_code == 401
        backend.factory.assert_not_called()
        assert client.get(PREFIX + "/settings", headers={"x-fixture-panel-session": "valid"}).status_code == 200
    backend.service.settings.assert_called_once_with()

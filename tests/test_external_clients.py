"""Offline contract tests: fake transports only, including requests adapters."""
from __future__ import annotations

import copy
import gzip
import io
import json
import socket
import traceback

import pytest
import requests
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict
from urllib3.response import HTTPResponse
from urllib3.util.retry import Retry

from common.external_clients import (
    FOARGE_API_BASE, MASI_API_BASE,
    MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, REQUEST_TIMEOUT, SCAN_BASE, UPI_BASE,
    ExternalClientError, ScanClient, UpiClient, public_upi_result,
)


API_KEY = "TEST_API_CREDENTIAL_84723"
BEARER = "test-bearer-sensitive"
CDK = "test-cdk-sensitive"
TOKEN = "test-access-token-sensitive"
JOB_SECRET = "test-job-secret-sensitive"
ENTRY = "http://entry-user:entry-pass@entry.invalid:8080"
EXIT = "socks5://exit-user:exit-pass@exit.invalid:1080"
LINK = "https://payments.stripe.com/upi/instructions/fixture?token=payment-token"
ORDER = {"order_no": "order_1", "link": LINK, "status": "pending"}
CONFIG = {"available": True, "max_batch": 200, "unit_price": "0.7",
          "accepting_supplier_count": 1, "cdk_purchase_url": "https://shop.invalid/cdk"}
BALANCE = {"balance": "10.0", "frozen_balance": "0", "unit_price": "0.7",
           "cdk_purchase_url": "https://shop.invalid/cdk"}
ORDERS = {"items": [ORDER], "pagination": {"page": 1, "page_size": 20, "total": 1, "total_pages": 1}}
SUBMITTED = {"batch_no": "batch_1", "order_count": 1, "total_amount": "0.7",
             "balance": "9.3", "frozen_balance": "0.7", "idempotent": False, "orders": [ORDER]}
CREATED = {"ok": True, "job_id": "job_1-A", "job_secret": JOB_SECRET,
           "status_url": "https://untrusted.invalid/status",
           "cancel_url": "https://untrusted.invalid/cancel"}


class FakeResponse:
    def __init__(self, data=None, status=200, *, raw=None, headers=None, chunks=None):
        self.status_code = status
        self.headers = CaseInsensitiveDict(headers or {})
        self.raw_body = json.dumps(data).encode() if raw is None else raw
        self.chunks = chunks
        self.closed = False
        self.read_count = 0

    def iter_content(self, chunk_size):
        assert 0 < chunk_size <= MAX_RESPONSE_BYTES
        chunks = self.chunks
        if chunks is None:
            chunks = (self.raw_body[i:i + chunk_size] for i in range(0, len(self.raw_body), chunk_size))
        for chunk in chunks:
            self.read_count += 1
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.trust_env = True
        self.closed = False

    def request(self, method, url, **kwargs):
        assert self.trust_env is False
        self.calls.append((method, url, kwargs))
        assert self.responses, "Unexpected additional HTTP request/retry"
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*_args, **_kwargs):
        pytest.fail("Contract tests must never open a network connection")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", blocked)


def envelope(data):
    return {"code": "OK", "data": data}


def operation(name, response):
    session = FakeSession(response)
    scan = ScanClient(API_KEY, BEARER, session)
    upi = UpiClient(CDK, ENTRY, EXIT, session)
    action = {
        "config": scan.config, "orders": scan.orders, "balance": scan.balance,
        "submit": lambda: scan.submit([LINK], "idem-key-123"),
        "create": lambda: upi.create(TOKEN),
        "create_batch": lambda: upi.create_batch([TOKEN, {"access_token": TOKEN}]),
        "cdk_fuse": lambda: upi.cdk_fuse([CDK, "source-cdk-sensitive"]),
        "status": lambda: upi.status("job_1-A", JOB_SECRET),
        "cancel": lambda: upi.cancel("job_1-A", JOB_SECRET),
        "cdk_status": upi.cdk_status,
    }[name]
    return action, session


def test_scan_endpoints_auth_values_and_unwrapping():
    responses = [FakeResponse(envelope(data)) for data in (CONFIG, ORDERS, SUBMITTED, BALANCE)]
    session = FakeSession(*responses)
    client = ScanClient(API_KEY, BEARER, session)
    assert client.config() == CONFIG
    assert client.orders(page=3, page_size=50) == ORDERS
    assert client.submit([LINK], "idem-key-123") == SUBMITTED
    assert client.balance() == BALANCE
    assert [(m, u) for m, u, _ in session.calls] == [
        ("GET", SCAN_BASE + "/api/v1/public/scan/config"),
        ("GET", SCAN_BASE + "/api/v1/client/orders"),
        ("POST", SCAN_BASE + "/api/v1/client/orders"),
        ("GET", SCAN_BASE + "/api/v1/user/overview"),
    ]
    config, orders, submit, balance = [call[2] for call in session.calls]
    assert config["headers"] == {"Accept": "application/json"}
    assert orders["headers"] == {"Accept": "application/json", "X-API-Key": API_KEY}
    assert orders["params"] == {"page": 3, "page_size": 50}
    assert submit["headers"] == {"Accept": "application/json", "X-API-Key": API_KEY,
                                  "Idempotency-Key": "idem-key-123", "Content-Type": "application/json"}
    assert submit["json"] == {"links": [LINK]}
    assert balance["headers"] == {"Accept": "application/json", "Authorization": "Bearer " + BEARER}
    for _, url, kwargs in session.calls:
        assert url.startswith(SCAN_BASE + "/")
        assert kwargs["timeout"] == REQUEST_TIMEOUT
        assert all(0 < value <= 30 for value in kwargs["timeout"])
        assert kwargs["allow_redirects"] is False
        assert kwargs["stream"] is True
        assert kwargs["verify"] is True
        assert kwargs["proxies"] == {}
    assert all(response.closed for response in responses)


def test_orders_default_pagination_and_empty_result():
    data = {"items": [], "pagination": {"page": 1, "page_size": 20, "total": 0, "total_pages": 0}}
    action, session = operation("orders", FakeResponse(envelope(data)))
    assert action() == data
    assert session.calls[0][2]["params"] == {"page": 1, "page_size": 20}


@pytest.mark.parametrize("token", [TOKEN, {"access_token": TOKEN, "extra": {"value": "fixture"}}])
@pytest.mark.parametrize("promo", [True, False])
def test_upi_contract_fixed_none_and_internal_secret(token, promo):
    status = {"status": "error", "result": {"payment_status": "failed"}, "logs": [TOKEN]}
    responses = [FakeResponse(CREATED, 202), FakeResponse(status),
                 FakeResponse({"ok": True}), FakeResponse({"available": 8})]
    session = FakeSession(*responses)
    client = UpiClient(CDK, ENTRY, EXIT, session)
    created = client.create(token, use_promo=promo)
    assert created["job_secret"] == JOB_SECRET
    assert client.status(created["job_id"], created["job_secret"]) == status
    assert client.cancel(created["job_id"], created["job_secret"]) == {"ok": True}
    assert client.cdk_status() == {"available": 8}
    assert [(m, u) for m, u, _ in session.calls] == [
        ("POST", UPI_BASE + "/api/v1/extractions"),
        ("GET", UPI_BASE + "/api/v1/extractions/job_1-A"),
        ("POST", UPI_BASE + "/api/v1/extractions/job_1-A/cancel"),
        ("POST", UPI_BASE + "/api/v1/cdk/status"),
    ]
    create, status_call, cancel, cdk = [call[2] for call in session.calls]
    assert create["json"] == {"cdk": CDK, "token": token, "entry_proxy": ENTRY,
                              "exit_proxy": EXIT, "use_promo": promo, "payment_platform": "none"}
    assert status_call["headers"] == {"Accept": "application/json", "X-UPI-Job-Secret": JOB_SECRET}
    assert cancel["headers"]["X-UPI-Job-Secret"] == JOB_SECRET
    assert cancel["json"] == {}
    assert cdk["json"] == {"cdk": CDK}
    assert all(call[2]["proxies"] == {} for call in session.calls)
    assert all(response.closed for response in responses)


def test_upi_defaults_and_no_configurable_base():
    session = FakeSession(FakeResponse(CREATED, 202))
    UpiClient(CDK, session=session).create(TOKEN)
    body = session.calls[0][2]["json"]
    assert body["use_promo"] is True
    assert body["entry_proxy"] == body["exit_proxy"] == ""
    for cls in (ScanClient, UpiClient):
        with pytest.raises(TypeError):
            cls(base_url="https://untrusted.invalid")


@pytest.mark.parametrize("platform, options, expected", [
    ("none", {}, {}),
    ("foarge", {}, {"foarge_auth_mode": "pbk", "foarge_credential": API_KEY,
                    "foarge_api_base": FOARGE_API_BASE, "foarge_payment_method": "upi"}),
    ("foarge", {"foarge_auth_mode": "api_key", "foarge_api_base": "https://pay.example.com/api/"},
     {"foarge_auth_mode": "api_key", "foarge_credential": API_KEY,
      "foarge_api_base": "https://pay.example.com/api", "foarge_payment_method": "upi"}),
    ("foarge", {"foarge_auth_mode": "cdk_session"},
     {"foarge_auth_mode": "cdk_session", "foarge_credential": API_KEY,
      "foarge_api_base": FOARGE_API_BASE, "foarge_payment_method": "upi"}),
    ("masi", {}, {"masi_cdk_ticket": CDK, "masi_api_base": MASI_API_BASE,
                  "masi_require_online_workers": True}),
    ("masi", {"masi_require_online_workers": False, "masi_api_base": "https://pay.example.com:8443"},
     {"masi_cdk_ticket": CDK, "masi_api_base": "https://pay.example.com:8443",
      "masi_require_online_workers": False}),
    ("seashore", {}, {"seashore_cdk": CDK}),
])
def test_upi_payment_payloads_are_selected_and_shared_by_single_and_batch(platform, options, expected):
    batch = {"jobs": [{"row": 1, "job_id": "job_1-A", "job_secret": JOB_SECRET,
                       "queue_position": 0}], "rejected": [{"row": 2, "error": "Invalid session"}]}
    session = FakeSession(FakeResponse(CREATED, 202), FakeResponse(batch, 202))
    client = UpiClient(CDK, ENTRY, EXIT, session)
    kwargs = {"payment_platform": platform, "foarge_credential": " " + API_KEY + " ",
              "masi_cdk_ticket": CDK, "seashore_cdk": CDK, **options}
    assert client.create(TOKEN, False, **kwargs) == CREATED
    sessions = [TOKEN, {"access_token": TOKEN}]
    assert client.create_batch(sessions, False, **kwargs) == batch
    common = {"cdk": CDK, "entry_proxy": ENTRY, "exit_proxy": EXIT, "use_promo": False,
              "payment_platform": platform, **expected}
    for call, field, value, suffix in zip(session.calls, ("token", "sessions"),
                                        (TOKEN, sessions), ("", "/batch")):
        assert call[:2] == ("POST", UPI_BASE + "/api/v1/extractions" + suffix)
        assert call[2]["json"] == {**common, field: value}
        assert call[2]["allow_redirects"] is False and call[2]["proxies"] == {}


@pytest.mark.parametrize("kwargs", [
    {"payment_platform": "unknown"}, {"payment_platform": ["foarge", "masi"]},
    {"payment_platform": True}, {"payment_platform": {"foarge": True}},
    {"payment_platform": "foarge,masi"}, {"payment_platform": "foarge"},
    {"payment_platform": "masi", "masi_cdk_ticket": " "},
    {"payment_platform": "seashore", "seashore_cdk": True},
    {"payment_platform": "foarge", "foarge_credential": API_KEY, "foarge_auth_mode": "other"},
    {"payment_platform": "foarge", "foarge_credential": API_KEY, "foarge_auth_mode": ["pbk"]},
    {"use_promo": 1}, {"use_promo": "false"},
    {"masi_require_online_workers": 0}, {"masi_require_online_workers": "true"},
])
def test_upi_payment_validation_precedes_transport(kwargs):
    session = FakeSession()
    client = UpiClient(CDK, session=session)
    for method, value in ((client.create, TOKEN), (client.create_batch, [TOKEN])):
        with pytest.raises(ExternalClientError) as caught:
            method(value, **kwargs)
        assert caught.value.uncertain is False
        assert all(secret not in repr(vars(caught.value)) for secret in (TOKEN, CDK, API_KEY))
    assert session.calls == []


@pytest.mark.parametrize("base", [
    "http://foarge.com/api", "https://localhost", "https://foo.local", "https://pay.internal",
    "https://127.0.0.1", "https://10.0.0.1", "https://8.8.8.8", "https://[::1]",
    "https://2130706433", "https://user:pass@foarge.com/api", "https://foarge.com?key=x",
    "https://foarge.com#key=x", "https://foo..com", "https://foo.com:99999",
    "https://foo.com\\@localhost", "https://foo.com\n", "https:///api", None,
])
def test_upi_payment_base_requires_https_public_dns_syntax(base):
    session = FakeSession()
    client = UpiClient(CDK, session=session)
    for platform, credential in (("foarge", "foarge_credential"), ("masi", "masi_cdk_ticket")):
        with pytest.raises(ExternalClientError) as caught:
            client.create(TOKEN, payment_platform=platform,
                          **{credential: CDK, platform + "_api_base": base})
        assert caught.value.code == "INVALID_REQUEST" and not caught.value.uncertain
    assert session.calls == []


def test_upi_rejects_legacy_enable_flags_and_configurable_seashore_base():
    session = FakeSession()
    client = UpiClient(CDK, session=session)
    for key in ("foarge_auto_submit", "masi_auto_submit", "seashore_auto_submit", "seashore_api_base"):
        for method, value in ((client.create, TOKEN), (client.create_batch, [TOKEN])):
            with pytest.raises(TypeError):
                method(value, **{key: True})
    assert session.calls == []


@pytest.mark.parametrize("sessions", [[], TOKEN, {}, [None], [True], [{}], ["  "]])
def test_upi_batch_rejects_invalid_sessions_locally(sessions):
    session = FakeSession()
    with pytest.raises(ExternalClientError) as caught:
        UpiClient(CDK, session=session).create_batch(sessions)
    assert caught.value.code == "INVALID_REQUEST" and not caught.value.uncertain
    assert session.calls == []


def test_upi_batch_preserves_partial_results_without_assuming_input_coverage():
    job = {"row": 3, "job_id": "job_3", "job_secret": JOB_SECRET, "queue_position": 2,
           "future_field": {"keep": True}}
    rejected = {"row": 1, "error": "Bad session " + TOKEN}
    cases = [
        ({"jobs": [job], "rejected": [rejected]}, False),
        ({"jobs": [], "rejected": [rejected], "ok": False}, False),
        ({"jobs": [job, {"row": 2, "job_id": "job_2"}], "rejected": []}, False),
        ({"jobs": [job], "rejected": {}}, True),
        ({"jobs": [job]}, False),
        ({"jobs": [job], "rejected": None}, True),
        ({"jobs": {}, "rejected": []}, True),
        ({"rejected": []}, True),
        ({"jobs": [job], "rejected": [None]}, False),
        ({"jobs": [job], "rejected": [], "ok": False}, True),
        ({"jobs": [], "rejected": []}, False),
    ]
    for payload, uncertain in cases:
        original = copy.deepcopy(payload)
        session = FakeSession(FakeResponse(payload, 202))
        result = UpiClient(CDK, session=session).create_batch([TOKEN] * 4)
        assert result == ({**payload, "uncertain": True} if uncertain else payload)
        assert payload == original
        assert len(session.calls) == 1


@pytest.mark.parametrize("duplicate, status", [(False, 201), (True, 200)])
def test_upi_cdk_fuse_normalizes_codes_and_accepts_public_js_shape(duplicate, status):
    result = {"duplicate": duplicate, "transferred_points": "1.50" if duplicate else 1.5,
              "target": {"remaining_uses": 9, "total_uses": 15, "enabled": True, "code": CDK},
              "future_field": "retained"}
    session = FakeSession(FakeResponse(result, status), FakeResponse(result, status))
    client = UpiClient(session=session)
    codes = [" " + CDK + " ", "source-cdk"]
    assert client.cdk_fuse(codes, " 融合测试 ") == result
    call = session.calls[0]
    assert call[:2] == ("POST", UPI_BASE + "/api/v1/cdk/fuse")
    assert call[2]["json"] == {"codes": [CDK, "source-cdk"], "label": "融合测试"}
    assert codes == [" " + CDK + " ", "source-cdk"]
    assert client.cdk_fuse(["card-" + str(i) for i in range(100)], "x" * 120) == result


@pytest.mark.parametrize("codes, label", [
    ([CDK], ""), ([CDK] * 101, ""), ([CDK, " " + CDK], ""),
    ([CDK, "source", CDK], ""), ([CDK, "source", " source "], ""),
    ([CDK, " "], ""), ([CDK, "source code"], ""), ([CDK, True], ""), (CDK, ""),
    ([CDK, "source"], "x" * 121), ([CDK, "source"], None),
])
def test_upi_cdk_fuse_rejects_invalid_input_before_send(codes, label):
    session = FakeSession()
    with pytest.raises(ExternalClientError) as caught:
        UpiClient(session=session).cdk_fuse(codes, label)
    assert not caught.value.uncertain and session.calls == []


def test_upi_cdk_fuse_requires_confirmed_result_types():
    valid = {"duplicate": False, "transferred_points": 2, "target": {"remaining_uses": 5}}
    for field, value in (("duplicate", 1), ("transferred_points", True),
                         ("transferred_points", -1), ("transferred_points", "-0.1"),
                         ("transferred_points", "NaN"), ("transferred_points", "Infinity"),
                         ("target", {}), ("target", {"remaining_uses": "invalid"})):
        action, _ = operation("cdk_fuse", FakeResponse({**valid, field: value}, 201))
        with pytest.raises(ExternalClientError) as caught:
            action()
        assert caught.value.code == "INVALID_RESPONSE" and caught.value.uncertain


@pytest.mark.parametrize("name", ["create_batch", "cdk_fuse"])
def test_upi_new_mutations_keep_safe_errors_and_never_replay(name):
    cases = [
        (requests.Timeout(TOKEN + CDK + ENTRY), "TIMEOUT", True),
        (FakeResponse({"error": CDK}, 502), "UPSTREAM_ERROR", True),
        (FakeResponse({"error": CDK}, 429), "RATE_LIMITED", False),
        (FakeResponse(status=307, headers={"Location": LINK}), "REDIRECT_BLOCKED", True),
        (FakeResponse(raw=b"not json"), "INVALID_RESPONSE", True),
        (FakeResponse({}), "INVALID_RESPONSE", True),
        (FakeResponse({"ok": False, "error": CDK}), "BUSINESS_ERROR", False),
    ]
    for response, code, uncertain in cases:
        action, session = operation(name, response)
        with pytest.raises(ExternalClientError) as caught:
            action()
        assert caught.value.code == code and caught.value.uncertain is uncertain
        assert all(secret not in repr(vars(caught.value)) for secret in (TOKEN, CDK, ENTRY))
        assert len(session.calls) == 1
        assert session.calls[0][2]["allow_redirects"] is False
        if isinstance(response, FakeResponse):
            assert response.closed


@pytest.mark.parametrize("code, expected", [
    ("INSUFFICIENT_BALANCE", "账户余额不足"), ("DUPLICATE_LINK", "支付链接已提交或本批次存在重复链接"),
    ("INVALID_API_KEY", "接口密钥无效或已撤销"), ("FUTURE_BUSINESS_CODE", "外部服务未接受此请求"),
    ("UNKNOWN", "外部服务未接受此请求"), (API_KEY, "外部服务未接受此请求"),
    ("bad code " + TOKEN, "外部服务未接受此请求"),
])
def test_scan_business_errors_are_safe_and_definite(code, expected):
    response = FakeResponse({"code": code, "message": API_KEY + TOKEN, "headers": {"secret": TOKEN}})
    action, session = operation("submit", response)
    with pytest.raises(ExternalClientError) as caught:
        action()
    error = caught.value
    assert error.message == expected
    assert str(error) == expected
    assert error.status == 200
    assert error.uncertain is False
    assert error.code == (code if code in ("INSUFFICIENT_BALANCE", "DUPLICATE_LINK", "INVALID_API_KEY",
                                          "FUTURE_BUSINESS_CODE") else "BUSINESS_ERROR")
    assert error.code != "UNKNOWN"
    assert API_KEY not in repr(vars(error)) and TOKEN not in repr(error)
    assert response.closed and len(session.calls) == 1


@pytest.mark.parametrize("name", ["create", "status", "cancel", "cdk_status"])
def test_upi_explicit_business_failure_with_no_schema(name):
    action, _ = operation(name, FakeResponse({"ok": False, "error": TOKEN, "message": CDK}))
    with pytest.raises(ExternalClientError) as caught:
        action()
    assert caught.value.code == "BUSINESS_ERROR"
    assert caught.value.uncertain is False
    assert TOKEN not in str(caught.value) and CDK not in str(caught.value)


@pytest.mark.parametrize("name", ["submit", "create", "status", "cancel", "cdk_status"])
@pytest.mark.parametrize("retry, expected", [("37", 37), ("0", 0), (" 12 ", 12), ("86400", 86400),
                                          ("-1", None), ("999999999999999999", None), ("90000", None),
                                          ("1.5", None), (TOKEN, None), ("１２", None)])
def test_429_preserves_only_bounded_retry_after_and_never_retries(name, retry, expected):
    action, session = operation(name, FakeResponse({"code": "RATE_LIMITED", "message": TOKEN}, 429,
                                                  headers={"retry-after": retry}))
    with pytest.raises(ExternalClientError) as caught:
        action()
    assert caught.value.code == "RATE_LIMITED"
    assert caught.value.status == 429
    assert caught.value.retry_after == expected
    assert caught.value.uncertain is False
    assert len(session.calls) == 1


@pytest.mark.parametrize("name, mutation", [("config", False), ("orders", False), ("balance", False),
                                           ("submit", True), ("create", True), ("status", False),
                                           ("cancel", True), ("cdk_status", False)])
@pytest.mark.parametrize("exception, code", [(requests.Timeout, "TIMEOUT"),
                                           (requests.ConnectTimeout, "TIMEOUT"),
                                           (requests.ConnectionError, "NETWORK_ERROR"),
                                           (requests.exceptions.ChunkedEncodingError, "NETWORK_ERROR")])
def test_network_errors_have_mutation_uncertainty_and_safe_tracebacks(name, mutation, exception, code):
    action, session = operation(name, exception(TOKEN + JOB_SECRET + ENTRY))
    with pytest.raises(ExternalClientError) as caught:
        action()
    error = caught.value
    assert error.code == code
    assert error.status is None
    assert error.uncertain is mutation
    rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    for secret in (TOKEN, JOB_SECRET, ENTRY):
        assert secret not in rendered and secret not in repr(vars(error))
    assert len(session.calls) == 1


@pytest.mark.parametrize("name, mutation", [("submit", True), ("create", True), ("status", False),
                                           ("cancel", True), ("cdk_status", False)])
@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 429, 500, 502, 503])
def test_http_errors_even_if_body_claims_success(name, mutation, status):
    action, session = operation(name, FakeResponse({"code": "OK", "ok": True, "data": SUBMITTED}, status))
    with pytest.raises(ExternalClientError) as caught:
        action()
    assert caught.value.status == status
    assert caught.value.code not in ("OK", "UNKNOWN")
    assert caught.value.uncertain is (mutation and status >= 500)
    assert len(session.calls) == 1


@pytest.mark.parametrize("status, code", [(401, "UNAUTHORIZED"), (429, "RATE_LIMITED"), (502, "UPSTREAM_ERROR")])
def test_non_json_http_errors_remain_safe(status, code):
    action, _ = operation("create", FakeResponse(status=status, raw=("<html>" + TOKEN).encode()))
    with pytest.raises(ExternalClientError) as caught:
        action()
    assert caught.value.code == code
    assert caught.value.status == status
    assert caught.value.uncertain is (status >= 500)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("name", ["submit", "create", "status", "cancel"])
def test_redirects_are_rejected_without_forwarding_credentials(name, status):
    response = FakeResponse(status=status, headers={"Location": "https://untrusted.invalid/?access_token=" + TOKEN})
    action, session = operation(name, response)
    with pytest.raises(ExternalClientError) as caught:
        action()
    assert caught.value.code == "REDIRECT_BLOCKED"
    assert caught.value.status == status
    assert caught.value.uncertain is (name != "status")
    assert TOKEN not in str(caught.value)
    assert len(session.calls) == 1 and response.closed
    assert response.read_count == 0
    assert session.calls[0][2]["allow_redirects"] is False


@pytest.mark.parametrize("job_id", ["", "../cdk/status", "https://untrusted.invalid", "//untrusted.invalid",
                                   "job?token=x", "job#fragment", "job%2F..", "job/name", "job\\name",
                                   "job\r\nX-Key: x", "job\n", "任务", "x" * 129, None, 123])
@pytest.mark.parametrize("method", ["status", "cancel"])
def test_job_id_injection_is_rejected_before_any_request(job_id, method):
    session = FakeSession()
    with pytest.raises(ExternalClientError) as caught:
        getattr(UpiClient(session=session), method)(job_id, JOB_SECRET)
    assert caught.value.code == "INVALID_JOB_ID"
    assert caught.value.uncertain is False
    assert session.calls == []


@pytest.mark.parametrize("job_id", ["a", "A_9-Z", "a" * 128])
def test_job_id_boundaries(job_id):
    session = FakeSession(FakeResponse({"status": "queued"}))
    UpiClient(session=session).status(job_id, JOB_SECRET)
    assert session.calls[0][1] == UPI_BASE + "/api/v1/extractions/" + job_id


@pytest.mark.parametrize("name", ["config", "orders", "submit", "balance", "create", "status", "cancel", "cdk_status"])
@pytest.mark.parametrize("raw", [b"", b"not-json", b"[]", b"null", b"42", b'"string"', b"{\"x\":NaN}", b"\xff"])
def test_malformed_success_bodies_are_never_accepted(name, raw):
    response = FakeResponse(raw=raw)
    action, _ = operation(name, response)
    with pytest.raises(ExternalClientError) as caught:
        action()
    assert caught.value.code == "INVALID_RESPONSE"
    assert caught.value.status == 200
    assert caught.value.uncertain is (name in ("submit", "create", "cancel"))
    assert response.closed


@pytest.mark.parametrize("name, data, field, bad", [
    ("config", CONFIG, "available", "true"), ("config", CONFIG, "max_batch", True),
    ("config", CONFIG, "unit_price", 0.7), ("config", CONFIG, "accepting_supplier_count", -1),
    ("balance", BALANCE, "balance", "NaN"), ("balance", BALANCE, "frozen_balance", None),
    ("orders", ORDERS, "items", {}), ("orders", ORDERS, "items", [None]),
    ("orders", ORDERS, "pagination", {"page": 1}),
    ("submit", SUBMITTED, "order_count", 2), ("submit", SUBMITTED, "order_count", True),
    ("submit", SUBMITTED, "orders", [{"order_no": "1"}]),
    ("submit", SUBMITTED, "idempotent", "false"), ("submit", SUBMITTED, "total_amount", "-1"),
])
def test_scan_endpoint_specific_shapes(name, data, field, bad):
    malformed = copy.deepcopy(data)
    malformed[field] = bad
    action, _ = operation(name, FakeResponse(envelope(malformed)))
    with pytest.raises(ExternalClientError) as caught:
        action()
    assert caught.value.code == "INVALID_RESPONSE"
    assert caught.value.uncertain is (name == "submit")


@pytest.mark.parametrize("body", [{}, {"data": SUBMITTED}, {"code": None}, {"code": "OK"},
                                  {"code": "OK", "data": []}, {"code": "OK", "data": {}}])
def test_scan_requires_success_code_and_data(body):
    action, _ = operation("submit", FakeResponse(body))
    with pytest.raises(ExternalClientError) as caught:
        action()
    assert caught.value.code == "INVALID_RESPONSE"
    assert caught.value.uncertain is True


@pytest.mark.parametrize("field, value", [("ok", None), ("ok", 1), ("job_id", "../job"),
                                         ("job_secret", ""), ("job_secret", "secret\nheader")])
def test_upi_create_requires_persistable_job_credentials(field, value):
    data = {**CREATED, field: value}
    action, _ = operation("create", FakeResponse(data, 202))
    with pytest.raises(ExternalClientError) as caught:
        action()
    assert caught.value.code == "INVALID_RESPONSE"
    assert caught.value.uncertain is True


@pytest.mark.parametrize("headers", [{}, {"Content-Length": "1"}, {"Content-Length": "invalid"}])
def test_stream_limit_uses_actual_decoded_bytes(headers):
    response = FakeResponse(headers=headers, chunks=[b"x" * MAX_RESPONSE_BYTES, b"x", b"unread"])
    action, _ = operation("create", response)
    with pytest.raises(ExternalClientError) as caught:
        action()
    assert caught.value.code == "RESPONSE_TOO_LARGE"
    assert caught.value.uncertain is True
    assert response.read_count == 2 and response.closed


@pytest.mark.parametrize("length", [str(MAX_RESPONSE_BYTES + 1), "9" * 100, "000000000" + str(MAX_RESPONSE_BYTES + 1)])
def test_declared_oversize_response_is_closed_without_reading(length):
    response = FakeResponse(headers={"Content-Length": length})
    action, _ = operation("status", response)
    with pytest.raises(ExternalClientError) as caught:
        action()
    assert caught.value.code == "RESPONSE_TOO_LARGE"
    assert caught.value.uncertain is False
    assert response.read_count == 0 and response.closed


def test_response_exactly_at_limit_is_allowed():
    raw = b'{"ok":true}'
    raw += b" " * (MAX_RESPONSE_BYTES - len(raw))
    action, _ = operation("status", FakeResponse(raw=raw))
    assert action() == {"ok": True}


def test_decompressed_gzip_is_bounded():
    raw = gzip.compress(b"x" * (MAX_RESPONSE_BYTES + 1))
    response = requests.Response()
    response.status_code = 200
    response.headers["Content-Length"] = str(len(raw))
    response.raw = HTTPResponse(body=io.BytesIO(raw), headers={"Content-Encoding": "gzip"}, preload_content=False)
    action, _ = operation("create", response)
    with pytest.raises(ExternalClientError) as caught:
        action()
    assert caught.value.code == "RESPONSE_TOO_LARGE"
    assert response.raw.closed


def test_network_error_mid_stream_keeps_http_metadata():
    response = FakeResponse(chunks=[b'{"ok":', requests.ConnectionError(TOKEN)],
                            headers={"Retry-After": "7"})
    action, _ = operation("create", response)
    with pytest.raises(ExternalClientError) as caught:
        action()
    assert caught.value.code == "NETWORK_ERROR"
    assert caught.value.status == 200
    assert caught.value.retry_after == 7 and caught.value.uncertain
    assert response.closed


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("exception", [requests.Timeout, requests.ConnectionError, requests.HTTPError])
def test_transport_exception_preserves_attached_http_status(status, exception):
    response = requests.Response()
    response.status_code = status
    response.headers["Retry-After"] = "9"
    response.raw = io.BytesIO(TOKEN.encode())
    action, session = operation("create", exception(TOKEN, response=response))
    with pytest.raises(ExternalClientError) as caught:
        action()
    assert caught.value.status == status
    assert caught.value.retry_after == 9
    assert caught.value.uncertain is (status == 503)
    assert TOKEN not in repr(vars(caught.value))
    assert response.raw.closed and len(session.calls) == 1


@pytest.mark.parametrize("exception", [requests.exceptions.InvalidHeader, requests.exceptions.InvalidURL,
                                      requests.exceptions.InvalidJSONError, requests.exceptions.InvalidSchema])
def test_transport_parameter_errors_do_not_imply_a_submission(exception):
    action, _ = operation("create", exception(TOKEN))
    with pytest.raises(ExternalClientError) as caught:
        action()
    assert caught.value.code == "INVALID_REQUEST" and caught.value.uncertain is False
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize("token", ["x" * MAX_REQUEST_BYTES, {"access_token": "中" * (MAX_REQUEST_BYTES // 4)}],
                         ids=["ascii-limit", "json-escaped-limit"])
def test_request_size_is_bounded_before_send(token):
    session = FakeSession()
    with pytest.raises(ExternalClientError) as caught:
        UpiClient(CDK, session=session).create(token)
    assert caught.value.code == "REQUEST_TOO_LARGE"
    assert caught.value.uncertain is False and session.calls == []


def test_circular_input_and_invalid_local_headers_are_safe():
    token = {}
    token["token"] = token
    session = FakeSession()
    with pytest.raises(ExternalClientError) as caught:
        UpiClient(CDK, session=session).create(token)
    assert caught.value.code == "INVALID_REQUEST"
    with pytest.raises(ExternalClientError):
        ScanClient(API_KEY + "\r\nInjected: x", session=session).orders()
    with pytest.raises(ExternalClientError):
        ScanClient(API_KEY, session=session).submit([LINK], "short")
    assert session.calls == []


def test_real_session_preparation_disables_ambient_auth_proxy_hooks_and_retries(monkeypatch):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(key, "http://env-proxy.invalid:8080")
    session = requests.Session()
    session.proxies = {"https": "http://session-proxy.invalid:8080"}
    session.headers["Authorization"] = "Bearer ambient-secret"
    session.headers["X-API-Key"] = "ambient-key"
    session.params["access_token"] = "ambient-token"
    session.auth = ("ambient-user", "ambient-password")
    session.cookies.set("secret", "ambient-cookie")
    session.hooks["response"].append(lambda *_a, **_k: pytest.fail("Ambient hook ran"))
    prepared = []

    class RecordingAdapter(HTTPAdapter):
        def send(self, request, **kwargs):
            prepared.append((request, kwargs))
            response = requests.Response()
            response.status_code = 200
            response.headers = {}
            response.raw = io.BytesIO(json.dumps(envelope(CONFIG)).encode())
            response.request = request
            response.url = request.url
            return response

    adapter = RecordingAdapter(max_retries=Retry(total=4, allowed_methods=None))
    session.mount(SCAN_BASE + "/", adapter)
    with ScanClient(API_KEY, BEARER, session) as client:
        assert client.config() == CONFIG
        assert session.trust_env is False
        assert adapter.max_retries.total == 0
        assert adapter.max_retries.connect == adapter.max_retries.read == adapter.max_retries.status == 0
    assert len(prepared) == 1
    request, kwargs = prepared[0]
    assert request.url == SCAN_BASE + "/api/v1/public/scan/config"
    assert "Authorization" not in request.headers
    assert "X-API-Key" not in request.headers
    assert "Cookie" not in request.headers
    assert kwargs["proxies"] == {}
    assert kwargs["timeout"] == REQUEST_TIMEOUT


def test_public_result_normalizes_confirmed_fields_and_preserves_payment_token():
    result = {"short_link": LINK, "verification_url": "https://verify.invalid/?token=pay-token",
              "checkout_url": "https://checkout.invalid/pay", "qr_data": "upi://pay?pa=fixture&token=pay-token",
              "qr_image_png": "data:image/png;base64,AAAA", "qr_image_svg": "data:image/svg+xml;base64,AAAA",
              "payment_status": "awaiting_payment", "payment_check_status": "running",
              "upi_payment_state": "pending", "upi_setup_status": "pending", "expires_at": 1900000000,
              "promo_requested": True, "promo_applied": False, "upi_mandate_succeeded": False,
              "entitlement_active": False, "has_active_subscription": False, "plan_type": "plus",
              "payment_platform": "none", "payment_submit_status": "idle", "payment_submit_final": False,
              "access_token": TOKEN, "job_secret": JOB_SECRET, "cdk": CDK, "entry_proxy": ENTRY,
              "account_email": "private@example.invalid", "checkout_session_id": "private-session",
              "payment_submit_id": "private-submit", "payment_submit_error": TOKEN,
              "unknown": {"access_token": TOKEN}, "logs": [TOKEN], "amount": {"token": TOKEN}}
    payload = {"result": result, "status": "error", "access_token": TOKEN, "logs": [TOKEN]}
    before = copy.deepcopy(payload)
    public = public_upi_result(result)
    assert public["payment_link"] == LINK
    assert public["qr_code"] == "data:image/png;base64,AAAA"
    assert public["payment_status"] == "awaiting_payment"
    assert public["account_email"] == "private@example.invalid"
    assert public["qr_data"].endswith("token=pay-token")
    assert public["verification_url"].endswith("token=pay-token")
    assert public["promo_applied"] is False
    assert public["expires_at"] == 1900000000
    assert public["payment_submit_id"] == "private-submit"
    assert public["payment_submit_error"] == "自动支付提交失败，请检查平台配置"
    assert all(key not in public for key in ("job_secret", "access_token", "cdk", "entry_proxy",
                                           "checkout_session_id",
                                           "unknown", "logs", "amount", "status"))
    serialized = json.dumps(public)
    assert all(secret not in serialized for secret in (TOKEN, JOB_SECRET, CDK, ENTRY))
    assert payload == before
    assert public_upi_result(payload) == public


def test_public_payment_submission_legacy_fallback_never_implies_paid():
    legacy = {"foarge_status": "submitted", "foarge_task_id": "task-42",
              "foarge_error": "No online workers", "payment_status": "awaiting_payment",
              "entitlement_active": False}
    public = public_upi_result(legacy)
    assert public["payment_platform"] == "foarge"
    assert public["payment_submit_status"] == public["foarge_status"] == "submitted"
    assert public["payment_submit_id"] == public["foarge_task_id"] == "task-42"
    assert public["payment_submit_error"] == public["foarge_error"] == "No online workers"
    assert public["payment_status"] == "awaiting_payment" and not public["entitlement_active"]
    current = public_upi_result({**legacy, "payment_platform": "masi",
                                "payment_submit_status": "processing", "payment_submit_id": "current-42"})
    assert current["payment_platform"] == "masi" and current["payment_submit_status"] == "processing"
    assert current["payment_submit_id"] == "current-42"
    assert public_upi_result({"payment_submit_status": "completed"})["payment_status"] == ""


@pytest.mark.parametrize("error", [
    TOKEN, CDK, ENTRY, "user:pass@host:8080", "access%5Ftoken=opaque", "sk-abcd1234ABCD5678", "卡密失效：opaque",
    "opaque-original-745", "entry-pass", "x" * 513, "first\nsecond",
])
def test_public_payment_errors_redact_secrets_keys_sensitive_words_and_unbounded_text(error):
    payload = {"access_token": "opaque-original-745", "entry_proxy": ENTRY,
               "result": {"payment_submit_error": error, "foarge_error": error,
                          "payment_submit_id": TOKEN, "foarge_task_id": "x" * 129}}
    before = copy.deepcopy(payload)
    public = public_upi_result(payload)
    assert public["payment_submit_error"] == public["foarge_error"] == "自动支付提交失败，请检查平台配置"
    assert "payment_submit_id" not in public and "foarge_task_id" not in public
    assert error not in json.dumps(public, ensure_ascii=False)
    assert payload == before


def test_public_payment_metadata_rejects_nested_values_and_keeps_bounded_plain_error():
    error = "支付暂时繁忙" * 80
    public = public_upi_result({"payment_submit_error": error, "foarge_error": {"token": TOKEN},
                               "payment_submit_id": [CDK], "foarge_status": "x" * 65})
    assert public["payment_submit_error"] == error
    assert all(key not in public for key in ("foarge_error", "payment_submit_id", "foarge_status"))


@pytest.mark.parametrize("index", range(5))
def test_public_link_priority_matches_public_javascript(index):
    fields = ["short_link", "verification_url", "provider_redirect_url", "checkout_url", "qr_data"]
    result = {key: "https://pay.invalid/" + key + "?token=pay-token" for key in fields[index:]}
    assert public_upi_result({"result": result})["payment_link"] == result[fields[index]]


@pytest.mark.parametrize("state", ["activation_pending", "verification_unavailable", "completed", "failed",
                                   "expired", "payment_timeout", "plus_not_received"])
def test_terminal_payment_states_hide_qr_like_public_javascript(state):
    public = public_upi_result({"payment_status": state, "qr_image_png": "data:image/png;base64,AAAA"})
    assert public["payment_status"] == state and public["qr_code"] == ""
    assert "qr_image_png" not in public


@pytest.mark.parametrize("url", ["https://pay.invalid/?access_token=" + TOKEN,
                                "https://pay.invalid/?ACCESS_TOKEN=" + TOKEN,
                                "https://pay.invalid/?%61ccess_token=" + TOKEN,
                                "https://pay.invalid/#job_secret=" + JOB_SECRET,
                                "https://user:password@pay.invalid/", "javascript:alert(1)", "https://"])
def test_public_link_excludes_explicit_auth_credentials_and_active_schemes(url):
    public = public_upi_result({"checkout_url": url, "short_link": LINK})
    assert "checkout_url" not in public
    assert public["payment_link"] == LINK


@pytest.mark.parametrize("qr", ["data:image/svg+xml,%3Csvg%3E%3C/svg%3E",
                               "data:image/svg+xml;charset=utf-8,%3Csvg%3E%3C/svg%3E",
                               "data:image/svg+xml;base64,AAAA", "https://pay.invalid/qr.svg?token=payment"])
def test_svg_qr_fallback_preserves_supported_image_uris(qr):
    public = public_upi_result({"result": {"qr_image_svg": qr, "payment_status": "awaiting_payment"}})
    assert public["qr_code"] == qr


@pytest.mark.parametrize("value", [None, [], "secret", {"result": []}, {"result": None}])
def test_public_result_invalid_shapes(value):
    assert public_upi_result(value) == {}


def test_public_result_drops_nested_values_and_has_stable_canonical_keys():
    public = public_upi_result({"result": {"short_link": {"access_token": TOKEN},
                                          "qr_image_png": [TOKEN], "payment_status": {"token": TOKEN},
                                          "promo_applied": "true", "expires_at": float("inf")}})
    assert public == {"payment_link": "", "qr_code": "", "account_email": "", "payment_status": ""}


@pytest.mark.parametrize("email", [None, {"access_token": TOKEN}, [TOKEN], TOKEN,
                                  "fixture@example.invalid\n" + TOKEN, "@example.invalid",
                                  "fixture@@example.invalid", "fixture\x00@example.invalid",
                                  "x" * 321 + "@example.invalid"],
                         ids=["null", "nested-object", "list", "token", "newline", "missing-local",
                              "multiple-at", "control", "oversize"])
def test_raw_result_email_does_not_expose_unstructured_data(email):
    public = public_upi_result({"account_email": email, "payment_status": "awaiting_payment",
                               "short_link": LINK, "job_secret": JOB_SECRET, "logs": [TOKEN]})
    assert public["account_email"] == ""
    assert public["payment_link"] == LINK
    assert public["payment_status"] == "awaiting_payment"
    assert TOKEN not in json.dumps(public) and JOB_SECRET not in json.dumps(public)

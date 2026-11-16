"""Imported SMS tests use a temporary store, virtual time and synthetic HTTP only."""

from dataclasses import replace
from email.message import Message
import hashlib
from io import BytesIO
import json
import threading
import traceback
from urllib.error import HTTPError, URLError
from urllib.request import OpenerDirector, Request

import pytest

from codex_oauth import imported_sms as adapter
from codex_oauth.sms import SmsActivation, SmsError, SmsSettings, check_sms_balance
from common.sms_number_pool import SmsNumberPool, SmsPoolError


PRIVATE_UUID = "00000000-0000-4000-8000-000000000001"
PRIVATE_URL = "https://168smsapi.com/sms/" + PRIVATE_UUID
SECRET_CODE = "654321"


class Clock:
    def __init__(self):
        self.now = 10000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Response(BytesIO):
    def __init__(self, body="", *, url=PRIVATE_URL, status=200, headers=None):
        super().__init__(body.encode() if isinstance(body, str) else body)
        self.url = url
        self.status = status
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value
        self.read_limits = []

    def geturl(self):
        return self.url

    def getcode(self):
        return self.status

    def read(self, size=-1):
        self.read_limits.append(size)
        assert 0 < size <= adapter.MAX_RESPONSE_BYTES + 1
        return super().read(size)


class Inbox:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append((request, timeout))
        assert self.responses, "unexpected synthetic request"
        item = self.responses.pop(0)
        if callable(item):
            item = item()
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, Response) else Response(item)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("tests must not perform real HTTP requests")

    monkeypatch.setattr(OpenerDirector, "open", blocked)


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(adapter, "time", clock)
    return clock


@pytest.fixture
def pool(tmp_path, clock):
    store = SmsNumberPool(tmp_path / "numbers.sqlite3", clock=clock.monotonic)
    result = store.import_text(f"2026-09-19|12025550123|{PRIVATE_URL}")
    assert result["imported"] == 1
    return store


@pytest.fixture
def make_client(pool, clock):
    def make(*responses, settings=None, timeout=5, store=None):
        settings = settings or SmsSettings(enabled=True, provider="imported_pool",
                                            timeout_seconds=3, poll_interval_ms=1000)
        client = adapter.ImportedPoolSmsClient(settings, timeout=timeout,
                                               pool=pool if store is None else store)
        inbox = Inbox(*responses)
        client._opener = inbox
        return client, inbox

    return make


def row(pool):
    return pool.list_numbers()["items"][0]


def assert_released(pool, clock, *, uses=0):
    item = row(pool)
    assert item["uses"] == uses
    assert item["status"] == "cooling"
    assert item["cooldown_until"] == clock.now + 3600
    assert pool.summary()["reserved"] == 0


def assert_private(error, *extra):
    rendered = "".join(traceback.format_exception(error.type, error.value, error.tb))
    for secret in (PRIVATE_URL, PRIVATE_UUID, SECRET_CODE, *extra):
        assert secret not in str(error.value)
        assert secret not in rendered


@pytest.mark.parametrize(("body", "expected"), [
    ("123456", ["123456"]),
    ("Your verification code is 012345. Do not share it.", ["012345"]),
    ("【服务】验证码654321，5分钟内有效。", ["654321"]),
    ("Old code: 123456\nNew code: 234567\nRepeat: 123456", ["123456", "234567"]),
    ("123456 234567", ["123456", "234567"]),
    ('{"code":200,"data":{"sms":"Your code is 123456"}}', ["123456"]),
    ('{"code":200,"data":{"verification_code":"012345"}}', ["012345"]),
    ('{"data":[{"otp":123456},{"smsCode":"234567"}]}', ["123456", "234567"]),
    ('{"messages":["code 123456",{"content":"验证码 234567"}]}', ["123456", "234567"]),
    ('{"code":200,"data":"Your code is 123456"}', ["123456"]),
    ('["code 123456", "code 234567"]', ["123456", "234567"]),
    ('"123456"', ["123456"]),
    ('<html><body><p>Your code is <b>123456</b>.</p><p>验证码：&#48;23456</p></body></html>',
     ["123456", "023456"]),
    ('<script>var otp="654321";</script><style>.x{color:#123456}</style>'
     '<div data-id="345678">No messages</div>', []),
    ("code=200", []),
    ('{"code":200,"phone":"+12025550123","date":"202609","id":123456}', []),
    ('{"metadata":{"timestamp":123456,"phone":"123456"},"request_id":"654321"}', []),
    ("Phone +12025550123, date 2026-09-19, HTTP 200", []),
    ("+1 (202) 555-0123; +1202 555123; 20260919; 2026-09-19T12:34:56.123456Z", []),
    ("phone=123456 date=260919 time=123456 id=654321", []),
    ("https://168smsapi.com/sms/123456 abc123456x 1234567", []),
    ("", []),
])
def test_extract_codes(body, expected):
    assert adapter._extract_codes(body) == expected


def test_acquire_snapshots_all_baseline_codes_before_phone_is_returned(make_client, pool, clock):
    old = '<p>Code 111111</p><p>Code 222222</p>'
    client, inbox = make_client(old, 'Code 222222', 'Code 111111', 'Code 333333')
    activation = client.acquire()
    assert len(inbox.requests) == 1  # The caller can send_phone only after this snapshot.
    assert activation == SmsActivation(activation.activation_id, "+12025550123", "US", "",
                                       provider="imported_pool", label="美国 +1")
    assert PRIVATE_URL not in repr(activation)
    assert client.wait_code(activation) == "333333"
    assert len(inbox.requests) == 4
    assert pool.summary()["reserved"] == 1
    assert row(pool)["uses"] == 0
    assert not activation.finished
    assert pool.remember_code(activation.activation_id, "333333") is False
    client.cancel(activation)
    assert_released(pool, clock)


def test_cross_lease_hashes_block_replay_and_finish_is_idempotent(make_client, pool, clock):
    client, _ = make_client("", "code 123456")
    first = client.acquire()
    assert client.wait_code(first) == "123456"
    client.finish(first)
    client.finish(first)
    client.cancel(first)
    assert first.finished
    assert_released(pool, clock, uses=1)
    clock.now += 3600

    other, inbox = make_client("", 'code 123456', 'code 123456', 'code 234567')
    second = other.acquire()
    assert second.activation_id != first.activation_id
    assert other.wait_code(second) == "234567"
    assert len(inbox.requests) == 4
    other.finish(second)
    other.cancel(second)
    assert_released(pool, clock, uses=2)
    clock.now += 3600
    reservation = pool.reserve()
    assert hashlib.sha256(b"123456").hexdigest() in reservation["used_codes"]
    assert hashlib.sha256(b"234567").hexdigest() in reservation["used_codes"]
    assert "123456" not in reservation["used_codes"]
    pool.cancel(reservation["lease_id"])


def test_repeated_wait_calls_do_not_return_the_same_code(make_client, pool, clock):
    client, _ = make_client("", "code 123456", "code 123456", "code 234567")
    activation = client.acquire()
    assert client.wait_code(activation) == "123456"
    assert client.wait_code(activation) == "234567"
    client.cancel(activation)
    client.cancel(activation)
    client.finish(activation)
    assert_released(pool, clock)


def test_atomic_remember_false_blocks_a_concurrent_replay(make_client, pool, monkeypatch):
    client, _ = make_client("", "code 123456", "code 123456", "code 234567")
    activation = client.acquire()
    # Another reader records the code after this client acquired its snapshot.
    pool.remember_code(activation.activation_id, "123456")
    recorded = []
    remember = pool.remember_code

    def record(lease_id, code):
        recorded.append(code)
        return remember(lease_id, code)

    monkeypatch.setattr(pool, "remember_code", record)
    assert client.wait_code(activation) == "234567"
    assert recorded == ["123456", "234567"]


def test_timeout_releases_and_obeys_poll_interval_and_remaining_http_budget(make_client, pool, clock):
    settings = SmsSettings(provider="imported_pool", timeout_seconds=2, poll_interval_ms=1500)
    client, inbox = make_client("", "no SMS", "no SMS", settings=settings, timeout=7)
    activation = client.acquire()
    with pytest.raises(SmsError, match="超时"):
        client.wait_code(activation)
    assert [timeout for _, timeout in inbox.requests] == [7, 2, 0.5]
    assert clock.sleeps == [1.5, 0.5]
    assert activation.finished
    client.cancel(activation)  # CLI/routes also release after wait_code raises.
    client.finish(activation)
    assert_released(pool, clock)


def test_no_code_404_is_an_empty_poll_and_later_code_is_returned(make_client, pool, clock):
    empty = HTTPError(
        PRIVATE_URL, 404, "No Code", Message(),
        BytesIO(b'{"count":0,"messages":[],"msg":"No Code.","status":"000"}'),
    )
    client, inbox = make_client(
        "",
        empty,
        HTTPError(
            PRIVATE_URL, 404, "No Code", Message(),
            BytesIO(b'{"count":0,"messages":[],"msg":"No Code.","status":"000"}'),
        ),
        "code 765432",
    )
    activation = client.acquire()
    assert client.wait_code(activation) == "765432"
    assert len(inbox.requests) == 4
    assert clock.sleeps == [1.0, 1.0]
    client.cancel(activation)


def test_other_404_still_fails(make_client, pool, clock):
    response = HTTPError(PRIVATE_URL, 404, "missing", Message(), BytesIO(b'{"error":"missing"}'))
    client, _ = make_client("", response)
    activation = client.acquire()
    with pytest.raises(SmsError, match="HTTP 404"):
        client.wait_code(activation)
    assert activation.finished
    assert_released(pool, clock)


@pytest.mark.parametrize("stage", ["before", "during_request", "during_sleep", "during_record"])
def test_cancellation_releases_without_counting_success(stage, make_client, pool, clock, monkeypatch):
    event = threading.Event()

    def response():
        if stage == "during_request":
            event.set()
        return "code 123456" if stage != "during_sleep" else "no SMS"

    client, inbox = make_client("", response)
    activation = client.acquire()
    if stage == "before":
        event.set()
    elif stage == "during_sleep":
        def wait(duration):
            clock.sleep(duration)
            event.set()
            return True
        monkeypatch.setattr(event, "wait", wait)
    elif stage == "during_record":
        remember = pool.remember_code
        def record(lease_id, code):
            result = remember(lease_id, code)
            event.set()
            return result
        monkeypatch.setattr(pool, "remember_code", record)
    with pytest.raises(SmsError, match="取消"):
        client.wait_code(activation, cancel_event=event)
    assert len(inbox.requests) == (1 if stage == "before" else 2)
    client.cancel(activation)
    assert_released(pool, clock)


def test_code_arriving_after_deadline_is_not_returned_or_recorded(make_client, pool, clock, monkeypatch):
    def late():
        clock.now += 4
        return "code 123456"

    client, _ = make_client("", late)
    activation = client.acquire()
    monkeypatch.setattr(pool, "remember_code", lambda *args: pytest.fail("late code recorded"))
    with pytest.raises(SmsError, match="超时"):
        client.wait_code(activation)
    assert_released(pool, clock)


@pytest.mark.parametrize("url", [
    "http://168smsapi.com/sms/private", "https://example.invalid/sms/private",
    "https://168smsapi.com.example.invalid/sms/private", "https://168smsapi.com:444/sms/private",
    "https://user@168smsapi.com/sms/private", "https://168smsapi.com/sms/private#secret",
    "https://168smsapi.com/other/private", "https://168smsapi.com/sms/",
    "https://168smsapi.com/sms/private\n", "https://168smsapi.com/sms/private\\secret",
    "https://[invalid/sms/private",
])
def test_url_validation_happens_after_reserve_and_failure_releases(url, make_client, pool, clock, monkeypatch):
    reserve = pool.reserve

    def invalid(**kwargs):
        reservation = reserve(**kwargs)
        reservation["url"] = url
        return reservation

    monkeypatch.setattr(pool, "reserve", invalid)
    client, inbox = make_client()
    with pytest.raises(SmsError) as error:
        client.acquire()
    assert_private(error, url)
    assert inbox.requests == []
    assert_released(pool, clock)


@pytest.mark.parametrize(("key", "value"), [
    ("phone", "+442055501234"), ("phone", None), ("country", "GB"),
    ("url", None), ("used_codes", None), ("used_codes", ["123456"]),
])
def test_invalid_reservation_releases_before_request(key, value, make_client, pool, clock, monkeypatch):
    reserve = pool.reserve

    def invalid(**kwargs):
        reservation = reserve(**kwargs)
        reservation[key] = value
        return reservation

    monkeypatch.setattr(pool, "reserve", invalid)
    client, inbox = make_client()
    with pytest.raises(SmsError) as error:
        client.acquire()
    assert_private(error)
    assert inbox.requests == []
    assert_released(pool, clock)


@pytest.mark.parametrize("phase", ["baseline", "poll"])
@pytest.mark.parametrize("kind", ["404", "network", "timeout", "bad_json", "large_header", "large_body", "bad_final_url"])
def test_failed_http_releases_and_hides_private_data(phase, kind, make_client, pool, clock):
    secret = f"{PRIVATE_URL} code {SECRET_CODE}"
    if kind == "404":
        response = HTTPError(PRIVATE_URL, 404, secret, Message(), BytesIO(secret.encode()))
    elif kind == "network":
        response = URLError(secret)
    elif kind == "timeout":
        response = TimeoutError(secret)
    elif kind == "bad_json":
        response = Response('{"code":', headers={"Content-Type": "application/json"})
    elif kind == "large_header":
        response = Response(secret, headers={"Content-Length": str(adapter.MAX_RESPONSE_BYTES + 1)})
    elif kind == "large_body":
        response = Response(b"x" * (adapter.MAX_RESPONSE_BYTES + 1))
    else:
        response = Response(secret, url="https://example.invalid/" + PRIVATE_UUID)
    client, _ = make_client(*([""] if phase == "poll" else []), response)
    with pytest.raises(SmsError) as error:
        if phase == "baseline":
            client.acquire()
        else:
            client.wait_code(client.acquire())
    assert_private(error)
    if kind == "404":
        assert "HTTP 404" in str(error.value)
    if isinstance(response, (Response, HTTPError)):
        assert response.closed
    assert_released(pool, clock)


def test_remember_error_releases_before_code_can_be_returned(make_client, pool, clock, monkeypatch):
    def failure(*args):
        raise SmsPoolError(f"{PRIVATE_URL} {SECRET_CODE}")

    client, _ = make_client("", "code " + SECRET_CODE)
    activation = client.acquire()
    monkeypatch.setattr(pool, "remember_code", failure)
    with pytest.raises(SmsError) as error:
        client.wait_code(activation)
    assert_private(error)
    assert_released(pool, clock)


def test_reserve_and_summary_errors_are_sanitized(make_client, pool, monkeypatch):
    def failure(*args, **kwargs):
        raise RuntimeError(f"{PRIVATE_URL} {SECRET_CODE}")

    client, inbox = make_client()
    monkeypatch.setattr(pool, "reserve", failure)
    monkeypatch.setattr(pool, "summary", failure)
    for operation in (client.acquire, client.balance):
        with pytest.raises(SmsError) as error:
            operation()
        assert_private(error)
    assert inbox.requests == []


def test_reserve_preserves_store_cooldown_diagnostic(make_client, pool, clock):
    client, inbox = make_client("")
    client.cancel(client.acquire())
    clock.now += 10
    with pytest.raises(SmsPoolError) as store_error:
        pool.reserve()
    with pytest.raises(SmsError) as client_error:
        client.acquire()
    assert str(client_error.value) == str(store_error.value)
    assert "3590" in str(client_error.value)
    assert_private(client_error)
    assert len(inbox.requests) == 1


@pytest.mark.parametrize("method", ["finish", "cancel"])
def test_failed_settlement_is_private_and_retryable(method, make_client, pool, clock, monkeypatch):
    client, _ = make_client("")
    activation = client.acquire()
    pool_method = "complete" if method == "finish" else "cancel"
    original = getattr(pool, pool_method)

    def failure(*args):
        raise SmsPoolError(f"{PRIVATE_URL} {SECRET_CODE}")

    monkeypatch.setattr(pool, pool_method, failure)
    with pytest.raises(SmsError) as error:
        getattr(client, method)(activation)
    assert_private(error)
    assert not activation.finished
    monkeypatch.setattr(pool, pool_method, original)
    getattr(client, method)(activation)
    client.cancel(activation)
    assert_released(pool, clock, uses=int(method == "finish"))


def test_http_response_at_limit_is_allowed_and_closes(make_client):
    response = Response(b" " * (adapter.MAX_RESPONSE_BYTES - 6) + b"123456")
    client, inbox = make_client(response)
    activation = client.acquire()
    assert response.closed
    assert response.read_limits == [adapter.MAX_RESPONSE_BYTES + 1]
    request, timeout = inbox.requests[0]
    assert timeout == 5
    assert request.get_header("Accept-encoding") == "identity"
    assert "no-cache" in request.get_header("Cache-control")
    client.cancel(activation)


@pytest.mark.parametrize("target", [
    "http://168smsapi.com/sms/private", "https://example.invalid/sms/private",
    "https://168smsapi.com.example.invalid/sms/private", "https://168smsapi.com:444/sms/private",
    "https://168smsapi.com/not-sms/private",
])
def test_redirect_handler_blocks_bad_targets_before_following(target):
    handler = adapter._SmsRedirectHandler()
    response = Response()
    with pytest.raises(SmsError) as error:
        handler.redirect_request(Request(PRIVATE_URL), response, 302, "Found", Message(), target)
    assert response.closed
    assert_private(error, target)


def test_redirect_handler_allows_only_valid_https_same_host_url():
    handler = adapter._SmsRedirectHandler()
    target = "https://168smsapi.com/sms/synthetic-next"
    request = handler.redirect_request(Request(PRIVATE_URL), Response(), 302, "Found", Message(), target)
    assert request.full_url == target


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_closes_body_without_unbounded_read_and_keeps_request_budget(status, clock):
    handler = adapter._SmsRedirectHandler()
    redirected = Response("code 123456")
    inbox = Inbox(redirected)
    handler.parent = inbox
    request = Request(PRIVATE_URL)
    request.timeout = 5
    request._sms_deadline = clock.now + 2
    response = Response(b"x" * (adapter.MAX_RESPONSE_BYTES + 1))
    headers = Message()
    headers["Location"] = "/sms/synthetic-next"
    result = getattr(handler, f"http_error_{status}")(request, response, status, "Found", headers)
    assert result is redirected
    assert response.closed
    assert response.read_limits == []
    following, timeout = inbox.requests[0]
    assert following.full_url == "https://168smsapi.com/sms/synthetic-next"
    assert timeout == 2


def test_redirect_limit_releases_and_does_not_follow_again(clock):
    handler = adapter._SmsRedirectHandler()
    inbox = Inbox()
    handler.parent = inbox
    request = Request(PRIVATE_URL)
    request.timeout = 5
    request._sms_redirects = handler.max_redirections
    response = Response()
    headers = Message()
    headers["Location"] = PRIVATE_URL
    with pytest.raises(SmsError, match="重定向次数超限") as error:
        handler.http_error_302(request, response, 302, "Found", headers)
    assert_private(error)
    assert response.closed
    assert inbox.requests == []


def test_environment_proxies_are_disabled(pool, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:8080")
    handlers = []
    build = adapter.build_opener

    def capture(*items):
        handlers.extend(items)
        return build(*items)

    monkeypatch.setattr(adapter, "build_opener", capture)
    adapter.ImportedPoolSmsClient(SmsSettings(provider="imported_pool"), pool=pool)
    assert next(item for item in handlers if isinstance(item, adapter.ProxyHandler)).proxies == {}


def test_default_pool_balance_and_import_factory_use_imported_provider(pool, monkeypatch):
    from codex_oauth import import_jobs, sms
    from common import sms_number_pool

    monkeypatch.setattr(sms_number_pool, "get_sms_pool", lambda: pool)
    monkeypatch.setattr(sms.SmsBowerClient, "__init__", lambda *args, **kwargs: pytest.fail("SMSBower fallback"))
    settings = SmsSettings.from_mapping({"enabled": True, "provider": "imported_pool"})
    assert settings.credentials_configured
    assert settings.provider_label == "导入号码池"
    assert settings.public_mapping()["configured"] is True
    result = check_sms_balance(settings, timeout=4)
    assert result == {"balance": "1", "total": "1", "available": "1", "cooling": "0", "reserved": "0", "exhausted": "0"}
    assert PRIVATE_UUID not in json.dumps(result)
    assert pool.summary()["available"] == 1  # Balance does not reserve or read an inbox.
    client = import_jobs._sms_client_for(settings, 4)
    assert isinstance(client, adapter.ImportedPoolSmsClient)
    assert client.timeout == 4
    assert import_jobs._sms_client_for(replace(settings, enabled=False), 4) is None


def test_default_pool_failure_does_not_fall_back_or_leak(monkeypatch):
    from codex_oauth import import_jobs
    from common import sms_number_pool

    def failure():
        raise SmsPoolError(PRIVATE_URL)

    monkeypatch.setattr(sms_number_pool, "get_sms_pool", failure)
    settings = SmsSettings(enabled=True, provider="imported_pool")
    for operation in (lambda: check_sms_balance(settings), lambda: import_jobs._sms_client_for(settings, 5)):
        with pytest.raises(SmsError) as error:
            operation()
        assert_private(error)


def test_reservation_budget_covers_baseline_polling_and_settlement(pool, monkeypatch, make_client):
    reserve = pool.reserve
    budgets = []

    def capture(*, lease_seconds):
        budgets.append(lease_seconds)
        return reserve(lease_seconds=lease_seconds)

    monkeypatch.setattr(pool, "reserve", capture)
    settings = SmsSettings(provider="imported_pool", timeout_seconds=1800)
    client, _ = make_client("", settings=settings, timeout=30)
    client.cancel(client.acquire())
    assert budgets == [1920]
    assert isinstance(budgets[0], int)

import pytest
import requests

from weather.config import HttpSettings
from weather.http_client import HttpClient, SourceRequestRejected, SourceUnavailable, parse_retry_after

SETTINGS = HttpSettings(
    timeout_s=5, max_attempts=4, backoff_base_s=1, backoff_max_s=30, min_interval_s=0, user_agent="test"
)


class FakeResponse:
    def __init__(self, status, content=b"{}", headers=None, url="https://example.test/x"):
        self.status_code = status
        self.content = content
        self.text = content.decode("utf-8", "replace")
        self.headers = headers or {}
        self.url = url


class FakeSession:
    """Отдаёт заранее заданные ответы или исключения по очереди."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_client(*outcomes, settings=SETTINGS):
    sleeps = []
    client = HttpClient(settings, session=FakeSession(*outcomes), sleep=sleeps.append, clock=lambda: 0.0)
    return client, sleeps


def test_success_first_try():
    client, sleeps = make_client(FakeResponse(200, b'{"a": 1}'))
    result = client.get("https://example.test/x", params={"q": 1})
    assert result.status == 200 and result.json() == {"a": 1}
    assert (client.requests, client.retries) == (1, 0)
    assert sleeps == []


def test_retries_timeouts_and_5xx_then_succeeds():
    client, sleeps = make_client(requests.Timeout("slow"), FakeResponse(503), FakeResponse(200))
    assert client.get("https://example.test/x").status == 200
    assert (client.requests, client.retries) == (3, 2)
    # экспоненциальная пауза: ~1 с, затем ~2 с (плюс случайная добавка до 1 с)
    assert 1 <= sleeps[0] < 2 and 2 <= sleeps[1] < 3


def test_gives_up_after_max_attempts():
    client, _ = make_client(*[requests.ConnectionError("down")] * 4)
    with pytest.raises(SourceUnavailable, match="после 4 попыток"):
        client.get("https://example.test/x")
    assert client.requests == 4 and client.retries == 3


def test_client_error_is_not_retried():
    client, sleeps = make_client(FakeResponse(400, b'{"error": true, "reason": "bad date"}'))
    with pytest.raises(SourceRequestRejected, match="HTTP 400.*bad date"):
        client.get("https://example.test/x")
    assert client.requests == 1 and sleeps == []


def test_retry_after_header_is_respected():
    client, sleeps = make_client(FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(200))
    client.get("https://example.test/x")
    assert sleeps == [7.0]


def test_rate_limit_without_retry_after_waits_longest_pause():
    client, sleeps = make_client(FakeResponse(429), FakeResponse(200))
    client.get("https://example.test/x")
    assert sleeps == [SETTINGS.backoff_max_s]


def test_daily_limit_is_not_retried():
    body = b'{"error":true,"reason":"Daily API request limit exceeded. Please try again tomorrow."}'
    client, sleeps = make_client(FakeResponse(429, body))
    with pytest.raises(SourceUnavailable, match="лимит запросов источника исчерпан.*Daily"):
        client.get("https://example.test/x")
    assert client.requests == 1 and sleeps == []


def test_error_reason_in_message():
    body = b'{"error":true,"reason":"Server overloaded"}'
    client, _ = make_client(*[FakeResponse(503, body)] * 4)
    with pytest.raises(SourceUnavailable, match="HTTP 503: Server overloaded"):
        client.get("https://example.test/x")


def test_too_long_retry_after_postpones_load():
    client, sleeps = make_client(FakeResponse(429, headers={"Retry-After": "3600"}))
    with pytest.raises(SourceUnavailable, match="отложена"):
        client.get("https://example.test/x")
    assert sleeps == []


def test_not_modified_and_custom_statuses_are_returned():
    client, _ = make_client(FakeResponse(304), FakeResponse(404))
    assert client.get("https://example.test/x").not_modified
    assert client.get("https://example.test/x", accept_statuses=frozenset({200, 404})).status == 404


def test_min_interval_between_requests():
    now = [0.0]
    sleeps = []
    session = FakeSession(FakeResponse(200), FakeResponse(200))
    client = HttpClient(SETTINGS, session=session, sleep=sleeps.append, clock=lambda: now[0])
    client.get("https://example.test/x", min_interval_s=5)
    now[0] = 2.0
    client.get("https://example.test/x", min_interval_s=5)
    assert sleeps == [3.0]


def test_parse_retry_after():
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("не дата") is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0  # дата в прошлом

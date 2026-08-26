import asyncio
from datetime import UTC, datetime
from email.utils import format_datetime

import pytest
from open_webui.retrieval.web import exa


class _FakeResponse:
    def __init__(self, status, body, *, headers=None, payload=None):
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def text(self):
        return self.body

    async def json(self, content_type=None):
        return self.payload


class _FakeSession:
    def __init__(self, loop, responses):
        self._loop = loop
        self.closed = False
        self.responses = list(responses)
        self.calls = 0

    def post(self, *args, **kwargs):
        response = self.responses[self.calls]
        self.calls += 1
        return response


def _run_request(responses):
    async def run():
        session = _FakeSession(asyncio.get_running_loop(), responses)
        result = await exa._post_exa_json(
            api_key="test-key",
            path="/search",
            payload={"query": "test"},
            timeout_seconds=30,
            session=session,
        )
        return result, session.calls

    return asyncio.run(run())


def test_exa_429_retries_before_returning_success(monkeypatch):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(exa.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(exa.random, "uniform", lambda _start, _end: 0.25)

    result, calls = _run_request(
        [
            _FakeResponse(429, "rate limited", headers={"Retry-After": "2"}),
            _FakeResponse(200, "ok", payload={"results": []}),
        ]
    )

    assert result == {"results": []}
    assert calls == 2
    assert sleeps == [2.25]


def test_exa_429_uses_exponential_backoff_without_header(monkeypatch):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(exa.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(exa.random, "uniform", lambda _start, _end: 0.0)

    result, calls = _run_request(
        [
            _FakeResponse(429, "rate limited"),
            _FakeResponse(429, "still rate limited"),
            _FakeResponse(200, "ok", payload={"results": []}),
        ]
    )

    assert result == {"results": []}
    assert calls == 3
    assert sleeps == [0.5, 1.0]


def test_exa_429_stops_after_bounded_attempts(monkeypatch):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(exa.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(exa.random, "uniform", lambda _start, _end: 0.0)

    responses = [
        _FakeResponse(429, "rate limited") for _ in range(exa.EXA_HTTP_MAX_ATTEMPTS)
    ]

    with pytest.raises(Exception, match="HTTP 429"):
        _run_request(responses)

    assert len(sleeps) == exa.EXA_HTTP_MAX_ATTEMPTS - 1


def test_exa_non_429_errors_are_not_retried(monkeypatch):
    async def fail_if_slept(_delay):
        raise AssertionError("non-429 responses must not be retried")

    monkeypatch.setattr(exa.asyncio, "sleep", fail_if_slept)

    with pytest.raises(Exception, match="HTTP 401"):
        _run_request([_FakeResponse(401, "unauthorized")])


def test_parse_retry_after_accepts_http_date():
    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    retry_at = datetime(2026, 8, 24, 12, 0, 3, tzinfo=UTC)

    assert (
        exa._parse_retry_after(
            format_datetime(retry_at, usegmt=True), now=now.timestamp()
        )
        == 3.0
    )

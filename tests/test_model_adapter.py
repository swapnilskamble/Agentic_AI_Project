import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx2
import pytest
from openai import OpenAI

from market_research.models import Briefing, ResearchRequest
from market_research.providers import ServiceError, StructuredLLM, interruptible_wait, retry_delay
from market_research.storage import BudgetExceeded, Cancelled, Store


def response(text='{"insights":[]}', refusal=False):
    content = (
        {"type": "refusal", "refusal": "Cannot fulfill"}
        if refusal
        else {"type": "output_text", "text": text, "annotations": []}
    )
    return {
        "id": "resp_fixture",
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "model": "fixture-model",
        "output": [
            {
                "id": "msg_fixture",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [content],
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "temperature": 1,
        "top_p": 1,
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "metadata": {},
    }


def test_real_sdk_structured_parse_contract_tokens_and_cache(tmp_path):
    store = Store(tmp_path)
    run_id = store.create(ResearchRequest(company="Target", domain="target.example"))
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.url.path == "/v1/responses"
        assert body["text"]["format"]["type"] == "json_schema"
        assert body["text"]["format"]["strict"]
        assert body["store"] is False
        assert "untrusted data" in body["instructions"]
        return httpx2.Response(200, json=response())

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as transport:
        client = OpenAI(api_key="fixture-key", http_client=transport, max_retries=0)
        model = StructuredLLM(store, model="fixture-model", client=client)
        first = model.generate(run_id, "Orchestrator", Briefing, "Compile supported evidence", {})
        second = model.generate(run_id, "Orchestrator", Briefing, "Compile supported evidence", {})
    assert first == second == Briefing(insights=[])
    assert len(calls) == 1
    assert store.get(run_id)["tokens"] == 30
    assert store.get(run_id)["model_calls"] == 1


@pytest.mark.parametrize("kind", ["refusal", "invalid_schema", "authentication"])
def test_model_errors_are_actionable_and_not_cached(tmp_path, kind):
    store = Store(tmp_path)
    run_id = store.create(ResearchRequest(company="Target", domain="target.example"))

    def handler(request):
        if kind == "authentication":
            return httpx2.Response(
                401,
                json={
                    "error": {"message": "secret server payload", "type": "authentication_error"}
                },
            )
        return httpx2.Response(
            200,
            json=response(
                refusal=kind == "refusal",
                text='{"insights":"wrong type"}' if kind == "invalid_schema" else '{"insights":[]}',
            ),
        )

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as transport:
        model = StructuredLLM(
            store,
            model="fixture",
            client=OpenAI(api_key="fake", http_client=transport, max_retries=0),
        )
        with pytest.raises(ServiceError) as error:
            model.generate(run_id, "Extractor", Briefing, "Analyze", {})
    assert "secret server payload" not in str(error.value)
    assert store.get(run_id)["model_calls"] == 1
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM cache WHERE run_id=?", (run_id,)).fetchone()[0] == 0


@pytest.fixture
def virtual_wait(monkeypatch):
    clock = [0.0]
    waits = []
    monkeypatch.setattr("market_research.providers.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("market_research.providers.random.uniform", lambda *args: 0)

    def wait(store, run_id, seconds):
        store.check(run_id)
        waits.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr("market_research.providers.interruptible_wait", wait)
    return waits


def make_run(store):
    return store.create(ResearchRequest(company="Target", domain="target.example"))


def test_retry_after_is_not_shortened_and_attempts_are_configurable(tmp_path, virtual_wait):
    store = Store(tmp_path)
    run_id = make_run(store)
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count < 4:
            return httpx2.Response(
                429,
                headers={"Retry-After": "45", "x-request-id": "req_rate_limit"},
                json={
                    "error": {
                        "code": "rate_limit_exceeded",
                        "type": "rate_limit_error",
                        "message": "private server response",
                    }
                },
            )
        return httpx2.Response(200, headers={"x-request-id": "req_success"}, json=response())

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as transport:
        model = StructuredLLM(
            store,
            "fixture",
            OpenAI(api_key="fake", http_client=transport, max_retries=0),
            max_attempts=4,
            max_concurrency=1,
        )
        assert model.generate(run_id, "Reviewer", Briefing, "Review", {}) == Briefing(insights=[])
    assert count == store.get(run_id)["model_calls"] == 4
    assert virtual_wait == [45, 45, 45]
    failures = [e["details"] for e in store.events(run_id) if e["action"] == "api_failure"]
    assert len(failures) == 3
    assert failures[0]["http_status"] == 429
    assert failures[0]["api_error_code"] == "rate_limit_exceeded"
    assert failures[0]["request_id"] == "req_rate_limit"
    assert failures[0]["exception_type"] == "RateLimitError"
    assert "private server response" not in json.dumps(store.events(run_id))


@pytest.mark.parametrize(
    "code",
    [
        "insufficient_quota",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
    ],
)
def test_quota_and_spend_limits_stop_without_retries(tmp_path, code, virtual_wait):
    store = Store(tmp_path)
    run_id = make_run(store)

    def handler(request):
        return httpx2.Response(
            429,
            headers={"Retry-After": "90", "x-request-id": "req_quota"},
            json={
                "error": {
                    "code": code,
                    "type": "insufficient_quota",
                    "message": "raw billing prose",
                }
            },
        )

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as transport:
        model = StructuredLLM(
            store,
            "fixture",
            OpenAI(api_key="fake", http_client=transport, max_retries=0),
            max_attempts=5,
            max_concurrency=1,
        )
        with pytest.raises(ServiceError, match="quota or billing limit") as error:
            model.generate(run_id, "Extractor", Briefing, "Extract", {})
    assert error.value.details["reason"] == "quota"
    assert not error.value.details["retryable"]
    assert store.get(run_id)["model_calls"] == 1
    assert not virtual_wait


def test_exhaustion_reports_cause_without_fake_final_retry(tmp_path, virtual_wait):
    store = Store(tmp_path)
    run_id = make_run(store)

    def handler(request):
        return httpx2.Response(
            503, json={"error": {"code": "server_is_overloaded", "message": "private"}}
        )

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as transport:
        model = StructuredLLM(
            store,
            "fixture",
            OpenAI(api_key="fake", http_client=transport, max_retries=0),
            max_attempts=2,
            max_concurrency=1,
        )
        with pytest.raises(ServiceError, match="server errors persisted after 2 attempts") as error:
            model.generate(run_id, "Extractor", Briefing, "Extract", {})
    assert error.value.details["http_status"] == 503
    assert store.get(run_id)["model_calls"] == 2
    assert len(virtual_wait) == 1
    assert len([e for e in store.events(run_id) if e["action"] == "retry"]) == 1


def test_final_rate_limit_cooldown_applies_to_the_next_run(tmp_path, virtual_wait):
    store = Store(tmp_path)
    first, second = make_run(store), make_run(store)
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return (
            httpx2.Response(
                429, headers={"Retry-After": "65"}, json={"error": {"code": "rate_limit_exceeded"}}
            )
            if count == 1
            else httpx2.Response(200, json=response())
        )

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as transport:
        model = StructuredLLM(
            store,
            "fixture",
            OpenAI(api_key="fake", http_client=transport, max_retries=0),
            max_attempts=1,
            max_concurrency=1,
        )
        with pytest.raises(ServiceError):
            model.generate(first, "Extractor", Briefing, "Extract", {})
        model.generate(second, "Extractor", Briefing, "Extract", {})
    assert virtual_wait == [65]
    assert store.get(first)["model_calls"] == store.get(second)["model_calls"] == 1


def test_shared_model_slot_serializes_calls_and_cancelled_queue_uses_no_budget(tmp_path):
    store = Store(tmp_path)
    first, second = make_run(store), make_run(store)
    entered, release = threading.Event(), threading.Event()
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        entered.set()
        assert release.wait(timeout=5)
        return httpx2.Response(200, json=response())

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as transport:
        model = StructuredLLM(
            store,
            "fixture",
            OpenAI(api_key="fake", http_client=transport, max_retries=0),
            max_attempts=1,
            max_concurrency=1,
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(model.generate, first, "Extractor", Briefing, "Extract", {})
            assert entered.wait(timeout=5)
            two = pool.submit(model.generate, second, "Reviewer", Briefing, "Review", {})
            try:
                until = time.monotonic() + 3
                while not any(e["action"] == "model_queued" for e in store.events(second)):
                    assert time.monotonic() < until
                    time.sleep(0.01)
                assert count == 1
                assert store.get(second)["model_calls"] == 0
                store.update(second, cancelled=1)
                with pytest.raises(Cancelled):
                    two.result(timeout=3)
            finally:
                release.set()
            assert one.result(timeout=5) == Briefing(insights=[])
    assert count == 1


def test_retry_wait_cancellation_and_deadline_stop_promptly(tmp_path):
    store = Store(tmp_path)
    run_id = make_run(store)
    store.update(run_id, cancelled=1)
    with pytest.raises(Cancelled):
        interruptible_wait(store, run_id, 120)
    store.update(run_id, cancelled=0, deadline=time.time() - 1)
    with pytest.raises(BudgetExceeded):
        interruptible_wait(store, run_id, 120)
    assert store.get(run_id)["model_calls"] == 0


def test_retry_after_formats_and_invalid_hints(monkeypatch):
    monkeypatch.setattr("market_research.providers.random.uniform", lambda *args: 0)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    monkeypatch.setattr("market_research.providers.time.time", lambda: now.timestamp())
    assert retry_delay(0, "120") == 120
    assert retry_delay(0, format_datetime(now + timedelta(seconds=90), usegmt=True)) == 90
    assert retry_delay(0, retry_after_ms="45000") == 45
    assert retry_delay(0, "NaN") == 2
    assert retry_delay(1, "invalid") == 4


def test_model_retry_budget_remains_a_hard_limit(tmp_path, virtual_wait):
    store = Store(tmp_path)
    run_id = store.create(
        ResearchRequest(company="Target", domain="target.example", max_model_calls=5)
    )
    with httpx2.Client(
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(
                429, headers={"Retry-After": "0"}, json={"error": {"code": "rate_limit_exceeded"}}
            )
        )
    ) as transport:
        model = StructuredLLM(
            store,
            "fixture",
            OpenAI(api_key="fake", http_client=transport, max_retries=0),
            max_attempts=10,
            max_concurrency=1,
        )
        with pytest.raises(BudgetExceeded):
            model.generate(run_id, "Extractor", Briefing, "Extract", {})
    assert store.get(run_id)["model_calls"] == 5


def test_retry_and_concurrency_settings_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_MAX_ATTEMPTS", "7")
    monkeypatch.setenv("OPENAI_MAX_CONCURRENCY", "2")
    model = StructuredLLM(Store(tmp_path))
    assert (model.max_attempts, model.max_concurrency) == (7, 2)
    monkeypatch.setenv("OPENAI_MAX_ATTEMPTS", "0")
    with pytest.raises(ValueError, match="OPENAI_MAX_ATTEMPTS"):
        StructuredLLM(Store(tmp_path))


@pytest.mark.parametrize(
    "kind, expected_type, message",
    [
        ("timeout", "APITimeoutError", "timed out after 2 attempts"),
        ("connection", "APIConnectionError", "Could not connect to OpenAI after 2 attempts"),
    ],
)
def test_transport_failure_type_is_traced(tmp_path, virtual_wait, kind, expected_type, message):
    store = Store(tmp_path)
    run_id = make_run(store)

    def handler(request):
        if kind == "timeout":
            raise httpx2.ReadTimeout("private timeout details", request=request)
        raise httpx2.ConnectError("private connection details", request=request)

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as transport:
        model = StructuredLLM(
            store,
            "fixture",
            OpenAI(api_key="fake", http_client=transport, max_retries=0),
            max_attempts=2,
            max_concurrency=1,
        )
        with pytest.raises(ServiceError, match=message) as error:
            model.generate(run_id, "Reviewer", Briefing, "Review", {})
    assert error.value.details["exception_type"] == expected_type
    assert error.value.details["reason"] == kind
    assert error.value.details["request_id"] is None
    assert store.get(run_id)["model_calls"] == 2
    assert "private" not in json.dumps(store.events(run_id))

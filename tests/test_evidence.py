from datetime import datetime, timedelta, timezone

import httpx
import pytest

from market_research.models import Claim, ClaimReview, Extraction, ResearchRequest, Review
from market_research.providers import ServiceError, YouSearch, normalize_results
from market_research.quality import evaluate
from market_research.storage import Store


def result(
    url="https://competitor.example/pricing", text="Pro costs USD 12 per seat monthly.", **kwargs
):
    return {"url": url, "title": "Pricing", "contents": {"markdown": text}, **kwargs}


def evidence(**kwargs):
    return normalize_results(
        {"results": {"web": [result(**kwargs)]}}, "pricing", "competitor.example", 90
    )


def claim(evidence_ids, category="pricing", **kwargs):
    return Claim(
        id="c1",
        category=category,
        label="Pro",
        statement="Pro costs USD 12 per seat monthly.",
        evidence_ids=evidence_ids,
        currency="USD",
        amount=12,
        billing_period="monthly",
        unit="seat",
        region=None,
        event_date=None,
        publication_date=None,
        interpretation=False,
        **kwargs,
    )


def review(conflict=None):
    return Review(
        assessments=[
            ClaimReview(
                claim_id="c1",
                supported=True,
                directness=1,
                explanation="Direct evidence",
                conflict=conflict,
            )
        ],
        followup_queries=[],
    )


def test_fabricated_citation_never_accepted_even_if_reviewer_approves():
    e = evidence()
    q = evaluate(Extraction(claims=[claim(["invented-id"])], gaps=[]), review(), e, 90)
    assert not q["claims"]
    assert q["rejected"][0]["confidence"] == 0


def test_conflicting_price_is_not_accepted():
    e = evidence()
    q = evaluate(
        Extraction(claims=[claim([e[0]["id"]])], gaps=[]),
        review("Conflicting official prices"),
        e,
        90,
    )
    assert q["claims"][0]["confidence"] <= 0.55
    assert not q["claims"][0]["accepted"]


def test_duplicate_claim_ids_reject_all_ambiguous_claims():
    e = evidence()
    c = claim([e[0]["id"]])
    q = evaluate(Extraction(claims=[c, c], gaps=[]), review(), e, 90)
    assert not q["claims"]
    assert len(q["rejected"]) == 2


def test_fabricated_news_publication_date_is_rejected():
    date = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
    e = evidence(page_age=date)
    c = claim([e[0]["id"]], category="news")
    c.publication_date = "2099-01-01"
    q = evaluate(Extraction(claims=[c], gaps=[]), review(), e, 90)
    assert not q["claims"]


def test_query_cache_retains_original_retrieval_time():
    payload = {"results": {"web": [result()]}, "_retrieved_at": "2026-01-01T00:00:00+00:00"}
    records = normalize_results(payload, "pricing", "competitor.example", 90)
    assert records[0]["retrieved_at"] == payload["_retrieved_at"]


def test_snippet_only_cannot_meet_confidence_threshold():
    e = normalize_results(
        {
            "results": {
                "web": [
                    {
                        "url": "https://competitor.example/pricing",
                        "snippets": ["Pro costs USD 12 per seat monthly."],
                    }
                ]
            }
        },
        "pricing",
        "competitor.example",
        90,
    )
    q = evaluate(Extraction(claims=[claim([e[0]["id"]])], gaps=[]), review(), e, 90)
    assert e[0]["content_type"] == "snippet"
    assert not q["claims"][0]["accepted"]


def test_news_requires_dated_source():
    e = evidence()
    q = evaluate(
        Extraction(claims=[claim([e[0]["id"]], category="news")], gaps=[]), review(), e, 90
    )
    assert not q["claims"]


def test_normalization_filters_old_news_duplicates_and_unsafe_urls():
    old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    payload = {
        "results": {
            "web": [
                result(),
                result(),
                result(url="javascript:alert(1)"),
                result(url="https://syndication.example/a"),
                result(url="https://competitor.example.evil.com/a", text="Different text"),
            ],
            "news": [result(url="https://news.example/a", text="Old event", page_age=old)],
        }
    }
    records = normalize_results(payload, "news", "competitor.example", 90)
    assert len(records) == 2
    assert records[0]["official"]
    assert not records[1]["official"]


def test_highlights_and_empty_results():
    payload = {
        "results": {
            "web": [
                {
                    "url": "https://competitor.example/features",
                    "contents": {"highlights": ["Workflow", {"text": "Automation"}]},
                }
            ],
            "news": None,
        }
    }
    records = normalize_results(payload, "features", "competitor.example", 90)
    assert records[0]["content_type"] == "highlights"
    assert records[0]["text"] == "Workflow\nAutomation"
    assert normalize_results({}, "news", "competitor.example", 90) == []


def test_you_search_request_contract_and_cache(tmp_path):
    store = Store(tmp_path)
    run_id = store.create(ResearchRequest(company="Target", domain="target.example"))
    requests = []

    def handler(request):
        import json

        requests.append(request)
        body = json.loads(request.content)
        assert request.method == "POST"
        assert request.headers["X-API-Key"] == "fake-key"
        assert body["include_domains"] == ["competitor.example"]
        assert body["extraction"]["extraction_source"] == "fetch"
        return httpx.Response(200, json={"results": {"web": [result()]}})

    search = YouSearch(store, api_key="fake-key", transport=httpx.MockTransport(handler))
    first = search.search(run_id, "pricing", "pricing", "competitor.example", 90, True)
    second = search.search(run_id, "pricing", "pricing", "competitor.example", 90, True)
    assert first[0]["id"] == second[0]["id"]
    assert len(requests) == 1
    assert store.get(run_id)["queries"] == 1


def test_you_transient_errors_retry_and_auth_errors_do_not(tmp_path, monkeypatch):
    monkeypatch.setattr("market_research.providers.interruptible_wait", lambda *args: None)
    store = Store(tmp_path)
    run_id = store.create(ResearchRequest(company="Target", domain="target.example"))
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return (
            httpx.Response(429, headers={"Retry-After": "0"})
            if count < 3
            else httpx.Response(200, json={"results": {}})
        )

    search = YouSearch(store, "fake", httpx.MockTransport(handler))
    assert search.search(run_id, "news", "news", "competitor.example", 90) == []
    assert store.get(run_id)["queries"] == 3
    search.transport = httpx.MockTransport(lambda request: httpx.Response(401))
    with pytest.raises(ServiceError, match="YOU_API_KEY"):
        search.search(run_id, "different query", "pricing", "competitor.example", 90)
    assert store.get(run_id)["queries"] == 4

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from market_research.models import ResearchRequest
from market_research.rendering import markdown_briefing
from market_research.service import ResearchService
from market_research.storage import BudgetExceeded, Store


@pytest.fixture
def service(tmp_path):
    instance = ResearchService(tmp_path)
    yield instance
    instance.close()


def request(**kwargs):
    return ResearchRequest(company="FlowPilot", domain="flowpilot.example", **kwargs)


def test_parallel_research_compiles_cited_briefing(service):
    run_id = service.create(request(), background=False)
    row = service.store.get(run_id)
    assert row["status"] == "complete", row["error"]
    report = row["result"]
    assert len(report["candidate_ranking"]) == 5
    assert len(report["profiles"]) == 3
    for profile in report["profiles"].values():
        assert profile["completeness"] == 1
        assert profile["rounds"] == 1
        sources = {e["id"] for e in profile["evidence"]}
        assert all(set(c["evidence_ids"]) <= sources for c in profile["claims"])
    events = service.store.events(run_id)
    assert any(e["action"] == "parallel_dispatch" for e in events)
    assert len([e for e in events if e["action"] == "quality_assessment"]) == 3
    assert "FICTIONAL DEMO" in markdown_briefing(report)
    assert "billed annually" in markdown_briefing(report)


def test_conflict_pauses_and_accept_partial_keeps_disputed_prices_out(service):
    run_id = service.create(request(demo_scenario="conflict"), background=False)
    row = service.store.get(run_id)
    assert row["status"] == "awaiting_review", row["error"]
    assert row["result"]["review"][0]["stage"] == "evidence_review"
    assert len(row["result"]["state"]["profiles"]) == 3
    before = row["queries"]
    service.resume(run_id, {"action": "accept_partial"}, background=False)
    row = service.store.get(run_id)
    assert row["status"] == "partial", row["error"]
    disputed = row["result"]["profiles"]["taskharbor.example"]
    assert disputed["conflicts"]
    assert not any(c["accepted"] for c in disputed["claims"] if c["category"] == "pricing")
    assert row["queries"] == before
    assert row["result"]["human_decisions"][0]["action"] == "accept_partial"
    assert disputed["stop_reason"] == "Follow-up research produced no new evidence"


def test_ambiguous_identity_clarification_resumes_discovery(service):
    run_id = service.create(request(demo_scenario="ambiguous"), background=False)
    assert service.store.get(run_id)["status"] == "awaiting_review"
    service.resume(
        run_id, {"action": "clarify", "clarification": "The SMB workflow product"}, background=False
    )
    row = service.store.get(run_id)
    assert row["status"] == "complete", row["error"]
    assert row["result"]["human_decisions"][0]["stage"] == "competitor_selection"


def test_human_selection_is_explicit(service):
    run_id = service.create(request(demo_scenario="ambiguous"), background=False)
    candidates = service.store.get(run_id)["result"]["review"][0]["selected"]
    keys = {
        "name",
        "domain",
        "relationship",
        "reason",
        "product_overlap",
        "customer_overlap",
        "geography_overlap",
        "evidence_ids",
    }
    replacements = [{k: v for k, v in c.items() if k in keys} for c in candidates]
    service.resume(run_id, {"action": "replace", "competitors": replacements}, background=False)
    row = service.store.get(run_id)
    assert row["status"] == "complete", row["error"]
    assert all(
        p["competitor"]["selection_method"] == "human selection"
        for p in row["result"]["profiles"].values()
    )


@pytest.mark.parametrize("limits", [{"max_queries": 5}, {"max_model_calls": 5}])
def test_budget_exhaustion_returns_partial_without_exceeding_limits(service, limits):
    run_id = service.create(request(**limits), background=False)
    row = service.store.get(run_id)
    assert row["status"] == "partial", row["error"]
    assert row["queries"] <= row["request"]["max_queries"]
    assert row["model_calls"] <= row["request"]["max_model_calls"]
    assert (
        any(p["budget_exhausted"] for p in row["result"]["profiles"].values())
        or row["result"]["synthesis_gap"]
    )


def test_resume_from_checkpoint_in_new_process_instance(tmp_path):
    first = ResearchService(tmp_path)
    run_id = first.create(request(demo_scenario="conflict"), background=False)
    first.close()
    second = ResearchService(tmp_path)
    try:
        second.resume(run_id, {"action": "accept_partial"}, background=False)
        assert second.store.get(run_id)["status"] == "partial"
    finally:
        second.close()


def test_cancel_human_review(service):
    run_id = service.create(request(demo_scenario="ambiguous"), background=False)
    service.resume(run_id, {"action": "cancel"}, background=False)
    assert service.store.get(run_id)["status"] == "cancelled"


def test_accepting_ambiguous_selection_preserves_caveat(service):
    run_id = service.create(request(demo_scenario="ambiguous"), background=False)
    service.resume(run_id, {"action": "accept_partial"}, background=False)
    row = service.store.get(run_id)
    assert row["status"] == "partial", row["error"]
    assert row["result"]["selection_issues"]


def test_unverified_baseline_cannot_become_fact_by_accepting_partial(service):
    from market_research.demo import DemoLLM, DemoSearch
    from market_research.graph import build_graph
    from market_research.models import DiscoveryReview

    class RejectBaseline(DemoLLM):
        def generate(self, run_id, agent, schema, instructions, payload):
            output = super().generate(run_id, agent, schema, instructions, payload)
            if schema is DiscoveryReview:
                output.target_supported = False
                output.target_explanation = "The baseline does not follow from the evidence."
            return output

    service.graphs[True] = build_graph(
        service.store,
        DemoSearch(service.store),
        RejectBaseline(service.store),
        service.checkpointer,
    )
    run_id = service.create(request(), background=False)
    assert service.store.get(run_id)["status"] == "awaiting_review"
    service.resume(run_id, {"action": "accept_partial"}, background=False)
    row = service.store.get(run_id)
    assert row["status"] == "partial", row["error"]
    assert not row["result"]["target_baseline_verified"]
    assert row["result"]["target_baseline"] == "Target baseline was not verified."


def test_cancel_flag_prevents_new_tool_calls(service):
    run_id = service.store.create(request())
    service.cancel(run_id)
    from market_research.storage import Cancelled

    with pytest.raises(Cancelled):
        service.store.reserve(run_id, "search")
    assert service.store.get(run_id)["queries"] == 0


def test_atomic_query_budget_across_parallel_workers(tmp_path):
    store = Store(tmp_path)
    run_id = store.create(request(max_queries=5))

    def attempt(_):
        try:
            store.reserve(run_id, "search")
            return 1
        except BudgetExceeded:
            return 0

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(20))) == 5
    assert store.get(run_id)["queries"] == 5


def test_unavailable_credentials_block_live_before_creating_run(service, monkeypatch):
    monkeypatch.delenv("YOU_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="requires"):
        service.create(request(demo=False), background=False)
    assert not service.store.list_runs()


def test_parallel_runs_do_not_mix_evidence(service):
    ids = [
        service.create(ResearchRequest(company=name, domain=domain))
        for name, domain in [("One", "one.example"), ("Two", "two.example")]
    ]
    for run_id in ids:
        service.futures[run_id].result(timeout=30)
        row = service.store.get(run_id)
        assert row["status"] == "complete", row["error"]
        assert row["result"]["company"] == row["request"]["company"]
        assert all(e["run_id"] == run_id for e in service.store.events(run_id))
        json.dumps(row["result"])

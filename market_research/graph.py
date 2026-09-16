import operator
from dataclasses import asdict
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from .industries import get_profile
from .models import (
    Briefing,
    Candidate,
    CollectionPlan,
    Discovery,
    DiscoveryReview,
    Extraction,
    ResearchPlan,
    ResearchRequest,
    Review,
    utcnow,
)
from .quality import evaluate
from .storage import BudgetExceeded, Store


def merge_profiles(left: dict, right: dict) -> dict:
    return {**left, **right}


class ResearchState(TypedDict, total=False):
    run_id: str
    request: dict
    plan: dict
    clarification: str
    discovery_evidence: list[dict]
    discovery: dict
    selected: list[dict]
    selection_issues: list[str]
    selection_caveats: list[str]
    profiles: Annotated[dict, merge_profiles]
    pending: list[dict]
    review_needed: bool
    review_payload: dict
    next_action: str
    human_decisions: Annotated[list[dict], operator.add]
    briefing: dict
    status: str


class BranchState(TypedDict, total=False):
    run_id: str
    request: dict
    competitor: dict
    clarification: str
    previous: dict
    evidence: list[dict]
    extraction: dict
    quality: dict
    round: int
    max_rounds: int
    no_progress: bool
    budget_reason: str
    profiles: Annotated[dict, merge_profiles]


class ProfileOutput(TypedDict):
    profiles: Annotated[dict, merge_profiles]


def build_graph(store: Store, search, llm, checkpointer):
    def collect(state: BranchState):
        run_id, competitor, request = state["run_id"], state["competitor"], state["request"]
        previous = state.get("previous") or {}
        evidence = list(state.get("evidence", previous.get("evidence", [])))
        old_ids = {e["id"] for e in evidence}
        round_no = state.get("round", 0) + 1
        profile = get_profile(request["industry"])
        quality = state.get("quality", previous)
        categories = quality.get("gaps", ["pricing", "features", "positioning", "news"])
        if quality.get("conflicts"):
            categories = list(dict.fromkeys(["pricing", *categories]))
        if not categories:
            categories = ["pricing", "features", "positioning", "news"]
        store.event(
            run_id,
            "Orchestrator",
            "delegate",
            competitor["name"],
            to="Research Collector",
            round=round_no,
            categories=categories,
        )
        budget_reason = ""
        queries = []
        for category in categories:
            terms = {
                "pricing": "pricing plans billing annual monthly seats",
                "features": "product features documentation " + " ".join(profile.feature_taxonomy),
                "positioning": "product customers use cases positioning",
                "news": "product launch announcement funding acquisition news",
            }[category]
            suffix = " official details clarification" if round_no > 1 else ""
            query = f"{competitor['name']} {competitor['domain']} {terms} {request['geography']} "
            query += suffix + " " + state.get("clarification", "")
            queries.append((query, category, category != "news"))
        if round_no > 1:
            queries.extend(
                (
                    f"{competitor['name']} {q}",
                    "pricing" if quality.get("conflicts") else "features",
                    False,
                )
                for q in quality.get("followup_queries", [])[:2]
            )
        elif not previous:
            try:
                collection = llm.generate(
                    run_id,
                    "Research Collector",
                    CollectionPlan,
                    "Plan four focused search tasks, one each for pricing, features, positioning "
                    "and news, tailored to the competitor and user scope. Start with official "
                    "sources for pricing/features/positioning; use broad independent news search. "
                    "Do not assume knowledge of the competitor's actual offerings.",
                    {"competitor": competitor, "request": request, "industry": asdict(profile)},
                )
                planned_categories = set()
                planned = []
                for task in collection.tasks[:6]:
                    if task.category in planned_categories or not task.query.strip():
                        continue
                    planned_categories.add(task.category)
                    planned.append(
                        (
                            f"{competitor['name']} {competitor['domain']} {task.query}",
                            task.category,
                            task.category != "news",
                        )
                    )
                # The model can tailor queries, but cannot silently omit a required category.
                queries = planned + [q for q in queries if q[1] not in planned_categories]
            except BudgetExceeded as exc:
                budget_reason = str(exc)
                queries = []
        for query, category, official_only in queries[:6]:
            try:
                new = search.search(
                    run_id,
                    query.strip(),
                    category,
                    competitor["domain"],
                    request["news_days"],
                    official_only,
                )
                # Limit context by category rather than allowing one query to crowd out the rest.
                for item in new[:6]:
                    if item["id"] not in {e["id"] for e in evidence}:
                        evidence.append(item)
            except BudgetExceeded as exc:
                budget_reason = str(exc)
                break
        store.event(
            run_id,
            "Research Collector",
            "handoff",
            competitor["name"],
            to="Analysis Extractor",
            evidence_count=len(evidence),
            round=round_no,
            budget_reason=budget_reason,
        )
        protected_ids = {i for c in quality.get("claims", []) for i in c["evidence_ids"]}
        protected = [e for e in evidence if e["id"] in protected_ids]
        rest = [e for e in evidence if e["id"] not in protected_ids]
        kept = (
            protected + rest[-max(0, 40 - len(protected)) :] if len(protected) < 40 else protected
        )
        return {
            "evidence": kept,
            "round": round_no,
            "max_rounds": state.get("max_rounds", 3),
            "budget_reason": budget_reason,
            "no_progress": round_no > 1 and {e["id"] for e in evidence} == old_ids,
        }

    def extract(state: BranchState):
        if state.get("budget_reason"):
            return {
                "extraction": state.get(
                    "extraction", {"claims": [], "gaps": [state["budget_reason"]]}
                )
            }
        profile = get_profile(state["request"]["industry"])
        try:
            output = llm.generate(
                state["run_id"],
                "Analysis Extractor",
                Extraction,
                "Extract only supported pricing, features, company-stated positioning and news. "
                "Use unique claim IDs. Features must use a relevant taxonomy label. "
                "Keep separate claims for conflicting prices. Limit to 20 claims. "
                "Dates must come from evidence; null when absent. " + profile.pricing_rules,
                {
                    "competitor": state["competitor"],
                    "scope": state["request"],
                    "feature_taxonomy": list(profile.feature_taxonomy),
                    "evidence": state["evidence"],
                },
            )
            return {"extraction": output.model_dump()}
        except BudgetExceeded as exc:
            return {
                "budget_reason": str(exc),
                "extraction": state.get("extraction", {"claims": [], "gaps": [str(exc)]}),
            }

    def review(state: BranchState):
        extraction = Extraction.model_validate(state["extraction"])
        if state.get("budget_reason"):
            # Preserve previously reviewed findings; newly extracted unreviewed facts are not accepted.
            quality = (
                state.get("quality")
                or state.get("previous")
                or evaluate(
                    extraction,
                    Review(assessments=[], followup_queries=[]),
                    state["evidence"],
                    state["request"]["news_days"],
                )
            )
        else:
            try:
                result = llm.generate(
                    state["run_id"],
                    "Evidence Reviewer",
                    Review,
                    "Review every claim against its cited sources. An ID alone is not proof. "
                    "Check company identity, price billing basis, support and material contradictions "
                    "across ALL evidence, including sources the extractor did not cite. A duplicate "
                    "article is not independent corroboration. Suggest at most three targeted queries "
                    "for missing or disputed evidence. Each claim must have one assessment.",
                    {
                        "competitor": state["competitor"],
                        "claims": extraction.model_dump()["claims"],
                        "evidence": state["evidence"],
                        "scope": state["request"],
                    },
                )
                quality = evaluate(
                    extraction, result, state["evidence"], state["request"]["news_days"]
                )
            except BudgetExceeded as exc:
                quality = (
                    state.get("quality")
                    or state.get("previous")
                    or evaluate(
                        extraction,
                        Review(assessments=[], followup_queries=[]),
                        state["evidence"],
                        state["request"]["news_days"],
                    )
                )
                return {"quality": quality, "budget_reason": str(exc)}
        store.event(
            state["run_id"],
            "Evidence Reviewer",
            "quality_assessment",
            state["competitor"]["name"],
            confidence=quality["confidence"],
            completeness=quality["completeness"],
            gaps=quality["gaps"],
            conflicts=quality["conflicts"],
            round=state["round"],
        )
        return {"quality": quality}

    def branch_route(state: BranchState):
        q = state["quality"]
        if (
            state.get("budget_reason")
            or state.get("no_progress")
            or state["round"] >= state["max_rounds"]
        ):
            return "finish"
        if q["completeness"] == 1 and not q["conflicts"]:
            return "finish"
        return "collect"

    def finish(state: BranchState):
        q = state["quality"]
        if state.get("budget_reason"):
            stop_reason = state["budget_reason"]
        elif q["completeness"] == 1 and not q["conflicts"]:
            stop_reason = "Evidence confidence and required coverage met"
        elif state.get("no_progress"):
            stop_reason = "Follow-up research produced no new evidence"
        else:
            stop_reason = "Maximum research rounds reached"
        result = {
            **q,
            "competitor": state["competitor"],
            "evidence": state["evidence"],
            "rounds": state["round"],
            "stop_reason": stop_reason,
            "budget_exhausted": bool(state.get("budget_reason")),
            "needs_human": bool(
                not state.get("budget_reason")
                and (q["conflicts"] or (q["claims"] and q["confidence"] < 0.6))
            ),
        }
        store.event(
            state["run_id"],
            "Research branch",
            "complete",
            state["competitor"]["name"],
            stop_reason=stop_reason,
            needs_human=result["needs_human"],
        )
        return {"profiles": {state["competitor"]["domain"]: result}}

    branch = StateGraph(BranchState, output_schema=ProfileOutput)
    for name, node in (
        ("collect", collect),
        ("extract", extract),
        ("review", review),
        ("finish", finish),
    ):
        branch.add_node(name, node)
    branch.add_edge(START, "collect")
    branch.add_edge("collect", "extract")
    branch.add_edge("extract", "review")
    branch.add_conditional_edges("review", branch_route, {"collect": "collect", "finish": "finish"})
    branch.add_edge("finish", END)
    competitor_graph = branch.compile()

    def plan(state: ResearchState):
        request = ResearchRequest.model_validate(state["request"])
        profile = get_profile(request.industry)
        research_plan = {
            "industry": asdict(profile),
            "scope": request.scope,
            "geography": request.geography,
            "segment": request.segment,
            "news_days": request.news_days,
            "competitor_count": 3,
            "max_rounds_per_competitor": 3,
            "ranking_weights": {"product": 0.5, "customer": 0.35, "geography": 0.15},
            "confidence_accept": 0.8,
            "confidence_escalate": 0.6,
        }
        try:
            strategy = llm.generate(
                state["run_id"],
                "Orchestrator",
                ResearchPlan,
                "Create a competitive intelligence research strategy for this SaaS company. "
                "Provide one or two competitor discovery search queries tailored to the product, "
                "buyer segment and geography, and the comparison questions to resolve. "
                "This is a plan, not factual findings; do not assert competitors or pricing.",
                {"request": request.model_dump(), "industry": asdict(profile)},
            )
            research_plan.update(strategy.model_dump())
        except BudgetExceeded as exc:
            research_plan["planning_gap"] = str(exc)
        store.event(state["run_id"], "Orchestrator", "plan", request.company, plan=research_plan)
        return {"plan": research_plan, "status": "running"}

    def discover(state: ResearchState):
        req, run_id = state["request"], state["run_id"]
        evidence = []
        try:
            queries = [
                (f"{req['company']} {req['domain']} product customers {req['scope']}", True),
                (
                    f"{req['company']} {get_profile(req['industry']).discovery_terms} "
                    f"{req['scope']} {req['segment']} {req['geography']} {state.get('clarification', '')}",
                    False,
                ),
            ]
            proposed = state["plan"].get("discovery_queries", [])[:2]
            if proposed:
                queries = [queries[0]] + [
                    (f"{req['company']} {q} {state.get('clarification', '')}", False)
                    for q in proposed
                    if q.strip()
                ]
            for query, official_only in queries:
                evidence.extend(
                    search.search(
                        run_id, query, "discovery", req["domain"], req["news_days"], official_only
                    )
                )
            evidence = list({e["id"]: e for e in evidence}.values())
            result = llm.generate(
                run_id,
                "Competitor Discovery",
                Discovery,
                "First identify the target product and its buyer jobs. Discover and score up to "
                "six candidate competitors. Scores reflect overlap within the user's scope, "
                "not popularity. Cite evidence that explicitly connects each candidate to the "
                "same market. Do not invent candidates to fill three slots. State ambiguity "
                "when identity or product scope cannot be established.",
                {
                    "request": req,
                    "evidence": evidence,
                    "clarification": state.get("clarification", ""),
                },
            )
            verification = llm.generate(
                run_id,
                "Evidence Reviewer",
                DiscoveryReview,
                "Independently verify the target baseline and each candidate's name, domain, "
                "competitive relationship and selection rationale against supplied evidence. "
                "Do not accept citations merely because their IDs exist. Set supported=false "
                "for guessed identities/domains or unsupported competitive relationships. "
                "Return one assessment per candidate domain.",
                {"request": req, "discovery": result.model_dump(), "evidence": evidence},
            )
        except BudgetExceeded as exc:
            store.event(run_id, "Orchestrator", "stop", req["company"], reason=str(exc))
            return {
                "discovery_evidence": evidence,
                "discovery": {},
                "selected": [],
                "selection_issues": [str(exc)],
                "review_needed": False,
                "next_action": "synthesize",
            }
        source_ids = {e["id"] for e in evidence}
        candidates, seen = [], set()
        issues = []
        exclusions = []
        target_supported = (
            verification.target_supported
            and bool(result.target_evidence_ids)
            and all(i in source_ids for i in result.target_evidence_ids)
        )
        if not target_supported:
            issues.append(
                "Target-company baseline could not be verified: " + verification.target_explanation
            )
        if result.ambiguity:
            issues.append(result.ambiguity)
        assessments = {a.domain: a for a in verification.assessments}
        source_map = {e["id"]: e for e in evidence}
        for candidate in sorted(result.candidates, key=lambda c: c.rank_score, reverse=True):
            if candidate.domain == req["domain"] or candidate.domain in seen:
                continue
            seen.add(candidate.domain)
            if not candidate.evidence_ids or not all(
                i in source_ids for i in candidate.evidence_ids
            ):
                exclusions.append(
                    f"Excluded {candidate.name}: missing or invalid selection citations."
                )
                continue
            assessment = assessments.get(candidate.domain)
            if not assessment or not assessment.supported:
                exclusions.append(
                    f"Excluded {candidate.name}: selection evidence did not pass review."
                )
                continue
            cited = [source_map[i] for i in candidate.evidence_ids]
            depth = max(
                {"full_page": 1.0, "highlights": 0.85, "snippet": 0.45}[e["content_type"]]
                for e in cited
            )
            independent = 1.0 if len({e["publisher"] for e in cited}) > 1 else 0.4
            evidence_confidence = round(
                0.6 * assessment.directness + 0.25 * depth + 0.15 * independent, 3
            )
            candidates.append(
                {
                    **candidate.model_dump(),
                    "rank_score": candidate.rank_score,
                    "evidence_confidence": evidence_confidence,
                    "selection_review": assessment.explanation,
                    "selection_method": "evidence-based overlap ranking",
                }
            )
        selected = candidates[:3]
        if len(selected) < 3:
            issues.append(f"Only {len(selected)} competitors have usable selection evidence.")
        if selected and min(c["rank_score"] for c in selected) < 0.65:
            issues.append("One or more selected competitors have weak competitive overlap.")
        if selected and min(c["evidence_confidence"] for c in selected) < 0.8:
            issues.append(
                "One or more selected competitors have weak selection evidence confidence."
            )
        if len(candidates) > 3 and candidates[2]["rank_score"] - candidates[3]["rank_score"] < 0.03:
            issues.append("Third and fourth candidates are closely ranked; review product scope.")
        discovery = {
            **result.model_dump(),
            "candidates": candidates,
            "target_supported": target_supported,
            "target_review": verification.target_explanation,
            "excluded_candidates": exclusions,
        }
        store.event(
            run_id,
            "Competitor Discovery",
            "selection",
            req["company"],
            selected=[c["name"] for c in selected],
            issues=issues,
        )
        return {
            "discovery_evidence": evidence,
            "discovery": discovery,
            "selected": selected,
            "pending": selected,
            "selection_issues": issues,
            "selection_caveats": issues,
            "review_needed": bool(issues),
            "next_action": "research",
        }

    def selection_route(state: ResearchState):
        if state.get("next_action") == "synthesize":
            return "synthesize"
        return "human" if state["review_needed"] else "dispatch"

    def dispatch(state: ResearchState):
        store.event(
            state["run_id"],
            "Orchestrator",
            "parallel_dispatch",
            competitors=[c["name"] for c in state["pending"]],
        )
        return {}

    def fanout(state: ResearchState):
        if not state["pending"]:
            return "assess"
        return [
            Send(
                "competitor_research",
                {
                    "run_id": state["run_id"],
                    "request": state["request"],
                    "competitor": c,
                    "clarification": state.get("clarification", ""),
                    "previous": state.get("profiles", {}).get(c["domain"], {}),
                },
            )
            for c in state["pending"]
        ]

    def assess(state: ResearchState):
        troubled = [
            p
            for d, p in state["profiles"].items()
            if d in {c["domain"] for c in state["selected"]} and p["needs_human"]
        ]
        payload = {
            "stage": "evidence_review",
            "issues": [
                {
                    "competitor": p["competitor"]["name"],
                    "domain": p["competitor"]["domain"],
                    "confidence": p["confidence"],
                    "conflicts": p["conflicts"],
                    "gaps": p["gaps"],
                }
                for p in troubled
            ],
            "actions": ["accept_partial", "retry", "cancel"],
        }
        return {
            "review_needed": bool(troubled),
            "review_payload": payload,
            "pending": [p["competitor"] for p in troubled],
        }

    def human(state: ResearchState):
        selection_stage = bool(state.get("selection_issues")) and not state.get("profiles")
        payload = (
            {
                "stage": "competitor_selection",
                "issues": state["selection_issues"],
                "selected": state["selected"],
                "candidates": state.get("discovery", {}).get("candidates", []),
                "actions": ["accept_partial", "clarify", "replace", "cancel"],
            }
            if selection_stage
            else state["review_payload"]
        )
        # No external calls before interrupt: the node restarts on resume.
        decision = interrupt(payload)
        action = decision.get("action")
        if action not in payload["actions"]:
            raise ValueError("Choose one of the offered review actions")
        clarification = str(decision.get("clarification", ""))[:1000]
        update = {
            "human_decisions": [{"time": utcnow(), "stage": payload["stage"], **decision}],
            "review_needed": False,
            "selection_issues": [],
        }
        if action == "cancel":
            update.update(status="cancelled", next_action="end")
        elif action == "clarify":
            if not clarification.strip():
                raise ValueError("Provide the target product or scope clarification")
            update.update(clarification=clarification, selection_caveats=[], next_action="discover")
        elif action == "replace":
            candidates = [
                Candidate.model_validate(c).model_dump() for c in decision.get("competitors", [])
            ]
            domains = {c["domain"] for c in candidates}
            if len(candidates) != 3 or len(domains) != 3 or state["request"]["domain"] in domains:
                raise ValueError("Supply three distinct competitors, excluding the target company")
            candidates = [
                {**c, "rank_score": 0, "selection_method": "human selection"} for c in candidates
            ]
            update.update(
                selected=candidates,
                pending=candidates,
                selection_caveats=[],
                next_action="research",
            )
        elif action == "retry":
            update.update(clarification=clarification, next_action="research")
        else:
            update["next_action"] = "research" if selection_stage else "synthesize"
        store.event(
            state["run_id"], "Human reviewer", "decision", payload["stage"], decision=decision
        )
        return update

    def human_route(state: ResearchState):
        return {
            "end": END,
            "discover": "discover",
            "research": "dispatch",
            "synthesize": "synthesize",
        }[state["next_action"]]

    def synthesize(state: ResearchState):
        selected_domains = {c["domain"] for c in state["selected"]}
        profiles = {d: p for d, p in state.get("profiles", {}).items() if d in selected_domains}
        safe_profiles = {
            d: {
                "competitor": p["competitor"],
                "claims": [c for c in p["claims"] if c["accepted"]],
                "gaps": p["gaps"],
                "conflicts": p["conflicts"],
            }
            for d, p in profiles.items()
        }
        allowed_refs = {f"{d}:{c['id']}" for d, p in safe_profiles.items() for c in p["claims"]}
        discovery = state.get("discovery", {})
        baseline_verified = discovery.get("target_supported", False)
        baseline = (
            discovery.get("target_summary", "Not established")
            if baseline_verified
            else "Target baseline was not verified."
        )
        synthesis_gap = None
        try:
            output = llm.generate(
                state["run_id"],
                "Orchestrator",
                Briefing,
                "Compile a concise competitive intelligence briefing with summary, opportunities, "
                "threats and recommendations. All insights are strategic interpretations. Cite "
                "accepted claims using domain:claim_id references. Do not turn unverified or "
                "missing facts into conclusions. Do not assert target-company advantages without "
                "target evidence. Limit to eight insights. Recommendations should identify "
                "what requires customer validation.",
                {
                    "request": state["request"],
                    "target_baseline": baseline,
                    "profiles": safe_profiles,
                },
            )
            # Reject an entire insight containing any fabricated or missing reference.
            insights = [
                i.model_dump()
                for i in output.insights
                if i.claim_refs and all(r in allowed_refs for r in i.claim_refs)
            ]
            if len(insights) < len(output.insights):
                synthesis_gap = "Some synthesis insights were omitted due to invalid citations."
        except BudgetExceeded as exc:
            insights, synthesis_gap = [], str(exc)
        partial = len(profiles) != 3 or any(
            p["completeness"] < 1 or p["conflicts"] for p in profiles.values()
        )
        caveats = state.get("selection_caveats", state.get("selection_issues", []))
        partial = partial or bool(caveats) or bool(synthesis_gap) or not baseline_verified
        briefing = {
            "generated_at": utcnow(),
            "company": state["request"]["company"],
            "demo": state["request"]["demo"],
            "scope": state["request"],
            "target_baseline": baseline,
            "target_baseline_verified": baseline_verified,
            "target_evidence_ids": state.get("discovery", {}).get("target_evidence_ids", []),
            "discovery_evidence": state.get("discovery_evidence", []),
            "candidate_ranking": state.get("discovery", {}).get("candidates", []),
            "selection_issues": caveats,
            "research_plan": state["plan"],
            "profiles": profiles,
            "insights": insights,
            "human_decisions": state.get("human_decisions", []),
            "synthesis_gap": synthesis_gap,
            "status": "partial" if partial else "complete",
        }
        store.event(
            state["run_id"],
            "Orchestrator",
            "briefing_complete",
            state["request"]["company"],
            status=briefing["status"],
            profiles=len(profiles),
        )
        return {"briefing": briefing, "status": briefing["status"]}

    graph = StateGraph(ResearchState)
    for name, node in (
        ("plan", plan),
        ("discover", discover),
        ("dispatch", dispatch),
        ("assess", assess),
        ("human", human),
        ("synthesize", synthesize),
    ):
        graph.add_node(name, node)
    graph.add_node("competitor_research", competitor_graph)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "discover")
    graph.add_conditional_edges(
        "discover",
        selection_route,
        {"synthesize": "synthesize", "human": "human", "dispatch": "dispatch"},
    )
    graph.add_conditional_edges("dispatch", fanout, ["competitor_research", "assess"])
    graph.add_edge("competitor_research", "assess")
    graph.add_conditional_edges(
        "assess",
        lambda s: "human" if s["review_needed"] else "synthesize",
        {"human": "human", "synthesize": "synthesize"},
    )
    graph.add_conditional_edges("human", human_route, [END, "discover", "dispatch", "synthesize"])
    graph.add_edge("synthesize", END)
    return graph.compile(checkpointer=checkpointer)

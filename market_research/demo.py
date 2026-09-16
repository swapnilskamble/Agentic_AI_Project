"""Fictional fixtures exercise the actual graph, budgets, review and interrupts offline."""

import hashlib
from datetime import datetime, timedelta, timezone

from .models import (
    Briefing,
    Candidate,
    Claim,
    ClaimReview,
    CollectionPlan,
    Discovery,
    DiscoveryReview,
    Evidence,
    Extraction,
    Insight,
    ResearchPlan,
    Review,
    SearchTask,
    SelectionAssessment,
    utcnow,
)
from .storage import Store

COMPANIES = [
    ("TaskHarbor", "taskharbor.example", 12, "Workflow"),
    ("SprintNest", "sprintnest.example", 18, "Automation"),
    ("TeamBeacon", "teambeacon.example", 9, "Collaboration"),
    ("DocGarden", "docgarden.example", 7, "Collaboration"),
    ("MetricCove", "metriccove.example", 24, "Analytics"),
]


class DemoSearch:
    def __init__(self, store: Store):
        self.store = store

    def search(self, run_id, query, category, domain, news_days, official_only=False):
        self.store.check(run_id)
        key = self.store.cache_key(
            "demo_search", {"query": query, "category": category, "domain": domain}
        )
        cached = self.store.cached(run_id, key)
        if cached is not None:
            self.store.event(run_id, "Research Collector", "cache_hit", domain, query=query)
            return cached
        self.store.reserve(run_id, "search")
        self.store.event(
            run_id,
            "Research Collector",
            "tool_start",
            domain,
            tool="Fictional demo search",
            query=query,
            category=category,
        )
        request = self.store.get(run_id)["request"]
        info = next((c for c in COMPANIES if c[1] == domain), None)
        name, price, feature = (
            (info[0], info[2], info[3]) if info else (request["company"], 15, "Workflow")
        )
        news_date = (
            (datetime.now(timezone.utc) - timedelta(days=min(7, news_days - 1))).date().isoformat()
        )
        if category == "discovery":
            text = f"{name} provides business workflow software. " + " ".join(
                f"{c[0]} ({c[1]}) competes for workflow, automation and team collaboration budgets."
                for c in COMPANIES
            )
        else:
            text = {
                "pricing": f"{name} Pro costs USD {price} per seat per month, billed annually. "
                "Enterprise pricing is contact sales; US list price excludes tax.",
                "features": f"{name} offers {feature.lower()} management, integrations and reporting.",
                "positioning": f"{name} describes itself as workflow software for growing SMB teams.",
                "news": f"{name} announced workflow automation improvements on {news_date}.",
            }.get(category, f"{name} business workflow software.")
        url = f"https://{domain}/{category}"
        records = [
            Evidence(
                id="e_" + hashlib.sha256((url + text).encode()).hexdigest()[:16],
                url=url,
                title=f"{name}: {category} (fictional demo)",
                publisher=domain,
                text=text,
                retrieved_at=utcnow(),
                published_at=news_date if category == "news" else None,
                kind="news" if category == "news" else "web",
                content_type="full_page",
                category=category,
                official=True,
            ).model_dump()
        ]
        if (
            request["demo_scenario"] == "conflict"
            and category == "pricing"
            and domain == COMPANIES[0][1]
        ):
            alternate = Evidence.model_validate(records[0])
            alternate.id += "_alt"
            alternate.url += "-alternate"
            alternate.text = text.replace(f"USD {price}", f"USD {price + 8}")
            alternate.title += " (conflicting official page)"
            records.append(alternate.model_dump())
        self.store.put_cache(run_id, key, records)
        self.store.event(
            run_id,
            "Research Collector",
            "tool_complete",
            domain,
            count=len(records),
            evidence_ids=[r["id"] for r in records],
        )
        return records


class DemoLLM:
    def __init__(self, store: Store):
        self.store = store

    def generate(self, run_id, agent, schema, instructions, payload):
        self.store.check(run_id)
        key = self.store.cache_key("demo_model", {"schema": schema.__name__, "payload": payload})
        cached = self.store.cached(run_id, key)
        if cached is not None:
            return schema.model_validate(cached)
        self.store.reserve(run_id, "model")
        self.store.event(run_id, agent, "analysis_start", schema.__name__, mode="fictional demo")
        request = self.store.get(run_id)["request"]
        if schema is ResearchPlan:
            output = ResearchPlan(
                objective="Compare competing SMB workflow SaaS products using cited evidence.",
                discovery_queries=["SMB workflow management direct competitors alternatives"],
                comparison_focus=[
                    "Annual seat pricing",
                    "Workflow and automation",
                    "Recent launches",
                ],
            )
        elif schema is CollectionPlan:
            output = CollectionPlan(
                tasks=[
                    SearchTask(query=q, category=category, official_only=category != "news")
                    for q, category in [
                        ("Pro Enterprise pricing billing", "pricing"),
                        ("workflow automation integrations features", "features"),
                        ("customers positioning SMB", "positioning"),
                        ("product launch funding acquisition news", "news"),
                    ]
                ]
            )
        elif schema is DiscoveryReview:
            output = DiscoveryReview(
                target_supported=True,
                target_explanation="The fictional source identifies the target product.",
                assessments=[
                    SelectionAssessment(
                        domain=c["domain"],
                        supported=True,
                        directness=1,
                        explanation="Candidate is explicitly identified in fictional evidence.",
                    )
                    for c in payload["discovery"]["candidates"]
                ],
            )
        elif schema is Discovery:
            ids = [e["id"] for e in payload["evidence"]]
            output = Discovery(
                target_summary=f"{request['company']} is a fictional SMB workflow SaaS platform.",
                target_evidence_ids=ids,
                candidates=[
                    Candidate(
                        name=c[0],
                        domain=c[1],
                        relationship="direct" if i < 3 else "adjacent",
                        reason="Competes for SMB workflow and collaboration budgets.",
                        product_overlap=0.95 - i * 0.12,
                        customer_overlap=0.9 - i * 0.1,
                        geography_overlap=0.9,
                        evidence_ids=ids,
                    )
                    for i, c in enumerate(COMPANIES)
                ],
                ambiguity="Two similarly named products need disambiguation."
                if (request["demo_scenario"] == "ambiguous" and not payload.get("clarification"))
                else None,
            )
        elif schema is Extraction:
            claims = []
            for index, e in enumerate(payload["evidence"]):
                if e["category"] not in {"pricing", "features", "positioning", "news"}:
                    continue
                category = e["category"]
                info = next(
                    (c for c in COMPANIES if c[1] == payload["competitor"]["domain"]), COMPANIES[0]
                )
                claims.append(
                    Claim(
                        id=f"c{index}",
                        category=category,
                        label=info[3] if category == "features" else category.title(),
                        statement=e["text"],
                        evidence_ids=[e["id"]],
                        currency="USD" if category == "pricing" else None,
                        amount=info[2] + (8 if e["id"].endswith("_alt") else 0)
                        if category == "pricing"
                        else None,
                        billing_period="Monthly equivalent, billed annually"
                        if category == "pricing"
                        else None,
                        unit="seat" if category == "pricing" else None,
                        region="US" if category == "pricing" else None,
                        event_date=e["published_at"] if category == "news" else None,
                        publication_date=e["published_at"],
                        interpretation=False,
                    )
                )
            output = Extraction(claims=claims, gaps=[])
        elif schema is Review:
            conflict = (
                request["demo_scenario"] == "conflict"
                and payload["competitor"]["domain"] == COMPANIES[0][1]
            )
            output = Review(
                assessments=[
                    ClaimReview(
                        claim_id=c["id"],
                        supported=True,
                        directness=1,
                        explanation="Directly stated in the fictional source.",
                        conflict="Two official pages quote different prices for the same plan and billing basis."
                        if conflict and c["category"] == "pricing"
                        else None,
                    )
                    for c in payload["claims"]
                ],
                followup_queries=["Pro plan official pricing clarification"] if conflict else [],
            )
        elif schema is Briefing:
            output = Briefing(
                insights=[
                    Insight(
                        kind="summary",
                        text="The three fictional competitors serve overlapping SMB workflow needs.",
                        claim_refs=[
                            f"{domain}:{c['id']}"
                            for domain, p in payload["profiles"].items()
                            for c in p["claims"]
                            if c["accepted"] and c["category"] == "positioning"
                        ],
                    ),
                    Insight(
                        kind="recommendation",
                        text="Validate buyer demand for deeper workflow automation before prioritizing investment.",
                        claim_refs=[
                            f"{domain}:{c['id']}"
                            for domain, p in payload["profiles"].items()
                            for c in p["claims"]
                            if c["accepted"] and c["category"] == "features"
                        ],
                    ),
                ]
            )
        else:
            raise ValueError(schema)
        self.store.put_cache(run_id, key, output.model_dump())
        self.store.event(run_id, agent, "analysis_complete", schema.__name__, mode="fictional demo")
        return output

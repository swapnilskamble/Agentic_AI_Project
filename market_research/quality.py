from collections import Counter
from datetime import datetime, timedelta, timezone

from .models import Extraction, Review
from .providers import parse_date

REQUIRED = {"pricing", "features", "positioning", "news"}


def evaluate(extraction: Extraction, review: Review, evidence: list[dict], news_days: int) -> dict:
    sources = {e["id"]: e for e in evidence}
    assessments = {a.claim_id: a for a in review.assessments}
    claim_counts = Counter(c.id for c in extraction.claims)
    assessment_counts = Counter(a.claim_id for a in review.assessments)
    claims, rejected, conflicts = [], [], []
    now = datetime.now(timezone.utc)
    seen_news = set()
    for claim in extraction.claims:
        assessment = assessments.get(claim.id)
        ids = claim.evidence_ids
        valid = (
            bool(ids)
            and all(i in sources for i in ids)
            and claim_counts[claim.id] == 1
            and assessment_counts[claim.id] == 1
        )
        support = bool(valid and assessment and assessment.supported)
        cited = [sources[i] for i in ids if i in sources]
        if claim.category == "news":
            # Search freshness is not enough: require a dated source inside the requested window.
            dates = [parse_date(e["published_at"]) for e in cited]
            support = support and any(
                d and now - timedelta(days=news_days) <= d <= now for d in dates
            )
            publication = parse_date(claim.publication_date)
            if claim.publication_date and (
                publication is None or not any(d and d.date() == publication.date() for d in dates)
            ):
                support = False
            event = parse_date(claim.event_date)
            if event and (event < now - timedelta(days=news_days) or event > now):
                support = False
            signature = " ".join(claim.statement.lower().split())
            if signature in seen_news:
                continue
            seen_news.add(signature)
        authority = max((1.0 if e["official"] else 0.65 for e in cited), default=0)
        depth = max(
            (
                {"full_page": 1.0, "highlights": 0.85, "snippet": 0.45}[e["content_type"]]
                for e in cited
            ),
            default=0,
        )
        publishers = {e["publisher"] for e in cited}
        # Corroboration only counts distinct publishers with different text, not syndication.
        distinct_text = {" ".join(e["text"].lower().split()) for e in cited}
        corroboration = 1.0 if len(publishers) > 1 and len(distinct_text) > 1 else 0.4
        fresh = 1.0 if claim.category != "news" else float(bool(support))
        directness = assessment.directness if assessment else 0
        score = (
            round(
                0.25 * authority
                + 0.35 * directness
                + 0.15 * depth
                + 0.10 * corroboration
                + 0.15 * fresh,
                3,
            )
            if support
            else 0.0
        )
        conflict = assessment.conflict if assessment else None
        if conflict:
            score = min(score, 0.55)
            conflicts.append(conflict)
        # Snippets alone must be researched further, regardless of the LLM support judgment.
        if depth <= 0.45:
            score = min(score, 0.69)
        accepted = support and score >= 0.8 and not conflict
        row = {
            **claim.model_dump(),
            "confidence": score,
            "accepted": accepted,
            "support": support,
            "review_reason": assessment.explanation if assessment else "Missing assessment",
            "conflict": conflict,
            "score_components": {
                "authority": authority,
                "directness": directness,
                "depth": depth,
                "corroboration": corroboration,
                "freshness": fresh,
            },
        }
        if claim.category == "news" and not row["publication_date"]:
            verified_dates = [d for d in dates if d and now - timedelta(days=news_days) <= d <= now]
            row["publication_date"] = max(verified_dates).isoformat() if verified_dates else None
        (claims if support else rejected).append(row)
    covered = {c["category"] for c in claims if c["accepted"]}
    gaps = sorted(REQUIRED - covered)
    confidence = round(sum(c["confidence"] for c in claims) / len(claims), 3) if claims else 0
    return {
        "claims": claims,
        "rejected": rejected,
        "confidence": confidence,
        "completeness": len(covered) / len(REQUIRED),
        "gaps": gaps,
        "reported_gaps": extraction.gaps,
        "conflicts": sorted(set(conflicts)),
        "followup_queries": review.followup_queries[:3],
    }

"""Shared, citation-preserving report rendering for UI and downloads."""

from .providers import canonical_url


def source_link(evidence: dict) -> str:
    url = canonical_url(evidence["url"])
    label = evidence["title"].replace("[", "(").replace("]", ")").replace("\n", " ")
    return f"[{label}]({url.replace(')', '%29')})" if url else label


def comparison_rows(briefing: dict) -> list[dict]:
    rows = []
    for profile in briefing.get("profiles", {}).values():
        claims = [c for c in profile["claims"] if c["accepted"]]
        row = {"Competitor": profile["competitor"]["name"]}
        for category in ("pricing", "features", "positioning"):
            row[category.title()] = (
                "\n".join(c["statement"] for c in claims if c["category"] == category)
                or "Unverified / not found"
            )
        row["Confidence"] = profile["confidence"]
        row["Coverage"] = f"{profile['completeness']:.0%}"
        rows.append(row)
    return rows


def markdown_briefing(briefing: dict) -> str:
    lines = [f"# Competitor analysis: {briefing['company']}", ""]
    if briefing.get("demo"):
        lines += [
            "**FICTIONAL DEMO — sample companies, pricing and news; not market research.**",
            "",
        ]
    lines += [
        f"Status: {briefing['status']} · Generated: {briefing['generated_at']}",
        "",
        "## Research scope",
        "",
        f"Product: {briefing['scope'].get('scope') or 'Company-wide'}",
        f"Geography: {briefing['scope']['geography']} · Segment: {briefing['scope']['segment']}",
        f"News window: {briefing['scope']['news_days']} days",
        "",
        "## Target baseline",
        "",
        briefing["target_baseline"],
        "",
    ]
    discovery = {e["id"]: e for e in briefing.get("discovery_evidence", [])}
    lines += [
        source_link(discovery[i]) for i in briefing.get("target_evidence_ids", []) if i in discovery
    ]
    lines += ["", "## Strategic interpretations", ""]
    refs = {
        f"{domain}:{c['id']}": (c, profile)
        for domain, profile in briefing.get("profiles", {}).items()
        for c in profile["claims"]
        if c["accepted"]
    }
    for insight in briefing.get("insights", []):
        lines.append(f"- **{insight['kind'].title()}:** {insight['text']}")
        for ref in insight["claim_refs"]:
            if ref in refs:
                claim, profile = refs[ref]
                evidence = {e["id"]: e for e in profile["evidence"]}
                lines.append(
                    "  Evidence: "
                    + ", ".join(
                        source_link(evidence[i]) for i in claim["evidence_ids"] if i in evidence
                    )
                )
    if briefing.get("synthesis_gap"):
        lines += [briefing["synthesis_gap"], ""]
    for profile in briefing.get("profiles", {}).values():
        company = profile["competitor"]
        lines += [
            "",
            f"## {company['name']}",
            "",
            company["reason"],
            f"Selection: {company.get('selection_method', 'Overlap ranking')}",
            f"Evidence confidence: {profile['confidence']:.0%}; coverage: {profile['completeness']:.0%}",
            "",
        ]
        evidence = {e["id"]: e for e in profile["evidence"]}
        for category in ("pricing", "features", "positioning", "news"):
            lines += ["", f"### {category.title()}", ""]
            accepted = [c for c in profile["claims"] if c["accepted"] and c["category"] == category]
            if not accepted:
                lines += ["Unverified / not found.", ""]
            for claim in accepted:
                lines += [
                    f"- {claim['statement']}",
                    "  Sources: "
                    + ", ".join(
                        source_link(evidence[i]) for i in claim["evidence_ids"] if i in evidence
                    ),
                ]
        if profile["conflicts"]:
            lines += ["", "### Unresolved conflicts", "", *[f"- {c}" for c in profile["conflicts"]]]
        lines += [
            "",
            "Research gaps: " + (", ".join(profile["gaps"]) or "None"),
            "Stop condition: " + profile["stop_reason"],
        ]
    lines += ["", "## Human decisions", ""]
    lines += [
        f"- {d['time']}: {d['stage']} — {d['action']}" for d in briefing.get("human_decisions", [])
    ]
    lines += ["", "Confidence is an evidence quality heuristic, not a probability of correctness."]
    return "\n".join(lines)

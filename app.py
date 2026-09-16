import json

import streamlit as st

from market_research.industries import PROFILES
from market_research.models import Candidate, ResearchRequest
from market_research.rendering import comparison_rows, markdown_briefing, source_link
from market_research.service import ResearchService

st.set_page_config(page_title="Market Lens | Competitor Intelligence", page_icon="◈", layout="wide")
st.markdown(
    """<style>
    .stApp {background: #f7f8fa;}
    [data-testid="stMetric"] {background: white; padding: 16px; border: 1px solid #e5e7eb; border-radius: 12px;}
    h1 {letter-spacing: -0.04em;}
    .block-container {padding-top: 2.4rem;}
</style>""",
    unsafe_allow_html=True,
)


@st.cache_resource
def get_service():
    return ResearchService()


service = get_service()
st.title("Market Lens")
st.caption("Competitor intelligence · Evidence you can inspect · Research you can follow")

with st.sidebar:
    st.header("Research a company")
    demo = st.toggle("Offline demo", value=True)
    if demo:
        st.caption("Fictional companies and evidence. No API calls.")
    with st.form("research_inputs"):
        company = st.text_input("Company", "FlowPilot" if demo else "")
        domain = st.text_input("Company domain", "flowpilot.example" if demo else "")
        scope = st.text_input("Product / use case", "Team workflow management" if demo else "")
        geography = st.text_input("Geography", "US")
        segment = st.selectbox(
            "Customer segment", ["SMB", "Mid-market", "Enterprise", "All business sizes"]
        )
        industry = st.selectbox("Industry", list(PROFILES), format_func=lambda k: PROFILES[k].name)
        news_days = st.slider("News window (days)", 1, 365, 90)
        with st.expander("Research limits"):
            queries = st.number_input("Search attempts", 5, 120, 40)
            model_calls = st.number_input("Analysis attempts", 5, 100, 35)
            seconds = st.number_input("Active time window (seconds)", 30, 3600, 600)
        scenario = (
            st.selectbox("Demo scenario", ["normal", "conflict", "ambiguous"]) if demo else "normal"
        )
        submitted = st.form_submit_button(
            "Start research", type="primary", use_container_width=True
        )
    if submitted:
        try:
            request = ResearchRequest(
                company=company,
                domain=domain,
                scope=scope,
                geography=geography,
                segment=segment,
                industry=industry,
                news_days=news_days,
                max_queries=queries,
                max_model_calls=model_calls,
                max_seconds=seconds,
                demo=demo,
                demo_scenario=scenario,
            )
            st.session_state.run_id = service.create(request)
        except Exception as exc:
            st.error(str(exc))
    runs = service.store.list_runs()
    if runs:
        options = [r["id"] for r in runs]
        labels = {
            r["id"]: f"{json.loads(r['request'])['company']} · {r['status']} · {r['id'][:6]}"
            for r in runs
        }
        selected = st.selectbox(
            "Saved research",
            options,
            format_func=lambda i: labels[i],
            index=options.index(st.session_state.run_id)
            if st.session_state.get("run_id") in options
            else 0,
        )
        if st.button("Open saved run", use_container_width=True):
            st.session_state.run_id = selected
            st.rerun()


def display_claim(claim, evidence):
    st.markdown(f"**{claim['label']}** — {claim['statement']}")
    st.caption(f"Evidence confidence {claim['confidence']:.0%} · {claim['review_reason']}")
    for source_id in claim["evidence_ids"]:
        if source_id in evidence:
            st.markdown(source_link(evidence[source_id]))


def display_analysis(briefing):
    tabs = st.tabs(
        ["Briefing", "Comparison", "Competitor profiles", "Recent news", "Sources", "Downloads"]
    )
    with tabs[0]:
        st.subheader("Target baseline")
        st.write(briefing["target_baseline"])
        discovery = {e["id"]: e for e in briefing.get("discovery_evidence", [])}
        for i in briefing.get("target_evidence_ids", []):
            if i in discovery:
                st.markdown(source_link(discovery[i]))
        st.subheader("Strategic interpretations")
        refs = {
            f"{d}:{c['id']}": (c, p)
            for d, p in briefing["profiles"].items()
            for c in p["claims"]
            if c["accepted"]
        }
        for insight in briefing["insights"]:
            st.markdown(f"**{insight['kind'].title()}**")
            st.write(insight["text"])
            with st.expander("Supporting findings"):
                for ref in insight["claim_refs"]:
                    if ref in refs:
                        c, p = refs[ref]
                        display_claim(c, {e["id"]: e for e in p["evidence"]})
        if not briefing["insights"]:
            st.info(
                "No sufficiently supported strategic synthesis is available. Review the profiles and gaps."
            )
        if briefing.get("synthesis_gap"):
            st.warning(briefing["synthesis_gap"])
        if briefing.get("human_decisions"):
            with st.expander("Human review decisions"):
                st.json(briefing["human_decisions"])
    with tabs[1]:
        st.dataframe(comparison_rows(briefing), hide_index=True, use_container_width=True)
        with st.expander("How competitors were selected"):
            st.caption(
                "Overlap score: product 50%, customer 35%, geography 15%. Scores are model-assisted heuristics."
            )
            st.dataframe(
                [
                    {k: c[k] for k in ("name", "domain", "relationship", "reason", "rank_score")}
                    for c in briefing["candidate_ranking"]
                ],
                hide_index=True,
            )
            st.json(briefing.get("selection_issues", []))
    with tabs[2]:
        for profile in briefing["profiles"].values():
            with st.expander(profile["competitor"]["name"], expanded=True):
                st.write(profile["competitor"]["reason"])
                st.caption(
                    f"Confidence {profile['confidence']:.0%} · Coverage {profile['completeness']:.0%} · "
                    f"{profile['rounds']} research rounds"
                )
                evidence = {e["id"]: e for e in profile["evidence"]}
                for category in ("pricing", "features", "positioning"):
                    st.markdown(f"#### {category.title()}")
                    claims = [
                        c for c in profile["claims"] if c["accepted"] and c["category"] == category
                    ]
                    if not claims:
                        st.write("Unverified / not found")
                    for claim in claims:
                        display_claim(claim, evidence)
                if profile["conflicts"]:
                    st.warning("Unresolved: " + "; ".join(profile["conflicts"]))
                st.caption("Gaps: " + (", ".join(profile["gaps"]) or "None"))
                st.caption("Stop condition: " + profile["stop_reason"])
                with st.expander("Review uncertain and rejected claims"):
                    for claim in [c for c in profile["claims"] if not c["accepted"]] + profile[
                        "rejected"
                    ]:
                        display_claim(claim, evidence)
                        st.json(claim["score_components"])
    with tabs[3]:
        news = [
            (c, p)
            for p in briefing["profiles"].values()
            for c in p["claims"]
            if c["accepted"] and c["category"] == "news"
        ]
        for claim, profile in sorted(
            news, key=lambda item: item[0].get("publication_date") or "", reverse=True
        ):
            st.markdown(
                f"**{profile['competitor']['name']} · {claim.get('publication_date') or 'Source dated'}**"
            )
            display_claim(claim, {e["id"]: e for e in profile["evidence"]})
            st.caption("Event date: " + (claim.get("event_date") or "Not stated"))
        if not news:
            st.info("No verified news inside the selected window.")
    with tabs[4]:
        records = {e["id"]: e for e in briefing.get("discovery_evidence", [])}
        for p in briefing["profiles"].values():
            records.update({e["id"]: e for e in p["evidence"]})
        for e in records.values():
            with st.expander(f"{e['title']} · {e['content_type']}"):
                st.markdown(source_link(e))
                st.caption(
                    f"Published: {e['published_at'] or 'Unknown'} · Retrieved: {e['retrieved_at']} · "
                    f"{'Official company source' if e['official'] else 'External source'}"
                )
                st.text(e["text"])
    with tabs[5]:
        st.download_button(
            "Download Markdown briefing",
            markdown_briefing(briefing),
            "competitor-briefing.md",
            "text/markdown",
        )
        st.download_button(
            "Download structured JSON",
            json.dumps(briefing, indent=2),
            "competitor-briefing.json",
            "application/json",
        )


def review_controls(run_id, row):
    review = row["result"]["review"][0]
    st.warning("Human review needed")
    st.json(review)
    with st.form(f"review_{run_id}"):
        action = st.selectbox(
            "Decision", review["actions"], format_func=lambda s: s.replace("_", " ").title()
        )
        clarification = st.text_area("Product clarification or targeted research direction")
        replacements = st.text_area(
            "Replacement competitors (for Replace)",
            placeholder="Company name | company.com\nCompany name | company.com\nCompany name | company.com",
        )
        st.caption(
            "Accept partial preserves gaps and conflicts. It does not verify disputed claims. "
            "Retry uses the remaining call budgets and grants another active time window."
        )
        submitted = st.form_submit_button("Apply decision and resume", type="primary")
    if submitted:
        try:
            decision = {"action": action, "clarification": clarification}
            if action == "clarify" and not clarification.strip():
                raise ValueError("Please provide a product or scope clarification")
            if action == "replace":
                candidates = []
                for line in replacements.splitlines():
                    if not line.strip():
                        continue
                    parts = line.split("|", 1)
                    if len(parts) != 2:
                        raise ValueError("Use Company name | company.com for each competitor")
                    candidates.append(
                        Candidate(
                            name=parts[0].strip(),
                            domain=parts[1].strip(),
                            relationship="direct",
                            reason="Selected by human reviewer.",
                            product_overlap=0,
                            customer_overlap=0,
                            geography_overlap=0,
                            evidence_ids=[],
                        ).model_dump()
                    )
                domains = {c["domain"] for c in candidates}
                if len(candidates) != 3 or len(domains) != 3 or row["request"]["domain"] in domains:
                    raise ValueError("Supply three distinct competitors excluding the target")
                decision["competitors"] = candidates
            service.resume(run_id, decision)
            st.rerun()
        except Exception as exc:
            st.error(str(exc))


@st.fragment(run_every="2s")
def run_panel(run_id):
    row = service.store.get(run_id)
    if row["request"]["demo"]:
        st.info("FICTIONAL DEMO — sample companies, pricing and news. No live market claims.")
    st.subheader(f"{row['request']['company']} · {row['status'].replace('_', ' ').title()}")
    cols = st.columns(4)
    cols[0].metric("Search attempts", f"{row['queries']} / {row['request']['max_queries']}")
    cols[1].metric(
        "Analysis attempts", f"{row['model_calls']} / {row['request']['max_model_calls']}"
    )
    cols[2].metric("Model tokens", f"{row['tokens']:,}")
    cols[3].metric("Run ID", run_id[:8])
    if row["status"] == "awaiting_review":
        review_controls(run_id, row)
    elif row["status"] == "error":
        st.error(row["error"])
        errors = [e for e in service.store.events(run_id) if e["action"] == "error"]
        if errors and errors[-1]["details"].get("diagnostics"):
            with st.expander("Failure details"):
                st.json(errors[-1]["details"]["diagnostics"])
        if st.button("Retry saved run"):
            try:
                service.resume(run_id)
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
    elif row["status"] == "running":
        if service.active(run_id):
            st.caption("Agents are researching. You can inspect their progress below.")
            if st.button("Cancel research"):
                service.cancel(run_id)
        else:
            st.warning("This run is not active in this process. Resume from its saved checkpoint.")
            if st.button("Resume interrupted run"):
                service.resume(run_id)
                st.rerun()
    elif row["status"] in {"complete", "partial"} and row["result"]:
        display_analysis(row["result"])
    events = service.store.events(run_id)
    with st.expander("Agent collaboration trace", expanded=row["status"] == "running"):
        st.caption(
            "Recorded delegation, tool calls, evidence handoffs, confidence, retries and stop decisions."
        )
        st.graphviz_chart(
            'digraph {rankdir=LR; "Orchestrator" -> "Discovery"; "Discovery" -> "Research × 3"; '
            '"Research × 3" -> "Extractor"; "Extractor" -> "Reviewer"; "Reviewer" -> "Research × 3" '
            '[label="feedback"]; "Reviewer" -> "Orchestrator"; "Orchestrator" -> "Human" '
            '[label="escalation"]; "Human" -> "Orchestrator" [label="resume"];}'
        )
        st.dataframe(
            [{k: e[k] for k in ("time", "agent", "action", "subject")} for e in events],
            hide_index=True,
            use_container_width=True,
        )
        if events:
            selected = st.selectbox(
                "Inspect event",
                range(len(events)),
                format_func=lambda i: f"{events[i]['agent']} · {events[i]['action']} · #{i + 1}",
            )
            st.json(events[selected]["details"])
        st.download_button(
            "Download trace JSON",
            json.dumps(events, indent=2),
            "agent-trace.json",
            "application/json",
        )
    st.caption(
        "Confidence scores describe evidence quality and are not calibrated probabilities of correctness."
    )


if st.session_state.get("run_id"):
    run_panel(st.session_state.run_id)
else:
    st.subheader("From company name to a cited competitive briefing")
    st.write(
        "Discover three competitors, investigate their products and pricing, verify recent news, "
        "and inspect how the agents reached their findings."
    )
    st.info(
        "Start with the offline demo in the sidebar, or configure your API keys and switch to live research."
    )

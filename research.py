import argparse
import json
from pathlib import Path

from market_research.models import ResearchRequest
from market_research.rendering import markdown_briefing
from market_research.service import ResearchService


def main():
    parser = argparse.ArgumentParser(
        description="Run the competitor intelligence LangGraph pipeline"
    )
    parser.add_argument("--company", default="FlowPilot")
    parser.add_argument("--domain", default="flowpilot.example")
    parser.add_argument("--scope", default="Team workflow management")
    parser.add_argument("--geography", default="US")
    parser.add_argument(
        "--live", action="store_true", help="Use You.com and OpenAI instead of fictional fixtures"
    )
    parser.add_argument("--scenario", choices=["normal", "conflict", "ambiguous"], default="normal")
    parser.add_argument("--resume", help="Saved run ID")
    parser.add_argument("--decision", help='Human decision JSON, e.g. {"action":"accept_partial"}')
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--output", help="Directory for report and trace exports")
    args = parser.parse_args()
    service = ResearchService(args.data_dir)
    try:
        if args.resume:
            run_id = args.resume
            service.resume(
                run_id, json.loads(args.decision) if args.decision else None, background=False
            )
        else:
            request = ResearchRequest(
                company=args.company,
                domain=args.domain,
                scope=args.scope,
                geography=args.geography,
                demo=not args.live,
                demo_scenario=args.scenario,
            )
            run_id = service.create(request, background=False)
        row = service.store.get(run_id)
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "status": row["status"],
                    "error": row["error"],
                    "search_attempts": row["queries"],
                    "analysis_attempts": row["model_calls"],
                },
                indent=2,
            )
        )
        if row["status"] == "awaiting_review":
            print(json.dumps(row["result"]["review"], indent=2))
        if args.output and row["status"] in {"complete", "partial"}:
            path = Path(args.output)
            path.mkdir(parents=True, exist_ok=True)
            (path / "briefing.md").write_text(markdown_briefing(row["result"]))
            (path / "briefing.json").write_text(json.dumps(row["result"], indent=2))
            (path / "trace.json").write_text(json.dumps(service.store.events(run_id), indent=2))
        return 1 if row["status"] == "error" else 0
    finally:
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())

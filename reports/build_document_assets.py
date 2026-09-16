"""Generate diagrams and a code-derived prompt/run inventory for the project overview."""

import ast
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from market_research.industries import get_profile
from market_research.storage import Store

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports"
OUT.mkdir(exist_ok=True)
FONT = "/System/Library/Fonts/Supplemental/Arial.ttf"
BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
NAVY, BLUE, TEAL, INK = "#13283e", "#e8f0ff", "#e2f4ef", "#23374a"


def canvas(title, height):
    im = Image.new("RGB", (1900, height), "#ffffff")
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, 1900, 125), fill=NAVY)
    d.text((65, 37), title, font=ImageFont.truetype(BOLD, 44), fill="white")
    return im, d


def box(d, bounds, title, body="", fill=BLUE):
    x1, y1, x2, y2 = bounds
    d.rounded_rectangle(bounds, radius=22, fill=fill, outline="#a6b7c8", width=3)
    title_font = ImageFont.truetype(BOLD, 28)
    body_font = ImageFont.truetype(FONT, 24)

    def wrap(value, font):
        lines, line = [], ""
        for word in value.split():
            candidate = (line + " " + word).strip()
            if line and d.textlength(candidate, font=font) > x2 - x1 - 35:
                lines.append(line)
                line = word
            else:
                line = candidate
        return lines + ([line] if line else [])

    lines = wrap(title, title_font)
    y = y1 + 16
    for line in lines:
        length = d.textlength(line, font=title_font)
        d.text(((x1 + x2 - length) / 2, y), line, font=title_font, fill=INK)
        y += 34
    for paragraph in body.split("\n"):
        for line in wrap(paragraph, body_font):
            length = d.textlength(line, font=body_font)
            d.text(((x1 + x2 - length) / 2, y + 8), line, font=body_font, fill=INK)
            y += 30


def arrow(d, points, label="", color="#496581"):
    d.line(points, fill=color, width=4)
    a, b = points[-2], points[-1]
    angle = math.atan2(b[1] - a[1], b[0] - a[0])
    head = [
        b,
        (b[0] - 18 * math.cos(angle - 0.45), b[1] - 18 * math.sin(angle - 0.45)),
        (b[0] - 18 * math.cos(angle + 0.45), b[1] - 18 * math.sin(angle + 0.45)),
    ]
    d.polygon(head, fill=color)
    if label:
        segments = list(zip(points, points[1:]))
        horizontal = [(a, b) for a, b in segments if a[1] == b[1]]
        a, b = max(horizontal or segments, key=lambda ab: math.dist(*ab))
        x, y = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        font = ImageFont.truetype(FONT, 24)
        w = d.textlength(label, font=font)
        d.rectangle((x - w / 2 - 8, y - 16, x + w / 2 + 8, y + 17), fill="white")
        d.text((x - w / 2, y - 14), label, font=font, fill=color)


def diagrams():
    im, d = canvas("Market Lens | System architecture", 1300)
    box(
        d,
        (90, 170, 1810, 310),
        "Streamlit interface",
        "Company + scope | Progress | Human review | Analysis | Evidence | Downloads",
    )
    box(
        d,
        (90, 390, 610, 630),
        "Research service",
        "Background workers\nRun lifecycle + resume\nOne worker per active run",
    )
    box(
        d,
        (710, 390, 1230, 630),
        "LangGraph orchestration",
        "Parent StateGraph\nThree competitor subgraphs\nConfidence + stop routing",
        TEAL,
    )
    box(
        d,
        (1330, 390, 1810, 630),
        "Provider adapters",
        "You.com web + news\nOpenAI structured analysis\nRetries + shared model slot",
    )
    arrow(d, [(350, 310), (350, 390)], "Start / resume")
    arrow(d, [(610, 510), (710, 510)])
    arrow(d, [(1230, 510), (1330, 510)])
    box(
        d,
        (90, 780, 890, 1030),
        "Durable research state",
        "checkpoints.sqlite: LangGraph state\nresearch.sqlite: runs, budgets, caches, events\nProfiles merged by competitor domain",
        TEAL,
    )
    box(
        d,
        (1010, 780, 1810, 1030),
        "External APIs",
        "You.com POST /v1/search\nOpenAI Responses API\nKeys supplied through environment / .env",
    )
    arrow(d, [(350, 630), (350, 780)], "Persist")
    arrow(d, [(970, 630), (970, 715), (660, 715), (660, 780)], "Checkpoint")
    arrow(d, [(1570, 630), (1570, 780)], "HTTPS")
    box(
        d,
        (90, 1120, 1810, 1230),
        "Outputs",
        "Cited briefing + comparison + profiles + news | Markdown + JSON | Collaboration trace",
    )
    arrow(d, [(490, 1030), (490, 1120)])
    im.save(OUT / "architecture.png")

    im, d = canvas("Market Lens | LangGraph workflow and feedback", 1720)
    box(d, (580, 165, 1190, 275), "START -> plan", "Orchestrator creates bounded research strategy")
    box(d, (580, 340, 1190, 490), "discover", "Candidate discovery + independent selection review")
    arrow(d, [(885, 275), (885, 340)])
    box(
        d,
        (1330, 340, 1830, 510),
        "human: selection",
        "Accept partial | Clarify\nReplace three | Cancel",
        "#fff0d9",
    )
    arrow(d, [(1190, 415), (1330, 415)], "Ambiguous")
    arrow(d, [(1580, 340), (1580, 305), (1040, 305), (1040, 340)], "Clarify -> rediscover")
    box(
        d, (580, 560, 1190, 670), "dispatch", "Send: up to three parallel competitor branches", TEAL
    )
    arrow(d, [(885, 490), (885, 560)], "Selection clear")
    arrow(d, [(1580, 510), (1580, 610), (1190, 610)], "Accept / replace")
    for x, title in [(90, "Competitor 1"), (690, "Competitor 2"), (1290, "Competitor 3")]:
        box(
            d,
            (x, 760, x + 520, 960),
            title,
            "collect -> extract -> review\nFollow up if gaps + limits allow\nfinish: one profile",
            TEAL,
        )
        arrow(d, [(885, 670), (885, 715), (x + 260, 715), (x + 260, 760)])
        arrow(d, [(x + 260, 960), (x + 260, 1020), (885, 1020), (885, 1080)])
    box(
        d,
        (580, 1080, 1190, 1230),
        "assess",
        "Join branch outputs\nEvaluate material unresolved issues",
    )
    box(
        d,
        (1330, 1080, 1830, 1250),
        "human: evidence",
        "Accept partial | Targeted retry\nCancel",
        "#fff0d9",
    )
    arrow(d, [(1190, 1155), (1330, 1155)], "Material issue")
    arrow(
        d,
        [(1830, 1165), (1870, 1165), (1870, 695), (1200, 695), (1200, 615), (1190, 615)],
        "Retry pending branches",
    )
    box(
        d,
        (580, 1330, 1190, 1490),
        "synthesize",
        "Accepted claims + verified target baseline\nCited strategic interpretations",
    )
    arrow(d, [(885, 1230), (885, 1330)], "Complete / ordinary gaps")
    arrow(d, [(1580, 1250), (1580, 1400), (1190, 1400)], "Accept partial")
    box(d, (580, 1550, 1190, 1640), "END", "Complete / partial briefing")
    arrow(d, [(885, 1490), (885, 1550)])
    d.text(
        (70, 1670),
        "Human boxes depict two review contexts of the same parent human node. Cancel routes to END.",
        font=ImageFont.truetype(FONT, 25),
        fill=INK,
    )
    im.save(OUT / "langgraph_workflow.png")


def inventory():
    graph = ast.parse((ROOT / "market_research/graph.py").read_text())
    provider = ast.parse((ROOT / "market_research/providers.py").read_text())
    system = next(
        ast.literal_eval(n.value)
        for n in provider.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "SYSTEM" for t in n.targets)
    )

    def literal(n):
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
            return literal(n.left) + literal(n.right)
        if isinstance(n, ast.Attribute) and n.attr == "pricing_rules":
            return get_profile("saas").pricing_rules
        raise ValueError(ast.dump(n))

    prompts = []
    for node in ast.walk(graph):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "generate"
        ):
            prompts.append(
                {
                    "agent": literal(node.args[1]),
                    "schema": node.args[2].id,
                    "line": node.lineno,
                    "prompt": literal(node.args[3]),
                }
            )
    store = Store(ROOT / "artifacts")
    runs = []
    for item in store.list_runs():
        row = store.get(item["id"])
        events = store.events(item["id"])
        runs.append(
            {
                "company": row["request"]["company"],
                "demo": row["request"]["demo"],
                "id": row["id"],
                "status": row["status"],
                "queries": row["queries"],
                "calls": row["model_calls"],
                "max_queries": row["request"]["max_queries"],
                "max_calls": row["request"]["max_model_calls"],
                "events": len(events),
                "historical_errors": [
                    e["details"].get("message") for e in events if e["action"] == "error"
                ],
                "human_decisions": len([e for e in events if e["action"] == "decision"]),
            }
        )
    (OUT / "document_inventory.json").write_text(
        json.dumps({"system": system, "prompts": prompts, "runs": runs}, indent=2)
    )


if __name__ == "__main__":
    diagrams()
    inventory()
    print("Generated architecture, LangGraph diagram, and prompt/run inventory.")

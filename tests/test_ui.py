import time
from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[1] / "app.py"


def test_streamlit_demo_reaches_analysis_and_trace(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(str(APP), default_timeout=20).run()
    assert not app.exception
    assert app.title[0].value == "Market Lens"
    start = next(b for b in app.button if b.label == "Start research")
    start.click().run()
    for _ in range(15):
        if app.tabs:
            break
        time.sleep(0.2)
        app.run()
    assert not app.exception
    assert [t.label for t in app.tabs] == [
        "Briefing",
        "Comparison",
        "Competitor profiles",
        "Recent news",
        "Sources",
        "Downloads",
    ]
    assert any(e.label == "Agent collaboration trace" for e in app.expander)
    assert any("FICTIONAL DEMO" in i.value for i in app.info)
    assert any("Complete" in h.value for h in app.subheader)


def test_streamlit_conflict_review_resumes_to_partial(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(str(APP), default_timeout=20).run()
    next(s for s in app.selectbox if s.label == "Demo scenario").select("conflict")
    next(b for b in app.button if b.label == "Start research").click().run()
    for _ in range(15):
        if any(s.label == "Decision" for s in app.selectbox):
            break
        time.sleep(0.2)
        app.run()
    assert not app.exception
    next(s for s in app.selectbox if s.label == "Decision").select("accept_partial")
    next(b for b in app.button if b.label == "Apply decision and resume").click().run()
    for _ in range(15):
        if app.tabs:
            break
        time.sleep(0.2)
        app.run()
    assert not app.exception
    assert app.tabs
    assert any("Partial" in h.value for h in app.subheader)

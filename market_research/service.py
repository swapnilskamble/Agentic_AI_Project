import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .demo import DemoLLM, DemoSearch
from .graph import build_graph
from .models import ResearchRequest
from .providers import ServiceError, StructuredLLM, YouSearch
from .storage import Cancelled, Store


class ResearchService:
    """One worker per run; Streamlit polls persisted events without blocking the UI."""

    def __init__(self, directory=None):
        load_dotenv()
        self.store = Store(directory or os.getenv("RESEARCH_DATA_DIR", "artifacts"))
        self.connection = sqlite3.connect(
            self.store.directory / "checkpoints.sqlite", check_same_thread=False
        )
        self.checkpointer = SqliteSaver(self.connection)
        self.graphs = {
            True: build_graph(
                self.store, DemoSearch(self.store), DemoLLM(self.store), self.checkpointer
            ),
            False: build_graph(
                self.store, YouSearch(self.store), StructuredLLM(self.store), self.checkpointer
            ),
        }
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="research")
        self.futures = {}
        self.lock = threading.RLock()

    @staticmethod
    def config(run_id):
        return {"configurable": {"thread_id": run_id}, "recursion_limit": 100, "max_concurrency": 3}

    def validate_live(self, request):
        if not request.demo:
            if not os.getenv("YOU_API_KEY") or not os.getenv("OPENAI_API_KEY"):
                raise ServiceError("Live mode requires YOU_API_KEY and OPENAI_API_KEY in .env.")

    def create(self, request: ResearchRequest, background=True):
        self.validate_live(request)
        run_id = self.store.create(request)
        payload = {
            "run_id": run_id,
            "request": request.model_dump(),
            "profiles": {},
            "human_decisions": [],
            "clarification": "",
        }
        self._submit(run_id, payload, background)
        return run_id

    def _submit(self, run_id, payload, background):
        with self.lock:
            future = self.futures.get(run_id)
            if future is not None and not future.done():
                raise ValueError("This run is already active")
            self.store.update(run_id, status="running", error=None)
            if background:
                self.futures[run_id] = self.pool.submit(self._execute, run_id, payload)
            else:
                self._execute(run_id, payload)

    def _execute(self, run_id, payload):
        row = self.store.get(run_id)
        graph = self.graphs[row["request"]["demo"]]
        config = self.config(run_id)
        try:
            for namespace, update in graph.stream(
                payload, config, stream_mode="updates", subgraphs=True
            ):
                for node, value in update.items():
                    if node == "__interrupt__":
                        self.store.event(
                            run_id,
                            "Orchestrator",
                            "human_escalation",
                            review=[i.value for i in value],
                        )
                    else:
                        self.store.event(run_id, node, "node_complete", namespace=list(namespace))
            snapshot = graph.get_state(config)
            if self.store.get(run_id)["cancelled"]:
                raise Cancelled("Research cancelled")
            interrupts = [i.value for task in snapshot.tasks for i in task.interrupts]
            values = snapshot.values
            if interrupts:
                self.store.update(
                    run_id, status="awaiting_review", result={"review": interrupts, "state": values}
                )
            else:
                self.store.update(
                    run_id,
                    status=values.get("status", "complete"),
                    result=values.get("briefing", {"state": values}),
                )
        except Cancelled:
            self.store.update(run_id, status="cancelled")
            self.store.event(run_id, "Orchestrator", "cancelled")
        except Exception as exc:
            message = (
                str(exc)
                if isinstance(exc, (ServiceError, ValueError))
                else (
                    f"Research stopped ({type(exc).__name__}). Resume the saved run after resolving the issue."
                )
            )
            self.store.update(run_id, status="error", error=message)
            self.store.event(
                run_id,
                "Orchestrator",
                "error",
                error_type=type(exc).__name__,
                message=message,
                diagnostics=getattr(exc, "details", {}),
            )

    def resume(self, run_id, decision=None, background=True):
        row = self.store.get(run_id)
        if row["status"] not in {"awaiting_review", "error", "running", "ready"}:
            raise ValueError("This run has already finished; start a new run")
        self.validate_live(ResearchRequest.model_validate(row["request"]))
        with self.lock:
            future = self.futures.get(run_id)
            if future is not None and not future.done():
                raise ValueError("This run is already active")
            if row["status"] == "awaiting_review" and not decision:
                raise ValueError("A human review decision is required")
            # Human waiting does not consume the next active execution window.
            self.store.update(
                run_id, deadline=time.time() + row["request"]["max_seconds"], cancelled=0
            )
            payload = Command(resume=decision) if decision else None
            self._submit(run_id, payload, background)

    def cancel(self, run_id):
        self.store.update(run_id, cancelled=1)
        self.store.event(run_id, "Human reviewer", "cancel_requested")
        row = self.store.get(run_id)
        if row["status"] in {"awaiting_review", "error", "ready"}:
            self.store.update(run_id, status="cancelled")

    def active(self, run_id):
        future = self.futures.get(run_id)
        return future is not None and not future.done()

    def close(self):
        self.pool.shutdown(wait=True)
        self.connection.close()

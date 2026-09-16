import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .models import ResearchRequest, utcnow


class BudgetExceeded(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


class Store:
    """Persistent run metadata, atomic budgets, query/model cache and collaboration events."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "research.sqlite"
        self._lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, created TEXT, request TEXT, status TEXT,
                    queries INTEGER DEFAULT 0, model_calls INTEGER DEFAULT 0,
                    tokens INTEGER DEFAULT 0, deadline REAL, cancelled INTEGER DEFAULT 0,
                    result TEXT, error TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, time TEXT,
                    agent TEXT, action TEXT, subject TEXT, details TEXT
                );
                CREATE TABLE IF NOT EXISTS cache (
                    run_id TEXT, key TEXT, value TEXT, PRIMARY KEY(run_id, key)
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, request: ResearchRequest) -> str:
        import time

        run_id = uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT INTO runs(id,created,request,status,deadline) VALUES(?,?,?,?,?)",
                (
                    run_id,
                    utcnow(),
                    request.model_dump_json(),
                    "ready",
                    time.time() + request.max_seconds,
                ),
            )
        return run_id

    def get(self, run_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        result = dict(row)
        result["request"] = json.loads(result["request"])
        result["result"] = json.loads(result["result"]) if result["result"] else None
        return result

    def list_runs(self) -> list[dict]:
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,created,status,request FROM runs ORDER BY created DESC LIMIT 30"
                )
            ]

    def update(self, run_id: str, **fields):
        allowed = {"status", "result", "error", "deadline", "cancelled"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("Invalid run update")
        if "result" in fields:
            fields["result"] = json.dumps(fields["result"], default=str)
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?",
                (*fields.values(), run_id),
            )

    def check(self, run_id: str):
        import time

        row = self.get(run_id)
        if row["cancelled"]:
            raise Cancelled("Research cancelled")
        if time.time() >= row["deadline"]:
            raise BudgetExceeded("Run time limit reached")

    def reserve(self, run_id: str, kind: str):
        """Count every attempted external call, including retries, across parallel branches."""
        import time

        column = {"search": "queries", "model": "model_calls"}[kind]
        limit_key = {"search": "max_queries", "model": "max_model_calls"}[kind]
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            request = json.loads(row["request"])
            if row["cancelled"]:
                raise Cancelled("Research cancelled")
            if time.time() >= row["deadline"]:
                raise BudgetExceeded("Run time limit reached")
            if row[column] >= request[limit_key]:
                raise BudgetExceeded(f"{kind.title()} call budget exhausted")
            db.execute(f"UPDATE runs SET {column}={column}+1 WHERE id=?", (run_id,))

    def add_tokens(self, run_id: str, tokens: int):
        with self.connect() as db:
            db.execute("UPDATE runs SET tokens=tokens+? WHERE id=?", (tokens, run_id))

    def event(self, run_id: str, agent: str, action: str, subject: str = "", **details):
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(run_id,time,agent,action,subject,details) VALUES(?,?,?,?,?,?)",
                (run_id, utcnow(), agent, action, subject, json.dumps(details, default=str)),
            )

    def events(self, run_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,))
            return [{**dict(r), "details": json.loads(r["details"])} for r in rows]

    @staticmethod
    def cache_key(kind: str, payload: dict) -> str:
        return kind + ":" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def cached(self, run_id: str, key: str):
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM cache WHERE run_id=? AND key=?", (run_id, key)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def put_cache(self, run_id: str, key: str, value):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO cache VALUES(?,?,?)", (run_id, key, json.dumps(value))
            )

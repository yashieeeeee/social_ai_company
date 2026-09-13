"""Message bus + shared state. Agents never call each other directly."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List


class MessageBus:
    """Append-only bus. Orchestrator routes; trace.jsonl is interview evidence."""

    def __init__(self, db_path: str | Path = "data/prodigal.db",
                 trace_path: str | Path = "logs/trace.jsonl") -> None:
        self.db_path = Path(db_path)
        self.trace_path = Path(trace_path)
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS messages("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, from_agent TEXT, to_agent TEXT,"
            " mtype TEXT, payload TEXT, ts REAL)")
        conn.commit()
        # :memory: creates a fresh DB per connection, so hold one open handle.
        self._conn = conn if str(self.db_path) == ":memory:" else None
        self._mem: List[Dict[str, Any]] = []

    def _db(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        return sqlite3.connect(str(self.db_path))

    def send(self, from_agent: str, to_agent: str, mtype: str, payload: Dict[str, Any]) -> None:
        msg = {"from_agent": from_agent, "to_agent": to_agent, "mtype": mtype,
               "payload": payload, "ts": time.time()}
        self._mem.append(msg)
        db = self._db()
        db.execute("INSERT INTO messages(from_agent,to_agent,mtype,payload,ts) VALUES(?,?,?,?,?)",
                     (from_agent, to_agent, mtype, json.dumps(payload, default=str), msg["ts"]))
        db.commit()
        if self._conn is None:
            db.close()
        try:  # trace is a mirror: a full disk must not kill the campaign
            with open(self.trace_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, default=str) + "\n")
        except OSError:
            pass

    def inbox(self, agent: str) -> List[Dict[str, Any]]:
        return [m for m in self._mem if m["to_agent"] in (agent, "*")]

    def all(self) -> List[Dict[str, Any]]:
        return list(self._mem)

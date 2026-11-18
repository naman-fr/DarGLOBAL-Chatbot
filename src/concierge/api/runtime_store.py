"""Runtime SQLite for leads, the CRM outbox and feedback. Separate from the read-only KB snapshot."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta

import httpx

from concierge import config
from concierge.observability import log

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (id TEXT PRIMARY KEY, created_at TEXT, lang TEXT, brand TEXT, name TEXT,
    contact TEXT, role TEXT, budget TEXT, timeline TEXT, country TEXT, interest TEXT, consent INTEGER,
    summary TEXT);
CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, lead_id TEXT, created_at TEXT, status TEXT,
    attempts INTEGER, last_error TEXT);
CREATE TABLE IF NOT EXISTS feedback (id TEXT PRIMARY KEY, created_at TEXT, rating INTEGER, reason TEXT,
    question TEXT, model TEXT, kb_version TEXT);
CREATE TABLE IF NOT EXISTS unanswered (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, question TEXT,
    lang TEXT);
"""

EMAIL = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]{2,}$")
PHONE = re.compile(r"^\+?[\d\s\-()]{8,20}$")


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class RuntimeStore:
    def __init__(self) -> None:
        self.cfg = config.load("app")
        d = config.path(self.cfg["runtime_dir"])
        d.mkdir(parents=True, exist_ok=True)
        self.path = d / "runtime.sqlite"
        self._lock = threading.Lock()
        with self._con() as con:
            con.executescript(SCHEMA)
        self.purge()

    def _con(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, check_same_thread=False)
        con.row_factory = sqlite3.Row
        return con

    def purge(self) -> None:
        """Retention policy from config: leads and feedback older than N days are deleted."""
        r = self.cfg["retention_days"]
        with self._lock, self._con() as con:
            cutoff = (datetime.now(UTC) - timedelta(days=r["leads"])).isoformat()
            ids = [x[0] for x in con.execute("SELECT id FROM leads WHERE created_at < ?", (cutoff,))]
            con.executemany("DELETE FROM outbox WHERE lead_id = ?", [(i,) for i in ids])
            con.execute("DELETE FROM leads WHERE created_at < ?", (cutoff,))
            cutoff = (datetime.now(UTC) - timedelta(days=r["feedback"])).isoformat()
            con.execute("DELETE FROM feedback WHERE created_at < ?", (cutoff,))
            con.execute("DELETE FROM unanswered WHERE created_at < ?", (cutoff,))

    # ---- leads ---------------------------------------------------------------------------
    @staticmethod
    def validate_lead(d: dict) -> str | None:
        if not d.get("consent"):
            return "consent"
        name = (d.get("name") or "").strip()
        contact = (d.get("contact") or "").strip()
        if not (2 <= len(name) <= 80):
            return "name"
        if not (EMAIL.match(contact) or PHONE.match(contact)):
            return "contact"
        return None

    def add_lead(self, d: dict) -> str:
        lead_id = uuid.uuid4().hex
        fields = {
            k: str(d.get(k) or "")[:200]
            for k in (
                "lang",
                "brand",
                "name",
                "contact",
                "role",
                "budget",
                "timeline",
                "country",
                "interest",
                "summary",
            )
        }
        with self._lock, self._con() as con:
            con.execute(
                "INSERT INTO leads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    lead_id,
                    now(),
                    fields["lang"],
                    fields["brand"],
                    fields["name"],
                    fields["contact"],
                    fields["role"],
                    fields["budget"],
                    fields["timeline"],
                    fields["country"],
                    fields["interest"],
                    1,
                    fields["summary"][:1000],
                ),
            )
            con.execute("INSERT INTO outbox VALUES (?,?,?,?,?,?)", (uuid.uuid4().hex, lead_id, now(), "pending", 0, ""))
        return lead_id

    def delete_lead(self, contact: str) -> int:
        """Data-subject deletion by contact detail (documented in docs/runbook.md)."""
        with self._lock, self._con() as con:
            ids = [x[0] for x in con.execute("SELECT id FROM leads WHERE contact = ?", (contact,))]
            con.executemany("DELETE FROM outbox WHERE lead_id = ?", [(i,) for i in ids])
            con.execute("DELETE FROM leads WHERE contact = ?", (contact,))
        return len(ids)

    async def flush_outbox(self, adapter: CRMAdapter) -> int:
        with self._con() as con:
            rows = con.execute(
                "SELECT o.id AS oid, o.attempts, l.* FROM outbox o JOIN leads l ON l.id = o.lead_id "
                "WHERE o.status = 'pending' AND o.attempts < 5"
            ).fetchall()
        sent = 0
        for row in rows:
            lead = {k: row[k] for k in row.keys() if k not in ("oid", "attempts")}
            ok, err = await adapter.send(lead)
            with self._lock, self._con() as con:
                con.execute(
                    "UPDATE outbox SET status = ?, attempts = attempts + 1, last_error = ? WHERE id = ?",
                    ("sent" if ok else "pending", err or "", row["oid"]),
                )
            sent += ok
        return sent

    # ---- feedback and gaps -------------------------------------------------------------------
    def add_feedback(self, d: dict) -> None:
        with self._lock, self._con() as con:
            con.execute(
                "INSERT INTO feedback VALUES (?,?,?,?,?,?,?)",
                (
                    uuid.uuid4().hex,
                    now(),
                    1 if d.get("rating", 0) > 0 else -1,
                    str(d.get("reason") or "")[:300],
                    str(d.get("question") or "")[:500],
                    str(d.get("model") or "")[:100],
                    str(d.get("kb_version") or "")[:40],
                ),
            )

    def add_unanswered(self, question: str, lang: str) -> None:
        with self._lock, self._con() as con:
            con.execute(
                "INSERT INTO unanswered (created_at, question, lang) VALUES (?,?,?)", (now(), question[:300], lang)
            )

    def summary(self) -> dict:
        with self._con() as con:
            return {
                "leads": con.execute("SELECT COUNT(*) FROM leads").fetchone()[0],
                "outbox_pending": con.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0],
                "feedback_up": con.execute("SELECT COUNT(*) FROM feedback WHERE rating > 0").fetchone()[0],
                "feedback_down": con.execute("SELECT COUNT(*) FROM feedback WHERE rating < 0").fetchone()[0],
                "top_unanswered": [
                    dict(r)
                    for r in con.execute(
                        "SELECT question, COUNT(*) AS n FROM unanswered GROUP BY LOWER(question) ORDER BY n DESC LIMIT 15"
                    )
                ],
            }


class CRMAdapter:
    """Posts leads to a webhook if one is configured; otherwise a stub that only logs (no PII)."""

    def __init__(self) -> None:
        cfg = config.load("app")["crm"]
        self.url = config.env(cfg["webhook_env"])
        self.timeout = cfg["timeout_seconds"]

    @staticmethod
    def payload(lead: dict) -> dict:
        return {
            "source": "concierge-demo",
            "lead_id": lead["id"],
            "created_at": lead["created_at"],
            "brand": lead["brand"],
            "contact": {"name": lead["name"], "value": lead["contact"]},
            "qualification": {k: lead[k] for k in ("role", "budget", "timeline", "country", "interest")},
            "language": lead["lang"],
            "conversation_summary": lead.get("summary", ""),
        }

    async def send(self, lead: dict) -> tuple[bool, str | None]:
        body = self.payload(lead)
        if not self.url:
            log.info("crm.stub_delivery", lead_id=lead["id"], brand=lead["brand"])
            return True, None
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(self.url, content=json.dumps(body), headers={"Content-Type": "application/json"})
            return r.status_code < 300, None if r.status_code < 300 else f"HTTP {r.status_code}"
        except httpx.HTTPError as exc:
            return False, str(exc)[:200]

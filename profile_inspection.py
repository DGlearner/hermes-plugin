"""Durable, Profile-scoped inspection of tasks and review queues.

The inspection decision is deliberately native code: a cron tick can query MCP
and finish without creating an LLM turn when nothing changed.  Hermes-specific
model and channel adapters are injected by the runtime integration.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, time as clock_time
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_MINUTES = 30
ALLOWED_INTERVALS = frozenset({2, 5, 30, 60})
DEFAULT_TIMEZONE = "Asia/Shanghai"


def query_profile_mcp(profile_home: Path, config: "InspectionConfig") -> dict[str, list[dict[str, Any]]]:
    """Query the two read-only MCP queues inside the active Profile scope."""
    from . import DEFAULT_ENDPOINT, ENDPOINT_ENV, _call_mcp, _validate_endpoint
    from .work_todos import profile_scope
    import os

    endpoint = _validate_endpoint(os.environ.get(ENDPOINT_ENV) or DEFAULT_ENDPOINT)
    result: dict[str, list[dict[str, Any]]] = {"tasks": [], "reviews": []}
    with profile_scope(profile_home):
        if config.tasks:
            payload = json.loads(_call_mcp(endpoint, 30, "list_my_tasks", {"limit": 100, "offset": 0}))
            result["tasks"] = list(payload.get("tasks") or [])
        if config.reviews:
            payload = json.loads(_call_mcp(endpoint, 30, "list_pending_knowledge_reviews", {"limit": 100}))
            result["reviews"] = list(payload.get("reviews") or [])
    return result


class InspectionConfig:
    def __init__(self, *, enabled: bool = True, timezone: str = DEFAULT_TIMEZONE,
                 work_start: str = "09:00", work_end: str = "18:00",
                 interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
                 quiet_start: str | None = None, quiet_end: str | None = None,
                 tasks: bool = True, reviews: bool = True, max_items: int = 10):
        if interval_minutes not in ALLOWED_INTERVALS:
            raise ValueError("interval_minutes must be 2, 5, 30 or 60")
        self.enabled = bool(enabled)
        self.timezone = ZoneInfo(timezone)
        self.work_start = _parse_clock(work_start)
        self.work_end = _parse_clock(work_end)
        self.interval_minutes = interval_minutes
        self.quiet_start = _parse_clock(quiet_start) if quiet_start else None
        self.quiet_end = _parse_clock(quiet_end) if quiet_end else None
        self.tasks = bool(tasks)
        self.reviews = bool(reviews)
        self.max_items = max(1, min(int(max_items), 100))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "InspectionConfig":
        value = value or {}
        work = value.get("work_hours") or {}
        quiet = value.get("quiet_hours") or {}
        sources = value.get("sources") or {}
        notifications = value.get("notifications") or {}
        return cls(
            enabled=value.get("enabled", True), timezone=value.get("timezone", DEFAULT_TIMEZONE),
            work_start=work.get("start", "09:00"), work_end=work.get("end", "18:00"),
            interval_minutes=int(value.get("interval_minutes", DEFAULT_INTERVAL_MINUTES)),
            quiet_start=quiet.get("start") if quiet.get("enabled", bool(quiet)) else None,
            quiet_end=quiet.get("end") if quiet.get("enabled", bool(quiet)) else None,
            tasks=sources.get("tasks", True), reviews=sources.get("knowledge_reviews", True),
            max_items=notifications.get("max_items", 10),
        )

    def localize(self, now: float | datetime) -> datetime:
        if isinstance(now, datetime):
            return now.astimezone(self.timezone)
        return datetime.fromtimestamp(now, self.timezone)

    def in_work_hours(self, now: float | datetime) -> bool:
        value = self.localize(now).time()
        return self.work_start <= value < self.work_end if self.work_start < self.work_end else value >= self.work_start or value < self.work_end

    def in_quiet_hours(self, now: float | datetime) -> bool:
        if self.quiet_start is None or self.quiet_end is None:
            return False
        value = self.localize(now).time()
        return self.quiet_start <= value < self.quiet_end if self.quiet_start < self.quiet_end else value >= self.quiet_start or value < self.quiet_end

    def due(self, last_checked_at: float | None, now: float | datetime) -> bool:
        if not self.enabled or not self.in_work_hours(now):
            return False
        if last_checked_at is None:
            return True
        current = now.timestamp() if isinstance(now, datetime) else float(now)
        return current - last_checked_at >= self.interval_minutes * 60


def _parse_clock(value: str) -> clock_time:
    try:
        hour, minute = (int(part) for part in str(value).split(":", 1))
        return clock_time(hour, minute)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid clock time: {value!r}") from exc


def item_fingerprint(kind: str, item: Mapping[str, Any]) -> str:
    """Hash only stable, non-secret state fields returned by MCP."""
    identity = item.get("task_no") if kind == "task" else item.get("document_id") or item.get("id")
    state = {key: item.get(key) for key in ("status", "version", "updated_at", "reviewed_at") if key in item}
    return hashlib.sha256(json.dumps([kind, identity, state], sort_keys=True, default=str).encode()).hexdigest()


class InspectionState:
    def __init__(self, profile_home: Path):
        self.profile_home = profile_home.resolve()
        self.path = self.profile_home / ".hermes-profile-inspection.sqlite3"
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS items (
                    kind TEXT NOT NULL, item_id TEXT NOT NULL,
                    observed_fingerprint TEXT NOT NULL, notified_fingerprint TEXT,
                    last_seen_at REAL NOT NULL, pending INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL,
                    PRIMARY KEY(kind, item_id)
                );
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def last_checked_at(self) -> float | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key='last_checked_at'").fetchone()
        return float(row[0]) if row else None

    def has_due_pending(self, now: float) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1 FROM items WHERE pending=1 AND (next_attempt IS NULL OR next_attempt<=?) LIMIT 1", (now,)).fetchone() is not None

    def mark_checked(self, value: float) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO meta VALUES ('last_checked_at', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(value),))


class InspectionRunner:
    def __init__(self, query: Callable[[Path, InspectionConfig], Mapping[str, list[Mapping[str, Any]]]],
                 model: Callable[[Path, list[dict[str, Any]]], str],
                 send: Callable[[Path, str], None], clock: Callable[[], float] = time.time):
        self.query, self.model, self.send, self.clock = query, model, send, clock

    def run(self, profile_home: Path, config: InspectionConfig | None = None, *, force: bool = False) -> str:
        now = self.clock()
        config = config or InspectionConfig()
        state = InspectionState(profile_home)
        if not force and not state.has_due_pending(now) and not config.due(state.last_checked_at(), now):
            return "not_due"
        if not config.in_work_hours(now) and not force:
            return "outside_work_hours"
        result = self.query(profile_home, config)
        items: list[dict[str, Any]] = []
        if config.tasks:
            items.extend({"kind": "task", "item": dict(item)} for item in result.get("tasks", []))
        if config.reviews:
            items.extend({"kind": "review", "item": dict(item)} for item in result.get("reviews", []))
        candidates: list[dict[str, Any]] = []
        with state.connect() as db:
            for entry in items:
                item = entry["item"]
                item_id = str(item.get("task_no") if entry["kind"] == "task" else item.get("document_id") or item.get("id") or "")
                if not item_id:
                    continue
                fingerprint = item_fingerprint(entry["kind"], item)
                row = db.execute("SELECT * FROM items WHERE kind=? AND item_id=?", (entry["kind"], item_id)).fetchone()
                changed = row is None or row["notified_fingerprint"] != fingerprint
                db.execute("INSERT INTO items(kind,item_id,observed_fingerprint,last_seen_at,pending,attempts,next_attempt) VALUES(?,?,?,?,?,?,?) "
                           "ON CONFLICT(kind,item_id) DO UPDATE SET observed_fingerprint=excluded.observed_fingerprint,last_seen_at=excluded.last_seen_at",
                           (entry["kind"], item_id, fingerprint, now, int(changed), row["attempts"] if row else 0, row["next_attempt"] if row else None))
                if changed and (not row or row["next_attempt"] is None or row["next_attempt"] <= now):
                    candidates.append({"kind": entry["kind"], "item_id": item_id, "fingerprint": fingerprint, "item": item})
            db.execute("INSERT INTO meta VALUES ('last_checked_at', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now),))
        if not candidates:
            return "changed_pending" if config.in_quiet_hours(now) else "unchanged"
        if config.in_quiet_hours(now) and not force:
            return "quiet_deferred"
        candidates = candidates[:config.max_items]
        try:
            message = self.model(profile_home, candidates)
            if not message.strip():
                raise RuntimeError("inspection model returned empty notification")
            self.send(profile_home, message)
        except Exception:
            with state.connect() as db:
                for candidate in candidates:
                    row = db.execute("SELECT attempts FROM items WHERE kind=? AND item_id=?", (candidate["kind"], candidate["item_id"])).fetchone()
                    attempts = int(row[0]) + 1
                    db.execute("UPDATE items SET pending=1,attempts=?,next_attempt=? WHERE kind=? AND item_id=?",
                               (attempts, now + min(3600, 30 * (2 ** min(attempts - 1, 6))), candidate["kind"], candidate["item_id"]))
            logger.warning("profile inspection notification failed", exc_info=False)
            return "retry_pending"
        with state.connect() as db:
            for candidate in candidates:
                db.execute("UPDATE items SET notified_fingerprint=?,pending=0,attempts=0,next_attempt=NULL WHERE kind=? AND item_id=?",
                           (candidate["fingerprint"], candidate["kind"], candidate["item_id"]))
        return "sent"


def load_profile_config(profile_home: Path) -> InspectionConfig:
    path = profile_home / "config.yaml"
    if not path.exists():
        return InspectionConfig()
    try:
        import yaml
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.warning("could not load Profile inspection config", exc_info=False)
        return InspectionConfig()
    return InspectionConfig.from_mapping(payload.get("profile_inspection"))


def run_once(profile_home: Path, model: Callable[[Path, list[dict[str, Any]]], str],
             send: Callable[[Path, str], None], *, force: bool = True,
             clock: Callable[[], float] = time.time) -> str:
    """Manual trigger entry point used by the scheduler/admin adapter."""
    config = load_profile_config(profile_home)
    runner = InspectionRunner(query_profile_mcp, model, send, clock)
    return runner.run(profile_home, config, force=force)

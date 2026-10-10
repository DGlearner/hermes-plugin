"""Durable, Profile-local delivery of completed knowledge review results.

Each event keeps its original recipients and exact text pages. A confirmed send
can only advance to an idempotent server acknowledgement; an ambiguous send is
never replayed automatically. This ledger is independent of daily-report state.
"""
from __future__ import annotations

import contextlib
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import UUID
from zoneinfo import ZoneInfo

from .daily_inspection import ItemNotification
from .profile_inspection import InspectionConfig


PLATFORMS = {"telegram", "weixin", "wecom_callback"}
FIELDS = ("notification_id", "document_id", "document_version", "knowledge_id", "title",
          "file_name", "decision", "reason", "reviewer_username", "reviewer_display_name",
          "reviewed_at", "created_at")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("invalid review timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("review timestamp requires timezone")
    return parsed


def _notification(value):
    if not isinstance(value, dict) or any(field not in value for field in FIELDS):
        raise ValueError("incomplete review notification")
    result = {field: value[field] for field in FIELDS}
    for field in ("notification_id", "document_id"):
        if not isinstance(result[field], str):
            raise ValueError("invalid review identity")
        result[field] = str(UUID(result[field]))
    if type(result["document_version"]) is not int or result["document_version"] < 1:
        raise ValueError("invalid review version")
    for field in ("knowledge_id", "title", "file_name", "reason", "reviewer_username"):
        if not isinstance(result[field], str) or not result[field].strip():
            raise ValueError("invalid review field")
    if result["reviewer_display_name"] is not None and not isinstance(result["reviewer_display_name"], str):
        raise ValueError("invalid reviewer name")
    if result["decision"] not in {"approved", "rejected"}:
        raise ValueError("invalid review decision")
    _timestamp(result["reviewed_at"])
    _timestamp(result["created_at"])
    return result


@contextlib.contextmanager
def _mcp(home, config):
    from . import DEFAULT_ENDPOINT, ENDPOINT_ENV, _call_mcp, _validate_endpoint
    from .work_todos import profile_scope

    expected = config.get("expected_username")
    if not isinstance(expected, str) or not expected:
        raise ValueError("missing Profile identity")
    endpoint = _validate_endpoint(os.environ.get(ENDPOINT_ENV) or DEFAULT_ENDPOINT)
    with profile_scope(home):
        def call(name, arguments):
            result = json.loads(_call_mcp(endpoint, 30, name, arguments))
            if not isinstance(result, dict) or result.get("error") or result.get("isError"):
                raise ValueError("review notification MCP failed")
            return result
        if call("get_current_user", {}).get("username") != expected:
            raise ValueError("Profile identity mismatch")
        yield call


def query_review_notifications(home, config):
    """Fetch a complete pending snapshot from cursor None under this Profile."""
    with _mcp(home, config) as call:
        notifications, identities, cursors = [], set(), set()
        cursor = None
        for _ in range(1000):
            page = call("list_my_knowledge_review_notifications", {"limit": 50, "cursor": cursor})
            rows = page.get("notifications")
            if (not isinstance(rows, list) or len(rows) > 50
                    or type(page.get("count")) is not int or page["count"] != len(rows)
                    or type(page.get("has_more")) is not bool or "next_cursor" not in page):
                raise ValueError("invalid review notification page")
            for raw in rows:
                item = _notification(raw)
                if item["notification_id"] in identities:
                    raise ValueError("duplicate review notification")
                identities.add(item["notification_id"])
                notifications.append(item)
            next_cursor = page["next_cursor"]
            if not page["has_more"]:
                if next_cursor is not None:
                    raise ValueError("unexpected final review cursor")
                return notifications
            if not rows or not isinstance(next_cursor, str) or not next_cursor or next_cursor in cursors:
                raise ValueError("incomplete or looping review pagination")
            cursors.add(next_cursor)
            cursor = next_cursor
    raise ValueError("review notification pagination limit")


def acknowledge_review_notification(home, config, notification_id):
    with _mcp(home, config) as call:
        return call("acknowledge_knowledge_review_notification", {"notification_id": notification_id})


def _targets(config):
    raw = config.get("notification_targets")
    if raw is None:
        single = config.get("notification_target")
        raw = [single] if single else []
    if not isinstance(raw, list):
        raise ValueError("invalid review notification targets")
    targets = []
    for target in raw:
        if (not isinstance(target, dict) or target.get("platform") not in PLATFORMS
                or not isinstance(target.get("chat_id"), (str, int)) or not str(target["chat_id"])):
            raise ValueError("invalid review notification target")
        entry = {"platform": target["platform"], "chat_id": str(target["chat_id"])}
        if entry not in targets:
            targets.append(entry)
    if len({target["platform"] for target in targets}) != len(targets):
        raise ValueError("ambiguous review notification targets")
    return sorted(targets, key=lambda target: (target["platform"], target["chat_id"]))


def active_review_bindings(home, config):
    """Validate every configured recipient against current enabled DM routes."""
    from gateway.config import load_gateway_config
    from .work_todos import active_binding, binding_identity, profile_scope

    targets = _targets(config)
    root = home.parent.parent
    with profile_scope(root):
        routes = load_gateway_config().profile_routes
    active = []
    identities = []
    for route in routes:
        platform = getattr(route.platform, "value", route.platform)
        if route.profile != home.name or platform not in PLATFORMS or not getattr(route, "enabled", True):
            continue
        if getattr(route, "chat_type", None) not in (None, "dm"):
            continue
        target = {"platform": platform, "chat_id": str(route.chat_id)}
        if target in active:
            raise ValueError("ambiguous review binding")
        active.append(target)
        identity = {**target, "route": getattr(route, "name", None)}
        if platform == "weixin":
            identity["binding"] = binding_identity(active_binding(root, home.name, target["chat_id"]))
        identities.append(identity)
    if sorted(active, key=lambda target: (target["platform"], target["chat_id"])) != targets or not targets:
        raise ValueError("review notification binding changed")
    return sorted(identities, key=lambda target: (target["platform"], target["chat_id"]))


def render_review_notification(item, targets):
    """Keep every Unicode character, including arbitrarily long review reasons."""
    when = _timestamp(item["reviewed_at"]).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    reviewer = item["reviewer_display_name"] or item["reviewer_username"]
    if reviewer != item["reviewer_username"]:
        reviewer += f"（{item['reviewer_username']}）"
    decision = "审核通过" if item["decision"] == "approved" else "审核驳回"
    body = (f"文件：{item['file_name']}\n标题：{item['title']}\n知识编号：{item['knowledge_id']}"
            f"\n版本：{item['document_version']}\n结果：{decision}\n审核人：{reviewer}"
            f"\n审核时间：{when}（上海时间）\n审核原因：\n{item['reason']}")
    limit = 4000 if any(target["platform"] == "wecom_callback" for target in targets) and all(
        target["platform"] != "weixin" for target in targets) else 1900
    header = f"【文件审核结果 999999/999999】\n通知编号：{item['notification_id']}\n"
    budget = limit - len(header.encode("utf-8"))
    chunks, start, size = [], 0, 0
    for index, character in enumerate(body):
        length = len(character.encode("utf-8"))
        if size + length > budget:
            chunks.append(body[start:index])
            start, size = index, 0
        size += length
    chunks.append(body[start:])
    if len(chunks) > 999999:
        raise ValueError("review notification too large")
    return [f"【文件审核结果 {index}/{len(chunks)}】\n通知编号：{item['notification_id']}\n{chunk}"
            for index, chunk in enumerate(chunks, 1)]


class ReviewResultNotificationRunner:
    def __init__(self, home, *, query=query_review_notifications, ack=acknowledge_review_notification,
                 send=None, clock=time.time, bindings=active_review_bindings):
        self.home = Path(home)
        self.query, self.ack, self.clock, self.bindings = query, ack, clock, bindings
        if send is None:
            from .daily_inspection import send_notification
            send = send_notification
        self.send = send

    def run(self, config):
        directory = self.home / "inspection" / "review-results"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (directory / "run.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"status": "busy"}
            with contextlib.closing(sqlite3.connect(directory / "state.sqlite3", timeout=10)) as db:
                db.row_factory = sqlite3.Row
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS notifications(
                        notification_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL UNIQUE,
                        payload TEXT NOT NULL, targets TEXT NOT NULL, bindings TEXT NOT NULL,
                        parts TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL, sent_at REAL, acked_at REAL, last_error TEXT);
                """)
                (directory / "state.sqlite3").chmod(0o600)
                with db:
                    db.execute("UPDATE notifications SET state='unconfirmed',last_error='interrupted_send' WHERE state='sending'")
                try:
                    result = self._run(db, config)
                except Exception:
                    result = {"status": "failed"}
                result["states"] = {row[0]: row[1] for row in db.execute(
                    "SELECT state,count(*) FROM notifications GROUP BY state")}
                result["recorded_at"] = self.clock()
                temporary = directory / "last_result.tmp"
                temporary.write_text(_json(result), encoding="utf-8")
                temporary.chmod(0o600)
                temporary.replace(directory / "last_result.json")
                return result

    def _run(self, db, config):
        if not config.get("enabled", False) or config.get("include_review_results", True) is False:
            return {"status": "disabled"}
        schedule, now = InspectionConfig.from_mapping(config), self.clock()
        if not schedule.in_work_hours(now):
            return {"status": "outside_work_hours"}
        if schedule.in_quiet_hours(now):
            return {"status": "quiet_deferred"}
        targets = _targets(config)
        if not targets:
            return {"status": "no_binding"}
        try:
            bindings = _json(self.bindings(self.home, config))
        except Exception:
            return {"status": "binding_changed"}
        previous = db.execute("SELECT value FROM meta WHERE key='last_checked_at'").fetchone()
        if not schedule.due(float(previous[0]) if previous else None, now):
            return {"status": "not_due"}
        with db:
            db.execute("INSERT INTO meta VALUES ('last_checked_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now),))
        try:
            raw = self.query(self.home, config)
            if not isinstance(raw, list):
                raise ValueError("invalid review snapshot")
            current = {}
            for value in raw:
                item = _notification(value)
                identity = item["notification_id"]
                if identity in current:
                    raise ValueError("duplicate review notification")
                current[identity] = item
                existing = db.execute("SELECT payload FROM notifications WHERE notification_id=?", (identity,)).fetchone()
                if existing and existing[0] != _json(item):
                    raise ValueError("review notification snapshot changed")
            # Finish validation/rendering before committing or sending any item.
            prepared = [(item, render_review_notification(item, targets)) for item in current.values()
                        if not db.execute("SELECT 1 FROM notifications WHERE notification_id=?", (item["notification_id"],)).fetchone()]
        except Exception:
            return {"status": "query_failed", "checked_at": now}
        with db:
            for item, parts in prepared:
                identity = item["notification_id"]
                db.execute("INSERT INTO notifications(notification_id,batch_id,payload,targets,bindings,parts,state,created_at) VALUES (?,?,?,?,?,?,'pending',?)",
                           (identity, "knowledge-review-" + identity, _json(item), _json(targets), bindings, _json(parts), now))
        counts = {"sent": 0, "acked": 0, "ack_failed": 0, "unconfirmed": 0, "rejected": 0, "binding_changed": 0}
        for row in db.execute("SELECT * FROM notifications WHERE state!='acked' ORDER BY created_at,notification_id").fetchall():
            identity, state = row["notification_id"], row["state"]
            if row["targets"] != _json(targets) or row["bindings"] != bindings:
                counts["binding_changed"] += 1
                continue
            if state == "unconfirmed":
                counts["unconfirmed"] += 1
                continue
            if state in {"pending", "rejected"}:
                if identity not in current:
                    continue  # Never send an old pending record absent from current authorization.
                if not self._still_bound(config, bindings):
                    counts["binding_changed"] += 1
                    continue
                with db:
                    db.execute("UPDATE notifications SET state='sending',attempts=attempts+1,last_error=NULL WHERE notification_id=?", (identity,))
                delivery_config = dict(config, notification_targets=json.loads(row["targets"]))
                delivery_config.pop("notification_target", None)
                try:
                    outcomes = self.send(self.home, delivery_config, ItemNotification(json.loads(row["parts"])),
                                         row["batch_id"], retry_rejected=True)
                except Exception as exc:
                    outcomes = getattr(exc, "outcomes", None)
                expected = {target["platform"] for target in targets}
                complete = isinstance(outcomes, dict) and set(outcomes) == expected
                if complete and all(value == "sent" for value in outcomes.values()):
                    state = "sent"
                elif complete and all(value in {"sent", "rejected"} for value in outcomes.values()):
                    state = "rejected"
                else:
                    state = "unconfirmed"
                with db:
                    db.execute("UPDATE notifications SET state=?,sent_at=?,last_error=? WHERE notification_id=?",
                               (state, now if state == "sent" else None, None if state == "sent" else state, identity))
                counts[state] += 1
            if state != "sent":
                continue
            if not self._still_bound(config, bindings):
                counts["binding_changed"] += 1
                continue
            try:
                response = self.ack(self.home, config, identity)
                if (not isinstance(response, dict) or response.get("notification_id") != identity
                        or type(response.get("already_delivered")) is not bool):
                    raise ValueError("invalid review acknowledgement")
                _timestamp(response.get("delivered_at"))
            except Exception:
                with db:
                    db.execute("UPDATE notifications SET last_error='ack_failed' WHERE notification_id=?", (identity,))
                counts["ack_failed"] += 1
                continue
            with db:
                db.execute("UPDATE notifications SET state='acked',acked_at=?,last_error=NULL WHERE notification_id=?", (now, identity))
            counts["acked"] += 1
        status = "unchanged"
        for key in ("binding_changed", "unconfirmed", "rejected", "ack_failed", "acked", "sent"):
            if counts[key]:
                status = key
                break
        return {"status": status, "checked_at": now, "pending_count": len(current), **counts}

    def _still_bound(self, config, expected):
        try:
            return _json(self.bindings(self.home, config)) == expected
        except Exception:
            return False


def run_review_result_notifications(home, config, *, query=None, ack=None, send=None,
                                    clock=time.time, bindings=None):
    return ReviewResultNotificationRunner(
        home, query=query or query_review_notifications, ack=ack or acknowledge_review_notification,
        send=send, clock=clock, bindings=bindings or active_review_bindings,
    ).run(config)

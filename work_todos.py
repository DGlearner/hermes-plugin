"""Durable, identity-scoped delivery of scheduled employee task summaries."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from functools import wraps
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event, Lock, Thread
from types import ModuleType
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
TIMEZONE = ZoneInfo("Asia/Shanghai")
MAX_ATTEMPTS = 4
RETRY_DELAYS = (30, 120, 300)
DELIVERY_TTL = 4 * 60 * 60
LEASE_SECONDS = 120
PROCESS_STATE = "_hermes_profile_rag_mcp_work_todos"


def daily_window(now: float) -> tuple[str, float]:
    local = datetime.fromtimestamp(now, TIMEZONE)
    tomorrow = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return local.date().isoformat(), tomorrow.timestamp()


class DeliveryError(Exception):
    def __init__(self, category: str, *, ret=None, errcode=None, errmsg=""):
        super().__init__(category)
        self.category = category
        self.details = {"category": category, "ret": ret, "errcode": errcode, "errmsg": errmsg}


def response_error(response: dict, secrets: tuple[str, ...] = ()) -> DeliveryError | None:
    ret, code = response.get("ret"), response.get("errcode")
    message = str(response.get("errmsg") or response.get("msg") or "")
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[redacted]")
    message = re.sub(r"(?i)(bearer\s+\S+|ragmcp_\S+|sk-\S+)", "[redacted]", message)
    message = re.sub(r"(?i)((?:context_token|authorization|api_key|token)\s*[:=]\s*)\S+", r"\1[redacted]", message)
    lower = message.lower()
    # iLink's HTTP-success body can be {}; match Hermes' optional-error-code contract.
    if ret in (None, 0) and code in (None, 0) and lower in {"", "ok", "success"} \
            and not response.get("error") and response.get("success") is not False:
        return None
    if -14 in (ret, code) or (-2 in (ret, code) and lower == "unknown error"):
        category = "context_rejected"
    elif -2 in (ret, code) and any(word in lower for word in ("rate limit", "freq limit", "frequency", "too frequent", "too many", "频率", "限流")):
        category = "rate_limited"
    else:
        # -2 / "prepare failed" is not evidence of a frequency limit or token expiry.
        category = "send_rejected"
    return DeliveryError(category, ret=ret if isinstance(ret, int) else None,
                         errcode=code if isinstance(code, int) else None, errmsg=message[:200])


@contextmanager
def profile_scope(home: Path):
    from agent.secret_scope import _is_global_env, build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    # Cron children inherit the default bot environment but not the parent's
    # in-memory multiplex flag. An absent profile credential would then fall
    # back to that default bot (or PAT). Mask inherited non-global values in
    # this context only; never mutate os.environ or a process-global flag.
    secrets = {name: "" for name in os.environ if not _is_global_env(name)}
    secrets.update(build_profile_secret_scope(home))
    scope = set_secret_scope(secrets)
    home_scope = set_hermes_home_override(str(home))
    try:
        yield
    finally:
        reset_hermes_home_override(home_scope)
        reset_secret_scope(scope)


def employee_home(root: Path, profile: str) -> Path:
    if not re.fullmatch(r"employee-[a-zA-Z0-9_-]+", profile):
        raise DeliveryError("binding_changed")
    profiles = (root / "profiles").resolve()
    home = (profiles / profile).resolve()
    if home.parent != profiles or not home.is_dir():
        raise DeliveryError("binding_changed")
    return home


def active_binding(root: Path, profile: str, peer: str) -> dict:
    employee_home(root, profile)
    payload = json.loads((root / "provisioner/wechat_bindings.json").read_text())
    matches = [binding for binding in payload.get("bindings", {}).values()
               if binding.get("profile_name") == profile and binding.get("external_user_id") == peer
               and binding.get("status") == "active"]
    if len(matches) != 1:
        raise DeliveryError("binding_changed")
    return matches[0]


def binding_identity(binding: dict) -> str:
    return json.dumps([binding["binding_id"], binding["employee_id"], binding.get("bound_at")], separators=(",", ":"))


def query_report(root: Path, profile: str, username: str) -> str:
    from . import DEFAULT_ENDPOINT, ENDPOINT_ENV, _call_mcp, _validate_endpoint

    started = time.monotonic()
    endpoint = _validate_endpoint(os.environ.get(ENDPOINT_ENV) or DEFAULT_ENDPOINT)
    with profile_scope(employee_home(root, profile)):
        def call(name: str, arguments: dict) -> dict:
            remaining = 45 - (time.monotonic() - started)
            if remaining < 1:
                raise DeliveryError("query_failed")
            result = json.loads(_call_mcp(endpoint, min(15, remaining), name, arguments))
            if not isinstance(result, dict) or result.get("error"):
                raise DeliveryError("query_failed")
            return result

        user = call("get_current_user", {})
        if user.get("username") != username:
            raise DeliveryError("identity_mismatch")
        tasks, offset = {}, 0
        for _ in range(100):
            result = call("list_my_tasks", {"limit": 100, "offset": offset})
            page, total = result.get("tasks"), result.get("total")
            if not isinstance(page, list) or not isinstance(total, int) or total < 0 or (not page and offset < total):
                raise DeliveryError("query_failed")
            for task in page:
                if not isinstance(task, dict) or not task.get("task_no"):
                    raise DeliveryError("query_failed")
                tasks[task["task_no"]] = task
            offset += len(page)
            if offset >= total:
                break
        else:
            raise DeliveryError("query_failed")

    now = datetime.now(TIMEZONE)
    def due(task):
        if not task.get("due_at"):
            return None
        value = datetime.fromisoformat(task["due_at"].replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise DeliveryError("query_failed")
        return value.astimezone(TIMEZONE)

    outstanding = [task for task in tasks.values() if task.get("status") in {"open", "in_progress"}]
    reviewing = sum(task.get("status") == "pending_review" for task in tasks.values())
    outstanding.sort(key=lambda task: (due(task) or datetime.max.replace(tzinfo=TIMEZONE), task["task_no"]))
    overdue = sum(bool(due(task) and due(task) < now) for task in outstanding)
    lines = [f"任务待办｜{now:%Y-%m-%d %H:%M}",
             f"员工：{user.get('display_name') or username}（{username}）",
             f"未完成：{len(outstanding)} 项；已逾期：{overdue} 项；待审核：{reviewing} 项"]
    if not outstanding:
        lines.append("当前暂无待开始或进行中的任务。")
    for index, task in enumerate(outstanding, 1):
        deadline = due(task)
        deadline_text = deadline.strftime("%m-%d %H:%M") if deadline else "未设置截止时间"
        if deadline and deadline < now:
            deadline_text += "（已逾期）"
        state = "待开始" if task["status"] == "open" else "进行中"
        title = " ".join(str(task.get("title") or task["task_no"]).split())[:120]
        item = f"\n{index}. {title}\n{task['task_no']}｜{state}｜{deadline_text}"
        if len("\n".join(lines)) + len(item) > 1750:
            lines.append(f"\n另有 {len(outstanding) - index + 1} 项未展开。")
            break
        lines.append(item)
    logger.info("work-todos query_completed profile=%s total=%d outstanding=%d duration=%.3fs",
                profile, len(tasks), len(outstanding), time.monotonic() - started)
    return "\n".join(lines)


def transport(root: Path, profile: str, peer: str) -> tuple[Path, object]:
    from gateway.config import Platform, load_gateway_config

    home = employee_home(root, profile)
    with profile_scope(home):
        own = load_gateway_config().platforms.get(Platform.WEIXIN)
    if own and own.enabled and own.token and own.extra.get("account_id"):
        return home, own
    with profile_scope(root):
        config = load_gateway_config()
    routes = [route for route in config.profile_routes
              if route.profile == profile and route.chat_id == peer and route.platform == Platform.WEIXIN.value]
    shared = config.platforms.get(Platform.WEIXIN)
    if len(routes) != 1 or not shared or not shared.enabled or not shared.token or not shared.extra.get("account_id"):
        raise DeliveryError("binding_changed")
    return root, shared


def context_revision(home: Path, account: str, peer: str, context: str | None) -> str:
    """Compare durable context changes without storing the bearer context itself."""
    return hashlib.sha256(json.dumps([str(home.resolve()), account, peer, context]).encode()).hexdigest()


def read_context(home: Path, account: str, peer: str) -> str | None:
    from gateway.platforms.weixin import ContextTokenStore

    store = ContextTokenStore(str(home))
    store.restore(account)
    return store.get(account, peer)


def send_report(root: Path, reminder: dict, report: str) -> None:
    from gateway.platforms import weixin

    home, platform = transport(root, reminder["profile"], reminder["peer"])
    account = str(platform.extra["account_id"])
    context = read_context(home, account, reminder["peer"])
    base_url = str(platform.extra.get("base_url") or weixin.ILINK_BASE_URL).rstrip("/")
    queue = ReminderQueue(root)

    def record_attempt(mode, sent_context, error=None, response=None):
        # Diagnostic metadata only: never persist message text or either credential.
        with queue.connect() as db:
            inbound = db.execute("SELECT updated_at FROM interactions WHERE profile=? AND peer=?",
                                 (reminder["profile"], reminder["peer"])).fetchone()
            data = {"mode": mode, "context_present": bool(sent_context), "text_chars": len(report),
                    "last_observed_inbound_age_seconds": round(max(0, time.time() - inbound[0]), 1) if inbound else None,
                    "api_accepted": error is None, "error": error.details if error else None,
                    "message_id_present": bool((response or {}).get("message_id"))}
            db.execute("INSERT INTO delivery_attempts (reminder_id,created_at,diagnostics) VALUES (?,?,?)",
                       (reminder["id"], time.time(), json.dumps(data, ensure_ascii=False)))
        logger.info("work-todos delivery_attempt profile=%s reminder=%s details=%s",
                    reminder["profile"], reminder["id"], json.dumps(data, ensure_ascii=False))

    async def send():
        import aiohttp
        async with aiohttp.ClientSession(trust_env=True, connector=weixin._make_ssl_connector()) as session:
            sent_context, mode = context, "stored_context" if context else "without_context"
            observed_context = context
            for index in range(2):
                # Check identity and channel again for the one permitted recovery request.
                current = active_binding(root, reminder["profile"], reminder["peer"])
                fresh_home, fresh_platform = transport(root, reminder["profile"], reminder["peer"])
                if (binding_identity(current) != reminder["binding_identity"] or fresh_home != home
                        or fresh_platform.token != platform.token or fresh_platform.extra != platform.extra):
                    raise DeliveryError("binding_changed")
                try:
                    response = await asyncio.wait_for(weixin._send_message(
                        session, base_url=base_url, token=platform.token, to=reminder["peer"], text=report,
                        context_token=sent_context, client_id="hermes-todos-" + reminder["id"],
                    ), timeout=15)
                    error = response_error(response, (platform.token, context or "", sent_context or "")) \
                        if isinstance(response, dict) else DeliveryError("delivery_uncertain")
                except aiohttp.ClientConnectorError:
                    error = DeliveryError("transport_failed")
                except (asyncio.TimeoutError, aiohttp.ClientError):
                    # A lost acknowledgement can mean delivery; never retry it here.
                    error = DeliveryError("delivery_uncertain")
                except RuntimeError as exc:
                    match = re.search(r"iLink POST \S+ HTTP (\d{3}):", str(exc))
                    status = int(match[1]) if match else None
                    if status == 429:
                        error = DeliveryError("rate_limited", errmsg="HTTP 429")
                    elif status in {401, 403}:
                        error = DeliveryError("channel_auth_failed", errmsg=f"HTTP {status}")
                    else:
                        error = DeliveryError("delivery_uncertain")
                except Exception:
                    error = DeliveryError("delivery_uncertain")
                record_attempt(mode, sent_context, error, response if not error else None)
                if not error:
                    return
                can_recover = error.category == "context_rejected" or (
                    error.details.get("ret") == -2 or error.details.get("errcode") == -2
                ) and error.details.get("errmsg", "").strip().lower() == "prepare failed"
                if index == 0 and sent_context and can_recover:
                    # A new inbound may have arrived during the rejected request. Prefer it;
                    # otherwise try once without the old token. Neither action renews a
                    # server-side interaction window. Do not delete the durable token.
                    latest = read_context(home, account, reminder["peer"])
                    sent_context = latest if latest and latest != sent_context else None
                    if sent_context:
                        observed_context = sent_context
                    mode = "refreshed_context" if sent_context else "without_context"
                    continue
                error.details["recovery_attempted"] = index == 1
                error.details["context_revision"] = context_revision(home, account, reminder["peer"], observed_context)
                raise error
    asyncio.run(send())


class ReminderQueue:
    def __init__(self, root: Path, *, query=query_report, send=send_report, binding=active_binding, clock=time.time):
        self.root = root.resolve()
        self.path = self.root / "notifications/work-todos.sqlite3"
        self.query, self.send, self.binding, self.clock = query, send, binding, clock
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id TEXT PRIMARY KEY, profile TEXT NOT NULL, peer TEXT NOT NULL,
                    username TEXT NOT NULL, binding_identity TEXT NOT NULL, reminder_key TEXT NOT NULL,
                    state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, expires_at REAL NOT NULL, next_attempt REAL,
                    lease_until REAL, interaction_version INTEGER NOT NULL DEFAULT 0,
                    error TEXT, sent_at REAL,
                    UNIQUE(profile, binding_identity, reminder_key)
                );
                CREATE TABLE IF NOT EXISTS interactions (
                    peer TEXT NOT NULL, profile TEXT NOT NULL, version INTEGER NOT NULL,
                    updated_at REAL NOT NULL, PRIMARY KEY(peer, profile)
                );
                CREATE TABLE IF NOT EXISTS delivery_attempts (
                    id INTEGER PRIMARY KEY, reminder_id TEXT NOT NULL,
                    created_at REAL NOT NULL, diagnostics TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS daily_subscriptions (
                    profile TEXT PRIMARY KEY, employee_id TEXT NOT NULL,
                    username TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
                );
            """)
            # Serialize the additive migration across cron and Gateway processes.
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute('PRAGMA table_info(reminders)')}
            for name in ('employee_id', 'local_day'):
                if name not in columns:
                    db.execute(f'ALTER TABLE reminders ADD COLUMN {name} TEXT')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS reminders_employee_day ON reminders(employee_id, local_day)')
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

    def enqueue(self, profile: str, peer: str, username: str, reminder_key: str) -> str:
        binding = self.binding(self.root, profile, peer)
        identity = binding_identity(binding)
        key = hashlib.sha256(json.dumps([profile, identity, reminder_key]).encode()).hexdigest()[:32]
        now = self.clock()
        with self.connect() as db:
            existing = db.execute("SELECT * FROM reminders WHERE id=?", (key,)).fetchone()
            if existing and (existing["peer"] != peer or existing["username"] != username):
                raise DeliveryError("identity_mismatch")
            db.execute("INSERT OR IGNORE INTO reminders (id,profile,peer,username,binding_identity,reminder_key,state,created_at,expires_at,next_attempt) VALUES (?,?,?,?,?,?,\"pending\",?,?,?)",
                       (key, profile, peer, username, identity, reminder_key, now, now + DELIVERY_TTL, now))
        return key

    def get(self, key: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM reminders WHERE id=?", (key,)).fetchone()
        return dict(row) if row else None

    def subscribe_daily(self, profile: str, peer: str, username: str) -> None:
        binding = self.binding(self.root, profile, peer)
        with self.connect() as db:
            db.execute('INSERT INTO daily_subscriptions VALUES (?,?,?,1) '
                       'ON CONFLICT(profile) DO UPDATE SET employee_id=excluded.employee_id,username=excluded.username,enabled=1',
                       (profile, binding['employee_id'], username))

    def enqueue_daily(self, profile: str, peer: str, username: str, *, interaction: bool = False) -> str:
        binding = self.binding(self.root, profile, peer)
        employee, identity = binding['employee_id'], binding_identity(binding)
        now = self.clock()
        day, expires = daily_window(now)
        occurrence = f'work-todos:daily-09:00:{day}'  # Preserve existing cron history.
        key = hashlib.sha256(json.dumps([profile, identity, occurrence]).encode()).hexdigest()[:32]
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM reminders WHERE employee_id=? AND local_day=?', (employee, day)).fetchone()
            if row is None:
                # Adopt today's pre-upgrade record, including a successful or
                # uncertain send, so deployment never sends it a second time.
                row = db.execute('SELECT * FROM reminders WHERE profile=? AND binding_identity=? AND reminder_key=?',
                                 (profile, identity, occurrence)).fetchone()
                if row:
                    db.execute('UPDATE reminders SET employee_id=?,local_day=? WHERE id=?', (employee, day, row['id']))
            if row is None:
                db.execute("INSERT INTO reminders (id,profile,peer,username,binding_identity,reminder_key,state,created_at,expires_at,next_attempt,employee_id,local_day) "
                           "VALUES (?,?,?,?,?,?,'pending',?,?,?,?,?)",
                           (key, profile, peer, username, identity, occurrence, now, expires, now, employee, day))
                return key
            key = row['id']
            if row['username'] != username:
                raise DeliveryError('identity_mismatch')
            # Never re-arm successful, in-flight, or possibly delivered messages.
            if row['state'] in {'sent', 'sending', 'delivery_uncertain'}:
                return key
            if row['binding_identity'] != identity:
                # Only an unsent occurrence may move to the employee's new binding.
                db.execute("UPDATE reminders SET profile=?,peer=?,binding_identity=?,state='pending',attempts=0,"
                           'expires_at=?,next_attempt=?,lease_until=NULL,interaction_version=0,error=NULL WHERE id=?',
                           (profile, peer, identity, expires, now, key))
            elif row['peer'] != peer or row['profile'] != profile:
                raise DeliveryError('identity_mismatch')
            elif row['state'] in {'pending', 'waiting_interaction'}:
                db.execute('UPDATE reminders SET expires_at=? WHERE id=?', (expires, key))
            elif interaction and row['state'] == 'expired' and row['attempts'] < MAX_ATTEMPTS:
                # Legacy reminders used a four-hour TTL. A new inbound can still
                # request today's current tasks in the afternoon, never yesterday's.
                db.execute("UPDATE reminders SET state='pending',expires_at=?,next_attempt=? WHERE id=?", (expires, now, key))
        return key

    def on_daily_interaction(self, profile: str, peer: str) -> str | None:
        with self.connect() as db:
            subscription = db.execute('SELECT * FROM daily_subscriptions WHERE profile=? AND enabled=1', (profile,)).fetchone()
        if not subscription:
            return None
        binding = self.binding(self.root, profile, peer)
        if binding['employee_id'] != subscription['employee_id']:
            raise DeliveryError('identity_mismatch')
        return self.enqueue_daily(profile, peer, subscription['username'], interaction=True)

    def note_interaction(self, profile: str, peer: str) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO interactions VALUES (?,?,1,?) ON CONFLICT(peer,profile) DO UPDATE SET version=version+1,updated_at=excluded.updated_at",
                       (peer, profile, self.clock()))

    def _refresh_waiting_context(self, key: str) -> None:
        reminder = self.get(key)
        if not reminder or reminder["state"] != "waiting_interaction" or reminder["expires_at"] <= self.clock():
            return
        try:
            error = json.loads(reminder["error"] or "{}")
            home, platform = transport(self.root, reminder["profile"], reminder["peer"])
            account = str(platform.extra["account_id"])
            context = read_context(home, account, reminder["peer"])
            # Missing/deleted cache is not evidence of a new user interaction.
            if not context:
                return
            revision = context_revision(home, account, reminder["peer"], context)
        except (DeliveryError, OSError, ValueError, ImportError):
            return
        previous = error.get("context_revision")
        with self.connect() as db:
            if not previous:
                # Older queue records lack a rejected-context baseline. Establish
                # it once, without assuming the current token is a fresh inbound.
                error["context_revision"] = revision
                db.execute("UPDATE reminders SET error=? WHERE id=? AND state='waiting_interaction' AND error IS ?",
                           (json.dumps(error, ensure_ascii=False), key, reminder["error"]))
            elif previous != revision:
                changed = db.execute(
                    "UPDATE reminders SET state='pending',next_attempt=? WHERE id=? AND state='waiting_interaction' AND error IS ? AND expires_at>?",
                    (self.clock(), key, reminder["error"], self.clock())).rowcount
                if changed:
                    logger.info("work-todos context_changed profile=%s reminder=%s", reminder["profile"], key)

    def _claim(self, key: str) -> dict | None:
        now = self.clock()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE reminders SET state=\"delivery_uncertain\",lease_until=NULL,error=? WHERE state=\"sending\" AND lease_until<=?",
                       (json.dumps({"category": "worker_interrupted"}), now))
            db.execute("UPDATE reminders SET state=\"expired\" WHERE expires_at<=? AND state IN (\"pending\",\"waiting_interaction\")", (now,))
            row = db.execute("SELECT * FROM reminders WHERE id=?", (key,)).fetchone()
            if not row or row["state"] not in {"pending", "waiting_interaction"}:
                return None
            interaction = db.execute("SELECT version FROM interactions WHERE profile=? AND peer=?", (row["profile"], row["peer"])).fetchone()
            version = interaction[0] if interaction else 0
            if row["state"] == "waiting_interaction" and version <= row["interaction_version"]:
                return None
            if row["state"] == "pending" and row["next_attempt"] > now:
                return None
            db.execute("UPDATE reminders SET state=\"sending\",attempts=attempts+1,lease_until=?,interaction_version=? WHERE id=?",
                       (now + LEASE_SECONDS, version, key))
            return dict(db.execute("SELECT * FROM reminders WHERE id=?", (key,)).fetchone())

    def process(self, key: str) -> str:
        # A persisted inbound token may outlive a missed hook during shutdown.
        # Reconcile locally; unchanged tokens never trigger network retries.
        self._refresh_waiting_context(key)
        reminder = self._claim(key)
        if not reminder:
            return (self.get(key) or {}).get("state", "missing")
        sending = False
        try:
            binding = self.binding(self.root, reminder["profile"], reminder["peer"])
            if binding_identity(binding) != reminder["binding_identity"]:
                raise DeliveryError("binding_changed")
            report = self.query(self.root, reminder["profile"], reminder["username"])
            if self.clock() >= reminder['expires_at']:
                raise DeliveryError('expired')
            binding = self.binding(self.root, reminder["profile"], reminder["peer"])
            if binding_identity(binding) != reminder["binding_identity"]:
                raise DeliveryError("binding_changed")
            sending = True
            self.send(self.root, reminder, report)
        except DeliveryError as exc:
            error = exc
        except Exception:
            error = DeliveryError("delivery_uncertain" if sending else "query_failed")
        else:
            with self.connect() as db:
                db.execute("UPDATE reminders SET state=\"sent\",sent_at=?,lease_until=NULL,error=NULL WHERE id=?", (self.clock(), key))
            logger.info("work-todos send_completed profile=%s reminder=%s attempts=%d", reminder["profile"], key, reminder["attempts"])
            return "sent"
        if error.category == 'expired':
            state, next_attempt = 'expired', None
        elif error.category in {"binding_changed", "identity_mismatch"}:
            state, next_attempt = "cancelled", None
        elif error.category == "channel_auth_failed":
            state, next_attempt = "failed", None
        elif error.category == "delivery_uncertain":
            state, next_attempt = "delivery_uncertain", None
        elif reminder["attempts"] >= MAX_ATTEMPTS:
            state, next_attempt = "failed", None
        elif error.category in {"context_rejected", "send_rejected"}:
            state, next_attempt = "waiting_interaction", None
        else:
            state, next_attempt = "pending", self.clock() + RETRY_DELAYS[reminder["attempts"] - 1]
        with self.connect() as db:
            db.execute("UPDATE reminders SET state=?,next_attempt=?,lease_until=NULL,error=? WHERE id=?",
                       (state, next_attempt, json.dumps(error.details, ensure_ascii=False), key))
        logger.warning("work-todos send_failed profile=%s reminder=%s state=%s error=%s", reminder["profile"], key, state,
                       json.dumps(error.details, ensure_ascii=False))
        return state

    def tick(self) -> None:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM reminders WHERE state IN (\"pending\",\"waiting_interaction\",\"sending\") ORDER BY created_at").fetchall()
            db.execute("DELETE FROM reminders WHERE state NOT IN (\"pending\",\"waiting_interaction\",\"sending\") AND created_at<?", (self.clock() - 7 * 86400,))
            db.execute("DELETE FROM delivery_attempts WHERE created_at<?", (self.clock() - 7 * 86400,))
        for row in rows:
            if row["state"] != "sending":
                try:
                    current = self.binding(self.root, row["profile"], row["peer"])
                    changed = binding_identity(current) != row["binding_identity"]
                except DeliveryError:
                    changed = True
                if changed:
                    with self.connect() as db:
                        db.execute("UPDATE reminders SET state=\"cancelled\",error=? WHERE id=? AND state IN (\"pending\",\"waiting_interaction\")",
                                   (json.dumps({"category": "binding_changed"}), row["id"]))
                    continue
            self.process(row["id"])


def run_reminder(home: Path, username: str, peer: str, *, reminder_key: str | None = None,
                 daily: bool = False, query_only: bool = False) -> str:
    home = home.resolve()
    if home.parent.name != "profiles":
        raise DeliveryError("binding_changed")
    root = home.parent.parent
    active_binding(root, home.name, peer)
    if query_only:
        return query_report(root, home.name, username)
    log_path = root / "notifications/work-todos-delivery.log"
    log_path.parent.mkdir(mode=0o700, exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        queue = ReminderQueue(root)
        if daily:
            key = queue.enqueue_daily(home.name, peer, username)
        else:
            if not reminder_key:
                raise ValueError('reminder_key is required for non-daily reminders')
            key = queue.enqueue(home.name, peer, username, reminder_key)
        result = queue.process(key)
        return f"work-todos reminder={key} state={result}"
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(previous_level)


def install_delivery_worker() -> bool:
    """Observe accepted inbound context updates, without changing normal chat sending."""
    try:
        from gateway.platforms.weixin import ContextTokenStore
        from hermes_constants import get_default_hermes_root
    except ImportError:
        return False

    state = sys.modules.get(PROCESS_STATE)
    if state is None:
        state = ModuleType(PROCESS_STATE)
        state.lock, state.wake, state.started = Lock(), Event(), False
        sys.modules[PROCESS_STATE] = state
    with state.lock:
        if state.started:
            return False
        root = Path(get_default_hermes_root()).resolve()
        try:
            queue = ReminderQueue(root)
        except (OSError, sqlite3.Error):
            logger.error("work-todos delivery worker could not open its durable queue")
            return False
        original = ContextTokenStore.set

        @wraps(original)
        def set_context(store, account_id, user_id, token):
            result = original(store, account_id, user_id, token)
            try:
                payload = json.loads((root / "provisioner/wechat_bindings.json").read_text())
                for binding in payload.get("bindings", {}).values():
                    if binding.get("status") != "active" or binding.get("external_user_id") != user_id:
                        continue
                    home, platform = transport(root, binding["profile_name"], user_id)
                    if str(platform.extra["account_id"]) == str(account_id) and store._root.resolve() == (home / "weixin/accounts").resolve():
                        queue.note_interaction(binding["profile_name"], user_id)
                        queue.on_daily_interaction(binding["profile_name"], user_id)
                        state.wake.set()
            except Exception:
                logger.warning("work-todos could not record accepted WeChat interaction", exc_info=False)
            return result

        ContextTokenStore.set = set_context

        def worker():
            while True:
                state.wake.wait(15)
                state.wake.clear()
                try:
                    queue.tick()
                except Exception:
                    logger.warning("work-todos maintenance failed", exc_info=False)

        Thread(target=worker, name="profile-rag-mcp-work-todos", daemon=True).start()
        state.started = True
        logger.info("work-todos delivery worker started")
        return True

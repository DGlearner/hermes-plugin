"""Require a real employee reply after an exact, validated daily-report preview.

The server independently enforces template/version/token checks. This gate only
adds trusted conversation confirmation for Hermes; no model-controlled argument
can manufacture a confirmed turn. Volatile state fails closed after a restart.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
from threading import RLock
import time
from types import ModuleType


CONFIRMATION_TEXT = "确认提交日报"
REPORT_TOOLS = frozenset({
    "prepare_daily_report_draft", "validate_daily_report_draft", "publish_daily_report_draft",
    "submit_daily_report", "update_daily_report", "save_daily_report_draft",
})
_STATE_MODULE = "_hermes_daily_report_confirmation_state"
_STATE_TTL_SECONDS = 3600
_MAX_SESSIONS = 1024


def _state():
    candidate = ModuleType(_STATE_MODULE)
    candidate.lock = RLock()
    candidate.sessions = {}
    shared = sys.modules.setdefault(_STATE_MODULE, candidate)
    return shared.lock, shared.sessions


def _key(resolve_home, session_id):
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("daily-report confirmation requires a current employee Session")
    return str(Path(resolve_home()).resolve()), session_id


def _prune(sessions):
    cutoff = time.monotonic() - _STATE_TTL_SECONDS
    for key in tuple(sessions):
        if sessions[key]["updated_at"] < cutoff:
            del sessions[key]
    while len(sessions) >= _MAX_SESSIONS:
        del sessions[min(sessions, key=lambda key: sessions[key]["updated_at"])]


def _error(message):
    return json.dumps({"error": message, "error_type": "daily_report_confirmation_required"}, ensure_ascii=False)


def pre_turn(resolve_home, *, session_id="", turn_id="", platform="", sender_id="",
             parent_session_id="", user_message=None, **_):
    """Called only by the native pre_llm_call hook, never by an MCP tool."""
    if (platform not in {"weixin", "wecom_callback", "telegram"} or not sender_id
            or not turn_id or parent_session_id or not isinstance(user_message, str)):
        return
    try:
        key = _key(resolve_home, session_id)
    except (OSError, RuntimeError, ValueError):
        return
    lock, sessions = _state()
    with lock:
        _prune(sessions)
        old = sessions.get(key, {})
        if old.get("turn_id") == turn_id:
            return
        same_sender = old.get("platform") == platform and old.get("sender_id") == sender_id
        pending = old.get("pending") if same_sender else None
        confirmed = (user_message.strip() == CONFIRMATION_TEXT and pending
                     and pending.get("previewed") and pending.get("validated_turn") != turn_id)
        sessions[key] = {
            "turn_id": turn_id, "platform": platform, "sender_id": sender_id,
            "updated_at": time.monotonic(), "pending": pending if confirmed else None,
            "confirmed": bool(confirmed),
        }


def post_turn(resolve_home, *, session_id="", turn_id="", platform="", assistant_response="", **_):
    """Arm only an exact complete preview in a completed assistant response."""
    try:
        key = _key(resolve_home, session_id)
    except (OSError, RuntimeError, ValueError):
        return
    lock, sessions = _state()
    with lock:
        state = sessions.get(key)
        if not state or state["turn_id"] != turn_id or state["platform"] != platform:
            return
        pending = state.get("pending")
        if not pending or pending["validated_turn"] != turn_id or not isinstance(assistant_response, str):
            return
        pending["previewed"] = (pending["content"] in assistant_response
                                and CONFIRMATION_TEXT in assistant_response)


def _validation(result, arguments, turn_id):
    try:
        value = json.loads(result)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("passed") is not True:
        return None
    draft = value.get("draft")
    if not isinstance(draft, dict):
        return None
    draft_id = draft.get("draft_id", draft.get("id"))
    version = draft.get("version")
    content = draft.get("content")
    token = value.get("confirmation_token")
    digest = value.get("content_digest")
    if (draft_id != arguments.get("draft_id") or version != arguments.get("expected_version")
            or not isinstance(content, str) or not content.strip()
            or not isinstance(token, str) or not token or not isinstance(digest, str) or not digest):
        return None
    return {"draft_id": draft_id, "expected_version": version, "content": content,
            "confirmation_token": token, "content_digest": digest,
            "validated_turn": turn_id, "previewed": False, "attempt_key": None}


def call_tool(tool_name, arguments, *, session_id, resolve_home, call):
    """Wrap only report writes; the callback retains native PAT authorization."""
    if tool_name in {"submit_daily_report", "update_daily_report", "save_daily_report_draft"}:
        return _error("日报须先保存草稿并通过检查，完整预览后由员工回复‘确认提交日报’，不能直接提交或修改正式日报。")
    if tool_name not in REPORT_TOOLS:
        return call()
    try:
        key = _key(resolve_home, session_id)
    except (OSError, RuntimeError, ValueError):
        return _error("无法确认当前员工会话，请在绑定的聊天窗口重新打开日报草稿。")
    lock, sessions = _state()
    with lock:
        _prune(sessions)
        state = sessions.get(key)
        if not state:
            return _error("缺少当前员工的真实消息，请在绑定的聊天窗口重新打开日报草稿。")
        turn_id = state["turn_id"]
        if tool_name == "publish_daily_report_draft":
            pending = state.get("pending")
            if (not state.get("confirmed") or not pending
                    or any(arguments.get(field) != pending[field] for field in
                           ("draft_id", "expected_version", "confirmation_token", "content_digest"))):
                return _error("请先检查并完整展示这一版日报，等待员工在之后的消息中明确回复‘确认提交日报’，再发布。")
            request_key = arguments.get("idempotency_key")
            if not isinstance(request_key, str) or not request_key:
                return _error("日报发布缺少重试标识，请保留当前确认并使用同一次发布标识。")
            if pending["attempt_key"] is not None and pending["attempt_key"] != request_key:
                return _error("本次日报已尝试发布；请用原发布标识核实结果，不得重复发起新提交。")
            pending["attempt_key"] = request_key
        else:
            # A new edit or validation cannot inherit any earlier approval.
            state["pending"] = None
            state["confirmed"] = False
        state["updated_at"] = time.monotonic()
    result = call()
    if tool_name == "validate_daily_report_draft":
        pending = _validation(result, arguments, turn_id)
        with lock:
            current = sessions.get(key)
            if current and current["turn_id"] == turn_id:
                current["pending"] = pending
    return result

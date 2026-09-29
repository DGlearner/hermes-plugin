"""Profile-local explicit read receipts and independent reminder scheduling."""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import re
import sqlite3
import time
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .profile_inspection import InspectionConfig
from .daily_inspection import fingerprint, report_key, ItemNotification


def clean_notice_message(message):
    """Hide internal notice ids while keeping legacy cached bodies readable."""
    lines = []
    for line in message.splitlines():
        if re.fullmatch(r'【未读提醒 R\d+】|提醒编号：R\d+|请回复“已读(?: R\d+)?”确认当前未读提醒', line.strip()):
            continue
        if line.strip().startswith('回复“已读'):
            lines.append('回复“已读”停止当前未读提醒。')
            continue
        lines.append(line)
    return '\n'.join(lines).strip()


def format_notice(report):
    return format_batch([report])


def _render_page(reports, *, flush_left=False):
    groups = {}
    for report in reports:
        kind = report.get('item_type', 'report')
        body = format_report(report)
        if flush_left:
            # Keep visible numbering without triggering Markdown list indentation.
            lines = []
            for line in body.splitlines():
                line = line.lstrip()
                line = re.sub(r'^(\d+)[.)](?=\s)', r'\1．', line)
                line = re.sub(r'^[-+*](?=\s)', '•', line)
                line = re.sub(r'^>(?=\s|$)', '＞', line)
                lines.append(line)
            body = '\n'.join(lines)
        if report.get('_notice_id') is not None:
            body = f"[R{report['_notice_id']}]\n" + body
        groups.setdefault(kind, []).append(body)
    titles = {'report': '日报提醒', 'task': '待办提醒', 'review': '文件审核提醒'}
    sections = [f"**{titles[kind]}**\n\n" + '\n\n'.join(groups[kind])
                for kind in ('report', 'task', 'review') if kind in groups]
    if 'review' in groups:
        sections.append('确认已读仅停止提醒，不代表批准入库。')
    return '\n\n'.join(sections) + '\n\n**回复“已读 R编号”确认单项；回复“全部已读”或“已读”确认全部。**'


def format_batch(reports, limit=1500, *, notice_ids=None, byte_limit=None, flush_left=False):
    """Pack complete items, counting headings/footer in the soft page limit.

    A single oversized item occupies its own page without loss or slicing.
    ItemNotification prevents the transport from slicing these pages again.
    """
    # IDs come only from the local ledger, never from MCP content or position.
    if notice_ids is not None and len(notice_ids) != len(reports):
        raise ValueError('notice ids must match items')
    reports = [dict(r, _notice_id=notice_ids[i] if notice_ids is not None else None)
               for i, r in enumerate(reports)]
    ordered = [r for kind in ('report', 'task', 'review') for r in reports
               if r.get('item_type', 'report') == kind]
    pages, current = [], []
    for report in ordered:
        candidate = _render_page(current + [report], flush_left=flush_left)
        if current and (len(candidate) > limit or (byte_limit and len(candidate.encode('utf-8')) > byte_limit)):
            pages.append(_render_page(current, flush_left=flush_left))
            current = []
        current.append(report)
    if current:
        pages.append(_render_page(current, flush_left=flush_left))
    return ItemNotification(pages)


def format_report(report):
    if report.get('item_type') == 'task':
        status = {'open': '待开始', 'in_progress': '进行中', 'pending_review': '待审核'}.get(report['status'], report['status'])
        return f"{report['title']}\n\n任务：{report['task_no']}\n\n状态：{status}\n\n截止时间：{report.get('due_at') or '未设置'}\n\n正文：\n\n{report['content']}"
    if report.get("item_type") == "review":
        if 'file_name' not in report:
            return "\n\n".join(line.strip() for line in report['content'].splitlines() if line.strip())
        requested = report.get('review_requested_at') or '未提供'
        try:
            timestamp = datetime.fromisoformat(requested.replace('Z', '+00:00'))
            if timestamp.tzinfo is not None:
                requested = timestamp.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S') + '（UTC+8）'
        except ValueError:
            pass
        author = report.get('uploaded_by_display_name') or report.get('uploaded_by_username') or '未提供'
        department = report.get('steward_department_name') or '未提供'
        download = report.get('download_url')
        download_line = f"\n\n下载：[点击下载]({download})" if isinstance(download, str) and download.startswith(('https://', 'http://')) else ''
        return (f"文件：{report['file_name']}\n\n标题：{report['title']}"
                f"\n\n提交人：{author}（{department}）\n\n申请时间：{requested}"
                f"\n\n版本：{report['version']}" + download_line)
    kind = report['report_type']
    subject = (report.get('employee_display_name') or report.get('employee_username')) if kind == 'personal' else (report.get('department_name') or report.get('department_code')) if kind == 'department' else '公司'
    author = report.get('author_display_name') or report.get('author_username') or '未提供'
    title = {'personal': '个人日报', 'department': '部门日报', 'company': '公司日报'}[kind]
    body = '\n'.join(line.lstrip() for line in report['content'].split('\n'))
    return f"{title}｜{subject}\n\n日期：{report['report_date']}｜发布人：{author}\n\n正文：\n\n{body}"


@contextlib.contextmanager
def ledger(home, *, blocking=True):
    directory = Path(home) / 'inspection'
    directory.mkdir(mode=0o700, exist_ok=True)
    with (directory / 'ack.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        path = directory / 'ack.sqlite3'
        with contextlib.closing(sqlite3.connect(path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.executescript('''
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS notices(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, item_key TEXT, fingerprint TEXT,
                    payload TEXT, state TEXT, message TEXT, next_send REAL DEFAULT 0,
                    round INTEGER DEFAULT 0, summary_attempts INTEGER DEFAULT 0,
                    acked_at REAL, ack_channel TEXT, sent_day TEXT, sent_today INTEGER DEFAULT 0,
                    last_outcomes TEXT, UNIQUE(item_key,fingerprint));
            ''')
            path.chmod(0o600)
            yield db


def get_meta(db, key, default=None):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def put_meta(db, key, value):
    db.execute('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))


class AckInspectionRunner:
    def __init__(self, home, query, summarize, send, clock=time.time):
        self.home = Path(home)
        self.query, self.summarize, self.send, self.clock = query, summarize, send, clock

    def run(self, config, *, force=False, remind_now=False):
        if not config.get('enabled') or not config.get('acknowledgement', {}).get('enabled'):
            return {'status': 'disabled'}
        try:
            with ledger(self.home, blocking=False) as db:
                return self._tick(db, config, force or remind_now, remind_now)
        except BlockingIOError:
            return {'status': 'busy'}

    def _tick(self, db, config, force, remind_now=False):
        now = self.clock()
        schedule = InspectionConfig.from_mapping(config)
        if not schedule.in_work_hours(now):
            return {'status': 'outside_work_hours'}
        ack = config['acknowledgement']
        interval = int(ack.get('reminder_minutes', 30)) * 60
        if interval < 60:
            raise ValueError('invalid reminder interval')
        last = get_meta(db, 'last_checked_at')
        due = force or schedule.due(float(last) if last else None, now)
        day = schedule.localize(now).date().isoformat()
        result = {'status': 'not_due', 'queried': False}
        if due:
            if float(get_meta(db, 'query_retry_at', 0)) > now:
                return {'status': 'query_retry_wait', 'queried': False}
            try:
                reports = self.query(self.home, config, day)
                current = {(report_key(r), fingerprint(r)): r for r in reports}
            except Exception:
                with db:
                    put_meta(db, 'query_ok', 0)
                    attempts = int(get_meta(db, 'query_failures', 0)) + 1
                    put_meta(db, 'query_failures', attempts)
                    put_meta(db, 'query_retry_at', now + min(1800, 60 * 2 ** min(attempts - 1, 5)))
                return {'status': 'query_failed', 'queried': True}
            with db:
                for row in db.execute("SELECT * FROM notices WHERE state='unread'").fetchall():
                    if (row['item_key'], row['fingerprint']) not in current:
                        db.execute("UPDATE notices SET state='superseded' WHERE id=?", (row['id'],))
                for (key, fp), report in current.items():
                    db.execute("INSERT OR IGNORE INTO notices(item_key,fingerprint,payload,state) VALUES (?,?,?,'unread')", (key, fp, json.dumps(report, ensure_ascii=False)))
                    # Refresh short-lived review URLs on every successful poll
                    # without changing the durable notice fingerprint/state.
                    db.execute("UPDATE notices SET payload=? WHERE item_key=? AND fingerprint=? AND state='unread'", (json.dumps(report, ensure_ascii=False), key, fp))
                put_meta(db, 'last_checked_at', now)
                put_meta(db, 'query_ok', 1)
                put_meta(db, 'query_failures', 0)
                put_meta(db, 'query_retry_at', 0)
            result = {'status': 'checked', 'queried': True, 'report_count': sum(r.get('item_type') not in {'task', 'review'} for r in reports), 'task_count': sum(r.get('item_type') == 'task' for r in reports), 'review_count': sum(r.get('item_type') == 'review' for r in reports), 'checked_at': now}
        # Never repeat cached data after a failed query or beyond one polling interval.
        last = get_meta(db, 'last_checked_at')
        if get_meta(db, 'query_ok') != '1' or not last or now-float(last) > schedule.interval_minutes*60:
            return result
        unread = db.execute("SELECT * FROM notices WHERE state='unread' ORDER BY id").fetchall()
        result['unread_count'] = len(unread)
        if schedule.in_quiet_hours(now):
            result['status'] = 'quiet_deferred'
            return result
        # One Profile-wide cadence coalesces notices whose individual deadlines
        # differ. Newly found items wait at most one reminder interval.
        if not remind_now and float(get_meta(db, 'batch_next_send', 0)) > now:
            result['reminders'] = []
            return result
        emitted = []
        selected = []
        for row in unread:
            if not remind_now and row['next_send'] > now:
                continue
            cap = ack.get('max_reminders_per_day', 3)
            if cap is not None and row['sent_day'] == day and row['sent_today'] >= int(cap):
                continue
            message = row['message']
            if not message:
                if row['summary_attempts'] >= 4:
                    continue
                try:
                    report = json.loads(row['payload'])
                    summary = self.summarize(self.home, [{'key': row['item_key'], 'fingerprint': row['fingerprint'], 'report': report}])
                    if not summary or summary.strip() == '[SILENT]':
                        raise ValueError('empty summary')
                    # Put the stable acknowledgement token in the first line so it
                    # remains visible even when a channel previews or truncates text.
                    # The model validates/normalizes the change, while the
                    # channel message keeps one authoritative copy: the source
                    # report itself.
                    message = format_notice(report)
                    with db:
                        db.execute('UPDATE notices SET message=? WHERE id=?', (message, row['id']))
                except Exception:
                    with db:
                        db.execute('UPDATE notices SET summary_attempts=summary_attempts+1,next_send=? WHERE id=?', (now+interval, row['id']))
                    continue
            else:
                # Upgrade any cached notice created by an older template and
                # remove the previously duplicated summary block.
                message = format_notice(json.loads(row['payload']))
                with db:
                    db.execute('UPDATE notices SET message=? WHERE id=?', (message, row['id']))
            selected.append((row, message))
        if selected:
            # Claim the entire batch before external I/O. A restart cannot replay
            # only half of a batch, and the Profile lock serializes read receipts.
            with db:
                put_meta(db, 'batch_next_send', now+interval)
                for row, _ in selected:
                    db.execute('UPDATE notices SET round=?,next_send=?,sent_day=?,sent_today=? WHERE id=?', (row['round']+1, now+interval, day, row['sent_today']+1 if row['sent_day']==day else 1, row['id']))
            message = format_batch([json.loads(row['payload']) for row, _ in selected],
                                   notice_ids=[row['id'] for row, _ in selected],
                                   flush_left=any(t.get('platform') == 'wecom_callback' for t in config.get('notification_targets', [])),
                                   byte_limit=4000 if any(t.get('platform') == 'wecom_callback' for t in config.get('notification_targets', [])) else None)
            batch = 'ack-batch-' + '-'.join(f"{row['id']}.{row['round']+1}" for row, _ in selected)
            try:
                outcomes = self.send(self.home, config, message, batch) or {'delivery':'sent'}
            except Exception as exc:
                outcomes = getattr(exc, 'outcomes', {'delivery':'uncertain'})
            with db:
                for row, _ in selected:
                    db.execute('UPDATE notices SET last_outcomes=? WHERE id=?', (json.dumps(outcomes), row['id']))
                    emitted.append({'notice': f"R{row['id']}", 'channels': outcomes})
        result['reminders'] = emitted
        if config.get('test_notify_each_check') and result['queried']:
            when = schedule.localize(now).strftime('%m-%d %H:%M:%S')
            message = f"【后台巡检测试】{when}\n查询完成：可见日报 {result['report_count']} 条，未读 {result['unread_count']} 条。\n未读正文按 {interval//60} 分钟间隔提醒；本条执行回执无需确认。"
            if reports:
                message += "\n\n" + "\n\n".join(format_report(report) for report in reports)
            try:
                result['test_notice_channels'] = self.send(self.home, config, message, f"ack-check-{now}")
            except Exception as exc:
                result['test_notice_channels'] = getattr(exc, 'outcomes', {'delivery':'uncertain'})
        return result


# Only explicit commands count as acknowledgement; arbitrary prose never does.
COMMAND = re.compile(r'^(已读|确认已读|看过了|收到|全部已读|稍后提醒|未读提醒)(?:\s*(R\d+))?[。！!\s]*$', re.I)
TRUSTED = ContextVar('inspection_ack_source', default=None)


def parse_command(text, quote=''):
    match = COMMAND.fullmatch(text.strip())
    if not match:
        return None
    action, token = match.groups()
    # Bare read commands always confirm all, including replies quoting several
    # items. A single item must be selected explicitly in the user's command.
    if not token and action == '稍后提醒':
        quoted = set(re.findall(r'提醒编号：\s*(R\d+)', quote, re.I))
        if len(quoted) == 1:
            token = quoted.pop()
        elif len(quoted) > 1:
            return ('ambiguous', None)
    return action, token.upper() if token else None


def acknowledge(home, action, token, platform, clock=time.time):
    with ledger(home) as db:
        config = json.loads((Path(home)/'inspection.json').read_text())
        if not config.get('enabled') or not config.get('acknowledgement',{}).get('enabled'):
            return '当前未启用已读确认。'
        rows = db.execute("SELECT id FROM notices WHERE state='unread' ORDER BY id").fetchall()
        ids = [r['id'] for r in rows]
        if action == '未读提醒' or action == 'ambiguous':
            return '当前未读：'+ '、'.join(f'R{i}' for i in ids) if ids else '当前没有未读提醒。'
        if token:
            number = int(token[1:])
            row = db.execute('SELECT state FROM notices WHERE id=?', (number,)).fetchone()
            if not row:
                return '没有找到该提醒编号。'
            if row[0] == 'acked':
                return f'{token} 已确认已读，不会重复提醒。'
            if row[0] != 'unread':
                return f'{token} 已更新或不再可见，请确认最新提醒。'
            ids = [number]
        elif action not in {'已读', '确认已读', '看过了', '收到', '全部已读'} and len(ids)>1:
            return '有多条未读提醒，请回复“已读”全部确认，或回复“已读 R编号”精确确认：'+ '、'.join(f'R{i}' for i in ids)
        if not ids:
            return '当前没有未读提醒。'
        now = clock()
        with db:
            for number in ids:
                if action == '稍后提醒':
                    minutes = int(config['acknowledgement'].get('snooze_minutes',30))
                    db.execute('UPDATE notices SET next_send=? WHERE id=?', (now+minutes*60, number))
                else:
                    db.execute("UPDATE notices SET state='acked',acked_at=?,ack_channel=? WHERE id=?", (now,platform,number))
        names = '、'.join(f'R{i}' for i in ids)
        if action == '稍后提醒':
            return f'{names} 已延后 {minutes} 分钟提醒。'
        return f'{names} 已确认已读，所有已绑定通道均停止该版本的重复提醒；后台继续检索新内容。'


def pre_dispatch(event=None, gateway=None, **kwargs):
    TRUSTED.set(None)
    if event is None or gateway is None or getattr(event,'internal',False):
        return None
    parsed = parse_command(getattr(event,'text',''), getattr(event,'reply_to_text','') or '')
    if not parsed:
        return None
    source = event.source
    platform = getattr(source.platform,'value',source.platform)
    if platform not in {'weixin','telegram','wecom_callback'} or source.chat_type != 'dm' or str(source.user_id) != str(source.chat_id):
        return None
    try:
        if getattr(source,'profile_route_rejected',False) or not gateway._is_user_authorized_for_source(source):
            return None
        home = Path(gateway._resolve_profile_home_for_source(source)).resolve()
        if home.parent.name != 'profiles' or not home.name.startswith('employee-'):
            return None
        config = json.loads((home/'inspection.json').read_text())
        targets = config.get('notification_targets') or [config.get('notification_target',{})]
        if not config.get('enabled') or not config.get('acknowledgement',{}).get('enabled'):
            return None
        if not any(t.get('platform')==platform and str(t.get('chat_id'))==str(source.chat_id) for t in targets):
            return None
    except Exception:
        return None
    TRUSTED.set((home,*parsed,platform))
    return {'action':'rewrite','text':'/inspection-ack'}


async def command_handler(raw_args):
    context = TRUSTED.get()
    TRUSTED.set(None)
    if not context:
        return '请在绑定的微信或 TG 私聊中回复“已读”。'
    try:
        return await asyncio.to_thread(acknowledge,*context)
    except Exception:
        return '暂未完成已读确认，请稍后重试。'


def register_ack(ctx):
    ctx.register_hook('pre_gateway_dispatch',pre_dispatch)
    ctx.register_command('inspection-ack',command_handler,description='确认巡检提醒已读')

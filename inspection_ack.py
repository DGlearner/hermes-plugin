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
from pathlib import Path

from .profile_inspection import InspectionConfig
from .daily_inspection import fingerprint, report_key


def format_report(report):
    kind = report['report_type']
    subject = (report.get('employee_display_name') or report.get('employee_username')) if kind == 'personal' else (report.get('department_name') or report.get('department_code')) if kind == 'department' else '公司'
    author = report.get('author_display_name') or report.get('author_username') or '未提供'
    title = {'personal': '个人日报', 'department': '部门日报', 'company': '公司日报'}[kind]
    body = '\n'.join(line.lstrip() for line in report['content'].split('\n'))
    return f"1. {title}｜{subject}\n\n日期：{report['report_date']}｜发布人：{author}\n\n正文：\n\n{body}"


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

    def run(self, config, *, force=False):
        if not config.get('enabled') or not config.get('acknowledgement', {}).get('enabled'):
            return {'status': 'disabled'}
        try:
            with ledger(self.home, blocking=False) as db:
                return self._tick(db, config, force)
        except BlockingIOError:
            return {'status': 'busy'}

    def _tick(self, db, config, force):
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
                put_meta(db, 'last_checked_at', now)
                put_meta(db, 'query_ok', 1)
                put_meta(db, 'query_failures', 0)
                put_meta(db, 'query_retry_at', 0)
            result = {'status': 'checked', 'queried': True, 'report_count': len(reports), 'checked_at': now}
        # Never repeat cached data after a failed query or beyond one polling interval.
        last = get_meta(db, 'last_checked_at')
        if get_meta(db, 'query_ok') != '1' or not last or now-float(last) > schedule.interval_minutes*60:
            return result
        unread = db.execute("SELECT * FROM notices WHERE state='unread' ORDER BY id").fetchall()
        result['unread_count'] = len(unread)
        if schedule.in_quiet_hours(now):
            result['status'] = 'quiet_deferred'
            return result
        emitted = []
        for row in unread:
            if row['next_send'] > now:
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
                    message = f"提醒编号：R{row['id']}\n\n{summary.strip()}\n\n{format_report(report)}\n\n回复“已读 R{row['id']}”停止该条提醒；也可回复“全部已读”或“稍后提醒 R{row['id']}”。"
                    with db:
                        db.execute('UPDATE notices SET message=? WHERE id=?', (message, row['id']))
                except Exception:
                    with db:
                        db.execute('UPDATE notices SET summary_attempts=summary_attempts+1,next_send=? WHERE id=?', (now+interval, row['id']))
                    continue
            occurrence = row['round']+1
            # Claim before external I/O. Restart will wait for the next intentional
            # reminder occurrence, rather than replay an uncertain send immediately.
            with db:
                db.execute('UPDATE notices SET round=?,next_send=?,sent_day=?,sent_today=? WHERE id=?', (occurrence, now+interval, day, row['sent_today']+1 if row['sent_day']==day else 1, row['id']))
            try:
                outcomes = self.send(self.home, config, message, f"ack-R{row['id']}-round-{occurrence}") or {'delivery':'sent'}
            except Exception as exc:
                outcomes = getattr(exc, 'outcomes', {'delivery':'uncertain'})
            with db:
                db.execute('UPDATE notices SET last_outcomes=? WHERE id=?', (json.dumps(outcomes), row['id']))
            emitted.append({'notice': f"R{row['id']}", 'channels': outcomes})
        result['reminders'] = emitted
        if config.get('test_notify_each_check') and result['queried']:
            when = schedule.localize(now).strftime('%m-%d %H:%M:%S')
            message = f"【后台巡检测试】{when}\n查询完成：可见日报 {result['report_count']} 条，未读 {result['unread_count']} 条。\n未读正文按 {interval//60} 分钟间隔提醒；本条执行回执无需确认。"
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
    if not token and action not in {'全部已读','未读提醒'}:
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
        elif action != '全部已读' and len(ids)>1:
            return '有多条未读提醒，请回复“已读 R编号”或“全部已读”：'+ '、'.join(f'R{i}' for i in ids)
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
        return f'{names} 已确认已读，微信和 TG 均停止该版本的重复提醒；后台继续检索新内容。'


def pre_dispatch(event=None, gateway=None, **kwargs):
    TRUSTED.set(None)
    if event is None or gateway is None or getattr(event,'internal',False):
        return None
    parsed = parse_command(getattr(event,'text',''), getattr(event,'reply_to_text','') or '')
    if not parsed:
        return None
    source = event.source
    platform = getattr(source.platform,'value',source.platform)
    if platform not in {'weixin','telegram'} or source.chat_type != 'dm' or str(source.user_id) != str(source.chat_id):
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
        return '请在绑定的微信或 TG 私聊中回复“已读 R编号”。'
    try:
        return await asyncio.to_thread(acknowledge,*context)
    except Exception:
        return '暂未完成已读确认，请稍后重试。'


def register_ack(ctx):
    ctx.register_hook('pre_gateway_dispatch',pre_dispatch)
    ctx.register_command('inspection-ack',command_handler,description='确认巡检提醒已读')

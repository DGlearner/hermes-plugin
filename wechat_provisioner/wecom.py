"""Employee-owned WeCom binding codes, claimed only by verified callback messages."""
from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import SecretStr, BaseModel, Field
from typing import Literal

from .contracts import WechatQrRequest
from .telegram import channel_lock
from .errors import ProvisioningError


class WecomRequest(WechatQrRequest):
    session_id: UUID | None = None
    personal_token: SecretStr | None = None


class WecomCallbackRequest(BaseModel):
    method: Literal['GET', 'POST']
    query: dict[str, str]
    body: str = Field(default='', max_length=65536)


def wecom_routes(control):
    return [r for r in control._read_gateway_config().get('gateway', {}).get('profile_routes', [])
            if r.get('platform') == 'wecom_callback' and r.get('enabled', True)]


class WecomBindings:
    def __init__(self, control, *, clock=time.time):
        self.control, self.clock = control, clock
        self.path = control._home / 'provisioner' / 'wecom.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS links (
                id TEXT PRIMARY KEY, employee TEXT NOT NULL, username TEXT NOT NULL,
                display_name TEXT NOT NULL, code_hash TEXT NOT NULL UNIQUE,
                expires REAL NOT NULL, state TEXT NOT NULL, user_id TEXT, user_name TEXT)''')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def status(self, request):
        routes = [r for r in wecom_routes(self.control) if r.get('profile') == request.profile_name]
        return {'status': 'bound' if routes else 'unbound',
                'account_hint': routes[0]['chat_id'] if routes else None}

    def settings(self):
        return self.control._read_gateway_config().get('wecom_binding') or {}

    def start(self, request):
        with channel_lock(self.control._home):
            current = self.status(request)
            if current['status'] == 'bound':
                return current
            config = self.settings()
            corp = str(config.get('corp_id') or '')
            if not config.get('enabled') or not re.fullmatch(r'[A-Za-z0-9_-]{3,128}', corp):
                raise ProvisioningError('wecom_unavailable', '企业微信应用尚未配置，请联系管理员。', status_code=503)
            code, sid = secrets.token_hex(12).upper(), str(uuid4())
            with self.db() as db:
                db.execute('DELETE FROM links WHERE expires < ?', (self.clock() - 86400,))
                db.execute("UPDATE links SET state='expired' WHERE employee=? AND state IN ('waiting','confirmed')", (str(request.employee_id),))
                db.execute('INSERT INTO links VALUES (?,?,?,?,?,?,?,NULL,NULL)',
                           (sid, str(request.employee_id), request.username, request.display_name,
                            hashlib.sha256((corp + ':' + code).encode()).hexdigest(), self.clock() + 300, 'waiting'))
            return {'status': 'waiting', 'session_id': sid, 'expires_in': 300,
                    'binding_code': code}

    def claim(self, code, user_id, user_name):
        config = self.settings()
        corp = str(config.get('corp_id') or '')
        prefix = corp + ':'
        if (not config.get('enabled') or not corp or not str(user_id).startswith(prefix)
                or not re.fullmatch(r'[A-Za-z0-9_@.=-]{1,128}', str(user_id)[len(prefix):])
                or not re.fullmatch(r'[A-F0-9]{24}', code)):
            return False
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            cursor = db.execute("UPDATE links SET state='confirmed',user_id=?,user_name=? WHERE code_hash=? AND state='waiting' AND expires>?",
                                (str(user_id), str(user_name)[:100], hashlib.sha256((corp + ':' + code).encode()).hexdigest(), self.clock()))
            return cursor.rowcount == 1

    def poll(self, request):
        with channel_lock(self.control._home):
            with self.db() as db:
                row = db.execute('SELECT * FROM links WHERE id=? AND employee=?',
                                 (str(request.session_id), str(request.employee_id))).fetchone()
            if not row:
                raise ProvisioningError('link_not_found', '绑定码不存在，请重新生成。', status_code=404)
            if row['state'] == 'complete':
                return self.status(request)
            if row['expires'] <= self.clock() or row['state'] == 'expired':
                return {'status': 'expired'}
            if row['state'] != 'confirmed':
                return {'status': row['state']}
            settings = self.settings()
            if not settings.get('enabled') or not row['user_id'].startswith(str(settings.get('corp_id') or '') + ':'):
                raise ProvisioningError('wecom_unavailable', '企业微信配置已变更，请重新生成绑定码。', status_code=503)
            routes = wecom_routes(self.control)
            if any(r.get('chat_id') == row['user_id'] and r.get('profile') != request.profile_name for r in routes):
                raise ProvisioningError('wecom_already_bound', '这个 企业微信 帐号已绑定其他员工，请先解绑。', status_code=409)
            if any(r.get('profile') == request.profile_name and r.get('chat_id') != row['user_id'] for r in routes):
                raise ProvisioningError('employee_already_bound', '你已绑定其他 企业微信 帐号，请先解绑。', status_code=409)
            home = self.control._profile_home(request.profile_name)
            existing_pat = self.control._profile_secret_values(home, ('MCP_COMPANY_MCP_API_KEY',)).get('MCP_COMPANY_MCP_API_KEY') if home.exists() else None
            if not existing_pat and not request.personal_token:
                return {'status': 'needs_credential'}
            if request.personal_token and not request.personal_token.get_secret_value().startswith('ragmcp_'):
                raise ProvisioningError('invalid_credential', '员工凭据格式错误。')
            self.control.ensure_employee_profile(request)
            if not existing_pat:
                self.control.set_profile_pat(request.profile_name, request.personal_token.get_secret_value())
            self._grant(request.profile_name, row['user_id'], row['user_name'])
            config = self.control._read_gateway_config()
            all_routes = config.setdefault('gateway', {}).setdefault('profile_routes', [])
            all_routes[:] = [r for r in all_routes if not (r.get('platform') == 'wecom_callback' and r.get('profile') == request.profile_name)]
            all_routes.append({'name': 'rag-mcp-wecom-' + request.employee_id.hex, 'platform': 'wecom_callback',
                               'chat_id': row['user_id'], 'profile': request.profile_name})
            self.control._write_gateway_config(config)
            with self.db() as db:
                db.execute("UPDATE links SET state='complete' WHERE id=?", (row['id'],))
            return {**self.status(request), 'credential_installed': not bool(existing_pat)}

    def _grant(self, profile, user_id, user_name):
        from gateway.pairing import PairingStore
        for name, home in [(profile, self.control._profile_home(profile)), ('default', self.control._home)]:
            with self.control._profile_scope(home):
                store = PairingStore(profile=name)
                with store._lock:
                    store._approve_user('wecom_callback', user_id, user_name)

    def revoke(self, request):
        with channel_lock(self.control._home):
            config = self.control._read_gateway_config()
            routes = config.setdefault('gateway', {}).setdefault('profile_routes', [])
            removed = [r for r in routes if r.get('platform') == 'wecom_callback' and r.get('profile') == request.profile_name]
            # Remove routing first: the adapter fails closed on an unbound user even
            # if an old process retains an env allowlist or a pairing grant.
            routes[:] = [r for r in routes if r not in removed]
            self.control._write_gateway_config(config)
            with self.db() as db:
                db.execute("UPDATE links SET state='expired' WHERE employee=?", (str(request.employee_id),))
            from gateway.pairing import PairingStore
            for route in removed:
                for name, home in [(request.profile_name, self.control._profile_home(request.profile_name)), ('default', self.control._home)]:
                    with self.control._profile_scope(home):
                        PairingStore(profile=name).revoke('wecom_callback', route['chat_id'])
            return {'status': 'unbound'}

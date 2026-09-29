"""Force WeCom API requests through an authenticated Tailscale-only ECS relay."""
from __future__ import annotations

import ipaddress
from pathlib import Path
from types import MethodType
from urllib.parse import parse_qs, urlsplit

import httpx
import yaml


def relay_settings(root):
    from wechat_provisioner.settings import ProvisionerSettings
    config = yaml.safe_load((Path(root) / 'config.yaml').read_text()) or {}
    url = str((config.get('wecom_binding') or {}).get('relay_url') or '').rstrip('/')
    parsed = urlsplit(url)
    try:
        valid = (parsed.scheme == 'http' and not parsed.username and not parsed.password
                 and parsed.path == '' and not parsed.query and not parsed.fragment
                 and parsed.port is not None
                 and ipaddress.ip_address(parsed.hostname or '') in ipaddress.ip_network('100.64.0.0/10'))
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeError('wecom_private_relay_not_configured')
    return url, ProvisionerSettings.from_env().token


class RelayHTTPClient:
    def __init__(self, original, url, token):
        self.original, self.url, self.token = original, url, token

    async def aclose(self):
        await self.original.aclose()

    async def request(self, method, url, *, params=None, json=None):
        parsed = urlsplit(str(url))
        if parsed.scheme != 'https' or parsed.netloc != 'qyapi.weixin.qq.com' or parsed.fragment:
            raise RuntimeError('wecom_relay_destination_rejected')
        query = parse_qs(parsed.query, strict_parsing=True)
        if any(len(v) != 1 for v in query.values()):
            raise RuntimeError('wecom_relay_query_rejected')
        fields = {k: v[0] for k, v in query.items()}
        if set(fields) & set(params or {}):
            raise RuntimeError('wecom_relay_duplicate_query')
        fields.update(params or {})
        if method == 'GET' and parsed.path == '/cgi-bin/gettoken' and set(fields) == {'corpid', 'corpsecret'}:
            operation, body = 'gettoken', fields
        elif method == 'POST' and parsed.path == '/cgi-bin/message/send' and set(fields) == {'access_token'}:
            operation, body = 'send', {'access_token': fields['access_token'], 'message': json}
        else:
            raise RuntimeError('wecom_relay_operation_rejected')
        # A separate short-lived client avoids inherited Mac proxy settings.
        # Failure is returned to Hermes; there is deliberately no direct fallback.
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
            response = await client.post(self.url + '/internal/hermes/wecom-api/' + operation,
                headers={'Authorization': 'Bearer ' + self.token}, json=body)
        if response.status_code != 200:
            raise RuntimeError('wecom_relay_unavailable')
        return response

    async def get(self, url, *, params=None):
        return await self.request('GET', url, params=params)

    async def post(self, url, *, params=None, json=None):
        return await self.request('POST', url, params=params, json=json)


def enable_adapter_relay(adapter, root):
    if getattr(adapter, '_employee_ecs_relay_installed', False):
        return
    original_refresh = adapter._refresh_access_token

    async def refresh(self, app):
        # connect() creates/recreates the native client before initial refresh.
        if not isinstance(self._http_client, RelayHTTPClient):
            url, token = relay_settings(root)
            self._http_client = RelayHTTPClient(self._http_client, url, token)
        return await original_refresh(app)

    adapter._refresh_access_token = MethodType(refresh, adapter)
    adapter._employee_ecs_relay_installed = True

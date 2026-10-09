"""Bind verified WeCom callback senders before model/session dispatch."""
from __future__ import annotations
import asyncio
import re
from functools import wraps

from .wecom_display import render_wecom_text


def split_reply(content, limit=2000):
    """Preserve all text while staying below WeCom's 2048 UTF-8 byte cap."""
    parts = []
    while content:
        size = 0
        end = 0
        for char in content:
            size += len(char.encode('utf-8'))
            if size > limit:
                break
            end += 1
        if end < len(content):
            newline = content.rfind('\n', 0, end)
            if newline >= end // 2:
                end = newline + 1
        parts.append(content[:end])
        content = content[end:]
    return parts or ['']


def patch_reply_delivery(cls):
    if getattr(cls, '_employee_wecom_complete_replies', False):
        return
    original = cls.send

    @wraps(original)
    async def send(adapter, chat_id, content, reply_to=None, metadata=None):
        for part in split_reply(render_wecom_text(content)):
            result = await original(adapter, chat_id, part, reply_to=reply_to, metadata=metadata)
            if not result.success:
                return result
        return result

    cls.send = send
    cls._employee_wecom_complete_replies = True


def patch_adapter(cls, control, bindings):
    if getattr(cls, '_employee_wecom_binding_installed', False):
        return
    original = cls.handle_message

    @wraps(original)
    async def handle(adapter, event):
        from wechat_provisioner.wecom import wecom_routes
        source = event.source
        platform = getattr(source.platform, 'value', source.platform)
        if platform != 'wecom_callback' or source.chat_type != 'dm':
            return
        settings = bindings.settings()
        corp = str(settings.get('corp_id') or '')
        chat = str(source.chat_id)
        if not settings.get('enabled') or not corp or chat != corp + ':' + str(source.user_id):
            return
        text = str(event.text or '').strip()
        match = re.fullmatch(r'(?:绑定|/bind)\s+([A-Fa-f0-9]{24})', text)
        if match:
            claimed = await asyncio.to_thread(bindings.claim, match[1].upper(), chat, source.user_name or '')
            await adapter.send(chat, '已确认企业微信身份，请返回帐号设置页面完成绑定。' if claimed
                               else '绑定码已失效或已使用，请返回帐号设置重新生成。')
            return
        # Binding-code-like messages must never enter conversation logs/models.
        if text.startswith(('绑定 ', '/bind ')):
            return
        routes = wecom_routes(control)
        route = next((r for r in routes if r.get('chat_id') == chat and r.get('platform') == 'wecom_callback'), None)
        if route is None:
            return
        # Scope the principal to the corporation, not just its bare member ID.
        source.user_id = chat
        # Handle inspection receipts at the adapter boundary. This keeps the
        # receipt path independent of model/session dispatch and guarantees the
        # same durable ledger is updated for WeCom, TG and Weixin.
        from .inspection_ack import parse_command, acknowledge
        parsed = parse_command(text, getattr(event, 'reply_to_text', '') or '')
        if parsed:
            profile = str(route.get('profile') or '')
            home = control._home / 'profiles' / profile
            action, token = parsed
            if (home / 'inspection.json').exists():
                reply = await asyncio.to_thread(acknowledge, home, action, token, 'wecom_callback')
                await adapter.send(chat, reply)
                return
        return await original(adapter, event)

    cls.handle_message = handle
    cls._employee_wecom_binding_installed = True


def install_wecom_binding():
    from gateway.run import GatewayRunner
    if getattr(GatewayRunner, '_wecom_binding_factory_installed', False):
        return
    original = GatewayRunner._create_adapter

    @wraps(original)
    def create(runner, platform, config):
        adapter = original(runner, platform, config)
        if adapter is not None and getattr(platform, 'value', platform) == 'wecom_callback':
            from wechat_provisioner.hermes_control import InstalledHermesControl
            from wechat_provisioner.settings import ProvisionerSettings
            from wechat_provisioner.wecom import WecomBindings
            control = InstalledHermesControl(ProvisionerSettings.from_env(require_token=False))
            patch_adapter(type(adapter), control, WecomBindings(control))
            patch_reply_delivery(type(adapter))
            from .wecom_egress import enable_adapter_relay
            enable_adapter_relay(adapter, control._home)
        return adapter

    GatewayRunner._create_adapter = create
    GatewayRunner._wecom_binding_factory_installed = True


def send_notification(root, target, message):
    """Standalone proactive send; never start a second callback HTTP listener."""
    from .work_todos import profile_scope
    from gateway.config import load_gateway_config, Platform
    from plugins.platforms.wecom.callback_adapter import WecomCallbackAdapter
    with profile_scope(root):
        config = load_gateway_config().platforms.get(Platform.WECOM_CALLBACK)
    if config is None or not config.enabled:
        raise RuntimeError('wecom_unavailable')
    adapter = WecomCallbackAdapter(config)
    corp, separator, member = target['chat_id'].partition(':')
    apps = [app for app in adapter._apps if app.get('corp_id') == corp]
    if not separator or not member or len(apps) != 1:
        raise RuntimeError('wecom_ambiguous_destination')

    async def send():
        import httpx
        from .wecom_egress import RelayHTTPClient, relay_settings
        async with httpx.AsyncClient(timeout=20) as client:
            relay_url, relay_token = relay_settings(root)
            relay = RelayHTTPClient(client, relay_url, relay_token)
            adapter._http_client = relay
            token = await adapter._get_access_token(apps[0])
            response = await relay.post('https://qyapi.weixin.qq.com/cgi-bin/message/send',
                params={'access_token': token}, json={'touser': member, 'msgtype': 'markdown',
                    'agentid': int(apps[0]['agent_id']), 'markdown': {'content': str(message)}})
            data = response.json()
            if response.status_code != 200 or data.get('errcode') != 0 or data.get('invaliduser'):
                raise RuntimeError('wecom_send_rejected')
    try:
        asyncio.run(send())
    except Exception:
        raise RuntimeError('wecom_delivery_failed') from None

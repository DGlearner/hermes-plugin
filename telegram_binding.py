"""Intercept binding /start before authorization, logging, batching and model dispatch."""
from __future__ import annotations

import asyncio
from functools import wraps


def install_telegram_binding():
    from gateway.run import GatewayRunner
    if getattr(GatewayRunner, '_telegram_binding_factory_installed', False):
        return False
    factory = GatewayRunner._create_adapter

    @wraps(factory)
    def create(runner, platform, config):
        adapter = factory(runner, platform, config)
        if adapter is not None and getattr(platform, 'value', platform) == 'telegram':
            _patch_adapter(type(adapter))
        return adapter

    GatewayRunner._create_adapter = create
    GatewayRunner._telegram_binding_factory_installed = True
    return True


def _patch_adapter(TelegramAdapter):
    from wechat_provisioner.telegram import TelegramBindings, telegram_routes
    from wechat_provisioner.hermes_control import InstalledHermesControl
    from wechat_provisioner.settings import ProvisionerSettings
    if getattr(TelegramAdapter, '_employee_binding_installed', False):
        return False
    control = InstalledHermesControl(ProvisionerSettings.from_env(require_token=False))
    bindings = TelegramBindings(control)
    _patch_media_group_dispatch(TelegramAdapter)
    original = TelegramAdapter._handle_command
    auth = TelegramAdapter._is_user_authorized_from_message

    @wraps(original)
    async def command(adapter, update, context):
        msg = adapter._effective_update_message(update)
        text = str(getattr(msg, 'text', '') or '')
        parts = text.split(maxsplit=1)
        if parts and parts[0].split('@')[0] == '/start' and len(parts) == 2 and parts[1].startswith('bind_'):
            user, chat = getattr(msg, 'from_user', None), getattr(msg, 'chat', None)
            if not user or getattr(user, 'is_bot', True) or getattr(chat, 'type', '') != 'private' or chat.id != user.id:
                return
            claimed = await asyncio.to_thread(bindings.claim, parts[1][5:], user.id, getattr(user, 'username', '') or '')
            await msg.reply_text('已确认 Telegram 帐号，请返回帐号设置页面完成连接。' if claimed else '绑定链接已使用或已过期，请返回帐号设置重新生成。')
            from telegram.ext import ApplicationHandlerStop
            raise ApplicationHandlerStop
        return await original(adapter, update, context)

    @wraps(auth)
    def authorized(adapter, msg):
        try:
            user, chat = getattr(msg, 'from_user', None), getattr(msg, 'chat', None)
            if not user or not chat or str(chat.type) != 'private' or chat.id != user.id:
                return False
            if not any(r.get('chat_id') == str(chat.id) for r in telegram_routes(control)):
                return False
        except Exception:
            return False
        return auth(adapter, msg)

    TelegramAdapter._handle_command = command
    TelegramAdapter._is_user_authorized_from_message = authorized
    TelegramAdapter._employee_binding_installed = True
    import logging
    logging.getLogger(__name__).info('Telegram employee self-service binding installed')
    return True


def _patch_media_group_dispatch(adapter_class):
    """A late album item may cancel a debounce timer, never an in-flight dispatch."""
    if getattr(adapter_class, "_employee_media_dispatch_installed", False):
        return
    original = getattr(adapter_class, "_queue_media_group_event", None)
    if original is None:
        return

    @wraps(original)
    async def queue(adapter, media_group_id, event):
        # flush pops the buffered event before awaiting handle_message. There is
        # no await between this check and the original enqueue's cancellation.
        if media_group_id not in adapter._media_group_events:
            adapter._media_group_tasks.pop(media_group_id, None)
        return await original(adapter, media_group_id, event)

    adapter_class._queue_media_group_event = queue
    adapter_class._employee_media_dispatch_installed = True

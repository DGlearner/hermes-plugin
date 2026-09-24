"""Attachment receipt and queue regressions; no provider or outbound messaging."""
import asyncio
import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import patch

import pytest


def load_plugin():
    path = Path(__file__).parents[1] / '__init__.py'
    spec = importlib.util.spec_from_file_location('delivery_test_plugin', path, submodule_search_locations=[str(path.parent)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    mod._process_pending_attachments()[1].clear()
    return mod


@pytest.mark.parametrize('platform', ['telegram', 'weixin'])
@pytest.mark.parametrize('thread_id', [None, '2277'])
def test_six_files_survive_lost_memory_and_are_session_scoped(tmp_path, monkeypatch, platform, thread_id):
    p = load_plugin()
    home = tmp_path / 'profiles/employee-test'; home.mkdir(parents=True)
    cache = tmp_path / 'cache/documents'; cache.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv(p.ATTACHMENT_ROOTS_ENV, str(cache))
    monkeypatch.setenv(p.ATTACHMENT_SHARED_ROOT_ENV, str(cache))
    monkeypatch.setattr(p, '_profile_name', lambda: 'employee-test')
    monkeypatch.setattr(p, '_current_profile_home', lambda: home)
    source = NS(platform=platform, user_id='123', chat_id='123', profile='employee-test',
                chat_type='dm', thread_id=thread_id)
    files = []
    for i in range(6):
        f = cache / f'doc_{i:012x}_test{i}.md'; f.write_text('test attachment')
        files.append(str(f))
        event = NS(media_urls=[str(f)])
        p._capture_pending_attachments(event, source, 'session-a')
        p._capture_pending_attachments(event, source, 'session-a')  # redispatch must be idempotent
    p._process_pending_attachments()[1].clear()  # cancellation/restart before pre_llm
    other = 'weixin' if platform == 'telegram' else 'telegram'
    p._on_pre_llm_call(session_id='session-a', platform=other, sender_id='123', user_message='hello')
    assert not p._load_session_grants('session-a')
    # Remove the test-only foreign turn before the legitimate platform's turn.
    state_path = home / p.ATTACHMENT_STORE_FILE
    import json
    state = json.loads(state_path.read_text()); state['sessions'].pop('session-a', None)
    state_path.write_text(json.dumps(state))
    p._on_pre_llm_call(session_id='session-b', platform=platform, sender_id='123', user_message='hello')
    assert not p._load_session_grants('session-b')
    result = p._on_pre_llm_call(session_id='session-a', platform=platform, sender_id='123', user_message='analyze files')
    assert len(p._load_session_grants('session-a')) == 6
    assert all(f in result['context'] for f in files)
    handler = p._analyze_attachment_handler(p.DEFAULT_ENDPOINT, 5)
    assert json.loads(handler({'attachment_path': files[2]}, session_id='session-a'))['text'] == 'test attachment'
    assert json.loads(handler({'attachment_path': files[2]}, session_id='session-b'))['error_type'] == 'attachment_missing'
    other_home = tmp_path / 'profiles/employee-other'; other_home.mkdir()
    monkeypatch.setattr(p, '_profile_name', lambda: 'employee-other')
    monkeypatch.setattr(p, '_current_profile_home', lambda: other_home)
    assert not p._load_session_grants('session-a')


def test_media_dispatch_not_cancelled_but_debounce_is():
    from telegram_binding import _patch_media_group_dispatch
    class Adapter:
        async def _queue_media_group_event(self, group, event):
            prior = self._media_group_tasks.get(group)
            if prior:
                prior.cancel()
    _patch_media_group_dispatch(Adapter)
    async def run():
        a = Adapter(); a._media_group_events = {}; a._media_group_tasks = {}
        dispatch = asyncio.create_task(asyncio.sleep(100))
        await asyncio.sleep(0)
        a._media_group_tasks['album'] = dispatch
        await a._queue_media_group_event('album', None)
        assert not dispatch.cancelling()
        timer = asyncio.create_task(asyncio.sleep(100))
        a._media_group_tasks['album'] = timer; a._media_group_events['album'] = object()
        await a._queue_media_group_event('album', None)
        assert timer.cancelling()
        dispatch.cancel()
        await asyncio.gather(dispatch, timer, return_exceptions=True)
    asyncio.run(run())


def test_attachment_queue_keeps_authorization_controls_and_text_behavior(monkeypatch):
    p = load_plugin()
    class Runner:
        def _session_key_for_source(self, source): return source.user_id
        def _adapter_for_source(self, source): return NS(_pending_messages={})
        def _effective_busy_input_mode(self, source): return 'interrupt'
        async def _handle_active_session_busy_message(self, event, key):
            if not event.authorized: return 'denied'
            if event.text == '/stop': return 'stopped'
            return self._effective_busy_input_mode(event.source)
        async def _handle_message(self, event):
            trailing = NS(source=event.source, media_urls=[], text='analyze', authorized=True)
            assert await self._handle_active_session_busy_message(trailing, event.source.user_id) == 'queue'
    module = ModuleType('gateway.run'); module.GatewayRunner = Runner
    monkeypatch.setitem(sys.modules, 'gateway.run', module)
    p._install_attachment_queue_policy()
    async def run():
        r = Runner()
        for platform in ('telegram', 'weixin'):
            event = NS(source=NS(platform=platform, user_id='123'), media_urls=['file'], text='', authorized=True)
            assert await r._handle_active_session_busy_message(event, '123') == 'queue'
            await r._handle_message(event)
            assert not r._profile_attachment_active
            event.authorized = False
            assert await r._handle_active_session_busy_message(event, '123') == 'denied'
            event.authorized = True; event.text = '/stop'
            assert await r._handle_active_session_busy_message(event, '123') == 'stopped'
            event.media_urls = []; event.text = 'hello'
            assert await r._handle_active_session_busy_message(event, '123') == 'interrupt'
    asyncio.run(run())


def test_real_telegram_late_album_items_preserve_all_six():
    path = Path('/opt/hermes/plugins/platforms/telegram/adapter.py')
    if not path.exists():
        pytest.skip('requires deployed Hermes adapter')
    spec = importlib.util.spec_from_file_location('telegram_delivery_runtime', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    from telegram_binding import _patch_media_group_dispatch
    cls = module.TelegramAdapter
    _patch_media_group_dispatch(cls)
    async def run():
        a = object.__new__(cls)
        a._media_group_events = {}; a._media_group_tasks = {}
        a.MEDIA_GROUP_WAIT_SECONDS = 0
        a._should_drop_delayed_delivery = lambda: False
        held = []; a._hold_inbound_event = lambda event, **kw: held.append(event)
        started = asyncio.Event(); release = asyncio.Event(); received = []
        async def handle(event):
            started.set()
            await release.wait()
            received.extend(event.media_urls)
        a.handle_message = handle
        event = lambda i: NS(media_urls=[str(i)], media_types=['document'], text='')
        await a._queue_media_group_event('album', event(0))
        first = a._media_group_tasks['album']
        await started.wait()
        for i in range(1, 6):
            await a._queue_media_group_event('album', event(i))
        last = a._media_group_tasks['album']
        assert not first.cancelling()
        release.set()
        await asyncio.gather(first, last)
        assert sorted(received) == [str(i) for i in range(6)]
        assert not held
    asyncio.run(run())

def test_media_first_telegram_capture_does_not_require_existing_session(tmp_path, monkeypatch):
    p = load_plugin()
    home = tmp_path / 'profiles/employee-test'; home.mkdir(parents=True)
    cache = tmp_path / 'cache/documents'; cache.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv(p.ATTACHMENT_ROOTS_ENV, str(cache))
    monkeypatch.setenv(p.ATTACHMENT_SHARED_ROOT_ENV, str(cache))
    monkeypatch.setattr(p, '_profile_name', lambda: 'employee-test')
    monkeypatch.setattr(p, '_current_profile_home', lambda: home)
    source = NS(platform='telegram', user_id='7094514345', chat_id='7094514345', profile='employee-test')
    file = cache / 'doc_abcdefabcdef_telegram.md'; file.write_text('telegram attachment smoke test')
    event = NS(media_urls=[str(file)])
    grants = p._capture_pending_attachments(event, source, '')
    assert str(file) not in grants
    pending = p._process_pending_attachments()[1]
    assert ('employee-test', 'telegram:7094514345') in pending
    # Profile-local media is normalized into the shared spool before grant
    # creation, so assert the identity by basename rather than the pre-normalized
    # path supplied by the adapter.
    assert any(Path(item.get('path', '')).name == file.name for item in pending[('employee-test', 'telegram:7094514345')])
    result = p._on_pre_llm_call(session_id='telegram-session', platform='telegram', sender_id='7094514345', user_message='分析这个文件')
    assert result and len(p._load_session_grants('telegram-session')) == 1


def test_telegram_capture_uses_gateway_profile_route_before_source_is_stamped(tmp_path, monkeypatch):
    p = load_plugin()
    home = tmp_path / 'profiles/employee-routed'; home.mkdir(parents=True)
    cache = tmp_path / 'cache/documents'; cache.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv(p.ATTACHMENT_ROOTS_ENV, str(cache))
    monkeypatch.setenv(p.ATTACHMENT_SHARED_ROOT_ENV, str(cache))
    monkeypatch.setattr(p, '_current_profile_home', lambda: home)
    source = NS(platform='telegram', user_id='7094514345', chat_id='7094514345')
    file = cache / 'doc_route_telegram.md'; file.write_text('routed telegram attachment')
    p._capture_pending_attachments(event=NS(media_urls=[str(file)]), source=source,
                                   profile_hint='employee-routed')
    pending = p._process_pending_attachments()[1]
    assert ('employee-routed', 'telegram:7094514345') in pending


def test_telegram_private_topic_real_gateway_capture_to_tools(tmp_path, monkeypatch):
    """Replay the failed DM-topic envelope through the installed Gateway hook."""
    import json
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from gateway.config import Platform

    p = load_plugin()
    home = tmp_path / 'profiles/employee-test'; home.mkdir(parents=True)
    cache = tmp_path / 'cache/documents'; cache.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv(p.ATTACHMENT_ROOTS_ENV, str(cache))
    monkeypatch.setenv(p.ATTACHMENT_SHARED_ROOT_ENV, str(cache))
    monkeypatch.setattr(p, '_profile_name', lambda: 'employee-test')
    monkeypatch.setattr(p, '_current_profile_home', lambda: home)
    source = SessionSource(platform=Platform.TELEGRAM, user_id='123', chat_id='123',
                           chat_type='dm', thread_id='2277', profile='employee-test')
    file = cache / 'doc_919edf46bb53_git.md'; file.write_text('attachment regression fixture')
    event = NS(media_urls=[str(file)], text='analyze')
    runner = object.__new__(GatewayRunner)
    runner.session_store = NS(peek_session_id=lambda key: 'topic-session')
    async def prepare(runner, **kwargs):
        # Registration must have completed before the ordinary text preparation.
        assert p._process_pending_attachments()[1]
        return kwargs['event'].text
    monkeypatch.setattr(GatewayRunner, '_prepare_inbound_message_text', prepare)
    monkeypatch.setattr(GatewayRunner, p.ATTACHMENT_CAPTURE_PATCH_ATTR, False, raising=False)
    p._install_attachment_capture()
    asyncio.run(runner._prepare_inbound_message_text(event=event, source=source,
                history=[], session_key='agent:employee-test:telegram:dm:123:2277'))
    p._process_pending_attachments()[1].clear()  # prove durable handoff works too
    p._on_pre_llm_call(session_id='other-topic', platform='telegram', sender_id='123', user_message='hello')
    assert not p._load_session_grants('other-topic')
    p._on_pre_llm_call(session_id='topic-session', platform='telegram', sender_id='123', user_message='analyze')
    analyze = p._analyze_attachment_handler(p.DEFAULT_ENDPOINT, 5)
    for args in ({}, {'attachment_path': str(file)}):
        assert json.loads(analyze(args, session_id='topic-session'))['text'] == 'attachment regression fixture'
    # Stop at the network boundary: authorization must reach upload preparation,
    # without creating a real knowledge entry or sending anything to OSS.
    seen = []
    def prepare_upload(**kwargs):
        seen.append(kwargs['grant'].path)
        return None, p._error('test network boundary', error_type='test_boundary')
    monkeypatch.setattr(p, '_prepare_attachment_upload', prepare_upload)
    upload = p._upload_attachment_handler(p.DEFAULT_ENDPOINT, 5)
    assert json.loads(upload({'attachment_path': str(file), 'visibility': 'personal'},
                           session_id='topic-session'))['error_type'] == 'test_boundary'
    assert seen == [str(file)]


@pytest.mark.parametrize('chat_id,chat_type,thread_id,session_id', [
    ('-100123', 'group', '2277', 'group-session'),
    ('123', 'group', None, 'group-session'),
    ('123', 'dm', '2277', ''),
])
def test_telegram_group_and_unresolved_topic_never_use_sender_fallback(
    tmp_path, monkeypatch, chat_id, chat_type, thread_id, session_id,
):
    p = load_plugin()
    source = NS(platform='telegram', user_id='123', chat_id=chat_id,
                chat_type=chat_type, thread_id=thread_id, profile='employee-test')
    assert p._capture_pending_attachments(NS(media_urls=['/untrusted']), source, session_id) == {}
    assert not p._process_pending_attachments()[1]

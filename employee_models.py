"""Two named model routes; Profile preferences contain no credentials.

Activation is deliberately gated until both provider endpoints are verified.
Uses Hermes' native per-request fallback; never replays a completed tool turn.
"""
from __future__ import annotations

from copy import deepcopy
from functools import wraps
import json
import os
from pathlib import Path
import tempfile
import threading

import yaml

SETTINGS = Path(__file__).with_name('employee_models.json')
_lock = threading.RLock()
ALIASES = ('deepseek', 'gpt')


def settings():
    data = json.loads(SETTINGS.read_text())
    if not isinstance(data, dict):
        raise ValueError('invalid model routing settings')
    return data


def validate_routes(data, *, require_credentials=True):
    routes = data.get('routes', {})
    from urllib.parse import urlsplit
    for alias in ALIASES:
        route = routes.get(alias, {})
        parsed = urlsplit(route.get('base_url', ''))
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or not route.get('model')):
            raise ValueError(f'{alias}: endpoint/model not configured')
        expected_env = 'HERMES_DEEPSEEK_API_KEY' if alias == 'deepseek' else 'HERMES_SHARED_MODEL_API_KEY'
        if route.get('api_key_env') != expected_env:
            raise ValueError(f'{alias}: invalid credential reference')
        if require_credentials and not os.environ.get(expected_env, '').strip():
            raise ValueError(f'{alias}: credential not configured')
    if data.get('default') not in ALIASES:
        raise ValueError('invalid default model')
    return routes


def configured_profile(raw, data, alias):
    """Pure, non-secret config transformation; preserve PAT, sessions and permissions."""
    if alias not in ALIASES:
        raise ValueError('unsupported model choice')
    routes = validate_routes(data)
    result = deepcopy(raw)
    providers = result.setdefault('providers', {})
    for name, route in routes.items():
        if name not in ALIASES:
            continue
        provider = 'employee-' + name
        providers[provider] = {
            'name': provider, 'base_url': route['base_url'], 'api_mode': 'chat_completions',
            'api_key_env': route['api_key_env'], 'default_model': route['model'],
        }
    selected = routes[alias]
    backup = routes['gpt' if alias == 'deepseek' else 'deepseek']
    # Replace the complete model route so URL, credentials and capabilities
    # cannot be inherited from the formerly selected provider.
    result['model'] = {'default': selected['model'], 'provider': 'employee-' + alias,
                       'base_url': selected['base_url'], 'api_key_env': selected['api_key_env'],
                       'api_mode': 'chat_completions'}
    result['fallback_providers'] = [{
        'provider': 'custom', 'model': backup['model'], 'base_url': backup['base_url'],
        'api_key_env': backup['api_key_env'], 'api_mode': 'chat_completions',
    }]
    result.pop('fallback_model', None)
    agent = result.setdefault('agent', {})
    # A failed request goes to the alternate instead of repeating five times.
    agent['api_max_retries'] = 1
    agent['reasoning_effort'] = selected.get('reasoning_effort', 'low')
    result['employee_model_routing'] = {'choice': alias, 'version': 1}
    return result


def save_choice(home, data, alias):
    home = Path(home)
    if home.parent.name != 'profiles' or not home.name.startswith('employee-'):
        raise ValueError('unconfirmed employee Profile')
    path = home / 'config.yaml'
    with _lock:
        raw = yaml.safe_load(path.read_text()) or {}
        result = configured_profile(raw, data, alias)
        fd, tmp = tempfile.mkstemp(prefix='.model-config-', dir=home)
        try:
            with os.fdopen(fd, 'w') as f:
                yaml.safe_dump(result, f, allow_unicode=True, sort_keys=False)
                f.flush(); os.fsync(f.fileno())
            os.chmod(tmp, path.stat().st_mode & 0o777)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)


def install_employee_models():
    from gateway import run
    from hermes_cli import config as cli_config, managed_scope
    cls = run.GatewayRunner
    if getattr(cls, '_employee_models_installed', False):
        return False
    original_load = cli_config._load_config_impl
    original_managed = managed_scope.apply_managed_overlay
    original = cls._handle_model_command
    original_override = cls._apply_session_model_override
    original_error = run._gateway_provider_error_reply
    original_fallback = cls._refresh_fallback_model

    def overlay(raw):
        data = settings()
        if not data.get('enabled'):
            return raw
        choice = (raw.get('employee_model_routing') or {}).get('choice', data['default'])
        return configured_profile(raw, data, choice)

    @wraps(original_load)
    def load(*args, **kwargs):
        return overlay(original_load(*args, **kwargs))

    @wraps(original_managed)
    def managed(raw):
        return overlay(original_managed(raw))

    @wraps(original)
    async def command(runner, event):
        data = settings()
        if not data.get('enabled'):
            return await original(runner, event)
        platform = getattr(event.source.platform, 'value', event.source.platform)
        if platform not in {'telegram', 'weixin'}:
            return await original(runner, event)
        home = Path(runner._resolve_profile_home_for_source(event.source))
        args = event.get_command_args().strip().lower()
        if not args:
            raw = yaml.safe_load((home / 'config.yaml').read_text()) or {}
            alias = raw.get('employee_model_routing', {}).get('choice', data['default'])
            return (f'当前首选模型：{alias}。故障时自动尝试另一套模型服务。\n'
                    '/model deepseek — 切换 DeepSeek\n/model gpt — 切换 GPT\n'
                    '选择按员工保存，微信和 Telegram 共用，每日换会话后保留。')
        if args not in ALIASES:
            return '请使用 /model deepseek、/model gpt 或 /model 查看当前选择。'
        try:
            save_choice(home, data, args)
        except (ValueError, OSError):
            return '模型配置尚未就绪，未更改当前选择，请联系管理员检查服务配置。'
        return f'已将首选模型设为 {args}，从下一次请求生效；微信和 Telegram 共用，失败时自动尝试备用服务。'

    @wraps(original_override)
    def apply_override(runner, session_key, model, runtime_kwargs):
        # In managed mode the employee preference is authoritative; stale
        # native /model overrides must not pin one channel to another route.
        if settings().get('enabled'):
            return model, runtime_kwargs
        return original_override(runner, session_key, model, runtime_kwargs)

    @wraps(original_error)
    def error(text):
        if settings().get('enabled'):
            return '模型 API 异常，当前请求未能通过主服务及备用服务完成，请稍后重试；若持续出现，请联系管理员。'
        return original_error(text)

    @wraps(original_fallback)
    def fallback(runner):
        from hermes_cli.config import get_config_path
        from hermes_cli.fallback_config import get_fallback_chain
        if settings().get('enabled'):
            raw = yaml.safe_load(get_config_path().read_text()) or {}
            # Do not store a Profile-specific chain on the shared runner.
            return get_fallback_chain(overlay(raw))
        return original_fallback(runner)

    cli_config._load_config_impl = load
    managed_scope.apply_managed_overlay = managed
    cls._refresh_fallback_model = fallback
    cls._handle_model_command = command
    cls._apply_session_model_override = apply_override
    run._gateway_provider_error_reply = error
    cls._employee_models_installed = True
    return True

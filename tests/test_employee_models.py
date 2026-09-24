import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
import yaml
import employee_models as m


@pytest.fixture
def routes(monkeypatch):
    monkeypatch.setenv('HERMES_SHARED_MODEL_API_KEY', 'test-gpt-key')
    monkeypatch.setenv('HERMES_DEEPSEEK_API_KEY', 'test-deepseek-key')
    return {'enabled':True,'default':'deepseek','routes':{
        'gpt':{'base_url':'https://gpt.example/v1','model':'gpt-test','api_key_env':'HERMES_SHARED_MODEL_API_KEY','reasoning_effort':'low'},
        'deepseek':{'base_url':'https://deepseek.example/v1','model':'deepseek-test','api_key_env':'HERMES_DEEPSEEK_API_KEY'}}}


@pytest.mark.parametrize('alias,backup', [('deepseek','gpt'),('gpt','deepseek')])
def test_complete_route_and_fallback_credentials(routes, alias, backup):
    from hermes_cli.fallback_config import get_fallback_chain, resolve_entry_api_key
    raw={'env':{'MCP_COMPANY_MCP_API_KEY':'employee-pat'},'session_reset':{'daily_hour':4},'approvals':{'deny':['*']},
         'model':{'api_key':'old-key','context_length':99999},'fallback_model':{'provider':'stale'}}
    before=deepcopy(raw)
    out=m.configured_profile(raw,routes,alias)
    assert raw == before
    assert out['env']==raw['env'] and out['session_reset']==raw['session_reset'] and out['approvals']==raw['approvals']
    assert out['model']['base_url']==routes['routes'][alias]['base_url']
    assert 'api_key' not in out['model'] and 'context_length' not in out['model']
    chain=get_fallback_chain(out)
    assert len(chain)==1 and chain[0]['base_url']==routes['routes'][backup]['base_url']
    assert resolve_entry_api_key(chain[0])=='test-'+backup+'-key'
    assert out['agent']['api_max_retries']==1
    assert 'test-gpt-key' not in json.dumps(out)
    assert 'test-deepseek-key' not in json.dumps(out)


def test_employee_choice_persists_without_changing_other_employee(tmp_path,routes):
    homes=[tmp_path/'profiles'/('employee-'+x) for x in ('a','b')]
    for home in homes:
        home.mkdir(parents=True); (home/'config.yaml').write_text('env:\n  MCP_COMPANY_MCP_API_KEY: employee-pat\n')
        (home/'state.db').write_bytes(b'unchanged-session-data')
    b=(homes[1]/'config.yaml').read_bytes()
    m.save_choice(homes[0],routes,'gpt')
    assert yaml.safe_load((homes[0]/'config.yaml').read_text())['employee_model_routing']['choice']=='gpt'
    assert (homes[0]/'state.db').read_bytes()==b'unchanged-session-data'
    assert (homes[1]/'config.yaml').read_bytes()==b


def test_missing_credentials_does_not_change_profile(tmp_path,routes,monkeypatch):
    home=tmp_path/'profiles/employee-a';home.mkdir(parents=True)
    path=home/'config.yaml';path.write_text('model: old\n')
    monkeypatch.delenv('HERMES_DEEPSEEK_API_KEY')
    with pytest.raises(ValueError):m.save_choice(home,routes,'deepseek')
    assert path.read_text()=='model: old\n'


def test_native_fallback_resolves_alternate_key_and_exhausts(routes,monkeypatch):
    from agent import chat_completion_helpers as h, auxiliary_client
    config=m.configured_profile({},routes,'deepseek')
    agent=Mock()
    agent._fallback_chain=config['fallback_providers']; agent._fallback_index=0
    agent._fallback_activated=False;agent._unavailable_fallback_keys=set()
    agent._rate_limited_until=0
    agent.provider='custom';agent.model='deepseek-test';agent.base_url='https://deepseek.example/v1'
    agent._try_activate_fallback=lambda reason=None:h.try_activate_fallback(agent,reason)
    monkeypatch.setattr(h,'_fallback_entry_unavailable_without_network',lambda *a:None)
    resolver=Mock(return_value=(None,None))  # alternate unavailable too
    monkeypatch.setattr(auxiliary_client,'resolve_provider_client',resolver)
    assert not agent._try_activate_fallback()
    assert resolver.call_count==1
    assert resolver.call_args.kwargs['explicit_base_url']=='https://gpt.example/v1'
    assert resolver.call_args.kwargs['explicit_api_key']=='test-gpt-key'
    assert not agent._try_activate_fallback()  # no infinite ping-pong
    assert resolver.call_count==1


def test_commands_shared_profile_and_sanitized_failure(routes,tmp_path,monkeypatch):
    from gateway import run
    from hermes_cli import config, managed_scope
    monkeypatch.setattr(config,"_load_config_impl",config._load_config_impl)
    monkeypatch.setattr(managed_scope,"apply_managed_overlay",managed_scope.apply_managed_overlay)
    home=tmp_path/'profiles/employee-a';home.mkdir(parents=True)
    (home/'config.yaml').write_text('{}')
    monkeypatch.setattr(m,'settings',lambda:routes)
    cls=run.GatewayRunner
    for name in ['_handle_model_command','_apply_session_model_override','_refresh_fallback_model']:
        monkeypatch.setattr(cls,name,getattr(cls,name))
    monkeypatch.setattr(cls,'_employee_models_installed',False,raising=False)
    monkeypatch.setattr(run,'_gateway_provider_error_reply',run._gateway_provider_error_reply)
    monkeypatch.setattr(config,'get_config_path',lambda:home/'config.yaml')
    assert m.install_employee_models()
    runner=object.__new__(cls);runner._resolve_profile_home_for_source=lambda source:home
    async def go():
        for channel,alias in [('weixin','gpt'),('telegram','deepseek')]:
            event=NS(source=NS(platform=channel),get_command_args=lambda:alias)
            reply=await runner._handle_model_command(event)
            assert alias in reply
            assert yaml.safe_load((home/'config.yaml').read_text())['employee_model_routing']['choice']==alias
        event.get_command_args=lambda:'gpt --global'
        assert '请使用' in await runner._handle_model_command(event)
        assert len(runner._refresh_fallback_model())==1
        merged = managed_scope.apply_managed_overlay(yaml.safe_load((home/'config.yaml').read_text()))
        assert merged['model']['default'] == 'deepseek-test'
        assert merged['agent']['api_max_retries'] == 1
        assert runner._apply_session_model_override('old-session','deepseek-test',{})==('deepseek-test',{})
        result=run._gateway_provider_error_reply('API failed with secret test-gpt-key')
        assert '模型 API 异常' in result and 'test-gpt-key' not in result
    asyncio.run(go())


@pytest.mark.parametrize('alias', ['deepseek','gpt'])
def test_native_primary_provider_resolves_matching_url_and_key(routes,alias,monkeypatch):
    from hermes_cli import runtime_provider
    configured=m.configured_profile({},routes,alias)
    monkeypatch.setattr(runtime_provider,'load_config',lambda:configured)
    runtime=runtime_provider.resolve_runtime_provider(requested='employee-'+alias,target_model=routes['routes'][alias]['model'])
    assert runtime['base_url'].rstrip('/')==routes['routes'][alias]['base_url']
    assert runtime['api_key']=='test-'+alias+'-key'


@pytest.mark.parametrize('alias,backup',[('deepseek','gpt'),('gpt','deepseek')])
def test_native_fallback_switches_complete_runtime(routes,alias,backup,monkeypatch):
    from agent import chat_completion_helpers as h, auxiliary_client
    configured=m.configured_profile({},routes,alias)
    a=Mock()
    a._fallback_chain=configured['fallback_providers'];a._fallback_index=0
    a._fallback_activated=False;a._unavailable_fallback_keys=set();a._rate_limited_until=0
    a.provider='custom';a.model=routes['routes'][alias]['model'];a.base_url=routes['routes'][alias]['base_url']
    a._try_activate_fallback=lambda reason=None:h.try_activate_fallback(a,reason)
    a._is_azure_openai_url.return_value=False
    a._primary_runtime={'provider':'custom','model':a.model,'base_url':a.base_url}
    a._credential_pool=None
    a._anthropic_prompt_cache_policy.return_value=(False,False)
    a.context_compressor=None
    a._custom_providers=[]
    a.request_overrides={}
    client=Mock();client.base_url=routes['routes'][backup]['base_url'];client.api_key='test-'+backup+'-key'
    client._custom_headers={};client.default_headers={}
    monkeypatch.setattr(h,'_fallback_entry_unavailable_without_network',lambda *args:None)
    resolver=Mock(return_value=(client,routes['routes'][backup]['model']))
    monkeypatch.setattr(auxiliary_client,'resolve_provider_client',resolver)
    assert a._try_activate_fallback()
    assert a.model==routes['routes'][backup]['model']
    assert a.base_url==routes['routes'][backup]['base_url']
    assert resolver.call_args.kwargs['explicit_api_key']=='test-'+backup+'-key'
    assert configured['employee_model_routing']['choice']==alias

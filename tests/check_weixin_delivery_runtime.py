"""Run inside the Hermes environment; all outbound calls are mocked."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from gateway.platforms import weixin as w
from weixin_delivery import install_weixin_delivery, response_state

async def main():
    install_weixin_delivery()
    adapter=SimpleNamespace(_account_id='test-account',_owner_profile='employee-test',
        _send_session=object(),_base_url='https://example.invalid',_token='test-secret',
        _rate_limit_cooldown_remaining=lambda:0,_reset_rate_limit_circuit=lambda:None,
        _record_rate_limit_event=lambda:False)
    for response, expected in [({'ret':0},'accepted'),({'errcode':0},'accepted'),
        ({'ret':0,'errcode':-14},'rejected'),({},'accepted_unconfirmed'),(None,'unknown'),
        ({'ret':0,'error':'rejected'},'rejected'),
        ({'message_id':123456},'accepted_unconfirmed'),
        ({'message_id':123456,'ret':-14},'rejected'),
        ({'message_id':123456,'success':False},'rejected'),
        ({'message_id':True},'unknown'),
        ({'base_resp':{'ret':0}},'accepted'),
        ({'ret':False},'unknown'),({'ret':'0'},'unknown')]:
        assert response_state(response)==expected
        fake=AsyncMock(return_value=response)
        with patch.object(w,'_send_message',fake):
            try:
                await w.WeixinAdapter._send_text_chunk_locked(adapter,chat_id='peer',chunk='test',context_token='test-context',client_id='test-trace')
                assert expected in ('accepted','accepted_unconfirmed')
            except TimeoutError as e:
                from gateway.platforms.base import BasePlatformAdapter
                assert expected=='unknown'
                assert BasePlatformAdapter._is_timeout_error(str(e))
                assert not BasePlatformAdapter._is_retryable_error(str(e))
            except RuntimeError: assert expected=='rejected'
        assert fake.await_count==1
        assert fake.call_args.kwargs['context_token']=='test-context'
    with patch.object(w,'_send_message',AsyncMock(side_effect=TimeoutError('secret raw response'))) as fake:
        try:
            await w.WeixinAdapter._send_text_chunk_locked(adapter,chat_id='peer',chunk='test',context_token='test-context',client_id='test-trace')
        except TimeoutError as e: assert 'secret' not in str(e)
        else: raise AssertionError('timeout incorrectly succeeded')
        assert fake.await_count==1
    print('PASS explicit ACK, malformed ACK, API rejection, preserved context, no ambiguous retry, sanitized errors')
asyncio.run(main())

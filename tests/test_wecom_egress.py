import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import test_inspection_ack
from ack_test import wecom_egress as relay


class RelayClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_native_url_shapes_use_private_post_and_no_proxy(self):
        native=SimpleNamespace(get=AsyncMock(),post=AsyncMock(),aclose=AsyncMock())
        client=relay.RelayHTTPClient(native,'http://100.97.84.9:8790','test-auth')
        direct=httpx.AsyncClient; requests=[]; options=[]
        def capture(request):
            requests.append(request);return httpx.Response(200,json={'errcode':0})
        def factory(**kwargs):
            options.append(kwargs);return direct(transport=httpx.MockTransport(capture),**kwargs)
        with patch.object(relay.httpx,'AsyncClient',side_effect=factory):
            await client.get('https://qyapi.weixin.qq.com/cgi-bin/gettoken',params={'corpid':'test','corpsecret':'test-secret'})
            await client.post('https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=test-token',json={'touser':'alice'})
        self.assertEqual([r.url.path for r in requests],['/internal/hermes/wecom-api/gettoken','/internal/hermes/wecom-api/send'])
        self.assertTrue(all(r.url.host=='100.97.84.9' for r in requests))
        self.assertTrue(all(not x['trust_env'] for x in options))
        native.get.assert_not_awaited();native.post.assert_not_awaited()
        await client.aclose();native.aclose.assert_awaited_once()

    async def test_relay_failure_never_calls_original_client(self):
        native=SimpleNamespace(get=AsyncMock(),post=AsyncMock())
        client=relay.RelayHTTPClient(native,'http://100.97.84.9:8790','test-auth')
        direct=httpx.AsyncClient
        with patch.object(relay.httpx,'AsyncClient',side_effect=lambda **kw:direct(transport=httpx.MockTransport(lambda r:httpx.Response(503)),**kw)):
            with self.assertRaises(RuntimeError):
                await client.get('https://qyapi.weixin.qq.com/cgi-bin/gettoken',params={'corpid':'test','corpsecret':'test-secret'})
        native.get.assert_not_awaited();native.post.assert_not_awaited()

    async def test_non_wecom_destinations_are_rejected_before_network(self):
        client=relay.RelayHTTPClient(None,'http://100.97.84.9:8790','test-auth')
        with patch.object(relay.httpx,'AsyncClient') as network:
            with self.assertRaises(RuntimeError):await client.get('https://evil.example/cgi-bin/gettoken')
            with self.assertRaises(RuntimeError):await client.post('https://qyapi.weixin.qq.com/cgi-bin/menu/create')
            network.assert_not_called()

    async def test_reconnect_wraps_fresh_client_before_token_fetch(self):
        class Adapter:
            async def _refresh_access_token(self, app):
                self.observed=isinstance(self._http_client,relay.RelayHTTPClient)
                return 'test-token'
        adapter=Adapter();adapter._http_client=object()
        with patch.object(relay,'relay_settings',return_value=('http://100.97.84.9:8790','test-auth')):
            relay.enable_adapter_relay(adapter,Path('/tmp'))
            await adapter._refresh_access_token({});self.assertTrue(adapter.observed)
            adapter._http_client=object()
            await adapter._refresh_access_token({});self.assertTrue(adapter.observed)

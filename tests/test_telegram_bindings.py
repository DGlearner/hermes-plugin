import copy
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch
from contextlib import nullcontext

from wechat_provisioner.telegram import TelegramBindings, TelegramRequest
from wechat_provisioner.errors import ProvisioningError


class Control:
    def __init__(self, home):
        self._home = home
        self.config = {'telegram_binding_bot_username': 'company_test_bot', 'gateway': {'profile_routes': []}}
        self.pats = {}
        self.grants = []

    def _read_gateway_config(self): return copy.deepcopy(self.config)
    def _write_gateway_config(self, config): self.config = copy.deepcopy(config)
    def _profile_home(self, profile): return self._home / 'profiles' / profile
    def _profile_secret_values(self, home, keys): return {'MCP_COMPANY_MCP_API_KEY': self.pats.get(home.name)}
    def ensure_employee_profile(self, request): self._profile_home(request.profile_name).mkdir(parents=True, exist_ok=True)
    def set_profile_pat(self, profile, token): self.pats[profile] = token
    def _profile_scope(self, home): return nullcontext()


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.control = Control(Path(self.temp.name)); self.now = 1000
        self.service = TelegramBindings(self.control, clock=lambda: self.now)
        self.service._grant = lambda *args: self.control.grants.append(args)
        self.employee = TelegramRequest(employee_id=uuid4(), username='a', display_name='A')

    def start(self, employee=None):
        employee = employee or self.employee
        link = self.service.start(employee)
        request = employee.model_copy(update={'session_id': link['session_id']})
        code = link['url'].split('bind_')[1]
        return request, code

    def finish(self, request, token='ragmcp_test'):
        return self.service.poll(request.model_copy(update={'personal_token': __import__('pydantic').SecretStr(token)}))

    def test_link_only_stores_hash_and_is_single_use(self):
        request, code = self.start()
        self.assertNotIn(code.encode(), self.service.path.read_bytes())
        self.assertTrue(self.service.claim(code, 123, 'alice'))
        self.assertFalse(self.service.claim(code, 456, 'other'))
        self.assertEqual(self.service.poll(request)['status'], 'needs_credential')
        self.assertEqual(self.finish(request)['status'], 'bound')
        self.assertEqual(self.service.poll(request)['status'], 'bound')
        self.assertEqual(len(self.control.grants), 1)

    def test_expiry_and_regeneration(self):
        request, code = self.start(); self.start()
        self.assertFalse(self.service.claim(code, 123, 'a'))
        self.assertEqual(self.service.poll(request)['status'], 'expired')
        request, code = self.start(); self.now += 301
        self.assertFalse(self.service.claim(code, 123, 'a'))
        self.assertEqual(self.service.poll(request)['status'], 'expired')

    def test_cross_employee_poll_rejected(self):
        request, _ = self.start()
        with self.assertRaises(ProvisioningError):
            self.service.poll(request.model_copy(update={'employee_id': uuid4()}))

    def test_cross_employee_telegram_conflict(self):
        request, code = self.start(); self.service.claim(code,123,'a'); self.finish(request)
        other = TelegramRequest(employee_id=uuid4(),username='b',display_name='B')
        request, code = self.start(other); self.service.claim(code,123,'a')
        with self.assertRaises(ProvisioningError): self.finish(request)
        self.assertEqual(len(self.control.grants), 1)

    def test_existing_pat_and_wechat_route_are_preserved(self):
        e=self.employee; self.control.ensure_employee_profile(e)
        self.control.pats[e.profile_name]='ragmcp_original'
        wx={'platform':'weixin','chat_id':'wx1','profile':e.profile_name}
        self.control.config['gateway']['profile_routes']=[wx]
        request, code=self.start(); self.service.claim(code,123,'a')
        self.assertEqual(self.service.poll(request)['status'],'bound')
        self.assertEqual(self.control.pats[e.profile_name],'ragmcp_original')
        with patch('gateway.pairing.PairingStore'):
            self.assertEqual(self.service.revoke(e)['status'],'unbound')
        self.assertEqual(self.control.config['gateway']['profile_routes'],[wx])
        self.assertEqual(self.control.pats[e.profile_name],'ragmcp_original')
        self.assertEqual(self.service.poll(request)['status'],'expired')

    def test_concurrent_claim_has_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        request, code=self.start()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda i:self.service.claim(code,100+i,'u'),range(8)))
        self.assertEqual(sum(results),1)

    def test_invalid_token_never_creates_route(self):
        request,code=self.start();self.service.claim(code,123,'a')
        with self.assertRaises(ProvisioningError):self.finish(request,'bad')
        self.assertEqual(self.control.config['gateway']['profile_routes'],[])

    def test_manual_binding_is_visible_and_blocks_new_link(self):
        self.control.config['gateway']['profile_routes']=[{'platform':'telegram','chat_id':'123','profile':self.employee.profile_name}]
        self.assertEqual(self.service.start(self.employee),{'status':'bound','account_hint':'123'})

    def test_shared_pat_not_removed_when_wechat_unbinds(self):
        from wechat_provisioner.hermes_control import InstalledHermesControl
        self.control.config['gateway']['profile_routes']=[{'platform':'telegram','chat_id':'123','profile':self.employee.profile_name}]
        # This must return before entering any secret writer.
        InstalledHermesControl.remove_profile_pat(self.control,self.employee.profile_name)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_start_bypasses_normal_dispatch_and_hides_payload(self):
        import importlib.util, os
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from telegram.ext import ApplicationHandlerStop
        spec=importlib.util.spec_from_file_location('telegram_binding_test',Path(__file__).parents[1]/'telegram_binding.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        class Adapter:
            _effective_update_message=staticmethod(lambda update:update.message)
            _handle_command=AsyncMock()
            _is_user_authorized_from_message=lambda self,msg:True
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'HERMES_HOME':tmp}):
            module._patch_adapter(Adapter)
            msg=SimpleNamespace(text='/start bind_'+'a'*32,from_user=SimpleNamespace(id=123,is_bot=False,username='a'),
                                chat=SimpleNamespace(id=123,type='private'),reply_text=AsyncMock())
            with self.assertRaises(ApplicationHandlerStop):
                await Adapter()._handle_command(SimpleNamespace(message=msg),None)
            msg.reply_text.assert_awaited_once()
            self.assertFalse(Adapter()._is_user_authorized_from_message(msg))

if __name__=='__main__': unittest.main()

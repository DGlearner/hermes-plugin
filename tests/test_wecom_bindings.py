import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from pydantic import SecretStr
from test_telegram_bindings import Control
from wechat_provisioner.wecom import WecomBindings, WecomRequest
from wechat_provisioner.errors import ProvisioningError
import test_inspection_ack
from ack_test.wecom_binding import patch_adapter


class WecomTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.control = Control(Path(self.temp.name))
        self.control.config['wecom_binding'] = {'enabled':True, 'corp_id':'wwtest'}
        self.now = 1000
        self.service = WecomBindings(self.control, clock=lambda:self.now)
        self.service._grant = lambda *args:None
        self.employee = WecomRequest(employee_id=uuid4(),username='alice',display_name='Alice')

    def start(self, employee=None):
        employee = employee or self.employee
        result = self.service.start(employee)
        return employee.model_copy(update={'session_id':result['session_id']}), result['binding_code']

    def finish(self, request):
        return self.service.poll(request.model_copy(update={'personal_token':SecretStr('ragmcp_test')}))

    def test_confirm_only_then_credentials_and_shared_profile(self):
        req, code = self.start()
        self.assertEqual(self.service.poll(req)['status'],'waiting')
        self.assertNotIn(code, self.service.path.read_bytes().decode(errors='ignore'))
        self.assertTrue(self.service.claim(code,'wwtest:alice','Alice'))
        self.assertFalse(self.service.claim(code,'wwtest:bob','Bob'))
        self.assertEqual(self.service.poll(req)['status'],'needs_credential')
        self.assertEqual(self.finish(req)['status'],'bound')
        route = self.control.config['gateway']['profile_routes'][0]
        self.assertEqual(route['platform'],'wecom_callback')
        self.assertEqual(route['profile'],self.employee.profile_name)
        self.assertEqual(route['chat_id'],'wwtest:alice')

    def test_tenant_expiry_replay_and_employee_ownership(self):
        req, code = self.start()
        self.assertFalse(self.service.claim(code,'other:alice','Alice'))
        other = WecomRequest(employee_id=uuid4(),username='bob',display_name='Bob',session_id=req.session_id)
        with self.assertRaises(ProvisioningError):self.service.poll(other)
        self.now += 301
        self.assertFalse(self.service.claim(code,'wwtest:alice','Alice'))
        self.assertEqual(self.service.poll(req)['status'],'expired')

    def test_same_member_cannot_bind_two_employees(self):
        req, code = self.start();self.service.claim(code,'wwtest:alice','Alice');self.finish(req)
        other = WecomRequest(employee_id=uuid4(),username='bob',display_name='Bob')
        req, code = self.start(other);self.service.claim(code,'wwtest:alice','Alice')
        with self.assertRaises(ProvisioningError):self.finish(req)

    def test_disabled_and_changed_corp_fail_closed(self):
        req, code = self.start();self.service.claim(code,'wwtest:alice','Alice')
        self.control.config['wecom_binding']['corp_id']='different'
        with self.assertRaises(ProvisioningError):self.finish(req)
        self.control.config['wecom_binding']['enabled']=False
        with self.assertRaises(ProvisioningError):self.start()

    def test_existing_pat_is_reused(self):
        self.control.ensure_employee_profile(self.employee)
        self.control.pats[self.employee.profile_name]='ragmcp_existing'
        req, code=self.start();self.service.claim(code,'wwtest:alice','Alice')
        result=self.service.poll(req)
        self.assertEqual(result['status'],'bound');self.assertFalse(result['credential_installed'])


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_codes_bypass_model_and_unbound_members_are_denied(self):
        with tempfile.TemporaryDirectory() as d:
            control=Control(Path(d));control.config['wecom_binding']={'enabled':True,'corp_id':'wwtest'}
            bindings=WecomBindings(control)
            employee=WecomRequest(employee_id=uuid4(),username='a',display_name='A')
            code=bindings.start(employee)['binding_code']
            class Adapter:
                def __init__(self):self.events=[];self.messages=[]
                async def handle_message(self,event):self.events.append(event)
                async def send(self,chat,text):self.messages.append((chat,text))
            patch_adapter(Adapter,control,bindings);adapter=Adapter()
            def event(text,corp='wwtest'):
                return SimpleNamespace(text=text,source=SimpleNamespace(platform='wecom_callback',chat_type='dm',chat_id=corp+':a',user_id='a',user_name='A'))
            await adapter.handle_message(event('绑定 '+code))
            self.assertEqual(len(adapter.messages),1);self.assertFalse(adapter.events)
            await adapter.handle_message(event('hello'));self.assertFalse(adapter.events)
            control.config['gateway']['profile_routes']=[{'platform':'wecom_callback','chat_id':'wwtest:a','profile':employee.profile_name}]
            await adapter.handle_message(event('hello','wrong'));self.assertFalse(adapter.events)
            await adapter.handle_message(event('已读 R1'))
            self.assertEqual(adapter.events[0].source.user_id,'wwtest:a')

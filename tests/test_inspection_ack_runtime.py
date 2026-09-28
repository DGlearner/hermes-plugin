"""Real Hermes hook dispatch with isolated Profiles and no outbound messaging."""
import sys,json,tempfile,unittest,asyncio
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,'/opt/hermes')

@unittest.skipUnless(Path('/opt/hermes/cron/scheduler.py').exists(),'requires Hermes')
class RuntimeAckTests(unittest.TestCase):
 def test_real_hook_and_command_dispatch_context(self):
  from hermes_cli.plugins import get_plugin_manager, get_plugin_command_handler
  from hermes_cli.lifecycle import invoke_hook
  from plugins.profile_rag_mcp.inspection_ack import AckInspectionRunner,pre_dispatch,command_handler
  manager=get_plugin_manager()
  with tempfile.TemporaryDirectory() as d:
   home=Path(d)/'profiles/employee-ack-test';home.mkdir(parents=True)
   cfg={'enabled':True,'interval_minutes':2,'acknowledgement':{'enabled':True,'reminder_minutes':3},'notification_target':{'platform':'telegram','chat_id':'123'}}
   (home/'inspection.json').write_text(json.dumps(cfg))
   from datetime import datetime
   now=datetime.fromisoformat('2026-09-28T09:00:00+08:00').timestamp()
   calls=[]
   runner=AckInspectionRunner(home,lambda *a:[dict(report_type='department',department_code='test',report_date='2026-09-28',version=1,updated_at='v1',content='synthetic')],lambda *a:'summary',lambda *a:calls.append(1),lambda:now)
   runner.run(cfg)
   manager._hooks.setdefault('pre_gateway_dispatch',[]).append(pre_dispatch)
   old=manager._plugin_commands.get('inspection-ack')
   manager._plugin_commands['inspection-ack']={'handler':command_handler}
   event=SimpleNamespace(text='已读',reply_to_text='提醒编号：R1',internal=False,source=SimpleNamespace(platform=SimpleNamespace(value='telegram'),chat_type='dm',user_id='123',chat_id='123',profile_route_rejected=False))
   gateway=SimpleNamespace(_is_user_authorized_for_source=lambda s:True,_resolve_profile_home_for_source=lambda s:home)
   async def dispatch():
    results=invoke_hook('pre_gateway_dispatch',event=event,gateway=gateway)
    self.assertIn({'action':'rewrite','text':'/inspection-ack'},results)
    handler=get_plugin_command_handler('inspection-ack')
    reply=await handler('')
    self.assertIn('已确认已读',reply)
   try:asyncio.run(dispatch())
   finally:
    manager._hooks['pre_gateway_dispatch'].remove(pre_dispatch)
    if old is None:manager._plugin_commands.pop('inspection-ack',None)
    else:manager._plugin_commands['inspection-ack']=old
   now+=180;runner.run(cfg);self.assertEqual(len(calls),1)

if __name__=='__main__':unittest.main()

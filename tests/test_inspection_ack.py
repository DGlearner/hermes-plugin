import sys,json,sqlite3,tempfile,unittest,asyncio
from types import ModuleType,SimpleNamespace
from pathlib import Path
from datetime import datetime
root=Path(__file__).parents[1]
pkg=ModuleType('ack_test');pkg.__path__=[str(root)];sys.modules['ack_test']=pkg
from ack_test.inspection_ack import AckInspectionRunner,acknowledge,parse_command,pre_dispatch,command_handler,ledger

class AckTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.home=Path(self.tmp.name)/'profiles/employee-test';self.home.mkdir(parents=True)
  self.now=datetime.fromisoformat('2026-09-28T09:00:00+08:00').timestamp()
  self.cfg={'enabled':True,'interval_minutes':2,'acknowledgement':{'enabled':True,'reminder_minutes':3,'max_reminders_per_day':None},'notification_targets':[{'platform':'telegram','chat_id':'123'},{'platform':'weixin','chat_id':'wx'}]}
  (self.home/'inspection.json').write_text(json.dumps(self.cfg))
  self.reports=[dict(report_type='department',department_code='engineering',report_date='2026-09-28',content='test',version=1,updated_at='v1')]
  self.queries=0;self.models=0;self.sent=[]
  self.runner=AckInspectionRunner(self.home,self.query,self.model,self.send,lambda:self.now)
 def query(self,*a):self.queries+=1;return self.reports
 def model(self,*a):self.models+=1;return '摘要'
 def send(self,h,c,text,key):self.sent.append((text,key));return {'telegram':'sent','weixin':'sent'}
 def run_tick(self,seconds=0):self.now+=seconds;return self.runner.run(self.cfg)
 def ack(self,action='已读',token=None,platform='telegram'):return acknowledge(self.home,action,token,platform,lambda:self.now)
 def test_independent_two_and_three_minutes(self):
  self.run_tick();self.run_tick(60);self.run_tick(60);self.run_tick(60)
  self.assertEqual(self.queries,2);self.assertEqual(self.models,1);self.assertEqual(len(self.sent),2)
  self.assertIn('**日报提醒**',self.sent[0][0]);self.assertIn('**回复“已读”停止当前提醒。**',self.sent[0][0]);self.assertNotIn('R1',self.sent[0][0]);self.assertIn('正文：\n\ntest',self.sent[0][0])
 def test_ack_stops_both_channels_but_queries_continue(self):
  self.run_tick();self.assertIn('已确认',self.ack())
  self.run_tick(180);self.assertEqual(len(self.sent),1);self.assertEqual(self.queries,2)
 def test_restart_ack_and_idempotence(self):
  self.run_tick();self.ack(token='R1');self.runner=AckInspectionRunner(self.home,self.query,self.model,self.send,lambda:self.now)
  self.run_tick(180);self.assertEqual(len(self.sent),1);self.assertIn('已确认',self.ack(token='R1'))
 def test_edit_gets_new_receipt(self):
  self.run_tick();self.ack();self.reports[0]['version']=2;self.run_tick(120);self.run_tick(60)
  self.assertIn('**日报提醒**',self.sent[-1][0]);self.assertIn('**回复“已读”停止当前提醒。**',self.sent[-1][0]);self.assertNotIn('R2',self.sent[-1][0]);self.assertEqual(self.models,2)
 def test_plain_ack_clears_multiple(self):
  self.reports.append(dict(self.reports[0],department_code='sales'));self.run_tick()
  self.assertIn('R1、R2',self.ack());self.run_tick(180)
  self.assertEqual(len(self.sent),1)
 def test_snooze(self):
  self.run_tick();self.assertIn('30 分钟',self.ack(action='稍后提醒'));self.run_tick(180);self.assertEqual(len(self.sent),1)
  self.run_tick(1620);self.assertEqual(len(self.sent),2)
 def test_withdrawn_permissions_stop_repeats(self):
  self.run_tick();self.reports=[];self.run_tick(120);self.run_tick(60);self.assertEqual(len(self.sent),1)
 def test_query_failure_stops_cached_send(self):
  self.run_tick()
  def fail(*a):raise RuntimeError()
  self.runner.query=fail;self.run_tick(120);self.run_tick(60);self.assertEqual(len(self.sent),1)
 def test_quiet_work_and_cap(self):
  self.cfg['quiet_hours']={'enabled':True,'start':'09:00','end':'10:00'};self.run_tick();self.assertFalse(self.sent)
  self.run_tick(3600);self.assertEqual(len(self.sent),1)
  self.cfg['acknowledgement']['max_reminders_per_day']=1;self.run_tick(180);self.assertEqual(len(self.sent),1)
  self.now=datetime.fromisoformat('2026-09-28T18:00:00+08:00').timestamp();self.assertEqual(self.run_tick()['status'],'outside_work_hours')
 def test_parse_exact_commands_and_quote(self):
  self.assertEqual(parse_command('已读','提醒编号：R12'),('已读','R12'))
  self.assertEqual(parse_command('收到 R2'),('收到','R2'))
  self.assertIsNone(parse_command('帮我查询已读日报'))
  self.assertEqual(parse_command('全部已读'),('全部已读',None))
 def test_profile_isolation(self):
  self.run_tick();other=Path(self.tmp.name)/'profiles/employee-other';other.mkdir();(other/'inspection.json').write_text(json.dumps(self.cfg))
  self.assertIn('没有找到',acknowledge(other,'已读','R1','telegram'))
  self.run_tick(180);self.assertEqual(len(self.sent),2)
 def event(self,user='123',chat='123',kind='dm',text='已读'):
  return SimpleNamespace(text=text,reply_to_text='',internal=False,source=SimpleNamespace(platform='telegram',user_id=user,chat_id=chat,chat_type=kind,profile_route_rejected=False))
 def gateway(self,allowed=True):return SimpleNamespace(_resolve_profile_home_for_source=lambda s:self.home,_is_user_authorized_for_source=lambda s:allowed)
 def test_native_handler_without_llm_and_fail_closed(self):
  self.run_tick()
  async def process():
   self.assertIsNone(pre_dispatch(self.event(),self.gateway(False)))
   self.assertIsNone(pre_dispatch(self.event(kind='group'),self.gateway()))
   self.assertIsNone(pre_dispatch(self.event(user='456'),self.gateway()))
   self.assertEqual(pre_dispatch(self.event(),self.gateway())['text'],'/inspection-ack')
   self.assertIn('已确认',await command_handler(''))
   self.assertIn('绑定',await command_handler(''))
  asyncio.run(process());self.run_tick(180);self.assertEqual(len(self.sent),1)
 def test_uncertain_round_does_not_replay_on_every_tick(self):
  def fail(*args):self.sent.append(args);raise TimeoutError()
  self.runner.send=fail;self.run_tick();self.run_tick(60);self.assertEqual(len(self.sent),1)
  self.run_tick(120);self.assertEqual(len(self.sent),2);self.assertEqual(self.models,1)

if __name__=='__main__':unittest.main()

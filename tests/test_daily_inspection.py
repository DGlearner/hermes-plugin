import importlib.util
import sys
import tempfile
import unittest
import json
import sqlite3
from pathlib import Path
from types import ModuleType
from datetime import datetime
root=Path(__file__).parents[1]
pkg=ModuleType('inspection_test'); pkg.__path__=[str(root)]; sys.modules[pkg.__name__]=pkg
from inspection_test.daily_inspection import DailyInspectionRunner, report_key

class DailyInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name)
        self.now=datetime.fromisoformat('2026-09-28T09:00:00+08:00').timestamp()
        self.config={'enabled':True,'interval_minutes':30}
        self.reports=[]; self.calls=[]
        self.runner=DailyInspectionRunner(self.home,self.query,self.model,self.send,lambda:self.now)
    def query(self,*args): self.calls.append('query'); return self.reports
    def model(self,*args): self.calls.append('model'); return '日报提醒'
    def send(self,*args): self.calls.append('send')
    def add(self):
        self.reports=[dict(report_type='department',department_code='engineering',report_date='2026-09-28',content='完成测试',version=1,updated_at='2026-09-28T01:00:00Z')]
    def run_check(self,force=False): return self.runner.run(self.config,force=force)['status']
    def test_empty_then_new_then_restart_silent(self):
        self.assertEqual(self.run_check(),'unchanged'); self.assertEqual(self.calls,['query'])
        self.add(); self.now+=1800; self.assertEqual(self.run_check(),'sent')
        self.runner=DailyInspectionRunner(self.home,self.query,self.model,self.send,lambda:self.now)
        self.now+=1800; self.assertEqual(self.run_check(),'unchanged')
        self.assertEqual(self.calls.count('send'),1)
    def test_changed_version(self):
        self.add(); self.run_check(); self.reports[0]['version']=2
        self.now+=1800; self.assertEqual(self.run_check(),'sent')
    def test_test_receipt_each_query_without_model_or_per_tick_notice(self):
        self.config.update(interval_minutes=5,test_notify_each_check=True)
        self.assertEqual(self.run_check(),'unchanged')
        self.now+=60; self.assertEqual(self.run_check(),'not_due')
        self.now+=240; self.assertEqual(self.run_check(),'unchanged')
        self.assertEqual(self.calls,['query','send','query','send'])
    def test_test_receipt_failure_does_not_repeat_or_break_dedup(self):
        self.config['test_notify_each_check']=True
        def fail(*a): self.calls.append('send'); raise TimeoutError()
        self.runner.send=fail
        result=self.runner.run(self.config)
        self.assertEqual(result['status'],'unchanged')
        self.assertEqual(result['test_notice'],'uncertain')
        self.assertEqual(self.run_check(),'not_due')
        self.assertEqual(self.calls.count('send'),1)
    def test_test_receipts_respect_quiet_hours(self):
        self.config.update(test_notify_each_check=True,quiet_hours={'enabled':True,'start':'09:00','end':'10:00'})
        self.assertEqual(self.run_check(),'unchanged')
        self.assertEqual(self.calls,['query'])
    def test_unchanged_receipt_contains_fresh_full_body_without_model(self):
        self.add(); self.run_check()
        self.now+=1800; self.config['test_notify_each_check']=True
        messages=[]
        self.runner.send=lambda home,cfg,text,batch: messages.append(text)
        self.calls.clear()
        result=self.runner.run(self.config)
        self.assertIn('1. 部门日报｜engineering\n\n日期：2026-09-28｜发布人：未提供\n\n正文：\n\n完成测试',messages[0])
        self.assertIn('部门日报｜engineering',messages[0])
        self.assertEqual(self.calls,['query'])
        self.assertNotIn('_reports',result)
        self.reports=[];self.now+=1800;self.runner.run(self.config)
        self.assertNotIn('完成测试',messages[-1])
        self.assertIn('没有可见的今日日报',messages[-1])
    def test_intervals(self):
        for minutes in (2,5,30,60):
            self.config['interval_minutes']=minutes; self.run_check(force=True)
            self.now+=minutes*60-1; self.assertEqual(self.run_check(),'not_due')
            self.now+=1; self.assertEqual(self.run_check(),'unchanged')
    def test_disabled_and_work_boundary(self):
        self.config['enabled']=False; self.add(); self.assertEqual(self.run_check(),'disabled')
        self.config['enabled']=True
        self.now=datetime.fromisoformat('2026-09-28T18:00:00+08:00').timestamp()
        self.assertEqual(self.run_check(),'outside_work_hours'); self.assertEqual(self.calls,[])
    def test_quiet_deferral_and_permission_removed(self):
        self.config['quiet_hours']={'enabled':True,'start':'09:00','end':'10:00'}
        self.add(); self.assertEqual(self.run_check(),'quiet_deferred'); self.assertEqual(self.calls,['query'])
        self.reports=[]; self.now+=3600
        self.assertEqual(self.run_check(),'unchanged'); self.assertNotIn('send',self.calls)
    def test_quiet_recovery(self):
        self.config['quiet_hours']={'enabled':True,'start':'09:00','end':'10:00'}
        self.add(); self.run_check(); self.now+=3600
        self.assertEqual(self.run_check(),'sent')
    def test_model_failure_bounded(self):
        self.add()
        def fail(*a): raise RuntimeError('failure')
        self.runner.summarize=fail
        for i in range(4):
            state=self.run_check(force=True); self.now+=3600
        self.assertEqual(state,'failed')
        self.assertEqual(self.run_check(force=True),'unchanged'); self.assertNotIn('send',self.calls)
    def test_uncertain_send_not_replayed(self):
        self.add()
        def fail(*a): self.calls.append('send'); raise TimeoutError()
        self.runner.send=fail
        self.assertEqual(self.run_check(),'uncertain'); self.now+=1800
        self.assertEqual(self.run_check(),'unchanged'); self.assertEqual(self.calls.count('send'),1)
    def test_query_failure_no_success_watermark(self):
        def fail(*a): raise RuntimeError()
        self.runner.query=fail
        self.assertEqual(self.run_check(),'query_failed')
        self.runner.query=self.query; self.assertEqual(self.run_check(),'retry_wait')
        self.now+=60; self.assertEqual(self.run_check(),'unchanged')
    def test_concurrent_lock(self):
        import fcntl
        self.run_check()
        with (self.home/'inspection/run.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            self.assertEqual(self.run_check(),'busy')
    def test_report_keys_separate_types_and_subjects(self):
        self.add(); a=self.reports[0]
        self.assertNotEqual(report_key(a),report_key(dict(a,department_code='sales')))
    def test_crash_after_send_mark_not_replayed(self):
        self.add(); self.config['quiet_hours']={'enabled':True,'start':'09:00','end':'10:00'}; self.run_check()
        with sqlite3.connect(self.home/'inspection/state.sqlite3') as db: db.execute("UPDATE batches SET state='sending'")
        self.now+=3600
        self.assertEqual(self.run_check(),'unchanged'); self.assertNotIn('send',self.calls)

class DeliveryFanoutTests(unittest.TestCase):
    def test_long_content_is_complete_on_both_channels(self):
        from inspection_test.daily_inspection import send_notification,split_notification
        from unittest.mock import patch
        body='日报正文😀\n'*900
        parts=split_notification(body)
        self.assertEqual(''.join(p.split('\n',1)[1] for p in parts),body)
        with tempfile.TemporaryDirectory() as d:
            cfg={'notification_targets':[{'platform':'telegram','chat_id':'tg'},{'platform':'weixin','chat_id':'wx'}]}
            with patch('inspection_test.daily_inspection._send_notification_target') as send:
                send_notification(Path(d),cfg,body,'long')
                send_notification(Path(d),cfg,body,'long')
                for platform in ('telegram','weixin'):
                    self.assertEqual([c.args[2] for c in send.call_args_list if c.args[1]['platform']==platform],parts)
    def test_both_channels_once_and_restart_dedup(self):
        from inspection_test.daily_inspection import send_notification
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            cfg={'notification_targets':[{'platform':'telegram','chat_id':'tg-test'},{'platform':'weixin','chat_id':'wx-test'}]}
            with patch('inspection_test.daily_inspection._send_notification_target') as send:
                self.assertEqual(send_notification(Path(d),cfg,'test','batch'),{'telegram':'sent','weixin':'sent'})
                self.assertEqual(send_notification(Path(d),cfg,'test','batch'),{'telegram':'sent','weixin':'sent'})
                self.assertEqual(send.call_count,2)
    def test_one_failed_channel_does_not_block_other_or_replay(self):
        from inspection_test.daily_inspection import send_notification, UncertainDelivery
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            cfg={'notification_targets':[{'platform':'weixin','chat_id':'wx-test'},{'platform':'telegram','chat_id':'tg-test'}]}
            def deliver(home,target,*a):
                if target['platform']=='weixin': raise TimeoutError()
            with patch('inspection_test.daily_inspection._send_notification_target',side_effect=deliver) as send:
                for _ in range(2):
                    with self.assertRaises(UncertainDelivery) as caught: send_notification(Path(d),cfg,'test','batch')
                    self.assertEqual(caught.exception.outcomes,{'weixin':'uncertain','telegram':'sent'})
                self.assertEqual(send.call_count,2)

if __name__=='__main__': unittest.main()

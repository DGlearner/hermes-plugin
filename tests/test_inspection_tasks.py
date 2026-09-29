import unittest
from test_inspection_ack import AckTests
from ack_test.daily_inspection import query_all_tasks

class TaskQueryTests(unittest.TestCase):
 def test_all_pages_no_date_filter_full_detail(self):
  calls=[]
  def call(name,args):
   calls.append((name,args))
   if name=='list_my_tasks':
    return {'total':2,'tasks':[{'task_no':'OLD' if args['offset']==0 else 'DONE','status':'open' if args['offset']==0 else 'completed'}]}
   return {'task_no':'OLD','status':'open','title':'Old task','instructions':'full body','expected_submission':'full requirements','created_at':'2020-01-01'}
  result=query_all_tasks(call)
  self.assertEqual(len(result),1)
  self.assertIn('full requirements',result[0]['content'])
  self.assertEqual(calls[:2],[('list_my_tasks',{'limit':100,'offset':0}),('list_my_tasks',{'limit':100,'offset':1})])
 def test_incomplete_page_fails_closed(self):
  with self.assertRaises(ValueError):query_all_tasks(lambda *a:{'tasks':[],'total':1})

class TaskStateTests(AckTests):
 def test_old_task_survives_day_change_report_drops(self):
  task={'item_type':'task','task_no':'OLD','title':'Old task','status':'open','version':1,'content':'full task body'}
  self.reports.append(task);self.run_tick()
  self.assertEqual(len(self.sent),1)
  self.reports=[task];self.run_tick(86400)
  self.assertEqual(len(self.sent),2)
  self.assertIn('**待办提醒**',self.sent[-1][0])
  self.ack();self.run_tick(180);self.assertEqual(len(self.sent),2)
  self.reports[0]=dict(task,status='in_progress',version=2)
  self.run_tick(120);self.assertEqual(len(self.sent),3)
  self.reports=[];self.run_tick(180);self.assertEqual(len(self.sent),3)

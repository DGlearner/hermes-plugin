import unittest
from test_inspection_ack import AckTests
from ack_test.daily_inspection import query_pending_reviews

class ReviewQueryTests(unittest.TestCase):
 def test_acl_endpoint_and_full_page_guard(self):
  calls=[]
  row=dict(document_id='doc',status='pending_review',file_name='a.pdf',title='Title',version=1)
  def call(name,args):calls.append((name,args));return {'count':1,'reviews':[row]}
  items=query_pending_reviews(call)
  self.assertEqual(calls,[('list_pending_knowledge_reviews',{'limit':200})])
  self.assertIn('a.pdf',items[0]['content'])
  for result in ({'count':2,'reviews':[row]}, {'count':200,'reviews':[row]*200}, {'count':1,'reviews':[dict(row,status='approved')]}):
   with self.assertRaises(ValueError):query_pending_reviews(lambda *a:result)
 def test_employee_does_not_call_restricted_tool(self):
  def unexpected(*a):raise AssertionError("must not call")
  self.assertEqual(query_pending_reviews(unexpected,{"organization_role":"employee","system_roles":[]}),[])
 def test_permission_failure_propagates(self):
  def denied(*a):raise PermissionError()
  with self.assertRaises(PermissionError):query_pending_reviews(denied)

class ReviewStateTests(AckTests):
 def test_read_restart_new_version_and_withdrawal(self):
  review=dict(item_type='review',document_id='doc',version=1,status='pending_review',content='文件：a.pdf')
  self.reports=[review];result=self.run_tick()
  self.assertEqual(result['review_count'],1);self.assertEqual(result['report_count'],0)
  self.assertIn('文件审核提醒',self.sent[-1][0])
  self.assertIn('不代表批准入库',self.sent[-1][0])
  self.ack();self.runner=type(self.runner)(self.home,self.query,self.model,self.send,lambda:self.now)
  self.run_tick(180);self.assertEqual(len(self.sent),1)
  self.reports=[dict(review,version=2)];self.run_tick(180);self.assertEqual(len(self.sent),2)
  self.reports=[];self.run_tick(180);self.assertEqual(len(self.sent),2)

 def test_mixed_batch_one_send_full_bodies_and_read(self):
  self.reports += [dict(item_type='task',task_no='T1',title='Task',status='open',version=1,content='task original'),dict(item_type='review',document_id='D1',status='pending_review',version=1,content='文件：review.pdf')]
  result=self.run_tick()
  self.assertEqual(len(self.sent),1)
  self.assertEqual(len(result['reminders']),3)
  text=self.sent[0][0]
  for body in ('正文：\n\ntest','task original','文件：review.pdf'):
   self.assertEqual(text.count(body),1)
  self.assertEqual(text.count('回复“已读”'),1)
  self.ack();self.run_tick(180);self.assertEqual(len(self.sent),1)

 def test_uncertain_batch_restart_waits_and_then_repeats_together(self):
  self.reports += [dict(item_type='review',document_id='D1',status='pending_review',version=1,content='文件：review.pdf')]
  def failed(*args):
   self.sent.append(args)
   raise TimeoutError()
  self.runner.send=failed
  self.run_tick();self.assertEqual(len(self.sent),1)
  self.runner=type(self.runner)(self.home,self.query,self.model,self.send,lambda:self.now)
  self.run_tick(60);self.assertEqual(len(self.sent),1)
  self.run_tick(120);self.assertEqual(len(self.sent),2)
  self.assertIn('review.pdf',self.sent[-1][0])

 def test_staggered_notices_converge_to_grouped_batch(self):
  self.run_tick()
  self.reports += [dict(item_type='task',task_no='T1',title='Task',status='open',version=1,content='task original'),dict(item_type='review',document_id='D1',status='pending_review',version=1,content='文件：first.pdf\n标题：First'),dict(item_type='review',document_id='D2',status='pending_review',version=1,content='文件：second.pdf\n标题：Second')]
  self.run_tick(120);self.assertEqual(len(self.sent),1)
  self.run_tick(60);self.assertEqual(len(self.sent),2)
  text=self.sent[-1][0]
  self.assertEqual(text.count('**文件审核提醒**'),1)
  self.assertEqual(text.count('**待办提醒**'),1)
  self.assertIn('标题：First\n\n文件：second.pdf',text)
  self.assertEqual(text.count('回复“已读”'),1)

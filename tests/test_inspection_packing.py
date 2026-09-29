import unittest,tempfile,json
from pathlib import Path
from unittest.mock import patch
import test_inspection_ack
from ack_test.inspection_ack import format_batch
from ack_test.daily_inspection import split_notification,send_notification,query_reports

class PackingTests(unittest.TestCase):
 def test_wecom_lists_are_flush_left_without_losing_body(self):
  task=dict(item_type='task',task_no='T1',title='任务',status='open',content='  1. 第一项\n\t2) 第二项\n  - 第三项\n> 引用')
  message=format_batch([task],notice_ids=[79],flush_left=True,byte_limit=4000)
  self.assertIn('1． 第一项\n2． 第二项\n• 第三项\n＞ 引用',message)
  self.assertIn('**待办提醒**',message)
  self.assertIn('[R79]',message)
  self.assertTrue(all(line==line.lstrip() for line in message.splitlines()))
  self.assertIn(task['content'],format_batch([task]))
 def test_wecom_byte_limit_keeps_chinese_items_whole(self):
  rows=[dict(item_type='review',content='文件：'+str(i)+'\n标题：'+'中'*650) for i in range(2)]
  parts=split_notification(format_batch(rows,notice_ids=[1,2],byte_limit=4000))
  self.assertEqual(len(parts),2)
  for part in parts:self.assertLessEqual(len(part.encode('utf-8')),4000)
 def test_paged_ids_stay_with_their_items(self):
  rows=[self.item(1,800),self.item(2,800)]
  parts=split_notification(format_batch(rows,notice_ids=[79,85]))
  self.assertEqual(len(parts),2)
  self.assertIn('[R79]\n文件：1.pdf',parts[0])
  self.assertIn('[R85]\n文件：2.pdf',parts[1])
  for p in parts:self.assertLessEqual(len(p),1500)
 def item(self,i,n):return dict(item_type='review',content=f'文件：{i}.pdf\n标题：'+str(i)*n)
 def test_whole_items_and_single_heading_per_page(self):
  rows=[self.item(i,550) for i in range(1,4)]
  parts=split_notification(format_batch(rows))
  self.assertEqual(len(parts),2)
  for r in rows:self.assertEqual(sum(r['content'].replace('\n','\n\n') in p for p in parts),1)
  for p in parts:
   self.assertLessEqual(len(p),1500)
   self.assertEqual(p.count('**文件审核提醒**'),1)
   self.assertEqual(p.count('回复“已读 R编号”'),1)
 def test_single_oversize_preserved_and_isolated(self):
  rows=[self.item(1,100),self.item(2,1800),self.item(3,100)]
  parts=split_notification(format_batch(rows))
  self.assertEqual(len(parts),3)
  self.assertIn(rows[1]['content'].replace('\n','\n\n'),parts[1])
 def test_fields_have_real_paragraph_breaks_and_body_is_intact(self):
  task=dict(item_type='task',task_no='T1',title='上传文件',status='open',content='原文第一行\n原文第二行')
  review=dict(item_type='review',content='文件：a.pdf\n标题：标题\n提交人：员工\n状态：待审核')
  message=format_batch([task,review])
  self.assertIn('任务：T1\n\n状态：待开始\n\n截止时间：未设置',message)
  self.assertIn(task['content'],message)
  self.assertIn('文件：a.pdf\n\n标题：标题\n\n提交人：员工\n\n状态：待审核',message)
  self.assertNotIn('\\n',message)
  self.assertTrue(all(line == line.lstrip() for line in message.splitlines()))
 def test_transport_does_not_resplit_and_deduplicates(self):
  message=format_batch([self.item(1,1700),self.item(2,100)])
  cfg={'notification_targets':[{'platform':'weixin','chat_id':'wx'},{'platform':'telegram','chat_id':'tg'}]}
  with tempfile.TemporaryDirectory() as d,patch('ack_test.daily_inspection._send_notification_target') as send:
   send_notification(Path(d),cfg,message,'batch')
   send_notification(Path(d),cfg,message,'batch')
   self.assertEqual(send.call_count,4)
   for platform in ('weixin','telegram'):
    self.assertEqual([c.args[2] for c in send.call_args_list if c.args[1]['platform']==platform],list(message.parts))
 def test_all_sources_in_one_query_cycle(self):
  calls=[]
  def call(endpoint,timeout,name,args):
   calls.append((name,args))
   return json.dumps({'get_current_user':{'username':'crz','organization_role':'department_manager'},'list_daily_reports':{'reports':[],'count':0},'list_my_tasks':{'tasks':[],'total':0},'list_pending_knowledge_reviews':{'reviews':[],'count':0}}[name])
  with patch('ack_test.work_todos.profile_scope'),patch('ack_test._call_mcp',call,create=True),patch('ack_test.DEFAULT_ENDPOINT','test',create=True),patch('ack_test.ENDPOINT_ENV','INSPECTION_TEST_ENDPOINT',create=True),patch('ack_test._validate_endpoint',lambda s:s,create=True):
   self.assertEqual(query_reports(Path('/tmp'),{'expected_username':'crz','include_tasks':True,'include_reviews':True},'2026-09-29'),[])
  self.assertEqual([c[0] for c in calls],['get_current_user','list_daily_reports','list_my_tasks','list_pending_knowledge_reviews'])
  self.assertEqual(calls[1][1]['start_date'],'2026-09-29')
  self.assertNotIn('start_date',calls[2][1]);self.assertNotIn('start_date',calls[3][1])

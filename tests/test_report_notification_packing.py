"""Long daily reports retain all content and one receipt across WeCom pages."""
import json
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import test_inspection_ack  # noqa: F401 - installs the isolated ack_test package used below.
from ack_test.daily_inspection import UncertainDelivery, send_notification, split_notification
from ack_test.inspection_ack import ACK_FOOTER, AckInspectionRunner, acknowledge, format_batch


class ReportNotificationPackingTests(unittest.TestCase):
    def report(self, content, **overrides):
        return dict(report_type='personal', employee_username='report-test',
                    employee_display_name='测试员工', author_display_name='测试员工',
                    report_date='2026-10-08', content=content, version=1, **overrides)

    def assert_complete(self, report, *, notice_id=79, flush_left=True):
        original = str(format_batch([report], notice_ids=[notice_id], flush_left=flush_left))
        expected = original.removeprefix(f'**日报提醒**\n\n[R{notice_id}]\n').removesuffix(ACK_FOOTER)
        parts = split_notification(format_batch([report], notice_ids=[notice_id],
                                                flush_left=flush_left, byte_limit=4000))
        self.assertGreater(len(parts), 1)
        chunks = []
        for index, part in enumerate(parts, 1):
            header = f'**日报提醒（第 {index}/{len(parts)} 部分）**\n\n[R{notice_id}]\n'
            self.assertTrue(part.startswith(header))
            self.assertTrue(part.endswith(ACK_FOOTER))
            self.assertLessEqual(len(part.encode('utf-8')), 4000)
            self.assertLessEqual(len(part), 1500)
            chunks.append(part.removeprefix(header).removesuffix(ACK_FOOTER))
        self.assertEqual(''.join(chunks), expected)
        return parts, chunks

    def test_multibyte_long_single_line_is_complete(self):
        content = '中文😀é' * 1600
        _, chunks = self.assert_complete(self.report(content))
        self.assertEqual(''.join(chunks).split('正文：\n\n', 1)[1], content)

    def test_complete_paragraphs_are_kept_together(self):
        paragraphs = [f'项目 {i}：' + '完成设计并与团队确认下一步。' * 16 for i in range(12)]
        content = '\n\n'.join(paragraphs)
        _, chunks = self.assert_complete(self.report(content))
        for paragraph in paragraphs:
            self.assertEqual(sum(paragraph in chunk for chunk in chunks), 1)

    def test_eight_section_template_fits_with_full_content(self):
        template = (Path(__file__).parents[1] / 'docs/daily-report-template.md').read_text()
        self.assertGreater(len(template.encode('utf-8')), 4000)
        _, chunks = self.assert_complete(self.report(template))
        for title in re.findall(r'^## .+$', template, flags=re.M):
            self.assertEqual(sum(title in chunk for chunk in chunks), 1)

    def test_large_metadata_and_many_page_digits_stay_within_budget(self):
        report = self.report('中文😀' * 15000)
        report['employee_display_name'] = '团队' * 2200
        parts, _ = self.assert_complete(report, notice_id=123456789)
        self.assertGreaterEqual(len(parts), 10)

    def test_short_report_and_non_wecom_pages_are_unchanged(self):
        short = self.report('今日完成测试。')
        self.assertEqual(format_batch([short], notice_ids=[79]),
                         format_batch([short], notice_ids=[79], byte_limit=4000))
        long = self.report('中文😀' * 1800)
        original = format_batch([long], notice_ids=[79])
        self.assertEqual(len(split_notification(original)), 1)
        self.assertIn(long['content'], original)

    def test_task_and_review_items_remain_whole(self):
        task = dict(item_type='task', task_no='T1', title='测试任务', status='open', content='中' * 1800)
        review = dict(item_type='review', content='文件：测试.pdf\n正文：' + '文' * 1800)
        original = split_notification(format_batch([task, review], notice_ids=[1, 2]))
        self.assertEqual(split_notification(format_batch([task, review], notice_ids=[1, 2], byte_limit=4000)),
                         original)
        self.assertEqual(len(original), 2)

    def test_partial_delivery_remains_uncertain_and_single_receipt_stays_scoped(self):
        reports = [self.report('中文😀' * 1600), self.report('另一位员工的日报')]
        reports[1]['employee_username'] = 'other-report-test'
        config = {
            'enabled': True, 'interval_minutes': 30,
            'acknowledgement': {'enabled': True, 'reminder_minutes': 30, 'max_reminders_per_day': None},
            'notification_targets': [
                {'platform': 'wecom_callback', 'chat_id': 'test-corp:test-user'},
                {'platform': 'telegram', 'chat_id': 'test-chat'},
            ],
        }
        now = datetime.fromisoformat('2026-10-08T09:00:00+08:00').timestamp()
        batches, sent = [], []

        def deliver(home, target, part, part_id):
            sent.append((target['platform'], part, part_id))
            if target['platform'] == 'wecom_callback' and '（第 2/' in part:
                raise TimeoutError('synthetic uncertain delivery')

        def send(home, config, message, batch):
            batches.append((message, batch))
            return send_notification(home, config, message, batch)

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'inspection.json').write_text(json.dumps(config))
            runner = AckInspectionRunner(home, lambda *a: reports, lambda *a: 'summary', send, lambda: now)
            with patch('ack_test.daily_inspection._send_notification_target', side_effect=deliver):
                result = runner.run(config)
                self.assertEqual(result['reminders'][0]['channels'],
                                 {'wecom_callback': 'uncertain', 'telegram': 'sent'})
                count = len(sent)
                with self.assertRaises(UncertainDelivery) as caught:
                    send_notification(home, config, *batches[0])
                self.assertEqual(caught.exception.outcomes['wecom_callback'], 'uncertain')
                self.assertEqual(len(sent), count, 'the same batch must not replay successful or uncertain pages')
            with sqlite3.connect(home / 'inspection/ack.sqlite3') as db:
                self.assertEqual(db.execute('SELECT state FROM notices ORDER BY id').fetchall(),
                                 [('unread',), ('unread',)])
            reply = acknowledge(home, '已读', 'R1', 'wecom_callback', lambda: now)
            self.assertIn('R1 已确认已读', reply)
            with sqlite3.connect(home / 'inspection/ack.sqlite3') as db:
                self.assertEqual(db.execute('SELECT state FROM notices ORDER BY id').fetchall(),
                                 [('acked',), ('unread',)])
            for platform in ('wecom_callback', 'telegram'):
                actual = [part for channel, part, _ in sent if channel == platform]
                self.assertEqual(actual, list(batches[0][0].parts))
                self.assertTrue(all(len(part.encode('utf-8')) <= 4000 for part in actual))


if __name__ == '__main__':
    unittest.main()

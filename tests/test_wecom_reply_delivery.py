import unittest
from types import SimpleNamespace
import test_inspection_ack
from ack_test.wecom_binding import split_reply, patch_reply_delivery
from ack_test.report_display import render_report_text

class ReplyTests(unittest.IsolatedAsyncioTestCase):
    def test_lossless_utf8_and_paragraphs(self):
        for text in ['简短回复', ('中文🙂abc\n\n' * 1000), '汉' * 3000, 'a' * 5000]:
            parts = split_reply(text)
            self.assertEqual(''.join(parts), text)
            self.assertTrue(all(len(p.encode('utf-8')) <= 2000 for p in parts))
        self.assertTrue(split_reply('a' * 1500 + '\n' + 'b' * 900)[0].endswith('\n'))

    async def test_native_sends_all_parts_and_stops_on_failure(self):
        class Adapter:
            def __init__(self): self.sent=[]; self.fail=False
            async def send(self, chat_id, content, reply_to=None, metadata=None):
                self.sent.append(content[:2048])
                return SimpleNamespace(success=not self.fail)
        patch_reply_delivery(Adapter)
        patch_reply_delivery(Adapter)
        a=Adapter(); text='正文🙂\n' * 1000
        self.assertTrue((await a.send('test',text)).success)
        self.assertEqual(''.join(a.sent),text)
        a=Adapter();a.fail=True
        self.assertFalse((await a.send('test',text)).success)
        self.assertEqual(len(a.sent),1)

    async def test_report_is_formatted_before_utf8_chunking(self):
        class Adapter:
            def __init__(self): self.sent=[]
            async def send(self, chat_id, content, reply_to=None, metadata=None):
                self.sent.append(content)
                return SimpleNamespace(success=True)
        patch_reply_delivery(Adapter)
        adapter = Adapter()
        content = '# 工作进展同步日报\n\n| 事项 | 负责人 |\n|---|---|\n' + '| 交付🙂 | 本人 |\n' * 200
        await adapter.send('test', content)
        self.assertGreater(len(adapter.sent), 1)
        self.assertEqual(''.join(adapter.sent), render_report_text(content))
        self.assertEqual(''.join(adapter.sent).count('事项：交付🙂\n负责人：本人'), 200)
        self.assertTrue(all(len(part.encode('utf-8')) <= 2000 for part in adapter.sent))

import unittest
from types import SimpleNamespace
import test_inspection_ack
from ack_test.wecom_binding import split_reply, patch_reply_delivery

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

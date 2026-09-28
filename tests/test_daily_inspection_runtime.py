"""Pinned-runtime integration: real cron script subprocess, synthetic MCP/model/send."""
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,'/opt/hermes')

@unittest.skipUnless(Path('/opt/hermes/cron/scheduler.py').exists(),'requires Hermes runtime')
class NativeCronTests(unittest.TestCase):
    def test_native_no_agent_new_then_unchanged(self):
        from cron.scheduler import run_job, SILENT_MARKER
        from plugins.profile_rag_mcp.work_todos import profile_scope
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory)/'profiles/employee-isolated-test'
            scripts=home/'scripts';scripts.mkdir(parents=True)
            script=scripts/'run_inspection_test.py'
            script.write_text('''import sys
from pathlib import Path
sys.path.insert(0,'/opt/hermes')
from plugins.profile_rag_mcp.daily_inspection import DailyInspectionRunner
home=Path(__file__).parent.parent
report=dict(report_type='department',report_date='2026-09-28',department_code='test',content='synthetic',version=1,updated_at='v1')
def model(*args):
 with (home/'model_calls').open('a') as f: f.write('1')
 return 'synthetic notice'
def send(*args):
 with (home/'send_calls').open('a') as f: f.write('1')
DailyInspectionRunner(home,lambda *a:[report],model,send).run({'enabled':True},force=True)
print('{"wakeAgent": false}')
''')
            job={'id':'inspection-native-test','name':'isolated inspection test','no_agent':True,'script':str(script),'deliver':'local'}
            with profile_scope(home):
                for _ in range(2):
                    ok,_,reply,error=run_job(job)
                    self.assertTrue(ok,error)
                    self.assertEqual(reply,SILENT_MARKER)
            self.assertEqual((home/'model_calls').read_text(),'1')
            self.assertEqual((home/'send_calls').read_text(),'1')

if __name__=='__main__': unittest.main()

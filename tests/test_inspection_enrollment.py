import json
import tempfile
import unittest
from pathlib import Path
import test_inspection_ack
from ack_test.inspection_enrollment import install_template, sync_all, DEFAULTS


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.template = self.root / 'profiles/employee-template'
        self.template.mkdir(parents=True)
        install_template(self.template)
        self.jobs = {}

    def employee(self, name):
        home = self.root / 'profiles' / ('employee-' + name)
        home.mkdir()
        (home / '.rag-mcp-employee.json').write_text(json.dumps(
            {'profile_name': home.name, 'username': name, 'employee_id': name}))
        return home

    def scheduler(self, home, enabled):
        self.jobs[home.name] = enabled
        return home.name

    def sync(self, routes):
        return sync_all(self.root, 'employee-template', routes, scheduler=self.scheduler)

    def route(self, home, channel='weixin', peer='own-peer'):
        return dict(profile=home.name, platform=channel, chat_id=peer)

    def config(self, home):
        return json.loads((home / 'inspection.json').read_text())

    def test_isolated_defaults_and_no_state_copy(self):
        a, b = self.employee('a'), self.employee('b')
        (a / 'inspection').mkdir()
        (a / 'inspection/ack.sqlite3').write_bytes(b'untouched')
        self.sync([self.route(a)])
        self.assertEqual(self.config(a)['expected_username'], 'a')
        self.assertTrue(self.config(a)['include_review_results'])
        self.assertEqual(self.config(b)['expected_username'], 'b')
        self.assertEqual(self.config(b)['notification_targets'], [])
        self.assertFalse(self.jobs[b.name]); self.assertTrue(self.jobs[a.name])
        self.assertFalse((b / 'inspection').exists())
        self.assertEqual((a / 'inspection/ack.sqlite3').read_bytes(), b'untouched')
        self.assertFalse(self.config(self.template)['enabled'])
        self.assertFalse((self.template / 'cron').exists())
        self.assertFalse((self.template / '.env').exists())

    def test_bind_dual_unbind_and_rebind(self):
        a = self.employee('a')
        self.sync([])
        self.sync([self.route(a), self.route(a, 'telegram', 'tg-peer')])
        self.assertTrue(self.jobs[a.name])
        self.assertEqual(len(self.config(a)['notification_targets']), 2)
        self.sync([]); self.assertFalse(self.jobs[a.name])
        self.sync([self.route(a)]); self.assertTrue(self.jobs[a.name])
        self.assertEqual(len(self.jobs), 1)

    def test_preserve_pilot_and_explicit_opt_out(self):
        a = self.employee('a')
        (a / 'inspection.json').write_text(json.dumps(dict(DEFAULTS, enabled=True,
            expected_username='a', interval_minutes=2,
            acknowledgement={'enabled':True, 'reminder_minutes':3})))
        self.sync([self.route(a)])
        self.assertEqual(self.config(a)['interval_minutes'], 2)
        self.assertEqual(self.config(a)['acknowledgement']['reminder_minutes'], 3)
        cfg = self.config(a); cfg['enabled'] = False; cfg['include_review_results'] = False
        (a / 'inspection.json').write_text(json.dumps(cfg))
        self.sync([self.route(a)]); self.assertFalse(self.jobs[a.name])
        self.assertFalse(self.config(a)['include_review_results'])
        self.sync([]); self.sync([self.route(a)])
        self.assertFalse(self.jobs[a.name])

    def test_identity_mismatch_and_ambiguous_routes_fail_closed(self):
        a = self.employee('a')
        with self.assertRaises(ValueError):
            self.sync([self.route(a), self.route(a, peer='another-peer')])
        self.assertFalse(self.jobs)
        (a / 'inspection.json').write_text(json.dumps({'expected_username':'someone-else'}))
        with self.assertRaises(ValueError): self.sync([self.route(a)])
        self.assertFalse(self.jobs)

    def test_provisioning_is_opt_in(self):
        (self.template / 'inspection-defaults.json').unlink()
        self.employee('a')
        self.assertEqual(self.sync([]), [])

    def test_wecom_binding_uses_same_employee_and_scheduler(self):
        a = self.employee('a')
        self.sync([self.route(a), self.route(a, 'wecom_callback', 'corp:member')])
        cfg = self.config(a)
        self.assertEqual(cfg['expected_username'], 'a')
        self.assertEqual({t['platform'] for t in cfg['notification_targets']}, {'weixin','wecom_callback'})
        self.assertEqual(len(self.jobs), 1)


@unittest.skipUnless(Path('/opt/hermes/cron/scheduler.py').exists(), 'requires Hermes')
class EnrollmentRuntimeTests(unittest.TestCase):
    def test_real_scheduler_idempotence_and_route_hook(self):
        import sys
        from types import SimpleNamespace
        sys.path.insert(0, '/opt/hermes')
        from cron.jobs import use_cron_store, list_jobs
        from wechat_provisioner.hermes_control import InstalledHermesControl
        from plugins.profile_rag_mcp.inspection_enrollment import install_template as install
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            home = root / 'profiles/employee-test'
            home.mkdir(parents=True)
            (home / '.rag-mcp-employee.json').write_text(json.dumps(
                {'profile_name':home.name,'username':'test'}))
            install(root / 'profiles/employee-template')
            control = InstalledHermesControl(SimpleNamespace(hermes_home=root, template_profile='employee-template'))
            cfg = {'gateway': {'profile_routes': []}}
            control._write_gateway_config(cfg)
            with use_cron_store(home):
                jobs = list_jobs(include_disabled=True)
                self.assertEqual(len(jobs), 1); self.assertFalse(jobs[0]['enabled'])
                job_id = jobs[0]['id']
            cfg['gateway']['profile_routes'] = [{'profile':home.name,'platform':'telegram','chat_id':'123'}]
            control._write_gateway_config(cfg); control._write_gateway_config(cfg)
            with use_cron_store(home):
                jobs = list_jobs(include_disabled=True)
                self.assertEqual(len(jobs), 1); self.assertEqual(jobs[0]['id'],job_id)
                self.assertTrue(jobs[0]['enabled']); self.assertTrue(jobs[0]['no_agent'])
                self.assertEqual(jobs[0]['deliver'],'local')
            self.assertFalse((root / 'cron/jobs.json').exists())
            self.assertFalse((root / 'profiles/employee-template/cron').exists())

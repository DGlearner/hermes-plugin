"""Provision independent inspection schedules without copying employee state/secrets."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path

DEFAULTS = {
    'timezone': 'Asia/Shanghai', 'interval_minutes': 30,
    'work_hours': {'start': '09:00', 'end': '18:00'},
    'include_tasks': True, 'include_reviews': True, 'include_review_results': True,
    'test_notify_each_check': False,
    'acknowledgement': {'enabled': True, 'reminder_minutes': 30,
                        'max_reminders_per_day': None, 'snooze_minutes': 30},
}
JOB_NAME = 'profile-inspection-daily-reports'
SCRIPT_NAME = 'profile_daily_inspection.py'
SCRIPT = '''import sys
from pathlib import Path
sys.path.insert(0, "/opt/hermes")
from plugins.profile_rag_mcp.daily_inspection import main
main(Path(__file__).resolve().parent.parent)
'''


def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='inspection-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def install_resources(home):
    write_text(home / 'scripts' / SCRIPT_NAME, SCRIPT)
    skill = Path(__file__).parent / 'skills/profile-inspection/SKILL.md'
    write_text(home / 'skills/profile-inspection/SKILL.md', skill.read_text())


def install_template(home):
    """Store only neutral defaults/resources; never clone a live employee."""
    path = home / 'inspection-defaults.json'
    if not path.exists():
        write_json(path, DEFAULTS)
    defaults = json.loads(path.read_text())
    if set(defaults) - set(DEFAULTS):
        raise ValueError('unexpected inspection template fields')
    install_resources(home)
    write_json(home / 'inspection.json', dict(defaults, enabled=False,
               notification_targets=[]))


def ensure_job(home, enabled):
    from cron.jobs import (use_cron_store, list_jobs, create_job,
                           update_job, pause_job, resume_job)
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(home)
    try:
        with use_cron_store(home):
            jobs = [j for j in list_jobs(include_disabled=True)
                    if j.get('name') == JOB_NAME or j.get('script') == SCRIPT_NAME]
            if len(jobs) > 1:
                raise ValueError('duplicate inspection schedules')
            if jobs:
                job = jobs[0]
                updates = {k: v for k, v in {'script': SCRIPT_NAME, 'no_agent': True,
                                            'deliver': 'local'}.items() if job.get(k) != v}
                if updates:
                    job = update_job(job['id'], updates)
            else:
                job = create_job(prompt='Native Profile inspection', schedule='* * * * *',
                                 name=JOB_NAME, script=SCRIPT_NAME, no_agent=True, deliver='local')
            if enabled and (not job.get('enabled') or job.get('state') == 'paused'):
                job = resume_job(job['id'])
            elif not enabled and job.get('enabled'):
                job = pause_job(job['id'], reason='inspection disabled or awaiting channel binding')
            return job['id']
    finally:
        reset_hermes_home_override(token)


def sync_profile(home, defaults, routes, *, scheduler=ensure_job):
    marker = json.loads((home / '.rag-mcp-employee.json').read_text())
    if marker.get('profile_name') != home.name or not marker.get('username'):
        raise ValueError('invalid employee identity marker')
    if home.is_symlink() or not home.name.startswith('employee-'):
        raise ValueError('invalid employee Profile')
    targets = []
    for route in routes:
        if route.get('profile') != home.name or route.get('platform') not in {'weixin', 'telegram', 'wecom_callback'}:
            continue
        if not route.get('chat_id') or route.get('chat_type') not in (None, 'dm'):
            raise ValueError('direct employee route required')
        target = {'platform': route['platform'], 'chat_id': str(route['chat_id'])}
        if target not in targets:
            targets.append(target)
    if len({t['platform'] for t in targets}) != len(targets):
        raise ValueError('ambiguous employee channel routes')
    path = home / 'inspection.json'
    existing = json.loads(path.read_text()) if path.exists() else {}
    if existing.get('expected_username', marker['username']) != marker['username']:
        raise ValueError('inspection identity mismatch')
    cfg = deepcopy(defaults)
    cfg.update(existing)
    cfg.setdefault('include_review_results', True)
    cfg['acknowledgement'] = {**defaults['acknowledgement'], **existing.get('acknowledgement', {})}
    # Per-profile frequencies, quiet times and explicit opt-outs survive syncing.
    desired = existing.get('enrollment', {}).get('desired_enabled', existing.get('enabled', True))
    if existing.get('notification_targets') and existing.get('enabled') is False:
        desired = False
    cfg.update(expected_username=marker['username'], notification_targets=targets,
               enabled=bool(targets) and bool(desired), include_tasks=True, include_reviews=True,
               enrollment={'desired_enabled': bool(desired), 'waiting_for_binding': not bool(targets)})
    cfg.pop('notification_target', None)
    install_resources(home)
    write_json(path, cfg)
    job = scheduler(home, cfg['enabled'])
    return {'profile': home.name, 'username': marker['username'], 'enabled': cfg['enabled'],
            'channels': [t['platform'] for t in targets], 'interval_minutes': cfg['interval_minutes'],
            'reminder_minutes': cfg['acknowledgement']['reminder_minutes'], 'job_id': job}


def sync_all(root, template_name, routes, *, scheduler=ensure_job):
    root = Path(root)
    template = root / 'profiles' / template_name
    path = template / 'inspection-defaults.json'
    if not path.exists():
        return []  # Rollout is explicitly opted in by installing the template.
    with (root / '.inspection-enrollment.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        defaults = json.loads(path.read_text())
        if set(defaults) - set(DEFAULTS):
            raise ValueError('unexpected inspection template fields')
        results = []
        for marker in sorted((root / 'profiles').glob('employee-*/.rag-mcp-employee.json')):
            if marker.parent == template:
                continue
            results.append(sync_profile(marker.parent, defaults, routes, scheduler=scheduler))
        return results

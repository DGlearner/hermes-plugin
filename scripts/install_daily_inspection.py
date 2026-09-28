"""Run in Hermes to enroll exactly one employee Profile in the report pilot."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,'/opt/hermes')
from plugins.profile_rag_mcp.work_todos import profile_scope
from plugins.profile_rag_mcp.daily_inspection import query_reports
from cron.jobs import create_job, list_jobs
from gateway.config import load_gateway_config
from datetime import datetime
from zoneinfo import ZoneInfo


def install(home, username, platform):
    home=Path(home).resolve()
    if home.parent.name!='profiles' or not home.name.startswith('employee-') or not home.is_dir():
        raise ValueError('existing employee Profile required')
    with profile_scope(home.parent.parent):
        routes=[r for r in load_gateway_config().profile_routes if r.profile==home.name and r.platform==platform]
    if len(routes)!=1:
        raise ValueError('one unambiguous bound route required')
    cfg={'enabled':True,'expected_username':username,'timezone':'Asia/Shanghai',
         'interval_minutes':30,'work_hours':{'start':'09:00','end':'18:00'},
         'notification_target':{'platform':platform,'chat_id':str(routes[0].chat_id)}}
    query_reports(home,cfg,datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat())
    path=home/'inspection.json'
    if path.exists() and json.loads(path.read_text())!=cfg:
        raise ValueError('existing configuration differs; preserve and review it')
    path.write_text(json.dumps(cfg,indent=2)+'\n');path.chmod(0o600)
    script=home/'scripts/profile_daily_inspection.py';script.parent.mkdir(exist_ok=True)
    script.write_text('import sys\nfrom pathlib import Path\nsys.path.insert(0,"/opt/hermes")\nfrom plugins.profile_rag_mcp.daily_inspection import main\nmain(Path(__file__).resolve().parent.parent)\n')
    with profile_scope(home):
        name='profile-inspection-daily-reports'
        existing=[j for j in list_jobs(include_disabled=True) if j.get('name')==name]
        if len(existing)>1: raise ValueError('duplicate jobs')
        job=existing[0] if existing else create_job(prompt='Native Profile report inspection',schedule='* * * * *',name=name,script=script.name,no_agent=True,deliver='local')
    print(json.dumps({'profile':home.name,'job_id':job['id'],'name':name,'interval_minutes':30,'platform':platform}))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('profile_home');parser.add_argument('username');parser.add_argument('--platform',choices=['telegram','weixin'],required=True)
    a=parser.parse_args();install(a.profile_home,a.username,a.platform)

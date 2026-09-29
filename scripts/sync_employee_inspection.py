"""Install neutral inspection defaults and enroll all managed employee Profiles."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, '/opt/hermes')
import yaml
from plugins.profile_rag_mcp.inspection_enrollment import install_template, sync_all
from wechat_provisioner.telegram import channel_lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/opt/data'))
    parser.add_argument('--template', default='employee-template')
    args = parser.parse_args()
    if Path(args.template).name != args.template or not args.template.startswith('employee-'):
        parser.error('invalid template name')
    root = args.root.resolve()
    template = root / 'profiles' / args.template
    if not template.is_dir():
        parser.error('employee template must already exist')
    with channel_lock(root):
        install_template(template)
        config = yaml.safe_load((root / 'config.yaml').read_text())
        results = sync_all(root, args.template, config.get('gateway', {}).get('profile_routes', []))
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

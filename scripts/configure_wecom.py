"""Validate/apply the private WeCom application form without printing its values."""
import argparse
import base64
import json
import re
import sys
from copy import deepcopy
from pathlib import Path

import yaml

FIELDS = ('corp_id', 'agent_id', 'corp_secret', 'token', 'encoding_aes_key')


def validate(values):
    if not isinstance(values, dict) or set(values) != set(FIELDS):
        raise ValueError('配置必须且只能包含模板中的五个字段。')
    values = {key: str(value).strip() if value is not None else '' for key, value in values.items()}
    missing = [key for key in FIELDS if not values[key]]
    if missing:
        raise ValueError('尚未填写：' + '、'.join(missing))
    if not re.fullmatch(r'[A-Za-z0-9_-]{3,128}', values['corp_id']):
        raise ValueError('corp_id 格式不正确。')
    if not values['agent_id'].isdigit() or int(values['agent_id']) <= 0:
        raise ValueError('agent_id 必须是正整数。')
    if not re.fullmatch(r'[A-Za-z0-9]{3,32}', values['token']):
        raise ValueError('token 必须是 3–32 位英文字母或数字。')
    aes = values['encoding_aes_key']
    if not re.fullmatch(r'[A-Za-z0-9+/]{43}', aes) or len(base64.b64decode(aes + '=', validate=True)) != 32:
        raise ValueError('encoding_aes_key 必须是企业微信生成的 43 位密钥。')
    if any(c.isspace() for c in values['corp_secret']):
        raise ValueError('corp_secret 不能包含空白字符。')
    return values


def merge_config(existing, values):
    cfg = deepcopy(existing)
    routes = (cfg.get('gateway') or {}).get('profile_routes') or []
    if any(r.get('platform') == 'wecom_callback' and
           not str(r.get('chat_id', '')).startswith(values['corp_id'] + ':') for r in routes):
        raise ValueError('已有其他企业的绑定，请先处理现有绑定。')
    platform = cfg.setdefault('platforms', {}).setdefault('wecom_callback', {})
    if (platform.get('extra') or {}).get('apps'):
        raise ValueError('已存在多应用配置，不能用单应用模板覆盖。')
    platform.update(enabled=True, gateway_restart_notification=False)
    platform['extra'] = dict(values, host='127.0.0.1', port=8645, path='/wecom/callback')
    cfg['wecom_binding'] = {**cfg.get('wecom_binding', {}), 'enabled': True, 'corp_id': values['corp_id']}
    return cfg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/opt/data'))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        values = validate(yaml.safe_load((root / 'wecom-application.yaml').read_text()))
        sys.path.insert(0, '/opt/hermes')
        from wechat_provisioner.telegram import channel_lock
        from plugins.profile_rag_mcp.inspection_enrollment import write_text
        with channel_lock(root):
            path = root / 'config.yaml'
            config = merge_config(yaml.safe_load(path.read_text()) or {}, values)
            if args.apply:
                write_text(path, yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
        print('配置已应用；请重启 Hermes 后，在企业微信后台保存回调地址。' if args.apply
              else '配置格式检查通过；未修改运行配置。真实凭据有效性需启动后验证。')
    except yaml.YAMLError:
        parser.exit(1, 'YAML 格式不正确，请检查引号和缩进；错误内容不回显。\n')
    except ValueError as exc:
        parser.exit(1, str(exc) + '\n')
    except OSError:
        parser.exit(1, '无法读写配置文件，请检查文件是否存在及权限。\n')


if __name__ == '__main__':
    main()

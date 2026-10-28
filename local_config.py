"""Owner configuration; no account credentials are stored by this application."""
import json
import os
from pathlib import Path
import time
import uuid

ROOT = Path(__file__).resolve().parent
DEFAULTS = {
    'group': '', 'self_name': '', 'members': [], 'nickname': '小群友',
    'style': '自然、简短地接话，有自己的看法。认真问题认真回答，闲聊可以轻轻接梗。',
    'character': '你是群里的 AI 群友，不虚构真人经历。',
    'participation': '可以不回复。别人互相聊天、重复应声、话题结束时不要抢话；被点名或提问时积极回应。',
    'model': '', 'reasoning_effort': 'low', 'context_messages': 24,
    'stickers': False, 'consent': False,
}


def read_config(root=ROOT):
    path = Path(root) / 'config.json'
    return DEFAULTS | (json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {})


def configured_group():
    return read_config()['group']


def validate(data):
    config = {key: data.get(key, value) for key, value in DEFAULTS.items()}
    for key in ('group', 'self_name', 'nickname', 'style', 'character', 'participation'):
        if not isinstance(config[key], str) or not config[key].strip():
            raise ValueError('请填写群名、本人群昵称、机器人名字和人设。')
        config[key] = config[key].strip()
    for key in ('group', 'self_name', 'nickname'):
        if any(ord(c) < 32 for c in config[key]) or len(config[key]) > 80:
            raise ValueError('名称过长或含换行等控制字符。')
    members = config['members']
    if not isinstance(members, list) or not members or any(not isinstance(n, str) or not n.strip() for n in members):
        raise ValueError('请填写完整的群成员显示昵称，每行一人，包含自己。')
    members = [n.strip() for n in members]
    if len(set(members)) != len(members):
        raise ValueError('成员昵称重复，无法可靠分辨；请先在微信中区分同名成员。')
    if config['self_name'] not in members:
        raise ValueError('成员名单必须包含本人群昵称，且与微信显示完全一致。')
    for name in members:
        if any(other != name and other.startswith(name + ' ') for other in members):
            raise ValueError('昵称有前缀歧义（例如“小王”和“小王 同学”）；请先调整昵称。')
    if not isinstance(config['model'], str) or len(config['model']) > 150:
        raise ValueError('模型名称格式无效。留空使用 Codex CLI 默认模型。')
    if config['reasoning_effort'] not in ('low', 'medium', 'high'):
        raise ValueError('推理档位只能是 low / medium / high。')
    if type(config['context_messages']) is not int or not 1 <= config['context_messages'] <= 60:
        raise ValueError('上下文条数需要在 1 到 60 之间。')
    if type(config['stickers']) is not bool or config['consent'] is not True:
        raise ValueError('请确认使用本人的账号，并同意把指定群的消息交给自己的 GPT 处理。')
    config['members'] = members
    return config


def atomic_json(path, data):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def generated(config):
    return {
        'bot_settings.json': {
            'model': config['model'].strip(), 'reasoning_effort': config['reasoning_effort'],
            'context_messages': config['context_messages'], 'service_tier': 'default',
            'transport': 'auto', 'reader_backend': 'selection', 'execution_mode': 'local',
            'auto_collect_stickers': config['stickers'], 'sticker_replies': config['stickers'],
            'sticker_reply_preference': 'balanced', 'sticker_cooldown_seconds': 60,
            'sticker_same_cooldown_seconds': 600, 'sticker_min_text_replies': 2,
            'max_reply_parts': 3, 'reply_part_interval_seconds': 0.5, 'group_tools_mode': 'off',
        },
        'sender_settings.json': {'group': config['group'], 'self_name': config['self_name'],
                                 'members': config['members'], 'member_count': len(config['members'])},
        'persona.json': {key: config[key] for key in ('nickname', 'style', 'character', 'participation')},
    }


def save_config(data, root=ROOT):
    root = Path(root)
    config = validate(data)
    old = read_config(root)
    # A new target or roster must never inherit queued replies or identity aliases.
    if any(old.get(k) != config[k] for k in ('group', 'self_name', 'members')):
        archive = root / 'local_data' / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
        for name in ('.runtime', 'group_profile.json'):
            path = root / name
            if path.exists():
                archive.mkdir(parents=True, exist_ok=True)
                path.rename(archive / name)
    for name, content in generated(config).items():
        atomic_json(root / name, content)
    atomic_json(root / 'config.json', config)
    return config


def validate_installation(root=ROOT):
    config = validate(read_config(root))
    for name, expected in generated(config).items():
        path = Path(root) / name
        if not path.exists() or json.loads(path.read_text(encoding='utf-8-sig')) != expected:
            raise ValueError('配置不完整或被手动修改，请在配置窗口重新保存。')
    return config

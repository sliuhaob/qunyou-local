"""Export ONLY reviewed source files. Never archive the installed directory."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
VERSION = '0.1.0'
FILES = (
    'README.md', 'REFERENCES.md', 'CHANGELOG.md', 'requirements.txt', '.gitignore',
    '01-install.cmd', '02-open.cmd', '03-stop.cmd', '04-check.cmd', 'install.ps1',
    'app.py', 'local_config.py', 'local_control.py', 'local_codex.py', 'local_driver.py',
    'wechat_bot.py', 'sender_reader.py', 'background_send.py', 'reply_bot.py',
    'codex_runner.py', 'reply_plan.py', 'group_profile.py', 'message_types.py',
    'sticker_library.py', 'native_stickers.py', 'native_group_tools.py',
    'build_release.py', 'test_local.py',
)


def build(root=ROOT, destination=None):
    root = Path(root)
    destination = Path(destination) if destination else root / 'dist'
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / f'QunYou-Local-{VERSION}.zip'
    prefix = f'QunYou-Local-{VERSION}/'
    hashes = {}
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            source = root / name
            if source.is_symlink() or not source.is_file() or source.resolve().parent != root.resolve():
                raise ValueError('Missing or redirected release source: ' + name)
            content = source.read_bytes()
            hashes[name] = hashlib.sha256(content).hexdigest()
            archive.writestr(prefix + name, content)
        archive.writestr(prefix + 'MANIFEST.json', json.dumps({'version': VERSION, 'sha256': hashes}, indent=2))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix('.zip.sha256').write_text(digest + '  ' + path.name + '\n', encoding='ascii')
    return path


if __name__ == '__main__':
    print(build())

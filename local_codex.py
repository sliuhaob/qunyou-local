"""Locate the recipient's CLI. Never copy or inspect authentication files."""
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent


def resolve_codex_executable():
    # Local npm installation includes a native executable; avoid shell shims.
    candidates = list((ROOT / 'tools/codex/node_modules/@openai').glob('**/codex.exe'))
    exe = shutil.which('codex.exe')
    if exe:
        candidates.append(Path(exe))
    for variable, suffix in [('LOCALAPPDATA', 'OpenAI/Codex/bin'), ('APPDATA', 'npm/node_modules/@openai')]:
        if os.environ.get(variable):
            candidates.extend((Path(os.environ[variable]) / suffix).glob('**/codex.exe'))
    candidates = [p for p in candidates if p.is_file()]
    if not candidates:
        raise RuntimeError('未找到 Codex CLI。请先运行 01-install.cmd，或安装官方 Codex CLI。')
    return str(candidates[0].resolve())


def cli(args, timeout=25):
    result = subprocess.run([resolve_codex_executable(), *args], capture_output=True,
                            encoding='utf-8', errors='replace', timeout=timeout,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return result.returncode, (result.stdout + '\n' + result.stderr).strip()


def check_codex():
    code, version = cli(['--version'])
    if code:
        raise RuntimeError('Codex CLI 无法运行：' + version[:300])
    code, help_text = cli(['exec', '--help'])
    needed = ('--ignore-user-config', '--ignore-rules', '--ephemeral', '--output-schema')
    if code or any(flag not in help_text for flag in needed):
        raise RuntimeError('Codex CLI 版本过旧，缺少隔离运行选项；重新运行安装脚本更新。')
    code, login = cli(['login', 'status'])
    if code:
        raise RuntimeError('请先点“登录自己的 GPT”完成官方登录。' + login[:200])
    return {'version': version, 'login': login, 'path': resolve_codex_executable()}

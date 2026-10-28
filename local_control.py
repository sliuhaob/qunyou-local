"""Local lifecycle only. No listening socket, remote control, or scheduled task."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import subprocess
import sys
import time
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parent
RUN = ROOT / '.runtime'


def process_stamp(pid):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
            return None
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


@contextmanager
def lifecycle_lock():
    import msvcrt
    # Outside .runtime so a configuration change can archive its data.
    with (ROOT / '.control.lock').open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError('另一个窗口正在启动或配置，请稍后再试。') from exc
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def status():
    launch = read_json(RUN / 'launch.json')
    actual = process_stamp(launch['pid']) if launch.get('pid') else None
    alive = actual is not None and actual == launch.get('stamp')
    result = read_json(RUN / 'bot_status.json')
    result['process_running'] = alive
    if not alive:
        result['state'] = 'stopped'
    elif not result.get('state'):
        result['state'] = 'starting'
    if result.get('heartbeat'):
        result['heartbeat_age_seconds'] = round(time.time() - result['heartbeat'], 1)
    return result


def save(data):
    from local_config import save_config
    with lifecycle_lock():
        if status()['process_running']:
            raise RuntimeError('请先停止群友，等待状态变为已停止，再修改配置。')
        return save_config(data)


def start():
    from local_config import validate_installation, atomic_json
    from local_codex import check_codex
    with lifecycle_lock():
        if status()['process_running']:
            return status()
        validate_installation()
        check_codex()
        RUN.mkdir(exist_ok=True)
        (RUN / 'STOP').unlink(missing_ok=True)
        (RUN / 'bot_status.json').unlink(missing_ok=True)
        with (RUN / 'bot.log').open('ab') as log:
            child = subprocess.Popen([sys.executable, '-X', 'utf8', str(ROOT / 'wechat_bot.py'), 'run'],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        stamp = process_stamp(child.pid)
        atomic_json(RUN / 'launch.json', {'pid': child.pid, 'stamp': stamp})
        for _ in range(16):
            if child.poll() is not None:
                raise RuntimeError('启动失败，请查看 .runtime/bot.log；常见原因是微信未登录或控件不可用。')
            if (RUN / 'bot_status.json').exists():
                break
            time.sleep(0.5)
        return status()


def stop():
    with lifecycle_lock():
        RUN.mkdir(exist_ok=True)
        (RUN / 'STOP').write_text('Stopped by local owner\n', encoding='utf-8')
    return {'stop_requested': True}


def doctor(wechat=False):
    from local_config import validate_installation
    from local_codex import check_codex
    config = validate_installation()
    result = {'codex': check_codex(), 'config': 'ok'}
    if wechat:
        from background_send import NativePort
        port = NativePort()
        controls = port.controls()
        if controls['title'].Name != config['group']:
            raise RuntimeError('请手动切到配置中的目标群，再检测。')
        count = controls.get('member_count')
        if count is None or str(count.Name).strip() not in (f"({len(config['members'])})", f"（{len(config['members'])}）"):
            raise RuntimeError('微信群成员数与名单不同，请补全名单。')
        result['wechat'] = '目标群与成员数量匹配；未输入或发送消息'
    return result


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else 'status'
    try:
        if action == 'login':
            from local_codex import resolve_codex_executable
            return subprocess.call([resolve_codex_executable(), 'login'])
        if action == 'probe':
            from local_config import validate_installation
            validate_installation()
            from reply_bot import CodexBackend
            backend = CodexBackend()
            reply = backend.reply('你好，请用一句话介绍自己。', sender='本地测试')
            result = {'reply': reply, 'sent': False, 'model': backend.last_call}
        elif action == 'doctor':
            result = doctor('--wechat' in sys.argv)
        else:
            if action not in ('start', 'stop', 'status'):
                raise ValueError('支持 start / stop / status / doctor / login / probe')
            result = globals()[action]()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

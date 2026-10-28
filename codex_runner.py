"""Return only a completed Codex JSONL turn; reap CLI shutdown separately."""
from __future__ import annotations
import collections
import json
import queue
import subprocess
import threading
import time


class CompletedReply:
    def __init__(self):
        self.text = None
        self.usage = None

    def accept(self, event):
        kind = event.get("type")
        if kind in ("turn.failed", "error"):
            raise RuntimeError("Codex 未成功完成本轮回复。")
        if kind == "item.completed" and event.get("item", {}).get("type") == "agent_message":
            self.text = event["item"].get("text")
        if kind == "turn.completed":
            if not isinstance(self.text, str) or not self.text.strip():
                raise RuntimeError("Codex 已结束，但没有完整回复。")
            self.usage = event.get("usage")
            return self.text
        return None


def run_completed(args, prompt, cwd, timeout=120, on_reaped=None):
    """Read successful turn completion, without waiting for telemetry shutdown.

    No partial chunks or unconfirmed agent messages are returned. Both pipes
    stay drained until the child is reaped. Only our own child is terminated
    on failure/timeout; the WeChat process is never accessed here.
    """
    started = time.monotonic()
    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               encoding="utf-8", errors="replace", cwd=cwd,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    events = queue.Queue()
    stderr = collections.deque(maxlen=80)
    first_event = None
    thread_started = None
    turn_started = None
    state = CompletedReply()

    def stdout_reader():
        try:
            for line in process.stdout:
                events.put(line)
        finally:
            events.put(None)

    def stderr_reader():
        for line in process.stderr:
            stderr.append(line)

    readers = [threading.Thread(target=stdout_reader, daemon=True),
               threading.Thread(target=stderr_reader, daemon=True)]
    for reader in readers:
        reader.start()

    def reap():
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        finally:
            for reader in readers:
                reader.join(timeout=3)
            process.stdout.close()
            process.stderr.close()
            if on_reaped is not None:
                on_reaped()

    try:
        process.stdin.write(prompt)
        process.stdin.close()
        while True:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("Codex 生成超时。")
            try:
                line = events.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError("Codex 生成超时。") from exc
            if line is None:
                raise RuntimeError("Codex 在确认本轮完成前退出。")
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            elapsed = time.monotonic() - started
            if first_event is None:
                first_event = round(elapsed, 2)
            if event.get("type") == "thread.started":
                thread_started = round(elapsed, 2)
            if event.get("type") == "turn.started":
                turn_started = round(elapsed, 2)
            reply = state.accept(event)
            if reply is not None:
                metadata = {"completion_event": "turn.completed", "seconds": round(elapsed, 2),
                            "first_event_seconds": first_event, "thread_started_seconds": thread_started,
                            "turn_started_seconds": turn_started, "usage": state.usage}
                # Let normal Python shutdown finish reaping before tempfile
                # finalizers run; this matters for Windows directory handles.
                threading.Thread(target=reap, daemon=False, name="codex-cleanup").start()
                return reply, metadata
    except BaseException:
        if not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            process.kill()
        reap()
        raise

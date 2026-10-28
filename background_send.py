"""Experimental UIA transport. No activation, focus, clipboard or key fallback.

doctor is read-only. send-test writes one fixed message to the allowed group.
Live test detected a foreground change without a sent-message echo.
Live sending is disabled; doctor remains available for read-only checks.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import sys
import time

from reply_bot import GROUP

TEST_TEXT = "本地测试"
LIVE_SEND_ENABLED = False


class NotReady(RuntimeError):
    pass


def send_with_patterns(port, text: str, *, resume_staged: bool = False) -> dict:
    before = port.inspect()
    if before["group"] != GROUP:
        raise NotReady("当前窗口不是指定群，禁止切换或发送。")
    if before["draft"] and not resume_staged:
        raise NotReady("输入框有未发送草稿，保留原文并停止。")
    if resume_staged and (before["draft"] != text or before["matching_rows"] != 0):
        raise NotReady("待续发草稿不匹配，或已有同文消息；停止。")
    if not before["value_pattern"] or not before["invoke_pattern"]:
        raise NotReady("微信没有提供所需的后台接口。")
    if before["idle_seconds"] < 3:
        raise NotReady("用户正在操作，停止本次测试。")
    if not resume_staged:
        port.set_value(text)
    # A changed foreground or draft is an unknown outcome: never send/retry.
    staged = port.inspect()
    if (staged["foreground"] != before["foreground"]
            or staged["input_tick"] != before["input_tick"]
            or staged["group"] != GROUP or staged["draft"] != text):
        details = {"foreground_changed": staged["foreground"] != before["foreground"],
                   "user_input_changed": staged["input_tick"] != before["input_tick"],
                   "group_changed": staged["group"] != GROUP,
                   "draft_matches": staged["draft"] == text}
        raise NotReady("写入后检查未通过，未调用发送：" + json.dumps(details))
    port.invoke_send()
    after = port.inspect()
    if after["foreground"] != before["foreground"]:
        raise NotReady("调用后检测到前台窗口变化，已停止后续操作。")
    if after["draft"]:
        raise NotReady("后台发送未生效或结果未知；草稿保留，不使用键鼠兜底。")
    if after["matching_rows"] <= before["matching_rows"]:
        raise NotReady("输入框已清空，但未看到新增消息，结果未知，禁止自动重发。")
    return {"status": "local_echo_observed", "group": GROUP,
            "text": text, "foreground_unchanged": True,
            "remote_delivery_confirmed": False}


class NativePort:
    def __init__(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent / ".deps"))
        try:
            import uiautomation as auto
            import win32gui
            import win32process
        except ImportError as exc:
            raise NotReady("缺少 UIA 依赖，参见 background-requirements.txt。") from exc
        self.auto, self.gui = auto, win32gui
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                     wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        candidates = []

        def consider(hwnd, _):
            if win32gui.GetWindowText(hwnd) not in ("微信", GROUP):
                return
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return
            try:
                size = wintypes.DWORD(32768)
                buf = ctypes.create_unicode_buffer(size.value)
                if kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                    if Path(buf.value).name.lower() == "weixin.exe":
                        # A separately opened login window may have the same
                        # title. Bind only an already logged-in main window.
                        if auto.ControlFromHandle(hwnd).ClassName == "mmui::MainWindow":
                            candidates.append(hwnd)
            finally:
                kernel.CloseHandle(handle)

        win32gui.EnumWindows(consider, None)
        if len(candidates) != 1:
            raise NotReady("未找到唯一微信窗口，不猜测目标。")
        self.window = auto.ControlFromHandle(candidates[0])
        self.text = TEST_TEXT

    def controls(self, *, require_input=True):
        queue = [(self.window, 0)]
        found = {"title": [], "edit": [], "send": [], "messages": [], "member_count": []}
        count = 0
        while queue:
            control, depth = queue.pop()
            count += 1
            if count > 2000 or depth > 45:
                raise NotReady("控件树超出预期，停止。")
            aid = control.AutomationId
            if aid.endswith("current_chat_name_label"):
                found["title"].append(control)
            if aid.endswith("current_chat_count_label"):
                found["member_count"].append(control)
            if aid == "chat_input_field" and control.ControlTypeName == "EditControl":
                found["edit"].append(control)
            if control.ControlTypeName == "ButtonControl" and control.Name == "发送":
                found["send"].append(control)
            if aid == "chat_message_list":
                found["messages"].append(control)
                continue  # Do not traverse chat bodies or other conversation histories.
            if aid == "session_list":
                continue
            queue.extend((child, depth + 1) for child in control.GetChildren())
        required = ("title", "edit", "send", "messages") if require_input else ("title", "messages")
        if any(len(found[key]) != 1 for key in required) or any(len(v) > 1 for v in found.values()):
            raise NotReady("目标群控件缺失或不唯一，不进行输入。")
        return {key: controls[0] for key, controls in found.items() if controls}

    def inspect(self):
        controls = self.controls()
        if controls["title"].Name != GROUP:
            raise NotReady("当前显示的不是指定群，不切换窗口。")
        value = controls["edit"].GetValuePattern()
        invoke = controls["send"].GetInvokePattern()

        class LastInputInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LastInputInfo()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            raise NotReady("无法读取空闲状态。")
        tick = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
        rows = controls["messages"].GetChildren()
        return {"group": GROUP, "draft": str(value.Value or "") if value else "",
                "value_pattern": value is not None, "invoke_pattern": invoke is not None,
                "foreground": self.gui.GetForegroundWindow(), "input_tick": info.dwTime,
                "idle_seconds": ((tick - info.dwTime) & 0xFFFFFFFF) / 1000,
                "matching_rows": sum(self.text in str(row.Name or "") for row in rows)}

    def set_value(self, text):
        controls = self.controls()
        if controls["title"].Name != GROUP:
            raise NotReady("群名称发生变化，停止。")
        value = controls["edit"].GetValuePattern()
        if value is None or str(value.Value or ""):
            raise NotReady("输入框接口不可用或已有草稿。")
        self.text = text
        value.SetValue(text)

    def invoke_send(self):
        controls = self.controls()
        if controls["title"].Name != GROUP:
            raise NotReady("群名称发生变化，停止。")
        value = controls["edit"].GetValuePattern()
        if value is None or value.Value != self.text:
            raise NotReady("草稿不匹配，停止。")
        invoke = controls["send"].GetInvokePattern()
        if invoke is None or not controls["send"].IsEnabled:
            raise NotReady("后台发送接口不可用。")
        invoke.Invoke()
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["doctor", "send-test", "send-staged-test"])
    parser.add_argument("--wait-idle", type=int, default=0, choices=range(0, 61),
                        metavar="0..60", help="发送前最多只读等待空闲的秒数")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        if args.mode != "doctor" and not LIVE_SEND_ENABLED:
            raise NotReady("本机测试检测到前台窗口变化且未确认发送，已禁用此发送路径，禁止自动重试。")
        port = NativePort()
        if args.mode == "doctor":
            state = port.inspect()
            state["draft_matches_test"] = state["draft"] == TEST_TEXT
            state["draft_present"] = bool(state.pop("draft"))
            state["status"] = "read_only_probe"
        else:
            deadline = time.monotonic() + args.wait_idle
            while port.inspect()["idle_seconds"] < 3 and time.monotonic() < deadline:
                time.sleep(0.5)
            state = send_with_patterns(port, TEST_TEXT, resume_staged=args.mode == "send-staged-test")
        print(json.dumps(state, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"status": "stopped", "error": str(exc)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

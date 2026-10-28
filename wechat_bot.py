"""Background receiver/draft generator with a brief foreground send.

The sharing edition uses UIA only. No login automation, process patching or
chat-supplied commands. The owner configures one local group before starting.
"""
from __future__ import annotations

import argparse
import collections
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import uuid

if __name__ == "__main__":
    # sender_reader shares these exception classes when the daemon is invoked
    # as a script, rather than loading a second copy of this module.
    sys.modules.setdefault("wechat_bot", sys.modules[__name__])

ROOT = Path(__file__).resolve().parent
RUN = ROOT / ".runtime"
sys.path.insert(0, str(ROOT / ".deps"))
from reply_bot import GROUP, CodexBackend, load_settings
from message_types import MESSAGE_CLASSES, message_kind, context_entry
from reply_plan import describe_messages, parse_messages
from group_profile import parse_actions, action_confirmation


class Busy(RuntimeError):
    pass


class DeliveryUnknown(RuntimeError):
    pass


class SkipReply(RuntimeError):
    """The source cannot safely be answered; no text has been staged."""


def message_id(control):
    return ":".join(map(str, control.GetRuntimeId()))


def observation_key(control):
    # Qt recycles row runtime IDs as messages scroll out. Include the exposed
    # name (sender + body in listening mode) so a new occupant isn't lost.
    digest = hashlib.sha256(str(control.Name).encode("utf-8")).hexdigest()[:24]
    return message_id(control) + ":" + digest


def direction(image):
    """Compare the first textured column at each edge; reject ambiguous crops.

    Same geometric principle as wechatauto's message-direction detector, with
    an explicit unknown state instead of guessing on blank/ambiguous captures.
    """
    im = image.convert("RGB")
    w, h = im.size
    if w < 160 or h < 30:
        return "unknown"
    p = im.load()
    y0, y1 = max(1, h // 10), min(h - 1, h * 9 // 10)

    def varied(x):
        base = p[x, y0]
        return any(max(abs(v - b) for v, b in zip(p[x, y], base)) > 16
                   for y in range(y0 + 1, y1, 2))

    left = next((x for x in range(3, w - 8) if varied(x)), w)
    right = next((w - 1 - x for x in range(w - 9, 2, -1) if varied(x)), w)
    if min(left, right) > w * 0.22 or abs(left - right) < 20:
        return "unknown"
    return "left" if left < right else "right"


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, body TEXT NOT NULL, reply TEXT,
            state TEXT NOT NULL, created REAL NOT NULL, error TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS listener_checkpoint (
            id INTEGER PRIMARY KEY CHECK(id=1), data_json TEXT NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS outgoing_parts (
            job_id TEXT NOT NULL, part_index INTEGER NOT NULL, state TEXT NOT NULL,
            actual_json TEXT, echo_id TEXT, sent_at REAL,
            PRIMARY KEY(job_id,part_index))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS group_actions (
            job_id TEXT NOT NULL, action_index INTEGER NOT NULL, state TEXT NOT NULL,
            result_json TEXT, updated REAL NOT NULL, PRIMARY KEY(job_id,action_index))""")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}
        for name, kind in (("context_json", "TEXT"), ("generation_started", "REAL"),
                           ("generated_at", "REAL"), ("send_started", "REAL"),
                           ("sent_at", "REAL"), ("model_call_json", "TEXT"),
                           ("source_id", "TEXT"), ("source_list_id", "TEXT"), ("sender", "TEXT"), ("sender_id", "TEXT"),
                           ("kind", "TEXT NOT NULL DEFAULT 'text'"), ("media_json", "TEXT"), ("response_json", "TEXT")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")
        self.db.commit()

    def recover(self):
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET state='pending' WHERE state='generating'")
            for row in self.db.execute("SELECT id,response_json FROM jobs WHERE state='sending'").fetchall():
                response = json.loads(row["response_json"]) if row["response_json"] else {}
                if response.get("kind") == "group_actions":
                    uncertain = any(a["state"] != "done" for a in self.group_action_parts(row["id"]))
                    self.db.execute("UPDATE jobs SET state=?,error=? WHERE id=?",
                                    ("unknown" if uncertain else "ready",
                                     "interrupted group rename" if uncertain else None, row["id"]))
                    continue
                parts = self.parts(row["id"])
                if (response.get("kind") == "silent"
                        or (response.get("kind") == "sequence" and response.get("messages") == [])) and not parts:
                    self.db.execute("UPDATE jobs SET state='silent',send_started=NULL,sent_at=NULL,error=NULL WHERE id=?", (row["id"],))
                    continue
                if response.get("kind") == "sequence" and not any(p["state"] in ("sending", "unknown") for p in parts):
                    complete = len([p for p in parts if p["state"] == "sent"]) == len(response["messages"])
                    self.db.execute("UPDATE jobs SET state=?,sent_at=CASE WHEN ? THEN ? ELSE sent_at END WHERE id=?",
                                    ("sent" if complete else "ready", complete, time.time(), row["id"]))
                else:
                    self.db.execute("UPDATE jobs SET state='unknown',error='interrupted send' WHERE id=?", (row["id"],))
                    self.db.execute("UPDATE outgoing_parts SET state='unknown' WHERE job_id=? AND state='sending'", (row["id"],))

    def group_action_parts(self, key):
        with self.lock:
            rows = self.db.execute("SELECT * FROM group_actions WHERE job_id=? ORDER BY action_index", (key,)).fetchall()
        return [dict(r) | {"result": json.loads(r["result_json"]) if r["result_json"] else None} for r in rows]

    def begin_group_action(self, key, index):
        with self.lock, self.db:
            if self.db.execute("SELECT 1 FROM group_actions WHERE job_id=? AND action_index=?", (key, index)).fetchone():
                raise DeliveryUnknown("改名动作已记录，不能重复写入")
            self.db.execute("INSERT INTO group_actions(job_id,action_index,state,updated) VALUES(?,?,'applying',?)",
                            (key, index, time.time()))

    def confirm_group_action(self, key, index, result):
        with self.lock, self.db:
            old = self.db.execute("SELECT state,result_json FROM group_actions WHERE job_id=? AND action_index=?", (key, index)).fetchone()
            encoded = json.dumps(result, ensure_ascii=False)
            if old and old[0] == "done" and old[1] != encoded:
                raise DeliveryUnknown("改名结果记录不一致")
            self.db.execute("INSERT OR REPLACE INTO group_actions(job_id,action_index,state,result_json,updated) VALUES(?,?,'done',?,?)",
                            (key, index, encoded, time.time()))

    def parts(self, key):
        with self.lock:
            rows = self.db.execute("SELECT * FROM outgoing_parts WHERE job_id=? ORDER BY part_index", (key,)).fetchall()
        return [dict(r) | {"actual": json.loads(r["actual_json"]) if r["actual_json"] else None} for r in rows]

    def begin_part(self, key, index):
        with self.lock, self.db:
            row = self.db.execute("SELECT state FROM outgoing_parts WHERE job_id=? AND part_index=?", (key, index)).fetchone()
            if row and row[0] in ("sent", "unknown", "sending"):
                raise DeliveryUnknown("分条消息已发送或结果未明，不重复尝试")
            self.db.execute("INSERT OR REPLACE INTO outgoing_parts(job_id,part_index,state) VALUES(?,?,'sending')", (key, index))

    def confirm_part(self, key, index, actual, echo_id):
        with self.lock, self.db:
            changed = self.db.execute("""UPDATE outgoing_parts SET state='sent',actual_json=?,echo_id=?,sent_at=?
                WHERE job_id=? AND part_index=? AND state='sending'""",
                (json.dumps(actual, ensure_ascii=False), echo_id, time.time(), key, index)).rowcount
            if changed != 1:
                raise DeliveryUnknown("分条发送确认记录不一致")

    def release_part(self, key, index, unknown=False):
        if index is None:
            return
        with self.lock, self.db:
            self.db.execute("UPDATE outgoing_parts SET state=? WHERE job_id=? AND part_index=? AND state='sending'",
                            ("unknown" if unknown else "ready", key, index))

    def load_checkpoint(self):
        with self.lock:
            row = self.db.execute("SELECT data_json FROM listener_checkpoint WHERE id=1").fetchone()
        try:
            return json.loads(row[0]) if row else None
        except (ValueError, TypeError):
            return None

    def save_checkpoint(self, data):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO listener_checkpoint(id,data_json) VALUES(1,?)",
                            (json.dumps(data, ensure_ascii=False),))

    def add(self, key, text, context=None, *, source_id=None, source_list_id=None, sender=None,
            kind="text", media=None, sender_id=None):
        with self.lock, self.db:
            self.db.execute("""INSERT OR IGNORE INTO jobs
                (id,body,state,created,context_json,source_id,source_list_id,sender,kind,media_json,sender_id)
                VALUES(?,?,'pending',?,?,?,?,?,?,?,?)""",
                (key, text, time.time(), json.dumps(context or [], ensure_ascii=False),
                 source_id, source_list_id, sender, kind, json.dumps(media or {}, ensure_ascii=False), sender_id))

    def next(self, state):
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE state=? ORDER BY created,id LIMIT 1", (state,)).fetchone()
            return dict(row) if row else None

    def update(self, key, state, reply=None, error=None, model_call=None, response=None):
        with self.lock, self.db:
            now = time.time()
            self.db.execute("""UPDATE jobs SET state=?, reply=COALESCE(?,reply), error=?,
                generation_started=CASE WHEN ?='generating' THEN ? ELSE generation_started END,
                generated_at=CASE WHEN ? IN ('ready','silent') THEN COALESCE(generated_at,?) ELSE generated_at END,
                send_started=CASE WHEN ?='sending' THEN ? ELSE send_started END,
                sent_at=CASE WHEN ?='sent' THEN ? ELSE sent_at END,
                model_call_json=COALESCE(?,model_call_json),response_json=COALESCE(?,response_json) WHERE id=?""",
                (state, reply, error, state, now, state, now, state, now, state, now,
                 json.dumps(model_call, ensure_ascii=False) if model_call is not None else None,
                 json.dumps(response, ensure_ascii=False) if response is not None else None, key))

    def last_delivery(self):
        with self.lock:
            row = self.db.execute("""SELECT * FROM jobs WHERE state='sent' OR EXISTS(
                SELECT 1 FROM outgoing_parts WHERE job_id=jobs.id AND state='sent') ORDER BY created DESC LIMIT 1""").fetchone()
        if not row:
            return None
        pairs = {"queue_wait_seconds": ("created", "generation_started"),
                 "generation_seconds": ("generation_started", "generated_at"),
                 "send_wait_seconds": ("generated_at", "send_started"),
                 "send_seconds": ("send_started", "sent_at"),
                 "total_seconds": ("created", "sent_at")}
        timings = {name: round(row[end] - row[start], 2) if row[start] is not None and row[end] is not None else None
                   for name, (start, end) in pairs.items()}
        response = json.loads(row["response_json"]) if row["response_json"] else {"kind": "text"}
        actual = [p["actual"] for p in self.parts(row["id"]) if p["state"] == "sent"]
        planned_count = len(response["messages"]) if response["kind"] == "sequence" else 1
        if response["kind"] == "sequence":
            response = {"kind": "sequence", "messages": actual}
            rendered = describe_messages(actual)
        else:
            rendered = "[表情]" + response.get("description", "") if response["kind"] == "sticker" else row["reply"]
        return {"event_id": row["id"], "sender": row["sender"], "kind": row["kind"],
                "media": json.loads(row["media_json"]) if row["media_json"] else {},
                "reply": rendered, "planned_count": planned_count,
                "delivered_count": len(actual) if response["kind"] == "sequence" else 1, "delivery_state": row["state"],
                "response": response, "accepted_at": row["created"],
                "sent_at": row["sent_at"], **timings,
                "model_call": json.loads(row["model_call_json"]) if row["model_call_json"] else None}

    def context(self, key=None):
        with self.lock:
            if key is not None:
                row = self.db.execute("SELECT context_json FROM jobs WHERE id=?", (key,)).fetchone()
                if row and row["context_json"] is not None:
                    return json.loads(row["context_json"])
            # Compatibility for pending work created before context snapshots.
            rows = self.db.execute("SELECT body,reply FROM jobs WHERE state='sent' ORDER BY created DESC LIMIT 6").fetchall()
        return [part for row in reversed(rows) for part in (
            {"role": "user", "text": row["body"][:1000]},
            {"role": "assistant", "text": row["reply"]})]

    def counts(self):
        with self.lock:
            return dict(self.db.execute("SELECT state,count(*) FROM jobs GROUP BY state").fetchall())

    def recent_responses(self):
        with self.lock:
            rows = self.db.execute("""SELECT id,response_json,COALESCE(sent_at,created) AS at FROM jobs WHERE state='sent'
                ORDER BY COALESCE(sent_at,created) DESC,id DESC LIMIT 60""").fetchall()
            parts = self.db.execute("SELECT actual_json,sent_at FROM outgoing_parts WHERE state='sent' ORDER BY sent_at DESC LIMIT 60").fetchall()
        events = [(r["at"], json.loads(r["response_json"]) if r["response_json"] else {"kind": "text"}) for r in rows]
        events = [(at, p) for at, p in events if p.get("kind") != "sequence"]
        events.extend((r["sent_at"], json.loads(r["actual_json"])) for r in parts)
        events.sort(key=lambda item: item[0])
        return [{"role": "assistant", "kind": p.get("kind", "text")} for _, p in events[-60:]]


class Transport:
    def __init__(self):
        from background_send import NativePort
        from local_driver import LocalDriver
        self.port = NativePort()
        self.driver = LocalDriver(self.port.gui, self.port.auto)
        # Bind only the already verified process/window. Never invoke upstream
        # wake/auto-login/accessibility-memory-patching or its database scanner.
        self.driver._win = self.port.window
        self.hwnd = self.port.window.NativeWindowHandle
        self.last_visit = None

    def controls(self):
        controls = self.port.controls(require_input=False)
        expected_group = self.group_profile.group_name if getattr(self, "group_profile", None) else GROUP
        if controls["title"].Name != expected_group:
            raise Busy("微信当前不是目标群，等待切回目标群")
        return controls

    def rows(self):
        controls = self.controls()
        return controls, controls["messages"].GetChildren()

    def activate(self):
        if self.input_state()["idle_seconds"] < 1.5:
            raise Busy("等待键鼠空闲")
        self.controls()
        if not self.driver._force_foreground(self.hwnd):
            raise Busy("微信无法置前，可能处于锁屏或其他桌面")
        # Let restore animation and Qt's row geometry settle before screenshot
        # verification. A shorter wait produced a stale crop on this client.
        time.sleep(0.35)
        self.controls()
        if self.port.gui.GetForegroundWindow() != self.hwnd:
            raise Busy("前台窗口已改变")

    @staticmethod
    def input_state():
        from ctypes import wintypes

        class LastInputInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        info = LastInputInfo()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            raise Busy("无法读取键鼠空闲状态")
        tick = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
        return {"tick": info.dwTime, "idle_seconds": ((tick - info.dwTime) & 0xFFFFFFFF) / 1000}

    def background_side(self, row):
        # A minimized UIA row still exposes text but not its author's identity.
        # Never activate a window or capture another app to classify it.
        if self.port.gui.IsIconic(self.hwnd) or self.port.gui.GetForegroundWindow() != self.hwnd:
            return "unknown"
        try:
            return self.classify(row)
        except Busy:
            return "unknown"

    def classify(self, row):
        from PIL import ImageGrab
        if self.port.gui.GetForegroundWindow() != self.hwnd:
            raise Busy("需要前台微信核对消息方向")
        controls = self.controls()
        r, box = row.BoundingRectangle, controls["messages"].BoundingRectangle
        if r.top < box.top or r.bottom > box.bottom or r.height() < 30:
            raise Busy("消息未完整显示，暂不猜测发送者")
        # A foreign overlay could make a desktop crop unreliable.
        for point in ((r.left + 40, r.top + 20), (r.right - 40, r.top + 20)):
            top = self.port.gui.GetAncestor(self.port.gui.WindowFromPoint(point), 2)
            if top != self.hwnd:
                raise Busy("消息被其他窗口遮挡")
        picture = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom), all_screens=True)
        if self.port.gui.GetForegroundWindow() != self.hwnd:
            raise Busy("截图时前台窗口已改变")
        return direction(picture)

    @staticmethod
    def source_row(controls, source_id, source_text, source_list_id, source_kind="text"):
        if source_id is None:  # Legacy jobs were classified before generation.
            return None
        if message_id(controls["messages"]) != source_list_id:
            raise SkipReply("消息列表已重建，丢弃旧列表的回复草稿")
        row = next((r for r in controls["messages"].GetChildren()
                    if message_id(r) == source_id), None)
        if (row is None or message_kind(row.ClassName, str(row.Name)) != source_kind
                or str(row.Name).strip() != source_text):
            raise SkipReply("原消息已离开列表或发生变化，无法核验，不发送")
        return row

    @staticmethod
    def empty_draft(controls):
        value = controls["edit"].GetValuePattern()
        if value is None or str(value.Value or ""):
            raise Busy("目标群有未发送草稿或接口不可用，等待用户处理")
        return value

    def minimize_after_visit(self, expected_tick):
        """Hide only our verified window, unless the user has taken over."""
        if self.port.gui.GetForegroundWindow() != self.hwnd:
            return "foreground_changed"
        controls = self.controls()
        self.empty_draft(controls)
        if self.input_state()["tick"] != expected_tick:
            return "user_input_changed"
        self.port.gui.ShowWindow(self.hwnd, 6)  # SW_MINIMIZE, no focus forcing elsewhere.
        return "minimized" if self.port.gui.IsIconic(self.hwnd) else "minimize_not_confirmed"

    def send(self, text, *, source_id=None, source_text=None, source_list_id=None):
        # Read-only preflight happens while WeChat can remain minimized.
        controls = self.controls()
        self.source_row(controls, source_id, source_text, source_list_id)
        self.empty_draft(controls)
        idle = self.input_state()
        if idle["idle_seconds"] < 1.5:
            raise Busy("回复已备好，等待键鼠空闲")
        expected_tick = idle["tick"]
        started = time.monotonic()
        cleanup = True
        outcome = "waiting"
        try:
            self.activate()
            controls = self.controls()
            self.empty_draft(controls)
            row = self.source_row(controls, source_id, source_text, source_list_id)
            if row is not None:
                try:
                    side = self.classify(row)
                except Busy as exc:
                    raise SkipReply("发送者核验失败，不发送：" + str(exc)) from exc
                if side != "left":
                    reason = "本人消息" if side == "right" else "无法确认发送者"
                    raise SkipReply(reason + "，丢弃预生成回复")
            if self.input_state()["tick"] != expected_tick:
                raise Busy("用户开始操作，暂缓发送")
            echo_id, expected_tick = self.send_foreground(text, expected_tick)
            outcome = "sent"
            return echo_id
        except SkipReply:
            outcome = "skipped"
            raise
        except Busy:
            raise
        except Exception:
            # A failed/uncertain send should remain available for inspection.
            cleanup = False
            outcome = "unknown"
            raise
        finally:
            minimized = "left_visible_for_inspection"
            if cleanup:
                try:
                    minimized = self.minimize_after_visit(expected_tick)
                except Exception as exc:
                    # Never turn a confirmed send into an uncertain send just
                    # because minimizing failed; that could provoke a resend.
                    minimized = "not_minimized: " + str(exc)
            self.last_visit = {"outcome": outcome, "seconds": round(time.monotonic() - started, 2),
                               "window_result": minimized}

    def send_foreground(self, text, expected_tick):
        controls = self.controls()
        edit = controls["edit"]
        self.empty_draft(controls)
        before_ids = {message_id(r) for r in controls["messages"].GetChildren()}
        # UIA focus avoids moving the owner's mouse. It is only used after
        # the allowed window is already foreground and the source is verified.
        edit.SetFocus()
        controls = self.controls()
        edit = controls["edit"]
        value = edit.GetValuePattern()
        if (self.port.gui.GetForegroundWindow() != self.hwnd or not edit.HasKeyboardFocus
                or value is None or str(value.Value or "")
                or self.input_state()["tick"] != expected_tick):
            raise Busy("输入框焦点或草稿发生变化")
        try:
            value.SetValue(text)
            time.sleep(0.1)
            controls = self.controls()
            edit = controls["edit"]
            if (self.port.gui.GetForegroundWindow() != self.hwnd or not edit.HasKeyboardFocus
                    or edit.GetValuePattern().Value != text
                    or self.input_state()["tick"] != expected_tick):
                raise DeliveryUnknown("写入后状态变化；保留草稿，不重发")
            edit.SendKeys("{Enter}", waitTime=0.05)
            expected_tick = self.input_state()["tick"]
            for _ in range(15):
                time.sleep(0.2)
                controls, rows = self.rows()
                if not str(controls["edit"].GetValuePattern().Value or ""):
                    for row in rows:
                        if row.Name == text and message_id(row) not in before_ids:
                            return message_id(row), expected_tick
        except Exception as exc:
            # Once staging begins, even Busy/target-change is NOT retryable.
            raise DeliveryUnknown("输入/发送结果未确认；不重发：" + str(exc)) from exc
        raise DeliveryUnknown("未看到新增消息回显；停止发送，不自动重试")


class Bot:
    def __init__(self, transport=None, backend=None, store=None):
        RUN.mkdir(exist_ok=True)
        if transport is None:
            load_settings(ROOT / 'bot_settings.json')
            from sender_reader import NamedTransport
            transport = NamedTransport()
        self.transport = transport
        self.stickers = getattr(transport, "sticker_library", None)
        self.backend = backend or CodexBackend()
        self.store = store or Store(RUN / "messages.sqlite3")
        self.stop = threading.Event()
        self.session = uuid.uuid4().hex
        self.seen = set()
        self.order = collections.deque()
        self.recent = collections.deque(maxlen=60)
        self.echoes = collections.OrderedDict()
        self.error = None
        self.state = "starting"
        self.generation_failures = 0
        self.generation_error = None
        self.last_status = 0
        self.list_id = None
        self.checkpoint_dirty = False
        self.resume_mode = None

    def remember(self, key):
        if key not in self.seen:
            self.seen.add(key)
            self.order.append(key)
            self.checkpoint_dirty = True
        while len(self.order) > 10000:
            self.seen.discard(self.order.popleft())

    def save_progress(self):
        if self.list_id is None or not self.checkpoint_dirty:
            return
        self.store.save_checkpoint({"version": 1, "group": GROUP, "list_id": self.list_id,
                                    "session": self.session, "seen": list(self.order),
                                    "context": list(self.recent)})
        self.checkpoint_dirty = False

    def resume_progress(self, controls, rows):
        saved = self.store.load_checkpoint()
        if not isinstance(saved, dict) or saved.get("version") != 1 or saved.get("group") != GROUP:
            return False
        seen, context = saved.get("seen"), saved.get("context")
        if (saved.get("list_id") != message_id(controls["messages"])
                or not isinstance(saved.get("session"), str) or not saved["session"]
                or not isinstance(seen, list) or not all(isinstance(k, str) for k in seen)
                or not isinstance(context, list) or not all(isinstance(c, dict) for c in context)):
            return False
        # Only resume an overlapping, unchanged UIA list. A new window/list or
        # wholly replaced history still gets a fresh baseline, never a replay.
        known = set(seen[-10000:])
        if not any(observation_key(row) in known for row in rows):
            return False
        self.session = saved["session"]  # Keeps INSERT OR IGNORE crash-safe.
        self.list_id = saved["list_id"]
        self.order = collections.deque(seen[-10000:])
        self.seen = known
        self.recent.extend(context[-60:])
        self.resume_mode = "saved_progress"
        return True

    def baseline(self):
        controls, rows = self.transport.rows()
        self.list_id = message_id(controls["messages"])
        self.resume_mode = "history_baseline"
        self.recent.clear()
        self.seen.clear()
        self.order.clear()
        self.checkpoint_dirty = True
        for row in rows:
            self.remember(observation_key(row))
            # Context only: history must never become reply jobs. UIA text does
            # not establish its author, so don't guess or force foreground.
            if row.ClassName in MESSAGE_CLASSES and str(row.Name).strip():
                try:
                    event = self.transport.read_message(row)
                    kind = message_kind(row.ClassName, event["text"])
                    if kind:
                        self.recent.append(context_entry(event, kind))
                except Busy:
                    pass  # Unattributed history must not masquerade as a person.
        self.save_progress()

    def poll(self):
        try:
            self.poll_messages()
        finally:
            self.save_progress()

    def poll_messages(self):
        self.transport.ensure_listening()
        controls, rows = self.transport.rows()
        if self.list_id is None:
            if not self.resume_progress(controls, rows):
                self.baseline()
                self.error = None
                return
        if self.list_id != message_id(controls["messages"]):
            self.baseline()
            self.error = "消息列表已重建，重新设定起点，避免补回历史消息"
            return
        candidates = [r for r in rows if observation_key(r) not in self.seen]
        message_rows = []
        for row in candidates:
            if row.ClassName in MESSAGE_CLASSES and str(row.Name).strip():
                message_rows.append(row)
            else:
                self.remember(observation_key(row))
        if not message_rows:
            return
        snapshots = []
        # Capturing/collecting the first sticker changes Qt row providers.
        # Read every author/body now, before any foreground operation, rather
        # than treating a recycled handle as the next member's original text.
        for row in message_rows:
            key = observation_key(row)
            source_id = message_id(row)
            try:
                event = self.transport.read_message(row)
            except Busy as exc:
                self.error = str(exc)
                continue
            body = event["text"]
            kind = message_kind(row.ClassName, body)
            snapshots.append((key, source_id, dict(event), kind))
        for key, source_id, event, kind in snapshots:
            body = event["text"]
            if kind is None:
                self.remember(key)
                continue
            if event["is_self"] and self.echoes.get(source_id) == body:
                self.remember(key)
                continue
            if not event["is_self"] and len(body) <= 2000:
                media = {}
                if kind == "sticker":
                    try:
                        media = self.transport.capture_sticker(source_id, body, self.list_id, event["sender"])
                    except Busy as exc:
                        self.error = str(exc)
                        # Preserve ordering: retry this source when input is idle.
                        return
                    except SkipReply as exc:
                        self.error = str(exc)
                        self.remember(key)
                        continue
                self.store.add(self.session + ":" + key, body, list(self.recent),
                               source_id=source_id, source_list_id=self.list_id, sender=event["sender"],
                               kind=kind, media=media)
            self.recent.append(context_entry(event, kind))
            self.remember(key)

    def generate(self):
        while not self.stop.is_set():
            if self.store.next("unknown"):
                self.stop.wait(0.5)
                continue
            job = self.store.next("pending")
            if not job or self.generation_failures >= 3:
                self.stop.wait(0.5)
                continue
            if not job["sender"]:
                self.store.update(job["id"], "skipped", error="旧任务缺少已核验发送者，不生成回复")
                continue
            self.store.update(job["id"], "generating")
            try:
                media = json.loads(job["media_json"]) if job["media_json"] else {}
                incoming_id = media.get("collection", {}).get("sticker_id", "")
                candidates = []
                if self.stickers is not None:
                    candidates = self.stickers.candidates(load_settings(ROOT / "bot_settings.json"), self.store.recent_responses())
                extra = {}
                if getattr(self.transport, "group_profile", None):
                    extra["group_profile"] = self.transport.group_profile.context(load_settings(ROOT / "bot_settings.json")["group_tools_mode"])
                reply = self.backend.reply(job["body"], context=self.store.context(job["id"]), sender=job["sender"],
                                           kind=job["kind"], media=media, sticker_candidates=candidates, **extra)
                decision = getattr(self.backend, "last_decision", None) or {}
                response = {"kind": "text"}
                if self.stickers is not None:
                    if incoming_id:
                        self.stickers.annotate(incoming_id, decision.get("sticker_description", ""), decision.get("sticker_tags", []))
                    choice = next((s for s in candidates if s["id"] == decision.get("sticker_id")), None)
                    if choice:
                        response = {"kind": "sticker", "sticker_id": choice["id"], "description": choice["description"]}
                if isinstance(decision.get("messages"), list):
                    allowed = {c["id"]: c for c in candidates}
                    parts = parse_messages(decision, allowed, load_settings(ROOT / "bot_settings.json")["max_reply_parts"])
                    for part in parts:
                        if part["kind"] == "sticker":
                            part["description"] = allowed[part["sticker_id"]]["description"]
                    response = {"kind": "sequence" if parts else "silent", "messages": parts}
                actions = parse_actions(decision, enabled=bool(extra.get("group_profile", {}).get("rename_available")) and job["kind"] == "text")
                if actions:
                    response = {"kind": "group_actions", "actions": actions}
                self.store.update(job["id"], "silent" if response["kind"] == "silent" else "ready", reply=reply,
                                  model_call=getattr(self.backend, "last_call", None), response=response)
                self.generation_failures = 0
                self.generation_error = None
            except Exception as exc:
                self.generation_failures += 1
                self.generation_error = "模型生成失败：" + str(exc)
                self.store.update(job["id"], "error", error=self.generation_error)

    def send_ready(self):
        if self.store.next("unknown"):
            self.state = "paused_unknown_delivery"
            return
        job = self.store.next("ready")
        if not job:
            return
        if not job["sender"]:
            self.store.update(job["id"], "skipped", error="缺少已核验发送者，不发送回复")
            return
        response = json.loads(job["response_json"]) if job["response_json"] else {"kind": "text"}
        if response["kind"] == "group_actions":
            self.perform_group_actions(job, response)
            return
        if response["kind"] == "silent":
            self.store.update(job["id"], "silent", reply="")
            return
        if response["kind"] == "sequence":
            self.send_sequence_job(job, response)
            return
        # Save the transition before UI actions; a crash cannot silently resend.
        self.store.update(job["id"], "sending")
        try:
            response = json.loads(job["response_json"]) if job["response_json"] else {"kind": "text"}
            sticker = None
            if response["kind"] == "sticker" and self.stickers is not None:
                allowed = self.stickers.candidates(load_settings(ROOT / "bot_settings.json"), self.store.recent_responses())
                if any(s["id"] == response["sticker_id"] for s in allowed):
                    sticker = self.stickers.get(response["sticker_id"])
            extra = {"sticker": sticker} if sticker is not None else {}
            echo_id = self.transport.send(job["reply"], source_id=job["source_id"],
                                          source_text=job["body"], source_list_id=job["source_list_id"],
                                          source_sender=job["sender"], source_kind=job["kind"], **extra)
            used_sticker = sticker is not None and getattr(self.transport, "last_sent_kind", "text") == "sticker"
            if used_sticker:
                self.stickers.mark_used(sticker["id"])
                self.recent.append({"role": "assistant", "kind": "sticker", "text": response["description"]})
                self.echoes[echo_id] = "动画表情"
            else:
                response = {"kind": "text"}
                self.recent.append({"role": "assistant", "text": job["reply"]})
                self.echoes[echo_id] = job["reply"]
            while len(self.echoes) > 1000:
                self.echoes.popitem(last=False)
            self.store.update(job["id"], "sent", response=response)
            self.checkpoint_dirty = True
            self.error = None
        except SkipReply as exc:
            self.store.update(job["id"], "skipped", error=str(exc))
            self.error = str(exc)
        except Busy:
            self.store.update(job["id"], "ready")
            raise
        except Exception as exc:
            self.error = str(exc)
            self.store.update(job["id"], "unknown", error=self.error)
            self.state = "paused_unknown_delivery"
        # A checkpoint failure must not turn an already confirmed send unknown.
        self.save_progress()

    def perform_group_actions(self, job, response):
        from native_group_tools import GroupActionUnavailable
        self.store.update(job["id"], "sending")
        try:
            enabled = load_settings(ROOT / "bot_settings.json")["group_tools_mode"] != "off"
            actions = parse_actions(response, enabled=True)
            results = []
            for index, action in enumerate(actions):
                prior = next((p for p in self.store.group_action_parts(job["id"]) if p["action_index"] == index), None)
                if prior:
                    if prior["state"] != "done":
                        raise DeliveryUnknown("改名动作结果未明，不再次执行")
                    results.append(prior["result"])
                    continue
                operation_id = job["id"] + ":rename:" + str(index)
                try:
                    if not enabled:
                        raise GroupActionUnavailable("改名能力已关闭")
                    if self.stop.is_set() or (getattr(self, "live_run", False) and (RUN / "STOP").exists()):
                        raise Busy("已停止，保留未执行的改名动作")
                    result = self.transport.rename_in_group(action, operation_id,
                        lambda i=index: self.store.begin_group_action(job["id"], i),
                        source_id=job["source_id"], source_text=job["body"], source_list_id=job["source_list_id"],
                        source_sender=job["sender"], source_kind=job["kind"])
                except (GroupActionUnavailable, ValueError) as exc:
                    result = {"kind": action["kind"], "value": action["value"], "status": "failed", "reason": str(exc)}
                self.store.confirm_group_action(job["id"], index, result)
                results.append(result)
            # Only observed outcomes can become chat text. Never send a model's
            # speculative "done" message for an action that failed.
            parts = action_confirmation(results)
            self.store.update(job["id"], "ready", reply=describe_messages(parts),
                              response={"kind": "sequence", "messages": parts, "group_action_results": results})
            self.error = None
        except Busy:
            self.store.update(job["id"], "ready")
            raise
        except SkipReply as exc:
            self.store.update(job["id"], "skipped", error=str(exc))
            self.error = str(exc)
        except Exception as exc:
            self.store.update(job["id"], "unknown", error=str(exc))
            self.error, self.state = str(exc), "paused_unknown_delivery"

    def send_sequence_job(self, job, response):
        if response["messages"] == [] and not self.store.parts(job["id"]):
            self.store.update(job["id"], "silent", reply="")
            return
        confirmed = [p for p in self.store.parts(job["id"]) if p["state"] == "sent"]
        start = len(confirmed)
        if [p["part_index"] for p in confirmed] != list(range(start)):
            self.store.update(job["id"], "unknown", error="分条发送进度不连续")
            self.state = "paused_unknown_delivery"
            return
        if start == len(response["messages"]):
            self.store.update(job["id"], "sent")
            return
        settings = load_settings(ROOT / "bot_settings.json")
        active = None

        def resolve(part):
            resolved = dict(part)
            if part["kind"] == "sticker":
                allowed = self.stickers.candidates(load_settings(ROOT / "bot_settings.json"), self.store.recent_responses()) if self.stickers else []
                asset = self.stickers.get(part["sticker_id"]) if any(s["id"] == part["sticker_id"] for s in allowed) else None
                resolved["sticker"] = asset
                if asset is None:
                    resolved["kind"] = "text"
            return resolved

        def before(index):
            nonlocal active
            self.store.begin_part(job["id"], index)
            active = index

        def after(index, actual, echo_id):
            nonlocal active
            self.store.confirm_part(job["id"], index, actual, echo_id)
            active = None
            if actual["kind"] == "sticker":
                self.stickers.mark_used(actual["sticker_id"])
                self.recent.append({"role": "assistant", "kind": "sticker", "text": actual["description"]})
                self.echoes[echo_id] = "动画表情"
            else:
                self.recent.append({"role": "assistant", "text": actual["text"]})
                self.echoes[echo_id] = actual["text"]
            while len(self.echoes) > 1000:
                self.echoes.popitem(last=False)
            self.checkpoint_dirty = True
            self.save_progress()

        self.store.update(job["id"], "sending")
        try:
            self.transport.send_sequence(response["messages"], start_index=start, resolve_part=resolve,
                before_part=before, after_part=after, interval=settings["reply_part_interval_seconds"],
                should_stop=lambda: self.stop.is_set() or (getattr(self, "live_run", False) and (RUN / "STOP").exists()),
                source_id=job["source_id"], source_text=job["body"], source_list_id=job["source_list_id"],
                source_sender=job["sender"], source_kind=job["kind"])
            if len([p for p in self.store.parts(job["id"]) if p["state"] == "sent"]) != len(response["messages"]):
                raise DeliveryUnknown("未确认所有分条消息")
            self.store.update(job["id"], "sent")
            self.error = None
        except Busy:
            self.store.release_part(job["id"], active)
            self.store.update(job["id"], "ready")
            raise
        except SkipReply as exc:
            self.store.release_part(job["id"], active)
            state = "partial" if any(p["state"] == "sent" for p in self.store.parts(job["id"])) else "skipped"
            self.store.update(job["id"], state, error=str(exc))
            self.error = str(exc)
        except Exception as exc:
            self.store.release_part(job["id"], active, unknown=True)
            self.store.update(job["id"], "unknown", error=str(exc))
            self.error, self.state = str(exc), "paused_unknown_delivery"

    def status(self, force=False):
        if not force and time.monotonic() - self.last_status < 2:
            return
        generation_paused = self.generation_failures >= 3
        data = {"pid": os.getpid(), "group": GROUP,
                "state": "paused_generation" if generation_paused and self.state == "listening" else self.state,
                "heartbeat": time.time(), "counts": self.store.counts(),
                "error": self.error or self.generation_error,
                "generation_failures": self.generation_failures,
                "generation_paused": generation_paused,
                "generation_error": self.generation_error,
                "transport": "background_named_read_brief_foreground_send",
                "scope": "new_visible_text_link_sticker_in_current_group",
                "supported_kinds": ["text", "link", "sticker"],
                "sticker_capture": "brief_foreground_single_frame",
                "sticker_library": self.stickers.stats() if self.stickers is not None else None,
                "model_settings": getattr(self.backend, "settings", None),
                "group_profile": self.transport.group_profile.context(load_settings(ROOT / "bot_settings.json")["group_tools_mode"])
                    if getattr(self.transport, "group_profile", None) else None,
                "last_model_call": getattr(self.backend, "last_call", None),
                "last_delivery": self.store.last_delivery(),
                "last_window_visit": getattr(self.transport, "last_visit", None),
                "sender_identity": "verified_group_display_name",
                "resume_mode": self.resume_mode,
                "context_rows": len(self.recent)}
        temp = RUN / "bot_status.tmp"
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(RUN / "bot_status.json")
        self.last_status = time.monotonic()

    def run(self):
        self.live_run = True
        # Windows named mutex prevents duplicate responders, including restarts.
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        mutex = k32.CreateMutexW(None, False, "Local\\QunYouLocal_" + hashlib.sha256(GROUP.encode()).hexdigest()[:20])
        if not mutex or ctypes.get_last_error() == 183:
            raise RuntimeError("群友程序已经在运行")
        self.store.recover()
        # Establish history inside the guarded poll loop. Starting while the
        # owner is in another chat should wait, not terminate the responder.
        worker = threading.Thread(target=self.generate, daemon=True, name="codex-replies")
        worker.start()
        try:
            while not (RUN / "STOP").exists():
                try:
                    self.state = "listening"
                    self.poll()
                    if (RUN / "STOP").exists():
                        break
                    self.send_ready()
                except Busy as exc:
                    self.state = "waiting"
                    self.error = str(exc)
                except Exception as exc:
                    self.state = "waiting"
                    self.error = str(exc)
                self.status()
                time.sleep(0.6)
        finally:
            self.stop.set()
            self.state = "stopped"
            self.status(force=True)
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            k32.CloseHandle(mutex)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["run", "inspect"])
    args = parser.parse_args()
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
    if args.mode == "inspect":
        transport = Transport()
        transport.activate()
        _, rows = transport.rows()
        result = []
        for row in rows:
            if row.ClassName == "mmui::ChatTextItemView":
                try:
                    side = transport.classify(row)
                except Busy as exc:
                    side = str(exc)
                result.append({"text": row.Name, "direction": side, "id": message_id(row)})
        print(json.dumps(result, ensure_ascii=False))
    else:
        from local_config import validate_installation
        validate_installation(ROOT)
        Bot().run()


if __name__ == "__main__":
    main()

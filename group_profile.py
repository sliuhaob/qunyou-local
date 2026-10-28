"""Owner-scoped display names and a durable journal for native rename actions."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import threading
import time
import unicodedata
import uuid

from local_config import configured_group
GROUP = configured_group()  # Fixed at process start, never supplied by chat.
ACTION_FIELDS = {"rename_self": "self_name", "rename_group": "group_name"}
ACTION_LABELS = {"rename_self": "本群昵称", "rename_group": "群名"}


def validate_name(kind, value):
    if kind not in ACTION_FIELDS or not isinstance(value, str):
        raise ValueError("不支持的改名动作")
    value = value.strip()
    maximum = 24 if kind == "rename_self" else 40
    if not value or len(value.encode("utf-16-le", errors="surrogatepass")) // 2 > maximum:
        raise ValueError(f"{ACTION_LABELS[kind]}需要1到{maximum}个字符（表情可能占两个字符）")
    if any(unicodedata.category(c) in ("Cc", "Cs") or c in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069" for c in value):
        raise ValueError("名字不能包含换行、控制字符或方向控制符")
    return value


def parse_actions(data, enabled=False):
    actions = data.get("actions", [])
    if not isinstance(actions, list) or len(actions) > 2:
        raise ValueError("改名动作格式无效")
    if actions and not enabled:
        raise ValueError("本轮没有启用群改名能力")
    result, used = [], set()
    for action in actions:
        if not isinstance(action, dict) or set(action) != {"kind", "value"}:
            raise ValueError("改名动作只能包含 kind 和 value")
        kind = action["kind"]
        value = validate_name(kind, action["value"])
        if kind in used:
            raise ValueError("同一轮不能反复修改同一个名字")
        used.add(kind)
        result.append({"kind": kind, "value": value})
    return result


def atomic_json(path, data):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class GroupProfile:
    def __init__(self, path, sender_settings):
        self.path = Path(path)
        self.lock = threading.RLock()
        if sender_settings.get("group") != GROUP:
            raise ValueError("群资料不属于已授权群")
        self.original_self = sender_settings["self_name"]
        self.other_members = [n for n in sender_settings["members"] if n != self.original_self]
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        else:
            self.data = {"version": 1, "group_key": GROUP, "group_name": GROUP,
                         "self_name": self.original_self, "self_aliases": [self.original_self],
                         "revision": 0, "pending": None, "last_result": None}
            atomic_json(self.path, self.data)
        if self.data.get("version") != 1 or self.data.get("group_key") != GROUP:
            raise ValueError("群资料标识无效，不扩大接入范围")
        validate_name("rename_group", self.data["group_name"])
        self.validate("rename_self", self.data["self_name"])
        if (not isinstance(self.data.get("self_aliases"), list)
                or self.data["self_name"] not in self.data["self_aliases"]
                or any(not isinstance(a, str) or not a or a in self.other_members for a in self.data["self_aliases"])):
            raise ValueError("本人昵称历史无效或与群成员冲突")
        if len(set(self.data["self_aliases"])) != len(self.data["self_aliases"]):
            raise ValueError("本人昵称历史重复")
        for alias in self.data["self_aliases"]:
            self.validate("rename_self", alias)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    @property
    def group_name(self):
        return self.snapshot()["group_name"]

    def context(self, mode="requested"):
        data = self.snapshot()
        return {"group_name": data["group_name"], "self_group_nickname": data["self_name"],
                "rename_mode": mode, "rename_available": mode != "off" and not data["pending"],
                "scope": "current_authorized_group_only"}

    def validate(self, kind, value):
        value = validate_name(kind, value)
        if kind == "rename_self" and any(value == name or value.startswith(name + " ")
                                         or name.startswith(value + " ") for name in self.other_members):
            raise ValueError("新昵称与群成员重名或前缀有歧义，会影响发送者识别")
        return value

    def begin(self, operation_id, kind, value, binding):
        with self.lock:
            if self.data["pending"]:
                raise RuntimeError("上一次改名结果待核对，不继续改名")
            value = self.validate(kind, value)
            state = copy.deepcopy(self.data)
            state["pending"] = {"operation_id": operation_id, "kind": kind,
                                "old": state[ACTION_FIELDS[kind]], "new": value,
                                "binding": binding, "started_at": time.time()}
            atomic_json(self.path, state)
            self.data = state

    def complete(self, operation_id, observed):
        with self.lock:
            pending = self.data["pending"]
            if not pending or pending["operation_id"] != operation_id or observed != pending["new"]:
                raise RuntimeError("改名回读或操作标识不一致")
            state = copy.deepcopy(self.data)
            state[ACTION_FIELDS[pending["kind"]]] = observed
            if pending["kind"] == "rename_self" and observed not in state["self_aliases"]:
                state["self_aliases"].append(observed)
            result = {"operation_id": operation_id, "kind": pending["kind"],
                      "old": pending["old"], "value": observed, "status": "changed"}
            state.update(pending=None, last_result=result, revision=state["revision"] + 1)
            atomic_json(self.path, state)
            self.data = state
            return result


def action_confirmation(results):
    sentences = []
    for result in results:
        label = ACTION_LABELS[result["kind"]]
        if result["status"] == "failed":
            sentences.append(f"主人，{label}没改成：{result['reason']}")
        elif result["status"] == "unchanged":
            sentences.append(f"{label}本来就叫「{result['value']}」呀")
        else:
            sentences.append(f"{label}改成「{result['value']}」了，主人")
    return [{"kind": "text", "text": sentence[:300], "sticker_id": ""} for sentence in sentences]

"""Rename only the bot's group nickname or the currently authorized group's title."""
from __future__ import annotations

import time

from group_profile import ACTION_FIELDS, ACTION_LABELS
from native_stickers import walk
from wechat_bot import Busy, DeliveryUnknown, SkipReply, message_id


class GroupActionUnavailable(RuntimeError):
    """No rename value was written; a failure acknowledgement is safe."""


class NativeGroupUI:
    LABELS = {"rename_self": "我在本群的昵称", "rename_group": "群聊名称"}

    def __init__(self, transport):
        self.t = transport
        self.tick = transport.input_state()["tick"]
        self.container = None
        self.grid_id = None
        self.list_id = message_id(transport.controls()["messages"])
        self.original_group = transport.group_profile.group_name
        self.allowed_titles = {self.original_group}
        self.staged = False

    def guard(self):
        controls = self.t.port.controls(require_input=False)
        count = controls.get("member_count")
        if (controls["title"].Name not in self.allowed_titles
                or message_id(controls["messages"]) != self.list_id
                or count is None or str(count.Name).strip() not in
                (f"({self.t.catalog.count})", f"（{self.t.catalog.count}）")):
            raise Busy("改名期间目标群或消息列表发生变化")
        if self.t.port.gui.GetForegroundWindow() != self.t.hwnd:
            raise Busy("前台已改变，停止群设置操作")
        if self.t.input_state()["tick"] != self.tick:
            raise Busy("用户开始操作，停止群设置操作")
        return controls

    def panel(self):
        found = [c for c in walk(self.t.port.window) if c.ClassName == "mmui::ChatRoomMemberInfoView"]
        if len(found) > 1:
            raise GroupActionUnavailable("群信息面板不唯一")
        return found[0] if found else None

    def click(self, control):
        self.guard()
        rect = control.BoundingRectangle
        if not control.IsEnabled or rect.width() < 5 or rect.height() < 5:
            raise GroupActionUnavailable("改名控件不可用或没有编辑权限")
        for point in ((rect.left + 2, rect.top + 2), (rect.right - 2, rect.bottom - 2)):
            if self.t.port.gui.GetAncestor(self.t.port.gui.WindowFromPoint(point), 2) != self.t.hwnd:
                raise Busy("群设置被其他窗口遮挡")
        if not self.t.driver._click_ctrl(control):
            raise GroupActionUnavailable("无法点击已核验的群设置控件")
        self.tick = self.t.input_state()["tick"]
        time.sleep(0.12)

    def toggle_panel(self):
        buttons = [c for c in walk(self.t.port.window)
                   if c.ControlTypeName == "ButtonControl" and c.Name == "聊天信息"]
        if len(buttons) != 1:
            raise GroupActionUnavailable("聊天信息入口不可用")
        self.click(buttons[0])

    def open(self):
        self.guard()
        if self.panel() is None:
            self.toggle_panel()
        if self.panel() is None:
            raise GroupActionUnavailable("群信息面板未打开")
        self.fields()

    def fields(self):
        panel = self.panel()
        if panel is None:
            raise GroupActionUnavailable("群信息面板已关闭")
        grids = [c for c in walk(panel) if c.AutomationId == "chat_member_list"]
        if len(grids) != 1:
            raise GroupActionUnavailable("群成员容器不唯一")
        grid = grids[0]
        # Qt's logical list children omit the settings footer. A visible member
        # exposes the real QFWidget parent and its settings through UIA.
        members = [c for c in grid.GetChildren() if c.ClassName == "mmui::ChatMemberCell"]
        if members:
            self.container = members[0].GetParentControl()
            self.grid_id = message_id(grid)
        if self.container is None or self.container.ClassName != "QFWidget" or self.grid_id != message_id(grid):
            raise GroupActionUnavailable("无法核验群设置所在容器")
        parent_grid = self.container.GetParentControl().GetParentControl()
        if message_id(parent_grid) != message_id(grid):
            raise GroupActionUnavailable("群设置容器归属改变")
        controls = [c for c in walk(self.container) if c.ClassName == "mmui::XPreviewInputField"]
        result = {}
        for kind, label in self.LABELS.items():
            found = [c for c in controls if c.Name == label and c.ControlTypeName == "ButtonControl"]
            if len(found) != 1:
                raise GroupActionUnavailable(label + "编辑入口不可用")
            result[kind] = found[0]
        return result

    @staticmethod
    def preview(field):
        edits = [c for c in walk(field) if c.ClassName == "mmui::XLineEdit"]
        if edits:
            raise GroupActionUnavailable("字段仍处于编辑状态，不能当作已保存结果")
        values = [str(c.Name) for c in walk(field) if c.ClassName == "mmui::XTextView"]
        if len(values) != 1:
            raise GroupActionUnavailable("无法唯一回读名字")
        return values[0]

    def reveal(self, kind):
        for _ in range(8):
            self.guard()
            field = self.fields()[kind]
            box, area = field.BoundingRectangle, self.panel().BoundingRectangle
            if box.top >= area.top + 70 and box.bottom <= area.bottom - 12:
                return field
            direction = -240 if box.bottom > area.bottom - 12 else 240
            x, y = area.right - 24, area.bottom - 40
            if self.t.port.gui.GetAncestor(self.t.port.gui.WindowFromPoint((x, y)), 2) != self.t.hwnd:
                raise Busy("群信息面板被遮挡")
            self.t.driver._wheel_at(x, y, direction)
            self.tick = self.t.input_state()["tick"]
            time.sleep(0.15)
        raise GroupActionUnavailable("改名字段未完整显示")

    def editor(self, kind):
        field = self.fields()[kind]
        editors = [c for c in walk(field) if c.ClassName == "mmui::XLineEdit"
                   and c.ControlTypeName == "EditControl"]
        if len(editors) != 1:
            raise GroupActionUnavailable("改名输入框不可用")
        focused = self.t.port.auto.GetFocusedControl()
        if message_id(focused) != message_id(editors[0]):
            raise Busy("改名输入框焦点已改变")
        value = editors[0].GetValuePattern()
        if value is None or value.IsReadOnly:
            raise GroupActionUnavailable("改名输入框不可写")
        return editors[0], value

    def focus(self, kind):
        field = self.reveal(kind)
        before = self.preview(field)
        self.click(field)
        for _ in range(6):
            try:
                editor, value = self.editor(kind)
                if str(value.Value) != before:
                    raise GroupActionUnavailable("编辑框原值与字段不一致")
                return editor, value, before
            except GroupActionUnavailable:
                time.sleep(0.08)
        raise GroupActionUnavailable("未能打开改名输入框")

    def cancel(self, kind):
        self.guard()
        editor, _ = self.editor(kind)
        editor.SendKeys("{Esc}", waitTime=0.1)
        self.tick = self.t.input_state()["tick"]

    def close(self):
        self.guard()
        if self.panel() is not None:
            self.toggle_panel()
            if self.panel() is not None:
                raise Busy("群信息面板未关闭")

    def apply(self, action, operation_id, before_write):
        kind = action["kind"]
        value = self.t.group_profile.validate(kind, action["value"])
        self.open()
        current = self.preview(self.fields()[kind])
        expected = self.t.group_profile.snapshot()[ACTION_FIELDS[kind]]
        if current != expected:
            raise GroupActionUnavailable("微信中的名字与本地记录不同，需要先核对")
        if current == value:
            return {"kind": kind, "value": value, "old": current, "status": "unchanged"}
        editor, pattern, _ = self.focus(kind)
        self.guard()
        binding = {"hwnd": self.t.hwnd, "pid": self.t.port.window.ProcessId,
                   "list_id": self.list_id, "group_name": self.original_group}
        before_write()
        self.t.group_profile.begin(operation_id, kind, value, binding)
        # A focus loss may commit the edit, so every failure after SetValue is
        # uncertain and must stop rather than retry or send a success message.
        self.staged = True
        try:
            if kind == "rename_group":
                self.allowed_titles.add(value)
            pattern.SetValue(value)
            self.guard()
            editor, pattern = self.editor(kind)
            if str(pattern.Value) != value:
                raise DeliveryUnknown("改名输入框内容未完整写入")
            editor.SendKeys("{Enter}", waitTime=0.1)
            self.tick = self.t.input_state()["tick"]
            for _ in range(15):
                self.guard()
                try:
                    observed = self.preview(self.fields()[kind])
                except GroupActionUnavailable:
                    observed = None
                header = self.t.port.controls(require_input=False)["title"].Name
                if observed == value and (kind != "rename_group" or header == value):
                    result = self.t.group_profile.complete(operation_id, observed)
                    self.t.refresh_profile()
                    self.staged = False
                    return result
                time.sleep(0.1)
            raise DeliveryUnknown("尚未确认改名保存结果，已暂停，禁止自动重试")
        except Exception as exc:
            raise DeliveryUnknown("改名已进入写入阶段，结果待核对：" + str(exc)) from exc


def rename_in_group(transport, action, operation_id, before_write, *, source_id,
                    source_text, source_list_id, source_sender, source_kind="text"):
    if source_kind != "text":
        raise GroupActionUnavailable("改名只接受群里的文字互动")
    transport.group_profile.validate(action["kind"], action["value"])
    if transport.group_profile.snapshot()["pending"]:
        raise DeliveryUnknown("上一次改名结果尚未核对")
    transport.named_source(source_id, source_text, source_list_id, source_sender, source_kind)
    started = time.monotonic()
    idle = transport.input_state()
    if idle["idle_seconds"] < 1.5:
        raise Busy("等待键鼠空闲后执行群改名")
    transport.activate()
    ui = NativeGroupUI(transport)
    ui.tick = idle["tick"]
    outcome, warning, result = "group_action", None, None
    try:
        ui.guard()
        transport.named_source(source_id, source_text, source_list_id, source_sender, source_kind)
        if ui.panel() is not None:
            ui.close()
        transport.exit_selection()
        ui.tick = transport.input_state()["tick"]
        transport.source_row(transport.controls(), source_id, source_text, source_list_id, source_kind)
        result = ui.apply(action, operation_id, before_write)
        return result
    except GroupActionUnavailable:
        outcome = "group_action_unavailable"
        raise
    except DeliveryUnknown:
        outcome = "group_action_unknown"
        raise
    finally:
        window_result = "left_for_inspection"
        if not ui.staged:
            try:
                if transport.port.gui.GetForegroundWindow() == transport.hwnd and transport.input_state()["tick"] == ui.tick:
                    # Only cancel an observed editor, never send Esc in bare chat.
                    if ui.panel() is not None:
                        for kind, field in ui.fields().items():
                            if any(c.ClassName == "mmui::XLineEdit" for c in walk(field)):
                                ui.cancel(kind)
                        ui.close()
                    transport.enter_selection()
                    ui.tick = transport.input_state()["tick"]
                window_result = transport.minimize_after_visit(ui.tick)
            except Exception as exc:
                warning = str(exc)
                transport.reader_prepare_error = warning
                transport.reader_retry_tick = transport.input_state()["tick"]
        transport.last_visit = {"outcome": outcome, "seconds": round(time.monotonic() - started, 2),
                                "window_result": window_result, "warning": warning, "group_action": result}

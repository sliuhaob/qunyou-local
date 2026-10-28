"""Save and reuse original WeChat favorite stickers through verified UI controls."""
import time
from PIL import Image, ImageGrab

from message_types import message_kind
from sticker_library import choose_preview, choose_source_preview
from wechat_bot import Busy, DeliveryUnknown, SkipReply, message_id, observation_key


class StickerUnavailable(RuntimeError):
    """No sticker click has happened; a text fallback is safe."""


def walk(root):
    pending, count = [(root, 0)], 0
    while pending:
        c, depth = pending.pop()
        count += 1
        if count > 2500:
            raise Busy("表情控件树超出预期")
        if depth > 25 or c.AutomationId in ("chat_message_list", "session_list"):
            continue
        yield c
        pending.extend((k, depth + 1) for k in reversed(c.GetChildren()))


class NativeStickerUI:
    def __init__(self, transport):
        self.t = transport
        self.tick = transport.input_state()["tick"]
        self.popup = None

    def guard(self, popup=False, verify_chat=True):
        if verify_chat:
            self.t.controls()  # Exact allowed group and member count before actions.
        expected = self.popup.NativeWindowHandle if popup and self.popup else self.t.hwnd
        if self.t.port.gui.GetForegroundWindow() != expected:
            raise Busy("前台已改变，停止表情操作")
        if self.t.input_state()["tick"] != self.tick:
            raise Busy("用户开始操作，暂缓表情操作")

    def clicked(self):
        self.tick = self.t.input_state()["tick"]

    def popups(self):
        return [w for w in self.t.port.auto.GetRootControl().GetChildren()
                if w.ProcessId == self.t.port.window.ProcessId
                and w.ClassName == "mmui::EmoticonPopover"
                and self.t.port.gui.IsWindowVisible(w.NativeWindowHandle)]

    def open_picker(self):
        self.guard()
        self.t.empty_draft(self.t.controls())
        buttons = [c for c in walk(self.t.port.window)
                   if c.ControlTypeName == "ButtonControl" and c.Name == "发送表情(Alt+E)"]
        if len(buttons) != 1:
            raise StickerUnavailable("没有唯一表情按钮")
        self.t.driver._click_ctrl(buttons[0])
        self.clicked()
        for _ in range(8):
            popups = self.popups()
            if len(popups) == 1:
                self.popup = popups[0]
                break
            time.sleep(0.1)
        if self.popup is None:
            raise Busy("表情面板没有确认打开")
        self.guard(True)
        tabs = [c for c in walk(self.popup) if c.ControlTypeName == "TabItemControl" and c.Name == "自定义表情"]
        if len(tabs) != 1:
            raise StickerUnavailable("没有唯一自定义表情页")
        self.t.driver._click_ctrl(tabs[0])
        self.clicked()
        time.sleep(0.15)

    def close_picker(self):
        if self.popup is None:
            return
        current = self.popups()
        fresh = [p for p in current if p.NativeWindowHandle == self.popup.NativeWindowHandle]
        if not fresh:
            self.popup = None
            return
        self.popup = fresh[0]
        self.guard(True)
        # Only the observed, newly opened picker receives Esc, never normal chat.
        try:
            self.popup.SendKeys("{Esc}", waitTime=0.1)
        except Exception:
            # Qt may report a disconnected provider *after* closing its popup.
            # Verify disappearance instead of sending a second Esc to the chat.
            if any(p.NativeWindowHandle == self.popup.NativeWindowHandle for p in self.popups()):
                raise
        self.clicked()
        self.popup = None
        self.guard()

    def capture_items(self):
        self.guard(True)
        grids = [c for c in walk(self.popup) if c.ClassName == "mmui::EmoticonGridView"]
        if len(grids) != 1:
            raise StickerUnavailable("表情列表未确认")
        grid, items = grids[0], []
        box = grid.BoundingRectangle
        bounds = (box.left, box.top, box.right, box.bottom)
        visible = []
        for item in grid.GetChildren():
            if item.ClassName != "mmui::FavEmoticonItemView":
                continue
            pictures = [c for c in walk(item) if c.ClassName == "mmui::XImage"]
            if len(pictures) != 1:
                continue
            r = pictures[0].BoundingRectangle
            if (r.left < box.left or r.right > box.right or r.top < box.top or r.bottom > box.bottom
                    or r.width() < 24 or r.height() < 24):
                continue
            point = ((r.left + r.right) // 2, (r.top + r.bottom) // 2)
            if self.t.port.gui.GetAncestor(self.t.port.gui.WindowFromPoint(point), 2) != self.popup.NativeWindowHandle:
                raise Busy("表情面板被其他窗口遮挡")
            visible.append((item, pictures[0], (r.left, r.top, r.right, r.bottom)))
        # One screenshot keeps every GIF on the same frame and avoids a slow
        # desktop capture for each favorite (seven favorites used ~0.65s/frame).
        if visible:
            frame = ImageGrab.grab(bbox=bounds, all_screens=True).convert("RGB")
            after = grid.BoundingRectangle
            if bounds != (after.left, after.top, after.right, after.bottom):
                raise Busy("表情面板位置改变，丢弃本帧")
            for item, picture, r in visible:
                fresh = picture.BoundingRectangle
                if r != (fresh.left, fresh.top, fresh.right, fresh.bottom):
                    raise Busy("表情位置改变，丢弃本帧")
                items.append((item, frame.crop((r[0]-box.left, r[1]-box.top, r[2]-box.left, r[3]-box.top))))
        self.guard(True)
        return grid, items

    def locate(self, reference, source=False):
        previous = None
        deadline = time.monotonic() + 4
        for page in range(5):
            # Favorites animate while displayed. Wait for one positively
            # matching frame; never weaken the image-match threshold.
            for frame in range(32):
                grid, items = self.capture_items()
                images = [im for _, im in items]
                index = choose_source_preview(reference, images) if source else choose_preview(reference, images)
                self.guard(True, verify_chat=False)
                if index is not None:
                    return items[index]
                if time.monotonic() >= deadline:
                    raise StickerUnavailable("表情定位超过短暂显示时间，改用文字")
                if not items:
                    break
                time.sleep(0.1)
            signature = tuple(im.resize((8, 8)).tobytes() for im in images)
            if signature == previous or not items or page == 4:
                break
            previous = signature
            # Bounded scrolling affects only the observed favorite picker.
            grid.WheelDown(wheelTimes=3, waitTime=0.1)
            self.clicked()
            time.sleep(0.1)
        raise StickerUnavailable("收藏页没有可唯一核验的匹配表情")

    def verify_item(self, item):
        """Revalidate the matched widget, allowing its animation to advance."""
        self.guard(True)
        key = message_id(item)
        r = item.BoundingRectangle
        bounds = (r.left, r.top, r.right, r.bottom)
        matches = [c for c in walk(self.popup)
                   if c.ClassName == "mmui::FavEmoticonItemView" and message_id(c) == key]
        if len(matches) != 1:
            raise StickerUnavailable("匹配表情控件已变化")
        fresh = matches[0]
        rect = fresh.BoundingRectangle
        if bounds != (rect.left, rect.top, rect.right, rect.bottom):
            raise StickerUnavailable("匹配表情位置已变化")
        point = ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
        if self.t.port.gui.GetAncestor(self.t.port.gui.WindowFromPoint(point), 2) != self.popup.NativeWindowHandle:
            raise Busy("匹配表情被遮挡")
        return fresh

    def capture_known_frames(self, item):
        """Sample only the same verified native widget before any send click."""
        item = self.verify_item(item)
        key = message_id(item)
        images = [c for c in walk(item) if c.ClassName == "mmui::XImage"]
        if len(images) != 1:
            return []
        picture = images[0]
        r = picture.BoundingRectangle
        bounds = (r.left,r.top,r.right,r.bottom)
        frames, deadline = [], time.monotonic() + 1.5
        for _ in range(10):
            self.guard(True, verify_chat=False)
            fresh = picture.BoundingRectangle
            if message_id(item) != key or bounds != (fresh.left,fresh.top,fresh.right,fresh.bottom):
                raise Busy("采集动图参考帧时控件改变")
            point = ((r.left+r.right)//2, (r.top+r.bottom)//2)
            if self.t.port.gui.GetAncestor(self.t.port.gui.WindowFromPoint(point), 2) != self.popup.NativeWindowHandle:
                raise Busy("动图参考帧被遮挡")
            frame = ImageGrab.grab(bbox=bounds, all_screens=True).convert("RGB")
            self.guard(True, verify_chat=False)
            after = picture.BoundingRectangle
            if bounds != (after.left,after.top,after.right,after.bottom):
                raise Busy("采集动图参考帧时位置改变，丢弃本帧")
            frames.append(frame)
            if time.monotonic() >= deadline:
                break
            time.sleep(0.08)
        self.verify_item(item)
        return frames

    def collect(self, source, image_path, library):
        try:
            return self._collect(source, image_path, library)
        finally:
            # Include menu/add failures in cleanup, not only picker matching.
            self.close_picker()
            if (self.t.port.gui.GetForegroundWindow() == self.t.hwnd
                    and self.t.input_state()["tick"] == self.tick):
                self.tick = self.t.close_selection_menu(self.tick)

    def _collect(self, source, image_path, library):
        result = {"state": "unindexed"}
        self.guard()
        self.t.named_source(**source)
        self.t.exit_selection()
        self.clicked()
        row = self.t.source_row(self.t.controls(), source["source_id"], source["source_text"],
                                source["source_list_id"], "sticker")
        if self.t.classify(row) != "left":
            raise SkipReply("表情来源方向不符")
        r = row.BoundingRectangle
        if r.height() < 90:
            raise StickerUnavailable("表情消息太小，无法可靠打开其菜单")
        self.guard()
        self.t.driver._click_at(r.left + 140, r.bottom - 60, right=True)
        self.clicked()
        time.sleep(0.1)
        menu = self.t.selection_menu()
        add = [c for c in menu if c.Name == "添加到表情"]
        self.guard()
        if len(add) == 1:
            self.t.driver._click_ctrl(add[0])
            self.clicked()
            result["native_add_clicked"] = True
            time.sleep(0.2)
        elif menu:
            self.t.port.window.SendKeys("{Esc}", waitTime=0.1)
            self.clicked()
        else:
            raise StickerUnavailable("没有可确认的表情菜单")
        try:
            self.open_picker()
            with Image.open(image_path) as reference:
                item, thumbnail = self.locate(reference, source=True)
            key = library.add(thumbnail, source["source_sender"])
            library.add_frames(key, self.capture_known_frames(item))
            result.update(state="saved", sticker_id=key, storage="wechat_native_favorite")
            return result
        finally:
            self.close_picker()

    def send(self, sticker, source):
        """Click once; anything uncertain after that is not eligible for fallback."""
        try:
            self.open_picker()
            self.t.source_row(self.t.controls(), source["source_id"], source["source_text"],
                              source["source_list_id"], source["source_kind"])
            _, before = self.t.rows()
            before_keys = {observation_key(r) for r in before}
            references = []
            for path in sticker.get("preview_paths", [sticker["preview_path"]]):
                with Image.open(path) as reference:
                    references.append(reference.convert("RGB"))
            item, _ = self.locate(references)
            if len(references) == 1 and sticker.get("id"):
                self.t.sticker_library.add_frames(sticker["id"], self.capture_known_frames(item))
            # Requiring two consecutive identical frames rejects valid GIFs.
            # This is still the uniquely image-matched, unmoved native widget.
            item = self.verify_item(item)
            self.guard(True)
        except Exception:
            self.close_picker()
            raise
        # A favorite click sends immediately, without pressing Enter afterwards.
        try:
            if not self.t.driver._click_ctrl(item):
                raise DeliveryUnknown("表情点击结果未知")
            self.clicked()
            time.sleep(0.1)
            self.close_picker()  # Some client layouts keep the picker open after sending.
            for _ in range(35):
                time.sleep(0.1)
                self.t.controls()
                if self.t.port.gui.GetForegroundWindow() != self.t.hwnd:
                    continue
                controls, rows = self.t.rows()
                for row in rows:
                    if observation_key(row) in before_keys or message_kind(row.ClassName, str(row.Name)) != "sticker":
                        continue
                    if self.t.classify(row) == "right":
                        self.t.empty_draft(controls)
                        self.popup = None
                        return message_id(row), self.tick
            raise DeliveryUnknown("未确认本账号新增表情回显，不重试也不补发文字")
        except Exception as exc:
            raise DeliveryUnknown("表情发送结果未确认；停止，不重发：" + str(exc)) from exc

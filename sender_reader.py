"""WeChat selection-mode accessibility exposes member names in the background.

Names are matched against the inspected group roster, never guessed from the
first word. This does not extract wxids, decrypt data, or modify process memory.
"""
import json
import hashlib
import time
from pathlib import Path

from wechat_bot import ROOT, RUN, GROUP, Busy, SkipReply, DeliveryUnknown, Transport, message_id
from message_types import MESSAGE_CLASSES, TEXT_CLASS, message_kind
from group_profile import GroupProfile


class SenderCatalog:
    def __init__(self, data, profile=None):
        if data.get("group") != GROUP:
            raise ValueError("发送者配置不属于指定群")
        self.names = list(data["members"])
        self.count = int(data["member_count"])
        self.self_name = data["self_name"]
        if len(self.names) != self.count or self.self_name not in self.names:
            raise ValueError("群成员配置不完整")
        self.self_names = {self.self_name}
        if profile is not None:
            self.names = [name for name in self.names if name != self.self_name]
            self.self_name = profile["self_name"]
            self.self_names = set(profile["self_aliases"])
            if self.self_names.intersection(self.names):
                raise ValueError("本人昵称与其他群成员冲突")
            self.names.extend(profile["self_aliases"])

    def parse(self, raw):
        # Keep duplicates: equal or overlapping display names are ambiguous.
        matches = [name for name in self.names if raw.startswith(name + " ")]
        if len(matches) > 1 and all(name in self.self_names for name in matches):
            matches = [max(matches, key=len)]  # Old/new aliases still identify the same bot.
        if len(matches) != 1:
            raise Busy("发送者昵称未知或有歧义，暂不生成回复；需要核对成员名单")
        sender = matches[0]
        body = raw[len(sender) + 1:].strip()
        if not body:
            raise Busy("消息正文为空")
        return {"sender": sender, "text": body, "is_self": sender in self.self_names}


class NamedTransport(Transport):
    def __init__(self, settings_path=ROOT / "sender_settings.json"):
        self.settings_path = Path(settings_path)
        self.sender_settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.group_profile = GroupProfile(self.settings_path.with_name("group_profile.json"), self.sender_settings)
        self.refresh_profile()
        self.prepared = False
        self.reader_prepare_error = None
        self.reader_retry_tick = None
        self.reader_prepare_attempts = 0
        from sticker_library import StickerLibrary
        self.sticker_library = StickerLibrary(RUN / "stickers", GROUP)
        super().__init__()

    def refresh_profile(self):
        self.catalog = SenderCatalog(self.sender_settings, self.group_profile.snapshot())

    def rename_in_group(self, action, operation_id, before_write, **source):
        from native_group_tools import rename_in_group
        return rename_in_group(self, action, operation_id, before_write, **source)

    def controls(self):
        controls = super().controls()
        count = controls.get("member_count")
        if count is None or str(count.Name).strip() not in (f"({self.catalog.count})", f"（{self.catalog.count}）"):
            raise Busy("群成员数量改变或无法核验，暂缓回复并重新核对昵称名单")
        return controls

    @staticmethod
    def selection_mode(rows):
        return any(row.ClassName in MESSAGE_CLASSES and row.ControlTypeName == "CheckBoxControl"
                   for row in rows)

    def read_message(self, row):
        if row.ControlTypeName != "CheckBoxControl":
            raise Busy("消息尚未包含发送者昵称，不生成回复")
        return self.catalog.parse(str(row.Name))

    def foreground_guard(self):
        if self.port.gui.GetForegroundWindow() != self.hwnd:
            raise Busy("用户已切换前台，停止界面操作")
        self.controls()

    def selection_menu(self):
        # Qt can invalidate a menu provider as an animation/new message updates.
        # Re-read the tree once; do not reuse an expired menu object or click twice.
        from native_stickers import walk
        for attempt in range(2):
            try:
                return [c for c in walk(self.port.window) if c.ControlTypeName == "MenuItemControl"]
            except Exception:
                if attempt:
                    raise
                time.sleep(0.1)

    def close_selection_menu(self, tick):
        self.foreground_guard()
        if self.input_state()["tick"] != tick:
            raise Busy("用户开始操作，停止恢复昵称读取")
        if self.selection_menu():
            # Esc only after observing a menu, never in bare normal chat.
            self.port.window.SendKeys("{Esc}", waitTime=0.1)
            tick = self.input_state()["tick"]
            self.reader_action_tick = tick
        return tick

    def enter_selection(self):
        self.reader_action_tick = self.input_state()["tick"]
        self.foreground_guard()
        controls, rows = self.rows()
        if self.selection_mode(rows):
            return
        self.empty_draft(controls)
        originals = {message_id(r): str(r.Name) for r in rows if message_kind(r.ClassName, str(r.Name))}
        # Prefer text, but support a list containing only links/stickers too.
        # Right-click only a verified, fully visible bubble; never open its content.
        tick = self.input_state()["tick"]
        self.reader_action_tick = tick
        tick = self.close_selection_menu(tick)
        tried, selected = set(), []
        for _ in range(3):
            # Re-query after each menu action. Prefer unquoted text; a quoted
            # message's bottom area belongs to its quote, not the main bubble.
            _, fresh = self.rows()
            ordered = sorted(reversed(fresh), key=lambda r: (
                r.ClassName != TEXT_CLASS, "\n" in str(r.Name)))
            candidate = None
            for row in ordered:
                key = message_id(row)
                if key in tried or originals.get(key) != str(row.Name):
                    continue
                try:
                    side = self.classify(row)
                except Busy:
                    continue
                if side in ("left", "right"):
                    candidate = (row, side)
                    break
            if candidate is None:
                break
            row, side = candidate
            tried.add(message_id(row))
            rect = row.BoundingRectangle
            x = rect.left + 140 if side == "left" else rect.right - 140
            y = rect.bottom - 35
            if row.ClassName == TEXT_CLASS:
                y = min(y, rect.top + (80 if side == "left" else 45))
            self.foreground_guard()
            if self.input_state()["tick"] != tick:
                raise Busy("用户开始操作，停止恢复昵称读取")
            self.driver._click_at(x, y, right=True)
            tick = self.input_state()["tick"]
            self.reader_action_tick = tick
            menu = []
            for _ in range(4):
                menu = [c for c in self.selection_menu() if c.Name == "多选"]
                if menu:
                    break
                time.sleep(0.1)
            if len(menu) != 1:
                tick = self.close_selection_menu(tick)
                continue
            self.foreground_guard()
            if self.input_state()["tick"] != tick:
                raise Busy("用户开始操作，停止恢复昵称读取")
            if not self.driver._click_ctrl(menu[0]):
                raise Busy("未能点击多选读取菜单")
            tick = self.input_state()["tick"]
            self.reader_action_tick = tick
            for _ in range(5):
                _, selected = self.rows()
                if self.selection_mode(selected):
                    break
                time.sleep(0.1)
            if self.selection_mode(selected):
                break
            # Clicking an observed menu has uncertain effect: never click again.
            raise Busy("多选读取状态未确认")
        if not self.selection_mode(selected):
            raise Busy("没有可用的消息多选菜单，已完成本次有限恢复尝试")
        # Validate the name/body boundary against the same rows in normal mode.
        for item in selected:
            key = message_id(item)
            if key in originals:
                event = self.read_message(item)
                if event["text"] != originals[key].strip():
                    raise Busy("昵称与正文边界校验失败，不猜测发送者")

    def exit_selection(self):
        controls, rows = self.rows()
        if not self.selection_mode(rows):
            return
        self.foreground_guard()
        # Esc is sent exactly once and only with observed selection controls.
        # Esc in normal chat would hide the entire WeChat window to the tray.
        self.port.window.SendKeys("{Esc}", waitTime=0.1)
        for _ in range(5):
            controls, rows = self.rows()
            if not self.selection_mode(rows) and "edit" in controls:
                self.empty_draft(controls)
                return
            time.sleep(0.1)
        raise Busy("未能退出多选状态，不输入回复")

    def minimize_after_visit(self, expected_tick):
        if self.port.gui.GetForegroundWindow() != self.hwnd:
            return "foreground_changed"
        controls, rows = self.rows()
        if "edit" in controls:
            self.empty_draft(controls)
        elif not self.selection_mode(rows):
            raise Busy("窗口状态未知，不自动最小化")
        if self.input_state()["tick"] != expected_tick:
            return "user_input_changed"
        self.port.gui.ShowWindow(self.hwnd, 6)
        return "minimized" if self.port.gui.IsIconic(self.hwnd) else "minimize_not_confirmed"

    def ensure_listening(self):
        if getattr(self, "group_profile", None) and self.group_profile.snapshot()["pending"]:
            raise Busy("上一次改名结果待核对，已暂停自动回复和改名")
        controls, rows = self.rows()
        if self.selection_mode(rows):
            self.prepared = True
            self.reader_prepare_error = None
            return
        idle = self.input_state()
        # A failed preparation must not turn the polling loop into a window
        # restore/minimize loop. Retry only after the owner brings WeChat to
        # the foreground and supplies new input, or restores selection mode.
        waiting = self.reader_prepare_error or ("带昵称的读取模式已退出" if self.prepared else None)
        if waiting and (self.port.gui.GetForegroundWindow() != self.hwnd
                        or self.port.gui.IsIconic(self.hwnd)
                        or idle["tick"] == self.reader_retry_tick):
            raise Busy(waiting + "；已暂停自动拉起窗口，打开目标群后再尝试恢复")
        if idle["idle_seconds"] < 1.5:
            raise Busy("等待键鼠空闲后准备昵称读取")
        self.empty_draft(controls)
        started = time.monotonic()
        tick = idle["tick"]
        visited = False
        self.reader_prepare_attempts += 1
        try:
            self.activate()
            visited = True
            self.enter_selection()
            tick = self.input_state()["tick"]
            self.prepared = True
            self.reader_prepare_error = None
        except Exception as exc:
            self.reader_prepare_error = str(exc)
            raise
        finally:
            result = "not_visited"
            if visited:
                tick = getattr(self, "reader_action_tick", tick)
                try:
                    result = self.minimize_after_visit(tick)
                except Exception as exc:
                    result = "not_minimized: " + str(exc)
                    self.reader_prepare_error = self.reader_prepare_error or str(exc)
            self.reader_retry_tick = self.input_state()["tick"]
            self.last_visit = {"outcome": "prepare_reader", "seconds": round(time.monotonic() - started, 2),
                               "window_result": result, "attempt": self.reader_prepare_attempts,
                               "error": self.reader_prepare_error}

    def named_source(self, source_id, source_text, source_list_id, source_sender, source_kind="text"):
        controls, rows = self.rows()
        if not all((source_id, source_list_id, source_sender)):
            raise SkipReply("缺少可核验的原消息或发送者")
        if message_id(controls["messages"]) != source_list_id:
            raise SkipReply("消息列表已重建，丢弃旧回复")
        row = next((r for r in rows if message_id(r) == source_id), None)
        if row is None or row.ClassName not in MESSAGE_CLASSES:
            raise SkipReply("原消息已离开列表，不发送旧回复")
        event = self.read_message(row)
        if (event["is_self"] or event["sender"] != source_sender or event["text"] != source_text
                or message_kind(row.ClassName, event["text"]) != source_kind):
            raise SkipReply("原消息发送者或正文不匹配，不发送")
        return row

    def capture_sticker(self, source_id, source_text, source_list_id, source_sender):
        """Capture only this verified row, then hide before model generation.

        Minimized WeChat exposes names but renders no message pixels. The owner
        opted into this extra brief visit for stickers. No clicks or keypresses
        are needed for capture; a clipped/blank row gets an explicit text fallback.
        """
        from PIL import ImageGrab, ImageStat
        self.named_source(source_id, source_text, source_list_id, source_sender, "sticker")
        idle = self.input_state()
        if idle["idle_seconds"] < 1.5:
            raise Busy("等待键鼠空闲后截取表情")
        tick, started = idle["tick"], time.monotonic()
        media = {"visual_status": "unavailable", "images": []}
        result = "not_visited"
        restore_selection = False
        try:
            self.activate()
            if self.input_state()["tick"] != tick:
                raise Busy("用户开始操作，暂缓截取表情")
            row = self.named_source(source_id, source_text, source_list_id, source_sender, "sticker")
            r, box = row.BoundingRectangle, self.controls()["messages"].BoundingRectangle
            bounds = (r.left, r.top, r.right, r.bottom)
            if (r.left < box.left or r.right > box.right or r.top < box.top or r.bottom > box.bottom
                    or r.width() < 60 or r.height() < 40):
                media["reason"] = "source_not_fully_visible"
                return media
            points = [(r.left + 5, r.top + 5), (r.right - 5, r.top + 5),
                      (r.left + 5, r.bottom - 5), (r.right - 5, r.bottom - 5),
                      ((r.left + r.right) // 2, (r.top + r.bottom) // 2)]
            self.foreground_guard()
            if any(self.port.gui.GetAncestor(self.port.gui.WindowFromPoint(p), 2) != self.hwnd for p in points):
                raise Busy("表情被其他窗口遮挡，暂缓截图")
            picture = ImageGrab.grab(bbox=bounds, all_screens=True).convert("RGB")
            self.foreground_guard()
            verified = self.named_source(source_id, source_text, source_list_id, source_sender, "sticker")
            after = verified.BoundingRectangle
            if bounds != (after.left, after.top, after.right, after.bottom) or self.input_state()["tick"] != tick:
                raise Busy("截图期间界面或用户输入变化，丢弃截图后重试")
            if max(ImageStat.Stat(picture).stddev) < 2:
                media["reason"] = "blank_capture"
                return media
            folder = RUN / "media"
            folder.mkdir(exist_ok=True)
            digest = hashlib.sha256((source_list_id + source_id + str(time.time_ns())).encode()).hexdigest()[:32]
            path = folder / (digest + ".png")
            picture.save(path)
            media.update(visual_status="captured_frame", images=[str(path)],
                         width=picture.width, height=picture.height)
            library = getattr(self, "sticker_library", None)
            from reply_bot import load_settings
            if library is not None and load_settings(ROOT / "bot_settings.json").get("auto_collect_stickers", True):
                known = library.recognize_source(picture)
                if known:
                    media["collection"] = {"state": "saved", "sticker_id": known, "already_indexed": True,
                                           "storage": "wechat_native_favorite"}
                else:
                    from native_stickers import NativeStickerUI, StickerUnavailable
                    ui = NativeStickerUI(self)
                    restore_selection = True
                    try:
                        media["collection"] = ui.collect(
                            {"source_id": source_id, "source_text": source_text, "source_list_id": source_list_id,
                             "source_sender": source_sender, "source_kind": "sticker"}, path, library)
                    except (Busy, SkipReply):
                        raise
                    except Exception as exc:
                        media["collection"] = {"state": "unindexed", "reason": str(exc)}
                    finally:
                        tick = ui.tick
            return media
        except (Busy, SkipReply):
            raise
        except Exception as exc:
            # Capture errors cannot silently masquerade as visual understanding.
            media["reason"] = "capture_failed: " + type(exc).__name__
            return media
        finally:
            restore_error = None
            try:
                if (restore_selection and self.port.gui.GetForegroundWindow() == self.hwnd
                        and self.input_state()["tick"] == tick):
                    self.enter_selection()
                    tick = self.input_state()["tick"]
            except Exception as exc:
                restore_error = str(exc)
                self.reader_prepare_error = restore_error
                tick = getattr(self, "reader_action_tick", tick)
                self.reader_retry_tick = tick
            try:
                result = self.minimize_after_visit(tick)
            except Exception as exc:
                result = "not_minimized: " + str(exc)
            if restore_error:
                media["reader_restore_error"] = restore_error
            media["capture_seconds"] = round(time.monotonic() - started, 2)
            self.last_visit = {"outcome": "capture_sticker", "seconds": media["capture_seconds"],
                               "window_result": result, "visual_status": media["visual_status"],
                               "collection": media.get("collection"), "reader_restore_error": restore_error}

    def send(self, text, *, source_id=None, source_text=None, source_list_id=None, source_sender=None,
             source_kind="text", sticker=None):
        part = {"kind": "sticker" if sticker is not None else "text", "text": text,
                "sticker": sticker, "sticker_id": sticker.get("id", "") if sticker else "",
                "description": sticker.get("description", "") if sticker else ""}
        sent = self.send_sequence([part], source_id=source_id, source_text=source_text,
                                 source_list_id=source_list_id, source_sender=source_sender, source_kind=source_kind)
        return sent[-1]["echo_id"]

    def send_sequence(self, messages, *, start_index=0, resolve_part=None, before_part=None,
                      after_part=None, should_stop=None, interval=0.4, source_id=None,
                      source_text=None, source_list_id=None, source_sender=None, source_kind="text"):
        resolve_part = resolve_part or (lambda part: dict(part))
        before_part = before_part or (lambda index: None)
        after_part = after_part or (lambda index, actual, echo_id: None)
        should_stop = should_stop or (lambda: False)
        self.last_sent_kind = "text"
        self.last_sticker_fallback = None
        if should_stop():
            raise Busy("已停止，保留未发出的分条消息")
        self.named_source(source_id, source_text, source_list_id, source_sender, source_kind)
        idle = self.input_state()
        if idle["idle_seconds"] < 1.5:
            raise Busy("回复已备好，等待键鼠空闲")
        started = time.monotonic()
        tick = idle["tick"]
        outcome, minimize, warning = "waiting", True, None
        sent, fallbacks = [], []
        try:
            self.activate()
            self.named_source(source_id, source_text, source_list_id, source_sender, source_kind)
            if self.input_state()["tick"] != tick:
                raise Busy("用户开始操作，暂缓发送")
            self.exit_selection()
            tick = self.input_state()["tick"]
            # Also compare normal-mode body before touching the input field.
            self.source_row(self.controls(), source_id, source_text, source_list_id, source_kind)
            delivery_list_id = message_id(self.controls()["messages"])
            anchor = {"source_id": source_id, "source_text": source_text,
                      "source_list_id": source_list_id, "source_kind": source_kind}
            for index in range(start_index, len(messages)):
                if index > start_index and interval:
                    time.sleep(interval)
                if should_stop():
                    raise Busy("已停止，保留剩余分条消息")
                self.foreground_guard()
                if self.input_state()["tick"] != tick:
                    raise Busy("用户开始操作，保留剩余分条消息")
                self.empty_draft(self.controls())
                part = resolve_part(messages[index])
                before_part(index)
                echo_id = None
                actual = {"kind": "text", "text": part["text"]}
                sticker = part.get("sticker") if part["kind"] == "sticker" else None
                if sticker is not None:
                    from native_stickers import NativeStickerUI, StickerUnavailable
                    ui = NativeStickerUI(self)
                    try:
                        echo_id, tick = ui.send(sticker, anchor)
                        actual = {"kind": "sticker", "sticker_id": part["sticker_id"],
                                  "description": part.get("description", "")}
                    except StickerUnavailable as exc:
                        self.last_sticker_fallback = str(exc)
                        fallbacks.append({"index": index, "reason": str(exc)})
                        actual["requested_sticker_id"] = part["sticker_id"]
                        actual["sticker_fallback_reason"] = str(exc)
                    finally:
                        tick = ui.tick
                if echo_id is None:
                    echo_id, tick = self.send_foreground(part["text"], tick)
                self.last_sent_kind = actual["kind"]
                # Commit each observed echo before any next message or cleanup.
                after_part(index, actual, echo_id)
                sent.append({"index": index, "actual": actual, "echo_id": echo_id})
                if index + 1 < len(messages):
                    controls, rows = self.rows()
                    echo = next((row for row in rows if message_id(row) == echo_id), None)
                    if echo is None or message_id(controls["messages"]) != delivery_list_id:
                        raise Busy("已发送消息离开列表，暂停剩余分条消息")
                    echo_kind = message_kind(echo.ClassName, str(echo.Name))
                    if echo_kind != actual["kind"]:
                        raise Busy("已发送消息类型变化，暂停剩余分条消息")
                    # Earlier source rows can scroll away as the bot replies.
                    # Within this uninterrupted visit, use our verified echo.
                    anchor = {"source_id": echo_id, "source_text": str(echo.Name).strip(),
                              "source_list_id": delivery_list_id, "source_kind": echo_kind}
            outcome = "sent"
        except SkipReply:
            outcome = "skipped"
            raise
        except Busy:
            raise
        except Exception:
            outcome, minimize = "unknown", False
            raise
        finally:
            # Confirmed delivery remains confirmed even if restoring the
            # reader fails. Never resend because of a window-cleanup failure.
            if minimize:
                try:
                    if (self.port.gui.GetForegroundWindow() == self.hwnd
                            and self.input_state()["tick"] == tick):
                        self.enter_selection()
                        tick = self.input_state()["tick"]
                    result = self.minimize_after_visit(tick)
                except Exception as exc:
                    warning = str(exc)
                    self.reader_prepare_error = warning
                    tick = getattr(self, "reader_action_tick", tick)
                    self.reader_retry_tick = tick
                    result = "reader_restore_failed"
                    try:
                        result += ":" + self.minimize_after_visit(tick)
                    except Exception:
                        pass
            else:
                result = "left_visible_for_inspection"
            self.last_visit = {"outcome": outcome, "seconds": round(time.monotonic() - started, 2),
                               "window_result": result, "warning": warning,
                               "sticker_fallback": self.last_sticker_fallback,
                               "planned_parts": len(messages), "confirmed_parts_this_visit": len(sent),
                               "start_index": start_index, "part_fallbacks": fallbacks}
        return sent

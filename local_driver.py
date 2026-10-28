"""Minimal local UI operations, using the already verified WeChat window."""
class LocalDriver:
    def __init__(self, gui, auto):
        self.gui, self.auto = gui, auto

    def _force_foreground(self, hwnd):
        try:
            if self.gui.IsIconic(hwnd):
                self.gui.ShowWindow(hwnd, 9)
            self.gui.SetForegroundWindow(hwnd)
            return self.gui.GetForegroundWindow() == hwnd
        except Exception:
            return False

    def _click_at(self, x, y, right=False):
        if self.gui.GetForegroundWindow() != self._win.NativeWindowHandle:
            raise RuntimeError('微信不在前台，暂停操作')
        top = self.gui.GetAncestor(self.gui.WindowFromPoint((int(x), int(y))), 2)
        # Qt menus can have a separate HWND owned by the verified main window.
        for _ in range(8):
            if not top or top == self._win.NativeWindowHandle:
                break
            top = self.gui.GetWindow(top, 4)  # GW_OWNER
        if top != self._win.NativeWindowHandle:
            raise RuntimeError('目标控件被遮挡，暂停操作')
        (self.auto.RightClick if right else self.auto.Click)(int(x), int(y))
        return True

    def _click_ctrl(self, control):
        r = control.BoundingRectangle
        if r.width() <= 0 or r.height() <= 0:
            return False
        return self._click_at((r.left + r.right) // 2, (r.top + r.bottom) // 2)

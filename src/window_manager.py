"""
Cross-Platform Overlay Window Manager for VoiceHUD
รองรับทั้ง Windows 11 และ Linux Cinnamon / KDE / X11
Optimize: Persistent Display connection & state caching เพื่อความเร็วสูงสุด ไม่หน่วง
"""

import sys
import os
import platform
import ctypes
from typing import Optional


class CrossPlatformWindowManager:
    """
    จัดการสถานะการคลิกทะลุหน้าจอ (Click-through / Ignore Mouse Events)
    - Windows 11: Win32 SetWindowLongPtr with WS_EX_TRANSPARENT | WS_EX_LAYERED
    - Linux (X11 / Cinnamon): XShapeCombineRectangles (ShapeInput)
    """

    def __init__(self, window_id: Optional[int] = None):
        self.os_type = platform.system()
        self.window_id = window_id
        self._current_state = None  # Cache state เพื่อไม่ให้ยิง X11 ซ้ำถ้าค่าเดิม
        self.x11_display = None
        self._setup_platform()

    def _setup_platform(self):
        if self.os_type == "Linux":
            try:
                self.x11 = ctypes.cdll.LoadLibrary("libX11.so.6")
                self.xext = ctypes.cdll.LoadLibrary("libXext.so.6")
                self.x11.XOpenDisplay.restype = ctypes.c_void_p
                # เปิด display connection ค้างไว้เพื่อความเร็ว (ไม่ต้อง open/close ทุกรอบ)
                self.x11_display = self.x11.XOpenDisplay(None)
            except Exception as e:
                print(f"[Warning] Linux X11 libraries could not be loaded: {e}")
        elif self.os_type == "Windows":
            try:
                self.user32 = ctypes.windll.user32
            except Exception as e:
                print(f"[Warning] Windows user32 could not be loaded: {e}")

    def set_click_through(self, hwnd_or_xid: int, enable: bool) -> bool:
        target_id = hwnd_or_xid or self.window_id
        if not target_id:
            return False

        # ถ้าสถานะเดิมอยู่แล้ว ไม่ต้องยิงคำสั่งซ้ำให้เปลืองรอบ
        if self._current_state == enable:
            return True

        success = False
        if self.os_type == "Windows":
            success = self._set_windows_click_through(target_id, enable)
        elif self.os_type == "Linux":
            success = self._set_linux_click_through(target_id, enable)

        if success:
            self._current_state = enable
        return success

    def _set_windows_click_through(self, hwnd: int, enable: bool) -> bool:
        GWL_EXSTYLE = -20
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_LAYERED = 0x00080000

        try:
            GetWindowLong = getattr(self.user32, "GetWindowLongPtrW", self.user32.GetWindowLongW)
            SetWindowLong = getattr(self.user32, "SetWindowLongPtrW", self.user32.SetWindowLongW)

            style = GetWindowLong(hwnd, GWL_EXSTYLE)
            if enable:
                new_style = style | WS_EX_TRANSPARENT | WS_EX_LAYERED
            else:
                new_style = (style & ~WS_EX_TRANSPARENT) | WS_EX_LAYERED

            SetWindowLong(hwnd, GWL_EXSTYLE, new_style)
            return True
        except Exception as e:
            print(f"[Error] Failed to set Windows click-through: {e}")
            return False

    def _set_linux_click_through(self, xid: int, enable: bool) -> bool:
        if not self.x11_display:
            try:
                self.x11_display = self.x11.XOpenDisplay(None)
            except Exception:
                return False

        ShapeInput = 2
        ShapeSet = 0

        try:
            if enable:
                # เจาะทะลุทั้งหน้าต่างด้วย Empty Rectangle (0 rects)
                self.xext.XShapeCombineRectangles(
                    ctypes.c_void_p(self.x11_display),
                    ctypes.c_ulong(xid),
                    ctypes.c_int(ShapeInput),
                    ctypes.c_int(0),
                    ctypes.c_int(0),
                    None,
                    ctypes.c_int(0),
                    ctypes.c_int(ShapeSet),
                    ctypes.c_int(0)
                )
            else:
                # รีเซ็ต ShapeInput ให้รับคลิกได้ปกติ
                class XRectangle(ctypes.Structure):
                    _fields_ = [
                        ("x", ctypes.c_short),
                        ("y", ctypes.c_short),
                        ("width", ctypes.c_ushort),
                        ("height", ctypes.c_ushort)
                    ]

                rect = XRectangle(0, 0, 32767, 32767)
                self.xext.XShapeCombineRectangles(
                    ctypes.c_void_p(self.x11_display),
                    ctypes.c_ulong(xid),
                    ctypes.c_int(ShapeInput),
                    ctypes.c_int(0),
                    ctypes.c_int(0),
                    ctypes.byref(rect),
                    ctypes.c_int(1),
                    ctypes.c_int(ShapeSet),
                    ctypes.c_int(0)
                )

            self.x11.XFlush(ctypes.c_void_p(self.x11_display))
            return True
        except Exception as e:
            print(f"[Error] Failed to set Linux X11 click-through: {e}")
            return False

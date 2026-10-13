"""
Global Keyboard Listener using evdev (Linux)
- Robust file descriptor polling (re-open / non-blocking)
- Debounce ป้องกัน Signal ค้างหรือยิงซ้ำรัวจนคิว UI ค้าง
"""

import sys
import time
import threading
import platform
from typing import Callable, Optional


class GlobalHotkeyListener:
    def __init__(
        self,
        on_toggle_interactive: Callable[[], None],
        on_toggle_minimap: Callable[[], None],
        on_zoom_in: Callable[[], None],
        on_zoom_out: Callable[[], None],
        on_m_hold_open: Optional[Callable[[], None]] = None,
        on_m_quick_close: Optional[Callable[[], None]] = None,
        is_interactive_check: Optional[Callable[[], bool]] = None,
        on_exit: Optional[Callable[[], None]] = None
    ):
        self.on_toggle_interactive = on_toggle_interactive
        self.on_toggle_minimap = on_toggle_minimap
        self.on_zoom_in = on_zoom_in
        self.on_zoom_out = on_zoom_out
        self.on_m_hold_open = on_m_hold_open
        self.on_m_quick_close = on_m_quick_close
        self.is_interactive_check = is_interactive_check
        self.on_exit = on_exit

        self.is_running = False
        self._thread = None
        self._last_trigger = {}

        # M Key Hold Timer
        self._m_hold_timer: Optional[threading.Timer] = None
        self._m_pressed_time: float = 0.0
        self._m_hold_triggered: bool = False
        self._m_is_pressed: bool = False

    def _debounce(self, key_name: str, delay_sec: float = 0.40) -> bool:
        now = time.time()
        if now - self._last_trigger.get(key_name, 0.0) < delay_sec:
            return False
        self._last_trigger[key_name] = now
        return True

    def start(self):
        sys_type = platform.system()
        self.is_running = True
        if sys_type == "Linux":
            self._thread = threading.Thread(target=self._linux_evdev_worker, daemon=True)
            self._thread.start()
        elif sys_type == "Windows":
            self._thread = threading.Thread(target=self._windows_hook_worker, daemon=True)
            self._thread.start()

    def stop(self):
        self.is_running = False

    def _windows_hook_worker(self):
        """
        Windows Low-Level Keyboard Hook (WH_KEYBOARD_LL) + GetAsyncKeyState Hybrid
        ทำงานได้เสถียรทั้งใน Windowed และ Exclusive Fullscreen Games
        """
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        WH_KEYBOARD_LL = 13
        WM_KEYDOWN = 0x0100
        WM_KEYUP = 0x0101
        WM_SYSKEYDOWN = 0x0104
        WM_SYSKEYUP = 0x0105

        VK_ESCAPE = 0x1B
        VK_M = 0x4D
        VK_8 = 0x38  # Top-row number key 8
        VK_NUMPAD8 = 0x68  # Numpad 8
        VK_F7 = 0x76
        VK_OEM_PLUS = 0xBB
        VK_ADD = 0x6B
        VK_OEM_MINUS = 0xBD
        VK_SUBTRACT = 0x6D

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [
                ("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p),
            ]

        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = ctypes.c_longlong
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL

        def handle_key_action(vk: int, is_down: bool, is_up: bool):
            if vk == VK_ESCAPE:
                is_open = self.is_interactive_check() if self.is_interactive_check else False
                if is_open and is_down and self._debounce("ESC_CLOSE", delay_sec=0.25):
                    if self.on_m_quick_close:
                        self.on_m_quick_close()
                    else:
                        self.on_toggle_interactive()

            elif vk in (VK_8, VK_NUMPAD8) and is_down:
                if self._debounce("EXIT_KEY", delay_sec=0.3):
                    if self.on_exit:
                        self.on_exit()

            elif vk == VK_M:
                if is_down:
                    is_open = self.is_interactive_check() if self.is_interactive_check else False
                    if is_open:
                        # ถ้าเปิดอยู่แล้วและเป็นการกดใหม่ (ไม่ใช่กดค้างต่อเนื่องมาจากตอนเปิด)
                        if not self._m_hold_triggered and not self._m_is_pressed:
                            if self._debounce("M_CLOSE", delay_sec=0.35):
                                print("[Hotkey Debug] M key pressed while map OPEN -> Closing map")
                                if self.on_m_quick_close:
                                    self.on_m_quick_close()
                                else:
                                    self.on_toggle_interactive()
                        else:
                            # Ignored repeat while hold is active or just opened
                            pass
                    else:
                        # แผนที่ปิดอยู่: เริ่มจับเวลากดค้าง 1 วินาที (ครั้งแรกที่กดลง)
                        if not self._m_is_pressed:
                            self._m_is_pressed = True
                            self._m_hold_triggered = False
                            if self._m_hold_timer:
                                self._m_hold_timer.cancel()

                            print("[Hotkey Debug] M key pressed DOWN -> Hold timer started (1.0s)")

                            def trigger_open():
                                self._m_hold_triggered = True
                                # ตั้ง cooldown ให้ M_CLOSE ป้องกันการปิดซ้ำ
                                self._last_trigger["M_CLOSE"] = time.time()
                                print("[Hotkey Debug] >>> M HOLD 1.0s reached! -> Opening full map")
                                if self.on_m_hold_open:
                                    self.on_m_hold_open()
                                else:
                                    self.on_toggle_interactive()

                            self._m_hold_timer = threading.Timer(1.0, trigger_open)
                            self._m_hold_timer.daemon = True
                            self._m_hold_timer.start()

                elif is_up:
                    # ปล่อยปุ่ม M
                    print(f"[Hotkey Debug] M key released UP (was_hold_triggered={self._m_hold_triggered})")
                    self._m_is_pressed = False
                    if self._m_hold_timer:
                        self._m_hold_timer.cancel()
                        self._m_hold_timer = None
                    # หน่วงเวลาเล็กน้อยก่อนยอมให้กดปิดได้
                    if self._m_hold_triggered:
                        self._last_trigger["M_CLOSE"] = time.time()
                        self._m_hold_triggered = False

            elif is_down:
                if vk == VK_F7 and self._debounce("F7"):
                    self.on_toggle_minimap()
                elif vk in (VK_OEM_PLUS, VK_ADD) and self._debounce("PLUS", delay_sec=0.1):
                    self.on_zoom_in()
                elif vk in (VK_OEM_MINUS, VK_SUBTRACT) and self._debounce("MINUS", delay_sec=0.1):
                    self.on_zoom_out()

        def low_level_keyboard_proc(nCode, wParam, lParam):
            if nCode >= 0:
                kb_struct = KBDLLHOOKSTRUCT.from_address(lParam)
                vk = kb_struct.vkCode
                is_down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                is_up = wParam in (WM_KEYUP, WM_SYSKEYUP)
                handle_key_action(vk, is_down, is_up)

            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        # เก็บ hook_func ไว้ใน instance เพื่อป้องกัน Garbage Collector ทำลาย callback
        self._win_hook_func = HOOKPROC(low_level_keyboard_proc)

        # สำหรับ WH_KEYBOARD_LL พารามิเตอร์ HINSTANCE ต้องเป็น NULL (None)
        hook_id = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._win_hook_func, None, 0)
        if not hook_id:
            # Fallback หากระบุ NULL ไม่ผ่าน ลอง GetModuleHandleW
            h_mod = kernel32.GetModuleHandleW(None)
            hook_id = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._win_hook_func, h_mod, 0)

        use_hook = bool(hook_id)
        if use_hook:
            print(f"[Hotkey] Windows WH_KEYBOARD_LL Hook installed successfully (ID: {hook_id}).")
        else:
            print("[Hotkey] Fallback to GetAsyncKeyState polling listener.")

        # Polling fallback thread (ช่วยเสริมกรณีเกมบล็อค Low-Level Hook หรือ Hook หลุด)
        prev_states = {
            VK_M: False,
            VK_ESCAPE: False,
            VK_8: False,
            VK_NUMPAD8: False,
            VK_F7: False,
            VK_OEM_PLUS: False,
            VK_ADD: False,
            VK_OEM_MINUS: False,
            VK_SUBTRACT: False
        }

        def polling_worker():
            user32_poll = ctypes.windll.user32
            while self.is_running:
                for vk_code in list(prev_states.keys()):
                    state = user32_poll.GetAsyncKeyState(vk_code)
                    curr_down = bool(state & 0x8000)
                    was_down = prev_states[vk_code]

                    if curr_down and not was_down:
                        handle_key_action(vk_code, is_down=True, is_up=False)
                    elif not curr_down and was_down:
                        handle_key_action(vk_code, is_down=False, is_up=True)

                    prev_states[vk_code] = curr_down
                time.sleep(0.015)

        # รัน Polling worker เสริมเพื่อความชัวร์ 100%
        poll_thread = threading.Thread(target=polling_worker, daemon=True)
        poll_thread.start()

        if use_hook:
            msg = wintypes.MSG()
            while self.is_running:
                b_ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if b_ret <= 0:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

            user32.UnhookWindowsHookEx(hook_id)

    def _linux_evdev_worker(self):
        try:
            import evdev
            from evdev import ecodes
        except ImportError:
            print("[Hotkey] evdev not installed")
            return

        import select

        while self.is_running:
            try:
                device_paths = evdev.list_devices()
                devices = []
                for p in device_paths:
                    try:
                        d = evdev.InputDevice(p)
                        # กรองเฉพาะ Main Keyboard จริง (มี KEY_A, KEY_M, KEY_ESC) และไม่เอา Consumer/System Control
                        caps = d.capabilities().get(ecodes.EV_KEY, [])
                        if ecodes.KEY_M in caps and ecodes.KEY_A in caps:
                            devices.append(d)
                    except Exception:
                        pass

                if not devices:
                    # Fallback ทุก device ที่ส่ง EV_KEY ได้
                    for p in device_paths:
                        try:
                            d = evdev.InputDevice(p)
                            if ecodes.EV_KEY in d.capabilities():
                                devices.append(d)
                        except Exception:
                            pass

                devices_map = {dev.fd: dev for dev in devices}
                if not devices_map:
                    time.sleep(1.0)
                    continue

                while self.is_running:
                    r, _, _ = select.select(devices_map.keys(), [], [], 1.0)
                    if not r:
                        continue

                    for fd in r:
                        dev = devices_map.get(fd)
                        if not dev:
                            continue
                        try:
                            # อ่าน events ทั้งหมดใน buffer
                            for event in dev.read():
                                # event.value: 0=Release, 1=Press, 2=Autorepeat
                                if event.type == ecodes.EV_KEY:
                                    if event.code == ecodes.KEY_M:
                                        if event.value == 1:  # Key Down
                                            is_open = self.is_interactive_check() if self.is_interactive_check else False
                                            if is_open:
                                                if not self._m_hold_triggered and not self._m_is_pressed:
                                                    if self._debounce("M_CLOSE", delay_sec=0.35):
                                                        if self.on_m_quick_close:
                                                            self.on_m_quick_close()
                                                        else:
                                                            self.on_toggle_interactive()
                                            else:
                                                if not self._m_is_pressed:
                                                    self._m_is_pressed = True
                                                    self._m_hold_triggered = False
                                                    if self._m_hold_timer:
                                                        self._m_hold_timer.cancel()

                                                    def trigger_open():
                                                        self._m_hold_triggered = True
                                                        self._last_trigger["M_CLOSE"] = time.time()
                                                        if self.on_m_hold_open:
                                                            self.on_m_hold_open()
                                                        else:
                                                            self.on_toggle_interactive()

                                                    self._m_hold_timer = threading.Timer(1.0, trigger_open)
                                                    self._m_hold_timer.daemon = True
                                                    self._m_hold_timer.start()

                                        elif event.value == 0:  # Key Up (Release)
                                            self._m_is_pressed = False
                                            if self._m_hold_timer:
                                                self._m_hold_timer.cancel()
                                                self._m_hold_timer = None
                                            if self._m_hold_triggered:
                                                self._last_trigger["M_CLOSE"] = time.time()
                                                self._m_hold_triggered = False

                                    elif event.value == 1:
                                        if event.code in (ecodes.KEY_8, ecodes.KEY_KP8):
                                            if self._debounce("EXIT_KEY", delay_sec=0.3):
                                                if self.on_exit:
                                                    self.on_exit()
                                        elif event.code == ecodes.KEY_F7:
                                            if self._debounce("F7"):
                                                self.on_toggle_minimap()
                                        elif event.code in (ecodes.KEY_EQUAL, ecodes.KEY_KPPLUS):
                                            if self._debounce("PLUS", delay_sec=0.1):
                                                self.on_zoom_in()
                                        elif event.code in (ecodes.KEY_MINUS, ecodes.KEY_KPMINUS):
                                            if self._debounce("MINUS", delay_sec=0.1):
                                                self.on_zoom_out()
                        except (OSError, IOError):
                            # Device อาจถูก disconnect หรือ fd invalid -> re-scan
                            raise

            except Exception:
                time.sleep(0.5)

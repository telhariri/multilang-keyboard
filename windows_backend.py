"""Native Windows keyboard hook and Unicode insertion backend."""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
import time
from ctypes import wintypes

logger = logging.getLogger("multilang.windows")

VK_BACK = 0x08
VK_DELETE = 0x2E
VK_RETURN = 0x0D
VK_TAB = 0x09
VK_CAPITAL = 0x14
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_NUMLOCK = 0x90
VK_SCROLL = 0x91
VK_CONTROL_KEYS = {0x11, 0xA2, 0xA3}
VK_ALT_KEYS = {0x12, 0xA4, 0xA5}
VK_WINDOWS_KEYS = {0x5B, 0x5C}
VK_SHIFT_KEYS = {0x10, 0xA0, 0xA1}
VK_LOCK_KEYS = {VK_CAPITAL, VK_NUMLOCK, VK_SCROLL}
VK_NAVIGATION_KEYS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D}

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_LBUTTONDOWN = 0x0201
WM_RBUTTONDOWN = 0x0204
WM_MBUTTONDOWN = 0x0207
WM_QUIT = 0x0012
WM_APP_INJECT = 0x8001
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
HC_ACTION = 0
LLKHF_INJECTED = 0x10
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
INPUT_KEYBOARD = 1
TU_NO_STATE_CHANGE = 0x0004
PRIMARYLANGID_ARABIC = 0x01
ARABIC_LAYOUT_SYMBOLS = set("`[];',./")
IDLE_RESET_SECONDS = 8.0


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_uint16),
        ("wScan", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_int32),
        ("dy", ctypes.c_int32),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_uint32),
        ("wParamL", ctypes.c_uint16),
        ("wParamH", ctypes.c_uint16),
    ]


class INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint32), ("u", INPUTUNION)]


def build_keyboard_sequence(backspaces: int, text: str) -> list[tuple[int, int, int]]:
    """Return (virtual_key, scan_code, flags) records for SendInput."""
    records: list[tuple[int, int, int]] = []
    for _ in range(backspaces):
        records.append((VK_BACK, 0, 0))
        records.append((VK_BACK, 0, KEYEVENTF_KEYUP))

    for char in text:
        codepoint = ord(char)
        if codepoint <= 0xFFFF:
            codepoints = (codepoint,)
        else:
            adjusted = codepoint - 0x10000
            codepoints = (0xD800 + (adjusted >> 10), 0xDC00 + (adjusted & 0x3FF))
        for unit in codepoints:
            records.append((0, unit, KEYEVENTF_UNICODE))
            records.append((0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    return records


class WindowsKeyboardHook:
    """Observe real key events; injected correction events are ignored."""

    def __init__(self, monitor):
        self.monitor = monitor
        self.thread = None
        self.thread_id = 0
        self.hook = None
        self.mouse_hook = None
        self.callback_ref = None
        self.mouse_callback_ref = None
        self.ready = threading.Event()
        self.error: Exception | None = None
        self.modifiers_down: set[int] = set()
        self.shift_keys_down: set[int] = set()
        self.shift_down_at: float = 0.0
        self.modifier_down_at: float = 0.0
        self.last_hwnd: int = 0
        self.last_key_time: float = 0.0
        self.suppressed_keyups: set[int] = set()
        self.pending_replacement: tuple[int, str] | None = None
        self.pending_lock = threading.Lock()
        self.switch_queue: queue.Queue[tuple[str, int, bool] | None] | None = None
        self.switch_thread: threading.Thread | None = None
        self.user32 = None
        self.kernel32 = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.ready.clear()
        self.error = None
        self.switch_queue = queue.Queue()
        self.switch_thread = threading.Thread(
            target=self._layout_worker,
            name="MultilangPyLayoutSwitcher",
            daemon=True,
        )
        self.switch_thread.start()
        self.thread = threading.Thread(target=self._run, name="MultilangKeyboardHook", daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            self.stop()
            raise RuntimeError("Windows keyboard hook did not initialize")
        if self.error:
            self.stop()
            raise self.error

    def stop(self):
        if self.thread_id and self.user32:
            self.user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)
        if self.switch_queue is not None:
            self.switch_queue.put(None)
        if self.switch_thread and self.switch_thread.is_alive():
            self.switch_thread.join(timeout=2)
        self.thread = None
        self.thread_id = 0
        self.switch_thread = None
        self.switch_queue = None

    def enqueue_layout_switch(self, target: str, hwnd: int, switch_requested: bool = True):
        """Run layout switching off the low-level hook thread so typing never stalls."""
        if self.switch_queue is not None:
            self.switch_queue.put((target, hwnd, switch_requested))

    def _layout_worker(self):
        assert self.switch_queue is not None
        while True:
            item = self.switch_queue.get()
            if item is None:
                return
            target, hwnd, switch_requested = item
            if not switch_requested:
                self.monitor.on_status("تم تصحيح الكلمة")
                continue
            try:
                switched = self.monitor.switcher.switch(target, hwnd=hwnd)
            except Exception as exc:
                logger.debug("Background Windows layout switch failed: %s", exc)
                switched = False
            if switched:
                self.monitor.on_status("تم التصحيح — " + ("English" if target == "en" else "العربية"))
            else:
                target_name = "English" if target == "en" else "العربية"
                self.monitor.on_status(
                    f"تم تصحيح الكلمة، لكن تعذّر التبديل إلى {target_name}؛ سأعيد المحاولة عند كلمة واثقة أخرى"
                )

    def _run(self):
        try:
            self._run_hook()
        except Exception as exc:
            self.error = exc
            self.ready.set()
            self.monitor.on_status(f"تعذّر تهيئة مراقبة لوحة المفاتيح: {exc}")

    def _run_hook(self):
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [
                ("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        hook_proc_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
        )
        self.user32.CallNextHookEx.restype = ctypes.c_ssize_t
        self.user32.CallNextHookEx.argtypes = [
            wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM,
        ]
        self.user32.SetWindowsHookExW.restype = wintypes.HHOOK
        self.user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int, hook_proc_type, wintypes.HINSTANCE, wintypes.DWORD,
        ]
        self.user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        self.user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        self.user32.GetMessageW.restype = ctypes.c_int
        self.user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self.user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self.user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        self.user32.GetKeyboardState.argtypes = [ctypes.POINTER(ctypes.c_ubyte)]
        self.user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
        self.user32.GetKeyboardLayout.restype = wintypes.HKL
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user32.GetAsyncKeyState.restype = ctypes.c_short
        self.user32.GetKeyState.argtypes = [ctypes.c_int]
        self.user32.GetKeyState.restype = ctypes.c_short
        self.user32.LoadKeyboardLayoutW.argtypes = [wintypes.LPCWSTR, wintypes.UINT]
        self.user32.LoadKeyboardLayoutW.restype = wintypes.HKL
        self.user32.ToUnicodeEx.argtypes = [
            wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_ubyte),
            ctypes.POINTER(wintypes.WCHAR), ctypes.c_int, wintypes.UINT, wintypes.HKL,
        ]
        self.user32.SendInput.restype = wintypes.UINT
        self.user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
        self.kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self.kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self.kernel32.GetModuleHandleW.restype = wintypes.HMODULE

        @hook_proc_type
        def hook_proc(n_code, w_param, l_param):
            if n_code == HC_ACTION and l_param:
                try:
                    event = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    if not event.flags & LLKHF_INJECTED:
                        suppress = self._handle_event(int(w_param), event)
                        if suppress:
                            return 1
                except Exception as exc:
                    self.monitor.on_status(f"فشل استقبال لوحة المفاتيح: {exc}")
            return self.user32.CallNextHookEx(self.hook, n_code, w_param, l_param)

        @hook_proc_type
        def mouse_hook_proc(n_code, w_param, l_param):
            if n_code == HC_ACTION and int(w_param) in (WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN):
                try:
                    self.monitor.clear_buffer()
                except Exception:
                    pass
            return self.user32.CallNextHookEx(self.mouse_hook, n_code, w_param, l_param)

        self.callback_ref = hook_proc
        self.mouse_callback_ref = mouse_hook_proc
        self.thread_id = self.kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        self.user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        module = self.kernel32.GetModuleHandleW(None)
        self.hook = self.user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self.callback_ref, module, 0
        )
        if not self.hook:
            self.error = ctypes.WinError(ctypes.get_last_error())
            self.ready.set()
            return
        self.mouse_hook = self.user32.SetWindowsHookExW(
            WH_MOUSE_LL, self.mouse_callback_ref, module, 0
        )

        self.ready.set()
        try:
            while self.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_APP_INJECT:
                    self._send_pending_replacement()
                else:
                    self.user32.TranslateMessage(ctypes.byref(msg))
                    self.user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if self.mouse_hook:
                self.user32.UnhookWindowsHookEx(self.mouse_hook)
                self.mouse_hook = None
            self.user32.UnhookWindowsHookEx(self.hook)
            self.hook = None

    def _reconcile_stuck_keys(self, now: float, force: bool = False):
        if self.modifiers_down and (force or now - self.modifier_down_at > 3.0):
            self.modifiers_down = {
                vk for vk in self.modifiers_down
                if self.user32.GetAsyncKeyState(vk) & 0x8000
            }
        if self.shift_keys_down and (force or now - self.shift_down_at > 3.0):
            self.shift_keys_down = {
                vk for vk in self.shift_keys_down
                if self.user32.GetAsyncKeyState(vk) & 0x8000
            }

    def _handle_event(self, message: int, event) -> bool:
        key = int(event.vkCode)
        is_down = message in (WM_KEYDOWN, WM_SYSKEYDOWN)
        is_up = message in (WM_KEYUP, WM_SYSKEYUP)
        if not is_down and not is_up:
            return False

        if is_up:
            if key in self.suppressed_keyups:
                self.suppressed_keyups.discard(key)
                return True
            if key in VK_SHIFT_KEYS:
                self.shift_keys_down.discard(key)
                return False
            if key in VK_CONTROL_KEYS | VK_ALT_KEYS | VK_WINDOWS_KEYS:
                self.modifiers_down.discard(key)
            return False

        if key in self.suppressed_keyups:
            return True

        if key in VK_LOCK_KEYS:
            return False

        now = time.monotonic()
        if key in VK_SHIFT_KEYS:
            self.shift_keys_down.add(key)
            self.shift_down_at = now
            return False
        if key in VK_CONTROL_KEYS | VK_ALT_KEYS | VK_WINDOWS_KEYS:
            self.modifiers_down.add(key)
            self.modifier_down_at = now
            self.monitor.clear_buffer()
            return False

        hwnd = self.user32.GetForegroundWindow() or 0
        hwnd_changed = self.last_hwnd != 0 and hwnd != self.last_hwnd
        self.last_hwnd = hwnd
        if hwnd_changed or (self.last_key_time and now - self.last_key_time > IDLE_RESET_SECONDS):
            self.monitor.clear_buffer()
        self.last_key_time = now

        self._reconcile_stuck_keys(now, force=hwnd_changed)
        if self.modifiers_down & (VK_CONTROL_KEYS | VK_ALT_KEYS | VK_WINDOWS_KEYS):
            self.monitor.clear_buffer()
            return False

        if key == VK_SPACE:
            suppress = self.monitor.finish_native_word(" ", hwnd)
            if suppress:
                self.suppressed_keyups.add(key)
            return suppress
        if key == VK_BACK:
            self.monitor.native_backspace()
            return False
        if key == VK_DELETE or key in VK_NAVIGATION_KEYS or key in (VK_RETURN, VK_TAB, VK_ESCAPE):
            self.monitor.clear_buffer()
            return False

        char = self._translate_key(
            int(event.vkCode), int(event.scanCode), hwnd,
            shift_pressed=bool(self.shift_keys_down),
        )
        if self.shift_keys_down and (self._active_layout_id(hwnd) & 0x03FF) == PRIMARYLANGID_ARABIC:
            us_layout = self.user32.LoadKeyboardLayoutW("00000409", 0)
            if us_layout:
                physical = self._translate_key(
                    int(event.vkCode), int(event.scanCode), hwnd,
                    layout_override=us_layout,
                    shift_pressed=True,
                )
                if len(physical) == 1 and physical.isascii() and physical.isalpha():
                    char = physical
        if not char:
            self.monitor.clear_buffer()
            return False
        if all(
            c.isalpha() or c in "'’-" or c in ARABIC_LAYOUT_SYMBOLS
            for c in char
        ):
            self.monitor.native_character(char)
        elif len(char) == 1 and not char.isdigit():
            suppress = self.monitor.finish_native_word(char, hwnd)
            if suppress:
                self.suppressed_keyups.add(key)
            return suppress
        else:
            self.monitor.clear_buffer()
        return False

    def _active_layout_id(self, hwnd) -> int:
        thread_id = self.user32.GetWindowThreadProcessId(hwnd, None) if hwnd else 0
        layout = self.user32.GetKeyboardLayout(thread_id)
        if isinstance(layout, int):
            value = layout
        else:
            value = ctypes.cast(layout, ctypes.c_void_p).value or 0
        return value & 0xFFFF

    def _translate_key(
        self,
        vk_code: int,
        scan_code: int,
        hwnd,
        *,
        layout_override=None,
        shift_pressed: bool | None = None,
    ) -> str:
        thread_id = self.user32.GetWindowThreadProcessId(hwnd, None) if hwnd else 0
        layout = layout_override or self.user32.GetKeyboardLayout(thread_id)
        state = (ctypes.c_ubyte * 256)()
        self.user32.GetKeyboardState(state)
        for key in VK_SHIFT_KEYS | VK_CONTROL_KEYS | VK_ALT_KEYS:
            if self.user32.GetAsyncKeyState(key) & 0x8000:
                state[key] |= 0x80
        get_key_state = getattr(self.user32, "GetKeyState", None)
        if get_key_state is not None and (get_key_state(VK_CAPITAL) & 1):
            state[VK_CAPITAL] |= 0x01
        if shift_pressed is not None:
            shifted_keys = self.shift_keys_down if shift_pressed else set()
            state[0x10] = (state[0x10] & 0x01) | (0x80 if shifted_keys else 0)
            for key in VK_SHIFT_KEYS - {0x10}:
                state[key] = (state[key] & 0x01) | (0x80 if key in shifted_keys else 0)
        if 0 <= vk_code < 256:
            state[vk_code] |= 0x80
        output = (wintypes.WCHAR * 8)()
        length = self.user32.ToUnicodeEx(
            vk_code, scan_code, state, output, len(output), TU_NO_STATE_CHANGE, layout
        )
        return "".join(output[:length]) if length > 0 else ""

    def inject_replacement(self, backspaces: int, text: str) -> bool:
        records = build_keyboard_sequence(backspaces, text)
        inputs = (INPUT * len(records))()
        for index, (vk, scan, flags) in enumerate(records):
            inputs[index].type = INPUT_KEYBOARD
            inputs[index].ki = KEYBDINPUT(vk, scan, flags, 0, 0)
        record_size = ctypes.sizeof(INPUT)
        sent_total = 0
        while sent_total < len(inputs):
            sent = self.user32.SendInput(
                len(inputs) - sent_total,
                ctypes.byref(inputs, sent_total * record_size),
                record_size,
            )
            if not sent:
                return False
            sent_total += sent
        return True

    def queue_replacement(self, backspaces: int, text: str) -> bool:
        """Inject after the low-level hook returns and suppresses the boundary."""
        with self.pending_lock:
            if self.pending_replacement is not None:
                return False
            self.pending_replacement = (backspaces, text)
        if not self.user32.PostThreadMessageW(self.thread_id, WM_APP_INJECT, 0, 0):
            with self.pending_lock:
                self.pending_replacement = None
            return False
        return True

    def _send_pending_replacement(self):
        with self.pending_lock:
            pending = self.pending_replacement
            self.pending_replacement = None
        if pending is None:
            return
        sent = False
        try:
            sent = self.inject_replacement(*pending)
            if not sent:
                self.monitor.on_status("فشل إدخال النص المصحح")
        except Exception as exc:
            sent = False
            self.monitor.on_status(f"فشل إدخال النص المصحح: {exc}")
        finally:
            self.monitor.native_replacement_complete(sent)

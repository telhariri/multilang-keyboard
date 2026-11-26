import os
import platform
import socket
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import layout_switcher
import main
import wayland_backend
from wayland_backend import WaylandCorrectionServer
from windows_backend import VK_SPACE, WM_KEYDOWN, WindowsKeyboardHook


class WindowsBackendEnhancementTests(unittest.TestCase):
    def test_lam_alef_counts_two_backspaces_and_single_backspace_keeps_lam(self):
        monitor = main.TypingMonitor(lambda _status: None)
        monitor.enabled = True
        monitor.native_character("لا")
        monitor.native_character("خ")
        self.assertEqual(monitor.buffer, "لاخ")
        self.assertEqual(monitor.backspace_count, 3)

        monitor.native_backspace()
        self.assertEqual(monitor.buffer, "لا")
        self.assertEqual(monitor.backspace_count, 2)

        monitor.native_backspace()
        self.assertEqual(monitor.buffer, "ل")
        self.assertEqual(monitor.backspace_count, 1)

    def test_digits_clear_buffer_and_are_not_captured_on_windows_hook(self):
        received = []
        cleared = []
        monitor = SimpleNamespace(
            native_character=received.append,
            native_backspace=Mock(),
            finish_native_word=Mock(return_value=False),
            clear_buffer=lambda: cleared.append(True),
        )
        hook = WindowsKeyboardHook(monitor)

        class FakeUser32:
            def GetForegroundWindow(self):
                return 42

            def GetWindowThreadProcessId(self, _hwnd, _pid):
                return 10

            def GetKeyboardLayout(self, _tid):
                return 0x0409

            def GetKeyboardState(self, _state):
                return 1

            def GetAsyncKeyState(self, _vk):
                return 0

            def ToUnicodeEx(self, vk, _scan, _state, output, _cap, _flags, _layout):
                output[0] = chr(vk)
                return 1

        hook.user32 = FakeUser32()
        hook.last_hwnd = 42
        hook._handle_event(WM_KEYDOWN, SimpleNamespace(vkCode=ord("5"), scanCode=6))
        self.assertEqual(received, [])
        self.assertTrue(cleared)

    def test_hwnd_change_and_vk_bounds_are_safe_in_windows_hook(self):
        received = []
        cleared = []
        monitor = SimpleNamespace(
            native_character=received.append,
            native_backspace=Mock(),
            finish_native_word=Mock(return_value=False),
            clear_buffer=lambda: cleared.append(True),
        )
        hook = WindowsKeyboardHook(monitor)

        class FakeUser32:
            def __init__(self):
                self.hwnd = 10

            def GetForegroundWindow(self):
                return self.hwnd

            def GetWindowThreadProcessId(self, _hwnd, _pid):
                return 10

            def GetKeyboardLayout(self, _tid):
                return 0x0C01  # Egyptian Arabic primary lang ID 0x01

            def GetKeyboardState(self, _state):
                return 1

            def GetAsyncKeyState(self, _vk):
                return 0

            def LoadKeyboardLayoutW(self, _name, _flags):
                return 0x0409

            def ToUnicodeEx(self, vk, _scan, _state, output, _cap, _flags, _layout):
                if vk > 255:
                    return 0
                output[0] = "a"
                return 1

        user32 = FakeUser32()
        hook.user32 = user32
        hook.last_hwnd = 10
        # Oversized VK code should not raise IndexError.
        hook._handle_event(WM_KEYDOWN, SimpleNamespace(vkCode=300, scanCode=0))
        # Window switch clears buffer.
        user32.hwnd = 20
        hook._handle_event(WM_KEYDOWN, SimpleNamespace(vkCode=ord("A"), scanCode=30))
        self.assertTrue(cleared)
        self.assertEqual(hook._active_layout_id(20) & 0x03FF, 0x01)

    def test_non_blocking_async_layout_worker_runs_switch_off_hook(self):
        statuses = []
        done = threading.Event()
        monitor = main.TypingMonitor(lambda msg: (statuses.append(msg), done.set()))
        monitor.enabled = True
        monitor.switcher.switch = Mock(return_value=True)

        hook = WindowsKeyboardHook(monitor)
        hook.queue_replacement = Mock(return_value=True)
        hook.switch_queue = __import__("queue").Queue()
        worker = threading.Thread(target=hook._layout_worker, daemon=True)
        worker.start()

        try:
            monitor.listener = hook
            monitor.buffer = "اثممخ"
            monitor.buffer_units = list("اثممخ")
            monitor.backspace_count = 5

            self.assertTrue(monitor.finish_native_word(" ", hwnd=55))
            # Crucial guarantee: hook callback never calls switcher.switch synchronously.
            monitor.switcher.switch.assert_not_called()

            monitor.native_replacement_complete(True)
            self.assertTrue(done.wait(timeout=2))
            monitor.switcher.switch.assert_called_once_with("en", hwnd=55)
            self.assertIn("English", statuses[-1])
        finally:
            hook.switch_queue.put(None)
            worker.join(timeout=2)


class X11AndLayoutSwitcherTests(unittest.TestCase):
    def test_x11_replacement_pastes_before_switching_layout(self):
        monitor = main.TypingMonitor(lambda _status: None)
        events = []

        class FakeController:
            def press(self, key):
                events.append(("press", key))

            def release(self, key):
                events.append(("release", key))

            def pressed(self, key):
                class Ctx:
                    def __enter__(self_inner):
                        events.append(("hold", key))

                    def __exit__(self_inner, *_args):
                        events.append(("unhold", key))

                return Ctx()

            def type(self, text):
                events.append(("type", text))

        def record_switch(lang):
            events.append(("switch", lang))
            return True

        monitor.switcher.switch = record_switch
        with patch.object(main.keyboard, "Controller", return_value=FakeController()), patch.object(
            main.pyperclip, "paste", return_value="prev"
        ), patch.object(main.pyperclip, "copy") as copy_mock, patch.object(main.time, "sleep"):
            monitor._replace_typed_word(4, "سلام ", "ar", True)

        paste_idx = events.index(("hold", main.keyboard.Key.ctrl))
        switch_idx = events.index(("switch", "ar"))
        self.assertLess(paste_idx, switch_idx)
        copy_mock.assert_any_call("سلام ")
        copy_mock.assert_any_call("prev")

    def test_x11_clipboard_failure_falls_back_to_controller_type(self):
        monitor = main.TypingMonitor(lambda _status: None)
        typed = []

        class FakeController:
            def press(self, _key):
                pass

            def release(self, _key):
                pass

            def type(self, text):
                typed.append(text)

        monitor.switcher.switch = Mock(return_value=True)
        with patch.object(main.keyboard, "Controller", return_value=FakeController()), patch.object(
            main.pyperclip, "paste", side_effect=RuntimeError("no xclip")
        ), patch.object(main.time, "sleep"):
            monitor._replace_typed_word(3, "hi ", "en", True)

        self.assertEqual(typed, ["hi "])

    def test_x11_on_press_excludes_digits_and_handles_two_char_backspace(self):
        monitor = main.TypingMonitor(lambda _status: None)
        monitor.enabled = True
        monitor._on_press(SimpleNamespace(char="لا"))
        self.assertEqual(monitor.backspace_count, 2)
        monitor._on_press(main.keyboard.Key.backspace)
        self.assertEqual(monitor.buffer, "ل")
        self.assertEqual(monitor.backspace_count, 1)
        monitor._on_press(SimpleNamespace(char="7"))
        self.assertEqual(monitor.buffer, "")
        self.assertEqual(monitor.backspace_count, 0)

    def test_layout_matches_arabic_variants(self):
        for variant in ("ara", "sy", "eg", "ara(digits)", "Sy"):
            self.assertTrue(layout_switcher._layout_matches(variant, "ar"))
        for variant in ("us", "gb", "uk", "us(intl)"):
            self.assertTrue(layout_switcher._layout_matches(variant, "en"))
        self.assertFalse(layout_switcher._layout_matches("fr", "ar"))

    def test_windows_layout_switcher_uses_installed_primary_lang_id(self):
        from ctypes import wintypes

        posted = []

        class FakeUser32:
            def __init__(self):
                self.current = 0x0409  # US English

            def GetForegroundWindow(self):
                return 777

            def GetWindowThreadProcessId(self, _hwnd, _pid):
                return 888

            def GetKeyboardLayout(self, _tid):
                return self.current

            def GetKeyboardLayoutList(self, count, buf):
                installed = [0x0409, 0x0C01]  # US + Arabic Egypt
                if count == 0:
                    return len(installed)
                for idx, val in enumerate(installed):
                    buf[idx] = val
                return len(installed)

            def LoadKeyboardLayoutW(self, *_args):
                raise AssertionError("Should not load 0401 when 0C01 is already installed")

            def GetGUIThreadInfo(self, _tid, _ptr):
                return 0

            def PostMessageW(self, hwnd, msg, wparam, lparam):
                posted.append((hwnd, msg, wparam, lparam))
                self.current = 0x0C01
                return 1

        class _Callable:
            def __init__(self, fn):
                self._fn = fn
                self.restype = None
                self.argtypes = None

            def __call__(self, *args):
                return self._fn(*args)

        fake = FakeUser32()
        lib = SimpleNamespace(**{
            fn: _Callable(getattr(fake, fn))
            for fn in (
                "LoadKeyboardLayoutW",
                "GetForegroundWindow",
                "GetWindowThreadProcessId",
                "GetKeyboardLayout",
                "GetKeyboardLayoutList",
                "GetGUIThreadInfo",
                "PostMessageW",
            )
        })

        with patch.object(layout_switcher.ctypes, "WinDLL", return_value=lib, create=True):
            self.assertTrue(layout_switcher.LayoutSwitcher._switch_windows("ar"))
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0][3] & 0x03FF, 0x01)


class WaylandServerAndCliTests(unittest.TestCase):
    @unittest.skipUnless(
        os.name == "posix" and hasattr(socketserver, "ThreadingUnixStreamServer"),
        "Unix domain socket server is POSIX-only",
    )
    def test_wayland_ipc_supports_state_and_show_and_handles_bad_input(self):
        old_runtime = os.environ.get("XDG_RUNTIME_DIR")
        shown = []
        with tempfile.TemporaryDirectory() as runtime:
            os.environ["XDG_RUNTIME_DIR"] = runtime
            monitor = SimpleNamespace(
                enabled=True,
                current_wayland_language=lambda: "ar",
                correct_wayland_word=lambda word: ("right", "en", None) if word == "قهلاف" else None,
                show_window=lambda: shown.append(True),
            )
            server = WaylandCorrectionServer(monitor)
            try:
                server.start()
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(1)
                    client.connect(str(server.path))
                    with client.makefile("rb") as reader:
                        client.sendall(b"STATE\nWORD\t\xd9\x82\xd9\x87\xd9\x84\xd8\xa7\xd9\x81\nUNKNOWN\nSHOW\n")
                        self.assertEqual(reader.readline(), b"READY\tar\n")
                        self.assertEqual(reader.readline(), b"FIX\ten\t?\tright\n")
                        self.assertEqual(reader.readline(), b"ERR\n")
                        self.assertEqual(reader.readline(), b"SHOWN\n")
                self.assertEqual(shown, [True])
                self.assertTrue(wayland_backend.show_running_instance())
            finally:
                server.stop()
                if old_runtime is None:
                    os.environ.pop("XDG_RUNTIME_DIR", None)
                else:
                    os.environ["XDG_RUNTIME_DIR"] = old_runtime

    def test_ui_text_only_reshapes_on_linux(self):
        with patch.object(platform, "system", return_value="Windows"):
            self.assertEqual(main.ui_text("مرحبا"), "مرحبا")

    def test_cli_self_test_and_version_succeed(self):
        self.assertEqual(main.run_self_test(), 0)
        self.assertEqual(main.main_cli(["--self-test"]), 0)
        with self.assertRaises(SystemExit) as ctx:
            main.main_cli(["--version"])
        self.assertEqual(ctx.exception.code, 0)

    def test_cli_handles_none_stdout_and_stderr_when_windowed(self):
        with patch.object(sys, "stdout", None), patch.object(sys, "stderr", None):
            self.assertEqual(main.main_cli(["--self-test"]), 0)
            with self.assertRaises(SystemExit) as ctx:
                main.main_cli(["--version"])
            self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()

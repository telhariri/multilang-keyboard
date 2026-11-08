import ctypes
import os
import socket
import stat
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main
from rust_backend import RustKeyboardHook
from windows_backend import (
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    INPUT,
    VK_BACK,
    VK_SPACE,
    WM_APP_INJECT,
    WM_KEYDOWN,
    WM_KEYUP,
    WindowsKeyboardHook,
    build_keyboard_sequence,
)
from wayland_backend import WaylandCorrectionServer


class ConversionTests(unittest.TestCase):
    def test_arabic_layout_text_maps_back_to_english(self):
        self.assertEqual(main.to_english("اثممخ اخص شقث غخع"), "hello how are you")

    def test_english_layout_text_maps_back_to_arabic(self):
        self.assertEqual(main.to_arabic("sghl"), "سلام")

    def test_language_recognition_corrects_both_directions(self):
        self.assertEqual(main.TypingMonitor._recognize("اثممخ"), ("hello", "en"))
        self.assertEqual(main.TypingMonitor._recognize("sghl"), ("سلام", "ar"))

    def test_known_words_in_their_own_language_are_never_rewritten(self):
        self.assertEqual(main.TypingMonitor._recognize("اذهب"), (None, None))
        self.assertEqual(main.TypingMonitor._recognize("خطا"), (None, None))
        self.assertEqual(main.TypingMonitor._recognize("hello"), (None, None))
        self.assertEqual(main.TypingMonitor._recognize("erm"), (None, None))

    def test_known_arabic_and_english_vocabulary_is_not_converted_crosswise(self):
        for word in main.ARABIC_WORDS:
            with self.subTest(word=word):
                self.assertNotEqual(main.TypingMonitor._recognize(word)[1], "en")
        for word in main.ENGLISH_WORDS:
            with self.subTest(word=word):
                self.assertNotEqual(main.TypingMonitor._recognize(word)[1], "ar")

    def test_missing_frequency_data_disables_automatic_conversion(self):
        with patch.object(main, "zipf_frequency", None):
            self.assertEqual(main.TypingMonitor._recognize("hgg,pm"), (None, None))

    def test_arabic_punctuation_keys_are_kept_inside_mislaid_words(self):
        self.assertEqual(main.TypingMonitor._recognize("hgg,pm"), ("اللوحة", "ar"))
        self.assertEqual(main.TypingMonitor._recognize("fa;g"), ("بشكل", "ar"))
        self.assertEqual(main.TypingMonitor._recognize("hgho'hx"), (None, None))
        self.assertEqual(main.TypingMonitor._recognize("ال;edv"), ("الكثير", "ar"))
        self.assertEqual(main.TypingMonitor._recognize(";ما"), ("كما", "ar"))
        self.assertEqual(main.TypingMonitor._recognize("hg,h[ihj"), (None, None))
        self.assertEqual(main.TypingMonitor._recognize("d[f"), ("يجب", "ar"))
        self.assertEqual(main.TypingMonitor._recognize("df]g"), (None, None))
        self.assertEqual(main.TypingMonitor._recognize("gghk;gd.dm"), (None, None))
        self.assertEqual(main.TypingMonitor._recognize("hk;gd.dm"), (None, None))
        self.assertEqual(main.TypingMonitor._recognize("h`h"), ("اذا", "ar"))
        self.assertEqual(main.TypingMonitor._recognize("lu]g"), ("معدل", "ar"))
        self.assertEqual(main.TypingMonitor._recognize("erm"), (None, None))
        self.assertEqual(main.TypingMonitor._recognize("[]h"), ("جدا", "ar"))

    def test_language_decision_needs_minimum_score_and_margin(self):
        self.assertTrue(main._passes_language_confidence(4.0, 2.0))
        self.assertFalse(main._passes_language_confidence(3.99, 0.0))
        self.assertFalse(main._passes_language_confidence(5.0, 3.01))

    def test_per_word_recognition_corrects_in_both_directions_and_stays_in_language(self):
        self.assertEqual(main.TypingMonitor._recognize("اثممخ"), ("hello", "en"))
        self.assertEqual(main.TypingMonitor._recognize("Hثممخ"), ("Hello", "en"))
        self.assertEqual(main.TypingMonitor._recognize("hello"), (None, None))
        self.assertEqual(main.TypingMonitor._recognize("hgg,pm"), ("اللوحة", "ar"))
        self.assertEqual(main.TypingMonitor._recognize("اللوحة"), (None, None))

    def test_one_confident_corrected_word_switches_layout_immediately(self):
        monitor = main.TypingMonitor(lambda _status: None)

        class FakeListener:
            def __init__(self):
                self.queued = []

            def queue_replacement(self, count, text):
                self.queued.append((count, text))
                return True

            def stop(self):
                pass

        monitor.listener = FakeListener()
        monitor.switcher.switch = Mock(return_value=True)
        monitor.enabled = True
        for raw, replacement, target in (
            ("اثممخ", "hello", "en"),
            ("hgg,pm", "اللوحة", "ar"),
        ):
            monitor.buffer = raw
            monitor.buffer_units = list(raw)
            monitor.backspace_count = len(raw)
            self.assertTrue(monitor.finish_native_word(" ", hwnd=123))
            self.assertEqual(monitor.listener.queued[-1], (len(raw), replacement + " "))
            self.assertTrue(monitor.pending_layout_switch_requested)
            self.assertEqual(monitor.pending_language, target)
            monitor.native_replacement_complete(True)

        self.assertEqual(monitor.switcher.switch.call_args_list, [
            unittest.mock.call("en", hwnd=123),
            unittest.mock.call("ar", hwnd=123),
        ])

    def test_failed_switch_keeps_correction_and_retries_on_next_confident_word(self):
        statuses = []
        monitor = main.TypingMonitor(statuses.append)

        class FakeListener:
            def queue_replacement(self, count, text):
                return True

        monitor.listener = FakeListener()
        monitor.switcher.switch = Mock(return_value=False)
        monitor.enabled = True
        for raw in ("اثممخ", "hgg,pm"):
            monitor.buffer = raw
            monitor.buffer_units = list(raw)
            monitor.backspace_count = len(raw)
            self.assertTrue(monitor.finish_native_word(" ", hwnd=123))
            monitor.native_replacement_complete(True)

        self.assertEqual(monitor.switcher.switch.call_count, 2)
        self.assertEqual(monitor._recognize_word("اثممخ"), ("hello", "en"))
        self.assertIn("سأعيد المحاولة", statuses[-1])

    def test_ambiguous_word_is_not_corrected_or_switched(self):
        monitor = main.TypingMonitor(lambda _status: None)
        self.assertEqual(monitor._recognize_word("hg,h[ihj"), (None, None))
        monitor.switcher.switch = Mock()
        monitor.enabled = True
        monitor.buffer = "hg,h[ihj"
        monitor.buffer_units = list(monitor.buffer)
        monitor.backspace_count = len(monitor.buffer)
        self.assertFalse(monitor.finish_native_word(" ", hwnd=123))
        monitor.switcher.switch.assert_not_called()

    def test_kde_layout_switch_is_reported_only_after_readback(self):
        layouts = "([('us', '', 'English (US)'), ('ara', '', 'Arabic')],)"
        with patch.object(main.shutil, "which", return_value="/usr/bin/gdbus"), patch.object(
            main.subprocess,
            "check_output",
            side_effect=[layouts, "(uint32 0,)", "(true,)", "(uint32 1,)"]
        ) as dbus_call, patch.dict(os.environ, {"XDG_SESSION_TYPE": "x11"}):
            self.assertTrue(main.LayoutSwitcher._switch_kde("ar"))
        self.assertEqual(dbus_call.call_count, 4)

    def test_kde_does_not_claim_switch_when_target_layout_is_missing(self):
        layouts = "([('us', '', 'English (US)')],)"
        with patch.object(main.shutil, "which", return_value="/usr/bin/gdbus"), patch.object(
            main.subprocess, "check_output", return_value=layouts
        ) as dbus_call, patch.dict(os.environ, {"XDG_SESSION_TYPE": "x11"}):
            self.assertFalse(main.LayoutSwitcher._switch_kde("ar"))
        self.assertEqual(dbus_call.call_count, 1)

    def test_kde_wayland_requires_fcitx_sync_even_when_kwin_already_matches(self):
        layouts = "([('ara', '', 'Arabic'), ('us', '', 'English (US)')],)"
        with patch.object(main.shutil, "which", return_value="/usr/bin/gdbus"), patch.object(
            main.subprocess, "check_output", side_effect=[layouts, "(uint32 1,)"]
        ), patch.object(main.LayoutSwitcher, "_sync_fcitx_wayland_layout", return_value=False) as sync, patch.dict(
            os.environ, {"XDG_SESSION_TYPE": "wayland"}
        ):
            self.assertFalse(main.LayoutSwitcher._switch_kde("en"))
        sync.assert_called_once_with("en")

    def test_kde_reports_the_current_language_from_kwin(self):
        layouts = "([('ara', '', 'Arabic'), ('us', '', 'English (US)')],)"
        with patch.object(main.shutil, "which", return_value="/usr/bin/gdbus"), patch.object(
            main.subprocess, "check_output", side_effect=[layouts, "(uint32 1,)"]
        ):
            self.assertEqual(main.LayoutSwitcher.current_kde_language(), "en")

    def test_fcitx_wayland_layout_refresh_preserves_items_and_reactivates_multilang(self):
        methods = []
        group_info = "('sy', [('keyboard-sy', ''), ('keyboard-us', ''), ('multilang', '')])"
        verified_group_info = "('us', [('keyboard-sy', ''), ('keyboard-us', ''), ('multilang', '')])"
        info_calls = 0

        def check_output(args, **_kwargs):
            nonlocal info_calls
            if args[0] == "/usr/bin/gdbus":
                method = args[args.index("--method") + 1]
                methods.append(method)
            else:
                method = None
            if method == "org.fcitx.Fcitx.Controller1.CurrentInputMethodGroup":
                return "('Default',)"
            if method == "org.fcitx.Fcitx.Controller1.InputMethodGroupInfo":
                info_calls += 1
                return group_info if info_calls == 1 else verified_group_info
            if method == "org.fcitx.Fcitx.Controller1.SetInputMethodGroupInfo":
                self.assertEqual(args[-3], "Default")
                self.assertEqual(args[-2], "us")
                self.assertIn("('keyboard-sy', '')", args[-1])
                self.assertIn("('multilang', '')", args[-1])
                return "()"
            if args[0] == "/usr/bin/fcitx5-remote" and args[-1] == "-n":
                return "multilang\n"
            raise AssertionError(args)

        with patch.object(main.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"), patch.object(
            main.subprocess, "check_output", side_effect=check_output
        ), patch.object(main.subprocess, "run") as run_call:
            self.assertTrue(main.LayoutSwitcher._sync_fcitx_wayland_layout("en"))

        self.assertEqual([call.args[0][-1] for call in run_call.call_args_list], [
            "-r", "keyboard-us", "multilang",
        ])
        self.assertEqual(len(methods), 4)


class WindowsInputTests(unittest.TestCase):
    def test_shift_is_kept_for_key_translation_and_english_case_under_arabic_layout(self):
        received = []
        monitor = SimpleNamespace(
            native_character=received.append,
            native_backspace=Mock(),
            finish_native_word=Mock(return_value=False),
            clear_buffer=Mock(),
        )
        hook = WindowsKeyboardHook(monitor)

        class FakeUser32:
            def GetForegroundWindow(self):
                return 99

            def GetWindowThreadProcessId(self, _hwnd, _process_id):
                return 7

            def GetKeyboardLayout(self, _thread_id):
                return 0x0401

            def GetKeyboardState(self, state):
                for index in range(256):
                    state[index] = 0
                return 1

            def GetAsyncKeyState(self, _key):
                return 0

            def LoadKeyboardLayoutW(self, _name, _flags):
                return 0x0409

            def ToUnicodeEx(
                self, _vk, _scan, state, output, _capacity, _flags, layout
            ):
                shifted = bool(state[0x10] & 0x80)
                if layout == 0x0409:
                    output[0] = "H" if shifted else "h"
                else:
                    output[0] = "َ" if shifted else "ث"
                return 1

        hook.user32 = FakeUser32()
        shift = SimpleNamespace(vkCode=0xA0, scanCode=42)
        letter = SimpleNamespace(vkCode=0x48, scanCode=35)

        self.assertFalse(hook._handle_event(WM_KEYDOWN, shift))
        self.assertFalse(hook._handle_event(WM_KEYDOWN, letter))
        self.assertEqual(received, ["H"])
        self.assertEqual(main.TypingMonitor._recognize("Hثممخ"), ("Hello", "en"))
        self.assertFalse(hook._handle_event(WM_KEYUP, shift))
        self.assertFalse(hook._handle_event(WM_KEYDOWN, letter))
        self.assertEqual(received, ["H", "ث"])

    def test_windows_input_union_has_native_size(self):
        expected_size = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        self.assertEqual(ctypes.sizeof(INPUT), expected_size)

    def test_rust_bridge_returns_utf8_replacement_and_target_layout(self):
        monitor = main.TypingMonitor(lambda _status: None)
        bridge = RustKeyboardHook(monitor)
        word = "اثممخ".encode("utf-8")
        word_buffer = (ctypes.c_ubyte * len(word)).from_buffer_copy(word)
        output_buffer = (ctypes.c_ubyte * 64)()
        output_len = ctypes.c_size_t()
        target_language = ctypes.c_uint32()

        corrected = bridge._correction_ref(
            word_buffer,
            len(word),
            output_buffer,
            len(output_buffer),
            ctypes.pointer(output_len),
            ctypes.pointer(target_language),
        )

        self.assertEqual(corrected, 1)
        self.assertEqual(ctypes.string_at(output_buffer, output_len.value), b"hello")
        self.assertEqual(target_language.value, 0x101)

    def test_rust_bridge_requests_switch_for_each_confident_correction(self):
        monitor = main.TypingMonitor(lambda _status: None)
        bridge = RustKeyboardHook(monitor)
        for word, expected_language in (("اثممخ", 0x101), ("hgg,pm", 0x102)):
            encoded = word.encode("utf-8")
            word_buffer = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
            output_buffer = (ctypes.c_ubyte * 64)()
            output_len = ctypes.c_size_t()
            target_language = ctypes.c_uint32()
            corrected = bridge._correction_ref(
                word_buffer,
                len(encoded),
                output_buffer,
                len(output_buffer),
                ctypes.pointer(output_len),
                ctypes.pointer(target_language),
            )
            self.assertEqual(corrected, 1)
            self.assertEqual(target_language.value, expected_language)

    def test_rust_status_keeps_correction_enabled_after_switch_failure(self):
        statuses = []
        monitor = main.TypingMonitor(statuses.append)
        bridge = RustKeyboardHook(monitor)
        message = b"MULTILANG_SWITCH_FAILED:ar"
        message_buffer = (ctypes.c_ubyte * len(message)).from_buffer_copy(message)

        bridge._status(message_buffer, len(message))

        self.assertEqual(monitor._recognize_word("اثممخ"), ("hello", "en"))
        self.assertIn("سأعيد المحاولة", statuses[-1])

    def test_unicode_sendinput_sequence_deletes_word_and_inserts_boundary(self):
        sequence = build_keyboard_sequence(2, "hello ")
        self.assertEqual(sequence[:4], [
            (VK_BACK, 0, 0), (VK_BACK, 0, KEYEVENTF_KEYUP),
            (VK_BACK, 0, 0), (VK_BACK, 0, KEYEVENTF_KEYUP),
        ])

        unicode_events = [entry for entry in sequence if entry[2] & KEYEVENTF_UNICODE]
        self.assertEqual(len(unicode_events), 12)
        self.assertEqual(
            [unicode_events[i][1] for i in range(0, len(unicode_events), 2)],
            list("hello ".encode("utf-16-le"))[::2],
        )
        self.assertTrue(all(entry[0] == 0 for entry in unicode_events))
        self.assertTrue(all(entry[2] & KEYEVENTF_KEYUP for entry in unicode_events[1::2]))

    def test_boundary_is_queued_and_not_replaced_through_clipboard(self):
        completed = []
        monitor = SimpleNamespace(native_replacement_complete=completed.append)
        hook = WindowsKeyboardHook(monitor)

        class FakeUser32:
            def __init__(self):
                self.messages = []

            def PostThreadMessageW(self, thread_id, message, w_param, l_param):
                self.messages.append((thread_id, message, w_param, l_param))
                return 1

        hook.user32 = FakeUser32()
        hook.thread_id = 71
        sent = []
        hook.inject_replacement = lambda backspaces, text: sent.append((backspaces, text)) or True

        self.assertTrue(hook.queue_replacement(5, "hello "))
        self.assertEqual(hook.user32.messages[0][1], WM_APP_INJECT)
        self.assertEqual(sent, [])
        hook._send_pending_replacement()
        self.assertEqual(sent, [(5, "hello ")])
        self.assertEqual(completed, [True])

    def test_corrected_boundary_keydown_repeat_and_keyup_are_suppressed(self):
        calls = []
        monitor = SimpleNamespace(
            finish_native_word=lambda boundary, hwnd: calls.append((boundary, hwnd)) or True,
            clear_buffer=Mock(),
        )
        hook = WindowsKeyboardHook(monitor)
        hook.user32 = SimpleNamespace(GetForegroundWindow=lambda: 99)
        event = SimpleNamespace(vkCode=VK_SPACE, scanCode=57)

        self.assertTrue(hook._handle_event(WM_KEYDOWN, event))
        self.assertTrue(hook._handle_event(WM_KEYDOWN, event))  # held-key repeat
        self.assertTrue(hook._handle_event(WM_KEYUP, event))
        self.assertFalse(hook._handle_event(WM_KEYUP, event))
        self.assertEqual(calls, [(" ", 99)])

    def test_native_word_replacement_suppresses_original_boundary(self):
        statuses = []
        monitor = main.TypingMonitor(statuses.append)

        class FakeListener:
            def __init__(self):
                self.queued = []

            def queue_replacement(self, count, text):
                self.queued.append((count, text))
                return True

        monitor.listener = FakeListener()
        monitor.switcher.switch = Mock(return_value=True)
        monitor.enabled = True
        monitor.buffer = "اثممخ"
        monitor.buffer_units = list(monitor.buffer)
        monitor.backspace_count = 5

        self.assertTrue(monitor.finish_native_word(" ", hwnd=123))
        self.assertEqual(monitor.listener.queued, [(5, "hello ")])
        self.assertTrue(monitor.pending_layout_switch_requested)
        monitor.switcher.switch.assert_called_once_with("en", hwnd=123)
        self.assertTrue(monitor.busy)
        monitor.native_replacement_complete(True)
        self.assertFalse(monitor.busy)
        self.assertIn("English", statuses[-1])


class BackgroundStartTests(unittest.TestCase):
    def test_successful_start_hides_window_and_keeps_tray(self):
        app = main.App.__new__(main.App)
        app.root = Mock()
        app.monitor = SimpleNamespace(enabled=False, start=lambda: setattr(app.monitor, "enabled", True))
        app._ensure_tray = Mock()

        app.start_background()

        app._ensure_tray.assert_called_once_with()
        app.root.withdraw.assert_called_once_with()

    def test_failed_start_leaves_window_visible(self):
        app = main.App.__new__(main.App)
        app.root = Mock()
        app.monitor = SimpleNamespace(enabled=False, start=Mock())
        app._ensure_tray = Mock()

        app.start_background()

        app._ensure_tray.assert_not_called()
        app.root.withdraw.assert_not_called()


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "Unix domain sockets are Linux-only here")
class WaylandIpcTests(unittest.TestCase):
    def test_fcitx_multilang_activation_is_verified(self):
        with patch.object(main.shutil, "which", return_value="/usr/bin/fcitx5-remote"):
            with patch.object(main.subprocess, "run") as run:
                with patch.object(
                    main.subprocess, "check_output", return_value="multilang\n"
                ):
                    self.assertTrue(main.LayoutSwitcher.activate_fcitx_multilang())
            run.assert_called_once_with(
                ["/usr/bin/fcitx5-remote", "-s", "multilang"],
                check=True,
                stdout=main.subprocess.DEVNULL,
                stderr=main.subprocess.DEVNULL,
                timeout=2,
            )

    def test_fcitx_multilang_activation_reports_wrong_current_method(self):
        with patch.object(main.shutil, "which", return_value="/usr/bin/fcitx5-remote"):
            with patch.object(main.subprocess, "run"):
                with patch.object(main.subprocess, "check_output", return_value="keyboard-sy\n"):
                    self.assertFalse(main.LayoutSwitcher.activate_fcitx_multilang())

    def test_fcitx_multilang_activation_requires_remote_tool(self):
        with patch.object(main.shutil, "which", return_value=None):
            self.assertFalse(main.LayoutSwitcher.activate_fcitx_multilang())

    def test_wayland_switches_immediately_after_confident_words_in_both_directions(self):
        monitor = main.TypingMonitor(lambda _status: None)
        monitor.enabled = True
        monitor._recognize = lambda word: {
            "اثممخ": ("hello", "en"),
            "hgg,pm": ("اللوحة", "ar"),
        }.get(word, (None, None))
        switch_calls = []
        first_switch = threading.Event()
        second_switch = threading.Event()

        def record_switch(language):
            switch_calls.append(language)
            (first_switch if len(switch_calls) == 1 else second_switch).set()
            return True

        monitor.switcher.switch = record_switch
        self.assertEqual(
            monitor.correct_wayland_word("اثممخ"), ("hello", "en", None)
        )
        self.assertTrue(first_switch.wait(timeout=1))
        self.assertEqual(
            monitor.correct_wayland_word("hgg,pm"), ("اللوحة", "ar", None)
        )
        self.assertTrue(second_switch.wait(timeout=1))
        self.assertEqual(switch_calls, ["en", "ar"])

    def test_fcitx_ipc_serves_readiness_and_word_correction(self):
        old_runtime = os.environ.get("XDG_RUNTIME_DIR")
        with tempfile.TemporaryDirectory() as runtime:
            os.environ["XDG_RUNTIME_DIR"] = runtime
            monitor = SimpleNamespace(
                enabled=True,
                current_wayland_language=lambda: "ar",
                correct_wayland_word=lambda word: ("hello", "en", True)
                if word == "اثممخ" else None,
            )
            server = WaylandCorrectionServer(monitor)
            try:
                server.start()
                self.assertEqual(stat.S_IMODE(server.path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(server.path.parent.stat().st_mode), 0o700)

                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(1)
                    client.connect(str(server.path))
                    with client.makefile("rb") as reader:
                        client.sendall(b"PING\nLANG\nWORD\t\xd8\xa7\xd8\xab\xd9\x85\xd9\x85\xd8\xae\n")
                        self.assertEqual(reader.readline(), b"READY\n")
                        self.assertEqual(reader.readline(), b"LANG\tar\n")
                        self.assertEqual(reader.readline(), b"FIX\ten\t1\thello\n")
            finally:
                server.stop()
                if old_runtime is None:
                    os.environ.pop("XDG_RUNTIME_DIR", None)
                else:
                    os.environ["XDG_RUNTIME_DIR"] = old_runtime
            self.assertFalse(server.path.exists())


if __name__ == "__main__":
    unittest.main()

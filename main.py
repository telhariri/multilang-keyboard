from __future__ import annotations

import argparse
import collections.abc
import contextlib
import ctypes
import logging
import os
import platform
import shutil  # noqa: F401 - re-exported for test mocking compatibility
import subprocess  # noqa: F401 - re-exported for test mocking compatibility
import sys
import threading
import time
import tkinter as tk
import pystray
from PIL import Image, ImageDraw

import recognizer
from layout_switcher import LayoutSwitcher
from recognizer import (
    ARABIC_LAYOUT_SYMBOLS,
    ARABIC_RANGE_END,
    ARABIC_RANGE_START,
    ARABIC_TO_ENGLISH,
    ARABIC_WORDS,
    BOUNDARY_PUNCTUATION,
    ENGLISH_TO_ARABIC,
    ENGLISH_WORD_PATTERN,
    ENGLISH_WORDS,
    MAX_NATIVE_SCORE,
    MIN_LANGUAGE_MARGIN,
    MIN_LANGUAGE_SCORE,
    SHIFTED_ASCII_TO_ARABIC,
    SHIFTED_HAMZA_MAP,
    _passes_language_confidence,
    has_arabic,
    reconstruct_arabic_from_shifted,
    to_arabic,
    to_arabic_with_hamzas,
    to_english,
    to_english_candidates,
    zipf_frequency,
)
from rust_backend import RustKeyboardHook, _library_candidates
from windows_backend import WindowsKeyboardHook

__version__ = "1.1.0"

logger = logging.getLogger("multilang")

try:
    import pyperclip
    from pynput import keyboard, mouse
except ImportError as exc:
    raise SystemExit(
        "المكتبات المطلوبة غير مثبّتة. شغّل: python -m pip install -r requirements.txt"
    ) from exc

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except ImportError:
    arabic_reshaper = None
    get_display = None

IDLE_RESET_SECONDS = 8.0
_WINDOWS_MUTEX_HANDLE = None


def ui_text(text: str) -> str:
    """Shape and reorder Arabic only on Linux Tk builds without native RTL rendering."""
    if platform.system() != "Linux":
        return text
    try:
        if float(getattr(tk, "TkVersion", 8.6)) >= 9.0:
            return text
    except (TypeError, ValueError):
        pass
    if arabic_reshaper is None or get_display is None:
        return text
    return get_display(arabic_reshaper.reshape(text), base_dir="R")


def _acquire_windows_single_instance() -> bool:
    """Return True if this is the first running instance on Windows."""
    global _WINDOWS_MUTEX_HANDLE
    if platform.system() != "Windows":
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        handle = kernel32.CreateMutexW(None, 0, "Local\\MultilangSingleInstance")
        err = ctypes.get_last_error()
        if handle and err == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return False
        _WINDOWS_MUTEX_HANDLE = handle
        return True
    except Exception:
        return True


def run_self_test() -> int:
    """Verify wordfreq data, core language recognition checks, and optional native DLL."""
    failures: list[str] = []

    if zipf_frequency is None:
        failures.append("wordfreq library is not available")
    else:
        if zipf_frequency("hello", "en") < 4.0:
            failures.append("wordfreq English data appears missing or corrupted")
        if zipf_frequency("مرحبا", "ar") < 4.0:
            failures.append("wordfreq Arabic data appears missing or corrupted")

    checks = [
        ("اثممخ", ("hello", "en")),
        ("قهلاف", ("right", "en")),
        ("\ufefbخخن", ("book", "en")),
        ("Hkh", ("أنا", "ar")),
        ("hgg,pm", ("اللوحة", "ar")),
        ("of.", (None, None)),
        ("Hي", (None, None)),
    ]
    for sample, expected in checks:
        observed = recognizer.recognize(sample, zipf_fn=zipf_frequency)
        if observed != expected:
            failures.append(f"recognition mismatch for {sample!r}: expected {expected!r}, got {observed!r}")

    if platform.system() == "Windows":
        dll_path = next((p for p in _library_candidates() if p.is_file()), None)
        if dll_path is None:
            if getattr(sys, "frozen", False):
                failures.append("native DLL is missing from packaged Windows build")
        else:
            try:
                lib = ctypes.CDLL(str(dll_path))
                lib.multilang_abi_version.argtypes = []
                lib.multilang_abi_version.restype = ctypes.c_uint32
                abi = lib.multilang_abi_version()
                if abi < 2:
                    failures.append(f"native DLL ABI too old: {abi}")
            except Exception as exc:
                failures.append(f"failed loading native DLL {dll_path}: {exc}")

    if failures:
        for item in failures:
            print(f"FAIL: {item}", file=sys.stderr)
        return 1

    print(f"Multilang {__version__} self-test passed ({len(checks)} recognition checks OK).")
    return 0


class TypingMonitor:
    def __init__(self, on_status):
        self.on_status = on_status
        self.switcher = LayoutSwitcher()
        self.listener = None
        self.mouse_listener = None
        self.instance_server = None
        self.buffer = ""
        self.buffer_units: list[str] = []
        self.backspace_count = 0
        self.last_key_time = 0.0
        self.enabled = False
        self.busy = False
        self.modifier_keys = set()
        self.pending_layout_switched = False
        self.pending_layout_switch_requested = False
        self.pending_language = None
        self.pending_hwnd = None
        self.layout_switch_lock = threading.Lock()
        self.wayland_server = None
        self.lock = threading.Lock()
        self._state_lock = threading.RLock()

    def start(self):
        if self.enabled:
            return
        self.clear_buffer()
        if platform.system() == "Linux" and os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
            try:
                # Wayland does not expose a process-wide keyboard hook. The Fcitx5
                # input method sends word candidates to this local service instead.
                from wayland_backend import WaylandCorrectionServer

                if not self.switcher.activate_fcitx_multilang():
                    raise RuntimeError(
                        "تعذّر التحقق من إضافة Multilang في Fcitx5. تأكد من تثبيتها وإعادة تشغيل Fcitx5."
                    )
                self._warm_language_model()
                self.wayland_server = WaylandCorrectionServer(self)
                self.wayland_server.start()
                self.enabled = True
                logger.debug("Wayland IPC server started cleanly")
                self.on_status("خدمة Wayland جاهزة — تعمل مع العربية والإنجليزية")
            except Exception as exc:
                logger.debug("Failed to start Wayland backend: %s", exc)
                self.wayland_server = None
                self.enabled = False
                self.on_status(f"تعذّر بدء تكامل Wayland: {exc}")
            return

        self.enabled = True
        try:
            # Load both offline tables before listening so first-word detection
            # does not stall inside a keyboard callback.
            self._warm_language_model()
            if platform.system() == "Windows":
                try:
                    self.listener = RustKeyboardHook(self)
                    self.listener.start()
                    logger.debug("Started Rust Windows keyboard hook")
                except (FileNotFoundError, OSError, RuntimeError) as exc:
                    logger.debug("Falling back to Python WindowsKeyboardHook: %s", exc)
                    self.listener = WindowsKeyboardHook(self)
                    self.listener.start()
            else:
                self.listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
                self.listener.start()
                self._start_x11_helpers()
            self.on_status("يعمل — التصحيح بعد المسافة")
        except Exception as exc:
            logger.debug("Failed to start keyboard listener: %s", exc)
            self.enabled = False
            self._stop_x11_helpers()
            self.on_status(f"تعذّر تشغيل مراقبة لوحة المفاتيح: {exc}")

    def _start_x11_helpers(self):
        try:
            self.mouse_listener = mouse.Listener(on_click=self._on_mouse_click)
            self.mouse_listener.start()
        except Exception as exc:
            logger.debug("X11 mouse listener unavailable: %s", exc)
            self.mouse_listener = None

        if platform.system() == "Linux":
            try:
                from wayland_backend import WaylandCorrectionServer

                self.instance_server = WaylandCorrectionServer(self, socket_name="instance.sock")
                self.instance_server.start()
            except Exception as exc:
                logger.debug("X11 single-instance socket unavailable: %s", exc)
                self.instance_server = None

    def _stop_x11_helpers(self):
        if self.mouse_listener is not None:
            try:
                self.mouse_listener.stop()
            except Exception:
                pass
            self.mouse_listener = None
        if self.instance_server is not None:
            try:
                self.instance_server.stop()
            except Exception:
                pass
            self.instance_server = None

    def _on_mouse_click(self, _x, _y, _button, pressed):
        if pressed:
            self.clear_buffer()

    @staticmethod
    def _warm_language_model():
        if zipf_frequency is not None:
            zipf_frequency("hello", "en")
            zipf_frequency("مرحبا", "ar")

    def stop(self):
        with self._state_lock:
            self.enabled = False
            self.clear_buffer()
            self.pending_layout_switched = False
            self.pending_layout_switch_requested = False
            self.pending_language = None
            self.pending_hwnd = None
        if self.wayland_server is not None:
            try:
                self.wayland_server.stop()
            finally:
                self.wayland_server = None
        self._stop_x11_helpers()
        if self.listener is not None:
            try:
                self.listener.stop()
            finally:
                self.listener = None
        self.on_status("متوقف")

    def correct_wayland_word(self, word: str):
        """Return a correction for the Fcitx5 module, which switches between AR/EN in-process."""
        if not self.enabled or not word:
            return None
        replacement, target = self._recognize_word(word)
        if replacement is None:
            return None
        self.on_status("تم التصحيح — " + ("English" if target == "en" else "العربية"))
        return replacement, target, True

    def current_wayland_language(self):
        if not self.enabled or platform.system() != "Linux":
            return None
        if os.environ.get("XDG_SESSION_TYPE", "").lower() != "wayland":
            return None
        fcitx_lang = self.switcher.current_fcitx_language()
        if fcitx_lang in ("en", "ar"):
            return fcitx_lang
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
        if "kde" not in desktop and "plasma" not in desktop:
            return None
        return self.switcher.current_kde_language()

    def _switch_wayland_layout(self, target: str):
        try:
            with self.layout_switch_lock:
                switched = self.switcher.switch(target)
        except Exception as exc:
            logger.debug("Wayland layout switch raised: %s", exc)
            switched = False
        if switched:
            self.on_status("تم التصحيح — " + ("English" if target == "en" else "العربية"))
        else:
            language = "English" if target == "en" else "العربية"
            self.on_status(
                f"تم تصحيح الكلمة، لكن تعذّر التبديل إلى {language}؛ سأعيد المحاولة عند كلمة واثقة أخرى"
            )

    def _recognize_word(self, word: str):
        return self._recognize(word)

    _MODIFIER_KEYS = frozenset(
        k for k in (
            getattr(keyboard.Key, name, None)
            for name in ("ctrl", "ctrl_l", "ctrl_r", "alt", "alt_l", "alt_r", "cmd", "cmd_l", "cmd_r")
        )
        if k is not None
    )
    _LOCK_KEYS = frozenset(
        k for k in (
            getattr(keyboard.Key, name, None)
            for name in ("caps_lock", "num_lock", "scroll_lock")
        )
        if k is not None
    )
    _SHIFT_KEYS = frozenset(
        k for k in (
            getattr(keyboard.Key, name, None)
            for name in ("shift", "shift_l", "shift_r")
        )
        if k is not None
    )

    def _on_press(self, key):
        with self._state_lock:
            if not self.enabled or self.busy:
                return
            is_hashable = isinstance(key, collections.abc.Hashable)
            if is_hashable and (key in self._SHIFT_KEYS or key in self._LOCK_KEYS):
                return
            if is_hashable and key in self._MODIFIER_KEYS:
                self.clear_buffer()
                self.modifier_keys.add(key)
                return
            if self.modifier_keys:
                return

            now = time.monotonic()
            if self.last_key_time and now - self.last_key_time > IDLE_RESET_SECONDS:
                self.clear_buffer()
            self.last_key_time = now

            try:
                char = key.char
            except AttributeError:
                char = None

            if key == keyboard.Key.space or char == " ":
                self._finish_word(" ")
            elif char and len(char) == 1 and char in BOUNDARY_PUNCTUATION and char not in ARABIC_LAYOUT_SYMBOLS:
                self._finish_word(char)
            elif char and all(
                c.isalpha() or c in "'’-" or c in ARABIC_LAYOUT_SYMBOLS
                for c in char
            ):
                self.buffer += char
                self.buffer_units.append(char)
                self.backspace_count += len(char)
            elif key == keyboard.Key.backspace:
                if self.buffer_units:
                    last_unit = self.buffer_units.pop()
                    if len(last_unit) > 1:
                        self.buffer_units.append(last_unit[:-1])
                    self.buffer = self.buffer[:-1]
                    self.backspace_count = max(0, self.backspace_count - 1)
            elif key == keyboard.Key.delete or key in (keyboard.Key.enter, keyboard.Key.tab, keyboard.Key.esc):
                self.clear_buffer()
            elif char and len(char) == 1 and not char.isdigit():
                # Punctuation and symbols end a word, preserving the character if a
                # conversion is made (for example Arabic comma or a closing bracket).
                self._finish_word(char)
            else:
                # Digits, cursor movement, and other special keys break the current word.
                self.clear_buffer()

    def _on_release(self, key):
        if isinstance(key, collections.abc.Hashable):
            with self._state_lock:
                self.modifier_keys.discard(key)

    def clear_buffer(self):
        with self._state_lock:
            self.buffer = ""
            self.buffer_units = []
            self.backspace_count = 0

    def native_character(self, char: str):
        with self._state_lock:
            if not self.enabled or self.busy:
                return
            self.buffer += char
            self.buffer_units.append(char)
            self.backspace_count += len(char)
            self.last_key_time = time.monotonic()

    def native_backspace(self):
        with self._state_lock:
            if not self.enabled or self.busy or not self.buffer_units:
                return
            last_unit = self.buffer_units.pop()
            if len(last_unit) > 1:
                self.buffer_units.append(last_unit[:-1])
            self.buffer = self.buffer[:-1]
            self.backspace_count = max(0, self.backspace_count - 1)

    def finish_native_word(self, boundary: str, hwnd) -> bool:
        """Correct a Windows word inside the low-level hook without blocking on layout switch."""
        with self._state_lock:
            if not self.enabled or self.busy:
                return False
            word = self.buffer
            key_count = self.backspace_count
            self.clear_buffer()
            if not word:
                return False

        replacement, target = self._recognize_word(word)
        if replacement is None:
            return False

        with self._state_lock:
            if not self.enabled or self.busy:
                return False
            self.busy = True
            self.pending_layout_switched = False
            self.pending_layout_switch_requested = True
            self.pending_language = target
            self.pending_hwnd = hwnd

        try:
            queued = self.listener.queue_replacement(key_count, replacement + boundary)
            if not queued:
                with self._state_lock:
                    self.busy = False
                    self.pending_layout_switched = False
                    self.pending_layout_switch_requested = False
                    self.pending_language = None
                    self.pending_hwnd = None
                self.on_status("فشل إدخال النص المصحح")
                return False
            return True
        except Exception as exc:
            with self._state_lock:
                self.busy = False
                self.pending_layout_switched = False
                self.pending_layout_switch_requested = False
                self.pending_language = None
                self.pending_hwnd = None
            self.on_status(f"فشل التصحيح: {exc}")
            return False

    def native_replacement_complete(self, inserted: bool):
        with self._state_lock:
            switch_requested = self.pending_layout_switch_requested
            language = self.pending_language
            hwnd = self.pending_hwnd
            self.pending_layout_switched = False
            self.pending_layout_switch_requested = False
            self.pending_language = None
            self.pending_hwnd = None
            self.busy = False

        if not inserted:
            return

        enqueue = getattr(self.listener, "enqueue_layout_switch", None)
        if enqueue is not None and language is not None:
            enqueue(language, hwnd, switch_requested)
            return

        if not switch_requested or language is None:
            self.on_status("تم تصحيح الكلمة")
            return

        try:
            switched = self.switcher.switch(language, hwnd=hwnd)
        except Exception:
            switched = False
        if switched:
            self.on_status("تم التصحيح — " + ("English" if language == "en" else "العربية"))
        else:
            target_name = "English" if language == "en" else "العربية"
            self.on_status(
                f"تم تصحيح الكلمة، لكن تعذّر التبديل إلى {target_name}؛ سأعيد المحاولة عند كلمة واثقة أخرى"
            )

    def _finish_word(self, boundary: str):
        with self._state_lock:
            word = self.buffer
            count = self.backspace_count
            self.clear_buffer()
            if not word:
                return

        replacement, target = self._recognize_word(word)
        if replacement is None:
            return

        switch_requested = True

        # X11 reports the key after it reaches the target window. Let the boundary
        # land, then replace the word and boundary together.
        with self._state_lock:
            self.busy = True
        threading.Thread(
            target=self._replace_typed_word,
            args=(count + len(boundary), replacement + boundary, target, switch_requested),
            daemon=True,
        ).start()

    @staticmethod
    def _recognize(word: str):
        return recognizer.recognize(word, zipf_fn=zipf_frequency)

    def _replace_typed_word(
        self, key_count: int, replacement: str, target: str, switch_requested: bool
    ):
        with self.lock:
            old_clipboard = None
            clipboard_changed = False
            try:
                time.sleep(0.05)
                controller = keyboard.Controller()
                for _ in range(key_count):
                    controller.press(keyboard.Key.backspace)
                    controller.release(keyboard.Key.backspace)

                pasted = False
                try:
                    old_clipboard = pyperclip.paste()
                    pyperclip.copy(replacement)
                    clipboard_changed = True
                    with controller.pressed(keyboard.Key.ctrl):
                        controller.press("v")
                        controller.release("v")
                    pasted = True
                except Exception as exc:
                    logger.debug("Clipboard unavailable on X11, falling back to direct typing: %s", exc)
                    controller.type(replacement)

                if pasted:
                    time.sleep(0.15)

                # Switch layout AFTER Ctrl+v completes so switching to Arabic never
                # breaks the Ctrl+v paste shortcut.
                try:
                    switched = self.switcher.switch(target) if switch_requested else False
                except Exception:
                    switched = False

                if switched:
                    self.on_status("تم التصحيح — " + ("English" if target == "en" else "العربية"))
                elif not switch_requested:
                    self.on_status("تم تصحيح الكلمة")
                else:
                    language = "English" if target == "en" else "العربية"
                    self.on_status(
                        f"تم تصحيح الكلمة، لكن تعذّر التبديل إلى {language}؛ سأعيد المحاولة عند كلمة واثقة أخرى"
                    )
            except Exception as exc:
                self.on_status(f"فشل التصحيح: {exc}")
            finally:
                if clipboard_changed:
                    try:
                        pyperclip.copy(old_clipboard or "")
                    except Exception:
                        pass
                with self._state_lock:
                    self.busy = False


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Multilang — تبديل تلقائي للغة")
        self.root.geometry("440x260")
        self.root.resizable(False, False)
        self.monitor = TypingMonitor(self._set_status)
        self.monitor.show_window = lambda: self._on_tk(self.show_window)
        self.tray_icon = None
        self.tray_ready = None

        # Pre-warm wordfreq on a background thread so pressing Start stays snappy.
        threading.Thread(
            target=self.monitor._warm_language_model,
            name="MultilangModelWarmup",
            daemon=True,
        ).start()

        tk.Label(root, text="Multilang", font=("Arial", 20, "bold")).pack(pady=(18, 4))
        tk.Label(
            root,
            text=ui_text("يفحص كل كلمة عند المسافة؛ يصحح الكلمة عالية الثقة ويبدّل التخطيط فوراً، ويترك الكلمات الملتبسة كما هي."),
            wraplength=390,
            justify="center",
        ).pack(pady=6)

        self.status = tk.StringVar(value=ui_text("متوقف"))
        tk.Label(root, textvariable=self.status, fg="#176b3a", wraplength=390).pack(pady=8)

        controls = tk.Frame(root)
        controls.pack(pady=5)
        tk.Button(controls, text=ui_text("تشغيل"), width=12, command=self.start_background).pack(side="left", padx=6)
        tk.Button(controls, text=ui_text("إيقاف"), width=12, command=self.stop_monitor).pack(side="left", padx=6)

        tk.Label(
            root,
            text=ui_text("على Wayland يعمل في التطبيقات التي تدعم Fcitx5. بعض التطبيقات لا تسمح للنظام باعتراض الكتابة؛ جرّب جلسة X11 معها."),
            font=("Arial", 9),
            wraplength=390,
            justify="center",
            fg="#555555",
        ).pack(side="bottom", pady=14)

        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def start_background(self):
        self.monitor.start()
        if not self.monitor.enabled:
            return
        try:
            self._ensure_tray()
            self.root.withdraw()
        except Exception as exc:
            self._set_status(f"يعمل في هذه النافذة (تعذّر إنشاء أيقونة الخلفية: {exc})")
            try:
                self.show_window()
            except Exception:
                pass

    def stop_monitor(self):
        self.monitor.stop()
        if self.tray_icon:
            try:
                self.tray_icon.update_menu()
            except Exception:
                pass

    def _ensure_tray(self):
        if self.tray_icon:
            return
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((4, 4, 60, 60), radius=12, fill=(37, 99, 235, 255))
        draw.text((18, 15), "M", fill="white", stroke_width=1)
        menu = pystray.Menu(
            pystray.MenuItem(ui_text("فتح Multilang"), self._tray_show, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda item: ui_text("إيقاف المراقبة" if self.monitor.enabled else "بدء المراقبة"),
                self._tray_toggle,
            ),
            pystray.MenuItem(ui_text("خروج"), self._tray_exit),
        )
        self.tray_icon = pystray.Icon("multilang", image, "Multilang", menu)
        self.tray_ready = threading.Event()

        def tray_setup(icon):
            icon.visible = True
            self.tray_ready.set()

        self.tray_icon.run_detached(setup=tray_setup)
        if not self.tray_ready.wait(timeout=5):
            try:
                self.tray_icon.stop()
            finally:
                self.tray_icon = None
            raise RuntimeError("لم تظهر أيقونة التطبيق في شريط النظام")

    def _tray_show(self, icon=None, item=None):
        self._on_tk(self.show_window)

    def _tray_toggle(self, icon=None, item=None):
        self._on_tk(self._toggle_monitor)

    def _tray_exit(self, icon=None, item=None):
        self._on_tk(self.exit_app)

    def _toggle_monitor(self):
        if self.monitor.enabled:
            self.stop_monitor()
        else:
            self.monitor.start()
            if not self.monitor.enabled:
                self.show_window()
        if self.tray_icon:
            try:
                self.tray_icon.update_menu()
            except Exception:
                pass

    def _on_tk(self, callback):
        try:
            self.root.after(0, callback)
        except tk.TclError:
            pass

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _set_status(self, value):
        try:
            shaped = ui_text(value)
            self.root.after(0, lambda: self.status.set(shaped))
        except tk.TclError:
            pass

    def close(self):
        if self.monitor.enabled:
            try:
                self._ensure_tray()
                self.root.withdraw()
                return
            except Exception:
                try:
                    self.root.iconify()
                    return
                except tk.TclError:
                    pass
        self.exit_app()

    def exit_app(self):
        self.monitor.stop()
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None
        self.root.destroy()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Multilang keyboard layout correction")
    parser.add_argument(
        "--start-in-background",
        action="store_true",
        help="start correction automatically and keep the window in the tray",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="enable privacy-safe diagnostic logging to stderr (never logs typed words)",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="verify language models and recognition checks, then exit",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"Multilang {__version__}",
    )
    return parser


def main_cli(argv: list[str] | None = None) -> int:
    with contextlib.ExitStack() as stack:
        if sys.stdout is None:
            devnull_out = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
            stack.callback(setattr, sys, "stdout", None)
            sys.stdout = devnull_out
        if sys.stderr is None:
            devnull_err = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
            stack.callback(setattr, sys, "stderr", None)
            sys.stderr = devnull_err

        parser = build_arg_parser()
        args = parser.parse_args(argv)

        if args.debug:
            logging.basicConfig(
                level=logging.DEBUG,
                format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            )
            logger.debug("Debug logging enabled for Multilang %s", __version__)

        if args.self_test:
            return run_self_test()

        if platform.system() == "Linux":
            from wayland_backend import show_running_instance

            if show_running_instance():
                return 0
        elif platform.system() == "Windows":
            if not _acquire_windows_single_instance():
                return 0

        root = tk.Tk()
        app = App(root)
        if args.start_in_background:
            root.withdraw()

            def start_after_login():
                app.start_background()
                if not app.monitor.enabled:
                    app.show_window()

            # Give Fcitx and the desktop session time to finish starting.
            root.after(1500, start_after_login)
        root.mainloop()
        return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())

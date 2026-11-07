from __future__ import annotations

import argparse
import ast
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import tkinter as tk
import pystray
from PIL import Image, ImageDraw
from rust_backend import RustKeyboardHook
from windows_backend import WindowsKeyboardHook

try:
    import pyperclip
    from pynput import keyboard
except ImportError as exc:
    raise SystemExit(
        "المكتبات المطلوبة غير مثبّتة. شغّل: python -m pip install -r requirements.txt"
    ) from exc
try:
    from wordfreq import zipf_frequency
except ImportError:
    zipf_frequency = None
try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except ImportError:
    arabic_reshaper = None
    get_display = None


# Arabic PC keyboard, mapped by physical QWERTY key position.
ARABIC_TO_ENGLISH = {
    "ذ": "`", "ض": "q", "ص": "w", "ث": "e", "ق": "r", "ف": "t",
    "غ": "y", "ع": "u", "ه": "i", "خ": "o", "ح": "p", "ج": "[",
    "د": "]", "ش": "a", "س": "s", "ي": "d", "ب": "f", "ل": "g",
    "ا": "h", "ت": "j", "ن": "k", "م": "l", "ك": ";", "ط": "'",
    "ئ": "z", "ء": "x", "ؤ": "c", "ر": "v", "لا": "b", "ى": "n",
    "ة": "m", "و": ",", "ز": ".", "ظ": "/",
}
ENGLISH_TO_ARABIC = {value: key for key, value in ARABIC_TO_ENGLISH.items()}

# Offline vocabulary is deliberately conservative: unknown words are left alone.
ENGLISH_WORDS = set("""
a about after again all also an and any are as at be because been before being best better big but by can come could day did do does down each even every few find first for from get give go good great had has have he her here him his how I if in into is it its just know language last learn let like little long look made make man many may me more most much my name need new no not now of off on one only or other our out over people place please point program python question quick read really right said same say see she should so some take than that the their them then there these they thing think this those time to too two under up use very want was way we well were what when where which who will with word work world would write yes you your hello hi
""".split())

ARABIC_WORDS = set("""
انا نحن انت انتم هو هي هم هذا هذه ذلك تلك هنا هناك من الى عن على في مع بين بعد قبل كل بعض لا ما ماذا كيف متى اين لماذا نعم ليس كان تكون يكون كنت قد سوف لن لم اذا أو ثم ايضا فقط جدا اكثر اقل مثل لان لكن حتى حيث كما الذي التي الذين شيء اشياء شخص الناس انسان بيت منزل باب كتاب قلم مدرسة جامعة عمل وقت يوم ايام سنة شهر ساعة صباح مساء ليل نهار اليوم غدا امس الان دائما ربما ممكن صحيح خطا جيد جميل كبير صغير جديد قديم اول اخر افضل مهم سهلة سهل صعب مشكلة حل فكرة سؤال جواب لغة عربي العربية انجليزي الانجليزية انكليزية الانكليزية للانكليزية يبدل يجب الواجهات اللوحة بشكل الكثير الاخطاء معدل ثقة كلمة كلمات حرف حروف لوحة مفاتيح كتابة تطبيق برنامج نظام ويندوز لينكس مرحبا اهلا وسهلا شكرا عفوا من فضلك بخير الحمد لله السلام عليكم وعليكم انت انا اريد اكتب كتب يكتب كتابة نص رسالة مساعدة اذهب يأتي قال يقول يعرف يفهم يريد يعمل يستخدم يقوم يبدأ ينتهي المستخدم الكمبيوتر الحاسوب شاشة نافذة اعدادات خيارات
""".split())

BOUNDARY_PUNCTUATION = set(".,!?;:")
ENGLISH_WORD_PATTERN = re.compile(r"[a-z]+(?:'[a-z]+)*\Z")
ARABIC_RANGE_START = "\u0600"
ARABIC_RANGE_END = "\u06ff"
ARABIC_LAYOUT_SYMBOLS = set("`[];',./")
MIN_LANGUAGE_SCORE = 4.0
MIN_LANGUAGE_MARGIN = 2.0


def _passes_language_confidence(target_score: float, other_score: float) -> bool:
    return (
        target_score >= MIN_LANGUAGE_SCORE
        and target_score - other_score >= MIN_LANGUAGE_MARGIN
    )


def ui_text(text: str) -> str:
    """Shape and reorder Arabic for Tk builds without native RTL rendering."""
    if arabic_reshaper is None or get_display is None:
        return text
    return get_display(arabic_reshaper.reshape(text), base_dir="R")


def has_arabic(text: str) -> bool:
    return any(ARABIC_RANGE_START <= char <= ARABIC_RANGE_END for char in text)


def to_english(text: str) -> str:
    # "لا" is emitted by one physical key on the standard Arabic PC layout.
    result = []
    i = 0
    while i < len(text):
        if text.startswith("لا", i):
            result.append("b")
            i += 2
        else:
            result.append(ARABIC_TO_ENGLISH.get(text[i], text[i]))
            i += 1
    return "".join(result)


def to_arabic(text: str) -> str:
    return "".join(ENGLISH_TO_ARABIC.get(char.lower(), char) for char in text)


class LayoutSwitcher:
    """Switch the foreground app's input layout on Windows or Linux."""

    def switch(self, language: str, hwnd=None) -> bool:
        if platform.system() == "Windows":
            return self._switch_windows(language, hwnd)
        if platform.system() == "Linux":
            return self._switch_linux(language)
        return False

    @staticmethod
    def activate_fcitx_multilang() -> bool:
        """Select the Multilang engine for the shared Fcitx input state."""
        remote = shutil.which("fcitx5-remote")
        if not remote:
            return False
        try:
            subprocess.run(
                [remote, "-s", "multilang"], check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
            )
            active = subprocess.check_output(
                [remote, "-n"], text=True,
                stderr=subprocess.DEVNULL, timeout=2,
            ).strip()
            return active == "multilang"
        except (OSError, subprocess.SubprocessError):
            return False

    @staticmethod
    def _switch_windows(language: str, hwnd=None) -> bool:
        import ctypes
        from ctypes import wintypes

        layout_id = "00000409" if language == "en" else "00000401"
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.LoadKeyboardLayoutW.restype = wintypes.HKL
        user32.LoadKeyboardLayoutW.argtypes = [wintypes.LPCWSTR, wintypes.UINT]
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, wintypes.LPDWORD]
        user32.GetKeyboardLayout.restype = wintypes.HKL
        user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
        user32.PostMessageW.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        ]
        hwnd = hwnd or user32.GetForegroundWindow()
        if not hwnd:
            return False
        thread_id = user32.GetWindowThreadProcessId(hwnd, None)
        if not thread_id:
            return False

        current = user32.GetKeyboardLayout(thread_id)
        def hkl_value(value):
            if value is None:
                return None
            if isinstance(value, int):
                return value
            return ctypes.cast(value, ctypes.c_void_p).value

        target_langid = int(layout_id[-4:], 16)
        current_value = hkl_value(current)
        if current_value is not None and (current_value & 0xFFFF) == target_langid:
            return True

        # Load the target without activating it, then ask the foreground app.
        layout = user32.LoadKeyboardLayoutW(layout_id, 0)
        if not layout:
            return False

        # Ask the foreground app to switch, then confirm its thread really did.
        if not user32.PostMessageW(hwnd, 0x0050, 0, layout):  # WM_INPUTLANGCHANGEREQUEST
            return False
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            current = user32.GetKeyboardLayout(thread_id)
            current_value = hkl_value(current)
            if current_value is not None and (current_value & 0xFFFF) == target_langid:
                return True
            time.sleep(0.01)
        return False

    @staticmethod
    def _switch_linux(language: str) -> bool:
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
        if "kde" in desktop or "plasma" in desktop:
            return LayoutSwitcher._switch_kde(language)

        # GNOME supports changing the current source while preserving the source list.
        if shutil.which("gsettings"):
            try:
                sources_text = subprocess.check_output(
                    ["gsettings", "get", "org.gnome.desktop.input-sources", "sources"],
                    text=True, stderr=subprocess.DEVNULL, timeout=1.5,
                ).strip()
                sources = ast.literal_eval(sources_text)
                targets = {"en": {"us", "gb"}, "ar": {"ara"}}[language]
                for index, source in enumerate(sources):
                    if len(source) == 2 and source[0] == "xkb" and source[1] in targets:
                        current_text = subprocess.check_output(
                            ["gsettings", "get", "org.gnome.desktop.input-sources", "current"],
                            text=True, stderr=subprocess.DEVNULL, timeout=1.5,
                        ).strip()
                        current_match = re.search(r"(\d+)\s*;?$", current_text)
                        if current_match and int(current_match.group(1)) == index:
                            return True
                        subprocess.run(
                            ["gsettings", "set", "org.gnome.desktop.input-sources", "current", str(index)],
                            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1.5,
                        )
                        observed = subprocess.check_output(
                            ["gsettings", "get", "org.gnome.desktop.input-sources", "current"],
                            text=True, stderr=subprocess.DEVNULL, timeout=1.5,
                        ).strip()
                        match = re.search(r"(\d+)\s*;?$", observed)
                        return bool(match and int(match.group(1)) == index)
            except (OSError, ValueError, SyntaxError, subprocess.SubprocessError):
                pass

        # X11 needs a tool that can report the active group after switching.
        # setxkbmap only configures layouts; it cannot confirm the active group.
        xkb_switch = shutil.which("xkb-switch")
        if xkb_switch and os.environ.get("XDG_SESSION_TYPE", "x11").lower() != "wayland":
            try:
                target = "us" if language == "en" else "ara"
                current = subprocess.check_output(
                    [xkb_switch, "-p"], text=True, stderr=subprocess.DEVNULL, timeout=1.5,
                ).strip()
                if current in ({"us", "gb"} if language == "en" else {"ara"}):
                    return True
                subprocess.run(
                    [xkb_switch, "-s", target], check=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1.5,
                )
                observed = subprocess.check_output(
                    [xkb_switch, "-p"], text=True, stderr=subprocess.DEVNULL, timeout=1.5,
                ).strip()
                return observed in ({"us", "gb"} if language == "en" else {"ara"})
            except (OSError, subprocess.SubprocessError):
                pass
        return False

    @staticmethod
    def _switch_kde(language: str) -> bool:
        gdbus = shutil.which("gdbus")
        if not gdbus:
            return False
        try:
            base = [gdbus, "call", "--session", "--dest", "org.kde.keyboard",
                    "--object-path", "/Layouts", "--method"]
            layouts_text = subprocess.check_output(
                base + ["org.kde.KeyboardLayouts.getLayoutsList"],
                text=True, stderr=subprocess.DEVNULL, timeout=1.5,
            ).strip()
            layout_groups = ast.literal_eval(layouts_text)[0]
            targets = {"en": {"us", "gb"}, "ar": {"ara"}}[language]
            index = next(
                i for i, item in enumerate(layout_groups) if item and item[0] in targets
            )
            def current_index():
                value = subprocess.check_output(
                    base + ["org.kde.KeyboardLayouts.getLayout"],
                    text=True, stderr=subprocess.DEVNULL, timeout=1.5,
                )
                match = re.search(r"uint32\s+(\d+)", value)
                return int(match.group(1)) if match else None

            if current_index() != index:
                result = subprocess.check_output(
                    base + ["org.kde.KeyboardLayouts.setLayout", str(index)],
                    text=True, stderr=subprocess.DEVNULL, timeout=1.5,
                ).strip()
                if not re.search(r"\(\s*true\s*,?\s*\)", result, re.IGNORECASE):
                    return False

                deadline = time.monotonic() + 0.5
                while time.monotonic() < deadline:
                    if current_index() == index:
                        break
                    time.sleep(0.02)
                else:
                    return False

            if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
                return LayoutSwitcher._sync_fcitx_wayland_layout(language)
            return True
        except (OSError, StopIteration, SyntaxError, ValueError, subprocess.SubprocessError):
            return False

    @staticmethod
    def current_kde_language():
        """Return the active Arabic/English KWin layout, if it is configured."""
        gdbus = shutil.which("gdbus")
        if not gdbus:
            return None
        base = [gdbus, "call", "--session", "--dest", "org.kde.keyboard",
                "--object-path", "/Layouts", "--method"]
        try:
            layouts_text = subprocess.check_output(
                base + ["org.kde.KeyboardLayouts.getLayoutsList"],
                text=True, stderr=subprocess.DEVNULL, timeout=1.5,
            )
            layout_groups = ast.literal_eval(layouts_text.strip())[0]
            current_text = subprocess.check_output(
                base + ["org.kde.KeyboardLayouts.getLayout"],
                text=True, stderr=subprocess.DEVNULL, timeout=1.5,
            )
            match = re.search(r"uint32\s+(\d+)", current_text)
            if not match:
                return None
            index = int(match.group(1))
            if index >= len(layout_groups):
                return None
            layout = layout_groups[index][0]
            if layout == "ara":
                return "ar"
            if layout in {"us", "gb"}:
                return "en"
        except (OSError, IndexError, SyntaxError, TypeError, ValueError,
                subprocess.SubprocessError):
            return None
        return None

    @staticmethod
    def _sync_fcitx_wayland_layout(language: str) -> bool:
        """Refresh Fcitx's own keymap after KWin changes the Wayland layout."""
        gdbus = shutil.which("gdbus")
        remote = shutil.which("fcitx5-remote")
        if not gdbus or not remote:
            return False

        base = [gdbus, "call", "--session", "--dest", "org.fcitx.Fcitx5",
                "--object-path", "/controller", "--method"]
        try:
            group_text = subprocess.check_output(
                base + ["org.fcitx.Fcitx.Controller1.CurrentInputMethodGroup"],
                text=True, stderr=subprocess.DEVNULL, timeout=1.5,
            )
            group_value = ast.literal_eval(group_text.strip())
            group_name = group_value[0] if isinstance(group_value, tuple) else group_value

            info_method = "org.fcitx.Fcitx.Controller1.InputMethodGroupInfo"
            info_text = subprocess.check_output(
                base + [info_method, group_name],
                text=True, stderr=subprocess.DEVNULL, timeout=1.5,
            )
            _current_layout, items = ast.literal_eval(info_text.strip())
            if not any(name == "multilang" for name, _item_layout in items):
                return False

            desired_layout = "us" if language == "en" else "ara"
            keyboard_ims = [name for name, _item_layout in items
                            if name.startswith("keyboard-")]
            if not keyboard_ims:
                return False
            preferred = ("keyboard-us", "keyboard-gb", "keyboard-uk") if language == "en" else (
                "keyboard-ara", "keyboard-sy"
            )
            temporary_im = next(
                (name for name in preferred if name in keyboard_ims), keyboard_ims[0]
            )
            items_arg = "[" + ", ".join(
                f"({name!r}, {item_layout!r})" for name, item_layout in items
            ) + "]"
            subprocess.check_output(
                base + ["org.fcitx.Fcitx.Controller1.SetInputMethodGroupInfo",
                        group_name, desired_layout, items_arg],
                text=True, stderr=subprocess.DEVNULL, timeout=1.5,
            )

            # A profile edit alone leaves existing Wayland input contexts on
            # their previous keymap. Reload, then re-activate Multilang.
            subprocess.run(
                [remote, "-r"], check=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=2,
            )
            subprocess.run(
                [remote, "-s", temporary_im], check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
            )
            subprocess.run(
                [remote, "-s", "multilang"], check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2,
            )
            active_im = subprocess.check_output(
                [remote, "-n"], text=True, stderr=subprocess.DEVNULL, timeout=1.5,
            ).strip()
            if active_im != "multilang":
                return False

            verified_text = subprocess.check_output(
                base + [info_method, group_name],
                text=True, stderr=subprocess.DEVNULL, timeout=1.5,
            )
            verified_layout, _verified_items = ast.literal_eval(verified_text.strip())
            return verified_layout == desired_layout
        except (OSError, IndexError, SyntaxError, TypeError, ValueError,
                subprocess.SubprocessError):
            return False


class TypingMonitor:
    def __init__(self, on_status):
        self.on_status = on_status
        self.switcher = LayoutSwitcher()
        self.listener = None
        self.buffer = ""
        self.buffer_units = []
        self.backspace_count = 0
        self.enabled = False
        self.busy = False
        self.modifier_keys = set()
        self.pending_layout_switched = False
        self.pending_layout_switch_requested = False
        self.pending_language = None
        self.layout_switch_lock = threading.Lock()
        self.wayland_server = None
        self.lock = threading.Lock()

    def start(self):
        if self.enabled:
            return
        self.buffer = ""
        self.buffer_units = []
        self.backspace_count = 0
        if platform.system() == "Linux" and os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
            try:
                # Wayland does not expose a process-wide keyboard hook. The Fcitx5
                # input method sends word candidates to this local service instead.
                from wayland_backend import WaylandCorrectionServer

                if not self.switcher.activate_fcitx_multilang():
                    raise RuntimeError(
                        "تعذّر تفعيل Multilang في Fcitx5. تأكد من تثبيت الإضافة وإضافتها إلى الملف الشخصي."
                    )
                self._warm_language_model()
                self.wayland_server = WaylandCorrectionServer(self)
                self.wayland_server.start()
                self.enabled = True
                self.on_status("خدمة Wayland جاهزة — Multilang مفعّل في Fcitx5")
            except Exception as exc:
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
                except (FileNotFoundError, OSError, RuntimeError):
                    # Keep source checkouts usable before the optional native DLL
                    # is built. The Windows test bundle ships the Rust library.
                    self.listener = WindowsKeyboardHook(self)
                    self.listener.start()
            else:
                self.listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
                self.listener.start()
            self.on_status("يعمل — التصحيح بعد المسافة")
        except Exception as exc:
            self.enabled = False
            self.on_status(f"تعذّر تشغيل مراقبة لوحة المفاتيح: {exc}")

    @staticmethod
    def _warm_language_model():
        if zipf_frequency is not None:
            zipf_frequency("hello", "en")
            zipf_frequency("مرحبا", "ar")

    def stop(self):
        self.enabled = False
        self.buffer = ""
        self.buffer_units = []
        self.backspace_count = 0
        self.pending_layout_switched = False
        self.pending_layout_switch_requested = False
        self.pending_language = None
        if self.wayland_server is not None:
            try:
                self.wayland_server.stop()
            finally:
                self.wayland_server = None
        if self.listener is not None:
            self.listener.stop()
            self.listener = None
        self.on_status("متوقف")

    def correct_wayland_word(self, word: str):
        """Return a correction for the Fcitx5 adapter and switch KDE's layout."""
        if not self.enabled or not word:
            return None
        replacement, target = self._recognize_word(word)
        if replacement is None:
            return None
        # Wayland asks KWin to switch after correcting this word. Keep the
        # readback off Fcitx's key-event thread so typing stays responsive.
        threading.Thread(
            target=self._switch_wayland_layout,
            args=(target,),
            name="MultilangWaylandLayout",
            daemon=True,
        ).start()
        return replacement, target, None

    def current_wayland_language(self):
        if not self.enabled or platform.system() != "Linux":
            return None
        if os.environ.get("XDG_SESSION_TYPE", "").lower() != "wayland":
            return None
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
        if "kde" not in desktop and "plasma" not in desktop:
            return None
        return self.switcher.current_kde_language()

    def _switch_wayland_layout(self, target: str):
        try:
            with self.layout_switch_lock:
                switched = self.switcher.switch(target)
        except Exception:
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

    def _on_press(self, key):
        if not self.enabled or self.busy:
            return
        modifier_keys = {
            keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r,
            keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r,
            keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r,
        }
        if key in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r):
            return
        if key in modifier_keys:
            self.buffer = ""
            self.buffer_units = []
            self.backspace_count = 0
            self.modifier_keys.add(key)
            return
        if self.modifier_keys:
            return
        try:
            char = key.char
        except AttributeError:
            char = None

        if key == keyboard.Key.space or char == " ":
            self._finish_word(" ")
        elif char and len(char) == 1 and char in BOUNDARY_PUNCTUATION and char not in ARABIC_LAYOUT_SYMBOLS:
            self._finish_word(char)
        elif char and all(
            c.isalpha() or c.isdigit() or c in "'’-" or c in ARABIC_LAYOUT_SYMBOLS
            for c in char
        ):
            self.buffer += char
            self.buffer_units.append(char)
            self.backspace_count += 1
        elif key in (keyboard.Key.backspace, keyboard.Key.delete):
            if key == keyboard.Key.backspace:
                if self.buffer_units:
                    last_unit = self.buffer_units.pop()
                    self.buffer = self.buffer[:-len(last_unit)]
                    self.backspace_count = max(0, self.backspace_count - 1)
            else:
                self.buffer = ""
                self.buffer_units = []
                self.backspace_count = 0
        elif key in (keyboard.Key.enter, keyboard.Key.tab, keyboard.Key.esc):
            self.buffer = ""
            self.buffer_units = []
            self.backspace_count = 0
        elif char and len(char) == 1:
            # Punctuation and symbols end a word, preserving the character if a
            # conversion is made (for example Arabic comma or a closing bracket).
            self._finish_word(char)
        elif char is None:
            # Cursor movement and other special keys break the current word.
            self.buffer = ""
            self.buffer_units = []
            self.backspace_count = 0

    def _on_release(self, key):
        modifier_keys = {
            keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r,
            keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r,
            keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r,
        }
        self.modifier_keys.discard(key)

    def clear_buffer(self):
        self.buffer = ""
        self.buffer_units = []
        self.backspace_count = 0

    def native_character(self, char: str):
        if not self.enabled or self.busy:
            return
        self.buffer += char
        self.buffer_units.append(char)
        self.backspace_count += 1

    def native_backspace(self):
        if not self.enabled or self.busy or not self.buffer_units:
            return
        last_unit = self.buffer_units.pop()
        self.buffer = self.buffer[:-len(last_unit)]
        self.backspace_count = max(0, self.backspace_count - 1)

    def finish_native_word(self, boundary: str, hwnd) -> bool:
        """Correct a Windows word inside the low-level hook, before its boundary is sent."""
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

        self.busy = True
        try:
            # A recognized correction already passed the language score and
            # margin checks, so switch after this word without extra evidence.
            switch_requested = True
            try:
                switched = self.switcher.switch(target, hwnd=hwnd)
            except Exception:
                switched = False
            self.pending_layout_switched = switched
            self.pending_layout_switch_requested = switch_requested
            self.pending_language = target
            queued = self.listener.queue_replacement(key_count, replacement + boundary)
            if not queued:
                self.busy = False
                self.pending_layout_switched = False
                self.pending_layout_switch_requested = False
                self.pending_language = None
                self.on_status("فشل إدخال النص المصحح")
                return False
            return True
        except Exception as exc:
            self.busy = False
            self.pending_layout_switched = False
            self.pending_layout_switch_requested = False
            self.pending_language = None
            self.on_status(f"فشل التصحيح: {exc}")
            return False

    def native_replacement_complete(self, inserted: bool):
        switched = self.pending_layout_switched
        switch_requested = self.pending_layout_switch_requested
        language = self.pending_language
        self.pending_layout_switched = False
        self.pending_layout_switch_requested = False
        self.pending_language = None
        self.busy = False
        if not inserted:
            return
        if switched:
            self.on_status("تم التصحيح — " + ("English" if language == "en" else "العربية"))
        elif not switch_requested:
            self.on_status("تم تصحيح الكلمة")
        else:
            target_name = "English" if language == "en" else "العربية"
            self.on_status(
                f"تم تصحيح الكلمة، لكن تعذّر التبديل إلى {target_name}؛ سأعيد المحاولة عند كلمة واثقة أخرى"
            )

    def _finish_word(self, boundary: str):
        word = self.buffer
        count = self.backspace_count
        self.buffer = ""
        self.buffer_units = []
        self.backspace_count = 0
        if not word:
            return

        replacement, target = self._recognize_word(word)
        if replacement is None:
            return

        # The correction decision already met the high-confidence threshold.
        switch_requested = True

        # X11 reports the key after it reaches the target window. Let the boundary
        # land, then replace the word and boundary together. Windows uses a native
        # hook and suppresses the boundary in the same input transaction.
        self.busy = True
        threading.Thread(
            target=self._replace_typed_word,
            args=(count + 1, replacement + boundary, target, switch_requested),
            daemon=True,
        ).start()

    @staticmethod
    def _recognize(word: str):
        if len(word) < 2 or zipf_frequency is None:
            return None, None
        normalized = word.casefold()

        # Several Arabic letters live on punctuation keys in the Arabic PC
        # layout (for example ',' -> و and ';' -> ك). Keep those keys inside
        # the candidate and score the converted Arabic word as a whole.
        has_layout_key = any(char in ENGLISH_TO_ARABIC for char in word)
        if has_arabic(word) and has_layout_key:
            candidate = to_arabic(word)
            if candidate != word:
                arabic_score = zipf_frequency(candidate, "ar")
                english_score = (
                    0.0 if any(char in ARABIC_LAYOUT_SYMBOLS for char in word)
                    else zipf_frequency(to_english(word).casefold(), "en")
                )
                if _passes_language_confidence(arabic_score, english_score):
                    return candidate, "ar"

        if has_arabic(word):
            if normalized in ARABIC_WORDS:
                return None, None
            candidate = to_english(word).casefold()
            # Wordfreq normalizes punctuation away, so a candidate such as
            # `,a,` (Arabic "وشو" on the physical layout) can otherwise score
            # like a common English token. Only score plausible English words.
            if not ENGLISH_WORD_PATTERN.fullmatch(candidate):
                return None, None
            english_score = zipf_frequency(candidate, "en")
            arabic_score = zipf_frequency(normalized, "ar")
            if _passes_language_confidence(english_score, arabic_score):
                if word.isupper():
                    candidate = candidate.upper()
                elif word.istitle():
                    candidate = candidate.title()
                return candidate, "en"
        elif word.isascii() and word and all(char in ENGLISH_TO_ARABIC for char in word):
            if normalized in ENGLISH_WORDS:
                return None, None
            candidate = to_arabic(word)
            has_arabic_key_symbols = any(char in ARABIC_LAYOUT_SYMBOLS for char in word)
            # Punctuation keys such as ';' and '[' produce Arabic letters in
            # Arabic PC layouts. Their raw strings are not meaningful input
            # to an English frequency model, so ignore that score only when
            # the physical-key sequence contains one of those symbols.
            english_score = (
                0.0 if has_arabic_key_symbols
                else zipf_frequency(normalized, "en")
            )
            arabic_score = zipf_frequency(candidate, "ar")
            if _passes_language_confidence(arabic_score, english_score):
                return candidate, "ar"
        return None, None

    def _replace_typed_word(
        self, key_count: int, replacement: str, target: str, switch_requested: bool
    ):
        with self.lock:
            self.busy = True
            old_clipboard = None
            clipboard_changed = False
            try:
                time.sleep(0.08)
                try:
                    switched = self.switcher.switch(target) if switch_requested else False
                except Exception:
                    switched = False
                old_clipboard = pyperclip.paste()
                pyperclip.copy(replacement)
                clipboard_changed = True

                controller = keyboard.Controller()
                for _ in range(key_count):
                    controller.press(keyboard.Key.backspace)
                    controller.release(keyboard.Key.backspace)
                with controller.pressed(keyboard.Key.ctrl):
                    controller.press("v")
                    controller.release("v")
                time.sleep(0.4)

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
                self.busy = False


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(ui_text("Multilang — تبديل تلقائي للغة"))
        self.root.geometry("440x260")
        self.root.resizable(False, False)
        self.monitor = TypingMonitor(self._set_status)
        self.monitor.show_window = lambda: self._on_tk(self.show_window)
        self.tray_icon = None
        self.tray_ready = None

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
            font=("Arial", 9), wraplength=390, justify="center", fg="#555555",
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
            self.monitor.stop()
            self._set_status(f"تعذّر إنشاء أيقونة الخلفية: {exc}")

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
            pystray.MenuItem("Open Multilang", self._tray_show, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda item: "Stop monitoring" if self.monitor.enabled else "Start monitoring",
                self._tray_toggle,
            ),
            pystray.MenuItem("Exit", self._tray_exit),
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
            except Exception as exc:
                self._set_status(f"تعذّر الانتقال إلى الخلفية: {exc}")
                return
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multilang keyboard layout correction")
    parser.add_argument(
        "--start-in-background",
        action="store_true",
        help="start correction automatically and keep the window in the tray",
    )
    args = parser.parse_args()

    if (
        platform.system() == "Linux"
        and os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
    ):
        from wayland_backend import show_running_instance

        if show_running_instance():
            raise SystemExit(0)

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

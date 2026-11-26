"""Cross-platform keyboard layout switching for Windows, KDE, GNOME, and X11."""

from __future__ import annotations

import ast
import ctypes
import logging
import os
import platform
import re
import shutil
import subprocess
import time

logger = logging.getLogger("multilang.layout")

PRIMARYLANGID_ARABIC = 0x01
PRIMARYLANGID_ENGLISH = 0x09

ENGLISH_XKB_LAYOUTS = {"us", "gb", "uk", "ca", "au"}
ARABIC_XKB_LAYOUTS = {
    "ara", "sy", "eg", "iq", "ma", "dz", "tn", "jo", "lb", "kw",
    "sa", "ae", "qa", "bh", "om", "ye", "ly", "sd", "ps",
}


def _layout_matches(layout_name: str, language: str) -> bool:
    """Match an XKB/Fcitx layout name or variant (e.g. 'ara(digits)', 'ara-digits') to 'en' or 'ar'."""
    if not layout_name:
        return False
    base = re.split(r"[(-]", layout_name, maxsplit=1)[0].strip().lower()
    targets = ENGLISH_XKB_LAYOUTS if language == "en" else ARABIC_XKB_LAYOUTS
    return base in targets


def _xkb_group_name_matches(group_name: str, language: str) -> bool:
    lowered = group_name.strip().lower()
    if language == "ar":
        return "arabic" in lowered or _layout_matches(lowered, "ar")
    return "english" in lowered or lowered.startswith("us") or _layout_matches(lowered, "en")


_GVARIANT_TOKEN_RE = re.compile(
    r"'(?:\\.|[^'\\])*'"
    r'|"(?:\\.|[^"\\])*"'
    r"|@[a-zA-Z0-9(){}_\[\]]+\s*"
    r"|\b(?:u?int(?:16|32|64)|byte|double)\s+(?=[-+]?(?:0x[0-9a-fA-F]+|\d))"
    r"|\b(true|false)\b"
)


def _parse_gvariant_literal(text: str):
    """Parse gdbus/gsettings GVariant text output safely via ast.literal_eval."""

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token[0] in ("'", '"'):
            return token
        boolean = match.group(1)
        if boolean is not None:
            return "True" if boolean == "true" else "False"
        return ""

    normalized = _GVARIANT_TOKEN_RE.sub(_replace, text.strip())
    return ast.literal_eval(normalized)


class LayoutSwitcher:
    """Switch the foreground app's input layout on Windows or Linux."""

    def switch(self, language: str, hwnd=None) -> bool:
        system = platform.system()
        if system == "Windows":
            return self._switch_windows(language, hwnd)
        if system == "Linux":
            return self._switch_linux(language)
        return False

    @staticmethod
    def activate_fcitx_multilang() -> bool:
        """Verify that Fcitx5 is running and the Multilang module addon is enabled."""
        remote = shutil.which("fcitx5-remote")
        if not remote:
            return False
        try:
            subprocess.run(
                [remote, "--check"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("fcitx5-remote --check failed: %s", exc)
            return False

        gdbus = shutil.which("gdbus")
        if gdbus:
            try:
                addons_text = subprocess.check_output(
                    [
                        gdbus, "call", "--session", "--dest", "org.fcitx.Fcitx5",
                        "--object-path", "/controller",
                        "--method", "org.fcitx.Fcitx.Controller1.GetAddons",
                    ],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                ).strip()
                addons = _parse_gvariant_literal(addons_text)[0]
                return any(
                    item and item[0] == "multilang" and (len(item) < 6 or bool(item[5]))
                    for item in addons
                )
            except (OSError, IndexError, SyntaxError, TypeError, ValueError, subprocess.SubprocessError) as exc:
                logger.debug("GetAddons check failed: %s", exc)
                return False
        return True

    @staticmethod
    def current_fcitx_language():
        """Return 'en' or 'ar' for the active Fcitx5 keyboard-* input method."""
        remote = shutil.which("fcitx5-remote")
        if not remote:
            return None
        try:
            active = subprocess.check_output(
                [remote, "-n"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=1.5,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            return None
        if not active.startswith("keyboard-"):
            return None
        layout = active[len("keyboard-"):]
        if _layout_matches(layout, "ar"):
            return "ar"
        if _layout_matches(layout, "en"):
            return "en"
        return None

    @staticmethod
    def _switch_fcitx(language: str) -> bool:
        """Switch directly between the user's Arabic and English Fcitx5 keyboard IMs."""
        gdbus = shutil.which("gdbus")
        remote = shutil.which("fcitx5-remote")
        if not gdbus or not remote:
            return False
        base = [
            gdbus, "call", "--session", "--dest", "org.fcitx.Fcitx5",
            "--object-path", "/controller", "--method",
        ]
        try:
            group_text = subprocess.check_output(
                base + ["org.fcitx.Fcitx.Controller1.CurrentInputMethodGroup"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=1.5,
            ).strip()
            group_val = _parse_gvariant_literal(group_text)
            group_name = group_val[0] if isinstance(group_val, tuple) else group_val

            info_text = subprocess.check_output(
                base + ["org.fcitx.Fcitx.Controller1.InputMethodGroupInfo", group_name],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=1.5,
            ).strip()
            _default_layout, items = _parse_gvariant_literal(info_text)
            target_im = next(
                (
                    name for name, _layout in items
                    if name.startswith("keyboard-")
                    and _layout_matches(name[len("keyboard-"):], language)
                ),
                None,
            )
            if not target_im:
                return False

            current = subprocess.check_output(
                [remote, "-n"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=1.5,
            ).strip()
            if current == target_im:
                return True

            subprocess.run(
                [remote, "-s", target_im],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1.5,
            )
            observed = subprocess.check_output(
                [remote, "-n"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=1.5,
            ).strip()
            return observed == target_im
        except (OSError, IndexError, SyntaxError, TypeError, ValueError, subprocess.SubprocessError) as exc:
            logger.debug("Fcitx5 IM switch failed: %s", exc)
            return False

    @staticmethod
    def _switch_windows(language: str, hwnd=None) -> bool:
        from ctypes import wintypes

        class GUITHREADINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND),
                ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND),
                ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT),
            ]

        def hkl_value(value):
            if value is None:
                return None
            if isinstance(value, int):
                return value
            return ctypes.cast(value, ctypes.c_void_p).value

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.LoadKeyboardLayoutW.restype = wintypes.HKL
        user32.LoadKeyboardLayoutW.argtypes = [wintypes.LPCWSTR, wintypes.UINT]
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, wintypes.LPDWORD]
        user32.GetKeyboardLayout.restype = wintypes.HKL
        user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
        user32.GetKeyboardLayoutList.restype = ctypes.c_int
        user32.GetKeyboardLayoutList.argtypes = [ctypes.c_int, ctypes.POINTER(wintypes.HKL)]
        user32.GetGUIThreadInfo.restype = wintypes.BOOL
        user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
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

        target_primary = PRIMARYLANGID_ENGLISH if language == "en" else PRIMARYLANGID_ARABIC

        def matches_target(hkl_val) -> bool:
            return hkl_val is not None and (hkl_val & 0x03FF) == target_primary

        current_value = hkl_value(user32.GetKeyboardLayout(thread_id))
        if matches_target(current_value):
            return True

        # Prefer an installed HKL matching the target PRIMARYLANGID so regional
        # Arabic/English variants (Egypt, Syria, Jordan, UK, etc.) are preserved.
        target_layout = None
        count = user32.GetKeyboardLayoutList(0, None)
        if count > 0:
            hkls = (wintypes.HKL * count)()
            actual = user32.GetKeyboardLayoutList(count, hkls)
            for idx in range(min(count, actual)):
                val = hkl_value(hkls[idx])
                if matches_target(val):
                    target_layout = hkls[idx]
                    break

        if not target_layout:
            fallback_id = "00000409" if language == "en" else "00000401"
            target_layout = user32.LoadKeyboardLayoutW(fallback_id, 0)
        if not target_layout:
            return False

        target_hwnd = hwnd
        gui = GUITHREADINFO()
        gui.cbSize = ctypes.sizeof(GUITHREADINFO)
        if user32.GetGUIThreadInfo(thread_id, ctypes.byref(gui)) and gui.hwndFocus:
            target_hwnd = gui.hwndFocus

        if not user32.PostMessageW(target_hwnd, 0x0050, 0, target_layout):
            if target_hwnd == hwnd or not user32.PostMessageW(hwnd, 0x0050, 0, target_layout):
                return False

        posted_fallback = target_hwnd == hwnd
        start = time.monotonic()
        deadline = start + 0.5
        while time.monotonic() < deadline:
            current_value = hkl_value(user32.GetKeyboardLayout(thread_id))
            if matches_target(current_value):
                return True
            if not posted_fallback and time.monotonic() - start >= 0.15:
                user32.PostMessageW(hwnd, 0x0050, 0, target_layout)
                posted_fallback = True
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
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=1.5,
                ).strip()
                sources = _parse_gvariant_literal(sources_text)
                for index, source in enumerate(sources):
                    if len(source) == 2 and source[0] == "xkb" and _layout_matches(source[1], language):
                        current_text = subprocess.check_output(
                            ["gsettings", "get", "org.gnome.desktop.input-sources", "current"],
                            text=True,
                            stderr=subprocess.DEVNULL,
                            timeout=1.5,
                        ).strip()
                        current_match = re.search(r"(\d+)\s*;?$", current_text)
                        if current_match and int(current_match.group(1)) == index:
                            return True
                        subprocess.run(
                            ["gsettings", "set", "org.gnome.desktop.input-sources", "current", str(index)],
                            check=True,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=1.5,
                        )
                        observed = subprocess.check_output(
                            ["gsettings", "get", "org.gnome.desktop.input-sources", "current"],
                            text=True,
                            stderr=subprocess.DEVNULL,
                            timeout=1.5,
                        ).strip()
                        match = re.search(r"(\d+)\s*;?$", observed)
                        return bool(match and int(match.group(1)) == index)
            except (OSError, ValueError, SyntaxError, subprocess.SubprocessError) as exc:
                logger.debug("GNOME layout switch failed: %s", exc)

        # On X11, prefer direct libX11 XKB switching before falling back to xkb-switch.
        if os.environ.get("XDG_SESSION_TYPE", "x11").lower() != "wayland":
            if LayoutSwitcher._switch_x11_xkb(language):
                return True

        xkb_switch = shutil.which("xkb-switch")
        if xkb_switch and os.environ.get("XDG_SESSION_TYPE", "x11").lower() != "wayland":
            try:
                target = "us" if language == "en" else "ara"
                current = subprocess.check_output(
                    [xkb_switch, "-p"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=1.5,
                ).strip()
                if _layout_matches(current, language):
                    return True
                subprocess.run(
                    [xkb_switch, "-s", target],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=1.5,
                )
                observed = subprocess.check_output(
                    [xkb_switch, "-p"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=1.5,
                ).strip()
                return _layout_matches(observed, language)
            except (OSError, subprocess.SubprocessError) as exc:
                logger.debug("xkb-switch failed: %s", exc)
        return False

    @staticmethod
    def _switch_x11_xkb(language: str) -> bool:
        """Switch X11 keyboard group directly via libX11 XKB without external binaries."""
        try:
            x11 = ctypes.CDLL("libX11.so.6")
        except OSError:
            return False

        XKB_USE_CORE_KBD = 0x0100
        XKB_GROUP_NAMES_MASK = 1 << 12
        XKB_ALL_COMPONENTS_MASK = 0x7F

        class XkbStateRec(ctypes.Structure):
            _fields_ = [
                ("group", ctypes.c_ubyte),
                ("locked_group", ctypes.c_ubyte),
                ("base_group", ctypes.c_ushort),
                ("latched_group", ctypes.c_ushort),
                ("mods", ctypes.c_ubyte),
                ("base_mods", ctypes.c_ubyte),
                ("latched_mods", ctypes.c_ubyte),
                ("locked_mods", ctypes.c_ubyte),
                ("compat_state", ctypes.c_ubyte),
                ("grab_mods", ctypes.c_ubyte),
                ("compat_grab_mods", ctypes.c_ubyte),
                ("lookup_mods", ctypes.c_ubyte),
                ("compat_lookup_mods", ctypes.c_ubyte),
                ("ptr_buttons", ctypes.c_ushort),
            ]

        class XkbNamesRec(ctypes.Structure):
            _fields_ = [
                ("keycodes", ctypes.c_ulong),
                ("geometry", ctypes.c_ulong),
                ("symbols", ctypes.c_ulong),
                ("types", ctypes.c_ulong),
                ("compat", ctypes.c_ulong),
                ("vmods", ctypes.c_ulong * 16),
                ("indicators", ctypes.c_ulong * 32),
                ("groups", ctypes.c_ulong * 4),
            ]

        class XkbDescRec(ctypes.Structure):
            _fields_ = [
                ("dpy", ctypes.c_void_p),
                ("flags", ctypes.c_ushort),
                ("device_spec", ctypes.c_ushort),
                ("min_key_code", ctypes.c_ubyte),
                ("max_key_code", ctypes.c_ubyte),
                ("ctrls", ctypes.c_void_p),
                ("server", ctypes.c_void_p),
                ("map", ctypes.c_void_p),
                ("indicators", ctypes.c_void_p),
                ("names", ctypes.POINTER(XkbNamesRec)),
                ("compat", ctypes.c_void_p),
                ("geom", ctypes.c_void_p),
            ]

        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XCloseDisplay.restype = ctypes.c_int
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x11.XFlush.restype = ctypes.c_int
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XFree.restype = ctypes.c_int
        x11.XFree.argtypes = [ctypes.c_void_p]
        x11.XGetAtomName.restype = ctypes.c_void_p
        x11.XGetAtomName.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x11.XkbGetKeyboard.restype = ctypes.POINTER(XkbDescRec)
        x11.XkbGetKeyboard.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
        x11.XkbFreeKeyboard.restype = None
        x11.XkbFreeKeyboard.argtypes = [ctypes.POINTER(XkbDescRec), ctypes.c_uint, ctypes.c_int]
        x11.XkbGetState.restype = ctypes.c_int
        x11.XkbGetState.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(XkbStateRec)]
        x11.XkbLockGroup.restype = ctypes.c_int
        x11.XkbLockGroup.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]

        display = x11.XOpenDisplay(None)
        if not display:
            return False
        xkb_desc = None
        try:
            xkb_desc = x11.XkbGetKeyboard(display, XKB_GROUP_NAMES_MASK, XKB_USE_CORE_KBD)
            if not xkb_desc or not xkb_desc.contents.names:
                return False
            names = xkb_desc.contents.names.contents
            target_index = None
            for idx in range(4):
                atom = names.groups[idx]
                if not atom:
                    continue
                ptr = x11.XGetAtomName(display, atom)
                if not ptr:
                    continue
                try:
                    group_name = ctypes.cast(ptr, ctypes.c_char_p).value.decode("utf-8", "replace")
                finally:
                    x11.XFree(ptr)
                if _xkb_group_name_matches(group_name, language):
                    target_index = idx
                    break
            if target_index is None:
                return False

            state = XkbStateRec()
            if x11.XkbGetState(display, XKB_USE_CORE_KBD, ctypes.byref(state)) == 0:
                if state.locked_group == target_index:
                    return True

            if not x11.XkbLockGroup(display, XKB_USE_CORE_KBD, target_index):
                return False
            x11.XFlush(display)

            if x11.XkbGetState(display, XKB_USE_CORE_KBD, ctypes.byref(state)) == 0:
                return state.locked_group == target_index
            return True
        except Exception as exc:
            logger.debug("libX11 XKB switch failed: %s", exc)
            return False
        finally:
            if xkb_desc:
                x11.XkbFreeKeyboard(xkb_desc, XKB_ALL_COMPONENTS_MASK, 1)
            x11.XCloseDisplay(display)

    @staticmethod
    def _switch_kde(language: str) -> bool:
        if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
            if LayoutSwitcher._switch_fcitx(language):
                return True
        gdbus = shutil.which("gdbus")
        if not gdbus:
            return False
        try:
            base = [
                gdbus, "call", "--session", "--dest", "org.kde.keyboard",
                "--object-path", "/Layouts", "--method",
            ]
            layouts_text = subprocess.check_output(
                base + ["org.kde.KeyboardLayouts.getLayoutsList"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=1.5,
            ).strip()
            layout_groups = _parse_gvariant_literal(layouts_text)[0]
            index = next(
                i for i, item in enumerate(layout_groups)
                if item and _layout_matches(item[0], language)
            )

            def current_index():
                value = subprocess.check_output(
                    base + ["org.kde.KeyboardLayouts.getLayout"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=1.5,
                )
                match = re.search(r"uint32\s+(\d+)", value)
                return int(match.group(1)) if match else None

            if current_index() == index:
                return True

            result = subprocess.check_output(
                base + ["org.kde.KeyboardLayouts.setLayout", str(index)],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=1.5,
            ).strip()
            if not re.search(r"\(\s*true\s*,?\s*\)", result, re.IGNORECASE):
                return False

            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                if current_index() == index:
                    return True
                time.sleep(0.02)
            return False
        except (OSError, StopIteration, SyntaxError, ValueError, subprocess.SubprocessError) as exc:
            logger.debug("KDE layout switch failed: %s", exc)
            return False

    @staticmethod
    def current_kde_language():
        """Return the active Arabic/English KWin layout, if it is configured."""
        gdbus = shutil.which("gdbus")
        if not gdbus:
            return None
        base = [
            gdbus, "call", "--session", "--dest", "org.kde.keyboard",
            "--object-path", "/Layouts", "--method",
        ]
        try:
            layouts_text = subprocess.check_output(
                base + ["org.kde.KeyboardLayouts.getLayoutsList"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=1.5,
            )
            layout_groups = _parse_gvariant_literal(layouts_text)[0]
            current_text = subprocess.check_output(
                base + ["org.kde.KeyboardLayouts.getLayout"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=1.5,
            )
            match = re.search(r"uint32\s+(\d+)", current_text)
            if not match:
                return None
            index = int(match.group(1))
            if index >= len(layout_groups) or not layout_groups[index]:
                return None
            layout = layout_groups[index][0]
            if _layout_matches(layout, "ar"):
                return "ar"
            if _layout_matches(layout, "en"):
                return "en"
        except (OSError, IndexError, SyntaxError, TypeError, ValueError, subprocess.SubprocessError):
            return None
        return None

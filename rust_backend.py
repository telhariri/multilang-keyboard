"""Python bridge to the compiled Rust Windows keyboard hook."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path


_CORRECTION = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_ubyte),
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_ubyte),
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
    ctypes.POINTER(ctypes.c_uint32),
)
_STATUS = ctypes.CFUNCTYPE(None, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t)


def _library_candidates() -> list[Path]:
    root = Path(__file__).resolve().parent
    if os.name == "nt":
        filename = "multilang_native.dll"
    elif os.name == "posix":
        filename = "libmultilang_native.so"
    else:
        filename = "multilang_native"
    candidates = []
    configured = os.environ.get("MULTILANG_NATIVE_LIBRARY")
    if configured:
        candidates.append(Path(configured))
    candidates.extend((
        root / "native" / filename,
        root / "native" / "target" / "release" / filename,
        root / "native" / "target" / "x86_64-pc-windows-gnu" / "release" / filename,
    ))
    return candidates


class RustKeyboardHook:
    """Own the Rust library and keep its Python callbacks alive while hooked."""

    def __init__(self, monitor):
        self.monitor = monitor
        self.library = None
        self._correction_ref = _CORRECTION(self._correct)
        self._status_ref = _STATUS(self._status)
        self.started = False

    def start(self):
        path = next((candidate for candidate in _library_candidates() if candidate.is_file()), None)
        if path is None:
            raise FileNotFoundError("Rust native library is missing")
        self.library = ctypes.CDLL(str(path))
        try:
            abi_version = self.library.multilang_abi_version
        except AttributeError as exc:
            raise RuntimeError("Rust keyboard library is outdated") from exc
        abi_version.argtypes = []
        abi_version.restype = ctypes.c_uint32
        if abi_version() < 2:
            raise RuntimeError("Rust keyboard library is outdated")
        self.library.multilang_start.argtypes = [_CORRECTION, _STATUS]
        self.library.multilang_start.restype = ctypes.c_int
        self.library.multilang_stop.argtypes = []
        self.library.multilang_stop.restype = ctypes.c_int
        result = self.library.multilang_start(self._correction_ref, self._status_ref)
        if result != 0:
            raise RuntimeError(f"Rust keyboard hook failed to start (code {result})")
        self.started = True

    def stop(self):
        if self.started and self.library is not None:
            result = self.library.multilang_stop()
            self.started = False
            if result != 0:
                self.monitor.on_status(f"Rust keyboard hook failed to stop (code {result})")

    def _correct(self, word_ptr, word_len, output_ptr, output_capacity, output_len, target_language):
        try:
            word = ctypes.string_at(word_ptr, word_len).decode("utf-8")
            replacement, target = self.monitor._recognize_word(word)
            if replacement is None:
                return 0
            encoded = replacement.encode("utf-8")
            if len(encoded) > output_capacity:
                return 0
            ctypes.memmove(output_ptr, encoded, len(encoded))
            output_len[0] = len(encoded)
            # Any correction passed the high-confidence language check, so
            # switch layouts immediately after this word.
            target_language[0] = (1 if target == "en" else 2) | 0x100
            return 1
        except Exception as exc:
            self.monitor.on_status(f"فشل تحليل الكلمة: {exc}")
            return 0

    def _status(self, message_ptr, message_len):
        try:
            message = ctypes.string_at(message_ptr, message_len).decode("utf-8", "replace")
            if message.startswith("MULTILANG_SWITCH_FAILED:"):
                target = message.partition(":")[2]
                if target in {"en", "ar"}:
                    language = "English" if target == "en" else "العربية"
                    self.monitor.on_status(
                        f"تم تصحيح الكلمة، لكن تعذّر التبديل إلى {language}؛ سأعيد المحاولة عند كلمة واثقة أخرى"
                    )
            else:
                self.monitor.on_status(message)
        except Exception:
            pass

"""Private, same-user IPC used by the Fcitx5 Wayland input method."""

from __future__ import annotations

import errno
import os
import socket
import socketserver
import stat
import threading
from pathlib import Path


def socket_path() -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir and os.path.isabs(runtime_dir):
        directory = Path(runtime_dir) / "multilang"
    else:
        uid = getattr(os, "getuid", lambda: os.getpid())()
        directory = Path("/tmp") / f"multilang-{uid}"
    return directory / "correction.sock"


def show_running_instance() -> bool:
    """Ask an existing Wayland instance to show its window, if one is active."""
    if not hasattr(socket, "AF_UNIX"):
        return False
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(0.5)
        client.connect(str(socket_path()))
        client.sendall(b"SHOW\n")
        return client.recv(64) == b"SHOWN\n"
    except OSError:
        return False
    finally:
        client.close()


class _RequestHandler(socketserver.StreamRequestHandler):
    def handle(self):
        monitor = self.server.monitor
        while True:
            line = self.rfile.readline(8193)
            if not line:
                return
            if len(line) > 8192 or not line.endswith(b"\n"):
                self.wfile.write(b"ERR\n")
                return
            try:
                request = line.decode("utf-8").rstrip("\n")
            except UnicodeDecodeError:
                self.wfile.write(b"ERR\n")
                continue

            if request == "PING":
                response = b"READY\n" if monitor.enabled else b"STOPPED\n"
            elif request == "SHOW":
                show_window = getattr(monitor, "show_window", None)
                if show_window is None:
                    response = b"ERR\n"
                else:
                    try:
                        show_window()
                        response = b"SHOWN\n"
                    except Exception:
                        response = b"ERR\n"
            elif request == "LANG":
                try:
                    language = monitor.current_wayland_language()
                except Exception:
                    language = None
                response = (
                    f"LANG\t{language}\n".encode("ascii")
                    if language in ("en", "ar") else b"NONE\n"
                )
            elif request.startswith("WORD\t"):
                word = request[5:]
                if len(word.encode("utf-8")) > 2048 or not word:
                    self.wfile.write(b"ERR\n")
                    self.wfile.flush()
                    continue
                try:
                    result = monitor.correct_wayland_word(word)
                except Exception:
                    result = None
                if result is None:
                    response = b"NONE\n"
                else:
                    replacement, language, switched = result
                    # Replacements produced by the Arabic/English converter never
                    # contain tabs or line breaks; validate the wire format anyway.
                    if any(char in replacement for char in "\t\r\n"):
                        response = b"ERR\n"
                    else:
                        switch_state = "?" if switched is None else str(int(switched))
                        response = (
                            f"FIX\t{language}\t{switch_state}\t{replacement}\n"
                        ).encode("utf-8")
            else:
                response = b"ERR\n"
            self.wfile.write(response)
            self.wfile.flush()


if hasattr(socketserver, "ThreadingUnixStreamServer"):
    class _ThreadedUnixServer(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True
        allow_reuse_address = False
else:
    _ThreadedUnixServer = None


class WaylandCorrectionServer:
    """Expose only word correction through a user-owned Unix domain socket."""

    def __init__(self, monitor):
        self.monitor = monitor
        self.path = socket_path()
        self.server = None
        self.thread = None
        self._socket_identity = None
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            if self.server is not None:
                return
            if not hasattr(socket, "AF_UNIX") or _ThreadedUnixServer is None:
                raise RuntimeError("مقابس Unix غير متاحة على هذا النظام")
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            parent_stat = self.path.parent.lstat()
            if not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_uid != os.getuid():
                raise RuntimeError(f"مجلد مقبس Wayland غير آمن: {self.path.parent}")
            os.chmod(self.path.parent, 0o700)
            self._remove_stale_socket()
            server = _ThreadedUnixServer(str(self.path), _RequestHandler)
            server.monitor = self.monitor
            os.chmod(self.path, 0o600)
            socket_stat = self.path.lstat()
            self._socket_identity = (socket_stat.st_dev, socket_stat.st_ino)
            self.server = server
            self.thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.2},
                name="MultilangWaylandIPC",
                daemon=True,
            )
            self.thread.start()

    def stop(self):
        with self._lock:
            server, thread = self.server, self.thread
            self.server = None
            self.thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        self._unlink_owned_socket()

    def _remove_stale_socket(self):
        try:
            existing = self.path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(existing.st_mode):
            raise RuntimeError(f"المسار موجود وليس مقبساً: {self.path}")
        if existing.st_uid != os.getuid():
            raise RuntimeError("مقبس Multilang الموجود ليس ملكاً للمستخدم الحالي")

        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.2)
            probe.connect(str(self.path))
        except OSError as exc:
            if exc.errno not in (errno.ECONNREFUSED, errno.ENOENT, errno.ECONNRESET):
                raise RuntimeError(f"تعذّر فحص مقبس Multilang: {exc}") from exc
        else:
            raise RuntimeError("خدمة Multilang تعمل بالفعل")
        finally:
            probe.close()

        current = self.path.lstat()
        if (current.st_dev, current.st_ino) == (existing.st_dev, existing.st_ino):
            self.path.unlink()

    def _unlink_owned_socket(self):
        identity = self._socket_identity
        self._socket_identity = None
        if identity is None:
            return
        try:
            current = self.path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(current.st_mode) and (current.st_dev, current.st_ino) == identity:
            self.path.unlink()

#!/usr/bin/env python3
"""Orqella Terminal sidecar.

The daemon never owns the terminal. This sidecar starts local shells under a
PTY and keeps each session alive while a browser detaches, so a phone can lock,
reconnect, and continue where it left off. It exposes a small JSON API plus an
authenticated WebSocket at /ws/terminal.

Unix uses the standard-library pty module. Windows uses pywinpty/ConPTY when
the optional dependency is installed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import queue
import shutil
import signal
import struct
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HOST_HOME = os.environ.get("ORQELLA_HOST_HOME") or os.path.expanduser("~")
TOKEN = os.environ.get("ORQELLA_MODULE_TOKEN", "")
try:
    CONFIG = json.loads(os.environ.get("ORQELLA_MODULE_CONFIG") or "{}")
except json.JSONDecodeError:
    CONFIG = {}
PLATFORM = platform.system()
IS_WINDOWS = PLATFORM == "Windows"
IS_MACOS = PLATFORM == "Darwin"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_SESSIONS = 16
MAX_FRAME = 4 * 1024 * 1024
REPLAY_CHUNK = 32 * 1024
WS_TOKEN = uuid.uuid4().hex

if IS_WINDOWS:
    try:
        from winpty import PtyProcess  # type: ignore
    except ImportError:
        PtyProcess = None  # type: ignore
else:
    import fcntl
    import pty
    import termios

SESSIONS: dict[str, "TerminalSession"] = {}
SESSIONS_LOCK = threading.Lock()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def buffer_limit() -> int:
    try:
        kb = int(CONFIG.get("scrollback_kb") or 512)
    except (TypeError, ValueError):
        kb = 512
    return max(64, min(4096, kb)) * 1024


def strip_secrets(env: dict[str, str]) -> dict[str, str]:
    clean = dict(env)
    clean.pop("ORQELLA_MODULE_TOKEN", None)
    clean.pop("ORQELLA_MODULE_CONFIG", None)
    clean["HOME"] = HOST_HOME
    clean["TERM"] = "xterm-256color"
    clean["COLORTERM"] = "truecolor"
    clean["TERM_PROGRAM"] = "Orqella"
    return clean


def shell_candidates() -> list[dict]:
    names: list[str] = []
    if IS_WINDOWS:
        names = ["pwsh", "powershell", "cmd"]
    else:
        configured = str(CONFIG.get("default_shell") or "").strip()
        if configured:
            names.append(configured)
        names.extend([os.environ.get("SHELL", ""), "zsh", "bash", "fish", "sh"])
    rows: list[dict] = []
    seen: set[str] = set()
    for name in names:
        if not name:
            continue
        path = name if os.path.isabs(name) else shutil.which(name)
        if not path:
            continue
        resolved = os.path.realpath(path)
        if resolved in seen or not os.access(resolved, os.X_OK):
            continue
        seen.add(resolved)
        rows.append({"id": path, "path": resolved, "name": Path(resolved).stem})
    return rows


def default_shell() -> str:
    configured = str(CONFIG.get("default_shell") or "").strip()
    if configured:
        path = configured if os.path.isabs(configured) else shutil.which(configured)
        if path and os.access(path, os.X_OK):
            return path
    shells = shell_candidates()
    if not shells:
        raise RuntimeError("no usable shell was found on PATH")
    return shells[0]["id"]


def resolve_shell(requested: str | None) -> str:
    value = str(requested or default_shell()).strip()
    if not value:
        raise ValueError("shell is required")
    path = value if os.path.isabs(value) else shutil.which(value)
    if not path or not os.access(path, os.X_OK):
        raise ValueError(f"shell not found: {value}")
    return os.path.realpath(path)


def resolve_cwd(requested: str | None) -> str:
    raw = str(requested or "").strip()
    value = os.path.expanduser(raw) if raw else HOST_HOME
    if not os.path.isdir(value):
        raise ValueError(f"working directory does not exist: {value}")
    return value


class Client:
    def __init__(self) -> None:
        self.queue: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=600)
        self.alive = True

    def send(self, kind: str, payload: object) -> None:
        if not self.alive:
            return
        try:
            self.queue.put_nowait((kind, payload))
        except queue.Full:
            pass


class UnixPty:
    def __init__(self, command: list[str], cwd: str, env: dict[str, str], cols: int, rows: int):
        pid, master = pty.fork()
        if pid == 0:  # child
            try:
                os.chdir(cwd)
                os.execvpe(command[0], command, env)
            except BaseException as error:  # noqa: BLE001 - child must exit
                try:
                    os.write(2, f"\r\norqella terminal: {error}\r\n".encode())
                finally:
                    os._exit(127)
        self.pid = pid
        self.master = master
        self.resize(cols, rows)

    def read(self) -> bytes:
        return os.read(self.master, 65536)

    def write(self, data: bytes) -> None:
        os.write(self.master, data)

    def resize(self, cols: int, rows: int) -> None:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        try:
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

    def close(self) -> None:
        try:
            pgid = os.getpgid(self.pid)
        except OSError:
            pgid = None
        try:
            os.close(self.master)
        except OSError:
            pass
        if pgid is None:
            return
        for sig, wait_seconds in ((signal.SIGHUP, 0.15), (signal.SIGTERM, 0.15), (signal.SIGKILL, 0.15)):
            try:
                os.killpg(pgid, sig)
            except OSError:
                try:
                    os.waitpid(self.pid, os.WNOHANG)
                except (ChildProcessError, OSError):
                    pass
                return
            deadline = time.time() + wait_seconds
            while time.time() < deadline:
                try:
                    pid, _ = os.waitpid(self.pid, os.WNOHANG)
                except (ChildProcessError, OSError):
                    return
                if pid:
                    return
                time.sleep(0.01)
        try:
            os.waitpid(self.pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass


class WindowsPty:
    def __init__(self, command: list[str], cwd: str, env: dict[str, str], cols: int, rows: int):
        if PtyProcess is None:
            raise RuntimeError(
                "Windows terminal support needs pywinpty. Run: python -m pip install pywinpty"
            )
        self.process = PtyProcess.spawn(
            command,
            cwd=cwd,
            env=env,
            dimensions=(rows, cols),
        )
        self.resize(cols, rows)

    def read(self) -> bytes:
        value = self.process.read(65536)
        if value is None or value == "":
            raise EOFError
        return value.encode("utf-8", "replace") if isinstance(value, str) else value

    def write(self, data: bytes) -> None:
        self.process.write(data.decode("utf-8", "replace"))

    def resize(self, cols: int, rows: int) -> None:
        try:
            self.process.setwinsize(rows, cols)
        except (AttributeError, OSError):
            pass

    def close(self) -> None:
        try:
            self.process.terminate(force=True)
        except (AttributeError, OSError):
            pass


class TerminalSession:
    def __init__(self, title: str, command: list[str], cwd: str, cols: int, rows: int):
        self.id = uuid.uuid4().hex[:12]
        self.title = title
        self.command = command
        self.cwd = cwd
        self.cols = cols
        self.rows = rows
        self.created_at = now_iso()
        self.status = "running"
        self.exit_code: int | None = None
        self.last_activity = time.time()
        self.lock = threading.Lock()
        self.clients: set[Client] = set()
        self.buffer = bytearray()
        self.buffer_max = buffer_limit()
        env = strip_secrets(dict(os.environ))
        if IS_WINDOWS:
            self.pty = WindowsPty(command, cwd, env, cols, rows)
        else:
            self.pty = UnixPty(command, cwd, env, cols, rows)
        self.reader = threading.Thread(target=self._read_loop, name=f"terminal-{self.id}", daemon=True)
        self.reader.start()

    def _read_loop(self) -> None:
        while True:
            try:
                data = self.pty.read()
            except (EOFError, OSError):
                break
            if not data:
                break
            with self.lock:
                self.last_activity = time.time()
                self.buffer.extend(data)
                if len(self.buffer) > self.buffer_max:
                    del self.buffer[: len(self.buffer) - self.buffer_max]
                clients = list(self.clients)
            for client in clients:
                client.send("data", data)
        with self.lock:
            self.status = "exited"
            clients = list(self.clients)
        for client in clients:
            client.send("event", {"type": "exit", "session": self.id})
        if self.exit_code is None:
            self.exit_code = 0

    def info(self) -> dict:
        with self.lock:
            return {
                "id": self.id,
                "title": self.title,
                "status": self.status,
                "cwd": self.cwd,
                "command": Path(self.command[0]).name,
                "cols": self.cols,
                "rows": self.rows,
                "created_at": self.created_at,
                "last_activity": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.last_activity)),
                "attached": len(self.clients),
                "buffered": len(self.buffer),
            }

    def attach(self) -> Client:
        client = Client()
        with self.lock:
            client.queue.put_nowait(("event", {"type": "ready", "session": self.info_unlocked()}))
            for start in range(0, len(self.buffer), REPLAY_CHUNK):
                client.queue.put_nowait(("data", bytes(self.buffer[start : start + REPLAY_CHUNK])))
            self.clients.add(client)
        return client

    def info_unlocked(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "cwd": self.cwd,
            "command": Path(self.command[0]).name,
            "cols": self.cols,
            "rows": self.rows,
            "created_at": self.created_at,
            "last_activity": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.last_activity)),
            "attached": len(self.clients),
            "buffered": len(self.buffer),
        }

    def detach(self, client: Client) -> None:
        client.alive = False
        with self.lock:
            self.clients.discard(client)

    def write(self, data: bytes) -> None:
        if self.status != "running":
            return
        try:
            self.pty.write(data)
            with self.lock:
                self.last_activity = time.time()
        except OSError:
            pass

    def resize(self, cols: int, rows: int) -> None:
        cols = max(2, min(400, int(cols)))
        rows = max(2, min(200, int(rows)))
        with self.lock:
            self.cols = cols
            self.rows = rows
        self.pty.resize(cols, rows)

    def close(self) -> None:
        self.pty.close()
        with self.lock:
            self.status = "exited"
            clients = list(self.clients)
        for client in clients:
            client.send("event", {"type": "exit", "session": self.id})


def create_session(payload: dict) -> TerminalSession:
    payload = payload or {}
    try:
        cols = max(2, min(400, int(payload.get("cols") or 80)))
        rows = max(2, min(200, int(payload.get("rows") or 24)))
    except (TypeError, ValueError):
        cols, rows = 80, 24
    shell = resolve_shell(payload.get("shell"))
    command = [shell]
    cwd = resolve_cwd(payload.get("cwd"))
    title = str(payload.get("title") or "").strip() or Path(shell).stem
    title = title[:80]
    with SESSIONS_LOCK:
        if len(SESSIONS) >= MAX_SESSIONS:
            exited = [session for session in SESSIONS.values() if session.status == "exited"]
            if exited:
                SESSIONS.pop(exited[0].id, None)
            else:
                raise ValueError(f"maximum of {MAX_SESSIONS} terminal sessions is already open")
        session = TerminalSession(
            title=title,
            command=command,
            cwd=cwd,
            cols=cols,
            rows=rows,
        )
        SESSIONS[session.id] = session
    return session


def close_session(session_id: str) -> bool:
    with SESSIONS_LOCK:
        session = SESSIONS.pop(session_id, None)
    if not session:
        return False
    session.close()
    return True


def status_payload() -> dict:
    with SESSIONS_LOCK:
        sessions = sorted(
            (session.info() for session in SESSIONS.values()),
            key=lambda item: item["created_at"],
        )
    return {
        "platform": PLATFORM,
        "capabilities": {
            "local_shell": True,
            "windows_pty": (not IS_WINDOWS) or PtyProcess is not None,
            "mobile_safe_area": True,
        },
        "shells": shell_candidates(),
        "default_shell": default_shell(),
        "sessions": sessions,
        "ws_token": WS_TOKEN,
    }


def ws_accept(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def ws_frame(opcode: int, payload: bytes) -> bytes:
    header = bytearray([0x80 | opcode])
    size = len(payload)
    if size < 126:
        header.append(size)
    elif size < 65536:
        header.append(126)
        header += struct.pack("!H", size)
    else:
        header.append(127)
        header += struct.pack("!Q", size)
    return bytes(header) + payload


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def authorized(self) -> bool:
        if not TOKEN or self.headers.get("X-Orqella-Module-Token") == TOKEN:
            return True
        self.send_json(401, {"error": "unauthorized"})
        return False

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > MAX_FRAME:
            raise ValueError("request body is too large")
        value = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("JSON object required")
        return value

    def discard_body(self) -> None:
        """Drain a request body the route does not need.

        DELETE requests from the browser include an empty JSON object. If the
        body stays in the HTTP/1.1 keep-alive stream, the next request line is
        parsed as `{}POST` and the sidecar returns 501.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return
        if length > MAX_FRAME:
            self.close_connection = True
            return
        self.rfile.read(length)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/ws/terminal":
            self.handle_ws(parsed)
            return
        if not self.authorized():
            return
        try:
            if parsed.path == "/health":
                return self.send_json(200, {"ok": True})
            if parsed.path == "/api/status":
                return self.send_json(200, status_payload())
            if parsed.path == "/api/sessions":
                with SESSIONS_LOCK:
                    rows = sorted(
                        (session.info() for session in SESSIONS.values()),
                        key=lambda item: item["created_at"],
                    )
                return self.send_json(200, {"sessions": rows})
            return self.send_json(404, {"error": "not found"})
        except Exception as error:  # noqa: BLE001 - API boundary
            return self.send_json(500, {"error": str(error)})

    def do_POST(self) -> None:
        if not self.authorized():
            return
        parsed = urlparse(self.path)
        try:
            payload = self.read_json()
            if parsed.path == "/api/sessions":
                session = create_session(payload)
                return self.send_json(201, {"session": session.info()})
            return self.send_json(404, {"error": "not found"})
        except ValueError as error:
            return self.send_json(400, {"error": str(error)})
        except Exception as error:  # noqa: BLE001 - API boundary
            return self.send_json(500, {"error": str(error)})

    def do_DELETE(self) -> None:
        if not self.authorized():
            return
        self.discard_body()
        parsed = urlparse(self.path)
        try:
            if parsed.path.startswith("/api/sessions/"):
                session_id = parsed.path.rsplit("/", 1)[-1]
                if not close_session(session_id):
                    return self.send_json(404, {"error": "session not found"})
                return self.send_json(200, {"ok": True})
            return self.send_json(404, {"error": "not found"})
        except Exception as error:  # noqa: BLE001 - API boundary
            return self.send_json(500, {"error": str(error)})

    def handle_ws(self, parsed: ParsedPath) -> None:
        if not self.authorized():
            return
        query = parse_qs(parsed.query)
        session_id = (query.get("session") or [""])[0]
        if (query.get("token") or [""])[0] != WS_TOKEN:
            return self.send_json(403, {"error": "invalid terminal websocket token"})
        with SESSIONS_LOCK:
            session = SESSIONS.get(session_id)
        if not session:
            return self.send_json(404, {"error": "session not found"})
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            return self.send_json(400, {"error": "websocket handshake required"})
        self.websocket_handshake(key)
        write_lock = threading.Lock()

        def send(opcode: int, payload: bytes) -> None:
            with write_lock:
                try:
                    self.wfile.write(ws_frame(opcode, payload))
                    self.wfile.flush()
                except OSError:
                    raise

        client = session.attach()

        def sender() -> None:
            try:
                while client.alive:
                    try:
                        kind, value = client.queue.get(timeout=20)
                    except queue.Empty:
                        send(0x9, b"")
                        continue
                    if kind == "data":
                        send(0x2, value)  # type: ignore[arg-type]
                    else:
                        send(0x1, json.dumps(value).encode())
            except OSError:
                pass
            finally:
                try:
                    send(0x8, b"")
                except OSError:
                    pass

        threading.Thread(target=sender, daemon=True).start()
        try:
            while True:
                opcode, data = self.ws_read()
                if opcode is None or opcode == 0x8:
                    break
                if opcode == 0x9:
                    send(0xA, data)
                    continue
                if opcode not in (0x1, 0x2):
                    continue
                if opcode == 0x1:
                    try:
                        message = json.loads(data.decode("utf-8", "replace"))
                    except json.JSONDecodeError:
                        session.write(data)
                        continue
                    kind = message.get("type")
                    if kind == "input":
                        text = str(message.get("data") or "")
                        if text:
                            session.write(text.encode("utf-8"))
                    elif kind == "resize":
                        session.resize(message.get("cols"), message.get("rows"))
                    elif kind == "ping":
                        client.send("event", {"type": "pong", "at": now_iso()})
                    elif kind == "detach":
                        break
                else:
                    session.write(data)
        except OSError:
            pass
        finally:
            session.detach(client)

    def websocket_handshake(self, key: str) -> None:
        self.close_connection = False
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", ws_accept(key))
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True

    def ws_read(self) -> tuple[int | None, bytes]:
        head = self.rfile.read(2)
        if len(head) < 2:
            return None, b""
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        size = head[1] & 0x7F
        if size == 126:
            size = struct.unpack("!H", self.rfile.read(2))[0]
        elif size == 127:
            size = struct.unpack("!Q", self.rfile.read(8))[0]
        if size > MAX_FRAME:
            raise ValueError("websocket frame too large")
        mask = self.rfile.read(4) if masked else b""
        data = self.rfile.read(size)
        if masked and mask:
            data = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        return opcode, data


class ParsedPath:
    def __init__(self, path: str, query: str):
        self.path = path
        self.query = query


def main() -> None:
    port = int(os.environ["ORQELLA_MODULE_PORT"])
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    finally:
        with SESSIONS_LOCK:
            sessions = list(SESSIONS.values())
        for session in sessions:
            session.close()


if __name__ == "__main__":
    main()

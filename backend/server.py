#!/usr/bin/env python3
"""Orqella Terminal sidecar.

The daemon never owns the terminal. This sidecar starts real shells or SSH
clients under a PTY and keeps each session alive while a browser detaches, so a
phone can lock, reconnect, and continue where it left off. It exposes a small
JSON API plus an authenticated WebSocket at /ws/terminal.

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
import re
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
MODULE_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = MODULE_ROOT / "data"
PROFILES_FILE = DATA_DIR / "profiles.json"
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
PROFILES: list[dict] = []
PROFILES_LOCK = threading.Lock()


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


HOST_RE = re.compile(r"^[A-Za-z0-9_.:%\[\]-]+$")
USER_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def ssh_command(payload: dict) -> list[str]:
    ssh = shutil.which("ssh")
    if not ssh:
        raise RuntimeError("OpenSSH client (`ssh`) is not installed or not on PATH")
    host = str(payload.get("host") or "").strip()
    user = str(payload.get("user") or "").strip()
    if not host or not HOST_RE.fullmatch(host):
        raise ValueError("SSH host is invalid")
    if not user or not USER_RE.fullmatch(user):
        raise ValueError("SSH user is invalid")
    try:
        port = int(payload.get("port") or 22)
    except (TypeError, ValueError) as error:
        raise ValueError("SSH port is invalid") from error
    if port < 1 or port > 65535:
        raise ValueError("SSH port is out of range")
    command = [
        ssh,
        "-tt",
        "-p",
        str(port),
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
    ]
    identity = str(payload.get("identity_file") or "").strip()
    if identity:
        identity = os.path.expanduser(identity)
        if not os.path.isfile(identity):
            raise ValueError(f"identity file does not exist: {identity}")
        command += ["-i", identity, "-o", "IdentitiesOnly=yes"]
    command.append(f"{user}@{host}")
    return command


def profile_from_payload(payload: dict) -> dict:
    kind = str(payload.get("kind") or "local").strip().lower()
    if kind not in ("local", "ssh"):
        raise ValueError("profile kind must be local or ssh")
    name = str(payload.get("name") or "").strip()
    if not name:
        name = "Local shell" if kind == "local" else f"{payload.get('user', '')}@{payload.get('host', '')}"
    if len(name) > 80 or any(ord(c) < 32 for c in name):
        raise ValueError("profile name is invalid")
    profile = {
        "id": str(payload.get("id") or uuid.uuid4().hex[:10]),
        "name": name,
        "kind": kind,
        "host": str(payload.get("host") or "").strip(),
        "user": str(payload.get("user") or "").strip(),
        "port": int(payload.get("port") or 22),
        "identity_file": str(payload.get("identity_file") or "").strip(),
        "shell": str(payload.get("shell") or "").strip(),
        "cwd": str(payload.get("cwd") or "").strip(),
        "created_at": str(payload.get("created_at") or now_iso()),
    }
    if kind == "ssh":
        if not profile["host"] or not HOST_RE.fullmatch(profile["host"]):
            raise ValueError("SSH host is invalid")
        if not profile["user"] or not USER_RE.fullmatch(profile["user"]):
            raise ValueError("SSH user is invalid")
        if not (1 <= profile["port"] <= 65535):
            raise ValueError("SSH port is out of range")
        if profile["identity_file"]:
            expanded = os.path.expanduser(profile["identity_file"])
            if not os.path.isfile(expanded):
                raise ValueError(f"identity file does not exist: {expanded}")
            profile["identity_file"] = expanded
    else:
        if profile["shell"]:
            profile["shell"] = resolve_shell(profile["shell"])
    return profile


def load_profiles() -> list[dict]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not PROFILES_FILE.exists():
        return []
    try:
        raw = json.loads(PROFILES_FILE.read_text())
        return [item for item in raw if isinstance(item, dict) and item.get("id")]
    except (OSError, json.JSONDecodeError):
        return []


def save_profiles() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = PROFILES_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(PROFILES, indent=2) + "\n")
    os.replace(temp, PROFILES_FILE)


def public_profile(profile: dict) -> dict:
    return {
        "id": profile.get("id"),
        "name": profile.get("name"),
        "kind": profile.get("kind"),
        "host": profile.get("host"),
        "user": profile.get("user"),
        "port": profile.get("port"),
        "identity_file": profile.get("identity_file"),
        "shell": profile.get("shell"),
        "cwd": profile.get("cwd"),
    }


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
            os.killpg(os.getpgid(self.pid), signal.SIGHUP)
        except OSError:
            pass
        try:
            os.close(self.master)
        except OSError:
            pass
        try:
            os.waitpid(self.pid, os.WNOHANG)
        except OSError:
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
    def __init__(
        self,
        title: str,
        command: list[str],
        cwd: str,
        cols: int,
        rows: int,
        kind: str,
        profile_id: str = "",
        host: str = "",
        user: str = "",
    ):
        self.id = uuid.uuid4().hex[:12]
        self.title = title
        self.command = command
        self.cwd = cwd
        self.cols = cols
        self.rows = rows
        self.kind = kind
        self.profile_id = profile_id
        self.host = host
        self.user = user
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
                "kind": self.kind,
                "status": self.status,
                "profile_id": self.profile_id,
                "host": self.host,
                "user": self.user,
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
            "kind": self.kind,
            "status": self.status,
            "profile_id": self.profile_id,
            "host": self.host,
            "user": self.user,
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


def profile_payload(payload: dict) -> dict:
    profile_id = str(payload.get("profile_id") or "").strip()
    if profile_id:
        with PROFILES_LOCK:
            profile = next((item for item in PROFILES if item.get("id") == profile_id), None)
        if not profile:
            raise ValueError("profile not found")
        merged = dict(profile)
        merged.update({key: value for key, value in payload.items() if value not in (None, "")})
        return merged
    return payload


def create_session(payload: dict) -> TerminalSession:
    payload = profile_payload(payload or {})
    kind = str(payload.get("kind") or "local").strip().lower()
    if kind not in ("local", "ssh"):
        raise ValueError("session kind must be local or ssh")
    try:
        cols = max(2, min(400, int(payload.get("cols") or 80)))
        rows = max(2, min(200, int(payload.get("rows") or 24)))
    except (TypeError, ValueError):
        cols, rows = 80, 24
    if kind == "ssh":
        command = ssh_command(payload)
        cwd = resolve_cwd(payload.get("cwd") or HOST_HOME)
        title = str(payload.get("title") or "").strip()
        if not title:
            title = f"{payload.get('user')}@{payload.get('host')}"
        host = str(payload.get("host") or "")
        user = str(payload.get("user") or "")
    else:
        shell = resolve_shell(payload.get("shell"))
        command = [shell]
        cwd = resolve_cwd(payload.get("cwd"))
        title = str(payload.get("title") or "").strip()
        if not title:
            title = Path(shell).stem
        host = ""
        user = ""
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
            kind=kind,
            profile_id=str(payload.get("profile_id") or ""),
            host=host,
            user=user,
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
    with PROFILES_LOCK:
        profiles = [public_profile(profile) for profile in PROFILES]
    return {
        "platform": PLATFORM,
        "capabilities": {
            "local_shell": True,
            "ssh": shutil.which("ssh") is not None,
            "windows_pty": (not IS_WINDOWS) or PtyProcess is not None,
            "mobile_safe_area": True,
        },
        "shells": shell_candidates(),
        "default_shell": default_shell(),
        "sessions": sessions,
        "profiles": profiles,
        "ws_token": WS_TOKEN,
        "data_dir": str(DATA_DIR),
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
            if parsed.path == "/api/profiles":
                with PROFILES_LOCK:
                    rows = [public_profile(profile) for profile in PROFILES]
                return self.send_json(200, {"profiles": rows})
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
            if parsed.path == "/api/profiles":
                with PROFILES_LOCK:
                    profile = profile_from_payload(payload)
                    PROFILES.append(profile)
                    save_profiles()
                return self.send_json(201, {"profile": public_profile(profile)})
            if parsed.path.startswith("/api/sessions/") and parsed.path.endswith("/rename"):
                session_id = parsed.path.split("/")[3]
                title = str(payload.get("title") or "").strip()
                if not title or len(title) > 80:
                    raise ValueError("title is invalid")
                with SESSIONS_LOCK:
                    session = SESSIONS.get(session_id)
                if not session:
                    return self.send_json(404, {"error": "session not found"})
                session.title = title
                return self.send_json(200, {"session": session.info()})
            return self.send_json(404, {"error": "not found"})
        except ValueError as error:
            return self.send_json(400, {"error": str(error)})
        except Exception as error:  # noqa: BLE001 - API boundary
            return self.send_json(500, {"error": str(error)})

    def do_PUT(self) -> None:
        if not self.authorized():
            return
        parsed = urlparse(self.path)
        try:
            payload = self.read_json()
            if parsed.path.startswith("/api/profiles/"):
                profile_id = parsed.path.rsplit("/", 1)[-1]
                with PROFILES_LOCK:
                    existing = next((item for item in PROFILES if item.get("id") == profile_id), None)
                    if not existing:
                        return self.send_json(404, {"error": "profile not found"})
                    update = dict(payload)
                    update["id"] = profile_id
                    update["created_at"] = existing.get("created_at")
                    profile = profile_from_payload(update)
                    PROFILES[PROFILES.index(existing)] = profile
                    save_profiles()
                return self.send_json(200, {"profile": public_profile(profile)})
            return self.send_json(404, {"error": "not found"})
        except ValueError as error:
            return self.send_json(400, {"error": str(error)})

    def do_DELETE(self) -> None:
        if not self.authorized():
            return
        parsed = urlparse(self.path)
        try:
            if parsed.path.startswith("/api/sessions/"):
                session_id = parsed.path.rsplit("/", 1)[-1]
                if not close_session(session_id):
                    return self.send_json(404, {"error": "session not found"})
                return self.send_json(200, {"ok": True})
            if parsed.path.startswith("/api/profiles/"):
                profile_id = parsed.path.rsplit("/", 1)[-1]
                with PROFILES_LOCK:
                    before = len(PROFILES)
                    PROFILES[:] = [item for item in PROFILES if item.get("id") != profile_id]
                    if len(PROFILES) == before:
                        return self.send_json(404, {"error": "profile not found"})
                    save_profiles()
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
    global PROFILES
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROFILES = load_profiles()
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

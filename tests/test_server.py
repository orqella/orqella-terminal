from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import server  # noqa: E402


class TerminalBackendTests(unittest.TestCase):
    def test_finds_a_shell(self) -> None:
        shells = server.shell_candidates()
        self.assertTrue(shells)
        self.assertTrue(server.default_shell())

    def test_status_is_local_only(self) -> None:
        status = server.status_payload()
        self.assertNotIn("profiles", status)
        self.assertNotIn("ssh", status["capabilities"])
        self.assertTrue(status["capabilities"]["local_shell"])
        self.assertGreaterEqual(len(status["ws_token"]), 32)

    def test_rejects_missing_working_directory(self) -> None:
        with self.assertRaises(ValueError):
            server.resolve_cwd("/definitely/not/a/directory")

    def test_defaults_working_directory_to_home(self) -> None:
        self.assertEqual(server.resolve_cwd(""), server.HOST_HOME)

    def test_windows_pty_explains_the_missing_dependency(self) -> None:
        had = hasattr(server, "PtyProcess")
        old = getattr(server, "PtyProcess", None)
        server.PtyProcess = None
        try:
            with self.assertRaises(RuntimeError) as raised:
                server.WindowsPty(["cmd"], str(Path.cwd()), {}, 80, 24)
            self.assertIn("pywinpty", str(raised.exception))
        finally:
            if had:
                server.PtyProcess = old
            else:
                del server.PtyProcess

    def test_websocket_token_is_not_empty(self) -> None:
        self.assertGreaterEqual(len(server.WS_TOKEN), 32)

    @unittest.skipIf(server.IS_WINDOWS, "PTY integration test runs on POSIX hosts")
    def test_delete_body_does_not_poison_keep_alive(self) -> None:
        old_token = server.TOKEN
        server.TOKEN = "test-token"
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        httpd.daemon_threads = True
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection(
            "127.0.0.1", httpd.server_address[1], timeout=10
        )
        session_ids: list[str] = []
        try:
            payload = json.dumps({"cols": 80, "rows": 24})
            headers = {
                "Content-Type": "application/json",
                "X-Orqella-Module-Token": "test-token",
            }

            connection.request("POST", "/api/sessions", payload, headers)
            response = connection.getresponse()
            self.assertEqual(response.status, 201)
            first = json.loads(response.read())["session"]
            session_ids.append(first["id"])

            # The browser used to send DELETE with an empty JSON body. The
            # handler must drain it before reusing the keep-alive connection.
            connection.request(
                "DELETE", f"/api/sessions/{first['id']}", "{}", headers
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()

            connection.request("POST", "/api/sessions", payload, headers)
            response = connection.getresponse()
            self.assertEqual(response.status, 201)
            second = json.loads(response.read())["session"]
            session_ids.append(second["id"])
        finally:
            connection.close()
            for session_id in session_ids:
                server.close_session(session_id)
            httpd.shutdown()
            httpd.server_close()
            server.TOKEN = old_token

    def test_page_loads_vendored_xterm_stylesheet_first(self) -> None:
        root = Path(__file__).resolve().parents[1]
        page = (root / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertTrue((root / "ui" / "vendor" / "xterm" / "xterm.css").is_file())
        self.assertIn('href="vendor/xterm/xterm.css"', page)
        self.assertLess(page.index("vendor/xterm/xterm.css"), page.index("terminal.css"))

    def test_close_uses_an_in_module_dialog(self) -> None:
        root = Path(__file__).resolve().parents[1]
        page = (root / "ui" / "index.html").read_text(encoding="utf-8")
        script = (root / "ui" / "terminal.js").read_text(encoding="utf-8")
        self.assertIn('id="closeDialog"', page)
        self.assertNotIn("confirm(", script)
        self.assertNotIn("prompt(", script)
        self.assertIn('id="textSheet"', page)
        self.assertIn("openTextSheet", script)

    def test_single_keybar_and_mobile_palette(self) -> None:
        root = Path(__file__).resolve().parents[1]
        page = (root / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="keyPalette"', page)
        self.assertIn('id="selectionBar"', page)
        self.assertNotIn('id="keysSecondary"', page)


class TerminalCoreTests(unittest.TestCase):
    def tearDown(self) -> None:
        with server.SESSIONS_LOCK:
            sessions = list(server.SESSIONS.values())
        for session in sessions:
            server.close_session(session.id)

    def test_strip_secrets_replaces_host_environment(self) -> None:
        old_home = server.HOST_HOME
        server.HOST_HOME = "/tmp/orqella-home"
        try:
            clean = server.strip_secrets(
                {
                    "ORQELLA_MODULE_TOKEN": "secret",
                    "ORQELLA_MODULE_CONFIG": "{}",
                    "HOME": "/root",
                    "TERM": "dumb",
                    "PATH": "/bin",
                }
            )
        finally:
            server.HOST_HOME = old_home
        self.assertNotIn("ORQELLA_MODULE_TOKEN", clean)
        self.assertNotIn("ORQELLA_MODULE_CONFIG", clean)
        self.assertEqual(clean["HOME"], "/tmp/orqella-home")
        self.assertEqual(clean["TERM"], "xterm-256color")
        self.assertEqual(clean["COLORTERM"], "truecolor")
        self.assertEqual(clean["TERM_PROGRAM"], "Orqella")
        self.assertEqual(clean["PATH"], "/bin")

    def test_buffer_limit_is_clamped(self) -> None:
        old = dict(server.CONFIG)
        try:
            for value, expected in [
                ("1", 64 * 1024),
                ("512", 512 * 1024),
                ("999999", 4096 * 1024),
                ("not-a-number", 512 * 1024),
            ]:
                server.CONFIG["scrollback_kb"] = value
                self.assertEqual(server.buffer_limit(), expected, value)
        finally:
            server.CONFIG.clear()
            server.CONFIG.update(old)

    def test_idle_timeout_is_clamped(self) -> None:
        old = dict(server.CONFIG)
        try:
            for value, expected in [
                ("0", 0),
                ("5", 300),
                ("99999", 1440 * 60),
                ("bad", 0),
            ]:
                server.CONFIG["idle_timeout_minutes"] = value
                self.assertEqual(server.idle_timeout_seconds(), expected, value)
        finally:
            server.CONFIG.clear()
            server.CONFIG.update(old)

    def test_shell_resolution_rejects_missing_binaries(self) -> None:
        with self.assertRaises(ValueError):
            server.resolve_shell("/definitely/not/a/shell")
        with self.assertRaises(ValueError):
            server.resolve_shell(" ")
        self.assertTrue(server.resolve_shell(None))

    @unittest.skipIf(server.IS_WINDOWS, "POSIX shell test")
    def test_session_lifecycle_and_status_payload(self) -> None:
        session = server.create_session(
            {"shell": "/bin/sh", "cwd": "/", "cols": 120, "rows": 40, "title": "test"}
        )
        try:
            info = session.info()
            self.assertEqual(info["title"], "test")
            self.assertEqual(info["cols"], 120)
            self.assertEqual(info["rows"], 40)
            self.assertEqual(info["status"], "running")
            self.assertIn(session.id, [item["id"] for item in server.status_payload()["sessions"]])
            session.write(b"printf 'orqella-test\\n'\n")
            deadline = __import__("time").time() + 5
            while __import__("time").time() < deadline:
                if b"orqella-test" in session.buffer:
                    break
                __import__("time").sleep(0.05)
            self.assertIn(b"orqella-test", session.buffer)
            session.resize(1, 999)
            self.assertEqual(session.cols, 2)
            self.assertEqual(session.rows, 200)
        finally:
            self.assertTrue(server.close_session(session.id))
        self.assertFalse(server.close_session(session.id))

    @unittest.skipIf(server.IS_WINDOWS, "POSIX shell test")
    def test_natural_exit_code_is_recorded(self) -> None:
        session = server.create_session({"shell": "/bin/sh", "cwd": "/"})
        try:
            session.write(b"exit 7\n")
            deadline = __import__("time").time() + 5
            while __import__("time").time() < deadline and session.status != "exited":
                __import__("time").sleep(0.05)
            self.assertEqual(session.status, "exited")
            self.assertEqual(session.exit_code, 7)
        finally:
            server.close_session(session.id)

    @unittest.skipIf(server.IS_WINDOWS, "POSIX shell test")
    def test_shutdown_closes_sessions(self) -> None:
        session = server.create_session({"shell": "/bin/sh", "cwd": "/"})
        server.shutdown_sessions()
        self.assertNotIn(session.id, server.SESSIONS)
        self.assertEqual(session.status, "exited")

    @unittest.skipIf(server.IS_WINDOWS, "POSIX shell test")
    def test_session_limit_is_enforced_and_exited_slots_are_reused(self) -> None:
        old_limit = server.MAX_SESSIONS
        server.MAX_SESSIONS = 1
        try:
            first = server.create_session({"shell": "/bin/sh", "cwd": "/"})
            with self.assertRaises(ValueError):
                server.create_session({"shell": "/bin/sh", "cwd": "/"})
            first.close()
            second = server.create_session({"shell": "/bin/sh", "cwd": "/"})
            self.assertNotEqual(first.id, second.id)
        finally:
            server.MAX_SESSIONS = old_limit

    def test_websocket_handshake_and_frame_encoding(self) -> None:
        # RFC 6455 example key from the specification.
        self.assertEqual(
            server.ws_accept("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )
        short = server.ws_frame(1, b"hello")
        self.assertEqual(short[:2], bytes([0x81, 5]))
        medium = server.ws_frame(2, b"x" * 126)
        self.assertEqual(medium[:4], bytes([0x82, 126, 0, 126]))
        large = server.ws_frame(2, b"x" * 65536)
        self.assertEqual(large[:2], bytes([0x82, 127]))
        self.assertEqual(int.from_bytes(large[2:10], "big"), 65536)

    def test_ws_read_handles_truncated_frames_and_bad_resize_without_crashing(self) -> None:
        import io

        handler = server.Handler.__new__(server.Handler)
        handler.rfile = io.BytesIO(bytes([0x81, 0xFE, 0x00]))  # truncated 16-bit length
        self.assertEqual(handler.ws_read(), (None, b""))

        handler.rfile = io.BytesIO(bytes([0x81, 0x82, 0x01, 0x02]))  # truncated mask
        self.assertEqual(handler.ws_read(), (None, b""))

        session = server.TerminalSession.__new__(server.TerminalSession)
        session.lock = __import__("threading").Lock()
        session.cols = 80
        session.rows = 24
        session.pty = type("FakePty", (), {"resize": lambda self, c, r: None})()
        session.resize(None, "bad")
        self.assertEqual((session.cols, session.rows), (80, 24))


if __name__ == "__main__":
    unittest.main()

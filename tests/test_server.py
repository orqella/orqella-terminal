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
            headers = {"Content-Type": "application/json"}

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

    def test_single_keybar_and_mobile_palette(self) -> None:
        root = Path(__file__).resolve().parents[1]
        page = (root / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="keyPalette"', page)
        self.assertIn('id="selectionBar"', page)
        self.assertNotIn('id="keysSecondary"', page)


if __name__ == "__main__":
    unittest.main()

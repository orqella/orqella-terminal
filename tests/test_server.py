from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import server  # noqa: E402


class TerminalBackendTests(unittest.TestCase):
    def test_finds_a_shell(self) -> None:
        shells = server.shell_candidates()
        self.assertTrue(shells)
        self.assertTrue(server.default_shell())

    def test_rejects_unsafe_ssh_hosts(self) -> None:
        for host in ("", "host with spaces", "host;rm -rf /", "-oProxyCommand=x"):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    server.ssh_command({"host": host, "user": "eolu"})

    def test_builds_ssh_command_without_a_shell(self) -> None:
        command = server.ssh_command(
            {"host": "studio.local", "user": "eolu", "port": 2222}
        ) if server.shutil.which("ssh") else None
        if command:
            self.assertIn("eolu@studio.local", command)
            self.assertIn("2222", command)
            self.assertEqual(command[-1], "eolu@studio.local")

    def test_profiles_default_to_a_local_shell(self) -> None:
        profile = server.profile_from_payload({"name": "Work", "kind": "local"})
        self.assertEqual(profile["kind"], "local")
        self.assertEqual(profile["name"], "Work")

    def test_profile_rejects_bad_kind(self) -> None:
        with self.assertRaises(ValueError):
            server.profile_from_payload({"kind": "telnet", "name": "bad"})

    def test_websocket_token_is_not_empty(self) -> None:
        self.assertGreaterEqual(len(server.WS_TOKEN), 32)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import tempfile
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

    def test_profile_rejects_unsafe_ssh_fields(self) -> None:
        for payload in (
            {"kind": "ssh", "name": "bad", "host": "host;rm", "user": "eolu"},
            {"kind": "ssh", "name": "bad", "host": "studio", "user": "bad user"},
            {"kind": "ssh", "name": "bad", "host": "studio", "user": "eolu", "port": 70000},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    server.profile_from_payload(payload)

    def test_profile_requires_an_existing_identity_file(self) -> None:
        with self.assertRaises(ValueError):
            server.profile_from_payload({
                "kind": "ssh",
                "name": "studio",
                "host": "studio.local",
                "user": "eolu",
                "identity_file": "/definitely/not/here",
            })

    def test_profile_rejects_control_characters_in_name(self) -> None:
        with self.assertRaises(ValueError):
            server.profile_from_payload({"kind": "local", "name": "bad\x00name"})

    def test_ssh_command_uses_identity_and_identities_only(self) -> None:
        if not server.shutil.which("ssh"):
            self.skipTest("OpenSSH client is not installed")
        with tempfile.NamedTemporaryFile() as identity:
            command = server.ssh_command({
                "host": "studio.local",
                "user": "eolu",
                "identity_file": identity.name,
            })
        self.assertIn("-i", command)
        self.assertIn("IdentitiesOnly=yes", command)
        self.assertEqual(command[-1], "eolu@studio.local")

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


if __name__ == "__main__":
    unittest.main()

# Terminal

A persistent terminal for Orqella: local shells and SSH sessions with a
mobile-first UI built around xterm.js.

## What it does

- Starts real PTYs for local shells on Linux, macOS, and Windows.
- Keeps sessions alive when the page closes, the phone locks, or the connection
  drops. Reopening the page replays the recent output and reattaches.
- Supports multiple sessions, a saved-connection drawer, and a Termius-style
  mobile key bar (Esc, Tab, arrows, Ctrl/Alt, common symbols, paste, keyboard).
- Connects to SSH hosts using the host's OpenSSH client, config, agent, and
  `known_hosts`. Passwords are never stored.
- Uses the selected Orqella theme for chrome and terminal colors.

## Install

```bash
orqella module install ~/Projects/orqella/modules/terminal
```

Then open **Terminal** from the dashboard sidebar, or enable it in
Settings → Terminal.

The sidecar needs Python 3 on the host. On Windows, install the optional PTY
dependency once:

```powershell
python -m pip install pywinpty
```

Linux and macOS use the standard library `pty` module and need no extra
packages.

## Connections

Saved connections live in the module's `data/profiles.json`. SSH profiles store
the host, user, port, and an optional identity-file path; they never store a
password or private key. Identity-file paths are resolved on the terminal host,
not on the phone.

The mobile drawer is meant for quick switching. Use **Add SSH connection** in
the drawer or the module's Settings tab to manage presets.

## Mobile

- The terminal page is fixed-height and avoids body scrolling.
- The key bar sits above the on-screen keyboard and respects safe areas.
- Tap the terminal to focus; use the ⌨ key to reopen the keyboard.
- Use **Fit** after rotating, changing font size, or opening the keyboard.
- Copy/paste uses the clipboard where the WebView allows it, with a prompt
  fallback.

## Security

This module runs unsandboxed (`backend.sandbox = false`) because it must spawn
the user's shell, SSH client, and PTY. Treat it like any local terminal. The
sidecar strips the Orqella module token from the child environment. It binds
only loopback and authenticates both HTTP and WebSocket requests with the module
token supplied by the daemon.

## Vendored UI

xterm.js 5.5.0 and the fit, web-links, and Unicode 11 addons are vendored under
`ui/vendor/xterm` under their MIT licenses. No CDN is used.

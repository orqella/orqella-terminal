# Terminal

A persistent local terminal for Orqella with a mobile-first tabbed UI built
around xterm.js.

## What it does

- Starts real PTYs for local shells on Linux, macOS, and Windows.
- Keeps tabs alive when the page closes, the phone locks, or the connection
  drops. Reopening the page replays recent output and reattaches.
- Supports multiple local tabs from a compact top bar and a Termius-style mobile
  key bar (Esc, Tab, arrows, Ctrl/Alt, common symbols, paste, keyboard).
- Uses the selected Orqella theme for chrome and terminal colors. The module
  loads `/assets/theme.css` and `/assets/module.css`, then its own CSS for
  terminal-specific layout.

## Install

```bash
orqella module install ~/Projects/orqella/modules/terminal
```

Then open **Terminal** from the dashboard sidebar, or enable it in
Settings → Terminal.

The sidecar declares Python 3.9+ and `backend/requirements.txt`. Orqella finds
a compatible interpreter, creates the module's managed virtual environment on
first start, and installs `pywinpty` there on Windows. Linux and macOS use the
standard-library `pty` module and need no extra packages. If Python is missing,
the module shows a setup error with an install hint instead of failing silently.

## Tabs

Each tab is an independent local shell on the machine running the daemon. Tabs
are not saved across a daemon restart, but they stay attached while the page is
closed. Create a tab with **＋**; close it from the tab strip or the menu. The
page uses its own confirmation dialog because module iframes cannot use the
browser's native `confirm()` modal.

## Mobile

- The terminal page is fixed-height and avoids body scrolling.
- The key bar is mobile-only, sits above the on-screen keyboard, and respects
  safe areas. It keeps Esc, Tab, Ctrl/Alt, arrows, and the keyboard toggle.
- The ⌨ key toggles the on-screen keyboard; it does not only summon it.
- The **⋯** palette adds navigation/function keys and actions such as Paste,
  Copy, Select all, Clear, font sizing, Fit, Reconnect, and Close tab.
- Long-press terminal text to select a word, then drag to extend the selection.
  Use the selection bar to copy or dismiss it. **Select text** is also available
  in the palette.
- Use **Fit** after rotating, changing font size, or opening the keyboard.
- Copy/paste uses the clipboard where the WebView allows it, with a prompt
  fallback.
- The key bar is hidden on desktop; desktop keyboards and mouse selection use
  the normal terminal behavior.

## Security

This module runs unsandboxed (`backend.sandbox = false`) because it must spawn
the user's shell and PTY. Treat it like any local terminal. The sidecar strips
the Orqella module token from the child environment. It binds only loopback and
authenticates both HTTP and WebSocket requests with the module token supplied by
the daemon.

## Vendored UI

xterm.js 5.5.0, its base stylesheet, and the fit, web-links, and Unicode 11
addons are vendored under `ui/vendor/xterm` under their MIT licenses. No CDN is
used. The page loads the vendored xterm stylesheet before the Terminal
stylesheet so xterm's helper and measurement elements stay hidden and its
screen/viewport layout is correct.

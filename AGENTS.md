# Terminal module

The terminal page is a module, not a core Orqella feature. Keep the PTY,
session persistence, and shell selection in this module's sidecar. The daemon
only provides the generic module WebSocket bridge.

- `backend/server.py` owns local PTYs, sessions, and the WebSocket protocol.
- `ui/terminal.js` owns the xterm instance, tab strip, mobile key bar, and
  reconnection behavior.
- This module is local-only: do not add SSH clients, saved connections, or a
  connection drawer.
- Keep child environments free of `ORQELLA_MODULE_TOKEN` and
  `ORQELLA_MODULE_CONFIG`.
- Load `/assets/theme.css` and `/assets/module.css` before the module CSS so the
  selected Orqella theme and core component primitives apply.
- Prefer core classes (`.button`, `.field`, `.card`, `.chip`, `.empty`, `.row`,
  `.muted`, `.small`, `.mono`) before adding module-specific styles.
- Vendor browser dependencies under `ui/vendor/`; do not load a CDN.

# Terminal module

The terminal page is a module, not a core Orqella feature. Keep the PTY,
session persistence, SSH command construction, and profile storage in this
module's sidecar. The daemon only provides the generic module WebSocket bridge.

- `backend/server.py` owns PTYs, sessions, profiles, and the WebSocket protocol.
- `ui/terminal.js` owns the xterm instance, session drawer, mobile key bar, and
  reconnection behavior.
- Keep child environments free of `ORQELLA_MODULE_TOKEN` and
  `ORQELLA_MODULE_CONFIG`.
- Do not add password storage. SSH uses the host's OpenSSH configuration.
- Vendor browser dependencies under `ui/vendor/`; do not load a CDN.

(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
  const api = (path) => window.orqella.call("backend.request", { method: "GET", path });

  async function load() {
    try {
      const status = await api("/api/status");
      const sessions = status.sessions || [];
      $("count").textContent = String(sessions.length);
      $("host").textContent = status.platform || "host";
      if (!sessions.length) {
        $("sessions").innerHTML = `<span class="muted">No open tabs. Open Terminal to start one.</span>`;
        return;
      }
      $("sessions").innerHTML = sessions.slice(0, 3).map((session) => {
        const meta = `${session.command || "shell"} · ${session.cwd || "~"}`;
        return `<div class="session"><span class="dot ${session.status === "exited" ? "dead" : ""}"></span><div><b>${esc(session.title || "Terminal")}</b><br><small>${esc(meta)}</small></div></div>`;
      }).join("");
    } catch (error) {
      $("sessions").innerHTML = `<span class="muted">Terminal backend unavailable</span>`;
    }
  }

  window.orqella.ready(() => {
    load();
    setInterval(load, 15000);
  });
})();

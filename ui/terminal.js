(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const api = (path, method = "GET", body) =>
    window.orqella.call("backend.request", {
      method,
      path,
      ...(body !== undefined ? { body } : {}),
    });
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
  const isMobile = () => matchMedia("(max-width: 760px), (pointer: coarse)").matches;

  const state = {
    context: null,
    config: {},
    status: null,
    sessions: [],
    profiles: [],
    activeId: null,
    term: null,
    fit: null,
    socket: null,
    generation: 0,
    intentionalClose: false,
    retryTimer: null,
    retryDelay: 900,
    fitTimer: null,
    toastTimer: null,
    autoStarted: false,
    fontSize: 14,
    lastSession: "",
    wsToken: "",
    kind: "local",
    ctrl: false,
    alt: false,
  };

  function toast(message, bad = false) {
    const el = $("toast");
    if (!el) return;
    el.textContent = message;
    el.style.borderColor = bad ? "var(--term-danger)" : "";
    el.hidden = false;
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => { el.hidden = true; }, 3200);
  }

  function savePrefs() {
    try {
      window.orqella.call("storage.set", {
        key: "terminal.prefs",
        value: { fontSize: state.fontSize, lastSession: state.lastSession },
      }).catch(() => {});
    } catch (_) {}
  }

  async function loadPrefs() {
    try {
      const prefs = await window.orqella.call("storage.get", { key: "terminal.prefs" });
      if (prefs && typeof prefs === "object") {
        if (Number.isFinite(Number(prefs.fontSize))) state.fontSize = clamp(Number(prefs.fontSize), 10, 24);
        if (typeof prefs.lastSession === "string") state.lastSession = prefs.lastSession;
      }
    } catch (_) {}
  }

  function buildTheme() {
    const css = getComputedStyle(document.documentElement);
    const token = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
    return {
      background: token("--term-bg", "#0a0f13"),
      foreground: token("--term-ink", "#e7f0ee"),
      cursor: token("--term-accent", "#7bd7b6"),
      cursorAccent: token("--term-bg", "#0a0f13"),
      selectionBackground: "rgba(123, 215, 182, 0.28)",
      black: "#10181d",
      red: "#f38ba8",
      green: "#8bd5a4",
      yellow: "#f9e2af",
      blue: "#89b4fa",
      magenta: "#cba6f7",
      cyan: "#89dceb",
      white: "#d9e6e2",
      brightBlack: "#5c7078",
      brightRed: "#ff9eb5",
      brightGreen: "#a6e3b7",
      brightYellow: "#ffe9b8",
      brightBlue: "#a7c8ff",
      brightMagenta: "#e0c4ff",
      brightCyan: "#b4f0fa",
      brightWhite: "#f3fbf8",
    };
  }

  function setupTerminal() {
    state.term = new Terminal({
      allowProposedApi: true,
      cursorBlink: true,
      cursorStyle: String(state.config.cursor_style || "block"),
      fontFamily: getComputedStyle(document.documentElement).getPropertyValue("--term-font").trim()
        || 'ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace',
      fontSize: state.fontSize,
      fontWeight: "400",
      fontWeightBold: "650",
      lineHeight: 1.16,
      letterSpacing: 0,
      scrollback: 10000,
      theme: buildTheme(),
      macOptionIsMeta: true,
      rightClickSelectsWord: true,
      convertEol: false,
    });
    state.fit = new FitAddon.FitAddon();
    state.term.loadAddon(state.fit);
    state.term.loadAddon(new WebLinksAddon.WebLinksAddon());
    try {
      state.term.loadAddon(new Unicode11Addon.Unicode11Addon());
      state.term.unicode.activeVersion = "11";
    } catch (_) {}
    state.term.open($("terminal"));
    state.term.onData((data) => sendInput(data));
    state.term.onResize(() => updateSize());
    state.term.onTitleChange((title) => {
      if (!state.activeId || !title) return;
      const session = state.sessions.find((item) => item.id === state.activeId);
      if (session) {
        session.title = title.trim().slice(0, 80) || session.title;
        $("sessionTitle").textContent = session.title;
        renderSessions();
      }
    });
    requestAnimationFrame(() => fitTerminal());
  }

  function scheduleFit() {
    clearTimeout(state.fitTimer);
    state.fitTimer = setTimeout(fitTerminal, 110);
  }

  function fitTerminal() {
    if (!state.term || !state.fit) return;
    try {
      state.fit.fit();
      updateSize();
      sendResize();
    } catch (_) {}
  }

  function updateSize() {
    if (!state.term) return;
    $("sizeText").textContent = `${state.term.cols} × ${state.term.rows}`;
  }

  function wsUrl(sessionId) {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const match = location.pathname.match(/^(.*\/m\/terminal)\/?/);
    const prefix = match ? `${match[1]}/` : location.pathname.replace(/[^/]*$/, "");
    return `${scheme}://${location.host}${prefix}ws/terminal?session=${encodeURIComponent(sessionId)}&token=${encodeURIComponent(state.wsToken)}`;
  }

  function setConnection(status) {
    const dot = $("connectionDot");
    dot.className = "status-dot " + status;
    const online = status === "online";
    $("statusText").textContent = online
      ? (state.status?.platform ? `${state.status.platform} · connected` : "Connected")
      : status === "busy" ? "Connecting…" : "Disconnected";
  }

  function showBanner(text) {
    $("bannerText").textContent = text;
    $("banner").hidden = false;
  }

  function hideBanner() {
    $("banner").hidden = true;
  }

  function updateIdentity() {
    const session = state.sessions.find((item) => item.id === state.activeId);
    if (!session) {
      $("sessionTitle").textContent = "Terminal";
      $("sessionMeta").textContent = "No session selected";
      return;
    }
    $("sessionTitle").textContent = session.title || "Terminal";
    const meta = session.kind === "ssh"
      ? `${session.user || "user"}@${session.host || "host"}`
      : `${session.command || "shell"} · ${session.cwd || "~"}`;
    const attached = Number(session.attached || 0);
    $("sessionMeta").textContent = `${meta}${attached > 1 ? ` · ${attached} attached` : ""}${session.status === "exited" ? " · exited" : ""}`;
  }

  function renderSessions() {
    const list = $("sessionList");
    if (!state.sessions.length) {
      list.innerHTML = `<div class="empty small">No sessions yet. Start a local shell or SSH connection.</div>`;
      return;
    }
    list.innerHTML = state.sessions.map((session) => {
      const statusClass = session.status === "exited" ? "dead" : "online";
      const meta = session.kind === "ssh"
        ? `${session.user || "user"}@${session.host || "host"}`
        : `${session.command || "shell"} · ${session.cwd || "~"}`;
      return `<div class="session-row ${session.id === state.activeId ? "active" : ""}" data-session="${esc(session.id)}">
        <span class="session-glyph"><span class="status-dot ${statusClass}"></span></span>
        <div class="session-copy"><b>${esc(session.title || "Terminal")}</b><small>${esc(meta)}${session.attached ? " · attached" : ""}</small></div>
        <button class="close-session" data-close-session="${esc(session.id)}" type="button" aria-label="Close session">✕</button>
      </div>`;
    }).join("");
    list.querySelectorAll("[data-session]").forEach((row) => {
      row.onclick = (event) => {
        if (event.target.closest("[data-close-session]")) return;
        attachTo(row.dataset.session, true);
        closeDrawer();
      };
    });
    list.querySelectorAll("[data-close-session]").forEach((button) => {
      button.onclick = (event) => {
        event.stopPropagation();
        closeSession(button.dataset.closeSession);
      };
    });
  }

  function renderProfiles() {
    const list = $("profileList");
    $("profileCount").textContent = state.profiles.length ? String(state.profiles.length) : "";
    const rows = state.profiles.map((profile) => {
      const meta = profile.kind === "ssh"
        ? `${profile.user || "user"}@${profile.host || "host"}${profile.port && profile.port !== 22 ? `:${profile.port}` : ""}`
        : (profile.shell || "local shell");
      return `<div class="profile-row" data-profile="${esc(profile.id)}">
        <span class="profile-icon">${profile.kind === "ssh" ? "⇢" : "›_"}</span>
        <div><b>${esc(profile.name)}</b><small>${esc(meta)}</small></div>
      </div>`;
    }).join("");
    list.innerHTML = rows + `<div class="profile-row" data-add-profile="1"><span class="profile-icon">＋</span><div><b>Add SSH connection</b><small>Use a key, agent, or SSH config</small></div></div>`;
    list.querySelectorAll("[data-profile]").forEach((row) => {
      row.onclick = () => {
        const profile = state.profiles.find((item) => item.id === row.dataset.profile);
        if (profile) {
          createSession(profile.kind === "ssh"
            ? { kind: "ssh", profile_id: profile.id, title: profile.name }
            : { kind: "local", profile_id: profile.id, title: profile.name });
          closeDrawer();
        }
      };
    });
    list.querySelector("[data-add-profile]").onclick = () => openNewSheet("ssh");
  }

  function renderStatus() {
    if (!state.status) return;
    const shells = (state.status.shells || []).length;
    const ssh = state.status.capabilities?.ssh ? "ssh ready" : "ssh unavailable";
    $("platformLabel").textContent = `${state.status.platform} · ${shells} shell${shells === 1 ? "" : "s"} · ${ssh}`;
    if (state.status.capabilities?.windows_pty === false) {
      $("drawerHint").textContent = "Install pywinpty on this Windows host to enable terminals.";
    }
    const select = $("shellSelect");
    const current = select.value;
    select.innerHTML = (state.status.shells || []).map((shell) =>
      `<option value="${esc(shell.id)}">${esc(shell.name)} — ${esc(shell.path)}</option>`).join("");
    if (current && (state.status.shells || []).some((shell) => shell.id === current)) select.value = current;
    else if (state.status.default_shell) select.value = state.status.default_shell;
    const sshDisabled = !state.status.capabilities?.ssh;
    $("connectButton").disabled = false;
    $("connectButton").title = sshDisabled && state.kind === "ssh" ? "OpenSSH is not available on this host" : "";
  }

  function renderAll() {
    renderSessions();
    renderProfiles();
    renderStatus();
    updateIdentity();
  }

  async function refresh() {
    try {
      const result = await api("/api/status");
      state.status = result;
      state.wsToken = result.ws_token || "";
      state.sessions = result.sessions || [];
      state.profiles = result.profiles || [];
      renderAll();
      if (state.activeId && !state.sessions.some((session) => session.id === state.activeId)) {
        state.activeId = null;
      }
      if (!state.activeId) {
        const preferred = state.lastSession && state.sessions.find((session) => session.id === state.lastSession);
        if (preferred) await attachTo(preferred.id, false);
        else if (state.sessions.length) await attachTo(state.sessions[0].id, false);
        else if (state.config.auto_start !== false && !state.autoStarted) {
          state.autoStarted = true;
          await createSession({ kind: "local" }, false);
        } else {
          showEmpty();
        }
      }
    } catch (error) {
      setConnection("dead");
      showBanner("Terminal backend is unavailable");
      $("statusText").textContent = String(error.message || error);
    }
  }

  function showEmpty() {
    $("emptyState").hidden = false;
    $("terminalWrap").style.visibility = "hidden";
    updateIdentity();
  }

  function hideEmpty() {
    $("emptyState").hidden = true;
    $("terminalWrap").style.visibility = "";
  }

  async function createSession(payload, focus = true) {
    try {
      const result = await api("/api/sessions", "POST", payload);
      const session = result.session;
      state.sessions.push(session);
      state.lastSession = session.id;
      savePrefs();
      renderAll();
      hideSheet();
      await attachTo(session.id, focus);
      toast(session.kind === "ssh" ? `Connecting to ${session.title}` : "Local shell opened");
    } catch (error) {
      const message = String(error.message || error);
      if (!$("newSheet").hidden) {
        $("formError").textContent = message;
        $("formError").hidden = false;
      } else {
        toast(message, true);
      }
    }
  }

  function disconnectSocket() {
    state.intentionalClose = true;
    clearTimeout(state.retryTimer);
    state.retryTimer = null;
    if (state.socket) {
      try { state.socket.close(); } catch (_) {}
      state.socket = null;
    }
  }

  async function attachTo(sessionId, focus = true) {
    const session = state.sessions.find((item) => item.id === sessionId);
    if (!session) return;
    if (state.activeId === sessionId && state.socket && state.socket.readyState === WebSocket.OPEN) {
      if (focus) state.term.focus();
      fitTerminal();
      return;
    }
    disconnectSocket();
    state.activeId = sessionId;
    state.lastSession = sessionId;
    state.autoStarted = true;
    savePrefs();
    hideEmpty();
    state.term.reset();
    state.term.clear();
    setConnection("busy");
    hideBanner();
    updateIdentity();
    renderSessions();
    requestAnimationFrame(fitTerminal);
    connectSocket(sessionId, focus);
  }

  async function connectSocket(sessionId, focus) {
    const generation = ++state.generation;
    state.intentionalClose = false;
    clearTimeout(state.retryTimer);
    state.retryTimer = null;
    if (!state.wsToken) {
      try {
        const result = await api("/api/status");
        state.wsToken = result.ws_token || "";
        state.sessions = result.sessions || state.sessions;
      } catch (error) {
        setConnection("dead");
        showBanner("Terminal backend is unavailable");
        return;
      }
    }
    if (generation !== state.generation) return;
    let socket;
    try {
      socket = new WebSocket(wsUrl(sessionId));
    } catch (error) {
      setConnection("dead");
      showBanner(String(error.message || error));
      return;
    }
    socket.binaryType = "arraybuffer";
    state.socket = socket;
    socket.onopen = () => {
      if (generation !== state.generation) return;
      state.retryDelay = 900;
      setConnection("online");
      hideBanner();
      fitTerminal();
      sendResize();
      if (focus && !isMobile()) setTimeout(() => state.term.focus(), 20);
    };
    socket.onmessage = (event) => {
      if (generation !== state.generation) return;
      if (typeof event.data !== "string") {
        state.term.write(new Uint8Array(event.data));
        return;
      }
      let message;
      try { message = JSON.parse(event.data); } catch (_) { return; }
      if (message.type === "ready" && message.session) {
        const updated = message.session;
        const index = state.sessions.findIndex((item) => item.id === updated.id);
        if (index >= 0) state.sessions[index] = { ...state.sessions[index], ...updated };
        updateIdentity();
        renderSessions();
        if (updated.status === "exited") {
          setConnection("dead");
          showBanner("This session has exited. Close it or open a new session.");
        } else {
          setConnection("online");
        }
      } else if (message.type === "exit") {
        setConnection("dead");
        showBanner("Session exited. Close it or open a new session.");
        const current = state.sessions.find((item) => item.id === state.activeId);
        if (current) current.status = "exited";
        renderSessions();
        updateIdentity();
      }
    };
    socket.onerror = () => {
      if (generation !== state.generation) return;
      setConnection("dead");
    };
    socket.onclose = () => {
      if (generation !== state.generation || state.intentionalClose) return;
      const session = state.sessions.find((item) => item.id === state.activeId);
      if (session?.status === "exited") {
        setConnection("dead");
        showBanner("Session exited. Close it or open a new session.");
        return;
      }
      setConnection("busy");
      showBanner(`Connection lost · retrying in ${Math.round(state.retryDelay / 1000)}s`);
      clearTimeout(state.retryTimer);
      state.wsToken = "";
      state.retryTimer = setTimeout(() => {
        if (generation !== state.generation) return;
        state.retryDelay = Math.min(8000, Math.round(state.retryDelay * 1.6));
        connectSocket(sessionId, false);
      }, state.retryDelay);
    };
  }

  function sendJson(payload) {
    if (state.socket && state.socket.readyState === WebSocket.OPEN) {
      state.socket.send(JSON.stringify(payload));
    }
  }

  function sendInput(data) {
    if (!data) return;
    sendJson({ type: "input", data });
  }

  function sendResize() {
    if (!state.term) return;
    sendJson({ type: "resize", cols: state.term.cols, rows: state.term.rows });
  }

  async function closeSession(sessionId) {
    const session = state.sessions.find((item) => item.id === sessionId);
    if (!session) return;
    if (state.config.confirm_close !== false && session.status !== "exited"
      && !confirm(`Close ${session.title || "this session"}?`)) return;
    try {
      await api(`/api/sessions/${encodeURIComponent(sessionId)}`, "DELETE", {});
    } catch (error) {
      toast(String(error.message || error), true);
      return;
    }
    state.sessions = state.sessions.filter((item) => item.id !== sessionId);
    if (state.activeId === sessionId) {
      disconnectSocket();
      state.activeId = null;
      state.term.reset();
      state.term.clear();
      showEmpty();
    }
    renderAll();
    toast("Session closed");
  }

  function openNewSheet(kind = "local") {
    $("newSheet").hidden = false;
    $("sheetBackdrop").hidden = false;
    setKind(kind);
    $("formError").hidden = true;
    $("formError").textContent = "";
    requestAnimationFrame(() => {
      if (kind === "local") $("shellSelect").focus();
    });
  }

  function hideSheet() {
    $("newSheet").hidden = true;
    $("sheetBackdrop").hidden = true;
  }

  function setKind(kind) {
    state.kind = kind;
    document.querySelectorAll(".segmented button").forEach((button) => {
      button.classList.toggle("active", button.dataset.kind === kind);
    });
    $("localFields").hidden = kind !== "local";
    $("sshFields").hidden = kind !== "ssh";
  }

  function openDrawer() {
    $("drawer").classList.add("open");
    $("scrim").hidden = false;
  }

  function closeDrawer() {
    $("drawer").classList.remove("open");
    $("scrim").hidden = true;
  }

  function toggleMenu(force) {
    const show = force ?? $("menuPanel").hidden;
    $("menuPanel").hidden = !show;
    $("menuBackdrop").hidden = !show;
  }

  async function pasteClipboard() {
    let text = "";
    try {
      text = await navigator.clipboard.readText();
    } catch (_) {
      text = prompt("Paste into the terminal:") || "";
    }
    if (text) {
      state.term.paste(text);
      state.term.focus();
    }
  }

  async function copySelection() {
    const selection = state.term.getSelection();
    if (!selection) return toast("Select terminal text first");
    try {
      await navigator.clipboard.writeText(selection);
      toast("Copied");
    } catch (_) {
      prompt("Copy terminal selection:", selection);
    }
  }

  function setFont(delta) {
    state.fontSize = clamp(state.fontSize + delta, 10, 24);
    state.term.options.fontSize = state.fontSize;
    savePrefs();
    fitTerminal();
  }

  function keySequence(def) {
    let data = def.data || def.letter || "";
    if (def.ctrl) {
      const code = def.ctrl.toUpperCase().charCodeAt(0);
      data = String.fromCharCode(code & 31);
    }
    if (state.ctrl && def.letter) {
      data = String.fromCharCode(def.letter.toUpperCase().charCodeAt(0) & 31);
    } else if (state.alt && data) {
      data = `\x1b${data}`;
    }
    if (state.ctrl || state.alt) {
      state.ctrl = false;
      state.alt = false;
      renderKeys();
    }
    sendInput(data);
  }

  const KEY_DEFS = [
    { label: "Esc", data: "\x1b" },
    { label: "Tab", data: "\t" },
    { label: "Ctrl", toggle: "ctrl" },
    { label: "Alt", toggle: "alt" },
    { label: "←", data: "\x1b[D", icon: true },
    { label: "↑", data: "\x1b[A", icon: true },
    { label: "↓", data: "\x1b[B", icon: true },
    { label: "→", data: "\x1b[C", icon: true },
    { label: "Paste", action: "paste", wide: true, secondary: true },
    { label: "⌨", action: "focus", icon: true, secondary: true },
    { label: "/", data: "/", secondary: true },
    { label: "-", data: "-", secondary: true },
    { label: "|", data: "|", secondary: true },
    { label: "~", data: "~", secondary: true },
    { label: "C", letter: "c", secondary: true },
    { label: "D", letter: "d", secondary: true },
    { label: "Z", letter: "z", secondary: true },
    { label: "L", letter: "l", secondary: true },
  ];

  function renderKeys() {
    const render = (bar, defs) => {
      bar.innerHTML = defs.map((def) => {
        const index = KEY_DEFS.indexOf(def);
        const active = (def.toggle === "ctrl" && state.ctrl) || (def.toggle === "alt" && state.alt);
        return `<button class="key ${def.wide ? "wide" : ""} ${def.icon ? "icon" : ""} ${active ? "active" : ""}" type="button" data-key="${index}">${esc(def.label)}</button>`;
      }).join("");
      bar.querySelectorAll("[data-key]").forEach((button) => {
        button.onclick = () => {
          const def = KEY_DEFS[Number(button.dataset.key)];
          if (def.toggle === "ctrl") { state.ctrl = !state.ctrl; renderKeys(); return; }
          if (def.toggle === "alt") { state.alt = !state.alt; renderKeys(); return; }
          if (def.action === "paste") { pasteClipboard(); return; }
          if (def.action === "focus") { state.term.focus(); return; }
          keySequence(def);
        };
      });
    };
    render($("keysPrimary"), KEY_DEFS.filter((def) => !def.secondary));
    render($("keysSecondary"), KEY_DEFS.filter((def) => def.secondary));
  }

  function bindUi() {
    $("newButton").onclick = () => openNewSheet("local");
    $("quickLocal").onclick = () => createSession({ kind: "local" });
    $("emptyNew").onclick = () => openNewSheet("local");
    $("drawerToggle").onclick = () => {
      if ($("drawer").classList.contains("open")) closeDrawer();
      else openDrawer();
    };
    $("scrim").onclick = closeDrawer;
    $("sheetBackdrop").onclick = hideSheet;
    $("closeSheet").onclick = hideSheet;
    $("cancelSheet").onclick = hideSheet;
    $("menuBackdrop").onclick = () => toggleMenu(false);
    $("menuButton").onclick = () => toggleMenu();
    $("fitButton").onclick = fitTerminal;
    $("reconnectButton").onclick = () => {
      if (state.activeId) connectSocket(state.activeId, true);
    };
    $("terminal").addEventListener("pointerdown", () => {
      if (!isMobile()) state.term.focus();
    });
    document.querySelectorAll(".segmented button").forEach((button) => {
      button.onclick = () => setKind(button.dataset.kind);
    });
    $("sessionForm").onsubmit = async (event) => {
      event.preventDefault();
      $("formError").hidden = true;
      $("connectButton").disabled = true;
      try {
        if (state.kind === "local") {
          await createSession({
            kind: "local",
            shell: $("shellSelect").value,
            cwd: $("cwdInput").value.trim() || "~",
          });
          return;
        }
        const host = $("sshHost").value.trim();
        const user = $("sshUser").value.trim();
        const port = Number($("sshPort").value) || 22;
        if (!host || !user) throw new Error("Host and user are required.");
        const payload = {
          kind: "ssh",
          host,
          user,
          port,
          identity_file: $("sshIdentity").value.trim(),
          title: $("sshName").value.trim() || `${user}@${host}`,
        };
        if ($("saveProfile").checked) {
          const saved = await api("/api/profiles", "POST", {
            name: payload.title,
            kind: "ssh",
            host,
            user,
            port,
            identity_file: payload.identity_file,
          });
          state.profiles.push(saved.profile);
          payload.profile_id = saved.profile.id;
        }
        await createSession(payload);
      } catch (error) {
        $("formError").textContent = String(error.message || error);
        $("formError").hidden = false;
      } finally {
        $("connectButton").disabled = false;
      }
    };
    $("menuPanel").querySelectorAll("[data-menu]").forEach((button) => {
      button.onclick = () => {
        const action = button.dataset.menu;
        toggleMenu(false);
        if (action === "paste") pasteClipboard();
        else if (action === "copy") copySelection();
        else if (action === "clear") state.term.clear();
        else if (action === "font-down") setFont(-1);
        else if (action === "font-up") setFont(1);
        else if (action === "fit") fitTerminal();
        else if (action === "reconnect") state.activeId && connectSocket(state.activeId, true);
        else if (action === "close") state.activeId && closeSession(state.activeId);
      };
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !$("newSheet").hidden) {
        hideSheet();
      } else if (event.key === "Escape" && !$("menuPanel").hidden) {
        toggleMenu(false);
      } else if (event.key === "Escape" && $("drawer").classList.contains("open")) {
        closeDrawer();
      }
    });
    window.addEventListener("resize", scheduleFit);
    window.addEventListener("orientationchange", scheduleFit);
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", scheduleFit);
    }
    if (window.ResizeObserver) {
      new ResizeObserver(scheduleFit).observe($("terminalWrap"));
    }
  }

  window.orqella.ready(async (context) => {
    state.context = context;
    state.config = context.config || {};
    state.fontSize = clamp(Number(state.config.font_size) || 14, 10, 24);
    setupTerminal();
    bindUi();
    renderKeys();
    setKind("local");
    await loadPrefs();
    state.term.options.fontSize = state.fontSize;
    await refresh();
    requestAnimationFrame(() => {
      fitTerminal();
      if (state.activeId && !isMobile()) state.term.focus();
    });
  });
})();

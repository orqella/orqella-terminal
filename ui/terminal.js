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
    ctrl: false,
    alt: false,
    keyboardOpen: false,
    selectMode: false,
    selectionUiVisible: false,
    touchSelection: null,
    pendingSelection: null,
    longPressTimer: null,
    touchStart: null,
    touchMoved: false,
    pendingCloseId: null,
  };

  function toast(message, bad = false) {
    const el = $("toast");
    if (!el) return;
    el.textContent = message;
    el.style.borderColor = bad ? "var(--red)" : "";
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

  function cssToken(name, fallback) {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  }

  function syncThemeColor() {
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = cssToken("--bg", "#1e1e2e");
  }

  function buildTheme() {
    return {
      background: cssToken("--bg", "#1e1e2e"),
      foreground: cssToken("--ink", "#cdd6f4"),
      cursor: cssToken("--mint", "#89b4fa"),
      cursorAccent: cssToken("--bg", "#1e1e2e"),
      selectionBackground: cssToken("--accent-soft", "rgba(137, 180, 250, .2)"),
      black: cssToken("--bg-inset", "#11111b"),
      red: cssToken("--red", "#f38ba8"),
      green: cssToken("--green", "#a6e3a1"),
      yellow: cssToken("--amber", "#f9e2af"),
      blue: cssToken("--blue", "#89b4fa"),
      magenta: cssToken("--accent-2", "#cba6f7"),
      cyan: cssToken("--mint-dim", "#74c7ec"),
      white: cssToken("--ink", "#cdd6f4"),
      brightBlack: cssToken("--muted-2", "#7f849c"),
      brightRed: cssToken("--red", "#f38ba8"),
      brightGreen: cssToken("--green", "#a6e3a1"),
      brightYellow: cssToken("--amber-2", "#fab387"),
      brightBlue: cssToken("--blue", "#89b4fa"),
      brightMagenta: cssToken("--accent-2", "#cba6f7"),
      brightCyan: cssToken("--mint", "#89b4fa"),
      brightWhite: cssToken("--ink", "#cdd6f4"),
    };
  }

  function setupTerminal() {
    state.term = new Terminal({
      allowProposedApi: true,
      cursorBlink: true,
      cursorStyle: String(state.config.cursor_style || "block"),
      fontFamily: cssToken("--font-mono", 'ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace'),
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
    state.term.onResize(() => sendResize());
    state.term.onSelectionChange(() => updateSelectionBar());
    state.term.onTitleChange((title) => {
      if (!state.activeId || !title) return;
      const session = state.sessions.find((item) => item.id === state.activeId);
      if (!session) return;
      session.title = title.trim().slice(0, 80) || session.title;
      renderTabs();
    });
    state.term.textarea?.addEventListener("focus", () => {
      state.keyboardOpen = true;
      renderKeys();
    });
    state.term.textarea?.addEventListener("blur", () => {
      state.keyboardOpen = false;
      renderKeys();
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
      sendResize();
    } catch (_) {}
  }

  function wsUrl(sessionId) {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const match = location.pathname.match(/^(.*\/m\/terminal)\/?/);
    const prefix = match ? `${match[1]}/` : location.pathname.replace(/[^/]*$/, "");
    return `${scheme}://${location.host}${prefix}ws/terminal?session=${encodeURIComponent(sessionId)}&token=${encodeURIComponent(state.wsToken)}`;
  }

  function showBanner(text) {
    $("bannerText").textContent = text;
    $("banner").hidden = false;
  }

  function hideBanner() {
    $("banner").hidden = true;
  }

  function renderTabs() {
    const tabs = $("tabs");
    if (!state.sessions.length) {
      tabs.innerHTML = "";
      return;
    }
    tabs.innerHTML = state.sessions.map((session) => {
      const active = session.id === state.activeId;
      const status = session.status === "exited" ? " · exited" : "";
      return `<div class="tab ${active ? "active" : ""}" role="tab" tabindex="0" aria-selected="${active}" data-tab="${esc(session.id)}" title="${esc(session.title || "Terminal")}${status}">
        <span class="tab-title">${esc(session.title || "Terminal")}</span>
        <button class="tab-close" type="button" data-close-tab="${esc(session.id)}" aria-label="Close tab">×</button>
      </div>`;
    }).join("");
    tabs.querySelectorAll("[data-tab]").forEach((tab) => {
      tab.onclick = (event) => {
        if (event.target.closest("[data-close-tab]")) return;
        attachTo(tab.dataset.tab, true);
      };
      tab.onkeydown = (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          attachTo(tab.dataset.tab, true);
        }
      };
    });
    tabs.querySelectorAll("[data-close-tab]").forEach((button) => {
      button.onclick = (event) => {
        event.stopPropagation();
        requestCloseSession(button.dataset.closeTab);
      };
    });
    const active = tabs.querySelector(".tab.active");
    if (active) active.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  function renderAll() {
    renderTabs();
  }

  async function refresh() {
    try {
      const result = await api("/api/status");
      state.status = result;
      state.wsToken = result.ws_token || "";
      state.sessions = result.sessions || [];
      if (state.activeId && !state.sessions.some((session) => session.id === state.activeId)) {
        state.activeId = null;
      }
      renderAll();
      if (!state.activeId) {
        const preferred = state.lastSession && state.sessions.find((session) => session.id === state.lastSession);
        if (preferred) await attachTo(preferred.id, false);
        else if (state.sessions.length) await attachTo(state.sessions[0].id, false);
        else if (state.config.auto_start !== false && !state.autoStarted) {
          state.autoStarted = true;
          await createSession({}, false);
        } else {
          showEmpty();
        }
      }
    } catch (error) {
      showBanner("Terminal backend is unavailable");
      toast(String(error.message || error), true);
    }
  }

  function showEmpty() {
    exitSelectionMode(true);
    $("emptyState").hidden = false;
    $("terminalWrap").style.visibility = "hidden";
    renderTabs();
  }

  function hideEmpty() {
    $("emptyState").hidden = true;
    $("terminalWrap").style.visibility = "";
  }

  async function createSession(payload = {}, focus = true) {
    try {
      const result = await api("/api/sessions", "POST", {
        cols: state.term?.cols || 80,
        rows: state.term?.rows || 24,
        ...payload,
      });
      const session = result.session;
      state.sessions.push(session);
      state.lastSession = session.id;
      savePrefs();
      hideEmpty();
      renderAll();
      await attachTo(session.id, focus);
      toast("New tab opened");
    } catch (error) {
      if (!state.sessions.length) showEmpty();
      toast(String(error.message || error), true);
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
      renderTabs();
      fitTerminal();
      return;
    }
    disconnectSocket();
    state.activeId = sessionId;
    state.lastSession = sessionId;
    state.autoStarted = true;
    savePrefs();
    hideEmpty();
    exitSelectionMode(true);
    state.term.reset();
    state.term.clear();
    hideBanner();
    renderTabs();
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
        state.sessions = result.sessions || [];
        if (!state.sessions.some((session) => session.id === sessionId)) {
          state.activeId = null;
          showBanner("This tab ended while the terminal was disconnected.");
          renderAll();
          showEmpty();
          return;
        }
      } catch (error) {
        showBanner("Terminal backend is unavailable");
        return;
      }
    }
    if (generation !== state.generation) return;
    let socket;
    try {
      socket = new WebSocket(wsUrl(sessionId));
    } catch (error) {
      showBanner(String(error.message || error));
      return;
    }
    socket.binaryType = "arraybuffer";
    state.socket = socket;
    socket.onopen = () => {
      if (generation !== state.generation) return;
      state.retryDelay = 900;
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
        const updated = { ...message.session, attached: Math.max(1, Number(message.session.attached || 0)) };
        const index = state.sessions.findIndex((item) => item.id === updated.id);
        if (index >= 0) state.sessions[index] = { ...state.sessions[index], ...updated };
        renderTabs();
        if (updated.status === "exited") {
          showBanner("This tab has exited. Close it or open a new tab.");
        } else {
          hideBanner();
        }
      } else if (message.type === "exit") {
        showBanner("Tab exited. Close it or open a new tab.");
        const current = state.sessions.find((item) => item.id === state.activeId);
        if (current) current.status = "exited";
        renderTabs();
      }
    };
    socket.onerror = () => {
      if (generation !== state.generation) return;
      showBanner("Connection lost");
    };
    socket.onclose = () => {
      if (generation !== state.generation || state.intentionalClose) return;
      const session = state.sessions.find((item) => item.id === state.activeId);
      if (session?.status === "exited") {
        showBanner("Tab exited. Close it or open a new tab.");
        return;
      }
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
    if (state.selectMode) exitSelectionMode(true);
    sendJson({ type: "input", data });
  }

  function sendResize() {
    if (!state.term) return;
    sendJson({ type: "resize", cols: state.term.cols, rows: state.term.rows });
  }

  function requestCloseSession(sessionId) {
    const session = state.sessions.find((item) => item.id === sessionId);
    if (!session) return;
    if (state.config.confirm_close === false || session.status === "exited") {
      closeSession(sessionId);
      return;
    }
    state.pendingCloseId = sessionId;
    $("closeText").textContent = `End ${session.title || "this tab"} and its shell?`;
    $("closeBackdrop").hidden = false;
    $("closeDialog").hidden = false;
    setTimeout(() => $("closeCancel").focus(), 0);
  }

  function hideCloseDialog() {
    state.pendingCloseId = null;
    $("closeBackdrop").hidden = true;
    $("closeDialog").hidden = true;
  }

  async function closeSession(sessionId) {
    const session = state.sessions.find((item) => item.id === sessionId);
    if (!session) return;
    try {
      await api(`/api/sessions/${encodeURIComponent(sessionId)}`, "DELETE", {});
    } catch (error) {
      toast(String(error.message || error), true);
      return;
    }
    const index = state.sessions.findIndex((item) => item.id === sessionId);
    state.sessions = state.sessions.filter((item) => item.id !== sessionId);
    if (state.activeId === sessionId) {
      disconnectSocket();
      state.activeId = null;
      state.term.reset();
      state.term.clear();
      const next = state.sessions[Math.min(index, state.sessions.length - 1)];
      if (next) await attachTo(next.id, true);
      else showEmpty();
    }
    renderAll();
    toast("Tab closed");
  }

  function toggleMenu(force) {
    const show = force ?? $("menuPanel").hidden;
    $("menuPanel").hidden = !show;
    $("menuBackdrop").hidden = !show;
    $("menuButton").setAttribute("aria-expanded", String(show));
    if (show) {
      renderPalette();
      setTimeout(() => $("menuClose").focus(), 0);
    }
  }

  function toggleKeyboard() {
    if (!state.term) return;
    if (state.keyboardOpen) {
      state.term.blur();
    } else {
      state.term.focus();
    }
    setTimeout(renderKeys, 40);
  }

  function updateSelectionBar() {
    const bar = $("selectionBar");
    if (!state.selectMode || !state.selectionUiVisible) {
      bar.hidden = true;
      return;
    }
    const text = state.pendingSelection ? pendingSelectionText() : (state.term?.getSelection() || "");
    bar.hidden = false;
    $("selectionInfo").textContent = text
      ? `${text.length} character${text.length === 1 ? "" : "s"} selected`
      : "Drag to select";
    $("selectionCopy").disabled = text.length === 0;
  }

  function pendingSelectionText() {
    const pending = state.pendingSelection;
    if (!pending || !state.term) return "";
    if (pending.kind === "lines") {
      const rows = [];
      for (let row = pending.first; row <= pending.last; row += 1) {
        rows.push(state.term.buffer.active.getLine(row)?.translateToString(true) || "");
      }
      return rows.join("\n");
    }
    const line = state.term.buffer.active.getLine(pending.row)?.translateToString(true) || "";
    return line.slice(pending.start, pending.start + pending.length);
  }

  function applyPendingSelection() {
    const pending = state.pendingSelection;
    if (!pending) return;
    state.pendingSelection = null;
    if (pending.kind === "lines") {
      state.term.selectLines(pending.first, pending.last);
    } else if (pending.length > 0) {
      state.term.select(pending.start, pending.row, pending.length);
    } else {
      state.term.clearSelection();
    }
    updateSelectionBar();
  }

  function showSelectionUi() {
    if (!state.term) return;
    state.selectionUiVisible = true;
    $("app").classList.add("selecting");
    $("terminal").classList.add("selecting");
    updateSelectionBar();
  }

  function enterSelectionMode() {
    if (!state.term) return;
    state.selectMode = true;
    showSelectionUi();
  }

  function exitSelectionMode(clearSelection = true) {
    state.selectMode = false;
    state.selectionUiVisible = false;
    state.touchSelection = null;
    state.pendingSelection = null;
    clearTimeout(state.longPressTimer);
    state.longPressTimer = null;
    $("app").classList.remove("selecting");
    $("terminal").classList.remove("selecting");
    if (clearSelection) state.term?.clearSelection();
    $("selectionBar").hidden = true;
  }

  function touchPoint(event) {
    const touch = event.touches?.[0] || event.changedTouches?.[0];
    return touch ? { x: touch.clientX, y: touch.clientY } : null;
  }

  function cellFromPoint(x, y) {
    const screen = $("terminal")?.querySelector(".xterm-screen");
    if (!screen || !state.term) return null;
    const rect = screen.getBoundingClientRect();
    if (x < rect.left || x > rect.right || y < rect.top || y > rect.bottom) return null;
    const cellWidth = rect.width / state.term.cols;
    const cellHeight = rect.height / state.term.rows;
    return {
      col: clamp(Math.floor((x - rect.left) / cellWidth), 0, state.term.cols - 1),
      row: state.term.buffer.active.viewportY
        + clamp(Math.floor((y - rect.top) / cellHeight), 0, state.term.rows - 1),
    };
  }

  function wordAtCell(cell) {
    const line = state.term?.buffer.active.getLine(cell.row);
    if (!line) return null;
    const text = line.translateToString(true);
    if (!text) return null;
    const col = clamp(cell.col, 0, text.length - 1);
    const separators = String(state.term.options.wordSeparator || " ()[]{}',\"`");
    const isWord = (char) => Boolean(char) && !/\s/.test(char) && !separators.includes(char);
    let start = col;
    let end = col;
    if (isWord(text[col])) {
      while (start > 0 && isWord(text[start - 1])) start -= 1;
      while (end + 1 < text.length && isWord(text[end + 1])) end += 1;
    } else {
      while (start > 0 && !isWord(text[start - 1])) start -= 1;
      while (end + 1 < text.length && !isWord(text[end + 1])) end += 1;
    }
    return { start, end: end + 1, row: cell.row };
  }

  function applySelection(start, row, length) {
    if (length <= 0) {
      state.term.clearSelection();
    } else {
      state.term.select(start, row, length);
    }
    updateSelectionBar();
  }

  function selectLinesBetween(firstRow, lastRow) {
    state.term.selectLines(Math.min(firstRow, lastRow), Math.max(firstRow, lastRow));
    updateSelectionBar();
  }

  function applyTouchSelection(cell) {
    const selection = state.touchSelection;
    if (!selection || !cell) return;
    if (selection.mode === "word" && selection.startWord) {
      const current = wordAtCell(cell);
      if (!current) return;
      if (current.row === selection.startWord.row) {
        const start = Math.min(selection.startWord.start, current.start);
        const end = Math.max(selection.startWord.end, current.end);
        state.pendingSelection = { kind: "range", start, row: current.row, length: end - start };
      } else {
        state.pendingSelection = {
          kind: "lines",
          first: Math.min(selection.startWord.row, current.row),
          last: Math.max(selection.startWord.row, current.row),
        };
      }
      updateSelectionBar();
      return;
    }
    if (cell.row === selection.startCell.row) {
      const start = Math.min(selection.startCell.col, cell.col);
      const end = Math.max(selection.startCell.col, cell.col);
      state.pendingSelection = end > start
        ? { kind: "range", start, row: cell.row, length: end - start }
        : null;
    } else {
      state.pendingSelection = {
        kind: "lines",
        first: Math.min(selection.startCell.row, cell.row),
        last: Math.max(selection.startCell.row, cell.row),
      };
    }
    updateSelectionBar();
  }

  function startWordSelection(cell) {
    const word = wordAtCell(cell);
    state.touchSelection = { startCell: cell, startWord: word, mode: "word" };
    if (word) applySelection(word.start, word.row, word.end - word.start);
    updateSelectionBar();
  }

  function onTerminalTouchStart(event) {
    if (!state.term || event.touches.length !== 1) return;
    const point = touchPoint(event);
    if (!point) return;
    state.touchStart = point;
    state.touchMoved = false;
    clearTimeout(state.longPressTimer);
    state.longPressTimer = null;
    if (state.selectMode) {
      const cell = cellFromPoint(point.x, point.y);
      if (cell) {
        state.pendingSelection = null;
        state.touchSelection = { startCell: cell, startWord: null, mode: "char" };
      }
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    state.longPressTimer = setTimeout(() => {
      state.longPressTimer = null;
      if (state.touchMoved) return;
      const cell = cellFromPoint(point.x, point.y);
      if (!cell) return;
      if (isMobile() && navigator.vibrate) navigator.vibrate(10);
      enterSelectionMode();
      state.pendingSelection = null;
      state.touchSelection = { startCell: cell, startWord: wordAtCell(cell), mode: "word" };
      updateSelectionBar();
    }, 480);
  }

  function onTerminalTouchMove(event) {
    const point = touchPoint(event);
    if (!point) return;
    if (state.touchSelection) {
      const cell = cellFromPoint(point.x, point.y);
      if (cell) applyTouchSelection(cell);
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    if (!state.touchStart) return;
    if (Math.abs(point.x - state.touchStart.x) > 8 || Math.abs(point.y - state.touchStart.y) > 8) {
      state.touchMoved = true;
      clearTimeout(state.longPressTimer);
      state.longPressTimer = null;
    }
  }

  function onTerminalTouchEnd(event) {
    clearTimeout(state.longPressTimer);
    state.longPressTimer = null;
    if (state.touchSelection) {
      const selection = state.touchSelection;
      if (state.pendingSelection) {
        applyPendingSelection();
      } else if (!state.term.hasSelection() && selection.mode === "word" && selection.startWord) {
        state.pendingSelection = {
          kind: "range",
          start: selection.startWord.start,
          row: selection.startWord.row,
          length: selection.startWord.end - selection.startWord.start,
        };
        applyPendingSelection();
      }
      state.touchSelection = null;
      state.pendingSelection = null;
      if (state.selectMode) showSelectionUi();
      event.preventDefault();
      event.stopPropagation();
    }
    state.touchStart = null;
  }

  function onTerminalContextMenu(event) {
    if (!isMobile()) return;
    event.preventDefault();
    event.stopPropagation();
    if (state.selectMode) return;
    const cell = cellFromPoint(event.clientX, event.clientY);
    if (!cell) return;
    state.selectMode = true;
    startWordSelection(cell);
    showSelectionUi();
    state.touchSelection = null;
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

  function selectAllText() {
    enterSelectionMode();
    state.pendingSelection = null;
    state.term.selectAll();
    updateSelectionBar();
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
    { label: "Esc", data: "\x1b", group: "mod" },
    { label: "Tab", data: "\t", group: "mod" },
    { label: "Ctrl", toggle: "ctrl", group: "mod" },
    { label: "Alt", toggle: "alt", group: "mod" },
    { label: "←", data: "\x1b[D", icon: true, group: "arrows" },
    { label: "↑", data: "\x1b[A", icon: true, group: "arrows" },
    { label: "↓", data: "\x1b[B", icon: true, group: "arrows" },
    { label: "→", data: "\x1b[C", icon: true, group: "arrows" },
    { label: "⌨", action: "keyboard", icon: true, group: "system", keyboard: true },
  ];

  const PALETTE_KEYS = [
    { label: "←", data: "\x1b[D" },
    { label: "↑", data: "\x1b[A" },
    { label: "↓", data: "\x1b[B" },
    { label: "→", data: "\x1b[C" },
    { label: "Home", data: "\x1b[H" },
    { label: "End", data: "\x1b[F" },
    { label: "PgUp", data: "\x1b[5~" },
    { label: "PgDn", data: "\x1b[6~" },
    { label: "Esc", data: "\x1b" },
    { label: "Tab", data: "\t" },
    { label: "Enter", data: "\r" },
    { label: "Space", data: " " },
    { label: "⌫", data: "\x7f" },
    { label: "Del", data: "\x1b[3~" },
    { label: "|", data: "|" },
    { label: "~", data: "~" },
    { label: "/", data: "/" },
    { label: "-", data: "-" },
    { label: "_", data: "_" },
    { label: "=", data: "=" },
    { label: "+", data: "+" },
    { label: "*", data: "*" },
    { label: "&", data: "&" },
    { label: ";", data: ";" },
    { label: ":", data: ":" },
    { label: "'", data: "'" },
    { label: "\"", data: "\"" },
    { label: "(", data: "(" },
    { label: ")", data: ")" },
    { label: "[", data: "[" },
    { label: "]", data: "]" },
    { label: "{", data: "{" },
    { label: "}", data: "}" },
    { label: "$", data: "$" },
    { label: "#", data: "#" },
    { label: "!", data: "!" },
    { label: "?", data: "?" },
    { label: "@", data: "@" },
    { label: "%", data: "%" },
    { label: "^", data: "^" },
    { label: "\\", data: "\\" },
    { label: "`", data: "`" },
  ];

  function renderKeys() {
    const bar = $("keysPrimary");
    let html = "";
    let group = null;
    KEY_DEFS.forEach((def) => {
      if (def.group !== group) {
        if (group) html += "</div>";
        html += `<div class="key-group ${def.group}">`;
        group = def.group;
      }
      const index = KEY_DEFS.indexOf(def);
      const active = (def.toggle === "ctrl" && state.ctrl)
        || (def.toggle === "alt" && state.alt)
        || (def.keyboard && state.keyboardOpen);
      const classes = ["key", def.icon ? "icon" : "", def.keyboard ? "keyboard" : "", active ? "active" : ""]
        .filter(Boolean)
        .join(" ");
      const pressed = def.toggle || def.keyboard ? ` aria-pressed="${active}"` : "";
      html += `<button class="${classes}" type="button" data-key="${index}"${pressed}>${esc(def.label)}</button>`;
    });
    if (group) html += "</div>";
    bar.innerHTML = html;
    bar.querySelectorAll("[data-key]").forEach((button) => {
      button.addEventListener("pointerdown", (event) => event.preventDefault());
      button.onclick = () => {
        const def = KEY_DEFS[Number(button.dataset.key)];
        if (def.toggle === "ctrl") { state.ctrl = !state.ctrl; renderKeys(); return; }
        if (def.toggle === "alt") { state.alt = !state.alt; renderKeys(); return; }
        if (def.action === "keyboard") { toggleKeyboard(); return; }
        if (isMobile() && navigator.vibrate) navigator.vibrate(6);
        keySequence(def);
      };
    });
  }

  function renderPalette() {
    const palette = $("keyPalette");
    palette.innerHTML = PALETTE_KEYS.map((def, index) =>
      `<button class="palette-key" type="button" data-palette="${index}" aria-label="${esc(def.label)}">${esc(def.label)}</button>`
    ).join("");
    palette.querySelectorAll("[data-palette]").forEach((button) => {
      button.onclick = () => {
        const def = PALETTE_KEYS[Number(button.dataset.palette)];
        if (isMobile() && navigator.vibrate) navigator.vibrate(6);
        sendInput(def.data);
        toggleMenu(false);
        if (isMobile()) state.term.focus();
      };
    });
  }

  function bindUi() {
    $("newButton").onclick = () => createSession({});
    $("emptyNew").onclick = () => createSession({});
    $("menuBackdrop").onclick = () => toggleMenu(false);
    $("menuButton").onclick = () => toggleMenu();
    $("menuClose").onclick = () => toggleMenu(false);
    $("closeBackdrop").onclick = hideCloseDialog;
    $("closeCancel").onclick = hideCloseDialog;
    $("closeConfirm").onclick = () => {
      const sessionId = state.pendingCloseId;
      hideCloseDialog();
      if (sessionId) closeSession(sessionId);
    };
    $("selectionCopy").onclick = copySelection;
    $("selectionAll").onclick = selectAllText;
    $("selectionDone").onclick = () => exitSelectionMode(true);
    $("reconnectButton").onclick = () => {
      if (state.activeId) connectSocket(state.activeId, true);
    };
    const terminalEl = $("terminal");
    terminalEl.addEventListener("pointerdown", () => {
      if (!isMobile()) state.term.focus();
    });
    terminalEl.addEventListener("touchstart", onTerminalTouchStart, { passive: false, capture: true });
    terminalEl.addEventListener("touchmove", onTerminalTouchMove, { passive: false, capture: true });
    terminalEl.addEventListener("touchend", onTerminalTouchEnd, { passive: false, capture: true });
    terminalEl.addEventListener("touchcancel", onTerminalTouchEnd, { passive: false, capture: true });
    terminalEl.addEventListener("contextmenu", onTerminalContextMenu, { capture: true });
    $("menuPanel").querySelectorAll("[data-menu]").forEach((button) => {
      button.onclick = () => {
        const action = button.dataset.menu;
        toggleMenu(false);
        if (action === "paste") pasteClipboard();
        else if (action === "copy") copySelection();
        else if (action === "select") {
          enterSelectionMode();
          toast("Drag to select terminal text");
        } else if (action === "select-all") selectAllText();
        else if (action === "keyboard") toggleKeyboard();
        else if (action === "clear") state.term.clear();
        else if (action === "font-down") setFont(-1);
        else if (action === "font-up") setFont(1);
        else if (action === "fit") fitTerminal();
        else if (action === "reconnect") state.activeId && connectSocket(state.activeId, true);
        else if (action === "close") state.activeId && requestCloseSession(state.activeId);
      };
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        if (!$("closeDialog").hidden) {
          event.preventDefault();
          hideCloseDialog();
        } else if (!$("menuPanel").hidden) {
          event.preventDefault();
          toggleMenu(false);
        } else if (state.selectMode) {
          event.preventDefault();
          exitSelectionMode(true);
        }
      } else if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === "t") {
        event.preventDefault();
        createSession({});
      } else if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === "w") {
        event.preventDefault();
        if (state.activeId) requestCloseSession(state.activeId);
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

  function watchTheme() {
    window.addEventListener("message", (event) => {
      if (event.source !== parent) return;
      if (event.data?.type !== "orqella:context") return;
      state.config = event.data.context?.config || state.config;
      state.term.options.theme = buildTheme();
      syncThemeColor();
    });
    document.addEventListener("orqella:theme-loaded", () => {
      if (!state.term) return;
      state.term.options.theme = buildTheme();
      syncThemeColor();
    });
  }

  window.orqella.ready(async (context) => {
    state.context = context;
    state.config = context.config || {};
    state.fontSize = clamp(Number(state.config.font_size) || 14, 10, 24);
    setupTerminal();
    bindUi();
    watchTheme();
    syncThemeColor();
    renderKeys();
    if (state.config.mobile_toolbar === false) $("keybar").hidden = true;
    await loadPrefs();
    state.term.options.fontSize = state.fontSize;
    await refresh();
    requestAnimationFrame(() => {
      fitTerminal();
      if (state.activeId && !isMobile()) state.term.focus();
    });
  });
})();

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
  const state = { kind: "local", editing: "", profiles: [] };

  function toast(message, bad = false) {
    $("toast").textContent = message;
    $("toast").style.borderColor = bad ? "var(--red, #f38ba8)" : "";
    $("toast").hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { $("toast").hidden = true; }, 3000);
  }

  function setKind(kind) {
    state.kind = kind;
    document.querySelectorAll(".segmented button").forEach((button) => {
      button.classList.toggle("active", button.dataset.kind === kind);
    });
    document.querySelectorAll("[data-ssh]").forEach((el) => { el.hidden = kind !== "ssh"; });
    document.querySelectorAll("[data-local]").forEach((el) => { el.hidden = kind !== "local"; });
  }

  function resetForm() {
    state.editing = "";
    $("editorTitle").textContent = "Add connection";
    $("profileForm").reset();
    $("port").value = "22";
    $("error").hidden = true;
    setKind("local");
  }

  function editProfile(profile) {
    state.editing = profile.id;
    $("editorTitle").textContent = "Edit connection";
    $("name").value = profile.name || "";
    $("host").value = profile.host || "";
    $("user").value = profile.user || "";
    $("port").value = profile.port || 22;
    $("identity").value = profile.identity_file || "";
    $("shell").value = profile.shell || "";
    $("cwd").value = profile.cwd || "";
    setKind(profile.kind || "local");
    $("error").hidden = true;
    $("profileForm").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function renderProfiles() {
    $("count").textContent = state.profiles.length ? String(state.profiles.length) : "";
    if (!state.profiles.length) {
      $("profiles").innerHTML = `<div class="empty">No saved connections yet. Add a local shell preset or an SSH host.</div>`;
      return;
    }
    $("profiles").innerHTML = state.profiles.map((profile) => {
      const meta = profile.kind === "ssh"
        ? `${profile.user || "user"}@${profile.host || "host"}${profile.port && profile.port !== 22 ? `:${profile.port}` : ""}`
        : (profile.cwd || profile.shell || "local shell");
      return `<article class="profile-card">
        <div><b>${esc(profile.name)}</b><small>${esc(meta)}</small></div>
        <div><button class="delete" data-edit="${esc(profile.id)}" type="button" title="Edit">✎</button><button class="delete" data-delete="${esc(profile.id)}" type="button" title="Delete">✕</button></div>
      </article>`;
    }).join("");
    document.querySelectorAll("[data-edit]").forEach((button) => {
      button.onclick = () => {
        const profile = state.profiles.find((item) => item.id === button.dataset.edit);
        if (profile) editProfile(profile);
      };
    });
    document.querySelectorAll("[data-delete]").forEach((button) => {
      button.onclick = async () => {
        const profile = state.profiles.find((item) => item.id === button.dataset.delete);
        if (!profile || !confirm(`Delete ${profile.name}?`)) return;
        try {
          await api(`/api/profiles/${encodeURIComponent(profile.id)}`, "DELETE", {});
          state.profiles = state.profiles.filter((item) => item.id !== profile.id);
          renderProfiles();
          if (state.editing === profile.id) resetForm();
          toast("Connection deleted");
        } catch (error) {
          toast(String(error.message || error), true);
        }
      };
    });
  }

  async function load() {
    try {
      const status = await api("/api/status");
      state.profiles = status.profiles || [];
      $("platform").textContent = `${status.platform} · ${status.shells?.length || 0} shells · ${status.capabilities?.ssh ? "SSH ready" : "SSH unavailable"}`;
      renderProfiles();
    } catch (error) {
      $("profiles").innerHTML = `<div class="empty">Could not load connections: ${esc(error.message || error)}</div>`;
    }
  }

  function formPayload() {
    const kind = state.kind;
    const payload = {
      id: state.editing || undefined,
      kind,
      name: $("name").value.trim(),
      host: $("host").value.trim(),
      user: $("user").value.trim(),
      port: Number($("port").value) || 22,
      identity_file: $("identity").value.trim(),
      shell: $("shell").value.trim(),
      cwd: $("cwd").value.trim(),
    };
    if (!payload.name) {
      payload.name = kind === "ssh"
        ? `${payload.user || "user"}@${payload.host || "host"}`
        : "Local shell";
    }
    if (kind === "ssh" && (!payload.host || !payload.user)) {
      throw new Error("Host and user are required for SSH.");
    }
    return payload;
  }

  function bind() {
    document.querySelectorAll(".segmented button").forEach((button) => {
      button.onclick = () => setKind(button.dataset.kind);
    });
    $("refresh").onclick = load;
    $("reset").onclick = resetForm;
    $("profileForm").onsubmit = async (event) => {
      event.preventDefault();
      $("error").hidden = true;
      $("save").disabled = true;
      try {
        const payload = formPayload();
        if (state.editing) {
          await api(`/api/profiles/${encodeURIComponent(state.editing)}`, "PUT", payload);
          toast("Connection updated");
        } else {
          const result = await api("/api/profiles", "POST", payload);
          state.profiles.push(result.profile);
          toast("Connection saved");
        }
        resetForm();
        await load();
      } catch (error) {
        $("error").textContent = String(error.message || error);
        $("error").hidden = false;
      } finally {
        $("save").disabled = false;
      }
    };
  }

  window.orqella.ready(() => {
    bind();
    resetForm();
    load();
  });
})();

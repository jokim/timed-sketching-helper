"use strict";

const $ = (sel) => document.querySelector(sel);

const views = {
  start: $("#view-start"),
  session: $("#view-session"),
  done: $("#view-done"),
};

function show(name) {
  for (const [key, el] of Object.entries(views)) el.hidden = key !== name;
}

async function api(path, options) {
  const res = await fetch(path, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || body.detail || `Request failed (${res.status})`);
  return body;
}

// ---- Start view ------------------------------------------------------------

const state = {
  listId: null,
  duration: 90,
  count: 20,
};

async function loadRecent() {
  try {
    const recent = await api("/api/recent");
    const list = $("#recent-urls");
    list.innerHTML = "";
    for (const r of recent) {
      const opt = document.createElement("option");
      opt.value = r.url;
      opt.label = `${r.title} (${r.kind})`;
      list.appendChild(opt);
    }
  } catch {
    /* recent list is a nicety; ignore failures */
  }
}

async function loadAuthStatus() {
  const wrap = $("#da-auth");
  const text = $("#da-auth-text");
  const btn = $("#da-auth-btn");
  try {
    const status = await api("/auth/deviantart/status");
    if (status.connected) {
      text.textContent = status.username
        ? `DeviantArt: connected as ${status.username}.`
        : "DeviantArt: connected.";
      btn.textContent = "Disconnect";
      btn.onclick = async () => {
        await fetch("/auth/deviantart/logout", { method: "POST" });
        loadAuthStatus();
      };
    } else {
      text.textContent =
        "Mature / sensitive images stay blurred until you connect DeviantArt.";
      btn.textContent = "Connect DeviantArt";
      btn.onclick = () => {
        window.location.href = "/auth/deviantart/login";
      };
    }
    wrap.hidden = false;
  } catch {
    wrap.hidden = true;
  }
}

function showAuthReturnMessage() {
  const params = new URLSearchParams(window.location.search);
  const result = params.get("da_auth");
  if (!result) return;
  if (result === "failed") {
    setStartStatus("DeviantArt sign-in failed. Try connecting again.", true);
  } else if (result === "connected") {
    setStartStatus(
      "Connected. Tick “Re-download images” to un-blur an already-fetched list.",
    );
  }
  window.history.replaceState({}, "", window.location.pathname);
}

async function loadPrefs() {
  try {
    const prefs = await api("/api/prefs");
    $("#count").value = prefs.default_count;
    $("#duration").value = prefs.default_duration;
  } catch {
    /* keep the HTML defaults */
  }
}

function setStartStatus(message, isError = false) {
  const el = $("#start-status");
  el.hidden = !message;
  el.textContent = message || "";
  el.classList.toggle("error", isError);
}

$("#start-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const url = $("#url").value.trim();
  const forceRefresh = $("#force-refresh").checked;
  state.count = Number($("#count").value);
  state.duration = Number($("#duration").value);
  const btn = $("#start-btn");
  btn.disabled = true;
  setStartStatus("Fetching list from the source…");
  try {
    const list = await api("/api/lists", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, force_refresh: forceRefresh }),
    });
    state.listId = list.list_id;
    setStartStatus(`Fetched ${list.count} images. Preparing session…`);
    await api("/api/prefs", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ default_count: state.count, default_duration: state.duration }),
    }).catch(() => {});
    await startSession();
  } catch (err) {
    setStartStatus(err.message, true);
  } finally {
    btn.disabled = false;
  }
});

// ---- Session --------------------------------------------------------------

const session = {
  items: [],
  pool: [],
  index: 0,
  remaining: 0,
  paused: false,
  ticker: null,
};

async function startSession() {
  const data = await api("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      list_id: state.listId,
      count: state.count,
      duration: state.duration,
    }),
  });
  session.items = data.items;
  session.pool = data.reroll_pool;
  session.index = 0;
  session.paused = false;
  state.duration = data.duration;
  setStartStatus("");
  show("session");
  renderCurrent();
}

function imageUrl(item) {
  return `/api/images/${encodeURIComponent(item.source_id)}`;
}

function preloadNext() {
  const next = session.items[session.index + 1];
  if (next) new Image().src = imageUrl(next);
}

function renderCurrent() {
  const item = session.items[session.index];
  if (!item) return finishSession();
  $("#stage-img").src = imageUrl(item);
  $("#progress").textContent = `${session.index + 1} / ${session.items.length}`;
  const link = $("#page-link");
  link.href = item.page_url || "#";
  link.style.visibility = item.page_url ? "visible" : "hidden";
  session.remaining = state.duration;
  updateTimer();
  restartTicker();
  preloadNext();
}

function updateTimer() {
  const t = $("#timer");
  t.textContent = String(session.remaining);
  t.classList.toggle("paused", session.paused);
}

function restartTicker() {
  clearInterval(session.ticker);
  session.ticker = setInterval(() => {
    if (session.paused) return;
    session.remaining -= 1;
    if (session.remaining <= 0) {
      next();
    } else {
      updateTimer();
    }
  }, 1000);
}

function next() {
  if (session.index + 1 >= session.items.length) return finishSession();
  session.index += 1;
  renderCurrent();
}

function prev() {
  if (session.index === 0) return;
  session.index -= 1;
  renderCurrent();
}

function togglePause() {
  session.paused = !session.paused;
  $("#controls button[data-action=pause]").textContent = session.paused ? "Resume" : "Pause";
  updateTimer();
}

function reroll() {
  if (session.pool.length === 0) return;
  const swapIn = session.pool.splice(
    Math.floor(Math.random() * session.pool.length),
    1,
  )[0];
  session.pool.push(session.items[session.index]);
  session.items[session.index] = swapIn;
  renderCurrent();
}

function finishSession() {
  clearInterval(session.ticker);
  $("#done-summary").textContent = `You practiced ${session.items.length} images at ${state.duration}s each.`;
  show("done");
}

$("#controls").addEventListener("click", (event) => {
  const action = event.target.dataset.action;
  if (action === "prev") prev();
  else if (action === "skip") next();
  else if (action === "pause") togglePause();
  else if (action === "reroll") reroll();
  else if (action === "end") finishSession();
});

document.addEventListener("keydown", (event) => {
  if (views.session.hidden) return;
  if (event.key === " ") { event.preventDefault(); togglePause(); }
  else if (event.key === "ArrowLeft") prev();
  else if (event.key === "ArrowRight") next();
  else if (event.key === "r" || event.key === "R") reroll();
});

$("#again-btn").addEventListener("click", startSession);
$("#new-btn").addEventListener("click", () => {
  loadRecent();
  loadAuthStatus();
  show("start");
});

// ---- Boot ----------------------------------------------------------------

loadRecent();
loadPrefs();
loadAuthStatus();
showAuthReturnMessage();
show("start");

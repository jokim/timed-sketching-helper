"use strict";

const $ = (sel) => document.querySelector(sel);

const views = {
  start: $("#view-start"),
  session: $("#view-session"),
  done: $("#view-done"),
};

function show(name) {
  for (const [key, el] of Object.entries(views)) el.hidden = key !== name;
  document.body.dataset.view = name;
}

// ---- Toolbar dock position ----------------------------------------------

const DOCK_KEY = "tsh:dock";
const DOCKS = ["top", "left", "right", "bottom"];

function readDock() {
  try {
    return localStorage.getItem(DOCK_KEY) || "top";
  } catch {
    return "top";
  }
}

function setDock(pos) {
  if (!DOCKS.includes(pos)) pos = "top";
  views.session.dataset.dock = pos;
  try {
    localStorage.setItem(DOCK_KEY, pos);
  } catch {
    /* private mode / blocked storage — position still applies for this session */
  }
  for (const b of document.querySelectorAll(".dock button")) {
    b.setAttribute("aria-pressed", String(b.dataset.dock === pos));
  }
}

async function api(path, options) {
  const res = await fetch(path, options);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || body.detail || `Request failed (${res.status})`);
  return body;
}

// ---- Saved URLs (favorites + recent) -------------------------------------
//
// Purely client-side, by design — the URLs you have practised are private and
// never leave the browser. Each store is an array of { url, title, kind }.

const FAV_KEY = "tsh:favorites";
const RECENT_KEY = "tsh:recent";
const RECENT_MAX = 10;

function readStore(key) {
  try {
    const parsed = JSON.parse(localStorage.getItem(key) || "[]");
    return Array.isArray(parsed) ? parsed.filter((e) => e && e.url) : [];
  } catch {
    return [];
  }
}

function writeStore(key, entries) {
  try {
    localStorage.setItem(key, JSON.stringify(entries));
  } catch {
    /* private mode / blocked storage — the lists just won't persist */
  }
}

function isFavorite(url) {
  return readStore(FAV_KEY).some((e) => e.url === url);
}

function rememberRecent(entry) {
  if (!entry || !entry.url) return;
  if (isFavorite(entry.url)) return; // favorites never double up in "Last added"
  const recent = readStore(RECENT_KEY).filter((e) => e.url !== entry.url);
  recent.unshift(entry);
  writeStore(RECENT_KEY, recent.slice(0, RECENT_MAX));
}

function toggleFavorite(url, entry) {
  const favorites = readStore(FAV_KEY);
  const recent = readStore(RECENT_KEY);
  if (favorites.some((e) => e.url === url)) {
    const restored = favorites.find((e) => e.url === url);
    writeStore(FAV_KEY, favorites.filter((e) => e.url !== url));
    writeStore(
      RECENT_KEY,
      [restored, ...recent.filter((e) => e.url !== url)].slice(0, RECENT_MAX),
    );
  } else {
    const promoted = recent.find((e) => e.url === url) || entry || { url, title: url, kind: "" };
    writeStore(FAV_KEY, [promoted, ...favorites.filter((e) => e.url !== url)]);
    writeStore(RECENT_KEY, recent.filter((e) => e.url !== url));
  }
}

function forgetUrl(url) {
  writeStore(FAV_KEY, readStore(FAV_KEY).filter((e) => e.url !== url));
  writeStore(RECENT_KEY, readStore(RECENT_KEY).filter((e) => e.url !== url));
}

function savedThumb(entry) {
  const box = document.createElement("span");
  box.className = "saved-thumb";
  box.textContent = (entry.kind || entry.title || "?").trim().charAt(0).toUpperCase() || "?";
  if (entry.thumb) {
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = "";
    img.src = `/api/images/${encodeURIComponent(entry.thumb)}`;
    img.addEventListener("error", () => img.remove());
    box.appendChild(img);
  }
  return box;
}

function savedRow(entry) {
  const li = document.createElement("li");
  li.className = "saved-row";

  const pick = document.createElement("button");
  pick.type = "button";
  pick.className = "saved-pick";
  pick.dataset.url = entry.url;
  pick.dataset.act = "pick";
  const name = document.createElement("span");
  name.className = "saved-name";
  name.textContent = entry.title || entry.url;
  const sub = document.createElement("span");
  sub.className = "saved-url";
  sub.textContent = entry.kind ? `${entry.kind} · ${entry.url}` : entry.url;
  pick.append(name, sub);

  const favd = isFavorite(entry.url);
  const fav = document.createElement("button");
  fav.type = "button";
  fav.className = "saved-star";
  fav.dataset.url = entry.url;
  fav.dataset.act = "fav";
  fav.setAttribute("aria-pressed", String(favd));
  fav.title = favd ? "Remove from favorites" : "Add to favorites";
  fav.textContent = favd ? "★" : "☆";

  const del = document.createElement("button");
  del.type = "button";
  del.className = "saved-del";
  del.dataset.url = entry.url;
  del.dataset.act = "del";
  del.title = "Remove";
  del.textContent = "✕";

  li.append(savedThumb(entry), pick, fav, del);
  return li;
}

function fillSavedGroup(id, entries) {
  const group = document.getElementById(id);
  const ul = group.querySelector(".saved-list");
  ul.innerHTML = "";
  for (const entry of entries) ul.appendChild(savedRow(entry));
  group.hidden = entries.length === 0;
}

function renderSaved() {
  const favorites = readStore(FAV_KEY);
  const recent = readStore(RECENT_KEY);
  fillSavedGroup("saved-favorites", favorites);
  fillSavedGroup("saved-recent", recent);
  $("#saved").hidden = favorites.length === 0 && recent.length === 0;

  const list = $("#recent-urls");
  list.innerHTML = "";
  for (const r of [...favorites, ...recent]) {
    const opt = document.createElement("option");
    opt.value = r.url;
    opt.label = r.kind ? `${r.title} (${r.kind})` : r.title;
    list.appendChild(opt);
  }
}

$("#saved").addEventListener("click", (event) => {
  const btn = event.target.closest("button");
  if (!btn || !btn.dataset.url) return;
  const { url, act } = btn.dataset;
  if (act === "pick") {
    $("#url").value = url;
    $("#url").focus();
  } else if (act === "fav") {
    toggleFavorite(url);
    renderSaved();
  } else if (act === "del") {
    forgetUrl(url);
    renderSaved();
  }
});

// ---- Start view ------------------------------------------------------------

const state = {
  listId: null,
  listUrl: null,
  listTitle: null,
  listKind: null,
  listThumb: null,
  duration: 90,
  count: 20,
};

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
        "Sensitive / mature images are skipped until you connect DeviantArt.";
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
      "Connected. Tick “Re-download images” to refetch an already-fetched list with sensitive images included.",
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

// ---- List fetch with streamed progress --------------------------------
//
// The backend can't know up front how many API requests a list will take, so
// the bar eases toward — but never reaches — 100%; the point is to show that
// work is still happening. It snaps to 100% when the result line arrives.

function setFetchProgress(state) {
  const wrap = $("#fetch-progress");
  const fill = wrap.querySelector(".fetch-bar span");
  const text = $("#fetch-progress-text");
  if (state === null) {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;
  if (state === "done") {
    fill.style.width = "100%";
    text.textContent = "Ready.";
    return;
  }
  const { requests, images } = state;
  const pct = requests ? (1 - 1 / (1 + requests / 4)) * 100 : 4;
  fill.style.width = Math.min(94, Math.max(4, pct)) + "%";
  const req = `${requests} request${requests === 1 ? "" : "s"}`;
  text.textContent = images
    ? `Fetched ${images} image${images === 1 ? "" : "s"} · ${req}…`
    : `Contacting the source · ${req}…`;
}

async function fetchListStreaming(url, forceRefresh, onProgress) {
  const res = await fetch("/api/lists", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/x-ndjson",
    },
    body: JSON.stringify({ url, force_refresh: forceRefresh }),
  });
  if (!res.ok || !res.body) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || body.detail || `Request failed (${res.status})`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result = null;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, nl).trim();
      buffer = buffer.slice(nl + 1);
      if (!line) continue;
      const msg = JSON.parse(line);
      if (msg.type === "progress") onProgress(msg);
      else if (msg.type === "result") result = msg;
      else if (msg.type === "error") throw new Error(msg.error);
    }
  }
  if (!result) {
    throw new Error("The source stopped responding before the list was ready.");
  }
  return result;
}

$("#start-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const url = $("#url").value.trim();
  const forceRefresh = $("#force-refresh").checked;
  state.count = Number($("#count").value);
  state.duration = Number($("#duration").value);
  const btn = $("#start-btn");
  btn.disabled = true;
  setStartStatus("");
  setFetchProgress({ requests: 0, images: 0 });
  try {
    const list = await fetchListStreaming(url, forceRefresh, setFetchProgress);
    setFetchProgress("done");
    state.listId = list.list_id;
    state.listUrl = url;
    state.listTitle = list.title || url;
    state.listKind = list.kind || "";
    state.listThumb = list.thumb || "";
    rememberRecent({
      url,
      title: state.listTitle,
      kind: state.listKind,
      thumb: state.listThumb,
    });
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
    setFetchProgress(null);
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
  state.duration = data.duration;
  preloaded.clear();
  setStartStatus("");
  show("session");
  resetPauseUI();
  renderCurrent();
}

function resetPauseUI() {
  session.paused = false;
  const lbl = $("#controls button[data-action=pause] .lbl");
  if (lbl) lbl.textContent = "Pause";
  $("#paused-veil").hidden = true;
}

// ---- Zoom & pan ---------------------------------------------------------

const ZOOM_MIN = 1;
const ZOOM_MAX = 8;
const zoomView = { z: 1, tx: 0, ty: 0, dragging: false, sx: 0, sy: 0 };

function clampPan() {
  const stage = $("#stage");
  const maxX = (stage.clientWidth * (zoomView.z - 1)) / 2;
  const maxY = (stage.clientHeight * (zoomView.z - 1)) / 2;
  zoomView.tx = Math.max(-maxX, Math.min(maxX, zoomView.tx));
  zoomView.ty = Math.max(-maxY, Math.min(maxY, zoomView.ty));
}

function applyZoom() {
  if (zoomView.z <= 1.001) {
    zoomView.z = 1;
    zoomView.tx = 0;
    zoomView.ty = 0;
  }
  $("#stage-img").style.transform =
    `translate(${zoomView.tx}px, ${zoomView.ty}px) scale(${zoomView.z})`;
  $("#zoom").hidden = zoomView.z <= 1.001;
  $("#stage").classList.toggle("pannable", zoomView.z > 1.001);
}

function resetZoom() {
  zoomView.z = 1;
  zoomView.tx = 0;
  zoomView.ty = 0;
  zoomView.dragging = false;
  $("#stage").classList.remove("panning");
  applyZoom();
}

function zoomBy(factor, clientX, clientY) {
  const rect = $("#stage").getBoundingClientRect();
  const cx = (clientX ?? rect.left + rect.width / 2) - rect.left - rect.width / 2;
  const cy = (clientY ?? rect.top + rect.height / 2) - rect.top - rect.height / 2;
  const prev = zoomView.z;
  const z = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, prev * factor));
  if (z === prev) return;
  const ratio = z / prev;
  zoomView.z = z;
  zoomView.tx = cx - ratio * (cx - zoomView.tx);
  zoomView.ty = cy - ratio * (cy - zoomView.ty);
  clampPan();
  applyZoom();
}

function initZoomControls() {
  const stage = $("#stage");

  stage.addEventListener(
    "wheel",
    (event) => {
      event.preventDefault();
      zoomBy(event.deltaY < 0 ? 1.18 : 1 / 1.18, event.clientX, event.clientY);
    },
    { passive: false },
  );

  stage.addEventListener("pointerdown", (event) => {
    if (zoomView.z <= 1.001 || event.button !== 0) return;
    zoomView.dragging = true;
    zoomView.sx = event.clientX - zoomView.tx;
    zoomView.sy = event.clientY - zoomView.ty;
    stage.classList.add("panning");
    try {
      stage.setPointerCapture(event.pointerId);
    } catch {
      /* not all pointer types support capture */
    }
  });

  stage.addEventListener("pointermove", (event) => {
    if (!zoomView.dragging) return;
    zoomView.tx = event.clientX - zoomView.sx;
    zoomView.ty = event.clientY - zoomView.sy;
    clampPan();
    applyZoom();
  });

  const endDrag = (event) => {
    if (!zoomView.dragging) return;
    zoomView.dragging = false;
    stage.classList.remove("panning");
    try {
      stage.releasePointerCapture(event.pointerId);
    } catch {
      /* capture may already be gone */
    }
  };
  stage.addEventListener("pointerup", endDrag);
  stage.addEventListener("pointercancel", endDrag);

  $("#zoom-in").addEventListener("click", () => zoomBy(1.4));
  $("#zoom-out").addEventListener("click", () => zoomBy(1 / 1.4));
}

function imageUrl(item) {
  return `/api/images/${encodeURIComponent(item.source_id)}`;
}

// The browser holds decoded bytes for an <img> only while something references
// it, so keep the prefetched Image objects alive until they fall out of range.
const PRELOAD_AHEAD = 3;
const preloaded = new Map(); // source_id -> Image

function preloadAhead() {
  const want = new Map();
  for (let i = 1; i <= PRELOAD_AHEAD; i += 1) {
    const it = session.items[session.index + i];
    if (it) want.set(it.source_id, it);
  }
  for (const id of [...preloaded.keys()]) {
    if (!want.has(id)) preloaded.delete(id);
  }
  for (const [id, it] of want) {
    if (preloaded.has(id)) continue;
    const img = new Image();
    img.src = imageUrl(it);
    preloaded.set(id, img);
  }
}

// Bumped on every image change. A load that resolves for a superseded token
// (rapid prev / skip / reroll) is ignored, so the timer only ever starts for
// the image currently on screen.
let renderToken = 0;

function renderCurrent() {
  const item = session.items[session.index];
  if (!item) return finishSession();

  const token = (renderToken += 1);
  clearInterval(session.ticker);
  session.ticker = null;

  $("#progress").textContent = `${session.index + 1} / ${session.items.length}`;
  const link = $("#page-link");
  link.href = item.page_url || "#";
  link.style.visibility = item.page_url ? "visible" : "hidden";

  resetZoom();

  // Blank the stage and hold the countdown at full until the image has
  // actually decoded: the timer must not run against a picture the user can't
  // see yet, and the previous image must not linger underneath.
  session.remaining = state.duration;
  $("#stage").classList.add("loading");
  const bar = $("#time-bar");
  if (bar) {
    bar.style.transition = "none";
    bar.style.width = "100%";
  }
  updateTimer();

  const img = $("#stage-img");
  img.onload = null;
  img.onerror = null;
  img.src = imageUrl(item);
  // `img.complete` / `naturalWidth` still report the *previous* image for a
  // tick after the src assignment, so they can't gate this — `decode()` tracks
  // the pending request and resolves as soon as the new bytes are ready
  // (near-instant for a prefetched image).
  const settle = () => beginImage(token);
  if (img.decode) {
    img.decode().then(settle, settle);
  } else {
    img.onload = settle;
    img.onerror = settle;
  }

  preloadAhead();
}

// The image for `token` is decoded and painted — start its timer from a clean
// full bar.
function beginImage(token) {
  if (token !== renderToken) return;
  $("#stage").classList.remove("loading");
  session.remaining = state.duration;
  const bar = $("#time-bar");
  if (bar) {
    bar.style.transition = "none";
    bar.style.width = "100%";
    void bar.offsetWidth; // reflow so the shrink animates from full
    bar.style.transition = "";
  }
  updateTimer();
  restartTicker();
}

function formatTime(secs) {
  return Math.floor(secs / 60) + ":" + String(secs % 60).padStart(2, "0");
}

function updateTimer() {
  const t = $("#timer");
  t.textContent = formatTime(Math.max(0, session.remaining));
  t.classList.toggle("paused", session.paused);
  const bar = $("#time-bar");
  if (bar) {
    const pct = Math.max(0, (session.remaining / state.duration) * 100);
    bar.style.width = pct + "%";
  }
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
  const lbl = $("#controls button[data-action=pause] .lbl");
  if (lbl) lbl.textContent = session.paused ? "Resume" : "Pause";
  $("#paused-veil").hidden = !session.paused;
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

function renderFavButton() {
  const btn = $("#fav-btn");
  btn.hidden = !state.listUrl;
  if (!state.listUrl) return;
  const favd = isFavorite(state.listUrl);
  btn.setAttribute("aria-pressed", String(favd));
  btn.textContent = favd ? "★ Saved to favorites" : "☆ Save to favorites";
}

function finishSession() {
  clearInterval(session.ticker);
  $("#done-summary").textContent = `You practiced ${session.items.length} images at ${state.duration}s each.`;
  renderFavButton();
  show("done");
}

function endSession() {
  clearInterval(session.ticker);
  renderSaved();
  loadAuthStatus();
  show("start");
}

$("#fav-btn").addEventListener("click", () => {
  if (!state.listUrl) return;
  toggleFavorite(state.listUrl, {
    url: state.listUrl,
    title: state.listTitle || state.listUrl,
    kind: state.listKind || "",
    thumb: state.listThumb || "",
  });
  renderFavButton();
});

$("#controls").addEventListener("click", (event) => {
  const btn = event.target.closest("button");
  if (!btn) return;
  if (btn.dataset.dock) {
    setDock(btn.dataset.dock);
    return;
  }
  const action = btn.dataset.action;
  if (action === "prev") prev();
  else if (action === "skip") next();
  else if (action === "pause") togglePause();
  else if (action === "reroll") reroll();
  else if (action === "end") endSession();
});

document.addEventListener("keydown", (event) => {
  if (views.session.hidden) return;
  if (event.key === " ") { event.preventDefault(); togglePause(); }
  else if (event.key === "ArrowLeft") prev();
  else if (event.key === "ArrowRight") next();
  else if (event.key === "r" || event.key === "R") reroll();
  else if (event.key === "+" || event.key === "=") zoomBy(1.4);
  else if (event.key === "-" || event.key === "_") zoomBy(1 / 1.4);
});

$("#again-btn").addEventListener("click", startSession);
$("#new-btn").addEventListener("click", () => {
  renderSaved();
  loadAuthStatus();
  show("start");
});

// ---- Boot ----------------------------------------------------------------

setDock(readDock());
initZoomControls();
renderSaved();
loadPrefs();
loadAuthStatus();
showAuthReturnMessage();
show("start");

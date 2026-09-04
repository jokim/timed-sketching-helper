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

// A URL from the API is only safe to drop into an href if it's http(s) — a
// `javascript:` (or `data:`) page_url would otherwise run in our origin when
// the "view on DeviantArt" link is clicked. Returns null for anything else.
function externalHref(url) {
  try {
    const u = new URL(url, window.location.href);
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch {
    return null;
  }
}

// Fill the session caption link with the deviation's title and author, falling
// back to a plain "source" when the API gave us neither. The title goes in its
// own <span> so it can be brighter than the "by <author>" tail.
function setPageLink(link, item) {
  const title = (item.title || "").trim();
  const author = (item.author || "").trim();
  link.textContent = "";
  if (title) {
    const t = document.createElement("span");
    t.className = "page-link-title";
    t.textContent = `“${title}”`;
    link.append(t);
  }
  if (author) link.append(`${title ? " " : ""}by ${author}`);
  if (!title && !author) link.append("source");
  link.append(" ↗");
  link.title = [title && `“${title}”`, author && `by ${author}`]
    .filter(Boolean)
    .join(" ");
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

// ---- Minimized toolbar (icon-only, floating over the image) -------------

const COMPACT_KEY = "tsh:compact";

function readCompact() {
  try {
    return localStorage.getItem(COMPACT_KEY) === "1";
  } catch {
    return false;
  }
}

function setCompact(on) {
  if (on) views.session.dataset.compact = "";
  else delete views.session.dataset.compact;
  try {
    localStorage.setItem(COMPACT_KEY, on ? "1" : "0");
  } catch {
    /* private mode / blocked storage — still applies for this session */
  }
  const btn = $("#controls button[data-action=compact]");
  if (btn) {
    btn.setAttribute("aria-pressed", String(on));
    btn.title = on ? "Expand toolbar" : "Minimize toolbar";
  }
}

// ---- Countdown beep -------------------------------------------------------
// A short, calm tick plays each of the final BEEP_WINDOW seconds before an
// image's timer runs out, so the user notices without being startled. Synthesized
// via Web Audio (no asset file); the AudioContext is created lazily on first
// use, which only happens once a session is running — well after the "Start
// practice" click that satisfies browsers' autoplay-gesture requirement.

const AUDIO_KEY = "tsh:audio";
const BEEP_WINDOW = 5;

let audioCtx = null;

function readAudioEnabled() {
  try {
    return localStorage.getItem(AUDIO_KEY) !== "0";
  } catch {
    return true;
  }
}

function setAudioEnabled(on) {
  try {
    localStorage.setItem(AUDIO_KEY, on ? "1" : "0");
  } catch {
    /* private mode / blocked storage — still applies for this session */
  }
  const btn = $("#controls button[data-action=audio]");
  if (btn) {
    btn.setAttribute("aria-pressed", String(!on));
    btn.title = on ? "Mute countdown beep" : "Unmute countdown beep";
  }
}

function playBeep() {
  if (!audioCtx) {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    audioCtx = new Ctx();
  }
  if (audioCtx.state === "suspended") audioCtx.resume();
  const now = audioCtx.currentTime;
  const osc = audioCtx.createOscillator();
  const gain = audioCtx.createGain();
  osc.type = "sine";
  osc.frequency.value = 800;
  gain.gain.setValueAtTime(0, now);
  gain.gain.linearRampToValueAtTime(0.15, now + 0.015);
  gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.14);
  osc.connect(gain).connect(audioCtx.destination);
  osc.start(now);
  osc.stop(now + 0.15);
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

// The backend titles repeat the kind ("user · gallery · folder", 'Search: "x"')
// which is redundant with the "kind · url" sub-line under each saved row. Strip
// that here, at render time, so already-saved entries clean up too.
function displayTitle(entry) {
  const title = (entry.title || "").trim();
  if (!title) return entry.url;
  if (entry.kind === "search") {
    return title.replace(/^Search:\s*/i, "").replace(/^"(.*)"$/, "$1") || title;
  }
  if (entry.kind === "gallery" || entry.kind === "favourites") {
    const parts = title.split(" · ").filter((p) => p.toLowerCase() !== entry.kind);
    return parts.join(" · ") || title;
  }
  return title;
}

// `entry.thumb` is a source_id resolved through our own image cache;
// `entry.thumb_url` (collections) is already an absolute DeviantArt CDN URL,
// hot-linked directly since it's just a preview and not worth caching.
function savedThumb(entry) {
  const box = document.createElement("span");
  box.className = "saved-thumb";
  box.textContent = (entry.kind || entry.title || entry.name || "?").trim().charAt(0).toUpperCase() || "?";
  const src = entry.thumb_url || (entry.thumb ? `/api/images/${encodeURIComponent(entry.thumb)}` : null);
  if (src) {
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = "";
    img.src = src;
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
  name.textContent = displayTitle(entry);
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

function updateSavedVisibility() {
  const anySaved = readStore(FAV_KEY).length > 0 || readStore(RECENT_KEY).length > 0;
  $("#saved").hidden = !anySaved && $("#saved-collections").hidden;
}

function renderSaved() {
  const favorites = readStore(FAV_KEY);
  const recent = readStore(RECENT_KEY);
  fillSavedGroup("saved-favorites", favorites);
  fillSavedGroup("saved-recent", recent);
  updateSavedVisibility();

  const list = $("#recent-urls");
  list.innerHTML = "";
  for (const r of [...favorites, ...recent]) {
    const opt = document.createElement("option");
    opt.value = r.url;
    opt.label = r.kind ? `${displayTitle(r)} (${r.kind})` : displayTitle(r);
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

// ---- Collections (connected DeviantArt account's favourites folders) ------
//
// Fetched from the backend (it needs the OAuth token), then cached in
// localStorage so reconnecting/reloading doesn't re-hit the API every time.

const COLLECTIONS_KEY = "tsh:collections";
// Matches the default list cache TTL (config.LIST_TTL_HOURS) so a "how fresh
// is this" story stays consistent across the app.
const COLLECTIONS_TTL_MS = 24 * 60 * 60 * 1000;
let connectedUsername = null;

function readCollectionsCache(username) {
  try {
    const cached = JSON.parse(localStorage.getItem(COLLECTIONS_KEY) || "null");
    if (!cached || cached.username !== username) return null;
    if (Date.now() - cached.fetchedAt > COLLECTIONS_TTL_MS) return null;
    return cached.collections;
  } catch {
    return null;
  }
}

function writeCollectionsCache(username, collections) {
  try {
    localStorage.setItem(
      COLLECTIONS_KEY,
      JSON.stringify({ username, fetchedAt: Date.now(), collections }),
    );
  } catch {
    /* private mode / blocked storage — just refetches next time */
  }
}

function setCollectionsStatus(text) {
  const status = $("#saved-collections .saved-status");
  status.textContent = text;
  status.hidden = !text;
}

function collectionRow(entry) {
  const li = document.createElement("li");
  li.className = "saved-row";

  const pick = document.createElement("button");
  pick.type = "button";
  pick.className = "saved-pick";
  pick.dataset.url = entry.url;
  pick.dataset.act = "pick";
  const name = document.createElement("span");
  name.className = "saved-name";
  name.textContent = entry.name;
  pick.appendChild(name);
  if (entry.size != null) {
    const sub = document.createElement("span");
    sub.className = "saved-url";
    sub.textContent = `${entry.size} image${entry.size === 1 ? "" : "s"}`;
    pick.appendChild(sub);
  }

  li.append(savedThumb(entry), pick);
  return li;
}

function renderCollections(collections) {
  const group = $("#saved-collections");
  const ul = group.querySelector(".saved-list");
  ul.innerHTML = "";
  for (const entry of collections) ul.appendChild(collectionRow(entry));
  setCollectionsStatus("");
  group.hidden = false;
  updateSavedVisibility();
}

function clearCollections() {
  connectedUsername = null;
  const group = $("#saved-collections");
  group.hidden = true;
  group.querySelector(".saved-list").innerHTML = "";
  setCollectionsStatus("");
  updateSavedVisibility();
}

async function loadCollections(username, { force = false } = {}) {
  if (!force) {
    const cached = readCollectionsCache(username);
    if (cached) {
      renderCollections(cached);
      return;
    }
  }
  $("#saved-collections").hidden = false;
  updateSavedVisibility();
  setCollectionsStatus("Loading your collections…");
  try {
    const data = await api("/api/deviantart/collections");
    writeCollectionsCache(data.username, data.collections);
    renderCollections(data.collections);
  } catch {
    setCollectionsStatus("Couldn't load collections.");
  }
}

$("#collections-refresh").addEventListener("click", (event) => {
  // Nested inside the <summary>; don't let the click also toggle collapse.
  event.preventDefault();
  event.stopPropagation();
  if (connectedUsername) loadCollections(connectedUsername, { force: true });
});

// ---- Collapsible saved groups -----------------------------------------
//
// Each group is a native <details>; open/closed state persists per group.

const GROUP_OPEN_KEY = "tsh:group-open";

function readGroupOpenState() {
  try {
    const parsed = JSON.parse(localStorage.getItem(GROUP_OPEN_KEY) || "{}");
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

for (const group of document.querySelectorAll(".saved-group")) {
  const state = readGroupOpenState();
  if (Object.prototype.hasOwnProperty.call(state, group.id)) {
    group.open = state[group.id];
  }
  group.addEventListener("toggle", () => {
    const state = readGroupOpenState();
    state[group.id] = group.open;
    try {
      localStorage.setItem(GROUP_OPEN_KEY, JSON.stringify(state));
    } catch {
      /* private mode / blocked storage — collapse state just won't persist */
    }
  });
}

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

// DeviantArt chevron-D mark (simpleicons.org path). Uses currentColor so it
// picks up the button's hover accent for free.
const DA_LOGO_SVG =
  '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
  '<path fill="currentColor" d="M19.207 4.794l.23-.43V0h-4.128l-.436.44-2.02 3.86-.65.44H4.564v5.15h4.847l.436.44-4.847 9.191-.23.43V24h4.128l.436-.44 2.02-3.86.65-.44h7.639v-5.15h-4.847l-.436-.44 4.847-9.191z"/>' +
  "</svg>";

function setAuthBtnLabel(btn, label) {
  btn.replaceChildren();
  btn.insertAdjacentHTML("afterbegin", DA_LOGO_SVG);
  btn.append(Object.assign(document.createElement("span"), { textContent: label }));
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
      setAuthBtnLabel(btn, "Disconnect");
      btn.onclick = async () => {
        await fetch("/auth/deviantart/logout", { method: "POST" });
        clearCollections();
        loadAuthStatus();
      };
      connectedUsername = status.username;
      loadCollections(status.username);
    } else {
      text.textContent =
        "Connect to your DeviantArt account more functionality.";
      setAuthBtnLabel(btn, "Connect DeviantArt");
      btn.onclick = () => {
        window.location.href = "/auth/deviantart/login";
      };
      clearCollections();
    }
    wrap.hidden = false;
  } catch {
    wrap.hidden = true;
    clearCollections();
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

function formatDuration(seconds) {
  const s = Number(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return rem ? `${m}m ${rem}s` : `${m}m`;
}

const rangeSyncs = [];

function initRangeInputs() {
  const bind = (id, format) => {
    const input = $(`#${id}`);
    const out = $(`#${id}-value`);
    const sync = () => {
      out.textContent = format(input.value);
    };
    input.addEventListener("input", sync);
    sync();
    rangeSyncs.push(sync);
  };
  bind("count", (v) => String(v));
  bind("duration", formatDuration);
}

async function loadPrefs() {
  try {
    const prefs = await api("/api/prefs");
    $("#count").value = prefs.default_count;
    $("#duration").value = prefs.default_duration;
    rangeSyncs.forEach((sync) => sync());
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

async function fetchListStreaming(url, forceRefresh, maxImages, maxRequests, onProgress) {
  const body = { url, force_refresh: forceRefresh };
  if (maxImages) body.max_images = maxImages;
  if (maxRequests) body.max_requests = maxRequests;
  const res = await fetch("/api/lists", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/x-ndjson",
    },
    body: JSON.stringify(body),
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
  const maxImagesRaw = $("#max-images").value.trim();
  const maxImages = maxImagesRaw ? Math.max(1, Math.floor(Number(maxImagesRaw))) : null;
  const maxRequestsRaw = $("#max-requests").value.trim();
  const maxRequests = maxRequestsRaw ? Math.max(1, Math.floor(Number(maxRequestsRaw))) : null;
  state.count = Number($("#count").value);
  state.duration = Number($("#duration").value);
  const btn = $("#start-btn");
  btn.disabled = true;
  setStartStatus("");
  setFetchProgress({ requests: 0, images: 0 });
  try {
    const list = await fetchListStreaming(
      url,
      forceRefresh,
      maxImages,
      maxRequests,
      setFetchProgress,
    );
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

// The black-screen countdown: shown before the first image (`start`) and
// between images once the main timer expires, while the next image finishes
// loading underneath. `n` counts down from COUNTDOWN_SECONDS; the big Pause
// button freezes it there until the user resumes.
const COUNTDOWN_SECONDS = 5;
const countdown = {
  active: false,
  paused: false,
  start: false,
  n: COUNTDOWN_SECONDS,
  final: false,
  timer: null,
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
  beginCountdown({ start: true });
}

function resetPauseUI() {
  session.paused = false;
  cancelCountdown();
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
    if (event.target.closest("#zoom")) return;
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
  const pageHref = externalHref(item.page_url);
  link.href = pageHref || "#";
  setPageLink(link, item);
  $("#caption").style.visibility = pageHref ? "visible" : "hidden";

  resetZoom();

  // Blank the stage and hold the countdown at full until the next image is
  // ready: the timer must not run against a picture the user can't see yet,
  // and the previous image must not linger underneath.
  session.remaining = state.duration;
  $("#stage").classList.add("loading");
  const bar = $("#time-bar");
  if (bar) {
    bar.style.transition = "none";
    bar.style.width = "100%";
  }
  updateTimer();

  // A plain `#stage-img.src = url` keeps painting the *previous* image for the
  // whole download, and `img.decode()` right after a src change can resolve
  // against that old frame (the load is deferred to a microtask). So load into
  // a detached Image and only swap the on-screen element once its bytes are
  // decoded — then the visible <img> is never mid-download.
  const url = imageUrl(item);
  const loader = preloaded.get(item.source_id) || new Image();
  if (!loader.src) loader.src = url;

  const reveal = () => {
    if (token !== renderToken) return; // superseded by a newer navigation
    const img = $("#stage-img");
    img.src = url; // already fetched + decoded — this swap can't show a gap
    requestAnimationFrame(() => {
      if (token === renderToken) beginImage(token);
    });
  };
  if (loader.decode) {
    loader.decode().then(reveal, reveal);
  } else if (loader.complete) {
    reveal();
  } else {
    loader.onload = reveal;
    loader.onerror = reveal;
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
  // Swell + pulse the clock over its final three seconds. Re-add the class
  // each tick so the CSS animation replays from the top.
  const ending = session.remaining <= 3 && session.remaining > 0 && !session.paused;
  t.classList.remove("ending");
  if (ending) {
    void t.offsetWidth;
    t.classList.add("ending");
  }
  const bar = $("#time-bar");
  if (bar) {
    const pct = Math.max(0, (session.remaining / state.duration) * 100);
    bar.style.width = pct + "%";
  }
}

// ---- Black-screen countdown (session start + between images) ----------

function showCountdownNumber(n) {
  const el = $("#countdown-number");
  el.textContent = String(n);
  el.classList.remove("tick");
  void el.offsetWidth; // restart the pop animation
  el.classList.add("tick");
}

function setCountdownPauseLabel() {
  const lbl = $("#countdown-pause .lbl");
  if (lbl) lbl.textContent = countdown.paused ? "Resume" : "Pause";
}

function beginCountdown({ start = false } = {}) {
  clearInterval(session.ticker);
  session.ticker = null;
  countdown.active = true;
  countdown.paused = false;
  countdown.start = start;
  countdown.n = COUNTDOWN_SECONDS;
  countdown.final = !start && session.index + 1 >= session.items.length;

  const veil = $("#countdown-veil");
  veil.classList.toggle("final", countdown.final);
  veil.classList.toggle("start", start);
  veil.classList.remove("paused");
  const label = $("#countdown-label");
  label.hidden = !(start || countdown.final);
  label.textContent = start ? "Get ready" : "Final image";
  $("#countdown-prev").hidden = start || session.index === 0;
  setCountdownPauseLabel();
  showCountdownNumber(countdown.n);
  veil.hidden = false;

  // The image we're counting toward is normally already warm (preloadAhead
  // between images; the /api/sessions precache job for the first). Make sure
  // it is at least in flight so the reveal after the count is instant.
  const upcoming = start
    ? session.items[0]
    : countdown.final
      ? null
      : session.items[session.index + 1];
  if (upcoming && !preloaded.has(upcoming.source_id)) {
    const img = new Image();
    img.src = imageUrl(upcoming);
    preloaded.set(upcoming.source_id, img);
  }

  countdown.timer = setInterval(countdownTick, 1000);
}

function countdownTick() {
  if (!countdown.active || countdown.paused) return;
  countdown.n -= 1;
  if (countdown.n >= 1) showCountdownNumber(countdown.n);
  else finishCountdown();
}

function toggleCountdownPause() {
  countdown.paused = !countdown.paused;
  $("#countdown-veil").classList.toggle("paused", countdown.paused);
  setCountdownPauseLabel();
}

// Tear down the countdown without advancing — used when the user navigates
// away (prev / skip / reroll / end) mid-count.
function cancelCountdown() {
  if (!countdown.active) return;
  clearInterval(countdown.timer);
  countdown.timer = null;
  countdown.active = false;
  countdown.paused = false;
  countdown.start = false;
  const veil = $("#countdown-veil");
  veil.hidden = true;
  veil.classList.remove("paused");
}

// The count reached zero: drop the veil and reveal the image it was counting
// toward — the first one at session start, otherwise the next (or the done
// screen when the last image's timer just ran out).
function finishCountdown() {
  const wasStart = countdown.start;
  clearInterval(countdown.timer);
  countdown.timer = null;
  countdown.active = false;
  countdown.paused = false;
  countdown.start = false;
  const veil = $("#countdown-veil");
  veil.hidden = true;
  veil.classList.remove("paused");
  if (wasStart) {
    renderCurrent();
  } else if (session.index + 1 >= session.items.length) {
    finishSession();
  } else {
    session.index += 1;
    renderCurrent();
  }
}

function restartTicker() {
  clearInterval(session.ticker);
  session.ticker = setInterval(() => {
    if (session.paused) return;
    session.remaining -= 1;
    updateTimer();
    if (session.remaining <= 0) beginCountdown();
    else if (session.remaining <= BEEP_WINDOW && readAudioEnabled()) playBeep();
  }, 1000);
}

function next() {
  // During the "Get ready" countdown, skip means "start now", not "skip the
  // first image".
  if (countdown.active && countdown.start) return finishCountdown();
  cancelCountdown();
  if (session.index + 1 >= session.items.length) return finishSession();
  session.index += 1;
  renderCurrent();
}

function prev() {
  if (countdown.active && countdown.start) return finishCountdown();
  cancelCountdown();
  if (session.index === 0) return;
  session.index -= 1;
  renderCurrent();
}

function togglePause() {
  if (countdown.active) return toggleCountdownPause();
  session.paused = !session.paused;
  const lbl = $("#controls button[data-action=pause] .lbl");
  if (lbl) lbl.textContent = session.paused ? "Resume" : "Pause";
  $("#paused-veil").hidden = !session.paused;
  updateTimer();
}

// The reroll pool arrives pre-shuffled from the backend, so taking it from the
// front is exactly as random as picking any index — but keeps the array
// aligned with the backup images the backend has pre-downloaded (see
// BACKUP_POOL_SIZE), so a reroll can swap in an already-cached image. The
// image being swapped out is *not* returned to the pool: once an image has
// been shown, it should never reappear later in the same session.
const BACKUP_POOL_SIZE = 3;

function reroll() {
  cancelCountdown();
  if (session.pool.length === 0) return;
  const swapIn = session.pool.shift();
  session.items[session.index] = swapIn;
  renderCurrent();
  topUpBackupPool();
}

function topUpBackupPool() {
  const ids = session.pool.slice(0, BACKUP_POOL_SIZE).map((it) => it.source_id);
  if (ids.length === 0) return;
  api("/api/precache", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source_ids: ids }),
  }).catch(() => {});
}

function renderFavButton() {
  const btn = $("#fav-btn");
  btn.hidden = !state.listUrl;
  if (!state.listUrl) return;
  const favd = isFavorite(state.listUrl);
  btn.setAttribute("aria-pressed", String(favd));
  btn.setAttribute(
    "aria-label",
    favd ? "Remove this reference from favorites" : "Save this reference to favorites",
  );
  btn.title = favd ? "Saved to favorites" : "Save to favorites";
}

function finishSession() {
  clearInterval(session.ticker);
  cancelCountdown();
  $("#done-summary").textContent = `You practiced ${session.items.length} images at ${state.duration}s each.`;
  renderFavButton();
  show("done");
}

function endSession() {
  clearInterval(session.ticker);
  cancelCountdown();
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

$("#countdown-pause").addEventListener("click", () => {
  if (countdown.active) toggleCountdownPause();
});
$("#countdown-prev").addEventListener("click", () => {
  if (countdown.active) prev();
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
  else if (action === "compact") setCompact(!readCompact());
  else if (action === "audio") setAudioEnabled(!readAudioEnabled());
});

document.addEventListener("keydown", (event) => {
  if (views.session.hidden) return;
  if (event.key === " ") { event.preventDefault(); togglePause(); }
  else if (event.key === "ArrowLeft") prev();
  else if (event.key === "ArrowRight") next();
  else if (event.key === "r" || event.key === "R") reroll();
  else if (event.key === "Escape" || event.key === "q" || event.key === "Q") endSession();
  else if (event.key === "+" || event.key === "=") zoomBy(1.4);
  else if (event.key === "-" || event.key === "_") zoomBy(1 / 1.4);
});

$("#again-btn").addEventListener("click", startSession);
$("#new-btn").addEventListener("click", () => {
  renderSaved();
  loadAuthStatus();
  show("start");
});

// ---- Pointer-idle watcher ----------------------------------------------
//
// Stamps document.body.dataset.activity while the pointer (or keyboard) is
// active and clears it after 2s of stillness. Only the compact toolbar reacts
// to it (in styles.css): it fades away when idle so the reference image is
// unobstructed, and snaps back the instant the mouse moves.

const IDLE_MS = 2000;

function initIdleWatcher() {
  let idle = null;
  const wake = () => {
    if (!("activity" in document.body.dataset)) document.body.dataset.activity = "";
    clearTimeout(idle);
    idle = setTimeout(() => {
      delete document.body.dataset.activity;
    }, IDLE_MS);
  };
  for (const evt of ["mousemove", "pointerdown", "keydown"]) {
    document.addEventListener(evt, wake, { passive: true });
  }
  wake();
}

// ---- Boot ----------------------------------------------------------------

setDock(readDock());
setCompact(readCompact());
setAudioEnabled(readAudioEnabled());
initIdleWatcher();
initZoomControls();
initRangeInputs();
renderSaved();
loadPrefs();
loadAuthStatus();
showAuthReturnMessage();
show("start");

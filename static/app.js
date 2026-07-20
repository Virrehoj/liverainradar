/* Nederbörd — frontend
 *
 * Three independent pieces:
 *   1. Radar animation: preload N frame overlays, show one at a time.
 *      All frames are added to the map with opacity 0 and we only flip
 *      opacities when stepping — this way the browser keeps every decoded
 *      image alive and the animation never flickers or re-downloads.
 *   2. Search: debounced calls to /api/geocode, fly the map to the pick.
 *   3. Forecast panel: /api/forecast for the picked coordinate.
 */

"use strict";

const FRAME_COUNT = 12;          // 12 frames × 5 min = last hour of rain
const FRAME_MS = 500;            // animation step
const LAST_FRAME_DWELL_MS = 1500;
const RADAR_OPACITY = 0.75;
const FORECAST_OPACITY = 0.6;    // slightly lighter: this part is computed, not measured
const REFRESH_MS = 5 * 60 * 1000;

// ---------------------------------------------------------------- map

const map = L.map("map", { zoomControl: false, attributionControl: true })
  .setView([62.4, 16.5], 5);

L.control.zoom({ position: "bottomright" }).addTo(map);

L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
  maxZoom: 19,
  attribution:
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> ' +
    '&copy; <a href="https://carto.com/attributions">CARTO</a> · ' +
    'Radar &amp; forecast: <a href="https://opendata.smhi.se">SMHI</a> · ' +
    'Search: <a href="https://nominatim.org">Nominatim</a>',
}).addTo(map);

const statusEl = document.getElementById("status");
function setStatus(text, isError = false) {
  statusEl.hidden = !text;
  statusEl.textContent = text || "";
  statusEl.classList.toggle("error", isError);
}

// ---------------------------------------------------------------- radar animation

const timelineEl = document.getElementById("timeline");
const scrubber = document.getElementById("scrubber");
const ticksEl = document.getElementById("ticks");
const playBtn = document.getElementById("play-btn");
const frameTimeEl = document.getElementById("frame-time");
const frameDateEl = document.getElementById("frame-date");
const badgeEl = document.getElementById("frame-badge");

let frames = [];        // [{key, valid, url, overlay, loaded}]
let current = -1;
let playing = true;
let playTimer = null;

async function loadFrames() {
  const resp = await fetch(`/api/radar/frames?count=${FRAME_COUNT}`);
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  if (!data.frames.length) throw new Error("SMHI returned no radar frames.");

  // Nothing new? Keep the running animation untouched.
  const keys = data.frames.map((f) => f.key).join();
  if (keys === frames.map((f) => f.key).join()) return;

  // Tear down old overlays, build the new stack.
  frames.forEach((f) => f.overlay && map.removeLayer(f.overlay));
  const wasAtEnd = current === -1 || current >= frames.length - 1;

  frames = data.frames.map((f) => {
    const overlay = L.imageOverlay(f.url, data.bounds, {
      opacity: 0,
      className: "radar-frame",
      interactive: false,
    }).addTo(map);
    const frame = { ...f, overlay, loaded: false };
    overlay.on("load", () => { frame.loaded = true; renderTicks(); });
    return frame;
  });

  scrubber.max = String(frames.length - 1);
  renderTicks();
  timelineEl.hidden = false;
  showFrame(wasAtEnd ? frames.length - 1 : Math.min(current, frames.length - 1));
  setStatus("");
}

function renderTicks() {
  ticksEl.innerHTML = "";
  frames.forEach((f, i) => {
    const tick = document.createElement("i");
    if (f.kind === "forecast") tick.classList.add("forecast");
    if (f.loaded) tick.classList.add("loaded");
    if (i === current) tick.classList.add("active");
    ticksEl.appendChild(tick);
  });
}

function showFrame(index) {
  if (!frames.length) return;
  if (current >= 0 && frames[current]) frames[current].overlay.setOpacity(0);
  current = index;
  const frame = frames[current];
  frame.overlay.setOpacity(frame.kind === "forecast" ? FORECAST_OPACITY : RADAR_OPACITY);
  scrubber.value = String(index);

  // `valid` arrives as explicit UTC ("...Z"); Date converts it to the
  // browser's own timezone, including DST (CEST in summer, CET in winter).
  const t = new Date(frame.valid);
  frameTimeEl.textContent = t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const tzName = new Intl.DateTimeFormat([], { timeZoneName: "short" })
    .formatToParts(t).find((p) => p.type === "timeZoneName")?.value ?? "";
  frameDateEl.textContent =
    t.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" }) +
    (tzName ? ` · ${tzName}` : "");

  if (frame.kind === "forecast") {
    const ahead = frames.filter((f, i) => f.kind === "forecast" && i <= index).length * 5;
    badgeEl.textContent = `FORECAST +${ahead} min`;
    badgeEl.hidden = false;
  } else {
    badgeEl.hidden = true;
  }
  renderTicks();
}

function stepAnimation() {
  const next = (current + 1) % frames.length;
  showFrame(next);
  // Linger both at "now" (last observed frame) and at the end of the loop,
  // so the eye can separate measurement from extrapolation.
  const isLastObserved =
    frames[next].kind === "observed" &&
    (next === frames.length - 1 || frames[next + 1].kind === "forecast");
  const dwell = next === frames.length - 1 || isLastObserved
    ? LAST_FRAME_DWELL_MS : FRAME_MS;
  playTimer = setTimeout(stepAnimation, dwell);
}

function setPlaying(on) {
  playing = on;
  playBtn.classList.toggle("playing", on);
  playBtn.setAttribute("aria-label", on ? "Pause animation" : "Play animation");
  clearTimeout(playTimer);
  if (on && frames.length) playTimer = setTimeout(stepAnimation, FRAME_MS);
}

playBtn.addEventListener("click", () => setPlaying(!playing));
scrubber.addEventListener("input", () => {
  setPlaying(false);
  showFrame(Number(scrubber.value));
});

async function boot() {
  try {
    await loadFrames();
    setPlaying(true);
    showMotion();
  } catch (err) {
    setStatus(`Radar unavailable: ${err.message}`, true);
  }
}
boot();
setInterval(() => loadFrames().then(showMotion).catch(() => {}), REFRESH_MS);

// How the nowcast thinks the rain is moving — shown so the forecast's
// assumption can be checked against what the animation actually shows.
const COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
async function showMotion() {
  try {
    const resp = await fetch("/api/radar/motion");
    if (!resp.ok) return;
    const m = await resp.json();
    if (m.method === "none" || m.bearing_deg == null || m.speed_kmh < 2) {
      setStatus("Nowcast: too little rain to track motion");
      return;
    }
    const dir = COMPASS[Math.round(m.bearing_deg / 45) % 8];
    setStatus(`Nowcast basis: rain drifting ${dir} at ~${Math.round(m.speed_kmh)} km/h ` +
              `(${m.valid_windows} tracked windows)`);
  } catch { /* diagnostics are optional */ }
}

// ---------------------------------------------------------------- search

const searchInput = document.getElementById("search-input");
const resultsEl = document.getElementById("search-results");
let searchTimer = null;
let marker = null;

searchInput.addEventListener("input", () => {
  clearTimeout(searchTimer);
  const q = searchInput.value.trim();
  if (q.length < 2) { resultsEl.hidden = true; return; }
  searchTimer = setTimeout(() => runSearch(q), 300);
});

searchInput.addEventListener("keydown", (e) => {
  if (e.key === "Escape") resultsEl.hidden = true;
  if (e.key === "Enter") {
    const first = resultsEl.querySelector("li[data-lat]");
    if (first) first.click();
  }
});

document.addEventListener("click", (e) => {
  if (!e.target.closest(".search")) resultsEl.hidden = true;
});

async function runSearch(q) {
  const resp = await fetch(`/api/geocode?q=${encodeURIComponent(q)}`);
  const data = await resp.json();
  resultsEl.innerHTML = "";
  const items = data.results || [];
  if (!items.length) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = resp.ok ? "No places found." : (data.error || "Search failed.");
    resultsEl.appendChild(li);
  }
  for (const item of items) {
    const li = document.createElement("li");
    li.tabIndex = 0;
    li.dataset.lat = item.lat;
    li.textContent = item.name;
    li.addEventListener("click", () => pickPlace(item));
    li.addEventListener("keydown", (e) => { if (e.key === "Enter") pickPlace(item); });
    resultsEl.appendChild(li);
  }
  resultsEl.hidden = false;
}

function pickPlace(place) {
  resultsEl.hidden = true;
  searchInput.value = place.name.split(",")[0];
  if (marker) map.removeLayer(marker);
  marker = L.marker([place.lat, place.lon]).addTo(map);
  map.flyTo([place.lat, place.lon], Math.max(map.getZoom(), 8), { duration: 1.1 });
  openForecast(place);
}

// ---------------------------------------------------------------- forecast panel

const panel = document.getElementById("panel");
document.getElementById("panel-close").addEventListener("click", () => { panel.hidden = true; });

// SMHI Wsymb2 weather symbols, 1–27.
const SYMBOLS = {
  1: ["☀️", "Clear sky"], 2: ["🌤️", "Nearly clear"], 3: ["⛅", "Variable clouds"],
  4: ["⛅", "Half-clear"], 5: ["🌥️", "Cloudy"], 6: ["☁️", "Overcast"],
  7: ["🌫️", "Fog"], 8: ["🌦️", "Light rain showers"], 9: ["🌦️", "Rain showers"],
  10: ["🌧️", "Heavy rain showers"], 11: ["⛈️", "Thunderstorm"],
  12: ["🌨️", "Light sleet showers"], 13: ["🌨️", "Sleet showers"], 14: ["🌨️", "Heavy sleet showers"],
  15: ["🌨️", "Light snow showers"], 16: ["🌨️", "Snow showers"], 17: ["❄️", "Heavy snow showers"],
  18: ["🌦️", "Light rain"], 19: ["🌧️", "Rain"], 20: ["🌧️", "Heavy rain"],
  21: ["🌩️", "Thunder"], 22: ["🌨️", "Light sleet"], 23: ["🌨️", "Sleet"], 24: ["🌨️", "Heavy sleet"],
  25: ["🌨️", "Light snowfall"], 26: ["❄️", "Snowfall"], 27: ["❄️", "Heavy snowfall"],
};

async function openForecast(place) {
  panel.hidden = false;
  document.getElementById("panel-place").textContent = place.name.split(",").slice(0, 2).join(",");
  document.getElementById("hours").innerHTML = "";
  document.getElementById("now-desc").textContent = "Loading…";

  const resp = await fetch(`/api/forecast?lat=${place.lat}&lon=${place.lon}`);
  const data = await resp.json();
  if (!resp.ok) {
    document.getElementById("now-desc").textContent = data.error || "Forecast failed.";
    document.getElementById("now-temp").textContent = "–°";
    document.getElementById("now-symbol").textContent = "";
    document.getElementById("now-wind").textContent = "";
    return;
  }

  const series = data.series || [];
  const now = series[0] || {};
  const [emoji, label] = SYMBOLS[now.symbol] || ["", ""];
  document.getElementById("now-symbol").textContent = emoji;
  document.getElementById("now-temp").textContent =
    now.temp != null ? `${Math.round(now.temp)}°` : "–°";
  document.getElementById("now-desc").textContent = label;
  document.getElementById("now-wind").textContent =
    now.wind != null ? `Wind ${Math.round(now.wind)} m/s · Humidity ${now.humidity ?? "–"}%` : "";

  const hoursEl = document.getElementById("hours");
  const maxPrecip = Math.max(1, ...series.map((p) => p.precip || 0));
  for (const point of series.slice(1, 13)) {
    const [hEmoji] = SYMBOLS[point.symbol] || [""];
    const li = document.createElement("li");
    const time = new Date(point.time)
      .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const pct = Math.round(((point.precip || 0) / maxPrecip) * 100);
    li.innerHTML =
      `<span class="h-time">${time}</span>` +
      `<span class="h-symbol">${hEmoji}</span>` +
      `<span class="h-temp">${point.temp != null ? Math.round(point.temp) + "°" : "–"}</span>` +
      `<span class="h-precip" title="${point.precip ?? 0} mm/h"><span style="width:${pct}%"></span></span>`;
    hoursEl.appendChild(li);
  }
}

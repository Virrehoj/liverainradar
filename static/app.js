/* Nederbörd — frontend
 *
 * Independent pieces:
 *   1. Radar animation: preload N frame overlays, show one at a time.
 *      All frames are added to the map with opacity 0 and we only flip
 *      opacities when stepping — this way the browser keeps every decoded
 *      image alive and the animation never flickers or re-downloads.
 *   2. Theme: light/dark toggle, persisted, swaps the basemap tile set too.
 *   3. Search: debounced calls to /api/geocode, fly the map to the pick,
 *      plus a small "recent places" list kept in localStorage.
 *   4. Geolocation: "use my location" shortcut into the same pick flow.
 *   5. Forecast panel: /api/forecast for the picked coordinate.
 */

"use strict";

const LOCALE = "sv-SE";
const FRAME_COUNT = 12;          // 12 frames × 5 min = last hour of rain
const FRAME_MS = 500;            // animation step
const LAST_FRAME_DWELL_MS = 1500;
const RADAR_OPACITY = 0.75;
const FORECAST_OPACITY = 0.6;    // slightly lighter: this part is computed, not measured
const REFRESH_MS = 5 * 60 * 1000;
const COMPASS = ["N", "NO", "O", "SO", "S", "SV", "V", "NV"];

function compassFromDeg(deg) {
  return COMPASS[Math.round(deg / 45) % 8];
}

// ---------------------------------------------------------------- map

const LIGHT_TILES = "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png";
const DARK_TILES = "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png";

const map = L.map("map", { zoomControl: false, attributionControl: true })
  .setView([62.4, 16.5], 5);

L.control.zoom({ position: "bottomright" }).addTo(map);

const tileLayer = L.tileLayer(LIGHT_TILES, {
  maxZoom: 19,
  attribution:
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> ' +
    '&copy; <a href="https://carto.com/attributions">CARTO</a> · ' +
    'Radar och prognos: <a href="https://opendata.smhi.se">SMHI</a> · ' +
    'Sök: <a href="https://nominatim.org">Nominatim</a>',
}).addTo(map);

const statusEl = document.getElementById("status");
function setStatus(text, isError = false) {
  statusEl.hidden = !text;
  statusEl.textContent = text || "";
  statusEl.classList.toggle("error", isError);
}

// ---------------------------------------------------------------- theme

const THEME_KEY = "nederbord-theme";
const themeBtn = document.getElementById("theme-btn");

function currentTheme() {
  return localStorage.getItem(THEME_KEY) || "light";
}
function applyTheme(name) {
  if (name === "dark") document.documentElement.setAttribute("data-theme", "dark");
  else document.documentElement.removeAttribute("data-theme");
  tileLayer.setUrl(name === "dark" ? DARK_TILES : LIGHT_TILES);
  localStorage.setItem(THEME_KEY, name);
}
themeBtn.addEventListener("click", () => applyTheme(currentTheme() === "dark" ? "light" : "dark"));
applyTheme(currentTheme());

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
  if (!data.frames.length) throw new Error("SMHI returnerade inga radarbilder.");

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
  frameTimeEl.textContent = t.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
  const tzName = new Intl.DateTimeFormat(LOCALE, { timeZoneName: "short" })
    .formatToParts(t).find((p) => p.type === "timeZoneName")?.value ?? "";
  frameDateEl.textContent =
    t.toLocaleDateString(LOCALE, { weekday: "short", day: "numeric", month: "short" }) +
    (tzName ? ` · ${tzName}` : "");

  if (frame.kind === "forecast") {
    const ahead = frames.filter((f, i) => f.kind === "forecast" && i <= index).length * 5;
    badgeEl.textContent = `PROGNOS +${ahead} min`;
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
  playBtn.setAttribute("aria-label", on ? "Pausa animering" : "Spela upp animering");
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
    setStatus(`Radar otillgänglig: ${err.message}`, true);
  }
}
boot();
setInterval(() => loadFrames().then(showMotion).catch(() => {}), REFRESH_MS);

// How the nowcast thinks the rain is moving — shown so the forecast's
// assumption can be checked against what the animation actually shows.
async function showMotion() {
  try {
    const resp = await fetch("/api/radar/motion");
    if (!resp.ok) return;
    const m = await resp.json();
    if (m.method === "none" || m.bearing_deg == null || m.speed_kmh < 2) {
      setStatus("Prognos: för lite regn för att följa rörelsen");
      return;
    }
    const dir = compassFromDeg(m.bearing_deg);
    setStatus(`Prognosunderlag: regnet rör sig mot ${dir} i ~${Math.round(m.speed_kmh)} km/h ` +
              `(${m.valid_windows} spårade fönster)`);
  } catch { /* diagnostics are optional */ }
}

// ---------------------------------------------------------------- search

const searchInput = document.getElementById("search-input");
const resultsEl = document.getElementById("search-results");
const locateBtn = document.getElementById("locate-btn");
let searchTimer = null;
let marker = null;

searchInput.addEventListener("input", () => {
  clearTimeout(searchTimer);
  const q = searchInput.value.trim();
  if (q.length < 2) { resultsEl.hidden = true; renderRecent(); return; }
  recentEl.hidden = true;
  searchTimer = setTimeout(() => runSearch(q), 300);
});

searchInput.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { resultsEl.hidden = true; renderRecent(); }
  if (e.key === "Enter") {
    const first = resultsEl.querySelector("li[data-lat]");
    if (first) first.click();
  }
});

document.addEventListener("click", (e) => {
  if (!e.target.closest(".search")) { resultsEl.hidden = true; renderRecent(); }
});

async function runSearch(q) {
  const resp = await fetch(`/api/geocode?q=${encodeURIComponent(q)}`);
  const data = await resp.json();
  resultsEl.innerHTML = "";
  const items = data.results || [];
  if (!items.length) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = resp.ok ? "Inga platser hittades." : (data.error || "Sökningen misslyckades.");
    resultsEl.appendChild(li);
  }
  for (const item of items) {
    const li = document.createElement("li");
    li.tabIndex = 0;
    li.dataset.lat = item.lat;
    li.textContent = item.name;
    li.addEventListener("click", () => selectPlace(item));
    li.addEventListener("keydown", (e) => { if (e.key === "Enter") selectPlace(item); });
    resultsEl.appendChild(li);
  }
  resultsEl.hidden = false;
}

function selectPlace(place, { remember = true } = {}) {
  resultsEl.hidden = true;
  searchInput.value = place.name.split(",")[0];
  if (marker) map.removeLayer(marker);
  marker = L.marker([place.lat, place.lon]).addTo(map);
  map.flyTo([place.lat, place.lon], Math.max(map.getZoom(), 8), { duration: 1.1 });
  if (remember) saveRecent(place);
  else renderRecent();
  openForecast(place);
}

// "Use my location" — feeds the same pick flow as a search result.
locateBtn.addEventListener("click", () => {
  if (!navigator.geolocation) {
    setStatus("Geolokalisering stöds inte av den här webbläsaren.", true);
    return;
  }
  locateBtn.classList.add("busy");
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      locateBtn.classList.remove("busy");
      selectPlace({ name: "Min plats", lat: pos.coords.latitude, lon: pos.coords.longitude },
                  { remember: false });
    },
    (err) => {
      locateBtn.classList.remove("busy");
      setStatus(`Kunde inte hämta din plats: ${err.message || "okänt fel"}`, true);
    },
    { enableHighAccuracy: true, timeout: 10000 }
  );
});

// ---------------------------------------------------------------- recent places

const RECENT_KEY = "nederbord-recent";
const recentEl = document.getElementById("recent");

function getRecent() {
  try { return JSON.parse(localStorage.getItem(RECENT_KEY)) || []; }
  catch { return []; }
}
function saveRecent(place) {
  const list = getRecent().filter((p) => p.name !== place.name);
  list.unshift({ name: place.name, lat: place.lat, lon: place.lon });
  localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, 6)));
  renderRecent();
}
function renderRecent() {
  const list = getRecent();
  recentEl.innerHTML = "";
  recentEl.hidden = list.length === 0;
  for (const place of list) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip";
    chip.textContent = place.name.split(",")[0];
    chip.addEventListener("click", () => selectPlace(place));
    recentEl.appendChild(chip);
  }
}
renderRecent();

// ---------------------------------------------------------------- forecast panel

const panel = document.getElementById("panel");
document.getElementById("panel-close").addEventListener("click", () => { panel.hidden = true; });

// SMHI Wsymb2 weather symbols, 1–27.
const SYMBOLS = {
  1: ["☀️", "Klart väder"], 2: ["🌤️", "Lätt molnighet"], 3: ["⛅", "Halvklart väder"],
  4: ["⛅", "Molnigt väder"], 5: ["🌥️", "Tätt molntäcke"], 6: ["☁️", "Mulet väder"],
  7: ["🌫️", "Dimma"], 8: ["🌦️", "Lätta regnskurar"], 9: ["🌦️", "Måttliga regnskurar"],
  10: ["🌧️", "Kraftiga regnskurar"], 11: ["⛈️", "Åska"],
  12: ["🌨️", "Lätta snöblandade regnskurar"], 13: ["🌨️", "Måttliga snöblandade regnskurar"],
  14: ["🌨️", "Kraftiga snöblandade regnskurar"],
  15: ["🌨️", "Lätta snöbyar"], 16: ["🌨️", "Måttliga snöbyar"], 17: ["❄️", "Kraftiga snöbyar"],
  18: ["🌦️", "Lätt regn"], 19: ["🌧️", "Måttligt regn"], 20: ["🌧️", "Kraftigt regn"],
  21: ["🌩️", "Åska"], 22: ["🌨️", "Lätt snöblandat regn"], 23: ["🌨️", "Måttligt snöblandat regn"],
  24: ["🌨️", "Kraftigt snöblandat regn"],
  25: ["🌨️", "Lätt snöfall"], 26: ["❄️", "Måttligt snöfall"], 27: ["❄️", "Kraftigt snöfall"],
};

function addStat(container, label, value) {
  if (value == null) return;
  const div = document.createElement("div");
  div.className = "stat";
  div.innerHTML = `<span class="stat-label">${label}</span><span class="stat-value">${value}</span>`;
  container.appendChild(div);
}

async function openForecast(place) {
  panel.hidden = false;
  document.getElementById("panel-place").textContent = place.name.split(",").slice(0, 2).join(",");
  document.getElementById("hours").innerHTML = "";
  document.getElementById("days").innerHTML = "";
  document.getElementById("now-stats").innerHTML = "";
  document.getElementById("now-feels").textContent = "";
  document.getElementById("sun-row").hidden = true;
  document.getElementById("now-desc").textContent = "Laddar…";

  // The backend groups days in THIS timezone, so "regn på tisdag" means
  // Tuesday where the viewer is, not Tuesday in UTC.
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const resp = await fetch(
    `/api/forecast?lat=${place.lat}&lon=${place.lon}&tz=${encodeURIComponent(tz)}`);
  const data = await resp.json();
  if (!resp.ok) {
    document.getElementById("now-desc").textContent = data.error || "Prognosen kunde inte hämtas.";
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
  document.getElementById("now-feels").textContent =
    now.feelsLike != null && now.temp != null && Math.round(now.feelsLike) !== Math.round(now.temp)
      ? `Känns som ${Math.round(now.feelsLike)}°` : "";

  const windBits = [];
  if (now.wind != null) {
    windBits.push(`Vind ${Math.round(now.wind)} m/s${now.windDir != null ? " " + compassFromDeg(now.windDir) : ""}`);
  }
  if (now.gust != null && now.wind != null && now.gust > now.wind + 1) {
    windBits.push(`byar ${Math.round(now.gust)} m/s`);
  }
  document.getElementById("now-wind").textContent = windBits.join(", ");

  const statsEl = document.getElementById("now-stats");
  addStat(statsEl, "Luftfuktighet", now.humidity != null ? `${Math.round(now.humidity)}%` : null);
  addStat(statsEl, "Lufttryck", now.pressure != null ? `${Math.round(now.pressure)} hPa` : null);
  addStat(statsEl, "Sikt", now.visibility != null ? `${Math.round(now.visibility)} km` : null);
  addStat(statsEl, "Molnighet", now.cloudCover != null ? `${Math.round((now.cloudCover / 8) * 100)}%` : null);

  const sunRow = document.getElementById("sun-row");
  if (data.sun) {
    if (data.sun.polarDay) {
      sunRow.innerHTML = `<span>☀️ Midnattssol — solen går inte ner idag</span>`;
      sunRow.hidden = false;
    } else if (data.sun.polarNight) {
      sunRow.innerHTML = `<span>🌑 Polarnatt — solen går inte upp idag</span>`;
      sunRow.hidden = false;
    } else if (data.sun.sunrise && data.sun.sunset) {
      const rise = new Date(data.sun.sunrise).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
      const set = new Date(data.sun.sunset).toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
      sunRow.innerHTML = `<span>🌅 ${rise}</span><span>🌇 ${set}</span>`;
      sunRow.hidden = false;
    }
  }

  const hoursEl = document.getElementById("hours");
  const maxPrecip = Math.max(1, ...series.map((p) => p.precip || 0));
  for (const point of series.slice(1, 13)) {
    const [hEmoji] = SYMBOLS[point.symbol] || [""];
    const li = document.createElement("li");
    const time = new Date(point.time)
      .toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
    const pct = Math.round(((point.precip || 0) / maxPrecip) * 100);
    const precipTitle = `${point.precip ?? 0} mm/h` +
      (point.precipProb != null ? ` · ${Math.round(point.precipProb)}% sannolikhet` : "");
    li.innerHTML =
      `<span class="h-time">${time}</span>` +
      `<span class="h-symbol">${hEmoji}</span>` +
      `<span class="h-temp">${point.temp != null ? Math.round(point.temp) + "°" : "–"}</span>` +
      `<span class="h-precip" title="${precipTitle}"><span style="width:${pct}%"></span></span>`;
    hoursEl.appendChild(li);
  }

  // ---- Coming days ----
  const daysEl = document.getElementById("days");
  for (const day of data.days || []) {
    const [dEmoji, dLabel] = SYMBOLS[day.symbol] || ["", ""];
    // Noon avoids the date sliding a day when parsed/formatted across zones.
    const label = new Date(day.date + "T12:00:00")
      .toLocaleDateString(LOCALE, { weekday: "short", day: "numeric", month: "short" });
    // Hour ranges get coarse (3-12 h steps) beyond ~day 2: mark as approximate.
    const approx = day.resolution_h > 2 ? "≈" : "";
    const periods = (day.rain_periods || [])
      .map((p) => `${approx}${String(p.from).padStart(2, "0")}–${String(p.to).padStart(2, "0")}`)
      .join(", ");
    const rain = day.precip_mm >= 0.1
      ? `<span class="d-mm">${day.precip_mm} mm</span><span class="d-when">${periods}</span>`
      : `<span class="d-when d-dry">torrt</span>`;
    const li = document.createElement("li");
    li.title = dLabel;
    li.innerHTML =
      `<span class="d-name">${label}</span>` +
      `<span class="d-symbol">${dEmoji}</span>` +
      `<span class="d-temp">${day.temp_max}° <em>${day.temp_min}°</em></span>` +
      `<span class="d-rain">${rain}</span>`;
    daysEl.appendChild(li);
  }
}

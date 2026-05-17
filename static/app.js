export const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

export const ICON_OPTIONS = [
  { value: "", label: "None", icon: "" },
  { value: "fa-bell", label: "Bell", icon: "fa-bell" },
  { value: "fa-bullhorn", label: "Bullhorn", icon: "fa-bullhorn" },
  { value: "fa-volume-high", label: "Volume", icon: "fa-volume-high" },
  { value: "fa-school", label: "School", icon: "fa-school" },
  { value: "fa-music", label: "Music", icon: "fa-music" },
  { value: "fa-triangle-exclamation", label: "Warning", icon: "fa-triangle-exclamation" },
  { value: "fa-fire", label: "Fire", icon: "fa-fire" },
  { value: "fa-truck-medical", label: "Medical", icon: "fa-truck-medical" },
  { value: "fa-repeat", label: "Loop", icon: "fa-repeat" }
];

export const qs = (selector, root = document) => root.querySelector(selector);
export const qsa = (selector, root = document) => Array.from(root.querySelectorAll(selector));

export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

export function icon(name) {
  return `<i class="fa-solid ${escapeHtml(name)}" aria-hidden="true"></i>`;
}

export function buttonIcon(name) {
  const aliases = {
    "fa-exclamation-triangle": "fa-triangle-exclamation",
    "fa-ambulance": "fa-truck-medical"
  };
  return aliases[name] || name || "fa-bell";
}

export function formatTime12h(value) {
  if (!value) return "";
  const [hourRaw, minuteRaw = "00"] = value.split(":");
  let hour = Number.parseInt(hourRaw, 10);
  const minute = minuteRaw.padStart(2, "0").slice(0, 2);
  const period = hour >= 12 ? "PM" : "AM";
  hour %= 12;
  if (hour === 0) hour = 12;
  return `${hour}:${minute} ${period}`;
}

export function formatMinutes(minutes) {
  if (minutes == null) return "Not scheduled";
  if (minutes < 1) return "Due now";
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? `${hours}h ${remainder}m` : `${hours}h`;
}

export function fillSoundSelect(select, audioFiles, selected = "") {
  select.innerHTML = "";
  if (!audioFiles.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No audio files";
    select.appendChild(option);
    select.disabled = true;
    return;
  }
  select.disabled = false;
  audioFiles.forEach((file) => {
    const option = document.createElement("option");
    option.value = file.file;
    option.textContent = file.name;
    if (file.file === selected) option.selected = true;
    select.appendChild(option);
  });
}

export async function api(path, options = {}) {
  const requestOptions = { method: "GET", ...options };
  if (requestOptions.body && !(requestOptions.body instanceof FormData)) {
    requestOptions.headers = {
      "Content-Type": "application/json",
      ...(requestOptions.headers || {})
    };
    requestOptions.body = JSON.stringify(requestOptions.body);
  }

  const response = await fetch(path, requestOptions);
  const contentType = response.headers.get("content-type") || "";
  let payload = null;
  if (contentType.includes("application/json")) {
    payload = await response.json();
  } else {
    payload = await response.text();
  }

  if (!response.ok) {
    const detail = payload && payload.detail ? payload.detail : payload;
    const message =
      typeof detail === "object"
        ? detail.message || JSON.stringify(detail)
        : detail || response.statusText;
    const error = new Error(message);
    error.status = response.status;
    error.detail = detail;
    throw error;
  }
  return payload;
}

export function notify(message, type = "info") {
  let region = qs("#toast-region");
  if (!region) {
    region = document.createElement("div");
    region.id = "toast-region";
    region.className = "toast-region";
    document.body.appendChild(region);
  }
  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  region.appendChild(toast);
  window.setTimeout(() => toast.classList.add("show"), 20);
  window.setTimeout(() => {
    toast.classList.remove("show");
    window.setTimeout(() => toast.remove(), 200);
  }, 4200);
}

export function setButtonBusy(button, busy, label = "") {
  if (!button) return;
  if (busy) {
    button.dataset.originalHtml = button.innerHTML;
    button.disabled = true;
    button.innerHTML = `${icon("fa-spinner fa-spin")}<span>${escapeHtml(label || "Working")}</span>`;
  } else {
    button.disabled = false;
    if (button.dataset.originalHtml) button.innerHTML = button.dataset.originalHtml;
  }
}

function applyTheme(mode) {
  let resolved = mode;
  if (mode === "auto") {
    resolved = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  document.documentElement.dataset.theme = resolved;
  localStorage.setItem("theme", mode);
  qsa(".theme-btn").forEach((button) => {
    button.classList.toggle("active", button.dataset.theme === mode);
  });
}

function initTheme() {
  const saved = localStorage.getItem("theme") || "auto";
  applyTheme(saved);
  qsa(".theme-btn").forEach((button) => {
    button.addEventListener("click", () => applyTheme(button.dataset.theme));
  });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if ((localStorage.getItem("theme") || "auto") === "auto") applyTheme("auto");
  });
}

function initMenu() {
  const toggle = qs("#menu-toggle");
  const nav = qs(".app-nav");
  if (!toggle || !nav) return;
  toggle.addEventListener("click", () => {
    const isOpen = nav.classList.toggle("show");
    toggle.setAttribute("aria-expanded", String(isOpen));
  });
}

function initClock() {
  const clock = qs("#current-time");
  if (!clock) return;
  const render = () => {
    const now = new Date();
    const time = now.toLocaleTimeString([], {
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit"
    });
    const day = now.toLocaleDateString([], { weekday: "short" });
    clock.textContent = `${day} ${time}`;
  };
  render();
  window.setInterval(render, 1000);
}

async function initHost() {
  const host = qs("#hostname");
  if (!host) return;
  try {
    const data = await api("/api/network");
    host.textContent = `${data.hostname} ${data.ip}`;
  } catch {
    host.textContent = "Network unavailable";
  }
}

function initActiveNav(activePage) {
  qsa("[data-nav]").forEach((item) => {
    item.classList.toggle("active", item.dataset.nav === activePage);
  });
}

function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) return;
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/service-worker.js").catch(() => {});
  });
}

export function initShell(activePage) {
  initTheme();
  initMenu();
  initClock();
  initHost();
  initActiveNav(activePage);
  registerServiceWorker();
}

export function initAuthShell() {
  initTheme();
  initHost();
}

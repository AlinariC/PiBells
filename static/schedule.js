import {
  DAYS,
  api,
  buttonIcon,
  escapeHtml,
  fillSoundSelect,
  formatMinutes,
  formatTime12h,
  icon,
  initShell,
  notify,
  qs,
  setButtonBusy
} from "./app.js";

let audioFiles = [];
let audioMap = {};
let entries = [];
let buttons = [];
let editingEntryId = "";
let playingButtonId = "";

function audioName(filename) {
  return audioMap[filename] || filename || "Unknown audio";
}

async function loadAudio() {
  audioFiles = await api("/api/audio");
  audioMap = Object.fromEntries(audioFiles.map((file) => [file.file, file.name]));
  fillSoundSelect(qs("#entry-sound"), audioFiles);
}

async function loadSchedules() {
  const data = await api("/api/schedules");
  const select = qs("#schedule-select");
  select.innerHTML = "";
  data.schedules.forEach((name) => {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    if (name === data.active) option.selected = true;
    select.appendChild(option);
  });
  qs("#active-schedule-name").textContent = data.active || "Default";
}

async function loadEntries() {
  entries = await api("/api/schedule");
  renderSchedule();
}

async function loadButtons() {
  buttons = await api("/api/buttons");
  renderQuickButtons();
}

async function loadDashboard() {
  const data = await api("/api/dashboard");
  const metrics = qs("#dashboard-metrics");
  const next = data.next_event;
  metrics.innerHTML = `
    <article class="metric metric-accent">
      <span class="metric-label">Next Bell</span>
      <strong>${next ? escapeHtml(formatTime12h(next.time)) : "None"}</strong>
      <span>${next ? `${escapeHtml(next.day_name)} · ${escapeHtml(formatMinutes(next.in_minutes))}` : "No upcoming event"}</span>
    </article>
    <article class="metric">
      <span class="metric-label">Enabled</span>
      <strong>${data.enabled_event_count}</strong>
      <span>${data.event_count} total events</span>
    </article>
    <article class="metric">
      <span class="metric-label">Devices</span>
      <strong>${data.online_device_count}/${data.device_count}</strong>
      <span>online</span>
    </article>
    <article class="metric">
      <span class="metric-label">Library</span>
      <strong>${data.audio_count}</strong>
      <span>${data.button_count} quick buttons</span>
    </article>
  `;
}

function renderQuickButtons() {
  const container = qs("#quick-buttons");
  if (!buttons.length) {
    container.innerHTML = `<div class="empty-state">${icon("fa-bell-slash")}<span>No quick buttons yet</span></div>`;
    return;
  }
  container.innerHTML = "";
  buttons.forEach((button) => {
    const element = document.createElement("button");
    element.className = "quick-button";
    element.style.setProperty("--button-color", button.color);
    element.innerHTML = `
      ${icon(button.loop && playingButtonId === button.id ? "fa-stop" : buttonIcon(button.icon))}
      <span>${escapeHtml(button.name)}</span>
    `;
    element.addEventListener("click", () => playButton(button, element));
    container.appendChild(element);
  });
}

async function playButton(button, element) {
  try {
    if (button.loop && playingButtonId === button.id) {
      await api("/api/stop", { method: "POST" });
      playingButtonId = "";
      renderQuickButtons();
      return;
    }
    if (button.loop && playingButtonId) {
      await api("/api/stop", { method: "POST" });
    }
    setButtonBusy(element, true, "Playing");
    await api("/api/test", {
      method: "POST",
      body: { sound_file: button.sound_file, loop: button.loop }
    });
    if (button.loop) {
      playingButtonId = button.id;
      renderQuickButtons();
    } else {
      setButtonBusy(element, false);
    }
  } catch (error) {
    playingButtonId = "";
    setButtonBusy(element, false);
    notify(error.message, "error");
  }
}

function renderSchedule() {
  const board = qs("#schedule-board");
  board.innerHTML = "";
  DAYS.forEach((day, dayIndex) => {
    const dayEntries = entries
      .filter((entry) => entry.day === dayIndex)
      .sort((a, b) => a.time.localeCompare(b.time));
    const section = document.createElement("section");
    section.className = "day-column";
    section.innerHTML = `
      <div class="day-heading">
        <h2>${day}</h2>
        <div class="day-tools">
          <span>${dayEntries.length}</span>
          <button class="icon-btn day-add" type="button" title="Add bell to ${escapeHtml(day)}" aria-label="Add bell to ${escapeHtml(day)}">${icon("fa-plus")}</button>
        </div>
      </div>
      <div class="event-list"></div>
    `;
    qs(".day-add", section).addEventListener("click", () => openEntryDialog(dayIndex));
    const list = qs(".event-list", section);
    if (!dayEntries.length) {
      list.innerHTML = `<div class="empty-state compact">${icon("fa-calendar-plus")}<span>No bells</span></div>`;
    } else {
      dayEntries.forEach((entry) => list.appendChild(renderEvent(entry)));
    }
    board.appendChild(section);
  });
}

function renderEvent(entry) {
  const row = document.createElement("article");
  row.className = `event-row${entry.enabled ? "" : " muted"}`;
  row.innerHTML = `
    <div class="event-time">${escapeHtml(formatTime12h(entry.time))}</div>
    <div class="event-main">
      <strong>${escapeHtml(entry.label || audioName(entry.sound_file))}</strong>
      <span>${escapeHtml(audioName(entry.sound_file))}</span>
    </div>
    <div class="event-actions">
      <button class="icon-btn" data-action="play" title="Play">${icon("fa-play")}</button>
      <button class="icon-btn" data-action="toggle" title="${entry.enabled ? "Disable" : "Enable"}">${icon(entry.enabled ? "fa-toggle-on" : "fa-toggle-off")}</button>
      <button class="icon-btn" data-action="edit" title="Edit">${icon("fa-pen")}</button>
      <button class="icon-btn danger" data-action="delete" title="Delete">${icon("fa-trash")}</button>
    </div>
  `;
  qs('[data-action="play"]', row).addEventListener("click", async () => {
    try {
      await api("/api/test", { method: "POST", body: { sound_file: entry.sound_file } });
      notify("Bell sent", "success");
    } catch (error) {
      notify(error.message, "error");
    }
  });
  qs('[data-action="toggle"]', row).addEventListener("click", () => saveEntry({ ...entry, enabled: !entry.enabled }));
  qs('[data-action="edit"]', row).addEventListener("click", () => openEntryDialog(entry.day, entry));
  qs('[data-action="delete"]', row).addEventListener("click", async () => {
    if (!confirm("Delete this bell?")) return;
    try {
      await api(`/api/schedule/${encodeURIComponent(entry.id)}`, { method: "DELETE" });
      await refreshScheduleData();
      notify("Bell deleted", "success");
    } catch (error) {
      notify(error.message, "error");
    }
  });
  return row;
}

function openEntryDialog(dayIndex, entry = null) {
  if (entry) {
    populateEntryForm(entry);
  } else {
    resetEntryForm(dayIndex);
  }
  qs("#entry-dialog").showModal();
  window.setTimeout(() => qs("#entry-time").focus(), 0);
}

function closeEntryDialog() {
  const dialog = qs("#entry-dialog");
  if (dialog.open) dialog.close();
  resetEntryForm();
}

function populateEntryForm(entry) {
  editingEntryId = entry.id;
  qs("#entry-form-title").textContent = "Edit Bell";
  qs("#entry-day").value = entry.day;
  qs("#entry-time").value = entry.time.slice(0, 5);
  fillSoundSelect(qs("#entry-sound"), audioFiles, entry.sound_file);
  qs("#entry-label").value = entry.label || "";
  qs("#entry-enabled").checked = entry.enabled;
  qs("#entry-submit").innerHTML = `${icon("fa-floppy-disk")}<span>Save</span>`;
}

function resetEntryForm(dayIndex = new Date().getDay() === 0 ? 6 : new Date().getDay() - 1) {
  editingEntryId = "";
  qs("#entry-form-title").textContent = "Add Bell";
  qs("#entry-form").reset();
  qs("#entry-day").value = String(dayIndex);
  qs("#entry-enabled").checked = true;
  fillSoundSelect(qs("#entry-sound"), audioFiles);
  qs("#entry-submit").innerHTML = `${icon("fa-plus")}<span>Add Bell</span>`;
}

async function saveEntry(entry, { closeDialog = true } = {}) {
  try {
    const method = entry.id ? "PUT" : "POST";
    const path = entry.id ? `/api/schedule/${encodeURIComponent(entry.id)}` : "/api/schedule";
    await api(path, { method, body: entry });
    await refreshScheduleData();
    if (closeDialog && qs("#entry-dialog").open) {
      qs("#entry-dialog").close();
    }
    resetEntryForm();
    notify("Schedule updated", "success");
  } catch (error) {
    notify(error.message, "error");
  }
}

async function refreshScheduleData() {
  await Promise.all([loadSchedules(), loadEntries(), loadDashboard()]);
}

function initForms() {
  DAYS.forEach((day, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = day;
    qs("#entry-day").appendChild(option);
  });

  qs("#entry-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const entry = {
      id: editingEntryId || undefined,
      day: Number(qs("#entry-day").value),
      time: qs("#entry-time").value,
      sound_file: qs("#entry-sound").value,
      label: qs("#entry-label").value.trim(),
      enabled: qs("#entry-enabled").checked
    };
    await saveEntry(entry);
  });

  qs("#entry-cancel").addEventListener("click", closeEntryDialog);
  qs("#entry-close").addEventListener("click", closeEntryDialog);
  qs("#entry-dialog").addEventListener("cancel", () => resetEntryForm());

  qs("#set-schedule").addEventListener("click", async () => {
    const name = qs("#schedule-select").value;
    try {
      await api(`/api/schedules/activate/${encodeURIComponent(name)}`, { method: "POST" });
      await refreshScheduleData();
      notify("Schedule activated", "success");
    } catch (error) {
      notify(error.message, "error");
    }
  });

  qs("#add-schedule").addEventListener("click", async () => {
    const name = qs("#new-schedule").value.trim();
    if (!name) return;
    try {
      await api("/api/schedules", { method: "POST", body: { name } });
      qs("#new-schedule").value = "";
      await refreshScheduleData();
      notify("Schedule created", "success");
    } catch (error) {
      notify(error.message, "error");
    }
  });

  qs("#delete-schedule").addEventListener("click", async () => {
    const name = qs("#schedule-select").value;
    if (!name || !confirm(`Delete "${name}"?`)) return;
    try {
      await api(`/api/schedules/${encodeURIComponent(name)}`, { method: "DELETE" });
      await refreshScheduleData();
      notify("Schedule deleted", "success");
    } catch (error) {
      notify(error.message, "error");
    }
  });
}

async function init() {
  initShell("schedule");
  initForms();
  try {
    await loadAudio();
    await Promise.all([loadSchedules(), loadEntries(), loadButtons(), loadDashboard()]);
  } catch (error) {
    notify(error.message, "error");
  }
}

document.addEventListener("DOMContentLoaded", init);

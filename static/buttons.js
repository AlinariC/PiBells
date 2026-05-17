import {
  ICON_OPTIONS,
  api,
  buttonIcon,
  escapeHtml,
  fillSoundSelect,
  icon,
  initShell,
  notify,
  qs
} from "./app.js";

let audioFiles = [];
let audioMap = {};
let buttons = [];
let editingButtonId = "";

async function loadAudio() {
  audioFiles = await api("/api/audio");
  audioMap = Object.fromEntries(audioFiles.map((file) => [file.file, file.name]));
  fillSoundSelect(qs("#button-sound"), audioFiles);
}

async function loadButtons() {
  buttons = await api("/api/buttons");
  renderButtons();
}

function renderIconOptions() {
  const select = qs("#button-icon");
  select.innerHTML = "";
  ICON_OPTIONS.forEach((option) => {
    const element = document.createElement("option");
    element.value = option.value;
    element.textContent = option.label;
    select.appendChild(element);
  });
}

function renderButtons() {
  const container = qs("#buttons");
  container.innerHTML = "";
  if (!buttons.length) {
    container.innerHTML = `<div class="empty-state">${icon("fa-bell-slash")}<span>No quick buttons</span></div>`;
    return;
  }
  buttons.forEach((button) => {
    const item = document.createElement("article");
    item.className = "button-item";
    item.style.setProperty("--button-color", button.color);
    item.innerHTML = `
      <div class="button-swatch">${icon(buttonIcon(button.icon))}</div>
      <div class="button-main">
        <strong>${escapeHtml(button.name)}</strong>
        <span>${escapeHtml(audioMap[button.sound_file] || button.sound_file)}${button.loop ? " · loops" : ""}</span>
      </div>
      <div class="button-actions">
        <button class="icon-btn" data-action="play" title="Play">${icon("fa-play")}</button>
        <button class="icon-btn" data-action="edit" title="Edit">${icon("fa-pen")}</button>
        <button class="icon-btn danger" data-action="delete" title="Delete">${icon("fa-trash")}</button>
      </div>
    `;
    qs('[data-action="play"]', item).addEventListener("click", () => playButton(button));
    qs('[data-action="edit"]', item).addEventListener("click", () => editButton(button));
    qs('[data-action="delete"]', item).addEventListener("click", () => deleteButton(button));
    container.appendChild(item);
  });
}

async function playButton(button) {
  try {
    await api("/api/test", {
      method: "POST",
      body: { sound_file: button.sound_file, loop: button.loop }
    });
    notify("Button sent", "success");
  } catch (error) {
    notify(error.message, "error");
  }
}

function editButton(button) {
  editingButtonId = button.id;
  qs("#button-form-title").textContent = "Edit Button";
  qs("#button-name").value = button.name;
  fillSoundSelect(qs("#button-sound"), audioFiles, button.sound_file);
  qs("#button-color").value = button.color;
  qs("#button-icon").value = button.icon;
  qs("#button-loop").checked = button.loop;
  qs("#button-submit").innerHTML = `${icon("fa-floppy-disk")}<span>Save</span>`;
  qs("#button-cancel").hidden = false;
  updateIconPreview();
}

async function deleteButton(button) {
  if (!confirm(`Delete "${button.name}"?`)) return;
  try {
    await api(`/api/buttons/${encodeURIComponent(button.id)}`, { method: "DELETE" });
    await loadButtons();
    notify("Button deleted", "success");
  } catch (error) {
    notify(error.message, "error");
  }
}

function resetButtonForm() {
  editingButtonId = "";
  qs("#button-form").reset();
  qs("#button-form-title").textContent = "Add Button";
  fillSoundSelect(qs("#button-sound"), audioFiles);
  qs("#button-submit").innerHTML = `${icon("fa-plus")}<span>Add Button</span>`;
  qs("#button-cancel").hidden = true;
  updateIconPreview();
}

function updateIconPreview() {
  const value = qs("#button-icon").value;
  qs("#icon-preview").innerHTML = value ? icon(buttonIcon(value)) : "";
}

function initForm() {
  renderIconOptions();
  qs("#button-icon").addEventListener("change", updateIconPreview);
  qs("#button-cancel").addEventListener("click", resetButtonForm);
  qs("#button-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = {
      id: editingButtonId || undefined,
      name: qs("#button-name").value.trim(),
      sound_file: qs("#button-sound").value,
      color: qs("#button-color").value,
      icon: qs("#button-icon").value,
      loop: qs("#button-loop").checked
    };
    try {
      const method = editingButtonId ? "PUT" : "POST";
      const path = editingButtonId ? `/api/buttons/${encodeURIComponent(editingButtonId)}` : "/api/buttons";
      await api(path, { method, body: button });
      resetButtonForm();
      await loadButtons();
      notify("Button saved", "success");
    } catch (error) {
      notify(error.message, "error");
    }
  });
}

async function init() {
  initShell("buttons");
  initForm();
  await loadAudio();
  await loadButtons();
}

document.addEventListener("DOMContentLoaded", () => {
  init().catch((error) => notify(error.message, "error"));
});

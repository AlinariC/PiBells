import {
  api,
  escapeHtml,
  icon,
  initShell,
  notify,
  qs,
  qsa,
  setButtonBusy
} from "./app.js";

let audioFiles = [];
let scanEvents = null;
let scanResults = new Map();

function deviceKey(device) {
  return `${device.ip}:${device.port || 3030}`;
}

function deviceLabel(device) {
  return device.name || `Barix ${device.ip}`;
}

function renderAddress(device) {
  const port = device.port || 3030;
  return port === 3030 ? device.ip : `${device.ip}:${port}`;
}

async function loadDevices() {
  const devices = await api("/api/devices");
  const tbody = qs("#devices tbody");
  tbody.innerHTML = "";
  if (!devices.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="table-empty">No devices configured</td></tr>`;
    return;
  }
  devices.forEach((device, index) => {
    const key = deviceKey(device);
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>
        <strong>${escapeHtml(deviceLabel(device))}</strong>
      </td>
      <td><code>${escapeHtml(renderAddress(device))}</code></td>
      <td><span class="status-pill pending" data-device-key="${escapeHtml(key)}">Checking</span></td>
      <td class="table-actions">
        <button class="icon-btn danger" title="Delete" data-index="${index}">${icon("fa-trash")}</button>
      </td>
    `;
    qs("button", row).addEventListener("click", () => deleteDevice(index));
    tbody.appendChild(row);
  });
  updateDeviceStatuses();
}

async function updateDeviceStatuses() {
  try {
    const statuses = await api("/api/devices/status");
    qsa("[data-device-key]").forEach((status) => {
      const online = Boolean(statuses[status.dataset.deviceKey]);
      status.className = `status-pill ${online ? "online" : "offline"}`;
      status.textContent = online ? "Online" : "Offline";
    });
  } catch {
    qsa("[data-device-key]").forEach((status) => {
      status.className = "status-pill offline";
      status.textContent = "Unknown";
    });
  }
}

async function deleteDevice(index) {
  if (!confirm("Delete this device?")) return;
  try {
    await api(`/api/devices/${index}`, { method: "DELETE" });
    await loadDevices();
    notify("Device deleted", "success");
  } catch (error) {
    notify(error.message, "error");
  }
}

async function loadAudio() {
  audioFiles = await api("/api/audio");
  const list = qs("#audio-list");
  list.innerHTML = "";
  if (!audioFiles.length) {
    list.innerHTML = `<div class="empty-state">${icon("fa-file-audio")}<span>No audio files</span></div>`;
    return;
  }
  audioFiles.forEach((file) => {
    const item = document.createElement("article");
    item.className = "media-item";
    item.innerHTML = `
      <div class="media-icon">${icon("fa-music")}</div>
      <div class="media-main">
        <strong>${escapeHtml(file.name)}</strong>
        <span>${escapeHtml(file.file)}</span>
      </div>
      <div class="media-actions">
        <button class="icon-btn" data-action="play" title="Play">${icon("fa-play")}</button>
        <button class="icon-btn" data-action="rename" title="Rename">${icon("fa-pen")}</button>
        <button class="icon-btn danger" data-action="delete" title="Delete">${icon("fa-trash")}</button>
      </div>
    `;
    qs('[data-action="play"]', item).addEventListener("click", () => testAudio(file.file));
    qs('[data-action="rename"]', item).addEventListener("click", () => renameAudio(file));
    qs('[data-action="delete"]', item).addEventListener("click", () => deleteAudio(file.file));
    list.appendChild(item);
  });
}

async function testAudio(filename) {
  try {
    await api("/api/test", { method: "POST", body: { sound_file: filename } });
    notify("Bell sent", "success");
  } catch (error) {
    notify(error.message, "error");
  }
}

async function renameAudio(file) {
  const name = prompt("New display name", file.name);
  if (!name) return;
  try {
    await api(`/api/audio/${encodeURIComponent(file.file)}`, {
      method: "PUT",
      body: { name }
    });
    await loadAudio();
    notify("Audio renamed", "success");
  } catch (error) {
    notify(error.message, "error");
  }
}

async function deleteAudio(filename, force = false) {
  if (!force && !confirm("Delete this audio file?")) return;
  try {
    await api(`/api/audio/${encodeURIComponent(filename)}${force ? "?force=true" : ""}`, {
      method: "DELETE"
    });
    await loadAudio();
    notify("Audio deleted", "success");
  } catch (error) {
    if (error.status === 409) {
      const ok = confirm("This file is used by schedules or quick buttons. Delete it and remove those references?");
      if (ok) return deleteAudio(filename, true);
    }
    notify(error.message, "error");
  }
}

async function loadAccount() {
  try {
    const data = await api("/api/account");
    qs("#account-name").textContent = data.username;
  } catch {
    qs("#account-name").textContent = "Admin";
  }
}

function initDeviceForms() {
  qs("#add-device-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const ip = qs("#device-ip").value.trim();
    const name = qs("#device-name").value.trim();
    try {
      await api("/api/devices", { method: "POST", body: { ip, name } });
      event.target.reset();
      await loadDevices();
      notify("Device saved", "success");
    } catch (error) {
      notify(error.message, "error");
    }
  });

  qs("#scan-btn").addEventListener("click", openScanDialog);
}

function resetScanDialog() {
  stopScan();
  scanResults = new Map();
  qs("#scan-results").innerHTML = `<div class="empty-state compact">${icon("fa-magnifying-glass")}<span>No scan results</span></div>`;
  qs("#scan-progress").value = 0;
  qs("#scan-progress").hidden = true;
  qs("#scan-status").textContent = "";
  qs("#scan-add-selected").disabled = true;
}

async function openScanDialog() {
  resetScanDialog();
  try {
    const data = await api("/api/devices/scan_ranges");
    qs("#scan-ranges").value = (data.ranges || []).join(", ");
    qs("#scan-ranges").placeholder =
      (data.automatic || []).join(", ") || "10.80.2.0/24, 10.80.3.0/24, 10.80.4.0/24, 10.80.5.0/24";
  } catch {
    qs("#scan-ranges").value = "";
  }
  qs("#scan-dialog").showModal();
}

function closeScanDialog() {
  stopScan();
  qs("#scan-dialog").close();
}

function renderScanResults() {
  const results = Array.from(scanResults.values()).sort((a, b) => a.ip.localeCompare(b.ip, undefined, { numeric: true }));
  const box = qs("#scan-results");
  if (!results.length) {
    box.innerHTML = `<div class="empty-state compact">${icon("fa-magnifying-glass")}<span>No Barix devices found</span></div>`;
    qs("#scan-add-selected").disabled = true;
    return;
  }
  box.innerHTML = "";
  results.forEach((device) => {
    const key = device.key || deviceKey(device);
    const item = document.createElement("article");
    item.className = "scan-result";
    item.innerHTML = `
      <label class="check-row scan-check">
        <input type="checkbox" data-scan-select="${escapeHtml(key)}" checked>
        <span>
          <strong>${escapeHtml(device.ip)}</strong>
          <small>${escapeHtml(device.model || "Barix device")} · ${escapeHtml(device.method || "Discovery")}</small>
        </span>
      </label>
      <input type="text" data-scan-name="${escapeHtml(key)}" value="${escapeHtml(device.name || `Barix ${device.ip}`)}" autocomplete="off">
    `;
    qs("input[type='checkbox']", item).addEventListener("change", updateScanSelectionState);
    box.appendChild(item);
  });
  updateScanSelectionState();
}

function updateScanSelectionState() {
  qs("#scan-add-selected").disabled = !qsa("[data-scan-select]").some((input) => input.checked);
}

async function startScan() {
  stopScan();
  scanResults = new Map();
  renderScanResults();
  const progress = qs("#scan-progress");
  const scanText = qs("#scan-status");
  const ranges = qs("#scan-ranges").value.trim();
  const startButton = qs("#scan-start");
  const stopButton = qs("#scan-stop");

  try {
    await api("/api/devices/scan_ranges", {
      method: "PUT",
      body: { ranges: ranges.split(/[\s,;]+/).filter(Boolean) }
    });
  } catch (error) {
    notify(error.message, "error");
    return;
  }

  progress.value = 0;
  progress.max = 100;
  progress.hidden = false;
  scanText.textContent = "Scanning";
  startButton.disabled = true;
  stopButton.hidden = false;

  const query = ranges ? `?ranges=${encodeURIComponent(ranges)}` : "";
  scanEvents = new EventSource(`/api/devices/scan_stream${query}`);
  scanEvents.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if ("total" in data) progress.max = Math.max(Number(data.total) || 1, 1);
    if ("progress" in data) progress.value = Number(data.progress) || 0;
    if (data.step) scanText.textContent = data.step;
    if (data.device) {
      scanResults.set(data.device.key || deviceKey(data.device), data.device);
      renderScanResults();
    }
    if (data.complete || data.error) {
      stopScan(false);
      progress.hidden = true;
      scanText.textContent = data.error || `Scan complete · ${scanResults.size} found`;
      if (data.error) notify(data.error, "error");
    }
  };
  scanEvents.onerror = () => {
    stopScan(false);
    progress.hidden = true;
    scanText.textContent = "Scan stopped";
    notify("Network scan stopped", "error");
  };
}

function stopScan(markStopped = true) {
  if (scanEvents) {
    scanEvents.close();
    scanEvents = null;
  }
  const startButton = qs("#scan-start");
  const stopButton = qs("#scan-stop");
  if (startButton) startButton.disabled = false;
  if (stopButton) stopButton.hidden = true;
  if (markStopped && qs("#scan-status")) qs("#scan-status").textContent = "";
}

async function addSelectedScanDevices() {
  const selected = qsa("[data-scan-select]").filter((input) => input.checked);
  if (!selected.length) return;
  const submit = qs("#scan-add-selected");
  setButtonBusy(submit, true, "Adding");
  try {
    for (const input of selected) {
      const device = scanResults.get(input.dataset.scanSelect);
      const nameInput = qs(`[data-scan-name="${CSS.escape(input.dataset.scanSelect)}"]`);
      await api("/api/devices", {
        method: "POST",
        body: {
          ip: device.ip,
          name: nameInput ? nameInput.value.trim() : device.name,
          port: device.port || 3030
        }
      });
    }
    closeScanDialog();
    await loadDevices();
    notify("Selected devices added", "success");
  } catch (error) {
    notify(error.message, "error");
  } finally {
    setButtonBusy(submit, false);
    updateScanSelectionState();
  }
}

function initScanDialog() {
  qs("#scan-start").addEventListener("click", startScan);
  qs("#scan-stop").addEventListener("click", () => stopScan());
  qs("#scan-close").addEventListener("click", closeScanDialog);
  qs("#scan-cancel").addEventListener("click", closeScanDialog);
  qs("#scan-add-selected").addEventListener("click", addSelectedScanDevices);
  qs("#scan-dialog").addEventListener("cancel", () => stopScan());
}

function initAudioForm() {
  qs("#upload-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const fileInput = qs("#audio-file");
    const nameInput = qs("#audio-name");
    if (!fileInput.files.length) return;
    const data = new FormData();
    data.append("file", fileInput.files[0]);
    data.append("name", nameInput.value);
    const submit = qs("#upload-submit");
    setButtonBusy(submit, true, "Uploading");
    try {
      await api("/api/audio", { method: "POST", body: data });
      event.target.reset();
      await loadAudio();
      notify("Audio uploaded", "success");
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setButtonBusy(submit, false);
    }
  });
}

function initPowerActions() {
  qs("#reboot-btn").addEventListener("click", async () => {
    if (!confirm("Reboot the device now?")) return;
    try {
      await api("/api/reboot", { method: "POST" });
      window.location.href = "/rebooting";
    } catch (error) {
      notify(error.message, "error");
    }
  });
}

function initAccountForm() {
  qs("#password-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const current_password = qs("#current-password").value;
    const new_password = qs("#new-password").value;
    const confirmPassword = qs("#confirm-password").value;
    if (new_password !== confirmPassword) {
      notify("Passwords do not match", "error");
      return;
    }
    try {
      await api("/api/account/password", {
        method: "POST",
        body: { current_password, new_password }
      });
      event.target.reset();
      notify("Password updated", "success");
    } catch (error) {
      notify(error.message, "error");
    }
  });
}

async function init() {
  initShell("admin");
  initDeviceForms();
  initScanDialog();
  initAudioForm();
  initPowerActions();
  initAccountForm();
  await Promise.all([loadDevices(), loadAudio(), loadAccount()]);
  window.setInterval(updateDeviceStatuses, 6000);
}

document.addEventListener("DOMContentLoaded", () => {
  init().catch((error) => notify(error.message, "error"));
});

import { api, initAuthShell, notify, qs, setButtonBusy } from "./app.js";

document.addEventListener("DOMContentLoaded", () => {
  initAuthShell();
  qs("#setup-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const username = qs("#setup-username").value.trim();
    const password = qs("#setup-password").value;
    const confirmPassword = qs("#setup-confirm").value;
    if (password !== confirmPassword) {
      notify("Passwords do not match", "error");
      return;
    }
    const submit = qs("#setup-submit");
    setButtonBusy(submit, true, "Creating");
    try {
      await api("/api/setup", {
        method: "POST",
        body: { username, password }
      });
      window.location.href = "/login";
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setButtonBusy(submit, false);
    }
  });
});

import { initAuthShell, qs } from "./app.js";

document.addEventListener("DOMContentLoaded", () => {
  initAuthShell();
  const params = new URLSearchParams(window.location.search);
  if (params.get("error")) {
    qs("#error-msg").textContent = "Invalid username or password";
  }
});

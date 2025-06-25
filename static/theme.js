function applyTheme(mode) {
  let theme = mode;
  if (mode === 'auto') {
    theme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  document.body.classList.toggle('dark-mode', theme === 'dark');
  document.body.classList.add('theme-transition');
  setTimeout(() => document.body.classList.remove('theme-transition'), 500);
  localStorage.setItem('theme', mode);
  const select = document.getElementById('theme-select');
  if (select && select.value !== mode) select.value = mode;
  document.querySelectorAll('.theme-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.theme === mode);
  });
}

function initTheme() {
  const saved = localStorage.getItem('theme') || 'auto';
  applyTheme(saved);
  const select = document.getElementById('theme-select');
  if (select) {
    select.value = saved;
    select.addEventListener('change', (e) => applyTheme(e.target.value));
  }
  document.querySelectorAll('.theme-btn').forEach(btn => {
    btn.addEventListener('click', () => applyTheme(btn.dataset.theme));
  });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (localStorage.getItem('theme') === 'auto') applyTheme('auto');
  });

}

document.addEventListener('DOMContentLoaded', initTheme);

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/service-worker.js');
  });
}

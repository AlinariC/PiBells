function applyTheme(mode) {
  let theme = mode;
  if (mode === 'auto') {
    theme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  document.body.classList.toggle('dark-mode', theme === 'dark');
  localStorage.setItem('theme', mode);
  const select = document.getElementById('theme-select');
  if (select && select.value !== mode) select.value = mode;
}

function initTheme() {
  const saved = localStorage.getItem('theme') || 'auto';
  applyTheme(saved);
  const select = document.getElementById('theme-select');
  if (select) {
    select.value = saved;
    select.addEventListener('change', (e) => applyTheme(e.target.value));
  }
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (localStorage.getItem('theme') === 'auto') applyTheme('auto');
  });
}

document.addEventListener('DOMContentLoaded', initTheme);

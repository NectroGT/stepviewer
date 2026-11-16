// Light (default) or dark, as saved by the theme button. Loaded in <head> so a saved dark choice
// is applied before the page is drawn and it doesn't flash light first. app.js adds the button.
(() => {
  const root = document.documentElement;
  const bar = document.querySelector('meta[name="theme-color"]'); // phone status/address bar colour
  function apply(t) {
    if (t === "dark") root.dataset.theme = "dark"; else delete root.dataset.theme;
    if (bar) bar.content = t === "dark" ? "#171f28" : "#ffffff";
  }
  let saved = "light";
  try { saved = localStorage.getItem("theme") || "light"; } catch {}
  apply(saved);
  window.stepTheme = { apply };
})();

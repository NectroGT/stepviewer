// Light/dark choice ("auto" follows the device). Loaded in <head> so a saved choice is applied
// before the page is drawn and it doesn't flash the other theme. app.js adds the button.
(() => {
  const root = document.documentElement;
  const metas = [...document.querySelectorAll('meta[name="theme-color"]')];
  const original = metas.map((m) => m.content);
  const BAR = { light: "#ffffff", dark: "#171f28" }; // phone status/address bar colour
  function apply(t) {
    if (t === "light" || t === "dark") root.dataset.theme = t; else delete root.dataset.theme;
    metas.forEach((m, i) => { m.content = BAR[t] || original[i]; });
  }
  let saved = "auto";
  try { saved = localStorage.getItem("theme") || "auto"; } catch {}
  apply(saved);
  window.stepTheme = { apply };
})();

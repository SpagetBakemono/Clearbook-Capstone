/* Light/dark theme. Loaded in <head> so the saved choice applies before
   the page paints (no flash). With no saved choice the page follows the
   system setting (CSS prefers-color-scheme); clicking the toggle saves an
   explicit choice. Storage can be blocked (private windows) -- the toggle
   still works for the current page. */
(function () {
  "use strict";
  var root = document.documentElement;
  try {
    var saved = localStorage.getItem("clearbook-theme");
    if (saved === "light" || saved === "dark") root.setAttribute("data-theme", saved);
  } catch (e) { /* storage unavailable: follow the system */ }

  function current() {
    var set = root.getAttribute("data-theme");
    if (set) return set;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest && ev.target.closest("[data-theme-toggle]");
    if (!btn) return;
    var next = current() === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("clearbook-theme", next); } catch (e) { /* not saved */ }
    btn.setAttribute("aria-label", next === "dark" ? "Switch to light theme" : "Switch to dark theme");
  });
})();

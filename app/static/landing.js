/* Landing hero: show one line at a time, swapping every 5 seconds: fade out,
   then fade in (the fades themselves are CSS). Pauses while hovered, and stays put on
   the first line for visitors who prefer reduced motion. */
(function () {
  "use strict";
  var lines = document.querySelectorAll(".hero-rotator .hero-text");
  if (lines.length < 2) return;
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

  var current = 0;
  var paused = false;
  var rotator = lines[0].parentNode;
  rotator.addEventListener("mouseenter", function () { paused = true; });
  rotator.addEventListener("mouseleave", function () { paused = false; });

  // Fade the current line out completely before the next fades in, so
  // two different sentences never overlap mid-transition.
  var FADE_MS = 700; // matches the CSS transition
  setInterval(function () {
    if (paused || document.hidden) return;
    lines[current].classList.remove("is-active");
    current = (current + 1) % lines.length;
    var next = lines[current];
    setTimeout(function () { next.classList.add("is-active"); }, FADE_MS);
  }, 5000);
})();

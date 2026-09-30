/* Trends charts: stacked monthly bars and a balance line, drawn as plain
   SVG from the JSON blob in #chart-data (see app/routers/trends.py).
   Deliberately no third-party chart library -- this app holds bank
   tokens, so it loads no code it doesn't ship itself.

   Every chart has a real Y-axis (nice round ticks + faint gridlines) and
   a hover tooltip. Bars stack in the series' fixed legend order, the
   order the category palette was validated for (services.py). The
   balance line's Y-axis starts at $0; the mouse wheel zooms its floor up
   toward the data to show variation, and "Reset scale" goes back. */
(function () {
  "use strict";

  var DATA = JSON.parse(document.getElementById("chart-data").textContent);
  var SVG_NS = "http://www.w3.org/2000/svg";
  var HEIGHT = 260;
  var M = { top: 22, right: 12, bottom: 28, left: 58 };

  function el(tag, attrs, parent) {
    var node = document.createElementNS(SVG_NS, tag);
    for (var k in attrs) node.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(node);
    return node;
  }

  function money(v, cents) {
    return v.toLocaleString("en-US", {
      style: "currency", currency: "USD",
      minimumFractionDigits: cents ? 2 : 0, maximumFractionDigits: cents ? 2 : 0,
    });
  }

  // Axis/total labels: "$25k" style once the scale reaches $10k, so
  // one axis never mixes "$5,000" and "$25k".
  function axisMoney(v, scaleMax) {
    if (v !== 0 && (scaleMax || Math.abs(v)) >= 10000) {
      return "$" + (v / 1000).toLocaleString("en-US", { maximumFractionDigits: 1 }) + "k";
    }
    return money(v, false);
  }

  // ~5 round ticks covering [lo, hi]: steps of 1, 2, 2.5 or 5 x 10^n.
  function niceTicks(lo, hi, count) {
    if (hi <= lo) hi = lo + 1;
    var raw = (hi - lo) / (count || 5);
    var mag = Math.pow(10, Math.floor(Math.log10(raw)));
    var step = [1, 2, 2.5, 5, 10].map(function (m) { return m * mag; })
      .find(function (s) { return s >= raw; });
    var start = Math.floor(lo / step) * step;
    var end = Math.ceil(hi / step) * step;
    var ticks = [];
    for (var v = start; v <= end + step / 2; v += step) ticks.push(Math.round(v * 100) / 100);
    return ticks;
  }

  function drawYAxis(svg, ticks, y, width) {
    ticks.forEach(function (t) {
      var yy = y(t);
      el("line", { x1: M.left, x2: width - M.right, y1: yy, y2: yy, class: t === 0 ? "axis-base" : "grid" }, svg);
      var label = el("text", { x: M.left - 8, y: yy + 4, class: "axis-label", "text-anchor": "end" }, svg);
      label.textContent = axisMoney(t, ticks[ticks.length - 1]);
    });
  }

  // Month labels thinned to fit: every k-th, always including the last.
  function monthLabel(label, i, all) {
    var parts = label.split(" "); // "Sep 2026"
    var prev = i > 0 ? all[i - 1].split(" ")[1] : null;
    return prev === parts[1] ? parts[0] : parts[0] + " ’" + parts[1].slice(2);
  }

  function tooltip(container) {
    var tip = container.querySelector(".chart-tip");
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "chart-tip";
      container.appendChild(tip);
    }
    return tip;
  }

  function placeTip(tip, container, x, y) {
    tip.style.display = "block";
    var w = tip.offsetWidth, cw = container.clientWidth;
    var left = x + 14;
    if (left + w > cw) left = x - w - 14;
    tip.style.left = Math.max(0, left) + "px";
    tip.style.top = Math.max(0, y - tip.offsetHeight / 2) + "px";
  }

  function legend(container, series) {
    var box = document.createElement("div");
    box.className = "chart-legend";
    series.forEach(function (s) {
      var item = document.createElement("span");
      item.className = "chart-legend-item";
      var sw = document.createElement("span");
      sw.className = "chart-swatch";
      sw.style.background = s.color;
      item.appendChild(sw);
      item.appendChild(document.createTextNode(s.label));
      box.appendChild(item);
    });
    container.appendChild(box);
  }

  function roundedTopRect(x, y, w, h, r) {
    r = Math.min(r, h, w / 2);
    return "M" + x + "," + (y + h) + "V" + (y + r) + "Q" + x + "," + y + " " + (x + r) + "," + y +
      "H" + (x + w - r) + "Q" + (x + w) + "," + y + " " + (x + w) + "," + (y + r) + "V" + (y + h) + "Z";
  }

  function renderBars(container, data) {
    var width = container.clientWidth;
    if (!width) return;
    container.innerHTML = "";
    if (data.series.length > 1) legend(container, data.series);

    var n = data.months.length;
    var totals = data.months.map(function (_, i) {
      return data.series.reduce(function (sum, s) { return sum + s.values[i]; }, 0);
    });
    var ticks = niceTicks(0, Math.max.apply(null, totals.concat([1])));
    var yMax = ticks[ticks.length - 1];
    var plotH = HEIGHT - M.top - M.bottom;
    var y = function (v) { return M.top + plotH - (v / yMax) * plotH; };

    var svg = el("svg", { width: width, height: HEIGHT, class: "chart-svg", role: "img" }, null);
    container.appendChild(svg);
    drawYAxis(svg, ticks, y, width);

    var band = (width - M.left - M.right) / n;
    var barW = Math.max(6, Math.min(40, band * 0.6));
    var every = Math.ceil(n / Math.max(1, Math.floor((width - M.left) / 56)));
    var tip = tooltip(container);

    data.months.forEach(function (label, i) {
      var x = M.left + band * i + (band - barW) / 2;
      var base = y(0);
      var drawn = data.series.filter(function (s) { return s.values[i] > 0; });
      drawn.forEach(function (s, j) {
        var h = (s.values[i] / yMax) * plotH;
        var top = base - h;
        var isTop = j === drawn.length - 1;
        // 2px surface gap between stacked segments (not under the bottom one).
        var gap = j > 0 ? 2 : 0;
        var segH = Math.max(0, h - gap);
        if (isTop) el("path", { d: roundedTopRect(x, top, barW, segH, 4), fill: s.color }, svg);
        else el("rect", { x: x, y: top, width: barW, height: segH, fill: s.color }, svg);
        base = top;
      });
      if (totals[i] > 0) {
        var t = el("text", { x: x + barW / 2, y: y(totals[i]) - 6, class: "bar-total", "text-anchor": "middle" }, svg);
        t.textContent = axisMoney(totals[i]);
      }
      if (i % every === 0 || i === n - 1) {
        var xl = el("text", { x: M.left + band * i + band / 2, y: HEIGHT - 8, class: "axis-label", "text-anchor": "middle" }, svg);
        xl.textContent = monthLabel(label, i, data.months);
      }
      // Hit target: the whole column, not just the bar.
      var hit = el("rect", { x: M.left + band * i, y: M.top, width: band, height: plotH, fill: "transparent" }, svg);
      hit.addEventListener("mousemove", function (ev) {
        var rows = data.series.filter(function (s) { return s.values[i] > 0; }).slice().reverse()
          .map(function (s) {
            return '<div class="tip-row"><span class="chart-swatch" style="background:' + s.color + '"></span>' +
              escapeHtml(s.label) + '<b>' + money(s.values[i], true) + "</b></div>";
          }).join("");
        tip.innerHTML = '<div class="tip-head">' + escapeHtml(label) + "</div>" + rows +
          '<div class="tip-row tip-total">Total<b>' + money(totals[i], true) + "</b></div>";
        var r = container.getBoundingClientRect();
        placeTip(tip, container, ev.clientX - r.left, ev.clientY - r.top);
      });
      hit.addEventListener("mouseleave", function () { tip.style.display = "none"; });
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function fmtDate(iso) {
    var d = new Date(iso + "T00:00:00");
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  }

  function renderLine(container, points) {
    var width = container.clientWidth;
    if (!width) return;
    container.innerHTML = "";
    var values = points.map(function (p) { return p.balance; });
    var dataMin = Math.min.apply(null, values), dataMax = Math.max.apply(null, values);
    // Floor: $0 by default; the wheel moves it between 0 and just under
    // the lowest balance (never above it, or the line would clip).
    var maxFloor = Math.max(0, dataMin - (dataMax - dataMin) * 0.1 - 1);
    var floor = Math.min(container._floor || 0, maxFloor);
    // Round ticks; the lowest snaps down to a round value at or below
    // the floor, so a zoomed axis reads "$20k", not "$18,437".
    var ticks = niceTicks(floor, dataMax).filter(function (t) { return t >= 0; });
    var lo = ticks[0], hi = ticks[ticks.length - 1];
    var plotH = HEIGHT - M.top - M.bottom;
    var plotW = width - M.left - M.right;
    var y = function (v) { return M.top + plotH - ((v - lo) / (hi - lo)) * plotH; };
    var x = function (i) { return M.left + (points.length > 1 ? (i / (points.length - 1)) * plotW : plotW / 2); };

    var svg = el("svg", { width: width, height: HEIGHT, class: "chart-svg", role: "img" }, null);
    container.appendChild(svg);
    drawYAxis(svg, ticks, y, width);

    var line = points.map(function (p, i) { return (i ? "L" : "M") + x(i).toFixed(1) + "," + y(p.balance).toFixed(1); }).join("");
    el("path", { d: line + "L" + x(points.length - 1) + "," + y(lo) + "L" + x(0) + "," + y(lo) + "Z", class: "line-area" }, svg);
    el("path", { d: line, class: "line-path" }, svg);

    var monthly = /month$/.test(container.dataset.key);
    var last = points.length - 1;
    // Evenly stepped labels (about one per 90px). The last point always
    // gets one; if it would crowd the previous label, it replaces it.
    var step = Math.max(1, Math.ceil(last / Math.max(1, Math.floor(plotW / 90))));
    var shown = {};
    for (var k = 0; k <= last; k += step) shown[k] = true;
    var lastStepped = last - (last % step);
    if (lastStepped !== last && last - lastStepped <= step / 2) delete shown[lastStepped];
    shown[last] = true;
    Object.keys(shown).forEach(function (i) {
      var t = el("text", { x: x(+i), y: HEIGHT - 8, class: "axis-label", "text-anchor": "middle" }, svg);
      var d = new Date(points[i].date + "T00:00:00");
      t.textContent = monthly
        ? d.toLocaleDateString("en-US", { month: "short" }) + " \u2019" + String(d.getFullYear()).slice(2)
        : d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
    });

    // Emphasized endpoint: today's balance.
    el("circle", { cx: x(last), cy: y(points[last].balance), r: 4.5, class: "line-end" }, svg);

    var cross = el("line", { y1: M.top, y2: M.top + plotH, class: "crosshair", visibility: "hidden" }, svg);
    var dot = el("circle", { r: 4, class: "line-dot", visibility: "hidden" }, svg);
    var tip = tooltip(container);
    var hit = el("rect", { x: M.left, y: M.top, width: plotW, height: plotH, fill: "transparent" }, svg);
    hit.addEventListener("mousemove", function (ev) {
      var r = svg.getBoundingClientRect();
      var px = ev.clientX - r.left;
      var i = Math.round(((px - M.left) / plotW) * (points.length - 1));
      i = Math.max(0, Math.min(points.length - 1, i));
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i));
      cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(points[i].balance));
      dot.setAttribute("visibility", "visible");
      tip.innerHTML = '<div class="tip-head">' + fmtDate(points[i].date) + '</div><div class="tip-row">Balance<b>' +
        money(points[i].balance, true) + "</b></div>";
      placeTip(tip, container, x(i), y(points[i].balance));
    });
    hit.addEventListener("mouseleave", function () {
      cross.setAttribute("visibility", "hidden");
      dot.setAttribute("visibility", "hidden");
      tip.style.display = "none";
    });

    if (!container._wheelBound) {
      container._wheelBound = true;
      container.addEventListener("wheel", function (ev) {
        ev.preventDefault();
        var step = Math.max(1, maxFloorOf(container) / 12);
        var next = (container._floor || 0) + (ev.deltaY < 0 ? step : -step);
        container._floor = Math.max(0, Math.min(maxFloorOf(container), next));
        render(container);
      }, { passive: false });
    }
    container._maxFloor = maxFloor;
  }

  function maxFloorOf(container) { return container._maxFloor || 0; }

  function render(container) {
    var data = DATA[container.dataset.key];
    if (container.dataset.chart === "bars") renderBars(container, data);
    else renderLine(container, data);
  }

  var charts = Array.prototype.slice.call(document.querySelectorAll(".chart[data-chart]"));

  // Draw whatever is visible now; hidden panels measure 0 wide and are
  // skipped until shown.
  function renderVisible() {
    charts.forEach(function (c) {
      var w = c.clientWidth;
      if (w && w !== c._lastWidth) { c._lastWidth = w; render(c); }
    });
  }
  renderVisible();
  // A toggle reveals a hidden panel; a window resize changes widths.
  document.querySelectorAll(".toggle-input").forEach(function (r) {
    r.addEventListener("change", renderVisible);
  });
  window.addEventListener("resize", renderVisible);
  // Catches container-only resizes too, where supported.
  if (window.ResizeObserver) new ResizeObserver(renderVisible).observe(document.querySelector(".container"));

  document.querySelectorAll("[data-reset-scale]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var chart = btn.closest(".chart-card").querySelector(".chart");
      chart._floor = 0;
      render(chart);
    });
  });
})();

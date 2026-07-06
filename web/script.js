// Hero scope trace: a scrolling classification-score line that occasionally
// spikes into "harm" territory and gets flagged, then settles back to safe.
(function () {
  var canvas = document.getElementById("hero-scope");
  if (!canvas) return;

  var ctx = canvas.getContext("2d");
  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var dpr = Math.min(window.devicePixelRatio || 1, 2);

  var W, H;
  function resize() {
    var rect = canvas.getBoundingClientRect();
    W = rect.width;
    H = rect.height;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  window.addEventListener("resize", resize);
  resize();

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  var points = [];
  var n = 160;
  var t = 0;
  var spikeUntil = -1;

  function seed() {
    points = [];
    for (var i = 0; i < n; i++) points.push(0.5);
  }
  seed();

  function step() {
    t += 0.045;
    var base = 0.5 + Math.sin(t) * 0.06 + Math.sin(t * 2.7) * 0.03;
    var noise = (Math.random() - 0.5) * 0.05;
    var v = base + noise;

    if (spikeUntil < t && Math.random() < 0.004) {
      spikeUntil = t + 1.6;
    }
    if (t < spikeUntil) {
      v = 0.82 + Math.sin((spikeUntil - t) * 9) * 0.1 + noise;
    }
    points.push(Math.max(0.05, Math.min(0.95, v)));
    if (points.length > n) points.shift();
  }

  function draw(offsetFrac) {
    ctx.clearRect(0, 0, W, H);

    var safe = css("--safe") || "#4fb894";
    var harm = css("--harm") || "#e2564f";
    var grid = css("--border-soft") || "rgba(255,255,255,0.08)";

    // faint grid
    ctx.strokeStyle = grid;
    ctx.lineWidth = 1;
    var rows = 5;
    for (var r = 0; r <= rows; r++) {
      var y = (H / rows) * r;
      ctx.beginPath();
      ctx.moveTo(0, y + 0.5);
      ctx.lineTo(W, y + 0.5);
      ctx.stroke();
    }

    // threshold line (harm cutoff)
    var threshY = H * (1 - 0.72);
    ctx.strokeStyle = harm;
    ctx.globalAlpha = 0.35;
    ctx.setLineDash([4, 5]);
    ctx.beginPath();
    ctx.moveTo(0, threshY);
    ctx.lineTo(W, threshY);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;

    // trace — rendered with a continuous sub-slot offset so the line glides
    // between data steps instead of jumping a full slot at once
    var stepX = W / (n - 1);
    var offsetPx = (offsetFrac || 0) * stepX;
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.beginPath();
    for (var i = 0; i < points.length; i++) {
      var x = i * stepX - offsetPx;
      var y = H - points[i] * H;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    var last = points[points.length - 1];
    ctx.strokeStyle = last > 0.72 ? harm : safe;
    ctx.stroke();

    // leading dot
    var lx = (points.length - 1) * stepX - offsetPx;
    var ly = H - last * H;
    ctx.fillStyle = ctx.strokeStyle;
    ctx.beginPath();
    ctx.arc(lx, ly, 3.5, 0, Math.PI * 2);
    ctx.fill();
  }

  if (reduceMotion) {
    for (var i = 0; i < 40; i++) step();
    draw();
  } else {
    var framesPerStep = 8; // wall-clock pace of the data itself — unchanged
    var scrollPos = 0; // fractional progress toward the next step, drives a continuous glide
    (function loop() {
      scrollPos += 1 / framesPerStep;
      while (scrollPos >= 1) {
        step();
        scrollPos -= 1;
      }
      draw(scrollPos);
      requestAnimationFrame(loop);
    })();
  }
})();

// Theme toggle: cycles the explicit override; default is the OS preference.
(function () {
  var btn = document.getElementById("theme-toggle");
  if (!btn) return;
  var root = document.documentElement;

  function apply(mode) {
    if (mode) root.setAttribute("data-theme", mode);
    else root.removeAttribute("data-theme");
    btn.textContent = mode === "dark" ? "●" : mode === "light" ? "○" : "◐";
  }

  var saved = localStorage.getItem("sentinel-theme");
  apply(saved);

  btn.addEventListener("click", function () {
    var current = root.getAttribute("data-theme");
    var next = current === "dark" ? "light" : current === "light" ? null : "dark";
    if (next) localStorage.setItem("sentinel-theme", next);
    else localStorage.removeItem("sentinel-theme");
    apply(next);
  });
})();

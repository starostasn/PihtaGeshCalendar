// Лёгкий снегопад на странице входа зимой. Отключается при «уменьшении движения» и в фоновой вкладке.
(function () {
  const page = document.querySelector(".login-page");
  const canvas = document.getElementById("snow");
  if (!page || !canvas || !page.hasAttribute("data-snow")) return;
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const ctx = canvas.getContext("2d");
  let w = 0, h = 0, flakes = [], raf = null;
  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = canvas.clientWidth; h = canvas.clientHeight;
    canvas.width = w * dpr; canvas.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const count = Math.round(Math.min(110, (w * h) / 14000));
    flakes = Array.from({ length: count }, () => ({
      x: Math.random() * w, y: Math.random() * h, r: 0.8 + Math.random() * 2.2,
      s: 0.25 + Math.random() * 0.75, d: Math.random() * Math.PI * 2,
    }));
  }
  function tick() {
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "rgba(255,255,255,0.85)";
    for (const f of flakes) {
      f.y += f.s; f.d += 0.01; f.x += Math.sin(f.d) * 0.3;
      if (f.y > h + 4) { f.y = -4; f.x = Math.random() * w; }
      ctx.beginPath(); ctx.arc(f.x, f.y, f.r, 0, Math.PI * 2); ctx.fill();
    }
    raf = requestAnimationFrame(tick);
  }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { cancelAnimationFrame(raf); raf = null; } else if (!raf) { tick(); }
  });
  window.addEventListener("resize", resize);
  resize();
  tick();
})();

// Просмотр фото дома: клик по превью — полноэкранный просмотр, стрелки ← → и Esc.
(function () {
  const dialog = document.getElementById("lightbox");
  const dataNode = document.getElementById("photo-list");
  if (!dialog || !dataNode) return;
  const photos = JSON.parse(dataNode.textContent);
  if (!photos.length) return;
  const img = dialog.querySelector("img");
  const caption = dialog.querySelector(".lightbox-caption");
  let index = 0;

  function show(i) {
    index = (i + photos.length) % photos.length;
    const p = photos[index];
    img.src = p.src;
    img.alt = p.caption || "";
    caption.textContent = (p.caption ? p.caption + " · " : "") + (index + 1) + " из " + photos.length;
    if (!dialog.open) dialog.showModal();
  }

  document.querySelectorAll("[data-lightbox-index]").forEach((el) => {
    el.addEventListener("click", () => {
      const id = Number(el.dataset.lightboxIndex);
      show(Math.max(0, photos.findIndex((p) => p.id === id)));
    });
  });
  dialog.querySelector(".lb-prev").addEventListener("click", () => show(index - 1));
  dialog.querySelector(".lb-next").addEventListener("click", () => show(index + 1));
  dialog.querySelector(".lb-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });
  dialog.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft") show(index - 1);
    if (e.key === "ArrowRight") show(index + 1);
  });
  // свайп на телефоне
  let startX = null;
  dialog.addEventListener("touchstart", (e) => { startX = e.touches[0].clientX; }, { passive: true });
  dialog.addEventListener("touchend", (e) => {
    if (startX === null) return;
    const dx = e.changedTouches[0].clientX - startX;
    if (Math.abs(dx) > 50) show(index + (dx < 0 ? 1 : -1));
    startX = null;
  });
})();

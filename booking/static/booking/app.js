// Общие действия на всех страницах (вместо встроенных onclick/onsubmit — их запрещает политика безопасности CSP).
(function () {
  // <form data-confirm="Текст вопроса"> — спросить подтверждение перед отправкой
  document.addEventListener("submit", (e) => {
    const form = e.target.closest("form[data-confirm]");
    if (form && !window.confirm(form.dataset.confirm)) e.preventDefault();
  });

  // <button data-copy="id-элемента"> — скопировать текст элемента в буфер обмена
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-copy]");
    if (!btn) return;
    const target = document.getElementById(btn.dataset.copy);
    if (!target) return;
    try {
      await navigator.clipboard.writeText(target.textContent.trim());
      btn.textContent = "Скопировано";
    } catch (err) {
      // Нет доступа к буферу (например, не HTTPS) — выделяем текст, чтобы скопировать вручную
      const range = document.createRange();
      range.selectNodeContents(target);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      btn.textContent = "Выделено — нажмите Ctrl+C";
    }
  });
})();

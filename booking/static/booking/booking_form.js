// Пересчёт стоимости при выборе дат и скрытие полей гостя для «Закрыто».
(function () {
  const form = document.getElementById("bookingForm");
  if (!form) return;
  const ci = form.querySelector("[name=check_in]");
  const co = form.querySelector("[name=check_out]");
  const total = form.querySelector("[name=total_price]");
  const box = document.getElementById("quote");
  const fmt = (n) => n.toLocaleString("ru-RU");
  let totalTouched = total && total.value !== "" && form.dataset.exclude; // у существующей брони сумму не трогаем

  if (total) total.addEventListener("input", () => { totalTouched = true; });

  async function update() {
    if (!ci.value || !co.value || co.value <= ci.value) { box.hidden = true; return; }
    const params = new URLSearchParams({ check_in: ci.value, check_out: co.value });
    if (form.dataset.exclude) params.set("exclude", form.dataset.exclude);
    try {
      const r = await fetch(form.dataset.quoteUrl + "?" + params);
      if (!r.ok) { box.hidden = true; return; }
      const q = await r.json();
      let html = `${q.nights} ноч. · по нашим ценам <b>${fmt(q.total)} ₽</b>`;
      if (!q.min_stay_ok) html += ` · <span class="bad">меньше минимального срока (${q.min_stay} н.)</span>`;
      if (!q.available) html += ` · <span class="bad">даты заняты</span>`;
      box.innerHTML = html;
      box.hidden = false;
      if (total && !totalTouched && !total.disabled) total.value = q.total;
    } catch (e) { box.hidden = true; }
  }

  function toggleKind() {
    const kind = form.querySelector("[name=kind]:checked");
    const isBlock = kind && kind.value === "block";
    form.querySelectorAll(".booking-only").forEach((el) => { el.hidden = isBlock; });
  }

  ci.addEventListener("change", () => {
    if (ci.value && (!co.value || co.value <= ci.value)) {
      const d = new Date(ci.value); d.setDate(d.getDate() + 1);
      co.value = d.toISOString().slice(0, 10);
    }
    update();
  });
  co.addEventListener("change", update);
  form.querySelectorAll("[name=kind]").forEach((el) => el.addEventListener("change", toggleKind));
  toggleKind();
  update();
})();

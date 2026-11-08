// Выбор дат в календаре: первый клик — заезд, второй — выезд.
(function () {
  const hint = document.getElementById("selectHint");
  const hintText = document.getElementById("selectText");
  const days = Array.from(document.querySelectorAll(".day[data-date]"));
  let start = null;

  const fmt = (iso) => iso.split("-").reverse().slice(0, 2).join(".");
  const todayIso = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);

  function clear() {
    start = null;
    days.forEach((d) => d.classList.remove("sel", "in-range"));
    hint.classList.remove("show");
  }

  function highlight(endIso) {
    days.forEach((d) => {
      const iso = d.dataset.date;
      d.classList.toggle("sel", iso === start || iso === endIso);
      d.classList.toggle("in-range", !!endIso && iso > start && iso < endIso);
    });
  }

  days.forEach((el) => {
    el.addEventListener("click", () => {
      const iso = el.dataset.date;
      if (start === null) {
        if (el.dataset.booking) { window.location = el.dataset.booking; return; }
        if (iso < todayIso) return;
        start = iso;
        highlight(null);
        hintText.textContent = "Заезд " + fmt(iso) + " — выберите дату выезда";
        hint.classList.add("show");
        return;
      }
      if (iso <= start) {          // кликнули раньше заезда — начинаем выбор заново с этой даты
        if (el.dataset.booking || iso < todayIso) { clear(); return; }
        start = iso;
        highlight(null);
        hintText.textContent = "Заезд " + fmt(iso) + " — выберите дату выезда";
        return;
      }
      const url = window.BOOKING_NEW_URL + "?check_in=" + start + "&check_out=" + iso;
      window.location = url;
    });
    el.addEventListener("mouseenter", () => { if (start !== null && el.dataset.date > start) highlight(el.dataset.date); });
  });

  document.getElementById("selectCancel").addEventListener("click", clear);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") clear(); });
})();

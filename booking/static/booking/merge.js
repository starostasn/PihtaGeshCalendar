// Объединение броней: если источник итоговой брони — площадка с iCal, даты и статус
// берутся с площадки, поэтому их выбор блокируется и показывается подходящее пояснение.
(function () {
  const form = document.getElementById("mergeForm");
  const ownersEl = document.getElementById("merge-owners");
  if (!form || !ownersEl) return;
  const owners = JSON.parse(ownersEl.textContent);

  function update() {
    const checked = form.querySelector("input[name=choice_channel]:checked");
    const owner = owners[checked ? checked.value : "a"] || "";
    form.querySelectorAll("[data-owner-locked] input[type=radio]").forEach((r) => {
      r.disabled = Boolean(owner);
      if (owner) r.checked = r.value === owner;
    });
    form.querySelectorAll("[data-mode]").forEach((el) => {
      el.hidden = el.dataset.mode !== (owner ? "owner" : "own");
    });
  }
  form.addEventListener("change", (e) => { if (e.target.name === "choice_channel") update(); });
  update();
})();

document.querySelectorAll('input[type="radio"][name="year"], input[type="radio"][name="compare"]').forEach(input => {
  input.addEventListener("change", () => input.form.requestSubmit());
});

// Keep the native month input as the submitted value and the fallback without JS.
const period = document.getElementById("id_period");
const picker = document.getElementById("periodPicker");
if (period && picker) {
  const months = picker.querySelector("select");
  const yearButtons = [...picker.querySelectorAll("input")];
  const [year, month] = period.value.split("-");
  const initial = yearButtons.find(button => button.value === (year || ""));
  if (initial) {
    initial.checked = true;
    months.value = month || "";
    picker.hidden = false;
    period.hidden = true;
    period.before(picker);
    const label = period.labels[0];
    if (label) label.htmlFor = months.id;
    const other = document.createElement("button");
    other.type = "button";
    other.className = "btn small";
    other.textContent = "Другой год";
    picker.append(other);
    other.addEventListener("click", () => {
      picker.hidden = true;
      months.required = false;
      period.hidden = false;
      if (label) label.htmlFor = period.id;
      period.focus();
    });
    function update() {
      const selected = yearButtons.find(button => button.checked)?.value;
      months.disabled = !selected;
      months.required = Boolean(selected);
      period.value = selected && months.value ? selected + "-" + months.value : "";
    }
    picker.addEventListener("change", update);
    update();
  }
}

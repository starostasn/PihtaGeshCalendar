// Выбор гостя из справочника: поиск по имени/телефону с подсказками.
// Используется в форме брони (#guestPicker) и в объединении гостей (#mergePicker).
(function () {
  function setup(root) {
    if (!root) return;
    const hidden = root.querySelector("input[type=hidden]");
    const input = root.querySelector(".gp-input");
    const list = root.querySelector(".gp-results");
    const selected = root.querySelector(".gp-selected");
    const search = root.querySelector(".gp-search");
    const url = root.dataset.searchUrl;
    let timer = null;
    let seq = 0;

    function choose(g) {
      hidden.value = g.id;
      const name = selected.querySelector(".gp-name");
      name.textContent = g.name;
      name.href = g.url;
      const phone = selected.querySelector(".gp-phone");
      phone.textContent = g.phone || "нет телефона";
      selected.hidden = false;
      search.hidden = true;
      list.hidden = true;
      root.dispatchEvent(new CustomEvent("guest-chosen", { detail: g }));
    }

    function render(results) {
      list.replaceChildren();
      if (!results.length) {
        const li = document.createElement("li");
        li.className = "gp-empty muted";
        li.textContent = "Никого не нашли";
        list.append(li);
      }
      for (const g of results) {
        const li = document.createElement("li");
        const btn = document.createElement("button");
        btn.type = "button";
        const b = document.createElement("b");
        b.textContent = g.name;
        const meta = document.createElement("span");
        meta.className = "muted small";
        meta.textContent = [g.phone || "нет телефона", g.city, g.bookings ? g.bookings + " брон." : ""].filter(Boolean).join(" · ");
        btn.append(b, " ", meta);
        btn.addEventListener("click", () => choose(g));
        li.append(btn);
        list.append(li);
      }
      list.hidden = false;
    }

    input.addEventListener("input", () => {
      clearTimeout(timer);
      const q = input.value.trim();
      if (q.length < 2) { list.hidden = true; return; }
      timer = setTimeout(async () => {
        const my = ++seq;
        const params = new URLSearchParams({ q });
        if (root.dataset.exclude) params.set("exclude", root.dataset.exclude);
        try {
          const r = await fetch(url + "?" + params);
          const data = await r.json();
          if (my === seq) render(data.results || []);
        } catch (e) { list.hidden = true; }
      }, 250);
    });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") e.preventDefault(); });

    const clear = root.querySelector("[data-gp-clear]");
    if (clear) clear.addEventListener("click", () => {
      hidden.value = "";
      selected.hidden = true;
      search.hidden = false;
      input.value = "";
      input.focus();
    });
  }
  setup(document.getElementById("guestPicker"));
  setup(document.getElementById("mergePicker"));
})();

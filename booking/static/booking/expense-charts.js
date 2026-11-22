(() => {
  const money = new Intl.NumberFormat("ru-RU", { style: "currency", currency: "RUB" });
  const compact = new Intl.NumberFormat("ru-RU", { notation: "compact" });
  if (!window.Chart) return;

  function draw() {
    const css = getComputedStyle(document.documentElement);
    const token = name => css.getPropertyValue(name).trim();
    const colors = [1, 2, 3].map(n => token("--series-" + n));
    const [primary, secondary, tertiary] = colors;
    Chart.defaults.color = token("--muted");
    Chart.defaults.borderColor = token("--grid");
    Chart.defaults.datasets.bar.borderRadius = 4;
    function render(id, type, makeData, extra = {}) {
      const canvas = document.getElementById(id);
      if (!canvas) return;
      const rows = JSON.parse(document.getElementById(id + "-data").textContent);
      Chart.getChart(canvas)?.destroy();
      new Chart(canvas, {
        type, data: makeData(rows),
        options: {
          responsive: true, maintainAspectRatio: false, locale: "ru-RU",
          plugins: {
            legend: { display: id !== "expense-categories", position: "bottom", labels: { usePointStyle: true, boxWidth: 10 } },
            tooltip: { backgroundColor: token("--card"), borderColor: token("--line"), borderWidth: 1,
              titleColor: token("--muted"), bodyColor: token("--text"), padding: 10, cornerRadius: 8, callbacks: { label: item =>
              (item.dataset.label || item.label) + ": " + money.format(Number(item.raw)) } }
          }, ...extra
        }
      });
    }
    const groupColors = { "Строительство": primary, "Содержание": secondary, "Налог на аренду": tertiary };
    render("expense-mix", "doughnut", rows => ({
      labels: rows.map(r => r.label),
      datasets: [{ data: rows.map(r => Number(r.amount)), backgroundColor: rows.map(r => groupColors[r.label]) }]
    }), { cutout: "64%" });
    const amountAxis = { beginAtZero: true, ticks: { callback: value => compact.format(value) } };
    render("expense-trend", "bar", rows => ({
      labels: rows.map(r => r.label),
      datasets: (rows[0]?.parts || []).map((part, i) => ({
        label: part.label, data: rows.map(r => Number(r.parts[i].amount)), backgroundColor: colors[i]
      }))
    }), { scales: { x: { stacked: true }, y: { ...amountAxis, stacked: true } } });
    render("expense-categories", "bar", rows => ({
      labels: rows.slice(0, 10).map(r => r.label),
      datasets: [{ label: "Расходы", data: rows.slice(0, 10).map(r => Number(r.amount)), backgroundColor: primary }]
    }), { indexAxis: "y", scales: { x: amountAxis } });
    render("expense-funders", "bar", rows => ({
      labels: rows.map(r => r.label),
      datasets: [
        { label: "Оплаты", data: rows.map(r => Number(r.paid)), backgroundColor: primary },
        { label: "Взносы", data: rows.map(r => Number(r.deposits)), backgroundColor: secondary },
        { label: "Возвраты", data: rows.map(r => -Number(r.refunds)), backgroundColor: tertiary }
      ]
    }), { indexAxis: "y", scales: { x: { ...amountAxis, stacked: true }, y: { stacked: true } } });
    render("expense-comparison", "bar", row => ({
      labels: row.labels,
      datasets: [
        { label: String(row.year), data: row.values.map(Number), backgroundColor: primary },
        { label: String(row.baseline), data: row.base_values.map(Number), backgroundColor: secondary }
      ]
    }), { scales: { y: amountAxis } });
  }
  draw();
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", draw);
})();

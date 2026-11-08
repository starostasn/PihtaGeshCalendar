// Графики страницы «Статистика». Цвета берутся из CSS-переменных, поэтому светлая
// и тёмная темы работают без отдельной настройки. Одна серия на график — без легенды,
// название графика говорит, что показано. Значения дублируются в таблицах под графиками.
(function () {
  const form = document.getElementById("statsFilters");
  const custom = document.getElementById("customRange");
  form.querySelectorAll("input[name=period]").forEach((el) => {
    el.addEventListener("change", () => {
      if (el.value === "custom") { custom.hidden = false; return; }
      custom.hidden = true;
      form.querySelectorAll(".custom-range input").forEach((i) => { i.disabled = true; });
      form.submit();
    });
  });

  if (!window.Chart) return;
  const data = JSON.parse(document.getElementById("chart-data").textContent);
  const nf = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 });
  const rub = (v) => nf.format(Math.round(v)) + " ₽";
  const compactRub = (v) => {
    if (Math.abs(v) >= 1e6) return (v / 1e6).toLocaleString("ru-RU", { maximumFractionDigits: 1 }) + " млн";
    if (Math.abs(v) >= 1e4) return nf.format(v / 1000) + " тыс";
    return nf.format(v);
  };
  const pct = (v) => (v === null || v === undefined ? "—" : nf.format(v) + "%");

  function tokens() {
    const cs = getComputedStyle(document.documentElement);
    const v = (n) => cs.getPropertyValue(n).trim();
    return {
      series: v("--series-1"),
      seriesSoft: v("--series-1-soft"),
      grid: v("--grid"),
      axis: v("--axis"),
      muted: v("--muted"),
      text: v("--text"),
      surface: v("--card"),
      line: v("--line"),
    };
  }

  function options(t, { horizontal = false, valueFmt = (v) => v, tickFmt = valueFmt, max, title, label, footer }) {
    const valueAxis = {
      beginAtZero: true,
      max,
      grid: { color: t.grid, lineWidth: 1, drawTicks: false },
      border: { display: false },
      ticks: { color: t.muted, font: { size: 11 }, padding: 6, maxTicksLimit: 5, callback: (v) => tickFmt(v) },
    };
    const catAxis = {
      grid: { display: false },
      border: { color: t.axis, width: 1 },
      ticks: { color: t.muted, font: { size: 11 }, autoSkip: true, maxRotation: 0 },
    };
    return {
      indexAxis: horizontal ? "y" : "x",
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 250 },
      layout: { padding: { top: 4, right: 8 } },
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: t.surface,
          borderColor: t.line,
          borderWidth: 1,
          titleColor: t.muted,
          titleFont: { size: 12, weight: "normal" },
          bodyColor: t.text,
          bodyFont: { size: 14, weight: "bold" },
          footerColor: t.muted,
          footerFont: { size: 12, weight: "normal" },
          footerMarginTop: 2,
          padding: 10,
          cornerRadius: 8,
          displayColors: false,
          callbacks: {
            title: title || ((items) => items[0].label),
            label: label || ((item) => valueFmt(item.raw)),
            footer: footer || (() => ""),
          },
        },
      },
      scales: horizontal ? { x: valueAxis, y: catAxis } : { x: catAxis, y: valueAxis },
    };
  }

  function bars(id, labels, values, t, opts = {}) {
    const el = document.getElementById(id);
    if (!el) return null;
    return new Chart(el, {
      type: "bar",
      data: {
        labels,
        datasets: [{
          data: values,
          backgroundColor: opts.colors || t.series,
          hoverBackgroundColor: opts.hoverColors || opts.colors || t.series,
          borderRadius: 4,
          borderSkipped: "start",
          maxBarThickness: 24,
          categoryPercentage: 0.8,
          barPercentage: 0.9,
        }],
      },
      options: options(t, opts),
    });
  }

  let charts = [];
  function render() {
    charts.forEach((c) => c && c.destroy());
    const t = tokens();
    const s = data.series;
    const futureColors = s.map((x) => (x.future ? t.seriesSoft : t.series));

    charts = [
      bars("chartRevenue", s.map((x) => x.label), s.map((x) => x.revenue), t, {
        colors: futureColors,
        valueFmt: rub,
        tickFmt: compactRub,
        label: (item) => rub(s[item.dataIndex].revenue),
        footer: (items) => {
          const x = s[items[0].dataIndex];
          return (x.future ? "будущие брони · " : "") + "загрузка " + pct(x.occupancy);
        },
      }),
      bars("chartOccupancy", s.map((x) => x.label), s.map((x) => x.occupancy), t, {
        colors: futureColors,
        max: 100,
        valueFmt: pct,
        label: (item) => pct(s[item.dataIndex].occupancy),
        footer: (items) => {
          const x = s[items[0].dataIndex];
          return `занято ${x.occupied} из ${x.available} ноч.`;
        },
      }),
      bars("chartSources", data.sources.map((x) => x.name), data.sources.map((x) => x.revenue), t, {
        horizontal: true,
        valueFmt: rub,
        tickFmt: compactRub,
        label: (item) => rub(data.sources[item.dataIndex].revenue),
        footer: (items) => {
          const x = data.sources[items[0].dataIndex];
          return `${x.share}% выручки · ${x.bookings} брон. · ${x.nights} ноч.`;
        },
      }),
      bars("chartWeekdays", data.weekdays.map((x) => x.label), data.weekdays.map((x) => x.occupancy), t, {
        max: 100,
        valueFmt: pct,
        label: (item) => pct(item.raw),
        footer: (items) => `занятых ночей: ${data.weekdays[items[0].dataIndex].nights}`,
      }),
      bars("chartStay", data.stay.map((x) => x.label), data.stay.map((x) => x.count), t, {
        valueFmt: (v) => nf.format(v),
        title: (items) => "Ночей: " + items[0].label,
        label: (item) => item.raw + " заезд(ов)",
      }),
      bars("chartLead", data.lead.map((x) => x.label), data.lead.map((x) => x.count), t, {
        valueFmt: (v) => nf.format(v),
        title: (items) => "За " + items[0].label.replace(" дня", "") + " дн. до заезда",
        label: (item) => item.raw + " брон.",
      }),
    ];
  }

  render();
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  if (mq.addEventListener) mq.addEventListener("change", render);
})();

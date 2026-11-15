// Графики страницы «Статистика». Цвета берутся из CSS-переменных, поэтому светлая
// и тёмная темы работают без отдельной настройки. Одна серия на график — без легенды,
// название графика говорит, что показано (кроме сравнения лет: там легенда и подписи у линий).
// Значения дублируются в таблицах под графиками.
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

  // ---- сравнение лет и сезонов: по линии на год, цвет закреплён за годом
  // (самый свежий — первый цвет палитры), будущие месяцы — пунктиром.
  const compare = JSON.parse(document.getElementById("compare-data").textContent);
  const cmpMetric = document.getElementById("cmpMetric");
  const METRICS = {
    revenue: { fmt: rub, tick: compactRub },
    occupancy: { fmt: pct, tick: pct, max: 100 },
    occupied: { fmt: (v) => nf.format(v) + " ноч.", tick: (v) => nf.format(v) },
    adr: { fmt: rub, tick: compactRub },
    arrivals: { fmt: (v) => nf.format(v), tick: (v) => nf.format(v) },
  };
  const MONTHS_FULL = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"];

  function cmpMode() {
    const el = document.querySelector("input[name=cmpMode]:checked");
    return el ? el.value : "year";
  }

  function seriesColors(periods) {
    const cs = getComputedStyle(document.documentElement);
    // самый свежий год — слот 1, предыдущий — слот 2 и т. д.; не больше 8 линий
    return periods.map((_, i) => cs.getPropertyValue(`--series-${periods.length - i}`).trim());
  }

  // Подписи у концов линий (при 4 линиях и меньше), раздвинутые, чтобы не налезали друг на друга.
  const endLabels = {
    id: "endLabels",
    afterDatasetsDraw(chart, _args, opts) {
      if (!opts || !opts.enabled) return;
      const { ctx, chartArea } = chart;
      const items = [];
      chart.data.datasets.forEach((ds, i) => {
        const meta = chart.getDatasetMeta(i);
        if (meta.hidden) return;
        for (let j = ds.data.length - 1; j >= 0; j--) {
          if (ds.data[j] !== null && ds.data[j] !== undefined) {
            items.push({ text: ds.label, x: meta.data[j].x, y: meta.data[j].y });
            break;
          }
        }
      });
      items.sort((a, b) => a.y - b.y);
      for (let i = 1; i < items.length; i++) {
        if (items[i].y - items[i - 1].y < 14) items[i].y = items[i - 1].y + 14;
      }
      const overflow = items.length ? items[items.length - 1].y - chartArea.bottom : 0;
      if (overflow > 0) items.forEach((it) => { it.y -= overflow; });
      ctx.save();
      ctx.font = "600 11px system-ui, -apple-system, 'Segoe UI', sans-serif";
      ctx.fillStyle = opts.color;
      ctx.textBaseline = "middle";
      items.forEach((it) => ctx.fillText(it.text, it.x + 8, it.y));
      ctx.restore();
    },
  };

  function renderCompare(t) {
    const el = document.getElementById("chartCompare");
    if (!el) return null;
    const mode = cmpMode();
    const metricKey = cmpMetric.value;
    const metric = METRICS[metricKey];
    const block = compare.modes[mode];
    const periods = block.periods;
    const colors = seriesColors(periods);

    document.querySelectorAll("[data-cmp-mode]").forEach((node) => { node.hidden = node.dataset.cmpMode !== mode; });
    document.querySelectorAll(`[data-cmp-mode=${mode}] .swatch`).forEach((sw) => {
      const idx = periods.findIndex((p) => String(p.key) === sw.dataset.seriesKey);
      if (idx >= 0) sw.style.setProperty("--c", colors[idx]);
    });

    const firstMonth = mode === "year" ? 0 : 10;
    const direct = periods.length <= 4;
    const datasets = periods.map((p, i) => ({
      label: p.label,
      // будущие месяцы, на которые ещё нет броней, не рисуем (иначе линия падает в ноль)
      data: p.months.map((m) => (!m || (m.future && !m.occupied) ? null : m[metricKey])),
      borderColor: colors[i],
      backgroundColor: colors[i],
      borderWidth: 2,
      pointRadius: 4,
      pointHoverRadius: 6,
      pointBorderColor: t.surface,
      pointBorderWidth: 2,
      pointHoverBorderWidth: 2,
      cubicInterpolationMode: "monotone",
      spanGaps: false,
      segment: {
        borderDash: (ctx) => (p.months[ctx.p1DataIndex] && p.months[ctx.p1DataIndex].future ? [5, 4] : undefined),
      },
    }));

    const base = options(t, {
      valueFmt: metric.fmt,
      tickFmt: metric.tick,
      max: metric.max,
      title: (items) => {
        const idx = (firstMonth + items[0].dataIndex) % 12;
        return MONTHS_FULL[idx].charAt(0).toUpperCase() + MONTHS_FULL[idx].slice(1);
      },
      label: (item) => {
        const m = periods[item.datasetIndex].months[item.dataIndex];
        if (!m) return null;
        return `${item.dataset.label}: ${metric.fmt(item.raw)}${m.future ? " (уже забронировано)" : ""}`;
      },
    });
    base.plugins.tooltip.displayColors = true;
    base.plugins.tooltip.usePointStyle = true;
    base.plugins.tooltip.bodyFont = { size: 13, weight: "normal" };
    base.plugins.tooltip.itemSort = (a, b) => b.datasetIndex - a.datasetIndex;
    base.plugins.legend = {
      display: true,
      position: "top",
      align: "start",
      labels: {
        color: t.text,
        boxWidth: 18,
        boxHeight: 3,
        font: { size: 12 },
        padding: 14,
        generateLabels(chart) {
          return chart.data.datasets.map((ds, i) => ({
            text: ds.label,
            fillStyle: ds.borderColor,
            strokeStyle: ds.borderColor,
            lineWidth: 0,
            fontColor: t.text,
            hidden: !chart.isDatasetVisible(i),
            datasetIndex: i,
          }));
        },
      },
    };
    base.plugins.endLabels = { enabled: direct, color: t.text };
    base.layout.padding.right = direct ? 52 : 8;
    base.scales.x.grid = { display: false };

    return new Chart(el, {
      type: "line",
      data: { labels: block.months, datasets },
      options: base,
      plugins: [endLabels],
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
      renderCompare(t),
    ];
  }
  document.querySelectorAll("input[name=cmpMode]").forEach((el) => el.addEventListener("change", render));
  cmpMetric.addEventListener("change", render);

  render();
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  if (mq.addEventListener) mq.addEventListener("change", render);
})();

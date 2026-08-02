(() => {
  const range = window.ENVIROPI_RANGE || "24h";
  const gridColor = "rgba(255,255,255,0.08)";
  const tickColor = "#9bb0a6";

  const baseOptions = {
    responsive: true,
    maintainAspectRatio: true,
    animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: {
        labels: { color: tickColor, boxWidth: 12 },
      },
    },
    scales: {
      x: {
        ticks: { color: tickColor, maxTicksLimit: 8 },
        grid: { color: gridColor },
      },
      y: {
        ticks: { color: tickColor },
        grid: { color: gridColor },
      },
    },
  };

  function labels(points) {
    return points.map((p) => {
      const d = new Date(p.ts);
      return d.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
    });
  }

  function series(points, key) {
    return points.map((p) => p[key]);
  }

  async function load() {
    const res = await fetch(`/api/history?range=${encodeURIComponent(range)}`);
    if (!res.ok) return;
    const data = await res.json();
    const points = data.points || [];
    const lbs = labels(points);

    new Chart(document.getElementById("chart-climate"), {
      type: "line",
      data: {
        labels: lbs,
        datasets: [
          {
            label: "Temp °C",
            data: series(points, "temperature"),
            borderColor: "#3dba7a",
            backgroundColor: "transparent",
            tension: 0.25,
            pointRadius: 0,
          },
          {
            label: "Humidity %",
            data: series(points, "humidity"),
            borderColor: "#6cb6ff",
            backgroundColor: "transparent",
            tension: 0.25,
            pointRadius: 0,
          },
          {
            label: "Pressure hPa",
            data: series(points, "pressure"),
            borderColor: "#c4a35a",
            backgroundColor: "transparent",
            tension: 0.25,
            pointRadius: 0,
            yAxisID: "y1",
          },
        ],
      },
      options: {
        ...baseOptions,
        scales: {
          ...baseOptions.scales,
          y1: {
            position: "right",
            ticks: { color: tickColor },
            grid: { drawOnChartArea: false },
          },
        },
      },
    });

    new Chart(document.getElementById("chart-gas"), {
      type: "line",
      data: {
        labels: lbs,
        datasets: [
          {
            label: "Reducing",
            data: series(points, "gas_reducing"),
            borderColor: "#e07060",
            tension: 0.25,
            pointRadius: 0,
            backgroundColor: "transparent",
          },
          {
            label: "Oxidising",
            data: series(points, "gas_oxidising"),
            borderColor: "#d4a017",
            tension: 0.25,
            pointRadius: 0,
            backgroundColor: "transparent",
          },
          {
            label: "NH3",
            data: series(points, "gas_nh3"),
            borderColor: "#9b7bff",
            tension: 0.25,
            pointRadius: 0,
            backgroundColor: "transparent",
          },
        ],
      },
      options: baseOptions,
    });

    new Chart(document.getElementById("chart-ambient"), {
      type: "line",
      data: {
        labels: lbs,
        datasets: [
          {
            label: "Lux",
            data: series(points, "lux"),
            borderColor: "#f0d060",
            tension: 0.25,
            pointRadius: 0,
            backgroundColor: "transparent",
          },
          {
            label: "Noise",
            data: series(points, "noise"),
            borderColor: "#7ec8e3",
            tension: 0.25,
            pointRadius: 0,
            backgroundColor: "transparent",
            yAxisID: "y1",
          },
        ],
      },
      options: {
        ...baseOptions,
        scales: {
          ...baseOptions.scales,
          y1: {
            position: "right",
            ticks: { color: tickColor },
            grid: { drawOnChartArea: false },
          },
        },
      },
    });
  }

  async function refreshLatest() {
    try {
      const res = await fetch("/api/latest");
      if (!res.ok) return;
      const data = await res.json();
      const sample = data.sample;
      if (!sample) return;
      document.querySelectorAll("[data-metric]").forEach((el) => {
        const key = el.getAttribute("data-metric");
        const v = sample[key];
        el.textContent = v == null ? "—" : Number(v).toFixed(1);
      });
      const ts = document.getElementById("latest-ts");
      if (ts) {
        ts.dateTime = sample.ts;
        ts.textContent = sample.ts;
      }
    } catch (_) {
      /* ignore */
    }
  }

  load();
  setInterval(refreshLatest, 30000);
})();

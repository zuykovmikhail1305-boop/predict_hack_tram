// Графики — Chart.js
let chartInstance = null;

function renderChart(forecasts, granularity = 'hour') {
  const canvas = document.getElementById('chartCanvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');

  if (chartInstance) chartInstance.destroy();

  // Агрегируем по времени
  const labels = [];
  const dataPoints = [];
  const dataLower = [];
  const dataUpper = [];

  // Группируем по timestamp
  const grouped = {};
  forecasts.forEach(f => {
    const key = f.timestamp || `${f.route_id}_${f.hour}`;
    if (!grouped[key]) {
      grouped[key] = { ...f };
    } else {
      grouped[key].passengers_predicted += f.passengers_predicted;
      if (f.passengers_lower) grouped[key].passengers_lower = (grouped[key].passengers_lower || 0) + f.passengers_lower;
      if (f.passengers_upper) grouped[key].passengers_upper = (grouped[key].passengers_upper || 0) + f.passengers_upper;
    }
  });

  const sorted = Object.values(grouped).sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));

  sorted.forEach(f => {
    const d = new Date(f.timestamp);
    let label;
    if (granularity === 'hour') {
      label = `${d.toLocaleDateString('ru')} ${f.hour}:00`;
    } else if (granularity === 'month') {
      label = d.toLocaleDateString('ru', { month: 'long', year: 'numeric' });
    } else {
      label = d.toLocaleDateString('ru');
    }
    labels.push(label);
    dataPoints.push(f.passengers_predicted);
    dataLower.push(f.passengers_lower || null);
    dataUpper.push(f.passengers_upper || null);
  });

  chartInstance = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: 'Прогноз',
          data: dataPoints,
          borderColor: '#6c8cff',
          backgroundColor: 'rgba(108, 140, 255, 0.1)',
          fill: true,
          tension: 0.3,
          pointRadius: 3,
          pointHoverRadius: 6,
        },
        {
          label: 'Верхняя граница',
          data: dataUpper,
          borderColor: 'rgba(108, 140, 255, 0.3)',
          borderDash: [5, 5],
          pointRadius: 0,
          fill: false,
        },
        {
          label: 'Нижняя граница',
          data: dataLower,
          borderColor: 'rgba(108, 140, 255, 0.3)',
          borderDash: [5, 5],
          pointRadius: 0,
          fill: false,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: '#e4e6f0' } },
        tooltip: {
          mode: 'index',
          intersect: false,
          callbacks: {
            label: ctx => `${ctx.dataset.label}: ${ctx.raw?.toLocaleString('ru')} пасс.`,
          },
        },
      },
      scales: {
        x: {
          ticks: { color: '#a0a3b5', maxRotation: 45 },
          grid: { color: 'rgba(255,255,255,0.05)' },
        },
        y: {
          beginAtZero: true,
          ticks: {
            color: '#a0a3b5',
            callback: v => v >= 1000 ? `${(v / 1000).toFixed(1)}K` : v,
          },
          grid: { color: 'rgba(255,255,255,0.05)' },
        },
      },
      interaction: { mode: 'nearest', axis: 'x' },
    },
  });
}
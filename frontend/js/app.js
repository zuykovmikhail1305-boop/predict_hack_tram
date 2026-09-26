// Главное приложение
let allRoutes = [];
let allStops = [];
let currentForecasts = [];
let currentGranularity = 'hour';

document.addEventListener('DOMContentLoaded', async () => {
  // Инициализация
  initMap();

  // Загружаем маршруты
  try {
    allRoutes = await api.getRoutes();
    populateRoutes(allRoutes);
  } catch (e) {
    console.error('Ошибка загрузки маршрутов:', e);
  }

  // Загружаем все остановки для карты
  try {
    allStops = await api.getStops();
  } catch (e) {
    console.error('Ошибка загрузки остановок:', e);
  }

  // Устанавливаем даты по умолчанию
  const today = new Date();
  document.getElementById('fromDate').value = '2025-11-01';
  document.getElementById('toDate').value = '2025-12-31';

  // События
  document.getElementById('routeSelect').addEventListener('change', onRouteChange);
  document.getElementById('applyScenarioBtn').addEventListener('click', onApplyScenario);
  document.getElementById('fromDate').addEventListener('change', onFilterChange);
  document.getElementById('toDate').addEventListener('change', onFilterChange);
  document.getElementById('granularity').addEventListener('change', onFilterChange);
  document.getElementById('exportCsvBtn').addEventListener('click', () => exportForecast('csv'));
  document.getElementById('exportXlsxBtn').addEventListener('click', () => exportForecast('xlsx'));

  // Ползунки
  ['weatherFactor', 'eventFactor', 'seasonFactor'].forEach(id => {
    const el = document.getElementById(id);
    el.addEventListener('input', () => {
      document.getElementById(`${id}Val`).textContent = parseFloat(el.value).toFixed(2);
    });
  });

  // Авто-загрузка прогноза при старте
  if (allRoutes.length > 0) {
    document.getElementById('routeSelect').value = allRoutes[0].id;
    await loadForecast(allRoutes[0].id);
  }
});

function populateRoutes(routes) {
  const sel = document.getElementById('routeSelect');
  // Сохраняем placeholder
  sel.innerHTML = '<option value="">— Выберите маршрут —</option>';
  routes.forEach(r => {
    const opt = document.createElement('option');
    opt.value = r.id;
    opt.textContent = `Маршрут ${r.number}${r.name ? ` · ${r.name}` : ''}`;
    sel.appendChild(opt);
  });
}

async function onRouteChange() {
  const routeId = parseInt(document.getElementById('routeSelect').value);
  if (!routeId) return;

  // Показываем остановки маршрута на карте
  const stops = allStops.filter(s => s.route_id === routeId);
  clearMapMarkers();
  stops.forEach(s => addRouteStop(s, true));
  addRoutePolyline(stops);

  await loadForecast(routeId);
}

async function loadForecast(routeId) {
  const fromDate = document.getElementById('fromDate').value;
  const toDate = document.getElementById('toDate').value;
  const granularity = document.getElementById('granularity').value;
  currentGranularity = granularity;

  try {
    currentForecasts = await api.getForecast({
      route: routeId,
      from_date: fromDate || undefined,
      to_date: toDate || undefined,
      granularity,
    });

    // Рендерим
    renderChart(currentForecasts, granularity);
    renderStats(currentForecasts);
    renderBusinessValue(currentForecasts, routeId);

    // Heatmap на карте
    const stops = allStops.filter(s => s.route_id === routeId);
    if (stops.length > 0) {
      showHeatmap(currentForecasts, stops);
    }
  } catch (e) {
    console.error('Ошибка загрузки прогноза:', e);
    document.getElementById('statsSummary').innerHTML = `<p class="error">⚠️ ${e.message}</p>`;
  }
}

function onFilterChange() {
  const routeId = parseInt(document.getElementById('routeSelect').value);
  if (routeId) loadForecast(routeId);
}

async function onApplyScenario() {
  const weatherFactor = parseFloat(document.getElementById('weatherFactor').value);
  const eventFactor = parseFloat(document.getElementById('eventFactor').value);
  const seasonFactor = parseFloat(document.getElementById('seasonFactor').value);

  try {
    const scenario = await api.applyScenario({
      weather_factor: weatherFactor,
      event_factor: eventFactor,
      season_factor: seasonFactor,
      name: `Сценарий ${new Date().toLocaleString('ru')}`,
    });
    // Применяем коэффициенты локально к прогнозу
    if (currentForecasts.length > 0) {
      const adjusted = currentForecasts.map(f => ({
        ...f,
        passengers_predicted: Math.round(f.passengers_predicted * weatherFactor * eventFactor * seasonFactor),
        passengers_lower: f.passengers_lower ? Math.round(f.passengers_lower * weatherFactor * eventFactor * seasonFactor) : null,
        passengers_upper: f.passengers_upper ? Math.round(f.passengers_upper * weatherFactor * eventFactor * seasonFactor) : null,
      }));
      renderChart(adjusted, currentGranularity);
      renderStats(adjusted);
      renderBusinessValue(adjusted, currentForecasts[0]?.route_id);
    }
  } catch (e) {
    console.error('Ошибка применения сценария:', e);
    alert('⚠️ Не удалось применить сценарий');
  }
}

function renderStats(forecasts) {
  const el = document.getElementById('statsSummary');
  if (!forecasts.length) {
    el.innerHTML = '<p class="hint">Нет данных для отображения</p>';
    return;
  }

  const total = forecasts.reduce((s, f) => s + f.passengers_predicted, 0);
  const max = Math.max(...forecasts.map(f => f.passengers_predicted));
  const avg = Math.round(total / forecasts.length);
  const dates = forecasts.map(f => new Date(f.timestamp).getTime());
  const days = dates.length > 1 ? Math.round((Math.max(...dates) - Math.min(...dates)) / 86400000) + 1 : 1;

  el.innerHTML = `
    <div class="stat-item">
      <div class="stat-label">Всего пассажиров</div>
      <div class="stat-value">${total.toLocaleString('ru')}</div>
    </div>
    <div class="stat-item">
      <div class="stat-label">Пик (макс. час)</div>
      <div class="stat-value">${max.toLocaleString('ru')}</div>
    </div>
    <div class="stat-item">
      <div class="stat-label">Среднее</div>
      <div class="stat-value">${avg.toLocaleString('ru')}</div>
    </div>
    <div class="stat-item">
      <div class="stat-label">Период (дней)</div>
      <div class="stat-value">${days}</div>
    </div>
  `;
}

function renderBusinessValue(forecasts, routeId) {
  const el = document.getElementById('businessValue');
  if (!forecasts.length || !routeId) {
    el.innerHTML = '<p class="hint">Выберите маршрут для расчёта</p>';
    return;
  }

  const capacity = 180; // вместимость одного вагона
  const peak = Math.max(...forecasts.map(f => f.passengers_predicted));
  const avgHourly = Math.round(forecasts.reduce((s, f) => s + f.passengers_predicted, 0) / forecasts.length);
  const total = forecasts.reduce((s, f) => s + f.passengers_predicted, 0);
  const carsNeeded = Math.ceil(peak / capacity);
  const currentFleet = Math.max(1, Math.round(carsNeeded * 0.7));
  const extraNeeded = Math.max(0, carsNeeded - currentFleet);

  const overloadRatio = peak / capacity;
  const status = overloadRatio > 0.85 ? '🔴 Высокая' : overloadRatio > 0.6 ? '🟡 Средняя' : '🟢 Низкая';
  const riskClass = overloadRatio > 0.85 ? 'risk-high' : overloadRatio > 0.6 ? 'risk-mid' : 'risk-low';

  // Финансовая оценка (условная)
  const costPerCarPerHour = 1200; // руб/час эксплуатации вагона
  const dailyCost = (carsNeeded * 18 * costPerCarPerHour).toLocaleString('ru'); // 18 часов работы
  const potentialSavings = extraNeeded > 0
    ? (extraNeeded * 18 * costPerCarPerHour * 0.3).toLocaleString('ru')
    : '0';

  el.innerHTML = `
    <div class="business-item">
      <span class="label">🚃 Пик загрузки</span>
      <span class="value">${peak.toLocaleString('ru')} чел.</span>
    </div>
    <div class="business-item">
      <span class="label">🚋 Рекомендуемый выпуск</span>
      <span class="value">${carsNeeded} вагонов</span>
    </div>
    <div class="business-item">
      <span class="label">⚠️ Риск переполнения</span>
      <span class="value ${riskClass}">${status}</span>
    </div>
    <div class="business-item">
      <span class="label">➕ Дополнительно нужно</span>
      <span class="value">${extraNeeded > 0 ? extraNeeded + ' ваг.' : '✅ Достаточно'}</span>
    </div>
    <div class="business-item">
      <span class="label">💰 Стоимость эксплуатации/день</span>
      <span class="value">≈ ${dailyCost} ₽</span>
    </div>
    <div class="business-item" style="border-bottom: none;">
      <span class="label">📉 Оптимизация (экономия)</span>
      <span class="value risk-low">до ${potentialSavings} ₽/день</span>
    </div>
    <p style="color: #a0a3b5; font-size: 0.7rem; margin-top: 0.5rem;">
      📌 Прогноз позволяет распределить ${carsNeeded} вагонов по часам пик, 
      снизив переполнение на ${(overloadRatio > 1 ? 100 : Math.round((1 - overloadRatio) * 100))}%.
    </p>
  `;
}

async function exportForecast(format) {
  const routeId = parseInt(document.getElementById('routeSelect').value);
  await api.exportData(format, routeId || undefined);
}
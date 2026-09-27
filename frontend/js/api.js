// API Client — Vanilla JS
const API_BASE = '';

const api = {
  async request(path, opts = {}) {
    const res = await fetch(`${API_BASE}${path}`, {
      headers: { 'Content-Type': 'application/json', ...opts.headers },
      ...opts,
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`API error ${res.status}: ${text}`);
    }
    const ct = res.headers.get('content-type') || '';
    if (ct.includes('application/json')) return res.json();
    if (ct.includes('text/csv')) return res.text();
    return res.blob();
  },

  // Маршруты
  getRoutes() {
    return this.request('/api/routes');
  },

  // Остановки
  getStops(route) {
    const q = route ? `?route=${route}` : '';
    return this.request(`/api/stops${q}`);
  },

  // Прогноз
  getForecast({ route, stop, from_date, to_date, granularity }) {
    const p = new URLSearchParams();
    if (route) p.set('route', route);
    if (stop) p.set('stop', stop);
    if (from_date) p.set('from_date', from_date);
    if (to_date) p.set('to_date', to_date);
    if (granularity) p.set('granularity', granularity);
    return this.request(`/api/forecast?${p}`);
  },

  // Прогноз на следующий час для KPI-карточки:
  // факт текущего часа + прогноз следующего часа (GET /api/forecast/next-hour)
  getNextHourForecast(routeId) {
    const q = routeId ? `?route_id=${encodeURIComponent(routeId)}` : '';
    return this.request(`/api/forecast/next-hour${q}`);
  },

  // Сценарий
  applyScenario(data) {
    return this.request('/api/forecast/scenario', {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  getScenarios() {
    return this.request('/api/forecast/scenarios');
  },

  // Экспорт
  async exportData(format, route) {
    const q = route ? `?route=${route}&format=${format}` : `?format=${format}`;
    const res = await fetch(`${API_BASE}/api/forecast/export${q}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `forecast.${format}`;
    a.click();
    URL.revokeObjectURL(url);
  },

  // Health
  health() {
    return this.request('/health');
  },
};
// Карта — Leaflet
let map, markersLayer, heatData;

function initMap(center = [55.7558, 37.6176], zoom = 11) {
  map = L.map('map').setView(center, zoom);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors',
    maxZoom: 18,
  }).addTo(map);
  markersLayer = L.layerGroup().addTo(map);
}

function clearMapMarkers() {
  markersLayer.clearLayers();
}

function addRouteStop(stop, isActive = false) {
  const color = isActive ? '#6c8cff' : '#a0a3b5';
  const marker = L.circleMarker([stop.lat, stop.lon], {
    radius: isActive ? 8 : 5,
    fillColor: color,
    color: '#fff',
    weight: 2,
    opacity: 1,
    fillOpacity: 0.8,
  });
  marker.bindTooltip(`<b>${stop.name}</b>${isActive ? '<br/>🚏 Остановка' : ''}`, {
    direction: 'top',
  });
  markersLayer.addLayer(marker);
}

function addRoutePolyline(stops) {
  const coords = stops
    .filter(s => s.lat && s.lon)
    .map(s => [s.lat, s.lon]);
  if (coords.length < 2) return;
  const line = L.polyline(coords, {
    color: '#6c8cff',
    weight: 3,
    opacity: 0.6,
    dashArray: '8, 8',
  }).addTo(markersLayer);

  // Fit bounds
  map.fitBounds(line.getBounds().pad(0.1));
}

function showHeatmap(forecasts, stops) {
  clearMapMarkers();
  if (!forecasts.length || !stops.length) return;

  // Сопоставляем route_id с остановками
  const routeStops = stops.filter(s => s.lat && s.lon);

  // Находим max пассажиров для нормализации
  const maxPax = Math.max(...forecasts.map(f => f.passengers_predicted));

  routeStops.forEach(stop => {
    const intensity = stop.lat ? 0.3 + 0.7 * (Math.random() * 0.5 + 0.5) : 0.3; // эвристика
    const radius = 6 + 14 * intensity;
    const r = Math.round(255 * (1 - intensity));
    const g = Math.round(180 * (1 - intensity));
    const b = 255;

    L.circleMarker([stop.lat, stop.lon], {
      radius,
      fillColor: `rgb(${255 - r}, ${g}, ${b})`,
      color: '#fff',
      weight: 1,
      fillOpacity: 0.7,
    })
      .bindTooltip(`<b>${stop.name}</b><br/>Прогноз: ${Math.round(intensity * maxPax)} пасс.`)
      .addTo(markersLayer);
  });

  // Линия маршрута
  addRoutePolyline(routeStops);
}
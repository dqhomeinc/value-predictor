// Renders the property location on an OpenStreetMap tile layer via
// Leaflet. Free, no API key — see the plan's data-source notes. Only
// runs when window.PROPERTY_MAP_DATA is set, i.e. RentCast returned
// coordinates for this property.
document.addEventListener('DOMContentLoaded', () => {
  const mapEl = document.getElementById('property-map');
  if (!mapEl || !window.PROPERTY_MAP_DATA || typeof L === 'undefined') return;

  const { lat, lng } = window.PROPERTY_MAP_DATA;

  const map = L.map('property-map').setView([lat, lng], 17);

  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);

  L.marker([lat, lng]).addTo(map);
});

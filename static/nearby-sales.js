// Client-side radius filter for the "Recent sales nearby" list. Zero
// network calls: all comps within NEARBY_SALES_MAX_RADIUS_MILES (see
// integrations/rentcast.py) are already fetched and embedded in the page,
// same pattern as the deal calculator's live recompute.
document.addEventListener('DOMContentLoaded', () => {
  const dataEl = document.getElementById('nearby-sales-data');
  const listEl = document.getElementById('nearby-sales-list');
  const emptyEl = document.getElementById('nearby-sales-empty');
  const buttons = document.querySelectorAll('.radius-btn');
  if (!dataEl || !listEl || !emptyEl || buttons.length === 0) return;

  const comps = JSON.parse(dataEl.textContent) || [];

  const currency = (value) =>
    value.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });

  function render(radius) {
    const withinRadius = comps
      .filter((comp) => typeof comp.distance === 'number' && comp.distance <= radius)
      .sort((a, b) => a.distance - b.distance);

    // Built via DOM methods + textContent rather than innerHTML — comp
    // fields (address, etc.) come from RentCast, but there's no reason to
    // trust externally-sourced strings inside HTML when textContent is
    // just as easy and can't be broken out of.
    listEl.textContent = '';
    emptyEl.hidden = withinRadius.length > 0;

    for (const comp of withinRadius) {
      const li = document.createElement('li');

      const addressEl = document.createElement('strong');
      addressEl.textContent = comp.formattedAddress || 'Address unavailable';
      li.appendChild(addressEl);

      const beds = comp.bedrooms ?? '—';
      const baths = comp.bathrooms ?? '—';
      const sqft = comp.squareFootage ?? '—';
      const price = typeof comp.price === 'number' ? currency(comp.price) : 'price unavailable';
      const distance = comp.distance.toFixed(1);

      const detailsEl = document.createElement('div');
      detailsEl.className = 'muted';
      detailsEl.textContent = `${beds} bd / ${baths} ba / ${sqft} sqft — ${price} — ${distance} mi away`;
      li.appendChild(detailsEl);

      listEl.appendChild(li);
    }
  }

  function selectButton(selected) {
    buttons.forEach((b) => {
      const isSelected = b === selected;
      b.classList.toggle('active', isSelected);
      b.setAttribute('aria-pressed', isSelected);
    });
  }

  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      selectButton(button);
      render(parseInt(button.dataset.radius, 10));
    });
  });

  // Default to the smallest radius — closest, most relevant comps first.
  selectButton(buttons[0]);
  render(parseInt(buttons[0].dataset.radius, 10));
});

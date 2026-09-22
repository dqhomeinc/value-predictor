// Progress for the on-demand build-limits search (services/build_limits_search.py).
// The search is a normal form POST that can take up to a minute, so this
// only disables the button and says what's happening; the results arrive
// with the next page load.
document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('build-limits-form');
  if (!form) return;

  form.addEventListener('submit', () => {
    const button = form.querySelector('button[type="submit"]');
    const hint = form.querySelector('.limits-search-hint');
    button.disabled = true;
    button.textContent = 'Searching…';
    if (hint) {
      hint.textContent = `Reading ${form.dataset.jurisdiction}'s zoning code. This can take up to a minute, so keep this page open.`;
    }
  });
});

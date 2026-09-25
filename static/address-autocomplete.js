// Address suggestions on the analyze form (blueprints/main.py's
// /addresses/suggest, integrations/address_suggest.py).
//
// Progressive: without JavaScript, or when the provider has nothing, the
// field is the plain text input it always was. Suggestions are inserted as
// text, never HTML, since they come from OpenStreetMap contributors.
document.addEventListener('DOMContentLoaded', () => {
  const input = document.getElementById('address');
  const list = document.getElementById('address-suggestions');
  if (!input || !list || !input.dataset.suggestUrl) return;

  const MIN_CHARS = 3;
  const PAUSE_MS = 250;
  let timer = null;
  let inFlight = null;
  let options = [];
  let active = -1;
  let lastPicked = '';

  function close() {
    list.hidden = true;
    list.replaceChildren();
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
    options = [];
    active = -1;
  }

  function highlight(index) {
    if (!options.length) return;
    active = (index + options.length) % options.length;
    options.forEach((option, i) => option.setAttribute('aria-selected', String(i === active)));
    input.setAttribute('aria-activedescendant', options[active].id);
    options[active].scrollIntoView({ block: 'nearest' });
  }

  function pick(address) {
    input.value = address;
    lastPicked = address;
    close();
    input.focus();
  }

  function render(suggestions) {
    list.replaceChildren();
    options = suggestions.map((suggestion, index) => {
      const option = document.createElement('li');
      option.className = 'address-suggestion';
      option.id = `address-suggestion-${index}`;
      option.setAttribute('role', 'option');
      option.setAttribute('aria-selected', 'false');

      const text = document.createElement('span');
      text.textContent = suggestion.address;
      option.appendChild(text);
      if (suggestion.source === 'history') {
        const tag = document.createElement('span');
        tag.className = 'address-suggestion-tag';
        tag.textContent = 'Analyzed before';
        option.appendChild(tag);
      }
      // mousedown, not click: blur would close the list first.
      option.addEventListener('mousedown', (event) => {
        event.preventDefault();
        pick(suggestion.address);
      });
      list.appendChild(option);
      return option;
    });
    list.hidden = options.length === 0;
    input.setAttribute('aria-expanded', String(options.length > 0));
  }

  async function search(query) {
    if (inFlight) inFlight.abort();
    inFlight = new AbortController();
    try {
      const response = await fetch(`${input.dataset.suggestUrl}?q=${encodeURIComponent(query)}`, {
        signal: inFlight.signal,
        headers: { Accept: 'application/json' },
      });
      if (!response.ok) return close();
      const data = await response.json();
      // Ignore an answer to typing that has moved on.
      if (input.value.trim() !== query) return;
      render(Array.isArray(data.suggestions) ? data.suggestions : []);
    } catch (error) {
      if (error.name !== 'AbortError') close();
    }
  }

  input.addEventListener('input', () => {
    const query = input.value.trim();
    clearTimeout(timer);
    if (query.length < MIN_CHARS || query === lastPicked) return close();
    timer = setTimeout(() => search(query), PAUSE_MS);
  });

  input.addEventListener('keydown', (event) => {
    if (list.hidden) return;
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      highlight(active + 1);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      highlight(active - 1);
    } else if (event.key === 'Enter' && active >= 0) {
      // Only steals Enter when a suggestion is highlighted; otherwise the
      // form submits as usual.
      event.preventDefault();
      pick(options[active].firstChild.textContent);
    } else if (event.key === 'Escape' || event.key === 'Tab') {
      close();
    }
  });

  input.addEventListener('blur', () => setTimeout(close, 120));
  document.addEventListener('click', (event) => {
    if (event.target !== input && !list.contains(event.target)) close();
  });
});

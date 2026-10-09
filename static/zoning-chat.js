// The build-restrictions chat on the results page (services/zoning_chat.py).
// Replies are inserted as text, never as HTML: they come from a model that
// may have read arbitrary web pages. Source links are limited to http(s).
document.addEventListener('DOMContentLoaded', () => {
  const root = document.getElementById('zoning-chat');
  const form = document.getElementById('chat-form');
  if (!root || !form) return;

  const log = document.getElementById('chat-log');
  const input = document.getElementById('chat-input');
  const button = form.querySelector('button[type="submit"]');
  const status = document.getElementById('chat-status');
  const suggestions = root.querySelector('.chat-suggestions');
  let busy = false;

  function safeUrl(value) {
    try {
      const url = new URL(value);
      return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : '';
    } catch {
      return '';
    }
  }

  function addMessage(role, text, sources) {
    const item = document.createElement('li');
    item.className = `chat-message chat-${role}`;
    const body = document.createElement('div');
    body.className = 'chat-text';
    body.textContent = text;
    item.appendChild(body);

    const links = (sources || []).filter((source) => safeUrl(source.url));
    if (links.length) {
      const list = document.createElement('ul');
      list.className = 'chat-sources';
      for (const source of links) {
        const link = document.createElement('a');
        link.href = safeUrl(source.url);
        link.target = '_blank';
        link.rel = 'noopener';
        link.textContent = source.title || link.href;
        const entry = document.createElement('li');
        entry.appendChild(link);
        list.appendChild(entry);
      }
      item.appendChild(list);
    }
    log.appendChild(item);
    item.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    return item;
  }

  function setStatus(message) {
    status.textContent = message || '';
    status.hidden = !message;
  }

  async function send(question) {
    if (busy || !question) return;
    busy = true;
    button.disabled = true;
    input.value = '';
    if (suggestions) suggestions.hidden = true;
    const pending = addMessage('user', question);
    setStatus('Thinking… answers that need a web search can take up to 20 seconds.');

    try {
      const response = await fetch(root.dataset.chatUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': root.dataset.csrfToken },
        body: JSON.stringify({ message: question }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.reply) {
        throw new Error(data.error || "Couldn't get an answer right now. Please try again in a moment.");
      }
      addMessage('assistant', data.reply, data.sources);
      setStatus('');
    } catch (error) {
      // Nothing was saved, so take the question back out of the log and
      // put it back in the box for another try.
      pending.remove();
      input.value = question;
      if (suggestions && !log.querySelector('.chat-message')) suggestions.hidden = false;
      setStatus(error instanceof TypeError ? "Couldn't reach the server. Check your connection and try again." : error.message);
    } finally {
      busy = false;
      button.disabled = false;
      input.focus();
    }
  }

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    send(input.value.trim());
  });

  // Enter sends; Shift+Enter adds a new line.
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  if (suggestions) {
    suggestions.addEventListener('click', (event) => {
      const suggestion = event.target.closest('.chat-suggestion');
      if (suggestion) send(suggestion.textContent.trim());
    });
  }
});

// Visual feedback for the RentCast round trip on submit (a few seconds) —
// the form is a normal synchronous POST, this just makes the wait legible.
document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('analyze-form');
  const submitButton = document.getElementById('analyze-submit');
  const loadingMessage = document.getElementById('analyze-loading');
  if (!form || !submitButton || !loadingMessage) return;

  form.addEventListener('submit', () => {
    submitButton.disabled = true;
    submitButton.textContent = 'Analyzing…';
    loadingMessage.hidden = false;
  });
});

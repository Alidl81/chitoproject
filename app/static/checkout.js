const form = document.getElementById('order-access');
if (form && window.location.hash.length > 1) {
  const input = document.getElementById('order-token');
  input.value = window.location.hash.slice(1);
  window.history.replaceState(null, '', window.location.pathname);
  form.requestSubmit();
}

const copy = document.getElementById('copy-password');
if (copy && navigator.clipboard) {
  copy.hidden = false;
  copy.addEventListener('click', async () => {
    const password = document.getElementById('archive-password').textContent;
    const status = document.getElementById('copy-status');
    try {
      await navigator.clipboard.writeText(password);
      status.textContent = 'رمز کپی شد.';
    } catch {
      status.textContent = 'رمز رو از روی صفحه انتخاب و کپی کن.';
    }
  });
}

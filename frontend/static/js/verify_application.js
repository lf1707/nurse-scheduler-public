async function verifyApplication() {
  const token = new URLSearchParams(window.location.search).get('token');
  const state = document.getElementById('verify-state');
  const error = document.getElementById('verify-error');
  if (!token) { state.textContent = '缺少验证链接'; return; }
  try {
    const response = await fetch('/api/v1/tenant-applications/verify', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token}),
    });
    const body = await response.json().catch(() => ({}));
    const detail = body.detail;
    const detailMessage = typeof detail === 'string' ? detail : '验证失败';
    if (!response.ok) throw new Error(detailMessage);
    state.textContent = body.message || '邮箱验证完成，等待平台审核。';
  } catch (exception) {
    state.textContent = '验证未完成';
    error.textContent = exception.message;
    error.style.display = 'block';
  }
}
function initializeVerifyApplicationPage() {
  const state = document.getElementById('verify-state');
  if (!state || state.dataset.initialized) return;
  state.dataset.initialized = '1';
  verifyApplication();
}

initializeVerifyApplicationPage();
document.addEventListener('htmx:afterSettle', initializeVerifyApplicationPage);

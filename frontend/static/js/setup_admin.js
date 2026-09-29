async function setupAdminSubmit(event) {
  event.preventDefault();
  const error = document.getElementById('setup-error');
  const success = document.getElementById('setup-success');
  error.style.display = 'none'; success.style.display = 'none';
  const token = new URLSearchParams(window.location.search).get('token');
  const password = document.getElementById('password').value;
  const confirm = document.getElementById('password-confirm').value;
  if (!token || password !== confirm) {
    error.textContent = password !== confirm ? '两次输入的密码不一致' : '缺少设置链接';
    error.style.display = 'block'; return;
  }
  try {
    const response = await fetch('/api/v1/tenant-applications/setup', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token, new_password: password}),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || '设置失败');
    success.textContent = body.message || '帐号已激活';
    success.style.display = 'block';
    setTimeout(() => location.href = '/pages/login', 1200);
  } catch (exception) {
    error.textContent = exception.message; error.style.display = 'block';
  }
}

function initializeSetupAdminPage() {
  const form = document.getElementById('setup-form');
  if (!form || form.dataset.initialized) return;
  form.dataset.initialized = '1';
  form.addEventListener('submit', setupAdminSubmit);
}

initializeSetupAdminPage();
document.addEventListener('htmx:afterSettle', initializeSetupAdminPage);

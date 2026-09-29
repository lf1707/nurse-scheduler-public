async function loginSubmit(e) {
  e.preventDefault();
  const err = document.getElementById('login-error');
  err.style.display = 'none';
  const email = document.getElementById('email').value;
  const password = document.getElementById('password').value;
  const totpField = document.getElementById('mfa-field');
  const totpInput = document.getElementById('totp_code');
  const recoveryField = document.getElementById('recovery-field');
  const recoveryInput = document.getElementById('recovery_code');
  const payload = {email, password};
  if (totpField.style.display !== 'none' && totpInput.value) {
    payload.totp_code = totpInput.value;
  }
  if (recoveryField.style.display !== 'none' && recoveryInput.value) {
    payload.recovery_codes = [recoveryInput.value];
  }
  try {
    const r = await fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      if (r.status === 428) {
        totpField.style.display = 'block';
        recoveryField.style.display = 'block';
        totpInput.focus();
        throw new Error('请输入 MFA 验证码');
      }
      throw new Error(d.detail === 'Invalid email or password'
        ? '邮箱或密码错'
        : d.detail || '登录失败');
    }
    const {recovery_codes: recoveryCodes} = await r.json();
    const session = await fetch('/api/v1/auth/me');
    if (!session.ok) {
      throw new Error('登录 Cookie 未写入。请清除 localhost 的站点数据，或改用 http://127.0.0.1:9000 访问 dev。');
    }
    if (Array.isArray(recoveryCodes) && recoveryCodes.length) {
      sessionStorage.setItem('new_mfa_recovery_codes', JSON.stringify(recoveryCodes));
    }
    location.href = Array.isArray(recoveryCodes) && recoveryCodes.length
      ? '/pages/profile'
      : '/pages/dashboard';
  } catch (ex) {
    err.textContent = ex.message;
    err.style.display = 'block';
  }
}

function initializeLoginPage() {
  const form = document.getElementById('login-form');
  if (!form || form.dataset.initialized) return;
  form.dataset.initialized = '1';
  form.addEventListener('submit', loginSubmit);
}

initializeLoginPage();
document.addEventListener('htmx:afterSettle', initializeLoginPage);

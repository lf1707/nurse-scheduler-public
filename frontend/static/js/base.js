function syncServerUserRole() {
  const serverRole = document.body.dataset.userRole;
  if (serverRole) {
    localStorage.setItem('user_role', serverRole);
  } else {
    localStorage.removeItem('user_role');
  }
}

syncServerUserRole();

// Cookie sessions never expose tokens to JavaScript; CSRF uses double submit.
window.authHeaders = function () {
  let csrf = '';
  try {
    csrf = document.cookie.split('; ').find((row) => row.startsWith('ns_csrf='))?.split('=')[1] || '';
  } catch (_) { /* document.cookie is unavailable in locked-down embeds */ }
  return csrf ? {'X-CSRF-Token': decodeURIComponent(csrf)} : {};
};

document.addEventListener('htmx:configRequest', (e) => {
  Object.assign(e.detail.headers, window.authHeaders());
});

// Bootstrap requests can fire several times during one boosted-page lifecycle,
// so deduplicate concurrent calls in memory. Do not persist the result across
// navigations: cookie sessions rotate identities during impersonation, and
// HttpOnly tokens are unavailable to JavaScript for a cache fingerprint.
(function () {
  const inflight = new Map();

  async function dedupeInflight(name, loader) {
    if (inflight.has(name)) return inflight.get(name);
    const request = loader().finally(() => inflight.delete(name));
    inflight.set(name, request);
    return request;
  }

  window.fetchMeCached = function () {
    return dedupeInflight('me', async () => {
      const response = await fetch('/api/v1/auth/me', {
      });
      if (!response.ok) throw new Error(`auth/me failed: ${response.status}`);
      return response.json();
    });
  };

  window.fetchNavCached = function (loggedIn) {
    const role = localStorage.getItem('user_role') || '';
    const appVersion = document.body.dataset.appVersion || 'unknown';
    return dedupeInflight(
      `${appVersion}:nav:${loggedIn ? 1 : 0}:${role}`,
      async () => {
        const response = await fetch(
          '/pages/nav' + (loggedIn ? '?logged_in=1' : ''),
        );
        if (!response.ok) throw new Error(`pages/nav failed: ${response.status}`);
        return response.text();
      },
    );
  };
})();

// Session expiry (sliding/idle window): intercept every fetch to
// /api/v1/*. On 401, try to exchange the refresh_token for a new
// access_token and retry once — so active use never logs you out. Only
// when the refresh itself fails (idle longer than the refresh-token TTL)
// do we clear the session and redirect to login. The login endpoint is
// excluded so the login page can surface bad-credentials inline.
(function () {
  const origFetch = window.fetch.bind(window);
  let refreshing = null;
  function doRefresh() {
    if (refreshing) return refreshing;
    refreshing = origFetch('/api/v1/auth/refresh', {
      method: 'POST',
      headers: Object.assign({'Content-Type': 'application/json'}, window.authHeaders()),
      body: JSON.stringify({}),
    }).then((r) => r.ok)
      .finally(() => { refreshing = null; });
    return refreshing;
  }
  function bailToLogin() {
    ['user_role', 'user_display', 'impersonator_name']
      .forEach((k) => localStorage.removeItem(k));
    if (location.pathname !== '/pages/login') location.href = '/pages/login';
  }
  function bailToMfaEnrollment() {
    if (location.pathname !== '/pages/profile') location.href = '/pages/profile';
  }
  window.fetch = async function (input, init) {
    init = init || {};
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const isApi = url.includes('/api/v1/');
    const isAuthEndpoint = url.includes('/api/v1/auth/login');
    if (isApi && !(init.method || 'GET').toUpperCase().match(/^(?:GET|HEAD|OPTIONS|TRACE)$/)) {
      init = Object.assign({}, init, {
        headers: Object.assign({}, init.headers || {}, window.authHeaders()),
      });
    }
    const r = await origFetch(input, init);
    if (
      r.status === 403 &&
      isApi &&
      !init.__mfaRedirected &&
      typeof r.clone === 'function'
    ) {
      const cloned = r.clone();
      const body = await cloned.json().catch(() => ({}));
      if (body?.detail?.reason === 'mfa_enrollment_required') {
        init.__mfaRedirected = true;
        bailToMfaEnrollment();
      }
    }
    if (r.status === 401 && isApi && !isAuthEndpoint && !init.__retried) {
      const ok = await doRefresh();
      if (ok) {
        init = Object.assign({}, init, {
          __retried: true,
        });
        return origFetch(input, init);
      }
      bailToLogin();
    }
    return r;
  };
})();
// Render the nav links based on login state, and (when logged in) show
// the current user + tenant in the navbar. Runs after DOMContentLoaded
// and again after every htmx boost navigation (htmx:afterSettle), since
// boost swaps <main> and the <head> IIFE does not re-run.
async function refreshNav() {
  const nav = document.getElementById('nav-links');
  let loggedIn = false;
  try {
    const u = await fetchMeCached();
    loggedIn = true;
    localStorage.setItem('user_role', u.role);
    localStorage.setItem('user_display', u.tenant_name
      ? `${u.first_name}${u.last_name} (${u.tenant_name})`
      : `${u.first_name}${u.last_name}`);
    const el = document.getElementById('nav-user');
    if (el) el.textContent = localStorage.getItem('user_display');
  } catch (_) {
    ['user_role', 'user_display'].forEach((key) => localStorage.removeItem(key));
  }
  if (nav) {
    nav.innerHTML = await fetchNavCached(loggedIn);
    const isSuper = localStorage.getItem('user_role') === 'super_admin';
    for (const id of ['nav-tenants', 'nav-tenant-applications', 'nav-admin']) {
      const el = document.getElementById(id);
      if (el) el.style.display = isSuper ? '' : 'none';
    }
    // Show the masquerade banner while an impersonator token is saved.
    const banner = document.getElementById('impersonate-banner');
    if (banner) banner.style.display = localStorage.getItem('impersonator_name') ? '' : 'none';
  }
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', refreshNav);
} else {
  refreshNav();
}
document.addEventListener('htmx:afterSettle', refreshNav);
document.addEventListener('click', (event) => {
  const navMenu = document.getElementById('nav-links');
  if (!navMenu) return;
  if (!navMenu.contains(event.target)) {
    for (const menu of navMenu.querySelectorAll('details[open]')) {
      menu.open = false;
    }
    return;
  }
  const summary = event.target.closest('summary');
  if (summary) {
    for (const menu of navMenu.querySelectorAll('details[open]')) {
      if (!menu.contains(summary)) menu.open = false;
    }
    return;
  }
  const link = event.target.closest('a');
  if (!link || link.id === 'nav-logout') return;
  for (const menu of navMenu.querySelectorAll('details[open]')) {
    if (menu.contains(link)) menu.open = false;
  }
});
// Logout link: revoke this device family, clear local state, then redirect.
document.addEventListener('click', (e) => {
  const a = e.target.closest('#nav-logout');
  if (!a) return;
  e.preventDefault();
  const clearSession = () => {
    ['user_role', 'user_display', 'impersonator_name'].forEach((k) => localStorage.removeItem(k));
    try {
      sessionStorage.clear();
    } catch (_) { /* storage may be unavailable in private modes */ }
    location.href = '/pages/login';
  };
  fetch('/api/v1/auth/logout', {
    method: 'POST',
    headers: window.authHeaders(),
    body: JSON.stringify({}),
    keepalive: true,
  }).catch(() => {}).finally(clearSession);
});
// "Exit masquerade" banner link: restore the saved super-admin token.
document.addEventListener('click', (e) => {
  const link = e.target.closest('#exit-impersonate-link');
  if (!link) return;
  e.preventDefault();
  fetch('/api/v1/auth/impersonation-exit', {
    method: 'POST',
    headers: window.authHeaders(),
  }).then(() => {
    localStorage.removeItem('impersonator_name');
    location.href = '/pages/admin';
  });
});

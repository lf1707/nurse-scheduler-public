#!/usr/bin/env python3
"""Browser regression for HttpOnly sessions, CSRF, refresh, and impersonation."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
import time
import uuid

import httpx
from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:9000").rstrip("/")
SUPER_EMAIL = os.environ["SUPER_ADMIN_EMAIL"]
SUPER_PASSWORD = os.environ["SUPER_ADMIN_PASSWORD"]
MFA_SECRET = os.environ.get("SUPER_ADMIN_TEST_MFA_SECRET")
VERIFY_TLS = os.environ.get("E2E_VERIFY_TLS", "1").lower() not in {"0", "false"}
BROWSER_ENGINE = os.environ.get("E2E_BROWSER", "chromium").lower()


def _totp(secret: str) -> str:
    key = base64.b32decode(secret, casefold=True)
    counter = int(time.time()) // 30
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def main() -> None:
    bootstrap = httpx.post(
        f"{BASE_URL}/api/v1/auth/login",
        json={
            "email": SUPER_EMAIL,
            "password": SUPER_PASSWORD,
            **({"totp_code": _totp(MFA_SECRET)} if MFA_SECRET else {}),
        },
        verify=VERIFY_TLS,
    )
    bootstrap.raise_for_status()
    super_token = bootstrap.json()["access_token"]

    slug = f"cookie-ui-{uuid.uuid4().hex[:10]}"
    tenant = httpx.post(
        f"{BASE_URL}/api/v1/tenants",
        headers=_headers(super_token),
        json={"name": f"Cookie UI {slug}", "slug": slug},
        verify=VERIFY_TLS,
    )
    tenant.raise_for_status()
    tenant_id = tenant.json()["id"]
    target_email = f"admin@{slug}.example.com"
    httpx.post(
        f"{BASE_URL}/api/v1/tenants/{tenant_id}/admin",
        headers=_headers(super_token),
        json={
            "email": target_email,
            "password": "password123",
            "first_name": "Cookie",
            "last_name": "Ui",
            "role": "tenant_admin",
        },
        verify=VERIFY_TLS,
    ).raise_for_status()

    try:
        with sync_playwright() as playwright:
            browser = getattr(playwright, BROWSER_ENGINE).launch(headless=True)
            context = browser.new_context(ignore_https_errors=not VERIFY_TLS)
            page = context.new_page()
            page.goto(f"{BASE_URL}/pages/login", wait_until="domcontentloaded")
            page.locator("#email").fill(SUPER_EMAIL)
            page.locator("#password").fill(SUPER_PASSWORD)
            page.get_by_role("button", name="登录").click()
            if MFA_SECRET:
                page.locator("#mfa-field").wait_for(state="visible")
                page.locator("#totp_code").fill(_totp(MFA_SECRET))
                page.get_by_role("button", name="登录").click()
            page.wait_for_url("**/pages/dashboard", wait_until="domcontentloaded")

            cookies = {cookie["name"]: cookie for cookie in context.cookies()}
            assert cookies["ns_access"]["httpOnly"] is True
            assert cookies["ns_refresh"]["httpOnly"] is True
            assert cookies["ns_csrf"]["httpOnly"] is False
            if BASE_URL.startswith("https://"):
                assert cookies["ns_access"]["secure"] is True
                assert cookies["ns_refresh"]["secure"] is True
            local_storage = page.evaluate("() => ({...localStorage})")
            assert not any(key.endswith("_token") for key in local_storage)
            page.wait_for_function(
                "() => document.querySelector('#nav-user')?.innerText.trim()",
                timeout=5000,
            )
            nav_html = page.locator("#nav-links").inner_html()
            assert 'href="/pages/roles"' in nav_html
            assert "角色管理" in nav_html
            assert 'href="/pages/skills"' in nav_html
            assert "技能管理" in nav_html
            assert 'href="/pages/tenants"' in nav_html
            assert "租户管理" in nav_html
            nurse_menu = page.locator("#nav-links summary", has_text="护士")
            nurse_menu.click()
            assert page.locator("#nav-links a[href='/pages/roles']").is_visible()
            assert page.locator("#nav-links a[href='/pages/skills']").is_visible()

            tenant_menu = page.locator("#nav-links summary", has_text="租户")
            tenant_menu.click()
            assert page.locator("#nav-links a[href='/pages/tenants']").is_visible()
            assert page.locator("#nav-links a[href='/pages/users']").is_visible()
            page.screenshot(path="/tmp/nurse-nav-ui.png", full_page=True)

            page.goto(f"{BASE_URL}/pages/nurses", wait_until="domcontentloaded")
            page.wait_for_function(
                "() => document.querySelector('#nav-user')?.innerText.trim()",
                timeout=5000,
            )
            nurse_tenant_selector = page.locator("select").first
            assert nurse_tenant_selector.is_visible()
            page.wait_for_function(
                "() => document.querySelector('select')?.options.length > 1",
                timeout=5000,
            )

            page.goto(f"{BASE_URL}/pages/shifts", wait_until="domcontentloaded")
            page.wait_for_function(
                "() => document.querySelector('#nav-user')?.innerText.trim()",
                timeout=5000,
            )
            shift_tenant_selector = page.locator("select").first
            assert shift_tenant_selector.is_visible()
            page.wait_for_function(
                "() => document.querySelector('select')?.options.length > 1",
                timeout=5000,
            )

            page.goto(f"{BASE_URL}/pages/rules", wait_until="domcontentloaded")
            page.wait_for_function(
                "() => document.querySelector('#nav-user')?.innerText.trim()",
                timeout=5000,
            )
            rule_tenant_selector = page.locator("select").first
            assert rule_tenant_selector.is_visible()
            page.wait_for_function(
                "() => document.querySelector('select')?.options.length > 1",
                timeout=5000,
            )

            for path in ("/pages/roles", "/pages/skills"):
                page.goto(f"{BASE_URL}{path}", wait_until="domcontentloaded")
                page.wait_for_function(
                    "() => document.querySelector('#nav-user')?.innerText.trim()",
                    timeout=5000,
                )
                selector = page.locator("select").first
                assert selector.is_visible()
                page.wait_for_function(
                    "() => document.querySelector('select')?.options.length > 1",
                    timeout=5000,
                )

            csrf = cookies["ns_csrf"]["value"]
            blocked = context.request.post(
                f"{BASE_URL}/api/v1/auth/refresh",
                headers={"X-CSRF-Token": "invalid"},
                data="{}",
            )
            assert blocked.status == 403, blocked.text()
            refreshed = context.request.post(
                f"{BASE_URL}/api/v1/auth/refresh",
                headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
                data="{}",
            )
            assert refreshed.status == 200, refreshed.text()
            csrf = next(
                cookie["value"]
                for cookie in context.cookies()
                if cookie["name"] == "ns_csrf"
            )

            target_user = httpx.get(
                f"{BASE_URL}/api/v1/tenants/{tenant_id}/admin",
                headers=_headers(super_token),
                verify=VERIFY_TLS,
            )
            target_user.raise_for_status()
            target_id = target_user.json()["id"]
            impersonated = context.request.post(
                f"{BASE_URL}/api/v1/auth/impersonate/{target_id}",
                headers={"X-CSRF-Token": csrf},
            )
            assert impersonated.status == 200, impersonated.text()
            me_impersonated = context.request.get(f"{BASE_URL}/api/v1/auth/me")
            assert me_impersonated.status == 200
            assert me_impersonated.json()["email"] == target_email
            page.goto(f"{BASE_URL}/pages/dashboard", wait_until="domcontentloaded")
            page.wait_for_function(
                "() => document.querySelector('#nav-user')?.innerText.trim()",
                timeout=5000,
            )
            impersonated_nav = page.locator("#nav-links").inner_html()
            assert 'href="/pages/roles"' in impersonated_nav
            assert 'href="/pages/skills"' in impersonated_nav
            assert 'href="/pages/tenants"' not in impersonated_nav
            csrf = next(
                cookie["value"]
                for cookie in context.cookies()
                if cookie["name"] == "ns_csrf"
            )

            restored = context.request.post(
                f"{BASE_URL}/api/v1/auth/impersonation-exit",
                headers={"X-CSRF-Token": csrf},
            )
            assert restored.status == 200, restored.text()
            me_restored = context.request.get(f"{BASE_URL}/api/v1/auth/me")
            assert me_restored.status == 200
            assert me_restored.json()["email"] == SUPER_EMAIL
            page.goto(f"{BASE_URL}/pages/admin", wait_until="domcontentloaded")
            page.wait_for_function(
                "() => document.querySelector('#nav-user')?.innerText.trim()",
                timeout=5000,
            )
            restored_nav = page.locator("#nav-links").inner_html()
            assert 'href="/pages/roles"' in restored_nav
            assert 'href="/pages/skills"' in restored_nav
            assert 'href="/pages/tenants"' in restored_nav
            csrf = next(
                cookie["value"]
                for cookie in context.cookies()
                if cookie["name"] == "ns_csrf"
            )

            logged_out = context.request.post(
                f"{BASE_URL}/api/v1/auth/logout",
                headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
                data="{}",
            )
            assert logged_out.status == 204, (logged_out.status, logged_out.text())
            assert context.request.get(f"{BASE_URL}/api/v1/auth/me").status == 401
            browser.close()
    finally:
        deleted = httpx.delete(
            f"{BASE_URL}/api/v1/tenants/{tenant_id}",
            headers=_headers(super_token),
            verify=VERIFY_TLS,
        )
        assert deleted.status_code == 204, deleted.text

    print("cookie-session-ui: ok")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Browser regression for the super-admin manual invoice UI."""

from __future__ import annotations

import json
import os
import time
import uuid

import httpx
from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:9000").rstrip("/")
SUPER_EMAIL = os.environ["SUPER_ADMIN_EMAIL"]
SUPER_PASSWORD = os.environ["SUPER_ADMIN_PASSWORD"]
MFA_SECRET = os.environ.get("SUPER_ADMIN_TEST_MFA_SECRET")
VERIFY_TLS = os.environ.get("E2E_VERIFY_TLS", "1").lower() not in {"0", "false"}


def totp(secret: str) -> str:
    import base64
    import hashlib
    import hmac
    import struct

    key = base64.b32decode(secret, casefold=True)
    counter = int(time.time()) // 30
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def main() -> None:
    bootstrap = httpx.post(
        f"{BASE_URL}/api/v1/auth/login",
        json={
            "email": SUPER_EMAIL,
            "password": SUPER_PASSWORD,
            **({"totp_code": totp(MFA_SECRET)} if MFA_SECRET else {}),
        },
        verify=VERIFY_TLS,
    )
    bootstrap.raise_for_status()
    token = bootstrap.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    slug = f"billing-ui-{uuid.uuid4().hex[:10]}"
    tenant = httpx.post(
        f"{BASE_URL}/api/v1/tenants",
        headers=headers,
        json={"name": f"Billing UI {slug}", "slug": slug},
        verify=VERIFY_TLS,
    )
    if tenant.status_code != 201:
        raise AssertionError((tenant.status_code, tenant.text))
    tenant_id = tenant.json()["id"]
    number = f"UI-{int(time.time())}"
    invoice = httpx.post(
        f"{BASE_URL}/api/v1/billing/invoices",
        headers={**headers, "Content-Type": "application/json"},
        json={
            "tenant_id": tenant_id,
            "number": number,
            "amount_minor": 12345,
            "currency": "USD",
            "notes": "browser smoke",
        },
        verify=VERIFY_TLS,
    )
    invoice.raise_for_status()

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(ignore_https_errors=not VERIFY_TLS)
            page = context.new_page()
            console_messages: list[str] = []
            page.on("console", lambda message: console_messages.append(message.text))
            failed_api_requests: list[str] = []
            page.on(
                "response",
                lambda response: failed_api_requests.append(
                    f"{response.status} {response.url}"
                )
                if "/api/v1/" in response.url and response.status >= 400
                else None,
            )
            browser_login = context.request.post(
                f"{BASE_URL}/api/v1/auth/login",
                headers={"Content-Type": "application/json"},
                data=json.dumps(
                    {
                        "email": SUPER_EMAIL,
                        "password": SUPER_PASSWORD,
                        **({"totp_code": totp(MFA_SECRET)} if MFA_SECRET else {}),
                    }
                ),
            )
            assert browser_login.status == 200, browser_login.text()
            page.goto(f"{BASE_URL}/pages/dashboard", wait_until="domcontentloaded")
            page.wait_for_selector("#nav-user")
            assert page.evaluate("typeof billingPage") == "function"

            try:
                tenant_menu = page.locator("#nav-links summary", has_text="租户")
                tenant_menu.click()
                billing_link = page.locator("#nav-links a[href='/pages/billing']")
                assert billing_link.is_visible()
                billing_link.click()
                page.wait_for_url("**/pages/billing", wait_until="domcontentloaded")
                toolbar = page.locator(".filter-bar")
                create_button = page.get_by_role("button", name="登记账单")
                assert toolbar.is_visible()
                assert create_button.is_visible()
                create_button.click()
                page.wait_for_selector("form")
                period_inputs = page.locator("form input[type='date']")
                assert period_inputs.nth(0).get_attribute("required") is not None
                assert period_inputs.nth(1).get_attribute("required") is not None
                page.get_by_role("button", name="取消登记").click()
                page.wait_for_selector("form", state="hidden")
                toolbar_box = toolbar.bounding_box()
                button_box = create_button.bounding_box()
                assert toolbar_box and button_box
                assert button_box["x"] > toolbar_box["x"] + toolbar_box["width"] / 2
                button_center = button_box["y"] + button_box["height"] / 2
                assert toolbar_box["y"] <= button_center
                assert button_center <= toolbar_box["y"] + toolbar_box["height"]
                page.wait_for_selector("table")
                page.wait_for_selector(f"text={number}")
                row = page.locator("tr", has=page.get_by_text(number))
                assert "USD 123.45" in row.inner_text()
                assert "待付款" in row.inner_text()
                assert "Billing UI" in row.inner_text()
            except Exception:
                page.screenshot(path="/tmp/billing-ui-failure.png", full_page=True)
                print(page.locator("main").inner_text())
                print(console_messages)
                print(failed_api_requests)
                raise
    finally:
        deleted = httpx.delete(
            f"{BASE_URL}/api/v1/tenants/{tenant_id}",
            headers=headers,
            verify=VERIFY_TLS,
        )
        assert deleted.status_code == 204, deleted.text


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Regression test for editing a tenant-scoped shift sequence rule."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import time
import uuid
from urllib.parse import urlparse

import httpx
from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:9000").rstrip("/")
EMAIL = os.environ["SUPER_ADMIN_EMAIL"]
PASSWORD = os.environ["SUPER_ADMIN_PASSWORD"]
MFA_SECRET = os.environ.get("SUPER_ADMIN_TEST_MFA_SECRET")
VERIFY_TLS = os.environ.get("E2E_VERIFY_TLS", "1").lower() not in {"0", "false"}
HTTP = httpx.Client(verify=VERIFY_TLS)


def totp_code(secret: str) -> str:
    key = base64.b32decode(secret, casefold=True)
    counter = int(time.time()) // 30
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def _assert_ok(response, label: str):
    if not response.ok:
        raise AssertionError(f"{label} failed: {response.status} {response.text()}")
    return response


def _json_ok(response, label: str):
    _assert_ok(response, label)
    return response.json()


def _cookie_headers(login_response: httpx.Response) -> dict[str, str]:
    csrf = login_response.cookies.get("ns_csrf")
    if not csrf:
        raise AssertionError("login did not issue ns_csrf")
    return {"X-CSRF-Token": csrf, "Content-Type": "application/json"}


def _install_browser_session(context, login_response: httpx.Response) -> None:
    """Transfer an API login's HttpOnly session into a Playwright context."""
    domain = urlparse(BASE_URL).hostname or "127.0.0.1"
    secure = BASE_URL.startswith("https://")
    cookies = [
        {"name": "ns_access", "value": login_response.cookies.get("ns_access") or ""},
        {"name": "ns_refresh", "value": login_response.cookies.get("ns_refresh") or ""},
        {"name": "ns_csrf", "value": login_response.cookies.get("ns_csrf") or ""},
    ]
    if any(not cookie["value"] for cookie in cookies):
        raise AssertionError(f"login did not issue a complete session: {cookies}")
    context.add_cookies([
        {
            **cookie,
            "domain": domain,
            "path": "/",
            "secure": secure,
            "httpOnly": cookie["name"] != "ns_csrf",
            "sameSite": "Lax",
        }
        for cookie in cookies
    ])


def _login(context, email: str, password: str) -> httpx.Response:
    payload: dict[str, str] = {"email": email, "password": password}
    if MFA_SECRET:
        payload["totp_code"] = totp_code(MFA_SECRET)
    login = HTTP.post(f"{BASE_URL}/api/v1/auth/login", json=payload)
    if login.status_code >= 400:
        raise AssertionError(f"login failed: {login.status_code} {login.text}")
    login.raise_for_status()
    _install_browser_session(context, login)
    return login


def _delete_with_login(url: str, login_response: httpx.Response):
    return HTTP.delete(
        url,
        headers=_cookie_headers(login_response),
        cookies={
            "ns_access": login_response.cookies.get("ns_access", ""),
            "ns_refresh": login_response.cookies.get("ns_refresh", ""),
            "ns_csrf": login_response.cookies.get("ns_csrf", ""),
        },
    )


def main() -> None:
    rule = None
    tenant_id = None
    tenant_context = None
    tenant_login = None
    super_context = None
    super_login = None
    rule_name = f"e2e-sequence-{uuid.uuid4().hex[:10]}"

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            super_context = browser.new_context(
                ignore_https_errors=not VERIFY_TLS,
            )
            super_login = _login(super_context, EMAIL, PASSWORD)
            super_headers = _cookie_headers(super_login)

            slug = f"e2e-seq-{uuid.uuid4().hex[:10]}"
            tenant = _json_ok(
                super_context.request.post(
                    f"{BASE_URL}/api/v1/tenants",
                    headers=super_headers,
                    data=json.dumps({"name": f"Sequence {slug}", "slug": slug}),
                ),
                "tenant create",
            )
            tenant_id = tenant["id"]
            admin_email = f"admin@{slug}.example.com"
            _assert_ok(
                super_context.request.post(
                    f"{BASE_URL}/api/v1/tenants/{tenant_id}/admin",
                    headers=super_headers,
                    data=json.dumps({
                        "email": admin_email,
                        "password": "password123",
                        "first_name": "Sequence",
                        "last_name": "Ui",
                        "role": "tenant_admin",
                    }),
                ),
                "tenant admin create",
            )
            _assert_ok(
                super_context.request.put(
                    f"{BASE_URL}/api/v1/subscriptions/{tenant_id}",
                    headers=super_headers,
                    data=json.dumps({"plan": "max"}),
                ),
                "subscription update",
            )

            tenant_context = browser.new_context(
                ignore_https_errors=not VERIFY_TLS,
            )
            tenant_login = _login(tenant_context, admin_email, "password123")
            headers = _cookie_headers(tenant_login)

            _json_ok(
                tenant_context.request.post(
                    f"{BASE_URL}/api/v1/roles",
                    headers=headers,
                    data=json.dumps({"name": "RN", "code": "RN"}),
                ),
                "role create",
            )
            day_group = _json_ok(
                tenant_context.request.post(
                    f"{BASE_URL}/api/v1/day-groups",
                    headers=headers,
                    data=json.dumps({
                        "name": "All week",
                        "day_numbers": list(range(1, 8)),
                    }),
                ),
                "day group create",
            )
            shifts = {}
            for code, name, start, end in (
                ("N", "Night", "00:00", "08:00"),
                ("E", "Early", "08:00", "16:00"),
            ):
                shifts[code] = _json_ok(
                    tenant_context.request.post(
                        f"{BASE_URL}/api/v1/shift-templates",
                        headers=headers,
                        data=json.dumps({
                            "code": code,
                            "name": name,
                            "start_time": start,
                            "end_time": end,
                            "duration_hours": 8,
                            "day_group_id": day_group["id"],
                        }),
                    ),
                    f"{code} shift create",
                )

            night = shifts["N"]
            early = shifts["E"]
            rule = _json_ok(
                tenant_context.request.post(
                    f"{BASE_URL}/api/v1/shift-sequence-rules",
                    headers=headers,
                    data=json.dumps({
                        "name": rule_name,
                        "description": "Playwright regression rule",
                        "steps": [
                            {"position": 0, "shift_template_id": night["id"]},
                            {"position": 1, "shift_template_id": early["id"]},
                        ],
                        "role_ids": [],
                    }),
                ),
                "sequence rule create",
            )

            page_errors: list[str] = []
            console_errors: list[str] = []
            page = tenant_context.new_page()
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on(
                "console",
                lambda message: console_errors.append(message.text)
                if message.type == "error"
                else None,
            )

            page.goto(f"{BASE_URL}/pages/rules", wait_until="domcontentloaded")
            row = page.locator(
                "[data-testid='sequence-section'] tr", has_text=rule_name
            )
            row.wait_for(state="visible", timeout=10000)
            try:
                edit = row.get_by_role("button", name="编辑")
                edit.wait_for(state="visible", timeout=5000)
                edit.click()
            except Exception:
                page.screenshot(path="/tmp/sequence-rule-e2e.png", full_page=True)
                print(page.locator("[data-testid='sequence-section']").inner_text())
                raise
            modal = page.locator("[data-testid='sequence-modal']")
            modal.wait_for(state="visible")

            assert (
                modal.locator("[data-testid='sequence-name']").input_value()
                == rule_name
            )
            assert (
                modal.locator("[data-testid='sequence-description']").input_value()
                == "Playwright regression rule"
            )
            selects = modal.locator("[data-testid='sequence-step-select']")
            assert selects.count() == 2
            assert selects.nth(0).input_value() == night["id"]
            assert selects.nth(1).input_value() == early["id"]
            assert selects.nth(0).evaluate(
                "node => node.selectedOptions[0].textContent"
            ) == f"{night['code']} {night['name']}"
            assert selects.nth(1).evaluate(
                "node => node.selectedOptions[0].textContent"
            ) == f"{early['code']} {early['name']}"

            assert not page_errors, page_errors
            assert not [
                error for error in console_errors if "[Alpine CSP]" in error
            ], console_errors
    finally:
        if rule and tenant_context and tenant_login:
            deleted = _delete_with_login(
                f"{BASE_URL}/api/v1/shift-sequence-rules/{rule['id']}",
                tenant_login,
            )
            if deleted.status_code not in {200, 204, 404}:
                raise AssertionError(
                    f"rule cleanup failed: {deleted.status_code} {deleted.text}"
                )
        if tenant_id and super_context and super_login:
            deleted = _delete_with_login(
                f"{BASE_URL}/api/v1/tenants/{tenant_id}",
                super_login,
            )
            if deleted.status_code not in {200, 204, 404}:
                raise AssertionError(
                    f"tenant cleanup failed: {deleted.status_code} {deleted.text}"
                )

    print("shift-sequence-edit-modal: ok")


if __name__ == "__main__":
    main()

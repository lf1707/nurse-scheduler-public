#!/usr/bin/env python3
"""Regression test for schedule-edit saving, errors, and button recovery."""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import os
import secrets
import struct
import time
import uuid
from urllib.parse import urlparse

import httpx
from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:9000").rstrip("/")
SUPER_EMAIL = os.environ["SUPER_ADMIN_EMAIL"]
SUPER_PASSWORD = os.environ["SUPER_ADMIN_PASSWORD"]
MFA_SECRET = os.environ.get("SUPER_ADMIN_TEST_MFA_SECRET")
VERIFY_TLS = os.environ.get("E2E_VERIFY_TLS", "1").lower() not in {"0", "false"}
HTTP = httpx.Client(verify=VERIFY_TLS)


def _totp(secret: str) -> str:
    key = base64.b32decode(secret, casefold=True)
    counter = int(time.time()) // 30
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _install_browser_session(context: object, login_response: httpx.Response) -> None:
    """Transfer an API login's HttpOnly session into a Playwright context."""
    domain = urlparse(BASE_URL).hostname or "127.0.0.1"
    cookies = [
        {"name": "ns_access", "value": login_response.cookies.get("ns_access") or ""},
        {"name": "ns_refresh", "value": login_response.cookies.get("ns_refresh") or ""},
        {
            "name": "ns_csrf",
            "value": login_response.cookies.get("ns_csrf") or secrets.token_urlsafe(32),
        },
    ]
    context.add_cookies([
        {**cookie, "domain": domain, "path": "/", "secure": False, "httpOnly": cookie["name"] != "ns_csrf", "sameSite": "Lax"}
        for cookie in cookies
    ])
def main() -> None:
    login_payload = {"email": SUPER_EMAIL, "password": SUPER_PASSWORD}
    if MFA_SECRET:
        login_payload["totp_code"] = _totp(MFA_SECRET)
    super_login = HTTP.post(
        f"{BASE_URL}/api/v1/auth/login",
        json=login_payload,
    )
    super_login.raise_for_status()
    super_token = super_login.json()["access_token"]

    slug = f"e2e-edit-{uuid.uuid4().hex[:10]}"
    tenant = HTTP.post(
        f"{BASE_URL}/api/v1/tenants",
        headers=_headers(super_token),
        json={"name": f"Schedule Edit {slug}", "slug": slug},
    )
    tenant.raise_for_status()
    tenant_id = tenant.json()["id"]
    email = f"admin@{slug}.example.com"
    admin = HTTP.post(
        f"{BASE_URL}/api/v1/tenants/{tenant_id}/admin",
        headers=_headers(super_token),
        json={
            "email": email,
            "password": "password123",
            "first_name": "Edit",
            "last_name": "Ui",
            "role": "tenant_admin",
        },
    )
    admin.raise_for_status()
    login = HTTP.post(
        f"{BASE_URL}/api/v1/auth/login",
        json={"email": email, "password": "password123"},
    )
    login.raise_for_status()
    token = login.json()["access_token"]
    headers = _headers(token)

    try:
        HTTP.put(
            f"{BASE_URL}/api/v1/subscriptions/{tenant_id}",
            headers=_headers(super_token),
            json={"plan": "max"},
        ).raise_for_status()
        role = HTTP.post(
            f"{BASE_URL}/api/v1/roles", headers=headers, json={"name": "RN", "code": "RN"}
        )
        role.raise_for_status()
        role_id = role.json()["id"]
        day_group = HTTP.post(
            f"{BASE_URL}/api/v1/day-groups",
            headers=headers,
            json={"name": "All week", "day_numbers": list(range(1, 8))},
        )
        day_group.raise_for_status()
        day_group_id = day_group.json()["id"]
        shifts = {}
        for code, name, start, end in (
            ("D", "Day", "08:00", "16:00"),
            ("E", "Evening", "12:00", "20:00"),
        ):
            shift = HTTP.post(
                f"{BASE_URL}/api/v1/shift-templates",
                headers=headers,
                json={
                    "code": code,
                    "name": name,
                    "start_time": start,
                    "end_time": end,
                    "duration_hours": 8,
                    "day_group_id": day_group_id,
                },
            )
            shift.raise_for_status()
            shifts[code] = shift.json()
        for index in range(4):
            nurse = HTTP.post(
                f"{BASE_URL}/api/v1/nurses",
                headers=headers,
                json={
                    "employee_id": f"E2E-{index}",
                    "first_name": f"N{index}",
                    "last_name": "Edit",
                    "is_available": True,
                    "role_ids": [role_id],
                    "contract": {
                        "shifts_per_period": 1,
                        "max_shifts_per_period": 2,
                        "min_rest_hours": 11,
                        "max_consecutive_days": 5,
                        "enforce_balanced": False,
                        "enforce_shifts_per_period": True,
                        "enforce_one_shift_per_day": True,
                    },
                },
            )
            nurse.raise_for_status()

        generated = HTTP.post(
            f"{BASE_URL}/api/v1/schedules/generate",
            headers=headers,
            json={
                "period_start": datetime.date.today().isoformat(),
                "period_days": 7,
                "solver_config": {"timeout_seconds": 30, "num_workers": 2},
            },
        )
        generated.raise_for_status()
        request_id = generated.json()["id"]
        request_display_id = generated.json()["display_id"]
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            request = HTTP.get(
                f"{BASE_URL}/api/v1/schedules/{request_id}", headers=headers
            )
            request.raise_for_status()
            if request.json()["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.5)
        assert request.json()["status"] == "completed", request.text
        result = HTTP.get(
            f"{BASE_URL}/api/v1/schedules/{request_id}/result", headers=headers
        )
        result.raise_for_status()
        assignments = result.json()["assignments"]
        assert assignments, result.text
        first = assignments[0]
        other = next(
            assignment for assignment in assignments[1:]
            if assignment["nurse_id"] != first["nurse_id"]
            and assignment["date"] != first["date"]
        )
        first_shift_code = first["shift_template_code"]
        bump_shift_code = next(
            code for code in shifts if code != first_shift_code
        )
        bump_shift_id = shifts[bump_shift_code]["id"]
        bump = HTTP.patch(
            f"{BASE_URL}/api/v1/schedules/{request_id}/assignments",
            headers=headers,
            json={
                "base_version": 1,
                "operations": [{
                    "action": "add",
                    "nurse_id": other["nurse_id"],
                    "date": first["date"],
                    "shift_template_id": bump_shift_id,
                    "role_id": first["role_id"],
                }],
            },
        )
        assert bump.status_code == 200, bump.text

        skill_rule = HTTP.post(
            f"{BASE_URL}/api/v1/skill-mix-rules",
            headers=headers,
            json={
                "name": "UI override regression",
                "shift_template_id": first["shift_template_id"],
                "priority": 1,
                "requirements": [{"role_id": first["role_id"], "count": 2}],
            },
        )
        skill_rule.raise_for_status()

        page_errors: list[str] = []
        console_errors: list[str] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(ignore_https_errors=not VERIFY_TLS)
            _install_browser_session(context, login)
            page = context.new_page()
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on(
                "console",
                lambda message: console_errors.append(message.text)
                if message.type == "error"
                else None,
            )

            save_attempts = 0
            first_date = first["date"]
            first_shift_id = first["shift_template_id"]

            def delay_save(route):
                nonlocal save_attempts
                save_attempts += 1
                page.wait_for_timeout(400)
                if save_attempts == 1:
                    route.fulfill(
                        status=422,
                        content_type="application/json",
                        body=(
                            '{"detail":[{"code":"override_required",'
                            '"soft_violations":[{"code":"skill_mix_not_met",'
                            '"message":"技能配比不满足",'
                            f'"date":"{first_date}",'
                            f'"shift_template_id":"{first_shift_id}"'
                            "}]}]}"
                        ),
                    )
                else:
                    response = route.fetch()
                    route.fulfill(response=response)

            page.route("**/api/v1/schedules/*/assignments", delay_save)
            page.goto(
                f"{BASE_URL}/pages/schedules/{request_id}",
                wait_until="domcontentloaded",
            )
            save = page.get_by_test_id("schedule-save")
            page.get_by_role("button", name="编辑").click()
            selects = page.locator("tbody select")
            select = selects.first
            for index in range(selects.count()):
                candidate = selects.nth(index)
                if candidate.input_value():
                    select = candidate
                    break
            select.wait_for(state="visible", timeout=10000)
            selected_code = select.input_value()
            other_code = next(code for code in shifts if code != selected_code)
            select.select_option(other_code)
            save.click()
            page.wait_for_function(
                "button => button.disabled", arg=save.element_handle(), timeout=2000
            )
            assert save.text_content().strip() == "保存中…"
            errors = page.get_by_test_id("schedule-edit-errors")
            errors.wait_for(state="visible", timeout=5000)
            page.wait_for_function(
                "element => element.textContent.includes('技能配比不满足')"
                " && !element.textContent.includes('{')",
                arg=errors.element_handle(),
                timeout=5000,
            )
            error_text = errors.text_content()
            assert "技能配比不满足" in error_text
            assert first["date"] in error_text
            assert "{" not in error_text
            override_input = errors.get_by_placeholder("例如：人手不足，临时调整")
            assert override_input.is_visible()
            assert select.is_visible()
            assert save.is_enabled()
            assert save.text_content().strip() == "保存"
            override_input.fill("Approved by charge nurse")
            save.click()
            try:
                page.get_by_role("button", name="编辑").wait_for(
                    state="visible", timeout=5000
                )
            except Exception:
                page.screenshot(path="/tmp/schedule-edit-e2e.png", full_page=True)
                print("page_errors:", page_errors)
                print("console_errors:", console_errors)
                print(
                    "save:",
                    save.text_content(),
                    "disabled:",
                    save.is_disabled(),
                )
                print("edit_count:", page.get_by_role("button", name="编辑").count())
                raise
            errors.wait_for(state="hidden", timeout=5000)

            page.goto(f"{BASE_URL}/pages/schedules", wait_until="domcontentloaded")
            request_row = page.locator("tr", has_text=request_display_id)
            active_indicator = request_row.get_by_test_id(
                "schedule-active-version-current"
            )
            active_indicator.wait_for(
                state="visible",
                timeout=10000,
            )
            detail_response = HTTP.get(
                f"{BASE_URL}/api/v1/schedules/{request_id}", headers=headers
            )
            active_version = detail_response.json()["active_version"]
            assert active_indicator.text_content() == (
                f"当前选择 V{active_version}"
            )
            active_save = request_row.get_by_test_id(
                "schedule-active-version-save"
            )
            assert active_save.is_disabled()
            request_row.get_by_role("button", name="V1", exact=True).click()
            assert active_save.is_enabled()
            active_save.click()
            page.wait_for_function(
                "element => element.textContent === '保存' && element.disabled",
                arg=active_save.element_handle(),
                timeout=5000,
            )
            assert active_indicator.text_content() == "当前选择 V1"

            page.goto(
                f"{BASE_URL}/pages/schedules/{request_id}?version={active_version}",
                wait_until="domcontentloaded",
            )
            page.wait_for_selector(".version-chip", timeout=10000)
            current_chip = page.locator(".version-chip.active")
            effective_chip = page.locator(".version-chip.effective")
            assert f"V{active_version}" in current_chip.text_content()
            assert "当前" in current_chip.text_content()
            assert "V1" in effective_chip.text_content()
            assert "生效" in effective_chip.text_content()
            history_text = page.locator(
                f"text=正在查看 V{active_version} 历史版本"
            ).text_content()
            active_text = page.locator(
                f"text=当前浏览 V{active_version}；生效版本为 V1"
            ).text_content()
            assert "仅可查看和下载" in history_text
            assert "生效版本为 V1" in active_text

            HTTP.put(
                f"{BASE_URL}/api/v1/schedules/{request_id}/active-version",
                headers=headers,
                json={"version": active_version},
            ).raise_for_status()
            page.goto(
                f"{BASE_URL}/pages/schedules/{request_id}?version=2",
                wait_until="domcontentloaded",
            )
            delete_button = page.get_by_test_id("schedule-version-delete")
            delete_button.wait_for(state="visible", timeout=10000)
            effective_chip = page.locator(".version-chip.effective")
            assert f"V{active_version}" in effective_chip.text_content()
            page.once("dialog", lambda dialog: dialog.accept())
            delete_button.click()
            delete_button.wait_for(state="hidden", timeout=10000)
            versions_response = HTTP.get(
                f"{BASE_URL}/api/v1/schedules/{request_id}/versions",
                headers=headers,
            )
            versions_response.raise_for_status()
            assert [item["version"] for item in versions_response.json()] == [
                active_version,
                1,
            ]
            page.wait_for_function(
                "() => document.querySelectorAll('.version-chip').length === 2",
                timeout=10000,
            )
            assert page.locator(".version-chip").count() == 2

            page.goto(
                f"{BASE_URL}/pages/schedules/{request_id}?version=1",
                wait_until="domcontentloaded",
            )
            delete_button = page.get_by_test_id("schedule-version-delete")
            delete_button.wait_for(state="visible", timeout=10000)
            page.once("dialog", lambda dialog: dialog.accept())
            delete_button.click()
            delete_button.wait_for(state="hidden", timeout=10000)
            versions_response = HTTP.get(
                f"{BASE_URL}/api/v1/schedules/{request_id}/versions",
                headers=headers,
            )
            versions_response.raise_for_status()
            assert [item["version"] for item in versions_response.json()] == [
                active_version
            ]
            page.wait_for_function(
                "() => document.querySelectorAll('.version-chip').length === 1",
                timeout=10000,
            )
            assert page.locator(".version-chip").count() == 1

            assert not page_errors, page_errors
            assert not [error for error in console_errors if "[Alpine CSP]" in error], (
                console_errors
            )
            browser.close()
    finally:
        deleted = HTTP.delete(
            f"{BASE_URL}/api/v1/tenants/{tenant_id}", headers=_headers(super_token)
        )
        assert deleted.status_code == 204, deleted.text

    print("schedule-edit-ui-states: ok")


if __name__ == "__main__":
    main()

"""TD-4 HttpOnly cookie session regression tests."""

from __future__ import annotations

import os

import pyotp
import pytest
from httpx import AsyncClient

from app.tests.conftest import SUPER_ADMIN_TEST_MFA_SECRET


def _csrf(client: AsyncClient) -> str:
    return client.cookies.get("ns_csrf") or ""


@pytest.mark.asyncio
async def test_httponly_cookie_login_refresh_logout(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        json={
            "email": "admin@example.com",
            "password": os.environ.get("SUPER_ADMIN_PASSWORD", "changeme123"),
            "totp_code": pyotp.TOTP(SUPER_ADMIN_TEST_MFA_SECRET).now(),
        },
    )
    assert response.status_code == 200, response.text
    session_headers = response.headers.get_list("set-cookie")
    assert any("ns_access=" in header and "httponly" in header.lower() for header in session_headers)
    assert any("ns_refresh=" in header and "httponly" in header.lower() for header in session_headers)
    csrf_cookie = next(header for header in session_headers if "ns_csrf=" in header)
    assert "httponly" not in csrf_cookie.lower()

    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "admin@example.com"

    nav = await client.get("/pages/nav?logged_in=1")
    assert nav.status_code == 200
    assert "角色管理" in nav.text
    assert "技能管理" in nav.text
    assert "租户管理" in nav.text

    nurses_page = await client.get("/pages/nurses")
    assert nurses_page.status_code == 200
    assert 'data-user-role="super_admin"' in nurses_page.text

    blocked = await client.post("/api/v1/auth/refresh", json={})
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "CSRF token missing or invalid"
    assert client.cookies.get("ns_refresh")

    refreshed = await client.post(
        "/api/v1/auth/refresh",
        json={},
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["access_token"]

    me_after_refresh = await client.get("/api/v1/auth/me")
    assert me_after_refresh.status_code == 200

    logged_out = await client.post(
        "/api/v1/auth/logout",
        json={},
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert logged_out.status_code == 204
    assert "ns_access" not in client.cookies
    assert "ns_refresh" not in client.cookies

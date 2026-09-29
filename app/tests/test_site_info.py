"""Integration tests for super-admin editable About-page content."""

from __future__ import annotations

import os
from collections.abc import Generator
from typing import cast

import httpx
import pyotp
import pytest

from app.tests.conftest import SUPER_ADMIN_TEST_MFA_SECRET

BASE = os.environ.get("API_BASE_URL", "http://localhost:8000")
API = f"{BASE}/api/v1"


@pytest.fixture(scope="module")
def super_token() -> str:
    response = httpx.post(
        f"{API}/auth/login",
        json={
            "email": "admin@example.com",
            "password": os.environ.get("SUPER_ADMIN_PASSWORD", "changeme123"),
            "totp_code": pyotp.TOTP(SUPER_ADMIN_TEST_MFA_SECRET).now(),
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload["access_token"], str)
    return payload["access_token"]


@pytest.fixture(scope="module")
def original_site_info(super_token: str) -> Generator[dict[str, object], None, None]:
    response = httpx.get(f"{API}/admin/site-info", headers=_auth(super_token))
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, dict)
    yield cast(dict[str, object], payload)
    restore = httpx.put(
        f"{API}/admin/site-info",
        headers=_auth(super_token),
        json=response.json(),
    )
    assert restore.status_code == 200


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _put_site_info(token: str, body: dict[str, object]) -> httpx.Response:
    return httpx.put(f"{API}/admin/site-info", headers=_auth(token), json=body)


def test_about_page_uses_default_content(
    super_token: str,
    original_site_info: dict[str, object],
) -> None:
    assert _put_site_info(super_token, {"contact_email": None, "about_text": None}).status_code == 200

    site_info = httpx.get(f"{API}/admin/site-info", headers=_auth(super_token))
    about = httpx.get(f"{BASE}/pages/about")
    footer = httpx.get(f"{BASE}/pages/login").text

    assert site_info.status_code == 200
    assert about.status_code == 200
    default_text = site_info.json()["about_text"]
    assert "面向医院护理团队的多租户智能排班系统" in default_text
    assert "https://github.com/lf1707/nurse-scheduler-public" in default_text
    assert "roster-wizard 是基于 Django 的单实例排班工具" in default_text
    assert "https://github.com/galojix/roster-wizard" in about.text
    assert default_text in about.text
    assert "联系方式" in about.text
    assert "开源协议" in about.text
    assert "MIT License" in about.text
    assert "https://github.com/lf1707/nurse-scheduler-public/blob/main/LICENSE" in about.text
    assert "https://developers.google.com/optimization/scheduling/employee_scheduling?hl=zh-cn" in footer
    assert "https://github.com/galojix/roster-wizard" in footer
    assert "Fork from roster-wizard" in footer


def test_site_info_custom_text_round_trip_and_clear(
    super_token: str,
    original_site_info: dict[str, object],
) -> None:
    custom: dict[str, object] = {
        "contact_email": "contact@example.com",
        "about_text": "自定义项目介绍",
    }
    assert _put_site_info(super_token, custom).status_code == 200

    site_info = httpx.get(f"{API}/admin/site-info", headers=_auth(super_token))
    about = httpx.get(f"{BASE}/pages/about")
    assert site_info.json() == custom
    assert "自定义项目介绍" in about.text

    assert _put_site_info(super_token, {"contact_email": None, "about_text": None}).status_code == 200
    cleared = httpx.get(f"{API}/admin/site-info", headers=_auth(super_token)).json()
    assert cleared["contact_email"] is None
    assert "自定义项目介绍" not in cleared["about_text"]
    assert "https://github.com/lf1707/nurse-scheduler-public" in cleared["about_text"]

"""Stage 5 end-to-end API test: full generate flow via REST and polling.

Walks a tenant admin through: create config → generate → poll → fetch result,
all over HTTP (no DB seeding). Verifies the REST API is wired end-to-end.
"""

from __future__ import annotations

import csv
import datetime
import os
import time
from collections.abc import Iterator

import httpx
import pyotp
import pytest

from app.tests.conftest import SUPER_ADMIN_TEST_MFA_SECRET

BASE = os.environ.get("API_BASE_URL", "http://localhost:8000")
SUPER_EMAIL = "admin@example.com"
SUPER_PASSWORD = os.environ.get("SUPER_ADMIN_PASSWORD", "changeme123")
ADMIN_PASSWORD = "password123"

PERIOD_START = datetime.date(2026, 11, 1)
PERIOD_DAYS = 14
N_RN, N_LV = 28, 14
RN_PER, LV_PER = 4, 2
TARGET = 4


@pytest.fixture(scope="module")
def super_token() -> str:
    r = httpx.post(
        f"{BASE}/api/v1/auth/login",
        json={
            "email": SUPER_EMAIL,
            "password": SUPER_PASSWORD,
            "totp_code": pyotp.TOTP(SUPER_ADMIN_TEST_MFA_SECRET).now(),
        },
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert isinstance(payload["access_token"], str)
    return payload["access_token"]


@pytest.fixture(scope="module")
def tenant_and_admin(super_token: str) -> Iterator[tuple[str, str]]:
    """Create a unique tenant + its admin, return (tenant_id, admin_token)."""
    slug = f"e2e-api-{int(time.time())}"
    r = httpx.post(
        f"{BASE}/api/v1/tenants",
        headers={"Authorization": f"Bearer {super_token}"},
        json={"name": f"E2E API {slug}", "slug": slug},
    )
    assert r.status_code in (201, 409), r.text
    if r.status_code == 201:
        tenant_id = r.json()["id"]
    else:
        tenants = httpx.get(
            f"{BASE}/api/v1/tenants", headers={"Authorization": f"Bearer {super_token}"}
        ).json()["items"]
        tenant_id = next(t["id"] for t in tenants if t["slug"] == slug)

    admin_email = f"admin@{slug}.com"
    r = httpx.post(
        f"{BASE}/api/v1/tenants/{tenant_id}/admin",
        headers={"Authorization": f"Bearer {super_token}"},
        json={"email": admin_email, "password": ADMIN_PASSWORD, "first_name": "A", "last_name": "B",
              "role": "tenant_admin"},
    )
    assert r.status_code in (201, 409), r.text
    r = httpx.post(f"{BASE}/api/v1/auth/login", json={"email": admin_email, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    admin_token = r.json()["access_token"]

    # Active subscription so schedule generation passes the gate.
    r = httpx.put(
        f"{BASE}/api/v1/subscriptions/{tenant_id}",
        headers={"Authorization": f"Bearer {super_token}"},
        json={"plan": "max"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["is_active"] is True
    yield tenant_id, admin_token
    deleted = httpx.delete(
        f"{BASE}/api/v1/tenants/{tenant_id}",
        headers={"Authorization": f"Bearer {super_token}"},
    )
    assert deleted.status_code == 204, deleted.text


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_full_generate_flow(tenant_and_admin: tuple[str, str]) -> None:
    _tenant_id, admin_token = tenant_and_admin
    h = _h(admin_token)

    # 1. Role
    r = httpx.post(f"{BASE}/api/v1/roles", headers=h, json={"name": "RN", "code": "RN"})
    assert r.status_code == 201, r.text
    rn_id = r.json()["id"]
    r = httpx.post(f"{BASE}/api/v1/roles", headers=h, json={"name": "LV", "code": "LV"})
    assert r.status_code == 201, r.text
    lv_id = r.json()["id"]

    # 2. Day group
    r = httpx.post(f"{BASE}/api/v1/day-groups", headers=h,
                   json={"name": "All week", "day_numbers": [1, 2, 3, 4, 5, 6, 7]})
    assert r.status_code == 201, r.text
    dg_id = r.json()["id"]

    # 3. Shift templates
    r = httpx.post(f"{BASE}/api/v1/shift-templates", headers=h, json={
        "code": "E", "name": "Early", "start_time": "07:00", "end_time": "15:00",
        "duration_hours": 8.0, "day_group_id": dg_id,
    })
    assert r.status_code == 201, r.text
    shift_e = r.json()["id"]
    r = httpx.post(f"{BASE}/api/v1/shift-templates", headers=h, json={
        "code": "N", "name": "Night", "start_time": "15:00", "end_time": "23:00",
        "duration_hours": 8.0, "day_group_id": dg_id,
    })
    assert r.status_code == 201, r.text
    shift_n = r.json()["id"]

    # 4. Nurses with contracts + roles
    for i in range(N_RN + N_LV):
        role_id = rn_id if i < N_RN else lv_id
        r = httpx.post(f"{BASE}/api/v1/nurses", headers=h, json={
            "employee_id": f"E2E-{i:03d}", "first_name": f"J{i}", "last_name": "D",
            "is_available": True, "role_ids": [role_id],
            "contract": {"shifts_per_period": TARGET, "max_shifts_per_period": TARGET + 2,
                         "min_rest_hours": 11, "max_consecutive_days": 5,
                         "enforce_balanced": False, "enforce_shifts_per_period": True,
                         "enforce_one_shift_per_day": True},
        })
        assert r.status_code == 201, r.text

    # 5. Skill mix rules
    for sid in (shift_e, shift_n):
        r = httpx.post(f"{BASE}/api/v1/skill-mix-rules", headers=h, json={
            "name": f"{sid[:4]} mix", "shift_template_id": sid, "priority": 0,
            "requirements": [
                {"role_id": rn_id, "count": RN_PER},
                {"role_id": lv_id, "count": LV_PER},
            ],
        })
        assert r.status_code == 201, r.text

    # 6. Generate
    r = httpx.post(f"{BASE}/api/v1/schedules/generate", headers=h, json={
        "period_start": str(PERIOD_START), "period_days": PERIOD_DAYS,
        "solver_config": {"timeout_seconds": 60, "num_workers": 4},
    })
    assert r.status_code == 201, r.text
    request_id = r.json()["id"]
    assert r.json()["status"] == "pending"

    # 7. Poll until completed
    deadline = time.monotonic() + 90
    final = None
    while time.monotonic() < deadline:
        r = httpx.get(f"{BASE}/api/v1/schedules/{request_id}", headers=h)
        assert r.status_code == 200, r.text
        final = r.json()
        if final["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(1.5)
    assert final and final["status"] == "completed", f"unexpected: {final}"

    # 8. Fetch result + verify assignments
    r = httpx.get(f"{BASE}/api/v1/schedules/{request_id}/result", headers=h)
    assert r.status_code == 200, r.text
    sched = r.json()
    expected = PERIOD_DAYS * 2 * (RN_PER + LV_PER)
    assert len(sched["assignments"]) == expected, (
        f"expected {expected} assignments, got {len(sched['assignments'])}"
    )
    assert sched["outcome"] in ("optimal", "feasible")

    # 9. CSV export
    r = httpx.get(f"{BASE}/api/v1/schedules/{request_id}/export?format=csv", headers=h)
    assert r.status_code == 200, r.text
    assert "text/csv" in r.headers.get("content-type", "")
    # CSV now carries a title/metadata header block as leading "# " comment
    # rows + a blank row; the actual data table is the rest.
    data_rows = [
        row for row in csv.reader(r.text.splitlines())
        if row and not row[0].startswith("#")
    ]
    assert len(data_rows) == N_RN + N_LV + 1  # one row per nurse + header
    # Grid layout: one row per nurse + a "护士" header column; no raw IDs.
    assert data_rows[0][0] == "护士"
    assert "Early" in r.text

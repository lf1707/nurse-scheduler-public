"""RLS cross-tenant isolation tests — regression suite."""

import os
import time
import uuid
from collections.abc import AsyncGenerator
from typing import TypedDict

import pyotp
import pytest
from httpx import AsyncClient

from app.tests.conftest import SUPER_ADMIN_TEST_MFA_SECRET

UNIQ = f"rls{int(time.time())}{uuid.uuid4().hex[:6]}"


class TenantContext(TypedDict):
    tenant_id: str
    admin_token: str
    role_id: str


RLSContext = dict[str, TenantContext]


# ------------------------------------------------------------
# Module-level fixtures (shared across test classes)
# ------------------------------------------------------------
@pytest.fixture
async def super_token(client: AsyncClient) -> str:
    """Login as super-admin."""
    resp = await client.post(
        "/api/v1/auth/login",
        json={
            "email": "admin@example.com",
            "password": os.environ.get("SUPER_ADMIN_PASSWORD", "changeme123"),
            "totp_code": pyotp.TOTP(SUPER_ADMIN_TEST_MFA_SECRET).now(),
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert isinstance(payload["access_token"], str)
    return payload["access_token"]


@pytest.fixture(scope="module")
async def rls_context() -> AsyncGenerator[RLSContext, None]:
    """Create isolated tenants for this run and remove them afterward."""
    import os

    async with AsyncClient(base_url=os.environ.get("API_BASE_URL", "http://localhost:8000")) as client:
        super_resp = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "admin@example.com",
                "password": os.environ.get("SUPER_ADMIN_PASSWORD", "changeme123"),
                "totp_code": pyotp.TOTP(SUPER_ADMIN_TEST_MFA_SECRET).now(),
            },
        )
        assert super_resp.status_code == 200
        super_token = super_resp.json()["access_token"]
        assert isinstance(super_token, str)
        headers = {"Authorization": f"Bearer {super_token}"}

        context: RLSContext = {}
        for key, name, slug, admin_email in (
            ("a", "Tenant A", f"{UNIQ}-a", f"manager@{UNIQ}-a.example.com"),
            ("b", "Hospital B", f"{UNIQ}-b", f"manager@{UNIQ}-b.example.com"),
        ):
            created = await client.post(
                "/api/v1/tenants",
                headers=headers,
                json={"name": name, "slug": slug},
            )
            assert created.status_code == 201, created.text
            tenant_id = created.json()["id"]
            assert isinstance(tenant_id, str)
            admin = await client.post(
                f"/api/v1/tenants/{tenant_id}/admin",
                headers=headers,
                json={
                    "email": admin_email,
                    "password": "password123",
                    "first_name": "Manager",
                    "last_name": key.upper(),
                },
            )
            assert admin.status_code == 201, admin.text
            login = await client.post(
                "/api/v1/auth/login",
                json={"email": admin_email, "password": "password123"},
            )
            assert login.status_code == 200
            admin_token = login.json()["access_token"]
            assert isinstance(admin_token, str)
            role = await client.post(
                "/api/v1/roles",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"name": "RN", "code": "rn"},
            )
            assert role.status_code == 201, role.text
            context[key] = {
                "tenant_id": tenant_id,
                "admin_token": admin_token,
                "role_id": role.json()["id"],
            }

        yield context

        for item in context.values():
            await client.delete(f"/api/v1/tenants/{item['tenant_id']}", headers=headers)


@pytest.fixture
def tenant_a_id(rls_context: RLSContext) -> str:
    return rls_context["a"]["tenant_id"]


@pytest.fixture
def tenant_b_id(rls_context: RLSContext) -> str:
    return rls_context["b"]["tenant_id"]


class TestRLSIsolation:
    """Verify Row Level Security blocks cross-tenant access."""

    # ------------------------------------------------------------
    # Fixtures (function-scoped)
    # ------------------------------------------------------------

    @pytest.fixture
    async def admin_b_token(
        self, rls_context: RLSContext
    ) -> str:
        return rls_context["b"]["admin_token"]

    @pytest.fixture
    async def admin_a_token(
        self, rls_context: RLSContext
    ) -> str:
        return rls_context["a"]["admin_token"]

    # ------------------------------------------------------------
    # Cross-tenant isolation: tenant A admin cannot access tenant B
    # ------------------------------------------------------------
    async def test_admin_a_cannot_list_tenants(
        self, client: AsyncClient, admin_a_token: str
    ) -> None:
        """Non-super-admin cannot list all tenants."""
        resp = await client.get(
            "/api/v1/tenants",
            headers={"Authorization": f"Bearer {admin_a_token}"},
        )
        assert resp.status_code == 403

    async def test_admin_a_cannot_get_tenant_b(
        self, client: AsyncClient, admin_a_token: str, tenant_b_id: str
    ) -> None:
        """Tenant A admin cannot read tenant B details."""
        resp = await client.get(
            f"/api/v1/tenants/{tenant_b_id}",
            headers={"Authorization": f"Bearer {admin_a_token}"},
        )
        assert resp.status_code in (403, 404)

    async def test_admin_a_cannot_create_tenant_admin(
        self, client: AsyncClient, admin_a_token: str, tenant_b_id: str
    ) -> None:
        """Tenant A admin cannot use super-admin endpoint to bootstrap tenant B admin.

        The /tenants/{tenant_id}/admin endpoint is super-admin only; a tenant
        admin hitting it must be rejected with 403 (role guard), regardless of
        which tenant_id is in the path.
        """
        resp = await client.post(
            f"/api/v1/tenants/{tenant_b_id}/admin",
            headers={"Authorization": f"Bearer {admin_a_token}"},
            json={"email": "test@tenantb.com", "password": "password123", "first_name": "Test", "last_name": "User", "role": "tenant_admin"},
        )
        assert resp.status_code == 403

    async def test_admin_b_cannot_create_tenant_admin(
        self, client: AsyncClient, admin_b_token: str, tenant_b_id: str
    ) -> None:
        """Tenant B admin cannot use super-admin endpoint (role guard blocks)."""
        resp = await client.post(
            f"/api/v1/tenants/{tenant_b_id}/admin",
            headers={"Authorization": f"Bearer {admin_b_token}"},
            json={"email": "test@tenanta.com", "password": "password123", "first_name": "Test", "last_name": "User", "role": "tenant_admin"},
        )
        assert resp.status_code == 403

    # ------------------------------------------------------------
    # Same-tenant access: tenant admin CAN access own data
    # ------------------------------------------------------------
    async def test_admin_a_can_access_own_me(
        self, client: AsyncClient, rls_context: RLSContext, admin_a_token: str
    ) -> None:
        """Tenant A admin can access /auth/me (own user info)."""
        resp = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {admin_a_token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["email"] == f"manager@{UNIQ}-a.example.com"

    async def test_admin_b_can_access_own_me(
        self, client: AsyncClient, rls_context: RLSContext, admin_b_token: str
    ) -> None:
        """Tenant B admin can access /auth/me (own user info)."""
        resp = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {admin_b_token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["email"] == f"manager@{UNIQ}-b.example.com"

    async def test_admin_a_can_create_user_in_own_tenant(
        self, client: AsyncClient, admin_a_token: str
    ) -> None:
        """Tenant A admin can create user in own tenant."""
        email = f"newuser-a-{uuid.uuid4().hex[:8]}@test.com"
        resp = await client.post(
            "/api/v1/auth/users",
            headers={"Authorization": f"Bearer {admin_a_token}"},
            json={"email": email, "password": "password123", "first_name": "New", "last_name": "User", "role": "viewer"},
        )
        assert resp.status_code == 201
        assert resp.json()["email"] == email

    async def test_admin_b_can_create_user_in_own_tenant(
        self, client: AsyncClient, admin_b_token: str
    ) -> None:
        """Tenant B admin can create user in own tenant."""
        email = f"newuser-b-{uuid.uuid4().hex[:8]}@hospital-b.com"
        resp = await client.post(
            "/api/v1/auth/users",
            headers={"Authorization": f"Bearer {admin_b_token}"},
            json={"email": email, "password": "password123", "first_name": "New", "last_name": "User", "role": "viewer"},
        )
        assert resp.status_code == 201
        assert resp.json()["email"] == email

    # ------------------------------------------------------------
    # Super-admin CAN access all tenants
    # ------------------------------------------------------------
    async def test_super_admin_can_list_tenants(
        self, client: AsyncClient, super_token: str
    ) -> None:
        """Super-admin can list all tenants."""
        resp = await client.get(
            "/api/v1/tenants",
            headers={"Authorization": f"Bearer {super_token}"},
        )
        assert resp.status_code == 200
        assert isinstance(resp.json()["items"], list)

    async def test_super_admin_can_get_tenant_b(
        self, client: AsyncClient, super_token: str, tenant_b_id: str
    ) -> None:
        """Super-admin can read tenant B details."""
        resp = await client.get(
            f"/api/v1/tenants/{tenant_b_id}",
            headers={"Authorization": f"Bearer {super_token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == tenant_b_id

    async def test_super_admin_cannot_create_without_tenant(
        self, client: AsyncClient, super_token: str
    ) -> None:
        """super-admin has no tenant_id, so nurse/role/skill creation must 400
        (not crash with a 500 NotNull violation on the shared tenant_id column)."""
        for path, body in [
            ("/api/v1/nurses", {"employee_id": "SA-N", "first_name": "S", "last_name": "A", "role_ids": ["dummy"]}),
            ("/api/v1/roles", {"name": "SA Role", "code": "SAR"}),
            ("/api/v1/skills", {"name": "SA Skill", "code": "SAS"}),
        ]:
            resp = await client.post(path, headers={"Authorization": f"Bearer {super_token}"}, json=body)
            assert resp.status_code == 400, f"{path} -> {resp.status_code}: {resp.text}"
            assert "tenant" in resp.json()["detail"].lower()

    async def test_super_admin_can_create_nurse_in_tenant(
        self, client: AsyncClient, super_token: str, rls_context: RLSContext
    ) -> None:
        tenant_a_id = rls_context["a"]["tenant_id"]
        employee_id = f"SUPER-{uuid.uuid4().hex[:8]}"
        resp = await client.post(
            "/api/v1/nurses",
            params={"tenant_id": tenant_a_id},
            headers={"Authorization": f"Bearer {super_token}"},
            json={
                "employee_id": employee_id,
                "first_name": "Super",
                "last_name": "Created",
                "role_ids": [rls_context["a"]["role_id"]],
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["tenant_id"] == tenant_a_id

    async def test_duplicate_employee_id_rejected_within_tenant(
        self, client: AsyncClient, admin_a_token: str, rls_context: RLSContext
    ) -> None:
        """Two nurses in the same tenant cannot share an employee_id."""
        emp = f"EMP-{uuid.uuid4().hex[:8]}"
        rid = rls_context["a"]["role_id"]
        created = await client.post(
            "/api/v1/nurses",
            headers={"Authorization": f"Bearer {admin_a_token}"},
            json={"employee_id": emp, "first_name": "A", "last_name": "One", "role_ids": [rid]},
        )
        assert created.status_code == 201
        # Same code, different name -> rejected within the tenant.
        dup = await client.post(
            "/api/v1/nurses",
            headers={"Authorization": f"Bearer {admin_a_token}"},
            json={"employee_id": emp, "first_name": "B", "last_name": "Two", "role_ids": [rid]},
        )
        assert dup.status_code == 409
        assert "工号" in dup.json()["detail"]

    async def test_same_employee_id_allowed_across_tenants(
        self, client: AsyncClient, admin_a_token: str, admin_b_token: str, rls_context: RLSContext
    ) -> None:
        """The same employee_id is valid in different tenants."""
        emp = f"SHARED-{uuid.uuid4().hex[:8]}"
        a = await client.post(
            "/api/v1/nurses",
            headers={"Authorization": f"Bearer {admin_a_token}"},
            json={"employee_id": emp, "first_name": "A", "last_name": "TenA", "role_ids": [rls_context["a"]["role_id"]]},
        )
        assert a.status_code == 201
        b = await client.post(
            "/api/v1/nurses",
            headers={"Authorization": f"Bearer {admin_b_token}"},
            json={"employee_id": emp, "first_name": "B", "last_name": "TenB", "role_ids": [rls_context["b"]["role_id"]]},
        )
        assert b.status_code == 201
        assert b.json()["id"] != a.json()["id"]


# Run with: pytest -v app/tests/test_rls_isolation.py
